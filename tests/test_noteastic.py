"""Noteastic (``.ntcbak`` library backups) reader and the SQLite helper it uses.

No backup made by the app is public, so every test builds its input with
``tests/ntcbak_builder.py`` from the documented schema (``docs/noteastic.md``).
"""
from __future__ import annotations

import gc
import io
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import weakref
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from gnnote import formats, readutil, sqliteutil
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.model import Document, Page
from gnnote.noteastic import reader as R
from gnnote.noteastic.reader import EXPERIMENTAL_WARNING, UNIT_PT, find_database, parse_points_data, read_ntcbak
from gnnote.notability.reader import read_note
from gnnote.pdf.reader import read_pdf
from gnnote.sqliteutil import TOO_LARGE, SqliteDatabase, SqliteError, SqliteTooComplex, SqliteUnavailable
from tests.ntcbak_builder import (A4_UNITS, NtcbakBuilder, demo_builder, handwriting, pdf_bytes, png_bytes,
                                  points_data)

ROOT = Path(__file__).resolve().parent.parent
A4_PT = (A4_UNITS[0] * UNIT_PT, A4_UNITS[1] * UNIT_PT)


def _one_page(**page) -> Tuple[NtcbakBuilder, int]:
    b = NtcbakBuilder()
    book = b.book("Book", b.folder("Folder"))
    return b, b.page(book, **page)


def _warned(doc: Document, *fragments: str) -> bool:
    return any(all(f in w for f in fragments) for w in doc.warnings)


def _xy(page: Page, index: int) -> List[Tuple[float, float]]:
    return [(round(p.x, 4), round(p.y, 4)) for p in page.strokes[index].points]


def _zip(members: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --------------------------------------------------------------------------- the demo backup


def test_demo_backup_maps_every_element_kind() -> None:
    doc = read_ntcbak(demo_builder().backup())
    assert doc.warnings[0] == EXPERIMENTAL_WARNING
    assert doc.title == "Demo notebook" and len(doc.pages) == 2
    p1, p2 = doc.pages
    assert (p1.width, p1.height) == pytest.approx(A4_PT) and p1.paper == "lined" and p1.background is None
    assert [s.kind for s in p1.strokes] == ["pen", "pen", "pen", "highlighter", "pen", "fill", "pen", "pen", "fill",
                                            "pen", "fill", "pen", "fill", "pen", "pen", "fill", "pen"]
    black, red, blue, marker = p1.strokes[:4]
    # pen widths: StrokeWidth x 0.75 pt, pressure p scales them by 1.5 p + 0.25
    first = handwriting(80, 140, 520, 40)[0]
    assert black.width == pytest.approx(2.5 * UNIT_PT)
    assert (black.points[0].x, black.points[0].y) == pytest.approx((first[0] * UNIT_PT, first[1] * UNIT_PT))
    assert black.points[0].width == pytest.approx(2.5 * UNIT_PT * (1.5 * first[2] + 0.25))
    assert black.color == pytest.approx((0x1A / 255, 0x1A / 255, 0x1A / 255, 1.0))
    assert red.color == pytest.approx((0xD3 / 255, 0x2F / 255, 0x2F / 255, 1.0)) and red.width == pytest.approx(2.25)
    assert {round(p.width, 6) for p in blue.points} == {1.125}  # IgnorePressure
    # a highlighter's rectangular tip is drawn round with its larger side
    assert marker.color == pytest.approx((1.0, 0xEB / 255, 0x3B / 255, 1.0)) and marker.width == pytest.approx(13.5)
    # the arrow: shaft, then the head as a fill and its outline
    shaft, head_fill, head = p1.strokes[4:7]
    assert _xy(p1, 4) == [(60.0, 315.0), (270.0, 315.0)] and shaft.controls is not None
    assert head_fill.outline is not None and head.points[0].x == pytest.approx(270.0)
    size = max(7.0, 3.0 * 5.0) * UNIT_PT
    assert head.points[1].x == pytest.approx(270.0 - math.cos(0.45) * size)
    assert head.points[1].y == pytest.approx(315.0 + math.sin(0.45) * size)
    # the filled ellipse: a fill under its outline (64 samples, closed)
    fill, ellipse = p1.strokes[12:14]
    assert fill.kind == "fill" and fill.color == pytest.approx((0, 0xBF / 255, 0xA5 / 255, 0x66 / 255))
    assert len(ellipse.points) == 65 and ellipse.controls is None
    assert ellipse.bbox() == pytest.approx((60.0, 375.0, 225.0, 480.0))
    assert p1.strokes[14].kind == "pen" and len(p1.strokes[14].points) == 5  # the rectangle
    assert p1.strokes[16].kind == "pen" and _xy(p1, 16)[:3] == [(480.0, 375.0), (540.0, 480.0), (420.0, 480.0)]
    image, = p1.images
    assert (image.x, image.y, image.w, image.h, image.fmt, image.rotation) == pytest.approx(
        (330.0, 525.0, 144.0, 108.0, "png", 0.0)) and image.data == png_bytes()
    text, = p1.texts
    assert text.text == "Hello from Noteastic\nsecond line" and text.runs[0].font == "Segoe UI"
    assert (text.x, text.y, text.w, text.h, text.size, text.align) == (60.0, 525.0, 270.0, 45.0, 18.0, "left")
    # the PDF page takes the PDF's size; ink is mapped through the app's fit-and-centre
    assert (p2.width, p2.height) == pytest.approx((595.28, 841.89)) and p2.background.pdf_id == "PDFs/demo.pdf"
    assert doc.pdfs["PDFs/demo.pdf"] == pdf_bytes()
    s = min(A4_UNITS[0] / 595.28, A4_UNITS[1] / 841.89)
    ox, oy = (A4_UNITS[0] - 595.28 * s) / 2, (A4_UNITS[1] - 841.89 * s) / 2
    circle = p2.strokes[0]
    assert (circle.points[0].x, circle.points[0].y) == pytest.approx(((480 - ox) / s, (107 - oy) / s))
    assert circle.width == pytest.approx(3.0 / s)
    # the erased stroke is not there; the warnings say what was approximated
    assert sum(len(p.strokes) for p in doc.pages) == 19
    assert _warned(doc, "Pen pressure of 4 stroke(s)") and _warned(doc, "5 line(s) and shape(s)")
    assert _warned(doc, "1 dashed") and _warned(doc, "lined, squared or dotted paper")
    assert not _warned(doc, "differs from the documented layout")


def test_builder_script_writes_the_demo_backup(tmp_path: Path) -> None:
    out = tmp_path / "demo.ntcbak"
    subprocess.run([sys.executable, str(ROOT / "tests" / "ntcbak_builder.py"), str(out)], check=True,
                   capture_output=True)
    doc = read_ntcbak(out.read_bytes())
    assert doc.title == "Demo notebook" and len(doc.pages) == 2 and len(doc.pages[0].images) == 1
    assert subprocess.run([sys.executable, str(ROOT / "tests" / "ntcbak_builder.py")],
                          capture_output=True).returncode == 2


@pytest.mark.parametrize("target", ["notability", "goodnotes", "pdf", "xournalpp"])
def test_demo_converts_and_reads_back(target: str) -> None:
    data = demo_builder().backup()
    doc = read_ntcbak(data)
    result = convert(data, "Demo.ntcbak", Options(target=target))
    assert result.source_format == "noteastic" and result.filename == "Demo." + formats.get(target).extension[1:]
    back = formats.get(target).read(result.data)
    assert len(back.pages) == 2
    pens = sum(1 for p in doc.pages for s in p.strokes if s.kind != "fill")
    if target == "notability":
        assert sum(len(p.strokes) for p in back.pages) == pens  # Notability has no fills
    elif target == "goodnotes":
        assert sum(1 for p in back.pages for s in p.strokes if s.kind != "fill") == pens
        assert back.pages[1].background is not None
    elif target == "pdf":
        assert back.pages[1].background is not None
    if target != "pdf":  # gnnote's PDF reader keeps images and text as page content
        assert len(back.pages[0].images) == 1


# --------------------------------------------------------------------------- geometry


def test_element_matrices_place_strokes_images_and_text() -> None:
    b, page = _one_page()
    rot = (0.0, 1.0, -1.0, 0.0, 400.0, 100.0)  # 90 degrees clockwise (y down), then moved
    b.stroke(page, [(0, 0, 0.5), (100, 0, 0.5), (100, 50, 0.5)], matrix=rot, width=4)
    b.stroke(page, [(10, 10, 0.5), (20, 10, 0.5)], matrix=(2.0, 0.0, 0.0, 2.0, 0.0, 0.0), width=4)
    b.image(page, "img.png", png_bytes(40, 20), 40, 20, matrix=rot)
    b.text(page, "turned", width=100, size=20, matrix=rot)
    b.line(page, 0, 0, 10, 0, matrix=(1.0, 0.0, 0.5, 1.0, 0.0, 0.0))
    doc = read_ntcbak(b.backup())
    p = doc.pages[0]
    # x' = M11 x + M21 y + M31, y' = M12 x + M22 y + M32
    assert _xy(p, 0) == [(300.0, 75.0), (300.0, 150.0), (262.5, 150.0)]
    assert p.strokes[0].width == pytest.approx(3.0)
    assert _xy(p, 1) == [(15.0, 15.0), (30.0, 15.0)] and p.strokes[1].width == pytest.approx(6.0)
    image = p.images[0]
    # the 40 x 20 picture turned by 90 degrees: its centre (20, 10) goes to (390, 120) units
    assert (image.x + image.w / 2, image.y + image.h / 2) == pytest.approx((390 * 0.75, 120 * 0.75))
    assert (image.w, image.h, image.rotation) == pytest.approx((30.0, 15.0, 90.0))
    text = p.texts[0]
    assert (text.x, text.y, text.rotation, text.size, text.w) == pytest.approx((300.0, 75.0, 90.0, 15.0, 75.0))
    assert _xy(p, 2) == [(0.0, 0.0), (7.5, 0.0)]  # the shear moves nothing on the x axis
    assert not _warned(doc, "skewed")


def test_skewed_mirrored_and_unusable_matrices() -> None:
    b, page = _one_page()
    b.image(page, "a.png", png_bytes(), 64, 48, matrix=(1.0, 0.0, 0.5, 1.0, 0.0, 0.0))
    b.text(page, "mirror", matrix=(-1.0, 0.0, 0.0, 1.0, 300.0, 0.0))
    b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)], matrix=(1e9, 0.0, 0.0, 1.0, 0.0, 0.0))
    b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)], matrix=(1.0, 0.0, 0.0, 1.0, 5e6, 0.0))
    b.stroke(page, [(2e6, 0, 0.5), (1, 1, 0.5)])
    eid = b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)])
    b.execute("UPDATE Element SET M11 = 'abc' WHERE ID = ?", (eid,))
    ok = b.stroke(page, [(0, 0, 0.5), (8, 8, 0.5)])
    b.execute("UPDATE Element SET M11 = NULL, M22 = NULL WHERE ID = ?", (ok,))  # missing -> identity
    doc = read_ntcbak(b.backup())
    assert len(doc.pages[0].images) == 1 and len(doc.pages[0].texts) == 1
    assert _warned(doc, "2 skewed or mirrored")
    assert len(doc.pages[0].strokes) == 1 and _xy(doc.pages[0], 0) == [(0.0, 0.0), (6.0, 6.0)]
    assert _warned(doc, "4 element(s) with unusable positions")


