"""Централизованная настройка журнала VoxText."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "voxtext.log"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_LOG_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
HANDLER_MARK = "_voxtext_handler"

logger = logging.getLogger(__name__)


def setup_logging(log_dir: Path | None = None) -> Path:
    """Настроить вывод в терминал и ротацию файла без дублирования обработчиков."""
    directory = Path(log_dir) if log_dir is not None else LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "voxtext.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    _ensure_stream_handler(root, formatter)
    _ensure_file_handler(root, formatter, log_path)

    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("elevenlabs").setLevel(logging.WARNING)
    return log_path


def sanitize_log_value(value: Any, default: str = "none") -> str:
    """Убрать переводы строк и управляющие символы из значения журнала."""
    if value is None:
        return default
    text = str(value)
    cleaned: list[str] = []
    for char in text:
        if char in {"\r", "\n", "\t"} or ord(char) < 32:
            cleaned.append("_")
        else:
            cleaned.append(char)
    result = "".join(cleaned).strip()
    return result or default


def format_log_event(event: str, **fields: Any) -> str:
    """Собрать строку журнала в формате key=value."""
    parts = [f"event={sanitize_log_value(event)}"]
    for key, value in fields.items():
        parts.append(f"{key}={sanitize_log_value(value)}")
    return " ".join(parts)


def voice_id_tail(voice_id: str | None) -> str:
    """Вернуть только последние четыре символа Voice ID."""
    if not voice_id:
        return "none"
    return voice_id[-4:]


def _ensure_stream_handler(root: logging.Logger, formatter: logging.Formatter) -> None:
    if _has_handler(root, "stream"):
        return
    stream = logging.StreamHandler(_utf8_stream())
    stream.setLevel(logging.INFO)
    stream.setFormatter(formatter)
    setattr(stream, HANDLER_MARK, "stream")
    root.addHandler(stream)


def _ensure_file_handler(
    root: logging.Logger,
    formatter: logging.Formatter,
    log_path: Path,
) -> None:
    target = str(log_path.resolve())
    for handler in root.handlers:
        if getattr(handler, HANDLER_MARK, None) != "file":
            continue
        if getattr(handler, "baseFilename", None) == target:
            return
    handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        delay=False,
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    setattr(handler, HANDLER_MARK, "file")
    root.addHandler(handler)


def _has_handler(root: logging.Logger, kind: str) -> bool:
    return any(getattr(handler, HANDLER_MARK, None) == kind for handler in root.handlers)


def _utf8_stream():
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError, AttributeError):
            pass
    return stream
