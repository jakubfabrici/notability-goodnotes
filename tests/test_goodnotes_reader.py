"""Tests for ``gnnote.goodnotes.reader`` (GoodNotes -> Document).

Oracle: parser-for-goodnotes (MIT) is run in a subprocess with its own ``PYTHONPATH`` and never
imported into the package.  The element-level expectations (how many sub-strokes an element
yields) are computed with an independent walk over the record stream using ``gnnote.protobuf``
and ``gnnote.tpl``.  GoodNotes' own PDF export of Test5 is compared with PyMuPDF when installed.
"""
from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from gnnote import applelz4, protobuf, tpl
from gnnote.goodnotes.reader import (
    BUILTIN_TEMPLATE_RE, CANVAS_PER_POINT, DEFAULT_PAGE_SIZE, read_goodnotes,
)
from gnnote.model import Document
from gnnote.pdfutil import make_paper_pdf
from gnnote.rtf import make_rtf

K = CANVAS_PER_POINT
# The research builders' output (scratchpad/work/elems.goodnotes) sits next to the reference
# checkouts: GNNOTE_SCRATCH_WORK, else the "work" sibling of the GNNOTE_SAMPLES directory.
SCRATCH_WORK = Path(os.environ.get("GNNOTE_SCRATCH_WORK")
                    or (Path(os.environ["GNNOTE_SAMPLES"]).parent / "work" if os.environ.get("GNNOTE_SAMPLES") else "tests/.samples/work"))

ORACLE_SCRIPT = r"""
import json, sys
from goodnotes_re import GoodNotesDocument
out = []
with GoodNotesDocument.open(sys.argv[1]) as doc:
    for p in doc.pages():
        strokes = [{"uuid": s.uuid, "n": len(s.points), "x": s.points[0].x, "y": s.points[0].y,
                    "color": s.color_hex, "alpha": s.alpha, "width": s.width, "fmt": s.tpl_format}
                   for s in p.strokes]
        out.append({"member": p.member_path, "width": p.dimensions.width, "height": p.dimensions.height,
                    "background": p.background_attachment_path, "pdf_page_index": p.pdf_page_index,
                    "strokes": strokes, "shapes": len(p.shapes),
                    "images": [[i.x, i.y, i.width, i.height] for i in p.image_elements],
                    "texts": [t.text for t in p.text_fragments]})
json.dump(out, sys.stdout)
"""


# --------------------------------------------------------------------------- helpers


def _read(path: Path) -> Document:
    return read_goodnotes(path.read_bytes())


def _first(points) -> Tuple[float, float]:
    return points[0].x, points[0].y


