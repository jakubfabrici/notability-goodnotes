"""Samsung Notes ``.sdocx`` reader: record primitives, synthetic notes built in code, the public
sample notes (with the app's own PDF exports as a reference), inkterop's reader as an oracle,
conversions, the registry and sniffing, hostile input."""
from __future__ import annotations

import io
import json
import math
import os
import random
import struct
import subprocess
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from gnnote import formats, pdfutil, readutil
from gnnote.cli import main
from gnnote.codecutil import stroke_polyline
from gnnote.convert import Options, convert, detect_format, to_document
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.model import Document, Stroke
from gnnote.notability.reader import read_note
from gnnote.pdf.reader import read_pdf
from gnnote.samsungnotes import records as R
from gnnote.samsungnotes.reader import PAGE_WIDTH_PT, crop_png, find_note_root, read_sdocx

sys.path.insert(0, str(Path(__file__).parent))
import sdocx_builder as B  # noqa: E402

PEN = B.PEN
STRINGS = {1: PEN + "FountainPen", 2: PEN + "Marker4", 3: PEN + "StraightHighlighter", 4: PEN + "BrushPen",
           5: PEN + "InkPen2", 6: PEN + "Pencil2", 7: "18;0;100;", 8: PEN + "Marker3", 9: PEN + "TapePen"}
S = PAGE_WIDTH_PT / 1080.0  # pt per canvas unit of a 1080-unit-wide synthetic page
SAMPLES = ("01-basic-formatting.sdocx", "02-shapes-and-dot-calibration.sdocx", "03-image-placement.sdocx",
           "04-marker4-highlighter.sdocx")


def note(objects: Sequence[Tuple[int, bytes]] = (), **kwargs) -> bytes:
    """A one-page note (plus the empty last page the app keeps) holding ``objects``."""
    page_kwargs = {k: kwargs.pop(k) for k in ("template", "pdfs", "background", "recognition", "custom_objects")
                   if k in kwargs}
    pages = [B.page("page-0", 1080, 1527, [B.layer(objects)], **page_kwargs), B.page("page-1", 1080, 1527, [B.layer([])])]
    kwargs.setdefault("strings", STRINGS)
    return B.sdocx(pages, **kwargs)


def read(objects: Sequence[Tuple[int, bytes]] = (), **kwargs) -> Document:
    return read_sdocx(note(objects, **kwargs))


