"""Тесты длинного текста: черновики, лимиты, последовательная генерация и БД."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from database.db import STATUS_PARTIAL_FAILED, init_database
from handlers.long_text import (
    EMPTY_DRAFT,
    LongTextStates,
    _safe_callback_answer,
    _send_audio_file,
    cancel_long_text,
    collect_long_text_part,
    request_long_text_confirmation,
    start_long_generation,
    start_long_text_mode,
)
from handlers.text import active_jobs
from services.audio_service import delete_job_directory, group_chunks_for_telegram, merge_audio_chunks
from services.draft_store import DraftStore
from services.long_tts import GenerationSnapshot, generate_chunks_sequentially
from services.tts_service import TTSError
from test_text_handler import FakeMessage, FakeTTSService, FakeUser


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage, user_id: int = 100) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message
        self.bot = message.bot
        self.answered = False

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answered = True


class FakeFSM:
    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state.state if hasattr(state, "state") else state

    async def get_state(self) -> str | None:
        return self.state

    async def update_data(self, **kwargs: object) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


class DraftStoreTests(unittest.TestCase):
    def test_drafts_are_isolated_and_clear_is_personal(self) -> None:
        store = DraftStore()
        store.add_part(1, "один")
        store.add_part(2, "two")
        store.add_part(1, "два")
        self.assertEqual(store.get(1).parts_count, 2)
        self.assertIn("один", store.get(1).text)
        self.assertEqual(store.get(2).text, "two")
        store.clear(1)
        self.assertEqual(store.get(1).char_count, 0)
        self.assertEqual(store.get(2).text, "two")
        store.remove(2)
        self.assertIsNone(store.get(2))


class LongTextHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from services import draft_store as draft_module

        self.store = draft_module.draft_store
        self.store.remove(401)
        self.store.remove(402)
        for user_id in list(active_jobs._user_ids):
            active_jobs.release(user_id)
        self._tmp = tempfile.TemporaryDirectory()
        self.database = await init_database(Path(self._tmp.name) / "voxtext.db")

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()
        self.store.remove(401)
        self.store.remove(402)

    async def test_parts_accumulate_without_tts(self) -> None:
        tts = FakeTTSService()
        state = FakeFSM()
        first = FakeMessage("📚 Длинный текст", user_id=401)
        await start_long_text_mode(first, state)  # type: ignore[arg-type]
        part_one = FakeMessage("Часть один", user_id=401)
        part_two = FakeMessage("Часть два", user_id=401)
        await collect_long_text_part(part_one, state)  # type: ignore[arg-type]
        await collect_long_text_part(part_two, state)  # type: ignore[arg-type]
        draft = self.store.get(401)
        self.assertEqual(draft.parts_count, 2)
        self.assertEqual(tts.calls, [])
        self.assertIn("Часть добавлена", part_two.answers[-1])
        self.assertIn("Получено частей: 2", part_two.answers[-1])

    async def test_cancel_clears_state(self) -> None:
        state = FakeFSM()
        await start_long_text_mode(FakeMessage("📚 Длинный текст", user_id=401), state)  # type: ignore[arg-type]
        await collect_long_text_part(FakeMessage("черновик", user_id=401), state)  # type: ignore[arg-type]
        cancel = FakeMessage("❌ Отмена", user_id=401)
        await cancel_long_text(cancel, state)  # type: ignore[arg-type]
        self.assertIsNone(state.state)
        self.assertIsNone(self.store.get(401))
        self.assertIn("отменён", cancel.answers[-1])

    async def test_empty_draft_does_not_call_elevenlabs(self) -> None:
        tts = FakeTTSService()
        state = FakeFSM()
        message = FakeMessage("🎧 Озвучить", user_id=401)
        self.store.start(401)
        await request_long_text_confirmation(
            message,
            state,  # type: ignore[arg-type]
            database=self.database,
            tts_service=tts,
        )
        self.assertEqual(message.answers[-1], EMPTY_DRAFT)
        self.assertEqual(tts.calls, [])

    async def test_max_chars_blocks_generation(self) -> None:
        tts = FakeTTSService()
        state = FakeFSM()
        message = FakeMessage("🎧 Озвучить", user_id=401)
        self.store.start(401)
        self.store.add_part(401, "x" * 50)
        await request_long_text_confirmation(
            message,
            state,  # type: ignore[arg-type]
            database=self.database,
            tts_service=tts,
            max_long_text_chars=20,
        )
        self.assertIn("Текст слишком большой", message.answers[-1])
        self.assertEqual(tts.calls, [])

    async def test_confirmation_does_not_call_elevenlabs(self) -> None:
        tts = FakeTTSService()
        state = FakeFSM()
        await self.database.upsert_user(401)
        self.store.start(401)
        self.store.add_part(401, "Готовый текст")
        message = FakeMessage("🎧 Озвучить", user_id=401)
        await request_long_text_confirmation(
            message,
            state,  # type: ignore[arg-type]
            database=self.database,
            tts_service=tts,
            tts_chunk_size=4500,
        )
        self.assertIn("Текст подготовлен к озвучиванию", message.answers[-1])
        self.assertEqual(tts.calls, [])
        self.assertEqual(state.state, LongTextStates.confirming.state)

    async def test_double_start_is_rejected(self) -> None:
        active_jobs.try_acquire(401)
        callback = FakeCallback("long:start", FakeMessage("go", user_id=401), user_id=401)
        state = FakeFSM()
        await state.set_state(LongTextStates.confirming)
        tts = FakeTTSService()
        await start_long_generation(callback, state, tts)  # type: ignore[arg-type]
        self.assertEqual(tts.calls, [])
        self.assertTrue(callback.answered)
        active_jobs.release(401)

    async def test_stale_callback_answer_is_ignored(self) -> None:
        from aiogram.exceptions import TelegramBadRequest

        class StaleCallback(FakeCallback):
            async def answer(self, *args: object, **kwargs: object) -> None:
                raise TelegramBadRequest(
                    method=MagicMock(),
                    message="query is too old and response timeout expired or query ID is invalid",
                )

        await _safe_callback_answer(StaleCallback("long:start", FakeMessage("go"), user_id=401))

    async def test_audio_send_retries_timeout(self) -> None:
        from aiogram.exceptions import TelegramNetworkError

        class FlakyBot:
            def __init__(self) -> None:
                self.calls = 0

            async def send_audio(self, **kwargs: object) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise TelegramNetworkError(method=MagicMock(), message="Request timeout error")

        bot = FlakyBot()
        await _send_audio_file(bot, 401, Path("result.mp3"), "VoxText")
        self.assertEqual(bot.calls, 2)


class LongGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_use_same_voice_and_speed_sequentially(self) -> None:
        tts = FakeTTSService()
        snapshot = GenerationSnapshot(
            telegram_user_id=9,
            voice_id="voice-a",
            voice_key="default",
            voice_name="Основной голос",
            speech_speed=0.85,
            model_id="eleven_multilingual_v2",
            char_count=10,
            chunks=["one", "two", "three"],
            credit_multiplier=1.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = await generate_chunks_sequentially(tts, snapshot, Path(tmp), "job-1")
            self.assertEqual(len(paths), 3)
            self.assertEqual([call[0] for call in tts.calls], ["one", "two", "three"])
            self.assertTrue(all(call[2] == "voice-a" for call in tts.calls))
            self.assertTrue(all(call[3] == 0.85 for call in tts.calls))
            self.assertEqual(tts.contexts[1][0], "one")
            self.assertEqual(tts.contexts[1][1], "three")

    async def test_second_chunk_error_is_partial(self) -> None:
        class FailingTTS(FakeTTSService):
            def generate_speech(self, text, output_path, voice_id=None, speech_speed=None, previous_text=None, next_text=None):
                if text == "two":
                    raise TTSError("boom")
                return super().generate_speech(text, output_path, voice_id, speech_speed, previous_text, next_text)

        tts = FailingTTS()
        snapshot = GenerationSnapshot(
            telegram_user_id=9,
            voice_id="voice-a",
            voice_key="default",
            voice_name="Основной",
            speech_speed=1.0,
            model_id="eleven_multilingual_v2",
            char_count=6,
            chunks=["one", "two"],
            credit_multiplier=1.0,
        )
        with tempfile.TemporaryDirectory() as db_tmp, tempfile.TemporaryDirectory() as tmp:
            database = await init_database(Path(db_tmp) / "db.sqlite")
            await database.upsert_user(9)
            await database.create_tts_request(
                "long-1",
                9,
                6,
                request_type="long",
                chunk_count=2,
            )
            from services.long_tts import LongTTSInterrupted

            with self.assertRaises(LongTTSInterrupted) as ctx:
                await generate_chunks_sequentially(tts, snapshot, Path(tmp), "job-2")
            self.assertEqual(ctx.exception.completed_chunks, 1)
            await database.mark_tts_request_partial_failed(
                "long-1",
                "unknown",
                completed_chunks=1,
                processed_characters=3,
            )
            row = await database.get_user_usage_statistics(9)
            self.assertEqual(row["failed_count"], 1)
            async with __import__("aiosqlite").connect(database.path) as conn:
                status = (await (await conn.execute("SELECT status FROM tts_requests")).fetchone())[0]
            self.assertEqual(status, STATUS_PARTIAL_FAILED)
            await database.close()

    async def test_temp_dir_removed_after_success_and_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "job-ok"
            job_dir.mkdir()
            (job_dir / "chunk_001.mp3").write_bytes(b"x")
            delete_job_directory(job_dir, "job-ok")
            self.assertFalse(job_dir.exists())
            self.assertTrue(root.exists())
            fail_dir = root / "job-fail"
            fail_dir.mkdir()
            delete_job_directory(fail_dir, "job-fail")
            self.assertFalse(fail_dir.exists())

    async def test_delete_retries_locked_file(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-lock"
            job_dir.mkdir()
            (job_dir / "result.mp3").write_bytes(b"x")
            attempts = {"n": 0}
            real_rmtree = shutil.rmtree

            def flaky(path, *args, **kwargs):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise PermissionError(32, "locked")
                return real_rmtree(path, *args, **kwargs)

            with (
                patch("services.audio_service.shutil.rmtree", side_effect=flaky),
                patch("services.audio_service.time.sleep"),
            ):
                delete_job_directory(job_dir, "job-lock")
            self.assertFalse(job_dir.exists())
            self.assertGreaterEqual(attempts["n"], 3)


class AudioMergeTests(unittest.TestCase):
    def test_group_splits_before_telegram_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index in range(3):
                path = Path(tmp) / f"c{index}.bin"
                path.write_bytes(b"x" * 12)
                paths.append(path)
            groups = group_chunks_for_telegram(paths, max_bytes=25)
            self.assertEqual(len(groups), 2)
            self.assertEqual(len(groups[0]), 2)
            self.assertEqual(len(groups[1]), 1)

    def test_merge_keeps_order(self) -> None:
        from pydub import AudioSegment
        from services.audio_service import _configure_ffmpeg

        _configure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.mp3"
            second = Path(tmp) / "b.mp3"
            out = Path(tmp) / "out.mp3"
            first_export = AudioSegment.silent(duration=80).export(first, format="mp3")
            second_export = AudioSegment.silent(duration=120).export(second, format="mp3")
            if first_export is not None:
                first_export.close()
            if second_export is not None:
                second_export.close()
            merge_audio_chunks([first, second], out, pause_ms=0)
            merged = AudioSegment.from_file(out)
            self.assertGreaterEqual(len(merged), 180)


if __name__ == "__main__":
    unittest.main()
