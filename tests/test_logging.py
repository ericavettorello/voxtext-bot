"""Тесты централизованного журнала без запросов к ElevenLabs."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from handlers.start import cmd_start
from handlers.text import handle_text_message
from logging_config import (
    HANDLER_MARK,
    LOG_DIR,
    LOG_FILE,
    format_log_event,
    sanitize_log_value,
    setup_logging,
    voice_id_tail,
)
from services.tts_service import TTSQuotaError
from test_text_handler import DummyChatAction, FakeMessage, FakeTTSService


def _clear_voxtext_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, HANDLER_MARK, None):
            root.removeHandler(handler)
            handler.close()


def _read_log(log_path: Path) -> str:
    for handler in logging.getLogger().handlers:
        if getattr(handler, HANDLER_MARK, None) == "file":
            handler.flush()
    content = log_path.read_text(encoding="utf-8")
    _clear_voxtext_handlers()
    return content


class SanitizeTests(unittest.TestCase):
    def test_username_newlines_are_removed(self) -> None:
        cleaned = sanitize_log_value("elena\nINFO | forged")
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("\r", cleaned)
        self.assertEqual(cleaned, "elena_INFO | forged")

    def test_missing_username_is_none(self) -> None:
        self.assertEqual(sanitize_log_value(None), "none")
        self.assertEqual(sanitize_log_value(""), "none")

    def test_voice_id_tail_hides_full_id(self) -> None:
        voice_id = "ABCDEFGHIJ1234"
        self.assertEqual(voice_id_tail(voice_id), "1234")
        self.assertNotEqual(voice_id_tail(voice_id), voice_id)


class LoggingSetupTests(unittest.TestCase):
    def tearDown(self) -> None:
        _clear_voxtext_handlers()

    def test_default_logs_dir_and_file_are_created(self) -> None:
        path = setup_logging()
        self.assertTrue(LOG_DIR.is_dir())
        self.assertEqual(path.resolve(), LOG_FILE.resolve())
        logging.getLogger("voxtext.test.default").info("event=logging_self_check")
        for handler in logging.getLogger().handlers:
            if getattr(handler, HANDLER_MARK, None) == "file":
                handler.flush()
        self.assertTrue(LOG_FILE.exists())

    def test_writes_info_warning_error_and_russian(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = setup_logging(Path(tmp))
            test_logger = logging.getLogger("voxtext.test.levels")
            test_logger.info("event=info_check text=Привет")
            test_logger.warning("event=warning_check text=проверка")
            test_logger.error("event=error_check text=ошибка")
            content = _read_log(log_path)
            self.assertIn("INFO | voxtext.test.levels | event=info_check text=Привет", content)
            self.assertIn("WARNING | voxtext.test.levels | event=warning_check text=проверка", content)
            self.assertIn("ERROR | voxtext.test.levels | event=error_check text=ошибка", content)

    def test_no_duplicate_handlers_or_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = setup_logging(Path(tmp))
            setup_logging(Path(tmp))
            marker = "event=duplicate_check token=uniq-dup-42"
            logging.getLogger("voxtext.test.dup").info(marker)
            content = _read_log(log_path)
            self.assertEqual(content.count(marker), 1)

    def test_logs_survive_setup_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = setup_logging(Path(tmp))
            logging.getLogger("voxtext.test.persist").info("event=before_restart")
            for handler in logging.getLogger().handlers:
                if getattr(handler, HANDLER_MARK, None) == "file":
                    handler.flush()
            setup_logging(Path(tmp))
            logging.getLogger("voxtext.test.persist").info("event=after_restart")
            content = _read_log(log_path)
            self.assertIn("event=before_restart", content)
            self.assertIn("event=after_restart", content)


class EventLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        _clear_voxtext_handlers()

    async def test_tts_flow_has_request_id_and_hides_secrets(self) -> None:
        from handlers.text import active_jobs

        for user_id in list(active_jobs._user_ids):
            active_jobs.release(user_id)

        secret_text = "СЕКРЕТНЫЙ_ТЕКСТ_ПОЛЬЗОВАТЕЛЯ_XYZ"
        fake_token = "123456:FAKE-TELEGRAM-TOKEN"
        fake_key = "sk_fake_elevenlabs_key_value"
        fake_voice = "FullVoiceIdentifier99"

        with tempfile.TemporaryDirectory() as tmp:
            log_path = setup_logging(Path(tmp))
            message = FakeMessage(secret_text, user_id=777)
            message.from_user.username = "elena\nadmin"
            tts = FakeTTSService()
            tts.voice_id = fake_voice

            with (
                patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
                tempfile.TemporaryDirectory() as audio_tmp,
                patch(
                    "handlers.text.create_temp_mp3_path",
                    return_value=Path(audio_tmp) / "tts_test.mp3",
                ),
            ):
                await handle_text_message(message, tts)

            content = _read_log(log_path)

        self.assertIn("event=tts_requested", content)
        self.assertIn("event=tts_started", content)
        self.assertIn("event=tts_generated", content)
        self.assertIn("event=audio_sent", content)
        self.assertIn("event=temp_file_deleted", content)
        self.assertIn("request_id=", content)
        self.assertIn("username=elena_admin", content)
        self.assertIn("voice_id_tail=er99", content)
        self.assertNotIn(secret_text, content)
        self.assertNotIn(fake_token, content)
        self.assertNotIn(fake_key, content)
        self.assertNotIn(fake_voice, content)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", content)
        self.assertNotIn("ELEVENLABS_API_KEY", content)

    async def test_rejected_requests_are_logged(self) -> None:
        from handlers.text import active_jobs

        for user_id in list(active_jobs._user_ids):
            active_jobs.release(user_id)

        with tempfile.TemporaryDirectory() as tmp:
            log_path = setup_logging(Path(tmp))
            await handle_text_message(FakeMessage("   "), FakeTTSService())
            await handle_text_message(FakeMessage("б" * 3501), FakeTTSService())
            active_jobs.try_acquire(55)
            await handle_text_message(FakeMessage("Текст", user_id=55), FakeTTSService())
            active_jobs.release(55)
            content = _read_log(log_path)

        self.assertIn("event=input_rejected reason=empty user_id=100", content)
        self.assertIn("event=input_rejected reason=too_long", content)
        self.assertIn("event=request_rejected reason=already_processing user_id=55", content)

    async def test_start_command_is_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = setup_logging(Path(tmp))
            message = FakeMessage("/start")
            message.from_user.username = "elena"
            await cmd_start(message)
            content = _read_log(log_path)

        self.assertIn("event=start_command", content)
        self.assertIn("user_id=100", content)
        self.assertIn("username=elena", content)
        self.assertIn("chat_id=100", content)
        self.assertIn("message_id=10", content)

    async def test_tts_failed_does_not_write_user_text(self) -> None:
        from handlers.text import active_jobs

        for user_id in list(active_jobs._user_ids):
            active_jobs.release(user_id)

        secret_text = "Ещё один секретный абзац"
        with tempfile.TemporaryDirectory() as tmp:
            log_path = setup_logging(Path(tmp))
            message = FakeMessage(secret_text)
            tts = FakeTTSService(error=TTSQuotaError())
            with (
                patch("handlers.text.ChatActionSender.upload_voice", return_value=DummyChatAction()),
                tempfile.TemporaryDirectory() as audio_tmp,
                patch(
                    "handlers.text.create_temp_mp3_path",
                    return_value=Path(audio_tmp) / "tts_test.mp3",
                ),
            ):
                await handle_text_message(message, tts)
            content = _read_log(log_path)

        self.assertIn("event=tts_failed", content)
        self.assertIn("error_kind=quota_exceeded", content)
        self.assertNotIn(secret_text, content)


class FormatEventTests(unittest.TestCase):
    def test_event_format_is_key_value(self) -> None:
        line = format_log_event("tts_requested", user_id=123, username="elena", chars=245)
        self.assertEqual(
            line,
            "event=tts_requested user_id=123 username=elena chars=245",
        )


if __name__ == "__main__":
    unittest.main()
