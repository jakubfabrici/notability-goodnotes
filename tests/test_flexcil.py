"""Flexcil (``.flx`` documents, ``.flex`` backups) reader: the sample, an oracle, synthetic files, hardening."""
from __future__ import annotations

import base64
import io
import json
import math
import os
import random
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from gnnote import pdfutil, readutil
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format, to_document
from gnnote.flexcil.reader import decode_points, list_flexcil_documents, read_flexcil
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.notability.reader import read_note

W, H = 768.0, 1024.0

# --------------------------------------------------------------------------- builders


def points(triples: Sequence[Tuple[float, float, float]]) -> str:
    raw = struct.pack("<I", len(triples)) + b"".join(struct.pack("<fff", *t) for t in triples)
    return base64.b64encode(raw).decode()


def drawing(key: str, start: Tuple[float, float], triples: Sequence[Tuple[float, float, float]],
            color: int = 0xFF000000, mode: int = 5, **extra: Any) -> dict:
    obj = {"figure": 0, "start": {"x": start[0], "y": start[1]}, "mode": mode, "dashtype": 0, "fillColor": 0,
           "strokeColor": color, "key": key, "type": 1, "scale": {"x": 1.0, "y": 1.0}, "rotate": 0.0,
           "points": points(triples)}
    obj.update(extra)
    return obj


def shape(key: str, kind: int, start: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float],
          width: float = 0.004, color: int = 0xFFF7B500, **extra: Any) -> dict:
    obj = {"shapeType": kind, "points": points([(a[0], a[1], width), (b[0], b[1], width)]), "rotate": 0.0,
           "start": {"x": start[0], "y": start[1]}, "dashtype": 0, "fillColor": 0, "strokeColor": color,
           "key": key, "type": 32, "scale": {"x": 1.0, "y": 1.0}, "controlPoints": []}
    obj.update(extra)
    return obj


def png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def flx(pages: List[dict], objects: Dict[str, Dict[str, list]], pdfs: Optional[Dict[str, bytes]] = None,
        images: Optional[Dict[str, bytes]] = None, name: str = "Synthetic", extra: Optional[Dict[str, bytes]] = None,
        key: str = "DOC-1") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("info", json.dumps({"name": name, "key": key, "version": "0.0.5", "type": 4}))
        zf.writestr("pages.index", json.dumps(pages))
        for pdf_name, data in (pdfs or {}).items():
            zf.writestr("attachment/PDF/" + pdf_name, data)
        for image_key, data in (images or {}).items():
            zf.writestr("attachment/image/" + image_key, data)
        for page_key, layers in objects.items():
            for layer, rows in layers.items():
                zf.writestr(f"objects/{page_key}.{layer}", json.dumps(rows))
        for member, data in (extra or {}).items():
            zf.writestr(member, data)
    return buf.getvalue()


def page_entry(key: str, w: float = W, h: float = H, pdf: Optional[str] = None, index: int = 0, **extra: Any) -> dict:
    entry: Dict[str, Any] = {"frame": {"x": 0.0, "y": 0.0, "width": w, "height": h}, "rotate": 0.0, "key": key,
                             "version": "0.0.5"}
    if pdf is not None:
        entry["attachmentPage"] = {"file": pdf, "index": index}
    entry.update(extra)
    return entry


def compressed_list(value: Any) -> bytes:
    plain = json.dumps(value).encode()
    stream = zlib.compressobj(wbits=-15)
    return struct.pack("<Q", len(plain)) + stream.compress(plain) + stream.flush()


