"""Synthetic Noteastic backups (``.ntcbak``) for the tests, built with the standard library.

The schema holds the documented tables and columns (``docs/noteastic.md`` section 4.2: the ones
the MIT reader ``Canxinner/noteastic-mcp-public`` queries) with declared types of gnnote's own
choosing; real backups probably hold more tables and columns.  :class:`NtcbakBuilder` adds folders, notebooks, pages
and elements and returns the database (:meth:`NtcbakBuilder.database`) or the ZIP backup
(:meth:`NtcbakBuilder.backup`).

Run as a script, it writes a demo backup with pen strokes (with pressure), a highlighter, an
arrow, a filled shape, a text box, a PNG image and a PDF-backed page::

    python3 tests/ntcbak_builder.py OUT.ntcbak
"""
from __future__ import annotations

import io
import math
import sqlite3
import struct
import sys
import zipfile
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# The tables and columns of docs/noteastic.md section 4.2 (the ones the MIT reference reads; a
# real database probably has more).  The declared types are gnnote's own guess in the style of
# sqlite-net, the .NET library a UWP app like Noteastic would typically use: the real ones are
# unknown, and SQLite does not enforce them anyway.
TABLES: Dict[str, Tuple[str, ...]] = {
    "Folder": ("ID:key", "Name", "Color", "CreationDate"),
    "FolderFolderContent": ("FolderID:int", "ChildFolderID:int"),
    "Book": ("ID:key", "Name", "TagColor", "CreationDate", "LastEdited"),
    "FolderBookContent": ("FolderID:int", "ChildBookID:int"),
    "Page": ("ID:key", "GridID:int", "Width:real", "Height:real", "BackgroundColor", "CreationDate"),
    "BookContent": ("BookID:int", "PageIndex:int", "PageID:int"),
    "Grid": ("ID:key", "GridType", "IsDeleted:int"),
    "PDFGrid": ("GridID:int", "FilePath", "PageIndex:int", "Password"),
    "PageContent": ("PageID:int", "ElementID:int"),
    "Element": ("ID:key", "ElementType", "CreationDate", "M11:real", "M12:real", "M21:real", "M22:real", "M31:real",
                "M32:real", "IsDeleted:int"),
    "StrokeElement": ("ElementID:int", "StrokeWidth:real", "StrokeHeight:real", "Color", "IsHighlighter:int",
                      "IgnorePressure:int", "PointsData"),
    "LineElement": ("ElementID:int", "LineType", "X0:real", "Y0:real", "X1:real", "Y1:real", "StrokeColor",
                    "StrokeWidth:real", "StrokeDashed:int", "IsHighlighter:int"),
    "ShapeElement": ("ElementID:int", "ShapeType", "X0:real", "Y0:real", "X1:real", "Y1:real", "StrokeColor",
                     "StrokeWidth:real", "StrokeDashed:int", "FillColor"),
    "ImageElement": ("ElementID:int", "ImageWidth:real", "ImageHeight:real", "FilePath"),
    "TextElement": ("ElementID:int", "Text", "Width:real", "FontFamily", "FontSize:real", "Color",
                    "HorizontalTextAlignment", "Spans"),
}
_TYPES = {"key": "integer primary key", "int": "integer", "real": "float", "": "varchar"}


def schema_sql(tables: Dict[str, Tuple[str, ...]] = TABLES) -> str:
    """``CREATE TABLE`` statements for ``tables`` (``"Name:kind"`` columns; text by default)."""
    statements = []
    for table, columns in tables.items():
        defs = []
        for column in columns:
            name, _, kind = column.partition(":")
            defs.append(f'"{name}" {_TYPES[kind]}')
        statements.append(f'CREATE TABLE "{table}" ({", ".join(defs)});')
    return "\n".join(statements) + "\n"


SCHEMA = schema_sql()

A4_UNITS = (793.7, 1122.5)  # A4 in Windows effective pixels (96 per inch)
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
Matrix = Tuple[float, float, float, float, float, float]


