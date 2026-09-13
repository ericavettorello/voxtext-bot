"""Тесты обработчика документов: лимиты, черновики и отсутствие реальных запросов к ElevenLabs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from database.db import SOURCE_TYPE_DOCX, SOURCE_TYPE_TXT, init_database
from handlers.documents import (
    DocumentStates,
    UNSUPPORTED_FORMAT,
    _validate_document_meta,
    cancel_document_mode,
    handle_document_message,
    start_document_generation,
    start_document_mode,
)
from handlers.text import active_jobs
from services.document_store import DocumentDraft, document_store
from services.long_tts import GenerationSnapshot
from test_long_text import FakeCallback, FakeFSM
from test_text_handler import FakeMessage, FakeTTSService, FakeUser


class FakeDocument:
    def __init__(
        self,
        file_name: str | None,
        content: bytes,
        mime_type: str | None = "text/plain",
        file_size: int | None = None,
    ) -> None:
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = len(content) if file_size is None else file_size
        self.file_id = "file-test"
        self.content = content


class DocumentMessage(FakeMessage):
    def __init__(self, document: FakeDocument | None, user_id: int = 501) -> None:
        super().__init__("", user_id=user_id)
        self.document = document
        self.bot = MagicMock()
        self.bot.download = AsyncMock(side_effect=self._download)

    async def _download(self, document: FakeDocument, destination) -> None:
        Path(destination).write_bytes(document.content)


class DocumentHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = document_store
        for user_id in (501, 502):
            leftover = self.store.remove(user_id)
            if leftover is not None:
                from services.audio_service import delete_job_directory

                delete_job_directory(leftover.job_dir, leftover.job_id)
        for user_id in list(active_jobs._user_ids):
            active_jobs.release(user_id)
        self._tmp = tempfile.TemporaryDirectory()
        self.database = await init_database(Path(self._tmp.name) / "voxtext.db")

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()
        for user_id in (501, 502):
            leftover = self.store.remove(user_id)
            if leftover is not None:
                from services.audio_service import delete_job_directory

                delete_job_directory(leftover.job_dir, leftover.job_id)

    def _draft(self, user_id: int = 501, extension: str = "txt") -> DocumentDraft:
        job_dir = Path(self._tmp.name) / f"job-{user_id}"
        job_dir.mkdir(exist_ok=True)
        source = job_dir / f"source.{extension}"
        source.write_bytes(b"ok")
        draft = DocumentDraft(
            job_id=f"job-{user_id}",
            job_dir=job_dir,
            source_path=source,
            extension=extension,
            display_name="note.txt" if extension == "txt" else "note.docx",
            text="Документ для озвучки",
            char_count=21,
            chunk_count=1,
            chunks=["Документ для озвучки"],
            voice_id="voice-a",
            voice_key="female",
            voice_name="Женский голос",
            speech_speed=0.85,
            credit_multiplier=1.0,
        )
        self.store.put(user_id, draft)
        return draft

    async def test_oversized_file_is_not_downloaded(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument("big.txt", b"hello", file_size=11 * 1024 * 1024)
        message = DocumentMessage(document, user_id=501)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            database=self.database,
            tts_service=tts,
            max_upload_file_mb=10,
        )
        message.bot.download.assert_not_awaited()
        self.assertEqual(tts.calls, [])
        self.assertTrue(any("слишком большой" in item.lower() for item in message.answers))

    async def test_too_long_extracted_text_skips_elevenlabs(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument("long.txt", ("слово " * 50).encode("utf-8"))
        message = DocumentMessage(document, user_id=501)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            database=self.database,
            tts_service=tts,
            max_long_text_chars=20,
        )
        self.assertEqual(tts.calls, [])
        self.assertTrue(any("слишком большой" in item.lower() for item in message.answers))
        self.assertIsNone(self.store.get(501))

    async def test_confirmation_does_not_call_elevenlabs(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument("note.txt", "Короткий текст".encode("utf-8"))
        message = DocumentMessage(document, user_id=501)
        await self.database.upsert_user(501)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            database=self.database,
            tts_service=tts,
        )
        self.assertEqual(tts.calls, [])
        self.assertIsNotNone(self.store.get(501))
        self.assertTrue(any("подготовлен к озвучиванию" in item.lower() for item in message.answers))

    async def test_confirmation_uses_long_tts_runner(self) -> None:
        draft = self._draft(501, "txt")
        callback = FakeCallback("doc:start", FakeMessage("go", user_id=501), user_id=501)
        state = FakeFSM()
        await state.set_state(DocumentStates.confirming)
        tts = FakeTTSService()
        with patch("handlers.documents.run_confirmed_long_tts", new_callable=AsyncMock) as runner:
            await start_document_generation(callback, state, tts, database=self.database)
        runner.assert_awaited()
        snapshot = runner.await_args.kwargs["snapshot"]
        self.assertIsInstance(snapshot, GenerationSnapshot)
        self.assertEqual(snapshot.voice_key, "female")
        self.assertEqual(snapshot.speech_speed, 0.85)
        self.assertEqual(snapshot.chunks, draft.chunks)
        self.assertEqual(runner.await_args.kwargs["options"].source_type, SOURCE_TYPE_TXT)

    async def test_docx_source_type_is_used(self) -> None:
        self._draft(501, "docx")
        callback = FakeCallback("doc:start", FakeMessage("go", user_id=501), user_id=501)
        state = FakeFSM()
        await state.set_state(DocumentStates.confirming)
        with patch("handlers.documents.run_confirmed_long_tts", new_callable=AsyncMock) as runner:
            await start_document_generation(callback, state, FakeTTSService(), database=self.database)
        self.assertEqual(runner.await_args.kwargs["options"].source_type, SOURCE_TYPE_DOCX)

    async def test_double_confirm_is_rejected(self) -> None:
        self._draft(501)
        active_jobs.try_acquire(501)
        callback = FakeCallback("doc:start", FakeMessage("go", user_id=501), user_id=501)
        state = FakeFSM()
        await state.set_state(DocumentStates.confirming)
        tts = FakeTTSService()
        with patch("handlers.documents.run_confirmed_long_tts", new_callable=AsyncMock) as runner:
            await start_document_generation(callback, state, tts)
        runner.assert_not_awaited()
        self.assertEqual(tts.calls, [])
        active_jobs.release(501)

    async def test_drafts_are_isolated(self) -> None:
        first = self._draft(501, "txt")
        second = self._draft(502, "docx")
        self.assertEqual(self.store.get(501).text, first.text)
        self.assertEqual(self.store.get(502).extension, "docx")
        self.assertNotEqual(self.store.get(501).job_id, self.store.get(502).job_id)

    async def test_cancel_clears_temp_files(self) -> None:
        draft = self._draft(501)
        job_dir = draft.job_dir
        state = FakeFSM()
        await state.set_state(DocumentStates.confirming)
        await cancel_document_mode(FakeMessage("❌ Отмена", user_id=501), state)  # type: ignore[arg-type]
        self.assertIsNone(self.store.get(501))
        self.assertFalse(job_dir.exists())
        self.assertIsNone(state.state)

    async def test_error_cleans_temp_dir(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument("broken.docx", b"not-zip", mime_type="application/zip")
        message = DocumentMessage(document, user_id=501)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            tts_service=tts,
        )
        self.assertIsNone(self.store.get(501))
        self.assertEqual(tts.calls, [])
        self.assertTrue(any("docx" in item.lower() or "поврежд" in item.lower() or "корректн" in item.lower() for item in message.answers))

    async def test_unsupported_exe_is_rejected(self) -> None:
        from services.document_text import DocumentError

        document = FakeDocument("file.exe", b"MZ", mime_type="application/octet-stream", file_size=12)
        with self.assertRaises(DocumentError) as ctx:
            _validate_document_meta(document, 10)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.user_message, UNSUPPORTED_FORMAT)

    async def test_source_types_are_stored(self) -> None:
        await self.database.upsert_user(501)
        await self.database.create_tts_request(
            "req-txt", 501, 10, source_type=SOURCE_TYPE_TXT, request_type="long"
        )
        await self.database.create_tts_request(
            "req-docx", 501, 12, source_type=SOURCE_TYPE_DOCX, request_type="long"
        )
        import aiosqlite

        async with aiosqlite.connect(self.database.path) as conn:
            rows = await (await conn.execute("SELECT source_type FROM tts_requests ORDER BY id")).fetchall()
        self.assertEqual([row[0] for row in rows], ["txt", "docx"])

    async def test_upload_mode_message(self) -> None:
        message = FakeMessage("📄 Загрузить файл", user_id=501)
        await start_document_mode(message, FakeFSM())  # type: ignore[arg-type]
        self.assertIn("TXT, DOCX или PDF", message.answers[-1])


if __name__ == "__main__":
    unittest.main()