def flex(documents: Dict[str, bytes], tree: Optional[list] = None, trash: Optional[list] = None,
         extra: Optional[Dict[str, bytes]] = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        if tree is not None:
            zf.writestr("documents.list", compressed_list(tree))
        if trash is not None:
            zf.writestr(".trash.list", compressed_list(trash))
        for member, data in documents.items():
            zf.writestr(member, data)
        for member, data in (extra or {}).items():
            zf.writestr(member, data)
    return buf.getvalue()


def one_stroke_doc(name: str, key: str, x: float = 0.1) -> bytes:
    return flx([page_entry("P1")], {"P1": {"drawings": [drawing("D1", (x, 0.1), [(0, 0, 0.003), (0.1, 0.1, 0.003)])]}},
               name=name, key=key)


# --------------------------------------------------------------------------- the sample

# forms.flx (flexcil-backup-viewer, MIT): one 768 x 1024 pt page on a one-page grid PDF, 13
# ink strokes (4 straight lines drawn with the line tool, 9 handwriting strokes "Test 123")
# and 7 shapes (line, two arcs, circle, arrow, rectangle, pentagon), in .objects z-order.
SAMPLE_EXPECTED = {
    "forms.flx": {
        "title": "Zeichen",
        "pages": [(768.0, 1024.0, "36823E82-A31C-47AF-B342-15860AECB5F1", 0)],
        "strokes": [  # (point count, rounded bbox in pt, colour ARGB, straight handles)
            (2, (68.7, 117.2, 68.7, 209.6), 0xFF6236FF, False),
            (2, (147.6, 116.9, 147.6, 207.2), 0xFF6236FF, False),
            (2, (147.6, 120.0, 227.2, 120.0), 0xFF6236FF, False),
            (2, (227.4, 119.3, 227.4, 207.8), 0xFF6236FF, False),
            (2, (148.9, 208.3, 228.5, 211.1), 0xFF6236FF, True),  # shape 5: line
            (5, (299.1, 111.0, 311.8, 231.9), 0xFFF7B500, True),  # shape 7: arrow (shaft + head)
            (5, (360.6, 108.7, 486.4, 231.1), 0xFFF7B500, True),  # shape 3: rectangle
            (6, (562.6, 98.7, 697.9, 227.5), 0xFFF7B500, True),  # shape 4: pentagon
            (65, (76.5, 312.6, 251.4, 486.3), 0xFFF7B500, False),  # shape 1: circle
            (16, (355.4, 343.2, 358.6, 380.8), 0xFF6236FF, False),
            (17, (347.1, 343.6, 373.3, 347.9), 0xFF6236FF, False),
            (37, (366.7, 355.7, 379.0, 378.1), 0xFF6236FF, False),
            (28, (383.5, 355.2, 393.9, 376.5), 0xFF6236FF, False),
            (11, (402.9, 353.3, 403.2, 375.8), 0xFF6236FF, False),
            (9, (399.6, 363.8, 410.6, 368.5), 0xFF6236FF, False),
            (21, (427.8, 355.5, 442.3, 372.3), 0xFF6236FF, False),
            (34, (449.5, 355.2, 462.2, 373.8), 0xFF6236FF, False),
            (45, (467.5, 351.8, 480.6, 375.6), 0xFF6236FF, False),
            (2, (460.9, 382.8, 629.9, 559.3), 0xFFF7B500, True),  # shape 6: arc (one cubic)
            (2, (101.3, 563.8, 309.2, 616.6), 0xFFF7B500, True),  # shape 6: arc
        ],
    },
}


def _argb(color) -> int:
    r, g, b, a = (round(c * 255) for c in color)
    return a << 24 | r << 16 | g << 8 | b


def test_sample_reads_with_its_counts_and_geometry(samples):
    for path in samples.flexcil_files():
        data = path.read_bytes()
        doc = read_flexcil(data)
        assert doc.source_format == "flexcil" and doc.pages
        for page in doc.pages:
            for stroke in page.strokes:
                x0, y0, x1, y1 = stroke.bbox()
                assert -1 <= x0 and x1 <= page.width + 1 and -1 <= y0 and y1 <= page.height + 1
        expected = samples.expected_for(path, SAMPLE_EXPECTED)
        if expected is None:
            continue
        assert doc.title == expected["title"]
        assert [(p.width, p.height, p.background.pdf_id, p.background.page_index) for p in doc.pages] == \
            expected["pages"]
        got = [(len(s.points), tuple(round(v, 1) for v in s.bbox()), _argb(s.color), s.controls is not None)
               for s in doc.pages[0].strokes]
        assert got == expected["strokes"]
        assert {s.kind for s in doc.pages[0].strokes} == {"pen"}
        assert doc.warnings == ["7 shape(s) were converted to ink strokes"]
        assert len(list_flexcil_documents(data)) == 1


ORACLE = r"""
import json, sys
from flexcil.codec import Document, decode_points
doc = Document.load(sys.argv[1])
out = []
for page in doc.pages:
    w = page["frame"]["width"]
    layers = doc.objects(page["key"])
    drawings = {}
    for obj in layers["drawings"]:
        sx, sy = obj["start"]["x"], obj["start"]["y"]
        drawings[obj["key"]] = [[(sx + x) * w, (sy + y) * w, t * w] for x, y, t in decode_points(obj["points"])]
    out.append({"frame": [w, page["frame"]["height"]], "drawings": drawings, "shapes": len(layers["shapes"]),
                "attachment": page.get("attachmentPage"), "title": doc.info.get("name")})
print(json.dumps(out))
"""


def test_sample_matches_the_codex_plugin_codec(samples):
    """flexcil-codex-plugin (MIT) validates and decodes the document in its own process."""
    src = samples.repo("flexcil-codex-plugin") / "plugins" / "flexcil-codex-plugin" / "src"
    for path in samples.flexcil_files():
        proc = subprocess.run([sys.executable, "-c", ORACLE, str(path)], env=dict(os.environ, PYTHONPATH=str(src)),
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            pytest.skip(f"flexcil-codex-plugin could not run: {proc.stderr[-400:]}")
        oracle = json.loads(proc.stdout)
        doc = read_flexcil(path.read_bytes())
        assert len(doc.pages) == len(oracle)
        for page, ref in zip(doc.pages, oracle):
            assert [page.width, page.height] == ref["frame"]
            assert (page.background.pdf_id, page.background.page_index) == (ref["attachment"]["file"],
                                                                             ref["attachment"]["index"])
            assert len(page.strokes) == len(ref["drawings"]) + ref["shapes"]
            wanted = sorted(ref["drawings"].values())
            mine = sorted([[p.x, p.y, p.width] for p in s.points] for s in page.strokes
                          if len(s.points) in {len(v) for v in wanted} and s.controls is None)
            for stroke in wanted:
                assert any(len(m) == len(stroke) and all(abs(a - b) < 1e-3 for pa, pb in zip(m, stroke)
                                                          for a, b in zip(pa, pb)) for m in mine)


@pytest.mark.parametrize("target", ["goodnotes", "notability"])
def test_sample_converts_and_reads_back(samples, target):
    for path in samples.flexcil_files():
        doc = read_flexcil(path.read_bytes())
        result = convert(path.read_bytes(), path.name, Options(target=target))
        assert result.source_format == "flexcil" and result.stats["pdfs"] == 1
        back = read_goodnotes(result.data) if target == "goodnotes" else read_note(result.data)
        assert len(back.pages) == len(doc.pages)
        assert [len(p.strokes) for p in back.pages] == [len(p.strokes) for p in doc.pages]
        assert all(p.background is not None and p.background.pdf_id in back.pdfs for p in back.pages)


# --------------------------------------------------------------------------- synthetic documents


def test_ink_highlighter_colour_and_z_order():
    ink = [drawing("A", (0.1, 0.2), [(0.0, 0.0, 0.002), (0.05, 0.01, 0.004), (0.1, 0.0, 0.003)], color=-16776961),
           drawing("B", (0.3, 0.3), [(0.0, 0.0, 0.02), (0.2, 0.0, 0.02)], color=0x80FFFF00, mode=2)]
    shapes = [shape("S", 5, (0.5, 0.5), (0.0, 0.0), (0.1, 0.0))]
    order = [{"type": 32, "key": "S"}, {"type": 1, "key": "B"}, {"type": 1, "key": "A"}]
    doc = read_flexcil(flx([page_entry("P1")], {"P1": {"drawings": ink, "shapes": shapes, "objects": order}}))
    line, highlighter, pen = doc.pages[0].strokes
    assert [p.x for p in line.points] == pytest.approx([0.5 * W, 0.6 * W])
    assert highlighter.kind == "highlighter" and highlighter.color == pytest.approx((1, 1, 0, 128 / 255))
    assert pen.color == pytest.approx((0, 0, 1, 1)) and pen.kind == "pen"  # signed ARGB from JSON
    assert [(round(p.x, 3), round(p.y, 3), round(p.width, 3)) for p in pen.points] == [
        (76.8, 153.6, 1.536), (115.2, 161.28, 3.072), (153.6, 153.6, 2.304)]
    assert pen.width == pytest.approx(0.003 * W)  # the median point width


def test_every_shape_type():
    rows = [shape("ell", 1, (0.1, 0.1), (0, 0), (0.2, 0.1), fillColor=0x40FF0000),
            shape("rect", 3, (0.4, 0.1), (0, 0), (0.1, 0.1), sides=4),
            shape("hex", 4, (0.6, 0.1), (0, 0), (0.2, 0.2), sides=6),
            shape("arc", 6, (0.1, 0.5), (0, 0.1), (0.2, 0.1), controlPoints=[{"x": 0.2, "y": 0.5}]),
            shape("arrow", 7, (0.5, 0.5), (0, 0.2), (0, 0)),
            shape("tri", 9, (0.7, 0.5), (0, 0), (0.1, 0.1), fillColor=0x00FFFFFF),
            shape("odd", 42, (0.8, 0.8), (0, 0), (0.05, 0.05))]
    doc = read_flexcil(flx([page_entry("P1")], {"P1": {"shapes": rows}}))
    strokes = doc.pages[0].strokes
    kinds = [(s.kind, len(s.points)) for s in strokes]
    assert kinds == [("pen", 65), ("fill", 65), ("pen", 5), ("pen", 7), ("pen", 2), ("pen", 5), ("pen", 2),
                     ("pen", 2)]
    ellipse, fill, rect, hexagon, arc, arrow, tri, odd = strokes
    assert ellipse.bbox() == pytest.approx((0.1 * W, 0.1 * W, 0.3 * W, 0.2 * W))
    assert fill.outline and fill.color == pytest.approx((1, 0, 0, 64 / 255))
    assert rect.controls is not None and rect.bbox() == pytest.approx((0.4 * W, 0.1 * W, 0.5 * W, 0.2 * W))
    assert hexagon.points[0].y == pytest.approx(0.1 * W)  # a corner on top
    c1, c2 = arc.controls[0]
    assert (c1.x, c1.y) == pytest.approx((0.1 * W + 2 / 3 * (0.1 * W), 0.6 * W + 2 / 3 * (-0.1 * W)))
    tip = arrow.points[1]
    assert (tip.x, tip.y) == pytest.approx((0.5 * W, 0.5 * W)) and arrow.points[3] == arrow.points[1]
    assert arrow.points[2].y > tip.y and arrow.points[4].y > tip.y  # the head opens away from the tip
    assert tri.points[0] == tri.points[-1] or len(tri.points) == 2  # closed polygon through the points
    assert any("unknown type" in w for w in doc.warnings)
    assert "7 shape(s) were converted to ink strokes" in doc.warnings


def test_text_boxes():
    text = {"key": "T1", "type": 30, "rotate": 0, "version": "0.0.2", "text": "Hello bold",
            "frame": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.05},
            "columns": [{"p": {"span": [{"style": {"font-family": "Helvetica, Helvetica-Light",
                                                   "font-size": 18 / W}, "text": "Hello "},
                                        {"style": {"font-family": "Helvetica-Bold", "font-size": 24 / W,
                                                   "color": 0xFFFF0000}, "text": "bold"}],
                               "reflow": [], "offset": 1}}]}
    plain = {"key": "T2", "type": 30, "text": "x", "frame": {"x": 0.1, "y": 0.5, "width": 0.1, "height": 0.1},
             "rotate": 0.5}
    doc = read_flexcil(flx([page_entry("P1")], {"P1": {"texts": [text, plain]}}))
    box, other = doc.pages[0].texts
    assert (box.x, box.y, box.w, box.h) == pytest.approx((0.1 * W, 0.2 * H, 0.5 * W, 0.05 * H))
    assert box.text == "Hello bold" and [r.text for r in box.runs] == ["Hello ", "bold"]
    assert box.runs[0].font == "Helvetica" and box.runs[0].size == pytest.approx(18.0)
    assert box.runs[1].bold and box.runs[1].color == pytest.approx((1, 0, 0, 1)) and box.size == pytest.approx(18)
    assert other.text == "x" and any("Rotated text" in w for w in doc.warnings)


def test_images_follow_the_frame_and_the_pixel_aspect():
    square = png(10, 10)
    images = [{"key": "IMG1", "frame": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2 * W / H}},
              {"key": "IMG2", "frame": {"x": 0.5, "y": 0.5, "width": 0.2, "height": 0.2},
               "cropBox": {"x": 0, "y": 0, "width": 1, "height": 1}, "rotate": math.pi / 2},
              {"key": "MISSING", "frame": {"x": 0, "y": 0, "width": 0.1, "height": 0.1}}]
    doc = read_flexcil(flx([page_entry("P1")], {"P1": {"images": images}},
                           images={"IMG1": square, "IMG2": square}))
    first, second = doc.pages[0].images
    assert (first.x, first.y, first.w, first.h) == pytest.approx((0.1 * W, 0.1 * H, 0.2 * W, 0.2 * W))
    assert first.fmt == "png" and first.data == square
    # 0.2 x 0.2 only stays square when both axes are normalised by the page width
    assert (second.y, second.w, second.h) == pytest.approx((0.5 * W, 0.2 * W, 0.2 * W))
    assert second.rotation == pytest.approx(90.0)
    assert any("missing or not PNG" in w for w in doc.warnings)
    assert any("unverified" in w for w in doc.warnings)


