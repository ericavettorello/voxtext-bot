"""Полный экспорт пользовательских таблиц SQLite в CSV ZIP для Excel."""

from __future__ import annotations

import base64
import csv
import io
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from services.admin_export import (
    TELEGRAM_EXPORT_LIMIT_BYTES,
    AdminExportError,
    create_sqlite_backup,
    export_stamp,
)

FULL_CSV_FAILED = "Не удалось подготовить полный CSV-экспорт базы. Подробности записаны в журнал."
BLOB_SAFE_LIMIT_BYTES = 8 * 1024
CSV_SIZE_HEADROOM_BYTES = 1024 * 1024
ZIP_ARCHIVE_OVERHEAD_BYTES = 1024
ZIP_FILE_OVERHEAD_BYTES = 256
BINARY_OMITTED = "[BINARY DATA OMITTED]"
REDACTED_CELL = "[REDACTED]"
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_SECRET_MARKERS = ("password", "secret", "token", "api_key", "authorization")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_-]+$")
_TABLES_SQL = """
SELECT name
FROM sqlite_master
WHERE type = 'table'
  AND name NOT LIKE 'sqlite_%'
ORDER BY name
"""


class FullCsvExportError(AdminExportError):
    def __init__(self) -> None:
        super().__init__(FULL_CSV_FAILED)


@dataclass(frozen=True)
class TableCsvResult:
    table_name: str
    row_count: int
    csv_paths: tuple[Path, ...]


@dataclass(frozen=True)
class FullCsvExportResult:
    archives: tuple[Path, ...]
    tables: tuple[TableCsvResult, ...]
    total_rows: int
    csv_file_count: int
    info_path: Path
    created_at: datetime


