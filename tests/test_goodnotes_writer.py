"""Tests for gnnote.goodnotes.writer (model -> .goodnotes).

Three layers: (1) container structure and the consistency rules of
docs/goodnotes-container.md section 13, checked with gnnote's own primitives; (2) event-log
field sets compared with a GoodNotes-written sample (Test4); (3) read-back with the two
reference parsers, each run in a subprocess with its own PYTHONPATH.
"""
from __future__ import annotations

import io
import json
import math
import os
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from gnnote import applelz4, pdfutil, protobuf as pb, rtf, tpl
from gnnote.goodnotes import constants as C
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.goodnotes.writer import (
    DASH_LENGTH, QUAD_MAX_DEPTH, QUAD_TOLERANCE, _cubic_to_quads, build_members, displayed_box,
    fill_matches_parent, order_key, stroke_to_flat, write_goodnotes,
)
from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun

K = C.CANVAS_PER_POINT
GN_W, GN_H = 455.04, 588.45
A4_W, A4_H = 595.28, 841.89
SLOVAK = "Ahoj svet, ľúbim ťa\nriadok dva"


class Opts:
    def __init__(self, **kw: Any):
        self.paper = "plain"
        self.pressure = True
        self.simplify = 0.0
        self.ribbon = False
        self.title = None
        self.notability_page_width = 574.0
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------------------
# document builders


def white_png(w: int, h: int) -> bytes:
    raw = b"".join(b"\x00" + b"\xff" * (3 * w) for _ in range(h))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def two_page_pdf(w1: float, h1: float, w2: float, h2: float) -> bytes:
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w1:g} {h1:g}] >>".encode(),
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w2:g} {h2:g}] >>".encode(),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def exif_jpeg(orientation: int, width: int, height: int) -> bytes:
    """A minimal JPEG skeleton: SOI, APP1 Exif (orientation tag only), SOF0 (size), EOI."""
    tiff = (b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1)
            + struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack("<I", 0))
    app1 = b"Exif\x00\x00" + tiff
    sof0 = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return (b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
            + b"\xff\xc0" + struct.pack(">H", len(sof0) + 2) + sof0 + b"\xff\xd9")


def ellipse_points(cx: float, cy: float, a: float, b: float, n: int = 64, w: float = 2.0) -> List[Point]:
    return [Point(cx + a * math.cos(2 * math.pi * i / n), cy + b * math.sin(2 * math.pi * i / n), w)
            for i in range(n + 1)]


def filled_ellipse(cx: float, cy: float, a: float, b: float, color=(0.2, 0.6, 0.2, 1.0)) -> Tuple[Stroke, Stroke]:
    """(auto-shape stroke, its fill) the way the reader reports a filled GoodNotes ellipse."""
    pts = ellipse_points(cx, cy, a, b)
    shape = Stroke(pts, color=color, width=2.0)
    outline = [Point(p.x, p.y, 0.0) for p in pts]
    fill = Stroke(list(outline), color=color[:3] + (0.1,), kind="fill", width=0.0, outline=[outline])
    return shape, fill


def wave(x0: float, y0: float, n: int = 20, w: float = 1.5, **kw: Any) -> Stroke:
    pts = [Point(x0 + 8.0 * i, y0 + 10.0 * math.sin(i / 2.0), w) for i in range(n)]
    return Stroke(pts, **kw)


def bezier_stroke() -> Stroke:
    anchors = [Point(30, 300, 2.0), Point(130, 300, 2.0), Point(230, 380, 2.0)]
    controls = [(Point(60, 240), Point(100, 360)), (Point(170, 230), Point(200, 420))]
    return Stroke(anchors, color=(1.0, 0.0, 0.0, 1.0), controls=controls, width=2.0)


def synthetic_document() -> Document:
    """5 pages: standard plain (ink, image, text), A4 lined, two pages of one user PDF, landscape."""
    doc = Document(title="Writer test", source_format="notability")
    doc.pdfs["notes.pdf"] = two_page_pdf(A4_W, A4_H, 400.0, 300.0)
    p1 = Page(GN_W, GN_H, paper="plain")
    p1.strokes = [
        wave(20, 40, color=(0.0, 0.478, 1.0, 1.0)),
        wave(20, 120, n=16, w=8.0, color=(1.0, 1.0, 0.0, 1.0), kind="highlighter"),
        bezier_stroke(),
        Stroke([Point(300, 100, 1.0)]),  # a dot
        Stroke([Point(20, 500 + i, 1.0 + 0.1 * i) for i in range(6)], color=(0, 0, 0, 1)),  # variable width
    ]
    p1.images = [Image(40, 400, 120, 60, white_png(8, 4), "png")]
    p1.texts = [TextBox(50, 480, 220, 40, SLOVAK, runs=[TextRun("Ahoj ", bold=True), TextRun(SLOVAK[5:])],
                        size=12.0)]
    p2 = Page(A4_W, A4_H, paper="lined", strokes=[wave(100, 700, w=2.0)])
    p3 = Page(A4_W, A4_H, background=PdfBackground("notes.pdf", 0), strokes=[wave(100, 100, w=3.0)])
    p4 = Page(400.0, 300.0, background=PdfBackground("notes.pdf", 1))
    p5 = Page(800.0, 500.0, strokes=[wave(50, 250, n=30, w=1.0, color=(0.2, 0.6, 0.2, 1.0))])
    doc.pages = [p1, p2, p3, p4, p5]
    return doc


def uniform_document() -> Document:
    """Pages of one size with long strokes: what goodparse's heuristics can read exactly."""
    doc = Document(title="Uniform")
    # coordinates whose float32 bytes contain no 0x00/0x01: goodparse's flag-run scan would
    # otherwise swallow the start point's bytes and lose the stroke (a heuristic of that parser)
    colours = [(0.0, 0.0, 0.0, 1.0), (1.0, 0.0, 0.0, 1.0), (0.0, 0.5, 1.0, 1.0)]
    p1 = Page(GN_W, GN_H, strokes=[wave(20.13, 60.37 + 80 * i, n=24, w=1.5 + i, color=c) for i, c in enumerate(colours)])
    p2 = Page(GN_W, GN_H, strokes=[wave(30.37, 300.21, n=40, w=2.0, color=(0.1, 0.2, 0.3, 1.0))])
    doc.pages = [p1, p2]
    return doc


# ---------------------------------------------------------------------------------------
# decoding helpers (gnnote primitives only)


def members_of(data: bytes) -> Dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


def events_of(members: Dict[str, bytes]) -> List[Tuple[str, int, List[pb.Field]]]:
    out = []
    for rec in pb.decode_records(members["index.events.pb"]):
        fields = pb.decode_message(rec)
        entity = pb.string_value(fields[0])
        body_field = [f for f in fields if f.number != 1][0]
        out.append((entity, body_field.number, pb.message_value(body_field)))
    return out


def element_pairs(notes: bytes) -> List[Tuple[List[pb.Field], pb.Field]]:
    recs = pb.decode_records(notes)
    assert len(recs) % 2 == 0
    out = []
    for i in range(0, len(recs), 2):
        meta = pb.decode_message(recs[i])
        content = pb.decode_message(recs[i + 1])
        assert len(content) == 1
        out.append((meta, content[0]))
    return out


