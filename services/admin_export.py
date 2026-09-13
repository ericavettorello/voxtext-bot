"""Экспорт CSV, снимок SQLite и очищенные копии журналов для администратора."""

from __future__ import annotations

import csv
import os
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from logging_config import BACKUP_COUNT, LOG_FILE
from services.admin_stats import format_db_datetime
from services.audio_service import delete_job_directory
from services.tts_service import TEMP_DIR

TELEGRAM_EXPORT_LIMIT_MB = 49
TELEGRAM_EXPORT_LIMIT_BYTES = TELEGRAM_EXPORT_LIMIT_MB * 1024 * 1024
ADMIN_EXPORTS_DIRNAME = "admin_exports"
REDACTED = "[REDACTED]"
_FORMULA_PREFIXES = ("=", "+", "-", "@")

USERS_CSV_HEADERS = (
    "telegram_user_id",
    "username",
    "first_name",
    "last_name",
    "created_at",
    "last_activity_at",
    "selected_voice_name",
    "speech_speed",
    "requests_count",
    "successful_requests",
    "total_characters",
    "estimated_credits",
    "estimated_cost_usd",
)
REQUESTS_CSV_HEADERS = (
    "request_id",
    "telegram_user_id",
    "source_type",
    "status",
    "character_count",
    "processed_characters",
    "chunk_count",
    "completed_chunks",
    "voice_name",
    "speech_speed",
    "estimated_credits",
    "estimated_cost_usd",
    "page_count",
    "created_at",
    "completed_at",
    "error_code",
)

_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)\S+")
_AUTHORIZATION_RE = re.compile(r"(?i)(Authorization\s*[:=]\s*)\S+")
_XI_API_RE = re.compile(r"(?i)(xi-api-key\s*[:=]\s*)\S+")
_NAMED_SECRET_RE = re.compile(
    r"(?i)((?:TELEGRAM_BOT_TOKEN|ELEVENLABS_API_KEY|[A-Z0-9_]*?(?:TOKEN|SECRET|PASSWORD|API_KEY))\s*[:=]\s*)\S+"
)


class AdminExportError(Exception):
    """Ошибка подготовки административного экспорта."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class ExportTooLargeError(AdminExportError):
    def __init__(self) -> None:
        super().__init__(
            "Файл экспорта превышает допустимый размер Telegram.\n"
            "Используйте экспорт за меньший период или получите файл непосредственно с сервера."
        )


class BackupIntegrityError(AdminExportError):
    def __init__(self) -> None:
        super().__init__("Не удалось подготовить экспорт. Подробности записаны в журнал.")


def export_stamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.strftime("%Y-%m-%d_%H-%M")


def create_export_directory(temp_root: Path | None = None) -> tuple[str, Path]:
    root = (temp_root or TEMP_DIR) / ADMIN_EXPORTS_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())
    job_dir = root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_id, job_dir


def cleanup_export_directory(job_dir: Path | None, job_id: str | None = None) -> None:
    delete_job_directory(job_dir, job_id)


def assert_export_size(path: Path) -> None:
    if path.stat().st_size > TELEGRAM_EXPORT_LIMIT_BYTES:
        raise ExportTooLargeError


def csv_safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        text = format(value, "f")
    else:
        text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\r\n")
        writer.writerow(headers)
        for row in rows:
            writer.writerow([csv_safe(item) for item in row])
    return path


def users_csv_rows(records: Iterable[dict[str, Any]]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in records:
        rows.append(
            [
                item.get("telegram_user_id"),
                item.get("username"),
                item.get("first_name"),
                item.get("last_name"),
                format_db_datetime(item.get("created_at")),
                format_db_datetime(item.get("last_activity_at")),
                item.get("selected_voice_name"),
                item.get("speech_speed"),
                item.get("requests_count"),
                item.get("successful_requests"),
                item.get("total_characters"),
                item.get("estimated_credits"),
                item.get("estimated_cost_usd"),
            ]
        )
    return rows


def requests_csv_rows(records: Iterable[dict[str, Any]]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in records:
        rows.append(
            [
                item.get("request_id"),
                item.get("telegram_user_id"),
                item.get("source_type"),
                item.get("status"),
                item.get("character_count"),
                item.get("processed_characters"),
                item.get("chunk_count"),
                item.get("completed_chunks"),
                item.get("voice_name"),
                item.get("speech_speed"),
                item.get("estimated_credits"),
                item.get("estimated_cost_usd"),
                item.get("page_count"),
                format_db_datetime(item.get("created_at")),
                format_db_datetime(item.get("completed_at")),
                item.get("error_code"),
            ]
        )
    return rows


def create_sqlite_backup(source_path: Path, dest_path: Path) -> Path:
    """Согласованный снимок через sqlite3.Connection.backup(), без shutil.copy."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        dest_path.unlink()
    source = sqlite3.connect(_sqlite_uri(source_path, readonly=True), uri=True, timeout=30)
    destination = sqlite3.connect(str(dest_path), timeout=30)
    try:
        _run_sqlite_backup(source, destination)
        destination.commit()
        row = destination.execute("PRAGMA integrity_check").fetchone()
        result = str(row[0]).lower() if row else ""
        if result != "ok":
            raise BackupIntegrityError
    except BackupIntegrityError:
        raise
    except Exception as exc:
        raise AdminExportError("Не удалось подготовить экспорт. Подробности записаны в журнал.") from exc
    finally:
        destination.close()
        source.close()
    return dest_path


