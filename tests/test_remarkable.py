"""reMarkable (``.rmdoc`` documents, v6 ``.rm`` pages) reader: samples, rmscene as oracle,
synthetic pages and documents, hardening."""
from __future__ import annotations

import io
import json
import math
import os
import random
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from gnnote import pdfutil, readutil
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format, to_document
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.notability.reader import read_note
from gnnote.remarkable import scene as rm_scene
from gnnote.remarkable.reader import PT_PER_UNIT, read_remarkable
from gnnote.remarkable.scene import HEADER_V6, Glyph, Line, crdt_order, parse_scene

S = PT_PER_UNIT

# --------------------------------------------------------------------------- a small v6 writer


def varuint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def tag(index: int, kind: int) -> bytes:
    return varuint(index << 4 | kind)


def cid(a: int, b: int) -> bytes:
    return bytes([a]) + varuint(b)


def t_id(index: int, a: int, b: int) -> bytes:
    return tag(index, 0xF) + cid(a, b)


def t_int(index: int, value: int) -> bytes:
    return tag(index, 0x4) + struct.pack("<I", value)


def t_float(index: int, value: float) -> bytes:
    return tag(index, 0x4) + struct.pack("<f", value)


def t_double(index: int, value: float) -> bytes:
    return tag(index, 0x8) + struct.pack("<d", value)


def t_byte(index: int, value: int) -> bytes:
    return tag(index, 0x1) + bytes([value])


def t_sub(index: int, body: bytes) -> bytes:
    return tag(index, 0xC) + struct.pack("<I", len(body)) + body


def t_string(index: int, text: str) -> bytes:
    data = text.encode()
    return t_sub(index, varuint(len(data)) + b"\x01" + data)


def lww(index: int, value: bytes) -> bytes:
    return t_sub(index, t_id(1, 0, 1) + value)


def block(kind: int, body: bytes, version: int = 2) -> bytes:
    return struct.pack("<IBBBB", len(body), 0, 1, version, kind) + body


Pt = Tuple[float, float, float, float]  # x, y, width (canvas units), pressure 0..1


def line_value(tool: int, color: int, pts: Sequence[Pt], rgba: Optional[Tuple[int, int, int, int]] = None,
               version: int = 2, thickness: float = 2.0) -> bytes:
    if version == 1:
        raw = b"".join(struct.pack("<6f", x, y, 0.0, 0.0, w, p) for x, y, w, p in pts)
    else:
        raw = b"".join(struct.pack("<ffHHBB", x, y, 0, round(w * 4), 0, round(p * 255)) for x, y, w, p in pts)
    out = t_int(1, tool) + t_int(2, color) + t_double(3, thickness) + t_float(4, 0.0) + t_sub(5, raw) + t_id(6, 0, 1)
    if rgba is not None:
        r, g, b, a = rgba
        out += t_int(8, a << 24 | r << 16 | g << 8 | b)
    return out


def item(kind: int, parent: Tuple[int, int], item_id: Tuple[int, int], left: Tuple[int, int],
         value: Optional[bytes], item_type: int, version: int = 2) -> bytes:
    body = t_id(1, *parent) + t_id(2, *item_id) + t_id(3, *left) + t_id(4, 0, 0) + t_int(5, 0)
    if value is not None:
        body += t_sub(6, bytes([item_type]) + value)
    return block(kind, body, version)


def text_block(paragraphs: Sequence[str], pos: Tuple[float, float] = (-468.0, 234.0), width: float = 936.0,
               styles: Optional[Dict[int, int]] = None) -> bytes:
    """Root text: one item per paragraph; paragraph i > 0 opens with the newline id (1, 100 + i)."""
    entries: List[bytes] = []
    left = (0, 0)
    counter = 200
    for i, paragraph in enumerate(paragraphs):
        if i:
            newline = (1, 100 + i)
            entries.append(t_sub(0, t_id(2, *newline) + t_id(3, *left) + t_id(4, 0, 0) + t_int(5, 0)
                                 + t_sub(6, varuint(1) + b"\x01\n")))
            left = newline
        if paragraph:
            data = paragraph.encode()
            entries.append(t_sub(0, t_id(2, 1, counter) + t_id(3, *left) + t_id(4, 0, 0) + t_int(5, 0)
                                 + t_sub(6, varuint(len(data)) + b"\x01" + data)))
            left = (1, counter + len(paragraph) - 1)
            counter += len(paragraph) + 10
    formats = b"".join((cid(0, 0) if key == 0 else cid(1, 100 + key)) + t_id(1, 1, 99) + t_sub(2, bytes([17, style]))
                       for key, style in (styles or {}).items())
    body = (t_id(1, 0, 0)
            + t_sub(2, t_sub(1, t_sub(1, varuint(len(entries)) + b"".join(entries)))
                    + t_sub(2, t_sub(1, varuint(len(styles or {})) + formats)))
            + t_sub(3, struct.pack("<dd", *pos)) + t_float(4, width))
    return block(0x07, body)


