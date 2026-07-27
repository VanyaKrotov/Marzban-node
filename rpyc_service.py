import time
import json
import threading
from socket import socket
from threading import Thread

import rpyc

from certificate_service import certificate_manager
from config import XRAY_ASSETS_PATH, XRAY_EXECUTABLE_PATH
from geo_resource_service import geo_resource_manager
from logger import logger
from static_log_service import StaticLogError, static_log_manager
from xray import XRayConfig, XRayCore


class XrayCoreLogsHandler(object):
    def __init__(self, core: XRayCore, callback: callable, interval: float = 0.6):
        self.core = core
        self.callback = callback
        self.interval = interval
        self.active = True
        self.thread = Thread(target=self.cast)
        self.thread.start()

    def stop(self):
        self.active = False
        self.thread.join()

    def cast(self):
        with self.core.get_logs() as logs:
            cache = ''
            last_sent_ts = 0
            while self.active:
                if time.time() - last_sent_ts >= self.interval and cache:
                    self.callback(cache)
                    cache = ''
                    last_sent_ts = time.time()

                if not logs:
                    time.sleep(0.2)
                    continue

                log = logs.popleft()
                cache += f'{log}\n'


@rpyc.service
class XrayService(rpyc.Service):
    def __init__(self):
        self.core = None
        self.connection = None
        self.config = None
        self.log_settings = None
        self._rotation_timer = None
        self._rotation_lock = threading.Lock()
        self._geo_uploads = set()

    def on_connect(self, conn):
        if self.connection:
            try:
                self.connection.ping()
                if self.connection.peer is not None:
                    logger.warning(
                        f'New connection rejected, already connected to {self.connection.peer}')
                return conn.close()
            except (EOFError, TimeoutError, AttributeError):
                if hasattr(self.connection, "peer"):
                    logger.warning(
                        f'Previous connection from {self.connection.peer} has lost')

        peer, _ = socket.getpeername(conn._channel.stream.sock)
        self.connection = conn
        self.connection.peer = peer
        logger.warning(f'Connected to {self.connection.peer}')

    def on_disconnect(self, conn):
        if conn is self.connection:
            logger.warning(f'Disconnected from {self.connection.peer}')

            if self.core is not None:
                self.core.stop()

            self.core = None
            self.connection = None
            self._cancel_log_rotation()
            geo_resource_manager.abort_uploads(list(self._geo_uploads))
            self._geo_uploads.clear()

    @rpyc.exposed
    def start(self, config: str, log_settings: dict | None = None):
        if self.core is not None:
            self.stop()

        try:
            config = self._configure_static_logs(XRayConfig(config, self.connection.peer), log_settings)
            self.core = XRayCore(executable_path=XRAY_EXECUTABLE_PATH,
                                 assets_path=XRAY_ASSETS_PATH)

            if self.connection and hasattr(self.connection.root, 'on_start'):
                @self.core.on_start
                def on_start():
                    try:
                        if self.connection:
                            self.connection.root.on_start()
                    except Exception as exc:
                        logger.debug('Peer on_start exception:', exc)
            else:
                logger.debug(
                    "Peer doesn't have on_start function on it's service, skipped")

            if self.connection and hasattr(self.connection.root, 'on_stop'):
                @self.core.on_stop
                def on_stop():
                    try:
                        if self.connection:
                            self.connection.root.on_stop()
                    except Exception as exc:
                        logger.debug('Peer on_stop exception:', exc)
            else:
                logger.debug(
                    "Peer doesn't have on_stop function on it's service, skipped")

            self.core.start(config)
            self._schedule_log_rotation()
        except Exception as exc:
            logger.error(exc)
            raise exc

    @rpyc.exposed
    def stop(self):
        self._cancel_log_rotation()
        if self.core:
            try:
                self.core.stop()
            except RuntimeError:
                pass
        self.core = None

    @rpyc.exposed
    def restart(self, config: str, log_settings: dict | None = None):
        config = self._configure_static_logs(XRayConfig(config, self.connection.peer), log_settings)
        self.core.restart(config)
        self._schedule_log_rotation()

    @rpyc.exposed
    def fetch_xray_version(self):
        if self.core is None:
            raise ProcessLookupError("Xray has not been started")

        return self.core.version

    @rpyc.exposed
    def list_static_logs(self):
        self._require_connection()
        return self._call_static_log(static_log_manager.list_files, self.log_settings)

    @rpyc.exposed
    def download_static_log(self, log_type: str, filename: str):
        self._require_connection()
        try:
            return static_log_manager.iter_file(log_type, filename)
        except StaticLogError as exc:
            raise ValueError(exc.detail) from exc

    @rpyc.exposed
    def delete_static_log(self, log_type: str, filename: str):
        self._require_connection()
        return self._call_static_log(static_log_manager.delete_file, log_type, filename, self.log_settings)

    def _configure_static_logs(self, config: XRayConfig, log_settings: dict | None) -> XRayConfig:
        self.log_settings = static_log_manager.normalize_settings(log_settings)
        self.config = static_log_manager.prepare_config(config, self.log_settings)
        return XRayConfig(json.dumps(self.config), self.connection.peer)

    def _cancel_log_rotation(self):
        if self._rotation_timer:
            self._rotation_timer.cancel()
            self._rotation_timer = None

    def _schedule_log_rotation(self):
        self._cancel_log_rotation()
        if not self.log_settings:
            return
        now = time.time()
        next_midnight = ((int(now) // 86400) + 1) * 86400
        self._rotation_timer = threading.Timer(next_midnight - now, self._rotate_static_logs)
        self._rotation_timer.daemon = True
        self._rotation_timer.start()

    def _rotate_static_logs(self):
        with self._rotation_lock:
            try:
                if self.config and self.log_settings:
                    self.config = static_log_manager.prepare_config(self.config, self.log_settings)
                    if static_log_manager.enabled_types(self.log_settings) and self.core and self.core.started:
                        self.core.restart(XRayConfig(json.dumps(self.config), self.connection.peer))
            except Exception as exc:
                logger.error(f"Failed to rotate static logs: {exc}")
            finally:
                self._schedule_log_rotation()

    @staticmethod
    def _call_static_log(operation, *args):
        try:
            return operation(*args)
        except StaticLogError as exc:
            raise ValueError(exc.detail) from exc

    @rpyc.exposed
    def issue_certificate(
        self,
        domain: str,
        email: str = None,
        staging: bool = False,
        force: bool = False
    ):
        if self.connection is None:
            raise ConnectionError("Controller is not connected")

        return certificate_manager.issue_certificate(
            domain=domain,
            email=email,
            staging=staging,
            force=force,
        )

    @rpyc.exposed
    def list_geo_resources(self):
        self._require_connection()
        return geo_resource_manager.list_resources()

    @rpyc.exposed
    def begin_geo_resource_upload(self, filename: str, overwrite: bool = False):
        self._require_connection()
        token = geo_resource_manager.begin_upload(filename=filename, overwrite=overwrite)
        self._geo_uploads.add(token)
        return token

    @rpyc.exposed
    def append_geo_resource_upload(self, token: str, chunk: bytes):
        self._require_connection()
        if token not in self._geo_uploads:
            raise ValueError("Geo-resource upload does not belong to this connection.")
        return geo_resource_manager.append_upload(token, chunk)

    @rpyc.exposed
    def finish_geo_resource_upload(self, token: str):
        self._require_connection()
        if token not in self._geo_uploads:
            raise ValueError("Geo-resource upload does not belong to this connection.")
        try:
            return geo_resource_manager.finish_upload(token)
        finally:
            self._geo_uploads.discard(token)

    @rpyc.exposed
    def abort_geo_resource_upload(self, token: str):
        if token in self._geo_uploads:
            try:
                geo_resource_manager.abort_upload(token)
            finally:
                self._geo_uploads.discard(token)

    @rpyc.exposed
    def download_geo_resource(self, filename: str):
        self._require_connection()
        return geo_resource_manager.iter_resource(filename)

    @rpyc.exposed
    def rename_geo_resource(
        self,
        filename: str,
        new_filename: str,
        overwrite: bool = False,
    ):
        self._require_connection()
        return geo_resource_manager.rename_resource(
            filename=filename,
            new_filename=new_filename,
            overwrite=overwrite,
        )

    @rpyc.exposed
    def delete_geo_resources(self, filenames: list[str]):
        self._require_connection()
        return geo_resource_manager.delete_resources(filenames)

    def _require_connection(self):
        if self.connection is None:
            raise ConnectionError("Controller is not connected")

    @rpyc.exposed
    def fetch_logs(self, callback: callable) -> XrayCoreLogsHandler:
        if self.core:
            logs = XrayCoreLogsHandler(self.core, callback)
            logs.exposed_stop = logs.stop
            logs.exposed_cast = logs.cast
            return logs
