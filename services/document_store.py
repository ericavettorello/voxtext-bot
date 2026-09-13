"""Временные черновики загруженных документов в памяти процесса."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class DocumentDraft:
    job_id: str
    job_dir: Path
    source_path: Path
    extension: str
    display_name: str
    text: str
    char_count: int
    chunk_count: int
    chunks: list[str]
    voice_id: str
    voice_key: str
    voice_name: str
    speech_speed: float
    credit_multiplier: float
    page_count: int | None = None
    pages_with_text: int | None = None
    pages_without_text: int | None = None


class DocumentStore:
    """Персональные документы. Исчезают после перезапуска процесса."""

    def __init__(self) -> None:
        self._drafts: dict[int, DocumentDraft] = {}

    def get(self, user_id: int) -> DocumentDraft | None:
        return self._drafts.get(user_id)

    def put(self, user_id: int, draft: DocumentDraft) -> DocumentDraft:
        self._drafts[user_id] = draft
        return draft

    def remove(self, user_id: int) -> DocumentDraft | None:
        return self._drafts.pop(user_id, None)


document_store = DocumentStore()
