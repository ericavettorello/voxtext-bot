"""Извлечение текста из PDF с текстовым слоем. OCR не используется."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import EmptyFileError, FileNotDecryptedError, PdfReadError, PdfStreamError
from pypdf.generic import ArrayObject, IndirectObject

from logging_config import format_log_event
from services.document_text import DocumentError
from utils.text_splitter import normalize_text

logger = logging.getLogger(__name__)

PDF_MIME_TYPES = {"application/pdf", "application/octet-stream"}
PDF_HEADER = b"%PDF-"
_PAGE_LOG_INTERVAL = 25


class PdfOpenError(DocumentError):
    def __init__(self) -> None:
        super().__init__(
            "Не удалось открыть PDF-файл. Возможно, файл повреждён или имеет неверный формат."
        )


class PdfEncryptedError(DocumentError):
    def __init__(self) -> None:
        super().__init__(
            "PDF защищён паролем.\n\n"
            "Снимите защиту с документа и отправьте файл повторно."
        )


class PdfNoTextError(DocumentError):
    def __init__(self) -> None:
        super().__init__(
            "В PDF не найден текст для озвучивания.\n\n"
            "Вероятно, документ состоит из отсканированных страниц или изображений. "
            "Распознавание сканов пока не поддерживается."
        )


class PdfComplexPageError(DocumentError):
    def __init__(self) -> None:
        super().__init__(
            "PDF содержит слишком сложную страницу и не может быть безопасно обработан."
        )


class PdfTooManyPagesError(DocumentError):
    def __init__(self, page_count: int, limit: int) -> None:
        super().__init__(
            "PDF содержит слишком много страниц.\n\n"
            f"Найдено страниц: {page_count}\n"
            f"Максимальное количество: {limit}\n\n"
            "Разделите документ на несколько файлов и отправьте их по отдельности."
        )
        self.page_count = page_count
        self.limit = limit


class PdfExtractError(DocumentError):
    def __init__(self) -> None:
        super().__init__("Не удалось извлечь текст из PDF. Возможно, файл повреждён.")


@dataclass(frozen=True)
class PdfExtractionResult:
    text: str
    total_pages: int
    pages_with_text: int
    pages_without_text: int


def validate_pdf_header(file_path: Path) -> None:
    """Проверить сигнатуру %PDF- в начале файла, не читая всё содержимое."""
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise PdfOpenError from exc
    if size <= 0:
        raise PdfOpenError
    try:
        with file_path.open("rb") as handle:
            header = handle.read(8)
    except OSError as exc:
        raise PdfOpenError from exc
    if not header.startswith(PDF_HEADER):
        raise PdfOpenError


def extract_text_from_pdf(
    file_path: Path,
    max_pages: int = 100,
    max_content_stream_bytes: int = 25 * 1024 * 1024,
    job_id: str | None = None,
) -> PdfExtractionResult:
    """Извлечь текст по страницам стандартным extract_text() без режима layout."""
    validate_pdf_header(file_path)
    try:
        reader = PdfReader(str(file_path))
    except FileNotDecryptedError as exc:
        raise PdfEncryptedError from exc
    except (EmptyFileError, PdfReadError, PdfStreamError, OSError, ValueError) as exc:
        logger.info(
            format_log_event(
                "pdf_validation_failed",
                job_id=job_id or "none",
                reason="open_failed",
                exception_class=type(exc).__name__,
            )
        )
        raise PdfOpenError from exc
    except Exception as exc:
        logger.exception(
            format_log_event(
                "pdf_validation_failed",
                job_id=job_id or "none",
                reason="open_failed",
                exception_class=type(exc).__name__,
            )
        )
        raise PdfOpenError from exc

    if reader.is_encrypted:
        raise PdfEncryptedError

    try:
        total_pages = len(reader.pages)
    except FileNotDecryptedError as exc:
        raise PdfEncryptedError from exc
    except Exception as exc:
        logger.exception(
            format_log_event(
                "pdf_validation_failed",
                job_id=job_id or "none",
                reason="structure",
                exception_class=type(exc).__name__,
            )
        )
        raise PdfOpenError from exc

    if total_pages <= 0:
        raise PdfOpenError
    if total_pages > max_pages:
        raise PdfTooManyPagesError(total_pages, max_pages)

    parts: list[str] = []
    pages_with_text = 0
    pages_without_text = 0
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            _ensure_page_content_limit(page, max_content_stream_bytes)
            page_text = page.extract_text() or ""
        except PdfComplexPageError:
            raise
        except MemoryError as exc:
            raise PdfComplexPageError from exc
        except FileNotDecryptedError as exc:
            raise PdfEncryptedError from exc
        except Exception as exc:
            logger.exception(
                format_log_event(
                    "pdf_validation_failed",
                    job_id=job_id or "none",
                    reason="extract_failed",
                    page=page_number,
                    pages=total_pages,
                    exception_class=type(exc).__name__,
                )
            )
            raise PdfExtractError from exc

        page_text = page_text.replace("\x00", "")
        normalized_page = normalize_text(page_text)
        if _should_log_page(page_number, total_pages):
            logger.info(
                format_log_event(
                    "pdf_page_extracted",
                    job_id=job_id or "none",
                    page=page_number,
                    pages=total_pages,
                    page_chars=len(normalized_page),
                )
            )
        if not normalized_page:
            pages_without_text += 1
            continue
        pages_with_text += 1
        parts.append(normalized_page)

    text = normalize_text("\n\n".join(parts).replace("\x00", ""))
    if not text:
        raise PdfNoTextError
    return PdfExtractionResult(
        text=text,
        total_pages=total_pages,
        pages_with_text=pages_with_text,
        pages_without_text=pages_without_text,
    )


def _should_log_page(page_number: int, total_pages: int) -> bool:
    return page_number == 1 or page_number == total_pages or page_number % _PAGE_LOG_INTERVAL == 0


def _ensure_page_content_limit(page, max_content_stream_bytes: int) -> None:
    try:
        size = _page_content_stream_size(page)
    except MemoryError as exc:
        raise PdfComplexPageError from exc
    except Exception as exc:
        logger.exception(
            format_log_event(
                "pdf_validation_failed",
                reason="content_stream",
                exception_class=type(exc).__name__,
            )
        )
        raise PdfComplexPageError from exc
    if size > max_content_stream_bytes:
        raise PdfComplexPageError


def _page_content_stream_size(page) -> int:
    return _object_stream_size(page.get("/Contents"))


def _object_stream_size(obj) -> int:
    obj = _resolve(obj)
    if obj is None:
        return 0
    if isinstance(obj, ArrayObject):
        return sum(_object_stream_size(item) for item in obj)
    declared = _declared_length(obj)
    decoded = 0
    get_data = getattr(obj, "get_data", None)
    if callable(get_data):
        data = get_data()
        if data:
            decoded = len(data)
    return max(declared, decoded)


def _declared_length(obj) -> int:
    getter = getattr(obj, "get", None)
    if not callable(getter):
        return 0
    length = _resolve(getter("/Length"))
    try:
        return int(length)
    except (TypeError, ValueError):
        return 0


def _resolve(obj):
    if isinstance(obj, IndirectObject):
        return obj.get_object()
    return obj
