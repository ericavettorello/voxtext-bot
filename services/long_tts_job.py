"""Общий запуск длинной озвучки: генерация, склейка, отправка и учёт."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import FSInputFile, Message
from aiogram.utils.chat_action import ChatActionSender

from database.db import REQUEST_TYPE_LONG, Database
from handlers.start import build_main_keyboard
from logging_config import format_log_event
from services.daily_quota import (
    DailyQuotaError,
    DailyQuotaSettings,
    append_remaining,
    format_quota_denied,
    log_quota_completed,
    release_daily_quota,
    try_reserve_daily_quota,
)
from services.audio_service import delete_job_directory
from services.long_tts import (
    GenerationSnapshot,
    LongTTSInterrupted,
    create_job_directory,
    generate_chunks_sequentially,
    merge_job_outputs,
)
from services.tts_service import MODEL_ID, TEMP_DIR, TTSError, TTSQuotaError, TTSService
from services.usage_estimator import estimate_cost_usd, estimate_credits

logger = logging.getLogger(__name__)

AUDIO_TITLE = "VoxText"
AUDIO_PERFORMER = "VoxText Bot"

PARTIAL_FAILED = (
    "Не удалось полностью озвучить длинный текст.\n"
    "Обработано частей: {done} из {total}.\n"
    "Попробуйте повторить позже."
)
SEND_FAILED = (
    "Аудио создано, но не удалось отправить файл в Telegram.\n"
    "Попробуйте повторить позже."
)
AUDIO_SEND_TIMEOUT = 300
AUDIO_SEND_ATTEMPTS = 2


@dataclass(frozen=True)
class LongTTSRunOptions:
    source_type: str
    request_type: str = REQUEST_TYPE_LONG
    start_text: str = "Начинаю озвучивание длинного текста…"
    progress_template: str = "Озвучивание: фрагмент {current} из {total}"
    merge_text: str = "Объединяю аудио…"
    done_text: str = "Готово! Длинный текст преобразован в аудио."
    caption: str = AUDIO_TITLE
    audio_filename: str = "VoxText.mp3"
    completed_event: str = "long_tts_completed"
    failed_event: str = "long_tts_failed"
    job_id: str | None = None
    job_dir: Path | None = None
    page_count: int | None = None
    pages_with_text: int | None = None
    estimated_credits: int | None = None
    estimated_cost_usd: Decimal | None = None


async def run_confirmed_long_tts(
    *,
    bot: Any,
    chat_id: int,
    status_message: Message | None,
    tts_service: TTSService,
    snapshot: GenerationSnapshot,
    database: Database | None = None,
    tts_chunk_pause_ms: int = 150,
    plan_price_usd: Decimal | None = None,
    plan_credits: int | None = None,
    options: LongTTSRunOptions | None = None,
    on_finally: Callable[[], None] | None = None,
    quota_settings: DailyQuotaSettings | None = None,
    admin_ids: set[int] | None = None,
) -> bool:
    """Озвучить уже подготовленный текст существующим механизмом длинных текстов."""
    opts = options or LongTTSRunOptions(source_type="text")
    request_id = str(uuid.uuid4())
    job_id = opts.job_id
    job_dir = opts.job_dir
    if job_dir is None:
        job_id, job_dir = create_job_directory(TEMP_DIR)
    assert job_id is not None
    request_recorded = False
    started_at = time.perf_counter()
    quota_result = None
    paid_audio_started = False
    logger.info(
        format_log_event(
            opts.completed_event.replace("completed", "confirmed"),
            telegram_user_id=snapshot.telegram_user_id,
            job_id=job_id,
            request_id=request_id,
            chars=snapshot.char_count,
            chunks=len(snapshot.chunks),
            voice_key=snapshot.voice_key,
            speech_speed=snapshot.speech_speed,
            status="started",
        )
    )
    try:
        if quota_settings is not None and database is not None:
            try:
                quota_result = await try_reserve_daily_quota(
                    database,
                    snapshot.telegram_user_id,
                    snapshot.char_count,
                    quota_settings,
                    admin_ids=admin_ids,
                    source_type=opts.source_type,
                )
            except DailyQuotaError as exc:
                if status_message is not None:
                    await status_message.answer(exc.user_message, reply_markup=build_main_keyboard())
                return False
            if not quota_result.allowed:
                if status_message is not None:
                    await status_message.answer(
                        format_quota_denied(quota_result, snapshot.char_count),
                        reply_markup=build_main_keyboard(),
                    )
                return False
        if database is not None:
            try:
                await database.create_tts_request(
                    request_id=request_id,
                    telegram_user_id=snapshot.telegram_user_id,
                    char_count=snapshot.char_count,
                    model_id=MODEL_ID,
                    voice_id=snapshot.voice_id,
                    voice_key=snapshot.voice_key,
                    speech_speed=snapshot.speech_speed,
                    source_type=opts.source_type,
                    request_type=opts.request_type,
                    chunk_count=len(snapshot.chunks),
                    page_count=opts.page_count,
                    pages_with_text=opts.pages_with_text,
                    estimated_credits=opts.estimated_credits,
                    estimated_cost_usd=opts.estimated_cost_usd,
                )
                request_recorded = True
            except Exception:
                logger.exception(
                    format_log_event(
                        "database_operation_failed",
                        operation="create_tts_request",
                        request_id=request_id,
                    )
                )
        progress_message = None
        if status_message is not None:
            await status_message.answer(opts.start_text)
            progress_message = await status_message.answer(
                opts.progress_template.format(current=1, total=max(1, len(snapshot.chunks)))
            )

        async def progress(current_chunk: int, total: int) -> None:
            await edit_progress(
                progress_message,
                opts.progress_template.format(current=current_chunk, total=total),
            )

        async with ChatActionSender.upload_voice(bot=bot, chat_id=chat_id):
            chunk_paths = await generate_chunks_sequentially(
                tts_service,
                snapshot,
                job_dir,
                job_id,
                progress,
            )
            paid_audio_started = True
            if database is not None and request_recorded:
                await database.update_tts_request_progress(
                    request_id,
                    completed_chunks=len(chunk_paths),
                    processed_characters=sum(len(chunk) for chunk in snapshot.chunks),
                )
            await edit_progress(progress_message, opts.merge_text)
            outputs = await merge_job_outputs(chunk_paths, job_dir, tts_chunk_pause_ms, job_id)
            for index, output_path in enumerate(outputs, start=1):
                caption = opts.caption
                if len(outputs) > 1:
                    caption = f"Часть {index} из {len(outputs)}"
                await send_audio_file(
                    bot,
                    chat_id,
                    output_path,
                    caption,
                    filename=opts.audio_filename,
                )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        credits = estimate_credits(snapshot.char_count, snapshot.credit_multiplier)
        cost = estimate_cost_usd(credits, plan_price_usd, plan_credits)
        total_size = sum(path.stat().st_size for path in outputs)
        if database is not None and request_recorded:
            await database.mark_tts_request_success(
                request_id,
                audio_size_bytes=total_size,
                duration_ms=duration_ms,
                estimated_credits=credits,
                estimated_cost_usd=cost,
            )
        logger.info(
            format_log_event(
                opts.completed_event,
                telegram_user_id=snapshot.telegram_user_id,
                job_id=job_id,
                request_id=request_id,
                chars=snapshot.char_count,
                chunks=len(snapshot.chunks),
                duration_ms=duration_ms,
                status="success",
            )
        )
        if status_message is not None:
            await status_message.answer(
                append_remaining(opts.done_text, quota_result),
                reply_markup=build_main_keyboard(),
            )
        log_quota_completed(quota_result, snapshot.telegram_user_id, opts.source_type, snapshot.char_count)
        return True
    except LongTTSInterrupted as exc:
        paid_audio_started = exc.completed_chunks > 0
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        error_code = _error_code(exc.cause)
        if database is not None and request_recorded:
            try:
                if exc.completed_chunks > 0:
                    await database.mark_tts_request_partial_failed(
                        request_id,
                        error_code,
                        duration_ms=duration_ms,
                        completed_chunks=exc.completed_chunks,
                        processed_characters=exc.processed_characters,
                    )
                else:
                    await database.mark_tts_request_failed(
                        request_id, error_code, duration_ms=duration_ms
                    )
            except Exception:
                logger.exception(
                    format_log_event(
                        "database_operation_failed",
                        operation="mark_long_tts_failed",
                        request_id=request_id,
                    )
                )
        logger.info(
            format_log_event(
                opts.failed_event,
                telegram_user_id=snapshot.telegram_user_id,
                job_id=job_id,
                request_id=request_id,
                status="partial_failed" if exc.completed_chunks else "failed",
                exception_class=type(exc.cause).__name__,
                duration_ms=duration_ms,
            )
        )
        if status_message is not None:
            if isinstance(exc.cause, TTSQuotaError):
                await status_message.answer(exc.cause.user_message, reply_markup=build_main_keyboard())
            elif isinstance(exc.cause, TTSError):
                await status_message.answer(
                    PARTIAL_FAILED.format(done=exc.completed_chunks, total=len(snapshot.chunks)),
                    reply_markup=build_main_keyboard(),
                )
                await status_message.answer(exc.cause.user_message)
            else:
                await status_message.answer(
                    PARTIAL_FAILED.format(done=exc.completed_chunks, total=len(snapshot.chunks)),
                    reply_markup=build_main_keyboard(),
                )
        return False
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        logger.exception(
            format_log_event(
                opts.failed_event,
                telegram_user_id=snapshot.telegram_user_id,
                job_id=job_id,
                request_id=request_id,
                exception_class=type(exc).__name__,
                duration_ms=duration_ms,
            )
        )
        if database is not None and request_recorded:
            try:
                await database.mark_tts_request_failed(request_id, type(exc).__name__, duration_ms)
            except Exception:
                logger.exception(format_log_event("database_operation_failed", request_id=request_id))
        if status_message is not None:
            message = SEND_FAILED if isinstance(exc, TelegramNetworkError) else (
                "Не удалось озвучить длинный текст. Подробности записаны в журнал."
            )
            await status_message.answer(message, reply_markup=build_main_keyboard())
        return False
    finally:
        if quota_result is not None and quota_result.reserved and not paid_audio_started and database is not None:
            await release_daily_quota(
                database,
                quota_result,
                snapshot.telegram_user_id,
                snapshot.char_count,
                opts.source_type,
            )
        delete_job_directory(job_dir, job_id)
        if on_finally is not None:
            on_finally()


async def safe_callback_answer(callback) -> None:
    try:
        await callback.answer()
    except TelegramBadRequest as exc:
        details = str(exc).lower()
        if "query is too old" not in details and "query id is invalid" not in details:
            raise
    except TelegramNetworkError:
        logger.info(format_log_event("callback_answer_skipped", reason="network"))


async def send_audio_file(
    bot,
    chat_id: int,
    output_path: Path,
    caption: str,
    filename: str = "VoxText.mp3",
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, AUDIO_SEND_ATTEMPTS + 1):
        try:
            audio = FSInputFile(output_path, filename=filename)
            await bot.send_audio(
                chat_id=chat_id,
                audio=audio,
                title=AUDIO_TITLE,
                performer=AUDIO_PERFORMER,
                caption=caption,
                request_timeout=AUDIO_SEND_TIMEOUT,
            )
            return
        except TelegramNetworkError as exc:
            last_error = exc
            logger.info(
                format_log_event(
                    "audio_send_retry",
                    attempt=attempt,
                    attempts=AUDIO_SEND_ATTEMPTS,
                    exception_class=type(exc).__name__,
                )
            )
    if last_error is not None:
        raise last_error


async def edit_progress(message: Message | None, text: str) -> None:
    if message is None:
        return
    try:
        await message.edit_text(text)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            logger.exception(format_log_event("database_operation_failed", operation="edit_progress"))


def _error_code(exc: Exception) -> str:
    if isinstance(exc, TTSError):
        from services.tts_service import describe_tts_failure

        details = describe_tts_failure(exc)
        return details.get("error_kind") or details.get("error_code") or type(exc).__name__
    return type(exc).__name__