def stroke_of(content: pb.Field) -> Tuple[List[pb.Field], tpl.FlatStroke]:
    assert content.number == C.CONTENT_STROKE
    body = pb.message_value(content)
    frame = pb.bytes_value(pb.get(body, 2))
    assert applelz4.is_apple_lz4(frame)
    geo = tpl.decode(applelz4.decompress(frame))
    assert isinstance(geo, tpl.FlatStroke)
    return body, geo


def field_numbers(fields: Sequence[pb.Field]) -> frozenset:
    return frozenset(f.number for f in fields)


def dist_to_polyline(p: Tuple[float, float], poly: Sequence[Tuple[float, float]]) -> float:
    best = float("inf")
    if len(poly) == 1:
        return math.hypot(p[0] - poly[0][0], p[1] - poly[0][1])
    for a, b in zip(poly, poly[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
        best = min(best, math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy))
    return best


def cubic_point(p0, c1, c2, p1, t):
    mt = 1 - t
    return (mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
            mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1])


def quad_point(p0, c, p1, t):
    mt = 1 - t
    return (mt * mt * p0[0] + 2 * mt * t * c[0] + t * t * p1[0],
            mt * mt * p0[1] + 2 * mt * t * c[1] + t * t * p1[1])


@pytest.fixture(scope="module")
def written() -> Tuple[Document, bytes, Dict[str, bytes]]:
    doc = synthetic_document()
    data = write_goodnotes(doc, Opts())
    return doc, data, members_of(data)


# ---------------------------------------------------------------------------------------
# 1. container structure


def test_zip_members_and_order(written):
    doc, data, members = written
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.testzip() is None
        names = z.namelist()
        for info in z.infolist():
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert info.create_system == 3
    notes = [n for n in names if n.startswith("notes/")]
    atts = [n for n in names if n.startswith("attachments/")]
    assert len(notes) == 5
    assert len(atts) == 5  # standard paper, A4 lined paper, the user PDF, the PNG, landscape paper
    expected = (["index.search.pb", "index.notes.pb"] + notes + ["index.events.pb", "thumbnail.jpg",
                "index.attachments.pb"] + atts + ["schema.pb"])
    assert names == expected
    assert members["schema.pb"] == b"\x08\x18"
    assert members["index.search.pb"] == b""
    assert members["thumbnail.jpg"] == C.THUMBNAIL_JPEG
    assert not doc.warnings or all("ribbon" not in w for w in doc.warnings)


def test_indexes_match_members(written):
    _doc, _data, members = written
    notes_index = [pb.decode_message(r) for r in pb.decode_records(members["index.notes.pb"])]
    assert [pb.string_value(f[1]) for f in notes_index] == [n for n in members if n.startswith("notes/")]
    for rec in notes_index:
        n = pb.string_value(rec[0])
        assert pb.string_value(rec[1]) == "notes/" + n
        assert len(n) == 36 and n == n.upper()
    att_index = [pb.decode_message(r) for r in pb.decode_records(members["index.attachments.pb"])]
    assert [pb.string_value(f[1]) for f in att_index] == [n for n in members if n.startswith("attachments/")]
    for rec in att_index:
        assert pb.string_value(rec[1]) == "attachments/" + pb.string_value(rec[0])


def test_thumbnail_is_baseline_jpeg():
    data = C.THUMBNAIL_JPEG
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
    assert data[2:4] == b"\xff\xe0" and data[6:10] == b"JFIF"
    markers = []
    i = 2
    while i < len(data) - 1:
        assert data[i] == 0xFF
        marker = data[i + 1]
        markers.append(marker)
        if marker == 0xDA:
            break
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    assert 0xC0 in markers  # SOF0 = baseline
    assert 0xC2 not in markers and 0xC1 not in markers
    assert 0xDB in markers and 0xC4 in markers and markers[-1] == 0xDA
    sof = data.index(b"\xff\xc0")
    precision, height, width, ncomp = struct.unpack(">BHHB", data[sof + 4:sof + 10])
    assert (precision, ncomp) == (8, 3)
    assert (width, height) == C.THUMBNAIL_SIZE


# ---------------------------------------------------------------------------------------
# 2. identifiers, clocks, binding chain


def test_page_and_notes_uuids(written):
    _doc, _data, members = written
    events = events_of(members)
    pages = [(entity, body) for entity, num, body in events if num == 54]
    notes_names = [n.split("/")[1] for n in members if n.startswith("notes/")]
    assert len(pages) == len(notes_names) == 5
    for (entity, body), n in zip(pages, notes_names):
        assert entity == pb.string_value(pb.get(body, 2))
        assert entity[-1] != "F"
        assert n[:35] == entity[:35]
        assert int(n[-1], 16) == int(entity[-1], 16) + 1
    keys = [pb.string_value(pb.message_value(pb.get(body, 4))[0]) for _e, body in pages]
    assert keys == sorted(keys) and len(set(keys)) == 5
    assert keys[0] == order_key(0) == "430001"
    assert order_key(35) == "430010" and order_key(36 ** 3 - 1) == "431000"


def test_device_sequence_and_timestamps(written):
    _doc, _data, members = written
    events = events_of(members)
    devices, seqs, stamps = set(), [], []
    for entity, num, body in events:
        d = {30: 13, 6: 14, 2: 15, 54: 13, 105: 13, 10: 13, 102: 13}[num]
        schema = {30: 20, 2: 21}.get(num, d + 2)
        devices.add(pb.varint_value(pb.get(body, d)))
        seqs.append(pb.varint_value(pb.get(body, d + 1)))
        assert pb.varint_value(pb.get(body, schema)) == 24
        stamps.append(pb.fixed64_float(pb.get(body, 10)))
        assert len(pb.string_value(pb.get(body, 11))) == 36
    assert len(devices) == 1
    device = devices.pop()
    assert 0 < device < 2 ** 63
    assert all(b == a + 1 for a, b in zip(seqs, seqs[1:]))
    assert stamps == sorted(stamps) and stamps[0] > 1.6e12
    for name, blob in members.items():
        if name.startswith("notes/") and blob:
            for meta, _content in element_pairs(blob):
                assert pb.varint_value(pb.get(meta, 8)) == device
                assert pb.varint_value(pb.get(meta, 14)) == 5381
                assert pb.varint_value(pb.get(meta, 16)) == 24
                assert pb.get(meta, 3) is None


def test_element_clocks_and_uuids(written):
    _doc, _data, members = written
    counters = []
    for name, blob in members.items():
        if not name.startswith("notes/") or not blob:
            continue
        for meta, content in element_pairs(blob):
            body = pb.message_value(content)
            assert pb.string_value(meta[0]) == pb.string_value(body[0])
            meta_clock = pb.bytes_value(pb.get(meta, 2))
            assert meta_clock == pb.bytes_value(pb.get(body, 15))
            clock = pb.decode_message(meta_clock)
            if content.number == C.CONTENT_TEXT:
                assert field_numbers(clock) == {2}  # text boxes: nonce only
            else:
                assert pb.varint_value(clock[0]) == 2 and clock[1].number == 2
            counters.append(pb.varint_value(pb.get(meta, 9)))
            if content.number == C.CONTENT_IMAGE:
                assert pb.string_value(pb.get(meta, 4)) == pb.string_value(pb.get(body, 4))
            else:
                assert pb.get(meta, 4) is None
    assert len(counters) == len(set(counters))