def test_pdf_backgrounds_and_page_problems():
    pdf = pdfutil.make_paper_pdf(595.0, 842.0, "plain")
    pages = [page_entry("P1", 595.0, 842.0, pdf="PDF1"), page_entry("P2", 595.0, 842.0, pdf="PDF1", index=3),
             page_entry("P3", pdf="NOPE"), page_entry("P4", w=float("nan"), pdf="PDF1"),
             page_entry("P5", rotate=90.0), page_entry("P6", 300.0, 300.0, pdf="PDF1")]
    doc = read_flexcil(flx(pages, {}, pdfs={"PDF1": pdf}))
    assert [(p.width, p.height) for p in doc.pages] == [(595.0, 842.0), (595.0, 842.0), (W, H), (595.0, 842.0),
                                                        (W, H), (300.0, 300.0)]
    assert [p.background is not None for p in doc.pages] == [True, False, False, True, False, True]
    assert list(doc.pdfs) == ["PDF1"] and not any(p.template_is_builtin for p in doc.pages)
    text = "\n".join(doc.warnings)
    for needle in ("PDF page 4 of PDF1 does not exist", "NOPE is missing", "no usable page frame",
                   "page rotation(s) were ignored", "differ in size from their PDF page"):
        assert needle in text


def test_objects_that_are_reported():
    ink = [drawing("A", (0.1, 0.1), [(0, 0, 0.003), (0.1, 0, 0.003)], rotate=0.3),
           drawing("B", (0.1, 0.2), [(0, 0, 0.003), (0.1, 0, 0.003)], dashtype=1),
           drawing("C", (0.1, 0.3), [(0, 0, 0.003), (0.1, 0, 0.003)], points="not base64!")]
    layers = {"drawings": ink, "maskings": [{"key": "M"}], "hyperlinks": [{"key": "L"}, {"key": "L2"}]}
    doc = read_flexcil(flx([page_entry("P1")], {"P1": layers}))
    assert len(doc.pages[0].strokes) == 2
    text = "\n".join(doc.warnings)
    for needle in ("rotated or scaled", "Dashed", "masking", "2 link(s)", "unreadable points"):
        assert needle in text