def zip_members(members: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def members_of(data: bytes) -> Dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def sample(samples, name: str) -> bytes:
    return samples.pinned_file(name).read_bytes()


PRESSURE = "Pen pressure was turned into per-point stroke widths with an approximate model; line weights may " \
    "differ from the app"


def quiet(doc: Document) -> List[str]:
    """The warnings without the one every note with pressure pens gets."""
    return [w for w in doc.warnings if w != PRESSURE]


# --------------------------------------------------------------------------- primitives


def test_fixed_point_deltas() -> None:
    assert R.fixed_point_delta(0x0020) == 1.0  # integer part 1
    assert R.fixed_point_delta(0x0001) == 1.0 / 32.0
    assert R.fixed_point_delta(0x8021) == -(1.0 + 1.0 / 32.0)
    assert R.fixed_point_delta(0x7FFF) == 1023.0 + 31.0 / 32.0
    assert R.fixed_small_delta(0x1000) == 1.0
    assert R.fixed_small_delta(0x0800) == 0.5
    assert R.fixed_small_delta(0x8001) == -1.0 / 4096.0
    assert R.fixed_small_delta(0x7FFF) == 7.0 + 4095.0 / 4096.0


def test_cursor_reads_are_bounded() -> None:
    c = R.Cursor(b"\x01\x02\x03")
    assert c.u16() == 0x0201 and c.remaining() == 1
    with pytest.raises(R.SdocxError, match="truncated"):
        c.u32()
    with pytest.raises(ValueError):  # SdocxError is a ValueError
        R.Cursor(b"abc", 2, 9)
    with pytest.raises(R.SdocxError, match="bitfield of 5 bytes"):
        R.Cursor(b"\x05" + bytes(5)).bitfield()
    assert R.Cursor(b"\x02\x01\x80").bitfield() == 0x8001
    with pytest.raises(R.SdocxError, match="does not fit"):
        R.Cursor(struct.pack("<I", 1000) + bytes(8)).count(4)
    sub = R.Cursor(bytes(10), 2, 8)
    with pytest.raises(R.SdocxError, match="outside the record"):
        sub.seek(9)
    assert R.units_to_text(R.Cursor("a\U0001F600".encode("utf-16-le")).utf16_units(3)) == "a\U0001F600"


def test_compressed_and_uncompressed_events_decode_alike() -> None:
    pts = [(100.0, 200.0), (101.5, 199.25), (103.0, 210.0), (90.0, 205.5)]
    pressures = [0.25, 0.5, 0.75, 0.5]
    strings = {1: PEN + "FountainPen"}
    for tilt in (False, True):
        decoded = []
        for compressed in (False, True):
            payload = B.stroke(pts, pressures, pen_id=1, compressed=compressed, tilt=tilt)
            obj = R.parse_object(R.Cursor(payload), 1, strings, R.Limits())
            assert isinstance(obj, R.Stroke)
            decoded.append((list(obj.xs), list(obj.ys), list(obj.pressures), obj.pen_name, obj.has_tilt))
        assert decoded[0] == decoded[1]
        assert decoded[0][:3] == ([p[0] for p in pts], [p[1] for p in pts], pressures)


def test_stroke_fields_follow_the_field_bits() -> None:
    payload = B.stroke([(1.0, 2.0), (3.0, 4.0)], pen_id=2, colour=b"\x10\x20\x30\x40", size=12.5, line_type=1)
    obj = R.parse_object(R.Cursor(payload), 1, {2: "pen"}, R.Limits())
    assert (obj.pen_name, obj.color_bgra, obj.pen_size, obj.line_type, obj.unknown_fields) == \
        ("pen", b"\x10\x20\x30\x40", 12.5, 1, 0)
    unknown = R.parse_object(R.Cursor(B.stroke([(1.0, 2.0)], unknown_field=True)), 1, {}, R.Limits())
    assert unknown.unknown_fields == 1 << 5


def test_object_limit_is_shared_and_raises() -> None:
    limits = R.Limits(objects=2)
    payload = B.stroke([(1.0, 1.0)])
    R.parse_object(R.Cursor(payload), 1, {}, limits)
    R.parse_object(R.Cursor(payload), 1, {}, limits)
    with pytest.raises(R.SdocxError, match="more objects"):
        R.parse_object(R.Cursor(payload), 1, {}, limits)
    assert limits.exhausted


# --------------------------------------------------------------------------- strokes


def test_stroke_geometry_colour_and_pressure_widths() -> None:
    pts = [(108.0, 216.0), (216.0, 324.0), (324.0, 216.0)]
    doc = read([(1, B.stroke(pts, [0.1, 0.55, 0.9], pen_id=1, colour=b"\x30\x20\x10\xff", size=9.0))])
    assert doc.source_format == "samsungnotes" and len(doc.pages) == 1  # the empty last page is dropped
    page = doc.pages[0]
    assert (page.width, page.height) == (600.0, pytest.approx(1527 * S))
    (st,) = page.strokes
    assert [(p.x, p.y) for p in st.points] == [(pytest.approx(x * S), pytest.approx(y * S)) for x, y in pts]
    assert st.color == pytest.approx((0x10 / 255, 0x20 / 255, 0x30 / 255, 1.0))  # stored B, G, R, A
    assert (st.kind, st.pen, st.controls) == ("pen", "fountain", None)
    # width = pen size x clamp(pressure, 0.4, 0.7) at every point
    assert [p.width for p in st.points] == pytest.approx([9 * S * 0.4, 9 * S * 0.55, 9 * S * 0.7])
    assert st.width == pytest.approx(9 * S * 0.55)
    assert doc.warnings == ["Pen pressure was turned into per-point stroke widths with an approximate model; "
                            "line weights may differ from the app"]


def test_compressed_strokes_with_tilt_read_like_plain_ones() -> None:
    pts = [(100.0, 100.0), (110.5, 98.25), (130.0, 120.0)]
    plain = read([(1, B.stroke(pts, pen_id=1))]).pages[0].strokes[0]
    packed = read([(1, B.stroke(pts, pen_id=1, compressed=True, tilt=True))]).pages[0].strokes[0]
    assert [(p.x, p.y, p.width) for p in packed.points] == [(p.x, p.y, p.width) for p in plain.points]


@pytest.mark.parametrize("pen_id, kind, pen, factor, n", [
    (2, "highlighter", "marker", 1.0, 3),  # Marker4: as wide as its pen size
    (8, "highlighter", "marker", 1.0, 3),  # Marker3
    (3, "highlighter", "marker", 1.0, 2),  # StraightHighlighter: first to last point
    (4, "pen", "brush", 0.45, 3),  # BrushPen ignores pressure
    (9, "pen", None, 0.45, 3),  # TapePen: not a known pen kind
])
def test_pen_families(pen_id: int, kind: str, pen: Optional[str], factor: float, n: int) -> None:
    pts = [(100.0, 100.0), (150.0, 130.0), (200.0, 100.0)]
    doc = read([(1, B.stroke(pts, [0.9, 0.1, 0.9], pen_id=pen_id, colour=b"\x00\xcc\xff\x80", size=20.0))])
    (st,) = doc.pages[0].strokes
    assert (st.kind, st.pen, len(st.points)) == (kind, pen, n)
    assert {round(p.width, 6) for p in st.points} == {round(20.0 * S * factor, 6)}
    assert st.color == pytest.approx((1.0, 0.8, 0.0, 0x80 / 255))
    assert (st.points[-1].x, st.points[-1].y) == (pytest.approx(200 * S), pytest.approx(100 * S))
    assert not any("pressure" in w for w in doc.warnings)


def test_fixed_width_ink_pen_and_fixed_opacity_pencil_ignore_pressure() -> None:
    doc = read([(1, B.stroke([(1.0, 1.0), (50.0, 50.0)], [0.1, 0.9], pen_id=5, fixed_width=True, size=10.0)),
                (1, B.stroke([(1.0, 1.0), (50.0, 50.0)], [0.1, 0.9], pen_id=5, size=10.0))])
    fixed, pressure = doc.pages[0].strokes
    assert [p.width for p in fixed.points] == pytest.approx([4.5 * S, 4.5 * S]) and fixed.pen == "ballpoint"
    assert [p.width for p in pressure.points] == pytest.approx([4.0 * S, 7.0 * S])


def test_erasers_unknown_fields_and_strokes_without_size_are_reported() -> None:
    good = B.stroke([(10.0, 10.0), (20.0, 20.0)], pen_id=1)
    doc = read([(1, good), (1, B.stroke([(1.0, 1.0)], pen_id=1, eraser=True)),
                (1, B.stroke([(1.0, 1.0)], pen_id=1, tool_type=4)),
                (1, B.stroke([(1.0, 1.0), (5.0, 5.0)], pen_id=1, unknown_field=True)),
                (1, B.stroke([(1.0, 1.0), (5.0, 5.0)], pen_id=1, size=0.0)),
                (1, B.stroke([], pen_id=1)),
                (1, B.stroke([(1.0, 1.0), (5.0, 5.0)], pen_id=1, line_type=2))])
    page = doc.pages[0]
    assert len(page.strokes) == 3  # the good one, the one without a size, the dotted one
    assert page.strokes[1].width == pytest.approx(2.0 * S * 0.55)
    assert doc.warnings == [
        "2 eraser stroke(s) were skipped",
        "1 stroke(s) store settings of a newer Samsung Notes version and were skipped",
        "1 stroke(s) without usable points were skipped",
        "1 stroke(s) without a pen size were drawn with a default pen size",
        "1 dashed or dotted stroke(s) or line(s) are drawn solid",
        "Pen pressure was turned into per-point stroke widths with an approximate model; line weights may differ "
        "from the app",
    ]


def test_invisible_objects_are_skipped() -> None:
    doc = read([(1, B.stroke([(5.0, 5.0)], pen_id=4)), (1, B.stroke([(9.0, 9.0)], pen_id=4, visible=False))])
    assert len(doc.pages[0].strokes) == 1
    assert doc.warnings == ["1 object(s) marked as invisible were not converted"]


def test_strokes_with_unusable_coordinates_are_dropped() -> None:
    nan = float("nan")
    doc = read([(1, B.stroke([(nan, 1.0), (1e300, 5.0)], pen_id=1)),
                (1, B.stroke([(nan, 1.0), (10.0, 5.0)], pen_id=1))])
    (st,) = doc.pages[0].strokes
    assert len(st.points) == 1 and st.points[0].x == pytest.approx(10 * S)
    assert "1 stroke(s) without usable points were skipped" in doc.warnings


def test_point_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    monkeypatch.setattr(RD, "PointBudget", lambda: readutil.PointBudget(5))
    doc = read([(1, B.stroke([(float(i), 1.0) for i in range(4)], pen_id=1))] * 3)
    assert len(doc.pages[0].strokes) == 1
    assert "The note holds more ink points than gnnote reads; the remaining strokes were skipped" in doc.warnings


BUDGET_NOTE = "The note holds more ink points than gnnote reads; the remaining strokes were skipped"


def test_stroke_events_are_charged_to_the_point_budget_before_decoding() -> None:
    budget = readutil.PointBudget(5)
    limits = R.Limits(budget=budget)
    four = B.stroke([(float(i), 1.0) for i in range(4)], pen_id=1, compressed=True)
    first = R.parse_object(R.Cursor(four), 1, STRINGS, limits)
    second = R.parse_object(R.Cursor(four), 1, STRINGS, limits)
    assert list(first.xs) == [0.0, 1.0, 2.0, 3.0] and not first.over_budget
    assert second.over_budget and (len(second.xs), second.event_count) == (0, 4)
    assert second.pen_name == PEN + "FountainPen"  # everything but the events is still read
    assert budget.exhausted and budget.left == 1
    # strokes the reader skips anyway are neither decoded nor charged
    for payload in (B.stroke([(1.0, 1.0)] * 3, eraser=True), B.stroke([(1.0, 1.0)] * 3, tool_type=4),
                    B.stroke([(1.0, 1.0)] * 3, unknown_field=True)):
        nothing = readutil.PointBudget(0)
        obj = R.parse_object(R.Cursor(payload), 1, {}, R.Limits(budget=nothing))
        assert len(obj.xs) == 0 and not obj.over_budget and not nothing.exhausted


def test_a_compressible_stroke_flood_stops_at_the_point_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delta-compressed events take 8 bytes each and compress to almost nothing: the events
    beyond the point budget must never be decoded (they used to be, all of them, first)."""
    monkeypatch.setattr(readutil, "MAX_POINTS", 50_000)
    decoded: List[int] = []
    real = R._events

    def counting(c: R.Cursor, count: int, *args: bool):  # type: ignore[no-untyped-def]
        decoded.append(count)
        return real(c, count, *args)

    monkeypatch.setattr(R, "_events", counting)
    big = B.stroke([(100.0 + (i % 7), 100.0) for i in range(20_000)], pen_id=1, compressed=True)
    pages = [B.page(f"p{i}", 1080, 1527, [B.layer([(1, big)] * 10)]) for i in range(3)]
    start = time.monotonic()
    doc = read_sdocx(B.sdocx(pages, strings=STRINGS, uuids=["p0", "p1", "p2"]))
    assert time.monotonic() - start < 30
    assert decoded == [20_000, 20_000]
    assert [sum(len(s.points) for s in p.strokes) for p in doc.pages] == [40_000, 0, 0]
    assert BUDGET_NOTE in doc.warnings


def test_colour_effects_are_drawn_solid_with_a_warning() -> None:
    doc = read([(1, B.stroke([(1.0, 1.0), (5.0, 5.0)], pen_id=4, rainbow=True)),
                (1, B.stroke([(1.0, 1.0), (5.0, 5.0)], pen_id=4, gradient=[b"\x00\x00\xff\xff", b"\xff\x00\x00\xff"],
                             colour_type=1)),
                (1, B.stroke([(1.0, 1.0), (5.0, 5.0)], pen_id=4, colour_type=2)),
                (1, B.stroke([(1.0, 1.0), (5.0, 5.0)], pen_id=4, colour_type=0)),
                (1, B.stroke([(1.0, 1.0), (5.0, 5.0)], pen_id=4, gradient=[b"\x00\x00\xff\xff"], colour_type=0))])
    assert len(doc.pages[0].strokes) == 5
    assert doc.warnings == ["3 stroke(s) with a gradient, pattern or rainbow colour are drawn in one solid colour"]


# --------------------------------------------------------------------------- pages, layers, container


def test_page_order_comes_from_the_page_list() -> None:
    pages = [B.page(u, 1080, 1527, [B.layer([(1, B.stroke([(10.0 * (i + 1), 10.0)], pen_id=1))])])
             for i, u in enumerate(("a", "b", "c"))]
    doc = read_sdocx(B.sdocx(pages, strings=STRINGS, uuids=["a", "b", "c"]))
    assert [p.strokes[0].points[0].x for p in doc.pages] == pytest.approx([10 * S, 20 * S, 30 * S])
    # the same members, listed in another order
    members = members_of(B.sdocx(pages, strings=STRINGS, uuids=["a", "b", "c"]))
    members["pageIdInfo.dat"] = B.page_list(["c", "a", "b"])
    doc = read_sdocx(zip_members(members))
    assert [p.strokes[0].points[0].x for p in doc.pages] == pytest.approx([30 * S, 10 * S, 20 * S])
    assert quiet(doc) == []


def test_missing_and_unlisted_pages() -> None:
    pages = [B.page(u, 1080, 1527, [B.layer([(1, B.stroke([(5.0, 5.0)], pen_id=1))])]) for u in ("a", "b")]
    members = members_of(B.sdocx(pages, strings=STRINGS, uuids=["a", "b"]))
    members["pageIdInfo.dat"] = B.page_list(["a", "zzz"])
    doc = read_sdocx(zip_members(members))
    assert len(doc.pages) == 1
    assert quiet(doc) == ["1 page(s) of the page list are missing from the archive",
                            "1 page file(s) not in the page list were ignored"]
    del members["pageIdInfo.dat"]
    doc = read_sdocx(zip_members(members))
    assert len(doc.pages) == 2
    assert quiet(doc) == ["The page list (pageIdInfo.dat) is missing; pages follow the archive order"]
    members["pageIdInfo.dat"] = b"\x01\x02"
    doc = read_sdocx(zip_members(members))
    assert len(doc.pages) == 2
    assert quiet(doc) == ["The page list (pageIdInfo.dat) is damaged; pages follow the archive order"]


def test_page_cap_is_reported_with_or_without_a_page_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    monkeypatch.setattr(RD, "MAX_PAGES", 2)
    pages = [B.page(u, 1080, 1527, [B.layer([(1, B.stroke([(5.0, 5.0)], pen_id=1))])]) for u in "abc"]
    listed = B.sdocx(pages, strings=STRINGS, uuids=list("abc"))
    unlisted = B.sdocx(pages, strings=STRINGS, uuids=list("abc"), page_list_member=False)
    for data in (listed, unlisted):
        doc = read_sdocx(data)
        assert len(doc.pages) == 2
        assert "The note has 3 pages; only the first 2 were converted" in doc.warnings


def test_sticky_notes_are_reported() -> None:
    sticky = B.short_utf8("sticky") + struct.pack("<4d", 100, 100, 500, 500)
    doc = read([(1, B.stroke([(5.0, 5.0)], pen_id=1))], recognition=[b"ink words", b""],
               custom_objects=[sticky, sticky])
    assert len(doc.pages[0].strokes) == 1
    assert quiet(doc) == ["2 sticky note(s) or other custom page object(s) were not converted"]
    # a page with nothing but a sticky note is not the app's empty last page
    data = B.sdocx([B.page("a", 1080, 1527, [B.layer([(1, B.stroke([(5.0, 5.0)], pen_id=1))])]),
                    B.page("b", 1080, 1527, [B.layer([])], custom_objects=[sticky])], strings=STRINGS)
    doc = read_sdocx(data)
    assert len(doc.pages) == 2
    assert "1 sticky note(s) or other custom page object(s) were not converted" in doc.warnings


def test_a_font_size_change_of_the_body_text_is_reported() -> None:
    body = B.text_object(B.text_common("Typed"))
    assert not any("changes the size" in w for w in read([], body=body, font_delta=0).warnings)
    doc = read([], body=body, font_delta=-2)
    assert "The note changes the size of its typed text by -2 (a setting of the app); gnnote did not apply it" \
        in doc.warnings


def test_a_note_without_pages_gets_one() -> None:
    doc = read_sdocx(B.sdocx([], body=B.text_object(B.text_common("Only typed text"))))
    assert len(doc.pages) == 1 and [t.text for t in doc.pages[0].texts] == ["Only typed text"]
    assert doc.warnings[0] == "The note has no pages; its typed text was laid out on new pages"
    doc = read_sdocx(B.sdocx([]))
    assert len(doc.pages) == 1 and doc.warnings == ["The note has no pages; an empty page was made"]
    assert (doc.pages[0].width, round(doc.pages[0].height, 2)) == (600.0, 848.53)


def test_page_size_comes_from_the_page_record() -> None:
    pages = [B.page("a", 1848, 2613, [B.layer([(1, B.stroke([(1848.0, 2613.0)], pen_id=1))])]),
             B.page("b", 2613, 1848, [B.layer([(1, B.stroke([(2613.0, 1848.0)], pen_id=1))])])]
    doc = read_sdocx(B.sdocx(pages, strings=STRINGS, width=1848, height=2613))
    assert [(p.width, round(p.height, 3)) for p in doc.pages] == [(600.0, 848.377), (600.0, 424.34)]
    assert [(p.strokes[0].points[0].x, round(p.strokes[0].points[0].y, 3)) for p in doc.pages] == \
        [(600.0, 848.377), (600.0, 424.34)]


def test_unusable_page_size_falls_back_to_a4() -> None:
    doc = read_sdocx(B.sdocx([B.page("a", 3, 0, [B.layer([(1, B.stroke([(10.0, 10.0)], pen_id=1))])])],
                             strings=STRINGS, width=0))
    assert (doc.pages[0].width, round(doc.pages[0].height, 2)) == (600.0, 848.33)
    assert doc.warnings[0] == "Page 1: the page has no usable size; an A4-shaped page was used"


def test_pages_too_low_to_hold_anything_fall_back_to_a4() -> None:
    # 1,000,000 x 16 canvas units passes the unit checks but is 0.01 pt high once 600 pt wide
    for width, height in ((1_000_000, 16), (1080, 60)):
        doc = read_sdocx(B.sdocx([B.page("a", width, height, [B.layer([(1, B.stroke([(10.0, 10.0)], pen_id=1))])])],
                                 strings=STRINGS))
        assert (doc.pages[0].width, round(doc.pages[0].height, 1)) == (600.0, 848.3)
        assert "Page 1: the page has no usable size; an A4-shaped page was used" in doc.warnings
    doc = read_sdocx(B.sdocx([B.page("a", 1080, 65, [B.layer([(1, B.stroke([(10.0, 10.0)], pen_id=1))])])],
                             strings=STRINGS))
    assert doc.pages[0].height == pytest.approx(65 * S)  # 36.1 pt: low, but a page


def test_ghost_page_is_dropped_only_when_empty_and_paged() -> None:
    blank = B.page("b", 1080, 1527, [B.layer([])])
    inked = B.page("a", 1080, 1527, [B.layer([(1, B.stroke([(5.0, 5.0)], pen_id=1))])])
    assert len(read_sdocx(B.sdocx([inked, blank], strings=STRINGS)).pages) == 1
    assert len(read_sdocx(B.sdocx([blank, inked], strings=STRINGS)).pages) == 2
    assert len(read_sdocx(B.sdocx([blank], strings=STRINGS)).pages) == 1  # never the only page
    lined = B.page("b", 1080, 1527, [B.layer([])], template=1)
    assert len(read_sdocx(B.sdocx([inked, lined], strings=STRINGS)).pages) == 1
    hidden = B.page("b", 1080, 1527, [B.layer([(1, B.stroke([(5.0, 5.0)], pen_id=1))], hidden=True)])
    assert len(read_sdocx(B.sdocx([inked, hidden], strings=STRINGS)).pages) == 2  # it holds something
    damaged = members_of(B.sdocx([inked, blank], strings=STRINGS))
    damaged["page-1.page"] = b"\x00" * 8
    doc = read_sdocx(zip_members(damaged))  # a damaged last page is kept (and reported)
    assert len(doc.pages) == 2 and any(w.startswith("Page 2: the page is damaged") for w in doc.warnings)
    # a pageless note is one long page, kept even when empty
    assert len(read_sdocx(B.sdocx([inked, blank], strings=STRINGS, pageless=True)).pages) == 2


@pytest.mark.parametrize("template, paper", [(None, "plain"), (0, "plain"), (1, "lined"), (3, "lined"),
                                             (4, "grid"), (6, "grid"), (7, "dotted"), (9, "dotted"), (13, "plain")])
def test_paper_templates(template: Optional[int], paper: str) -> None:
    doc = read([(1, B.stroke([(5.0, 5.0)], pen_id=1))], template=template)
    assert doc.pages[0].paper == paper
    planner = "1 page(s) use a paper template gnnote does not draw (planner, to-do, ...); they get plain paper"
    assert (planner in doc.warnings) == (template == 13)


def test_layers_hidden_and_grouped_objects() -> None:
    stroke = B.stroke([(5.0, 5.0)], pen_id=1)
    page = B.page("a", 1080, 1527, [B.layer([(1, stroke)], name="Layer 1"),
                                    B.layer([(1, stroke), (1, stroke)], name="Hidden", hidden=True),
                                    B.layer([(100, stroke)], children=2)])
    doc = read_sdocx(B.sdocx([page], strings=STRINGS))
    assert len(doc.pages[0].strokes) == 1
    assert quiet(doc) == ["1 hidden layer(s) were not converted", "1 grouped object(s) were not converted"]


def test_other_objects_are_counted_by_kind() -> None:
    doc = read([(10, B.other(10)), (10, B.other(10)), (22, B.other(22)), (55, B.other(55))])
    assert doc.warnings == ["1 object of type 55(s) were not converted", "1 table(s) were not converted",
                            "2 voice recording(s) were not converted"]


def test_title_and_lock_state() -> None:
    title = B.text_object(B.text_common("My  lecture\nnotes"))
    doc = read([(1, B.stroke([(5.0, 5.0)], pen_id=1))], title=title, locked=True)
    assert doc.title == "My lecture notes"
    assert quiet(doc) == ["The note is marked as locked; locked notes may hold encrypted content gnnote cannot read"]


def test_a_zipped_note_folder_is_read() -> None:
    data = note([(1, B.stroke([(5.0, 5.0)], pen_id=1))], folder="My note/")
    assert find_note_root(zipfile.ZipFile(io.BytesIO(data)).namelist()) == ["My note/"]
    doc = read_sdocx(data)
    assert len(doc.pages) == 1 and len(doc.pages[0].strokes) == 1 and quiet(doc) == []
    two = members_of(data)
    two.update({k.replace("My note/", "Other/"): v for k, v in members_of(data).items()})
    doc = read_sdocx(zip_members(two))
    assert len(doc.pages[0].strokes) == 1
    assert quiet(doc) == ["The archive holds 2 Samsung Notes notes; only 'My note' was converted (also: 'Other')"]


def test_find_note_root_rules() -> None:
    assert find_note_root(["note.note", "end_tag.bin"]) == [""]
    assert find_note_root(["note.note", "pageIdInfo.dat"]) == [""]
    assert find_note_root(["note.note"]) == []
    assert find_note_root(["a/b/note.note", "a/b/end_tag.bin"]) == []  # two folders down
    assert find_note_root(["__MACOSX/note.note", "__MACOSX/end_tag.bin"]) == []
    assert find_note_root(["x/note.note", "y/end_tag.bin"]) == []


# --------------------------------------------------------------------------- shapes, lines, pictures


def test_shapes_and_lines_become_strokes() -> None:
    rect = (100.0, 100.0, 300.0, 200.0)
    square = B.shape(7, rect, B.rect_path(rect), line_colour=b"\x00\x00\xff\xff", line_width=9.0)
    filled = B.shape(7, rect, B.rect_path(rect), line_width=9.0, fill=B.fill_colour(b"\xff\x00\x00\x80"))
    gradient = B.shape(7, rect, B.rect_path(rect), line_width=9.0, fill=B.fill_colour(b"\xff\x00\x00\x80", True))
    invisible = B.shape(7, rect, B.rect_path(rect))  # neither a line colour nor a line style
    doc = read([(7, square), (7, filled), (7, gradient), (7, invisible),
                (8, B.line((10.0, 20.0), (110.0, 220.0), end_arrow=1))])
    strokes = doc.pages[0].strokes
    assert [s.kind for s in strokes] == ["pen", "fill", "pen", "pen", "pen"]
    outline = strokes[0]
    assert [(round(p.x / S, 6), round(p.y / S, 6)) for p in outline.points] == \
        [(100, 100), (300, 100), (300, 200), (100, 200), (100, 100)]
    assert outline.controls is not None and len(outline.controls) == 4
    assert outline.color == (1.0, 0.0, 0.0, 1.0) and outline.width == pytest.approx(9 * S)
    assert strokes[2].color == (0.0, 0.0, 0.0, 1.0)  # a line style alone draws black (as the app does)
    fill = strokes[1]
    assert fill.color == pytest.approx((0.0, 0.0, 1.0, 0x80 / 255)) and fill.outline is not None
    assert fill.outline[0][0].x == pytest.approx(100 * S)
    line = strokes[4]
    assert [(round(p.x / S, 6), round(p.y / S, 6)) for p in line.points] == [(10, 20), (110, 220)]
    assert line.color == (1.0, 0.0, 0.0, 1.0) and line.width == pytest.approx(6 * S)
    assert doc.warnings == [
        "5 shape(s) and line(s) were converted to ink strokes",
        "1 shape fill(s) were converted; fills were not checked against a note from the app",
        "1 shape fill(s) with a gradient, picture or pattern were not converted",
        "1 arrowhead(s) of lines were not converted",
    ]


def test_rotated_shapes_follow_their_stored_outline_with_a_warning() -> None:
    corners = [(200.0, 50.0), (350.0, 200.0), (200.0, 350.0), (50.0, 200.0)]
    diamond = [(1, corners[0])] + [(2, p) for p in corners[1:]] + [(6, ())]
    turned = B.shape(7, (100.0, 100.0, 300.0, 300.0), diamond, angle=45.0, line_width=4.0)
    doc = read([(7, turned)])
    (stroke,) = doc.pages[0].strokes
    assert [(round(p.x / S, 6), round(p.y / S, 6)) for p in stroke.points] == corners + corners[:1]
    assert "1 rotated shape(s) were drawn along their stored outline; rotated shapes were not checked against a " \
           "note from the app" in doc.warnings
    assert not any("rotated" in w for w in read([(7, B.shape(7, (100.0, 100.0, 300.0, 300.0), diamond,
                                                             line_width=4.0))]).warnings)


def test_fill_outlines_are_flattened_within_the_point_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    # 2,500 straight edges zig-zagging 20,000 units: one vertex per edge (each used to become
    # 4,096 points, 10 million for this 6 KB note)
    n = 2_500
    zigzag = [(1, (0.0, 0.0))] + [(2, (20000.0 if i % 2 == 0 else 0.0, float(i))) for i in range(n)] + [(6, ())]
    shape = B.shape(7, (0, 0, 20000, n), zigzag, fill=B.fill_colour(b"\x00\xff\x00\xff"))
    (fill,) = read([(7, shape)]).pages[0].strokes
    assert fill.kind == "fill" and len(fill.points) == n + 2
    assert (fill.points[1].x, fill.points[1].y) == pytest.approx((20000 * S, 0.0))
    # long curves get a coarser spacing: about MAX_FILL_SAMPLES vertices, plus one per segment
    curves = [(1, (0.0, 0.0))] + [(4, (i * 100.0, 30000.0, i * 100.0 + 50, -30000.0, i * 100.0 + 100, 0.0))
                                  for i in range(200)] + [(6, ())]
    shape = B.shape(7, (0, 0, 20000, 100), curves, fill=B.fill_colour(b"\x00\xff\x00\xff"))
    (fill,) = read([(7, shape)]).pages[0].strokes
    assert 1_000 < len(fill.points) <= RD.MAX_FILL_SAMPLES + 2 * 201 + 2
    # small curves keep about 2 pt spacing (steps are even in the curve parameter, not in length)
    oval = [(7, (0.0, 0.0, 216.0, 108.0))]
    (fill,) = read([(7, B.shape(7, (0, 0, 216, 108), oval, fill=B.fill_colour(b"\x00\xff\x00\xff")))]).pages[0].strokes
    gaps = [math.dist((a.x, a.y), (b.x, b.y)) for a, b in zip(fill.points, fill.points[1:])]
    assert max(gaps) <= 1.5 * RD.FILL_SPACING and len(fill.points) > 100
    # the fill is charged to the point budget like any ink
    monkeypatch.setattr(RD, "PointBudget", lambda: readutil.PointBudget(1_000))
    doc = read([(7, B.shape(7, (0, 0, 20000, n), zigzag, fill=B.fill_colour(b"\x00\xff\x00\xff")))])
    assert doc.pages[0].strokes == [] and BUDGET_NOTE in doc.warnings


def test_bent_connector_lines_are_drawn_straight_with_a_warning() -> None:
    bent = B.line((100.0, 100.0), (500.0, 100.0), controls=[(300.0, 300.0)], connector=2)
    doc = read([(8, bent), (8, B.line((10.0, 20.0), (110.0, 220.0)))])
    first = doc.pages[0].strokes[0]
    assert [(round(p.x / S, 6), round(p.y / S, 6)) for p in first.points] == [(100, 100), (500, 100)]
    assert doc.warnings == ["2 shape(s) and line(s) were converted to ink strokes",
                            "1 bent or curved connector line(s) were drawn straight from their start to their end"]


def test_curved_paths_arcs_and_ovals() -> None:
    rect = (0.0, 0.0, 200.0, 100.0)
    segments = [(1, (0.0, 50.0)), (3, (100.0, -50.0, 200.0, 50.0)), (4, (150.0, 100.0, 50.0, 100.0, 0.0, 50.0)),
                (6, ())]
    curve = B.shape(7, rect, segments, line_width=4.0)
    oval = B.shape(7, rect, [(7, rect)], line_width=4.0)
    arc = B.shape(7, rect, [(1, (0.0, 0.0)), (5, (0.0, 0.0, 200.0, 100.0, 0.0, 90.0))], line_width=4.0)
    doc = read([(7, curve), (7, oval), (7, arc)])
    curve_st, oval_st, arc_st = doc.pages[0].strokes
    assert len(curve_st.points) == 3 and curve_st.controls is not None
    # the quadratic segment became the cubic with handles at 2/3 towards its control point
    (c1, c2) = curve_st.controls[0]
    assert (c1.x / S, c1.y / S) == (pytest.approx(200 / 3), pytest.approx(50 - 200 / 3))
    assert (curve_st.points[-1].x, curve_st.points[-1].y) == (0.0, pytest.approx(50 * S))
    xs = [p.x / S for p in stroke_polyline(oval_st)]
    ys = [p.y / S for p in stroke_polyline(oval_st)]
    assert min(xs) == pytest.approx(0, abs=0.5) and max(xs) == pytest.approx(200, abs=0.5)
    assert min(ys) == pytest.approx(0, abs=0.5) and max(ys) == pytest.approx(100, abs=0.5)
    # the arc: a line from (0, 0) to its start (200, 50), then a quarter ellipse to (100, 100)
    assert (arc_st.points[1].x / S, arc_st.points[1].y / S) == (pytest.approx(200), pytest.approx(50))
    assert (arc_st.points[-1].x / S, arc_st.points[-1].y / S) == (pytest.approx(100), pytest.approx(100))
    assert "2 shape(s) with arcs or ovals were drawn by gnnote's own construction, which was not checked " \
           "against a note from the app" in doc.warnings


def test_pictures_crops_rotation_and_missing_media() -> None:
    png = B.tiny_png(40, 20)
    rect = (108.0, 216.0, 216.0, 270.0)
    plain = B.shape(3, rect, [], fill=B.fill_image(5), uuid="p1")
    turned = B.shape(3, rect, [], fill=B.fill_image(5), angle=90.0, uuid="p2")
    cropped = B.shape(3, rect, [], fill=B.fill_image(5), crop=(10, 0, 30, 20), original=(54.0, 216.0, 270.0, 270.0))
    flipped = B.shape(3, rect, [], fill=B.fill_image(5), flip=1)
    missing = B.shape(3, rect, [], fill=B.fill_image(9))
    spi = B.shape(3, rect, [], fill=B.fill_image(6))
    doc = read([(3, plain), (3, turned), (3, cropped), (3, flipped), (3, missing), (3, spi)],
               media={5: ("0@paste_1.png", png), 6: ("0@page_1.spi", b"SPI\0data")})
    images = doc.pages[0].images
    assert len(images) == 4
    first = images[0]
    assert (first.x, first.y, first.w, first.h) == pytest.approx((60.0, 120.0, 60.0, 30.0))
    assert (first.fmt, first.data, first.rotation) == ("png", png, 0.0)
    assert images[1].rotation == 90.0
    assert readutil.image_pixel_size(images[2].data) == (20, 20)  # the crop was applied to the pixels
    assert doc.warnings == ["1 flipped picture(s) are shown unflipped",
                            "1 picture(s) in Samsung's SPI picture format were not converted",
                            "1 picture(s) or PDF(s) missing from the note's media folder were skipped"]


def test_crop_png_helper() -> None:
    png = B.tiny_png(8, 4)
    out = crop_png(png, (2, 1, 6, 3))
    assert out is not None and readutil.image_pixel_size(out) == (4, 2)
    assert crop_png(png, (6, 1, 2, 3)) is None
    assert crop_png(b"\x89PNG\r\n\x1a\nbroken", (0, 0, 1, 1)) is None


def _png(width: int, height: int, colour_type: int, depth: int, rows: bytes, extra: Sequence[Tuple[bytes, bytes]] = (),
         plte: bytes = b"") -> bytes:
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, colour_type, 0, 0, 0))
    out += b"".join(chunk(tag, body) for tag, body in extra if tag != b"tRNS")
    if plte:
        out += chunk(b"PLTE", plte)
    out += b"".join(chunk(tag, body) for tag, body in extra if tag == b"tRNS")
    return out + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def _chunks(png: bytes) -> List[Tuple[bytes, bytes]]:
    out, pos = [], 8
    while pos < len(png):
        n = int.from_bytes(png[pos:pos + 4], "big")
        out.append((png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]))
        pos += 12 + n
    return out


def test_crops_keep_transparency_and_colour_chunks() -> None:
    colour = [(b"gAMA", struct.pack(">I", 45455)), (b"sRGB", b"\x00"), (b"pHYs", struct.pack(">IIB", 2835, 2835, 1))]
    key = struct.pack(">HHH", 200, 30, 30)
    rgb = _png(8, 4, 2, 8, b"".join(b"\x00" + bytes([200, 30, 30]) * 8 for _ in range(4)), colour + [(b"tRNS", key)])
    out = crop_png(rgb, (2, 1, 6, 3))
    assert out is not None
    assert [t for t, _ in _chunks(out)] == [b"IHDR", b"gAMA", b"sRGB", b"pHYs", b"tRNS", b"IDAT", b"IEND"]
    assert dict(_chunks(out))[b"tRNS"] == key
    # 4-bit grey is decoded to 8 bits, and its colour key with it
    grey = _png(4, 2, 0, 4, b"\x00\xf0\x0f" * 2, [(b"tRNS", struct.pack(">H", 15))])
    out = crop_png(grey, (0, 0, 2, 2))
    assert out is not None and dict(_chunks(out))[b"tRNS"] == struct.pack(">H", 255)
    # a palette's transparency becomes the alpha of the decoded pixels: no tRNS is copied
    palette = _png(2, 1, 3, 8, b"\x00\x00\x01", [(b"tRNS", b"\x00\xff")], plte=b"\xff\x00\x00\x00\x00\xff")
    out = crop_png(palette, (0, 0, 2, 1))
    assert out is not None and [t for t, _ in _chunks(out)] == [b"IHDR", b"IDAT", b"IEND"]
    assert _chunks(out)[0][1][9] == 6  # RGBA


def test_many_cropped_references_to_one_picture_decode_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    rng = random.Random(7)
    rows = b"".join(b"\x04" + bytes(rng.randrange(256) for _ in range(3 * 600)) for _ in range(600))
    png = _png(600, 600, 2, 8, rows)
    decodes: List[int] = []
    real = RD._decode_png
    monkeypatch.setattr(RD, "_decode_png", lambda data: decodes.append(1) or real(data))
    same = [(3, B.shape(3, (10, 10, 110, 110), [], fill=B.fill_image(1), crop=(0, 0, 10, 10),
                        original=(0, 0, 200, 200), uuid=f"same{i}")) for i in range(50)]
    moved = [(3, B.shape(3, (10, 10, 110, 110), [], fill=B.fill_image(1), crop=(i, 0, i + 10, 10),
                         original=(0, 0, 200, 200), uuid=f"moved{i}")) for i in range(50)]
    start = time.monotonic()
    doc = read(same + moved, media={1: ("0@x.png", png)})
    assert time.monotonic() - start < 30
    images = doc.pages[0].images
    assert len(images) == 100 and len(decodes) == 1
    assert all(readutil.image_pixel_size(im.data) == (10, 10) for im in images)
    assert len({id(im.data) for im in images[:50]}) == 1  # one crop, shared
    assert quiet(doc) == []


def test_crop_work_is_capped_per_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    monkeypatch.setattr(RD, "MAX_CROP_WORK", 40 * 20 + 2 * 100)  # one decode and two 10 x 10 crops
    pictures = [(3, B.shape(3, (108.0, 216.0, 216.0, 270.0), [], fill=B.fill_image(5), crop=(i, 0, i + 10, 10),
                            original=(54.0, 216.0, 270.0, 270.0), uuid=f"p{i}")) for i in range(3)]
    doc = read(pictures, media={5: ("0@paste_1.png", B.tiny_png(40, 20))})
    sizes = [readutil.image_pixel_size(im.data) for im in doc.pages[0].images]
    assert sizes == [(10, 10), (10, 10), (40, 20)]
    assert doc.warnings == ["1 picture crop(s) were not applied (JPEG or very large pictures); the whole picture is "
                            "shown at its uncropped size"]


def test_pictures_referenced_too_often_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    monkeypatch.setattr(RD, "MAX_SHARED_IMAGE_BYTES", 0)  # pictures may then reference the file's own size
    rng = random.Random(3)
    png = _png(40, 40, 2, 8, b"".join(b"\x00" + bytes(rng.randrange(256) for _ in range(120)) for _ in range(40)))
    pictures = [(3, B.shape(3, (10.0, 10.0 + i, 110.0, 110.0 + i), [], fill=B.fill_image(1), uuid=f"p{i}"))
                for i in range(20)]
    data = note(pictures, media={1: ("0@x.png", png)})
    doc = read_sdocx(data)
    kept = len(data) // len(png)
    assert 1 <= kept < 20 and len(doc.pages[0].images) == kept
    assert doc.warnings == [f"{20 - kept} picture(s) were skipped: they reference the same pictures too often"]


def test_picture_borders_are_reported() -> None:
    png = B.tiny_png()
    rect = (108.0, 216.0, 216.0, 270.0)
    kinds = [dict(border=1, border_width=3.0), dict(border=0, border_width=3.0), dict(border_picture=4),
             dict(border_width=2.0), dict()]
    pictures = [(3, B.shape(3, rect, [], fill=B.fill_image(5), uuid=f"p{i}", **kw)) for i, kw in enumerate(kinds)]
    doc = read(pictures, media={5: ("0@paste_1.png", png)})
    assert len(doc.pages[0].images) == 5
    assert doc.warnings == ["3 picture frame(s) or border(s) were not drawn"]


def test_jpeg_crop_falls_back_to_the_whole_picture() -> None:
    jpeg = b"\xff\xd8\xff\xe0" + bytes(16)
    shape = B.shape(3, (108.0, 216.0, 216.0, 270.0), [], fill=B.fill_image(5), crop=(10, 0, 30, 20),
                    original=(54.0, 216.0, 270.0, 270.0))
    doc = read([(3, shape)], media={5: ("0@paste_1.jpg", jpeg)})
    (image,) = doc.pages[0].images
    assert image.fmt == "jpeg" and (image.x, image.w) == pytest.approx((30.0, 120.0))
    assert doc.warnings == ["1 picture crop(s) were not applied (JPEG or very large pictures); the whole picture is "
                            "shown at its uncropped size"]


def test_imported_pdf_page_becomes_the_background() -> None:
    pdf = pdfutil.make_paper_pdf(600.0, 848.0)
    full = [(4, 0, (0, 0, 1080, 1527))]
    doc = read([(1, B.stroke([(5.0, 5.0)], pen_id=1))], pdfs=full, media={4: ("0@pdf_a.pdf", pdf)})
    page = doc.pages[0]
    assert page.background is not None and page.background.page_index == 0
    assert doc.pdfs[page.background.pdf_id] == pdf
    assert quiet(doc) == ["1 imported PDF page(s) became page backgrounds; this was not checked against a note "
                          "from the app"]
    small = [(4, 0, (100, 100, 500, 500))]
    doc = read([(1, B.stroke([(5.0, 5.0)], pen_id=1))], pdfs=small, media={4: ("0@pdf_a.pdf", pdf)})
    assert doc.pages[0].background is None and doc.pdfs == {}
    assert "1 imported PDF page(s) that do not fill their page were not converted" in doc.warnings
    doc = read([], pdfs=full, media={4: ("0@pdf_a.pdf", b"not a pdf")})
    assert doc.pages[0].background is None
    assert "1 picture(s) or PDF(s) missing from the note's media folder were skipped" in doc.warnings
    # format versions before 2034 store the placement as doubles
    old = B.sdocx([B.page("a", 1080, 1527, [B.layer([])], pdfs=full, fmt=2000)], media={4: ("0@pdf_a.pdf", pdf)})
    assert read_sdocx(old).pages[0].background is not None


# --------------------------------------------------------------------------- typed text


def test_text_box_objects_become_text_boxes() -> None:
    text = B.text_common("Hello bold\nworld", spans=[B.size_span(0, 16, 18.0), B.flag_span(5, 6, 10)])
    box = B.shape(2, (108.0, 216.0, 540.0, 432.0), B.rect_path((108.0, 216.0, 540.0, 432.0)), text=text)
    doc = read([(2, box)])
    (tb,) = doc.pages[0].texts
    dp = 600.0 / 360.0
    assert (tb.x, tb.y) == pytest.approx((60.0 + 16 * dp, 120.0 + 10 * dp))
    assert (tb.w, tb.h) == pytest.approx((240.0 - 32 * dp, 120.0 - 20 * dp))
    assert tb.text == "Hello bold\nworld" and tb.size == pytest.approx(18 * dp)
    assert [(r.text, r.bold) for r in tb.runs] == [("Hello ", False), ("bold", True), ("\nworld", False)]
    assert doc.warnings == ["1 text box(es) were converted; their layout was not checked against a note from the app"]


def test_body_text_is_laid_out_one_box_per_paragraph() -> None:
    para = [(4, i, i + 1, struct.pack("<If", 1, 1.6)) for i in range(4)]
    body = B.text_common("First line\n\nSecond paragraph\nThird", spans=[B.size_span(0, 34, 15.0),
                                                                        B.flag_span(6, 12, 18)],
                         paragraphs=para)
    doc = read([], body=B.text_object(body))
    texts = doc.pages[0].texts
    assert [t.text for t in texts] == ["First line", "Second paragraph", "Third"]
    dp = 600.0 / 360.0
    size = 15 * dp
    assert [t.size for t in texts] == pytest.approx([size] * 3) and [t.x for t in texts] == pytest.approx([16 * dp] * 3)
    tops = [t.y for t in texts]
    pitch = 1.6 * size  # the paragraphs' line spacing; the empty line takes one pitch too
    assert tops[1] - tops[0] == pytest.approx(2 * pitch) and tops[2] - tops[1] == pytest.approx(pitch)
    assert [(r.text, r.italic) for r in texts[1].runs] == [("Second", True), (" paragraph", False)]
    assert doc.warnings == ["The typed text and the pictures in it were laid out by gnnote (the app stores no line "
                            "positions); line breaks and positions are approximate"]


def test_body_paragraph_indent_and_alignment() -> None:
    paragraphs = [(2, 1, 2, struct.pack("<II", 1, 1)), (2, 2, 3, struct.pack("<II", 2, 1)),
                  (3, 2, 3, struct.pack("<II", 1, 0))]
    doc = read([], body=B.text_object(B.text_common("Plain\nQuoted\nDeeper", paragraphs=paragraphs)))
    dp = 600.0 / 360.0
    assert [(t.text, round(t.x / dp, 6), round(t.w / dp, 6)) for t in doc.pages[0].texts] == \
        [("Plain", 16.0, 328.0), ("Quoted", 32.0, 312.0), ("Deeper", 48.0, 296.0)]
    assert "Text formatting without an equivalent in gnnote was dropped: paragraph alignment (shown left-aligned)" \
        in doc.warnings


def test_long_body_text_wraps_and_flows_onto_new_pages() -> None:
    words = " ".join(f"word{i}" for i in range(3000))
    doc = read([], body=B.text_object(B.text_common(words + "\nEnd")))
    assert len(doc.pages) > 2
    assert all(t.y + t.h <= p.height + 1e-6 for p in doc.pages for t in p.texts)
    assert doc.pages[-1].texts[-1].text == "End"
    assert "".join(t.text for p in doc.pages for t in p.texts).replace("End", "") == words
    added = len(doc.pages) - 2  # the note has two page records (the second one empty)
    assert f"The typed text needed {added} more page(s) than the note has; they were added" in doc.warnings


def test_body_text_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    monkeypatch.setattr(RD, "MAX_PAGES", 1)
    monkeypatch.setattr(RD, "MAX_EXTRA_PAGES", 1)
    doc = read([], body=B.text_object(B.text_common("\n".join(f"line {i}" for i in range(100)))))
    assert len(doc.pages) == 2
    assert "The typed text needed 1 more page(s) than the note has; they were added" in doc.warnings
    assert "The typed text is longer than gnnote reads (it would need more than 2 pages); 52 paragraph(s) of it " \
        "were not converted" in doc.warnings


def test_pictures_in_the_body_text_are_blocks() -> None:
    png = B.tiny_png(30, 10)
    picture = B.shape(3, (48.0, 0.0, 588.0, 180.0), [], fill=B.fill_image(3), page_index=0)
    body = B.text_common("Above\n￼\nBelow", inline=[(6, 3, picture, 0, 0)])
    doc = read([], body=B.text_object(body), media={3: ("0@paste_2.png", png)})
    page = doc.pages[0]
    (image,) = page.images
    above, below = page.texts
    assert above.text == "Above" and below.text == "Below"
    assert above.y < image.y < image.y + image.h < below.y
    assert (image.x, image.w, image.h) == pytest.approx((48 * S, 540 * S, 180 * S))


def test_list_items_have_no_paragraph_spacing_between_them() -> None:
    """The app leaves out the space before and after between two list items (sample 01: an
    unordered list followed by an ordered one)."""
    bullet, ordered = struct.pack("<IIII", 8, 0, 0, 1), struct.pack("<IIII", 4, 1, 0, 1)
    ten = struct.pack("<f", 10.0) + bytes(4)
    paragraphs = [(4, 0, 4, struct.pack("<If", 1, 1.6)), (5, 1, 2, bullet), (5, 2, 3, ordered),
                  (8, 1, 3, ten), (9, 1, 3, ten), (5, 3, 4, struct.pack("<IIII", 0, 0, 0, 0))]
    doc = read([], body=B.text_object(B.text_common("Intro\nItem one\nItem two\nAfter", paragraphs=paragraphs)))
    tops = [t.y for t in doc.pages[0].texts]
    dp = 600.0 / 360.0
    pitch = 1.6 * 15 * dp
    assert [b - a for a, b in zip(tops, tops[1:])] == pytest.approx([pitch + 10 * dp, pitch, pitch + 10 * dp])


def test_tables_and_code_blocks_keep_their_stored_position_near_the_flow() -> None:
    """A code block the app stored a little lower than gnnote's flow (sample 01: 25.7 pt, the
    margins of the table above it) stays where the app put it; the text after it follows."""
    dp = 600.0 / 360.0
    flow = (10 * dp + 15 * dp * 1.327) / S  # the top margin and one line, in canvas units

    def after(stored: float) -> float:
        block = B.other(23, (0.0, stored, 100.0, stored + 90.0), page_index=0)
        body = B.text_common("Text\n\ufffc\nAfter", inline=[(5, 23, block, 0, 1)])
        doc = read([], body=B.text_object(body))
        assert "1 code block(s) in the typed text were not converted" in doc.warnings
        return doc.pages[0].texts[1].y

    base = after(flow - 20.0)  # above the flow: the text above would overlap it, so ignored
    assert after(flow + 54.0) == pytest.approx(base + 54.0 * S)
    assert after(flow + 300.0) == pytest.approx(base)  # 167 pt below: too far to trust


def test_many_objects_in_one_paragraph_cost_linear_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    calls: List[int] = []
    real = RD._BodyLayout.size_of
    monkeypatch.setattr(RD._BodyLayout, "size_of", lambda self, a, b: calls.append(b - a) or real(self, a, b))
    n = 20_000
    table = B.other(22, (0.0, 0.0, 100.0, 10.0))
    body = B.text_common("\ufffc" * n + "\nEnd", inline=[(i, 22, table, 0, 0) for i in range(n)])
    start = time.monotonic()
    doc = read([], body=B.text_object(body))
    assert time.monotonic() - start < 20
    assert len(calls) <= 3  # once per paragraph (it used to be once per object: quadratic)
    assert f"{n} table(s) in the typed text were not converted" in doc.warnings


def test_blocks_running_over_tiny_pages_skip_them_in_one_step() -> None:
    """Pages after the note's last one are skipped arithmetically, so a code block's run over
    them costs the same on any page height (a 0.01 pt page used to cost 30,000 steps)."""
    from gnnote.samsungnotes import reader as RD

    n = 1_000
    block = B.other(23, (0.0, 0.0, 100.0, 1000.0))
    text = "\n".join("\ufffc" for _ in range(n))
    raw = B.text_object(B.text_common(text, spans=[B.size_span(0, len(text), 500.0)],
                                      inline=[(2 * i, 23, block, 0, 1) for i in range(n)]))
    shape = R.parse_object(R.Cursor(raw), 2, {}, R.Limits())
    assert isinstance(shape, R.Shape) and shape.text is not None

    class Counting(RD._BodyLayout):
        calls = 0

        def _page_height(self, page: int) -> float:
            Counting.calls += 1
            return super()._page_height(page)

    start = time.monotonic()
    layout = Counting(shape.text, [800.0, 0.01], S)
    placed = layout.layout()
    assert time.monotonic() - start < 10
    assert len(placed) == n and Counting.calls < 20 * n
    assert layout.page > 1_000_000  # pages of 0.01 pt: the run really skips that many


def test_text_formatting_losses_are_listed() -> None:
    body = B.text_common("Struck link marked", spans=[(20, 0, 6, struct.pack("<I", 1)),
                                                      (9, 7, 11, struct.pack("<III", 3, 0, 0)),
                                                      (17, 12, 18, b"\x00\xff\xff\xff")],
                         paragraphs=[(5, 0, 1, struct.pack("<IIII", 4, 1, 0, 1))])
    doc = read([], body=B.text_object(body))
    assert "Text formatting without an equivalent in gnnote was dropped: links (the text is kept), list bullets " \
           "and numbers, strikethrough, text highlight colours" in doc.warnings


def test_heavy_formatting_costs_linear_time() -> None:
    n = 200_000
    text = "x" * n
    spans = [B.flag_span(5, i, i + 1, i % 2 == 0) for i in range(0, 4000, 1)]
    body = B.text_common(text, spans=spans)
    start = time.monotonic()
    doc = read([], body=B.text_object(body))
    assert time.monotonic() - start < 20
    assert sum(len(t.text) for p in doc.pages for t in p.texts) == n


def test_objects_in_text_boxes_and_the_title_are_reported() -> None:
    """Only the body text places the objects anchored in it; those of text boxes, shapes and
    the title are counted, and damaged ones too."""
    png = B.tiny_png(40, 30)
    pic = B.shape(3, (100.0, 100.0, 500.0, 400.0), B.rect_path((100.0, 100.0, 500.0, 400.0)), fill=B.fill_image(7),
                  uuid="pic", fmt=5400)
    ink = B.stroke([(10.0, 10.0), (60.0, 60.0)], pen_id=1, uuid="ink")
    caption = B.text_common("Caption \ufffc and \ufffc here", fmt=5400,
                            inline=[(8, 3, pic, 0, 0), (14, 1, ink, 0, 0), (15, 3, b"\x00" * 5, 0, 0)])
    box = B.shape(2, (50.0, 50.0, 900.0, 700.0), [], text=caption, uuid="tb", fmt=5400)
    blank = B.shape(7, (50.0, 800.0, 400.0, 900.0), B.rect_path((50.0, 800.0, 400.0, 900.0)), uuid="sh", fmt=5400,
                    text=B.text_common("\ufffc", inline=[(0, 3, pic, 0, 0)], fmt=5400))
    title = B.text_object(B.text_common("Title \ufffc", inline=[(6, 3, pic, 0, 0)], fmt=5400), fmt=5400)
    doc = read([(2, box), (7, blank)], title=title, media={7: ("0@paste_a.png", png)})
    assert doc.title == "Title"
    assert [t.text for t in doc.pages[0].texts] == ["Caption  and  here"] and not doc.pages[0].images
    for message in ("2 picture(s) in text boxes or shapes were not converted",
                    "1 stroke(s) in text boxes or shapes were not converted",
                    "1 picture(s) in the note's title were not converted", "1 damaged object(s) were skipped"):
        assert message in doc.warnings


def test_pageless_notes_grow_their_one_page_and_are_reported() -> None:
    lines = [f"Line {i}" for i in range(120)]
    body = B.text_object(B.text_common("\n".join(lines)))
    ink = B.stroke([(10.0, 10.0), (60.0, 60.0)], pen_id=1)
    data = B.sdocx([B.page("p0", 1080, 1527, [B.layer([(1, ink)])])], body=body, pageless=True, strings=STRINGS)
    doc = read_sdocx(data)
    (page,) = doc.pages
    assert [t.text for t in page.texts] == lines
    assert page.height > 1527 * S and page.texts[-1].y + page.texts[-1].h <= page.height
    assert "The note is a pageless note (one endless page); gnnote keeps it as one long page, which was not " \
           "checked against a note from the app" in doc.warnings
    assert not any("more page(s)" in w for w in doc.warnings)
    # text that fits leaves the page as it is
    short = B.sdocx([B.page("p0", 1080, 1527, [B.layer([(1, ink)])])], pageless=True, strings=STRINGS,
                    body=B.text_object(B.text_common("One line")))
    assert read_sdocx(short).pages[0].height == pytest.approx(1527 * S)


def test_a_pageless_page_grows_only_up_to_the_page_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    monkeypatch.setattr(RD, "MAX_PAGE_RATIO", 2.0)
    body = B.text_object(B.text_common("\n".join(f"Line {i}" for i in range(100))))
    doc = read_sdocx(B.sdocx([B.page("p0", 1080, 1527, [B.layer([])])], body=body, pageless=True))
    (page,) = doc.pages
    assert page.height <= 1200.0 and all(t.y + t.h <= page.height for t in page.texts)
    dropped = 100 - len(page.texts)
    assert dropped > 0 and "The typed text is longer than gnnote reads (it would make the endless page longer than " \
        f"gnnote reads); {dropped} paragraph(s) of it were not converted" in doc.warnings


def test_landscape_pages_are_reported() -> None:
    ink = [B.layer([(1, B.stroke([(10.0, 10.0)], pen_id=1))])]
    message = "1 landscape page(s) were made 600 pt wide like the others; how the app scales landscape pages was " \
              "not checked against a note from the app"
    doc = read_sdocx(B.sdocx([B.page("p0", 1527, 1080, ink)], width=1527, height=1080, strings=STRINGS))
    assert (doc.pages[0].width, doc.pages[0].height) == pytest.approx((600.0, 1080 * 600.0 / 1527))
    assert message in doc.warnings
    doc = read_sdocx(B.sdocx([B.page("p0", 1080, 1527, ink, orientation=1)], strings=STRINGS))  # the field alone
    assert message in doc.warnings
    doc = read_sdocx(B.sdocx([B.page("p0", 1080, 1527, ink)], strings=STRINGS, landscape=True))  # the note's flag
    assert "The note is marked as a landscape note; this was not checked against a note from the app" in doc.warnings
    assert not any("landscape" in w for w in read_sdocx(B.sdocx([B.page("p0", 1080, 1527, ink)],
                                                                strings=STRINGS)).warnings)


# --------------------------------------------------------------------------- hostile input


def test_not_a_note() -> None:
    with pytest.raises(ValueError, match="not a Samsung Notes file: not a readable ZIP archive"):
        read_sdocx(b"definitely not a zip")
    with pytest.raises(ValueError, match="no note.note with pageIdInfo.dat or end_tag.bin"):
        read_sdocx(zip_members({"note.note": b"x"}))
    with pytest.raises(TypeError):
        read_sdocx("text")  # type: ignore[arg-type]


def test_damaged_members_degrade_with_warnings() -> None:
    members = members_of(note([(1, B.stroke([(5.0, 5.0)], pen_id=1))]))
    members["note.note"] = b"\x00\x01"
    members["end_tag.bin"] = b"\x05\x00abc"
    members["media/mediaInfo.dat"] = b"garbage!"
    doc = read_sdocx(zip_members(members))
    assert len(doc.pages[0].strokes) == 1 and doc.pages[0].strokes[0].pen is None  # the pen name is lost
    assert doc.warnings[:3] == [
        "The note header (note.note) is damaged (record truncated: 4 bytes needed at offset 0, 2 left); pen names "
        "and typed text are lost",
        "The note's end tag (end_tag.bin) is damaged; the note is read as a paged note",
        "The media list (media/mediaInfo.dat) is damaged; pictures and imported PDFs are lost",
    ]
    # a note.note whose data no longer matches its CRC cannot be read
    members = members_of(note([(1, B.stroke([(5.0, 5.0)], pen_id=1))]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, raw in members.items():
            zf.writestr(name, raw)
    damaged = bytearray(buf.getvalue())
    at = bytes(damaged).index(members["note.note"][:16]) + 8
    damaged[at] ^= 0xFF
    doc = read_sdocx(bytes(damaged))
    assert len(doc.pages[0].strokes) == 1
    assert doc.warnings[0] == "The note header (note.note) is missing or unreadable; pen names and typed text are lost"
    assert doc.warnings[-1] == "Samsung Notes: 1 damaged or unsupported part(s) could not be read (note.note)"


def test_damaged_pages_keep_what_could_be_read() -> None:
    good = B.stroke([(5.0, 5.0)], pen_id=1)
    data = B.page("a", 1080, 1527, [B.layer([(1, good)]), B.layer([(1, good)])])
    cut = data[:len(data) - 40 - len(good)]  # the second layer is cut short
    members = members_of(note())
    members["page-0.page"] = cut
    doc = read_sdocx(zip_members(members))
    assert len(doc.pages[0].strokes) == 1
    assert "Page 1: its content is damaged; what could be read was kept" in doc.warnings
    members["page-0.page"] = b"\xff" * 12
    doc = read_sdocx(zip_members(members))
    assert len(doc.pages) == 1 and not doc.pages[0].strokes
    assert any(w.startswith("Page 1: the page is damaged (") for w in doc.warnings)


def test_damaged_objects_are_skipped_one_by_one() -> None:
    good = B.stroke([(5.0, 5.0)], pen_id=1)
    broken = good[:-6]  # the stroke frame runs past the end of the object
    doc = read([(1, good), (1, broken), (7, b"\x00" * 40), (1, good)])
    assert len(doc.pages[0].strokes) == 2
    assert "2 damaged object(s) were skipped" in doc.warnings


def test_huge_counts_fail_fast() -> None:
    layer = B.layer([]).replace(struct.pack("<I", 0) + bytes(32), struct.pack("<I", 0xFFFFFFFF) + bytes(32))
    members = members_of(note())
    members["page-0.page"] = B.page("a", 1080, 1527, [layer])
    start = time.monotonic()
    doc = read_sdocx(zip_members(members))
    assert time.monotonic() - start < 5
    assert "Page 1: its content is damaged; what could be read was kept" in doc.warnings
    events = struct.pack("<H", 0xFFFF)  # a stroke claiming 65535 events with none stored
    payload = B.object_base("s", (0, 0, 1, 1)) + B.frame(1, 0, 0, events + struct.pack("<H", 2))
    assert "1 damaged object(s) were skipped" in read([(1, payload)]).warnings


def test_deeply_nested_inline_objects_are_refused() -> None:
    inner = B.shape(2, (0, 0, 10, 10), [], text=B.text_common("leaf"))
    for _ in range(R.MAX_DEPTH + 3):
        inner = B.shape(2, (0, 0, 10, 10), [], text=B.text_common("￼", inline=[(0, 2, inner, 0, 0)]))
    obj = R.parse_object(R.Cursor(inner), 2, {}, R.Limits())
    depth = 0
    while obj.text.inline:  # type: ignore[union-attr]
        obj = obj.text.inline[0].obj  # type: ignore[union-attr]
        depth += 1
    assert depth == R.MAX_DEPTH


def test_object_limit_stops_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "MAX_OBJECTS", 3)
    stroke = B.stroke([(5.0, 5.0)], pen_id=1)
    pages = [B.page(u, 1080, 1527, [B.layer([(1, stroke)] * 2)]) for u in ("a", "b", "c")]
    doc = read_sdocx(B.sdocx(pages, strings=STRINGS))
    assert sum(len(p.strokes) for p in doc.pages) == 2
    assert "The note holds more objects than gnnote reads; the rest were skipped" in doc.warnings


def test_oversized_members_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readutil, "MAX_MEMBER_BYTES", 1000)
    big = B.page("page-0", 1080, 1527, [B.layer([(1, B.stroke([(float(i), 1.0) for i in range(100)], pen_id=1))])])
    members = members_of(note())
    members["page-0.page"] = big
    doc = read_sdocx(zip_members(members))
    assert "Page 1: the page could not be read and was left empty" in doc.warnings
    assert any(w.startswith("Samsung Notes: 1 part(s) inflate above the") and "page-0.page" in w
               for w in doc.warnings)


def test_text_attributes_keep_only_used_kinds_and_non_empty_ranges() -> None:
    font = struct.pack("<H", 70) + "F".encode("utf-16-le") * 70
    spans = [B.flag_span(5, 0, 3), B.flag_span(5, 2, 2), (2, 0, 3, b"\x01" * 40), (4, 0, 3, font)]
    paragraphs = [(3, 0, 1, struct.pack("<I", 1) + bytes(20)), (7, 0, 1, bytes(8)), (5, 1, 1, bytes(16))]
    raw = B.text_object(B.text_common("abc", spans=spans, paragraphs=paragraphs))
    shape = R.parse_object(R.Cursor(raw), 2, {}, R.Limits())
    assert isinstance(shape, R.Shape) and shape.text is not None
    body = shape.text
    assert [(a.kind, a.start, a.end, len(a.data)) for a in body.spans] == [(5, 0, 3, 8), (4, 0, 3, 2 + 2 * 64)]
    assert [(a.kind, a.start, a.end, len(a.data)) for a in body.paragraphs] == [(3, 0, 1, 8)]
    assert body.attributes_dropped == 0
    # the font name keeps its first 64 units
    box = B.shape(2, (108.0, 216.0, 540.0, 432.0), [], text=B.text_common("abc", spans=[(4, 0, 3, font)]))
    (tb,) = read([(2, box)]).pages[0].texts
    assert [r.font for r in tb.runs] == ["F" * 64]


def test_text_attribute_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "MAX_TEXT_ATTRIBUTES", 5)
    monkeypatch.setattr(R, "MAX_DOC_ATTRIBUTES", 8)
    spans = [B.flag_span(5, i, i + 1) for i in range(4)]
    paragraphs = [(8, 0, 1, struct.pack("<f", 5.0) + bytes(4))] * 3
    text = B.text_common("abcd", spans=spans, paragraphs=paragraphs)
    raw = B.text_object(text)
    limits = R.Limits()
    first = R.parse_object(R.Cursor(raw), 2, {}, limits)
    second = R.parse_object(R.Cursor(raw), 2, {}, limits)
    assert isinstance(first, R.Shape) and first.text is not None
    assert isinstance(second, R.Shape) and second.text is not None
    assert (len(first.text.spans), len(first.text.paragraphs), first.text.attributes_dropped) == (4, 1, 2)
    assert (len(second.text.spans), len(second.text.paragraphs), second.text.attributes_dropped) == (3, 0, 4)
    assert limits.attributes == 0
    box = B.shape(2, (108.0, 216.0, 540.0, 432.0), [], text=text)
    for doc in (read([], body=raw), read([(2, box)])):
        assert "The typed text has more formatting than gnnote reads; some of it was ignored" in doc.warnings
    monkeypatch.setattr(R, "MAX_TEXT_ATTRIBUTES", 100)
    monkeypatch.setattr(R, "MAX_DOC_ATTRIBUTES", 100)
    assert "The typed text has more formatting than gnnote reads; some of it was ignored" not in \
        read([(2, box)], body=raw).warnings


def test_a_flood_of_formatting_records_stays_small() -> None:
    """Attribute records compress to almost nothing: a few hundred KB of file can hold
    millions.  Only the kinds gnnote uses are stored, at most MAX_TEXT_ATTRIBUTES per text
    (each used to become an object of its own, about 1.5 GB for 10 million)."""
    import tracemalloc

    n = 200_000
    bold = struct.pack("<H", 24) + struct.pack("<4I", 5, 0, 1, 0) + struct.pack("<I", 1) + bytes(4)
    unused = struct.pack("<H", 16) + bytes(16)
    body = struct.pack("<I", 1) + "x".encode("utf-16-le") + struct.pack("<I", 3 * n) + bold * n + unused * (2 * n)
    body += struct.pack("<I", n) + (struct.pack("<H", 12) + bytes(12)) * n
    body += struct.pack("<4f", 16, 10, 16, 10) + b"\x00" + struct.pack("<H", 0)
    data = B.sdocx([B.page("p0", 1080, 1527, [B.layer([])])], body=B.text_object(struct.pack("<I", len(body)) + body))
    assert len(data) < 200_000
    start = time.monotonic()
    tracemalloc.start()
    try:
        doc = read_sdocx(data)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert time.monotonic() - start < 30
    assert peak < 80 * 1024 * 1024, peak  # the member itself takes about 15 MB (read twice by zipfile)
    assert [t.text for t in doc.pages[0].texts] == ["x"] and doc.pages[0].texts[0].runs[0].bold
    assert "The typed text has more formatting than gnnote reads; some of it was ignored" in doc.warnings


def test_a_paragraph_flood_stays_under_the_text_box_cap() -> None:
    """A few KB of "x\\n" used to become 11,000 pages and 264,000 text boxes (gigabytes in
    some writers); text boxes are now granted in proportion to the file's size."""
    from gnnote.samsungnotes import reader as RD

    n = 200_000
    data = B.sdocx([B.page("p0", 1080, 1527, [B.layer([])])], body=B.text_object(B.text_common("x\n" * n)))
    assert len(data) < 5_000
    start = time.monotonic()
    doc = read_sdocx(data)
    assert time.monotonic() - start < 20
    texts = sum(len(p.texts) for p in doc.pages)
    assert texts == RD.MIN_TEXT_BOXES + len(data) // RD.TEXT_BOX_BYTES
    assert len(doc.pages) < 200
    assert "The typed text is longer than gnnote reads (the note would get more text boxes than gnnote reads); " \
           f"{n - texts} paragraph(s) of it were not converted" in doc.warnings


