"""Последовательная генерация длинного текста и сборка MP3."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from logging_config import format_log_event
from services.audio_service import (
    TELEGRAM_UPLOAD_SOFT_LIMIT_BYTES,
    AudioMergeError,
    group_chunks_for_telegram,
    merge_audio_chunks,
)
from services.tts_service import TTSService

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True)
class GenerationSnapshot:
    telegram_user_id: int
    voice_id: str
    voice_key: str
    voice_name: str
    speech_speed: float
    model_id: str
    char_count: int
    chunks: list[str]
    credit_multiplier: float


class LongTTSInterrupted(Exception):
    """Генерация остановлена из-за ошибки фрагмента."""

    def __init__(self, completed_chunks: int, processed_characters: int, cause: Exception) -> None:
        super().__init__(str(cause))
        self.completed_chunks = completed_chunks
        self.processed_characters = processed_characters
        self.cause = cause


def create_job_directory(temp_root: Path) -> tuple[str, Path]:
    job_id = str(uuid.uuid4())
    job_dir = temp_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_id, job_dir


async def generate_chunks_sequentially(
    tts_service: TTSService,
    snapshot: GenerationSnapshot,
    job_dir: Path,
    job_id: str,
    progress: ProgressCallback | None = None,
) -> list[Path]:
    """Озвучить фрагменты строго по порядку одним снимком настроек."""
    paths: list[Path] = []
    processed_characters = 0
    for index, chunk in enumerate(snapshot.chunks):
        if progress is not None:
            await progress(index + 1, len(snapshot.chunks))
        previous_text = snapshot.chunks[index - 1] if index > 0 else None
        next_text = snapshot.chunks[index + 1] if index + 1 < len(snapshot.chunks) else None
        output_path = job_dir / f"chunk_{index + 1:03d}.mp3"
        logger.info(
            format_log_event(
                "tts_chunk_started",
                job_id=job_id,
                telegram_user_id=snapshot.telegram_user_id,
                chunk=index + 1,
                chunks=len(snapshot.chunks),
                chars=len(chunk),
                voice_key=snapshot.voice_key,
                speech_speed=snapshot.speech_speed,
            )
        )
        try:
            await asyncio.to_thread(
                tts_service.generate_speech,
                chunk,
                output_path,
                snapshot.voice_id,
                snapshot.speech_speed,
                previous_text,
                next_text,
            )
        except Exception as exc:
            logger.info(
                format_log_event(
                    "tts_chunk_failed",
                    job_id=job_id,
                    telegram_user_id=snapshot.telegram_user_id,
                    chunk=index + 1,
                    chunks=len(snapshot.chunks),
                    exception_class=type(exc).__name__,
                )
            )
            raise LongTTSInterrupted(len(paths), processed_characters, exc) from exc
        paths.append(output_path)
        processed_characters += len(chunk)
        logger.info(
            format_log_event(
                "tts_chunk_completed",
                job_id=job_id,
                telegram_user_id=snapshot.telegram_user_id,
                chunk=index + 1,
                chunks=len(snapshot.chunks),
                chars=len(chunk),
            )
        )
    return paths


async def merge_job_outputs(
    chunk_paths: list[Path],
    job_dir: Path,
    pause_ms: int,
    job_id: str,
) -> list[Path]:
    logger.info(format_log_event("audio_merge_started", job_id=job_id, chunks=len(chunk_paths)))
    groups = group_chunks_for_telegram(chunk_paths, TELEGRAM_UPLOAD_SOFT_LIMIT_BYTES)
    outputs: list[Path] = []
    try:
        for index, group in enumerate(groups, start=1):
            output_path = job_dir / (f"result_{index:03d}.mp3" if len(groups) > 1 else "result.mp3")
            await asyncio.to_thread(merge_audio_chunks, group, output_path, pause_ms)
            outputs.append(output_path)
    except AudioMergeError:
        raise
    logger.info(
        format_log_event(
            "audio_merge_completed",
            job_id=job_id,
            files=len(outputs),
        )
    )
    return outputs
