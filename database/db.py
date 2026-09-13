"""Асинхронная работа с SQLite: пользователи, голоса и запросы озвучивания."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from logging_config import format_log_event, voice_id_tail
from services.speech_speed import ALLOWED_SPEED_VALUES, DEFAULT_SPEECH_SPEED, is_allowed_speed

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 6
MIN_SPEECH_SPEED = 0.85
MAX_SPEECH_SPEED = 1.15
DEFAULT_VOICE_KEY = "default"
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_PARTIAL_FAILED = "partial_failed"
STATUS_CANCELLED = "cancelled"
SOURCE_TYPE_TEXT = "text"
SOURCE_TYPE_LONG_TEXT = "long_text"
SOURCE_TYPE_TXT = "txt"
SOURCE_TYPE_DOCX = "docx"
SOURCE_TYPE_PDF = "pdf"
REQUEST_TYPE_SHORT = "short"
REQUEST_TYPE_LONG = "long"

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL UNIQUE,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        language_code TEXT,
        selected_voice_id TEXT,
        selected_voice_key TEXT,
        speech_speed REAL NOT NULL DEFAULT 1.0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tts_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL,
        source_type TEXT NOT NULL DEFAULT 'text',
        request_type TEXT NOT NULL DEFAULT 'short',
        char_count INTEGER NOT NULL,
        chunk_count INTEGER NOT NULL DEFAULT 1,
        completed_chunks INTEGER NOT NULL DEFAULT 0,
        processed_characters INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        model_id TEXT,
        voice_key TEXT,
        voice_id TEXT,
        speech_speed REAL NOT NULL DEFAULT 1.0,
        estimated_credits INTEGER,
        estimated_cost_usd TEXT,
        actual_credits INTEGER,
        audio_size_bytes INTEGER,
        duration_ms INTEGER,
        error_code TEXT,
        page_count INTEGER,
        pages_with_text INTEGER,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS voices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voice_key TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        voice_id TEXT NOT NULL,
        gender TEXT,
        credit_multiplier REAL NOT NULL DEFAULT 1.0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tts_requests_user_id ON tts_requests(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_tts_requests_status ON tts_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_tts_requests_created_at ON tts_requests(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_voices_is_active ON voices(is_active)",
    """
    CREATE TABLE IF NOT EXISTS daily_usage (
        user_id INTEGER NOT NULL,
        usage_date TEXT NOT NULL,
        request_count INTEGER NOT NULL DEFAULT 0,
        character_count INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, usage_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daily_usage_date ON daily_usage(usage_date)",
)

USERS_NEW_COLUMNS = (
    ("selected_voice_key", "TEXT"),
)
TTS_NEW_COLUMNS = (
    ("voice_key", "TEXT"),
    ("estimated_credits", "INTEGER"),
    ("estimated_cost_usd", "TEXT"),
    ("actual_credits", "INTEGER"),
)


class DatabaseError(Exception):
    """Ошибка слоя базы данных."""


class SpeechSpeedError(ValueError):
    """Скорость речи вне допустимого диапазона."""