def test_text_box_objects_share_the_text_box_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    monkeypatch.setattr(RD, "MIN_TEXT_BOXES", 3)
    monkeypatch.setattr(RD, "TEXT_BOX_BYTES", 10 ** 9)
    box = B.shape(2, (108.0, 216.0, 540.0, 432.0), [], text=B.text_common("Hi"))
    doc = read([(2, box)] * 5, body=B.text_object(B.text_common("Body\nMore")))
    assert sum(len(p.texts) for p in doc.pages) == 3
    assert "2 text box(es) were skipped: the note holds more typed text than gnnote reads" in doc.warnings
    assert "The typed text is longer than gnnote reads (the note would get more text boxes than gnnote reads); " \
           "2 paragraph(s) of it were not converted" in doc.warnings
    # many text-box objects in a small file: one more text box per TEXT_BOX_BYTES of the file
    n = 4_000
    data = B.sdocx([B.page("p0", 1080, 1527, [B.layer([(2, box)] * n)])])
    monkeypatch.setattr(RD, "MIN_TEXT_BOXES", 100)
    monkeypatch.setattr(RD, "TEXT_BOX_BYTES", 64)
    doc = read_sdocx(data)
    kept = RD.MIN_TEXT_BOXES + len(data) // RD.TEXT_BOX_BYTES
    assert kept < n and len(doc.pages[0].texts) == kept
    assert f"{n - kept} text box(es) were skipped: the note holds more typed text than gnnote reads" in doc.warnings


