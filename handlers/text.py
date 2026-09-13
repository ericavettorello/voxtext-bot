"""Обработчик обычных текстовых сообщений и запуск озвучивания."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal

from aiogram import F, Router
from aiogram.types import FSInputFile, Message, User
from aiogram.utils.chat_action import ChatActionSender

from database.db import DEFAULT_VOICE_KEY, Database
from services.daily_quota import (
    DailyQuotaError,
    DailyQuotaSettings,
    append_remaining,
    format_quota_denied,
    log_quota_completed,
    release_daily_quota,
    try_reserve_daily_quota,
)
from services.speech_speed import DEFAULT_SPEECH_SPEED, resolve_speech_speed
from logging_config import format_log_event, sanitize_log_value, voice_id_tail
from services.tts_service import (
    MODEL_ID,
    TTSError,
    TTSService,
    create_temp_mp3_path,
    delete_temp_file,
    describe_tts_failure,
)
from services.usage_estimator import estimate_cost_usd, estimate_credits
from services.voice_catalog import VoiceCatalog, VoiceOption
from texts import EMPTY_TEXT_MESSAGE

logger = logging.getLogger(__name__)

router = Router(name="text")

MAX_TEXT_LENGTH = 3500
MENU_BUTTONS = frozenset(
    {
        "Выбрать голос",
        "Моя статистика",
        "Настроить скорость",
        "⏱ Скорость",
        "📚 Длинный текст",
        "📄 Загрузить файл",
        "🎧 Озвучить",
        "🗑 Очистить текст",
        "❌ Отмена",
        "Помощь",
    }
)

TOO_LONG_MESSAGE = (
    "Сейчас в обычном режиме можно озвучить до 3 500 символов. "
    "Для более длинного текста нажмите «📚 Длинный текст»."
)
BUSY_MESSAGE = "Ваша предыдущая озвучка ещё создаётся. Пожалуйста, дождитесь результата."
ACCEPTED_MESSAGE = "Текст принят. Начинаю озвучивание, это может занять некоторое время…"
AUDIO_CAPTION = "Готово! Ваш текст преобразован в аудио."
AUDIO_TITLE = "VoxText"
AUDIO_PERFORMER = "VoxText Bot"


class ActiveJobs:
    """Защита от параллельных озвучек одного пользователя."""

    def __init__(self) -> None:
        self._user_ids: set[int] = set()

    def try_acquire(self, user_id: int) -> bool:
        if user_id in self._user_ids:
            return False
        self._user_ids.add(user_id)
        return True

    def release(self, user_id: int) -> None:
        self._user_ids.discard(user_id)

    def __contains__(self, user_id: int) -> bool:
        return user_id in self._user_ids


active_jobs = ActiveJobs()


def is_menu_button(text: str) -> bool:
    return text in MENU_BUTTONS


def validate_user_text(text: str) -> str | None:
    """Вернуть текст ошибки или None, если текст можно озвучивать."""
    if not text.strip():
        return EMPTY_TEXT_MESSAGE
    if len(text) > MAX_TEXT_LENGTH:
        return TOO_LONG_MESSAGE
    return None


def _username(user: User | None) -> str:
    if user is None:
        return "none"
    return sanitize_log_value(user.username)


def _user_id(user: User | None) -> int | str:
    return user.id if user is not None else "none"


async def _safe_upsert_user(
    database: Database | None,
    user: User,
    default_voice_id: str,
) -> dict | None:
    if database is None:
        return None
    try:
        return await database.upsert_user(
            telegram_user_id=user.id,
            username=user.username,
            first_name=getattr(user, "first_name", None),
            last_name=getattr(user, "last_name", None),
            language_code=getattr(user, "language_code", None),
            default_voice_id=default_voice_id or None,
        )
    except Exception:
        logger.exception(
            format_log_event(
                "database_operation_failed",
                operation="upsert_user",
                telegram_user_id=user.id,
            )
        )
        return None


async def _safe_create_tts_request(
    database: Database | None,
    request_id: str,
    telegram_user_id: int,
    char_count: int,
    voice_id: str,
    voice_key: str,
    speech_speed: float,
) -> bool:
    if database is None:
        return False
    try:
        await database.create_tts_request(
            request_id=request_id,
            telegram_user_id=telegram_user_id,
            char_count=char_count,
            model_id=MODEL_ID,
            voice_id=voice_id,
            voice_key=voice_key,
            speech_speed=speech_speed,
        )
        return True
    except Exception:
        logger.exception(
            format_log_event(
                "database_operation_failed",
                operation="create_tts_request",
                request_id=request_id,
                telegram_user_id=telegram_user_id,
            )
        )
        return False


async def _safe_mark_success(
    database: Database | None,
    request_recorded: bool,
    request_id: str,
    audio_size_bytes: int,
    duration_ms: int,
    estimated_credits: int | None = None,
    estimated_cost_usd: Decimal | None = None,
) -> None:
    if database is None or not request_recorded:
        return
    try:
        await database.mark_tts_request_success(
            request_id=request_id,
            audio_size_bytes=audio_size_bytes,
            duration_ms=duration_ms,
            estimated_credits=estimated_credits,
            estimated_cost_usd=estimated_cost_usd,
        )
    except Exception:
        logger.exception(
            format_log_event(
                "database_operation_failed",
                operation="mark_tts_request_success",
                request_id=request_id,
            )
        )


async def _safe_mark_failed(
    database: Database | None,
    request_recorded: bool,
    request_id: str,
    error_code: str | None,
    duration_ms: int | None = None,
) -> None:
    if database is None or not request_recorded:
        return
    try:
        await database.mark_tts_request_failed(
            request_id,
            error_code,
            duration_ms=duration_ms,
        )
    except Exception:
        logger.exception(
            format_log_event(
                "database_operation_failed",
                operation="mark_tts_request_failed",
                request_id=request_id,
                error_code=error_code,
            )
        )


async def _credit_multiplier(
    database: Database | None,
    voice_key: str,
    fallback: float,
) -> float:
    if database is None:
        return fallback
    try:
        stored = await database.get_voice_by_key(voice_key)
        if stored is not None and stored.get("credit_multiplier") is not None:
            return float(stored["credit_multiplier"])
    except Exception:
        logger.exception(
            format_log_event(
                "database_operation_failed",
                operation="get_voice_by_key",
                voice_key=voice_key,
            )
        )
    return fallback


async def resolve_user_voice(
    database: Database | None,
    telegram_user_id: int,
    db_user: dict | None,
    voice_catalog: VoiceCatalog | None,
    default_voice_id: str,
    fallback_voice_id: str,
) -> tuple[str, str, float]:
    """Вернуть Voice ID, ключ и множитель кредитов. При недоступном голосе — основной."""
    selected_key = DEFAULT_VOICE_KEY
    if db_user and db_user.get("selected_voice_key"):
        selected_key = str(db_user["selected_voice_key"])

    voice: VoiceOption | None = None
    fallback = False
    if voice_catalog is not None:
        voice = voice_catalog.get(selected_key)
        if voice is None:
            voice = voice_catalog.default
            fallback = selected_key != DEFAULT_VOICE_KEY

    used_voice_id = voice.voice_id if voice is not None else (default_voice_id or fallback_voice_id)
    used_voice_key = voice.key if voice is not None else DEFAULT_VOICE_KEY
    multiplier = voice.credit_multiplier if voice is not None else 1.0
    multiplier = await _credit_multiplier(database, used_voice_key, multiplier)

    if fallback and database is not None:
        try:
            await database.update_user_voice(
                telegram_user_id,
                DEFAULT_VOICE_KEY,
                voice.voice_id if voice is not None else used_voice_id,
            )
        except Exception:
            logger.exception(
                format_log_event(
                    "database_operation_failed",
                    operation="update_user_voice",
                    telegram_user_id=telegram_user_id,
                    voice_key=DEFAULT_VOICE_KEY,
                )
            )
        logger.info(
            format_log_event(
                "voice_fallback",
                telegram_user_id=telegram_user_id,
                voice_key=DEFAULT_VOICE_KEY,
                previous_voice_key=selected_key,
            )
        )

    return used_voice_id, used_voice_key, multiplier


@router.message(F.text, ~F.text.startswith("/"))
async def handle_text_message(
    message: Message,
    tts_service: TTSService,
    database: Database | None = None,
    default_voice_id: str = "",
    voice_catalog: VoiceCatalog | None = None,
    plan_price_usd: Decimal | None = None,
    plan_credits: int | None = None,
    quota_settings: DailyQuotaSettings | None = None,
    admin_ids: set[int] | None = None,
) -> None:
    text = message.text or ""
    if text.startswith("/") or is_menu_button(text):
        return

    user = message.from_user
    error = validate_user_text(text)
    if error:
        reason = "empty" if error == EMPTY_TEXT_MESSAGE else "too_long"
        extra = {"reason": reason, "user_id": _user_id(user)}
        if reason == "too_long":
            extra["chars"] = len(text)
        logger.warning(format_log_event("input_rejected", **extra))
        await message.answer(error)
        return

    if user is None:
        logger.warning(format_log_event("input_rejected", reason="empty", user_id="none"))
        await message.answer(EMPTY_TEXT_MESSAGE)
        return

    if not active_jobs.try_acquire(user.id):
        logger.warning(
            format_log_event(
                "request_rejected",
                reason="already_processing",
                user_id=user.id,
            )
        )
        await message.answer(BUSY_MESSAGE)
        return

    request_id = str(uuid.uuid4())
    started_at = time.perf_counter()
    output_path = None
    request_recorded = False
    speech_speed = DEFAULT_SPEECH_SPEED
    used_voice_key = DEFAULT_VOICE_KEY
    quota_result = None
    tts_invoked = False
    try:
        db_user = await _safe_upsert_user(database, user, default_voice_id)
        speech_speed = resolve_speech_speed(
            float(db_user["speech_speed"]) if db_user and db_user.get("speech_speed") is not None else None
        )

        used_voice_id, used_voice_key, multiplier = await resolve_user_voice(
            database=database,
            telegram_user_id=user.id,
            db_user=db_user,
            voice_catalog=voice_catalog,
            default_voice_id=default_voice_id,
            fallback_voice_id=tts_service.voice_id,
        )

        if quota_settings is not None and database is not None:
            try:
                quota_result = await try_reserve_daily_quota(
                    database,
                    user.id,
                    len(text),
                    quota_settings,
                    admin_ids=admin_ids,
                    source_type="text",
                )
            except DailyQuotaError as exc:
                await message.answer(exc.user_message)
                return
            if not quota_result.allowed:
                await message.answer(format_quota_denied(quota_result, len(text)))
                return

        logger.info(
            format_log_event(
                "tts_requested",
                request_id=request_id,
                user_id=user.id,
                username=_username(user),
                chat_id=message.chat.id,
                message_id=message.message_id,
                chars=len(text),
                voice_key=used_voice_key,
                speech_speed=speech_speed,
            )
        )
        request_recorded = await _safe_create_tts_request(
            database,
            request_id=request_id,
            telegram_user_id=user.id,
            char_count=len(text),
            voice_id=used_voice_id,
            voice_key=used_voice_key,
            speech_speed=speech_speed,
        )
        await message.answer(ACCEPTED_MESSAGE)
        output_path = create_temp_mp3_path()
        logger.info(
            format_log_event(
                "tts_started",
                request_id=request_id,
                user_id=user.id,
                chars=len(text),
                model=MODEL_ID,
                voice_key=used_voice_key,
                speech_speed=speech_speed,
                voice_id_tail=voice_id_tail(used_voice_id),
            )
        )

        generation_started = time.perf_counter()
        tts_invoked = True
        async with ChatActionSender.upload_voice(
            bot=message.bot,
            chat_id=message.chat.id,
        ):
            await asyncio.to_thread(
                tts_service.generate_speech,
                text,
                output_path,
                used_voice_id,
                speech_speed,
            )
        generation_ms = int((time.perf_counter() - generation_started) * 1000)
        file_size = output_path.stat().st_size
        logger.info(
            format_log_event(
                "tts_generated",
                request_id=request_id,
                user_id=user.id,
                size_bytes=file_size,
                duration_ms=generation_ms,
                chars=len(text),
                voice_key=used_voice_key,
                speech_speed=speech_speed,
            )
        )

        audio = FSInputFile(output_path, filename="VoxText.mp3")
        await message.answer_audio(
            audio=audio,
            title=AUDIO_TITLE,
            performer=AUDIO_PERFORMER,
            caption=append_remaining(AUDIO_CAPTION, quota_result),
        )
        total_ms = int((time.perf_counter() - started_at) * 1000)
        estimated = estimate_credits(len(text), multiplier)
        estimated_cost = estimate_cost_usd(estimated, plan_price_usd, plan_credits)
        logger.info(
            format_log_event(
                "audio_sent",
                request_id=request_id,
                user_id=user.id,
                size_bytes=file_size,
                duration_ms=total_ms,
                voice_key=used_voice_key,
                speech_speed=speech_speed,
            )
        )
        logger.info(
            format_log_event(
                "tts_completed",
                request_id=request_id,
                user_id=user.id,
                voice_key=used_voice_key,
                speech_speed=speech_speed,
                chars=len(text),
                status="success",
                duration_ms=total_ms,
            )
        )
        await _safe_mark_success(
            database,
            request_recorded,
            request_id,
            file_size,
            total_ms,
            estimated_credits=estimated,
            estimated_cost_usd=estimated_cost,
        )
        log_quota_completed(quota_result, user.id, "text", len(text))
    except TTSError as exc:
        details = describe_tts_failure(exc)
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        logger.error(
            format_log_event(
                "tts_failed",
                request_id=request_id,
                user_id=user.id,
                exception_class=details["exception_class"],
                status=details["status_code"],
                error_code=details["error_code"],
                error_kind=details["error_kind"],
                error_message=details["error_message"],
                duration_ms=duration_ms,
                speech_speed=speech_speed,
                voice_key=used_voice_key,
            )
        )
        await _safe_mark_failed(
            database,
            request_recorded,
            request_id,
            details["error_kind"] or details["error_code"],
            duration_ms=duration_ms,
        )
        await message.answer(exc.user_message)
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        logger.exception(
            format_log_event(
                "tts_failed",
                request_id=request_id,
                user_id=user.id,
                exception_class=type(exc).__name__,
                error_kind="unknown",
                duration_ms=duration_ms,
            )
        )
        await _safe_mark_failed(
            database,
            request_recorded,
            request_id,
            "unknown",
            duration_ms=duration_ms,
        )
        await message.answer("Не удалось создать аудио. Подробности записаны в журнал.")
    finally:
        if quota_result is not None and quota_result.reserved and not tts_invoked and database is not None:
            await release_daily_quota(database, quota_result, user.id, len(text), "text")
        delete_temp_file(output_path, request_id=request_id)
        active_jobs.release(user.id)