def _run_sqlite_backup(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    """Отдельная обёртка над Backup API, чтобы тесты могли проверить вызов."""
    source.backup(destination)


def zip_database_copy(db_path: Path, zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(db_path, arcname="voxtext_database.db")
    return zip_path


def rotating_log_files(log_path: Path | None = None, backup_count: int = BACKUP_COUNT) -> list[Path]:
    path = log_path or LOG_FILE
    files: list[Path] = []
    if path.is_file():
        files.append(path)
    for index in range(1, backup_count + 1):
        rotated = Path(f"{path}.{index}")
        if rotated.is_file():
            files.append(rotated)
    return files


class SecretRedactor:
    """Заменяет известные секреты и типичные токены на [REDACTED]."""

    def __init__(self, secrets: Iterable[str] | None = None) -> None:
        unique = {item.strip() for item in (secrets or []) if item and item.strip()}
        self._secrets = tuple(sorted(unique, key=len, reverse=True))

    @classmethod
    def from_environ(cls, extra: Iterable[str] | None = None) -> "SecretRedactor":
        collected: list[str] = list(extra or [])
        for name, value in os.environ.items():
            upper = name.upper()
            if any(marker in upper for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
                if value and value.strip():
                    collected.append(value.strip())
        return cls(collected)

    def redact_text(self, text: str) -> str:
        cleaned = text
        for secret in self._secrets:
            if secret:
                cleaned = cleaned.replace(secret, REDACTED)
        cleaned = _TELEGRAM_TOKEN_RE.sub(REDACTED, cleaned)
        cleaned = _BEARER_RE.sub(rf"\1{REDACTED}", cleaned)
        cleaned = _AUTHORIZATION_RE.sub(rf"\1{REDACTED}", cleaned)
        cleaned = _XI_API_RE.sub(rf"\1{REDACTED}", cleaned)
        cleaned = _NAMED_SECRET_RE.sub(rf"\1{REDACTED}", cleaned)
        return cleaned

    def redact_file(self, source: Path, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_text(encoding="utf-8", errors="replace")
        dest.write_text(self.redact_text(data), encoding="utf-8")
        return dest


def copy_current_log(source: Path, dest: Path, redactor: SecretRedactor) -> Path:
    if not source.is_file() or source.stat().st_size == 0:
        raise AdminExportError("Текущий журнал пока пуст или не найден.")
    return redactor.redact_file(source, dest)


def zip_redacted_logs(
    log_files: Sequence[Path],
    zip_path: Path,
    redactor: SecretRedactor,
    work_dir: Path,
) -> Path:
    if not log_files:
        raise AdminExportError("Текущий журнал пока пуст или не найден.")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in log_files:
            cleaned = work_dir / source.name
            redactor.redact_file(source, cleaned)
            archive.write(cleaned, arcname=source.name)
    return zip_path


def _sqlite_uri(path: Path, readonly: bool = False) -> str:
    uri = path.resolve().as_uri()
    if readonly:
        return f"{uri}?mode=ro"
    return uri
