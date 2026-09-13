"""Точка входа Telegram-бота VoxText."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from config import Config
from database.db import Database
from handlers.admin import router as admin_router
from handlers.documents import router as documents_router
from handlers.long_text import router as long_text_router
from handlers.settings import router as settings_router
from handlers.start import router as start_router
from handlers.text import router as text_router
from logging_config import format_log_event, setup_logging
from services.admin_export import SecretRedactor
from services.daily_quota import DailyQuotaSettings
from services.tts_service import TTSService
from services.voice_catalog import build_voice_catalog

logger = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="help", description="Как пользоваться VoxText"),
    BotCommand(command="text", description="Подсказка для обычного текста"),
    BotCommand(command="limit", description="Дневной лимит озвучивания"),
]
ADMIN_COMMANDS = [
    BotCommand(command="admin", description="Панель администратора"),
    BotCommand(command="stats", description="Общая статистика"),
]


async def main() -> None:
    log_path = setup_logging()
    logger.info(format_log_event("bot_starting"))
    database: Database | None = None

    try:
        config = Config()
        if config.admin_ids:
            logger.info(format_log_event("admin_enabled", admins=len(config.admin_ids)))
        else:
            logger.warning(format_log_event("admin_disabled", reason="admin_ids_empty"))
        logger.info(
            format_log_event(
                "daily_quota_configured",
                request_limit=config.daily_request_limit,
                character_limit=config.daily_character_limit,
                timezone=config.daily_limit_timezone,
            )
        )

        database = Database(config.database_path)
        try:
            await database.init_database()
        except Exception:
            logger.exception(format_log_event("database_initialization_failed"))
            print(
                "Не удалось создать или открыть базу данных. "
                "Проверьте путь DATABASE_PATH и права доступа.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None

        voice_catalog = build_voice_catalog(config)
        try:
            await database.sync_voices(voice_catalog.voices)
        except Exception:
            logger.exception(format_log_event("database_operation_failed", operation="sync_voices"))

        tts_service = TTSService(
            api_key=config.elevenlabs_api_key,
            voice_id=config.elevenlabs_voice_id,
        )
        secret_redactor = SecretRedactor.from_environ(
            extra=[config.telegram_bot_token, config.elevenlabs_api_key]
        )
        bot = Bot(token=config.telegram_bot_token)
        await _register_commands(bot, config.admin_ids)
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_routers(
            start_router,
            settings_router,
            admin_router,
            documents_router,
            long_text_router,
            text_router,
        )
        logger.info(format_log_event("bot_started"))
        try:
            await dp.start_polling(
                bot,
                tts_service=tts_service,
                database=database,
                default_voice_id=config.elevenlabs_voice_id,
                voice_catalog=voice_catalog,
                plan_price_usd=config.elevenlabs_plan_price_usd,
                plan_credits=config.elevenlabs_plan_credits,
                tts_chunk_size=config.tts_chunk_size,
                max_long_text_chars=config.max_long_text_chars,
                tts_chunk_pause_ms=config.tts_chunk_pause_ms,
                max_upload_file_mb=config.max_upload_file_mb,
                max_docx_uncompressed_mb=config.max_docx_uncompressed_mb,
                max_pdf_pages=config.max_pdf_pages,
                max_pdf_content_stream_mb=config.max_pdf_content_stream_mb,
                admin_ids=config.admin_ids,
                quota_settings=DailyQuotaSettings(
                    request_limit=config.daily_request_limit,
                    character_limit=config.daily_character_limit,
                    timezone_name=config.daily_limit_timezone,
                ),
                secret_redactor=secret_redactor,
                log_path=log_path,
            )
        finally:
            logger.info(format_log_event("bot_stopping"))
            if database is not None:
                await database.close()
            logger.info(format_log_event("bot_stopped"))
    except SystemExit:
        raise
    except Exception:
        logger.exception(format_log_event("bot_start_failed"))
        if database is not None:
            await database.close()
        raise


async def _register_commands(bot: Bot, admin_ids: set[int]) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(
                USER_COMMANDS + ADMIN_COMMANDS,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            logger.exception(format_log_event("admin_commands_failed"))


if __name__ == "__main__":
    asyncio.run(main())