TEXT_DROPPED = "The note holds more typed text than gnnote reads; {n} character(s) (UTF-16 units) of it were not " \
    "converted"


def test_typed_text_shares_one_allowance_per_document() -> None:
    """A text that does not fit the document's text allowance keeps what does; the rest is
    stepped over without being decoded and counted."""
    limits = R.Limits(text_units=5)
    first = R.parse_object(R.Cursor(B.text_object(B.text_common("abcd"))), 2, {}, limits)
    second = R.parse_object(R.Cursor(B.text_object(B.text_common("efgh", spans=[B.flag_span(5, 0, 4)]))), 2, {},
                            limits)
    third = R.parse_object(R.Cursor(B.text_object(B.text_common("ijk"))), 2, {}, limits)
    texts = [obj.text for obj in (first, second, third) if isinstance(obj, R.Shape)]
    assert [(t.text, t.text_dropped) for t in texts if t is not None] == [("abcd", 0), ("e", 3), ("", 3)]
    assert (limits.text_units, limits.text_dropped) == (0, 6)
    assert [(a.kind, a.start, a.end) for a in texts[1].spans] == [(5, 0, 4)]  # (kept; the styles clamp it)
    # a text whose units are not all there fails before anything is charged
    limits = R.Limits(text_units=100)
    common = B.text_common("abcd")
    damaged = common[:4] + struct.pack("<I", 1000) + common[8:]
    with pytest.raises(R.SdocxError, match="truncated"):
        R.parse_object(R.Cursor(B.text_object(damaged)), 2, {}, limits)
    assert (limits.text_units, limits.text_dropped) == (100, 0)
    # one character per UTF-16 unit, unpaired surrogates included
    raw = struct.pack("<5H", 0xD800, 0x41, 0xD83D, 0xDE00, 0xFFFF)
    assert R.Cursor(raw).utf16_units(5) == "\ud800A\ud83d\ude00\uffff"
    assert R.Cursor(b"").utf16_units(0) == ""


