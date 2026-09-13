"""Объединение MP3-фрагментов через pydub и FFmpeg из imageio-ffmpeg."""

from __future__ import annotations

import gc
import logging
import shutil
import time
from pathlib import Path

import imageio_ffmpeg
from pydub import AudioSegment

from logging_config import format_log_event

logger = logging.getLogger(__name__)

TELEGRAM_AUDIO_LIMIT_BYTES = 49 * 1024 * 1024
# Мягкий лимит отправки: один файл меньше 49 МБ, чтобы уложиться в таймаут Telegram.
TELEGRAM_UPLOAD_SOFT_LIMIT_BYTES = 20 * 1024 * 1024
_DELETE_ATTEMPTS = 5


class AudioMergeError(Exception):
    """Ошибка объединения или экспорта аудио."""


def _configure_ffmpeg() -> str:
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    AudioSegment.converter = ffmpeg_path
    AudioSegment.ffmpeg = ffmpeg_path
    return ffmpeg_path


def merge_audio_chunks(
    chunk_paths: list[Path],
    output_path: Path,
    pause_ms: int = 150,
) -> Path:
    """Склеить MP3 с паузой между частями. Без crossfade и без бинарной склейки."""
    if not chunk_paths:
        raise AudioMergeError("Нет аудиофрагментов для объединения.")
    _configure_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        combined = AudioSegment.empty()
        silence = AudioSegment.silent(duration=max(0, int(pause_ms)))
        for index, chunk_path in enumerate(chunk_paths):
            segment = AudioSegment.from_file(chunk_path)
            if index:
                combined += silence
            combined += segment
        exported = combined.export(
            output_path, format="mp3", bitrate="128k", parameters=["-ar", "44100"]
        )
        if exported is not None:
            exported.close()
    except AudioMergeError:
        raise
    except Exception as exc:
        raise AudioMergeError("Не удалось объединить аудиофрагменты.") from exc
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise AudioMergeError("Итоговый аудиофайл пустой.")
    return output_path


def group_chunks_for_telegram(
    chunk_paths: list[Path],
    max_bytes: int = TELEGRAM_AUDIO_LIMIT_BYTES,
) -> list[list[Path]]:
    """Сгруппировать готовые фрагменты так, чтобы каждый пакет был меньше лимита."""
    groups: list[list[Path]] = []
    current: list[Path] = []
    current_size = 0
    for path in chunk_paths:
        size = path.stat().st_size
        if size >= max_bytes:
            if current:
                groups.append(current)
                current = []
                current_size = 0
            groups.append([path])
            continue
        if current and current_size + size >= max_bytes:
            groups.append(current)
            current = [path]
            current_size = size
        else:
            current.append(path)
            current_size += size
    if current:
        groups.append(current)
    return groups


def delete_job_directory(job_dir: Path | None, job_id: str | None = None) -> None:
    """Удалить только каталог одного задания, не всю папку temp."""
    if job_dir is None or not job_dir.exists():
        return
    last_error: OSError | None = None
    for attempt in range(1, _DELETE_ATTEMPTS + 1):
        gc.collect()
        try:
            shutil.rmtree(job_dir)
            logger.info(
                format_log_event(
                    "temporary_files_cleaned",
                    job_id=job_id or "none",
                    path_name=job_dir.name,
                    attempt=attempt,
                )
            )
            return
        except OSError as exc:
            last_error = exc
            if attempt < _DELETE_ATTEMPTS:
                time.sleep(0.4 * attempt)
    logger.exception(
        format_log_event(
            "temporary_files_cleaned",
            job_id=job_id or "none",
            status="failed",
        ),
        exc_info=last_error,
    )