def page(lines: Sequence[bytes] = (), paper: Optional[Tuple[int, int]] = None, hidden: bool = False,
         anchor: Optional[Tuple[Tuple[int, int], float]] = None, text: Optional[bytes] = None,
         glyphs: Sequence[bytes] = (), extra_blocks: bytes = b"", version: int = 2) -> bytes:
    """One layer (0, 11) under the root holding ``lines`` (line values) then ``glyphs`` (glyph values)."""
    out = bytearray(HEADER_V6)
    out += block(0x09, varuint(0))  # author ids (ignored)
    out += block(0x01, t_id(1, 0, 11) + t_id(2, 0, 0) + t_byte(3, 1) + t_sub(4, t_id(1, 0, 1)))
    out += block(0x02, t_id(1, 0, 1) + lww(2, t_string(2, "")) + lww(3, t_byte(2, 1)))
    layer = t_id(1, 0, 11) + lww(2, t_string(2, "Layer 1")) + lww(3, t_byte(2, 0 if hidden else 1))
    if anchor is not None:
        (a, b), origin_x = anchor
        layer += lww(7, t_id(2, a, b)) + lww(8, t_byte(2, 2)) + lww(9, t_float(2, 67.0)) + lww(10, t_float(2, origin_x))
    out += block(0x02, layer)
    out += item(0x04, (0, 1), (0, 13), (0, 0), t_id(2, 0, 11), 2)
    left = (0, 0)
    for i, value in enumerate(lines):
        out += item(0x05, (0, 11), (1, 1000 + i), left, value, 3, version)
        left = (1, 1000 + i)
    for i, value in enumerate(glyphs):
        out += item(0x03, (0, 11), (1, 5000 + i), left, value, 1)
        left = (1, 5000 + i)
    if text is not None:
        out += text
    if paper is not None:
        out += block(0x0D, lww(1, t_id(2, 0, 11)) + t_sub(5, struct.pack("<II", *paper)), 1)
    out += extra_blocks
    return bytes(out)


