"""Обработчик команды /start и приветственной клавиатуры."""

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from services.document_store import document_store
from services.draft_store import draft_store

from database.db import Database
from logging_config import format_log_event, sanitize_log_value
from texts import SHORT_TEXT_HINT, WELCOME_TEXT

logger = logging.getLogger(__name__)

router = Router(name="start")


def build_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура с настройками и статистикой."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Выбрать голос")],
            [KeyboardButton(text="Моя статистика")],
            [KeyboardButton(text="⏱ Скорость")],
            [KeyboardButton(text="📚 Длинный текст")],
            [KeyboardButton(text="📄 Загрузить файл")],
            [KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    database: Database | None = None,
    default_voice_id: str = "",
    state: FSMContext | None = None,
) -> None:
    user = message.from_user
    logger.info(
        format_log_event(
            "start_command",
            user_id=user.id if user else "none",
            username=sanitize_log_value(user.username if user else None),
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
    )
    if user is not None:
        draft_store.remove(user.id)
        leftover = document_store.remove(user.id)
        if leftover is not None:
            from services.audio_service import delete_job_directory

            delete_job_directory(leftover.job_dir, leftover.job_id)
    if state is not None:
        await state.clear()
    if user is not None and database is not None:
        try:
            await database.upsert_user(
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
    await message.answer(WELCOME_TEXT, reply_markup=build_main_keyboard())


@router.message(Command("text"))
async def cmd_text_hint(message: Message) -> None:
    await message.answer(SHORT_TEXT_HINT)
