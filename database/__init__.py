"""Слой работы с базой данных."""

from database.db import (
    CURRENT_SCHEMA_VERSION,
    MAX_SPEECH_SPEED,
    MIN_SPEECH_SPEED,
    Database,
    DatabaseError,
    SpeechSpeedError,
    init_database,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MAX_SPEECH_SPEED",
    "MIN_SPEECH_SPEED",
    "Database",
    "DatabaseError",
    "SpeechSpeedError",
    "init_database",
]
