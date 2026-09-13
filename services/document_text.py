"""Извлечение текста из TXT и DOCX без записи содержимого в журнал."""

from __future__ import annotations

import zipfile
from pathlib import Path

from charset_normalizer import from_bytes
from docx import Document
from docx.document import Document as DocumentType
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from utils.text_splitter import normalize_text

MAX_DOCX_ENTRIES = 2000
TXT_MIME_TYPES = {"text/plain", "application/octet-stream"}
DOCX_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/zip",
    "application/octet-stream",
}
DOCX_REQUIRED_PARTS = ("[Content_Types].xml", "word/document.xml")
_SKIP_XML_TAGS = {qn("w:del"), qn("w:moveFrom")}


class DocumentError(Exception):
    """Ошибка чтения документа, безопасная для показа пользователю."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class DocumentEncodingError(DocumentError):
    def __init__(self) -> None:
        super().__init__(
            "Не удалось определить кодировку TXT-файла.\n"
            "Сохраните файл в кодировке UTF-8 и отправьте повторно."
        )


class DocumentEmptyError(DocumentError):
    def __init__(self, kind: str = "text") -> None:
        if kind == "images":
            message = (
                "В документе не найден текст для озвучивания.\n"
                "Текст внутри изображений пока не распознаётся."
            )
        else:
            message = "Файл пустой. Отправьте документ с текстом."
        super().__init__(message)


def extract_text_from_document(
    file_path: Path,
    extension: str,
    max_uncompressed_bytes: int | None = None,
    max_pdf_pages: int | None = None,
    max_pdf_content_stream_bytes: int | None = None,
) -> str:
    """Единая точка извлечения TXT/DOCX/PDF с нормализацией абзацев."""
    ext = extension.lower().lstrip(".")
    if ext == "txt":
        text = extract_text_from_txt(file_path)
    elif ext == "docx":
        text = extract_text_from_docx(file_path, max_uncompressed_bytes=max_uncompressed_bytes)
    elif ext == "pdf":
        from services.pdf_text import extract_text_from_pdf

        return extract_text_from_pdf(
            file_path,
            max_pages=max_pdf_pages or 100,
            max_content_stream_bytes=max_pdf_content_stream_bytes or 25 * 1024 * 1024,
        ).text
    else:
        raise DocumentError("Этот формат пока не поддерживается.\nОтправьте файл TXT, DOCX или PDF.")
    return finalize_extracted_text(text)


def extract_text_from_txt(file_path: Path) -> str:
    data = file_path.read_bytes()
    if not data or not data.strip():
        raise DocumentEmptyError("text")
    if _looks_binary(data):
        raise DocumentError("Файл выглядит как двоичный, а не как текст.\nОтправьте обычный TXT.")
    text = _decode_txt_bytes(data)
    text = text.replace("\x00", "")
    if not text.strip():
        raise DocumentEmptyError("text")
    return text


def extract_text_from_docx(file_path: Path, max_uncompressed_bytes: int | None = None) -> str:
    validate_docx_container(file_path, max_uncompressed_bytes)
    document = Document(str(file_path))
    parts = [_block_text(block) for block in _iter_body_blocks(document)]
    text = "\n\n".join(part for part in parts if part)
    if not text.strip():
        raise DocumentEmptyError("images")
    return text


def finalize_extracted_text(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized:
        raise DocumentEmptyError("text")
    return normalized


def validate_docx_container(file_path: Path, max_uncompressed_bytes: int | None = None) -> None:
    if not zipfile.is_zipfile(file_path):
        raise DocumentError("Не удалось прочитать DOCX. Файл повреждён или это не документ Word.")
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = archive.namelist()
            if len(names) > MAX_DOCX_ENTRIES:
                raise DocumentError("Документ отклонён: слишком много элементов внутри файла.")
            if any(".." in name.replace("\\", "/") or name.startswith("/") for name in names):
                raise DocumentError("Не удалось прочитать DOCX. Файл повреждён или это не документ Word.")
            missing = [part for part in DOCX_REQUIRED_PARTS if part not in names]
            if missing:
                raise DocumentError("Файл не является корректным DOCX.")
            total = 0
            for info in archive.infolist():
                if info.file_size < 0 or info.compress_size < 0:
                    raise DocumentError("Не удалось прочитать DOCX. Файл повреждён или это не документ Word.")
                total += info.file_size
                if max_uncompressed_bytes is not None and total > max_uncompressed_bytes:
                    raise DocumentError("Документ отклонён: слишком большой распакованный размер.")
    except DocumentError:
        raise
    except zipfile.BadZipFile as exc:
        raise DocumentError("Не удалось прочитать DOCX. Файл повреждён или это не документ Word.") from exc


def _decode_txt_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    candidates: list[str] = []
    match = from_bytes(data).best()
    if match is not None:
        candidates.append(str(match))
    try:
        candidates.append(data.decode("cp1251"))
    except UnicodeDecodeError:
        pass
    if not candidates:
        raise DocumentEncodingError
    return max(candidates, key=_text_score)


def _text_score(text: str) -> int:
    cyrillic = sum(1 for char in text if "А" <= char <= "я" or char in "Ёё")
    private = sum(1 for char in text if ord(char) >= 0xE000)
    return cyrillic * 10 - text.count("\ufffd") * 50 - private * 20


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    sample = data[:2048]
    if not sample:
        return False
    suspicious = sum(1 for byte in sample if byte < 9 or 13 < byte < 32 or byte == 127)
    return suspicious / len(sample) > 0.30


def _iter_body_blocks(document: DocumentType):
    body = document.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def _block_text(block: Paragraph | Table) -> str:
    if isinstance(block, Paragraph):
        return _visible_paragraph_text(block)
    return _table_text(block)


def _visible_paragraph_text(paragraph: Paragraph) -> str:
    parts: list[str] = []
    _collect_visible_text(paragraph._element, parts)
    return "".join(parts).strip()


def _collect_visible_text(element, parts: list[str]) -> None:
    for child in element:
        if child.tag in _SKIP_XML_TAGS:
            continue
        if child.tag == qn("w:t") and child.text:
            parts.append(child.text)
        if child.tail:
            parts.append(child.tail)
        _collect_visible_text(child, parts)


def _table_text(table: Table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        seen: set[int] = set()
        for cell in row.cells:
            marker = id(cell._tc)
            if marker in seen:
                continue
            seen.add(marker)
            cell_text = " ".join(
                part for part in (_visible_paragraph_text(paragraph) for paragraph in cell.paragraphs) if part
            )
            if cell_text:
                cells.append(cell_text)
        if cells:
            rows.append(", ".join(cells))
    return "\n".join(rows)