def test_binding_chain_and_attachment_sizes(written):
    doc, _data, members = written
    events = events_of(members)
    attachments = {entity: body for entity, num, body in events if num == 6}
    templates = {entity: body for entity, num, body in events if num == 2}
    assert set(attachments) == {n.split("/")[1] for n in members if n.startswith("attachments/")}
    for a, body in attachments.items():
        blob = members["attachments/" + a]
        assert pb.string_value(pb.get(body, 1)) == pb.string_value(pb.get(body, 2)) == a
        assert pb.varint_value(pb.get(body, 5)) == len(blob)
        kind = pb.bytes_value(pb.get(body, 12))
        bound = any(pb.string_value(pb.get(t, 4)) == a for t in templates.values())
        assert kind == (b"\x08\x01\x10\x01" if bound else b"")
        assert bound == blob.startswith(b"%PDF")  # no stickers in this document
    doc_uuid = [entity for entity, num, _b in events if num == 30][0]
    pages = [body for _e, num, body in events if num == 54]
    sizes = []
    for i, body in enumerate(pages):
        assert pb.string_value(pb.get(body, 1)) == doc_uuid
        t = pb.string_value(pb.message_value(pb.get(body, 3))[0])
        tmpl = templates[t]
        a = pb.string_value(pb.get(tmpl, 4))
        assert a in attachments
        blob = members["attachments/" + a]
        assert blob.startswith(b"%PDF")
        info = pdfutil.pdf_info(blob)
        page_index = pb.varint_value(pb.get(tmpl, 5))
        assert 1 <= page_index <= info.page_count
        pdf_page = info.pages[page_index - 1]
        canvas = pb.message_value(pb.get(tmpl, 8))
        cw, ch = pb.fixed32_float(canvas[0]), pb.fixed32_float(canvas[1])
        assert abs(cw - pdf_page.width * K) < 1e-3 and abs(ch - pdf_page.height * K) < 1e-3
        assert pb.varint_value(pb.get(tmpl, 6)) == 1
        assert pb.get(tmpl, 7) is None and pb.get(tmpl, 18) is None
        sizes.append((round(pdf_page.width, 2), round(pdf_page.height, 2)))
        assert pb.bytes_value(pb.message_value(pb.get(body, 17))[0]) == C.PAGE_COLOUR_BLOCK[2:]
    assert sizes == [(GN_W, GN_H), (A4_W, A4_H), (A4_W, A4_H), (400.0, 300.0), (800.0, 500.0)]
    # pages 3 and 4 share one attachment and bind PDF pages 1 and 2
    t3 = pb.string_value(pb.message_value(pb.get(pages[2], 3))[0])
    t4 = pb.string_value(pb.message_value(pb.get(pages[3], 3))[0])
    assert pb.string_value(pb.get(templates[t3], 4)) == pb.string_value(pb.get(templates[t4], 4))
    assert pb.varint_value(pb.get(templates[t3], 5)) == 1 and pb.varint_value(pb.get(templates[t4], 5)) == 2
    assert members["attachments/" + pb.string_value(pb.get(templates[t3], 4))] == doc.pdfs["notes.pdf"]
    assert pb.string_value(pb.get(templates[t3], 9)) == "notes.pdf"
    assert pb.string_value(pb.get(templates[pb.string_value(pb.message_value(pb.get(pages[0], 3))[0])], 9)).endswith(
        "_standard_1_1 - White")
    assert pb.string_value(pb.get(templates[pb.string_value(pb.message_value(pb.get(pages[1], 3))[0])], 9)).endswith(
        "_a4_1_1 - White")


def test_event_order_and_types(written):
    _doc, _data, members = written
    events = events_of(members)
    types = [num for _e, num, _b in events]
    assert types[0] == 30
    assert types[-6:] == [10, 102, 102, 102, 102] or types.count(102) == 4
    first54 = types.index(54)
    assert set(types[1:first54]) == {6, 2}
    assert types[first54:first54 + 5] == [54] * 5
    assert types[first54 + 5:first54 + 10] == [105] * 5
    assert types[first54 + 10] == 10
    assert types[first54 + 11:] == [102] * 4  # page 4 (PDF page only) has no ink
    entity, _n, body = events[0]
    assert pb.string_value(pb.get(body, 1)) == entity
    title = pb.string_value(pb.message_value(pb.get(body, 2))[0])
    assert title == "Writer test"
    for num in (3, 7):
        assert pb.string_value(pb.message_value(pb.get(body, num))[0]) == C.DOCUMENT_CONSTANT_UUID
    assert pb.string_value(pb.message_value(pb.get(body, 6))[0]) == "P"
    current = [body for _e, num, body in events if num == 10][0]
    assert pb.string_value(pb.get(current, 2)) == [e for e, num, _b in events if num == 54][0]
    assert pb.string_value(pb.get(current, 3)).startswith("PagingViewServiceUpdater:")
    notes_with_ink = [n.split("/")[1] for n, b in members.items() if n.startswith("notes/") and b]
    assert [e for e, num, _b in events if num == 102] == notes_with_ink
    for e, num, body in events:
        if num == 102:
            assert pb.string_value(pb.get(body, 16)) == entity
        if num == 105:
            assert pb.varint_value(pb.get(body, 1)) == 1
            assert pb.string_value(pb.get(body, 4)) == e
            assert pb.string_value(pb.get(body, 6)) == "auto"


# ---------------------------------------------------------------------------------------
# 3. event bodies versus a GoodNotes-written file


def test_event_field_sets_match_test4(samples, written):
    sample = samples.repo("goodparse") / "samples" / "Test4.goodnotes"
    if not sample.is_file():
        pytest.skip("Test4.goodnotes not available")
    observed: Dict[int, set] = {}
    for _e, num, body in events_of(members_of(sample.read_bytes())):
        observed.setdefault(num, set()).add(field_numbers(body))
    _doc, _data, members = written
    for _e, num, body in events_of(members):
        if num == 105:
            continue
        assert num in observed, f"event type {num} never written by GoodNotes"
        assert field_numbers(body) in observed[num], (num, sorted(field_numbers(body)), observed[num])
    # the blank-template variant must be the one Test4 record 2 uses (no #7 / #18)
    blank = {field_numbers(b) for _e, n, b in events_of(members) if n == 2}
    assert blank == {frozenset({1, 2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 17, 19, 21})}


def test_event_105_matches_record_sample(samples, written):
    sample = samples.repo("parser-for-goodnotes") / "assets" / "record.goodnotes"
    if not sample.is_file():
        pytest.skip("record.goodnotes not available")
    observed = {field_numbers(b) - {5} for _e, n, b in events_of(members_of(sample.read_bytes()))
                if n == 105 and pb.get(b, 4) is not None}
    assert observed
    _doc, _data, members = written
    mine = {field_numbers(b) for _e, n, b in events_of(members) if n == 105}
    assert mine == {frozenset({1, 2, 4, 6, 10, 11, 13, 14, 15})}
    assert mine <= observed


# ---------------------------------------------------------------------------------------
# 4. element records


