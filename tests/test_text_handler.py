"""Тесты обработчика текста без обращения к ElevenLabs и Telegram API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from handlers.text import (
    ACCEPTED_MESSAGE,
    AUDIO_CAPTION,
    AUDIO_PERFORMER,
    AUDIO_TITLE,
    BUSY_MESSAGE,
    EMPTY_TEXT_MESSAGE,
    MAX_TEXT_LENGTH,
    TOO_LONG_MESSAGE,
    ActiveJobs,
    active_jobs,
    handle_text_message,
    is_menu_button,
    validate_user_text,
)
from services.tts_service import TTSError, TTSQuotaError


class FakeUser:
    def __init__(self, user_id: int = 100, username: str | None = None) -> None:
        self.id = user_id
        self.username = username
        self.first_name = "Елена"
        self.last_name = "Иванова"
        self.language_code = "ru"


class FakeChat:
    def __init__(self, chat_id: int = 100) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, text: str, user_id: int = 100) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.message_id = 10
        self.bot = MagicMock()
        self.answers: list[str] = []
        self.audio_calls: list[dict] = []
        self.edited_markups: list[object] = []

    async def answer(self, text: str, **kwargs: object):
        self.answers.append(text)
        return self

    async def edit_text(self, text: str, **kwargs: object):
        self.answers.append(text)
        return self

    async def answer_audio(self, **kwargs: object) -> None:
        self.audio_calls.append(kwargs)

    async def edit_reply_markup(self, **kwargs: object) -> None:
        self.edited_markups.append(kwargs.get("reply_markup"))


class DummyChatAction:
    async def __aenter__(self) -> DummyChatAction:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeTTSService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.voice_id = "testvoiceid1234"
        self.calls: list[tuple[str, Path]] = []
        self.contexts: list[tuple[str | None, str | None]] = []

    def generate_speech(
        self,
        text: str,
        output_path: Path,
        voice_id: str | None = None,
        speech_speed: float | None = None,
        previous_text: str | None = None,
        next_text: str | None = None,
    ) -> Path:
        self.calls.append((text, output_path, voice_id, speech_speed))
        self.contexts.append((previous_text, next_text))
        if self.error:
            raise self.error
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"ID3fake-mp3")
        return output_path


class ValidationTests(unittest.TestCase):
    def test_empty_text(self) -> None:
        self.assertEqual(validate_user_text(""), EMPTY_TEXT_MESSAGE)
        self.assertEqual(validate_user_text("   \n"), EMPTY_TEXT_MESSAGE)

    def test_too_long_text(self) -> None:
        self.assertEqual(
            validate_user_text("а" * (MAX_TEXT_LENGTH + 1)),
            TOO_LONG_MESSAGE,
        )

    def test_valid_text_keeps_original_content(self) -> None:
        text = "  Инструкция по сборке  "
        self.assertIsNone(validate_user_text(text))
        self.assertEqual(text, "  Инструкция по сборке  ")

    def test_menu_buttons_are_not_regular_text(self) -> None:
        self.assertTrue(is_menu_button("Выбрать голос"))
        self.assertTrue(is_menu_button("Моя статистика"))
        self.assertTrue(is_menu_button("Настроить скорость"))
        self.assertTrue(is_menu_button("⏱ Скорость"))
        self.assertTrue(is_menu_button("📄 Загрузить файл"))
        self.assertTrue(is_menu_button("📚 Длинный текст"))
        self.assertTrue(is_menu_button("Помощь"))
        self.assertFalse(is_menu_button("Обычный текст"))


class ActiveJobsTests(unittest.TestCase):
    def test_same_user_cannot_start_second_job(self) -> None:
        jobs = ActiveJobs()
        self.assertTrue(jobs.try_acquire(7))
        self.assertFalse(jobs.try_acquire(7))
        jobs.release(7)
        self.assertTrue(jobs.try_acquire(7))

    def test_release_in_finally_style(self) -> None:
        jobs = ActiveJobs()
        try:
            self.assertTrue(jobs.try_acquire(3))
            raise RuntimeError("boom")
        except RuntimeError:
            pass
        finally:
            jobs.release(3)
        self.assertNotIn(3, jobs)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for user_id in list(active_jobs._user_ids):
            active_jobs.release(user_id)

    async def test_empty_message_reply(self) -> None:
        message = FakeMessage("   ")
        tts = FakeTTSService()
        await handle_text_message(message, tts)  # type: ignore[arg-type]
        self.assertEqual(message.answers, [EMPTY_TEXT_MESSAGE])
        self.assertEqual(tts.calls, [])

    async def test_too_long_message_reply(self) -> None:
        message = FakeMessage("б" * 3501)
        tts = FakeTTSService()
        await handle_text_message(message, tts)  # type: ignore[arg-type]
        self.assertEqual(message.answers, [TOO_LONG_MESSAGE])
        self.assertEqual(tts.calls, [])

    async def test_busy_user_reply(self) -> None:
        active_jobs.try_acquire(55)
        message = FakeMessage("Текст для озвучки", user_id=55)
        tts = FakeTTSService()
        await handle_text_message(message, tts)  # type: ignore[arg-type]
        self.assertEqual(message.answers, [BUSY_MESSAGE])
        self.assertEqual(tts.calls, [])
        active_jobs.release(55)

    async def test_menu_button_is_ignored(self) -> None:
        message = FakeMessage("Помощь")
        tts = FakeTTSService()
        await handle_text_message(message, tts)  # type: ignore[arg-type]
        self.assertEqual(message.answers, [])
        self.assertEqual(tts.calls, [])

    async def test_command_is_ignored(self) -> None:
        message = FakeMessage("/start")
        tts = FakeTTSService()
        await handle_text_message(message, tts)  # type: ignore[arg-type]
        self.assertEqual(message.answers, [])
        self.assertEqual(tts.calls, [])

    async def test_successful_flow_sends_audio_and_deletes_temp_file(self) -> None:
        message = FakeMessage("  Проверьте исходный текст  ")
        tts = FakeTTSService()

        with (
            patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            tempfile.TemporaryDirectory() as tmp,
            patch("handlers.text.create_temp_mp3_path", return_value=Path(tmp) / "tts_test.mp3"),
        ):
            await handle_text_message(message, tts)  # type: ignore[arg-type]
            leftover = list(Path(tmp).glob("*.mp3"))

        self.assertEqual(tts.calls[0][0], "  Проверьте исходный текст  ")
        self.assertIn(ACCEPTED_MESSAGE, message.answers)
        self.assertEqual(len(message.audio_calls), 1)
        self.assertEqual(message.audio_calls[0]["title"], AUDIO_TITLE)
        self.assertEqual(message.audio_calls[0]["performer"], AUDIO_PERFORMER)
        self.assertEqual(message.audio_calls[0]["caption"], AUDIO_CAPTION)
        self.assertEqual(leftover, [])
        self.assertNotIn(100, active_jobs)

    async def test_quota_error_shows_limit_message(self) -> None:
        message = FakeMessage("Текст")
        tts = FakeTTSService(error=TTSQuotaError())

        with (
            patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            tempfile.TemporaryDirectory() as tmp,
            patch("handlers.text.create_temp_mp3_path", return_value=Path(tmp) / "tts_test.mp3"),
        ):
            await handle_text_message(message, tts)  # type: ignore[arg-type]

        self.assertIn("лимит", message.answers[-1])
        self.assertEqual(message.audio_calls, [])
        self.assertNotIn(100, active_jobs)

    async def test_generic_tts_error_message(self) -> None:
        message = FakeMessage("Текст")
        tts = FakeTTSService(error=TTSError())

        with (
            patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            tempfile.TemporaryDirectory() as tmp,
            patch("handlers.text.create_temp_mp3_path", return_value=Path(tmp) / "tts_test.mp3"),
        ):
            await handle_text_message(message, tts)  # type: ignore[arg-type]

        self.assertEqual(message.answers[-1], "Не удалось создать аудио. Подробности записаны в журнал.")
        self.assertNotIn(100, active_jobs)


class ConfigTests(unittest.TestCase):
    def test_missing_required_variables(self) -> None:
        from config import Config, ConfigError

        def fake_getenv(name: str, default: str = "") -> str:
            return ""

        with patch("config.os.getenv", side_effect=fake_getenv):
            with self.assertRaises(ConfigError) as ctx:
                Config()

        message = str(ctx.exception)
        self.assertIn("TELEGRAM_BOT_TOKEN", message)
        self.assertIn("ELEVENLABS_API_KEY", message)
        self.assertIn("ELEVENLABS_VOICE_ID", message)
        self.assertNotIn("=", message.split(":")[0])


if __name__ == "__main__":
    unittest.main()
