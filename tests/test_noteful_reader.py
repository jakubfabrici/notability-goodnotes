"""Noteful reader: TTV codec, synthetic files, the 10 app-written samples against the
notesconverter oracle (subprocess) and against Noteful's own PDF exports, conversions."""
from __future__ import annotations

import math
import re
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from gnnote import pdfutil
from gnnote.convert import Options, convert, detect_format, document_stats, to_document
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.model import Stroke
from gnnote.notability.reader import read_note
from gnnote.noteful import A4_UNITS, POINTS_PER_UNIT, UNITS_PER_POINT, ttv
from gnnote.noteful.reader import _arrow_head, _ellipse_path, _rect_path, _Sub, decode_ink, read_noteful
from gnnote.noteful.ttv import BOOL, BYTES, F32, F64, I32, LIST, RECORD, SIZE, STAMPED, STRING, U16, U32, U64

from noteful_oracle import NOTEFUL_SAMPLES, run_oracle, sample_files

K = POINTS_PER_UNIT
U = UNITS_PER_POINT
A4_PT = (A4_UNITS[0] * K, A4_UNITS[1] * K)


# --------------------------------------------------------------------------- builders


def coll(keys: Sequence[Any], values: Sequence[bytes], present: Optional[Sequence[bool]] = None,
         key_type: int = STRING) -> bytes:
    present = list(present) if present is not None else [True] * len(keys)
    return ttv.encode([(1, LIST | key_type, list(keys)), (2, LIST | U64, [0] * len(keys)),
                       (3, LIST | BOOL, present), (0, LIST | RECORD, list(values))])


def user_pdf(width: float = 595.28, height: float = 841.89, pages: int = 1) -> bytes:
    """A PDF that is not gnnote paper (same length producer swap keeps the xref valid)."""
    if pages == 1:
        return pdfutil.make_paper_pdf(width, height).replace(b"/Producer (gnnote)", b"/Producer (Writer)")
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()]
    objects += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] >>".encode()] * pages
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def page_record(uid: str, tag: Optional[str], annotation: str = "", pdf: str = "PDF0", page_index: int = 0,
                size: Tuple[float, float] = A4_UNITS, kind: int = 1, template_json: str = "",
                files: Sequence[str] = ()) -> bytes:
    if kind == 2:
        bg = ttv.encode([(0, U16, 2), (1, SIZE, size), (2, BOOL, False), (3, STRING, pdf), (4, STRING, "h"),
                         (5, STRING, "cb.simpleline"), (6, STRING, template_json), (7, U64, 0)])
    else:
        bg = ttv.encode([(0, U16, kind), (1, SIZE, size), (2, BOOL, False), (3, U64, page_index), (4, STRING, pdf),
                         (5, U64, 0)])
    resources = ttv.encode([(0, STRING, annotation), (2, LIST | STRING, list(files)), (3, LIST | STRING, [])])
    entries: List[Tuple[Any, ...]] = [(1, STRING, uid), (2, RECORD, resources), (4, RECORD | STAMPED, bg, 0)]
    if tag is not None:
        entries.append((5, STRING | STAMPED, tag, 0))
    entries.append((6, STRING, uid + "L"))
    return ttv.encode(entries)


def ink_style(rgba: Sequence[float], blend: int = 0, dash: int = 0) -> bytes:
    return struct.pack(">H4dHQ", 0xF102, *rgba, blend, dash)


def ink_stroke(points: Sequence[Sequence[float]], nominal: float = 2.0, z: int = 0, variable: bool = False) -> bytes:
    """An ``F1 01`` record (points in units, (x, y[, r]))."""
    dims = 3 if variable else 2
    n = len(points)
    out = struct.pack(">HHIHH", 0xF101, 1, 2, 3, 1 if variable else 0) + struct.pack(">QQQ", 0, 0, z)
    out += struct.pack(">IdII", 0, nominal, 0, n)
    if n <= 4:
        return out + struct.pack(f">{n * dims}f", *(v for p in points for v in p[:dims]))
    ranges, quantised = [], []
    for k in range(dims):
        values = [p[k] for p in points]
        lo, span = min(values), max(max(values) - min(values), 1e-5)
        ranges += [lo, span]
        quantised.append([round((v - lo) / span * 65535) for v in values])
    out += struct.pack(f">{2 * dims}f", *ranges)
    return out + struct.pack(f">{n * dims}H", *(q[i] for i in range(n) for q in quantised))


def annotation(ink: bytes = b"", objects: Sequence[Tuple[str, bytes]] = (), version: int = 280) -> bytes:
    return ttv.encode([(1, U64, version), (2, BYTES, ink), (3, LIST | U64, []), (4, LIST | U64, []),
                       (5, RECORD, coll([k for k, _ in objects], [v for _, v in objects]))])


def obj(uid: str, otype: int, box: Sequence[float], z: int = 0, flip: int = 0, **data: Any) -> Tuple[str, bytes]:
    """An annotation object; ``data`` maps tag numbers (``t0002`` ...) to (type, value) pairs."""
    box_entries: List[Tuple[Any, ...]] = [(1, LIST | F64, list(box))]
    if flip:
        box_entries.append((2, I32, flip))
    body = [(0x16, U64, 280), (1, U64, otype)]
    for name, (typ, value) in data.items():
        body.append((int(name[1:], 16), typ, value) + ((0,) if typ & STAMPED else ()))
    rec = ttv.encode([(1, STRING, uid), (2, RECORD | STAMPED, ttv.encode(box_entries), 0), (5, U64 | STAMPED, z, 0),
                      (6, RECORD, ttv.encode(body)), (8, F64 | STAMPED, 1.0, 0)])
    return uid, rec