def oracle_pages(samples, path: Path) -> List[dict]:
    """parser-for-goodnotes' view of ``path`` (subprocess), or skip."""
    src = samples.repo("parser-for-goodnotes") / "src"
    env = dict(os.environ, PYTHONPATH=str(src))
    proc = subprocess.run([sys.executable, "-c", ORACLE_SCRIPT, str(path)], env=env,
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        pytest.skip(f"parser-for-goodnotes could not run: {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


def element_walk(data: bytes, member: str) -> List[dict]:
    """Independent walk of one ``notes/`` member: one entry per non-empty ink element in record
    order with the number of sub-strokes the reader must produce for it."""
    z = zipfile.ZipFile(io.BytesIO(data))
    records = protobuf.decode_records(z.read(member))
    out: List[dict] = []
    tombstone = False
    for rec in records:
        fields = protobuf.decode_message(rec)
        f1 = protobuf.get(fields, 1)
        if f1 is not None and f1.wire_type == protobuf.WIRE_LEN and len(f1.value) == 36 and b"-" in f1.value:
            tombstone = protobuf.get(fields, 3) is not None and protobuf.get(fields, 3).value == 1
            continue
        if len(fields) != 1 or fields[0].number != 7:
            tombstone = False
            continue
        body = protobuf.decode_message(fields[0].value)
        if tombstone:
            tombstone = False
            continue
        colour = protobuf.message_value(protobuf.get(body, 4)) if protobuf.get(body, 4) else []
        rgba = [0.0, 0.0, 0.0, 0.0]
        for f in colour:
            if 1 <= f.number <= 4 and f.wire_type == protobuf.WIRE_FIXED32:
                rgba[f.number - 1] = protobuf.fixed32_float(f)
        if protobuf.get(body, 4) is None:
            rgba = [0.0, 0.0, 0.0, 1.0]
        off = (0.0, 0.0)
        f6 = protobuf.get(body, 6)
        if f6 is not None and f6.value:
            sub = protobuf.decode_message(f6.value)
            off = (protobuf.fixed32_float(protobuf.get(sub, 1)) if protobuf.get(sub, 1) else 0.0,
                   protobuf.fixed32_float(protobuf.get(sub, 2)) if protobuf.get(sub, 2) else 0.0)
        f9 = protobuf.get(body, 9)
        if f9 is not None and f9.value:
            shape = protobuf.decode_message(f9.value)
            if any(f.number in (1, 2, 3, 4) for f in shape):
                out.append({"kind": "shape", "k": 1, "rgba": rgba})
                continue
        geo = tpl.decode(applelz4.decompress(protobuf.get(body, 2).value))
        if geo is None:
            continue
        if isinstance(geo, tpl.FlatStroke):
            k = geo.effective_flags().count(0)
            first = (geo.start[0] + off[0], geo.start[1] + off[1])
            fmt = "flat"
        elif isinstance(geo, tpl.RibbonStroke):
            k = len(geo.subpaths)
            first = (geo.points[0][0] + off[0], geo.points[0][1] + off[1])
            fmt = "ribbon"
        else:
            k = len(geo.subpaths)
            first = (geo.points[0][0] + off[0], geo.points[0][1] + off[1])
            fmt = "pencil"
        out.append({"kind": "ink", "k": k, "rgba": rgba, "first": first, "fmt": fmt})
    return out


# --------------------------------------------------------------------------- real samples


def test_all_samples_read_without_exception(samples):
    for path in samples.goodnotes_files():
        doc = _read(path)
        assert doc.source_format == "goodnotes"
        assert doc.pages, path.name
        for page in doc.pages:
            assert page.width > 0 and page.height > 0
            for s in page.strokes:
                assert s.points
                assert all(p.width > 0 for p in s.points)
                if s.controls is not None:
                    assert len(s.controls) == len(s.points) - 1
                x, y = _first(s.points)
                assert -0.05 * page.width <= x <= 1.05 * page.width, (path.name, x)
                assert -0.05 * page.height <= y <= 1.05 * page.height, (path.name, y)
                assert all(0.0 <= c <= 1.0 for c in s.color)
        for w in doc.warnings:
            assert "\n" not in w


def test_pages_match_oracle(samples):
    for path in samples.goodnotes_files():
        doc = _read(path)
        oracle = oracle_pages(samples, path)
        assert len(doc.pages) == len(oracle), path.name
        data = path.read_bytes()
        for i, (page, opage) in enumerate(zip(doc.pages, oracle)):
            assert page.width == pytest.approx(opage["width"], abs=0.01), (path.name, i)
            assert page.height == pytest.approx(opage["height"], abs=0.01), (path.name, i)
            scale = page.width / (opage["width"] * K)
            elements = element_walk(data, opage["member"])
            # stroke counts: one Stroke per sub-path, shapes count once
            assert len(page.strokes) == sum(e["k"] for e in elements), (path.name, i)
            # oracle: one or more strokes per non-empty ink element, identified by uuid
            ink = [e for e in elements if e["kind"] == "ink"]
            ouuids = []
            for s in opage["strokes"]:
                if not ouuids or ouuids[-1] != s["uuid"]:
                    ouuids.append(s["uuid"])
            assert len(ouuids) == len(ink), (path.name, i)
            assert len(set(ouuids)) == len(ink), (path.name, i)
            # shapes and images
            assert len(page.images) == len(opage["images"])
            assert sum(1 for e in elements if e["kind"] == "shape") == opage["shapes"]
            # match by order: first oracle stroke of each element <-> first sub-stroke of the element
            pos = 0
            first_oracle = {}
            for s in opage["strokes"]:
                first_oracle.setdefault(s["uuid"], s)
            oi = 0
            for e in elements:
                mine = page.strokes[pos:pos + e["k"]]
                pos += e["k"]
                if e["kind"] != "ink":
                    continue
                o = first_oracle[ouuids[oi]]
                oi += 1
                r, g, b, a = e["rgba"]
                assert tuple(round(c, 4) for c in mine[0].color) == tuple(round(c, 4) for c in (r, g, b, a))
                hexv = o["color"].lstrip("#")
                for comp, hx in zip((r, g, b), (hexv[0:2], hexv[2:4], hexv[4:6])):
                    assert abs(round(comp * 255) - int(hx, 16)) <= 1, (path.name, i, o["color"], e["rgba"])
                assert abs(o["alpha"] - a) < 0.01
                if e["fmt"] == "flat":
                    # exact: the oracle's first point is the stored start point (+ lasso offset)
                    x, y = _first(mine[0].points)
                    assert x == pytest.approx(o["x"] * scale, abs=0.01)
                    assert y == pytest.approx(o["y"] * scale, abs=0.01)
                    assert mine[0].width == pytest.approx(o["width"] / 2.0, rel=1e-4)
                else:
                    # ribbon / pencil: the oracle skips the start tuple, so stay within a few pt
                    x, y = _first(mine[0].points)
                    assert abs(x - o["x"] * scale) < 0.05 * page.width
                    assert abs(y - o["y"] * scale) < 0.05 * page.height
            assert pos == len(page.strokes)


def test_flat_strokes_are_exact_cubic_chains(samples):
    """Degree elevation of the stored quadratics: c1 = P0 + 2/3 (C - P0), c2 = P1 + 2/3 (C - P1)."""
    path = samples.repo("goodparse") / "samples" / "Test5.goodnotes"
    doc = _read(path)
    page = doc.pages[2]
    bezier = [s for s in page.strokes if s.is_bezier]
    assert len(bezier) >= 10
    for s in bezier:
        for (p0, p1), (c1, c2) in zip(zip(s.points, s.points[1:]), s.controls):
            # recover the quadratic control from both handles: they must agree
            qx1 = p0.x + 1.5 * (c1.x - p0.x)
            qx2 = p1.x + 1.5 * (c2.x - p1.x)
            qy1 = p0.y + 1.5 * (c1.y - p0.y)
            qy2 = p1.y + 1.5 * (c2.y - p1.y)
            assert qx1 == pytest.approx(qx2, abs=1e-6)
            assert qy1 == pytest.approx(qy2, abs=1e-6)
            assert c1.width == p0.width == s.width


def test_backgrounds_builtin_papers(samples):
    for name in ("Test4", "Test5", "test", "test2", "test3"):
        path = samples.repo("goodparse") / "samples" / f"{name}.goodnotes"
        doc = _read(path)
        for page in doc.pages:
            assert page.template_is_builtin
            assert page.background is not None
            assert page.background.page_index == 0
            assert page.background.pdf_id in doc.pdfs
            assert doc.pdfs[page.background.pdf_id].startswith(b"%PDF-")
            assert page.paper in ("plain", "lined", "grid", "dotted")
            size = (round(page.width, 2), round(page.height, 2))
            assert size in ((455.04, 588.45), (595.28, 841.89)), name
    t4 = _read(samples.repo("goodparse") / "samples" / "Test4.goodnotes")
    assert [p.paper for p in t4.pages] == ["plain", "grid"]  # blank "Blue", ruled "Yellow" (rules both ways)
    assert t4.title == "Test4"
    ex1 = _read(samples.repo("parser-for-goodnotes") / "assets" / "ex1.goodnotes")
    assert ex1.pages[0].paper == "dotted"
    assert ex1.title == "Homework04_B11315022"


def test_images_and_pens(samples):
    ex3 = _read(samples.repo("parser-for-goodnotes") / "assets" / "ex3.goodnotes")
    page = ex3.pages[0]
    assert len(page.images) == 2
    for im in page.images:
        assert im.fmt == "jpeg"
        assert im.data[:3] == b"\xff\xd8\xff"
        assert 0 <= im.x < page.width and 0 <= im.y < page.height
        assert im.w > 0 and im.h > 0 and im.x + im.w <= page.width + 1
    ex1 = _read(samples.repo("parser-for-goodnotes") / "assets" / "ex1.goodnotes")
    assert len(ex1.pages[0].images) == 3
    assert all(im.fmt == "png" for im in ex1.pages[0].images)
    t5 = _read(samples.repo("goodparse") / "samples" / "Test5.goodnotes")
    pens = {s.pen for p in t5.pages for s in p.strokes}
    assert {"ballpoint", "fountain", "pencil", "marker"} <= pens
    assert any(s.kind == "highlighter" and s.color[3] == pytest.approx(0.5) for s in t5.pages[2].strokes)
    assert any("pencil" in w for w in t5.warnings)
    # ribbon strokes carry per-point widths; flat ones a constant W/2
    ribbon = [s for s in t5.pages[1].strokes if s.pen == "fountain"]
    assert ribbon and any(len({round(p.width, 4) for p in s.points}) > 1 for s in ribbon)
    for s in t5.pages[2].strokes:
        if s.is_bezier:
            assert len({p.width for p in s.points}) == 1


def test_text_boxes(samples):
    t5 = _read(samples.repo("goodparse") / "samples" / "Test5.goodnotes")
    texts = t5.pages[2].texts
    assert [t.text for t in texts] == ["Hallo", "Test123italicunderline Strike "]
    hallo = texts[0]
    # text frame origin verified against GoodNotes' export: (272.65, 112.67) pt
    assert hallo.x == pytest.approx(272.65, abs=0.05)
    assert hallo.y == pytest.approx(112.67, abs=0.05)
    assert hallo.size == pytest.approx(24 * 72 / 132, abs=0.01)  # \fs48 -> 13.09 pt
    assert hallo.color == (0.0, 0.0, 0.0, 1.0)
    assert hallo.runs[0].font == "HelveticaNeue"
    styled = texts[1]
    assert any(r.bold for r in styled.runs) and any(r.italic for r in styled.runs)
    assert any(r.underline for r in styled.runs)
    assert "".join(r.text for r in styled.runs) == styled.text
    assert all(r.size == pytest.approx(hallo.size, abs=0.01) for r in styled.runs if r.size)


def test_against_goodnotes_pdf_export(samples):
    pymupdf = pytest.importorskip("pymupdf")
    export = samples.repo("goodparse") / "samples" / "Test5.pdf"
    if not export.is_file():
        pytest.skip("Test5.pdf export not available")
    doc = _read(samples.repo("goodparse") / "samples" / "Test5.goodnotes")
    pdf = pymupdf.open(str(export))
    assert pdf.page_count == len(doc.pages)
    for pi in range(pdf.page_count):
        assert pdf[pi].rect.width == pytest.approx(doc.pages[pi].width, abs=0.01)
        assert pdf[pi].rect.height == pytest.approx(doc.pages[pi].height, abs=0.01)
    page = doc.pages[2]
    pg = pdf[2]
    paper = {(0.82, 0.83, 0.83), (0.97, 0.97, 0.92)}
    drawings = [d for d in pg.get_drawings()
                if tuple(round(c, 2) for c in (d.get("fill") or d.get("color") or (0, 0, 0))) not in paper]

    def control_bbox(s):
        xs = [p.x for p in s.points] + [c.x for pair in s.controls for c in pair]
        ys = [p.y for p in s.points] + [c.y for pair in s.controls for c in pair]
        return min(xs), min(ys), max(xs), max(ys)

    # every flat stroke is one stroked path in the export with the same colour, width and bbox
    stroked = [d for d in drawings if d["type"] == "s" and d["rect"].height > 0.5]
    matched = 0
    for s in page.strokes:
        if not s.is_bezier:
            continue
        b = control_bbox(s)
        hits = [d for d in stroked
                if tuple(round(c, 2) for c in d["color"]) == tuple(round(c, 2) for c in s.color[:3])
                and abs(d["width"] - s.width) < 0.02
                and max(abs(b[0] - d["rect"].x0), abs(b[1] - d["rect"].y0),
                        abs(b[2] - d["rect"].x1), abs(b[3] - d["rect"].y1)) < 2.0]
        assert hits, (s.color, s.width, b)
        matched += 1
    assert matched >= 10
    # the ink bounding box agrees within 2 pt: flat strokes are stroked paths in the export (rect
    # without the line width), ribbon / pencil strokes are filled outlines or rasters (padded)
    xs0, ys0, xs1, ys1 = [], [], [], []
    for s in page.strokes:
        hw = 0.0 if s.is_bezier else s.width / 2.0
        xs0.append(min(p.x for p in s.points) - hw)
        ys0.append(min(p.y for p in s.points) - hw)
        xs1.append(max(p.x for p in s.points) + hw)
        ys1.append(max(p.y for p in s.points) + hw)
    for im in page.images:
        xs0.append(im.x); ys0.append(im.y); xs1.append(im.x + im.w); ys1.append(im.y + im.h)
    mine = (min(xs0), min(ys0), max(xs1), max(ys1))
    rects = [d["rect"] for d in drawings if d["rect"].height > 0.5] + [pymupdf.Rect(i["bbox"]) for i in pg.get_image_info()]
    theirs = (min(r.x0 for r in rects), min(r.y0 for r in rects), max(r.x1 for r in rects), max(r.y1 for r in rects))
    for a, b in zip(mine, theirs):
        assert abs(a - b) < 2.0, (mine, theirs)
    # the embedded PNG lands where the export draws it
    im = page.images[0]
    img_rects = [pymupdf.Rect(i["bbox"]) for i in pg.get_image_info() if i["width"] == 1536]
    assert img_rects and abs(img_rects[0].x0 - im.x) < 0.5 and abs(img_rects[0].y0 - im.y) < 0.5
    assert abs(img_rects[0].width - im.w) < 0.5 and abs(img_rects[0].height - im.h) < 0.5


# --------------------------------------------------------------------------- the research builders' file


def test_builders_elems_file():
    path = SCRATCH_WORK / "elems.goodnotes"
    if not path.is_file():
        pytest.skip("research builder output elems.goodnotes not available")
    doc = _read(path)
    assert doc.title == "Elements test"
    assert len(doc.pages) == 3
    assert len(doc.pdfs) == 3
    p0, p1, p2 = doc.pages
    assert (round(p0.width, 2), round(p0.height, 2)) == (455.04, 588.45)
    assert p0.template_is_builtin and p0.paper == "plain"
    assert len(p0.images) == 1 and len(p0.texts) == 1
    im = p0.images[0]
    assert im.fmt == "png"
    assert (im.x, im.y, im.w) == pytest.approx((100 / K, 200 / K, 500 / K), abs=0.01)
    assert im.h == pytest.approx(500 * 397 / 2048 / K, abs=0.01)
    tb = p0.texts[0]
    assert tb.text == "Hello from the converter"
    assert (tb.x, tb.y, tb.w, tb.h) == pytest.approx((110 / K, 510 / K, 380 / K, 40 / K), abs=0.01)
    for p in (p1, p2):
        assert (round(p.width, 2), round(p.height, 2)) == (595.28, 841.89)
        assert not p.template_is_builtin
        assert p.background is not None
    assert p1.background.page_index == 1  # page 2 of the 2-page attachment
    assert p2.background.page_index == 0
    assert p1.background.pdf_id != p2.background.pdf_id
    assert not doc.warnings


# --------------------------------------------------------------------------- synthetic containers

_f = protobuf


def _rec(body: bytes) -> bytes:
    return protobuf.varint(len(body)) + body


def _clock(version: int = 2, nonce: int = 12345) -> bytes:
    return _f.field_varint(1, version) + _f.field_varint(2, nonce)


def _pt(x: float, y: float) -> bytes:
    return _f.field_fixed32(1, x) + _f.field_fixed32(2, y)


def _rect(x: float, y: float, w: float, h: float) -> bytes:
    return _f.field_message(1, _pt(x, y)) + _f.field_message(2, _pt(w, h))


def _colour(r: float, g: float, b: float, a: float) -> bytes:
    out = b""
    for i, c in enumerate((r, g, b, a), 1):
        if c != 0.0:
            out += _f.field_fixed32(i, c)
    return out


def _two_page_pdf() -> bytes:
    """A tiny 2-page PDF (Letter + landscape A5) with a correct xref table."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 419.53] >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info << /Producer (test) >> >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _stroke_pair(uuid: str, frame: bytes, colour: bytes, extra: bytes = b"", tombstone: bool = False,
                 tool: Optional[int] = None) -> bytes:
    meta = _f.field_bytes(1, uuid) + _f.field_message(2, _clock()) + (_f.field_varint(3, 1) if tombstone else b"")
    meta += _f.field_varint(8, 123456789) + _f.field_varint(9, 7) + _f.field_varint(14, 5381) + _f.field_varint(16, 24)
    body = _f.field_bytes(1, uuid) + _f.field_bytes(2, frame)
    if tool is not None:
        body += _f.field_varint(3, tool)
    body += _f.field_message(4, colour) + extra + _f.field_message(15, _clock()) + _f.field_bytes(20, b"") + _f.field_varint(21, 24)
    return _rec(meta) + _rec(_f.field_message(7, body))


def _flat_frame(points, width: float) -> bytes:
    return applelz4.compress(tpl.encode_flat(tpl.FlatStroke.from_polyline(points, width)))


def build_container(pages: List[dict], attachments: Dict[str, bytes], templates: List[dict],
                    title: str = "Synthetic", renamed: Optional[str] = None, deleted: List[str] = (),
                    with_events: bool = True, with_notes_index: bool = True, reorder: Dict[str, str] = None) -> bytes:
    """pages: [{"P": page uuid (ends 0-E), "T": template uuid, "key": order key, "notes": bytes}]
    templates: [{"T": uuid, "A": attachment uuid, "page": 1-based, "canvas": (w, h) | None, "name": str, "lined": bool}]"""
    DOC = "D0000000-0000-4000-8000-000000000000"
    def trailer(d: int) -> bytes:
        return (_f.field_fixed64(10, 1.7e12) + _f.field_bytes(11, "E0000000-0000-4000-8000-000000000001")
                + _f.field_varint(d, 123456789) + _f.field_varint(d + 1, 99) + _f.field_varint(d + 2, 24))
    events = [_rec(_f.field_bytes(1, DOC) + _f.field_message(30, _f.field_bytes(1, DOC)
              + _f.field_message(2, _f.field_bytes(1, title) + _f.field_message(2, _clock())) + trailer(13)))]
    if renamed:
        events.append(_rec(_f.field_bytes(1, DOC) + _f.field_message(31, _f.field_bytes(1, DOC)
                      + _f.field_message(2, _f.field_bytes(1, renamed) + _f.field_message(2, _clock(3))) + trailer(13))))
    for a, data in attachments.items():
        events.append(_rec(_f.field_bytes(1, a) + _f.field_message(6, _f.field_bytes(1, a) + _f.field_bytes(2, a)
                      + _f.field_varint(5, len(data)) + _f.field_bytes(6, DOC) + trailer(14))))
    for t in templates:
        body = _f.field_bytes(1, DOC) + _f.field_bytes(2, t["T"])
        if t.get("A"):
            body += _f.field_bytes(4, t["A"])
        body += _f.field_varint(5, t.get("page", 1)) + _f.field_varint(6, 1)
        if t.get("lined"):
            body += _f.field_fixed64(7, 29.333333)
        if t.get("canvas"):
            body += _f.field_message(8, _pt(*t["canvas"]))
        body += _f.field_bytes(9, t.get("name", "user.pdf")) + trailer(15)
        events.append(_rec(_f.field_bytes(1, t["T"]) + _f.field_message(2, body)))
    for p in pages:
        body = _f.field_bytes(1, DOC) + _f.field_bytes(2, p["P"])
        if p.get("T"):
            body += _f.field_message(3, _f.field_bytes(1, p["T"]) + _f.field_message(2, _clock()))
        if p.get("key"):
            body += _f.field_message(4, _f.field_bytes(1, p["key"]) + _f.field_message(2, _clock()))
        events.append(_rec(_f.field_bytes(1, p["P"]) + _f.field_message(54, body + trailer(13))))
    for P, key in (reorder or {}).items():
        events.append(_rec(_f.field_bytes(1, P) + _f.field_message(55, _f.field_bytes(2, P)
                      + _f.field_message(3, _f.field_bytes(1, key) + _f.field_message(2, _clock())) + trailer(13))))
    for P in deleted:
        events.append(_rec(_f.field_bytes(1, P) + _f.field_message(56, _f.field_bytes(1, DOC) + _f.field_bytes(2, P)
                      + _f.field_message(3, _f.field_varint(1, 1) + _f.field_message(2, _clock())) + trailer(13))))
    def notes_uuid(P: str) -> str:
        return P[:-1] + "%X" % (int(P[-1], 16) + 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.search.pb", b"")
        if with_notes_index:
            z.writestr("index.notes.pb", b"".join(_rec(_f.field_bytes(1, notes_uuid(p["P"]))
                       + _f.field_bytes(2, "notes/" + notes_uuid(p["P"]))) for p in pages))
        for p in pages:
            z.writestr("notes/" + notes_uuid(p["P"]), p.get("notes", b""))
        if with_events:
            z.writestr("index.events.pb", b"".join(events))
        z.writestr("thumbnail.jpg", b"\xff\xd8\xff\xd9")
        z.writestr("index.attachments.pb", b"".join(_rec(_f.field_bytes(1, a) + _f.field_bytes(2, "attachments/" + a))
                                                    for a in attachments))
        for a, data in attachments.items():
            z.writestr("attachments/" + a, data)
        z.writestr("schema.pb", _f.field_varint(1, 24))
    return buf.getvalue()


A_PAPER = "A0000000-0000-4000-8000-00000000AAAA"
A_PDF = "A0000000-0000-4000-8000-00000000BBBB"
A_PNG = "A0000000-0000-4000-8000-00000000CCCC"
T_PAPER = "70000000-0000-4000-8000-000000000001"
T_PDF1 = "70000000-0000-4000-8000-000000000002"
T_PDF2 = "70000000-0000-4000-8000-000000000003"
PAGES = ["5000000%X-0000-4000-8000-000000000000" % i for i in (1, 2, 3, 4)]  # P ends in 0, N in 1
PNG_1x1 = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
           + b"\x90wS\xde" + struct.pack(">I", 0) + b"IEND\xaeB`\x82")


def _synthetic(**kw) -> bytes:
    paper = make_paper_pdf(455.04, 588.45, "lined")
    pdf2 = _two_page_pdf()
    u = "E0000000-0000-4000-8000-0000000000%02X"
    # page A: flat stroke with lasso offset, highlighter, tombstone, unknown kind, auto-shape, marker, image, text
    flat = _flat_frame([(100.0, 100.0), (200.0, 150.0), (300.0, 100.0)], 24.0)
    notes_a = _stroke_pair(u % 1, flat, _colour(0.0, 0.5, 1.0, 1.0),
                           extra=_f.field_message(6, _pt(10.0, -20.0)) + _f.field_message(7, _f.field_message(1, _clock())) + _f.field_bytes(9, b""))
    notes_a += _stroke_pair(u % 2, flat, _colour(1.0, 1.0, 0.0, 0.5), extra=_f.field_varint(5, 1) + _f.field_bytes(6, b""))
    notes_a += _stroke_pair(u % 3, flat, _colour(0, 0, 0, 1), tombstone=True)
    notes_a += _stroke_pair(u % 4, applelz4.compress(tpl.encode_empty_flat(5.0)), _colour(0, 0, 0, 1))
    notes_a += _stroke_pair(u % 5, flat, _colour(0, 0, 0, 1), extra=_f.field_message(20, _f.field_bytes(1, b"")))
    shape9 = _f.field_message(4, _f.field_message(1, _pt(200.0, 300.0)) + _f.field_message(2, _pt(50.0, 25.0))
                              + _f.field_fixed32(3, 0.5)) + _f.field_message(5, _f.field_varint(2, 1)) + _f.field_fixed32(15, 6.0)
    notes_a += _stroke_pair(u % 6, applelz4.compress(tpl.encode_empty_flat(6.0)), _colour(0.5, 0.8, 0.2, 1.0),
                            extra=_f.field_bytes(6, b"") + _f.field_message(9, shape9))
    line9 = _f.field_message(1, _f.field_message(1, _pt(10.0, 10.0)) + _f.field_message(2, _pt(110.0, 10.0))) + _f.field_fixed32(15, 2.0)
    notes_a += _stroke_pair(u % 7, applelz4.compress(tpl.encode_empty_flat(2.0)), _colour(0, 0, 0, 1), extra=_f.field_message(9, line9))
    img_meta = (_f.field_bytes(1, u % 8) + _f.field_message(2, _clock()) + _f.field_bytes(4, A_PNG)
                + _f.field_varint(8, 1) + _f.field_varint(9, 8) + _f.field_varint(14, 5381) + _f.field_varint(16, 24))
    img_body = (_f.field_bytes(1, u % 8) + _f.field_message(2, _rect(50.0, 60.0, 132.0, 66.0))
                + _f.field_message(3, _rect(116.0, 93.0, 132.0, 66.0)) + _f.field_bytes(4, A_PNG)
                + _f.field_message(15, _clock()) + _f.field_varint(18, 24))
    notes_a += _rec(img_meta) + _rec(_f.field_message(1, img_body))
    txt_meta = _f.field_bytes(1, u % 9) + _f.field_message(2, _f.field_varint(2, 5)) + _f.field_varint(8, 1) + _f.field_varint(9, 9) + _f.field_varint(16, 24)
    rtf = make_rtf("Ahoj\nsvet", size_half_points=48)
    txt_body = (_f.field_bytes(1, u % 9) + _f.field_message(2, _rect(100.0, 200.0, 210.0, 70.0))
                + _f.field_bytes(6, rtf) + _f.field_fixed32(10, 10.0) + _f.field_message(15, _f.field_varint(2, 5)) + _f.field_varint(27, 24))
    notes_a += _rec(txt_meta) + _rec(_f.field_message(8, txt_body))
    unknown_meta = _f.field_bytes(1, u % 10) + _f.field_message(2, _clock()) + _f.field_varint(8, 1) + _f.field_varint(9, 10) + _f.field_varint(16, 24)
    notes_a += _rec(unknown_meta) + _rec(_f.field_message(22, _f.field_bytes(1, u % 10) + _f.field_varint(2, 31)))
    # page B: ribbon-tool stroke on a multi-page user PDF page 2 (landscape A5), page C: page 1, page D deleted
    notes_b = _stroke_pair(u % 11, flat, _colour(1, 0, 0, 1), tool=1)
    pages = [
        {"P": PAGES[0], "T": T_PAPER, "key": "A0002", "notes": notes_a},
        {"P": PAGES[1], "T": T_PDF2, "key": "A0001", "notes": notes_b},
        {"P": PAGES[2], "T": T_PDF1, "key": "A0003", "notes": b""},
        {"P": PAGES[3], "T": T_PAPER, "key": "A0004", "notes": b""},
    ]
    templates = [
        {"T": T_PAPER, "A": A_PAPER, "page": 1, "canvas": (455.04 * K, 588.45 * K), "lined": True,
         "name": "9FE8F365-4BEE-5057-8573-1A56C77CAC19_standard_1_1 - Yellow"},
        {"T": T_PDF1, "A": A_PDF, "page": 1, "canvas": (612 * K, 792 * K), "name": "user.pdf"},
        {"T": T_PDF2, "A": A_PDF, "page": 2, "canvas": (595.28 * K, 419.53 * K), "name": "user.pdf"},
    ]
    kw.setdefault("deleted", [PAGES[3]])
    return build_container(pages, {A_PAPER: paper, A_PDF: pdf2, A_PNG: PNG_1x1}, templates,
                           title="Old name", renamed="New name", **kw)


def test_synthetic_container_round_trip():
    doc = read_goodnotes(_synthetic())
    assert doc.title == "New name"
    assert len(doc.pages) == 3  # page D deleted; order keys A0001 (PDF page 2) < A0002 (paper) < A0003
    pdf_p2, paper, pdf_p1 = doc.pages
    # page sizes from the attachment PDF pages, multi-page template binding honoured
    assert (round(pdf_p2.width, 2), round(pdf_p2.height, 2)) == (595.28, 419.53)
    assert pdf_p2.background.pdf_id == A_PDF and pdf_p2.background.page_index == 1
    assert not pdf_p2.template_is_builtin
    assert (round(pdf_p1.width, 2), round(pdf_p1.height, 2)) == (612.0, 792.0)
    assert pdf_p1.background.page_index == 0
    assert set(doc.pdfs) == {A_PAPER, A_PDF}
    # built-in paper: our generated lined paper is not svg2pdf, so the name alone must NOT make it built-in
    assert not paper.template_is_builtin
    assert (round(paper.width, 2), round(paper.height, 2)) == (455.04, 588.45)
    # elements of page A
    strokes = paper.strokes
    assert len(strokes) == 5  # offset stroke, highlighter, marker, ellipse shape, line shape (tombstone + empty skipped)
    s0, s1, s2, ellipse, line = strokes
    assert s0.is_bezier and len(s0.points) == 3 and s0.width == pytest.approx(12.0)
    assert _first(s0.points) == pytest.approx(((100 + 10) / K, (100 - 20) / K), abs=1e-4)
    assert s0.color == pytest.approx((0.0, 0.5, 1.0, 1.0))
    assert s0.pen == "ballpoint" and s0.kind == "pen"
    assert s1.kind == "highlighter" and s1.color[3] == pytest.approx(0.5)
    assert _first(s1.points) == pytest.approx((100 / K, 100 / K), abs=1e-4)
    assert s2.pen == "marker"
    assert ellipse.controls is None and len(ellipse.points) == 65 and ellipse.width == pytest.approx(3.0)
    assert ellipse.color == pytest.approx((0.5, 0.8, 0.2, 1.0))
    cx = sum(p.x for p in ellipse.points[:-1]) / 64
    cy = sum(p.y for p in ellipse.points[:-1]) / 64
    assert (cx, cy) == pytest.approx((200 / K, 300 / K), abs=1e-3)
    assert line.points[0].x == pytest.approx(10 / K) and line.points[-1].x == pytest.approx(110 / K)
    assert line.width == pytest.approx(1.0)
    # image and text
    assert len(paper.images) == 1
    im = paper.images[0]
    assert im.fmt == "png" and im.data == PNG_1x1
    assert (im.x, im.y, im.w, im.h) == pytest.approx((50 / K, 60 / K, 132 / K, 66 / K), abs=1e-4)
    assert len(paper.texts) == 1
    tb = paper.texts[0]
    assert tb.text == "Ahoj\nsvet"
    assert (tb.x, tb.y, tb.w, tb.h) == pytest.approx((110 / K, 210 / K, 190 / K, 50 / K), abs=1e-4)
    assert tb.size == pytest.approx(24 / K)
    assert any("kind #22" in w for w in doc.warnings)
    # page B: the ribbon tool flag on a flat blob still decodes (pen name from #3)
    assert len(pdf_p2.strokes) == 1 and pdf_p2.strokes[0].pen == "fountain"
    assert "\n" not in "".join(doc.warnings)


def test_reorder_event_and_missing_parts():
    data = _synthetic(reorder={PAGES[2]: "0000"}, deleted=[])
    doc = read_goodnotes(data)
    assert [p.background.page_index for p in doc.pages[:3]] == [0, 1, 0]
    assert len(doc.pages) == 4
    # no event log: pages in index order, size from the paper default, warning
    doc = read_goodnotes(_synthetic(with_events=False))
    assert len(doc.pages) == 4
    assert any("index.events.pb is missing" in w for w in doc.warnings)
    assert (round(doc.pages[0].width, 2), round(doc.pages[0].height, 2)) == DEFAULT_PAGE_SIZE
    assert doc.pages[0].strokes  # ink is still read with the 72/132 fallback scale
    # no page index: notes/ members are used
    doc = read_goodnotes(_synthetic(with_notes_index=False))
    assert len(doc.pages) == 3


def test_template_without_canvas_or_attachment():
    paper = make_paper_pdf(400.0, 500.0)
    flat = _flat_frame([(0.0, 0.0), (100.0, 100.0)], 4.0)
    pages = [{"P": PAGES[0], "T": T_PAPER, "key": "A", "notes": _stroke_pair("E0000000-0000-4000-8000-000000000001", flat, _colour(0, 0, 0, 1))},
             {"P": PAGES[1], "T": T_PDF1, "key": "B"},
             {"P": PAGES[2], "T": "70000000-0000-4000-8000-0000000000FF", "key": "C"}]
    templates = [{"T": T_PAPER, "A": A_PAPER, "page": 1, "canvas": None, "name": "x"},
                 {"T": T_PDF1, "A": "A0000000-0000-4000-8000-00000000DEAD", "page": 1, "canvas": (100.0, 200.0), "name": "gone.pdf"}]
    doc = read_goodnotes(build_container(pages, {A_PAPER: paper}, templates))
    p0, p1, p2 = doc.pages
    assert (p0.width, p0.height) == (400.0, 500.0)
    assert p0.strokes[0].points[-1].x == pytest.approx(100 / K)  # 72/132 fallback
    assert any("no canvas size" in w for w in doc.warnings)
    assert p1.background is None and (p1.width, p1.height) == pytest.approx((100 / K, 200 / K))
    assert any("missing" in w for w in doc.warnings)
    assert p2.background is None and any("no template event" in w for w in doc.warnings)


def test_out_of_range_pdf_page_is_clamped():
    pdf2 = _two_page_pdf()
    pages = [{"P": PAGES[0], "T": T_PDF1, "key": "A"}]
    templates = [{"T": T_PDF1, "A": A_PDF, "page": 7, "canvas": (595.28 * K, 419.53 * K), "name": "user.pdf"}]
    doc = read_goodnotes(build_container(pages, {A_PDF: pdf2}, templates))
    assert doc.pages[0].background.page_index == 1
    assert any("does not exist" in w for w in doc.warnings)


def test_damaged_content_is_tolerated():
    data = _synthetic()
    z = zipfile.ZipFile(io.BytesIO(data))
    members = {n: z.read(n) for n in z.namelist()}
    notes = [n for n in members if n.startswith("notes/") and members[n]]
    # truncate every ink layer and corrupt one stroke frame
    members[notes[0]] = members[notes[0]][:-40]
    bad = bytearray(members[notes[1]])
    pos = bad.find(b"bv41")
    bad[pos + 20:pos + 30] = b"\xff" * 10
    members[notes[1]] = bytes(bad)
    members["index.events.pb"] = members["index.events.pb"][:-7]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for n, d in members.items():
            out.writestr(n, d)
    doc = read_goodnotes(buf.getvalue())
    assert doc.pages
    assert any("truncated" in w for w in doc.warnings)
    assert any("could not be decoded" in w or "skipped" in w for w in doc.warnings)


def test_not_a_goodnotes_file():
    with pytest.raises(ValueError):
        read_goodnotes(b"%PDF-1.4 not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", b"hi")
    with pytest.raises(ValueError):
        read_goodnotes(buf.getvalue())
    with pytest.raises(TypeError):
        read_goodnotes("a string")  # type: ignore[arg-type]


def test_builtin_template_name_pattern():
    assert BUILTIN_TEMPLATE_RE.match("417A3734-B870-5E89-8055-DA029834F25E_standard_1_1 - Blue")
    assert BUILTIN_TEMPLATE_RE.match("9FE8F365-4BEE-5057-8573-1A56C77CAC19_a4_1_2 - White")
    assert not BUILTIN_TEMPLATE_RE.match("user.pdf")
    assert not BUILTIN_TEMPLATE_RE.match("lecture notes - Blue")
