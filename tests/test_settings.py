"""Тесты выбора голоса и статистики без Telegram API и ElevenLabs."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from database.db import init_database
from handlers.settings import (
    SPEED_INVALID,
    SPEED_SAVE_FAILED,
    STATS_UNAVAILABLE,
    VOICE_SAVE_FAILED,
    VOICE_UNAVAILABLE,
    build_speed_keyboard,
    build_voice_keyboard,
    choose_voice,
    configure_speed,
    format_user_statistics,
    on_speed_selected,
    on_voice_selected,
    show_my_statistics,
)
from handlers.text import active_jobs, handle_text_message, resolve_user_voice
from services.voice_catalog import VoiceCatalog, VoiceOption, build_voice_catalog
from test_text_handler import DummyChatAction, FakeMessage, FakeTTSService, FakeUser


def _catalog() -> VoiceCatalog:
    return build_voice_catalog(
        SimpleNamespace(
            elevenlabs_voice_id="voice-default",
            elevenlabs_default_voice_name="Основной голос",
            elevenlabs_female_voice_id="voice-female",
            elevenlabs_female_voice_name="Женский голос",
            elevenlabs_male_voice_id="voice-male",
            elevenlabs_male_voice_name="Мужской голос",
        )
    )


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage, user_id: int = 100) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message
        self.answered = False
        self.edited_markups: list[object] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answered = True


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = await init_database(Path(self._tmp.name) / "voxtext.db")
        self.catalog = _catalog()
        await self.database.sync_voices(self.catalog.voices)
        for user_id in list(active_jobs._user_ids):
            active_jobs.release(user_id)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    async def test_voice_keyboard_marks_selected_and_uses_safe_keys(self) -> None:
        markup = build_voice_keyboard(self.catalog.voices, "female")
        labels = [row[0].text for row in markup.inline_keyboard]
        callbacks = [row[0].callback_data for row in markup.inline_keyboard]
        self.assertEqual(labels, ["Основной голос", "✅ Женский голос", "Мужской голос"])
        self.assertEqual(callbacks, ["voice:default", "voice:female", "voice:male"])

    async def test_single_voice_keyboard(self) -> None:
        catalog = VoiceCatalog([VoiceOption("default", "Основной голос", "voice-default", None)])
        markup = build_voice_keyboard(catalog.voices, "default")
        self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "voice:default")

    async def test_choose_voice_shows_inline_keyboard(self) -> None:
        message = FakeMessage("Выбрать голос", user_id=2101)
        await choose_voice(message, database=self.database, voice_catalog=self.catalog)
        self.assertEqual(message.answers[0], "Выберите голос:")

    async def test_voice_selection_is_saved(self) -> None:
        message = FakeMessage("Выбрать голос", user_id=2102)
        callback = FakeCallback("voice:male", message, user_id=2102)
        await self.database.upsert_user(2102, username="voiceuser")
        await on_voice_selected(callback, database=self.database, voice_catalog=self.catalog)
        user = await self.database.get_user_by_telegram_id(2102)
        self.assertEqual(user["selected_voice_key"], "male")
        self.assertEqual(message.answers[-1], "Голос выбран: Мужской голос.")
        self.assertTrue(callback.answered)

    async def test_unavailable_voice_key_is_rejected(self) -> None:
        message = FakeMessage("Выбрать голос", user_id=2103)
        callback = FakeCallback("voice:other", message, user_id=2103)
        await on_voice_selected(callback, database=self.database, voice_catalog=self.catalog)
        self.assertEqual(message.answers[-1], VOICE_UNAVAILABLE)
        self.assertTrue(callback.answered)

    async def test_save_failure_shows_safe_message(self) -> None:
        message = FakeMessage("Выбрать голос", user_id=2104)
        callback = FakeCallback("voice:female", message, user_id=2104)

        class BrokenDatabase:
            async def get_voice_by_key(self, *args: object, **kwargs: object) -> dict:
                return {"is_active": 1}

            async def upsert_user(self, *args: object, **kwargs: object) -> dict:
                raise RuntimeError("db down")

            async def update_user_voice(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("db down")

        await on_voice_selected(callback, database=BrokenDatabase(), voice_catalog=self.catalog)
        self.assertEqual(message.answers[-1], VOICE_SAVE_FAILED)
        self.assertTrue(callback.answered)

    async def test_statistics_show_only_current_user(self) -> None:
        await self.database.upsert_user(2201, username="one")
        await self.database.upsert_user(2202, username="two")
        await self.database.create_tts_request("one-ok", 2201, 12, voice_key="default")
        await self.database.mark_tts_request_success(
            "one-ok",
            audio_size_bytes=10,
            duration_ms=10,
            estimated_credits=12,
            estimated_cost_usd=Decimal("0.012000"),
        )
        await self.database.create_tts_request("two-ok", 2202, 99, voice_key="female")
        await self.database.mark_tts_request_success(
            "two-ok",
            audio_size_bytes=10,
            duration_ms=10,
            estimated_credits=99,
        )
        message = FakeMessage("Моя статистика", user_id=2201)
        await show_my_statistics(message, database=self.database, voice_catalog=self.catalog)
        text = message.answers[-1]
        self.assertIn("Ориентировочный расход", text)
        self.assertIn("12", text)
        self.assertNotIn("99", text)
        self.assertIn("0.012000 USD", text)

    async def test_statistics_unavailable(self) -> None:
        message = FakeMessage("Моя статистика", user_id=2203)
        await show_my_statistics(message, database=None, voice_catalog=self.catalog)
        self.assertEqual(message.answers[-1], STATS_UNAVAILABLE)

    async def test_format_statistics_requires_estimated_wording(self) -> None:
        text = format_user_statistics(
            {
                "success_count": 1,
                "failed_count": 0,
                "success_char_count": 5,
                "estimated_credits": 5,
                "speech_speed": 1.0,
                "estimated_cost_usd": None,
            },
            "Основной голос",
        )
        self.assertIn("Ориентировочный расход", text)
        self.assertIn("Голос: Основной голос", text)
        self.assertIn("Скорость: Обычная — 1.0×", text)

    async def test_speed_keyboard_marks_current_and_uses_safe_keys(self) -> None:
        markup = build_speed_keyboard(1.0)
        callbacks = [row[0].callback_data for row in markup.inline_keyboard]
        labels = [row[0].text for row in markup.inline_keyboard]
        self.assertEqual(callbacks, ["speed:slow", "speed:normal", "speed:fast"])
        self.assertTrue(labels[1].startswith("✅"))
        self.assertNotIn("slow", "".join(labels))
        self.assertNotIn("normal", "".join(labels))
        self.assertNotIn("fast", "".join(labels))

    async def test_speed_selection_is_saved(self) -> None:
        await self.database.upsert_user(2401, username="speed-user")
        message = FakeMessage("⏱ Скорость", user_id=2401)
        await configure_speed(message, database=self.database)
        self.assertEqual(message.answers[0], "Выберите скорость озвучивания:")
        callback = FakeCallback("speed:fast", message, user_id=2401)
        await on_speed_selected(callback, database=self.database)
        user = await self.database.get_user_by_telegram_id(2401)
        self.assertEqual(user["speech_speed"], 1.15)
        self.assertEqual(message.answers[-1], "Скорость озвучивания изменена: Быстро — 1.15×")
        self.assertTrue(callback.answered)

    async def test_invalid_speed_callback_is_rejected(self) -> None:
        await self.database.upsert_user(2402)
        message = FakeMessage("⏱ Скорость", user_id=2402)
        callback = FakeCallback("speed:turbo", message, user_id=2402)
        await on_speed_selected(callback, database=self.database)
        self.assertEqual(message.answers[-1], SPEED_INVALID)
        user = await self.database.get_user_by_telegram_id(2402)
        self.assertEqual(user["speech_speed"], 1.0)
        self.assertTrue(callback.answered)

    async def test_speed_save_error_shows_safe_message(self) -> None:
        message = FakeMessage("⏱ Скорость", user_id=2403)
        callback = FakeCallback("speed:slow", message, user_id=2403)
        await on_speed_selected(callback, database=None)
        self.assertEqual(message.answers[-1], SPEED_SAVE_FAILED)
        self.assertTrue(callback.answered)

    async def test_text_handler_uses_user_speed_and_keeps_users_separate(self) -> None:
        await self.database.upsert_user(2501, username="slow-user")
        await self.database.upsert_user(2502, username="fast-user")
        await self.database.update_user_speed(2501, 0.85)
        await self.database.update_user_speed(2502, 1.15)
        first = FakeMessage("Привет", user_id=2501)
        second = FakeMessage("Привет", user_id=2502)
        tts = FakeTTSService()
        with (
            patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            tempfile.TemporaryDirectory() as tmp,
            patch("handlers.text.create_temp_mp3_path", return_value=Path(tmp) / "a.mp3"),
        ):
            await handle_text_message(
                first,
                tts,
                database=self.database,
                default_voice_id="voice-default",
                voice_catalog=self.catalog,
            )
        with (
            patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            tempfile.TemporaryDirectory() as tmp,
            patch("handlers.text.create_temp_mp3_path", return_value=Path(tmp) / "b.mp3"),
        ):
            await handle_text_message(
                second,
                tts,
                database=self.database,
                default_voice_id="voice-default",
                voice_catalog=self.catalog,
            )
        self.assertEqual(tts.calls[0][3], 0.85)
        self.assertEqual(tts.calls[1][3], 1.15)
        async with __import__("aiosqlite").connect(self.database.path) as conn:
            rows = await (
                await conn.execute(
                    "SELECT speech_speed FROM tts_requests ORDER BY id"
                )
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [0.85, 1.15])

    async def test_text_handler_uses_selected_voice_and_fallback(self) -> None:
        await self.database.upsert_user(2301, username="speaker")
        await self.database.update_user_voice(2301, "male", "voice-male")
        message = FakeMessage("Привет", user_id=2301)
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
                default_voice_id="voice-default",
                voice_catalog=self.catalog,
            )
        self.assertEqual(tts.calls[0][2], "voice-male")

        limited = VoiceCatalog([VoiceOption("default", "Основной голос", "voice-default", None)])
        used_id, used_key, _multiplier = await resolve_user_voice(
            self.database,
            2301,
            await self.database.get_user_by_telegram_id(2301),
            limited,
            "voice-default",
            "voice-default",
        )
        self.assertEqual(used_id, "voice-default")
        self.assertEqual(used_key, "default")
        user = await self.database.get_user_by_telegram_id(2301)
        self.assertEqual(user["selected_voice_key"], "default")


if __name__ == "__main__":
    unittest.main()
