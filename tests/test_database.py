"""Тесты SQLite-слоя на временной базе без ElevenLabs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from database.db import (
    CURRENT_SCHEMA_VERSION,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_SUCCESS,
    SpeechSpeedError,
    init_database,
)
from handlers.start import cmd_start
from handlers.text import active_jobs, handle_text_message
from services.tts_service import TTSQuotaError
from test_text_handler import DummyChatAction, FakeMessage, FakeTTSService


FORBIDDEN_COLUMN_MARKERS = (
    "source_text",
    "user_text",
    "original_text",
    "full_text",
    "mp3",
    "audio_blob",
    "audio_bytes",
    "api_key",
    "token",
)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "voxtext.db"
        self.database = await init_database(self.db_path)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    async def test_creates_required_tables(self) -> None:
        tables = await self._table_names()
        self.assertIn("users", tables)
        self.assertIn("tts_requests", tables)
        self.assertIn("voices", tables)
        self.assertIn("schema_version", tables)
        self.assertEqual(await self.database.get_schema_version(), CURRENT_SCHEMA_VERSION)
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("PRAGMA table_info(tts_requests)")
            columns = {row[1] for row in await cursor.fetchall()}
        self.assertTrue(
            {
                "request_type",
                "chunk_count",
                "completed_chunks",
                "processed_characters",
                "page_count",
                "pages_with_text",
            }.issubset(columns)
        )

    async def test_reinit_does_not_lose_data(self) -> None:
        await self.database.upsert_user(101, username="elena", first_name="Елена")
        await self.database.close()
        again = await init_database(self.db_path)
        user = await again.get_user_by_telegram_id(101)
        await again.close()
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "elena")
        self.assertEqual(user["first_name"], "Елена")

    async def test_create_user_and_repeated_start_has_no_duplicate(self) -> None:
        first = await self.database.upsert_user(202, username="one", first_name="Анна")
        second = await self.database.upsert_user(202, username="two", first_name="Мария")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["username"], "two")
        self.assertEqual(second["first_name"], "Мария")
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM users WHERE telegram_user_id = 202")
            count = (await cursor.fetchone())[0]
        self.assertEqual(count, 1)

    async def test_updates_last_seen_and_keeps_voice_settings(self) -> None:
        created = await self.database.upsert_user(
            303,
            username="voice",
            default_voice_id="defaultVoice1234",
        )
        await self.database.update_user_voice(303, "female", "customVoice5678")
        previous_seen = created["last_seen_at"]
        await self.database.update_user_last_seen(303)
        updated = await self.database.get_user_by_telegram_id(303)
        self.assertEqual(updated["selected_voice_key"], "female")
        self.assertEqual(updated["selected_voice_id"], "customVoice5678")
        self.assertGreaterEqual(updated["last_seen_at"], previous_seen)

    async def test_speed_saved_and_out_of_range_rejected(self) -> None:
        await self.database.upsert_user(404, username="speed")
        await self.database.update_user_speed(404, 1.15)
        user = await self.database.get_user_by_telegram_id(404)
        self.assertEqual(user["speech_speed"], 1.15)
        with self.assertRaises(SpeechSpeedError):
            await self.database.update_user_speed(404, 0.69)
        with self.assertRaises(SpeechSpeedError):
            await self.database.update_user_speed(404, 1.1)
        unchanged = await self.database.get_user_by_telegram_id(404)
        self.assertEqual(unchanged["speech_speed"], 1.15)

    async def test_missing_username_is_null(self) -> None:
        await self.database.upsert_user(505, username=None)
        user = await self.database.get_user_by_telegram_id(505)
        self.assertIsNone(user["username"])

    async def test_tts_request_status_flow(self) -> None:
        await self.database.upsert_user(606, username="tts")
        created = await self.database.create_tts_request(
            request_id="req-1",
            telegram_user_id=606,
            char_count=12,
            model_id="eleven_multilingual_v2",
            voice_id="voice1234",
            speech_speed=1.0,
        )
        self.assertEqual(created["status"], STATUS_PROCESSING)
        await self.database.mark_tts_request_success(
            "req-1",
            audio_size_bytes=2048,
            duration_ms=1500,
            estimated_credits=12,
        )
        await self.database.create_tts_request(
            request_id="req-2",
            telegram_user_id=606,
            char_count=8,
            voice_id="voice1234",
        )
        await self.database.mark_tts_request_failed("req-2", "payment_required")
        self.assertEqual(await self.database.get_user_request_count(606), 2)
        self.assertEqual(await self.database.get_user_character_total(606), 20)

        stats = await self.database.get_general_statistics()
        self.assertEqual(stats["users_total"], 1)
        self.assertEqual(stats["requests_total"], 2)
        self.assertEqual(stats["success_total"], 1)
        self.assertEqual(stats["failed_total"], 1)
        self.assertEqual(stats["characters_total"], 20)

    async def test_schema_has_no_source_text_or_mp3_columns(self) -> None:
        columns = await self._all_columns()
        joined = " ".join(columns).lower()
        for marker in FORBIDDEN_COLUMN_MARKERS:
            self.assertNotIn(marker, joined)
        self.assertNotIn("text", columns)

    async def test_foreign_key_rejects_unknown_user(self) -> None:
        with self.assertRaises(Exception):
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute("PRAGMA foreign_keys = ON")
                await conn.execute(
                    """
                    INSERT INTO tts_requests (
                        request_id, user_id, source_type, char_count, status,
                        speech_speed, created_at
                    ) VALUES (?, ?, 'text', 1, 'processing', 1.0, ?)
                    """,
                    ("bad-req", 9999, "2026-01-01T00:00:00+00:00"),
                )
                await conn.commit()

    async def test_russian_names_are_stored(self) -> None:
        await self.database.upsert_user(707, username="елена", first_name="Елена", last_name="Петрова")
        user = await self.database.get_user_by_telegram_id(707)
        self.assertEqual(user["first_name"], "Елена")
        self.assertEqual(user["last_name"], "Петрова")
        self.assertEqual(user["username"], "елена")

    async def test_start_handler_upserts_without_duplicate(self) -> None:
        message = FakeMessage("/start", user_id=808)
        await cmd_start(message, database=self.database, default_voice_id="defaultVoiceABCD")
        await cmd_start(message, database=self.database, default_voice_id="defaultVoiceABCD")
        user = await self.database.get_user_by_telegram_id(808)
        self.assertIsNotNone(user)
        self.assertEqual(user["selected_voice_id"], "defaultVoiceABCD")
        self.assertEqual(user["selected_voice_key"], "default")
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM users")
            self.assertEqual((await cursor.fetchone())[0], 1)

    async def test_existing_voice_is_not_overwritten_on_start(self) -> None:
        await self.database.upsert_user(909, username="keep", default_voice_id="firstVoice")
        await self.database.update_user_voice(909, "female", "keptVoice")
        message = FakeMessage("/start", user_id=909)
        await cmd_start(message, database=self.database, default_voice_id="otherVoice")
        user = await self.database.get_user_by_telegram_id(909)
        self.assertEqual(user["selected_voice_key"], "female")
        self.assertEqual(user["selected_voice_id"], "keptVoice")

    async def test_text_handler_records_success_and_failure(self) -> None:
        for user_id in list(active_jobs._user_ids):
            active_jobs.release(user_id)

        with tempfile.TemporaryDirectory() as audio_tmp:
            message = FakeMessage("Привет из теста", user_id=1212)
            tts = FakeTTSService()
            with (
                patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
                patch(
                    "handlers.text.create_temp_mp3_path",
                    return_value=Path(audio_tmp) / "tts_ok.mp3",
                ),
            ):
                await handle_text_message(
                    message,
                    tts,
                    database=self.database,
                    default_voice_id="defaultVoiceABCD",
                )

        fail_message = FakeMessage("Второй текст", user_id=1212)
        failing_tts = FakeTTSService(error=TTSQuotaError())
        with (
            patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            tempfile.TemporaryDirectory() as audio_tmp,
            patch(
                "handlers.text.create_temp_mp3_path",
                return_value=Path(audio_tmp) / "tts_fail.mp3",
            ),
        ):
            await handle_text_message(
                fail_message,
                failing_tts,
                database=self.database,
                default_voice_id="defaultVoiceABCD",
            )

        self.assertEqual(await self.database.get_user_request_count(1212), 2)
        stats = await self.database.get_general_statistics()
        self.assertEqual(stats["success_total"], 1)
        self.assertEqual(stats["failed_total"], 1)
        user = await self.database.get_user_by_telegram_id(1212)
        self.assertEqual(user["first_name"], "Елена")

    async def test_new_user_default_speed_is_one(self) -> None:
        user = await self.database.upsert_user(111, username="defaults", default_voice_id="abc")
        self.assertEqual(user["speech_speed"], 1.0)
        self.assertEqual(user["selected_voice_key"], "default")

    async def test_selected_voice_survives_reinit(self) -> None:
        await self.database.upsert_user(1313, username="persist")
        await self.database.update_user_voice(1313, "male")
        await self.database.close()
        again = await init_database(self.db_path)
        user = await again.get_user_by_telegram_id(1313)
        await again.close()
        self.assertEqual(user["selected_voice_key"], "male")

    async def test_usage_statistics_are_isolated_by_user(self) -> None:
        from decimal import Decimal

        await self.database.upsert_user(1401, username="alice")
        await self.database.upsert_user(1402, username="bob")
        await self.database.create_tts_request("alice-ok", 1401, 10, voice_key="default")
        await self.database.mark_tts_request_success(
            "alice-ok",
            audio_size_bytes=100,
            duration_ms=10,
            estimated_credits=10,
            estimated_cost_usd=Decimal("0.010000"),
        )
        await self.database.create_tts_request("alice-fail", 1401, 4, voice_key="default")
        await self.database.mark_tts_request_failed("alice-fail", "unknown")
        await self.database.create_tts_request("bob-ok", 1402, 99, voice_key="female")
        await self.database.mark_tts_request_success(
            "bob-ok",
            audio_size_bytes=200,
            duration_ms=20,
            estimated_credits=99,
        )

        alice = await self.database.get_user_usage_statistics(1401)
        bob = await self.database.get_user_usage_statistics(1402)
        self.assertEqual(alice["success_count"], 1)
        self.assertEqual(alice["failed_count"], 1)
        self.assertEqual(alice["success_char_count"], 10)
        self.assertEqual(alice["estimated_credits"], 10)
        self.assertEqual(alice["estimated_cost_usd"], Decimal("0.010000"))
        self.assertEqual(bob["success_count"], 1)
        self.assertEqual(bob["failed_count"], 0)
        self.assertEqual(bob["success_char_count"], 99)
        self.assertEqual(bob["estimated_credits"], 99)
        self.assertIsNone(bob["estimated_cost_usd"])

    async def test_estimated_credits_and_cost_on_success(self) -> None:
        from decimal import Decimal

        from services.usage_estimator import estimate_cost_usd, estimate_credits
        from services.voice_catalog import VoiceOption

        await self.database.sync_voices(
            [
                VoiceOption(
                    key="default",
                    display_name="Основной голос",
                    voice_id="voice-default",
                    gender=None,
                    credit_multiplier=1.5,
                )
            ]
        )
        stored = await self.database.get_voice_by_key("default")
        await self.database.upsert_user(1501, username="credits")
        created = await self.database.create_tts_request(
            "req-credits",
            1501,
            10,
            model_id="eleven_multilingual_v2",
            voice_key="default",
            voice_id="voice-default",
        )
        self.assertEqual(created["status"], STATUS_PROCESSING)
        credits = estimate_credits(10, stored["credit_multiplier"])
        cost = estimate_cost_usd(credits, Decimal("5"), 100)
        await self.database.mark_tts_request_success(
            "req-credits",
            audio_size_bytes=50,
            duration_ms=80,
            estimated_credits=credits,
            estimated_cost_usd=cost,
        )
        stats = await self.database.get_user_usage_statistics(1501)
        self.assertEqual(credits, 15)
        self.assertEqual(stats["estimated_credits"], 15)
        self.assertEqual(stats["success_char_count"], 10)
        self.assertEqual(stats["estimated_cost_usd"], Decimal("0.750000"))

        failed = await self.database.create_tts_request("req-fail-credits", 1501, 40, voice_key="default")
        await self.database.mark_tts_request_failed("req-fail-credits", "unknown", duration_ms=5)
        after_fail = await self.database.get_user_usage_statistics(1501)
        self.assertEqual(after_fail["estimated_credits"], 15)
        self.assertEqual(after_fail["success_char_count"], 10)
        self.assertEqual(failed["status"], STATUS_PROCESSING)

        general = await self.database.get_general_usage_statistics()
        self.assertEqual(general["users_total"], 1)
        self.assertEqual(general["success_total"], 1)
        self.assertEqual(general["failed_total"], 1)
        self.assertEqual(general["success_char_count"], 10)
        self.assertEqual(general["estimated_credits"], 15)
        self.assertEqual(general["estimated_cost_usd"], Decimal("0.750000"))
        self.assertEqual(general["usage_by_voice"][0]["voice_key"], "default")

    async def test_cost_is_null_without_plan(self) -> None:
        await self.database.upsert_user(1601, username="noplan")
        await self.database.create_tts_request("req-noplan", 1601, 8, voice_key="default")
        await self.database.mark_tts_request_success(
            "req-noplan",
            audio_size_bytes=10,
            duration_ms=10,
            estimated_credits=8,
            estimated_cost_usd=None,
        )
        stats = await self.database.get_user_usage_statistics(1601)
        self.assertIsNone(stats["estimated_cost_usd"])
        general = await self.database.get_general_usage_statistics()
        self.assertIsNone(general["estimated_cost_usd"])

    async def test_sync_voices_updates_without_deleting_history(self) -> None:
        from services.voice_catalog import VoiceOption

        await self.database.sync_voices(
            [
                VoiceOption("default", "Основной", "id-default", None),
                VoiceOption("female", "Женский", "id-female", "female"),
            ]
        )
        first = await self.database.get_active_voices()
        self.assertEqual([row["voice_key"] for row in first], ["default", "female"])
        await self.database.sync_voices(
            [
                VoiceOption("default", "Основной голос", "id-default-2", None),
            ]
        )
        active = await self.database.get_active_voices()
        self.assertEqual([row["voice_key"] for row in active], ["default"])
        self.assertEqual(active[0]["display_name"], "Основной голос")
        stored_female = await self.database.get_voice_by_key("female")
        self.assertIsNotNone(stored_female)
        self.assertEqual(stored_female["is_active"], 0)

    async def _table_names(self) -> set[str]:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def _all_columns(self) -> list[str]:
        names: list[str] = []
        async with aiosqlite.connect(self.db_path) as conn:
            for table in await self._table_names():
                cursor = await conn.execute(f"PRAGMA table_info({table})")
                names.extend(row[1] for row in await cursor.fetchall())
        return names


V1_USERS_SQL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    language_code TEXT,
    selected_voice_id TEXT,
    speech_speed REAL NOT NULL DEFAULT 1.0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
)
"""
V1_TTS_SQL = """
CREATE TABLE tts_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'text',
    char_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    model_id TEXT,
    voice_id TEXT,
    speech_speed REAL NOT NULL DEFAULT 1.0,
    audio_size_bytes INTEGER,
    duration_ms INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
)
"""


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "legacy.db"

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_migrates_v1_data_without_loss_and_is_idempotent(self) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(V1_USERS_SQL)
            await conn.execute(V1_TTS_SQL)
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
                    telegram_user_id, username, first_name, last_name,
                    language_code, selected_voice_id, speech_speed,
                    is_active, created_at, last_seen_at
                ) VALUES (7701, 'legacy', 'Анна', 'Иванова', 'ru', 'oldVoice', 1.0, 1, ?, ?)
                """,
                ("2026-01-15T10:00:00+00:00", "2026-01-15T10:00:00+00:00"),
            )
            await conn.execute(
                """
                INSERT INTO tts_requests (
                    request_id, user_id, source_type, char_count, status,
                    model_id, voice_id, speech_speed, created_at
                ) VALUES ('legacy-req', 1, 'text', 33, 'success', 'eleven_multilingual_v2',
                          'oldVoice', 1.0, '2026-01-15T10:01:00+00:00')
                """
            )
            await conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (1, ?)",
                ("2026-01-15T10:00:00+00:00",),
            )
            await conn.commit()

        database = await init_database(self.db_path)
        user = await database.get_user_by_telegram_id(7701)
        self.assertEqual(user["username"], "legacy")
        self.assertEqual(user["first_name"], "Анна")
        self.assertEqual(user["selected_voice_id"], "oldVoice")
        self.assertEqual(user["selected_voice_key"], "default")
        self.assertEqual(await database.get_schema_version(), CURRENT_SCHEMA_VERSION)
        self.assertIn("voices", await self._table_names())
        self.assertIn("daily_usage", await self._table_names())

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            request = await (
                await conn.execute("SELECT * FROM tts_requests WHERE request_id = 'legacy-req'")
            ).fetchone()
        self.assertEqual(request["char_count"], 33)
        self.assertEqual(request["status"], "success")
        self.assertIsNone(request["estimated_credits"])

        await database.init_database()
        again = await database.get_user_by_telegram_id(7701)
        await database.close()
        self.assertEqual(again["username"], "legacy")
        self.assertEqual(again["selected_voice_key"], "default")
        self.assertEqual(await self._count_users(), 1)

    async def _table_names(self) -> set[str]:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def _count_users(self) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM users")
            return int((await cursor.fetchone())[0])


class ConfigDatabasePathTests(unittest.TestCase):
    def test_default_path_is_under_project_root(self) -> None:
        from config import PROJECT_ROOT, resolve_database_path

        path = resolve_database_path(None)
        self.assertTrue(str(path).startswith(str(PROJECT_ROOT)))
        self.assertTrue(str(path).endswith("database\\voxtext.db") or str(path).endswith("database/voxtext.db"))

    def test_relative_path_does_not_depend_on_cwd(self) -> None:
        from config import PROJECT_ROOT, resolve_database_path

        path = resolve_database_path("database/custom.db")
        self.assertEqual(path, (PROJECT_ROOT / "database" / "custom.db").resolve())


if __name__ == "__main__":
    unittest.main()
