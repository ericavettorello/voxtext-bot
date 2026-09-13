"""Тесты дневных лимитов без Telegram API и без ElevenLabs."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from config import Config, ConfigError
from database.db import SOURCE_TYPE_DOCX, SOURCE_TYPE_PDF, SOURCE_TYPE_TXT, init_database
from handlers.settings import cmd_limit
from handlers.text import handle_text_message
from services.daily_quota import (
    DailyQuotaSettings,
    REASON_BOTH,
    REASON_CHARACTERS,
    REASON_REQUESTS,
    format_limit_status,
    format_quota_denied,
    get_daily_quota_status,
    try_reserve_daily_quota,
    usage_date_today,
)
from services.long_tts import GenerationSnapshot
from services.long_tts_job import LongTTSRunOptions, run_confirmed_long_tts
from services.tts_service import TTSError
from test_text_handler import DummyChatAction, FakeMessage, FakeTTSService, FakeUser


SETTINGS = DailyQuotaSettings(request_limit=5, character_limit=20000, timezone_name="UTC")
ADMIN_ID = 7001
USER_ID = 8001


class FailAfterFirstTTS(FakeTTSService):
    def __init__(self) -> None:
        super().__init__()
        self.calls_count = 0

    def generate_speech(self, text, output_path, voice_id=None, speech_speed=None, previous_text=None, next_text=None):
        self.calls_count += 1
        if self.calls_count > 1:
            raise TTSError("fail after first chunk")
        return super().generate_speech(text, output_path, voice_id, speech_speed, previous_text, next_text)


class DailyQuotaConfigTests(unittest.TestCase):
    def test_defaults_when_variables_missing(self) -> None:
        def fake_getenv(name: str, default: str = "") -> str:
            values = {
                "TELEGRAM_BOT_TOKEN": "demo-token",
                "ELEVENLABS_API_KEY": "demo-key",
                "ELEVENLABS_VOICE_ID": "demo-voice",
            }
            return values.get(name, default)

        with patch("config.os.getenv", side_effect=fake_getenv):
            config = Config()
        self.assertEqual(config.daily_request_limit, 5)
        self.assertEqual(config.daily_character_limit, 20000)
        self.assertEqual(config.daily_limit_timezone, "UTC")

    def test_invalid_limit_stops_startup(self) -> None:
        def fake_getenv(name: str, default: str = "") -> str:
            values = {
                "TELEGRAM_BOT_TOKEN": "demo-token",
                "ELEVENLABS_API_KEY": "demo-key",
                "ELEVENLABS_VOICE_ID": "demo-voice",
                "DAILY_REQUEST_LIMIT": "0",
            }
            return values.get(name, default)

        with patch("config.os.getenv", side_effect=fake_getenv):
            with self.assertRaises(ConfigError) as ctx:
                Config()
        self.assertIn("DAILY_REQUEST_LIMIT", str(ctx.exception))
        self.assertNotIn("demo-token", str(ctx.exception))
        self.assertNotIn("demo-key", str(ctx.exception))


class DailyQuotaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = await init_database(Path(self._tmp.name) / "quota.db")
        await self.database.upsert_user(USER_ID, username="quota-user")

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    async def test_first_and_fifth_allowed_sixth_blocked(self) -> None:
        for index in range(5):
            result = await try_reserve_daily_quota(
                self.database, USER_ID, 10, SETTINGS, source_type="text"
            )
            self.assertTrue(result.allowed, msg=f"request {index + 1}")
            self.assertEqual(result.used_requests, index + 1)
        sixth = await try_reserve_daily_quota(self.database, USER_ID, 10, SETTINGS, source_type="text")
        self.assertFalse(sixth.allowed)
        self.assertEqual(sixth.reason, REASON_REQUESTS)
        self.assertIn("Дневной лимит озвучиваний исчерпан", format_quota_denied(sixth, 10))
        status = await get_daily_quota_status(self.database, USER_ID, SETTINGS)
        self.assertEqual(status.used_requests, 5)

    async def test_exact_remaining_characters_allowed_and_plus_one_blocked(self) -> None:
        first = await try_reserve_daily_quota(self.database, USER_ID, 19999, SETTINGS)
        self.assertTrue(first.allowed)
        exact = await try_reserve_daily_quota(self.database, USER_ID, 1, SETTINGS)
        self.assertTrue(exact.allowed)
        blocked = await try_reserve_daily_quota(self.database, USER_ID, 1, SETTINGS)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, REASON_CHARACTERS)
        self.assertIn("Недостаточно доступных символов", format_quota_denied(blocked, 1))

    async def test_long_text_counts_as_one_request(self) -> None:
        result = await try_reserve_daily_quota(
            self.database, USER_ID, 9000, SETTINGS, source_type="long_text"
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.used_requests, 1)
        self.assertEqual(result.used_characters, 9000)

    async def test_txt_docx_pdf_each_count_as_one_request(self) -> None:
        for source in (SOURCE_TYPE_TXT, SOURCE_TYPE_DOCX, SOURCE_TYPE_PDF):
            result = await try_reserve_daily_quota(
                self.database, USER_ID, 20, SETTINGS, source_type=source
            )
            self.assertTrue(result.allowed)
        status = await get_daily_quota_status(self.database, USER_ID, SETTINGS)
        self.assertEqual(status.used_requests, 3)
        self.assertEqual(status.used_characters, 60)

    async def test_admin_is_not_limited(self) -> None:
        for _ in range(6):
            result = await try_reserve_daily_quota(
                self.database, ADMIN_ID, 30000, SETTINGS, admin_ids={ADMIN_ID}
            )
            self.assertTrue(result.allowed)
            self.assertTrue(result.unlimited)
            self.assertFalse(result.reserved)
        status = await get_daily_quota_status(self.database, ADMIN_ID, SETTINGS, admin_ids={ADMIN_ID})
        self.assertTrue(status.unlimited)
        self.assertIn("Ограничения отсутствуют", format_limit_status(status))
        stored = await get_daily_quota_status(self.database, ADMIN_ID, SETTINGS)
        self.assertEqual(stored.used_requests, 0)

    async def test_new_utc_day_starts_fresh_period(self) -> None:
        late = datetime(2026, 9, 13, 23, 30, tzinfo=timezone.utc)
        early = datetime(2026, 9, 14, 0, 1, tzinfo=timezone.utc)
        first = await try_reserve_daily_quota(
            self.database, USER_ID, 100, SETTINGS, now=late
        )
        self.assertEqual(first.usage_date, "2026-09-13")
        second = await try_reserve_daily_quota(
            self.database, USER_ID, 100, SETTINGS, now=early
        )
        self.assertTrue(second.allowed)
        self.assertEqual(second.usage_date, "2026-09-14")
        self.assertEqual(second.used_requests, 1)
        self.assertEqual(usage_date_today("UTC", early), "2026-09-14")

    async def test_parallel_requests_cannot_bypass_limit(self) -> None:
        tight = DailyQuotaSettings(request_limit=1, character_limit=20000, timezone_name="UTC")
        first, second = await asyncio.gather(
            try_reserve_daily_quota(self.database, USER_ID, 5, tight),
            try_reserve_daily_quota(self.database, USER_ID, 5, tight),
        )
        allowed = [item.allowed for item in (first, second)]
        self.assertEqual(sorted(allowed), [False, True])
        status = await get_daily_quota_status(self.database, USER_ID, tight)
        self.assertEqual(status.used_requests, 1)

    async def test_both_limits_shown_together(self) -> None:
        tight = DailyQuotaSettings(request_limit=1, character_limit=10, timezone_name="UTC")
        await try_reserve_daily_quota(self.database, USER_ID, 10, tight)
        denied = await try_reserve_daily_quota(self.database, USER_ID, 11, tight)
        self.assertEqual(denied.reason, REASON_BOTH)
        text = format_quota_denied(denied, 11)
        self.assertIn("Дневной лимит озвучиваний исчерпан", text)
        self.assertIn("Недостаточно доступных символов", text)

    async def test_admin_overview_includes_daily_quota(self) -> None:
        await try_reserve_daily_quota(self.database, USER_ID, 20000, SETTINGS)
        stats = await self.database.get_admin_overview_statistics(
            usage_date=usage_date_today("UTC"),
            request_limit=5,
            character_limit=20000,
        )
        self.assertEqual(stats["daily_quota_requests_today"], 1)
        self.assertEqual(stats["daily_quota_characters_today"], 20000)
        self.assertEqual(stats["daily_quota_users_at_character_limit"], 1)
        self.assertEqual(stats["daily_quota_users_at_request_limit"], 0)


class DailyQuotaHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = await init_database(Path(self._tmp.name) / "quota.db")
        await self.database.upsert_user(USER_ID, username="handler")

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    async def test_invalid_text_does_not_consume_quota(self) -> None:
        message = FakeMessage("   ", user_id=USER_ID)
        message.from_user = FakeUser(USER_ID)
        await handle_text_message(
            message,
            FakeTTSService(),
            database=self.database,
            quota_settings=SETTINGS,
        )
        status = await get_daily_quota_status(self.database, USER_ID, SETTINGS)
        self.assertEqual(status.used_requests, 0)
        self.assertEqual(status.used_characters, 0)

    async def test_quota_released_if_elevenlabs_not_started(self) -> None:
        message = FakeMessage("Привет", user_id=USER_ID)
        message.from_user = FakeUser(USER_ID)
        with (
            patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            patch("handlers.text.create_temp_mp3_path", side_effect=RuntimeError("temp fail")),
        ):
            await handle_text_message(
                message,
                FakeTTSService(),
                database=self.database,
                quota_settings=SETTINGS,
            )
        status = await get_daily_quota_status(self.database, USER_ID, SETTINGS)
        self.assertEqual(status.used_requests, 0)

    async def test_quota_kept_after_first_audio_chunk(self) -> None:
        snapshot = GenerationSnapshot(
            telegram_user_id=USER_ID,
            voice_id="voice",
            voice_key="default",
            voice_name="Основной",
            speech_speed=1.0,
            model_id="model",
            char_count=12,
            chunks=["one", "two"],
            credit_multiplier=1.0,
        )
        message = FakeMessage("status", user_id=USER_ID)
        with (
            patch("services.long_tts_job.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            tempfile.TemporaryDirectory() as tmp,
            patch("services.long_tts_job.TEMP_DIR", Path(tmp)),
        ):
            ok = await run_confirmed_long_tts(
                bot=message.bot,
                chat_id=USER_ID,
                status_message=message,
                tts_service=FailAfterFirstTTS(),
                snapshot=snapshot,
                database=self.database,
                quota_settings=SETTINGS,
                options=LongTTSRunOptions(source_type="txt"),
            )
        self.assertFalse(ok)
        status = await get_daily_quota_status(self.database, USER_ID, SETTINGS)
        self.assertEqual(status.used_requests, 1)
        self.assertEqual(status.used_characters, 12)

    async def test_limit_command_shows_used_and_remaining(self) -> None:
        await try_reserve_daily_quota(self.database, USER_ID, 7550, SETTINGS)
        await try_reserve_daily_quota(self.database, USER_ID, 10, SETTINGS)
        message = FakeMessage("/limit", user_id=USER_ID)
        message.from_user = FakeUser(USER_ID)
        await cmd_limit(message, database=self.database, quota_settings=SETTINGS)
        text = message.answers[-1]
        self.assertIn("Ваш лимит на сегодня", text)
        self.assertIn("2 из 5", text)
        self.assertIn("7 560 из 20 000", text)
        self.assertIn("озвучиваний: 3", text)
        self.assertIn("12 440", text)
        self.assertIn("00:00 UTC", text)

    async def test_limit_command_for_admin(self) -> None:
        message = FakeMessage("/limit", user_id=ADMIN_ID)
        message.from_user = FakeUser(ADMIN_ID)
        await cmd_limit(message, database=self.database, quota_settings=SETTINGS, admin_ids={ADMIN_ID})
        self.assertIn("Ограничения отсутствуют", message.answers[-1])

    async def test_successful_short_tts_appends_remaining(self) -> None:
        message = FakeMessage("Hello", user_id=USER_ID)
        message.from_user = FakeUser(USER_ID)
        tts = FakeTTSService()
        with (
            patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            tempfile.TemporaryDirectory() as tmp,
            patch("handlers.text.create_temp_mp3_path", return_value=Path(tmp) / "ok.mp3"),
        ):
            await handle_text_message(
                message,
                tts,
                database=self.database,
                quota_settings=SETTINGS,
            )
        self.assertTrue(tts.calls)
        caption = message.audio_calls[-1]["caption"]
        self.assertIn("озвучиваний: 4 из 5", caption)
        status = await get_daily_quota_status(self.database, USER_ID, SETTINGS)
        self.assertEqual(status.used_requests, 1)
        self.assertEqual(status.used_characters, 5)