def test_pdf_page_fit_letterbox_and_page_problems() -> None:
    b = NtcbakBuilder()
    book = b.book("PDFs")
    b.add_file("PDFs/two.pdf", pdf_bytes(((600.0, 400.0), (300.0, 300.0))))
    wide = b.page(book, width=800, height=800, pdf=("PDFs\\two.pdf", 0))  # letterboxed: s = 4/3, oy = 133.3
    b.stroke(wide, [(0, 400, 0.5), (800, 400, 0.5)])
    b.page(book, width=300, height=300, pdf=("PDFs/two.pdf", 1))  # same shape: s = 1
    b.page(book, pdf=("PDFs/two.pdf", 5))  # no such PDF page
    b.page(book, pdf=("PDFs/missing.pdf", 0))
    b.add_file("PDFs/broken.pdf", b"%PDF-1.4 garbage")
    b.page(book, pdf=("PDFs/broken.pdf", 0))
    b.page(book, pdf=("", 0))
    b.page(book, width=300, height=300, pdf=("PDFs/two.pdf", 1), password="secret")
    gid = b.next_id("grid", 100)
    b.execute("INSERT INTO Grid VALUES (?, 'PDF', 0)", (gid,))
    b.execute("INSERT INTO Page VALUES (?, ?, 100, 200, '#FFFFFFFF', '')", (9999, gid))
    b.execute("INSERT INTO BookContent VALUES (?, 99, 9999)", (book,))
    doc = read_ntcbak(b.backup())
    pages = doc.pages
    assert [(round(p.width, 2), round(p.height, 2)) for p in pages[:2]] == [(600.0, 400.0), (300.0, 300.0)]
    assert [p.background.page_index if p.background else None for p in pages] == [0, 1, None, None, None, None, 1,
                                                                                  None]
    assert list(doc.pdfs) == ["PDFs/two.pdf"]
    s = 800 / 600
    assert _xy(pages[0], 0) == [(0.0, round((400 - (800 - 400 * s) / 2) / s, 4)), (600.0, 200.0)]
    assert (pages[2].width, pages[2].height) == pytest.approx(A4_PT)
    assert (pages[7].width, pages[7].height) == pytest.approx((75.0, 150.0))
    assert _warned(doc, "Page 3: the PDF page it shows does not exist in PDFs/two.pdf")
    assert _warned(doc, "Page 4: its PDF background PDFs/missing.pdf is missing")
    assert _warned(doc, "Page 5: its PDF background PDFs/broken.pdf is missing from the backup or unreadable")
    assert _warned(doc, "Page 6: its PDF background has no file name")
    assert _warned(doc, "Page 8: it is a PDF page without a PDF reference")
    assert _warned(doc, "1 page(s) show a password-protected PDF") and not _warned(doc, "secret")
    assert _warned(doc, "1 PDF page(s) have a different shape")


def test_mirrored_text_and_images_stay_upright_where_noteastic_shows_them() -> None:
    b, page = _one_page()
    b.text(page, "mirrored", width=300, size=16, matrix=(-1.0, 0.0, 0.0, 1.0, 300.0, 100.0))
    b.text(page, "flipped", width=300, size=16, matrix=(1.0, 0.0, 0.0, -1.0, 0.0, 300.0))
    b.text(page, "turned", width=300, size=16, matrix=(0.0, 1.0, -1.0, 0.0, 400.0, 0.0))  # 90 degrees, not mirrored
    b.image(page, "a.png", png_bytes(), 64, 48, matrix=(-1.0, 0.0, 0.0, 1.0, 300.0, 0.0))
    b.image(page, "b.png", png_bytes(), 64, 48, matrix=(0.0, 1.0, 1.0, 0.0, 0.0, 0.0))  # mirrored across x = y
    doc = read_ntcbak(b.backup())
    mirrored, flipped, turned = doc.pages[0].texts
    height = 16 * 1.25 * UNIT_PT
    # the area Noteastic shows: x from 0 to 300 units, y from 100 units down; upright
    assert (mirrored.rotation, mirrored.x, mirrored.y, mirrored.w) == pytest.approx((0.0, 0.0, 75.0, 225.0))
    # flipped upside down around y = 300 units: the box ends at 300 units, upright
    assert (flipped.rotation, flipped.x, flipped.y + flipped.h) == pytest.approx((0.0, 0.0, 225.0))
    assert flipped.h == pytest.approx(height)
    assert (turned.rotation, turned.x, turned.y) == pytest.approx((90.0, 300.0, 0.0))
    a, c = doc.pages[0].images
    assert (a.rotation, a.x + a.w / 2, a.y + a.h / 2) == pytest.approx((0.0, 268 * UNIT_PT, 24 * UNIT_PT))
    assert c.rotation in (90.0, 270.0) and (c.x + c.w / 2, c.y + c.h / 2) == pytest.approx((18.0, 24.0))
    assert _warned(doc, "4 skewed or mirrored image(s) or text box(es) were placed without the skew or mirror")


def test_pdf_pages_are_fitted_by_their_crop_box() -> None:
    """pdf.js and Windows' PDF renderer fit the visible area (the CropBox), not the MediaBox."""
    b = NtcbakBuilder()
    book = b.book("Cropped")
    b.add_file("PDFs/crop.pdf", pdf_bytes(((600.0, 800.0), (600.0, 800.0), (600.0, 800.0)),
                                          extras=("/CropBox [50 50 550 750]", "/CropBox [100 50 500 750] /Rotate 90",
                                                  "/CropBox [700 900 800 1000]")))
    w, h = 500 * 4 / 3, 700 * 4 / 3  # the 500 x 700 pt visible area at 96 units per inch
    p1 = b.page(book, width=w, height=h, pdf=("PDFs/crop.pdf", 0))
    b.stroke(p1, [(100, 100, 0.5), (0, 0, 0.5), (w, h, 0.5)])
    p2 = b.page(book, width=700, height=400, pdf=("PDFs\\crop.pdf", 1))  # turned: 800 x 600, shows 700 x 400
    b.stroke(p2, [(0, 0, 0.5), (700, 400, 0.5)])
    p3 = b.page(book, width=600, height=800, pdf=("PDFs/crop.pdf", 2))  # a CropBox outside the page: ignored
    b.stroke(p3, [(0, 0, 0.5), (600, 800, 0.5)])
    doc = read_ntcbak(b.backup())
    pages = doc.pages
    assert [(p.width, p.height) for p in pages] == [(600.0, 800.0), (800.0, 600.0), (600.0, 800.0)]
    assert _xy(pages[0], 0) == [(125.0, 125.0), (50.0, 50.0), (550.0, 750.0)]
    assert _xy(pages[1], 0) == [(50.0, 100.0), (750.0, 500.0)]
    assert _xy(pages[2], 0) == [(0.0, 0.0), (600.0, 800.0)]
    assert _warned(doc, "2 PDF page(s) are cropped") and not _warned(doc, "different shape")
    assert R._shown(10, 20, (0, 0, 600, 800), 180) == (590, 20) and R._shown(10, 20, (0, 0, 600, 800), 270) == (
        780, 590)


def test_each_pdf_member_is_parsed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    b = NtcbakBuilder()
    book = b.book("One PDF")
    b.add_file("PDFs/big.pdf", pdf_bytes(((600.0, 400.0),)))
    spellings = ["PDFs/big.pdf", "PDFs\\big.pdf", "./PDFs/big.pdf", "x1\\PDFs\\big.pdf", "x2\\PDFs\\big.pdf",
                 "C:\\Users\\me\\LocalState\\PDFs\\BIG.PDF", "PDFs/missing.pdf", "elsewhere/missing.pdf"]
    for path in spellings:
        b.page(book, width=600, height=400, pdf=(path, 0))
    calls: List[int] = []
    original = R._pdf_boxes

    def counting(data: bytes):
        calls.append(len(data))
        return original(data)

    monkeypatch.setattr(R, "_pdf_boxes", counting)
    doc = read_ntcbak(b.backup())
    assert len(calls) == 1 and list(doc.pdfs) == ["PDFs/big.pdf"]
    assert [p.background is not None for p in doc.pages] == [True] * 6 + [False] * 2


def test_paper_types_page_sizes_and_colours() -> None:
    b = NtcbakBuilder()
    book = b.book("Papers")
    grids = ["Blank", "Lines", "Checkered", "Bullet", "Cornell", "Wavy", "", "SmallGrid", "DotGrid"]
    for grid in grids:
        b.page(book, grid=grid)
    b.page(book, width=-5, height=1e12)
    b.page(book, background="#FFFFE0B2")
    doc = read_ntcbak(b.backup())
    assert [p.paper for p in doc.pages] == ["plain", "lined", "grid", "dotted", "lined", "plain", "plain", "grid",
                                            "grid", "plain", "plain"]
    assert (doc.pages[9].width, doc.pages[9].height) == pytest.approx(A4_PT)
    assert _warned(doc, "Page 10: no usable page size; A4 was used")
    assert _warned(doc, "1 page(s) use paper types gnnote does not know and are plain (Wavy)")
    assert _warned(doc, "The background colour of 1 page(s)")
    assert _warned(doc, "6 page(s) use lined, squared or dotted paper")


# --------------------------------------------------------------------------- elements


def test_deleted_elements_order_and_duplicates() -> None:
    b, page = _one_page()
    late = b.stroke(page, [(1, 1, 0.5), (2, 2, 0.5)], created="2026-03-01")
    early = b.stroke(page, [(3, 3, 0.5), (4, 4, 0.5)], created="2026-02-01")
    tie_b = b.stroke(page, [(5, 5, 0.5), (6, 6, 0.5)], created="2026-02-15", color="#FF0000FF")
    b.stroke(page, [(7, 7, 0.5), (8, 8, 0.5)], created="2026-01-01", deleted=True)
    null_flag = b.stroke(page, [(9, 9, 0.5), (10, 10, 0.5)], created="2026-04-01")
    b.execute("UPDATE Element SET IsDeleted = NULL WHERE ID = ?", (null_flag,))
    b.execute("UPDATE Element SET CreationDate = '2026-02-15' WHERE ID = ?", (late,))
    b.execute("INSERT INTO PageContent VALUES (?, ?)", (page, early))  # listed twice
    b.execute("INSERT INTO PageContent VALUES (?, 424242)", (page,))  # no such element
    doc = read_ntcbak(b.backup())
    xs = [round(s.points[0].x / UNIT_PT) for s in doc.pages[0].strokes]
    # 2026-02-01 (3), then the two 2026-02-15 rows by ID (late < tie_b), then NULL-flagged (9)
    assert xs == [3, 1, 5, 9] and late < tie_b
    assert _warned(doc, "1 page item(s) refer to elements missing")


def test_strokes_highlighters_pressure_and_colours() -> None:
    b, page = _one_page()
    b.stroke(page, [(0, 0, 0.0), (1, 0, 1.0), (2, 0, 7.0), (3, 0)], width=4, raw=None)
    b.stroke(page, [(0, 0, 0.9), (1, 0, 0.1)], width=4, ignore_pressure=True)
    b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)], color="#80FF0000", element_type="Highlighter")
    b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)], color="#00FF00", highlighter=True, element_type="InkStroke")
    b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)], color="not a colour", width=0)
    b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)], color=None, width=-3, height=None)
    doc = read_ntcbak(b.backup())
    s = doc.pages[0].strokes
    assert [round(p.width, 4) for p in s[0].points] == [0.75, 5.25, 5.25, 3.0]  # 3 x (1.5 p + 0.25), clamped
    assert {p.width for p in s[1].points} == {3.0}
    assert [x.kind for x in s] == ["pen", "pen", "highlighter", "highlighter", "pen", "pen"]
    assert s[2].color == pytest.approx((1.0, 0.0, 0.0, 0x80 / 255)) and s[3].color == (0.0, 1.0, 0.0, 1.0)
    assert s[4].color == (0.0, 0.0, 0.0, 1.0) and s[4].width == pytest.approx(R.DEFAULT_PEN_UNITS * UNIT_PT)
    assert s[5].width == pytest.approx(3 * UNIT_PT)  # the larger of |width| and |height|
    assert R._colour(0x7F0000FF, None) == ((0.0, 0.0, 1.0, 0x7F / 255), True)  # an ARGB integer
    assert R._colour("FF102030", None)[0] == pytest.approx((0x10 / 255, 0x20 / 255, 0x30 / 255, 1.0))
    assert R._colour("transparent", None) == ((0.0, 0.0, 0.0, 0.0), True)
    assert R._colour(" ", R.BLACK) == (R.BLACK, True) and R._colour(b"#FF000000", R.BLACK) == (R.BLACK, False)
    assert _warned(doc, "1 colour value(s) could not be read")
    assert _warned(doc, "Pen pressure of 1 stroke(s)")


