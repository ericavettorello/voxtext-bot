"""Тесты полного CSV-экспорта базы для Excel. Без рабочей SQLite и без Telegram API."""

from __future__ import annotations

import csv
import io
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from database.db import SOURCE_TYPE_TEXT, init_database
from handlers.admin import (
    ACCESS_DENIED_TEXT,
    EXPORT_BUSY,
    FULL_CSV_CAPTION,
    FULL_CSV_CONFIRM_TEXT,
    FULL_CSV_FAILED,
    PANEL_TEXT,
    build_admin_keyboard,
    build_confirm_keyboard,
    on_db_backup,
    on_full_csv_ask,
    on_full_csv_export,
)
from services.admin_export import create_sqlite_backup, _run_sqlite_backup
from services.admin_full_csv import (
    BINARY_OMITTED,
    REDACTED_CELL,
    csv_stem_for_table,
    export_database_to_csv_archives,
    format_full_csv_cell,
    list_user_tables,
    quote_ident,
)
from services.admin_jobs import admin_export_lock
from test_admin import ADMIN_ID, OTHER_ID, AdminMessage
from test_long_text import FakeCallback
from test_text_handler import FakeUser


class CaptureAdminMessage(AdminMessage):
    async def answer_document(self, document=None, **kwargs: object):
        payload: dict = {"document": document, **kwargs}
        path = getattr(document, "path", None)
        if path:
            payload["bytes"] = Path(str(path)).read_bytes()
            payload["filename"] = getattr(document, "filename", None) or Path(str(path)).name
        self.document_calls.append(payload)
        return self


def _zip_names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.namelist()


def _zip_text(data: bytes, name: str, encoding: str = "utf-8") -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name).decode(encoding)


def _zip_bytes(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name)


class FullCsvButtonTests(unittest.TestCase):
    def test_admin_keyboard_uses_format_labels(self) -> None:
        labels = [row[0].text for row in build_admin_keyboard().inline_keyboard]
        self.assertIn("👥 Пользователи (.CSV)", labels)
        self.assertIn("🧾 Запросы (.CSV)", labels)
        self.assertIn("📦 Вся база (.CSV ZIP)", labels)
        self.assertIn("🗄 Резервная копия (.DB ZIP)", labels)

    def test_full_csv_confirm_button_text(self) -> None:
        markup = build_confirm_keyboard("admin:fullcsvok", ok_text="✅ Создать CSV-архив")
        self.assertEqual(markup.inline_keyboard[0][0].text, "✅ Создать CSV-архив")
        self.assertEqual(markup.inline_keyboard[1][0].text, "❌ Отмена")


class FullCsvCellTests(unittest.TestCase):
    def test_null_becomes_empty(self) -> None:
        self.assertEqual(format_full_csv_cell(None, "username"), "")

    def test_numbers_are_unchanged(self) -> None:
        self.assertEqual(format_full_csv_cell(42, "id"), "42")
        self.assertEqual(format_full_csv_cell(-7, "id"), "-7")
        self.assertEqual(format_full_csv_cell(1.5, "speech_speed"), "1.5")

    def test_formula_injection_after_lstrip(self) -> None:
        self.assertEqual(format_full_csv_cell("=CMD()", "note"), "'=CMD()")
        self.assertEqual(format_full_csv_cell("  =CMD()", "note"), "'  =CMD()")
        self.assertEqual(format_full_csv_cell("+1", "note"), "'+1")
        self.assertEqual(format_full_csv_cell("@ref", "note"), "'@ref")

    def test_secret_columns_are_redacted(self) -> None:
        self.assertEqual(format_full_csv_cell("value", "api_token"), REDACTED_CELL)
        self.assertEqual(format_full_csv_cell("value", "password_hash"), REDACTED_CELL)
        self.assertEqual(format_full_csv_cell("value", "user_secret"), REDACTED_CELL)
        self.assertEqual(format_full_csv_cell("value", "authorization"), REDACTED_CELL)
        self.assertEqual(format_full_csv_cell("keep", "note"), "keep")

    def test_blob_encoding_and_omission(self) -> None:
        self.assertEqual(format_full_csv_cell(b"\x01\x02", "payload"), "base64:AQI=")
        omitted = format_full_csv_cell(b"x" * 9000, "payload")
        self.assertEqual(omitted, BINARY_OMITTED)

    def test_unsafe_table_name_becomes_numbered_file(self) -> None:
        self.assertEqual(csv_stem_for_table("users", 1), "users")
        self.assertEqual(csv_stem_for_table("../users", 2), "table_002")
        self.assertEqual(csv_stem_for_table("weird name!", 3), "table_003")

    def test_quote_ident_escapes_quotes(self) -> None:
        self.assertEqual(quote_ident("users"), '"users"')
        self.assertEqual(quote_ident('we"ird'), '"we""ird"')


class FullCsvServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "voxtext.db"
        self.database = await init_database(self.db_path)
        await self.database.upsert_user(
            11,
            username="=SUM(A1)",
            first_name="Кириллица",
            last_name=None,
        )
        await self.database.create_tts_request("csv-1", 11, 8, source_type=SOURCE_TYPE_TEXT)
        await self._seed_extra_tables()
        self.job_dir = Path(self._tmp.name) / "job"
        self.job_dir.mkdir()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    async def _seed_extra_tables(self) -> None:
        async with self.database._lock:
            connection = await self.database.connect()
            await connection.execute("CREATE TABLE empty_demo (id INTEGER PRIMARY KEY, title TEXT)")
            await connection.execute(
                "CREATE TABLE secrets (id INTEGER PRIMARY KEY, api_token TEXT, password_hash TEXT, note TEXT)"
            )
            await connection.execute(
                "INSERT INTO secrets (api_token, password_hash, note) VALUES (?, ?, ?)",
                ("leaked-token", "leaked-password", "visible"),
            )
            await connection.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, payload BLOB)")
            await connection.execute("INSERT INTO blobs (payload) VALUES (?)", (b"\x01\x02",))
            await connection.execute("INSERT INTO blobs (payload) VALUES (?)", (b"x" * 9000,))
            await connection.execute(
                "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT, amount INTEGER, ratio REAL)"
            )
            await connection.execute(
                "INSERT INTO notes (body, amount, ratio) VALUES (?, ?, ?)",
                ("строка\nс переносом", 12, 0.5),
            )
            await connection.execute("CREATE VIEW users_view AS SELECT telegram_user_id FROM users")
            await connection.commit()

    def _export(self, **kwargs):
        return export_database_to_csv_archives(self.db_path, self.job_dir, **kwargs)

    async def test_backup_uses_sqlite_api_and_integrity_ok(self) -> None:
        dest = Path(self._tmp.name) / "copy.db"
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup) as backup_call:
            with patch("shutil.copy") as copy_file:
                with patch("shutil.copy2") as copy2_file:
                    create_sqlite_backup(self.db_path, dest)
        backup_call.assert_called()
        copy_file.assert_not_called()
        copy2_file.assert_not_called()
        copy = sqlite3.connect(dest)
        try:
            check = copy.execute("PRAGMA integrity_check").fetchone()
            self.assertEqual(str(check[0]).lower(), "ok")
        finally:
            copy.close()

    async def test_working_database_is_not_changed(self) -> None:
        before = await self.database.get_user_request_count(11)
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup):
            result = self._export()
        after = await self.database.get_user_request_count(11)
        self.assertEqual(before, after)
        await self.database.create_tts_request("csv-2", 11, 3)
        later = await self.database.get_user_request_count(11)
        self.assertEqual(later, before + 1)
        snapshot = sqlite3.connect(self.job_dir / "database_snapshot.db")
        try:
            snapshot_count = snapshot.execute("SELECT COUNT(*) FROM tts_requests").fetchone()[0]
        finally:
            snapshot.close()
        self.assertEqual(snapshot_count, before)
        self.assertTrue(result.archives)

    async def test_user_tables_are_found_and_internal_skipped(self) -> None:
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup):
            result = self._export()
        names = [item.table_name for item in result.tables]
        self.assertIn("users", names)
        self.assertIn("tts_requests", names)
        self.assertIn("voices", names)
        self.assertIn("empty_demo", names)
        self.assertNotIn("sqlite_sequence", names)
        self.assertFalse(any(name.startswith("sqlite_") for name in names))
        self.assertNotIn("users_view", names)
        csv_names = [path.name for item in result.tables for path in item.csv_paths]
        self.assertEqual(len(csv_names), len(set(csv_names)))
        self.assertTrue(all(name.endswith(".csv") for name in csv_names))

    async def test_each_table_has_csv_and_empty_has_headers(self) -> None:
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup):
            result = self._export()
        data = result.archives[0].read_bytes()
        names = _zip_names(data)
        self.assertIn("users.csv", names)
        self.assertIn("tts_requests.csv", names)
        self.assertIn("voices.csv", names)
        self.assertIn("empty_demo.csv", names)
        self.assertIn("export_info.txt", names)
        empty = _zip_bytes(data, "empty_demo.csv")
        self.assertTrue(empty.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(empty.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0], ["id", "title"])
        self.assertEqual(len(rows), 1)

    async def test_csv_format_cyrillic_null_newline_formula_secret_blob(self) -> None:
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup):
            result = self._export()
        data = result.archives[0].read_bytes()
        users = _zip_bytes(data, "users.csv")
        self.assertTrue(users.startswith(b"\xef\xbb\xbf"))
        text = users.decode("utf-8-sig")
        self.assertIn(";", text.splitlines()[0])
        self.assertIn("Кириллица", text)
        self.assertIn("'=SUM(A1)", text)
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        row = next(reader)
        self.assertEqual(row["last_name"], "")
        notes = _zip_text(data, "notes.csv", "utf-8-sig")
        notes_reader = csv.DictReader(io.StringIO(notes), delimiter=";")
        note_row = next(notes_reader)
        self.assertEqual(note_row["body"], "строка\nс переносом")
        self.assertEqual(note_row["amount"], "12")
        self.assertEqual(note_row["ratio"], "0.5")
        secrets = _zip_text(data, "secrets.csv", "utf-8-sig")
        self.assertIn("api_token", secrets.splitlines()[0])
        self.assertIn(REDACTED_CELL, secrets)
        self.assertNotIn("leaked-token", secrets)
        self.assertNotIn("leaked-password", secrets)
        self.assertIn("visible", secrets)
        blobs = _zip_text(data, "blobs.csv", "utf-8-sig")
        self.assertIn("base64:AQI=", blobs)
        self.assertIn(BINARY_OMITTED, blobs)

    async def test_zip_contains_only_csv_and_info(self) -> None:
        env_path = self.job_dir / ".env"
        env_path.write_text("TELEGRAM_BOT_TOKEN=secret-value", encoding="utf-8")
        (self.job_dir / "voxtext.log").write_text("log-secret", encoding="utf-8")
        (self.job_dir / "dummy.db-wal").write_bytes(b"wal")
        (self.job_dir / "dummy.db-shm").write_bytes(b"shm")
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup):
            result = self._export()
        data = result.archives[0].read_bytes()
        names = _zip_names(data)
        self.assertTrue(all(name.endswith(".csv") or name == "export_info.txt" for name in names))
        self.assertNotIn(".env", names)
        self.assertFalse(any(name.endswith(".db") for name in names))
        self.assertFalse(any(name.endswith(("-wal", "-shm")) for name in names))
        self.assertFalse(any("log" in name.lower() for name in names))
        packed = b"".join(_zip_bytes(data, name) for name in names)
        self.assertNotIn(b"secret-value", packed)
        self.assertNotIn(b"TELEGRAM_BOT_TOKEN", packed)
        info = _zip_text(data, "export_info.txt")
        self.assertIn(f"Количество таблиц: {len(result.tables)}", info)
        self.assertIn(f"Общее количество строк: {result.total_rows}", info)
        self.assertIn("- users:", info)
        self.assertIn("- empty_demo: 0", info)
        self.assertNotIn(str(self.db_path), info)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", info)
        self.assertNotIn(str(self.job_dir), info)

    async def test_list_user_tables_matches_sqlite_master(self) -> None:
        tables = list_user_tables(self.db_path)
        raw = sqlite3.connect(self.db_path)
        try:
            expected = [
                row[0]
                for row in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
        finally:
            raw.close()
        self.assertEqual(tables, expected)


class FullCsvSplitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "voxtext.db"
        self.database = await init_database(self.db_path)
        await self.database.upsert_user(31, username="split")
        async with self.database._lock:
            connection = await self.database.connect()
            await connection.execute("CREATE TABLE bulky (id INTEGER PRIMARY KEY, body TEXT)")
            for index in range(12):
                await connection.execute(
                    "INSERT INTO bulky (body) VALUES (?)",
                    ("строка-" + ("Я" * 40) + f"-{index}",),
                )
            await connection.commit()
        self.job_dir = Path(self._tmp.name) / "job"
        self.job_dir.mkdir()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    async def test_large_csv_is_split_with_repeated_header(self) -> None:
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup):
            result = export_database_to_csv_archives(
                self.db_path,
                self.job_dir,
                max_csv_bytes=180,
                max_zip_bytes=10 * 1024 * 1024,
            )
        bulky = next(item for item in result.tables if item.table_name == "bulky")
        self.assertGreater(len(bulky.csv_paths), 1)
        for path in bulky.csv_paths:
            self.assertRegex(path.name, r"bulky_part_\d{3}\.csv")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
            self.assertEqual(rows[0], ["id", "body"])
            self.assertGreater(len(rows), 1)
        info = result.info_path.read_text(encoding="utf-8")
        self.assertIn("Разделённые таблицы:", info)
        self.assertIn("bulky_part_001.csv", info)

    async def test_oversized_archive_is_split_into_openable_zips(self) -> None:
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup):
            result = export_database_to_csv_archives(
                self.db_path,
                self.job_dir,
                created_at=datetime(2026, 9, 10, 18, 30, tzinfo=timezone.utc),
                max_csv_bytes=220,
                max_zip_bytes=2048,
            )
        self.assertGreater(len(result.archives), 1)
        for archive in result.archives:
            self.assertLessEqual(archive.stat().st_size, 2048)
            with zipfile.ZipFile(archive) as zipped:
                zipped.testzip()
                names = zipped.namelist()
                self.assertIn("export_info.txt", names)
                self.assertTrue(any(name.endswith(".csv") for name in names))
                self.assertTrue(all(name.endswith(".csv") or name == "export_info.txt" for name in names))


class FullCsvHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = await init_database(Path(self._tmp.name) / "voxtext.db")
        await self.database.upsert_user(ADMIN_ID, username="admin", first_name="Админ")
        self.temp_root = Path(self._tmp.name) / "temp"
        self.temp_root.mkdir()
        self.patcher = patch("services.admin_export.TEMP_DIR", self.temp_root)
        self.patcher.start()
        for user_id in list(admin_export_lock._user_ids):
            admin_export_lock.release(user_id)

    async def asyncTearDown(self) -> None:
        self.patcher.stop()
        await self.database.close()
        self._tmp.cleanup()
        for user_id in list(admin_export_lock._user_ids):
            admin_export_lock.release(user_id)

    def _leftover(self) -> list[Path]:
        root = self.temp_root / "admin_exports"
        if not root.exists():
            return []
        return [path for path in root.iterdir()]

    async def test_admin_can_run_full_csv_export(self) -> None:
        message = CaptureAdminMessage("go", user_id=ADMIN_ID)
        callback = FakeCallback("admin:fullcsvok", message, user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup):
            await on_full_csv_export(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertTrue(message.document_calls)
        sent = message.document_calls[0]
        self.assertIn(FULL_CSV_CAPTION, str(sent.get("caption", "")))
        names = _zip_names(sent["bytes"])
        self.assertIn("users.csv", names)
        self.assertIn("export_info.txt", names)
        self.assertTrue(any("Экспорт завершён." in item for item in message.answers))
        self.assertEqual(self._leftover(), [])
        self.assertNotIn(ADMIN_ID, admin_export_lock)

    async def test_regular_user_cannot_export(self) -> None:
        message = CaptureAdminMessage("go", user_id=OTHER_ID)
        callback = FakeCallback("admin:fullcsvok", message, user_id=OTHER_ID)
        callback.from_user = FakeUser(OTHER_ID)
        await on_full_csv_export(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertFalse(message.document_calls)
        self.assertTrue(any(ACCESS_DENIED_TEXT in item for item in message.answers))
        self.assertEqual(self._leftover(), [])

    async def test_confirmation_does_not_create_files(self) -> None:
        message = CaptureAdminMessage("go", user_id=ADMIN_ID)
        callback = FakeCallback("admin:fullcsv", message, user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        await on_full_csv_ask(callback, admin_ids={ADMIN_ID})
        self.assertTrue(any(FULL_CSV_CONFIRM_TEXT in item for item in message.answers))
        self.assertFalse(message.document_calls)
        self.assertEqual(self._leftover(), [])
        self.assertNotIn(ADMIN_ID, admin_export_lock)

    async def test_repeat_confirmation_does_not_start_second_export(self) -> None:
        admin_export_lock.try_acquire(ADMIN_ID)
        message = CaptureAdminMessage("go", user_id=ADMIN_ID)
        callback = FakeCallback("admin:fullcsvok", message, user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        await on_full_csv_export(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertTrue(any(EXPORT_BUSY in item for item in message.answers))
        self.assertFalse(message.document_calls)
        admin_export_lock.release(ADMIN_ID)

    async def test_temp_files_removed_after_error(self) -> None:
        message = CaptureAdminMessage("go", user_id=ADMIN_ID)
        callback = FakeCallback("admin:fullcsvok", message, user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        with patch("handlers.admin.create_sqlite_backup", side_effect=RuntimeError("boom")):
            await on_full_csv_export(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertEqual(self._leftover(), [])
        self.assertTrue(any(FULL_CSV_FAILED in item for item in message.answers))
        self.assertFalse(message.document_calls)
        self.assertNotIn(ADMIN_ID, admin_export_lock)

    async def test_database_zip_backup_still_works(self) -> None:
        message = CaptureAdminMessage("go", user_id=ADMIN_ID)
        callback = FakeCallback("admin:dbok", message, user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        await on_db_backup(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertTrue(message.document_calls)
        names = _zip_names(message.document_calls[0]["bytes"])
        self.assertEqual(names, ["voxtext_database.db"])
        self.assertEqual(self._leftover(), [])

    async def test_progress_uses_one_status_flow(self) -> None:
        message = CaptureAdminMessage("go", user_id=ADMIN_ID)
        callback = FakeCallback("admin:fullcsvok", message, user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup):
            await on_full_csv_export(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertIn("Создаю копию базы данных…", message.answers)
        self.assertTrue(any(item.startswith("Экспортирую таблицы:") for item in message.answers))
        self.assertIn("Создаю ZIP-архив…", message.answers)
        self.assertNotEqual(message.answers[0], PANEL_TEXT)
