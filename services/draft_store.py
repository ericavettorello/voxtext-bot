"""Временные черновики длинного текста в памяти процесса."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Draft:
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(part for part in self.parts if part)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def parts_count(self) -> int:
        return len(self.parts)


class DraftStore:
    """Персональные черновики. Исчезают после перезапуска процесса."""

    def __init__(self) -> None:
        self._drafts: dict[int, Draft] = {}

    def start(self, user_id: int) -> Draft:
        draft = Draft()
        self._drafts[user_id] = draft
        return draft

    def get(self, user_id: int) -> Draft | None:
        return self._drafts.get(user_id)

    def add_part(self, user_id: int, text: str) -> Draft:
        draft = self._drafts.setdefault(user_id, Draft())
        draft.parts.append(text)
        return draft

    def clear(self, user_id: int) -> None:
        if user_id in self._drafts:
            self._drafts[user_id] = Draft()

    def remove(self, user_id: int) -> None:
        self._drafts.pop(user_id, None)


draft_store = DraftStore()