def build(pages: Sequence[Tuple[str, bytes]], blobs: Dict[str, bytes], title: str = "Built",
          layers: int = 1, bookmarks: int = 0, extra: Sequence[Tuple[Any, ...]] = (),
          with_page_list: bool = True, root_files: Optional[Sequence[str]] = None) -> bytes:
    """Assemble a container; ``blobs`` holds files and annotations by name."""
    meta = "AB" * 16
    header = ttv.encode([(1, STRING, meta), (3, STRING | STAMPED, title, 0)])
    layer_recs = [ttv.encode([(1, STRING | STAMPED, f"Layer {i + 1}", 0), (2, U32, i)]) for i in range(layers)]
    marks = [ttv.encode([(1, STRING, f"B{i}"), (2, STRING, pages[0][0])]) for i in range(bookmarks)]
    entries: List[Tuple[Any, ...]] = [(1, STRING, meta)]
    if with_page_list:
        entries.append((2, RECORD, coll([u for u, _ in pages], [r for _, r in pages])))
    entries += [(3, RECORD, coll(list(range(layers)), layer_recs, key_type=U32)),
                (4, RECORD, coll([f"B{i}" for i in range(bookmarks)], marks))]
    entries += list(extra)
    notebook = ttv.encode(entries)
    order = [("n:" + meta, header), ("d:" + meta, notebook)] + list(blobs.items())
    buf = bytearray(b"\xaa\xbb\xcc\xde")
    names, starts, lengths = [], [], []
    for name, body in order:
        names.append(name)
        starts.append(len(buf))
        lengths.append(len(body))
        buf += body
    annotations = [n for n in blobs if n.startswith("ANN")]
    files = list(root_files) if root_files is not None else [n for n in blobs if not n.startswith("ANN")]
    root = ttv.encode([(1, F32, 1.18), (2, LIST | STRING, [meta]), (3, LIST | STRING, files),
                       (4, LIST | STRING, annotations), (10, LIST | STRING, names), (11, LIST | U64, starts),
                       (12, LIST | U64, lengths)])
    start = len(buf)
    buf += root + b"\xaa\xbb\xcc\xde" + b"\x00" * 4 + struct.pack(">II", start, len(root))
    return bytes(buf)


def simple_file(ink: bytes = b"", objects: Sequence[Tuple[str, bytes]] = (), **kwargs: Any) -> bytes:
    pages = [("P1", page_record("P1", "+E00001", "ANN1"))]
    return build(pages, {"PDF0": user_pdf(), "ANN1": annotation(ink, objects)}, **kwargs)


# --------------------------------------------------------------------------- TTV codec


def test_ttv_round_trips_every_type() -> None:
    nested = ttv.encode([(0, U64, 7)])
    data = ttv.encode([
        (1, BOOL, True), (2, U64, 2 ** 63), (3, F32, 1.5), (4, ttv.DATE, 788732206.5), (5, STRING, "ä\u200b"),
        (6, BYTES, b"\x00\xff"), (7, RECORD, nested), (8, U16, 65535), (9, U32, 2 ** 32 - 1), (10, ttv.U64_ALT, 5),
        (11, I32, -3), (12, F64, -0.25), (13, SIZE, (1.0, 2.0)), (14, LIST | STRING, ["a", "b"]),
        (15, LIST | F64, [1.0, 2.0]), (16, LIST | RECORD, [nested, nested]), (17, STRING | STAMPED, "s", 99),
        (18, LIST | BOOL, [True, False]), (19, LIST | SIZE, [(1.0, 2.0)]), (20, LIST | I32, [-1, 2]),
    ])
    rec = ttv.Decoder(data).decode(0, len(data))
    assert rec.error is None
    assert rec.boolean(1) is True and rec.integer(2) == 2 ** 63 and rec.number(3) == 1.5
    assert rec.number(4) == 788732206.5 and rec.string(5) == "ä\u200b" and rec.data(6) == b"\x00\xff"
    assert rec.record(7).integer(0) == 7 and rec.integer(8) == 65535 and rec.integer(9) == 2 ** 32 - 1
    assert rec.integer(10) == 5 and rec.integer(11) == -3 and rec.number(12) == -0.25 and rec.size(13) == (1.0, 2.0)
    assert rec.strings(14) == ["a", "b"] and rec.numbers(15) == [1.0, 2.0]
    assert [r.integer(0) for r in rec.records(16)] == [7, 7]
    assert rec.string(17) == "s" and rec.stamp(17) == 99 and rec.booleans(18) == [True, False]
    assert rec.entry(19).value == [(1.0, 2.0)] and rec.integers(20) == [-1, 2]
    # typed accessors never hand out a value of another type
    assert rec.string(2) is None and rec.integer(5) is None and rec.records(14) is None and rec.number(1) is None


def test_ttv_unknown_type_stops_only_its_own_record() -> None:
    inner = ttv.encode([(1, U64, 1)]) + struct.pack(">HH", 2, 0x0019) + b"\x00" * 8
    data = ttv.encode([(1, RECORD, inner), (2, STRING, "after")]) + struct.pack(">HH", 3, 0x0030) + b"zz"
    decoder = ttv.Decoder(data)
    rec = decoder.decode(0, len(data))
    assert rec.record(1).integer(1) == 1 and "unknown value type 0x0019" in rec.record(1).error
    assert rec.string(2) == "after"  # the length-prefixed damage stayed inside the nested record
    assert "unknown value type 0x0030" in rec.error and len(decoder.errors) == 2


@pytest.mark.parametrize("payload, message", [
    (struct.pack(">HHI", 1, LIST | U64, 1000) + b"\x00" * 16, "overruns"),
    (struct.pack(">HHI", 1, LIST | STRING, 2 ** 31) + b"\x00" * 16, "overruns"),
    (struct.pack(">HHI", 1, STRING, 99) + b"abc", "overruns"),
    (struct.pack(">HH", 1, U64) + b"\x00\x00", "cut off"),
    (struct.pack(">HH", 1, STRING | STAMPED) + struct.pack(">I", 0) + b"\x00", "timestamp cut off"),
    (b"\x00\x01\x00", "header cut off"),
], ids=["list-count", "string-list-count", "string-length", "u64-cut", "stamp-cut", "header-cut"])
def test_ttv_counts_and_lengths_are_checked(payload: bytes, message: str) -> None:
    rec = ttv.Decoder(payload).decode(0, len(payload))
    assert message in (rec.error or "")


def test_ttv_nesting_and_value_budget_are_bounded() -> None:
    data = ttv.encode([(1, U64, 1)])
    for _ in range(ttv.MAX_DEPTH + 5):
        data = ttv.encode([(1, RECORD, data)])
    decoder = ttv.Decoder(data)
    rec = decoder.decode(0, len(data))
    assert any("nested deeper" in e for e in decoder.errors)
    depth = 0
    while rec is not None and rec.record(1) is not None:
        rec, depth = rec.record(1), depth + 1
    assert depth == ttv.MAX_DEPTH + 1
    big = ttv.encode([(1, LIST | BOOL, [True] * 50)])
    small = ttv.Decoder(big, max_values=10)
    assert "more than" in (small.decode(0, len(big)).error or "")
    assert "outside" in (ttv.Decoder(b"").decode(0, 5).error or "")


def test_ttv_encoder_is_strict() -> None:
    for bad in [(1, U16, 70000), (1, I32, 2 ** 31), (1, U64, -1), (1, F64, float("nan")), (1, U32, True),
                (1, F32, 1e39), (1, STRING | STAMPED, "x")]:
        with pytest.raises(ttv.TTVError):
            ttv.encode([bad])


# --------------------------------------------------------------------------- ink