def utc_now() -> str:
    """Текущее время UTC в ISO 8601."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_speech_speed(speed: float) -> float:
    value = float(speed)
    if not is_allowed_speed(value):
        raise SpeechSpeedError(
            f"speech_speed must be one of {sorted(ALLOWED_SPEED_VALUES)}"
        )
    return value


class Database:
    """Постоянное асинхронное соединение SQLite для VoxText."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> aiosqlite.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = await aiosqlite.connect(self.path)
            self._connection.row_factory = aiosqlite.Row
            await self._connection.execute("PRAGMA foreign_keys = ON")
            await self._connection.execute("PRAGMA journal_mode = WAL")
            await self._connection.execute("PRAGMA busy_timeout = 5000")
        return self._connection

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def init_database(self) -> None:
        """Создать файл, таблицы и индексы без удаления существующих данных."""
        try:
            connection = await self.connect()
            async with self._lock:
                for statement in SCHEMA_STATEMENTS:
                    await connection.execute(statement)
                await self._apply_migrations(connection)
                await connection.commit()
            logger.info(
                format_log_event(
                    "database_initialized",
                    path=self.path.name,
                    schema_version=CURRENT_SCHEMA_VERSION,
                )
            )
        except Exception:
            logger.exception(format_log_event("database_initialization_failed"))
            raise

    async def upsert_user(
        self,
        telegram_user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        default_voice_id: str | None = None,
    ) -> dict[str, Any]:
        username = _optional_text(username)
        first_name = _optional_text(first_name)
        last_name = _optional_text(last_name)
        language_code = _optional_text(language_code)
        now = utc_now()

        async with self._lock:
            connection = await self.connect()
            existing = await self._fetch_user(connection, telegram_user_id)
            if existing is None:
                cursor = await connection.execute(
                    """
                    INSERT INTO users (
                        telegram_user_id, username, first_name, last_name,
                        language_code, selected_voice_id, selected_voice_key,
                        speech_speed, is_active, created_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        telegram_user_id,
                        username,
                        first_name,
                        last_name,
                        language_code,
                        default_voice_id or None,
                        DEFAULT_VOICE_KEY,
                        DEFAULT_SPEECH_SPEED,
                        now,
                        now,
                    ),
                )
                await connection.commit()
                user = await self._fetch_user_by_id(connection, cursor.lastrowid)
                logger.info(
                    format_log_event(
                        "user_created",
                        telegram_user_id=telegram_user_id,
                        user_id=user["id"] if user else "none",
                    )
                )
                return dict(user) if user else {}

            await connection.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_name = ?,
                    language_code = ?, last_seen_at = ?
                WHERE telegram_user_id = ?
                """,
                (username, first_name, last_name, language_code, now, telegram_user_id),
            )
            await connection.commit()
            user = await self._fetch_user(connection, telegram_user_id)
            logger.info(
                format_log_event(
                    "user_updated",
                    telegram_user_id=telegram_user_id,
                    user_id=user["id"] if user else "none",
                )
            )
            return dict(user) if user else {}

    async def get_user_by_telegram_id(self, telegram_user_id: int) -> dict[str, Any] | None:
        async with self._lock:
            connection = await self.connect()
            user = await self._fetch_user(connection, telegram_user_id)
            return dict(user) if user else None

    async def update_user_last_seen(self, telegram_user_id: int) -> None:
        async with self._lock:
            connection = await self.connect()
            await connection.execute(
                "UPDATE users SET last_seen_at = ? WHERE telegram_user_id = ?",
                (utc_now(), telegram_user_id),
            )
            await connection.commit()

    async def update_user_voice(
        self,
        telegram_user_id: int,
        voice_key: str,
        voice_id: str | None = None,
    ) -> None:
        async with self._lock:
            connection = await self.connect()
            if voice_id is None:
                await connection.execute(
                    "UPDATE users SET selected_voice_key = ? WHERE telegram_user_id = ?",
                    (voice_key, telegram_user_id),
                )
            else:
                await connection.execute(
                    """
                    UPDATE users
                    SET selected_voice_key = ?, selected_voice_id = ?
                    WHERE telegram_user_id = ?
                    """,
                    (voice_key, voice_id, telegram_user_id),
                )
            await connection.commit()

    async def get_user_speech_speed(self, telegram_user_id: int) -> float:
        user = await self.get_user_by_telegram_id(telegram_user_id)
        if user is None or user.get("speech_speed") is None:
            return DEFAULT_SPEECH_SPEED
        return float(user["speech_speed"])

    async def update_user_speed(self, telegram_user_id: int, speed: float) -> None:
        validated = validate_speech_speed(speed)
        async with self._lock:
            connection = await self.connect()
            await connection.execute(
                "UPDATE users SET speech_speed = ? WHERE telegram_user_id = ?",
                (validated, telegram_user_id),
            )
            await connection.commit()

    async def create_tts_request(
        self,
        request_id: str,
        telegram_user_id: int,
        char_count: int,
        model_id: str | None = None,
        voice_id: str | None = None,
        voice_key: str | None = None,
        speech_speed: float = DEFAULT_SPEECH_SPEED,
        source_type: str = SOURCE_TYPE_TEXT,
        request_type: str = REQUEST_TYPE_SHORT,
        chunk_count: int = 1,
        page_count: int | None = None,
        pages_with_text: int | None = None,
        estimated_credits: int | None = None,
        estimated_cost_usd: Decimal | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            connection = await self.connect()
            user = await self._fetch_user(connection, telegram_user_id)
            if user is None:
                raise DatabaseError("user_not_found")
            speed = float(speech_speed)
            now = utc_now()
            used_voice_key = voice_key or DEFAULT_VOICE_KEY
            cursor = await connection.execute(
                """
                INSERT INTO tts_requests (
                    request_id, user_id, source_type, request_type, char_count,
                    chunk_count, completed_chunks, processed_characters, status,
                    model_id, voice_key, voice_id, speech_speed, created_at,
                    page_count, pages_with_text, estimated_credits, estimated_cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    user["id"],
                    source_type,
                    request_type,
                    char_count,
                    chunk_count,
                    STATUS_PROCESSING,
                    model_id,
                    used_voice_key,
                    voice_id,
                    speed,
                    now,
                    page_count,
                    pages_with_text,
                    estimated_credits,
                    _decimal_to_db(estimated_cost_usd),
                ),
            )
            await connection.commit()
            logger.info(
                format_log_event(
                    "tts_request_created",
                    request_id=request_id,
                    telegram_user_id=telegram_user_id,
                    user_id=user["id"],
                    chars=char_count,
                    chunks=chunk_count,
                    status=STATUS_PROCESSING,
                    voice_key=used_voice_key,
                    voice_id_tail=voice_id_tail(voice_id),
                    source_type=source_type,
                    pages=page_count if page_count is not None else "none",
                    pages_with_text=pages_with_text if pages_with_text is not None else "none",
                )
            )
            row = await self._fetch_request_by_pk(connection, cursor.lastrowid)
            return dict(row) if row else {}

    async def mark_tts_request_success(
        self,
        request_id: str,
        audio_size_bytes: int,
        duration_ms: int,
        estimated_credits: int | None = None,
        estimated_cost_usd: Decimal | None = None,
        actual_credits: int | None = None,
    ) -> None:
        cost_value = _decimal_to_db(estimated_cost_usd)
        async with self._lock:
            connection = await self.connect()
            await connection.execute(
                """
                UPDATE tts_requests
                SET status = ?, audio_size_bytes = ?, duration_ms = ?,
                    completed_at = ?, error_code = NULL,
                    estimated_credits = ?, estimated_cost_usd = ?,
                    actual_credits = ?,
                    completed_chunks = COALESCE(chunk_count, 1),
                    processed_characters = COALESCE(char_count, 0)
                WHERE request_id = ?
                """,
                (
                    STATUS_SUCCESS,
                    audio_size_bytes,
                    duration_ms,
                    utc_now(),
                    estimated_credits,
                    cost_value,
                    actual_credits,
                    request_id,
                ),
            )
            await connection.commit()
        logger.info(
            format_log_event(
                "tts_request_success",
                request_id=request_id,
                status=STATUS_SUCCESS,
                size_bytes=audio_size_bytes,
                duration_ms=duration_ms,
                estimated_credits=estimated_credits if estimated_credits is not None else "none",
            )
        )
        logger.info(
            format_log_event(
                "tts_usage_recorded",
                request_id=request_id,
                status=STATUS_SUCCESS,
                estimated_credits=estimated_credits if estimated_credits is not None else "none",
            )
        )

    async def mark_tts_request_failed(
        self,
        request_id: str,
        error_code: str | None,
        duration_ms: int | None = None,
    ) -> None:
        async with self._lock:
            connection = await self.connect()
            await connection.execute(
                """
                UPDATE tts_requests
                SET status = ?, error_code = ?, completed_at = ?,
                    duration_ms = COALESCE(?, duration_ms)
                WHERE request_id = ?
                """,
                (STATUS_FAILED, error_code, utc_now(), duration_ms, request_id),
            )
            await connection.commit()
        logger.info(
            format_log_event(
                "tts_request_failed",
                request_id=request_id,
                status=STATUS_FAILED,
                error_code=error_code,
            )
        )

    async def update_tts_request_progress(
        self,
        request_id: str,
        completed_chunks: int,
        processed_characters: int,
    ) -> None:
        async with self._lock:
            connection = await self.connect()
            await connection.execute(
                """
                UPDATE tts_requests
                SET completed_chunks = ?, processed_characters = ?
                WHERE request_id = ?
                """,
                (completed_chunks, processed_characters, request_id),
            )
            await connection.commit()

    async def mark_tts_request_partial_failed(
        self,
        request_id: str,
        error_code: str | None,
        duration_ms: int | None = None,
        completed_chunks: int = 0,
        processed_characters: int = 0,
    ) -> None:
        async with self._lock:
            connection = await self.connect()
            await connection.execute(
                """
                UPDATE tts_requests
                SET status = ?, error_code = ?, completed_at = ?,
                    duration_ms = COALESCE(?, duration_ms),
                    completed_chunks = ?, processed_characters = ?
                WHERE request_id = ?
                """,
                (
                    STATUS_PARTIAL_FAILED,
                    error_code,
                    utc_now(),
                    duration_ms,
                    completed_chunks,
                    processed_characters,
                    request_id,
                ),
            )
            await connection.commit()

    async def mark_tts_request_cancelled(self, request_id: str) -> None:
        async with self._lock:
            connection = await self.connect()
            await connection.execute(
                """
                UPDATE tts_requests
                SET status = ?, completed_at = ?
                WHERE request_id = ?
                """,
                (STATUS_CANCELLED, utc_now(), request_id),
            )
            await connection.commit()

    async def sync_voices(self, voices: Iterable[Any]) -> None:
        """Добавить или обновить голоса из конфигурации. Историю не удалять."""
        now = utc_now()
        configured_keys: list[str] = []
        async with self._lock:
            connection = await self.connect()
            for voice in voices:
                voice_key = getattr(voice, "key")
                display_name = getattr(voice, "display_name")
                voice_id = getattr(voice, "voice_id")
                gender = getattr(voice, "gender", None)
                multiplier = float(getattr(voice, "credit_multiplier", 1.0))
                configured_keys.append(voice_key)
                existing = await (
                    await connection.execute(
                        "SELECT id, credit_multiplier FROM voices WHERE voice_key = ?",
                        (voice_key,),
                    )
                ).fetchone()
                if existing is None:
                    await connection.execute(
                        """
                        INSERT INTO voices (
                            voice_key, display_name, voice_id, gender,
                            credit_multiplier, is_active, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (voice_key, display_name, voice_id, gender, multiplier, now, now),
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE voices
                        SET display_name = ?, voice_id = ?, gender = ?,
                            is_active = 1, updated_at = ?
                        WHERE voice_key = ?
                        """,
                        (display_name, voice_id, gender, now, voice_key),
                    )
            if configured_keys:
                placeholders = ", ".join("?" for _ in configured_keys)
                await connection.execute(
                    f"""
                    UPDATE voices
                    SET is_active = 0, updated_at = ?
                    WHERE voice_key NOT IN ({placeholders})
                    """,
                    (now, *configured_keys),
                )
            await connection.commit()

    async def get_active_voices(self) -> list[dict[str, Any]]:
        async with self._lock:
            connection = await self.connect()
            cursor = await connection.execute(
                """
                SELECT *
                FROM voices
                WHERE is_active = 1
                ORDER BY CASE voice_key
                    WHEN 'default' THEN 0
                    WHEN 'female' THEN 1
                    WHEN 'male' THEN 2
                    ELSE 3
                END
                """
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_voice_by_key(self, voice_key: str) -> dict[str, Any] | None:
        async with self._lock:
            connection = await self.connect()
            cursor = await connection.execute(
                "SELECT * FROM voices WHERE voice_key = ?",
                (voice_key,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_user_request_count(self, telegram_user_id: int) -> int:
        async with self._lock:
            connection = await self.connect()
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM tts_requests AS r
                JOIN users AS u ON u.id = r.user_id
                WHERE u.telegram_user_id = ?
                """,
                (telegram_user_id,),
            )
            row = await cursor.fetchone()
            return int(row["total"]) if row else 0

    async def get_user_character_total(self, telegram_user_id: int) -> int:
        async with self._lock:
            connection = await self.connect()
            cursor = await connection.execute(
                """
                SELECT COALESCE(SUM(r.char_count), 0) AS total
                FROM tts_requests AS r
                JOIN users AS u ON u.id = r.user_id
                WHERE u.telegram_user_id = ?
                """,
                (telegram_user_id,),
            )
            row = await cursor.fetchone()
            return int(row["total"]) if row else 0

    async def get_user_usage_statistics(self, telegram_user_id: int) -> dict[str, Any] | None:
        async with self._lock:
            connection = await self.connect()
            user = await self._fetch_user(connection, telegram_user_id)
            if user is None:
                return None
            cursor = await connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0) AS success_count,
                    COALESCE(SUM(CASE WHEN status IN (?, ?) THEN 1 ELSE 0 END), 0) AS failed_count,
                    COALESCE(SUM(CASE WHEN status = ? THEN char_count ELSE 0 END), 0)
                        AS success_char_count,
                    COALESCE(SUM(CASE WHEN status = ? THEN estimated_credits ELSE 0 END), 0)
                        AS estimated_credits
                FROM tts_requests
                WHERE user_id = ?
                """,
                (
                    STATUS_SUCCESS,
                    STATUS_FAILED,
                    STATUS_PARTIAL_FAILED,
                    STATUS_SUCCESS,
                    STATUS_SUCCESS,
                    user["id"],
                ),
            )
            row = await cursor.fetchone()
            cost_rows = await (
                await connection.execute(
                    """
                    SELECT estimated_cost_usd
                    FROM tts_requests
                    WHERE user_id = ? AND status = ?
                    """,
                    (user["id"], STATUS_SUCCESS),
                )
            ).fetchall()
        return {
            "telegram_user_id": telegram_user_id,
            "success_count": int(row["success_count"]) if row else 0,
            "failed_count": int(row["failed_count"]) if row else 0,
            "success_char_count": int(row["success_char_count"]) if row else 0,
            "estimated_credits": int(row["estimated_credits"]) if row else 0,
            "estimated_cost_usd": _sum_decimals(
                cost_row["estimated_cost_usd"] for cost_row in cost_rows
            ),
            "selected_voice_key": user["selected_voice_key"] or DEFAULT_VOICE_KEY,
            "speech_speed": float(user["speech_speed"]),
        }

    async def get_general_statistics(self) -> dict[str, int]:
        async with self._lock:
            connection = await self.connect()
            users = await (await connection.execute("SELECT COUNT(*) AS total FROM users")).fetchone()
            requests = await (
                await connection.execute("SELECT COUNT(*) AS total FROM tts_requests")
            ).fetchone()
            success = await (
                await connection.execute(
                    "SELECT COUNT(*) AS total FROM tts_requests WHERE status = ?",
                    (STATUS_SUCCESS,),
                )
            ).fetchone()
            failed = await (
                await connection.execute(
                    "SELECT COUNT(*) AS total FROM tts_requests WHERE status = ?",
                    (STATUS_FAILED,),
                )
            ).fetchone()
            chars = await (
                await connection.execute(
                    "SELECT COALESCE(SUM(char_count), 0) AS total FROM tts_requests"
                )
            ).fetchone()
        return {
            "users_total": int(users["total"]) if users else 0,
            "requests_total": int(requests["total"]) if requests else 0,
            "success_total": int(success["total"]) if success else 0,
            "failed_total": int(failed["total"]) if failed else 0,
            "characters_total": int(chars["total"]) if chars else 0,
        }

    async def get_general_usage_statistics(self) -> dict[str, Any]:
        month_start = (
            datetime.now(timezone.utc)
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        async with self._lock:
            connection = await self.connect()
            users = await (await connection.execute("SELECT COUNT(*) AS total FROM users")).fetchone()
            new_users = await (
                await connection.execute(
                    "SELECT COUNT(*) AS total FROM users WHERE created_at >= ?",
                    (month_start,),
                )
            ).fetchone()
            success = await (
                await connection.execute(
                    "SELECT COUNT(*) AS total FROM tts_requests WHERE status = ?",
                    (STATUS_SUCCESS,),
                )
            ).fetchone()
            failed = await (
                await connection.execute(
                    "SELECT COUNT(*) AS total FROM tts_requests WHERE status = ?",
                    (STATUS_FAILED,),
                )
            ).fetchone()
            usage = await (
                await connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(char_count), 0) AS success_char_count,
                        COALESCE(SUM(estimated_credits), 0) AS estimated_credits
                    FROM tts_requests
                    WHERE status = ?
                    """,
                    (STATUS_SUCCESS,),
                )
            ).fetchone()
            cost_rows = await (
                await connection.execute(
                    """
                    SELECT estimated_cost_usd
                    FROM tts_requests
                    WHERE status = ?
                    """,
                    (STATUS_SUCCESS,),
                )
            ).fetchall()
            voice_rows = await (
                await connection.execute(
                    """
                    SELECT
                        COALESCE(voice_key, 'unknown') AS voice_key,
                        COUNT(*) AS success_count,
                        COALESCE(SUM(char_count), 0) AS char_count,
                        COALESCE(SUM(estimated_credits), 0) AS estimated_credits
                    FROM tts_requests
                    WHERE status = ?
                    GROUP BY voice_key
                    ORDER BY voice_key
                    """,
                    (STATUS_SUCCESS,),
                )
            ).fetchall()
        return {
            "users_total": int(users["total"]) if users else 0,
            "new_users_this_month": int(new_users["total"]) if new_users else 0,
            "success_total": int(success["total"]) if success else 0,
            "failed_total": int(failed["total"]) if failed else 0,
            "success_char_count": int(usage["success_char_count"]) if usage else 0,
            "estimated_credits": int(usage["estimated_credits"]) if usage else 0,
            "estimated_cost_usd": _sum_decimals(row["estimated_cost_usd"] for row in cost_rows),
            "usage_by_voice": [
                {
                    "voice_key": row["voice_key"],
                    "success_count": int(row["success_count"]),
                    "char_count": int(row["char_count"]),
                    "estimated_credits": int(row["estimated_credits"]),
                }
                for row in voice_rows
            ],
        }

    async def try_reserve_daily_quota(
        self,
        telegram_user_id: int,
        character_count: int,
        settings,
        now: datetime | None = None,
    ):
        """Атомарно зарезервировать один запрос и символы на текущие сутки."""
        from services.daily_quota import DailyQuotaError, build_quota_result, denial_reasons, usage_date_today

        chars = int(character_count)
        if chars <= 0:
            raise DailyQuotaError
        usage_date = usage_date_today(settings.timezone_name, now)
        async with self._lock:
            connection = await self.connect()
            await self._begin_immediate(connection)
            try:
                used_requests, used_characters = await self._read_daily_usage(
                    connection, telegram_user_id, usage_date
                )
                reason = denial_reasons(used_requests, used_characters, chars, settings)
                if reason is not None:
                    await connection.commit()
                    return build_quota_result(
                        used_requests=used_requests,
                        used_characters=used_characters,
                        settings=settings,
                        usage_date=usage_date,
                        allowed=False,
                        reason=reason,
                    )
                await connection.execute(
                    """
                    INSERT INTO daily_usage (user_id, usage_date, request_count, character_count, updated_at)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(user_id, usage_date) DO UPDATE SET
                        request_count = request_count + 1,
                        character_count = character_count + excluded.character_count,
                        updated_at = excluded.updated_at
                    """,
                    (telegram_user_id, usage_date, chars, utc_now()),
                )
                used_requests += 1
                used_characters += chars
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return build_quota_result(
            used_requests=used_requests,
            used_characters=used_characters,
            settings=settings,
            usage_date=usage_date,
            reserved=True,
        )

    async def get_daily_quota_status(self, telegram_user_id: int, settings, now: datetime | None = None):
        from services.daily_quota import build_quota_result, usage_date_today

        usage_date = usage_date_today(settings.timezone_name, now)
        async with self._lock:
            connection = await self.connect()
            used_requests, used_characters = await self._read_daily_usage(
                connection, telegram_user_id, usage_date
            )
        return build_quota_result(
            used_requests=used_requests,
            used_characters=used_characters,
            settings=settings,
            usage_date=usage_date,
        )

    async def release_daily_quota(
        self,
        telegram_user_id: int,
        character_count: int,
        usage_date: str,
    ):
        from services.daily_quota import DailyQuotaSettings, build_quota_result

        chars = max(0, int(character_count))
        async with self._lock:
            connection = await self.connect()
            await self._begin_immediate(connection)
            try:
                used_requests, used_characters = await self._read_daily_usage(
                    connection, telegram_user_id, usage_date
                )
                next_requests = max(0, used_requests - 1)
                next_characters = max(0, used_characters - chars)
                if used_requests or used_characters:
                    await connection.execute(
                        """
                        UPDATE daily_usage
                        SET request_count = ?, character_count = ?, updated_at = ?
                        WHERE user_id = ? AND usage_date = ?
                        """,
                        (next_requests, next_characters, utc_now(), telegram_user_id, usage_date),
                    )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return build_quota_result(
            used_requests=next_requests,
            used_characters=next_characters,
            settings=DailyQuotaSettings(),
            usage_date=usage_date,
        )

    async def _read_daily_usage(
        self,
        connection: aiosqlite.Connection,
        telegram_user_id: int,
        usage_date: str,
    ) -> tuple[int, int]:
        cursor = await connection.execute(
            """
            SELECT request_count, character_count
            FROM daily_usage
            WHERE user_id = ? AND usage_date = ?
            """,
            (telegram_user_id, usage_date),
        )
        row = await cursor.fetchone()
        if row is None:
            return 0, 0
        return max(0, int(row["request_count"])), max(0, int(row["character_count"]))

    async def _begin_immediate(self, connection: aiosqlite.Connection) -> None:
        try:
            await connection.execute("BEGIN IMMEDIATE")
        except Exception:
            await connection.rollback()
            await connection.execute("BEGIN IMMEDIATE")

    async def _daily_quota_snapshot(
        self,
        connection: aiosqlite.Connection,
        usage_date: str,
        request_limit: int,
        character_limit: int,
    ) -> dict[str, int]:
        cursor = await connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN request_count >= ? THEN 1 ELSE 0 END), 0) AS users_at_request_limit,
                COALESCE(SUM(CASE WHEN character_count >= ? THEN 1 ELSE 0 END), 0) AS users_at_character_limit,
                COALESCE(SUM(request_count), 0) AS requests_today,
                COALESCE(SUM(character_count), 0) AS characters_today
            FROM daily_usage
            WHERE usage_date = ?
            """,
            (request_limit, character_limit, usage_date),
        )
        row = await cursor.fetchone()
        return {
            "daily_quota_users_at_request_limit": int(row["users_at_request_limit"]) if row else 0,
            "daily_quota_users_at_character_limit": int(row["users_at_character_limit"]) if row else 0,
            "daily_quota_requests_today": int(row["requests_today"]) if row else 0,
            "daily_quota_characters_today": int(row["characters_today"]) if row else 0,
        }

    async def get_admin_overview_statistics(
        self,
        now: datetime | None = None,
        usage_date: str | None = None,
        request_limit: int = 5,
        character_limit: int = 20000,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc).replace(microsecond=0)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        today_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        hours_24 = (current - timedelta(hours=24)).isoformat()
        days_7 = (current - timedelta(days=7)).isoformat()
        days_30 = (current - timedelta(days=30)).isoformat()
        async with self._lock:
            connection = await self.connect()
            users_total = await self._count(connection, "SELECT COUNT(*) AS total FROM users")
            new_today = await self._count(
                connection, "SELECT COUNT(*) AS total FROM users WHERE created_at >= ?", (today_start,)
            )
            new_7d = await self._count(
                connection, "SELECT COUNT(*) AS total FROM users WHERE created_at >= ?", (days_7,)
            )
            new_30d = await self._count(
                connection, "SELECT COUNT(*) AS total FROM users WHERE created_at >= ?", (days_30,)
            )
            active_24h = await self._count(
                connection,
                "SELECT COUNT(DISTINCT user_id) AS total FROM tts_requests WHERE created_at >= ?",
                (hours_24,),
            )
            active_7d = await self._count(
                connection,
                "SELECT COUNT(DISTINCT user_id) AS total FROM tts_requests WHERE created_at >= ?",
                (days_7,),
            )
            active_30d = await self._count(
                connection,
                "SELECT COUNT(DISTINCT user_id) AS total FROM tts_requests WHERE created_at >= ?",
                (days_30,),
            )
            usage = await self._request_usage_snapshot(connection, None)
            popular_voice = await self._popular_voice_name(connection)
            popular_speed = await self._popular_speed(connection)
            from services.daily_quota import usage_date_today

            quota_date = usage_date or usage_date_today("UTC", current)
            quota = await self._daily_quota_snapshot(
                connection, quota_date, request_limit, character_limit
            )
        usage.update(
            {
                "users_total": users_total,
                "new_users_today": new_today,
                "new_users_7d": new_7d,
                "new_users_30d": new_30d,
                "active_users_24h": active_24h,
                "active_users_7d": active_7d,
                "active_users_30d": active_30d,
                "popular_voice_name": popular_voice,
                "popular_speed": popular_speed,
            }
        )
        usage.update(quota)
        return usage

    async def get_admin_period_statistics(
        self,
        start_iso: str | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc).replace(microsecond=0)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        end_iso = current.isoformat()
        async with self._lock:
            connection = await self.connect()
            if start_iso:
                new_users = await self._count(
                    connection,
                    "SELECT COUNT(*) AS total FROM users WHERE created_at >= ? AND created_at <= ?",
                    (start_iso, end_iso),
                )
                active_users = await self._count(
                    connection,
                    """
                    SELECT COUNT(DISTINCT user_id) AS total
                    FROM tts_requests
                    WHERE created_at >= ? AND created_at <= ?
                    """,
                    (start_iso, end_iso),
                )
            else:
                new_users = await self._count(connection, "SELECT COUNT(*) AS total FROM users")
                active_users = await self._count(
                    connection, "SELECT COUNT(DISTINCT user_id) AS total FROM tts_requests"
                )
            usage = await self._request_usage_snapshot(connection, start_iso, end_iso if start_iso else None)
        usage.update({"new_users": new_users, "active_users": active_users})
        return usage

    async def fetch_users_export_rows(self) -> list[dict[str, Any]]:
        async with self._lock:
            connection = await self.connect()
            cursor = await connection.execute(
                """
                SELECT
                    u.telegram_user_id,
                    u.username,
                    u.first_name,
                    u.last_name,
                    u.created_at,
                    u.selected_voice_key,
                    u.speech_speed,
                    COALESCE(v.display_name, u.selected_voice_key) AS selected_voice_name,
                    COUNT(r.id) AS requests_count,
                    COALESCE(SUM(CASE WHEN r.status = ? THEN 1 ELSE 0 END), 0) AS successful_requests,
                    COALESCE(SUM(CASE WHEN r.status = ? THEN r.char_count ELSE 0 END), 0)
                        AS total_characters,
                    COALESCE(SUM(CASE WHEN r.status = ? THEN r.estimated_credits ELSE 0 END), 0)
                        AS estimated_credits,
                    MAX(r.created_at) AS last_activity_at
                FROM users AS u
                LEFT JOIN tts_requests AS r ON r.user_id = u.id
                LEFT JOIN voices AS v ON v.voice_key = u.selected_voice_key
                GROUP BY u.id
                ORDER BY u.created_at
                """,
                (STATUS_SUCCESS, STATUS_SUCCESS, STATUS_SUCCESS),
            )
            user_rows = await cursor.fetchall()
            cost_cursor = await connection.execute(
                """
                SELECT u.telegram_user_id, r.estimated_cost_usd
                FROM tts_requests AS r
                JOIN users AS u ON u.id = r.user_id
                WHERE r.status = ?
                """,
                (STATUS_SUCCESS,),
            )
            cost_rows = await cost_cursor.fetchall()
        costs: dict[int, Decimal | None] = {}
        for row in cost_rows:
            telegram_id = int(row["telegram_user_id"])
            costs[telegram_id] = _sum_decimals(
                [costs.get(telegram_id), row["estimated_cost_usd"]]
            )
        result: list[dict[str, Any]] = []
        for row in user_rows:
            telegram_id = int(row["telegram_user_id"])
            cost = costs.get(telegram_id)
            result.append(
                {
                    "telegram_user_id": telegram_id,
                    "username": row["username"],
                    "first_name": row["first_name"],
                    "last_name": row["last_name"],
                    "created_at": row["created_at"],
                    "last_activity_at": row["last_activity_at"],
                    "selected_voice_name": row["selected_voice_name"],
                    "speech_speed": row["speech_speed"],
                    "requests_count": int(row["requests_count"]),
                    "successful_requests": int(row["successful_requests"]),
                    "total_characters": int(row["total_characters"]),
                    "estimated_credits": int(row["estimated_credits"]),
                    "estimated_cost_usd": None if cost is None else format(cost, "f"),
                }
            )
        return result

    async def fetch_requests_export_rows(self, start_iso: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            connection = await self.connect()
            sql = """
                SELECT
                    r.request_id,
                    u.telegram_user_id,
                    r.source_type,
                    r.status,
                    r.char_count AS character_count,
                    r.processed_characters,
                    r.chunk_count,
                    r.completed_chunks,
                    COALESCE(v.display_name, r.voice_key) AS voice_name,
                    r.speech_speed,
                    r.estimated_credits,
                    r.estimated_cost_usd,
                    r.page_count,
                    r.created_at,
                    r.completed_at,
                    r.error_code
                FROM tts_requests AS r
                JOIN users AS u ON u.id = r.user_id
                LEFT JOIN voices AS v ON v.voice_key = r.voice_key
            """
            params: tuple[Any, ...] = ()
            if start_iso:
                sql += " WHERE r.created_at >= ?"
                params = (start_iso,)
            sql += " ORDER BY r.created_at"
            cursor = await connection.execute(sql, params)
            rows = await cursor.fetchall()
        return [
            {
                "request_id": row["request_id"],
                "telegram_user_id": int(row["telegram_user_id"]),
                "source_type": row["source_type"],
                "status": row["status"],
                "character_count": row["character_count"],
                "processed_characters": row["processed_characters"],
                "chunk_count": row["chunk_count"],
                "completed_chunks": row["completed_chunks"],
                "voice_name": row["voice_name"],
                "speech_speed": row["speech_speed"],
                "estimated_credits": row["estimated_credits"],
                "estimated_cost_usd": row["estimated_cost_usd"],
                "page_count": row["page_count"],
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
                "error_code": row["error_code"],
            }
            for row in rows
        ]

    async def _request_usage_snapshot(
        self,
        connection: aiosqlite.Connection,
        start_iso: str | None,
        end_iso: str | None = None,
    ) -> dict[str, Any]:
        where = ""
        params: list[Any] = []
        if start_iso:
            where = "WHERE created_at >= ?"
            params.append(start_iso)
            if end_iso:
                where += " AND created_at <= ?"
                params.append(end_iso)
        prefix = f"SELECT COUNT(*) AS total FROM tts_requests {where}"
        requests_total = await self._count(connection, prefix, tuple(params))
        success_sql = f"SELECT COUNT(*) AS total FROM tts_requests {where} {'AND' if where else 'WHERE'} status = ?"
        success_params = (*params, STATUS_SUCCESS)
        success_total = await self._count(connection, success_sql, success_params)
        failed_sql = (
            f"SELECT COUNT(*) AS total FROM tts_requests {where} "
            f"{'AND' if where else 'WHERE'} status IN (?, ?)"
        )
        failed_total = await self._count(
            connection, failed_sql, (*params, STATUS_FAILED, STATUS_PARTIAL_FAILED)
        )
        cancelled_sql = (
            f"SELECT COUNT(*) AS total FROM tts_requests {where} {'AND' if where else 'WHERE'} status = ?"
        )
        cancelled_total = await self._count(connection, cancelled_sql, (*params, STATUS_CANCELLED))
        chars_sql = (
            f"""
            SELECT COALESCE(SUM(
                CASE WHEN status = ? THEN COALESCE(NULLIF(processed_characters, 0), char_count) ELSE 0 END
            ), 0) AS total
            FROM tts_requests {where}
            """
        )
        chars = await (
            await connection.execute(chars_sql, (STATUS_SUCCESS, *params))
        ).fetchone()
        credits_sql = (
            f"""
            SELECT COALESCE(SUM(CASE WHEN status = ? THEN estimated_credits ELSE 0 END), 0) AS total
            FROM tts_requests {where}
            """
        )
        credits = await (
            await connection.execute(credits_sql, (STATUS_SUCCESS, *params))
        ).fetchone()
        cost_sql = f"SELECT estimated_cost_usd FROM tts_requests {where} {'AND' if where else 'WHERE'} status = ?"
        cost_rows = await (await connection.execute(cost_sql, (*params, STATUS_SUCCESS))).fetchall()
        sources = {}
        for source_type in (
            SOURCE_TYPE_TEXT,
            SOURCE_TYPE_LONG_TEXT,
            SOURCE_TYPE_TXT,
            SOURCE_TYPE_DOCX,
            SOURCE_TYPE_PDF,
        ):
            source_sql = (
                f"SELECT COUNT(*) AS total FROM tts_requests {where} "
                f"{'AND' if where else 'WHERE'} source_type = ?"
            )
            sources[source_type] = await self._count(connection, source_sql, (*params, source_type))
        return {
            "requests_total": requests_total,
            "success_total": success_total,
            "failed_total": failed_total,
            "cancelled_total": cancelled_total,
            "characters_total": int(chars["total"]) if chars else 0,
            "estimated_credits": int(credits["total"]) if credits else 0,
            "estimated_cost_usd": _sum_decimals(row["estimated_cost_usd"] for row in cost_rows),
            "source_text": sources[SOURCE_TYPE_TEXT],
            "source_long_text": sources[SOURCE_TYPE_LONG_TEXT],
            "source_txt": sources[SOURCE_TYPE_TXT],
            "source_docx": sources[SOURCE_TYPE_DOCX],
            "source_pdf": sources[SOURCE_TYPE_PDF],
        }

    async def _popular_voice_name(self, connection: aiosqlite.Connection) -> str | None:
        cursor = await connection.execute(
            """
            SELECT COALESCE(r.voice_key, '') AS voice_key, COUNT(*) AS total
            FROM tts_requests AS r
            WHERE r.status = ? AND r.voice_key IS NOT NULL AND r.voice_key != ''
            GROUP BY r.voice_key
            ORDER BY total DESC, voice_key
            LIMIT 1
            """,
            (STATUS_SUCCESS,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        voice_key = row["voice_key"]
        voice = await (
            await connection.execute(
                "SELECT display_name FROM voices WHERE voice_key = ?",
                (voice_key,),
            )
        ).fetchone()
        if voice and voice["display_name"]:
            return str(voice["display_name"])
        return str(voice_key)

    async def _popular_speed(self, connection: aiosqlite.Connection) -> float | None:
        cursor = await connection.execute(
            """
            SELECT speech_speed, COUNT(*) AS total
            FROM tts_requests
            WHERE status = ? AND speech_speed IS NOT NULL
            GROUP BY speech_speed
            ORDER BY total DESC, speech_speed
            LIMIT 1
            """,
            (STATUS_SUCCESS,),
        )
        row = await cursor.fetchone()
        return None if row is None else float(row["speech_speed"])

    async def _count(
        self,
        connection: aiosqlite.Connection,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> int:
        row = await (await connection.execute(sql, params)).fetchone()
        return int(row["total"]) if row else 0

    async def get_schema_version(self) -> int | None:
        async with self._lock:
            connection = await self.connect()
            cursor = await connection.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            return int(row["version"]) if row else None

    async def _apply_migrations(self, connection: aiosqlite.Connection) -> None:
        current = await self._read_schema_version(connection)
        needs_version = current is None or current < CURRENT_SCHEMA_VERSION
        if needs_version:
            logger.info(
                format_log_event(
                    "database_migration_started",
                    from_version=current if current is not None else 0,
                    to_version=CURRENT_SCHEMA_VERSION,
                )
            )
        try:
            await self._ensure_v2_schema(connection)
            await self._ensure_v3_schema(connection)
            await self._ensure_v4_schema(connection)
            await self._ensure_v5_schema(connection)
            await self._ensure_v6_schema(connection)
            if needs_version:
                await connection.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (CURRENT_SCHEMA_VERSION, utc_now()),
                )
                logger.info(
                    format_log_event(
                        "database_migration_completed",
                        from_version=current if current is not None else 0,
                        to_version=CURRENT_SCHEMA_VERSION,
                    )
                )
        except Exception:
            logger.exception(
                format_log_event(
                    "database_migration_failed",
                    from_version=current if current is not None else 0,
                    to_version=CURRENT_SCHEMA_VERSION,
                )
            )
            raise

    async def _ensure_v2_schema(self, connection: aiosqlite.Connection) -> None:
        for column_name, column_type in USERS_NEW_COLUMNS:
            await self._add_column_if_missing(connection, "users", column_name, column_type)
        for column_name, column_type in TTS_NEW_COLUMNS:
            await self._add_column_if_missing(connection, "tts_requests", column_name, column_type)
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS voices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voice_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                voice_id TEXT NOT NULL,
                gender TEXT,
                credit_multiplier REAL NOT NULL DEFAULT 1.0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_voices_is_active ON voices(is_active)"
        )
        await connection.execute(
            """
            UPDATE users
            SET selected_voice_key = ?
            WHERE selected_voice_key IS NULL OR selected_voice_key = ''
            """,
            (DEFAULT_VOICE_KEY,),
        )

    async def _ensure_v3_schema(self, connection: aiosqlite.Connection) -> None:
        await self._add_column_if_missing(connection, "users", "speech_speed", "REAL NOT NULL DEFAULT 1.0")
        await self._add_column_if_missing(
            connection,
            "tts_requests",
            "speech_speed",
            "REAL NOT NULL DEFAULT 1.0",
        )
        await connection.execute(
            "UPDATE users SET speech_speed = ? WHERE speech_speed IS NULL",
            (DEFAULT_SPEECH_SPEED,),
        )
        await connection.execute(
            "UPDATE tts_requests SET speech_speed = ? WHERE speech_speed IS NULL",
            (DEFAULT_SPEECH_SPEED,),
        )

    async def _ensure_v4_schema(self, connection: aiosqlite.Connection) -> None:
        await self._add_column_if_missing(
            connection, "tts_requests", "request_type", "TEXT NOT NULL DEFAULT 'short'"
        )
        await self._add_column_if_missing(
            connection, "tts_requests", "chunk_count", "INTEGER NOT NULL DEFAULT 1"
        )
        await self._add_column_if_missing(
            connection, "tts_requests", "completed_chunks", "INTEGER NOT NULL DEFAULT 0"
        )
        await self._add_column_if_missing(
            connection,
            "tts_requests",
            "processed_characters",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await connection.execute(
            "UPDATE tts_requests SET request_type = 'short' WHERE request_type IS NULL OR request_type = ''"
        )

    async def _ensure_v5_schema(self, connection: aiosqlite.Connection) -> None:
        await self._add_column_if_missing(connection, "tts_requests", "page_count", "INTEGER")
        await self._add_column_if_missing(connection, "tts_requests", "pages_with_text", "INTEGER")

    async def _ensure_v6_schema(self, connection: aiosqlite.Connection) -> None:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER NOT NULL,
                usage_date TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                character_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, usage_date)
            )
            """
        )
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_usage_date ON daily_usage(usage_date)"
        )

    async def _add_column_if_missing(
        self,
        connection: aiosqlite.Connection,
        table: str,
        column_name: str,
        column_type: str,
    ) -> None:
        if await self._column_exists(connection, table, column_name):
            return
        await connection.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type}")

    async def _column_exists(
        self,
        connection: aiosqlite.Connection,
        table: str,
        column_name: str,
    ) -> bool:
        cursor = await connection.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(row[1] == column_name for row in rows)

    async def _read_schema_version(self, connection: aiosqlite.Connection) -> int | None:
        cursor = await connection.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return int(row["version"]) if row else None

    async def _fetch_user(
        self,
        connection: aiosqlite.Connection,
        telegram_user_id: int,
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        return await cursor.fetchone()

    async def _fetch_user_by_id(
        self,
        connection: aiosqlite.Connection,
        user_id: int | None,
    ) -> aiosqlite.Row | None:
        if user_id is None:
            return None
        cursor = await connection.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return await cursor.fetchone()

    async def _fetch_request_by_pk(
        self,
        connection: aiosqlite.Connection,
        request_pk: int | None,
    ) -> aiosqlite.Row | None:
        if request_pk is None:
            return None
        cursor = await connection.execute("SELECT * FROM tts_requests WHERE id = ?", (request_pk,))
        return await cursor.fetchone()


async def init_database(path: str | Path) -> Database:
    """Создать и инициализировать базу. Безопасно вызывается повторно."""
    database = Database(path)
    await database.init_database()
    return database


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _decimal_to_db(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _db_to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _sum_decimals(values: Iterable[Any]) -> Decimal | None:
    total: Decimal | None = None
    for raw in values:
        amount = _db_to_decimal(raw)
        if amount is None:
            continue
        total = amount if total is None else total + amount
    return total
