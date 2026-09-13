"""Безопасные имена файлов для показа пользователю и отправки MP3."""

from __future__ import annotations

import html
import re
from pathlib import Path

MAX_DISPLAY_NAME_LENGTH = 80
FALLBACK_AUDIO_NAME = "VoxText_audio.mp3"
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def original_extension(file_name: str | None) -> str:
    """Вернуть нижний регистр расширения без точки."""
    if not file_name:
        return ""
    return Path(file_name.replace("\\", "/")).suffix.lower().lstrip(".")


def sanitize_display_name(file_name: str | None) -> str:
    """Убрать путь, управляющие символы и ограничить длину."""
    raw = (file_name or "").replace("\\", "/").split("/")[-1]
    cleaned = "".join(ch for ch in raw if ord(ch) >= 32)
    cleaned = cleaned.strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        return "document"
    if len(cleaned) > MAX_DISPLAY_NAME_LENGTH:
        stem = Path(cleaned).stem[: MAX_DISPLAY_NAME_LENGTH - 8]
        suffix = Path(cleaned).suffix[:8]
        cleaned = f"{stem}{suffix}" if suffix else stem
    return cleaned or "document"


def escape_user_filename(file_name: str | None) -> str:
    """Экранировать имя для вставки в HTML или обычный текст."""
    return html.escape(sanitize_display_name(file_name), quote=True)


def audio_filename_from_document(file_name: str | None) -> str:
    """Собрать имя MP3 без двойного расширения исходного файла."""
    display = sanitize_display_name(file_name)
    stem = Path(display).stem.strip(" .")
    stem = _UNSAFE_CHARS.sub("_", stem).strip(" ._")
    if not stem:
        return FALLBACK_AUDIO_NAME
    if len(stem) > MAX_DISPLAY_NAME_LENGTH:
        stem = stem[:MAX_DISPLAY_NAME_LENGTH].rstrip(" ._")
    return f"{stem or 'VoxText_audio'}.mp3"