def test_ink_float_and_quantised_encodings() -> None:
    dot = [(10.0, 20.0, 1.5), (10.0, 20.0, 1.5)]
    line = [(float(i * 10), 100.0 + i, 2.0 + i / 10) for i in range(7)]
    blob = (ink_style((0.1, 0.2, 0.3, 1.0)) + ink_stroke(dot, nominal=1.5, z=5, variable=True)
            + ink_style((1.0, 1.0, 0.0, 1.0), blend=1) + ink_stroke(line, nominal=12.0, z=3)
            + ink_style((0.0, 0.0, 1.0, 1.0), dash=2) + ink_stroke(line, nominal=2.0, z=9, variable=True))
    result = decode_ink(blob)
    assert result.problem is None and result.points == 16 and result.dashed == 1
    (z0, s0), (z1, s1), (z2, s2) = result.strokes
    assert (z0, z1, z2) == (5, 3, 9)
    assert [v for p in s0.points for v in (p.x, p.y)] == pytest.approx([10 * K, 20 * K] * 2)
    assert [p.width for p in s0.points] == pytest.approx([3.0 * K] * 2) and s0.width == pytest.approx(3.0 * K)
    assert s0.kind == "pen" and s0.color == pytest.approx((0.1, 0.2, 0.3, 1.0)) and s0.controls is None
    assert s1.kind == "highlighter" and s1.color == pytest.approx((1.0, 1.0, 0.0, 0.5))
    assert [p.width for p in s1.points] == pytest.approx([24.0 * K] * 7)
    step = 70.0 / 65535  # quantisation of the x span
    for p, (x, y, r) in zip(s2.points, line):
        assert p.x == pytest.approx(x * K, abs=step) and p.y == pytest.approx(y * K, abs=1e-3)
        assert p.width == pytest.approx(2 * r * K, abs=1e-3)


