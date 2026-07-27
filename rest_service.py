import asyncio
import json
import time
import threading
from uuid import UUID, uuid4

from fastapi import (APIRouter, Body, FastAPI, HTTPException, Request,
                     WebSocket, status)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketDisconnect

from certificate_service import (CertificateServiceError, certificate_manager)
from config import XRAY_ASSETS_PATH, XRAY_EXECUTABLE_PATH
from geo_resource_service import (GeoResourceError, geo_resource_manager)
from logger import logger
from static_log_service import StaticLogError, static_log_manager
from xray import XRayConfig, XRayCore

app = FastAPI()


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = {}
    for error in exc.errors():
        details[error["loc"][-1]] = error.get("msg")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder({"detail": details}),
    )


class Service(object):
    def __init__(self):
        self.router = APIRouter()

        self.connected = False
        self.client_ip = None
        self.session_id = None
        self.core = XRayCore(
            executable_path=XRAY_EXECUTABLE_PATH,
            assets_path=XRAY_ASSETS_PATH
        )
        self.core_version = self.core.get_version()
        self.config = None
        self.log_settings = None
        self._rotation_timer = None
        self._rotation_lock = threading.Lock()

        self.router.add_api_route("/", self.base, methods=["POST"])
        self.router.add_api_route("/ping", self.ping, methods=["POST"])
        self.router.add_api_route("/connect", self.connect, methods=["POST"])
        self.router.add_api_route("/disconnect", self.disconnect, methods=["POST"])
        self.router.add_api_route("/start", self.start, methods=["POST"])
        self.router.add_api_route("/stop", self.stop, methods=["POST"])
        self.router.add_api_route("/restart", self.restart, methods=["POST"])
        self.router.add_api_route(
            "/certificates/issue",
            self.issue_certificate,
            methods=["POST"]
        )
        self.router.add_api_route(
            "/certificates/import",
            self.import_certificate,
            methods=["POST"]
        )
        self.router.add_api_route(
            "/geo-resources",
            self.list_geo_resources,
            methods=["POST"]
        )
        self.router.add_api_route(
            "/geo-resources/upload",
            self.upload_geo_resource,
            methods=["POST"]
        )
        self.router.add_api_route(
            "/geo-resources/download",
            self.download_geo_resource,
            methods=["POST"]
        )
        self.router.add_api_route(
            "/geo-resources/rename",
            self.rename_geo_resource,
            methods=["POST"]
        )
        self.router.add_api_route(
            "/geo-resources/delete",
            self.delete_geo_resources,
            methods=["POST"]
        )
        self.router.add_api_route("/static-logs", self.list_static_logs, methods=["POST"])
        self.router.add_api_route("/static-logs/download", self.download_static_log, methods=["POST"])
        self.router.add_api_route("/static-logs/delete", self.delete_static_log, methods=["POST"])

        self.router.add_websocket_route("/logs", self.logs)

    def match_session_id(self, session_id: UUID):
        if session_id != self.session_id:
            raise HTTPException(
                status_code=403,
                detail="Session ID mismatch."
            )
        return True

    def response(self, **kwargs):
        return {
            "connected": self.connected,
            "started": self.core.started,
            "core_version": self.core_version,
            **kwargs
        }

    def base(self):
        return self.response()

    def connect(self, request: Request):
        self.session_id = uuid4()
        self.client_ip = request.client.host

        if self.connected:
            logger.warning(
                f'New connection from {self.client_ip}, Core control access was taken away from previous client.')
            if self.core.started:
                try:
                    self.core.stop()
                except RuntimeError:
                    pass

        self.connected = True
        logger.info(f'{self.client_ip} connected, Session ID = "{self.session_id}".')

        return self.response(
            session_id=self.session_id
        )

    def disconnect(self):
        self._cancel_log_rotation()
        if self.connected:
            logger.info(f'{self.client_ip} disconnected, Session ID = "{self.session_id}".')

        self.session_id = None
        self.client_ip = None
        self.connected = False

        if self.core.started:
            try:
                self.core.stop()
            except RuntimeError:
                pass

        return self.response()

    def ping(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        return {}

    def start(
        self,
        session_id: UUID = Body(embed=True),
        config: str = Body(embed=True),
        log_settings: dict | None = Body(default=None, embed=True),
    ):
        self.match_session_id(session_id)

        try:
            config = self._configure_static_logs(XRayConfig(config, self.client_ip), log_settings)
        except StaticLogError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except json.decoder.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "config": f'Failed to decode config: {exc}'
                }
            )

        with self.core.get_logs() as logs:
            try:
                self.core.start(config)

                start_time = time.time()
                end_time = start_time + 3
                last_log = ''
                while time.time() < end_time:
                    while logs:
                        log = logs.popleft()
                        if log:
                            last_log = log
                        if f'Xray {self.core_version} started' in log:
                            break
                    time.sleep(0.1)

            except Exception as exc:
                logger.error(f"Failed to start core: {exc}")
                raise HTTPException(
                    status_code=503,
                    detail=str(exc)
                )

        if not self.core.started:
            raise HTTPException(
                status_code=503,
                detail=last_log
            )

        self._schedule_log_rotation()
        return self.response()

    def stop(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        self._cancel_log_rotation()

        try:
            self.core.stop()

        except RuntimeError:
            pass

        return self.response()

    def restart(
        self,
        session_id: UUID = Body(embed=True),
        config: str = Body(embed=True),
        log_settings: dict | None = Body(default=None, embed=True),
    ):
        self.match_session_id(session_id)

        try:
            config = self._configure_static_logs(XRayConfig(config, self.client_ip), log_settings)
        except StaticLogError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except json.decoder.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "config": f'Failed to decode config: {exc}'
                }
            )

        try:
            with self.core.get_logs() as logs:
                self.core.restart(config)

                start_time = time.time()
                end_time = start_time + 3
                last_log = ''
                while time.time() < end_time:
                    while logs:
                        log = logs.popleft()
                        if log:
                            last_log = log
                        if f'Xray {self.core_version} started' in log:
                            break
                    time.sleep(0.1)

        except Exception as exc:
            logger.error(f"Failed to restart core: {exc}")
            raise HTTPException(
                status_code=503,
                detail=str(exc)
            )

        if not self.core.started:
            raise HTTPException(
                status_code=503,
                detail=last_log
            )

        self._schedule_log_rotation()
        return self.response()

    def list_static_logs(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        return self._call_static_log(static_log_manager.list_files, self.log_settings)

    def download_static_log(
        self,
        session_id: UUID = Body(embed=True),
        log_type: str = Body(embed=True),
        filename: str = Body(embed=True),
    ):
        self.match_session_id(session_id)
        try:
            return StreamingResponse(
                static_log_manager.iter_file(log_type, filename),
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except StaticLogError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    def delete_static_log(
        self,
        session_id: UUID = Body(embed=True),
        log_type: str = Body(embed=True),
        filename: str = Body(embed=True),
    ):
        self.match_session_id(session_id)
        return self._call_static_log(
            static_log_manager.delete_file, log_type, filename, self.log_settings
        )

    def _configure_static_logs(self, config: XRayConfig, log_settings: dict | None) -> XRayConfig:
        self.log_settings = static_log_manager.normalize_settings(log_settings)
        self.config = static_log_manager.prepare_config(config, self.log_settings)
        return XRayConfig(json.dumps(self.config), self.client_ip)

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
                    if static_log_manager.enabled_types(self.log_settings) and self.core.started:
                        self.core.restart(XRayConfig(json.dumps(self.config), self.client_ip))
            except Exception as exc:
                logger.error(f"Failed to rotate static logs: {exc}")
            finally:
                self._schedule_log_rotation()

    @staticmethod
    def _call_static_log(operation, *args):
        try:
            return operation(*args)
        except StaticLogError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    def issue_certificate(
        self,
        session_id: UUID = Body(embed=True),
        domain: str = Body(embed=True),
        email: str | None = Body(default=None, embed=True),
        staging: bool = Body(default=False, embed=True),
        force: bool = Body(default=False, embed=True),
    ):
        self.match_session_id(session_id)

        try:
            return certificate_manager.issue_certificate(
                domain=domain,
                email=email,
                staging=staging,
                force=force,
            )
        except CertificateServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=str(exc),
            ) from exc

    def import_certificate(
        self,
        session_id: UUID = Body(embed=True),
        domain: str = Body(embed=True),
        certificate_file: str = Body(embed=True),
        key_file: str = Body(embed=True),
    ):
        self.match_session_id(session_id)

        try:
            return certificate_manager.import_certificate(
                domain=domain,
                certificate_file=certificate_file,
                key_file=key_file,
            )
        except CertificateServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=str(exc),
            ) from exc

    def list_geo_resources(
        self,
        session_id: UUID = Body(embed=True),
    ):
        self.match_session_id(session_id)
        return self._call_geo_resource(geo_resource_manager.list_resources)

    def upload_geo_resource(
        self,
        session_id: UUID = Body(embed=True),
        filename: str = Body(embed=True),
        content: str = Body(embed=True),
        overwrite: bool = Body(default=False, embed=True),
    ):
        self.match_session_id(session_id)
        return self._call_geo_resource(
            geo_resource_manager.upload_resource,
            filename=filename,
            content=content,
            overwrite=overwrite,
        )

    def download_geo_resource(
        self,
        session_id: UUID = Body(embed=True),
        filename: str = Body(embed=True),
    ):
        self.match_session_id(session_id)
        return self._call_geo_resource(
            geo_resource_manager.download_resource,
            filename=filename,
        )

    def rename_geo_resource(
        self,
        session_id: UUID = Body(embed=True),
        filename: str = Body(embed=True),
        new_filename: str = Body(embed=True),
        overwrite: bool = Body(default=False, embed=True),
    ):
        self.match_session_id(session_id)
        return self._call_geo_resource(
            geo_resource_manager.rename_resource,
            filename=filename,
            new_filename=new_filename,
            overwrite=overwrite,
        )

    def delete_geo_resources(
        self,
        session_id: UUID = Body(embed=True),
        filenames: list[str] = Body(embed=True),
    ):
        self.match_session_id(session_id)
        return self._call_geo_resource(
            geo_resource_manager.delete_resources,
            filenames=filenames,
        )

    @staticmethod
    def _call_geo_resource(operation, **kwargs):
        try:
            return operation(**kwargs)
        except GeoResourceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=str(exc),
            ) from exc

    async def logs(self, websocket: WebSocket):
        session_id = websocket.query_params.get('session_id')
        interval = websocket.query_params.get('interval')

        try:
            session_id = UUID(session_id)
            if session_id != self.session_id:
                return await websocket.close(reason="Session ID mismatch.", code=4403)

        except ValueError:
            return await websocket.close(reason="session_id should be a valid UUID.", code=4400)

        if interval:
            try:
                interval = float(interval)

            except ValueError:
                return await websocket.close(reason="Invalid interval value.", code=4400)

            if interval > 10:
                return await websocket.close(reason="Interval must be more than 0 and at most 10 seconds.", code=4400)

        await websocket.accept()

        cache = ''
        last_sent_ts = 0
        with self.core.get_logs() as logs:
            while session_id == self.session_id:
                if interval and time.time() - last_sent_ts >= interval and cache:
                    try:
                        await websocket.send_text(cache)
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    cache = ''
                    last_sent_ts = time.time()

                if not logs:
                    try:
                        await asyncio.wait_for(websocket.receive(), timeout=0.2)
                        continue
                    except asyncio.TimeoutError:
                        continue
                    except (WebSocketDisconnect, RuntimeError):
                        break

                log = logs.popleft()

                if interval:
                    cache += f'{log}\n'
                    continue

                try:
                    await websocket.send_text(log)
                except (WebSocketDisconnect, RuntimeError):
                    break

        await websocket.close()


service = Service()
app.include_router(service.router)
