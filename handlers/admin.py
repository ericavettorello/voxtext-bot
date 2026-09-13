"""Защищённая административная панель VoxText."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database.db import Database
from logging_config import BACKUP_COUNT, format_log_event
from services.admin_access import ACCESS_DENIED_TEXT, is_admin, log_access_denied
from services.admin_export import (
    AdminExportError,
    ExportTooLargeError,
    SecretRedactor,
    assert_export_size,
    cleanup_export_directory,
    copy_current_log,
    create_export_directory,
    create_sqlite_backup,
    export_stamp,
    requests_csv_rows,
    rotating_log_files,
    users_csv_rows,
    write_csv,
    zip_database_copy,
    zip_redacted_logs,
    REQUESTS_CSV_HEADERS,
    USERS_CSV_HEADERS,
)
from services.admin_full_csv import (
    BLOB_SAFE_LIMIT_BYTES,
    FULL_CSV_FAILED,
    FullCsvExportError,
    export_table_to_csv,
    finalize_csv_archives,
    list_user_tables,
    unique_csv_stem,
)
from services.admin_jobs import admin_export_lock
from services.daily_quota import DailyQuotaSettings, usage_date_today
from services.admin_stats import (
    ALLOWED_PERIODS,
    PERIOD_7D,
    PERIOD_30D,
    PERIOD_ALL,
    PERIOD_FILE_LABELS,
    PERIOD_TODAY,
    format_overview,
    format_period_stats,
    period_start_iso,
)
from services.long_tts_job import safe_callback_answer

logger = logging.getLogger(__name__)

router = Router(name="admin")

EXPORT_BUSY = "Экспорт уже создаётся. Пожалуйста, дождитесь завершения."
EXPORT_FAILED = "Не удалось подготовить экспорт. Подробности записаны в журнал."
PANEL_TEXT = "Панель администратора VoxText"
DB_CONFIRM_TEXT = (
    "База данных содержит идентификаторы и статистику пользователей.\n\n"
    "Создать и отправить защищённую копию?"
)
FULL_CSV_CONFIRM_TEXT = (
    "Экспортировать все таблицы базы данных в CSV?\n\n"
    "Каждая таблица будет сохранена в отдельный файл. Все файлы будут упакованы "
    "в ZIP-архив, который можно открыть на компьютере и просмотреть в Excel."
)
FULL_CSV_CAPTION = "Все таблицы базы VoxText в формате CSV для Excel."
LOG_CONFIRM_TEXT = "Создать и отправить безопасную копию текущего журнала?"
LOGS_CONFIRM_TEXT = "Создать и отправить архив очищенных журналов?"
UNKNOWN_CALLBACK = "Команда не поддерживается."


def build_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Общая статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="📅 Статистика за период", callback_data="admin:periods")],
            [InlineKeyboardButton(text="👥 Пользователи (.CSV)", callback_data="admin:users")],
            [InlineKeyboardButton(text="🧾 Запросы (.CSV)", callback_data="admin:requests")],
            [InlineKeyboardButton(text="📦 Вся база (.CSV ZIP)", callback_data="admin:fullcsv")],
            [InlineKeyboardButton(text="🗄 Резервная копия (.DB ZIP)", callback_data="admin:db")],
            [InlineKeyboardButton(text="📋 Текущий лог", callback_data="admin:log")],
            [InlineKeyboardButton(text="🗜 Архив логов", callback_data="admin:logs")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:close")],
        ]
    )


def build_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")]]
    )


def build_back_refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:stats")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")],
        ]
    )


def build_period_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сегодня", callback_data=f"{prefix}:{PERIOD_TODAY}")],
            [InlineKeyboardButton(text="Последние 7 дней" if prefix == "admin:p" else "7 дней", callback_data=f"{prefix}:{PERIOD_7D}")],
            [InlineKeyboardButton(text="Последние 30 дней" if prefix == "admin:p" else "30 дней", callback_data=f"{prefix}:{PERIOD_30D}")],
            [InlineKeyboardButton(text="За всё время" if prefix == "admin:p" else "Всё время", callback_data=f"{prefix}:{PERIOD_ALL}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")],
        ]
    )


def build_confirm_keyboard(
    ok_data: str,
    cancel_data: str = "admin:back",
    ok_text: str = "✅ Создать копию",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ok_text, callback_data=ok_data)],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_data)],
        ]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(message, admin_ids, "admin_panel"):
        return
    logger.info(
        format_log_event(
            "admin_panel_opened",
            telegram_user_id=message.from_user.id if message.from_user else "none",
        )
    )
    await message.answer(PANEL_TEXT, reply_markup=build_admin_keyboard())


@router.message(Command("stats"))
async def cmd_stats(
    message: Message,
    database: Database | None = None,
    admin_ids: set[int] | None = None,
    quota_settings: DailyQuotaSettings | None = None,
) -> None:
    if not await _ensure_admin(message, admin_ids, "admin_stats"):
        return
    await _send_overview(
        message,
        database,
        telegram_user_id=message.from_user.id if message.from_user else 0,
        quota_settings=quota_settings,
    )


@router.callback_query(F.data == "admin:stats")
async def on_stats(
    callback: CallbackQuery,
    database: Database | None = None,
    admin_ids: set[int] | None = None,
    quota_settings: DailyQuotaSettings | None = None,
) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_stats"):
        return
    await safe_callback_answer(callback)
    await _send_overview(
        callback.message,
        database,
        telegram_user_id=_user_id(callback),
        edit=True,
        quota_settings=quota_settings,
    )


@router.callback_query(F.data == "admin:periods")
async def on_periods(callback: CallbackQuery, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_periods"):
        return
    await safe_callback_answer(callback)
    await _edit_or_answer(callback.message, "Выберите период:", build_period_keyboard("admin:p"))


@router.callback_query(F.data.startswith("admin:p:"))
async def on_period_stats(
    callback: CallbackQuery,
    database: Database | None = None,
    admin_ids: set[int] | None = None,
) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_period_stats"):
        return
    period = (callback.data or "").split(":")[-1]
    if period not in ALLOWED_PERIODS:
        await safe_callback_answer(callback)
        await _edit_or_answer(callback.message, UNKNOWN_CALLBACK, build_admin_keyboard())
        return
    await safe_callback_answer(callback)
    logger.info(
        format_log_event(
            "admin_stats_requested",
            telegram_user_id=_user_id(callback),
            action="period",
            period=period,
        )
    )
    started = time.perf_counter()
    try:
        if database is None:
            raise AdminExportError(EXPORT_FAILED)
        stats = await database.get_admin_period_statistics(period_start_iso(period))
        text = format_period_stats(stats, period)
        await _edit_or_answer(callback.message, text, build_back_keyboard())
        logger.info(
            format_log_event(
                "admin_stats_completed",
                telegram_user_id=_user_id(callback),
                period=period,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="success",
            )
        )
    except Exception:
        logger.exception(format_log_event("admin_export_failed", action="period_stats", telegram_user_id=_user_id(callback)))
        await _edit_or_answer(callback.message, EXPORT_FAILED, build_admin_keyboard())


@router.callback_query(F.data == "admin:requests")
async def on_requests_period(callback: CallbackQuery, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_requests"):
        return
    await safe_callback_answer(callback)
    await _edit_or_answer(callback.message, "Выберите период экспорта запросов:", build_period_keyboard("admin:r"))


@router.callback_query(F.data == "admin:users")
async def on_users_csv(
    callback: CallbackQuery,
    database: Database | None = None,
    admin_ids: set[int] | None = None,
) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_users_export"):
        return
    await _run_locked_export(callback, database, _export_users)


@router.callback_query(F.data.startswith("admin:r:"))
async def on_requests_csv(
    callback: CallbackQuery,
    database: Database | None = None,
    admin_ids: set[int] | None = None,
) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_requests_export"):
        return
    period = (callback.data or "").split(":")[-1]
    if period not in ALLOWED_PERIODS:
        await safe_callback_answer(callback)
        await _edit_or_answer(callback.message, UNKNOWN_CALLBACK, build_admin_keyboard())
        return
    await _run_locked_export(callback, database, lambda **kwargs: _export_requests(period=period, **kwargs))


@router.callback_query(F.data == "admin:fullcsv")
async def on_full_csv_ask(callback: CallbackQuery, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_full_csv"):
        return
    await safe_callback_answer(callback)
    logger.info(
        format_log_event(
            "admin_full_csv_export_requested",
            telegram_user_id=_user_id(callback),
        )
    )
    await _edit_or_answer(
        callback.message,
        FULL_CSV_CONFIRM_TEXT,
        build_confirm_keyboard("admin:fullcsvok", ok_text="✅ Создать CSV-архив"),
    )


@router.callback_query(F.data == "admin:fullcsvok")
async def on_full_csv_export(
    callback: CallbackQuery,
    database: Database | None = None,
    admin_ids: set[int] | None = None,
) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_full_csv_export"):
        return
    await _run_locked_export(
        callback,
        database,
        lambda **kwargs: _export_full_csv(admin_ids=admin_ids, **kwargs),
        fail_event="admin_full_csv_export_failed",
        cleanup_event="admin_full_csv_export_cleaned",
        generic_error=FULL_CSV_FAILED,
    )


@router.callback_query(F.data == "admin:db")
async def on_db_ask(callback: CallbackQuery, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_database"):
        return
    await safe_callback_answer(callback)
    await _edit_or_answer(callback.message, DB_CONFIRM_TEXT, build_confirm_keyboard("admin:dbok"))


@router.callback_query(F.data == "admin:dbok")
async def on_db_backup(
    callback: CallbackQuery,
    database: Database | None = None,
    admin_ids: set[int] | None = None,
) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_database_backup"):
        return
    await _run_locked_export(callback, database, _export_database)


@router.callback_query(F.data == "admin:log")
async def on_log_ask(callback: CallbackQuery, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_log"):
        return
    await safe_callback_answer(callback)
    await _edit_or_answer(callback.message, LOG_CONFIRM_TEXT, build_confirm_keyboard("admin:logok"))


@router.callback_query(F.data == "admin:logs")
async def on_logs_ask(callback: CallbackQuery, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_logs"):
        return
    await safe_callback_answer(callback)
    await _edit_or_answer(callback.message, LOGS_CONFIRM_TEXT, build_confirm_keyboard("admin:logsok"))


@router.callback_query(F.data == "admin:logok")
async def on_log_export(
    callback: CallbackQuery,
    database: Database | None = None,
    admin_ids: set[int] | None = None,
    secret_redactor: SecretRedactor | None = None,
    log_path: Path | None = None,
) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_log_export"):
        return
    await _run_locked_export(
        callback,
        database,
        lambda **kwargs: _export_current_log(redactor=secret_redactor, log_path=log_path, **kwargs),
    )


@router.callback_query(F.data == "admin:logsok")
async def on_logs_archive(
    callback: CallbackQuery,
    database: Database | None = None,
    admin_ids: set[int] | None = None,
    secret_redactor: SecretRedactor | None = None,
    log_path: Path | None = None,
) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_logs_export"):
        return
    await _run_locked_export(
        callback,
        database,
        lambda **kwargs: _export_logs_archive(redactor=secret_redactor, log_path=log_path, **kwargs),
    )


@router.callback_query(F.data.in_({"admin:back", "admin:dbno", "admin:logno", "admin:logsno", "admin:fullcsvno"}))
async def on_back(callback: CallbackQuery, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_back"):
        return
    await safe_callback_answer(callback)
    await _edit_or_answer(callback.message, PANEL_TEXT, build_admin_keyboard())


@router.callback_query(F.data == "admin:close")
async def on_close(callback: CallbackQuery, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_close"):
        return
    await safe_callback_answer(callback)
    if callback.message is not None:
        await _edit_or_answer(callback.message, "Панель администратора закрыта.", None)


@router.callback_query(F.data.startswith("admin:"))
async def on_unknown_admin(callback: CallbackQuery, admin_ids: set[int] | None = None) -> None:
    if not await _ensure_admin(callback, admin_ids, "admin_unknown"):
        return
    await safe_callback_answer(callback)
    await _edit_or_answer(callback.message, UNKNOWN_CALLBACK, build_admin_keyboard())


async def _send_overview(
    target: Message | None,
    database: Database | None,
    telegram_user_id: int,
    edit: bool = False,
    quota_settings: DailyQuotaSettings | None = None,
) -> None:
    logger.info(format_log_event("admin_stats_requested", telegram_user_id=telegram_user_id, action="overview"))
    started = time.perf_counter()
    try:
        if database is None or target is None:
            raise AdminExportError(EXPORT_FAILED)
        settings = quota_settings or DailyQuotaSettings()
        stats = await database.get_admin_overview_statistics(
            usage_date=usage_date_today(settings.timezone_name),
            request_limit=settings.request_limit,
            character_limit=settings.character_limit,
        )
        text = format_overview(stats)
        if edit:
            await _edit_or_answer(target, text, build_back_refresh_keyboard())
        else:
            await target.answer(text, reply_markup=build_back_refresh_keyboard())
        logger.info(
            format_log_event(
                "admin_stats_completed",
                telegram_user_id=telegram_user_id,
                action="overview",
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="success",
            )
        )
    except Exception:
        logger.exception(format_log_event("admin_export_failed", action="overview", telegram_user_id=telegram_user_id))
        if target is not None:
            await target.answer(EXPORT_FAILED, reply_markup=build_admin_keyboard())


async def _run_locked_export(
    callback: CallbackQuery,
    database: Database | None,
    exporter,
    *,
    fail_event: str = "admin_export_failed",
    cleanup_event: str = "admin_export_cleaned",
    generic_error: str = EXPORT_FAILED,
) -> None:
    user_id = _user_id(callback)
    if not admin_export_lock.try_acquire(user_id):
        await safe_callback_answer(callback)
        if callback.message is not None:
            await callback.message.answer(EXPORT_BUSY)
        return
    await safe_callback_answer(callback)
    job_id = ""
    job_dir: Path | None = None
    started = time.perf_counter()
    try:
        job_id, job_dir = create_export_directory()
        await exporter(callback=callback, database=database, job_id=job_id, job_dir=job_dir)
    except ExportTooLargeError as exc:
        logger.info(
            format_log_event(
                fail_event,
                telegram_user_id=user_id,
                job_id=job_id or "none",
                reason="too_large",
                exception_class=type(exc).__name__,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="failed",
            )
        )
        if callback.message is not None:
            await callback.message.answer(exc.user_message)
    except AdminExportError as exc:
        logger.exception(
            format_log_event(
                fail_event,
                telegram_user_id=user_id,
                job_id=job_id or "none",
                exception_class=type(exc).__name__,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="failed",
            )
        )
        if callback.message is not None:
            await callback.message.answer(exc.user_message)
    except Exception as exc:
        logger.exception(
            format_log_event(
                fail_event,
                telegram_user_id=user_id,
                job_id=job_id or "none",
                exception_class=type(exc).__name__,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="failed",
            )
        )
        if callback.message is not None:
            await callback.message.answer(generic_error)
    finally:
        try:
            cleanup_export_directory(job_dir, job_id or None)
        except Exception:
            logger.exception(
                format_log_event(
                    fail_event,
                    telegram_user_id=user_id,
                    job_id=job_id or "none",
                    exception_class="CleanupError",
                    status="failed",
                )
            )
        logger.info(
            format_log_event(
                cleanup_event,
                telegram_user_id=user_id,
                job_id=job_id or "none",
            )
        )
        admin_export_lock.release(user_id)


async def _export_full_csv(
    callback: CallbackQuery,
    database: Database | None,
    job_id: str,
    job_dir: Path,
    admin_ids: set[int] | None,
) -> None:
    if database is None:
        raise FullCsvExportError
    user_id = _user_id(callback)
    if not is_admin(user_id, admin_ids):
        raise AdminExportError(ACCESS_DENIED_TEXT)
    logger.info(
        format_log_event(
            "admin_full_csv_export_started",
            telegram_user_id=user_id,
            job_id=job_id,
        )
    )
    started = time.perf_counter()
    snapshot = job_dir / "database_snapshot.db"
    csv_dir = job_dir / "csv"
    archive_dir = job_dir / "archive"
    csv_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    await _edit_or_answer(callback.message, "Создаю копию базы данных…", None)
    try:
        await asyncio.to_thread(create_sqlite_backup, database.path, snapshot)
    except Exception as exc:
        raise FullCsvExportError from exc
    logger.info(
        format_log_event(
            "admin_database_snapshot_created",
            telegram_user_id=user_id,
            job_id=job_id,
            status="success",
        )
    )
    try:
        tables = await asyncio.to_thread(list_user_tables, snapshot)
    except Exception as exc:
        raise FullCsvExportError from exc
    results = []
    used_stems: set[str] = set()
    total = len(tables)
    for index, table in enumerate(tables, 1):
        if not is_admin(user_id, admin_ids):
            raise AdminExportError(ACCESS_DENIED_TEXT)
        await _edit_or_answer(callback.message, f"Экспортирую таблицы: {index} из {total}…", None)
        logger.info(
            format_log_event(
                "admin_table_export_started",
                telegram_user_id=user_id,
                job_id=job_id,
                table_name=table,
                table_index=index,
                table_total=total,
            )
        )
        try:
            result = await asyncio.to_thread(
                export_table_to_csv,
                snapshot,
                table,
                csv_dir,
                job_dir,
                index,
                None,
                BLOB_SAFE_LIMIT_BYTES,
                unique_csv_stem(table, index, used_stems),
            )
        except Exception as exc:
            raise FullCsvExportError from exc
        results.append(result)
        logger.info(
            format_log_event(
                "admin_table_export_completed",
                telegram_user_id=user_id,
                job_id=job_id,
                table_name=table,
                records=result.row_count,
                csv_files=len(result.csv_paths),
                status="success",
            )
        )
    await _edit_or_answer(callback.message, "Создаю ZIP-архив…", None)
    created_at = datetime.now(timezone.utc)
    try:
        export = await asyncio.to_thread(
            finalize_csv_archives,
            results,
            csv_dir,
            archive_dir,
            job_dir,
            created_at,
        )
    except Exception as exc:
        raise FullCsvExportError from exc
    archive_size = sum(path.stat().st_size for path in export.archives)
    logger.info(
        format_log_event(
            "admin_csv_archive_created",
            telegram_user_id=user_id,
            job_id=job_id,
            tables=len(export.tables),
            csv_files=export.csv_file_count,
            zip_count=len(export.archives),
            size_bytes=archive_size,
            status="success",
        )
    )
    zip_count = len(export.archives)
    for index, archive in enumerate(export.archives, 1):
        if not is_admin(user_id, admin_ids):
            raise AdminExportError(ACCESS_DENIED_TEXT)
        caption = FULL_CSV_CAPTION if zip_count == 1 else f"Часть {index} из {zip_count}\n{FULL_CSV_CAPTION}"
        await _send_document(callback, archive, archive.name, caption=caption)
    done = (
        "Экспорт завершён.\n\n"
        f"Таблиц: {len(export.tables)}\n"
        f"Строк: {export.total_rows}\n"
        f"Файлов CSV: {export.csv_file_count}"
    )
    await _edit_or_answer(callback.message, done, build_admin_keyboard())
    logger.info(
        format_log_event(
            "admin_full_csv_export_completed",
            telegram_user_id=user_id,
            job_id=job_id,
            tables=len(export.tables),
            records=export.total_rows,
            csv_files=export.csv_file_count,
            zip_count=zip_count,
            size_bytes=archive_size,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="success",
        )
    )


async def _export_users(callback: CallbackQuery, database: Database | None, job_id: str, job_dir: Path) -> None:
    if database is None:
        raise AdminExportError(EXPORT_FAILED)
    user_id = _user_id(callback)
    logger.info(format_log_event("admin_users_export_started", telegram_user_id=user_id, job_id=job_id))
    started = time.perf_counter()
    rows = await database.fetch_users_export_rows()
    filename = f"voxtext_users_{export_stamp()}.csv"
    path = job_dir / filename
    await asyncio.to_thread(write_csv, path, USERS_CSV_HEADERS, users_csv_rows(rows))
    assert_export_size(path)
    await _send_document(callback, path, filename, caption=f"Пользователи: {len(rows)}")
    logger.info(
        format_log_event(
            "admin_users_export_completed",
            telegram_user_id=user_id,
            job_id=job_id,
            records=len(rows),
            size_bytes=path.stat().st_size,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="success",
        )
    )


async def _export_requests(
    callback: CallbackQuery,
    database: Database | None,
    job_id: str,
    job_dir: Path,
    period: str,
) -> None:
    if database is None:
        raise AdminExportError(EXPORT_FAILED)
    user_id = _user_id(callback)
    logger.info(
        format_log_event(
            "admin_requests_export_started",
            telegram_user_id=user_id,
            job_id=job_id,
            period=period,
        )
    )
    started = time.perf_counter()
    rows = await database.fetch_requests_export_rows(period_start_iso(period))
    filename = f"voxtext_requests_{PERIOD_FILE_LABELS[period]}_{export_stamp()}.csv"
    path = job_dir / filename
    await asyncio.to_thread(write_csv, path, REQUESTS_CSV_HEADERS, requests_csv_rows(rows))
    assert_export_size(path)
    await _send_document(callback, path, filename, caption=f"Запросы: {len(rows)}")
    logger.info(
        format_log_event(
            "admin_requests_export_completed",
            telegram_user_id=user_id,
            job_id=job_id,
            period=period,
            records=len(rows),
            size_bytes=path.stat().st_size,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="success",
        )
    )


async def _export_database(callback: CallbackQuery, database: Database | None, job_id: str, job_dir: Path) -> None:
    if database is None:
        raise AdminExportError(EXPORT_FAILED)
    user_id = _user_id(callback)
    logger.info(format_log_event("admin_database_backup_started", telegram_user_id=user_id, job_id=job_id))
    started = time.perf_counter()
    db_copy = job_dir / "voxtext_database.db"
    zip_path = job_dir / f"voxtext_database_{export_stamp()}.zip"
    await asyncio.to_thread(create_sqlite_backup, database.path, db_copy)
    await asyncio.to_thread(zip_database_copy, db_copy, zip_path)
    assert_export_size(zip_path)
    await _send_document(callback, zip_path, zip_path.name, caption="Защищённая копия базы SQLite")
    logger.info(
        format_log_event(
            "admin_database_backup_completed",
            telegram_user_id=user_id,
            job_id=job_id,
            size_bytes=zip_path.stat().st_size,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="success",
        )
    )


async def _export_current_log(
    callback: CallbackQuery,
    database: Database | None,
    job_id: str,
    job_dir: Path,
    redactor: SecretRedactor | None,
    log_path: Path | None,
) -> None:
    if redactor is None or log_path is None:
        raise AdminExportError(EXPORT_FAILED)
    user_id = _user_id(callback)
    logger.info(format_log_event("admin_log_export_started", telegram_user_id=user_id, job_id=job_id))
    started = time.perf_counter()
    dest = job_dir / f"voxtext_log_{export_stamp()}.log"
    await asyncio.to_thread(copy_current_log, log_path, dest, redactor)
    assert_export_size(dest)
    await _send_document(callback, dest, dest.name, caption="Текущий журнал")
    logger.info(
        format_log_event(
            "admin_log_export_completed",
            telegram_user_id=user_id,
            job_id=job_id,
            size_bytes=dest.stat().st_size,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="success",
        )
    )


async def _export_logs_archive(
    callback: CallbackQuery,
    database: Database | None,
    job_id: str,
    job_dir: Path,
    redactor: SecretRedactor | None,
    log_path: Path | None,
) -> None:
    if redactor is None or log_path is None:
        raise AdminExportError(EXPORT_FAILED)
    user_id = _user_id(callback)
    logger.info(format_log_event("admin_log_export_started", telegram_user_id=user_id, job_id=job_id, action="archive"))
    started = time.perf_counter()
    files = rotating_log_files(log_path, BACKUP_COUNT)
    zip_path = job_dir / f"voxtext_logs_{export_stamp()}.zip"
    await asyncio.to_thread(zip_redacted_logs, files, zip_path, redactor, job_dir)
    assert_export_size(zip_path)
    await _send_document(callback, zip_path, zip_path.name, caption="Архив журналов")
    logger.info(
        format_log_event(
            "admin_log_export_completed",
            telegram_user_id=user_id,
            job_id=job_id,
            action="archive",
            files=len(files),
            size_bytes=zip_path.stat().st_size,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="success",
        )
    )


async def _send_document(callback: CallbackQuery, path: Path, filename: str, caption: str) -> None:
    if callback.message is None:
        raise AdminExportError(EXPORT_FAILED)
    await callback.message.answer_document(FSInputFile(path, filename=filename), caption=caption)


async def _ensure_admin(event: Message | CallbackQuery, admin_ids: set[int] | None, action: str) -> bool:
    user = event.from_user
    user_id = user.id if user is not None else None
    if is_admin(user_id, admin_ids):
        return True
    log_access_denied(user_id, action)
    if getattr(event, "data", None) is not None:
        await safe_callback_answer(event)
        target = getattr(event, "message", None)
        if target is not None:
            await target.answer(ACCESS_DENIED_TEXT)
    else:
        await event.answer(ACCESS_DENIED_TEXT)
    return False


async def _edit_or_answer(message: Message | None, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)


def _user_id(callback: CallbackQuery) -> int:
    return callback.from_user.id if callback.from_user is not None else 0