def test_ink_damage_keeps_what_came_before() -> None:
    good = ink_style((0, 0, 0, 1)) + ink_stroke([(1, 1), (2, 2)])
    assert decode_ink(good + b"\xf1\x07" + b"\x00" * 60).problem == "unknown ink record 0xf107"
    assert len(decode_ink(good + b"\xf1\x07").strokes) == 1
    cut = good + ink_stroke([(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])[:-3]
    result = decode_ink(cut)
    assert "cut off" in result.problem and len(result.strokes) == 1
    huge = struct.pack(">HHIHH", 0xF101, 0, 0, 0, 0) + b"\x00" * 24 + struct.pack(">IdII", 0, 1.0, 0, 2 ** 32 - 1)
    assert "cut off" in decode_ink(good + huge).problem
    nan = ink_stroke([(float("nan"), 1), (2, 2)])
    assert decode_ink(nan).non_finite == 1
    assert decode_ink(good, point_budget=1).over_limit == 1
    layout = bytearray(ink_stroke([(1, 1), (2, 2)]))
    layout[10:12] = b"\x00\x07"
    assert "point layout 7" in decode_ink(bytes(layout)).problem
    assert decode_ink(b"\xf1").problem


# --------------------------------------------------------------------------- shapes


def _anchors(stroke: Stroke) -> List[float]:
    """Anchor coordinates in units, flattened (x0, y0, x1, y1, ...)."""
    return [v for p in stroke.points for v in (p.x * U, p.y * U)]


def test_rect_ellipse_and_arrow_geometry() -> None:
    rect = _rect_path(100.0, 50.0, 0.0)
    assert rect.closed and [s[2] for s in rect.segs] == [(100, 0), (100, 50), (0, 50), (0, 0)]
    rounded = _rect_path(100.0, 50.0, 3.0)
    assert len(rounded.segs) == 8 and rounded.start == (3.0, 0.0) and rounded.segs[-1][2] == (3.0, 0.0)
    ellipse = _ellipse_path(80.0, 40.0)
    assert len(ellipse.segs) == 16 and ellipse.segs[-1][2] == ellipse.start == (80.0, 20.0)
    # every arc is within 1e-4 of the true ellipse at its middle
    p0 = ellipse.start
    for c1, c2, p1 in ellipse.segs:
        mid = ((p0[0] + 3 * c1[0] + 3 * c2[0] + p1[0]) / 8, (p0[1] + 3 * c1[1] + 3 * c2[1] + p1[1]) / 8)
        assert ((mid[0] - 40) / 40) ** 2 + ((mid[1] - 20) / 20) ** 2 == pytest.approx(1.0, abs=1e-4)
        p0 = p1
    line = _Sub((0.0, 0.0))
    line.line_to((100.0, 0.0))
    head = _arrow_head(line, 2.0)
    assert line.end() == pytest.approx((91.0, 0.0))  # shortened by 4.5 widths
    xs = [p[0] for p in head.path]
    ys = [p[1] for p in head.path]
    assert head.width == pytest.approx(2.0)
    assert max(xs) < 100 and min(xs) > 91 and max(ys) < 4.5 and min(ys) > -4.5


def test_shapes_become_strokes_and_fills() -> None:
    stroke = lambda color, width, dash=0, arrow=0: (RECORD | STAMPED, ttv.encode(  # noqa: E731
        ([(7, RECORD, ttv.encode([(0, LIST | F32, color)]))] if color else [])
        + [(2, F64, width), (3, RECORD, ttv.encode([(0, U64, dash)])), (8, RECORD, ttv.encode([(0, U64, arrow)]))]))
    fill = lambda color, flag=False: (RECORD | STAMPED, ttv.encode(  # noqa: E731
        [(1, RECORD, ttv.encode([(0, LIST | F32, color)])), (2, BOOL, flag)]))
    points = lambda pts, cmds: (RECORD | STAMPED, ttv.encode(  # noqa: E731
        [(1, LIST | F64, [v for p in pts for v in p]), (2, LIST | I32, cmds)]))
    objects = [
        obj("LINE", 20, [150, 100, 100, 0, 0], z=1, t0002=(SIZE | STAMPED, (100.0, 0.0)),
            t0007=stroke([1, 0, 0, 1], 4.0), t000d=points([(0, 0), (100, 0)], [0, 1])),
        obj("RECT", 3, [100, 100, 40, 20, math.pi / 2], z=2, t0002=(SIZE | STAMPED, (40.0, 20.0)),
            t0005=fill([0, 0, 1, 1], flag=True), t0007=stroke(None, 2.0, dash=1), t0014=(F64 | STAMPED, 0.0)),
        obj("POLY", 12, [300, 300, 20, 20, 0], z=3, t0002=(SIZE | STAMPED, (10.0, 10.0)),
            t0005=fill([0, 1, 0, 0.5]), t0007=stroke([0, 0, 0, 1], 0.0),
            t000d=points([(0, 0), (10, 0), (10, 10)], [0, 1, 1, 4])),
        obj("CURV", 21, [500, 500, 100, 100, 0], z=4, t0002=(SIZE | STAMPED, (100.0, 100.0)),
            t0007=stroke([0, 0, 0, 1], 2.0, arrow=1), t000d=points([(0, 100), (0, 0), (100, 0), (100, 100)], [0, 3])),
        obj("ODD", 99, [1, 1, 1, 1, 0], z=5),
        obj("BAD", 12, [1, 1, 1, 1, 0], z=6, t000d=points([(0, 0)], [0, 1, 1])),
    ]
    doc = read_noteful(simple_file(objects=objects))
    strokes = doc.pages[0].strokes
    assert [s.kind for s in strokes] == ["pen", "pen", "fill", "pen", "pen"]
    line, rect, poly_fill, curve, head = strokes
    assert _anchors(line) == pytest.approx([100, 100, 200, 100])
    assert line.width == pytest.approx(4.0 * K) and line.color == (1, 0, 0, 1)
    assert line.controls is not None and len(line.controls) == 1  # exact Bezier form of the line
    # the rectangle is turned 90 degrees about its centre: 20 wide, 40 high on the page
    xs, ys = _anchors(rect)[0::2], _anchors(rect)[1::2]
    assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((90, 110, 80, 120))
    assert rect.color == (0, 0, 1, 1)  # the flag: the "fill" colour is the outline's
    # polygon points live in the 10 x 10 data size and are scaled onto the 20 x 20 box
    assert poly_fill.outline and poly_fill.color == (0, 1, 0, 0.5) and poly_fill.width == 0
    fx = [p.x * U for p in poly_fill.outline[0]]
    assert (min(fx), max(fx)) == pytest.approx((290, 310))
    assert len(curve.points) == 2 and curve.controls is not None and len(head.points) == 6
    assert any("dashed" in w for w in doc.warnings)
    assert any("unsupported Noteful type 99" in w for w in doc.warnings)
    assert any("unreadable outline" in w for w in doc.warnings)


def test_text_box_runs_insets_and_rotation() -> None:
    rich = ttv.encode([
        (1, U64, 0), (2, LIST | STRING, ["Hi ", "there", "\u200b"]), (3, LIST | U64, [5, 3, 1]),
        (4, LIST | I32, [10, 6, 8, 12, 9, 1, 10, 2, 10]), (5, LIST | STRING, ["Papyrus"]),
        (6, LIST | BOOL, [True]), (7, LIST | RECORD, [ttv.encode([(0, LIST | F32, [1.0, 0.0, 0.0, 1.0])])]),
        (8, LIST | U64, [1]), (9, LIST | RECORD, [ttv.encode([(1, LIST | RECORD, [])])]),
        (10, LIST | F64, [22.0, 0.0, 44.0, 22.0]),
    ])
    theta = math.radians(30)
    objects = [obj("TXT", 2, [200, 300, 110, 44, theta], t0002=(SIZE | STAMPED, (110.0, 44.0)),
                   t0004=(RECORD, rich),
                   t0005=(RECORD | STAMPED, ttv.encode([(1, RECORD, ttv.encode([(0, LIST | F32, [1, 1, 0, 1])]))])))]
    doc = read_noteful(simple_file(objects=objects))
    (box,) = doc.pages[0].texts
    assert box.text == "Hi there" and box.align == "center" and box.rotation == pytest.approx(30)
    assert [(r.text, r.bold, r.font, r.size) for r in box.runs] == [
        ("Hi ", False, "Helvetica", pytest.approx(22 * K)), ("there", True, "Papyrus", pytest.approx(44 * K))]
    assert box.runs[0].color == (1.0, 0.0, 0.0, 1.0) == box.color
    assert (box.w, box.h) == pytest.approx((100 * K, 40 * K))
    # the inner frame's top-left corner, turned about the box centre
    lx, ly = -50.0, -20.0
    x = 200 + lx * math.cos(theta) - ly * math.sin(theta)
    y = 300 + lx * math.sin(theta) + ly * math.cos(theta)
    assert (box.x, box.y) == pytest.approx((x * K, y * K))
    assert any("background colours were dropped" in w for w in doc.warnings)


def test_image_crop_and_flip_placement() -> None:
    png = pdfutil.make_paper_pdf(10, 10)  # any bytes the reader classifies: a PDF "image"
    crop = ttv.encode([(1, LIST | F64, [20, 10, 60, 10, 60, 30, 20, 30]), (2, LIST | I32, [0, 1, 1, 1, 4])])
    objects = [obj("IMG", 1, [100, 100, 80, 40, 0], flip=1, t0002=(SIZE | STAMPED, (100.0, 50.0)),
                   t000a=(STRING, "PIC"), t000b=(SIZE, (200.0, 100.0)), t000c=(RECORD | STAMPED, crop)),
               obj("GONE", 1, [1, 1, 1, 1, 0], t000a=(STRING, "NOPE"))]
    pages = [("P1", page_record("P1", "+E00001", "ANN1"))]
    data = build(pages, {"PDF0": user_pdf(), "PIC": png, "ANN1": annotation(b"", objects)})
    doc = read_noteful(data)
    (image,) = doc.pages[0].images
    # crop 40 x 20 of a 100 x 50 picture shown at 80 x 40: scale 2, whole picture 200 x 100,
    # its left edge 2 * 20 = 40 units left of the box's left edge (60) -> 20
    assert (image.x * U, image.y * U, image.w * U, image.h * U) == pytest.approx((20, 60, 200, 100))
    assert image.fmt == "pdf"
    assert any("cropped" in w for w in doc.warnings) and any("mirrored" in w for w in doc.warnings)
    assert any("picture file is missing" in w for w in doc.warnings)


# --------------------------------------------------------------------------- pages


def test_absurd_crops_and_data_sizes_stay_on_the_page() -> None:
    picture = b"\x89PNG\r\n\x1a\n" + b"\x00" * 30
    crop = ttv.encode([(1, LIST | F64, [0, 0, 1e-5, 0, 1e-5, 1e-5, 0, 1e-5]), (2, LIST | I32, [0, 1, 1, 1, 4])])
    outline = (RECORD | STAMPED, ttv.encode([(2, F64, 1.0)]))
    points = (RECORD | STAMPED, ttv.encode([(1, LIST | F64, [0.0, 0.0, 5.0, 5.0]), (2, LIST | I32, [0, 1])]))
    objects = [obj("IMG", 1, [100, 100, 80, 40, 0], t0002=(SIZE | STAMPED, (1e12, 1e12)), t000a=(STRING, "PIC"),
                   t000c=(RECORD | STAMPED, crop)),
               obj("LINE", 20, [10, 10, 1e8, 1e8, 0], t0002=(SIZE | STAMPED, (1e-8, 1e-8)), t0007=outline, t000d=points)]
    pages = [("P1", page_record("P1", "+E1", "ANN1"))]
    doc = read_noteful(build(pages, {"PDF0": user_pdf(), "PIC": picture, "ANN1": annotation(b"", objects)}))
    (image,) = doc.pages[0].images
    assert (image.x * U, image.y * U, image.w * U, image.h * U) == pytest.approx((60, 80, 80, 40))
    assert doc.pages[0].strokes == [] and any("unreadable outline" in w for w in doc.warnings)


def test_pages_sort_by_tag_and_deleted_pages_are_skipped() -> None:
    pages = [("P3", page_record("P3", "+E00300")), ("P1", page_record("P1", "+E00100", page_index=0)),
             ("PX", page_record("PX", None)), ("P2", page_record("P2", "+E00200", page_index=1)),
             ("PD", page_record("PD", "+E00000"))]
    data = build(pages, {"PDF0": user_pdf(pages=2)})
    data = data.replace(coll([u for u, _ in pages], [r for _, r in pages]),
                        coll([u for u, _ in pages], [r for _, r in pages], present=[True, True, True, True, False]))
    doc = read_noteful(data)
    assert [p.background.page_index for p in doc.pages] == [0, 1, 0, 0]
    assert len(doc.pages) == 4 and doc.title == "Built" and not doc.warnings


def test_backgrounds_templates_and_generated_paper() -> None:
    paper = pdfutil.make_paper_pdf(595.28, 841.89, "grid")
    pages = [("P1", page_record("P1", "+E1", pdf="TPL", kind=2, template_json='{"name":"Blank","lt":0}')),
             ("P2", page_record("P2", "+E2", pdf="TPL", kind=2, template_json='{"name":"Narrow Ruled"}')),
             ("P3", page_record("P3", "+E3", pdf="GN")),
             ("P4", page_record("P4", "+E4", pdf="USER", page_index=5, size=(500.0, 400.0))),
             ("P5", page_record("P5", "+E5", pdf="MISSING")),
             ("P6", page_record("P6", "+E6", kind=7, size=(-5.0, 1.0)))]
    data = build(pages, {"TPL": user_pdf(), "GN": paper, "USER": user_pdf(pages=2)})
    doc = read_noteful(data)
    p1, p2, p3, p4, p5, p6 = doc.pages
    assert p1.template_is_builtin and p1.paper == "plain" and p1.background.pdf_id == "TPL"
    assert p2.paper == "lined" and p2.template_is_builtin
    assert p3.template_is_builtin and p3.paper == "grid"  # gnnote's own paper is stock paper
    assert not p4.template_is_builtin and p4.background.page_index == 1  # clamped to the last page
    assert (p4.width, p4.height) == pytest.approx((500 * K, 400 * K))
    assert p5.background is None and p6.background is None and (p6.width, p6.height) == pytest.approx(A4_PT)
    assert document_stats(doc)["pdfs"] == 1
    for fragment in ("does not exist", "file is missing", "unknown background kind 7", "no valid size"):
        assert any(fragment in w for w in doc.warnings), fragment


def test_generated_paper_is_classified_once_per_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.goodnotes import reader as gn_reader

    calls = []
    real = gn_reader._paper_style
    monkeypatch.setattr(gn_reader, "_paper_style", lambda data, hint: calls.append(1) or real(data, hint))
    paper = pdfutil.make_paper_pdf(595.28, 841.89, "dotted")
    pages = [(f"P{i}", page_record(f"P{i}", f"+E{i}", pdf="GN")) for i in range(30)]
    doc = read_noteful(build(pages, {"GN": paper}))
    assert [p.paper for p in doc.pages] == ["dotted"] * 30 and len(calls) == 1


def test_notebook_extras_are_reported() -> None:
    audio = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 20
    pages = [("P1", page_record("P1", "+E1", "ANN1"))]
    extra = [(5, RECORD, coll(["T1", "T2"], [ttv.encode([(1, STRING, "T1")]), ttv.encode([(1, STRING, "T2")])]))]
    data = build(pages, {"PDF0": user_pdf(), "REC": audio, "ANN1": annotation(), "ANN2": annotation()},
                 layers=2, bookmarks=1, extra=extra)
    doc = read_noteful(data)
    for fragment in ("2 layers were merged", "1 page bookmarks", "2 notebook entries", "1 embedded files",
                     "1 content records belong to no page"):
        assert any(fragment in w for w in doc.warnings), fragment


def test_unknown_type_costs_only_the_damaged_record() -> None:
    bad_obj = obj("T", 2, [1, 1, 1, 1, 0])[1] + struct.pack(">HH", 9, 0x00EE)
    ann_bad = ttv.encode([(1, U64, 280), (2, BYTES, ink_style((0, 0, 0, 1)) + ink_stroke([(1, 1), (5, 5)])),
                          (5, RECORD, coll(["T"], [bad_obj]))])
    page2 = page_record("P2", "+E2", "ANN2") + struct.pack(">HH", 0x20, 0x0077)  # tail of the record is unknown
    pages = [("P1", page_record("P1", "+E1", "ANN1")), ("P2", page2)]
    doc = read_noteful(build(pages, {"PDF0": user_pdf(), "ANN1": ann_bad, "ANN2": annotation(
        ink_style((1, 0, 0, 1)) + ink_stroke([(2, 2), (3, 3)]))}))
    assert [len(p.strokes) for p in doc.pages] == [1, 1]
    assert sum("is damaged" in w for w in doc.warnings) >= 1


def test_missing_page_list_salvages_the_content_records() -> None:
    blobs = {"ANN1": annotation(ink_style((0, 0, 0, 1)) + ink_stroke([(1, 1), (2, 2)])), "ANN2": annotation()}
    doc = read_noteful(build([("P1", b"")], blobs, with_page_list=False))
    assert len(doc.pages) == 2 and len(doc.pages[0].strokes) == 1
    assert any("rebuilt from the content records" in w for w in doc.warnings)
    with pytest.raises(ValueError, match="no readable page list"):
        read_noteful(build([("P1", b"")], {}, with_page_list=False))


@pytest.mark.parametrize("data", [
    b"", b"\xaa\xbb\xcc\xde", b"PK\x03\x04" + b"\x00" * 40, b"\xaa\xbb\xcc\xde" + b"\x00" * 30,
    b"\xaa\xbb\xcc\xde" + b"\xaa\xbb\xcc\xde" + b"\x00" * 4 + struct.pack(">II", 4, 100),
    b"\xaa\xbb\xcc\xde" + b"\x00" * 8 + b"\xaa\xbb\xcc\xde" + b"\x00" * 4 + struct.pack(">II", 4, 8),
], ids=["empty", "magic-only", "zip", "no-trailer", "root-outside", "no-blob-index"])
def test_not_a_noteful_file_is_a_value_error(data: bytes) -> None:
    with pytest.raises(ValueError):
        read_noteful(data)


def test_shared_records_and_pictures_cannot_amplify_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.noteful import reader as nf_reader

    ink = ink_style((0, 0, 0, 1)) + ink_stroke([(1, 1), (2, 2)]) + ink_stroke([(1e12, 1), (2, 2)])
    picture = b"\xff\xd8\xff\xe0" + b"\x00" * 4000
    objects = [obj(f"I{i}", 1, [50, 50, 20, 20, 0], z=i, t000a=(STRING, "PIC")) for i in range(6)]
    pages = [(f"P{i}", page_record(f"P{i}", f"+E{i}", "ANN1")) for i in range(3)]
    data = build(pages, {"PDF0": user_pdf(), "PIC": picture, "ANN1": annotation(ink, objects)})
    monkeypatch.setattr(nf_reader, "MAX_SHARED_IMAGE_BYTES", 0)
    doc = read_noteful(data)
    # the content record is decoded once; the other pages that name it stay empty
    assert [len(p.strokes) for p in doc.pages] == [1, 0, 0]
    assert sum("shares its content record" in w for w in doc.warnings) == 2
    assert any("1 strokes with non-finite or out-of-range" in w for w in doc.warnings)
    # every image uses the same picture: beyond the file's own size they are skipped
    images = doc.pages[0].images
    assert 0 < len(images) < 6 and all(im.data is images[0].data for im in images)
    assert any("reference the same pictures too often" in w for w in doc.warnings)
    no_header = build(pages[:1], {"PDF0": user_pdf()}).replace(b"n:" + b"AB" * 16, b"x:" + b"AB" * 16)
    assert any("header is missing" in w for w in read_noteful(no_header).warnings)


def test_shapes_share_the_point_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.noteful import reader as nf_reader

    fill = (RECORD | STAMPED, ttv.encode([(1, RECORD, ttv.encode([(0, LIST | F32, [1, 0, 0, 1])])), (2, BOOL, False)]))
    outline = (RECORD | STAMPED, ttv.encode([(2, F64, 1.0)]))
    huge = [obj(f"E{i}", 6, [5e8, 5e8, 1e9, 1e9, 0], z=i, t0005=fill, t0007=outline) for i in range(4)]
    doc = read_noteful(simple_file(objects=huge))
    fills = [s for s in doc.pages[0].strokes if s.kind == "fill"]
    assert len(fills) == 4 and all(len(f.outline[0]) <= nf_reader.MAX_FILL_SAMPLES + 17 for f in fills)
    monkeypatch.setattr(nf_reader, "MAX_INK_POINTS", 3000)
    doc = read_noteful(simple_file(objects=huge))
    assert 0 < len(doc.pages[0].strokes) < 8
    assert any("beyond the point limit" in w for w in doc.warnings)
    long = (RECORD | STAMPED, ttv.encode([(1, LIST | F64, [0.0] * (2 * nf_reader.MAX_SHAPE_POINTS + 2)),
                                          (2, LIST | I32, [0, 1])]))
    doc = read_noteful(simple_file(objects=[obj("P", 12, [1, 1, 1, 1, 0], t0007=outline, t000d=long)]))
    assert doc.pages[0].strokes == [] and any("unreadable outline" in w for w in doc.warnings)


def test_page_limit_and_values_outside_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.noteful import reader as nf_reader

    monkeypatch.setattr(nf_reader, "MAX_PAGES", 2)
    pages = [(f"P{i}", page_record(f"P{i}", f"+E{i}")) for i in range(4)]
    doc = read_noteful(build(pages, {"PDF0": user_pdf()}))
    assert len(doc.pages) == 2 and any("only the first 2" in w for w in doc.warnings)
    data = bytearray(simple_file())
    root_start = struct.unpack(">I", data[-8:-4])[0]
    starts_at = data.index(struct.pack(">HH", 11, LIST | U64), root_start)
    data[starts_at + 8:starts_at + 16] = struct.pack(">Q", 2 ** 40)  # first blob (n:) now points nowhere
    doc = read_noteful(bytes(data))
    assert doc.title == "Untitled" and any("outside the file" in w for w in doc.warnings)


def _damage(data: bytes, seed: int, mutations: int, cuts: int) -> None:
    """Truncate and mutate ``data``: reading may only ever end in a Document or ValueError."""
    import random

    rng = random.Random(seed)
    for cut in sorted({rng.randrange(len(data)) for _ in range(cuts)}):
        try:
            read_noteful(data[:cut])
        except ValueError:
            pass
    for _ in range(mutations):
        buf = bytearray(data)
        for _ in range(rng.choice((1, 2, 6, 16))):
            buf[rng.randrange(len(buf))] = rng.choice((0, 0xFF, 0x80, rng.randrange(256)))
        try:
            read_noteful(bytes(buf))
        except ValueError:
            pass


def test_truncation_and_random_damage_never_escape_as_other_errors() -> None:
    from gnnote.model import Document, Image, Page, Point, TextBox
    from gnnote.noteful.writer import write_noteful

    base = simple_file(ink_style((0, 0, 0, 1)) + ink_stroke([(i, i * 2, 1.0) for i in range(30)], variable=True))
    page = Page(300, 400, strokes=[Stroke([Point(10 + i, 20 + i % 3, 1 + i % 2) for i in range(40)])],
                texts=[TextBox(10, 10, 100, 20, "fuzz")],
                images=[Image(5, 5, 20, 20, b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)])
    written = write_noteful(Document(pages=[page]))
    for seed, data in enumerate((base, written)):
        _damage(data, seed, mutations=300, cuts=len(data) // 7)


@pytest.mark.parametrize("name", NOTEFUL_SAMPLES)
def test_damaged_samples_never_escape_as_other_errors(samples, name: str) -> None:
    path = sample_files(samples)[NOTEFUL_SAMPLES.index(name)]
    _damage(path.read_bytes(), seed=NOTEFUL_SAMPLES.index(name), mutations=80, cuts=40)


# --------------------------------------------------------------------------- samples vs oracle


@pytest.fixture(scope="module")
def oracle(samples) -> Dict[str, Any]:
    files = sample_files(samples)
    result = run_oracle(samples, files)
    return {Path(k).stem: v for k, v in result.items()}


@pytest.fixture(scope="module")
def sample_docs(samples) -> Dict[str, Any]:
    return {f.stem: read_noteful(f.read_bytes()) for f in sample_files(samples)}


# file -> (document stats, warning fragments)
SAMPLE_EXPECTED: Dict[str, Tuple[Dict[str, int], Tuple[str, ...]]] = {
    "bookmarks.noteful": ({"pages": 3, "strokes": 0, "images": 0, "texts": 0, "pdfs": 0}, ("2 page bookmarks",)),
    "dot-strokes.noteful": ({"pages": 1, "strokes": 5, "images": 0, "texts": 0, "pdfs": 0}, ()),
    "empty.noteful": ({"pages": 1, "strokes": 0, "images": 0, "texts": 0, "pdfs": 0}, ()),
    "handwriting.noteful": ({"pages": 1, "strokes": 6, "images": 0, "texts": 0, "pdfs": 0}, ("2 dashed or dotted",)),
    "image-insert.noteful": ({"pages": 1, "strokes": 0, "images": 2, "texts": 0, "pdfs": 0},
                     ("1 cropped images", "1 mirrored images")),
    "pdf-pages.noteful": ({"pages": 3, "strokes": 0, "images": 0, "texts": 0, "pdfs": 2}, ()),
    "shapes.noteful": ({"pages": 1, "strokes": 16, "images": 0, "texts": 0, "pdfs": 0}, ("2 dashed or dotted",)),
    "text.noteful": ({"pages": 1, "strokes": 0, "images": 0, "texts": 3, "pdfs": 0}, ("2 text box background",)),
    "three-pages.noteful": ({"pages": 3, "strokes": 3, "images": 0, "texts": 1, "pdfs": 0}, ()),
    "z-order.noteful": ({"pages": 1, "strokes": 2, "images": 0, "texts": 1, "pdfs": 0}, ("stacking of ink",)),
}


@pytest.mark.parametrize("name", NOTEFUL_SAMPLES)
def test_sample_counts_and_warnings(samples, sample_docs, name: str) -> None:
    doc = sample_docs[name]
    path = sample_files(samples)[NOTEFUL_SAMPLES.index(name)]
    expected = samples.expected_for(path, SAMPLE_EXPECTED)
    if expected is None:
        pytest.skip("notesconverter is not at the pinned commit")
    stats, fragments = expected
    assert document_stats(doc) == stats
    assert len(doc.warnings) == len(fragments), doc.warnings
    for fragment in fragments:
        assert any(fragment in w for w in doc.warnings), (fragment, doc.warnings)
    assert detect_format(path.name, path.read_bytes()) == "noteful"
    assert detect_format("renamed.bin", path.read_bytes()) == "noteful"


def _expected_shape_strokes(o: Dict[str, Any]) -> int:
    if o["type"] not in (3, 6, 12, 20, 21):
        return 0
    n = 0
    fill, stroke = o.get("fill"), o.get("stroke")
    closed = o["type"] in (3, 6) or 4 in o.get("commands", [])
    if fill and not fill["flag"] and fill["rgba"] and closed:
        n += 1
    if stroke and stroke["thickness"] > 0:
        n += 1 + (1 if stroke["arrow"] and o["type"] in (20, 21) else 0)
    return n


@pytest.mark.parametrize("name", NOTEFUL_SAMPLES)
def test_sample_matches_the_oracle(oracle, sample_docs, name: str) -> None:
    ref = oracle[name]
    assert "error" not in ref, ref.get("error")
    doc = sample_docs[name]
    assert doc.title == ref["title"]
    assert len(doc.pages) == len(ref["pages"])
    for page, rp in zip(doc.pages, ref["pages"]):
        # page order is the oracle's (ordering tags) and sizes / backgrounds agree
        assert (page.width * U, page.height * U) == pytest.approx(tuple(rp["size"]), abs=1e-9)
        assert page.background is not None and page.background.pdf_id == rp["pdf"]
        assert page.background.page_index == rp["pdf_page"]
        assert page.template_is_builtin == (rp["background"] == "template")
        # ink: our polyline strokes in z order are the oracle's records in z order
        ink = [s for s in page.strokes if s.kind != "fill" and s.controls is None]
        ref_ink = [s for _i, s in sorted(enumerate(rp["ink"]), key=lambda t: (t[1]["z"], t[0]))]
        assert len(ink) == len(ref_ink)
        for ours, theirs in zip(ink, ref_ink):
            assert len(ours.points) == len(theirs["points"])
            for p, q in zip(ours.points, theirs["points"]):
                assert (p.x * U, p.y * U) == pytest.approx((q[0], q[1]), abs=1e-6)
                radius = q[2] if theirs["variable"] else theirs["nominal"]
                assert p.width == pytest.approx(2 * radius * K, abs=1e-9)
            assert ours.width == pytest.approx(2 * theirs["nominal"] * K)
            rgba = list(theirs["rgba"])
            if theirs["blend"] == 1:
                assert ours.kind == "highlighter"
                rgba[3] *= 0.5
            else:
                assert ours.kind == "pen"
            assert list(ours.color) == pytest.approx(rgba)
        # shapes: one stroke per outline / fill / arrow head, starting where the oracle's path does
        shape_strokes = [s for s in page.strokes if s.kind == "fill" or s.controls is not None]
        objects = sorted(rp["objects"], key=lambda o: o["z"])
        assert len(shape_strokes) == sum(_expected_shape_strokes(o) for o in objects)
        starts = [(s.points[0].x * U, s.points[0].y * U) for s in shape_strokes if s.kind != "fill"]
        for o in objects:
            if "points" not in o or not o.get("stroke") or o["stroke"]["thickness"] <= 0:
                continue
            cx, cy, w, h, theta = o["box"]
            sw, sh = o["size"]
            px, py = o["points"][0]
            lx = px * (w / sw if sw > 1e-9 else 1) - w / 2
            ly = py * (h / sh if sh > 1e-9 else 1) - h / 2
            expected = (cx + lx * math.cos(theta) - ly * math.sin(theta), cy + lx * math.sin(theta) + ly * math.cos(theta))
            assert any(math.hypot(x - expected[0], y - expected[1]) < 1e-6 for x, y in starts), o["uuid"]
        # text boxes in z order: text, runs, alignment
        texts = [o for o in objects if o["type"] == 2]
        assert len(page.texts) == len(texts)
        for box, o in zip(page.texts, texts):
            t = o["text"]
            assert box.text == "".join(t["strings"]).replace("\u200b", "")
            assert sum(r.size is not None for r in box.runs) == len(box.runs)
            assert (box.w * U + 10, box.h * U + 4) == pytest.approx(tuple(o["box"][2:4]))
        # images: one per image object, the picture the object names
        images = [o for o in objects if o["type"] == 1]
        assert len(page.images) == len(images)
        assert sorted(rp["files_on_page"]) == sorted({o["image"]["uuid"] for o in images})


def test_text_sample_formatting(sample_docs) -> None:
    doc = sample_docs["text"]
    by_text = {box.text.split("\n")[0]: box for box in doc.pages[0].texts}
    assert set(by_text) == {"hello world", "center", "right"}
    assert by_text["center"].align == "center" and by_text["right"].align == "right"
    assert by_text["hello world"].align == "left"
    runs = [(r.text, r.bold, r.italic, r.underline, r.font) for r in by_text["hello world"].runs]
    assert runs == [("hello ", False, False, False, "Helvetica"), ("world", True, False, False, "Helvetica"),
                    ("\n", False, False, False, "Helvetica"), ("asd\n", True, True, True, "Helvetica"),
                    ("Pap", True, True, False, "Papyrus"), ("yrus", True, False, True, "Papyrus"),
                    ("\n", True, True, False, "Papyrus"), ("hello", False, False, False, "Helvetica")]
    purple = (0.37254902720451355, 0.18039216101169586, 0.9215686321258545, 1.0)
    assert all(r.color == pytest.approx(purple) and r.size == pytest.approx(12.0) for r in by_text["hello world"].runs)
    big = sample_docs["three-pages"].pages[2].texts[0]
    assert big.text == "3" and big.size == pytest.approx(288 * K)


# --------------------------------------------------------------------------- Noteful's own PDF exports


def _content_stream(pdf: bytes) -> str:
    for m in re.finditer(rb"<<[^>]*?/FlateDecode[^>]*?>>\s*stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            text = zlib.decompress(m.group(1)).decode("latin-1")
        except zlib.error:
            continue
        if re.search(r"\bcm\b", text):
            return text
    raise AssertionError("no page content stream")


def _export(samples, name: str) -> str:
    path = samples.repo("notesconverter") / "samples" / "noteful" / f"{name}.pdf"
    if not path.is_file():
        pytest.skip(f"{name}.pdf missing")
    return _content_stream(path.read_bytes())


def test_geometry_matches_noteful_pdf_exports(samples, sample_docs) -> None:
    height = 841.8898
    # straight lines (one shortened by its arrow head): end points of the stroked paths
    stream = _export(samples, "shapes")
    lines = re.findall(r"q ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)\s+cm ([-\d.]+) ([-\d.]+) m "
                       r"([-\d.]+) ([-\d.]+) l S Q", stream)
    assert len(lines) == 2
    strokes = [s for s in sample_docs["shapes"].pages[0].strokes if len(s.points) == 2]
    for a, b, c, d, e, f, x0, y0, x1, y1 in (map(float, m) for m in lines):
        ends = [(a * x + c * y + e, height - (b * x + d * y + f)) for x, y in ((x0, y0), (x1, y1))]
        assert any(max(abs(s.points[0].x - ends[0][0]), abs(s.points[0].y - ends[0][1]),
                       abs(s.points[-1].x - ends[1][0]), abs(s.points[-1].y - ends[1][1])) < 0.001
                   for s in strokes)
    # images: the plain one exactly where the export draws it, the flipped one mirrored about its box
    stream = _export(samples, "image-insert")
    draws = [tuple(map(float, m)) for m in re.findall(
        r"([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)\s+cm\s*/Im\d+\s+Do", stream)]
    clip = tuple(map(float, re.search(r"([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) re W n /Perceptual", stream).groups()))
    plain, flipped = sorted(sample_docs["image-insert"].pages[0].images, key=lambda im: im.w)
    a, _b, _c, d, e, f = draws[0]
    assert (plain.x, plain.y, plain.w, plain.h) == pytest.approx((e, height - f - d, a, d), abs=0.001)
    a, _b, _c, d, e, f = draws[1]
    centre_x = clip[0] + clip[2] / 2
    left = min(e, e + a)
    assert (flipped.w, flipped.h, flipped.y) == pytest.approx((abs(a), d, height - f - d), abs=0.001)
    assert flipped.x == pytest.approx(2 * centre_x - (left + abs(a)), abs=0.001)
    # text boxes: the export clips each box to its outer frame = our frame + the (5, 2) inset
    stream = _export(samples, "text")
    frames = sorted(tuple(map(float, m)) for m in re.findall(
        r"([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)\s+re\s+W\s+n", stream) if float(m[2]) < 500)
    ours = sorted((t.x - 5 * K, height - (t.y - 2 * K) - (t.h + 4 * K), t.w + 10 * K, t.h + 4 * K)
                  for t in sample_docs["text"].pages[0].texts)
    assert len(frames) == 3
    for mine, theirs in zip(ours, frames):
        assert mine == pytest.approx(theirs, abs=0.001)
    # ink: the dashed pen is stroked 4.158 units wide = twice its 2.079-unit radius
    stream = _export(samples, "handwriting")
    assert "4.158 w" in stream
    dashed = [s for s in sample_docs["handwriting"].pages[0].strokes if abs(s.width * U - 4.158) < 1e-3]
    assert len(dashed) == 2


# --------------------------------------------------------------------------- conversions


@pytest.mark.parametrize("name", NOTEFUL_SAMPLES)
def test_sample_converts_to_goodnotes_notability_and_back(samples, name: str) -> None:
    path = sample_files(samples)[NOTEFUL_SAMPLES.index(name)]
    data = path.read_bytes()
    source = read_noteful(data)
    stats = document_stats(source)
    gn = convert(data, path.name, Options(target="goodnotes"))
    nb = convert(data, path.name, Options(target="notability"))
    assert gn.filename == path.stem + ".goodnotes" and nb.filename == path.stem + ".note"
    back_gn = read_goodnotes(gn.data)
    back_nb = read_note(nb.data)
    assert len(back_gn.pages) == len(back_nb.pages) == stats["pages"]
    fills = sum(s.kind == "fill" for p in source.pages for s in p.strokes)
    assert sum(len(p.texts) for p in back_gn.pages) == stats["texts"]
    assert sum(len(p.images) for p in back_gn.pages) == stats["images"]
    assert sum(len(p.strokes) for p in back_nb.pages) == stats["strokes"] - fills  # Notability has no fills
    # and back to Noteful from both apps' files
    for other, filename in ((gn.data, gn.filename), (nb.data, nb.filename)):
        again = convert(other, filename, Options(target="noteful"))
        doc = read_noteful(again.data)
        assert len(doc.pages) == stats["pages"]
        assert sum(len(p.texts) for p in doc.pages) == stats["texts"]
    assert to_document(data, "x.noteful").source_format == "noteful"