def _points(text: str, **kw) -> Tuple:
    parsed = parse_points_data(text, **kw)
    return parsed.points, parsed.how


def test_points_data_parsing() -> None:
    assert _points("2;20,20,0.5;80,160,0.5") == ([(20.0, 20.0, 0.5), (80.0, 160.0, 0.5)], "ok")
    assert _points("v3;1,2,0.25;") == ([(1.0, 2.0, 0.25)], "ok")  # a version header, a trailing ';'
    assert _points("1,2,0.5;3,4,0.75") == ([(1.0, 2.0, 0.5), (3.0, 4.0, 0.75)], "ok")  # no header
    assert _points("1,2") == ([(1.0, 2.0, None)], "ok")  # a lone point without header or pressure
    assert _points("2; 1.5E-05 , -2 , 1 ;3,4,0") == ([(1.5e-05, -2.0, 1.0), (3.0, 4.0, 0.0)], "ok")
    assert _points("") == ([], "ok") and _points("0") == ([], "ok")
    assert _points("2;a,b,c;d,e,f") == (None, "unreadable")
    assert _points("1;nan,2,0.5") == (None, "unreadable")
    assert _points("1;1,2,3,4,5,6,7") == (None, "unreadable")
    assert _points("3;1;2;3", max_points=2) == (None, "too-many")
    assert _points("1;" + "1," * 5000 + "1") == (None, "unreadable")  # one huge part: no token flood
    # a header holding a comma (a version "1,0") is not a point: it differs from the points
    assert _points("1,0;100.5,100.5,0.5;200.5,200.5,0.5") == ([(100.5, 100.5, 0.5), (200.5, 200.5, 0.5)], "ok")
    # values after the pressure (a timestamp, tilt) are ignored, as the reference reader does
    parsed = parse_points_data("2;20.5,20.25,0.5,0.1;80.5,160.5,0.5,0.2")
    assert parsed == R.ParsedPoints([(20.5, 20.25, 0.5), (80.5, 160.5, 0.5)], "ok", 0, True, 2, False)
    # a damaged point is left out; the stroke survives while most points are readable
    parsed = parse_points_data("3;20.5,20.25,0.5;80.5,160.5,0.5;bad")
    assert parsed == R.ParsedPoints([(20.5, 20.25, 0.5), (80.5, 160.5, 0.5)], "ok", 1, False, 3, False)
    assert parse_points_data("2;NaN,1,0.5;20,20,0.5") == R.ParsedPoints([(20.0, 20.0, 0.5)], "ok", 1, False, 2)
    assert _points("3;1,1,0.5;x,y,z;inf,2,0.5") == (None, "unreadable")  # most points are damaged
    assert _points("2;1,2,x;3,4,5") == ([(1.0, 2.0, None), (3.0, 4.0, 5.0)], "ok")  # unreadable pressure: none
    assert parse_points_data("2;1.5,2,0.5;" + "9" * 300).dropped == 1  # an overlong part is a damaged point
    # decimal comma: "20,5,30,25,0,5" is (20.5, 30.25, 0.5); ambiguous points take the reading
    # nearest their neighbour
    assert _points("3;20,5,30,25,0,5;21,5,30,0,5;22,31,5,1") == (
        [(20.5, 30.25, 0.5), (21.5, 30.0, 0.5), (22.0, 31.5, 1.0)], "comma")
    assert _points("1;20,5,30,25,1,5") == (None, "unreadable")  # pressure 1.5 is impossible
    assert parse_points_data("3;20,5,30,25,0,5;21,5,30,0,5;x,y,z,w") == R.ParsedPoints(
        [(20.5, 30.25, 0.5), (21.5, 30.0, 0.5)], "comma", 1, False, 3, True)
    # the work done is reported whatever the outcome, so the caller can charge it
    assert parse_points_data("3;1,2,3,4,5,9;1,2,3,4,5,9;1,2,3,4,5,9") == R.ParsedPoints(
        None, "unreadable", 0, False, 3, True)
    assert parse_points_data("2;a,b,c;d,e,f") == R.ParsedPoints(None, "unreadable", 0, False, 2, False)
    assert parse_points_data("3;1,2,3,4,5,9;1,2,3,4,5,9;1,2,3,4,5,9", max_comma_points=2) == R.ParsedPoints(
        None, "too-many-comma", 0, False, 0, True)  # refused before any work
    assert _points("1,5;20,5,30,25,0,5;21,5,30,0,5") == ([(20.5, 30.25, 0.5), (21.5, 30.0, 0.5)], "comma")


def test_malformed_points_data_in_a_backup() -> None:
    b, page = _one_page()
    b.stroke(page, raw="2;a,b,c;d,e,f")
    b.stroke(page, raw="2;10,5,20,25,0,5;11,5,20,0,5")
    b.stroke(page, raw=b"2;1,1,0.5;2,2,0.5")  # a BLOB holding text
    b.stroke(page, raw=b"\xff\xfe\x00binary")
    b.stroke(page, raw="")
    b.stroke(page, raw=12345)
    b.stroke(page, raw="3;20.5,20.25,0.5;80.5,160.5,0.5;bad")
    b.stroke(page, raw="2;20.5,20.25,0.5,0.1;80.5,160.5,0.5,0.2")
    b.stroke(page, raw="1,0;100.5,100.5,0.5;200.5,200.5,0.5")
    eid = b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)])
    b.execute("UPDATE StrokeElement SET PointsData = NULL WHERE ElementID = ?", (eid,))
    doc = read_ntcbak(b.backup())
    assert [len(s.points) for s in doc.pages[0].strokes] == [2, 2, 2, 2, 2]
    assert _xy(doc.pages[0], 4) == [(75.375, 75.375), (150.375, 150.375)]  # no point at the corner
    assert _warned(doc, "1 stroke(s) store their numbers with a decimal comma")
    assert _warned(doc, "3 stroke(s) with unreadable point data")  # letters, binary, 12345 -> "12345" is no point
    assert _warned(doc, "2 stroke(s) without points")
    assert _warned(doc, "1 unreadable point(s) were left out of 1 stroke(s)")
    assert _warned(doc, "The points of 1 stroke(s) carry more values than position and pressure")


def test_lines_and_shapes() -> None:
    b, page = _one_page()
    b.line(page, 0, 0, 100, 0, line_type="DoubleArrow", width=1)
    b.line(page, 0, 0, 100, 0, line_type="Curvy")
    b.line(page, 0, 0, 0, 40, highlighter=True, line_type="Line")
    lid = b.line(page, 0, 0, 1, 1)
    b.execute("UPDATE LineElement SET X1 = NULL WHERE ElementID = ?", (lid,))
    for kind in ("Pentagon", "Hexagon", "Octagon", "OneQuadrant", "FourQuadrant", "Star", "Ellipse"):
        b.shape(page, kind, 100, 100, 0, 0, fill="#00000000" if kind == "Ellipse" else None)
    b.shape(page, "Rectangle", 0, 0, 10, 10, fill="#ZZ", dashed=True)
    doc = read_ntcbak(b.backup())
    s = doc.pages[0].strokes
    # DoubleArrow: shaft + two heads (fill + outline each); arrowhead length max(7, 5 x width)
    assert [x.kind for x in s[:5]] == ["pen", "fill", "pen", "fill", "pen"]
    assert s[2].points[0].x == pytest.approx(75.0) and s[4].points[0].x == pytest.approx(0.0)
    assert s[2].points[1].x == pytest.approx(75.0 - 7.0 * math.cos(0.45) * UNIT_PT)
    assert len(s[5].points) == 2 and s[6].kind == "highlighter"
    shapes = s[7:]
    assert [len(x.points) for x in shapes] == [6, 7, 9, 5, 2, 5, 2, 2, 5, 65, 5]
    pent = shapes[0]
    assert (pent.points[0].x, pent.points[0].y) == pytest.approx((37.5, 0.0))  # a corner on top
    assert _xy(doc.pages[0], 7 + 4) == [(37.5, 0.0), (37.5, 75.0)]  # OneQuadrant's centre line
    assert _warned(doc, "1 shape(s) of unknown types were drawn as rectangles (Star)")
    assert _warned(doc, "1 line(s) of unknown types were drawn without arrowheads (Curvy)")
    assert _warned(doc, "1 line(s) or shape(s) without usable coordinates")
    assert _warned(doc, "1 colour value(s) could not be read") and _warned(doc, "1 dashed")
    assert not any(x.kind == "fill" for x in shapes)  # transparent and unreadable fills draw nothing


def test_highlighter_arrows_are_translucent() -> None:
    b, page = _one_page()
    b.line(page, 0, 0, 100, 0, line_type="SingleArrow", color="#FFFFEB3B", width=8, highlighter=True)
    s = read_ntcbak(b.backup()).pages[0].strokes
    assert [x.kind for x in s] == ["highlighter", "fill", "highlighter"]
    assert s[1].color == pytest.approx((1.0, 0xEB / 255, 0x3B / 255, 0.5)) and s[0].color[3] == 1.0
    assert s[1].points[1].x == pytest.approx((100 - math.cos(0.45) * 40) * UNIT_PT)  # head: 5 x width


def test_images() -> None:
    b, page = _one_page()
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" \
           b"\xff\xc0\x00\x11\x08\x00\x1e\x00\x28\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9"
    b.image(page, "Images\\photo.jpg", jpeg, 0, 0)  # no stored size: the picture's pixels (40 x 30)
    b.image(page, "Images/missing.png", None, 10, 10)
    b.image(page, "Images/anim.gif", b"GIF89a" + bytes(20), 10, 10)
    b.image(page, "", None, 10, 10)
    b.image(page, "C:\\Users\\me\\AppData\\Local\\Packages\\45850BK.Noteastic_fx0e6khbrmsga\\LocalState\\Images\\"
                  "abs.png", png_bytes(8, 8), 8, 8, archive_name="Images/abs.png")
    b.image(page, "IMAGES/ABS.PNG", None, 8, 8)  # case differs from the member name
    doc = read_ntcbak(b.backup())
    images = doc.pages[0].images
    assert [(i.fmt, i.w, i.h) for i in images] == [("jpeg", 30.0, 22.5), ("png", 6.0, 6.0), ("png", 6.0, 6.0)]
    assert _warned(doc, "2 image(s) are missing from the backup")
    assert _warned(doc, "1 image(s) in formats other than PNG or JPEG")


