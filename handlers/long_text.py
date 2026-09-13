"""Режим накопления и озвучивания длинного текста."""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from database.db import REQUEST_TYPE_LONG, SOURCE_TYPE_LONG_TEXT, Database
from handlers.start import build_main_keyboard
from handlers.text import BUSY_MESSAGE, active_jobs, resolve_user_voice
from logging_config import format_log_event
from services.document_store import document_store
from services.draft_store import draft_store
from services.long_tts import GenerationSnapshot
from services.long_tts_job import (
    LongTTSRunOptions,
    run_confirmed_long_tts,
    safe_callback_answer as _safe_callback_answer,
    send_audio_file as _send_audio_file,
)
from services.speech_speed import get_speed_by_value
from services.tts_service import MODEL_ID, TTSService
from services.usage_estimator import estimate_cost_usd, estimate_credits
from services.voice_catalog import VoiceCatalog
from texts import LONG_TEXT_HINT
from utils.text_splitter import split_text_into_chunks

logger = logging.getLogger(__name__)

router = Router(name="long_text")

BUTTON_LONG_TEXT = "📚 Длинный текст"
BUTTON_SPEAK = "🎧 Озвучить"
BUTTON_CLEAR = "🗑 Очистить текст"
BUTTON_CANCEL = "❌ Отмена"

EMPTY_DRAFT = "Черновик пуст. Отправьте текст частями или нажмите «Отмена»."


class LongTextStates(StatesGroup):
    collecting = State()
    confirming = State()


def build_long_text_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_SPEAK)],
            [KeyboardButton(text=BUTTON_CLEAR)],
            [KeyboardButton(text=BUTTON_CANCEL)],
        ],
        resize_keyboard=True,
    )


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Начать озвучивание", callback_data="long:start")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="long:cancel")],
        ]
    )


def format_char_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _too_large_message(current: int, limit: int) -> str:
    return (
        "Текст слишком большой.\n"
        f"Максимальный объём: {format_char_count(limit)} символов.\n"
        f"Сейчас добавлено: {format_char_count(current)} символов."
    )