def test_stroke_records(written):
    doc, _data, members = written
    notes = [b for n, b in members.items() if n.startswith("notes/")]
    pairs = element_pairs(notes[0])
    strokes = [(meta, c) for meta, c in pairs if c.number == C.CONTENT_STROKE]
    assert len(strokes) == 5
    kinds = [c.number for _m, c in pairs]
    assert kinds == [1] + [7] * 5 + [8]  # image, strokes, text (z-order)
    page = doc.pages[0]
    for i, ((meta, content), stroke) in enumerate(zip(strokes, page.strokes)):
        body, geo = stroke_of(content)
        assert field_numbers(body) <= {1, 2, 4, 5, 6, 7, 9, 15, 20, 21}
        assert pb.get(body, 3) is None and pb.get(body, 14) is None
        assert pb.varint_value(pb.get(body, 21)) == 24
        assert pb.bytes_value(pb.get(body, 6)) == b"" and pb.bytes_value(pb.get(body, 9)) == b""
        assert pb.bytes_value(pb.get(body, 20)) == b""
        f7 = pb.message_value(pb.message_value(pb.get(body, 7))[0])
        assert pb.varint_value(f7[0]) == i + 1 and f7[1].number == 2
        colour = {f.number: pb.fixed32_float(f) for f in pb.message_value(pb.get(body, 4))}
        expected = list(stroke.color)
        if stroke.kind == "highlighter":
            expected[3] = 0.5
            assert pb.varint_value(pb.get(body, 5)) == 1
        else:
            assert pb.get(body, 5) is None
        for n, v in enumerate(expected, 1):
            if v == 0.0:
                assert n not in colour
            else:
                assert abs(colour[n] - v) < 1e-6
        widths = sorted(p.width for p in stroke.points)
        median = widths[len(widths) // 2] if len(widths) % 2 else (widths[len(widths) // 2 - 1] + widths[len(widths) // 2]) / 2
        assert abs(geo.width - 2 * median) < 1e-4
        assert geo.effective_flags() == [0] + [1] * len(geo.quads)
        assert len(geo.polyline()) % 2 == 1
        frame = pb.bytes_value(pb.get(body, 2))
        assert frame.startswith(b"bv41") and frame.endswith(b"bv4$")
    # polyline strokes: start + segment ends are exactly the input points
    body, geo = stroke_of(strokes[0][1])
    assert len(geo.quads) == 19
    for (x, y), p in zip(geo.anchors(), page.strokes[0].points):
        assert abs(x - p.x * K) < 1e-3 and abs(y - p.y * K) < 1e-3
    for (cx, cy, ex, ey), a, b in zip(geo.quads, page.strokes[0].points, page.strokes[0].points[1:]):
        assert abs(cx - (a.x + b.x) / 2 * K) < 1e-3 and abs(cy - (a.y + b.y) / 2 * K) < 1e-3
    # the dot is a 0.3-unit dash
    _body, dot = stroke_of(strokes[3][1])
    assert len(dot.quads) == 1 and abs(dot.quads[0][2] - dot.start[0] - DASH_LENGTH) < 1e-4
    assert any("median" in w for w in doc.warnings)


def test_bezier_stroke_geometry(written):
    doc, _data, members = written
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    strokes = [c for _m, c in element_pairs(notes) if c.number == C.CONTENT_STROKE]
    _body, geo = stroke_of(strokes[2])
    src = doc.pages[0].strokes[2]
    anchors = [(p.x * K, p.y * K) for p in src.points]
    # every quad end lies on one of the source cubics
    ends = [geo.start] + [(ex, ey) for _cx, _cy, ex, ey in geo.quads]
    assert ends[0] == pytest.approx(anchors[0], abs=1e-3) and ends[-1] == pytest.approx(anchors[-1], abs=1e-3)
    cubics = [[cubic_point(anchors[i], (c1.x * K, c1.y * K), (c2.x * K, c2.y * K), anchors[i + 1], t / 400)
               for t in range(401)] for i, (c1, c2) in enumerate(src.controls)]
    for e in ends:
        assert min(dist_to_polyline(e, c) for c in cubics) < 0.02
    # and the quad chain never deviates more than the tolerance from the cubic chain
    chain = []
    prev = geo.start
    for cx, cy, ex, ey in geo.quads:
        chain += [quad_point(prev, (cx, cy), (ex, ey), t / 40) for t in range(41)]
        prev = (ex, ey)
    for i, (c1, c2) in enumerate(src.controls):
        for t in range(0, 101):
            s = cubic_point(anchors[i], (c1.x * K, c1.y * K), (c2.x * K, c2.y * K), anchors[i + 1], t / 100)
            assert dist_to_polyline(s, chain) < QUAD_TOLERANCE + 0.05


def test_cubic_to_quads_tolerance_and_depth():
    import random
    rng = random.Random(7)
    for _ in range(50):
        p0, c1, c2, p1 = [(rng.uniform(0, 800), rng.uniform(0, 800)) for _ in range(4)]
        quads = _cubic_to_quads(p0, c1, c2, p1)
        assert 1 <= len(quads) <= 2 ** QUAD_MAX_DEPTH
        assert quads[-1][2:] == p1
        chain, prev = [], p0
        for cx, cy, ex, ey in quads:
            chain += [quad_point(prev, (cx, cy), (ex, ey), t / 64) for t in range(65)]
            prev = (ex, ey)
        if len(quads) < 2 ** QUAD_MAX_DEPTH:
            for t in range(101):
                assert dist_to_polyline(cubic_point(p0, c1, c2, p1, t / 100), chain) < QUAD_TOLERANCE + 0.02
    # a straight cubic is one quad with the control at the design formula
    quads = _cubic_to_quads((0, 0), (10, 0), (20, 0), (30, 0))
    assert quads == [(15.0, 0.0, 30.0, 0.0)]
    # a wild cubic hits the depth cap instead of recursing forever
    assert len(_cubic_to_quads((0, 0), (5000, 5000), (-5000, 5000), (0, 0))) == 2 ** QUAD_MAX_DEPTH


def test_stroke_to_flat_degenerate_cases():
    dash = stroke_to_flat(Stroke([Point(10, 10, 1), Point(10, 10, 1), Point(10, 10, 1)]), K, K, 2.0)
    assert len(dash.quads) == 1 and abs(dash.quads[0][2] - dash.start[0] - DASH_LENGTH) < 1e-6
    same = Stroke([Point(5, 5, 1), Point(5, 5, 1)], controls=[(Point(5, 5), Point(5, 5))])
    flat = stroke_to_flat(same, K, K, 2.0)
    assert len(flat.quads) == 1 and flat.quads[0][2] > flat.start[0]
    bad = Stroke([Point(0, 0, 1), Point(10, 0, 1), Point(20, 0, 1)], controls=[(Point(1, 1), Point(2, 2))])
    assert len(stroke_to_flat(bad, 1.0, 1.0, 2.0).quads) == 2  # inconsistent controls -> polyline
    with pytest.raises(ValueError):
        stroke_to_flat(Stroke([]), 1.0, 1.0, 2.0)


def test_image_and_text_records(written):
    doc, _data, members = written
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    pairs = element_pairs(notes)
    image = doc.pages[0].images[0]
    meta, content = [(m, c) for m, c in pairs if c.number == C.CONTENT_IMAGE][0]
    body = pb.message_value(content)
    assert field_numbers(body) == {1, 2, 3, 4, 5, 15, 18}
    att = pb.string_value(pb.get(body, 4))
    assert members["attachments/" + att] == image.data
    rect = pb.message_value(pb.get(body, 2))
    origin = [pb.fixed32_float(f) for f in pb.message_value(rect[0])]
    size = [pb.fixed32_float(f) for f in pb.message_value(rect[1])]
    assert origin == pytest.approx([image.x * K, image.y * K], abs=1e-3)
    assert size == pytest.approx([image.w * K, image.h * K], abs=1e-3)
    crop = pb.message_value(pb.get(body, 3))
    centre = [pb.fixed32_float(f) for f in pb.message_value(crop[0])]
    assert centre == pytest.approx([(image.x + image.w / 2) * K, (image.y + image.h / 2) * K], abs=1e-2)
    assert [pb.fixed32_float(f) for f in pb.message_value(crop[1])] == pytest.approx(size, abs=1e-3)
    assert pb.varint_value(pb.get(body, 18)) == 24
    # the raster attachment is declared with an empty #12
    att_event = [b for _e, n, b in events_of(members) if n == 6 and pb.string_value(pb.get(b, 1)) == att][0]
    assert pb.bytes_value(pb.get(att_event, 12)) == b""

    box = doc.pages[0].texts[0]
    meta, content = [(m, c) for m, c in pairs if c.number == C.CONTENT_TEXT][0]
    body = pb.message_value(content)
    assert field_numbers(body) == {1, 2, 3, 4, 5, 6, 7, 9, 10, 15, 18, 19, 20, 21, 27}
    outer = pb.message_value(pb.get(body, 2))
    inner = pb.message_value(pb.get(body, 3))
    ox, oy = [pb.fixed32_float(f) for f in pb.message_value(outer[0])]
    ow, oh = [pb.fixed32_float(f) for f in pb.message_value(outer[1])]
    ix, iy = [pb.fixed32_float(f) for f in pb.message_value(inner[0])]
    iw, ih = [pb.fixed32_float(f) for f in pb.message_value(inner[1])]
    assert (ix, iy) == pytest.approx((box.x * K, box.y * K), abs=1e-3)
    assert iw == pytest.approx(box.w * K, abs=1e-3) and ih >= box.h * K - 1e-3
    assert (ox, oy, ow, oh) == pytest.approx((ix - 10, iy - 10, iw + 20, ih + 20), abs=1e-3)
    assert pb.fixed32_float(pb.get(body, 10)) == 10.0
    assert pb.string_value(pb.get(body, 20)) == ".tb-0"
    assert pb.varint_value(pb.get(body, 27)) == 24
    payload = pb.bytes_value(pb.get(body, 6))
    assert payload.startswith(b"{\\rtf1\\ansi\\ansicpg1252\\cocoartf")
    assert all(b < 128 for b in payload)
    text, runs = rtf.parse_rtf(payload)
    assert text == SLOVAK
    assert runs[0].bold and runs[0].text == "Ahoj "
    assert abs(runs[0].size - 12.0 * K) < 0.5  # canvas units


def fill_of(content: pb.Field) -> Dict[int, Any]:
    """Decoded fields of a ``#9`` fill body: bbox, polygon, parent, colour, clocks."""
    assert content.number == C.CONTENT_FILL
    body = pb.message_value(content)
    rect = pb.message_value(pb.get(body, 2))
    geometry = pb.message_value(pb.get(body, 4))
    polygon_field = pb.get(geometry, 1)
    polygon = [tuple(pb.fixed32_float(f) for f in pb.message_value(pt))
               for pt in pb.message_value(polygon_field)]
    return {
        "uuid": pb.string_value(pb.get(body, 1)),
        "fields": field_numbers(body),
        "geometry_fields": field_numbers(geometry),
        "bbox": [pb.fixed32_float(f) for f in pb.message_value(rect[0])]
                + [pb.fixed32_float(f) for f in pb.message_value(rect[1])],
        "edit_clock": pb.decode_message(pb.message_value(pb.get(body, 3))[0].value),
        "polygon": polygon,
        "parent": pb.string_value(pb.get(body, 5)),
        "colour": {f.number: pb.fixed32_float(f) for f in pb.message_value(pb.get(body, 7))},
        "clock": pb.bytes_value(pb.get(body, 15)),
        "schema": pb.varint_value(pb.get(body, 18)),
    }


def test_fill_record_follows_its_parent():
    shape, fill = filled_ellipse(150, 200, 60, 40)
    doc = Document(title="Fills")
    doc.pages = [Page(GN_W, GN_H, strokes=[shape, fill, wave(20, 400)])]
    members = members_of(write_goodnotes(doc, Opts()))
    assert not any("fill" in w for w in doc.warnings)
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    pairs = element_pairs(notes)
    assert [c.number for _m, c in pairs] == [7, 9, 7]
    parent_body, _geo = stroke_of(pairs[0][1])
    meta, content = pairs[1]
    f = fill_of(content)
    # record layout of goodnotes-v35-elements.md section 1.2
    assert f["fields"] == {1, 2, 3, 4, 5, 6, 7, 15, 18}
    assert f["uuid"] == pb.string_value(meta[0]) and len(f["uuid"]) == 36
    assert f["parent"] == pb.string_value(parent_body[0])
    assert f["geometry_fields"] == {1}  # closed polygon form
    assert pb.bytes_value(pb.get(pb.message_value(content), 6)) == b""
    assert f["schema"] == 24
    # the polygon is the outline in canvas units, first point repeated last
    outline = fill.outline[0]
    assert len(f["polygon"]) == len(outline)  # 65 sampled points already close the ring
    assert f["polygon"][0] == pytest.approx(f["polygon"][-1], abs=1e-4)
    for (x, y), p in zip(f["polygon"], outline):
        assert (x, y) == pytest.approx((p.x * K, p.y * K), abs=1e-3)
    xs = [x for x, _y in f["polygon"]]
    ys = [y for _x, y in f["polygon"]]
    assert f["bbox"] == pytest.approx([min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)], abs=1e-3)
    # colour with the fill alpha, 0.0 components omitted
    assert f["colour"] == pytest.approx({1: 0.2, 2: 0.6, 3: 0.2, 4: 0.1}, abs=1e-6)
    # clocks: #3 {#1 {#1 1, #2 nonce}}; metadata #2 == #15 == {#2 nonce} (no version)
    assert [(e.number, e.value) for e in f["edit_clock"]][0] == (1, 1) and f["edit_clock"][1].number == 2
    assert f["clock"] == pb.bytes_value(pb.get(meta, 2))
    assert field_numbers(pb.decode_message(f["clock"])) == {2}
    assert field_numbers(meta) == {1, 2, 8, 9, 14, 16}
    assert pb.varint_value(pb.get(meta, 16)) == 24
    # the fill does not consume a draw index: the next stroke is #2
    next_body, _geo = stroke_of(pairs[2][1])
    assert pb.varint_value(pb.message_value(pb.message_value(pb.get(next_body, 7))[0])[0]) == 2