def quote_ident(name: str) -> str:
    """Экранировать идентификатор SQLite двойными кавычками."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise FullCsvExportError
    return '"' + name.replace('"', '""') + '"'


def is_secret_column(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def csv_stem_for_table(name: str, index: int) -> str:
    if _SAFE_FILENAME.fullmatch(name):
        return name
    return f"table_{index:03d}"


def format_full_csv_cell(
    value: Any,
    column: str,
    blob_limit: int = BLOB_SAFE_LIMIT_BYTES,
) -> str:
    if is_secret_column(column):
        return REDACTED_CELL
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        if len(data) > blob_limit:
            return BINARY_OMITTED
        encoded = base64.b64encode(data).decode("ascii")
        return f"base64:{encoded}"
    text = str(value)
    stripped = text.lstrip(" \t")
    if stripped.startswith(_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def ensure_inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise FullCsvExportError
    return resolved


def list_user_tables(db_path: Path) -> list[str]:
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        return _list_user_tables(connection)
    except sqlite3.Error as exc:
        raise FullCsvExportError from exc
    finally:
        connection.close()


def _list_user_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(_TABLES_SQL).fetchall()
    names: list[str] = []
    for row in rows:
        name = row[0] if row else None
        if not isinstance(name, str) or not name or name.startswith("sqlite_"):
            continue
        names.append(name)
    return names


def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    quoted = quote_ident(table)
    try:
        rows = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
    except sqlite3.Error as exc:
        raise FullCsvExportError from exc
    return [str(row[1]) for row in rows if row and row[1] is not None]


def unique_csv_stem(table: str, index: int, used_stems: set[str]) -> str:
    stem = csv_stem_for_table(table, index)
    if stem in used_stems:
        stem = f"table_{index:03d}"
        suffix = index
        while stem in used_stems:
            suffix += 1
            stem = f"table_{suffix:03d}"
    used_stems.add(stem)
    return stem


def export_table_to_csv(
    snapshot_path: Path,
    table: str,
    csv_dir: Path,
    job_dir: Path,
    index: int,
    max_csv_bytes: int | None = None,
    blob_limit: int = BLOB_SAFE_LIMIT_BYTES,
    stem: str | None = None,
) -> TableCsvResult:
    limit = max_csv_bytes if max_csv_bytes is not None else _default_max_csv_bytes()
    csv_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(snapshot_path), timeout=30)
    try:
        columns = table_columns(connection, table)
        file_stem = stem or csv_stem_for_table(table, index)
        writer = _CsvPartWriter(csv_dir, job_dir, file_stem, limit, columns)
        try:
            cursor = _select_table_rows(connection, table)
            for row in cursor:
                cells = [
                    format_full_csv_cell(value, columns[offset] if offset < len(columns) else f"col_{offset}", blob_limit)
                    for offset, value in enumerate(row)
                ]
                writer.write_row(cells)
            writer.finalize()
        finally:
            writer.close()
        return TableCsvResult(table_name=table, row_count=writer.row_count, csv_paths=tuple(writer.paths))
    except FullCsvExportError:
        raise
    except sqlite3.Error as exc:
        raise FullCsvExportError from exc
    finally:
        connection.close()


def write_export_info(
    path: Path,
    created_at: datetime,
    tables: Sequence[TableCsvResult],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    total_rows = sum(item.row_count for item in tables)
    lines = [
        "VoxText database CSV export",
        "",
        f"Дата создания: {created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Количество таблиц: {len(tables)}",
        f"Общее количество строк: {total_rows}",
        "",
        "Таблицы:",
    ]
    if tables:
        for item in tables:
            lines.append(f"- {item.table_name}: {item.row_count}")
    else:
        lines.append("- (нет пользовательских таблиц)")
    split_items = [item for item in tables if len(item.csv_paths) > 1]
    if split_items:
        lines.append("")
        lines.append("Разделённые таблицы:")
        for item in split_items:
            names = ", ".join(path.name for path in item.csv_paths)
            lines.append(f"- {item.table_name}: {names}")
    lines.extend(
        [
            "",
            "Формат CSV:",
            "- Encoding: UTF-8 with BOM",
            "- Delimiter: semicolon",
            "- One database table per CSV file",
            "",
            "Примечание:",
            "CSV предназначен для просмотра данных в Excel.",
            "Для полного восстановления базы используйте резервную копию SQLite в формате DB.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def pack_csv_archives(
    csv_paths: Sequence[Path],
    info_path: Path,
    archive_dir: Path,
    job_dir: Path,
    created_at: datetime,
    max_zip_bytes: int = TELEGRAM_EXPORT_LIMIT_BYTES,
) -> list[Path]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = export_stamp(created_at)
    allowed = [ensure_inside(job_dir, path) for path in csv_paths if path.suffix.lower() == ".csv"]
    info = ensure_inside(job_dir, info_path)
    groups = _group_files_for_zip(allowed, info, max_zip_bytes)
    archives: list[Path] = []
    total = len(groups)
    for index, group in enumerate(groups, 1):
        if total == 1:
            name = f"voxtext_database_csv_{stamp}.zip"
        else:
            name = f"voxtext_database_csv_{stamp}_part_{index:03d}.zip"
        zip_path = ensure_inside(job_dir, archive_dir / name)
        _write_zip(zip_path, group, job_dir)
        if zip_path.stat().st_size > max_zip_bytes:
            raise FullCsvExportError
        archives.append(zip_path)
    return archives


def export_database_to_csv_archives(
    source_db: Path,
    job_dir: Path,
    *,
    created_at: datetime | None = None,
    max_zip_bytes: int = TELEGRAM_EXPORT_LIMIT_BYTES,
    max_csv_bytes: int | None = None,
    blob_limit: int = BLOB_SAFE_LIMIT_BYTES,
) -> FullCsvExportResult:
    """Снимок Backup API, затем CSV и ZIP из одной временной копии."""
    csv_dir = job_dir / "csv"
    archive_dir = job_dir / "archive"
    snapshot = job_dir / "database_snapshot.db"
    csv_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        create_sqlite_backup(source_db, snapshot)
        tables = list_user_tables(snapshot)
        results: list[TableCsvResult] = []
        used_stems: set[str] = set()
        for index, table in enumerate(tables, 1):
            result = export_table_to_csv(
                snapshot,
                table,
                csv_dir,
                job_dir,
                index,
                max_csv_bytes=max_csv_bytes,
                blob_limit=blob_limit,
                stem=unique_csv_stem(table, index, used_stems),
            )
            results.append(result)
        return finalize_csv_archives(
            results,
            csv_dir,
            archive_dir,
            job_dir,
            created_at or datetime.now(timezone.utc),
            max_zip_bytes=max_zip_bytes,
        )
    except FullCsvExportError:
        raise
    except AdminExportError as exc:
        raise FullCsvExportError from exc
    except (OSError, sqlite3.Error, UnicodeError, zipfile.BadZipFile) as exc:
        raise FullCsvExportError from exc


def finalize_csv_archives(
    tables: Sequence[TableCsvResult],
    csv_dir: Path,
    archive_dir: Path,
    job_dir: Path,
    created_at: datetime,
    max_zip_bytes: int = TELEGRAM_EXPORT_LIMIT_BYTES,
) -> FullCsvExportResult:
    info_path = ensure_inside(job_dir, csv_dir / "export_info.txt")
    write_export_info(info_path, created_at, tables)
    csv_paths = [path for item in tables for path in item.csv_paths]
    archives = pack_csv_archives(csv_paths, info_path, archive_dir, job_dir, created_at, max_zip_bytes)
    return FullCsvExportResult(
        archives=tuple(archives),
        tables=tuple(tables),
        total_rows=sum(item.row_count for item in tables),
        csv_file_count=len(csv_paths),
        info_path=info_path,
        created_at=created_at,
    )


def _default_max_csv_bytes() -> int:
    return max(1024, TELEGRAM_EXPORT_LIMIT_BYTES - CSV_SIZE_HEADROOM_BYTES)


def _select_table_rows(connection: sqlite3.Connection, table: str) -> sqlite3.Cursor:
    quoted = quote_ident(table)
    try:
        return connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid")
    except sqlite3.OperationalError:
        columns = table_columns(connection, table)
        if columns:
            order = quote_ident(columns[0])
            return connection.execute(f"SELECT * FROM {quoted} ORDER BY {order}")
        return connection.execute(f"SELECT * FROM {quoted}")


def _csv_row_size(row: Sequence[str]) -> int:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(row)
    return len(buffer.getvalue().encode("utf-8"))


def _group_files_for_zip(csv_paths: Sequence[Path], info_path: Path, max_zip_bytes: int) -> list[list[Path]]:
    info_cost = info_path.stat().st_size + ZIP_FILE_OVERHEAD_BYTES
    base = ZIP_ARCHIVE_OVERHEAD_BYTES + info_cost
    groups: list[list[Path]] = []
    current: list[Path] = []
    current_size = 0
    for path in csv_paths:
        cost = path.stat().st_size + ZIP_FILE_OVERHEAD_BYTES
        if current and current_size + cost + base > max_zip_bytes:
            groups.append(current)
            current = []
            current_size = 0
        current.append(path)
        current_size += cost
    if current or not groups:
        groups.append(current)
    return [[*group, info_path] for group in groups]


def _write_zip(zip_path: Path, files: Iterable[Path], job_dir: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            resolved = ensure_inside(job_dir, path)
            arcname = resolved.name
            if arcname != path.name or not _safe_zip_arcname(arcname):
                raise FullCsvExportError
            if resolved.suffix.lower() == ".db" or arcname.endswith(("-wal", "-shm")):
                raise FullCsvExportError
            archive.write(resolved, arcname=arcname)
    return zip_path


def _safe_zip_arcname(name: str) -> bool:
    if not name or name != Path(name).name:
        return False
    if any(item in name for item in ("/", "\\", "..")):
        return False
    if name in {".env", "database_snapshot.db"}:
        return False
    return name == "export_info.txt" or name.endswith(".csv")


class _CsvPartWriter:
    def __init__(
        self,
        csv_dir: Path,
        job_dir: Path,
        stem: str,
        max_bytes: int,
        headers: Sequence[str],
    ) -> None:
        self.csv_dir = csv_dir
        self.job_dir = job_dir
        self.stem = stem
        self.max_bytes = max(1, max_bytes)
        self.headers = list(headers)
        self.paths: list[Path] = []
        self.row_count = 0
        self._handle: Any = None
        self._writer: Any = None
        self._part = 0
        self._rows_in_part = 0

    def write_row(self, row: Sequence[str]) -> None:
        if self._handle is None:
            self._open(split=False)
        row_size = _csv_row_size(row)
        current = self._handle.tell()
        if self._rows_in_part > 0 and current + row_size > self.max_bytes:
            self.close()
            self._promote_first_part()
            self._open(split=True)
        self._writer.writerow(row)
        self._rows_in_part += 1
        self.row_count += 1

    def finalize(self) -> None:
        if self._handle is None and not self.paths:
            self._open(split=False)
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._writer = None

    def _open(self, split: bool) -> None:
        self._part += 1
        path = self._path_for_part(self._part, split=split or self._part > 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w", encoding="utf-8-sig", newline="")
        self._writer = csv.writer(self._handle, delimiter=";", lineterminator="\r\n")
        self._writer.writerow(self.headers)
        self._rows_in_part = 0
        self.paths.append(path)

    def _path_for_part(self, part: int, split: bool) -> Path:
        name = f"{self.stem}_part_{part:03d}.csv" if split else f"{self.stem}.csv"
        if "/" in name or "\\" in name or ".." in name:
            raise FullCsvExportError
        return ensure_inside(self.job_dir, self.csv_dir / name)

    def _promote_first_part(self) -> None:
        if self._part != 1 or not self.paths:
            return
        first = self.paths[0]
        if first.name != f"{self.stem}.csv":
            return
        renamed = self._path_for_part(1, split=True)
        first.rename(renamed)
        self.paths[0] = renamed