@router.message(F.text == BUTTON_LONG_TEXT)
async def start_long_text_mode(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    if user.id in active_jobs:
        await message.answer(BUSY_MESSAGE)
        return
    leftover = document_store.remove(user.id)
    if leftover is not None:
        from services.audio_service import delete_job_directory

        delete_job_directory(leftover.job_dir, leftover.job_id)
    draft_store.start(user.id)
    await state.set_state(LongTextStates.collecting)
    logger.info(format_log_event("long_text_mode_started", telegram_user_id=user.id))
    await message.answer(LONG_TEXT_HINT, reply_markup=build_long_text_keyboard())


@router.message(LongTextStates.collecting, F.text == BUTTON_CLEAR)
async def clear_long_text(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    draft_store.clear(user.id)
    logger.info(format_log_event("long_text_cleared", telegram_user_id=user.id))
    await message.answer("Черновик очищен. Можете отправлять текст заново.")


@router.message(LongTextStates.collecting, F.text == BUTTON_CANCEL)
@router.message(LongTextStates.confirming, F.text == BUTTON_CANCEL)
async def cancel_long_text(message: Message, state: FSMContext) -> None:
    await _cancel_mode(message, state)


@router.message(LongTextStates.collecting, F.text == BUTTON_SPEAK)
async def request_long_text_confirmation(
    message: Message,
    state: FSMContext,
    database: Database | None = None,
    voice_catalog: VoiceCatalog | None = None,
    default_voice_id: str = "",
    tts_service: TTSService | None = None,
    tts_chunk_size: int = 4500,
    max_long_text_chars: int = 30000,
    plan_price_usd: Decimal | None = None,
    plan_credits: int | None = None,
) -> None:
    user = message.from_user
    if user is None:
        return
    if user.id in active_jobs:
        await message.answer(BUSY_MESSAGE)
        return
    draft = draft_store.get(user.id)
    text = draft.text if draft else ""
    if not text.strip():
        await message.answer(EMPTY_DRAFT)
        return
    if len(text) > max_long_text_chars:
        logger.info(
            format_log_event(
                "long_text_cancelled",
                telegram_user_id=user.id,
                reason="too_long",
                chars=len(text),
            )
        )
        await message.answer(_too_large_message(len(text), max_long_text_chars))
        return

    chunks = split_text_into_chunks(text, tts_chunk_size)
    logger.info(
        format_log_event(
            "text_split_completed",
            telegram_user_id=user.id,
            chars=len(text),
            chunks=len(chunks),
        )
    )
    db_user = None
    if database is not None:
        try:
            db_user = await database.upsert_user(
                telegram_user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code,
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
    fallback_voice = tts_service.voice_id if tts_service is not None else default_voice_id
    voice_id, voice_key, multiplier = await resolve_user_voice(
        database,
        user.id,
        db_user,
        voice_catalog,
        default_voice_id,
        fallback_voice,
    )
    speed = float(db_user["speech_speed"]) if db_user and db_user.get("speech_speed") is not None else 1.0
    voice_name = "Основной голос"
    if voice_catalog is not None:
        voice_name = voice_catalog.get_display_name(voice_key) or voice_catalog.default.display_name
    credits = estimate_credits(len(text), multiplier)
    cost = estimate_cost_usd(credits, plan_price_usd, plan_credits)
    lines = [
        "Текст подготовлен к озвучиванию.",
        "",
        f"Символов: {format_char_count(len(text))}",
        f"Фрагментов: {len(chunks)}",
        f"Голос: {voice_name}",
        f"Скорость: {get_speed_by_value(speed).value}×",
        f"Ориентировочный расход: {credits} кредитов",
    ]
    if cost is not None:
        lines.append(f"Ориентировочная стоимость: {format(cost, 'f')} USD")
    await state.set_state(LongTextStates.confirming)
    await state.update_data(
        voice_id=voice_id,
        voice_key=voice_key,
        voice_name=voice_name,
        speech_speed=speed,
        multiplier=multiplier,
        char_count=len(text),
        chunk_count=len(chunks),
    )
    logger.info(
        format_log_event(
            "long_text_confirmed",
            telegram_user_id=user.id,
            chars=len(text),
            chunks=len(chunks),
            voice_key=voice_key,
            speech_speed=speed,
            status="awaiting",
        )
    )
    await message.answer("\n".join(lines), reply_markup=build_confirm_keyboard())


@router.callback_query(LongTextStates.confirming, F.data == "long:cancel")
async def cancel_long_text_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is not None:
        await _cancel_mode(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "long:start")
async def start_long_generation(
    callback: CallbackQuery,
    state: FSMContext,
    tts_service: TTSService,
    database: Database | None = None,
    tts_chunk_size: int = 4500,
    tts_chunk_pause_ms: int = 150,
    plan_price_usd: Decimal | None = None,
    plan_credits: int | None = None,
    quota_settings=None,
    admin_ids: set[int] | None = None,
) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    current = await state.get_state()
    if current != LongTextStates.confirming.state:
        await callback.answer()
        return
    if not active_jobs.try_acquire(user.id):
        if callback.message is not None:
            await callback.message.answer(BUSY_MESSAGE)
        await callback.answer()
        return

    draft = draft_store.get(user.id)
    text = draft.text if draft else ""
    data = await state.get_data()
    if not text.strip():
        active_jobs.release(user.id)
        if callback.message is not None:
            await callback.message.answer(EMPTY_DRAFT)
        await _safe_callback_answer(callback)
        return

    await _safe_callback_answer(callback)

    chunks = split_text_into_chunks(text, tts_chunk_size)
    snapshot = GenerationSnapshot(
        telegram_user_id=user.id,
        voice_id=str(data.get("voice_id") or tts_service.voice_id),
        voice_key=str(data.get("voice_key") or "default"),
        voice_name=str(data.get("voice_name") or "Основной голос"),
        speech_speed=float(data.get("speech_speed") or 1.0),
        model_id=MODEL_ID,
        char_count=len(text),
        chunks=chunks,
        credit_multiplier=float(data.get("multiplier") or 1.0),
    )
    chat_id = callback.message.chat.id if callback.message else user.id
    try:
        await run_confirmed_long_tts(
            bot=callback.bot,
            chat_id=chat_id,
            status_message=callback.message,
            tts_service=tts_service,
            snapshot=snapshot,
            database=database,
            tts_chunk_pause_ms=tts_chunk_pause_ms,
            plan_price_usd=plan_price_usd,
            plan_credits=plan_credits,
            quota_settings=quota_settings,
            admin_ids=admin_ids,
            options=LongTTSRunOptions(
                source_type=SOURCE_TYPE_LONG_TEXT,
                request_type=REQUEST_TYPE_LONG,
                completed_event="long_tts_completed",
                failed_event="long_tts_failed",
            ),
        )
    finally:
        draft_store.remove(user.id)
        await state.clear()
        active_jobs.release(user.id)


@router.message(LongTextStates.collecting, F.text)
@router.message(LongTextStates.confirming, F.text)
async def collect_long_text_part(message: Message, state: FSMContext) -> None:
    user = message.from_user
    text = message.text or ""
    if user is None or not text.strip():
        return
    if text in {
        BUTTON_LONG_TEXT,
        BUTTON_SPEAK,
        BUTTON_CLEAR,
        BUTTON_CANCEL,
        "Выбрать голос",
        "Моя статистика",
        "⏱ Скорость",
        "Настроить скорость",
        "📄 Загрузить файл",
        "Помощь",
    }:
        return
    if user.id in active_jobs:
        await message.answer(BUSY_MESSAGE)
        return
    await state.set_state(LongTextStates.collecting)
    draft = draft_store.add_part(user.id, text)
    logger.info(
        format_log_event(
            "long_text_part_added",
            telegram_user_id=user.id,
            chars=draft.char_count,
            parts=draft.parts_count,
        )
    )
    await message.answer(
        "Часть добавлена.\n"
        f"Всего символов: {format_char_count(draft.char_count)}\n"
        f"Получено частей: {draft.parts_count}"
    )


async def _cancel_mode(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is not None:
        draft_store.remove(user.id)
        logger.info(format_log_event("long_text_cancelled", telegram_user_id=user.id))
    await state.clear()
    await message.answer("Режим длинного текста отменён.", reply_markup=build_main_keyboard())