def test_fill_before_its_parent_is_written_after_it():
    shape, fill = filled_ellipse(150, 200, 60, 40)
    doc = Document(pages=[Page(GN_W, GN_H, strokes=[fill, shape, wave(20, 400)])])
    members = members_of(write_goodnotes(doc, Opts()))
    assert not any("fill" in w for w in doc.warnings)
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    pairs = element_pairs(notes)
    assert [c.number for _m, c in pairs] == [7, 9, 7]
    parent_body, _geo = stroke_of(pairs[0][1])
    assert fill_of(pairs[1][1])["parent"] == pb.string_value(parent_body[0])


def test_fill_field_sets_match_test7(samples):
    sample = samples.repo("goodparse") / "samples" / "Test7.goodnotes"
    if not sample.is_file():
        pytest.skip("Test7.goodnotes not available")
    observed = set()
    geometry_forms = set()
    for name, blob in members_of(sample.read_bytes()).items():
        if not name.startswith("notes/") or not blob:
            continue
        recs = pb.decode_records(blob)
        for i in range(0, len(recs) - 1, 2):
            meta = pb.decode_message(recs[i])
            content = pb.decode_message(recs[i + 1])
            if len(content) == 1 and content[0].number == C.CONTENT_FILL and pb.get(meta, 3) is None:
                body = pb.message_value(content[0])
                observed.add(field_numbers(body))
                geometry_forms.add(field_numbers(pb.message_value(pb.get(body, 4))))
    assert observed
    shape, fill = filled_ellipse(100, 100, 30, 30)
    doc = Document(pages=[Page(GN_W, GN_H, strokes=[shape, fill])])
    notes = [b for n, b in members_of(write_goodnotes(doc, Opts())).items() if n.startswith("notes/")][0]
    _meta, content = element_pairs(notes)[1]
    assert field_numbers(pb.message_value(content)) in observed
    assert fill_of(content)["geometry_fields"] in geometry_forms


