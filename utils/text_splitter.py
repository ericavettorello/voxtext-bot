"""Безопасное разделение длинного текста на фрагменты для TTS."""

from __future__ import annotations

import re

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|(?<=\.\.\.)\s+")


def normalize_text(text: str) -> str:
    """Убрать лишние пробелы, сохранив пунктуацию и абзацы."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def split_text_into_chunks(text: str, max_chars: int) -> list[str]:
    """Разбить текст на фрагменты не длиннее max_chars без потери порядка."""
    if max_chars < 1:
        raise ValueError("max_chars должен быть положительным.")
    normalized = normalize_text(text)
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]
    chunks = _pack(normalized.split("\n\n"), max_chars, "\n\n", _split_paragraph)
    return [chunk for chunk in chunks if chunk]


def split_text(text: str, max_chars: int = 2500) -> list[str]:
    """Совместимая обёртка для существующего имени функции."""
    return split_text_into_chunks(text, max_chars)


def _split_paragraph(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = [part for part in _SENTENCE_SPLIT.split(text) if part]
    if len(sentences) > 1:
        return _pack(sentences, max_chars, " ", _split_sentence)
    return _split_sentence(text, max_chars)


def _split_sentence(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    words = [part for part in text.split(" ") if part]
    if len(words) > 1:
        return _pack(words, max_chars, " ", _hard_split)
    return _hard_split(text, max_chars)


def _hard_split(text: str, max_chars: int) -> list[str]:
    return [text[index : index + max_chars] for index in range(0, len(text), max_chars)]


def _pack(
    parts: list[str],
    max_chars: int,
    separator: str,
    finer,
) -> list[str]:
    chunks: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        if len(part) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(finer(part, max_chars))
            continue
        candidate = part if not current else f"{current}{separator}{part}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    return chunks