def points_data(points: Iterable[Sequence[float]], header: Optional[str] = None) -> str:
    """``PointsData`` text: ``"<n>;x,y,p;..."`` (``header`` replaces the count)."""
    pts = list(points)
    body = ";".join(",".join(repr(float(v)) for v in p) for p in pts)
    return f"{len(pts) if header is None else header};{body}"


class NtcbakBuilder:
    """Builds a Noteastic library database (and backup ZIP) row by row."""

    def __init__(self, schema: str = SCHEMA):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(schema)
        self.files: Dict[str, bytes] = {}
        self._ids: Dict[str, int] = {}
        self._clock = 0
        self._page_index: Dict[int, int] = {}

    # -- helpers -------------------------------------------------------------------------

    def next_id(self, kind: str, start: int) -> int:
        self._ids[kind] = self._ids.get(kind, start - 1) + 1
        return self._ids[kind]

    def stamp(self) -> str:
        self._clock += 1
        return f"2026-01-01T10:{self._clock // 60:02d}:{self._clock % 60:02d}"

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.con.execute(sql, tuple(params))

    # -- library -------------------------------------------------------------------------

    def folder(self, name: str, parent: Optional[int] = None, ident: Optional[int] = None) -> int:
        fid = ident if ident is not None else self.next_id("folder", 1)
        self.execute("INSERT INTO Folder (ID, Name, Color, CreationDate) VALUES (?, ?, ?, ?)",
                     (fid, name, "#FF000000", self.stamp()))
        if parent is not None:
            self.execute("INSERT INTO FolderFolderContent VALUES (?, ?)", (parent, fid))
        return fid

    def book(self, name: str, folder: Optional[int] = None, ident: Optional[int] = None) -> int:
        bid = ident if ident is not None else self.next_id("book", 10)
        self.execute("INSERT INTO Book (ID, Name, TagColor, CreationDate, LastEdited) VALUES (?, ?, ?, ?, ?)",
                     (bid, name, "#FFFF0000", self.stamp(), self.stamp()))
        if folder is not None:
            self.execute("INSERT INTO FolderBookContent VALUES (?, ?)", (folder, bid))
        return bid

    def page(self, book: int, width: float = A4_UNITS[0], height: float = A4_UNITS[1], grid: str = "Blank",
             pdf: Optional[Tuple[str, int]] = None, background: str = "#FFFFFFFF", index: Optional[int] = None,
             password: Optional[str] = None) -> int:
        gid = self.next_id("grid", 100)
        self.execute("INSERT INTO Grid VALUES (?, ?, 0)", (gid, "PDF" if pdf is not None and grid == "Blank" else grid))
        if pdf is not None:
            self.execute("INSERT INTO PDFGrid VALUES (?, ?, ?, ?)", (gid, pdf[0], pdf[1], password))
        pid = self.next_id("page", 1000)
        self.execute("INSERT INTO Page (ID, GridID, Width, Height, BackgroundColor, CreationDate) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (pid, gid, width, height, background, self.stamp()))
        if index is None:
            index = self._page_index.get(book, 0)
        self._page_index[book] = index + 1
        self.execute("INSERT INTO BookContent VALUES (?, ?, ?)", (book, index, pid))
        return pid

    # -- elements ------------------------------------------------------------------------

    def element(self, page: Optional[int], element_type: Optional[str], matrix: Matrix = IDENTITY,
                deleted: bool = False, created: Optional[Any] = None, ident: Optional[int] = None) -> int:
        eid = ident if ident is not None else self.next_id("element", 5000)
        self.execute("INSERT INTO Element VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (eid, element_type, self.stamp() if created is None else created, *matrix, int(deleted)))
        if page is not None:
            self.execute("INSERT INTO PageContent VALUES (?, ?)", (page, eid))
        return eid

    def stroke(self, page: Optional[int], points: Iterable[Sequence[float]] = (), color: str = "#FF000000",
               width: float = 2.0, height: Optional[float] = None, highlighter: bool = False,
               ignore_pressure: bool = False, matrix: Matrix = IDENTITY, deleted: bool = False,
               raw: Optional[Any] = None, element_type: Optional[str] = None, created: Optional[Any] = None) -> int:
        etype = element_type if element_type is not None else ("Highlighter" if highlighter else "InkStroke")
        eid = self.element(page, etype, matrix, deleted, created)
        data = raw if raw is not None else points_data(points)
        self.execute("INSERT INTO StrokeElement VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (eid, width, width if height is None else height, color, int(highlighter), int(ignore_pressure),
                      data))
        return eid

    def line(self, page: int, x0: float, y0: float, x1: float, y1: float, line_type: str = "Line",
             color: str = "#FF000000", width: float = 2.0, dashed: bool = False, highlighter: bool = False,
             matrix: Matrix = IDENTITY) -> int:
        eid = self.element(page, "LineHighlighter" if highlighter else "Line", matrix)
        self.execute("INSERT INTO LineElement VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (eid, line_type, x0, y0, x1, y1, color, width, int(dashed), int(highlighter)))
        return eid

    def shape(self, page: int, shape_type: str, x0: float, y0: float, x1: float, y1: float,
              color: str = "#FF000000", width: float = 2.0, dashed: bool = False, fill: Optional[str] = None,
              matrix: Matrix = IDENTITY) -> int:
        eid = self.element(page, "Shape", matrix)
        self.execute("INSERT INTO ShapeElement VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (eid, shape_type, x0, y0, x1, y1, color, width, int(dashed), fill))
        return eid

    def image(self, page: int, path: str, data: Optional[bytes], width: float, height: float,
              matrix: Matrix = IDENTITY, archive_name: Optional[str] = None) -> int:
        eid = self.element(page, "Image", matrix)
        self.execute("INSERT INTO ImageElement VALUES (?, ?, ?, ?)", (eid, width, height, path))
        if data is not None:
            self.files[archive_name or path.replace("\\", "/")] = data
        return eid

    def text(self, page: int, text: str, width: float = 300.0, font: str = "Segoe UI", size: float = 16.0,
             color: str = "#FF000000", align: Any = "Left", spans: Optional[str] = None,
             matrix: Matrix = IDENTITY) -> int:
        eid = self.element(page, "Text", matrix)
        self.execute("INSERT INTO TextElement VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (eid, text, width, font, size, color, align, spans))
        return eid

    def add_file(self, name: str, data: bytes) -> None:
        self.files[name] = data

    # -- output --------------------------------------------------------------------------

    def database(self) -> bytes:
        self.con.commit()
        return self.con.serialize()

    def backup(self, db_name: str = "noteastic.db", extra: Optional[Dict[str, bytes]] = None) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(db_name, self.database())
            for name, data in {**self.files, **(extra or {})}.items():
                zf.writestr(name, data)
        return buf.getvalue()


# --------------------------------------------------------------------------- small test assets


def png_bytes(width: int = 64, height: int = 48, rgb: Tuple[int, int, int] = (40, 120, 200)) -> bytes:
    """An RGB PNG with a diagonal gradient (stdlib only)."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            t = (x + y) / float(width + height)
            row += bytes((int(rgb[0] * (1 - t) + 250 * t), int(rgb[1] * (1 - t) + 200 * t), int(rgb[2] * (1 - t) + 60 * t)))
        rows.append(bytes(row))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + \
        chunk(b"IEND", b"")


def pdf_bytes(pages: Sequence[Tuple[float, float]] = ((595.28, 841.89),), label: str = "Imported PDF",
              extras: Sequence[str] = ()) -> bytes:
    """A small PDF: each page has a blue frame, a heading and ruled lines (stdlib only).
    ``extras[i]`` is added to page ``i``'s dictionary (e.g. ``"/CropBox [50 50 550 750]"``)."""
    objects: List[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_ref = 3 + 2 * n
    for i, (w, h) in enumerate(pages):
        ops = [f"0.2 0.4 0.8 RG 4 w 20 20 {w - 40:.2f} {h - 40:.2f} re S",
               f"BT /F1 28 Tf 0.1 0.2 0.5 rg 60 {h - 90:.2f} Td ({label} page {i + 1}) Tj ET",
               "0.6 0.6 0.6 RG 0.8 w"]
        y = h - 140
        while y > 60:
            ops.append(f"60 {y:.2f} m {w - 60:.2f} {y:.2f} l S")
            y -= 36
        content = "\n".join(ops).encode()
        extra = extras[i] if i < len(extras) else ""
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.2f} {h:.2f}] {extra} /Resources << /Font << "
                       f"/F1 {font_ref} 0 R >> >> /Contents {4 + 2 * i} 0 R >>".encode())
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def handwriting(x0: float, y0: float, width: float, height: float, waves: int = 5, n: int = 120,
                pressure: bool = True) -> List[Tuple[float, float, float]]:
    """A row of cursive loops ("llll") with varying pressure (a prolate cycloid)."""
    pts = []
    loop = 0.25 * width / waves  # loop radius: wider than the advance per radian, so it loops
    for i in range(n):
        t = i / (n - 1)
        theta = 2 * math.pi * waves * t
        x = x0 + width * t - loop * math.sin(theta)
        y = y0 + 0.5 * height * math.cos(theta)
        p = 0.25 + 0.6 * math.sin(math.pi * t) if pressure else 0.5
        pts.append((round(x, 3), round(y, 3), round(p, 3)))
    return pts


def demo_builder() -> NtcbakBuilder:
    """The demo library: one folder, one notebook, a paper page and a PDF-backed page."""
    b = NtcbakBuilder()
    folder = b.folder("gnnote demo")
    book = b.book("Demo notebook", folder)
    p1 = b.page(book, grid="Lines")
    b.stroke(p1, handwriting(80, 140, 520, 40), color="#FF1A1A1A", width=2.5)
    b.stroke(p1, handwriting(80, 230, 420, 50, waves=4), color="#FFD32F2F", width=3.0)
    b.stroke(p1, [(80 + 12 * i, 320 + (18 if i % 2 else -18), 0.5) for i in range(30)], color="#FF1565C0",
             width=1.5, ignore_pressure=True)
    b.stroke(p1, [(70, 140, 0.5), (620, 140, 0.5)], color="#FFFFEB3B", width=6.0, height=18.0, highlighter=True,
             ignore_pressure=True)
    b.line(p1, 80, 420, 360, 420, line_type="SingleArrow", color="#FF2E7D32", width=3.0)
    b.line(p1, 420, 420, 700, 470, line_type="DoubleArrow", color="#FF6A1B9A", width=2.0, dashed=True)
    b.shape(p1, "Ellipse", 80, 500, 300, 640, color="#FF00897B", width=3.0, fill="#6600BFA5")
    b.shape(p1, "Rectangle", 340, 500, 520, 640, color="#FFEF6C00", width=2.0)
    b.shape(p1, "Triangle", 560, 500, 720, 640, color="#FF5D4037", width=2.0, fill="#FFFFCCBC")
    b.text(p1, "Hello from Noteastic\nsecond line", width=360, size=24, color="#FF0D47A1",
           matrix=(1, 0, 0, 1, 80, 700))
    b.image(p1, "Images\\gradient.png", png_bytes(), 64, 48, matrix=(3, 0, 0, 3, 440, 700))
    b.stroke(p1, handwriting(100, 980, 300, 30), deleted=True)  # erased: must not appear
    pdf = pdf_bytes()
    b.add_file("PDFs/demo.pdf", pdf)
    p2 = b.page(book, pdf=("PDFs\\demo.pdf", 0))
    # a circle around the PDF's heading (60..330 x 70..90 pt = 80..440 x 93..120 units)
    b.stroke(p2, [(260 + 220 * math.cos(2 * math.pi * i / 60), 107 + 45 * math.sin(2 * math.pi * i / 60),
                   0.5 + 0.3 * math.sin(i / 5)) for i in range(61)], color="#FFE53935", width=3.0)
    b.stroke(p2, handwriting(100, 400, 500, 30), color="#FF283593", width=2.0)
    return b


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 tests/ntcbak_builder.py OUT.ntcbak", file=sys.stderr)
        return 2
    with open(argv[1], "wb") as fh:
        fh.write(demo_builder().backup())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