def test_the_text_allowance_of_a_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.samsungnotes import reader as RD

    box = B.shape(2, (108.0, 216.0, 540.0, 432.0), [], text=B.text_common("Hi", spans=[B.flag_span(5, 0, 2)]))
    body = B.text_object(B.text_common("Body\nMore"))
    title = B.text_object(B.text_common("T"))
    monkeypatch.setattr(RD, "TEXT_UNITS_PER_BYTE", 0)
    monkeypatch.setattr(RD, "MIN_TEXT_UNITS", 9)
    # the title and the body (note.note) come first, then the pages in order
    doc = read([(2, box)] * 2, title=title, body=body)
    assert doc.title == "T"
    assert [t.text for t in doc.pages[0].texts] == ["Body", "Mor"]
    assert TEXT_DROPPED.format(n=5) in doc.warnings
    monkeypatch.setattr(RD, "MIN_TEXT_UNITS", 15)  # the page's text boxes come before the body text
    doc = read([(2, box)] * 3, title=title, body=body)
    assert [t.text for t in doc.pages[0].texts] == ["Hi", "Hi", "H", "Body", "More"]
    assert doc.pages[0].texts[2].runs[0].bold
    assert TEXT_DROPPED.format(n=1) in doc.warnings
    monkeypatch.setattr(RD, "MIN_TEXT_UNITS", 16)
    assert not any(w.startswith("The note holds more typed text") for w in read([(2, box)] * 3, title=title,
                                                                                   body=body).warnings)
    # the allowance grows with the file, up to records.MAX_DOC_TEXT_UNITS
    monkeypatch.setattr(RD, "MIN_TEXT_UNITS", 0)
    monkeypatch.setattr(RD, "TEXT_UNITS_PER_BYTE", 1)
    data = note([(2, box)] * 3, title=title, body=body)
    assert len(data) > 15
    assert sum(len(t.text) for t in read_sdocx(data).pages[0].texts) == 14  # (without the newline)
    monkeypatch.setattr(R, "MAX_DOC_TEXT_UNITS", 7)
    doc = read_sdocx(data)
    assert [t.text for t in doc.pages[0].texts] == ["Body", "M"] and TEXT_DROPPED.format(n=9) in doc.warnings