def test_text_boxes() -> None:
    b, page = _one_page()
    b.text(page, "centred\r\nsecond\rthird", align=0, size=0, width=0, spans='[{"Bold": true}]')
    b.text(page, "right", align="Right", font="", color="#FF00FF00")
    b.text(page, "two", align="2")
    b.text(page, "justified", align="Justify", spans="[]")
    b.text(page, "   ")
    tid = b.text(page, "x")
    b.execute("UPDATE TextElement SET Text = NULL WHERE ElementID = ?", (tid,))
    doc = read_ntcbak(b.backup())
    t = doc.pages[0].texts
    assert [x.text for x in t] == ["centred\nsecond\nthird", "right", "two", "justified"]
    assert [x.align for x in t] == ["center", "right", "right", "left"]
    assert t[0].size == pytest.approx(R.DEFAULT_FONT_UNITS * UNIT_PT) and t[0].h == pytest.approx(3 * 12.0 * 1.25)
    assert t[0].w == pytest.approx(len("centred") * 12.0 * 0.6)
    assert t[1].runs[0].font is None and t[1].color == (0.0, 1.0, 0.0, 1.0)
    assert _warned(doc, "Rich-text formatting of 1 text box(es)")


def test_text_alignment_values_of_any_type() -> None:
    from tests.ntcbak_builder import SCHEMA

    untyped = SCHEMA.replace('"HorizontalTextAlignment" varchar', '"HorizontalTextAlignment"')
    assert untyped != SCHEMA
    b = NtcbakBuilder(schema=untyped)
    page = b.page(b.book("Align"))
    values = [float("inf"), float("-inf"), 1e300, 2.0, 2.5, -1, 0, "2", " Center ", "1e999", b"\x02", None, True]
    for value in values:
        b.text(page, "t", align=value)
    doc = read_ntcbak(b.backup())
    assert [t.align for t in doc.pages[0].texts] == ["left", "left", "left", "right", "left", "left", "center",
                                                     "right", "center", "left", "left", "left", "left"]


def test_repeated_picture_placements_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    b, page = _one_page()
    picture = png_bytes(64, 64)
    b.add_file("Images/one.png", picture)
    for _ in range(10):
        b.image(page, "Images\\one.png", None, 64, 64)
    data = b.backup()
    monkeypatch.setattr(R, "MAX_IMAGE_BYTES", 3 * len(picture) + 1)
    doc = read_ntcbak(data)
    assert len(doc.pages[0].images) == 3
    assert _warned(doc, "7 picture placement(s) were skipped")
    monkeypatch.setattr(R, "MAX_IMAGE_BYTES", 10 ** 9)
    monkeypatch.setattr(R, "SHARED_IMAGE_BYTES", 0)  # no more than the backup's own size
    doc = read_ntcbak(data)
    assert len(doc.pages[0].images) == min(10, len(data) // len(picture))
    assert len(read_ntcbak(b.database()).pages[0].images) == 0  # a bare database has no pictures


def test_unknown_and_untyped_elements() -> None:
    b, page = _one_page()
    b.element(page, "Protractor")
    b.element(page, "Protractor")
    b.element(page, "Laser")
    sid = b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)], element_type="")
    b.execute("UPDATE Element SET ElementType = NULL WHERE ID = ?", (sid,))
    b.element(page, "Image")  # no ImageElement row
    b.element(page, None)  # no type and no data
    doc = read_ntcbak(b.backup())
    assert len(doc.pages[0].strokes) == 1
    assert _warned(doc, "3 element(s) of unknown types were skipped (Protractor, Laser)")
    assert _warned(doc, "2 element(s) without their data row")


def test_text_ids_and_mixed_id_types() -> None:
    """IDs stored as text GUIDs (sqlite-net's default for Guid keys), in different cases."""
    b = NtcbakBuilder(schema=sqlite_schema_with_text_ids())
    g = "6F9619FF-8B86-D011-B42D-00C04FC964FF"
    b.execute("INSERT INTO Book VALUES (?, 'Guid book', '', '', '')", (g.lower(),))
    b.execute("INSERT INTO Grid VALUES ('grid-1', 'Lines', 0)")
    b.execute("INSERT INTO Page VALUES ('PAGE-1', 'GRID-1', 800, 600, '#FFFFFFFF', '')")
    b.execute("INSERT INTO BookContent VALUES (?, '0', 'page-1')", (g,))
    b.execute("INSERT INTO Element VALUES ('E-1', 'InkStroke', '', 1, 0, 0, 1, 0, 0, '0')")
    b.execute("INSERT INTO PageContent VALUES ('page-1', 'e-1')")
    b.execute("INSERT INTO StrokeElement VALUES ('E-1', '2', '2', '#FF000000', 'False', 'false', ?)",
              (points_data([(0, 0, 0.5), (8, 4, 0.5)]),))
    doc = read_ntcbak(b.backup())
    assert doc.title == "Guid book" and doc.pages[0].paper == "lined"
    assert _xy(doc.pages[0], 0) == [(0.0, 0.0), (6.0, 3.0)] and doc.pages[0].strokes[0].width == 1.5


def sqlite_schema_with_text_ids() -> str:
    from tests.ntcbak_builder import SCHEMA

    text_ids = SCHEMA.replace("integer primary key", "varchar primary key").replace(" integer", " varchar")
    assert "integer" not in text_ids
    return text_ids


# --------------------------------------------------------------------------- the library


def test_several_notebooks_in_library_order() -> None:
    b = NtcbakBuilder()
    school = b.folder("school")
    maths = b.folder("Maths", school)
    art = b.folder("art")
    b.folder("empty")
    b.execute("ALTER TABLE Book ADD COLUMN IsDeleted BOOL")
    for name, folder, pages in (("Zeta", school, 1), ("algebra", maths, 2), ("Geometry", maths, 1),
                                ("Sketches", art, 1), ("Loose", None, 1), ("Blank", school, 0), ("Gone", art, 1)):
        book = b.book(name, folder)
        for i in range(pages):
            b.stroke(b.page(book), [(i, 0, 0.5), (i, 1, 0.5)])
        if name == "Gone":
            b.execute("UPDATE Book SET IsDeleted = 1 WHERE ID = ?", (book,))
    b.execute("INSERT INTO BookContent VALUES (777, 0, 1000)")  # a notebook missing from the Book table
    doc = read_ntcbak(b.backup())
    assert doc.title == "Noteastic library" and len(doc.pages) == 6
    listing = next(w for w in doc.warnings if w.startswith("This Noteastic backup holds 6 notebooks"))
    assert listing.split(": ", 1)[1] == ("'art / Sketches' (page 1), 'school / Blank' (no pages), "
                                         "'school / Zeta' (page 2), 'school / Maths / algebra' (pages 3-4), "
                                         "'school / Maths / Geometry' (page 5), 'Loose' (page 6)")
    assert _warned(doc, "1 notebook(s) marked as deleted") and _warned(doc, "The pages of 1 notebook(s)")
    assert not _warned(doc, "differs from the documented layout")


def test_page_index_order_cycles_and_missing_pages() -> None:
    b = NtcbakBuilder()
    a = b.folder("A")
    c = b.folder("C", a)
    b.execute("INSERT INTO FolderFolderContent VALUES (?, ?)", (c, a))  # a cycle: A <-> C
    loop = b.folder("Loop")
    b.execute("INSERT INTO FolderFolderContent VALUES (?, ?)", (loop, loop))
    book = b.book("Book", c)
    for index in (2, 0, 1):
        b.stroke(b.page(book, index=index), [(index, 0, 0.5), (index, 1, 0.5)])
    b.execute("INSERT INTO BookContent VALUES (?, 3, 31337)", (book,))  # no such page
    b.execute("INSERT INTO BookContent VALUES (?, NULL, NULL)", (book,))
    b.book("In a loop", loop)
    doc = read_ntcbak(b.backup())
    assert [round(p.strokes[0].points[0].x / UNIT_PT) for p in doc.pages] == [0, 1, 2]
    assert _warned(doc, "1 page reference(s) point to pages missing") and _warned(doc, "1 unusable page reference")
    assert _warned(doc, "2 notebooks; they were converted into one document, in library order: "
                        "'Loop / In a loop' (no pages), 'Book' (pages 1-3)")  # the cycle's folders are not placed