def rmdoc(pages: Sequence[Tuple[str, Optional[bytes]]], content: dict, title: str = "Synthetic",
          pdf: Optional[bytes] = None, doc_id: str = "11111111-2222-3333-4444-555555555555",
          extra: Optional[Dict[str, bytes]] = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(doc_id + ".content", json.dumps(content))
        zf.writestr(doc_id + ".metadata", json.dumps({"visibleName": title, "type": "DocumentType"}))
        if pdf is not None:
            zf.writestr(doc_id + ".pdf", pdf)
        for page_id, data in pages:
            if data is not None:
                zf.writestr(f"{doc_id}/{page_id}.rm", data)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def cpages(entries: Sequence[dict]) -> dict:
    return {"cPages": {"pages": list(entries)}, "fileType": "notebook", "formatVersion": 2,
            "orientation": "portrait"}


def stroke_pts(x0: float, y0: float, x1: float, y1: float, n: int = 5, width: float = 2.0) -> List[Pt]:
    return [(x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * i / (n - 1), width, 0.5) for i in range(n)]


# --------------------------------------------------------------------------- samples

# What the sample pages hold once mapped: (pages, width pt, height pt, pen strokes,
# highlighter strokes, text boxes, ink bbox in pt rounded to 0.1).  Canvas: Paper Pro
# 1620 x 2160 units when the page records it, else 1404 x 1872; 72/226 pt per unit.
SAMPLE_EXPECTED = {
    "Layer_Test.rmdoc": (1, 516.1, 688.1, 3, 0, 0, (226.2, 31.9, 289.9, 175.2)),
    "Solid_Star_Test.rmdoc": (1, 516.1, 688.1, 195, 0, 0, (204.1, 287.3, 312.0, 390.0)),
    "Star_Test.rmdoc": (1, 516.1, 688.1, 51, 0, 0, (206.2, 294.9, 313.6, 400.5)),
    "ballpoint-grown-page.rm": (1, 516.1, 1124.1, 396, 0, 0, (-0.0, 0.7, 456.3, 1108.8)),
    "ballpoint-small.rm": (1, 516.1, 688.1, 39, 0, 0, (27.8, 29.4, 377.0, 210.5)),
    "calligraphy-marker-paintbrush-shader.rm": (1, 516.1, 688.1, 61, 21, 0, (-0.1, 81.1, 515.9, 495.9)),
    "fineliner-mechpencil-paintbrush.rm": (1, 516.1, 911.3, 117, 0, 1, (106.2, 35.7, 362.8, 896.0)),
    "fineliner-pencil-colors.rm": (1, 584.4, 899.1, 25, 0, 1, (78.0, 15.3, 569.1, 883.8)),
    "highlighter-marker-pencil.rm": (1, 516.1, 688.1, 53, 6, 0, (-0.1, 51.7, 515.8, 566.7)),
    "landscape-fineliner.rm": (1, 516.1, 688.1, 24, 0, 0, (41.4, 43.2, 403.8, 172.0)),
    "landscape-highlighter.rm": (1, 516.1, 688.1, 828, 5, 0, (-0.1, 0.2, 514.3, 611.7)),
    "Bold_Heading_Bullet_Normal.rm": (1, 447.3, 596.4, 0, 0, 1, None),
    "Highlighter.rm": (1, 516.1, 688.1, 48, 10, 0, (78.1, 30.6, 451.7, 401.9)),
    "Lines_v2.rm": (1, 447.3, 596.4, 10, 0, 0, (54.1, 27.5, 167.3, 54.5)),
    "Lines_v2_updated.rm": (1, 447.3, 596.4, 10, 0, 0, (54.1, 27.5, 167.3, 54.5)),  # anchored: same place
    "Normal_AB.rm": (1, 447.3, 596.4, 0, 0, 1, None),
    "Normal_A_stroke_2_layers.rm": (1, 447.3, 596.4, 2, 0, 1, (70.6, 75.5, 87.5, 90.2)),
    "abcd.strokes.rm": (1, 447.3, 596.4, 4, 0, 0, (71.3, 49.4, 178.5, 93.7)),
    "abcd.text.rm": (1, 447.3, 596.4, 0, 0, 1, None),
    "dot.stroke.rm": (1, 447.3, 596.4, 1, 0, 0, (87.5, 82.7, 88.2, 82.9)),
    "edc580a6-1c32-471e-823c-678f975eaf81.rm": (1, 516.1, 1063.7, 288, 0, 0, (84.3, 647.6, 399.9, 1048.4)),
    "erasers.rm": (1, 516.1, 1063.7, 288, 0, 0, (84.3, 647.6, 399.9, 1048.4)),
    "extended.stroke.rm": (1, 735.9, 2492.6, 34, 0, 0, (15.3, -0.0, 720.6, 2477.3)),
    "fullpage.rm": (1, 447.3, 596.4, 23, 0, 0, (-0.0, -0.0, 447.0, 596.1)),
    "keyboard-checkboxes-and-bullets.rm": (1, 516.1, 688.1, 0, 0, 1, None),
    "layers.stroke.rm": (1, 447.3, 596.4, 21, 0, 0, (84.1, 69.8, 256.0, 470.1)),
    "page_limits.rm": (1, 447.3, 596.4, 23, 0, 0, (-0.0, -0.0, 447.0, 596.1)),
    "pen_size_test.strokes.rm": (1, 447.3, 596.4, 174, 0, 0, (57.2, 22.3, 349.3, 524.2)),
    "text_and_strokes.rm": (1, 447.3, 596.4, 12, 0, 1, (85.9, 182.1, 306.8, 221.3)),
    "text_multiple_lines.rm": (1, 447.3, 596.4, 0, 0, 1, None),
    "writing_tools.rm": (1, 447.3, 596.4, 109, 7, 0, (0.0, -0.0, 447.0, 596.2)),
    "writing_tools_with_text.rm": (1, 447.3, 596.4, 100, 16, 1, (80.3, 24.0, 415.9, 543.2)),
    "Color_and_tool_v3.14.4.rm": (1, 516.1, 688.1, 19, 12, 0, (20.5, 77.2, 475.6, 364.2)),
    "More_color_highlight_shader_v3.15.4.2.rm": (1, 516.1, 688.1, 9, 14, 0, (123.1, 100.5, 373.7, 640.7)),
    "Normal_A_stroke_2_layers_v3.2.2.rm": (1, 447.3, 596.4, 3, 0, 1, (70.6, 75.5, 121.4, 108.1)),
    "Normal_A_stroke_2_layers_v3.3.2.rm": (1, 447.3, 596.4, 9, 0, 1, (70.6, 75.5, 223.7, 132.0)),
    "Wikipedia_highlighted_p1.rm": (1, 497.0, 596.4, 0, 5, 0, (15.3, 220.5, 342.1, 558.2)),
    "Wikipedia_highlighted_p2.rm": (1, 484.1, 659.9, 0, 3, 0, (1.4, 94.6, 468.8, 644.7)),
    "With_SceneInfo_Block.rm": (1, 447.3, 596.4, 13, 0, 1, (61.4, 20.8, 138.8, 40.7)),
    "test-crdt-ordering.rm": (1, 447.3, 596.4, 0, 0, 1, None),
}


def _summary(doc) -> tuple:
    p = doc.pages[0]
    bbox = None
    if p.strokes:
        boxes = [s.bbox() for s in p.strokes]
        bbox = tuple(round(v, 1) for v in (min(b[0] for b in boxes), min(b[1] for b in boxes),
                                           max(b[2] for b in boxes), max(b[3] for b in boxes)))
    return (len(doc.pages), round(p.width, 1), round(p.height, 1), sum(s.kind == "pen" for s in p.strokes),
            sum(s.kind == "highlighter" for s in p.strokes), len(p.texts), bbox)


def _all_files(samples) -> List[Path]:
    files = list(samples.remarkable_pages())
    try:
        files += samples.remarkable_documents()
    except pytest.skip.Exception:
        pass
    return files


def test_samples_read_with_their_counts_and_geometry(samples):
    seen = 0
    for path in _all_files(samples):
        doc = read_remarkable(path.read_bytes())
        assert doc.source_format == "remarkable" and doc.pages
        for p in doc.pages:
            for stroke in p.strokes:
                assert all(math.isfinite(pt.x) and math.isfinite(pt.y) and pt.width > 0 for pt in stroke.points)
                x0, y0, x1, y1 = stroke.bbox()
                assert -1 <= x0 and x1 <= p.width + 1 and -1 <= y0 and y1 <= p.height + 1, path.name
        expected = samples.expected_for(path, SAMPLE_EXPECTED)
        if expected is not None:
            assert _summary(doc) == expected, path.name
            seen += 1
    assert seen >= 30


def test_rmdoc_samples_carry_title_and_layers(samples):
    docs = {p.name: read_remarkable(p.read_bytes()) for p in samples.remarkable_documents()}
    if "Layer_Test.rmdoc" in docs:
        doc = docs["Layer_Test.rmdoc"]
        assert doc.title == "Layer Test" and doc.pages[0].paper == "plain"
        assert {s.color for s in doc.pages[0].strokes} == {(0.0, 0.0, 0.0, 1.0)}
    if "Star_Test.rmdoc" in docs:
        star = docs["Star_Test.rmdoc"].pages[0].strokes
        assert {s.color for s in star} == {(78 / 255, 105 / 255, 201 / 255, 1.0)}  # palette blue
        assert {round(s.width / S, 2) for s in star} == {2.75}  # stored width 11 / 4


ORACLE = r"""
import io, json, logging, sys, zipfile
logging.disable(logging.CRITICAL)
from rmscene import read_tree, scene_items as si
from rmscene.text import TextDocument
out = {}
for spec in sys.argv[1:]:
    path, _, member = spec.partition("::")
    if member:
        data = zipfile.ZipFile(path).read(member)
    else:
        data = open(path, "rb").read()
    tree = read_tree(io.BytesIO(data))
    items = []
    def walk(g):
        for key in g.children:
            c = g.children[key]
            if isinstance(c, si.Group):
                walk(c)
            elif isinstance(c, si.Line):
                items.append(["line", int(c.tool), int(c.color), c.thickness_scale,
                              list(c.color_rgba) if c.color_rgba else None,
                              [[p.x, p.y, p.width / 4.0, p.pressure / 255.0] for p in c.points]])
            elif isinstance(c, si.GlyphRange):
                items.append(["glyph", int(c.color), c.text, list(c.color_rgba) if c.color_rgba else None,
                              [[r.x, r.y, r.w, r.h] for r in c.rectangles]])
    walk(tree.root)
    paragraphs = None
    if tree.root_text is not None:
        paragraphs = [str(p) for p in TextDocument.from_scene_item(tree.root_text).contents]
    paper = None
    if tree.scene_info is not None and tree.scene_info.paper_size:
        paper = list(tree.scene_info.paper_size)
    out[spec] = {"items": items, "paragraphs": paragraphs, "paper": paper}
print(json.dumps(out))
"""


def _scene_items(data: bytes) -> list:
    scene = parse_scene(data)
    out = []
    for value, _chain in scene.walk():
        if isinstance(value, Line):
            out.append(["line", value.tool, value.color, value.thickness, list(value.rgba) if value.rgba else None,
                        [[p.x, p.y, p.width, p.pressure] for p in value.points]])
        elif isinstance(value, Glyph):
            out.append(["glyph", value.color, value.text, list(value.rgba) if value.rgba else None,
                        [list(r) for r in value.rects]])
    return out


def test_scenes_match_rmscene(samples):
    """rmscene (MIT) parses every page in its own process; the scene must agree item by item."""
    specs: List[str] = [str(p) for p in samples.remarkable_pages()]
    try:
        for doc in samples.remarkable_documents():
            with zipfile.ZipFile(doc) as zf:
                specs += [f"{doc}::{n}" for n in zf.namelist() if n.endswith(".rm")]
    except pytest.skip.Exception:
        pass
    src = samples.repo("rmscene") / "src"
    proc = subprocess.run([sys.executable, "-c", ORACLE, *specs], env=dict(os.environ, PYTHONPATH=str(src)),
                          capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        pytest.skip(f"rmscene could not run (it needs the 'packaging' module): {proc.stderr[-400:]}")
    oracle = json.loads(proc.stdout)
    for spec in specs:
        path, _, member = spec.partition("::")
        data = zipfile.ZipFile(path).read(member) if member else Path(path).read_bytes()
        mine = _scene_items(data)
        ref = oracle[spec]["items"]
        assert len(mine) == len(ref), spec
        for a, b in zip(mine, ref):
            if a[0] == "line":
                assert a[:5] == b[:5], spec
                assert len(a[5]) == len(b[5]), spec
                for p, q in zip(a[5], b[5]):
                    assert p[:2] == q[:2], spec  # positions are the stored floats, bit for bit
                    assert abs(p[2] - q[2]) <= 0.13 and abs(p[3] - q[3]) <= 1 / 255 + 1e-9, spec  # v1 rounding
            else:
                assert a == b, spec
        scene = parse_scene(data)
        paragraphs = [p.text for p in scene.text.paragraphs()] if scene.text is not None else None
        assert paragraphs == oracle[spec]["paragraphs"], spec
        assert (list(scene.paper_size) if scene.paper_size else None) == oracle[spec]["paper"], spec


@pytest.mark.parametrize("target", ["goodnotes", "notability"])
def test_samples_convert_and_read_back(samples, target):
    for path in _all_files(samples):
        doc = read_remarkable(path.read_bytes())
        result = convert(path.read_bytes(), path.name, Options(target=target))
        assert result.source_format == "remarkable"
        back = read_goodnotes(result.data) if target == "goodnotes" else read_note(result.data)
        assert len(back.pages) == len(doc.pages), path.name
        assert [len(p.strokes) for p in back.pages] == [len(p.strokes) for p in doc.pages], path.name
        assert sum(s.kind == "highlighter" for p in back.pages for s in p.strokes) == \
            sum(s.kind == "highlighter" for p in doc.pages for s in p.strokes), path.name
        assert sum(len(p.texts) for p in back.pages) == sum(len(p.texts) for p in doc.pages), path.name


# --------------------------------------------------------------------------- synthetic pages


def test_tools_colours_widths_and_order():
    lines = [line_value(15, 0, stroke_pts(-100, 100, 100, 100, width=2.5)),  # ballpoint, black
             line_value(18, 9, stroke_pts(-100, 200, 100, 200, width=30), rgba=(255, 195, 140, 255)),
             line_value(23, 9, stroke_pts(-100, 300, 100, 300, width=20), rgba=(48, 74, 224, 77)),  # shader
             line_value(14, 6, stroke_pts(-100, 400, 100, 400, width=4)),  # pencil, palette blue
             line_value(6, 0, stroke_pts(-100, 500, 100, 500)),  # eraser: skipped
             line_value(99, 7, stroke_pts(-100, 600, 100, 600)),  # unknown tool: a pen
             line_value(17, 0, stroke_pts(-50, 50, 50, 50, width=3.0), version=1)]
    doc = read_remarkable(page(lines[:6]))
    strokes = doc.pages[0].strokes
    assert [(s.kind, s.pen) for s in strokes] == [("pen", "ballpoint"), ("highlighter", None), ("highlighter", None),
                                                  ("pen", "pencil"), ("pen", None)]
    assert (doc.pages[0].width, doc.pages[0].height) == pytest.approx((1404 * S, 1872 * S))
    ballpoint, highlighter, shader, pencil, unknown = strokes
    assert [round(p.x / S, 3) for p in ballpoint.points] == [602.0, 652.0, 702.0, 752.0, 802.0]  # x + 702
    assert ballpoint.width == pytest.approx(2.5 * S) and ballpoint.color == (0.0, 0.0, 0.0, 1.0)
    assert highlighter.color == pytest.approx((1.0, 195 / 255, 140 / 255, 1.0))
    assert shader.color == pytest.approx((48 / 255, 74 / 255, 224 / 255, 77 / 255))
    assert pencil.color == pytest.approx((78 / 255, 105 / 255, 201 / 255, 1.0))
    assert unknown.color == pytest.approx((179 / 255, 62 / 255, 57 / 255, 1.0))
    assert "1 eraser stroke(s) were skipped" in doc.warnings
    assert any("unknown tool" in w for w in doc.warnings)
    v1 = read_remarkable(page([lines[6]], version=1)).pages[0].strokes[0]
    assert v1.width == pytest.approx(3.0 * S)  # version-1 points store the width itself


def test_paper_size_landscape_and_growth():
    pro = read_remarkable(page([line_value(17, 0, stroke_pts(-800, 10, 800, 2100))], paper=(1620, 2160)))
    assert (pro.pages[0].width, pro.pages[0].height) == pytest.approx((1620 * S, 2160 * S))
    grown = read_remarkable(page([line_value(17, 0, stroke_pts(0, 100, 0, 5000))]))
    assert grown.pages[0].height == pytest.approx((5000 + 48) * S)
    assert any("enlarged" in w for w in grown.warnings)
    content = dict(cpages([{"id": "p1"}]), orientation="landscape")
    landscape = read_remarkable(rmdoc([("p1", page([line_value(17, 0, stroke_pts(0, 10, 10, 20))]))], content))
    assert (landscape.pages[0].width, landscape.pages[0].height) == pytest.approx((1872 * S, 1404 * S))


def test_hidden_layers_and_glyph_highlights():
    hidden = read_remarkable(page([line_value(17, 0, stroke_pts(0, 10, 10, 20))], hidden=True))
    assert hidden.pages[0].strokes == [] and "1 stroke(s) on hidden layers were skipped" in hidden.warnings
    glyph = (t_int(2, 0) + t_int(3, 4) + t_int(4, 3) + t_string(5, "word")
             + t_sub(6, varuint(1) + struct.pack("<4d", -100.0, 300.0, 200.0, 40.0)))
    doc = read_remarkable(page(glyphs=[glyph]))
    (stroke,) = doc.pages[0].strokes
    assert stroke.kind == "highlighter" and stroke.color == pytest.approx((251 / 255, 247 / 255, 25 / 255, 1.0))
    assert [(round(p.x / S, 3), round(p.y / S, 3)) for p in stroke.points] == [(602.0, 320.0), (802.0, 320.0)]
    assert stroke.width == pytest.approx(40 * S)


def test_typed_text_and_anchors():
    text = text_block(["Title", "body text", "item"], styles={0: 2, 2: 4})
    anchored = page([line_value(17, 0, stroke_pts(10, 0, 50, 0))], anchor=((0, 0xFFFFFFFFFFFE), -464.0), text=text)
    doc = read_remarkable(anchored)
    (box,) = doc.pages[0].texts
    assert box.text == "Title\nbody text\n• item"
    assert (box.x, box.y) == pytest.approx(((-468 + 702) * S, 234 * S))
    assert box.runs[0].bold and box.runs[0].size == 22.0  # heading
    (stroke,) = doc.pages[0].strokes
    # the group sits at the anchor: x + origin, y + text top + the first-line offset
    assert stroke.points[0].x == pytest.approx((10 - 464 + 702) * S)
    assert stroke.points[0].y == pytest.approx((234 + 33.6) * S)
    char = page([line_value(17, 0, stroke_pts(0, 0, 10, 0))], anchor=((1, 102), 0.0), text=text)
    second = read_remarkable(char).pages[0].strokes[0]
    assert second.points[0].y == pytest.approx((234 + 33.6 + 150 + 70) * S)  # third paragraph: heading + plain
    lost = read_remarkable(page([line_value(17, 0, stroke_pts(0, 0, 10, 0))], anchor=((7, 7), 0.0), text=text))
    assert any("refer to text that is not in the page" in w for w in lost.warnings)
    assert any("approximated position" in w for w in doc.warnings)


def test_crdt_order():
    links = {(1, 3): ((1, 2), (0, 0)), (1, 2): ((1, 1), (1, 3)), (1, 1): ((0, 0), (1, 2)),
             (2, 9): ((1, 1), (1, 2))}  # inserted between 1 and 2 by another author
    assert crdt_order(links) == [(1, 1), (2, 9), (1, 2), (1, 3)]
    concurrent = {(1, 5): ((0, 0), (0, 0)), (2, 5): ((0, 0), (0, 0))}
    assert crdt_order(concurrent) == [(2, 5), (1, 5)]  # higher author first
    cycle = {(1, 1): ((1, 2), (0, 0)), (1, 2): ((1, 1), (0, 0))}
    assert sorted(crdt_order(cycle)) == [(1, 1), (1, 2)]  # nothing lost
    assert crdt_order({}) == []


# --------------------------------------------------------------------------- documents


def test_rmdoc_pages_order_deletion_templates_and_title():
    content = cpages([{"id": "b", "idx": {"timestamp": "1:2", "value": "bb"}, "template": {"value": "P Lines medium"}},
                      {"id": "a", "idx": {"timestamp": "1:2", "value": "ba"}, "template": {"value": "P Dots S"}},
                      {"id": "gone", "idx": {"value": "bc"}, "deleted": {"timestamp": "1:3", "value": 1}},
                      {"id": "c", "idx": {"value": "bd"}, "template": {"value": "P Grid large"}},
                      {"id": "d", "idx": {"value": "be"}, "template": {"value": "LS Isometric"}},
                      {"id": "e", "idx": {"value": "bf"}}])
    pages = [(pid, page([line_value(17, 0, stroke_pts(0, 10 * i, 10, 10 * i + 5))])) for i, pid in
             enumerate("abc")] + [("gone", page([line_value(17, 0, stroke_pts(0, 0, 10, 0))])), ("d", None)]
    doc = read_remarkable(rmdoc(pages, content, title="Lecture 3"))
    assert doc.title == "Lecture 3"
    assert [p.paper for p in doc.pages] == ["dotted", "lined", "grid", "plain", "plain"]
    assert [len(p.strokes) for p in doc.pages] == [1, 1, 1, 0, 0]
    assert doc.pages[0].strokes[0].points[0].y == pytest.approx(0.0)  # page "a" first (idx order)
    assert any("'LS Isometric'" in w for w in doc.warnings)


def test_rmdoc_pdf_pages_follow_redir_and_the_226_dpi_mapping():
    pdf = pdfutil.make_paper_pdf(612.0, 792.0, "plain")
    content = {"cPages": {"pages": [{"id": "p1", "idx": {"value": "ba"}, "redir": {"value": 0}},
                                    {"id": "new", "idx": {"value": "bb"}},
                                    {"id": "p9", "idx": {"value": "bc"}, "redir": {"value": 8}}]},
               "fileType": "pdf", "orientation": "portrait"}
    ink = page([line_value(17, 0, [(0.0, 0.0, 2.0, 0.5), (226.0, 226.0, 2.0, 0.5)])])
    doc = read_remarkable(rmdoc([("p1", ink), ("new", ink), ("p9", ink)], content, pdf=pdf))
    first, inserted, missing = doc.pages
    assert (first.width, first.height) == (612.0, 792.0) and first.background.page_index == 0
    assert first.background.pdf_id in doc.pdfs and not first.template_is_builtin
    assert [(round(p.x, 3), round(p.y, 3)) for p in first.strokes[0].points] == [(306.0, 0.0), (378.0, 72.0)]
    assert inserted.background is None and (inserted.width, inserted.height) == pytest.approx((1404 * S, 1872 * S))
    assert missing.background is None and any("PDF page 9 does not exist" in w for w in doc.warnings)
    epub = dict(content, fileType="epub")
    no_pdf = read_remarkable(rmdoc([("p1", ink)], epub))
    assert no_pdf.pages[0].background is None and any("EPUB" in w for w in no_pdf.warnings)


def test_rmdoc_without_content_and_legacy_page_list():
    legacy = {"pages": ["x", "y"], "redirectionPageMap": [-1, -1], "fileType": "notebook"}
    ink = page([line_value(17, 0, stroke_pts(0, 0, 10, 0))])
    doc = read_remarkable(rmdoc([("x", ink), ("y", None)], legacy,
                                extra={"11111111-2222-3333-4444-555555555555.pagedata": b"P Grid small\nBlank\n"}))
    assert [p.paper for p in doc.pages] == ["grid", "plain"] and [len(p.strokes) for p in doc.pages] == [1, 0]
    broken = read_remarkable(rmdoc([("x", ink)], {"unexpected": True}))
    assert len(broken.pages) == 1 and any("archive order" in w for w in broken.warnings)


# --------------------------------------------------------------------------- detection and CLI


def test_detection_and_errors():
    data = page([line_value(17, 0, stroke_pts(0, 0, 10, 0))])
    assert detect_format("x.rm", data) == "remarkable" and detect_format("x.bin", data) == "remarkable"
    doc_bytes = rmdoc([("p", data)], cpages([{"id": "p"}]))
    assert detect_format("x.rmdoc", doc_bytes) == "remarkable" and detect_format("x.zip", doc_bytes) == "remarkable"
    with pytest.raises(ValueError, match="version 5 is not supported"):
        to_document(b"reMarkable .lines file, version=5          " + b"\x00" * 20, "old.rm")
    with pytest.raises(ValueError, match="not a reMarkable file"):
        to_document(b"garbage", "x.rmdoc")
    other = io.BytesIO()
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("a.txt", "x")
    with pytest.raises(ValueError, match="not a reMarkable document"):
        read_remarkable(other.getvalue())


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["formats"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("remarkable"))
    assert ".rmdoc, .rm" in line and line.endswith("read only")
    src = tmp_path / "Notebook.rmdoc"
    src.write_bytes(rmdoc([("p", page([line_value(17, 0, stroke_pts(0, 0, 10, 0))]))], cpages([{"id": "p"}])))
    assert main(["convert", str(src), "--to", "goodnotes"]) == 0
    assert len(read_goodnotes((tmp_path / "Notebook.goodnotes").read_bytes()).pages[0].strokes) == 1
    single = tmp_path / "page.rm"
    single.write_bytes(page([line_value(17, 0, stroke_pts(0, 0, 10, 0))]))
    assert main(["convert", str(single)]) == 0 and (tmp_path / "page.note").is_file()
    capsys.readouterr()
    assert main(["convert", str(src), "--to", "remarkable"]) == 2
    assert "reMarkable files can be read but not written" in capsys.readouterr().err


# --------------------------------------------------------------------------- hardening


def test_truncated_and_mutated_pages_never_raise(samples):
    pages = [p for p in samples.remarkable_pages() if p.stat().st_size < 40_000][:8]
    rng = random.Random(7)
    for path in pages:
        data = path.read_bytes()
        for cut in range(0, len(data), max(1, len(data) // 40)):
            try:
                read_remarkable(data[:cut])
            except ValueError:
                assert cut < len(HEADER_V6)  # only a cut header is "not a reMarkable page"
        for _ in range(25):
            mutated = bytearray(data)
            for _ in range(rng.randint(1, 10)):
                mutated[rng.randrange(len(HEADER_V6), len(mutated))] = rng.randrange(256)
            doc = read_remarkable(bytes(mutated))
            for stroke in doc.pages[0].strokes:
                assert all(math.isfinite(p.x) and math.isfinite(p.y) for p in stroke.points)


def test_hostile_blocks_are_bounded():
    huge_block = struct.pack("<IBBBB", 0xFFFFFFF0, 0, 1, 2, 5)
    doc = read_remarkable(HEADER_V6 + huge_block + b"\x00" * 32)
    assert doc.pages[0].strokes == [] and any("damaged block" in w for w in doc.warnings)
    many_points = line_value(17, 0, [(1e30, float("nan"), 2.0, 0.5), (float("inf"), 0.0, 2.0, 0.5)])
    doc = read_remarkable(page([many_points]))
    assert doc.pages[0].strokes == [] and any("unusable coordinates" in w for w in doc.warnings)
    # a deleted-length bomb in the root text is bounded by the character budget
    bomb = t_sub(0, t_id(2, 1, 1) + t_id(3, 0, 0) + t_id(4, 0, 0) + t_int(5, 0xFFFFFFFF))
    text = block(0x07, t_id(1, 0, 0) + t_sub(2, t_sub(1, t_sub(1, varuint(1) + bomb)) + t_sub(2, t_sub(1, varuint(0))))
                 + t_sub(3, struct.pack("<dd", 0.0, 0.0)) + t_float(4, 100.0))
    doc = read_remarkable(page(text=text))
    assert doc.pages[0].texts == []


def test_size_guard_and_point_budget(monkeypatch: pytest.MonkeyPatch):
    ink = page([line_value(17, 0, stroke_pts(0, 0, 10, 0)), line_value(17, 0, stroke_pts(0, 10, 10, 10))])
    data = rmdoc([("p", ink)], cpages([{"id": "p"}]))
    monkeypatch.setattr(readutil, "MAX_MEMBER_BYTES", 200)
    doc = read_remarkable(data)
    assert doc.pages[0].strokes == [] and any("inflate above" in w for w in doc.warnings)
    monkeypatch.setattr(readutil, "MAX_MEMBER_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(readutil, "MAX_POINTS", 7)
    doc = read_remarkable(ink)
    assert len(doc.pages[0].strokes) == 1 and any("more ink points" in w for w in doc.warnings)
    monkeypatch.setattr(rm_scene, "MAX_POINTS_PER_LINE", 3)
    assert any("damaged block" in w for w in read_remarkable(ink).warnings)


def test_damaged_content_json_is_tolerated():
    deep = b"[" * 50_000 + b"]" * 50_000
    ink = page([line_value(17, 0, stroke_pts(0, 0, 10, 0))])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("d.content", deep)
        zf.writestr("d.metadata", b'{"visibleName": NaN')
        zf.writestr("d/p.rm", ink)
    doc = read_remarkable(buf.getvalue())
    assert len(doc.pages) == 1 and len(doc.pages[0].strokes) == 1
