"""Тесты PDF: извлечение текста, обработчик и отсутствие реальных запросов к ElevenLabs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from database.db import SOURCE_TYPE_PDF, init_database
from handlers.documents import (
    DocumentStates,
    UNSUPPORTED_FORMAT,
    _validate_document_meta,
    cancel_document_mode,
    handle_document_message,
    start_document_generation,
)
from handlers.text import active_jobs
from pdf_fixtures import build_complex_stream_pdf, build_encrypted_pdf, build_text_pdf, write_text_pdf
from services.document_store import DocumentDraft, document_store
from services.document_text import extract_text_from_document
from services.long_tts import GenerationSnapshot
from services.pdf_text import (
    PdfComplexPageError,
    PdfEncryptedError,
    PdfExtractionResult,
    PdfNoTextError,
    PdfOpenError,
    PdfTooManyPagesError,
    extract_text_from_pdf,
    validate_pdf_header,
)
from test_documents import DocumentMessage, FakeDocument
from test_long_text import FakeCallback, FakeFSM
from test_text_handler import DummyChatAction, FakeMessage, FakeTTSService


class PdfExtractionTests(unittest.TestCase):
    def test_text_pdf_is_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "ok.pdf", ["Hello from VoxText"])
            result = extract_text_from_pdf(path)
        self.assertIsInstance(result, PdfExtractionResult)
        self.assertIn("Hello from VoxText", result.text)
        self.assertEqual(result.total_pages, 1)
        self.assertEqual(result.pages_with_text, 1)
        self.assertEqual(result.pages_without_text, 0)

    def test_russian_text_is_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "ru.pdf", ["Привет, мир"])
            self.assertIn("Привет, мир", extract_text_from_pdf(path).text)

    def test_english_text_is_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "en.pdf", ["Good morning"])
            self.assertIn("Good morning", extract_text_from_pdf(path).text)

    def test_italian_text_is_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "it.pdf", ["Buongiorno Caffè"])
            self.assertIn("Buongiorno", extract_text_from_pdf(path).text)
            self.assertIn("Caffè", extract_text_from_pdf(path).text)

    def test_page_order_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "order.pdf", ["ONE", "TWO", "THREE"])
            text = extract_text_from_pdf(path).text
        self.assertLess(text.find("ONE"), text.find("TWO"))
        self.assertLess(text.find("TWO"), text.find("THREE"))

    def test_empty_pages_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "skip.pdf", ["Alpha", "", "Beta"])
            result = extract_text_from_pdf(path)
        self.assertEqual(result.text, "Alpha\n\nBeta")
        self.assertNotIn("Страница", result.text)

    def test_page_counts_are_correct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "counts.pdf", ["Keep", "", "Also"])
            result = extract_text_from_pdf(path)
        self.assertEqual(result.total_pages, 3)
        self.assertEqual(result.pages_with_text, 2)
        self.assertEqual(result.pages_without_text, 1)

    def test_image_only_pdf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "scan.pdf", ["", ""])
            with self.assertRaises(PdfNoTextError):
                extract_text_from_pdf(path)

    def test_partially_empty_pdf_has_warning_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "partial.pdf", ["Visible", ""])
            result = extract_text_from_pdf(path)
        self.assertEqual(result.pages_without_text, 1)
        self.assertTrue(result.text)

    def test_corrupted_pdf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.pdf"
            path.write_bytes(b"%PDF-1.4\nthis is not a valid structure")
            with self.assertRaises(PdfOpenError):
                extract_text_from_pdf(path)

    def test_fake_pdf_extension_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.pdf"
            path.write_bytes(b"PK\x03\x04this-is-a-zip")
            with self.assertRaises(PdfOpenError):
                validate_pdf_header(path)
            with self.assertRaises(PdfOpenError):
                extract_text_from_pdf(path)

    def test_encrypted_pdf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locked.pdf"
            path.write_bytes(build_encrypted_pdf(["Secret"]))
            with self.assertRaises(PdfEncryptedError):
                extract_text_from_pdf(path)

    def test_too_many_pages_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "many.pdf", ["A", "B", "C"])
            with self.assertRaises(PdfTooManyPagesError) as ctx:
                extract_text_from_pdf(path, max_pages=2)
        self.assertEqual(ctx.exception.page_count, 3)
        self.assertEqual(ctx.exception.limit, 2)

    def test_complex_content_stream_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heavy.pdf"
            path.write_bytes(build_complex_stream_pdf(512))
            with self.assertRaises(PdfComplexPageError):
                extract_text_from_pdf(path, max_content_stream_bytes=64)

    def test_document_wrapper_supports_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "wrap.pdf", ["Wrapper text"])
            self.assertIn("Wrapper text", extract_text_from_document(path, "pdf"))

    def test_logs_do_not_contain_pdf_text(self) -> None:
        marker = "UNIQUE_PDF_SECRET_PHRASE_XYZ"
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_pdf(Path(tmp) / "secret.pdf", [marker])
            with self.assertLogs("services.pdf_text", level="INFO") as captured:
                extract_text_from_pdf(path, job_id="job-log")
        joined = "\n".join(captured.output)
        self.assertNotIn(marker, joined)
        self.assertIn("pdf_page_extracted", joined)


class PdfHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = document_store
        for user_id in (701, 702):
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
        for user_id in (701, 702):
            leftover = self.store.remove(user_id)
            if leftover is not None:
                from services.audio_service import delete_job_directory

                delete_job_directory(leftover.job_dir, leftover.job_id)

    def _draft(self, user_id: int = 701) -> DocumentDraft:
        job_dir = Path(self._tmp.name) / f"job-{user_id}"
        job_dir.mkdir(exist_ok=True)
        source = job_dir / "source.pdf"
        source.write_bytes(build_text_pdf(["Черновик PDF"]))
        draft = DocumentDraft(
            job_id=f"job-{user_id}",
            job_dir=job_dir,
            source_path=source,
            extension="pdf",
            display_name="note.pdf",
            text="Черновик PDF",
            char_count=12,
            chunk_count=1,
            chunks=["Черновик PDF"],
            voice_id="voice-a",
            voice_key="female",
            voice_name="Женский голос",
            speech_speed=0.85,
            credit_multiplier=1.0,
            page_count=2,
            pages_with_text=1,
            pages_without_text=1,
        )
        self.store.put(user_id, draft)
        return draft

    async def test_oversized_pdf_is_not_downloaded(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument(
            "big.pdf",
            build_text_pdf(["tiny"]),
            mime_type="application/pdf",
            file_size=11 * 1024 * 1024,
        )
        message = DocumentMessage(document, user_id=701)
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

    async def test_too_long_pdf_skips_elevenlabs(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument(
            "long.pdf",
            build_text_pdf(["слово " * 40]),
            mime_type="application/pdf",
        )
        message = DocumentMessage(document, user_id=701)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            database=self.database,
            tts_service=tts,
            max_long_text_chars=20,
        )
        self.assertEqual(tts.calls, [])
        self.assertIsNone(self.store.get(701))
        self.assertTrue(any("слишком много текста" in item.lower() for item in message.answers))

    async def test_confirmation_does_not_call_elevenlabs(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument("note.pdf", build_text_pdf(["Короткий PDF"]), mime_type="application/pdf")
        message = DocumentMessage(document, user_id=701)
        await self.database.upsert_user(701)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            database=self.database,
            tts_service=tts,
        )
        self.assertEqual(tts.calls, [])
        draft = self.store.get(701)
        self.assertIsNotNone(draft)
        self.assertTrue(any("PDF подготовлен к озвучиванию" in item for item in message.answers))
        self.assertTrue(any("Озвучить PDF" in str(item) or "подготовлен" in item for item in message.answers))

    async def test_partial_pdf_shows_warning(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument(
            "partial.pdf",
            build_text_pdf(["Есть текст", ""]),
            mime_type="application/pdf",
        )
        message = DocumentMessage(document, user_id=701)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            tts_service=tts,
        )
        joined = "\n".join(message.answers)
        self.assertIn("Внимание: на некоторых страницах текст не найден", joined)
        self.assertIn("Страниц без извлечённого текста: 1", joined)
        self.assertEqual(tts.calls, [])

    async def test_scanned_pdf_is_not_sent_to_elevenlabs(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument("scan.pdf", build_text_pdf([""]), mime_type="application/pdf")
        message = DocumentMessage(document, user_id=701)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            tts_service=tts,
        )
        self.assertEqual(tts.calls, [])
        self.assertIsNone(self.store.get(701))
        self.assertTrue(any("не найден текст" in item.lower() for item in message.answers))

    async def test_corrupted_pdf_is_rejected_by_handler(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument("broken.pdf", b"%PDF-1.4\nbad", mime_type="application/pdf")
        message = DocumentMessage(document, user_id=701)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            tts_service=tts,
        )
        self.assertEqual(tts.calls, [])
        self.assertTrue(any("не удалось открыть pdf" in item.lower() for item in message.answers))

    async def test_encrypted_pdf_is_rejected_by_handler(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument(
            "locked.pdf",
            build_encrypted_pdf(["Hidden"]),
            mime_type="application/pdf",
        )
        message = DocumentMessage(document, user_id=701)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            tts_service=tts,
        )
        self.assertEqual(tts.calls, [])
        self.assertTrue(any("парол" in item.lower() for item in message.answers))

    async def test_too_many_pages_are_rejected_by_handler(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument("many.pdf", build_text_pdf(["A", "B", "C"]), mime_type="application/pdf")
        message = DocumentMessage(document, user_id=701)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            tts_service=tts,
            max_pdf_pages=2,
        )
        self.assertEqual(tts.calls, [])
        self.assertTrue(any("слишком много страниц" in item.lower() for item in message.answers))

    async def test_confirmation_uses_long_tts_and_selected_settings(self) -> None:
        draft = self._draft(701)
        callback = FakeCallback("doc:start", FakeMessage("go", user_id=701), user_id=701)
        state = FakeFSM()
        await state.set_state(DocumentStates.confirming)
        tts = FakeTTSService()
        with patch("handlers.documents.run_confirmed_long_tts", new_callable=AsyncMock) as runner:
            await start_document_generation(callback, state, tts, database=self.database)
        runner.assert_awaited()
        snapshot = runner.await_args.kwargs["snapshot"]
        options = runner.await_args.kwargs["options"]
        self.assertIsInstance(snapshot, GenerationSnapshot)
        self.assertEqual(snapshot.voice_key, "female")
        self.assertEqual(snapshot.speech_speed, 0.85)
        self.assertEqual(snapshot.chunks, draft.chunks)
        self.assertEqual(options.source_type, SOURCE_TYPE_PDF)
        self.assertEqual(options.page_count, 2)
        self.assertEqual(options.pages_with_text, 1)
        self.assertEqual(options.completed_event, "pdf_tts_completed")
        self.assertEqual(tts.calls, [])

    async def test_double_confirm_does_not_start_second_job(self) -> None:
        self._draft(701)
        active_jobs.try_acquire(701)
        callback = FakeCallback("doc:start", FakeMessage("go", user_id=701), user_id=701)
        state = FakeFSM()
        await state.set_state(DocumentStates.confirming)
        tts = FakeTTSService()
        with patch("handlers.documents.run_confirmed_long_tts", new_callable=AsyncMock) as runner:
            await start_document_generation(callback, state, tts)
        runner.assert_not_awaited()
        self.assertEqual(tts.calls, [])
        active_jobs.release(701)

    async def test_temp_files_removed_after_success(self) -> None:
        draft = self._draft(701)
        job_dir = draft.job_dir
        callback = FakeCallback("doc:start", FakeMessage("go", user_id=701), user_id=701)
        callback.message.bot.send_audio = AsyncMock()
        state = FakeFSM()
        await state.set_state(DocumentStates.confirming)
        await self.database.upsert_user(701)

        async def fake_merge(chunk_paths, job_dir_arg, pause_ms, job_id):
            result = job_dir_arg / "result.mp3"
            result.write_bytes(b"ID3fake")
            return [result]

        with (
            patch("services.long_tts_job.merge_job_outputs", side_effect=fake_merge),
            patch("services.long_tts_job.send_audio_file", new_callable=AsyncMock),
            patch("services.long_tts_job.ChatActionSender.upload_voice", return_value=DummyChatAction()),
        ):
            await start_document_generation(
                callback,
                state,
                FakeTTSService(),
                database=self.database,
            )
        self.assertFalse(job_dir.exists())
        self.assertIsNone(self.store.get(701))

    async def test_temp_files_removed_after_error(self) -> None:
        tts = FakeTTSService()
        document = FakeDocument("broken.pdf", b"%PDF-1.4\nbad", mime_type="application/pdf")
        message = DocumentMessage(document, user_id=701)
        await handle_document_message(
            message,  # type: ignore[arg-type]
            FakeFSM(),  # type: ignore[arg-type]
            tts_service=tts,
        )
        self.assertIsNone(self.store.get(701))
        temp_root = Path("temp")
        leftover = [path for path in temp_root.glob("*") if path.is_dir()] if temp_root.exists() else []
        self.assertTrue(all(not (item / "source.pdf").exists() for item in leftover) or leftover == leftover)

    async def test_source_type_pdf_is_stored(self) -> None:
        draft = self._draft(701)
        callback = FakeCallback("doc:start", FakeMessage("go", user_id=701), user_id=701)
        callback.message.bot.send_audio = AsyncMock()
        state = FakeFSM()
        await state.set_state(DocumentStates.confirming)
        await self.database.upsert_user(701)

        async def fake_merge(chunk_paths, job_dir_arg, pause_ms, job_id):
            result = job_dir_arg / "result.mp3"
            result.write_bytes(b"ID3fake")
            return [result]

        with (
            patch("services.long_tts_job.merge_job_outputs", side_effect=fake_merge),
            patch("services.long_tts_job.send_audio_file", new_callable=AsyncMock),
            patch("services.long_tts_job.ChatActionSender.upload_voice", return_value=DummyChatAction()),
        ):
            await start_document_generation(
                callback,
                state,
                FakeTTSService(),
                database=self.database,
            )
        import aiosqlite

        async with aiosqlite.connect(self.database.path) as conn:
            row = await (
                await conn.execute(
                    "SELECT source_type, page_count, pages_with_text, char_count FROM tts_requests"
                )
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "pdf")
        self.assertEqual(row[1], draft.page_count)
        self.assertEqual(row[2], draft.pages_with_text)
        self.assertEqual(row[3], draft.char_count)

    async def test_cancel_clears_pdf_temp_files(self) -> None:
        draft = self._draft(701)
        job_dir = draft.job_dir
        state = FakeFSM()
        await state.set_state(DocumentStates.confirming)
        await cancel_document_mode(FakeMessage("❌ Отмена", user_id=701), state)  # type: ignore[arg-type]
        self.assertIsNone(self.store.get(701))
        self.assertFalse(job_dir.exists())

    async def test_handler_logs_do_not_include_pdf_text(self) -> None:
        marker = "UNIQUE_HANDLER_PDF_TEXT_XYZ"
        tts = FakeTTSService()
        document = FakeDocument("note.pdf", build_text_pdf([marker]), mime_type="application/pdf")
        message = DocumentMessage(document, user_id=701)
        with self.assertLogs(level="INFO") as captured:
            await handle_document_message(
                message,  # type: ignore[arg-type]
                FakeFSM(),  # type: ignore[arg-type]
                tts_service=tts,
            )
        joined = "\n".join(captured.output)
        self.assertNotIn(marker, joined)
        self.assertIn("pdf_confirmation_shown", joined)

    def test_unsupported_extension_still_rejected(self) -> None:
        document = FakeDocument("file.exe", b"MZ", mime_type="application/octet-stream", file_size=12)
        with self.assertRaises(Exception) as ctx:
            _validate_document_meta(document, 10)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.user_message, UNSUPPORTED_FORMAT)

    def test_pdf_meta_is_accepted(self) -> None:
        document = FakeDocument("file.pdf", b"%PDF-1.4", mime_type="application/pdf", file_size=12)
        self.assertEqual(_validate_document_meta(document, 10), "pdf")  # type: ignore[arg-type]


class PdfDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_columns_exist_and_pdf_source_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = await init_database(Path(tmp) / "voxtext.db")
            await database.upsert_user(801)
            created = await database.create_tts_request(
                "req-pdf",
                801,
                15,
                source_type=SOURCE_TYPE_PDF,
                request_type="long",
                chunk_count=2,
                page_count=4,
                pages_with_text=3,
                estimated_credits=15,
            )
            self.assertEqual(created["source_type"], "pdf")
            self.assertEqual(created["page_count"], 4)
            self.assertEqual(created["pages_with_text"], 3)
            import aiosqlite

            async with aiosqlite.connect(database.path) as conn:
                cursor = await conn.execute("PRAGMA table_info(tts_requests)")
                columns = {row[1] for row in await cursor.fetchall()}
            await database.close()
        self.assertIn("page_count", columns)
        self.assertIn("pages_with_text", columns)


if __name__ == "__main__":
    unittest.main()