def test_minimal_database_without_optional_tables() -> None:
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE Page (ID INTEGER PRIMARY KEY, Width REAL, Height REAL);
        CREATE TABLE BookContent (BookID INTEGER, PageID INTEGER);
        CREATE TABLE PageContent (PageID INTEGER, ElementID INTEGER);
        CREATE TABLE Element (ID INTEGER PRIMARY KEY, ElementType TEXT);
        CREATE TABLE StrokeElement (ElementID INTEGER, PointsData TEXT);
        INSERT INTO Page VALUES (1, 800, 600), (2, 400, 300);
        INSERT INTO BookContent VALUES (7, 1), (7, 2), (3, 2);
        INSERT INTO Element VALUES (10, 'InkStroke'), (11, 'Line');
        INSERT INTO PageContent VALUES (1, 10), (1, 11);
        INSERT INTO StrokeElement VALUES (10, '2;0,0,0.5;4,4,0.5');
    """)
    doc = read_ntcbak(_zip({"noteastic.db": con.serialize()}))
    assert doc.title == "Noteastic library" and len(doc.pages) == 3  # notebooks 3 and 7, by ID
    assert [(p.width, p.height) for p in doc.pages] == [(300.0, 225.0), (600.0, 450.0), (300.0, 225.0)]
    assert _xy(doc.pages[1], 0) == [(0.0, 0.0), (3.0, 3.0)] and doc.pages[1].strokes[0].width == 1.5
    layout = next(w for w in doc.warnings if "differs from the documented layout" in w)
    assert "missing table(s) Folder, FolderFolderContent, Book, FolderBookContent, Grid, PDFGrid, LineElement" in layout
    assert "StrokeElement.Color" in layout and "BookContent.PageIndex" in layout and "Element.IsDeleted" in layout
    assert "Page.IsDeleted" not in layout and "Book.IsDeleted" not in layout  # undocumented extras
    assert _warned(doc, "'Notebook 3' (page 1), 'Notebook 7' (pages 2-3)")


def test_bare_database_and_wal_files() -> None:
    b = demo_builder()
    db = b.database()
    doc = read_ntcbak(db)
    assert len(doc.pages) == 2 and doc.pages[1].background is None and not doc.pages[0].images
    assert _warned(doc, "converted without its backup archive")
    # a database saved in write-ahead-log mode opens; a non-empty -wal member is reported
    wal = bytearray(db)
    wal[18] = wal[19] = 2
    doc = read_ntcbak(_zip({"Backup\\noteastic.db": bytes(wal), "Backup\\noteastic.db-wal": b"x" * 100}))
    assert len(doc.pages) == 2 and _warned(doc, "write-ahead log")
    # the database one folder down; assets relative to it
    doc = read_ntcbak(b.backup(db_name="Library/NOTEASTIC.DB"))  # assets at the root are found too
    assert doc.pages[1].background.pdf_id == "PDFs/demo.pdf"
    zipped = _zip({"Library/noteastic.db": db, "Library/PDFs/demo.pdf": pdf_bytes(),
                   "Library/Images/gradient.png": png_bytes()})
    doc = read_ntcbak(zipped)
    assert doc.pages[1].background.pdf_id == "Library/PDFs/demo.pdf" and len(doc.pages[0].images) == 1


def test_windows_member_names_and_shared_pdfs() -> None:
    """A ZIP written on Windows may store backslashes in member names; the PDF id uses '/' and
    one PDF shown by several pages (under differently spelt paths) is stored once."""
    b = NtcbakBuilder()
    book = b.book("Shared")
    for path, index in (("PDFs\\deck.pdf", 0), ("pdfs/DECK.pdf", 1), ("\\PDFs\\deck.pdf", 0)):
        b.page(book, width=400, height=300, pdf=(path, index))
    b.image(b.page(book), "Images\\pic.png", None, 20, 10)
    data = b.backup(extra={"PDFs\\deck.pdf": pdf_bytes(((400.0, 300.0), (400.0, 300.0))),
                           "Images\\pic.png": png_bytes(20, 10)})
    doc = read_ntcbak(data)
    assert list(doc.pdfs) == ["PDFs/deck.pdf"]
    assert [(p.background.pdf_id, p.background.page_index) for p in doc.pages[:3]] == [
        ("PDFs/deck.pdf", 0), ("PDFs/deck.pdf", 1), ("PDFs/deck.pdf", 0)]
    assert len(doc.pages[3].images) == 1 and doc.pages[3].images[0].data == png_bytes(20, 10)


def test_find_database() -> None:
    assert find_database(["PDFs/a.pdf", "noteastic.db"]) == "noteastic.db"
    assert find_database(["x/NoteAstic.DB", "noteastic.db"]) == "noteastic.db"
    assert find_database(["x\\noteastic.db"]) == "x\\noteastic.db"
    assert find_database(["a/b/noteastic.db", "__MACOSX/noteastic.db", "noteastic.db.bak"]) is None


# --------------------------------------------------------------------------- not Noteastic / hostile input


def test_inputs_that_are_not_noteastic_backups() -> None:
    with pytest.raises(TypeError):
        read_ntcbak("text")  # type: ignore[arg-type]
    cases = [
        (b"hello", "not a Noteastic backup"),
        (b"PK\x03\x04 broken", "not a readable ZIP archive"),
        (_zip({"PDFs/a.pdf": b"%PDF-1.4"}), "holds no noteastic.db"),
        (_zip({"noteastic.db": b"not sqlite at all"}), "is not an SQLite database"),
        (_zip({"noteastic.db": b"SQLite format 3\x00" + bytes(84)}), "not a readable Noteastic backup"),
    ]
    journal = sqlite3.connect(":memory:")
    journal.executescript("CREATE TABLE pages (id TEXT, data BLOB); CREATE TABLE blobs (id TEXT, data BLOB);")
    cases.append((journal.serialize(), "has no Page and BookContent tables (it holds blobs, pages)"))
    empty = NtcbakBuilder()
    empty.book("Nothing")
    cases.append((empty.backup(), "holds no notebook pages"))
    lacking = sqlite3.connect(":memory:")
    lacking.executescript("CREATE TABLE Page (ID INTEGER); CREATE TABLE BookContent (BookID INTEGER, PageIndex);"
                          "INSERT INTO Page VALUES (1);")
    cases.append((lacking.serialize(), "BookContent table lacks the column(s) PageID"))
    stored = io.BytesIO()
    with zipfile.ZipFile(stored, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("noteastic.db", demo_builder().database())
    bad_crc = bytearray(stored.getvalue())
    bad_crc[bad_crc.find(b"SQLite format 3") + 200] ^= 0xFF
    cases.append((bytes(bad_crc), "noteastic.db) in this backup cannot be unpacked"))
    for data, message in cases:
        with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
            read_ntcbak(data)


def test_views_triggers_and_virtual_tables_never_stand_in_for_tables() -> None:
    b, page = _one_page()
    b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)])
    b.execute("DROP TABLE StrokeElement")
    b.execute("CREATE VIEW StrokeElement AS SELECT 5000 AS ElementID, 2 AS StrokeWidth, 2 AS StrokeHeight, "
              "'#FF000000' AS Color, 0 AS IsHighlighter, 0 AS IgnorePressure, '2;0,0,0.5;9,9,0.5' AS PointsData")
    b.execute("CREATE TRIGGER t AFTER INSERT ON Element BEGIN DELETE FROM Page; END")
    b.execute("DROP TABLE LineElement")
    try:
        b.execute("CREATE VIRTUAL TABLE LineElement USING rtree(ElementID, X0, X1)")
    except sqlite3.OperationalError:  # an SQLite without R*Tree: the view and trigger still count
        pass
    doc = read_ntcbak(b.backup())
    assert len(doc.pages) == 1 and not doc.pages[0].strokes
    assert _warned(doc, "missing table(s) StrokeElement")
    db = SqliteDatabase(b.database())
    assert not db.has_table("StrokeElement") and not db.has_table("LineElement") and db.has_table("Element")
    db.close()
    view_page = sqlite3.connect(":memory:")
    view_page.executescript("CREATE TABLE BookContent (BookID, PageIndex, PageID); "
                            "CREATE VIEW Page AS SELECT 1 AS ID, 800 AS Width, 600 AS Height;"
                            "INSERT INTO BookContent VALUES (1, 0, 1); CREATE TABLE StrokeElement (x);")
    with pytest.raises(ValueError, match="has no Page and BookContent tables"):
        read_ntcbak(view_page.serialize())


@pytest.mark.parametrize("row", [
    ("table", "Evil", "Evil", 0, "CREATE TABLE Evil ("),  # unparsable schema SQL
    ("table", "StrokeElement", "StrokeElement", 0, "CREATE VIEW StrokeElement AS SELECT 1"),  # a view posing as table
    ("table", "StrokeElement", "StrokeElement", 2, "CREATE TABLE Phantom (x)"),  # names another table than it creates
])
def test_lying_schema_rows_are_refused(row: Tuple) -> None:
    b = demo_builder()
    if row[1] == "StrokeElement":
        b.execute("DROP TABLE StrokeElement")
    b.execute("PRAGMA writable_schema=ON")
    b.execute("INSERT INTO sqlite_master VALUES (?, ?, ?, ?, ?)", row)
    b.execute("PRAGMA writable_schema=OFF")
    with pytest.raises(ValueError, match="noteastic.db.* is damaged .*malformed database schema"):
        read_ntcbak(b.backup())


def test_corrupted_databases_raise_value_error_or_degrade() -> None:
    db = demo_builder().database()
    rng = random.Random(1234)
    outcomes = {"read": 0, "refused": 0}
    for trial in range(40):
        damaged = bytearray(db)
        if trial % 2:
            start = rng.randrange(100, len(damaged) - 64)
            damaged[start:start + 64] = bytes(rng.randrange(256) for _ in range(64))
        else:
            damaged = damaged[:rng.randrange(100, len(damaged))]
        try:
            doc = read_ntcbak(bytes(damaged))
        except ValueError:
            outcomes["refused"] += 1
        else:
            assert doc.warnings[0] == EXPERIMENTAL_WARNING
            outcomes["read"] += 1
    assert outcomes["refused"] and outcomes["read"]


def _dag(con: sqlite3.Connection, table: str) -> bytes:
    """The database of ``con`` with ``table``'s root page replaced by an interior page packed
    with cells that all point at the root's first child: a b-tree turned into a DAG, whose scan
    returns the rows of that child hundreds of times."""
    con.commit()
    data = bytearray(con.serialize())
    size = int.from_bytes(data[16:18], "big")
    root = con.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    base = (root - 1) * size
    assert root > 1 and data[base] == 0x05, "the root must be an interior page"
    cell = base + int.from_bytes(data[base + 12:base + 14], "big")
    first = bytes(data[cell:cell + 4])
    n = (size - 12) // 7  # cells of a 4-byte child page number and a 1-byte key, and their pointers
    page = bytearray(size)
    page[0] = 0x05
    page[3:5] = n.to_bytes(2, "big")
    page[5:7] = (size - 5 * n).to_bytes(2, "big")
    page[8:12] = first
    for i in range(n):
        at = size - 5 * n + 5 * i
        page[at:at + 5] = first + bytes([1 + i % 127])
        page[12 + 2 * i:14 + 2 * i] = at.to_bytes(2, "big")
    data[base:base + size] = page
    return bytes(data)


def test_btrees_that_share_pages_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    b, page = _one_page()
    for i in range(300):
        b.stroke(page, [(i, 0, 0.5), (i, 1, 0.5)], color="#" * 1000)  # long rows: a few per leaf page
    dag = _dag(b.con, "StrokeElement")
    db = SqliteDatabase(dag, "the test database")
    with pytest.raises(SqliteError, match="damaged .SQLite's check of its StrokeElement table reports: .*"):
        db.select("StrokeElement", ("ElementID",))
    with pytest.raises(SqliteError, match="check of its StrokeElement table"):
        db.count("StrokeElement")  # the verdict is remembered
    assert db.count("Element") == 300 and db.select("Page", ("ID",)).rows
    db.close()
    doc = read_ntcbak(dag)
    assert not doc.pages[0].strokes
    assert _warned(doc, "The StrokeElement table of noteastic.db could not be read", "check of its StrokeElement")
    # the data budget catches what a check misses: the repeated rows return more than the file holds
    monkeypatch.setattr(SqliteDatabase, "_verify", lambda self, table: None)
    monkeypatch.setattr(sqliteutil, "DATA_FACTOR", 1)
    monkeypatch.setattr(sqliteutil, "DATA_SLACK", 0)
    db = SqliteDatabase(dag, "the test database")
    with pytest.raises(SqliteTooComplex, match="returned more data than it holds"):
        db.select("StrokeElement", ("Color",), max_value=10 ** 6)
    assert db.exhausted
    with pytest.raises(SqliteTooComplex):
        db.select("Page", ("ID",))
    # a damaged Page table means there is nothing to convert
    con = sqlite3.connect(":memory:")
    con.executescript(sqlite_schema_with_text_ids())
    con.executemany("INSERT INTO Page (ID, Width, Height, BackgroundColor) VALUES (?, 800, 600, ?)",
                    ((f"page-{i}", "#" * 1500) for i in range(40)))
    con.execute("INSERT INTO Book (ID, Name) VALUES ('b', 'Book')")
    con.execute("INSERT INTO BookContent VALUES ('b', 0, 'page-1')")
    monkeypatch.undo()
    assert len(read_ntcbak(con.serialize()).pages) == 1
    with pytest.raises(ValueError, match="check of its Page table"):
        read_ntcbak(_dag(con, "Page"))


def test_data_budget_spans_every_select(monkeypatch: pytest.MonkeyPatch) -> None:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE T (v)")
    con.executemany("INSERT INTO T VALUES (?)", (("x" * 10_000,) for _ in range(4)))
    con.commit()
    data = con.serialize()
    monkeypatch.setattr(sqliteutil, "DATA_FACTOR", 1)
    monkeypatch.setattr(sqliteutil, "DATA_SLACK", 0)
    db = SqliteDatabase(data, "the test database")
    assert len(db.select("T", ("v",)).rows) == 4 and 40_000 < len(data) < 80_000
    with pytest.raises(SqliteTooComplex, match="the test database is too large or complex"):
        db.select("T", ("v",), accept=lambda row: False)  # rows the caller drops count too


def test_views_are_never_prepared() -> None:
    """Preparing a view that names the one before it twice doubles the work per level; the
    helper must open such a schema without ever expanding it."""
    b, page = _one_page()
    b.stroke(page, [(0, 0, 0.5), (5, 5, 0.5)])
    b.execute("CREATE VIEW c0 AS SELECT 1 AS x")
    for n in range(1, 15):  # preparing c14 takes seconds
        b.execute(f"CREATE VIEW c{n} AS SELECT a.x FROM c{n - 1} a, c{n - 1} b")
    data = b.backup()
    start = time.perf_counter()
    doc = read_ntcbak(data)
    assert time.perf_counter() - start < 1.0
    assert len(doc.pages[0].strokes) == 1


def test_check_constraints_and_computed_columns_are_never_evaluated() -> None:
    con = sqlite3.connect(":memory:")
    con.executescript("""
        PRAGMA ignore_check_constraints=ON;
        CREATE TABLE C (a CHECK (a > 0), b);
        INSERT INTO C VALUES (-1, 'kept');
        CREATE TABLE G (a, g AS (length(printf('%.*c', 50000000, 'x'))) NOT NULL);
        INSERT INTO G (a) VALUES (1), (2), (3);
        CREATE TABLE H (a, h AS (length(printf('%.*c', 50000000, 'x'))), s AS (a + 1) STORED NOT NULL);
        INSERT INTO H (a) VALUES (1), (2);
    """)
    db = SqliteDatabase(con.serialize(), "the test database")
    assert db.select("C", ("a", "b")).rows == [(-1, "kept")]  # a CHECK violation is not damage
    start = time.perf_counter()
    # The check would compute g for every row, and skipping the check would leave a scan of G
    # open to the repeated rows of a damaged b-tree: such a table is not read at all.
    with pytest.raises(SqliteError, match="its G table has an unusual definition .a computed column declared NOT"):
        db.select("G", ("a",))
    with pytest.raises(SqliteError, match="unusual definition"):
        db.count("G")  # the verdict is remembered
    assert db.select("H", ("a",)).rows == [(1,), (2,)]  # checked: h may be NULL and s is stored
    assert time.perf_counter() - start < 0.5 and db._checked["C"] is None and db._checked["H"] is None


def _dag_with_large_values(computed: bool, value_bytes: int) -> bytes:
    """A Noteastic database whose Page b-tree is a DAG (a scan repeats the rows of its first
    leaf hundreds of times) and whose BackgroundColor values are just above the 64 KB value
    cap; with ``computed`` the Page table also has a NOT NULL virtual computed column."""
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA page_size=4096")
    extra = ", G INTEGER AS (1) NOT NULL" if computed else ""
    con.executescript(f"""
        CREATE TABLE Book (ID, Name);
        CREATE TABLE BookContent (BookID, PageIndex, PageID);
        CREATE TABLE Page (ID, GridID, Width, Height, BackgroundColor{extra});
        INSERT INTO Book VALUES ('b', 'Book');
        INSERT INTO BookContent VALUES ('b', 0, 'p');
    """)
    rows = [("p", "#" + "f" * (value_bytes - 1))] * 3 + [("q", "x" * 200)] * 200  # an interior root page
    con.executemany("INSERT INTO Page (ID, Width, Height, BackgroundColor) VALUES (?, 800, 600, ?)", rows)
    return _dag(con, "Page")


def test_tables_with_computed_columns_cannot_bypass_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review R2-1: a NOT NULL computed column used to skip quick_check, and values over their
    cap were not charged, so a DAG-shaped table of over-cap values kept SQLite copying for
    minutes.  Such a table is now refused, and over-cap values count against the data budget."""
    data = _dag_with_large_values(computed=True, value_bytes=70_000)
    probe = sqlite3.connect(":memory:")
    probe.deserialize(data)
    assert "2nd reference to page" in probe.execute("PRAGMA quick_check(Page)").fetchone()[0]
    start = time.perf_counter()
    with pytest.raises(ValueError, match="its Page table has an unusual definition"):
        read_ntcbak(data)
    assert time.perf_counter() - start < 1.0
    # Without the computed column the check itself refuses the table at once.
    start = time.perf_counter()
    with pytest.raises(ValueError, match="check of its Page table reports"):
        read_ntcbak(_dag_with_large_values(computed=False, value_bytes=70_000))
    assert time.perf_counter() - start < 1.0
    # Whatever a check misses: values over their cap are loaded by SQLite to be measured, so they
    # are charged to the data budget like values handed to Python (here 3 x 70 KB per leaf visit
    # against a budget of twice the database plus 16 MB).
    monkeypatch.setattr(SqliteDatabase, "_verify", lambda self, table: None)
    db = SqliteDatabase(data, "the test database")
    start = time.perf_counter()
    with pytest.raises(SqliteTooComplex, match="returned more data than it holds"):
        db.select("Page", ("ID", "BackgroundColor"))
    assert time.perf_counter() - start < 1.0
    db.close()
    # a sound table with an over-cap value: charged by its size in bytes, read as TOO_LARGE
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE T (v)")
    con.execute("INSERT INTO T VALUES (?)", ("\u00e9" * 50_000,))  # 100 000 bytes of UTF-8
    con.commit()
    db = SqliteDatabase(con.serialize(), "the test database")
    left = db._data_left
    sel = db.select("T", ("v",))
    assert sel.rows == [(TOO_LARGE,)] and sel.oversized == {"v": 1} and left - db._data_left == 100_000