def test_decode_points_layouts():
    assert decode_points(points([(1, 2, 3)])) == [(1.0, 2.0, 3.0)]
    legacy = base64.b64encode(struct.pack("<II", 7, 0) + struct.pack("<fff", 0.5, 0.25, 0.1)).decode()
    assert decode_points(legacy) == [(0.5, 0.25, pytest.approx(0.1))]
    assert decode_points(base64.b64encode(struct.pack("<I", 1) + struct.pack("<fff", float("nan"), 0, 0)).decode()) \
        is None
    assert decode_points(base64.b64encode(struct.pack("<I", 1000) + b"\x00" * 13).decode()) is None
    assert decode_points(12) is None and decode_points("") is None


# --------------------------------------------------------------------------- backups


def test_backup_converts_the_first_listed_document_and_names_the_others():
    first = one_stroke_doc("Algebra", "AAAA-1111")
    second = one_stroke_doc("Biology", "BBBB-2222", x=0.4)
    trashed = one_stroke_doc("Old", "CCCC-3333")
    tree = [{"key": "F1", "name": "School", "type": 1, "children": [
        {"key": "L2", "name": "Biology (renamed)", "document": "BBBB-2222.flx"},
        {"key": "L1", "name": "Algebra", "document": "AAAA-1111"}]}]
    trash = [{"parentInfo": [], "item": {"key": "L3", "name": "Old", "document": "CCCC-3333", "state": "removed"}}]
    data = flex({"Flexcil/AAAA-1111.flx": first, "Flexcil/BBBB-2222.flx": second, "Flexcil/CCCC-3333.flx": trashed,
                 "audio/x.fab": b"PK"}, tree=tree, trash=trash)
    assert detect_format("backup.flex", data) == "flexcil" and detect_format("renamed.zip", data) == "flexcil"
    entries = list_flexcil_documents(data)
    assert [(e.title, e.key) for e in entries] == [("Biology (renamed)", "BBBB-2222"), ("Algebra", "AAAA-1111")]
    doc = read_flexcil(data)
    assert doc.title == "Biology (renamed)"
    assert doc.pages[0].strokes[0].points[0].x == pytest.approx(0.4 * W)
    assert "This Flexcil backup holds 2 documents; only 'Biology (renamed)' was converted (the others: 'Algebra')" \
        in doc.warnings
    assert read_flexcil(data, document=1).title == "Algebra"
    assert read_flexcil(data, document="algebra").title == "Algebra"
    assert read_flexcil(data, document="aaaa-1111").title == "Algebra"
    with pytest.raises(ValueError, match="no document 5"):
        read_flexcil(data, document=5)
    with pytest.raises(ValueError, match="no Flexcil document 'Physics'"):
        read_flexcil(data, document="Physics")


