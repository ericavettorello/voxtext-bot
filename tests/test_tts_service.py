"""Тесты TTS-сервиса с подменой ElevenLabs API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import httpx
from elevenlabs.core.api_error import ApiError

from services.tts_service import (
    OUTPUT_FORMAT,
    TEMP_DIR,
    TTSError,
    TTSQuotaError,
    TTSService,
    USER_ERROR_AUTH,
    USER_ERROR_FORBIDDEN,
    USER_ERROR_FREE_TIER,
    USER_ERROR_QUOTA,
    USER_ERROR_RATE_LIMIT,
    USER_ERROR_UNAVAILABLE,
    USER_ERROR_UNKNOWN,
    USER_ERROR_VOICE,
    classify_elevenlabs_error,
    create_temp_mp3_path,
    delete_temp_file,
)


class FakeTTSClient:
    def __init__(self, chunks: list[bytes] | Exception) -> None:
        self.text_to_speech = MagicMock()
        if isinstance(chunks, Exception):
            self.text_to_speech.convert.side_effect = chunks
        else:
            self.text_to_speech.convert.return_value = iter(chunks)


class TTSServiceTests(unittest.TestCase):
    def test_generate_speech_writes_non_empty_chunks(self) -> None:
        client = FakeTTSClient([b"", b"ID3", b"audio-data"])
        service = TTSService(api_key="test-key", voice_id="test-voice", client=client)

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "out.mp3"
            result = service.generate_speech("Привет, мир", output_path)

            self.assertEqual(result, output_path)
            self.assertTrue(output_path.exists())
            self.assertEqual(output_path.read_bytes(), b"ID3audio-data")

        convert = client.text_to_speech.convert
        convert.assert_called_once()
        kwargs = convert.call_args.kwargs
        self.assertEqual(kwargs["text"], "Привет, мир")
        self.assertEqual(kwargs["voice_id"], "test-voice")
        self.assertEqual(kwargs["model_id"], "eleven_multilingual_v2")
        self.assertEqual(kwargs["output_format"], OUTPUT_FORMAT)
        self.assertEqual(kwargs["voice_settings"].speed, 1.0)
        self.assertIsNone(kwargs["voice_settings"].stability)
        self.assertIsNone(kwargs["voice_settings"].similarity_boost)

    def test_generate_speech_uses_passed_voice_id(self) -> None:
        client = FakeTTSClient([b"ID3", b"audio"])
        service = TTSService(api_key="test-key", voice_id="default-voice", client=client)

        with tempfile.TemporaryDirectory() as tmp:
            service.generate_speech("Текст", Path(tmp) / "out.mp3", voice_id="selected-voice")

        self.assertEqual(client.text_to_speech.convert.call_args.kwargs["voice_id"], "selected-voice")

    def test_generate_speech_passes_user_speed(self) -> None:
        client = FakeTTSClient([b"ID3", b"audio"])
        service = TTSService(api_key="test-key", voice_id="default-voice", client=client)

        with tempfile.TemporaryDirectory() as tmp:
            service.generate_speech(
                "Текст",
                Path(tmp) / "out.mp3",
                voice_id="selected-voice",
                speech_speed=0.85,
            )

        settings = client.text_to_speech.convert.call_args.kwargs["voice_settings"]
        self.assertEqual(settings.speed, 0.85)
        self.assertIsNone(settings.style)
        self.assertIsNone(settings.use_speaker_boost)

    def test_empty_text_raises(self) -> None:
        client = FakeTTSClient([b"ID3"])
        service = TTSService(api_key="test-key", voice_id="test-voice", client=client)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TTSError):
                service.generate_speech("   ", Path(tmp) / "out.mp3")

        client.text_to_speech.convert.assert_not_called()

    def test_empty_audio_raises(self) -> None:
        client = FakeTTSClient([b"", b""])
        service = TTSService(api_key="test-key", voice_id="test-voice", client=client)

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "out.mp3"
            with self.assertRaises(TTSError):
                service.generate_speech("Текст", output_path)

    def test_quota_error_mapped_only_for_quota_exceeded(self) -> None:
        error = ApiError(
            status_code=401,
            body={"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota"}},
        )
        client = FakeTTSClient(error)
        service = TTSService(api_key="test-key", voice_id="test-voice", client=client)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TTSQuotaError) as ctx:
                service.generate_speech("Текст", Path(tmp) / "out.mp3")

        self.assertEqual(ctx.exception.user_message, USER_ERROR_QUOTA)

    def test_payment_required_library_voice_is_not_quota(self) -> None:
        error = ApiError(
            status_code=402,
            body={
                "detail": {
                    "status": "payment_required",
                    "message": (
                        "Free users cannot use library voices via the API. "
                        "Please upgrade your subscription to use this voice."
                    ),
                }
            },
        )
        client = FakeTTSClient(error)
        service = TTSService(api_key="test-key", voice_id="test-voice", client=client)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TTSError) as ctx:
                service.generate_speech("Текст", Path(tmp) / "out.mp3")

        self.assertIsInstance(ctx.exception, TTSError)
        self.assertNotIsInstance(ctx.exception, TTSQuotaError)
        self.assertEqual(ctx.exception.user_message, USER_ERROR_FREE_TIER)

    def test_invalid_api_key_does_not_expose_secret(self) -> None:
        error = ApiError(status_code=401, body={"detail": {"status": "invalid_api_key"}})
        client = FakeTTSClient(error)
        service = TTSService(api_key="super-secret-key", voice_id="test-voice", client=client)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TTSError) as ctx:
                service.generate_speech("Текст", Path(tmp) / "out.mp3")

        self.assertEqual(ctx.exception.user_message, USER_ERROR_AUTH)
        self.assertNotIn("super-secret-key", str(ctx.exception))
        self.assertNotIn("super-secret-key", ctx.exception.user_message)

    def test_timeout_and_network_errors(self) -> None:
        cases = (
            httpx.TimeoutException("timeout"),
            httpx.ConnectError("offline"),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                client = FakeTTSClient(error)
                service = TTSService(api_key="test-key", voice_id="voice", client=client)
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(TTSError) as ctx:
                        service.generate_speech("Текст", Path(tmp) / "out.mp3")
                self.assertEqual(ctx.exception.user_message, USER_ERROR_UNAVAILABLE)


class ErrorClassificationTests(unittest.TestCase):
    def test_quota_exceeded_only(self) -> None:
        error = classify_elevenlabs_error(402, "quota_exceeded", "credits are gone")
        self.assertIsInstance(error, TTSQuotaError)
        self.assertEqual(error.user_message, USER_ERROR_QUOTA)

    def test_payment_required_is_not_quota(self) -> None:
        error = classify_elevenlabs_error(
            402,
            "payment_required",
            "Free users cannot use library voices via the API.",
        )
        self.assertNotIsInstance(error, TTSQuotaError)
        self.assertEqual(error.user_message, USER_ERROR_FREE_TIER)

    def test_401_and_invalid_api_key(self) -> None:
        self.assertEqual(
            classify_elevenlabs_error(401, "invalid_api_key", "bad key").user_message,
            USER_ERROR_AUTH,
        )
        self.assertEqual(
            classify_elevenlabs_error(401, None, None).user_message,
            USER_ERROR_AUTH,
        )

    def test_voice_not_found(self) -> None:
        self.assertEqual(
            classify_elevenlabs_error(404, "voice_not_found", "missing").user_message,
            USER_ERROR_VOICE,
        )

    def test_forbidden_and_403(self) -> None:
        self.assertEqual(
            classify_elevenlabs_error(403, None, None).user_message,
            USER_ERROR_FORBIDDEN,
        )
        self.assertEqual(
            classify_elevenlabs_error(400, "missing_permissions", "no access").user_message,
            USER_ERROR_FORBIDDEN,
        )

    def test_rate_limit(self) -> None:
        self.assertEqual(
            classify_elevenlabs_error(429, "rate_limit_exceeded", "slow down").user_message,
            USER_ERROR_RATE_LIMIT,
        )
        self.assertEqual(
            classify_elevenlabs_error(429, None, None).user_message,
            USER_ERROR_RATE_LIMIT,
        )

    def test_unusual_activity(self) -> None:
        self.assertEqual(
            classify_elevenlabs_error(401, "detected_unusual_activity", "blocked").user_message,
            USER_ERROR_FREE_TIER,
        )

    def test_unknown_error(self) -> None:
        self.assertEqual(
            classify_elevenlabs_error(500, "server_error", "oops").user_message,
            USER_ERROR_UNKNOWN,
        )

    def test_generic_401_without_quota_wording_is_auth(self) -> None:
        error = classify_elevenlabs_error(401, None, "insufficient permissions for this credit")
        self.assertNotIsInstance(error, TTSQuotaError)
        self.assertEqual(error.user_message, USER_ERROR_AUTH)


class TempFileTests(unittest.TestCase):
    def test_temp_dir_created_and_names_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "missing-temp"
            first = create_temp_mp3_path(directory)
            second = create_temp_mp3_path(directory)

            self.assertTrue(directory.is_dir())
            self.assertNotEqual(first, second)
            self.assertTrue(first.name.startswith("tts_"))
            self.assertTrue(first.name.endswith(".mp3"))
            self.assertRegex(first.name, r"^tts_[0-9a-f-]{36}\.mp3$")
            self.assertNotIn("user", first.name)

    def test_default_temp_dir_is_project_temp(self) -> None:
        path = create_temp_mp3_path()
        self.assertEqual(path.parent.resolve(), TEMP_DIR.resolve())
        self.assertTrue(TEMP_DIR.is_dir())

    def test_delete_temp_file_removes_only_that_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = directory / "tts_test.mp3"
            neighbor = directory / "keep.mp3"
            target.write_bytes(b"ID3")
            neighbor.write_bytes(b"keep")

            delete_temp_file(target)

            self.assertFalse(target.exists())
            self.assertTrue(neighbor.exists())
            self.assertTrue(directory.exists())

    def test_delete_missing_file_does_not_raise(self) -> None:
        delete_temp_file(Path("missing-file-does-not-exist.mp3"))
        delete_temp_file(None)


if __name__ == "__main__":
    unittest.main()
