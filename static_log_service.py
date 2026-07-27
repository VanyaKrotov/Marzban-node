from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Iterator


LOG_ROOT = Path("/logs")
LOG_TYPES = ("access", "error")
LOG_FILENAME_RE = re.compile(r"^(0[1-9]|[12]\d|3[01])-(0[1-9]|1[0-2])-(\d{4})\.txt$")
CHUNK_SIZE = 64 * 1024


class StaticLogError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class StaticLogManager:
    def __init__(self, root: Path = LOG_ROOT):
        self.root = root

    @staticmethod
    def normalize_settings(settings: dict | None) -> dict:
        settings = settings or {}
        retention_days = settings.get("log_retention_days", 14)
        limit = settings.get("log_storage_limit_bytes")
        if not isinstance(retention_days, int) or retention_days < 1:
            raise StaticLogError(422, "log_retention_days must be a positive integer")
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise StaticLogError(422, "log_storage_limit_bytes must be null or a positive integer")
        return {
            "access_log_enabled": bool(settings.get("access_log_enabled", False)),
            "error_log_enabled": bool(settings.get("error_log_enabled", False)),
            "log_retention_days": retention_days,
            "log_storage_limit_bytes": limit,
        }

    @staticmethod
    def active_filename(now: datetime | None = None) -> str:
        return (now or datetime.now(timezone.utc)).strftime("%d-%m-%Y.txt")

    def enabled_types(self, settings: dict) -> tuple[str, ...]:
        return tuple(log_type for log_type in LOG_TYPES if settings[f"{log_type}_log_enabled"])

    def prepare_config(self, config: dict, settings: dict, now: datetime | None = None) -> dict:
        settings = self.normalize_settings(settings)
        prepared = deepcopy(config)
        log_config = prepared.get("log")
        if not isinstance(log_config, dict):
            log_config = {}
            prepared["log"] = log_config
        filename = self.active_filename(now)
        for log_type in LOG_TYPES:
            log_config.pop(log_type, None)
        for log_type in self.enabled_types(settings):
            directory = self.root / log_type
            directory.mkdir(parents=True, exist_ok=True)
            log_config[log_type] = str(directory / filename)
        self.cleanup(settings, now)
        return prepared

    def list_files(self, settings: dict) -> list[dict]:
        settings = self.normalize_settings(settings)
        files = []
        active = self.active_filename()
        for log_type in LOG_TYPES:
            directory = self.root / log_type
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.iterdir():
                try:
                    parsed_date = self._validate_path(log_type, path.name, require_exists=True)
                except StaticLogError:
                    continue
                stat = path.stat()
                files.append({
                    "type": log_type,
                    "filename": path.name,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "active": path.name == active and log_type in self.enabled_types(settings),
                    "date": parsed_date,
                })
        files.sort(key=lambda item: item["date"], reverse=True)
        for item in files:
            item.pop("date")
        return files

    def iter_file(self, log_type: str, filename: str) -> Iterator[bytes]:
        path = self._path(log_type, filename, require_exists=True)

        def stream() -> Iterator[bytes]:
            with path.open("rb") as file:
                while chunk := file.read(CHUNK_SIZE):
                    yield chunk

        return stream()

    def delete_file(self, log_type: str, filename: str, settings: dict) -> dict:
        settings = self.normalize_settings(settings)
        path = self._path(log_type, filename, require_exists=True)
        if filename == self.active_filename() and log_type in self.enabled_types(settings):
            with path.open("wb"):
                pass
            return {"cleared": True}
        path.unlink()
        return {"cleared": False}

    def cleanup(self, settings: dict, now: datetime | None = None) -> None:
        settings = self.normalize_settings(settings)
        now = now or datetime.now(timezone.utc)
        expired_before = now.date() - timedelta(days=settings["log_retention_days"])
        files = self.list_files(settings)
        for item in files:
            if datetime.strptime(item["filename"][:-4], "%d-%m-%Y").date() < expired_before:
                self._path(item["type"], item["filename"], require_exists=True).unlink()

        limit = settings["log_storage_limit_bytes"]
        if limit is None:
            return
        files = self.list_files(settings)
        total_size = sum(item["size"] for item in files)
        for item in sorted(
            (item for item in files if not item["active"]),
            key=lambda item: datetime.strptime(item["filename"][:-4], "%d-%m-%Y"),
        ):
            if total_size <= limit:
                return
            self._path(item["type"], item["filename"], require_exists=True).unlink()
            total_size -= item["size"]
        if total_size > limit:
            for item in self.list_files(settings):
                if item["active"]:
                    path = self._path(item["type"], item["filename"], require_exists=True)
                    with path.open("wb"):
                        pass
                    total_size -= item["size"]
                    if total_size <= limit:
                        return

    def _path(self, log_type: str, filename: str, require_exists: bool = False) -> Path:
        if log_type not in LOG_TYPES:
            raise StaticLogError(422, "Unknown log type")
        path = self.root / log_type / filename
        self._validate_path(log_type, filename, require_exists=require_exists)
        return path

    def _validate_path(self, log_type: str, filename: str, require_exists: bool) -> datetime | None:
        if log_type not in LOG_TYPES or not LOG_FILENAME_RE.fullmatch(filename):
            raise StaticLogError(422, "Invalid log file path")
        try:
            parsed_date = datetime.strptime(filename[:-4], "%d-%m-%Y").date()
        except ValueError as exc:
            raise StaticLogError(422, "Invalid log filename") from exc
        path = self.root / log_type / filename
        if require_exists:
            if not path.is_file() or path.is_symlink():
                raise StaticLogError(404, "Log file not found")
            try:
                path.resolve().relative_to((self.root / log_type).resolve())
            except ValueError as exc:
                raise StaticLogError(422, "Invalid log file path") from exc
        return parsed_date


static_log_manager = StaticLogManager()
