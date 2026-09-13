"""Извлечение текста из файлов TXT, DOCX и PDF."""

from pathlib import Path

from services.document_text import extract_text_from_document


def extract_text(file_path: str, extension: str | None = None) -> str:
    path = Path(file_path)
    ext = extension or path.suffix
    return extract_text_from_document(path, ext)
