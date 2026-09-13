"""Тесты пользовательских сообщений без запросов к ElevenLabs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from handlers.documents import start_document_mode
from handlers.settings import show_help
from handlers.start import cmd_start, cmd_text_hint
from handlers.text import handle_text_message
from test_long_text import FakeFSM
from test_text_handler import DummyChatAction, FakeMessage, FakeTTSService
from texts import DOCUMENT_UPLOAD_HINT, HELP_TEXT, SHORT_TEXT_HINT, WELCOME_TEXT


class InterfaceTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_lists_three_languages(self) -> None:
        message = FakeMessage("/start")
        await cmd_start(message)
        text = message.answers[-1]
        self.assertIn("русский", text)
        self.assertIn("английский", text)
        self.assertIn("итальянский", text)

    async def test_start_mentions_automatic_language(self) -> None:
        message = FakeMessage("/start")
        await cmd_start(message)
        self.assertIn("определяется автоматически", message.answers[-1].lower())
        self.assertIn("определяется автоматически", WELCOME_TEXT.lower())
        self.assertIn("PDF", WELCOME_TEXT)
        self.assertIn("PDF", HELP_TEXT)

    async def test_upload_hint_mentions_txt_docx_and_pdf(self) -> None:
        message = FakeMessage("📄 Загрузить файл", user_id=601)
        await start_document_mode(message, FakeFSM())  # type: ignore[arg-type]
        self.assertIn("TXT", message.answers[-1])
        self.assertIn("DOCX", message.answers[-1])
        self.assertIn("PDF", message.answers[-1])
        self.assertIn("TXT", DOCUMENT_UPLOAD_HINT)
        self.assertIn("DOCX", DOCUMENT_UPLOAD_HINT)
        self.assertIn("PDF", DOCUMENT_UPLOAD_HINT)

    async def test_help_command_returns_current_text(self) -> None:
        message = FakeMessage("/help")
        await show_help(message)
        self.assertEqual(message.answers[-1], HELP_TEXT)
        self.assertIn("Как пользоваться VoxText", message.answers[-1])
        self.assertIn("/limit", message.answers[-1])

    async def test_help_says_bot_does_not_translate(self) -> None:
        message = FakeMessage("Помощь")
        await show_help(message)
        self.assertIn("не переводит", message.answers[-1])
        self.assertIn("не переводит", HELP_TEXT)

    async def test_short_text_hint_is_not_sent_on_regular_tts(self) -> None:
        tts = FakeTTSService()
        message = FakeMessage("Hello from VoxText")
        with (
            patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
            tempfile.TemporaryDirectory() as tmp,
            patch("handlers.text.create_temp_mp3_path", return_value=Path(tmp) / "tts_test.mp3"),
        ):
            await handle_text_message(message, tts)
        self.assertTrue(tts.calls)
        self.assertNotIn(SHORT_TEXT_HINT, message.answers)

    async def test_text_command_shows_short_hint(self) -> None:
        message = FakeMessage("/text")
        await cmd_text_hint(message)
        self.assertEqual(message.answers[-1], SHORT_TEXT_HINT)
        self.assertIn("русский", message.answers[-1])
        self.assertIn("английский", message.answers[-1])
        self.assertIn("итальянский", message.answers[-1])


if __name__ == "__main__":
    unittest.main()