def test_the_check_stops_at_its_first_finding() -> None:
    """A b-tree that is a chain of pages each pointing twice at the next one makes a scan visit
    its leaf 2**depth times, while quick_check reports only a few dozen problems (fewer than the
    hundred after which it stops).  Fetching past the check's first result row would run its
    row-by-row pass over the whole DAG; the helper fetches only that row."""
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA page_size=4096")
    con.execute("CREATE TABLE T (a NOT NULL, b NOT NULL)")
    con.executemany("INSERT INTO T VALUES (1, 2)", [()] * 2000)
    con.commit()
    data = bytearray(con.serialize())
    size = 4096
    root = con.execute("SELECT rootpage FROM sqlite_master WHERE name = 'T'").fetchone()[0]
    base = (root - 1) * size
    child = int.from_bytes(data[base + int.from_bytes(data[base + 12:base + 14], "big"):][:4], "big")

    def doubled(target: int) -> bytes:  # an interior page whose one cell and right child are `target`
        page = bytearray(size)
        page[0] = 0x05
        page[3:5] = (1).to_bytes(2, "big")
        page[5:7] = (size - 5).to_bytes(2, "big")
        page[8:12] = target.to_bytes(4, "big")
        page[size - 5:size] = target.to_bytes(4, "big") + b"\x01"
        page[12:14] = (size - 5).to_bytes(2, "big")
        return bytes(page)

    pages = len(data) // size
    for _ in range(18):
        data += doubled(child)
        pages += 1
        child = pages
    data[base:base + size] = doubled(child)
    data[28:32] = pages.to_bytes(4, "big")
    db = SqliteDatabase(bytes(data), "the test database")
    start = time.perf_counter()
    with pytest.raises(SqliteError, match="check of its T table reports"):
        db.select("T", ("a",))
    assert time.perf_counter() - start < 0.5  # fetching on took seconds (2**19 visits of the leaf)