def test_fills_without_a_parent_are_skipped():
    shape, fill = filled_ellipse(150, 200, 60, 40)
    _other, orphan = filled_ellipse(300, 400, 20, 20)
    empty = Stroke([], kind="fill", outline=[[]])
    doc = Document(title="Orphans")
    # a fill with no matching neighbour (twice), two in a row before a stroke, one without an
    # outline, a good one, and one left over at the end of the page
    doc.pages = [Page(GN_W, GN_H, strokes=[orphan, wave(20, 40), orphan, empty, shape, fill, orphan, orphan])]
    members = members_of(write_goodnotes(doc, Opts()))
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    assert [c.number for _m, c in element_pairs(notes)] == [7, 7, 9]
    assert "4 shape fills were skipped (no neighbouring stroke matches them; GoodNotes binds a fill to the stroke it fills)" in doc.warnings
    assert "1 shape fills without an outline were skipped" in doc.warnings
    assert fill_matches_parent(fill.outline[0], shape)
    assert not fill_matches_parent(orphan.outline[0], shape)
    assert not fill_matches_parent(fill.outline[0], fill)  # a fill is never a parent
    # a fill with several polygons keeps the first one and says so
    multi = Stroke(list(fill.outline[0]), color=fill.color, kind="fill",
                   outline=[fill.outline[0], [Point(0, 0), Point(1, 0), Point(1, 1)]])
    doc = Document(pages=[Page(GN_W, GN_H, strokes=[shape, multi])])
    notes = [b for n, b in members_of(write_goodnotes(doc, Opts())).items() if n.startswith("notes/")][0]
    assert len(fill_of(element_pairs(notes)[1][1])["polygon"]) == len(fill.outline[0])
    assert any("several polygons" in w for w in doc.warnings)


def test_pdf_sticker_image():
    sticker = two_page_pdf(254, 214, 254, 214)[:0] + pdfutil.make_paper_pdf(254, 214, "plain")
    doc = Document(title="Sticker")
    doc.pages = [Page(GN_W, GN_H, images=[Image(63.08, 31.55, 138.55, 116.73, sticker, "pdf")])]
    members = members_of(write_goodnotes(doc, Opts()))
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    meta, content = element_pairs(notes)[0]
    body = pb.message_value(content)
    assert content.number == C.CONTENT_IMAGE
    assert field_numbers(body) == {1, 2, 3, 4, 5, 6, 15, 18}
    assert pb.varint_value(pb.get(body, 6)) == 3
    att = pb.string_value(pb.get(body, 4))
    assert members["attachments/" + att] == sticker and pb.string_value(pb.get(meta, 4)) == att
    rect = pb.message_value(pb.get(body, 2))
    assert [pb.fixed32_float(f) for f in pb.message_value(rect[0])] == pytest.approx([63.08 * K, 31.55 * K], abs=1e-3)
    assert [pb.fixed32_float(f) for f in pb.message_value(rect[1])] == pytest.approx([138.55 * K, 116.73 * K], abs=1e-3)
    events = events_of(members)
    att_event = [b for _e, n, b in events if n == 6 and pb.string_value(pb.get(b, 1)) == att][0]
    assert pb.bytes_value(pb.get(att_event, 12)) == b""  # like a raster, not like paper
    assert pb.varint_value(pb.get(att_event, 5)) == len(sticker)
    # the sticker is not a template: only the generated paper binds a page
    assert len([1 for _e, n, _b in events if n == 2]) == 1
    assert len([n for n in members if n.startswith("attachments/")]) == 2
    assert not any("PDF" in w and "display" in w for w in doc.warnings)
    # a JPEG gets #6 = 1, a PNG no #6 at all
    doc = Document(pages=[Page(GN_W, GN_H, images=[Image(0, 0, 10, 10, exif_jpeg(1, 4, 4), "jpeg"),
                                                   Image(0, 0, 10, 10, white_png(4, 4), "png")])])
    notes = [b for n, b in members_of(write_goodnotes(doc, Opts())).items() if n.startswith("notes/")][0]
    kinds = [pb.get(pb.message_value(c), 6) for _m, c in element_pairs(notes)]
    assert pb.varint_value(kinds[0]) == 1 and kinds[1] is None


def test_exif_rotated_jpeg_keeps_bytes_and_its_displayed_box():
    # landscape pixels, EXIF says "rotate 90 degrees clockwise": the model's box already is the
    # displayed (portrait) box, as the GoodNotes reader produces it (design.md 4.1)
    photo = exif_jpeg(6, 96, 64)
    image = Image(116, 34, 64, 96, photo, "jpeg", rotation=90.0)
    assert displayed_box(image) == pytest.approx((116, 34, 64, 96))
    doc = Document(pages=[Page(GN_W, GN_H, images=[image])])
    members = members_of(write_goodnotes(doc, Opts()))
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    body = pb.message_value(element_pairs(notes)[0][1])
    rect = pb.message_value(pb.get(body, 2))
    crop = pb.message_value(pb.get(body, 3))
    assert [pb.fixed32_float(f) for f in pb.message_value(rect[0])] == pytest.approx([116 * K, 34 * K], abs=1e-3)
    assert [pb.fixed32_float(f) for f in pb.message_value(rect[1])] == pytest.approx([64 * K, 96 * K], abs=1e-3)
    assert [pb.fixed32_float(f) for f in pb.message_value(crop[0])] == pytest.approx([148 * K, 82 * K], abs=1e-3)
    assert pb.get(crop, 3) is None  # no #3.#3 rotation: unverified, never written
    assert members["attachments/" + pb.string_value(pb.get(body, 4))] == photo
    assert any("EXIF" in w for w in doc.warnings) and not any("dropped" in w for w in doc.warnings)
    # 270 degrees with orientation 8, 180 with 3, -270 with 6: the box stays as it is
    assert displayed_box(Image(116, 34, 64, 96, exif_jpeg(8, 96, 64), rotation=270.0)) == pytest.approx((116, 34, 64, 96))
    assert displayed_box(Image(100, 50, 96, 64, exif_jpeg(3, 96, 64), rotation=180.0)) == pytest.approx((100, 50, 96, 64))
    assert displayed_box(Image(116, 34, 64, 96, exif_jpeg(6, 96, 64), rotation=-270.0)) == pytest.approx((116, 34, 64, 96))