def test_a_flood_of_long_texts_stays_within_the_text_allowance() -> None:
    """2 million repeated units compress to about 4 KB: a 130 KB file used to hold 64 million
    characters of typed text (minutes in the PDF writer), a 1 MB file 500 million."""
    import tracemalloc

    from gnnote.samsungnotes import reader as RD

    units = R.MAX_TEXT_CHARS
    box = B.shape(2, (50.0, 50.0, 900.0, 200.0), [], text=B.text_common("x" * units))
    small = B.sdocx([B.page("p0", 1080, 1527, [B.layer([(2, box)] * 3)])])
    big = B.sdocx([B.page(f"p{i}", 1080, 1527, [B.layer([(2, box)] * 8)]) for i in range(4)])
    assert len(small) * RD.TEXT_UNITS_PER_BYTE < RD.MIN_TEXT_UNITS
    assert len(big) * RD.TEXT_UNITS_PER_BYTE > R.MAX_DOC_TEXT_UNITS and len(big) < 200_000
    for data, total, allowance in ((small, 3 * units, RD.MIN_TEXT_UNITS), (big, 32 * units, R.MAX_DOC_TEXT_UNITS)):
        start = time.monotonic()
        tracemalloc.start()
        try:
            doc = read_sdocx(data)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert time.monotonic() - start < 20  # (it used to take 40 s, with tracemalloc on)
        assert peak < 128 * 1024 * 1024, peak  # each page member alone inflates to 32 MB
        texts = [t for p in doc.pages for t in p.texts]
        assert sum(len(t.text) for t in texts) == allowance
        assert len(texts) == -(-allowance // units)
        assert TEXT_DROPPED.format(n=total - allowance) in doc.warnings


def test_text_runs_follow_the_spans() -> None:
    """Runs are found from the ends of the spans; they match a unit-by-unit comparison of
    the styles."""
    from gnnote.samsungnotes import reader as RD

    rng = random.Random(97)
    kinds = (RD.SPAN_BOLD, RD.SPAN_ITALIC, RD.SPAN_UNDERLINE, RD.SPAN_COLOUR, RD.SPAN_SIZE)
    for _ in range(200):
        n = rng.randrange(0, 30)
        text = "".join(rng.choice("ab \ufffc\n") for _ in range(n))
        spans = []
        for _ in range(rng.randrange(0, 8)):
            kind = rng.choice(kinds)
            lo, hi = rng.randrange(-2, n + 3), rng.randrange(-2, n + 3)
            data = struct.pack("<f", rng.choice((10.0, 20.0))) + bytes(4) if kind == RD.SPAN_SIZE else \
                struct.pack("<I", rng.randrange(2)) + bytes(4)
            spans.append(R.TextSpan(kind, lo, hi, data))
        body = R.TextBody(text, spans, [], (0.0, 0.0, 0.0, 0.0), [])
        styles = RD._Styles(body, 1.0)
        for _ in range(5):
            start = rng.randrange(0, n + 1)
            end = rng.randrange(start, n + 1)
            expected = []
            lo = start
            while lo < end:
                hi = lo + 1
                while hi < end and styles._key(hi) == styles._key(lo):
                    hi += 1
                piece = text[lo:hi].replace("\ufffc", "")
                if piece:
                    expected.append((piece, styles._key(lo)))
                lo = hi
            got = [(r.text, (r.bold, r.italic, r.underline, r.color, r.size, r.font))
                   for r in styles.runs(text, start, end)]
            assert got == [(t, (bool(k[0]), bool(k[1]), bool(k[2]), k[3], k[4], k[5])) for t, k in expected]


def test_added_pages_are_capped_after_the_notes_own_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """MAX_EXTRA_PAGES counts from the note's last page (a one-page note used to get up to
    MAX_PAGES + MAX_EXTRA_PAGES pages)."""
    from gnnote.samsungnotes import reader as RD

    monkeypatch.setattr(RD, "MAX_EXTRA_PAGES", 2)
    body = B.text_object(B.text_common("\n".join(f"line {i}" for i in range(500))))
    doc = read_sdocx(B.sdocx([B.page("p0", 1080, 1527, [B.layer([])])], body=body))
    assert len(doc.pages) == 3
    assert "The typed text needed 2 more page(s) than the note has; they were added" in doc.warnings
    assert any(w.startswith("The typed text is longer than gnnote reads (it would need more than 3 pages); ")
               for w in doc.warnings)


def test_the_body_layout_stops_at_its_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing after the cap is laid out; what is left is counted."""
    from gnnote.samsungnotes import reader as RD

    table = B.other(22, (0.0, 0.0, 100.0, 10.0))
    text = "\n".join(["\ufffc"] + [f"line {i}" for i in range(1000)] + ["\ufffc"])
    raw = B.text_object(B.text_common(text, inline=[(0, 22, table, 0, 0), (len(text) - 1, 22, table, 0, 0),
                                                    (len(text) + 5, 22, table, 0, 0)]))
    shape = R.parse_object(R.Cursor(raw), 2, {}, R.Limits())
    assert isinstance(shape, R.Shape) and shape.text is not None
    wrapped: List[int] = []
    real = RD._BodyLayout.wrap
    monkeypatch.setattr(RD._BodyLayout, "wrap", lambda self, *a: wrapped.append(1) or real(self, *a))
    layout = RD._BodyLayout(shape.text, [800.0], S, max_pages=3)
    placed = layout.layout()
    chunks = [p for p in placed if p.obj is None]
    assert layout.full == "pages" and max(p.page for p in placed) == 2 and len(wrapped) == len(chunks) + 1
    assert (layout.dropped_paragraphs, layout.dropped_objects) == (1000 - len(chunks), 2)
    layout = RD._BodyLayout(shape.text, [800.0], S, max_chunks=5)
    placed = layout.layout()
    assert layout.full == "text boxes" and layout.chunks == 5
    assert (layout.dropped_paragraphs, layout.dropped_objects) == (995, 2)


def test_mutated_sample_pages_never_crash(samples) -> None:
    data = sample(samples, "04-marker4-highlighter.sdocx")
    members = members_of(data)
    page_name = next(n for n in members if n.endswith(".page") and len(members[n]) > 1000)
    rng = random.Random(1729)
    start = time.monotonic()
    for i in range(120):
        raw = bytearray(members[page_name])
        for _ in range(rng.randint(1, 8)):
            pos = rng.randrange(len(raw))
            raw[pos] = rng.randrange(256)
        if i % 4 == 0:
            raw = raw[:rng.randrange(len(raw))]
        mutated = dict(members)
        mutated[page_name] = bytes(raw)
        doc = read_sdocx(zip_members(mutated))
        assert doc.pages and all(math.isfinite(p.x) and math.isfinite(p.y)
                                 for pg in doc.pages for s in pg.strokes for p in s.points)
    assert time.monotonic() - start < 120


def test_mutated_note_headers_never_crash(samples) -> None:
    members = members_of(sample(samples, "01-basic-formatting.sdocx"))
    rng = random.Random(4242)
    for _ in range(60):
        raw = bytearray(members["note.note"])
        for _ in range(rng.randint(1, 6)):
            raw[rng.randrange(len(raw))] = rng.randrange(256)
        mutated = dict(members)
        mutated["note.note"] = bytes(raw)
        read_sdocx(zip_members(mutated))


# --------------------------------------------------------------------------- the public sample notes


def _pen_census(data: bytes) -> Tuple[List[int], Counter]:
    """Page-record count per page list entry and stroke pen names, decoded at the record level."""
    bundle = readutil.ZipBundle(data, "x")
    note_doc = R.parse_note(bundle.read("note.note"), R.Limits())
    pens: Counter = Counter()
    strokes = []
    for uuid in R.parse_page_list(bundle.read("pageIdInfo.dat")):
        page = R.parse_page(bundle.read(uuid + ".page"), note_doc.strings, R.Limits())
        found = [o for layer in page.layers for o in layer.objects if isinstance(o, R.Stroke)]
        strokes.append(len(found))
        pens.update(o.pen_name.rsplit(".", 1)[-1] for o in found)
    return strokes, pens


def test_samples_page_records_strokes_and_pens(samples) -> None:
    expected = {"01-basic-formatting.sdocx": ([0] * 6, {}),
                "02-shapes-and-dot-calibration.sdocx": ([77, 0], {"FountainPen": 77}),
                "03-image-placement.sdocx": ([0] * 4, {}),
                "04-marker4-highlighter.sdocx": ([41, 0], {"FountainPen": 29, "Marker4": 12})}
    for name, (strokes, pens) in expected.items():
        data = sample(samples, name)
        assert _pen_census(data) == (strokes, Counter(pens)), name
        doc = read_sdocx(data)
        assert len(doc.pages) == len(strokes) - 1, name  # the app's empty last page is left out, as its PDF does
        assert {p.width for p in doc.pages} == {600.0}, name
        ink = [s for p in doc.pages for s in p.strokes if s.pen is not None]
        assert len(ink) == sum(strokes), name
    doc = read_sdocx(sample(samples, "04-marker4-highlighter.sdocx"))
    kinds = Counter((s.kind, s.pen) for s in doc.pages[0].strokes)
    assert kinds == {("pen", "fountain"): 29, ("highlighter", "marker"): 12}


def test_sample_page_sizes(samples) -> None:
    heights = {"01-basic-formatting.sdocx": 1527 * 600 / 1080, "02-shapes-and-dot-calibration.sdocx": 2613 * 600 / 1848,
               "03-image-placement.sdocx": 1527 * 600 / 1080, "04-marker4-highlighter.sdocx": 2613 * 600 / 1848}
    for name, height in heights.items():
        pages = read_sdocx(sample(samples, name)).pages
        assert [p.height for p in pages] == pytest.approx([height] * len(pages)), name


def test_sample_01_typed_text(samples) -> None:
    doc = read_sdocx(sample(samples, "01-basic-formatting.sdocx"))
    assert doc.title == "01-basic-test" and len(doc.pages) == 5
    assert [len(p.texts) for p in doc.pages] == [11, 13, 12, 10, 5]
    first = doc.pages[0].texts[0]
    assert first.text == "Compatibility Test 01 - Basic Test"
    assert (first.x, first.size) == (pytest.approx(16 * 600 / 360), pytest.approx(25.0))
    texts = [t.text for p in doc.pages for t in p.texts]
    assert "Accented Latin: café, naïve, résumé, jalapeño, Ångström, Straße" in texts
    assert "Final sentence: The quick brown fox jumps over the lazy dog." == texts[-1]
    bold = [r.text for p in doc.pages for t in p.texts for r in t.runs if r.bold]
    assert bold == ["This phrase should be bold.", "Markdown heading level 1", "Markdown heading level 2",
                    "Bold with asterisks", "Bold with underscores"]
    by_text = {t.text: t for p in doc.pages for t in p.texts}
    # indent level 1, as in the app's PDF export (x = 53.3 pt there)
    assert by_text["Blockquote marker"].x == by_text["Example link"].x == pytest.approx(53.33, abs=0.01)
    sizes = {t.text: t.size for p in doc.pages for t in p.texts}
    assert sizes["Markdown heading level 1"] == pytest.approx(21 * 600 / 360)
    assert doc.warnings == [
        "The typed text and the pictures in it were laid out by gnnote (the app stores no line positions); line "
        "breaks and positions are approximate",
        "Text formatting without an equivalent in gnnote was dropped: links (the text is kept), list bullets and "
        "numbers, strikethrough",
        "1 code block(s) in the typed text were not converted",
        "1 table(s) in the typed text were not converted",
    ]


def test_sample_01_text_lines_match_the_apps_pdf_export(samples) -> None:
    """The first line of each paragraph sits where the app's own PDF export puts it (the
    export is in the research corpus only; this checks the values measured from it)."""
    doc = read_sdocx(sample(samples, "01-basic-formatting.sdocx"))
    # the baseline of every paragraph's first line in the app's export (pt from the page top),
    # page by page; lists, the table and the code block on page 4 included
    app = [
        [54.6, 101.2, 181.2, 381.3, 461.3, 501.3, 541.2, 581.2, 661.3, 741.3, 781.3],
        [47.9, 127.9, 207.9, 247.9, 287.9, 327.9, 367.9, 407.9, 487.9, 567.9, 647.9, 727.9, 767.9],
        [87.9, 167.9, 247.9, 287.9, 327.9, 367.9, 447.9, 573.4, 660.5, 712.8, 752.8, 792.8],
        [47.9, 87.9, 127.9, 167.9, 231.2, 271.2, 311.2, 351.3, 391.3, 484.6],
        [187.6, 267.6, 387.6, 427.6, 507.6],
    ]
    # where gnnote's writers put the first baseline of a text box
    ours = [[t.y + 0.952 * t.size for t in page.texts] for page in doc.pages]
    assert [len(p) for p in ours] == [len(p) for p in app]
    for number, (mine, theirs) in enumerate(zip(ours, app), 1):
        assert mine == pytest.approx(theirs, abs=0.5), f"page {number}"


def test_sample_02_shapes(samples) -> None:
    doc = read_sdocx(sample(samples, "02-shapes-and-dot-calibration.sdocx"))
    page = doc.pages[0]
    assert page.paper == "dotted" and len(page.strokes) == 83
    shapes = [s for s in page.strokes if s.pen is None]
    assert [len(s.points) for s in shapes] == [5, 10, 4, 6, 7, 2]  # square, circle, triangle, pentagon, hexagon, line
    assert all(s.controls is not None and s.color == (0.0, 0.0, 0.0, 1.0) for s in shapes)
    assert {round(s.width, 2) for s in shapes} == {2.96}
    square = shapes[0]
    assert (square.points[0].x, square.points[0].y) == (pytest.approx(28.15, abs=0.01), pytest.approx(42.26, abs=0.01))
    assert doc.warnings == ["6 shape(s) and line(s) were converted to ink strokes",
                            "Pen pressure was turned into per-point stroke widths with an approximate model; line "
                            "weights may differ from the app"]


def test_sample_03_pictures(samples) -> None:
    doc = read_sdocx(sample(samples, "03-image-placement.sdocx"))
    got = [(i, round(im.x, 2), round(im.y, 2), round(im.w, 2), round(im.h, 2), im.rotation,
            readutil.image_pixel_size(im.data)) for i, p in enumerate(doc.pages) for im in p.images]
    assert got == [
        (0, 26.67, 49.84, 250.56, 166.67, 0.0, (451, 300)),
        (0, 26.67, 225.26, 545.33, 362.75, 0.0, (451, 300)),
        (0, 26.67, 596.76, 250.56, 166.67, 180.0, (451, 300)),
        (1, -15.28, 58.61, 250.56, 166.67, 90.0, (451, 300)),
        (1, -15.28, 317.92, 250.56, 166.67, 270.0, (451, 300)),
        (2, 48.9, 80.84, 250.56, 166.67, 45.0, (451, 300)),
        (2, 26.67, 353.61, 185.45, 96.18, 0.0, (332, 172)),  # cropped from the 451 x 300 photo
    ]
    assert all(im.fmt == "png" for p in doc.pages for im in p.images)
    assert doc.warnings == ["The typed text and the pictures in it were laid out by gnnote (the app stores no line "
                            "positions); line breaks and positions are approximate"]


def test_sample_04_highlighter(samples) -> None:
    doc = read_sdocx(sample(samples, "04-marker4-highlighter.sdocx"))
    page = doc.pages[0]
    assert page.paper == "lined" and [t.text for t in page.texts] == ["Text"]
    colours = Counter(tuple(round(c, 3) for c in s.color) for s in page.strokes if s.kind == "highlighter")
    assert colours == {(1.0, 0.796, 0.188, 0.502): 10, (0.184, 0.922, 0.824, 0.502): 1, (0.2, 0.588, 1.0, 0.502): 1}
    assert {round(s.width, 3) for s in page.strokes if s.kind == "highlighter"} == {11.757}
    pens = [s for s in page.strokes if s.kind == "pen"]
    assert {round(s.width, 3) for s in pens} == {1.628}
    assert {tuple(round(c, 6) for c in s.color) for s in pens} == {(round(3 / 255, 6),) * 3 + (1.0,)}


def _ink_box(strokes: Sequence[Stroke], grow: bool) -> Tuple[float, float, float, float]:
    pts = [(p.x, p.y, p.width / 2 if grow else 0.0) for s in strokes for p in stroke_polyline(s)]
    return (min(x - h for x, _, h in pts), min(y - h for _, y, h in pts),
            max(x + h for x, _, h in pts), max(y + h for _, y, h in pts))


def test_ink_lands_inside_the_ink_of_the_apps_pdf_export(samples) -> None:
    """Samsung's PDF export draws ink as pictures, 600 pt wide pages: the strokes (scaled by
    600 / page width) must lie inside those pictures, their edges within a few pt."""
    pymupdf = pytest.importorskip("pymupdf")
    cases = [("02-shapes-and-dot-calibration", [lambda s: True]),
             ("04-marker4-highlighter", [lambda s: s.kind == "pen", lambda s: s.kind == "highlighter"])]
    for stem, groups in cases:
        doc = read_sdocx(sample(samples, stem + ".sdocx"))
        pdf = pymupdf.open(str(samples.pinned_file(stem + ".pdf")))
        assert len(pdf) == len(doc.pages) == 1
        assert (pdf[0].rect.width, pdf[0].rect.height) == (600.0, 848.0)
        boxes = [tuple(i["bbox"]) for i in pdf[0].get_image_info() if i["width"] < 1800]  # not the paper
        assert len(boxes) == len(groups)
        for keep, ref in zip(groups, boxes):
            strokes = [s for s in doc.pages[0].strokes if keep(s)]
            centre = _ink_box(strokes, grow=False)
            inked = _ink_box(strokes, grow=True)
            assert ref[0] <= centre[0] and ref[1] <= centre[1] and centre[2] <= ref[2] and centre[3] <= ref[3]
            assert ref[0] - 0.5 <= inked[0] and ref[1] - 0.5 <= inked[1]
            assert inked[2] <= ref[2] + 0.5 and inked[3] <= ref[3] + 0.5
            assert all(abs(a - b) < 3.5 for a, b in zip(inked, ref)), (stem, inked, ref)


def test_inkterop_fixture(samples) -> None:
    doc = read_sdocx(samples.inkterop_fixture("sdocx", "synthetic-two-page.sdocx").read_bytes())
    assert len(doc.pages) == 2 and [len(p.strokes) for p in doc.pages] == [2, 2]
    assert [(s.kind, s.pen) for p in doc.pages for s in p.strokes] == \
        [("pen", "fountain"), ("highlighter", "marker"), ("pen", "pencil"), ("pen", "fountain")]
    assert {p.width for p in doc.pages} == {600.0}


ORACLE = r"""
import json, sys, zipfile
sys.path.insert(0, sys.argv[1])
from inkterop.formats import sdocx as S
z = zipfile.ZipFile(sys.argv[2])
strings, w, h = S.read_note_strings(z.read("note.note"))
out = []
for uuid in S.read_page_list(z.read("pageIdInfo.dat")):
    page = S.parse_page(z.read(uuid + ".page"), strings)
    out.append({"w": page.width, "h": page.height,
                "strokes": [{"x": s.x, "y": s.y, "pen": s.pen_name, "bgra": list(s.color_bgra), "size": s.pen_size,
                             "eraser": s.eraser} for layer in page.layers if layer.visible for s in layer.strokes]})
