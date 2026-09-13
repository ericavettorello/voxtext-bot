"""Загрузка TXT/DOCX/PDF и передача текста в механизм длинных текстов."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from database.db import (
    REQUEST_TYPE_LONG,
    SOURCE_TYPE_DOCX,
    SOURCE_TYPE_PDF,
    SOURCE_TYPE_TXT,
    Database,
)
from handlers.start import build_main_keyboard
from handlers.text import BUSY_MESSAGE, active_jobs, resolve_user_voice
from logging_config import format_log_event
from services.audio_service import delete_job_directory
from services.document_store import DocumentDraft, document_store
from services.document_text import (
    DOCX_MIME_TYPES,
    TXT_MIME_TYPES,
    DocumentError,
    extract_text_from_document,
)
from services.draft_store import draft_store
from services.filenames import (
    audio_filename_from_document,
    escape_user_filename,
    original_extension,
)
from services.long_tts import GenerationSnapshot, create_job_directory
from services.long_tts_job import LongTTSRunOptions, run_confirmed_long_tts, safe_callback_answer
from services.pdf_text import (
    PDF_MIME_TYPES,
    PdfNoTextError,
    extract_text_from_pdf,
    validate_pdf_header,
)
from services.speech_speed import get_speed_by_value
from services.tts_service import MODEL_ID, TEMP_DIR, TTSService
from services.usage_estimator import estimate_cost_usd, estimate_credits
from services.voice_catalog import VoiceCatalog
from texts import DOCUMENT_UPLOAD_HINT
from utils.text_splitter import split_text_into_chunks

logger = logging.getLogger(__name__)

router = Router(name="documents")

BUTTON_UPLOAD = "📄 Загрузить файл"
BUTTON_CANCEL = "❌ Отмена"

UNSUPPORTED_FORMAT = "Этот формат пока не поддерживается.\nОтправьте файл TXT, DOCX или PDF."
OLD_DOC_FORMAT = (
    "Старый формат DOC не поддерживается. "
    "Сохраните документ в формате DOCX и отправьте его повторно."
)
PENDING_DOCUMENT = (
    "У вас уже есть документ, ожидающий подтверждения.\n"
    "Отмените его или начните озвучивание."
)
MISSING_NAME = "Не удалось прочитать имя файла. Отправьте документ повторно."
TOO_LARGE_FILE = "Файл слишком большой.\nМаксимальный размер: {limit} МБ."
TOO_LARGE_TEXT = (
    "Документ слишком большой для одного задания.\n"
    "Найдено символов: {chars}.\n"
    "Максимальный объём: {limit} символов.\n"
    "Сократите документ и отправьте его повторно."
)
TOO_LARGE_PDF_TEXT = (
    "PDF содержит слишком много текста для одного задания.\n\n"
    "Найдено символов: {chars}\n"
    "Максимальный объём: {limit}\n\n"
    "Разделите PDF на несколько файлов и отправьте их по отдельности."
)
DOWNLOAD_FAILED = "Не удалось скачать файл из Telegram. Попробуйте ещё раз."
PDF_PARTIAL_WARNING = (
    "Внимание: на некоторых страницах текст не найден. "
    "Возможно, они содержат изображения или сканы."
)


class DocumentStates(StatesGroup):
    waiting_file = State()
    confirming = State()
    processing = State()


def build_upload_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BUTTON_CANCEL)]],
        resize_keyboard=True,
    )


def build_document_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Озвучить документ", callback_data="doc:start")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="doc:cancel")],
        ]
    )


def build_pdf_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Озвучить PDF", callback_data="doc:start")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="doc:cancel")],
        ]
    )


def format_char_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


@router.message(F.text == BUTTON_UPLOAD)
async def start_document_mode(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    if user.id in active_jobs:
        await message.answer(BUSY_MESSAGE)
        return
    await _discard_long_text(user.id, state)
    existing = document_store.get(user.id)
    current = await state.get_state()
    if existing is not None and current == DocumentStates.confirming.state:
        await message.answer(PENDING_DOCUMENT)
        return
    _cleanup_document(user.id)
    await state.set_state(DocumentStates.waiting_file)
    await message.answer(DOCUMENT_UPLOAD_HINT, reply_markup=build_upload_keyboard())


@router.message(DocumentStates.waiting_file, F.text == BUTTON_CANCEL)
@router.message(DocumentStates.confirming, F.text == BUTTON_CANCEL)
async def cancel_document_mode(message: Message, state: FSMContext) -> None:
    await _cancel_document(message, state)


@router.callback_query(F.data == "doc:cancel")
async def cancel_document_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is not None:
        await _cancel_document(callback.message, state)
    await safe_callback_answer(callback)


@router.message(DocumentStates.waiting_file, F.text)
async def waiting_file_needs_document(message: Message) -> None:
    text = message.text or ""
    if text in {BUTTON_UPLOAD, BUTTON_CANCEL, "Выбрать голос", "Моя статистика", "⏱ Скорость", "Помощь", "📚 Длинный текст"}:
        return
    await message.answer("Отправьте файл TXT, DOCX или PDF или нажмите «Отмена».")


@router.message(F.document)
async def handle_document_message(
    message: Message,
    state: FSMContext,
    database: Database | None = None,
    voice_catalog: VoiceCatalog | None = None,
    default_voice_id: str = "",
    tts_service: TTSService | None = None,
    tts_chunk_size: int = 4500,
    max_long_text_chars: int = 30000,
    max_upload_file_mb: int = 10,
    max_docx_uncompressed_mb: int = 50,
    max_pdf_pages: int = 100,
    max_pdf_content_stream_mb: int = 25,
    plan_price_usd: Decimal | None = None,
    plan_credits: int | None = None,
) -> None:
    user = message.from_user
    document = message.document
    if user is None or document is None:
        return
    if user.id in active_jobs:
        await message.answer(BUSY_MESSAGE)
        return
    current = await state.get_state()
    if current == DocumentStates.processing.state:
        await message.answer(BUSY_MESSAGE)
        return
    if current == DocumentStates.confirming.state and document_store.get(user.id) is not None:
        await message.answer(PENDING_DOCUMENT)
        return

    await _discard_long_text(user.id, state)
    job_id = ""
    job_dir: Path | None = None
    progress = None
    extension = ""
    try:
        extension = _validate_document_meta(document, max_upload_file_mb)
        progress = await message.answer("Извлекаю текст из документа…")
        logger.info(
            format_log_event(
                _file_event(extension, "received"),
                telegram_user_id=user.id,
                extension=extension,
                size_bytes=document.file_size or 0,
            )
        )
        job_id, job_dir = create_job_directory(TEMP_DIR)
        source_path = job_dir / f"source.{extension}"
        logger.info(
            format_log_event(
                _file_event(extension, "download_started"),
                telegram_user_id=user.id,
                job_id=job_id,
                extension=extension,
                size_bytes=document.file_size or 0,
            )
        )
        await message.bot.download(document, destination=source_path)
        logger.info(
            format_log_event(
                _file_event(extension, "download_completed"),
                telegram_user_id=user.id,
                job_id=job_id,
                extension=extension,
                size_bytes=source_path.stat().st_size,
            )
        )
        _validate_downloaded_file(source_path, extension)
        logger.info(
            format_log_event(
                _file_event(extension, "extraction_started"),
                telegram_user_id=user.id,
                job_id=job_id,
                extension=extension,
            )
        )
        page_count = None
        pages_with_text = None
        pages_without_text = None
        if extension == "pdf":
            pdf_result = await asyncio.to_thread(
                extract_text_from_pdf,
                source_path,
                max_pdf_pages,
                max_pdf_content_stream_mb * 1024 * 1024,
                job_id,
            )
            text = pdf_result.text
            page_count = pdf_result.total_pages
            pages_with_text = pdf_result.pages_with_text
            pages_without_text = pdf_result.pages_without_text
        else:
            text = await asyncio.to_thread(
                extract_text_from_document,
                source_path,
                extension,
                max_docx_uncompressed_mb * 1024 * 1024,
            )
        if len(text) > max_long_text_chars:
            logger.info(
                format_log_event(
                    "pdf_validation_failed" if extension == "pdf" else "document_rejected",
                    telegram_user_id=user.id,
                    job_id=job_id,
                    reason="too_long",
                    chars=len(text),
                    extension=extension,
                    pages=page_count if page_count is not None else "none",
                )
            )
            limit_text = TOO_LARGE_PDF_TEXT if extension == "pdf" else TOO_LARGE_TEXT
            await progress.edit_text(
                limit_text.format(
                    chars=format_char_count(len(text)),
                    limit=format_char_count(max_long_text_chars),
                )
            )
            return
        chunks = split_text_into_chunks(text, tts_chunk_size)
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
        display_name = escape_user_filename(document.file_name)
        credits = estimate_credits(len(text), multiplier)
        cost = estimate_cost_usd(credits, plan_price_usd, plan_credits)
        document_store.put(
            user.id,
            DocumentDraft(
                job_id=job_id,
                job_dir=job_dir,
                source_path=source_path,
                extension=extension,
                display_name=display_name,
                text=text,
                char_count=len(text),
                chunk_count=len(chunks),
                chunks=chunks,
                voice_id=voice_id,
                voice_key=voice_key,
                voice_name=voice_name,
                speech_speed=speed,
                credit_multiplier=multiplier,
                page_count=page_count,
                pages_with_text=pages_with_text,
                pages_without_text=pages_without_text,
            ),
        )
        job_dir = None
        await state.set_state(DocumentStates.confirming)
        lines = _confirmation_lines(
            extension=extension,
            display_name=display_name,
            char_count=len(text),
            chunk_count=len(chunks),
            voice_name=voice_name,
            speed=speed,
            credits=credits,
            cost=cost,
            page_count=page_count,
            pages_with_text=pages_with_text,
            pages_without_text=pages_without_text,
        )
        logger.info(
            format_log_event(
                _file_event(extension, "extraction_completed"),
                telegram_user_id=user.id,
                job_id=document_store.get(user.id).job_id if document_store.get(user.id) else "none",
                extension=extension,
                chars=len(text),
                chunks=len(chunks),
                voice_key=voice_key,
                speech_speed=speed,
                pages=page_count if page_count is not None else "none",
                pages_with_text=pages_with_text if pages_with_text is not None else "none",
            )
        )
        logger.info(
            format_log_event(
                _file_event(extension, "confirmation_shown"),
                telegram_user_id=user.id,
                extension=extension,
                chars=len(text),
                chunks=len(chunks),
                pages=page_count if page_count is not None else "none",
            )
        )
        markup = build_pdf_confirm_keyboard() if extension == "pdf" else build_document_confirm_keyboard()
        await progress.edit_text("\n".join(lines), reply_markup=markup)
    except DocumentError as exc:
        event_name = "document_extraction_failed"
        if extension == "pdf":
            event_name = "pdf_no_text_found" if isinstance(exc, PdfNoTextError) else "pdf_validation_failed"
        logger.info(
            format_log_event(
                event_name,
                telegram_user_id=user.id,
                job_id=job_id or "none",
                exception_class=type(exc).__name__,
                extension=extension or "none",
            )
        )
        if progress is not None:
            await progress.edit_text(exc.user_message)
        else:
            await message.answer(exc.user_message)
    except Exception as exc:
        logger.exception(
            format_log_event(
                "pdf_validation_failed" if extension == "pdf" else "document_extraction_failed",
                telegram_user_id=user.id,
                job_id=job_id or "none",
                exception_class=type(exc).__name__,
                extension=extension or "none",
            )
        )
        error_text = (
            DOWNLOAD_FAILED
            if "download" in type(exc).__name__.lower()
            else "Не удалось обработать документ. Подробности записаны в журнал."
        )
        if progress is not None:
            await progress.edit_text(error_text)
        else:
            await message.answer(error_text)
    finally:
        if job_dir is not None:
            delete_job_directory(job_dir, job_id or None)
            logger.info(
                format_log_event(
                    _file_event(extension or "txt", "temp_files_cleaned"),
                    job_id=job_id or "none",
                )
            )


@router.callback_query(F.data == "doc:start")
async def start_document_generation(
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
        await safe_callback_answer(callback)
        return
    current = await state.get_state()
    if current != DocumentStates.confirming.state:
        await safe_callback_answer(callback)
        return
    draft = document_store.get(user.id)
    if draft is None:
        await safe_callback_answer(callback)
        if callback.message is not None:
            await callback.message.answer("Документ уже обработан или отменён.", reply_markup=build_main_keyboard())
        return
    if not active_jobs.try_acquire(user.id):
        if callback.message is not None:
            await callback.message.answer(BUSY_MESSAGE)
        await safe_callback_answer(callback)
        return

    await state.set_state(DocumentStates.processing)
    await safe_callback_answer(callback)
    snapshot = GenerationSnapshot(
        telegram_user_id=user.id,
        voice_id=draft.voice_id,
        voice_key=draft.voice_key,
        voice_name=draft.voice_name,
        speech_speed=draft.speech_speed,
        model_id=MODEL_ID,
        char_count=draft.char_count,
        chunks=list(draft.chunks),
        credit_multiplier=draft.credit_multiplier,
    )
    credits = estimate_credits(draft.char_count, draft.credit_multiplier)
    cost = estimate_cost_usd(credits, plan_price_usd, plan_credits)
    logger.info(
        format_log_event(
            _file_event(draft.extension, "tts_confirmed"),
            telegram_user_id=user.id,
            job_id=draft.job_id,
            extension=draft.extension,
            chars=draft.char_count,
            chunks=draft.chunk_count,
            voice_key=draft.voice_key,
            speech_speed=draft.speech_speed,
            pages=draft.page_count if draft.page_count is not None else "none",
        )
    )
    audio_name = audio_filename_from_document(draft.display_name)
    caption = f"Готово: {Path(audio_name).stem}"
    is_pdf = draft.extension == "pdf"
    try:
        await run_confirmed_long_tts(
            bot=callback.bot,
            chat_id=callback.message.chat.id if callback.message else user.id,
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
                source_type=_source_type_for_extension(draft.extension),
                request_type=REQUEST_TYPE_LONG,
                start_text="Документ обработан. Начинаю озвучивание…",
                progress_template="Озвучивание документа: фрагмент {current} из {total}",
                merge_text="Объединяю аудио…",
                done_text=f"Готово: {Path(audio_name).stem}",
                caption=caption,
                audio_filename=audio_name,
                completed_event="pdf_tts_completed" if is_pdf else "document_tts_completed",
                failed_event="pdf_tts_failed" if is_pdf else "document_tts_failed",
                job_id=draft.job_id,
                job_dir=draft.job_dir,
                page_count=draft.page_count,
                pages_with_text=draft.pages_with_text,
                estimated_credits=credits,
                estimated_cost_usd=cost,
            ),
        )
    finally:
        document_store.remove(user.id)
        await state.clear()
        active_jobs.release(user.id)


def _validate_document_meta(document: Document, max_upload_file_mb: int) -> str:
    file_name = document.file_name
    if not file_name or not str(file_name).strip():
        raise DocumentError(MISSING_NAME)
    extension = original_extension(file_name)
    mime = (document.mime_type or "").lower()
    if extension == "doc":
        logger.info(format_log_event("document_rejected", reason="doc_format", mime=mime or "none"))
        raise DocumentError(OLD_DOC_FORMAT)
    if extension not in {"txt", "docx", "pdf"}:
        logger.info(
            format_log_event(
                "document_rejected",
                reason="unsupported_extension",
                extension=extension or "none",
                mime=mime or "none",
            )
        )
        raise DocumentError(UNSUPPORTED_FORMAT)
    if document.file_size is not None and document.file_size > max_upload_file_mb * 1024 * 1024:
        logger.info(
            format_log_event(
                "pdf_validation_failed" if extension == "pdf" else "document_rejected",
                reason="too_large",
                size_bytes=document.file_size,
                extension=extension,
            )
        )
        raise DocumentError(TOO_LARGE_FILE.format(limit=max_upload_file_mb))
    if mime and extension == "txt" and mime not in TXT_MIME_TYPES:
        logger.info(format_log_event("document_rejected", reason="mime", extension=extension, mime=mime))
        raise DocumentError(UNSUPPORTED_FORMAT)
    if mime and extension == "docx" and mime not in DOCX_MIME_TYPES:
        logger.info(format_log_event("document_rejected", reason="mime", extension=extension, mime=mime))
        raise DocumentError(UNSUPPORTED_FORMAT)
    if mime and extension == "pdf" and mime not in PDF_MIME_TYPES:
        logger.info(
            format_log_event(
                "pdf_validation_failed",
                reason="mime",
                extension=extension,
                mime=mime,
            )
        )
        raise DocumentError(UNSUPPORTED_FORMAT)
    return extension


def _validate_downloaded_file(path: Path, extension: str) -> None:
    if extension == "txt":
        return
    if extension == "pdf":
        validate_pdf_header(path)
        return
    from services.document_text import validate_docx_container

    validate_docx_container(path, max_uncompressed_bytes=None)


async def _cancel_document(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is not None:
        draft = document_store.get(user.id)
        extension = draft.extension if draft is not None else ""
        _cleanup_document(user.id)
        logger.info(
            format_log_event(
                _file_event(extension, "cancelled") if extension else "document_cancelled",
                telegram_user_id=user.id,
            )
        )
    await state.clear()
    await message.answer("Загрузка документа отменена.", reply_markup=build_main_keyboard())


async def _discard_long_text(user_id: int, state: FSMContext) -> None:
    draft_store.remove(user_id)
    current = await state.get_state()
    if current and "LongText" in str(current):
        await state.clear()


def _cleanup_document(user_id: int) -> None:
    draft = document_store.remove(user_id)
    if draft is not None:
        delete_job_directory(draft.job_dir, draft.job_id)
        logger.info(
            format_log_event(
                _file_event(draft.extension, "temp_files_cleaned"),
                job_id=draft.job_id,
            )
        )


def _confirmation_lines(
    *,
    extension: str,
    display_name: str,
    char_count: int,
    chunk_count: int,
    voice_name: str,
    speed: float,
    credits: int,
    cost: Decimal | None,
    page_count: int | None,
    pages_with_text: int | None,
    pages_without_text: int | None,
) -> list[str]:
    if extension == "pdf":
        lines = [
            "PDF подготовлен к озвучиванию.",
            "",
            f"Файл: {display_name}",
            f"Страниц: {page_count}",
            f"Страниц с текстом: {pages_with_text}",
            f"Символов: {format_char_count(char_count)}",
            f"Фрагментов: {chunk_count}",
            f"Голос: {voice_name}",
            f"Скорость: {get_speed_by_value(speed).value}×",
            f"Ориентировочный расход: {credits} кредитов",
        ]
        if cost is not None:
            lines.append(f"Ориентировочная стоимость: {format(cost, 'f')} USD")
        if pages_without_text:
            lines.extend(
                [
                    "",
                    PDF_PARTIAL_WARNING,
                    f"Страниц без извлечённого текста: {pages_without_text}",
                ]
            )
        lines.extend(["", "Язык озвучивания определяется автоматически."])
        return lines
    lines = [
        "Документ подготовлен к озвучиванию.",
        "",
        f"Файл: {display_name}",
        f"Формат: {extension.upper()}",
        f"Символов: {format_char_count(char_count)}",
        f"Фрагментов: {chunk_count}",
        f"Голос: {voice_name}",
        f"Скорость: {get_speed_by_value(speed).value}×",
        f"Ориентировочный расход: {credits} кредитов",
    ]
    if cost is not None:
        lines.append(f"Ориентировочная стоимость: {format(cost, 'f')} USD")
    return lines


def _source_type_for_extension(extension: str) -> str:
    if extension == "txt":
        return SOURCE_TYPE_TXT
    if extension == "docx":
        return SOURCE_TYPE_DOCX
    return SOURCE_TYPE_PDF


def _file_event(extension: str, name: str) -> str:
    return f"pdf_{name}" if extension == "pdf" else f"document_{name}"
