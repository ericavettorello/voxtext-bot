"""Обработчики кнопок настроек, выбора голоса и статистики."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.db import DEFAULT_VOICE_KEY, Database
from logging_config import format_log_event
from services.speech_speed import SPEED_OPTIONS, get_speed_by_key, get_speed_by_value
from services.voice_catalog import VoiceCatalog, VoiceOption
from services.daily_quota import DailyQuotaError, DailyQuotaSettings, format_limit_status, get_daily_quota_status
from texts import HELP_TEXT

logger = logging.getLogger(__name__)

router = Router(name="settings")

BUTTON_CHOOSE_VOICE = "Выбрать голос"
BUTTON_MY_STATISTICS = "Моя статистика"
BUTTON_SPEED = "⏱ Скорость"
BUTTON_SPEED_LEGACY = "Настроить скорость"
BUTTON_HELP = "Помощь"

VOICE_UNAVAILABLE = "Этот голос сейчас недоступен. Выберите другой вариант."
VOICE_SAVE_FAILED = "Не удалось сохранить настройку. Попробуйте ещё раз."
STATS_UNAVAILABLE = "Не удалось получить статистику. Попробуйте позднее."
VOICE_MENU_UNAVAILABLE = "Выбор голоса сейчас недоступен. Попробуйте позднее."
SPEED_INVALID = "Не удалось выбрать скорость. Пожалуйста, воспользуйтесь кнопками."
SPEED_SAVE_FAILED = "Не удалось изменить скорость. Попробуйте ещё раз."


def build_voice_keyboard(
    voices: list[VoiceOption],
    selected_key: str,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for voice in voices:
        label = f"✅ {voice.display_name}" if voice.key == selected_key else voice.display_name
        buttons.append(
            [InlineKeyboardButton(text=label, callback_data=f"voice:{voice.key}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_speed_keyboard(selected_value: float) -> InlineKeyboardMarkup:
    selected = get_speed_by_value(selected_value)
    buttons: list[list[InlineKeyboardButton]] = []
    for option in SPEED_OPTIONS:
        label = f"✅ {option.button_label}" if option.key == selected.key else option.button_label
        buttons.append(
            [InlineKeyboardButton(text=label, callback_data=f"speed:{option.key}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_user_statistics(stats: dict, voice_name: str) -> str:
    lines = [
        "Ваша статистика",
        "",
        f"Успешных озвучек: {stats['success_count']}",
        f"Неуспешных запросов: {stats['failed_count']}",
        f"Успешно озвученных символов: {stats['success_char_count']}",
        f"Ориентировочный расход: {stats['estimated_credits']} кредитов",
        f"Голос: {voice_name}",
        f"Скорость: {get_speed_by_value(stats.get('speech_speed')).display}",
    ]
    cost = stats.get("estimated_cost_usd")
    if cost is not None:
        lines.append(f"Ориентировочный расход: {format(cost, 'f')} USD")
    return "\n".join(lines)


@router.message(F.text == BUTTON_CHOOSE_VOICE)
async def choose_voice(
    message: Message,
    database: Database | None = None,
    voice_catalog: VoiceCatalog | None = None,
    default_voice_id: str = "",
) -> None:
    user = message.from_user
    if voice_catalog is None or user is None:
        await message.answer(VOICE_MENU_UNAVAILABLE)
        return

    selected_key = DEFAULT_VOICE_KEY
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
            selected_key = db_user.get("selected_voice_key") or DEFAULT_VOICE_KEY
            if voice_catalog.get(selected_key) is None:
                selected_key = DEFAULT_VOICE_KEY
        except Exception:
            logger.exception(
                format_log_event(
                    "database_operation_failed",
                    operation="choose_voice",
                    telegram_user_id=user.id,
                )
            )

    logger.info(
        format_log_event(
            "voice_menu_opened",
            telegram_user_id=user.id,
            voice_key=selected_key,
        )
    )
    await message.answer(
        "Выберите голос:",
        reply_markup=build_voice_keyboard(voice_catalog.voices, selected_key),
    )


@router.callback_query(F.data.startswith("voice:"))
async def on_voice_selected(
    callback: CallbackQuery,
    database: Database | None = None,
    voice_catalog: VoiceCatalog | None = None,
) -> None:
    user = callback.from_user
    data = callback.data or ""
    voice_key = data.split(":", 1)[1] if ":" in data else ""
    voice = voice_catalog.get(voice_key) if voice_catalog is not None else None

    if voice is None:
        logger.info(
            format_log_event(
                "voice_selected",
                telegram_user_id=user.id if user else "none",
                voice_key=voice_key or "none",
                status="unavailable",
            )
        )
        if callback.message is not None:
            await callback.message.answer(VOICE_UNAVAILABLE)
        await callback.answer()
        return

    if database is not None:
        try:
            stored = await database.get_voice_by_key(voice.key)
        except Exception:
            stored = None
            logger.exception(
                format_log_event(
                    "database_operation_failed",
                    operation="get_voice_by_key",
                    telegram_user_id=user.id if user else "none",
                    voice_key=voice.key,
                )
            )
        else:
            if stored is not None and int(stored.get("is_active") or 0) != 1:
                logger.info(
                    format_log_event(
                        "voice_selected",
                        telegram_user_id=user.id if user else "none",
                        voice_key=voice.key,
                        status="unavailable",
                    )
                )
                if callback.message is not None:
                    await callback.message.answer(VOICE_UNAVAILABLE)
                await callback.answer()
                return

    if user is None or database is None:
        if callback.message is not None:
            await callback.message.answer(VOICE_SAVE_FAILED)
        await callback.answer()
        return

    try:
        await database.upsert_user(
            telegram_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
        )
        await database.update_user_voice(user.id, voice.key, voice.voice_id)
    except Exception:
        logger.exception(
            format_log_event(
                "database_operation_failed",
                operation="update_user_voice",
                telegram_user_id=user.id,
                voice_key=voice.key,
            )
        )
        if callback.message is not None:
            await callback.message.answer(VOICE_SAVE_FAILED)
        await callback.answer()
        return

    logger.info(
        format_log_event(
            "voice_selected",
            telegram_user_id=user.id,
            voice_key=voice.key,
            status="success",
        )
    )
    if callback.message is not None:
        await callback.message.answer(f"Голос выбран: {voice.display_name}.")
        try:
            await callback.message.edit_reply_markup(
                reply_markup=build_voice_keyboard(voice_catalog.voices, voice.key)
            )
        except Exception:
            logger.exception(
                format_log_event(
                    "database_operation_failed",
                    operation="edit_voice_keyboard",
                    telegram_user_id=user.id,
                    voice_key=voice.key,
                )
            )
    await callback.answer()


@router.message(F.text == BUTTON_MY_STATISTICS)
async def show_my_statistics(
    message: Message,
    database: Database | None = None,
    voice_catalog: VoiceCatalog | None = None,
    default_voice_id: str = "",
) -> None:
    user = message.from_user
    if user is None or database is None:
        await message.answer(STATS_UNAVAILABLE)
        return

    try:
        await database.upsert_user(
            telegram_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
            default_voice_id=default_voice_id or None,
        )
        stats = await database.get_user_usage_statistics(user.id)
    except Exception:
        logger.exception(
            format_log_event(
                "database_operation_failed",
                operation="get_user_usage_statistics",
                telegram_user_id=user.id,
            )
        )
        await message.answer(STATS_UNAVAILABLE)
        return

    if stats is None:
        await message.answer(STATS_UNAVAILABLE)
        return

    voice_key = stats["selected_voice_key"]
    voice_name = "Основной голос"
    if voice_catalog is not None:
        voice_name = voice_catalog.get_display_name(voice_key) or voice_catalog.default.display_name
    else:
        try:
            stored = await database.get_voice_by_key(voice_key)
            if stored is not None:
                voice_name = stored["display_name"]
        except Exception:
            logger.exception(
                format_log_event(
                    "database_operation_failed",
                    operation="get_voice_by_key",
                    telegram_user_id=user.id,
                    voice_key=voice_key,
                )
            )

    logger.info(
        format_log_event(
            "user_statistics_requested",
            telegram_user_id=user.id,
            voice_key=voice_key,
            estimated_credits=stats["estimated_credits"],
        )
    )
    await message.answer(format_user_statistics(stats, voice_name))


@router.message(F.text.in_({BUTTON_SPEED, BUTTON_SPEED_LEGACY}))
async def configure_speed(
    message: Message,
    database: Database | None = None,
    default_voice_id: str = "",
) -> None:
    user = message.from_user
    selected_speed = 1.0
    if user is not None and database is not None:
        try:
            db_user = await database.upsert_user(
                telegram_user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code,
                default_voice_id=default_voice_id or None,
            )
            selected_speed = float(db_user.get("speech_speed") or 1.0)
        except Exception:
            logger.exception(
                format_log_event(
                    "database_operation_failed",
                    operation="choose_speed",
                    telegram_user_id=user.id,
                )
            )
            await message.answer(SPEED_SAVE_FAILED)
            return

    logger.info(
        format_log_event(
            "speed_menu_opened",
            telegram_user_id=user.id if user else "none",
            speech_speed=selected_speed,
        )
    )
    await message.answer(
        "Выберите скорость озвучивания:",
        reply_markup=build_speed_keyboard(selected_speed),
    )


@router.callback_query(F.data.startswith("speed:"))
async def on_speed_selected(
    callback: CallbackQuery,
    database: Database | None = None,
    default_voice_id: str = "",
) -> None:
    user = callback.from_user
    data = callback.data or ""
    speed_key = data.split(":", 1)[1] if ":" in data else ""
    option = get_speed_by_key(speed_key)

    if option is None:
        logger.info(
            format_log_event(
                "speech_speed_changed",
                telegram_user_id=user.id if user else "none",
                status="invalid",
                speed_key=speed_key or "none",
            )
        )
        if callback.message is not None:
            await callback.message.answer(SPEED_INVALID)
        await callback.answer()
        return

    if user is None or database is None:
        if callback.message is not None:
            await callback.message.answer(SPEED_SAVE_FAILED)
        await callback.answer()
        return

    try:
        db_user = await database.upsert_user(
            telegram_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
            default_voice_id=default_voice_id or None,
        )
        previous_speed = float(db_user.get("speech_speed") or 1.0)
        await database.update_user_speed(user.id, option.value)
    except Exception:
        logger.exception(
            format_log_event(
                "database_operation_failed",
                operation="update_user_speed",
                telegram_user_id=user.id,
            )
        )
        if callback.message is not None:
            await callback.message.answer(SPEED_SAVE_FAILED)
        await callback.answer()
        return

    logger.info(
        format_log_event(
            "speech_speed_changed",
            telegram_user_id=user.id,
            old_speed=previous_speed,
            new_speed=option.value,
            status="success",
        )
    )
    if callback.message is not None:
        await callback.message.answer(option.confirmation)
        try:
            await callback.message.edit_reply_markup(
                reply_markup=build_speed_keyboard(option.value)
            )
        except Exception:
            logger.exception(
                format_log_event(
                    "database_operation_failed",
                    operation="edit_speed_keyboard",
                    telegram_user_id=user.id,
                )
            )
    await callback.answer()


@router.message(Command("help"))
@router.message(F.text == BUTTON_HELP)
async def show_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("limit"))
async def cmd_limit(
    message: Message,
    database: Database | None = None,
    quota_settings: DailyQuotaSettings | None = None,
    admin_ids: set[int] | None = None,
) -> None:
    user = message.from_user
    if user is None:
        return
    settings = quota_settings or DailyQuotaSettings()
    if database is None:
        await message.answer("Не удалось получить лимит. Попробуйте позднее.")
        return
    try:
        status = await get_daily_quota_status(database, user.id, settings, admin_ids=admin_ids)
    except DailyQuotaError as exc:
        await message.answer(exc.user_message)
        return
    logger.info(
        format_log_event(
            "daily_quota_status_requested",
            telegram_user_id=user.id,
            unlimited="yes" if status.unlimited else "no",
            used_requests=status.used_requests,
            used_characters=status.used_characters,
        )
    )
    await message.answer(format_limit_status(status))