print(json.dumps(out))
"""


def _oracle(samples, path: Path) -> list:
    src = samples.repo("inkterop") / "core" / "src"
    proc = subprocess.run([sys.executable, "-I", "-c", ORACLE, str(src), str(path)], cwd=str(src),
                          env=dict(os.environ, PYTHONPATH=str(src)), capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        pytest.skip(f"inkterop's sdocx reader could not run: {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


def test_strokes_match_inkterops_reader(samples) -> None:
    """inkterop (MIT) decodes the same records independently; it runs in its own process."""
    paths = [samples.pinned_file(n) for n in SAMPLES] + [samples.inkterop_fixture("sdocx", "synthetic-two-page.sdocx")]
    for path in paths:
        oracle = _oracle(samples, path)
        doc = read_sdocx(path.read_bytes())
        assert len(doc.pages) <= len(oracle)
        assert all(not page["strokes"] for page in oracle[len(doc.pages):]), path.name
        for page, want in zip(doc.pages, oracle):
            scale = 600.0 / want["w"]
            assert page.height == pytest.approx(want["h"] * scale)
            ink = [s for s in page.strokes if s.pen is not None or s.kind == "highlighter"]
            assert len(ink) == len(want["strokes"]), path.name
            for stroke, ref in zip(ink, want["strokes"]):
                assert [round(p.x / scale, 6) for p in stroke.points] == [round(v, 6) for v in ref["x"]]
                assert [round(p.y / scale, 6) for p in stroke.points] == [round(v, 6) for v in ref["y"]]
                b, g, r, a = ref["bgra"]
                assert stroke.color == pytest.approx((r / 255, g / 255, b / 255, a / 255))
                assert (stroke.kind == "highlighter") == ref["pen"].endswith(("Marker4", "Marker3", "Highlighter"))


# --------------------------------------------------------------------------- conversions, CLI


@pytest.mark.parametrize("target", ["notability", "goodnotes", "pdf"])
def test_samples_convert_and_read_back(samples, target: str) -> None:
    for name in SAMPLES:
        data = sample(samples, name)
        doc = read_sdocx(data)
        result = convert(data, name, Options(target=target))
        assert result.source_format == "samsungnotes" and result.target_format == target
        back = {"notability": read_note, "goodnotes": read_goodnotes, "pdf": read_pdf}[target](result.data)
        assert len(back.pages) == len(doc.pages), name
        if target != "pdf":  # flattened PDF ink is not editable any more
            assert [len(p.strokes) for p in back.pages] == [len(p.strokes) for p in doc.pages], name
            assert sum(s.kind == "highlighter" for p in back.pages for s in p.strokes) == \
                sum(s.kind == "highlighter" for p in doc.pages for s in p.strokes), name
            assert [len(p.images) for p in back.pages] == [len(p.images) for p in doc.pages], name


def test_samples_convert_to_pdf_annotations_that_stay_editable(samples) -> None:
    data = sample(samples, "04-marker4-highlighter.sdocx")
    back = read_pdf(convert(data, "x.sdocx", Options(target="pdf", pdf_ink="annotations")).data)
    assert len(back.pages[0].strokes) == 41
    assert sum(s.kind == "highlighter" for s in back.pages[0].strokes) == 12


def test_cli_convert_and_info(tmp_path: Path, samples, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "Highlights.sdocx"
    src.write_bytes(sample(samples, "04-marker4-highlighter.sdocx"))
    assert main(["convert", str(src), "--to", "notability"]) == 0
    out = capsys.readouterr().out
    assert "samsungnotes -> notability" in out and "41 strokes" in out
    back = read_note((tmp_path / "Highlights.note").read_bytes())
    assert back.title == "Highlights" and sum(len(p.strokes) for p in back.pages) == 41
    assert main(["convert", str(src), "--to", "samsungnotes"]) == 2
    assert "Samsung Notes files can be read but not written" in capsys.readouterr().err
    assert main(["info", str(src)]) == 0
    assert "Format:  samsungnotes" in capsys.readouterr().out


# --------------------------------------------------------------------------- registry and sniffing


def test_registry_entry() -> None:
    fmt = formats.get("samsungnotes")
    assert (fmt.name, fmt.extension, fmt.input_extensions) == ("Samsung Notes", ".sdocx", (".sdocx",))
    assert fmt.readable and not fmt.writable and fmt.reader == "gnnote.samsungnotes.reader:read_sdocx"
    ids = list(formats.FORMATS)
    assert ids.index("samsungnotes") == ids.index("remarkable") + 1
    assert formats.default_target("samsungnotes") == "notability"
    assert formats.format_for_extension("Lecture.SDOCX") is fmt
    with pytest.raises(ValueError, match="cannot be written"):
        Options(target="samsungnotes").validate()


def test_detection_by_content_and_extension() -> None:
    data = note([(1, B.stroke([(5.0, 5.0)], pen_id=1))])
    assert detect_format("x.sdocx", data) == "samsungnotes"
    assert detect_format("renamed.zip", data) == "samsungnotes"
    assert detect_format("folder.zip", note(folder="Note/")) == "samsungnotes"
    doc = to_document(data, "Lecture 3.sdocx")
    assert doc.title == "Lecture 3" and doc.source_format == "samsungnotes"


def test_sniffer_rules() -> None:
    sniff = formats.get("samsungnotes").sniff
    assert sniff(b"PK", ["note.note", "pageIdInfo.dat", "a.page"])
    assert sniff(b"PK", ["note.note", "end_tag.bin"])
    assert sniff(b"PK", ["Note/note.note", "Note/end_tag.bin"])
    assert not sniff(b"PK", ["note.note"])
    assert not sniff(b"PK", ["a/b/note.note", "a/b/end_tag.bin"])
    assert not sniff(b"PK", ["__MACOSX/note.note", "__MACOSX/end_tag.bin"])
    assert not sniff(b"PK", ["Session.plist", "note.note.bak"])
    assert not sniff(b"note.note end_tag.bin", None)


def test_other_sniffers_do_not_claim_samsung_notes(samples) -> None:
    synthetic = [note(), note(folder="Note/"), B.sdocx([B.page("a", 1080, 1527, [B.layer([])])], page_list_member=False)]
    real = [samples.pinned_file(n).read_bytes() for n in SAMPLES]
    for data in synthetic + real:
        names = formats.sniff_zip_names(data)
        claimed = [f.id for f in formats.FORMATS.values() if f.sniff(data, names)]
        assert claimed == ["samsungnotes"]


def test_sniffer_claims_no_other_sample(samples) -> None:
    """Every file of the reference repositories and pinned files: only ``.sdocx`` notes are claimed."""
    sniff = formats.get("samsungnotes").sniff
    seen = 0
    for dirpath, dirnames, filenames in os.walk(samples.root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.stat().st_size > 64 * 1024 * 1024:
                continue
            data = path.read_bytes()
            seen += 1
            assert sniff(data, formats.sniff_zip_names(data)) == (path.suffix == ".sdocx"), path
    if not seen:
        pytest.skip("no sample files available")
