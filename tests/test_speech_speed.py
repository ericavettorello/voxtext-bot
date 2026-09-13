"""Тесты каталога скоростей и сохранения настройки без ElevenLabs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from database.db import CURRENT_SCHEMA_VERSION, SpeechSpeedError, init_database
from services.speech_speed import (
    DEFAULT_SPEECH_SPEED,
    get_speed_by_key,
    get_speed_by_value,
    is_allowed_speed,
    resolve_speech_speed,
)


class SpeechSpeedCatalogTests(unittest.TestCase):
    def test_whitelist_has_three_values(self) -> None:
        self.assertEqual(resolve_speech_speed(None), 1.0)
        self.assertEqual(resolve_speech_speed(0.85), 0.85)
        self.assertEqual(resolve_speech_speed(1.15), 1.15)
        self.assertEqual(resolve_speech_speed(1.1), 1.0)
        self.assertFalse(is_allowed_speed(1.1))
        self.assertIsNone(get_speed_by_key("turbo"))
        self.assertEqual(get_speed_by_key("slow").value, 0.85)
        self.assertEqual(get_speed_by_value(1.0).title, "Обычная")


class SpeechSpeedDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "voxtext.db"
        self.database = await init_database(self.db_path)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    async def test_new_user_gets_default_speed(self) -> None:
        user = await self.database.upsert_user(3101, username="new")
        self.assertEqual(user["speech_speed"], DEFAULT_SPEECH_SPEED)
        self.assertEqual(await self.database.get_user_speech_speed(3101), 1.0)

    async def test_select_slow_and_fast(self) -> None:
        await self.database.upsert_user(3102)
        await self.database.update_user_speed(3102, 0.85)
        self.assertEqual(await self.database.get_user_speech_speed(3102), 0.85)
        await self.database.update_user_speed(3102, 1.15)
        self.assertEqual(await self.database.get_user_speech_speed(3102), 1.15)

    async def test_speed_survives_reinit(self) -> None:
        await self.database.upsert_user(3103)
        await self.database.update_user_speed(3103, 0.85)
        await self.database.close()
        again = await init_database(self.db_path)
        self.assertEqual(await again.get_user_speech_speed(3103), 0.85)
        await again.close()

    async def test_invalid_speed_is_not_saved(self) -> None:
        await self.database.upsert_user(3104)
        with self.assertRaises(SpeechSpeedError):
            await self.database.update_user_speed(3104, 2.0)
        self.assertEqual(await self.database.get_user_speech_speed(3104), 1.0)

    async def test_tts_history_keeps_used_speed_snapshot(self) -> None:
        await self.database.upsert_user(3105)
        await self.database.update_user_speed(3105, 0.85)
        await self.database.create_tts_request(
            request_id="speed-snap",
            telegram_user_id=3105,
            char_count=5,
            speech_speed=0.85,
        )
        await self.database.update_user_speed(3105, 1.15)
        async with aiosqlite.connect(self.db_path) as conn:
            row = await (
                await conn.execute(
                    "SELECT speech_speed FROM tts_requests WHERE request_id = ?",
                    ("speed-snap",),
                )
            ).fetchone()
        self.assertEqual(row[0], 0.85)
        self.assertEqual(await self.database.get_user_speech_speed(3105), 1.15)

    async def test_missing_user_speed_defaults(self) -> None:
        self.assertEqual(await self.database.get_user_speech_speed(999999), 1.0)
        self.assertEqual(await self.database.get_schema_version(), CURRENT_SCHEMA_VERSION)


class LegacyUserSpeedMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "legacy.db"

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_existing_user_without_speed_gets_default(self) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL UNIQUE,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    language_code TEXT,
                    selected_voice_id TEXT,
                    selected_voice_key TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE tts_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'text',
                    char_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE schema_version (
                    version INTEGER NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO users (
                    telegram_user_id, username, selected_voice_key,
                    is_active, created_at, last_seen_at
                ) VALUES (7702, 'legacy-speed', 'default', 1, ?, ?)
                """,
                ("2026-01-15T10:00:00+00:00", "2026-01-15T10:00:00+00:00"),
            )
            await conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (2, ?)",
                ("2026-01-15T10:00:00+00:00",),
            )
            await conn.commit()

        database = await init_database(self.db_path)
        user = await database.get_user_by_telegram_id(7702)
        self.assertEqual(user["speech_speed"], 1.0)
        self.assertEqual(await database.get_schema_version(), CURRENT_SCHEMA_VERSION)
        await database.init_database()
        again = await database.get_user_by_telegram_id(7702)
        await database.close()
        self.assertEqual(again["username"], "legacy-speed")
        self.assertEqual(again["speech_speed"], 1.0)


if __name__ == "__main__":
    unittest.main()