def test_image_rotation_without_matching_exif_is_dropped():
    cases = [
        Image(100, 50, 96, 64, white_png(4, 4), "png", rotation=90.0),  # PNG: no EXIF
        Image(100, 50, 96, 64, exif_jpeg(1, 96, 64), "jpeg", rotation=90.0),  # EXIF upright
        Image(100, 50, 96, 64, exif_jpeg(8, 96, 64), "jpeg", rotation=90.0),  # EXIF says the other way
        Image(100, 50, 96, 64, exif_jpeg(6, 96, 64), "jpeg", rotation=17.0),  # not a quarter turn
    ]
    for image in cases:
        assert displayed_box(image) == (100, 50, 96, 64)
    doc = Document(pages=[Page(GN_W, GN_H, images=cases)])
    members = members_of(write_goodnotes(doc, Opts()))
    assert "4 image rotations were dropped (GoodNotes has no verified image rotation field)" in doc.warnings
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    for _meta, content in element_pairs(notes):
        rect = pb.message_value(pb.get(pb.message_value(content), 2))
        assert [pb.fixed32_float(f) for f in pb.message_value(rect[1])] == pytest.approx([96 * K, 64 * K], abs=1e-3)
    assert displayed_box(Image(0, 0, 10, 10, white_png(4, 4), rotation=360.0)) == (0, 0, 10, 10)


def test_text_alignment_and_rotation():
    boxes = [TextBox(50, 80, 200, 30, "centred", align="center", rotation=12.0),
             TextBox(50, 120, 200, 30, "right", align="right"),
             TextBox(50, 160, 200, 30, "left", align="left")]
    doc = Document(pages=[Page(GN_W, GN_H, texts=boxes)])
    members = members_of(write_goodnotes(doc, Opts()))
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    payloads = [pb.bytes_value(pb.get(pb.message_value(c), 6)) for _m, c in element_pairs(notes)]
    assert b"\\qc\\pardirnatural" in payloads[0] and b"\\qr" not in payloads[0]
    assert b"\\qr\\pardirnatural" in payloads[1] and b"\\qc" not in payloads[1]
    assert b"\\qc" not in payloads[2] and b"\\qr" not in payloads[2]
    assert payloads[0].index(b"\\pard") < payloads[0].index(b"\\qc") < payloads[0].index(b"\\pardirnatural")
    for payload, box in zip(payloads, boxes):
        text, _runs = rtf.parse_rtf(payload)
        assert text == box.text
    assert "1 text box rotations were dropped (GoodNotes RTF text boxes cannot be rotated)" in doc.warnings
    # the frame of the rotated box is written unrotated at the model's top-left corner
    inner = pb.message_value(pb.get(pb.message_value(element_pairs(notes)[0][1]), 3))
    assert [pb.fixed32_float(f) for f in pb.message_value(inner[0])] == pytest.approx([50 * K, 80 * K], abs=1e-3)


# ---------------------------------------------------------------------------------------
# 5. options and edge cases


def test_ribbon_option_falls_back_to_flat():
    doc = Document(title="Ribbon")
    doc.pages = [Page(GN_W, GN_H, strokes=[Stroke([Point(10, 10, 1.0), Point(50, 20, 2.0), Point(90, 10, 3.0)])])]
    members = members_of(write_goodnotes(doc, Opts(ribbon=True, title="Renamed")))
    assert any("ribbon" in w for w in doc.warnings)
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    body, geo = stroke_of(element_pairs(notes)[0][1])
    assert pb.get(body, 3) is None and abs(geo.width - 4.0) < 1e-6
    title = pb.string_value(pb.message_value(pb.get(events_of(members)[0][2], 2))[0])
    assert title == "Renamed"


def test_empty_document_gets_one_page():
    doc = Document()
    members = members_of(write_goodnotes(doc, None))
    assert len([n for n in members if n.startswith("notes/")]) == 1
    assert any("no pages" in w for w in doc.warnings)
    tmpl = [b for _e, n, b in events_of(members) if n == 2][0]
    canvas = [pb.fixed32_float(f) for f in pb.message_value(pb.get(tmpl, 8))]
    assert canvas == pytest.approx([GN_W * K, GN_H * K], abs=1e-3)
    assert [n for _e, n, _b in events_of(members)].count(102) == 0


def test_missing_pdf_and_bad_paper_fall_back():
    doc = Document(title="Fallbacks")
    doc.pdfs["p"] = two_page_pdf(300, 400, 300, 400)
    doc.pages = [Page(300, 400, background=PdfBackground("gone", 0)),
                 Page(300, 400, background=PdfBackground("p", 5)),
                 Page(300, 400, paper="polka"),
                 Page(300, 400, paper="grid"), Page(300, 400, paper="grid")]
    members = members_of(write_goodnotes(doc, Opts()))
    assert any("missing" in w for w in doc.warnings)
    assert any("does not exist" in w for w in doc.warnings)
    assert any("polka" in w for w in doc.warnings)
    pdfs = [n for n, b in members.items() if n.startswith("attachments/")]
    # generated plain paper (shared by pages 1-3) + grid paper (shared by pages 4-5)
    assert len(pdfs) == 2
    templates = [b for _e, n, b in events_of(members) if n == 2]
    assert len(templates) == 2


def test_pdf_page_size_mismatch_is_scaled():
    doc = Document(title="Scale")
    doc.pdfs["p"] = two_page_pdf(400, 300, 400, 300)
    doc.pages = [Page(200, 150, background=PdfBackground("p", 0),
                      strokes=[Stroke([Point(0, 0, 1), Point(200, 150, 1)])])]
    members = members_of(write_goodnotes(doc, Opts()))
    assert any("scaled" in w for w in doc.warnings)
    notes = [b for n, b in members.items() if n.startswith("notes/")][0]
    _body, geo = stroke_of(element_pairs(notes)[0][1])
    end = geo.quads[-1][2:]
    assert end == pytest.approx((400 * K, 300 * K), abs=1e-2)


def test_build_members_matches_zip(written):
    doc = synthetic_document()
    members = build_members(doc, Opts())
    names = [n for n, _b in members]
    assert names[0] == "index.search.pb" and names[-1] == "schema.pb"
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------------------
# 6. oracle parsers (subprocess, own PYTHONPATH)

PFG_SCRIPT = r"""
import json, sys
import oracle_shims
oracle_shims.frame_parser_for_goodnotes()
from goodnotes_re import GoodNotesDocument
out = []
with GoodNotesDocument.open(sys.argv[1]) as doc:
    for p in doc.pages():
        out.append({
            "index": p.index, "width": p.dimensions.width, "height": p.dimensions.height,
            "background": p.background_attachment_path, "pdf_page": p.pdf_page_index,
            "strokes": [{"color": s.color_hex, "alpha": s.alpha, "width": s.width,
                         "highlighter": s.is_highlighter, "format": s.tpl_format,
                         "points": [[q.x, q.y, q.pressure] for q in s.points]} for s in p.strokes],
            "images": [[im.x, im.y, im.width, im.height] for im in p.image_elements],
            "texts": [[t.format, t.text] for t in p.text_fragments],
        })
print(json.dumps(out))
"""

GOODPARSE_SCRIPT = r"""
import json, sys
import oracle_shims
oracle_shims.guard_goodparse()
from goodparse import parse_goodnotes
d = parse_goodnotes(sys.argv[1])
out = []
for p in d.pages:
    out.append({"width": p.width, "height": p.height, "scale": p.scale,
                "strokes": [{"kind": s.kind, "color": list(s.color), "points": [list(pt) for pt in s.points]}
                            for s in p.strokes],
                "images": [[list(im.position), list(im.size)] for im in p.images],
                "texts": [[list(t.position), list(t.size), t.plain] for t in p.texts]})
print(json.dumps(out))
"""