def test_backup_without_a_list_and_with_folder_documents_and_a_broken_one():
    folder = {}
    with zipfile.ZipFile(io.BytesIO(one_stroke_doc("Folder doc", "FFFF"))) as zf:
        folder = {"Documents/FFFF.flx/" + n: zf.read(n) for n in zf.namelist()}
    data = flex({"Documents/BROKEN.flx": b"PK\x03\x04 not a zip", **folder})
    entries = list_flexcil_documents(data)
    assert [e.member for e in entries] == ["Documents/BROKEN.flx", "Documents/FFFF.flx/"]
    doc = read_flexcil(data)
    assert doc.title == "Folder doc" and len(doc.pages[0].strokes) == 1
    assert any("could not be read" in w and "BROKEN" in w for w in doc.warnings)
    with pytest.raises(ValueError, match="not a Flexcil file"):
        read_flexcil(flex({"readme.txt": b"hello"}))
    with pytest.raises(ValueError, match="none of its documents"):
        read_flexcil(flex({"x.flx": b"garbage"}))


# --------------------------------------------------------------------------- detection and CLI


def test_detection():
    data = one_stroke_doc("Doc", "D")
    assert detect_format("x.flx", data) == "flexcil" and detect_format("x.zip", data) == "flexcil"
    assert to_document(data, "x.zip").source_format == "flexcil"
    with pytest.raises(ValueError, match="not a Flexcil file"):
        to_document(b"garbage", "x.flx")
    with pytest.raises(ValueError, match="not a Flexcil file"):
        to_document(b"garbage", "x.flex")


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["formats"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("flexcil"))
    assert ".flx, .flex" in line and line.endswith("read only")
    src = tmp_path / "Backup.flex"
    src.write_bytes(flex({"A.flx": one_stroke_doc("Alpha", "A")}))
    assert main(["convert", str(src), "--to", "goodnotes"]) == 0
    assert len(read_goodnotes((tmp_path / "Backup.goodnotes").read_bytes()).pages) == 1
    capsys.readouterr()
    assert main(["convert", str(src), "--to", "flexcil"]) == 2
    assert "Flexcil files can be read but not written" in capsys.readouterr().err


