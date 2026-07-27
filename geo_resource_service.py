import os
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from config import XRAY_ASSETS_PATH


MAX_GEO_RESOURCE_SIZE = 128 * 1024 * 1024
UPLOAD_SESSION_TIMEOUT = 5 * 60


class GeoResourceError(Exception):
    status_code = 500


class GeoResourceInputError(GeoResourceError):
    status_code = 400


class GeoResourceNotFoundError(GeoResourceError):
    status_code = 404


class GeoResourceConflictError(GeoResourceError):
    status_code = 409


class GeoResourceTooLargeError(GeoResourceError):
    status_code = 413


class GeoResourceStorageError(GeoResourceError):
    status_code = 500


class GeoResourceUpload:
    def __init__(
        self, manager, token: str, target: Path, temporary_path: str, descriptor: int, overwrite: bool
    ):
        self.manager = manager
        self.token = token
        self.target = target
        self.temporary_path = temporary_path
        self.overwrite = overwrite
        self.file = os.fdopen(descriptor, "wb")
        self.size = 0
        self.created_at = time.monotonic()
        self.lock = threading.Lock()
        self.closed = False

    def write(self, chunk: bytes):
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise GeoResourceInputError("Geo-resource chunk must be bytes.")
        if not chunk:
            return

        with self.lock:
            if self.closed:
                raise GeoResourceInputError("Geo-resource upload is no longer active.")
            self.size += len(chunk)
            if self.size > self.manager.max_size:
                raise GeoResourceTooLargeError("Geo-resource exceeds the 128 MiB limit.")
            try:
                self.file.write(chunk)
            except OSError as exc:
                raise GeoResourceStorageError(
                    f'Failed to write geo-resource "{self.target.name}".'
                ) from exc

    def close(self):
        with self.lock:
            if self.closed:
                return
            try:
                self.file.flush()
                os.fsync(self.file.fileno())
                self.file.close()
                self.closed = True
            except OSError as exc:
                try:
                    self.file.close()
                except OSError:
                    pass
                self.closed = True
                raise GeoResourceStorageError(
                    f'Failed to finalize geo-resource "{self.target.name}".'
                ) from exc

    def discard(self):
        with self.lock:
            if not self.closed:
                try:
                    self.file.close()
                except OSError:
                    pass
                self.closed = True
        try:
            os.unlink(self.temporary_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise GeoResourceStorageError(
                f'Failed to remove temporary geo-resource "{self.target.name}".'
            ) from exc


class GeoResourceManager:
    def __init__(
        self,
        assets_path: str = XRAY_ASSETS_PATH,
        max_size: int = MAX_GEO_RESOURCE_SIZE,
    ):
        self.assets_path = Path(assets_path)
        self.max_size = max_size
        self._lock = threading.RLock()
        self._uploads: dict[str, GeoResourceUpload] = {}
        self._upload_timers: dict[str, threading.Timer] = {}

    def list_resources(self) -> dict:
        with self._lock:
            self._cleanup_expired_uploads()
            directory = self._directory()
            files = []
            try:
                entries = list(directory.iterdir())
            except OSError as exc:
                raise GeoResourceStorageError("Failed to list geo-resource directory.") from exc

            for entry in entries:
                if not entry.name.endswith(".dat"):
                    continue
                try:
                    file_stat = entry.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                    continue
                files.append(
                    {
                        "filename": entry.name,
                        "size": file_stat.st_size,
                        "modified_at": datetime.fromtimestamp(
                            file_stat.st_mtime, timezone.utc
                        ).isoformat().replace("+00:00", "Z"),
                    }
                )

            files.sort(key=lambda item: item["filename"])
            return {"files": files}

    def begin_upload(self, filename: str, overwrite: bool = False) -> str:
        filename = self.validate_filename(filename)
        with self._lock:
            self._cleanup_expired_uploads()
            target = self._target(filename)
            if self._existing_file(target) and not overwrite:
                raise GeoResourceConflictError(f'Geo-resource "{filename}" already exists.')
            try:
                descriptor, temporary_path = tempfile.mkstemp(
                    prefix=f".{filename}.", suffix=".tmp", dir=self._directory()
                )
            except OSError as exc:
                raise GeoResourceStorageError(
                    f'Failed to create geo-resource "{filename}".'
                ) from exc

            token = str(uuid4())
            self._uploads[token] = GeoResourceUpload(
                self, token, target, temporary_path, descriptor, overwrite
            )
            timer = threading.Timer(UPLOAD_SESSION_TIMEOUT, self.abort_upload, args=(token,))
            timer.daemon = True
            self._upload_timers[token] = timer
            timer.start()
            return token

    def append_upload(self, token: str, chunk: bytes):
        self._get_upload(token).write(chunk)

    def finish_upload(self, token: str) -> dict:
        upload = self._get_upload(token)
        try:
            upload.close()
            with self._lock:
                if self._uploads.get(token) is not upload:
                    raise GeoResourceInputError("Geo-resource upload is no longer active.")
                if self._existing_file(upload.target) and not upload.overwrite:
                    raise GeoResourceConflictError(
                        f'Geo-resource "{upload.target.name}" already exists.'
                    )
                try:
                    os.replace(upload.temporary_path, upload.target)
                except OSError as exc:
                    raise GeoResourceStorageError(
                        f'Failed to finalize geo-resource "{upload.target.name}".'
                    ) from exc
                self._uploads.pop(token, None)
                timer = self._upload_timers.pop(token, None)
                if timer:
                    timer.cancel()
                return self._metadata(upload.target)
        except Exception:
            self.abort_upload(token)
            raise

    def abort_upload(self, token: str):
        with self._lock:
            upload = self._uploads.pop(token, None)
            timer = self._upload_timers.pop(token, None)
        if timer:
            timer.cancel()
        if upload:
            upload.discard()

    def abort_uploads(self, tokens: list[str]):
        for token in tokens:
            self.abort_upload(token)

    def iter_resource(self, filename: str, chunk_size: int = 64 * 1024):
        filename = self.validate_filename(filename)
        if chunk_size <= 0:
            raise GeoResourceInputError("Geo-resource chunk size must be positive.")

        with self._lock:
            self._cleanup_expired_uploads()
            target = self._target(filename)
            self._require_regular_file(target)
            flags = os.O_RDONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(target, flags)
                file_stat = os.fstat(descriptor)
            except FileNotFoundError as exc:
                raise GeoResourceNotFoundError(f'Geo-resource "{filename}" was not found.') from exc
            except OSError as exc:
                raise GeoResourceStorageError(f'Failed to read geo-resource "{filename}".') from exc
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(descriptor)
                raise GeoResourceInputError("Geo-resource is not a regular file.")
            if file_stat.st_size > self.max_size:
                os.close(descriptor)
                raise GeoResourceTooLargeError("Geo-resource exceeds the 128 MiB limit.")

        def stream():
            try:
                with os.fdopen(descriptor, "rb") as resource:
                    while chunk := resource.read(chunk_size):
                        yield chunk
            except OSError as exc:
                raise GeoResourceStorageError(f'Failed to read geo-resource "{filename}".') from exc

        return stream()

    def rename_resource(self, filename: str, new_filename: str, overwrite: bool = False) -> dict:
        filename = self.validate_filename(filename)
        new_filename = self.validate_filename(new_filename)
        with self._lock:
            source = self._target(filename)
            target = self._target(new_filename)
            self._require_regular_file(source)
            if source == target:
                return self._metadata(source)
            if self._existing_file(target) and not overwrite:
                raise GeoResourceConflictError(f'Geo-resource "{new_filename}" already exists.')
            try:
                if overwrite:
                    os.replace(source, target)
                else:
                    os.rename(source, target)
            except FileExistsError as exc:
                raise GeoResourceConflictError(
                    f'Geo-resource "{new_filename}" already exists.'
                ) from exc
            except OSError as exc:
                raise GeoResourceStorageError(
                    f'Failed to rename geo-resource "{filename}".'
                ) from exc
            return self._metadata(target)

    def delete_resources(self, filenames: list[str]) -> dict:
        if not isinstance(filenames, list):
            raise GeoResourceInputError("filenames must be a list.")
        validated = [self.validate_filename(filename) for filename in filenames]
        if len(set(validated)) != len(validated):
            raise GeoResourceInputError("filenames must not contain duplicates.")
        with self._lock:
            targets = [self._target(filename) for filename in validated]
            for target in targets:
                self._existing_file(target)
            for target in targets:
                try:
                    target.unlink(missing_ok=True)
                except OSError as exc:
                    raise GeoResourceStorageError(
                        f'Failed to delete geo-resource "{target.name}".'
                    ) from exc
        return {"filenames": validated}

    def validate_filename(self, filename: str) -> str:
        if not isinstance(filename, str):
            raise GeoResourceInputError("Filename must be a string.")
        if not filename or filename in (".", ".."):
            raise GeoResourceInputError("Filename is required.")
        if filename != Path(filename).name or "/" in filename or "\\" in filename or "\0" in filename:
            raise GeoResourceInputError("Filename must be a plain basename.")
        if not filename.endswith(".dat"):
            raise GeoResourceInputError("Filename must end with .dat.")
        if Path(filename).is_absolute():
            raise GeoResourceInputError("Absolute paths are not allowed.")
        return filename

    def _get_upload(self, token: str) -> GeoResourceUpload:
        if not isinstance(token, str):
            raise GeoResourceInputError("Geo-resource upload token is invalid.")
        with self._lock:
            self._cleanup_expired_uploads()
            upload = self._uploads.get(token)
        if not upload:
            raise GeoResourceInputError("Geo-resource upload is no longer active.")
        return upload

    def _cleanup_expired_uploads(self):
        expired = [
            token
            for token, upload in self._uploads.items()
            if time.monotonic() - upload.created_at >= UPLOAD_SESSION_TIMEOUT
        ]
        for token in expired:
            upload = self._uploads.pop(token)
            timer = self._upload_timers.pop(token, None)
            if timer:
                timer.cancel()
            upload.discard()

    def _directory(self) -> Path:
        try:
            self.assets_path.mkdir(mode=0o755, parents=True, exist_ok=True)
            directory = self.assets_path.resolve(strict=True)
        except OSError as exc:
            raise GeoResourceStorageError("Geo-resource directory is unavailable.") from exc
        if not directory.is_dir():
            raise GeoResourceStorageError("Geo-resource path is not a directory.")
        return directory

    def _target(self, filename: str) -> Path:
        directory = self._directory()
        target = directory / filename
        try:
            resolved = target.resolve(strict=False)
        except OSError as exc:
            raise GeoResourceInputError("Invalid geo-resource path.") from exc
        if resolved.parent != directory:
            raise GeoResourceInputError("Geo-resource path escapes the asset directory.")
        return target

    def _existing_file(self, path: Path) -> bool:
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise GeoResourceStorageError(
                f'Failed to inspect geo-resource "{path.name}".'
            ) from exc
        if stat.S_ISLNK(file_stat.st_mode):
            raise GeoResourceInputError("Symbolic links are not allowed.")
        if not stat.S_ISREG(file_stat.st_mode):
            raise GeoResourceInputError("Geo-resource is not a regular file.")
        return True

    def _require_regular_file(self, path: Path):
        if not self._existing_file(path):
            raise GeoResourceNotFoundError(f'Geo-resource "{path.name}" was not found.')

    def _metadata(self, path: Path) -> dict:
        self._require_regular_file(path)
        try:
            file_stat = path.lstat()
        except OSError as exc:
            raise GeoResourceStorageError(
                f'Failed to inspect geo-resource "{path.name}".'
            ) from exc
        return {
            "filename": path.name,
            "size": file_stat.st_size,
            "modified_at": datetime.fromtimestamp(file_stat.st_mtime, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }


geo_resource_manager = GeoResourceManager()