def run_oracle(samples, repo: str, src: str, script: str, path: Path) -> Any:
    """``script``'s JSON output for ``path``; the scripts call tests/oracle_shims.py first."""
    root = samples.repo(repo) / src
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), str(Path(__file__).resolve().parent)]))
    proc = subprocess.run([sys.executable, "-c", script, str(path)], env=env, capture_output=True, text=True,
                          timeout=300)
    if proc.returncode != 0:
        if "ModuleNotFoundError" in proc.stderr and repo not in proc.stderr.split("ModuleNotFoundError")[-1]:
            pytest.skip(f"{repo} dependency missing: {proc.stderr.strip().splitlines()[-1]}")
        pytest.fail(f"{repo} failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def hex_colour(c) -> str:
    return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in c[:3])


def test_oracle_parser_for_goodnotes(samples, written, tmp_path):
    doc, data, members = written
    path = tmp_path / "written.goodnotes"
    path.write_bytes(data)
    pages = run_oracle(samples, "parser-for-goodnotes", "src", PFG_SCRIPT, path)
    assert len(pages) == 5
    # parser-for-goodnotes sizes every page of a multi-page PDF by the PDF's first MediaBox,
    # so page 4 (PDF page 2, 400 x 300) reports A4 there; test_binding_chain checks its canvas.
    expect_sizes = [(GN_W, GN_H), (A4_W, A4_H), (A4_W, A4_H), (A4_W, A4_H), (800.0, 500.0)]
    for p, (w, h) in zip(pages, expect_sizes):
        assert (p["width"], p["height"]) == pytest.approx((w, h), abs=0.01)
        assert p["background"] and members[p["background"]].startswith(b"%PDF")
    assert pages[2]["background"] == pages[3]["background"] and members[pages[2]["background"]] == doc.pdfs["notes.pdf"]
    assert [p["pdf_page"] for p in pages] == [1, 1, 1, 2, 1]
    assert pages[0]["background"] != pages[1]["background"]
    assert [len(p["strokes"]) for p in pages] == [5, 1, 1, 0, 1]
    for p, page in zip(pages, doc.pages):
        for s, stroke in zip(p["strokes"], page.strokes):
            assert s["format"] == tpl.FLAT_FORMAT
            assert s["color"] == hex_colour(stroke.color)
            assert s["highlighter"] == (stroke.kind == "highlighter")
            assert s["alpha"] == pytest.approx(0.5 if stroke.kind == "highlighter" else stroke.color[3], abs=1e-6)
            widths = sorted(q.width for q in stroke.points)
            median = widths[len(widths) // 2] if len(widths) % 2 else (widths[len(widths) // 2 - 1] + widths[len(widths) // 2]) / 2
            assert s["width"] == pytest.approx(2 * median, abs=1e-4)
            if stroke.controls is None:
                poly = [(q.x * K, q.y * K) for q in stroke.points]
                for x, y, _pr in s["points"]:
                    assert dist_to_polyline((x, y), poly) < 0.6 or len(poly) == 1
                if len(poly) > 1:
                    assert len(s["points"]) == 2 * len(poly) - 1
                    assert (s["points"][0][0], s["points"][0][1]) == pytest.approx(poly[0], abs=1e-3)
                    assert (s["points"][-1][0], s["points"][-1][1]) == pytest.approx(poly[-1], abs=1e-3)
    # Bezier stroke: segment ends on the source cubics (parser points = start, (c, e) pairs)
    bez = pages[0]["strokes"][2]["points"]
    src = doc.pages[0].strokes[2]
    anchors = [(q.x * K, q.y * K) for q in src.points]
    cubics = [[cubic_point(anchors[i], (c1.x * K, c1.y * K), (c2.x * K, c2.y * K), anchors[i + 1], t / 300)
               for t in range(301)] for i, (c1, c2) in enumerate(src.controls)]
    for x, y, _pr in [bez[0]] + bez[2::2]:
        assert min(dist_to_polyline((x, y), c) for c in cubics) < 0.02
    # image and text
    image = doc.pages[0].images[0]
    assert pages[0]["images"] == [pytest.approx([image.x * K, image.y * K, image.w * K, image.h * K], abs=1e-2)]
    # that parser's RTF-to-text drops \uN escapes and turns the raw newline between styled
    # runs into a line break; gnnote.rtf.parse_rtf reads the full text (test_image_and_text_records)
    rtf_texts = [t for fmt, t in pages[0]["texts"] if fmt == "rtf"]
    assert rtf_texts and "Ahoj" in rtf_texts[0] and "svet" in rtf_texts[0] and "riadok dva" in rtf_texts[0]


def bbox_of(points: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def test_oracle_goodparse(samples, tmp_path):
    """goodparse (GPL, subprocess) only answers "opens, page count, stroke count, colours";
    its flat-stroke decoding changed between versions, so geometry is checked against
    parser-for-goodnotes (bounding boxes) instead of goodparse's point lists."""
    doc = uniform_document()
    data = write_goodnotes(doc, Opts())
    path = tmp_path / "uniform.goodnotes"
    path.write_bytes(data)
    pages = run_oracle(samples, "goodparse", "src", GOODPARSE_SCRIPT, path)
    assert len(pages) == 2
    for p in pages:
        assert (p["width"], p["height"]) == pytest.approx((GN_W, GN_H), abs=0.01)
        assert p["scale"] == pytest.approx(72 / 132, abs=1e-4)
    assert [len(p["strokes"]) for p in pages] == [3, 1]
    for p, page in zip(pages, doc.pages):
        for s, stroke in zip(p["strokes"], page.strokes):
            assert s["kind"] == "pen"
            assert s["color"] == pytest.approx(list(stroke.color), abs=1e-6)
            assert len(s["points"]) >= 2
    pfg = run_oracle(samples, "parser-for-goodnotes", "src", PFG_SCRIPT, path)
    assert [len(p["strokes"]) for p in pfg] == [3, 1]
    for p, page in zip(pfg, doc.pages):
        for s, stroke in zip(p["strokes"], page.strokes):
            poly = [(q.x * K, q.y * K) for q in stroke.points]
            # start + (control, end) pairs: the bbox of a midpoint-controlled polyline is its own
            assert bbox_of(s["points"]) == pytest.approx(bbox_of(poly), abs=0.05)
            channels = [int(s["color"][i:i + 2], 16) for i in (1, 3, 5)]
            assert channels == pytest.approx([c * 255 for c in stroke.color[:3]], abs=0.51)
            for x, y, _pr in s["points"]:
                assert dist_to_polyline((x, y), poly) < 0.6


def test_generated_paper_styles_read_back():
    """Lined / grid / dotted paper drawn by make_paper_pdf ('re' operators) classifies as itself."""
    for style in ("plain", "lined", "grid", "dotted"):
        for w, h in ((455.04, 588.45), (595.28, 841.89), (612.0, 792.0)):
            back = read_goodnotes(write_goodnotes(Document(pages=[Page(w, h, paper=style)])))
            page = back.pages[0]
            assert page.template_is_builtin and page.paper == style, (style, w, h, page.paper)
            assert (round(page.width, 2), round(page.height, 2)) == (round(w, 2), round(h, 2))