# --------------------------------------------------------------------------- hardening


def _members(data: bytes) -> Dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _zip(members: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_truncated_and_mutated_json_never_raises(samples):
    members = _members(samples.flexcil_files()[0].read_bytes())
    rng = random.Random(99)
    json_parts = [n for n in members if n.startswith("objects/") or n in ("pages.index", "info")]
    for name in json_parts:
        for cut in range(0, len(members[name]), max(1, len(members[name]) // 25)):
            doc = read_flexcil(_zip({**members, name: members[name][:cut]}))
            assert doc.pages or name == "pages.index"
    for _ in range(200):
        name = rng.choice(json_parts)
        data = bytearray(members[name])
        for _ in range(rng.randint(1, 6)):
            if data:
                data[rng.randrange(len(data))] = rng.choice(b'{}[]":,0123456789.-eE ntrufalsAZ/+=')
        doc = read_flexcil(_zip({**members, name: bytes(data)}))
        for page in doc.pages:
            for stroke in page.strokes:
                assert all(math.isfinite(p.x) and math.isfinite(p.y) for p in stroke.points)


def test_hostile_values_are_bounded():
    huge_count = base64.b64encode(struct.pack("<I", 0xFFFFFFFF) + struct.pack("<fff", 0, 0, 0)).decode()
    far = drawing("F", (1e30, 1e30), [(0, 0, 0.003), (0.1, 0, 0.003)])
    deep = "[" * 50_000 + "]" * 50_000
    data = flx([page_entry("P1")], {"P1": {"drawings": [drawing("A", (0.1, 0.1), [(0, 0, 1)], points=huge_count), far]}},
               extra={"objects/P1.texts": deep.encode()})
    doc = read_flexcil(data)
    assert doc.pages[0].strokes == [] and doc.pages[0].texts == []
    bomb_list = struct.pack("<Q", 10**12) + zlib.compress(b"[]")[2:]
    assert read_flexcil(flex({"A.flx": one_stroke_doc("A", "A")}, extra={"documents.list": bomb_list})).title == "A"


def test_size_guard_and_point_budget(monkeypatch: pytest.MonkeyPatch):
    data = flex({"A.flx": one_stroke_doc("A", "A"), "B.flx": one_stroke_doc("B", "B")})
    monkeypatch.setattr(readutil, "MAX_TOTAL_BYTES", len(_members(data)["A.flx"]) + 600)
    doc = read_flexcil(data)  # A fits; B no longer does
    assert doc.title == "A"
    monkeypatch.setattr(readutil, "MAX_TOTAL_BYTES", 1024 * 1024 * 1024)
    monkeypatch.setattr(readutil, "MAX_MEMBER_BYTES", 300)
    with pytest.raises(ValueError, match="none of its documents could be opened .*inflate above"):
        read_flexcil(data)
    monkeypatch.setattr(readutil, "MAX_MEMBER_BYTES", 120)  # a bare .flx: its parts are skipped instead
    doc = read_flexcil(one_stroke_doc("A", "A"))
    assert [p.strokes for p in doc.pages] == [[]] and any("inflate above" in w for w in doc.warnings)
    monkeypatch.setattr(readutil, "MAX_MEMBER_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(readutil, "MAX_POINTS", 1)
    doc = read_flexcil(one_stroke_doc("A", "A"))
    assert doc.pages[0].strokes == [] and any("more ink points" in w for w in doc.warnings)
