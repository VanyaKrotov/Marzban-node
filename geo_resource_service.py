import base64
import binascii
import os
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import XRAY_ASSETS_PATH


MAX_GEO_RESOURCE_SIZE = 128 * 1024 * 1024


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


class GeoResourceManager:
    def __init__(
        self,
        assets_path: str = XRAY_ASSETS_PATH,
        max_size: int = MAX_GEO_RESOURCE_SIZE,
    ):
        self.assets_path = Path(assets_path)
        self.max_size = max_size
        self.max_base64_size = ((max_size + 2) // 3) * 4
        self._lock = threading.RLock()

    def list_resources(self) -> dict:
        with self._lock:
            directory = self._directory()
            files = []
            try:
                entries = list(directory.iterdir())
            except OSError as exc:
                raise GeoResourceStorageError(
                    "Failed to list geo-resource directory."
                ) from exc

            for entry in entries:
                if not entry.name.endswith(".dat"):
                    continue
                try:
                    file_stat = entry.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(
                    file_stat.st_mode
                ):
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

    def upload_resource(
        self,
        filename: str,
        content: str,
        overwrite: bool = False,
    ) -> dict:
        filename = self.validate_filename(filename)
        decoded = self._decode_content(content)

        with self._lock:
            target = self._target(filename)
            existing = self._existing_file(target)
            if existing and not overwrite:
                raise GeoResourceConflictError(
                    f'Geo-resource "{filename}" already exists.'
                )

            try:
                descriptor, temporary_path = tempfile.mkstemp(
                    prefix=f".{filename}.",
                    suffix=".tmp",
                    dir=self._directory(),
                )
            except OSError as exc:
                raise GeoResourceStorageError(
                    f'Failed to create geo-resource "{filename}".'
                ) from exc
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(decoded)
                    temporary.flush()
                    os.fsync(temporary.fileno())

                if not overwrite and target.exists():
                    raise GeoResourceConflictError(
                        f'Geo-resource "{filename}" already exists.'
                    )
                os.replace(temporary_path, target)
            except Exception as exc:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
                if isinstance(exc, GeoResourceError):
                    raise
                if isinstance(exc, OSError):
                    raise GeoResourceStorageError(
                        f'Failed to write geo-resource "{filename}".'
                    ) from exc
                raise

            return self._metadata(target)

    def download_resource(self, filename: str) -> dict:
        filename = self.validate_filename(filename)

        with self._lock:
            target = self._target(filename)
            self._require_regular_file(target)
            flags = os.O_RDONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW

            try:
                descriptor = os.open(target, flags)
                with os.fdopen(descriptor, "rb") as resource:
                    file_stat = os.fstat(resource.fileno())
                    if not stat.S_ISREG(file_stat.st_mode):
                        raise GeoResourceInputError(
                            "Geo-resource is not a regular file."
                        )
                    if file_stat.st_size > self.max_size:
                        raise GeoResourceTooLargeError(
                            "Geo-resource exceeds the 128 MiB limit."
                        )
                    content = resource.read(self.max_size + 1)
            except GeoResourceError:
                raise
            except FileNotFoundError as exc:
                raise GeoResourceNotFoundError(
                    f'Geo-resource "{filename}" was not found.'
                ) from exc
            except OSError as exc:
                raise GeoResourceStorageError(
                    f'Failed to read geo-resource "{filename}".'
                ) from exc

            if len(content) > self.max_size:
                raise GeoResourceTooLargeError(
                    "Geo-resource exceeds the 128 MiB limit."
                )
            return {"content": base64.b64encode(content).decode("ascii")}

    def rename_resource(
        self,
        filename: str,
        new_filename: str,
        overwrite: bool = False,
    ) -> dict:
        filename = self.validate_filename(filename)
        new_filename = self.validate_filename(new_filename)

        with self._lock:
            source = self._target(filename)
            target = self._target(new_filename)
            self._require_regular_file(source)

            if source == target:
                return self._metadata(source)

            existing = self._existing_file(target)
            if existing and not overwrite:
                raise GeoResourceConflictError(
                    f'Geo-resource "{new_filename}" already exists.'
                )

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

            # Validate the complete request before deleting any file.
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
        if filename != Path(filename).name:
            raise GeoResourceInputError("Filename must be a plain basename.")
        if "/" in filename or "\\" in filename or "\0" in filename:
            raise GeoResourceInputError("Filename must be a plain basename.")
        if not filename.endswith(".dat"):
            raise GeoResourceInputError("Filename must end with .dat.")
        if Path(filename).is_absolute():
            raise GeoResourceInputError("Absolute paths are not allowed.")
        return filename

    def _directory(self) -> Path:
        try:
            self.assets_path.mkdir(mode=0o755, parents=True, exist_ok=True)
            directory = self.assets_path.resolve(strict=True)
        except OSError as exc:
            raise GeoResourceStorageError(
                "Geo-resource directory is unavailable."
            ) from exc
        if not directory.is_dir():
            raise GeoResourceStorageError(
                "Geo-resource path is not a directory."
            )
        return directory

    def _target(self, filename: str) -> Path:
        directory = self._directory()
        target = directory / filename
        try:
            resolved = target.resolve(strict=False)
        except OSError as exc:
            raise GeoResourceInputError("Invalid geo-resource path.") from exc
        if resolved.parent != directory:
            raise GeoResourceInputError(
                "Geo-resource path escapes the asset directory."
            )
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
            raise GeoResourceInputError(
                "Geo-resource is not a regular file."
            )
        return True

    def _require_regular_file(self, path: Path):
        if not self._existing_file(path):
            raise GeoResourceNotFoundError(
                f'Geo-resource "{path.name}" was not found.'
            )

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
            "modified_at": datetime.fromtimestamp(
                file_stat.st_mtime, timezone.utc
            ).isoformat().replace("+00:00", "Z"),
        }

    def _decode_content(self, content: str) -> bytes:
        if not isinstance(content, str):
            raise GeoResourceInputError("content must be a base64 string.")
        if len(content) > self.max_base64_size:
            raise GeoResourceTooLargeError(
                "Geo-resource exceeds the 128 MiB limit."
            )
        try:
            encoded = content.encode("ascii")
            decoded = base64.b64decode(encoded, validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise GeoResourceInputError("content is not valid base64.") from exc
        if len(decoded) > self.max_size:
            raise GeoResourceTooLargeError(
                "Geo-resource exceeds the 128 MiB limit."
            )
        return decoded


geo_resource_manager = GeoResourceManager()
