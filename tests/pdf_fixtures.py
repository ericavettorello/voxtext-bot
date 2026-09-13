"""Программно созданные PDF без персональных данных."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def write_text_pdf(path: Path, pages: list[str]) -> Path:
    path.write_bytes(build_text_pdf(pages))
    return path


def build_text_pdf(pages: list[str]) -> bytes:
    writer = PdfWriter()
    for page_text in pages:
        page = writer.add_blank_page(width=612, height=792)
        if page_text:
            _add_extractable_text(writer, page, page_text)
    return _writer_bytes(writer)


def build_encrypted_pdf(pages: list[str], password: str = "secret") -> bytes:
    reader = PdfReader(BytesIO(build_text_pdf(pages)))
    writer = PdfWriter()
    writer.append(reader)
    writer.encrypt(password)
    return _writer_bytes(writer)


def build_complex_stream_pdf(payload_size: int = 2048) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (x) Tj ET\n" + (b" " * payload_size))
    page[NameObject("/Contents")] = stream
    return _writer_bytes(writer)


def _add_extractable_text(writer: PdfWriter, page, text: str) -> None:
    unique: list[str] = []
    mapping: dict[str, int] = {}
    for char in text:
        if char not in mapping:
            if len(unique) >= 255:
                raise ValueError("Слишком много уникальных символов для тестового PDF.")
            unique.append(char)
            mapping[char] = len(unique)
    payload = bytes(mapping[char] for char in text)
    cmap_lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<00> <FF>",
        "endcodespacerange",
        f"{len(unique)} beginbfchar",
    ]
    for code, char in enumerate(unique, start=1):
        cmap_lines.append(f"<{code:02x}> <{ord(char):04x}>")
    cmap_lines += [
        "endbfchar",
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    tounicode = DecodedStreamObject()
    tounicode.set_data(("\n".join(cmap_lines) + "\n").encode("ascii"))
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/ToUnicode"): writer._add_object(tounicode),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    escaped = "".join(f"\\{byte:03o}" for byte in payload)
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = content


def _writer_bytes(writer: PdfWriter) -> bytes:
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
