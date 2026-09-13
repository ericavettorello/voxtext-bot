"""Тесты административной панели: доступ, статистика, CSV, backup и логи."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import ConfigError, parse_admin_ids
from database.db import SOURCE_TYPE_PDF, SOURCE_TYPE_TEXT, init_database
from handlers.admin import (
    ACCESS_DENIED_TEXT,
    EXPORT_BUSY,
    PANEL_TEXT,
    UNKNOWN_CALLBACK,
    cmd_admin,
    cmd_stats,
    on_stats,
    on_unknown_admin,
    on_users_csv,
    on_db_backup,
    on_log_export,
)
from services.admin_access import is_admin
from services.admin_export import (
    REDACTED,
    REQUESTS_CSV_HEADERS,
    SecretRedactor,
    TELEGRAM_EXPORT_LIMIT_BYTES,
    USERS_CSV_HEADERS,
    ExportTooLargeError,
    assert_export_size,
    copy_current_log,
    create_sqlite_backup,
    csv_safe,
    requests_csv_rows,
    users_csv_rows,
    write_csv,
    zip_database_copy,
    zip_redacted_logs,
    _run_sqlite_backup,
)
from services.admin_jobs import admin_export_lock
from services.admin_stats import PERIOD_7D, PERIOD_TODAY, format_overview, period_start
from test_long_text import FakeCallback
from test_text_handler import FakeMessage, FakeUser


ADMIN_ID = 9001
OTHER_ID = 9002


class AdminMessage(FakeMessage):
    def __init__(self, text: str, user_id: int = ADMIN_ID) -> None:
        super().__init__(text, user_id=user_id)
        self.document_calls: list[dict] = []
        self.from_user = FakeUser(user_id)

    async def answer_document(self, document=None, **kwargs: object):
        self.document_calls.append({"document": document, **kwargs})
        return self


class AdminAccessTests(unittest.TestCase):
    def test_parse_admin_ids_supports_multiple_values(self) -> None:
        self.assertEqual(parse_admin_ids("123, 456,,789"), {123, 456, 789})
        self.assertEqual(parse_admin_ids("  "), set())
        self.assertEqual(parse_admin_ids(None), set())

    def test_invalid_admin_ids_raise_config_error(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse_admin_ids("12a")
        self.assertNotIn("12a", str(ctx.exception))
        with self.assertRaises(ConfigError):
            parse_admin_ids("-5")
        with self.assertRaises(ConfigError):
            parse_admin_ids("0")

    def test_is_admin(self) -> None:
        self.assertTrue(is_admin(1, {1, 2}))
        self.assertFalse(is_admin(3, {1, 2}))
        self.assertFalse(is_admin(1, set()))
        self.assertFalse(is_admin(None, {1}))


class AdminHandlerAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_can_open_panel(self) -> None:
        message = AdminMessage("/admin", user_id=ADMIN_ID)
        await cmd_admin(message, admin_ids={ADMIN_ID})
        self.assertEqual(message.answers[0], PANEL_TEXT)

    async def test_regular_user_cannot_open_admin(self) -> None:
        message = AdminMessage("/admin", user_id=OTHER_ID)
        await cmd_admin(message, admin_ids={ADMIN_ID})
        self.assertEqual(message.answers[-1], ACCESS_DENIED_TEXT)
        self.assertNotIn(PANEL_TEXT, message.answers)

    async def test_callback_checks_admin_each_time(self) -> None:
        callback = FakeCallback("admin:stats", AdminMessage("x", user_id=OTHER_ID), user_id=OTHER_ID)
        await on_stats(callback, admin_ids={ADMIN_ID})
        self.assertTrue(any(ACCESS_DENIED_TEXT in item for item in callback.message.answers))

    async def test_unknown_callback_is_rejected(self) -> None:
        callback = FakeCallback("admin:drop-table", AdminMessage("x"), user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        await on_unknown_admin(callback, admin_ids={ADMIN_ID})
        self.assertTrue(any(UNKNOWN_CALLBACK in item for item in callback.message.answers))


class AdminStatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = await init_database(Path(self._tmp.name) / "voxtext.db")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.now = now
        await self.database.upsert_user(1, username="alice", first_name="Анна")
        await self.database.upsert_user(2, username="=cmd", first_name="Борис")
        await self.database.create_tts_request(
            "r1", 1, 10, source_type=SOURCE_TYPE_TEXT, voice_key="default", speech_speed=1.0
        )
        await self.database.mark_tts_request_success("r1", 100, 10, estimated_credits=10, estimated_cost_usd=None)
        await self.database.create_tts_request(
            "r2", 1, 20, source_type=SOURCE_TYPE_PDF, voice_key="female", speech_speed=0.85
        )
        await self.database.mark_tts_request_success("r2", 100, 10, estimated_credits=20)
        await self.database.create_tts_request("r3", 2, 5, source_type=SOURCE_TYPE_TEXT, voice_key="default")
        await self.database.mark_tts_request_failed("r3", "timeout")

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    async def test_overview_counts_and_unique_active_users(self) -> None:
        stats = await self.database.get_admin_overview_statistics(now=self.now)
        self.assertEqual(stats["users_total"], 2)
        self.assertEqual(stats["requests_total"], 3)
        self.assertEqual(stats["success_total"], 2)
        self.assertEqual(stats["failed_total"], 1)
        self.assertEqual(stats["source_text"], 2)
        self.assertEqual(stats["source_pdf"], 1)
        self.assertEqual(stats["active_users_24h"], 2)
        self.assertEqual(stats["active_users_7d"], 2)
        text = format_overview(stats)
        self.assertIn("Статистика VoxText", text)
        self.assertIn("ориентировочно использовано кредитов", text)

    async def test_empty_database_is_safe(self) -> None:
        empty_dir = tempfile.TemporaryDirectory()
        empty = await init_database(Path(empty_dir.name) / "empty.db")
        stats = await empty.get_admin_overview_statistics()
        await empty.close()
        empty_dir.cleanup()
        self.assertEqual(stats["users_total"], 0)
        self.assertEqual(stats["requests_total"], 0)
        self.assertIsNone(stats["popular_voice_name"])
        self.assertIsNone(stats["popular_speed"])
        self.assertIsNone(stats["estimated_cost_usd"])

    async def test_period_uses_utc_bounds(self) -> None:
        old = (self.now - timedelta(days=10)).isoformat()
        async with self.database._lock:
            connection = await self.database.connect()
            await connection.execute(
                "UPDATE users SET created_at = ? WHERE telegram_user_id = 2",
                (old,),
            )
            await connection.execute(
                "UPDATE tts_requests SET created_at = ? WHERE request_id = 'r3'",
                (old,),
            )
            await connection.commit()
        # Fixtures are inserted after self.now is captured; the period upper bound
        # must be at or after those created_at values.
        query_now = self.now + timedelta(seconds=5)
        start = period_start(PERIOD_7D, query_now)
        stats = await self.database.get_admin_period_statistics(
            start.isoformat() if start else None, now=query_now
        )
        self.assertEqual(stats["new_users"], 1)
        self.assertEqual(stats["active_users"], 1)
        self.assertEqual(stats["requests_total"], 2)

    async def test_stats_command_requires_admin(self) -> None:
        message = AdminMessage("/stats", user_id=OTHER_ID)
        await cmd_stats(message, database=self.database, admin_ids={ADMIN_ID})
        self.assertEqual(message.answers[-1], ACCESS_DENIED_TEXT)


class AdminCsvTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = await init_database(Path(self._tmp.name) / "voxtext.db")
        await self.database.upsert_user(11, username="=SUM(A1)", first_name="+Hack", last_name="@x")
        await self.database.create_tts_request("csv-1", 11, 8, source_type=SOURCE_TYPE_TEXT)
        await self.database.mark_tts_request_success("csv-1", 10, 1, estimated_credits=8)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    def test_formula_injection_is_escaped(self) -> None:
        self.assertEqual(csv_safe("=CMD()"), "'=CMD()")
        self.assertEqual(csv_safe("+1"), "'+1")
        self.assertEqual(csv_safe("-1"), "'-1")
        self.assertEqual(csv_safe("@ref"), "'@ref")
        self.assertEqual(csv_safe(None), "")

    async def test_users_csv_has_bom_and_semicolon(self) -> None:
        records = await self.database.fetch_users_export_rows()
        path = Path(self._tmp.name) / "users.csv"
        write_csv(path, USERS_CSV_HEADERS, users_csv_rows(records))
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        header = raw.decode("utf-8-sig").splitlines()[0]
        self.assertIn(";", header)
        self.assertIn("telegram_user_id", header)
        self.assertNotIn("source_text", header)
        body = raw.decode("utf-8-sig")
        self.assertIn("'=SUM(A1)", body)
        self.assertIn("'+Hack", body)
        self.assertNotIn("USER_TTS_SECRET_TEXT", body)

    async def test_requests_csv_uses_period(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=20)).replace(microsecond=0).isoformat()
        await self.database.upsert_user(12, username="old")
        await self.database.create_tts_request("csv-old", 12, 3, source_type=SOURCE_TYPE_PDF)
        async with self.database._lock:
            connection = await self.database.connect()
            await connection.execute("UPDATE tts_requests SET created_at = ? WHERE request_id = 'csv-old'", (old,))
            await connection.commit()
        start = period_start(PERIOD_TODAY)
        recent = await self.database.fetch_requests_export_rows(start.isoformat() if start else None)
        all_rows = await self.database.fetch_requests_export_rows(None)
        self.assertEqual(len(recent), 1)
        self.assertEqual(len(all_rows), 2)
        path = Path(self._tmp.name) / "req.csv"
        write_csv(path, REQUESTS_CSV_HEADERS, requests_csv_rows(recent))
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("csv-1", text)
        self.assertNotIn("csv-old", text)
        self.assertNotIn("USER_TTS_SECRET_TEXT", text)


class AdminBackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "voxtext.db"
        self.database = await init_database(self.db_path)
        await self.database.upsert_user(21, username="backup")
        await self.database.create_tts_request("b1", 21, 4)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self._tmp.cleanup()

    async def test_backup_uses_sqlite_api_and_keeps_source(self) -> None:
        dest = Path(self._tmp.name) / "copy.db"
        with patch("services.admin_export._run_sqlite_backup", wraps=_run_sqlite_backup) as backup_call:
            create_sqlite_backup(self.db_path, dest)
        backup_call.assert_called()
        copy = sqlite3.connect(dest)
        try:
            check = copy.execute("PRAGMA integrity_check").fetchone()
            self.assertEqual(str(check[0]).lower(), "ok")
            before = await self.database.get_user_request_count(21)
            await self.database.create_tts_request("b2", 21, 7)
            after = await self.database.get_user_request_count(21)
            self.assertEqual(before, 1)
            self.assertEqual(after, 2)
            copy_count = copy.execute("SELECT COUNT(*) FROM tts_requests").fetchone()[0]
            self.assertEqual(copy_count, 1)
        finally:
            copy.close()

    async def test_zip_contains_only_database(self) -> None:
        dest = Path(self._tmp.name) / "copy.db"
        create_sqlite_backup(self.db_path, dest)
        zip_path = Path(self._tmp.name) / "db.zip"
        env_path = Path(self._tmp.name) / ".env"
        env_path.write_text("TELEGRAM_BOT_TOKEN=secret", encoding="utf-8")
        zip_database_copy(dest, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
        self.assertEqual(names, ["voxtext_database.db"])
        self.assertNotIn(".env", names)
        self.assertFalse(any(name.endswith("-wal") or name.endswith("-shm") for name in names))
        self.assertFalse(any("log" in name.lower() for name in names))


class AdminLogTests(unittest.TestCase):
    def test_redacts_secrets_and_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "voxtext.log"
            token = "123456789:AA" + ("B" * 33)
            key = "sk_test_elevenlabs_key_value"
            source.write_text(
                f"token={token}\nELEVENLABS_API_KEY={key}\nAuthorization: Bearer abc.def\nxi-api-key: zz\nplain\n",
                encoding="utf-8",
            )
            original = source.read_text(encoding="utf-8")
            dest = Path(tmp) / "clean.log"
            redactor = SecretRedactor([token, key])
            copy_current_log(source, dest, redactor)
            cleaned = dest.read_text(encoding="utf-8")
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertNotIn(token, cleaned)
            self.assertNotIn(key, cleaned)
            self.assertNotIn("Bearer abc.def", cleaned)
            self.assertIn("Authorization:", cleaned)
            self.assertIn(REDACTED, cleaned)
            self.assertIn("plain", cleaned)

    def test_archive_contains_only_redacted_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            current = log_dir / "voxtext.log"
            rotated = log_dir / "voxtext.log.1"
            current.write_text("TELEGRAM_BOT_TOKEN=111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n", encoding="utf-8")
            rotated.write_text("xi-api-key: leaked\n", encoding="utf-8")
            (log_dir / ".env").write_text("do-not-pack", encoding="utf-8")
            zip_path = log_dir / "logs.zip"
            redactor = SecretRedactor(["111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"])
            zip_redacted_logs([current, rotated], zip_path, redactor, log_dir / "work")
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())
                data = {name: archive.read(name).decode("utf-8") for name in names}
            self.assertEqual(names, {"voxtext.log", "voxtext.log.1"})
            self.assertTrue(all(REDACTED in body for body in data.values()))
            self.assertFalse(any("do-not-pack" in body for body in data.values()))
            self.assertEqual(current.read_text(encoding="utf-8").count("TELEGRAM_BOT_TOKEN=111"), 1)

    def test_oversized_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.bin"
            path.write_bytes(b"x")
            with patch.object(Path, "stat") as mocked:
                mocked.return_value = MagicMock(st_size=TELEGRAM_EXPORT_LIMIT_BYTES + 1)
                with self.assertRaises(ExportTooLargeError):
                    assert_export_size(path)


class AdminExportHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = await init_database(Path(self._tmp.name) / "voxtext.db")
        await self.database.upsert_user(ADMIN_ID, username="admin")
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

    async def test_users_export_sends_csv_and_cleans_temp(self) -> None:
        callback = FakeCallback("admin:users", AdminMessage("go", user_id=ADMIN_ID), user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        await on_users_csv(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertTrue(callback.message.document_calls)
        leftover = list((self.temp_root / "admin_exports").glob("*")) if (self.temp_root / "admin_exports").exists() else []
        self.assertEqual(leftover, [])

    async def test_repeat_export_is_blocked(self) -> None:
        admin_export_lock.try_acquire(ADMIN_ID)
        callback = FakeCallback("admin:users", AdminMessage("go", user_id=ADMIN_ID), user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        await on_users_csv(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertTrue(any(EXPORT_BUSY in item for item in callback.message.answers))
        self.assertFalse(callback.message.document_calls)
        admin_export_lock.release(ADMIN_ID)

    async def test_temp_files_removed_after_error(self) -> None:
        callback = FakeCallback("admin:users", AdminMessage("go", user_id=ADMIN_ID), user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        with patch("handlers.admin.write_csv", side_effect=RuntimeError("boom")):
            await on_users_csv(callback, database=self.database, admin_ids={ADMIN_ID})
        leftover = list((self.temp_root / "admin_exports").glob("*")) if (self.temp_root / "admin_exports").exists() else []
        self.assertEqual(leftover, [])
        self.assertTrue(any("Не удалось подготовить экспорт" in item for item in callback.message.answers))

    async def test_database_backup_handler_sends_zip(self) -> None:
        callback = FakeCallback("admin:dbok", AdminMessage("go", user_id=ADMIN_ID), user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        await on_db_backup(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertTrue(callback.message.document_calls)
        sent = callback.message.document_calls[0]["document"]
        self.assertTrue(str(getattr(sent, "filename", sent)).endswith(".zip") or hasattr(sent, "path"))

    async def test_current_log_export_uses_redactor(self) -> None:
        log_path = Path(self._tmp.name) / "voxtext.log"
        token = "123456789:AA" + ("C" * 33)
        log_path.write_text(f"secret {token}\n", encoding="utf-8")
        callback = FakeCallback("admin:logok", AdminMessage("go", user_id=ADMIN_ID), user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        await on_log_export(
            callback,
            database=self.database,
            admin_ids={ADMIN_ID},
            secret_redactor=SecretRedactor([token]),
            log_path=log_path,
        )
        self.assertTrue(callback.message.document_calls)
        self.assertIn(token, log_path.read_text(encoding="utf-8"))

    async def test_oversized_export_is_not_sent(self) -> None:
        callback = FakeCallback("admin:users", AdminMessage("go", user_id=ADMIN_ID), user_id=ADMIN_ID)
        callback.from_user = FakeUser(ADMIN_ID)
        with patch("handlers.admin.assert_export_size", side_effect=ExportTooLargeError()):
            await on_users_csv(callback, database=self.database, admin_ids={ADMIN_ID})
        self.assertFalse(callback.message.document_calls)
        self.assertTrue(any("превышает допустимый размер" in item for item in callback.message.answers))


if __name__ == "__main__":
    unittest.main()