def test_failed_opens_release_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    connections: List[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def connect(*args, **kw):
        con = real_connect(*args, **kw)
        connections.append(con)
        return con

    monkeypatch.setattr(sqlite3, "connect", connect)
    damaged = demo_builder()
    damaged.execute("PRAGMA writable_schema=ON")
    damaged.execute("INSERT INTO sqlite_master VALUES ('table', 'Evil', 'Evil', 0, 'CREATE TABLE Evil (')")
    with pytest.raises(SqliteError):
        SqliteDatabase(damaged.database(), "the test database")
    with pytest.raises(sqlite3.ProgrammingError):
        connections[-1].execute("SELECT 1")  # closed
    # no reference cycle keeps an open database alive until a garbage collection
    gc.disable()
    try:
        db = SqliteDatabase(demo_builder().database(), "the test database")
        ref = weakref.ref(db)
        del db
        assert ref() is None
    finally:
        gc.enable()


def test_huge_values_and_names_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    b, page = _one_page()
    b.execute("UPDATE Book SET Name = ?", ("N" * 100_000,))
    b.stroke(page, [(i, i, 0.5) for i in range(500)])
    b.stroke(page, [(0, 0, 0.5), (1, 1, 0.5)])
    b.text(page, "t" * 5000)
    monkeypatch.setattr(R, "MAX_POINTS_CHARS", 1000)
    monkeypatch.setattr(R, "MAX_TEXT_CHARS", 100)
    doc = read_ntcbak(b.backup())
    assert doc.title == "Untitled notebook" and len(doc.pages[0].strokes) == 1 and not doc.pages[0].texts
    assert _warned(doc, "1 value(s) of Book.Name were too large to read")
    assert _warned(doc, "1 value(s) of StrokeElement.PointsData were too large")
    assert _warned(doc, "1 value(s) of TextElement.Text were too large")


def test_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    b = NtcbakBuilder()
    book = b.book("Many")
    for i in range(6):
        page = b.page(book)
        b.stroke(page, [(j, i, 0.5) for j in range(40)])
    data = b.backup()
    monkeypatch.setattr(R, "MAX_PAGES", 4)
    monkeypatch.setattr(readutil, "MAX_POINTS", 100)
    doc = read_ntcbak(data)
    assert len(doc.pages) == 4 and sum(len(p.strokes) for p in doc.pages) == 2
    assert _warned(doc, "more than 4 pages; the rest (2)") and _warned(doc, "more ink points than gnnote reads")
    monkeypatch.setattr(R, "MAX_ELEMENTS", 3)
    doc = read_ntcbak(data)
    assert _warned(doc, "The PageContent table holds more than 3 rows")
    monkeypatch.setattr(R, "MAX_POINTS_PER_STROKE", 10)
    monkeypatch.setattr(R, "MAX_ELEMENTS", 1000)
    doc = read_ntcbak(data)
    assert _warned(doc, "4 stroke(s) with more than 10 points")
    monkeypatch.setattr(readutil, "MAX_MEMBER_BYTES", 1000)
    with pytest.raises(ValueError, match="too large to read"):
        read_ntcbak(data)


def test_decimal_comma_points_have_their_own_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    b, page = _one_page()
    comma = "3;10,5,20,25,0,5;11,5,20,0,5;12,5,21,0,5"
    for _ in range(4):
        b.stroke(page, raw=comma)
    b.stroke(page, [(1.5, 2.5, 0.5), (3.5, 4.5, 0.5)])
    monkeypatch.setattr(R, "MAX_COMMA_POINTS", 7)
    doc = read_ntcbak(b.backup())
    assert [len(s.points) for s in doc.pages[0].strokes] == [3, 3, 2]
    assert _warned(doc, "2 stroke(s) store their numbers with a decimal comma")
    assert _warned(doc, "2 stroke(s) written with a decimal comma were skipped: gnnote decodes at most 7")
    assert parse_points_data(comma, max_comma_points=2).how == "too-many-comma"
    assert parse_points_data("2;1.5,2,0.5;3,4,0.5", max_comma_points=0).how == "ok"


def test_unreadable_strokes_use_up_the_allowances(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review R2-2: a stroke whose points cannot be read cost the same decoding work as one
    whose points can, so it is charged too: against the decimal-comma allowance when it took
    that slow path, and against the point budget either way."""
    b, page = _one_page()
    bad_comma = "3;1,2,3,4,5,9;1,2,3,4,5,9;1,2,3,4,5,9"  # six tokens, no reading with a pressure <= 1
    good_comma = "3;10,5,20,25,0,5;11,5,20,0,5;12,5,21,0,5"
    for raw in (bad_comma, bad_comma, good_comma):
        b.stroke(page, raw=raw)
    b.stroke(page, [(1.5, 2.5, 0.5), (3.5, 4.5, 0.5)])
    monkeypatch.setattr(R, "MAX_COMMA_POINTS", 7)
    doc = read_ntcbak(b.backup())
    assert [len(s.points) for s in doc.pages[0].strokes] == [2]  # 3 + 3 parts left only 1 of the 7
    assert _warned(doc, "2 stroke(s) with unreadable point data were skipped")
    assert _warned(doc, "1 stroke(s) written with a decimal comma were skipped: gnnote decodes at most 7")
    # the point budget: an unreadable 60-part stroke uses 60 of 100, so the next one is not parsed
    b, page = _one_page()
    for _ in range(2):
        b.stroke(page, raw="60;" + ";".join(["a,b,c"] * 60))
    b.stroke(page, [(j, j, 0.5) for j in range(10)])
    data = b.backup()
    parsed: List[int] = []
    original = R.parse_points_data

    def counting(text: str, *args, **kw):
        parsed.append(text.count(";"))
        return original(text, *args, **kw)

    monkeypatch.setattr(R, "parse_points_data", counting)
    monkeypatch.setattr(readutil, "MAX_POINTS", 100)
    doc = read_ntcbak(data)
    assert parsed == [60, 10] and [len(s.points) for s in doc.pages[0].strokes] == [10]
    assert _warned(doc, "1 stroke(s) with unreadable point data") and _warned(doc, "more ink points than gnnote")


def test_point_budget_is_checked_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    b, page = _one_page()
    for _ in range(3):
        b.stroke(page, [(j, j, 0.5) for j in range(60)])
    b.stroke(page, [(j, 0, 0.5) for j in range(10)])
    data = b.backup()
    parsed: List[int] = []
    original = R.parse_points_data

    def counting(text: str, *args, **kw):
        parsed.append(text.count(";"))
        return original(text, *args, **kw)

    monkeypatch.setattr(R, "parse_points_data", counting)
    monkeypatch.setattr(readutil, "MAX_POINTS", 100)
    doc = read_ntcbak(data)
    assert parsed == [60, 10]  # the two 60-point strokes beyond the budget were never parsed
    assert [len(s.points) for s in doc.pages[0].strokes] == [60, 10]
    assert _warned(doc, "more ink points than gnnote reads")


def test_work_and_size_limits_of_the_sqlite_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    db = demo_builder().database()
    with pytest.raises(SqliteError, match="more than the 0 MB gnnote reads"):
        SqliteDatabase(db, "the test database", max_bytes=1000)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE Big (a, b)")
    con.executemany("INSERT INTO Big VALUES (?, ?)", ((i, str(i)) for i in range(50_000)))
    con.commit()
    handle = SqliteDatabase(con.serialize(), "the test database", max_steps=sqliteutil.PROGRESS_INTERVAL * 2)
    assert handle.tables == {"big": "Big"}  # discovery fits in the budget, a scan of the table does not
    with pytest.raises(SqliteTooComplex, match="the test database is too large or complex"):
        handle.select("Big", ("a", "b"))
    assert handle.exhausted
    with pytest.raises(SqliteTooComplex):
        handle.count("Big")
    # the reader keeps the pages it could read and says what ran out
    b, page = _one_page()
    for i in range(3000):
        b.stroke(page, [(i % 50, 0, 0.5), (i % 50, 1, 0.5)])
    monkeypatch.setattr(sqliteutil, "MAX_VM_STEPS", sqliteutil.PROGRESS_INTERVAL * 20)
    try:
        doc = read_ntcbak(b.backup())
    except ValueError as exc:
        assert "too large or complex" in str(exc)
    else:
        assert _warned(doc, "could not be read", "too large or complex")
        assert sum(len(p.strokes) for p in doc.pages) < 3000


def _schema_dag(fan: int = 500, pad: int = 0, short: bool = False) -> bytes:
    """A Noteastic database whose schema b-tree (page 1) is a DAG: page 1 points ``fan`` times
    at one extra interior page, which points ``fan`` times at the leaf of the last schema rows
    (duplicate index rows, which SQLite accepts while it loads the schema).  The extra page is
    the last one, after ``pad`` pages of zeros; ``short`` cuts its last byte off the file
    (SQLite still reads it, as a page that ends in a zero)."""
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA page_size=4096")
    con.executescript(sqlite_schema_with_text_ids())
    con.execute("CREATE TABLE u (a UNIQUE)")
    con.execute("INSERT INTO Book (ID, Name) VALUES ('b', 'Book')")
    con.execute("INSERT INTO BookContent VALUES ('b', 0, 'p')")
    con.execute("INSERT INTO Page (ID, Width, Height) VALUES ('p', 800, 600)")
    con.commit()
    root = con.execute("SELECT rootpage FROM sqlite_master WHERE name = 'sqlite_autoindex_u_1'").fetchone()[0]
    con.execute("PRAGMA writable_schema=ON")
    con.executemany("INSERT INTO sqlite_master VALUES ('index', 'sqlite_autoindex_u_1', 'u', ?, NULL)",
                    [(root,)] * 400)
    con.commit()
    con.execute("PRAGMA writable_schema=OFF")
    data = bytearray(con.serialize())
    size = 4096
    assert data[100] == 0x05, "the schema must span several pages"
    first = int.from_bytes(data[100 + 12 + 0:100 + 14], "big")
    first = int.from_bytes(data[first:first + 4], "big")  # the leaf with the CREATE statements
    last = int.from_bytes(data[108:112], "big")  # the leaf of the last index rows

    def interior(head: bytes, children: List[int], right: int) -> bytes:
        page = bytearray(head) + bytearray(size - len(head))
        at = len(head)
        page[at] = 0x05
        page[at + 1:at + 3] = b"\0\0"
        page[at + 3:at + 5] = len(children).to_bytes(2, "big")
        page[at + 7] = 0
        page[at + 8:at + 12] = right.to_bytes(4, "big")
        top = size
        for i, child in enumerate(children):
            top -= 5
            page[top:top + 5] = child.to_bytes(4, "big") + bytes([1 + i % 127])
            page[at + 12 + 2 * i:at + 14 + 2 * i] = top.to_bytes(2, "big")
        page[at + 5:at + 7] = top.to_bytes(2, "big")
        return bytes(page)

    data += bytes(size * pad)
    mid = len(data) // size + 1
    data += interior(b"", [last] * fan, last)
    data[:size] = interior(bytes(data[:100]), [first] + [mid] * (fan - 1), mid)
    data[28:32] = mid.to_bytes(4, "big")  # the database's size in pages
    return bytes(data[:-1] if short else data)


def test_damaged_schemas_are_refused_before_sqlite_loads_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review R2-3: SQLite reads every schema row before the first statement runs, so a schema
    b-tree whose pages are shared made it load the same rows until the work limit stopped it
    (about 50 seconds from 7.5 KB).  The schema is walked in Python first."""
    data = _schema_dag()
    assert len(zipfile_bytes(data)) < 16_000
    start = time.perf_counter()
    with pytest.raises(SqliteError, match=r"is damaged \(its schema reaches page \d+ twice\)"):
        SqliteDatabase(data, "the test database")
    with pytest.raises(ValueError, match="its schema reaches page"):
        read_ntcbak(data)
    assert time.perf_counter() - start < 0.2
    # Whatever the walk might miss: the work budget grows with the database's size, so a small
    # file is stopped within about a second instead of using the full 2 x 10^9 steps.
    monkeypatch.setattr(sqliteutil, "schema_tree_problem", lambda data, *a, **kw: None)
    start = time.perf_counter()
    with pytest.raises(SqliteError, match="too large or complex to read completely .gnnote's work limit"):
        SqliteDatabase(data, "the test database")
    assert time.perf_counter() - start < 3.0


@pytest.mark.parametrize("pad", [0, 8000])
def test_schema_walk_reads_a_last_page_the_file_ends_inside(pad: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review R3-1: SQLite rounds the file's size up to whole pages and reads a last page that
    the file ends inside with zeros for the missing bytes; the walk counted only whole pages, so
    a schema DAG whose shared page was the last one, one byte short, went unnoticed.  Zero pages
    in front of it (a 41 KB backup holding a 33 MB database) also raised the work budget, which
    followed the database's size, close to its maximum (about 50 seconds)."""
    data = _schema_dag(pad=pad, short=True)
    size = 4096
    assert len(data) % size == size - 1 and data[28:32] == (-(-len(data) // size)).to_bytes(4, "big")
    backup = zipfile_bytes(data)
    assert len(backup) < 64_000 and detect_format("x.ntcbak", backup) == "noteastic"
    whole = data + b"\0"
    hub = len(whole) // size
    assert sqliteutil.schema_tree_problem(whole) == sqliteutil.schema_tree_problem(data)
    assert re.fullmatch(r"its schema reaches page \d+ twice", sqliteutil.schema_tree_problem(data) or "")
    area = sqliteutil.schema_area(data)
    assert len(area) % size == 0 and whole[(hub - 1) * size:] in area  # the hub page, filled up with a zero
    start = time.perf_counter()
    with pytest.raises(SqliteError, match=r"is damaged \(its schema reaches page \d+ twice\)"):
        SqliteDatabase(data, "the test database")
    with pytest.raises(ValueError, match="its schema reaches page"):
        read_ntcbak(backup)
    assert time.perf_counter() - start < 2.0
    # The work budget follows the pages that hold data: the zero pages add nothing to it.
    assert sqliteutil.content_bytes(data) == len(data) - pad * size
    monkeypatch.setattr(sqliteutil, "schema_tree_problem", lambda data, *a, **kw: None)
    start = time.perf_counter()
    with pytest.raises(SqliteError, match="too large or complex to read completely .gnnote's work limit"):
        SqliteDatabase(data, "the test database")
    assert time.perf_counter() - start < 3.0


def test_content_bytes_and_partial_pages_of_sound_databases() -> None:
    """Sound databases: every page counts towards the work budget; a file with a zero page, a
    partial page of zeros or of data at its end, or that ends inside its last page, opens."""
    db = demo_builder().database()
    assert sqliteutil.content_bytes(db) == len(db)
    assert sqliteutil.content_bytes(b"not sqlite") == len(b"not sqlite")
    bad_size = bytearray(db)
    bad_size[16:18] = (1000).to_bytes(2, "big")
    assert sqliteutil.content_bytes(bytes(bad_size)) == len(db)
    size = int.from_bytes(db[16:18], "big")
    pages = len(read_ntcbak(db).pages)
    for tail, counted in ((bytes(size), 0), (bytes(100), 0), (b"\1" * 100, 100), (bytes(size) + b"\1", 1)):
        grown = db + tail
        assert sqliteutil.content_bytes(grown) == len(db) + counted, tail[:4]
        assert sqliteutil.schema_tree_problem(grown) is None
        assert sqliteutil.schema_area(grown) == sqliteutil.schema_area(db)
        assert len(read_ntcbak(grown).pages) == pages
    # the empty table's root page, the file's last, cut short: SQLite reads it filled up with zeros
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA page_size=4096")
    con.execute(f"CREATE TABLE a ({', '.join(f'x{j}' for j in range(30))})")
    con.commit()
    one = con.serialize()
    assert len(one) == 2 * 4096 and one[100] == 0x0D and not any(one[4096 + 8:])
    cut = one[:4096 + 8]
    assert sqliteutil.schema_tree_problem(cut) is None and sqliteutil.schema_area(cut) == one[:4096]
    assert sqliteutil.content_bytes(cut) == len(cut)
    with SqliteDatabase(cut, "the test database") as opened:
        assert opened.tables == {"a": "a"} and opened.count("a") == 0
        assert opened.select("a", ("x0", "x29")).present


def zipfile_bytes(db: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("noteastic.db", db)
    return buf.getvalue()


def test_schema_walk_accepts_sound_schemas() -> None:
    """Sound schemas pass the walk: one page, many pages, statements long enough to overflow
    and several page sizes."""
    assert sqliteutil.schema_tree_problem(demo_builder().database()) is None
    assert sqliteutil.schema_tree_problem(b"not sqlite") is None
    for page_size in (512, 4096, 65536):
        con = sqlite3.connect(":memory:")
        con.execute(f"PRAGMA page_size={page_size}")
        for i in range(120):
            columns = ", ".join(f"c{i}_{j} TEXT" for j in range(3 if i % 3 else 700))  # every third overflows
            con.execute(f"CREATE TABLE t{i} ({columns})")
            con.execute(f"CREATE INDEX i{i} ON t{i} (c{i}_0)")
        con.commit()
        data = con.serialize()
        assert len(data) // page_size > 2
        assert sqliteutil.schema_tree_problem(data) is None, page_size
        db = SqliteDatabase(data, "the test database")
        assert len(db.tables) == 120
        db.close()
        assert sqliteutil.schema_tree_problem(data, max_pages=2) == "its schema spans more than 2 pages"
        assert sqliteutil.schema_tree_problem(data, max_cells=10) == "its schema has more than 10 entries"
    # an overflow chain that two schema rows share is found too
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA page_size=1024")
    con.execute(f"CREATE TABLE a ({', '.join(f'x{j}' for j in range(400))})")
    con.execute(f"CREATE TABLE b ({', '.join(f'y{j}' for j in range(400))})")
    con.commit()
    data = bytearray(con.serialize())
    assert data[100] == 0x0D and int.from_bytes(data[103:105], "big") == 2  # both rows on page 1

    def overflow_slot(cell: int) -> int:
        at = int.from_bytes(data[108 + 2 * cell:110 + 2 * cell], "big")
        payload, at = sqliteutil._varint(bytes(data), at, 1024)
        _rowid, at = sqliteutil._varint(bytes(data), at, 1024)
        usable = 1024
        low = (usable - 12) * 32 // 255 - 23
        local = low + (payload - low) % (usable - 4)
        return at + (local if local <= usable - 35 else low)

    a, b = overflow_slot(0), overflow_slot(1)
    data[b:b + 4] = data[a:a + 4]  # the second row's chain now starts where the first one's does
    assert "twice" in (sqliteutil.schema_tree_problem(bytes(data)) or "")


def test_work_budget_is_proportional_to_the_database() -> None:
    small = demo_builder().database()
    db = SqliteDatabase(small, "the test database")
    expected = (sqliteutil.STEPS_PER_BYTE * len(small) + sqliteutil.STEP_SLACK) // sqliteutil.PROGRESS_INTERVAL
    assert db._steps[0] == expected
    db.close()
    # a sound database needs a small fraction of its budget: here 3 000 strokes with short records
    b, page = _one_page()
    for i in range(3000):
        b.stroke(page, [(i % 50, 0, 0.5), (i % 50, 1, 0.5)])
    data = b.database()
    budgets: List[Tuple[int, List[int]]] = []
    real_init = SqliteDatabase.__init__

    def init(self, raw, *args, **kw):
        real_init(self, raw, *args, **kw)
        budgets.append((self._steps[0], self._steps))

    try:
        SqliteDatabase.__init__ = init  # type: ignore[method-assign]
        doc = read_ntcbak(data)
    finally:
        SqliteDatabase.__init__ = real_init  # type: ignore[method-assign]
    start, steps = budgets[0]
    used = (start - steps[0]) * sqliteutil.PROGRESS_INTERVAL
    assert len(doc.pages[0].strokes) == 3000
    assert used < len(data) * sqliteutil.STEPS_PER_BYTE // 8, (used, len(data))


def test_sqlite_helper_select() -> None:
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE T (ID INTEGER PRIMARY KEY, Name TEXT, Data BLOB, g TEXT GENERATED ALWAYS AS (upper(Name)));
        CREATE VIEW V AS SELECT * FROM T;
        CREATE INDEX i ON T (Name);
    """)
    con.executemany("INSERT INTO T (ID, Name, Data) VALUES (?, ?, ?)",
                    [(1, "a", b"x"), (2, "b" * 100, b"y" * 100), (3, None, None)])
    con.execute("INSERT INTO T (ID, Name) VALUES (4, CAST(x'61ff62' AS TEXT))")  # not UTF-8
    con.execute("INSERT INTO T (ID, Name) VALUES (5, ?)", ("\x00" + "n" * 20,))  # length() stops at the NUL
    con.commit()
    db = SqliteDatabase(con.serialize(), "the test database")
    assert sqliteutil.is_sqlite(con.serialize()) and sqliteutil.sqlite_available()
    assert db.tables == {"t": "T"} and db.table("t") == "T" and db.count("T") == 5 and db.count("nope") is None
    assert db.columns("T") == {"id": "ID", "name": "Name", "data": "Data"}  # the generated column is hidden
    sel = db.select("t", ("id", "NAME", "data", "missing", "g"), required=("id",), caps={"name": 10})
    assert sel.present and sel.missing == ("missing", "g") and not sel.truncated
    assert sel.rows == [(1, "a", b"x", None, None), (2, TOO_LARGE, b"y" * 100, None, None),
                        (3, None, None, None, None), (4, "a\ufffdb", None, None, None),
                        (5, TOO_LARGE, None, None, None)]
    assert sel.oversized == {"NAME": 2} and list(sel.dicts())[0]["id"] == 1
    sel = db.select("T", ("ID",), max_rows=2, accept=lambda r: r[0] != 1)
    assert sel.rows == [(2,)] and sel.truncated
    assert not db.select("T", ("ID",), required=("Nope",)).present
    assert not db.select("V", ("ID",)).present and db.select("V", ("ID",)).missing == ("ID",)
    with pytest.raises(SqliteError, match="damaged"):
        db._query("INSERT INTO T (ID) VALUES (9)")  # read only
    with db:
        pass
    with pytest.raises(SqliteError, match="closed"):
        db.select("T", ("ID",))
    with pytest.raises(SqliteError, match="no SQLite header"):
        SqliteDatabase(b"nope")
    assert repr(TOO_LARGE) == "TOO_LARGE" and TOO_LARGE is type(TOO_LARGE)()


def test_sqlite_helper_without_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    db = demo_builder().database()
    import types

    monkeypatch.setitem(sys.modules, "sqlite3", types.SimpleNamespace(Connection=type("Connection", (), {})))
    assert not sqliteutil.sqlite_available()
    with pytest.raises(SqliteUnavailable, match="cannot open a database from memory"):
        SqliteDatabase(db)
    with pytest.raises(ValueError, match="cannot open a database from memory"):
        read_ntcbak(db)
    monkeypatch.setitem(sys.modules, "sqlite3", None)
    assert not sqliteutil.sqlite_available()
    with pytest.raises(SqliteUnavailable, match="no sqlite3 module"):
        SqliteDatabase(db)


# --------------------------------------------------------------------------- registry, sniffing, CLI


def test_registry_entry() -> None:
    fmt = formats.get("noteastic")
    assert (fmt.name, fmt.extension, fmt.input_extensions) == ("Noteastic", ".ntcbak", (".ntcbak",))
    assert fmt.readable and not fmt.writable and formats.default_target("noteastic") == "notability"
    ids = list(formats.FORMATS)
    assert ids.index("noteastic") > ids.index("remarkable") and ids.index("noteastic") < ids.index("pdf")
    with pytest.raises(ValueError, match="cannot be written"):
        Options(target="noteastic").validate()


def test_sniffing_backups_and_bare_databases() -> None:
    fmt = formats.get("noteastic")
    backup = demo_builder().backup()
    db = demo_builder().database()
    for data in (backup, db, demo_builder().backup(db_name="Folder\\noteastic.db")):
        assert detect_format("renamed.bin", data) == "noteastic"
        others = [f.id for f in formats.readable() if f.id != "noteastic"
                  and f.sniff(data, formats.sniff_zip_names(data))]
        assert others == []
    assert detect_format("x.ntcbak", b"junk") == "noteastic"  # by extension: the reader explains
    other = sqlite3.connect(":memory:")
    other.executescript("CREATE TABLE pages (id, data); CREATE TABLE Element (ID); CREATE TABLE notes (BookContent);")
    assert not fmt.sniff(other.serialize(), None)
    assert not fmt.sniff(b"SQLite format 3\x00" + b"StrokeElement", ["a"])
    assert not fmt.sniff(b"PK", ["a/b/noteastic.db", "__MACOSX/noteastic.db", "noteastic.db-wal"])
    assert fmt.sniff(b"PK", ["NOTEASTIC.DB"]) and fmt.sniff(b"PK", ["backup/noteastic.db"])


def test_sniffing_a_schema_that_moved_to_the_end_of_the_file() -> None:
    """A schema that outgrows page 1 moves its rows to new pages, anywhere in the file: here
    after 1.2 MB of data, as when an app update adds tables to a grown library."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE Filler (v)")
    con.executemany("INSERT INTO Filler VALUES (?)", (("f" * 1000,) for _ in range(1200)))
    con.executescript("""
        CREATE TABLE "Book" (ID, Name); CREATE TABLE "Page" (ID, Width, Height);
        CREATE TABLE "BookContent" (BookID, PageIndex, PageID); CREATE TABLE "PageContent" (PageID, ElementID);
        CREATE TABLE "Element" (ID, ElementType); CREATE TABLE "StrokeElement" (ElementID, PointsData);
        INSERT INTO Book VALUES (1, 'Grown'); INSERT INTO Page VALUES (1, 800, 600);
        INSERT INTO BookContent VALUES (1, 0, 1);
    """)
    for i in range(40):
        columns = ", ".join(f"c{j}" for j in range(20))
        con.execute(f'CREATE TABLE "Migration{i:02d}" (ID INTEGER PRIMARY KEY, {columns})')
    con.commit()
    db = con.serialize()
    assert b"StrokeElement" not in db[:1 << 20] and len(db) > 1 << 20
    assert formats.get("noteastic").sniff(db, None) and detect_format("noteastic.db", db) == "noteastic"
    assert read_ntcbak(db).title == "Grown"
    area = sqliteutil.schema_area(db)
    assert b"CREATE TABLE \"StrokeElement\"" in area and len(area) < len(db) // 4
    assert sqliteutil.schema_area(db, max_pages=1) == db[:4096]
    assert sqliteutil.schema_area(b"not sqlite") == b"" and sqliteutil.schema_area(db[:16] + bytes(100)) == b""
    bad_size = bytearray(db[:4096])
    bad_size[16:18] = (1000).to_bytes(2, "big")
    assert sqliteutil.schema_area(bytes(bad_size)) == b""


def _sample_files(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)


def test_sniffer_claims_no_other_sample(samples) -> None:
    fmt = formats.get("noteastic")
    root = samples.root
    if not root.is_dir():
        pytest.skip("no samples directory")
    checked = 0
    for path in _sample_files(root):
        if path.stat().st_size > 128 * 1024 * 1024:
            continue
        data = path.read_bytes()
        assert not fmt.sniff(data, formats.sniff_zip_names(data)), path
        checked += 1
    assert checked > 100


def test_cli_convert_and_info(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "Library.ntcbak"
    src.write_bytes(demo_builder().backup())
    assert main(["convert", str(src), "--to", "notability"]) == 0
    out = capsys.readouterr()
    assert "noteastic -> notability" in out.out and "experimental" in out.err
    back = read_note((tmp_path / "Library.note").read_bytes())
    assert back.title == "Demo notebook" and len(back.pages) == 2
    assert main(["convert", str(src), "--to", "pdf", "--pdf-ink", "annotations"]) == 0
    capsys.readouterr()
    pdf = read_pdf((tmp_path / "Library.pdf").read_bytes())
    assert len(pdf.pages) == 2 and sum(len(p.strokes) for p in pdf.pages) >= 14  # the ink as annotations
    assert main(["convert", str(src), "--to", "goodnotes"]) == 0
    capsys.readouterr()
    gn = read_goodnotes((tmp_path / "Library.goodnotes").read_bytes())
    assert len(gn.pages) == 2
    assert main(["info", str(src)]) == 0
    assert "Format:  noteastic" in capsys.readouterr().out
    assert main(["convert", str(src), "--to", "noteastic"]) == 2
    os.remove(src)
