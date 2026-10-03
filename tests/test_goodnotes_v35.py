"""Reader tests for the schema-25 / schema-35 GoodNotes samples (Test6 .. Test9).

Ground truth is GoodNotes' own PDF export next to every sample (``TestN.pdf``): page sizes and
order come from it directly, element positions from the measurements in
``docs/goodnotes-v35-binding.md`` / ``goodnotes-v35-elements.md`` / ``goodnotes-v35-strokes.md``.
Expectations that need the record stream (fill counts, the stored-block stroke, the multi-block
frames) are derived with an independent walk over ``gnnote.protobuf`` so the reader's own
decisions are never used as their own oracle.  The raster comparison needs PyMuPDF and is
skipped without it.
"""
from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from gnnote import applelz4, protobuf, tpl
from gnnote.goodnotes.reader import (
    CANVAS_PER_POINT, jpeg_exif_orientation, page_uuid_of_notes, read_goodnotes,
)
from gnnote.model import Document, Page
from gnnote.pdfutil import pdf_info

K = CANVAS_PER_POINT
NEW_SAMPLES = ("Test6", "Test7", "Test8", "Test9")
PENCIL_WARNING = "pencil strokes are approximated"


# --------------------------------------------------------------------------- helpers


def _sample(samples, name: str) -> Path:
    path = samples.repo("goodparse") / "samples" / f"{name}.goodnotes"
    if not path.is_file():
        pytest.skip(f"{name}.goodnotes not available (needs the goodparse clone with 9 samples)")
    return path


def _export(samples, name: str) -> Path:
    path = samples.repo("goodparse") / "samples" / f"{name}.pdf"
    if not path.is_file():
        pytest.skip(f"{name}.pdf export not available")
    return path


_docs: Dict[str, Document] = {}


def _read(samples, name: str) -> Document:
    if name not in _docs:
        _docs[name] = read_goodnotes(_sample(samples, name).read_bytes())
    return _docs[name]


def _records(path: Path, member_prefix: str) -> List[List[protobuf.Field]]:
    """Decoded records of the ``notes/`` member whose UUID starts with ``member_prefix``."""
    z = zipfile.ZipFile(io.BytesIO(path.read_bytes()))
    member = next(n for n in z.namelist() if n.startswith("notes/" + member_prefix))
    return [protobuf.decode_message(r) for r in protobuf.decode_records(z.read(member))]


def _is_metadata(fields) -> bool:
    f1 = protobuf.get(fields, 1)
    return (f1 is not None and f1.wire_type == protobuf.WIRE_LEN and len(f1.value) == 36
            and any(f.number in (8, 9, 16) and f.wire_type == protobuf.WIRE_VARINT for f in fields))


def _content_records(path: Path, member_prefix: str) -> List[Tuple[bool, int, List[protobuf.Field]]]:
    """[(tombstoned, kind, body)] for every content record of one page, in stream order."""
    out = []
    tombstone = False
    for fields in _records(path, member_prefix):
        if _is_metadata(fields):
            f3 = protobuf.get(fields, 3)
            tombstone = f3 is not None and f3.value == 1
            continue
        out.append((tombstone, fields[0].number, protobuf.decode_message(fields[0].value)))
        tombstone = False
    return out


def _f32(fields, number: int, default: float = 0.0) -> float:
    f = protobuf.get(fields, number)
    return protobuf.fixed32_float(f) if f is not None and f.wire_type == protobuf.WIRE_FIXED32 else default


def _msg(fields, number: int):
    f = protobuf.get(fields, number)
    return protobuf.decode_message(f.value) if f is not None and f.wire_type == protobuf.WIRE_LEN else None


def _notes_members(path: Path) -> Dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(path.read_bytes()))
    return {n[len("notes/"):]: z.read(n) for n in z.namelist() if n.startswith("notes/")}


def _bbox(page: Page, with_texts: bool, with_pencil: bool = True) -> Optional[Tuple[float, float, float, float]]:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for s in page.strokes:
        if s.pen == "pencil" and not with_pencil:
            continue
        hw = 0.0 if s.kind == "fill" else s.width / 2.0
        xs0.append(min(p.x for p in s.points) - hw)
        ys0.append(min(p.y for p in s.points) - hw)
        xs1.append(max(p.x for p in s.points) + hw)
        ys1.append(max(p.y for p in s.points) + hw)
    for im in page.images:
        xs0.append(im.x); ys0.append(im.y); xs1.append(im.x + im.w); ys1.append(im.y + im.h)
    if with_texts:
        for t in page.texts:
            xs0.append(t.x); ys0.append(t.y); xs1.append(t.x + t.w); ys1.append(t.y + t.h)
    if not xs0:
        return None
    return (min(xs0), min(ys0), max(xs1), max(ys1))


# --------------------------------------------------------------------------- page binding


def test_page_sizes_and_order_match_the_goodnotes_export(samples):
    """The strongest check: page sizes in display order equal GoodNotes' own PDF export."""
    for name in NEW_SAMPLES:
        doc = _read(samples, name)
        export = pdf_info(_export(samples, name).read_bytes())
        got = [(round(p.width, 2), round(p.height, 2)) for p in doc.pages]
        want = [(round(p.width, 2), round(p.height, 2)) for p in export.pages]
        assert got == want, name
    t9 = _read(samples, "Test9")
    assert [(round(p.width, 2), round(p.height, 2)) for p in t9.pages] == [
        (595.28, 841.89), (595.28, 841.89), (595.28, 841.89), (595.28, 841.89),
        (1280.0, 905.0), (595.2, 841.68), (454.91, 143.28)]


def test_notes_uuid_to_page_uuid_carries():
    assert page_uuid_of_notes("D298AC1F-CFFB-42D1-A4AE-D07D3281E450") == "D298AC1F-CFFB-42D1-A4AE-D07D3281E44F"
    assert page_uuid_of_notes("A37FF8B6-79CB-4486-B233-607737EDC920") == "A37FF8B6-79CB-4486-B233-607737EDC91F"
    assert page_uuid_of_notes("308EE367-D7BA-472E-AD76-F3671AC820A0") == "308EE367-D7BA-472E-AD76-F3671AC8209F"
    assert page_uuid_of_notes("50000001-0000-4000-8000-000000000001") == "50000001-0000-4000-8000-000000000000"
    # the borrow crosses hyphen groups and wraps at zero
    assert page_uuid_of_notes("00000000-0000-0000-0001-000000000000") == "00000000-0000-0000-0000-FFFFFFFFFFFF"
    assert page_uuid_of_notes("00000000-0000-0000-0000-000000000000") == "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"
    assert page_uuid_of_notes("d298ac1f-cffb-42d1-a4ae-d07d3281e450").isupper()


def test_carry_case_pages_are_bound(samples):
    """The three pages whose UUID ends in F used to be 'no template event' pages."""
    t6 = _read(samples, "Test6")
    t7 = _read(samples, "Test7")
    t9 = _read(samples, "Test9")
    members6 = _notes_members(_sample(samples, "Test6"))
    assert any(n.startswith("A37FF8B6") and n.endswith("EDC920") for n in members6)
    for doc, index, strokes in ((t6, 4, 1), (t7, 3, 5), (t9, 1, 123)):
        page = doc.pages[index]
        assert page.background is not None and page.template_is_builtin
        assert len([s for s in page.strokes if s.kind != "fill"]) == strokes
        assert not any("no template event" in w for w in doc.warnings)
    grid = t9.pages[1]
    assert grid.paper == "grid"
    assert (round(grid.width, 2), round(grid.height, 2)) == (595.28, 841.89)
    # the 123 strokes of the grid page all lie on it (75 of them below the old guessed height)
    for s in grid.strokes:
        for p in s.points:
            assert -1.0 <= p.x <= grid.width + 1.0 and -1.0 <= p.y <= grid.height + 1.0
    assert sum(1 for s in grid.strokes if min(p.y for p in s.points) > 588.45) >= 70


def test_rebound_paper_wins(samples):
    """Event #3 re-binds Test6 page 2 / Test7 page 1 to the blank paper (export: 0 ruled rows)."""
    t6 = _read(samples, "Test6")
    assert [p.paper for p in t6.pages] == ["lined", "plain", "plain", "plain", "plain"]
    assert all(p.template_is_builtin for p in t6.pages)
    ruled = t6.pages[0].background.pdf_id
    plain = t6.pages[1].background.pdf_id
    assert ruled != plain
    assert len(t6.pdfs[ruled]) > len(t6.pdfs[plain])  # 1904-byte ruled paper vs 892-byte blank one
    assert all(p.background.pdf_id == plain for p in t6.pages[1:])
    t7 = _read(samples, "Test7")
    assert [p.paper for p in t7.pages] == ["plain"] * 4
    t8 = _read(samples, "Test8")
    assert [p.paper for p in t8.pages] == ["plain"] * 4
    t9 = _read(samples, "Test9")
    assert [p.paper for p in t9.pages[:4]] == ["plain", "grid", "grid", "plain"]


def test_internal_paper_alias_and_user_pdf_pages(samples):
    t9 = _read(samples, "Test9")
    form = t9.pages[5]
    assert form.background is not None and not form.template_is_builtin
    assert form.background.pdf_id.startswith("80973E20")  # the attachment id, stored under 4452790C
    assert t9.pdfs[form.background.pdf_id].startswith(b"%PDF-")
    assert len(t9.pdfs[form.background.pdf_id]) == 779994
    assert len([s for s in form.strokes]) == 22
    figma, strip = t9.pages[4], t9.pages[6]
    assert not figma.template_is_builtin and figma.background.pdf_id.startswith("F5503752")
    assert not strip.template_is_builtin and strip.background.pdf_id.startswith("4043E92A")
    assert len(strip.strokes) == 6 and not figma.strokes


def test_no_warning_for_understood_content(samples):
    for name in NEW_SAMPLES:
        doc = _read(samples, name)
        assert doc.warnings == [w for w in doc.warnings if PENCIL_WARNING in w], (name, doc.warnings)
    assert _read(samples, "Test9").title == "Test"
    assert _read(samples, "Test6").title == "Test6"


# --------------------------------------------------------------------------- shape fills (#9)


def test_shape_fills_follow_their_outline_strokes(samples):
    total_records = live_records = 0
    for name in ("Test6", "Test7", "Test8"):
        doc = _read(samples, name)
        path = _sample(samples, name)
        members = _notes_members(path)
        fills_in_file = 0
        for page in doc.pages:
            for i, s in enumerate(page.strokes):
                if s.kind != "fill":
                    continue
                fills_in_file += 1
                assert i > 0, name
                parent = page.strokes[i - 1]
                assert parent.kind != "fill"
                assert tuple(round(c, 3) for c in s.color[:3]) == tuple(round(c, 3) for c in parent.color[:3])
                assert s.color[3] == pytest.approx(0.1, abs=1e-4)
                assert s.width == 0.0 and s.controls is None
                assert s.outline is not None and len(s.outline) == 1
                polygon = s.outline[0]
                assert len(polygon) >= 4
                assert (polygon[0].x, polygon[0].y) == (polygon[-1].x, polygon[-1].y)
                assert [(p.x, p.y) for p in s.points] == [(p.x, p.y) for p in polygon]
                # the fill lies inside the outline's bounding box (parent = sampled ellipse / polygon)
                px0, py0, px1, py1 = parent.bbox()
                fx0, fy0, fx1, fy1 = s.bbox()
                assert fx0 >= px0 - 0.5 and fy0 >= py0 - 0.5 and fx1 <= px1 + 0.5 and fy1 <= py1 + 0.5
        # independent count of the kind-#9 records (10 live + 3 erased over the three files)
        for member in members.values():
            if not member:
                continue
            tombstone = False
            for fields in (protobuf.decode_message(r) for r in protobuf.decode_records(member)):
                if _is_metadata(fields):
                    f3 = protobuf.get(fields, 3)
                    tombstone = f3 is not None and f3.value == 1
                    continue
                if fields[0].number == 9:
                    total_records += 1
                    body = protobuf.decode_message(fields[0].value)
                    erased = tombstone or (protobuf.get(body, 14) is not None and protobuf.get(body, 14).value == 1)
                    if not erased:
                        live_records += 1
                tombstone = False
        expected = {"Test6": 3, "Test7": 3, "Test8": 4}[name]
        assert fills_in_file == expected, name
    assert total_records == 13 and live_records == 10
    # Test9 (schema 35) has no fill records and its closed shapes stay unfilled
    assert not any(s.kind == "fill" for p in _read(samples, "Test9").pages for s in p.strokes)


def test_fill_geometry_matches_the_export(samples):
    """Test7 p2 rectangle fill: export rect (253.09, 156.94)-(413.18, 342.80) pt; triangle
    (29.12, 213.97)-(197.99, 397.60) pt (elements doc section 1.3)."""
    page = _read(samples, "Test7").pages[1]
    fills = [s for s in page.strokes if s.kind == "fill"]
    assert len(fills) == 3
    boxes = [tuple(round(v, 2) for v in s.bbox()) for s in fills]
    assert (253.09, 156.94, 413.18, 342.8) in [tuple(round(v, 2) for v in b) for b in boxes]
    assert any(abs(b[0] - 29.12) < 0.02 and abs(b[1] - 213.97) < 0.02 and abs(b[2] - 197.99) < 0.02
               and abs(b[3] - 397.6) < 0.02 for b in boxes)
    rect = next(s for s in fills if len(s.points) == 5)
    assert rect.color[:3] == pytest.approx((0.0, 0.451, 0.333), abs=1e-3)


# --------------------------------------------------------------------------- schema-35 text (#21)


def test_type35_text_elements(samples):
    page = _read(samples, "Test9").pages[2]
    texts = {t.text: t for t in page.texts}
    assert set(texts) == {"S U C H", "GOOD", "F R I E N D S", "Hallo Franz…,!,,!,"}
    assert len(page.texts) == 4
    hallo = texts["Hallo Franz…,!,,!,"]
    assert (hallo.x, hallo.y) == pytest.approx((274.77, 48.52), abs=0.5)
    assert hallo.size == pytest.approx(13.09, abs=0.01)
    assert hallo.align == "left" and hallo.rotation == 0.0
    assert hallo.runs[0].font == "Helvetica Neue" and not hallo.runs[0].bold
    assert hallo.color == pytest.approx((0.1176, 0.1059, 0.1059, 1.0), abs=1e-3)
    such = texts["S U C H"]
    assert such.x + such.w / 2 == pytest.approx(132.43, abs=0.5)
    assert such.size == pytest.approx(13.09, abs=0.01) and such.align == "center"
    assert such.runs[0].font == "Futura" and such.runs[0].bold
    assert such.color == pytest.approx((0.2941, 0.2941, 0.2941, 1.0), abs=1e-3)
    good = texts["GOOD"]
    assert good.x + good.w / 2 == pytest.approx(132.39, abs=0.5)
    assert good.y == pytest.approx(69.73, abs=0.5)
    assert good.size == pytest.approx(30.0, abs=0.01)
    assert good.color == pytest.approx((0.7614, 0.9729, 0.6978, 1.0), abs=1e-3)
    friends = texts["F R I E N D S"]
    assert friends.size == pytest.approx(13.09 * 1.00574, abs=0.02)
    assert friends.rotation == pytest.approx(-0.24, abs=0.02)  # -0.0042 rad, clockwise positive
    assert friends.align == "center"
    for t in page.texts:
        assert t.w > 0 and t.h > 0
        assert "".join(r.text for r in t.runs) == t.text
        assert all(r.size == pytest.approx(t.size, abs=0.01) for r in t.runs)
    # the RTF (#8) boxes of the schema-25 files are still read (one caption per page)
    t6 = _read(samples, "Test6")
    assert [len(p.texts) for p in t6.pages] == [1, 1, 1, 1, 1]
    assert t6.pages[1].texts[0].text == "Isolated marker stroke (line, thickness 4.6)"
    assert (t6.pages[1].texts[0].x, t6.pages[1].texts[0].y) == pytest.approx((48.91, 15.15), abs=0.05)
    assert [len(p.texts) for p in _read(samples, "Test7").pages] == [7, 4, 1, 2]


# --------------------------------------------------------------------------- images


def test_sticker_pdf_image(samples):
    page = _read(samples, "Test9").pages[2]
    assert len(page.images) == 1
    im = page.images[0]
    assert im.fmt == "pdf" and im.data.startswith(b"%PDF-")
    assert (im.x, im.y, im.w, im.h) == pytest.approx((63.08, 31.55, 138.55, 116.73), abs=0.05)
    assert im.rotation == 0.0
    info = pdf_info(im.data)
    assert (info.pages[0].width, info.pages[0].height) == (254.0, 214.0)
    assert (im.w, im.h) == pytest.approx((254 / K, 214 / K), abs=0.01)  # 1 PDF pt = 1 canvas unit


def test_exif_rotated_photo(samples):
    page = _read(samples, "Test9").pages[3]
    assert len(page.images) == 1
    im = page.images[0]
    assert im.fmt == "jpeg" and im.data[:3] == b"\xff\xd8\xff"
    assert jpeg_exif_orientation(im.data) == 6
    assert im.rotation == 90.0
    # the box is the displayed (portrait) size: 360 x 640 canvas units
    assert (im.x, im.y, im.w, im.h) == pytest.approx((516.006 / K, 91.923 / K, 360 / K, 640 / K), abs=0.01)
    assert (im.x, im.y, im.x + im.w, im.y + im.h) == pytest.approx((281.46, 50.14, 477.82, 399.23), abs=0.01)
    # the raw JPEG is landscape 3840 x 2160 (SOF0)
    pos = 2
    while im.data[pos + 1] != 0xC0:
        pos += 2 + struct.unpack(">H", im.data[pos + 2:pos + 4])[0]
    height, width = struct.unpack(">HH", im.data[pos + 5:pos + 9])
    assert (width, height) == (3840, 2160)
    # the older samples' photos carry no EXIF rotation and the reader does not guess from aspect
    ex3 = read_goodnotes((samples.repo("parser-for-goodnotes") / "assets" / "ex3.goodnotes").read_bytes())
    assert all(im.rotation == 0.0 for im in ex3.pages[0].images)


def test_exif_rotated_photo_survives_a_goodnotes_round_trip(samples):
    """The writer keeps the displayed box the reader produced: the portrait photo of Test9
    stays portrait (it used to come back turned to 349 x 196 pt)."""
    from gnnote.convert import Options
    from gnnote.goodnotes.writer import write_goodnotes

    page = _read(samples, "Test9").pages[3]
    im = page.images[0]
    back = read_goodnotes(write_goodnotes(Document(title="EXIF", pages=[page]), Options()))
    again = back.pages[0].images[0]
    assert again.rotation == im.rotation == 90.0 and again.data == im.data
    assert (again.x, again.y, again.w, again.h) == pytest.approx((im.x, im.y, im.w, im.h), abs=0.01)
    assert again.h > again.w  # portrait, as GoodNotes shows it


def _jpeg_with_exif(tiff: bytes) -> bytes:
    app1 = b"Exif\x00\x00" + tiff
    return b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xd9"


def _tiff(endian: str, orientation: int, typ: int = 3) -> bytes:
    order = b"II" if endian == "<" else b"MM"
    header = order + struct.pack(endian + "HI", 42, 8)
    entry = struct.pack(endian + "HHI", 0x0112, typ, 1)
    entry += struct.pack(endian + "H", orientation) + b"\x00\x00" if typ == 3 else struct.pack(endian + "I", orientation)
    return header + struct.pack(endian + "H", 2) + struct.pack(endian + "HHII", 0x0100, 4, 1, 3840) + entry + b"\x00\x00\x00\x00"


def test_exif_orientation_parser():
    assert jpeg_exif_orientation(_jpeg_with_exif(_tiff("<", 6))) == 6
    assert jpeg_exif_orientation(_jpeg_with_exif(_tiff(">", 8))) == 8
    assert jpeg_exif_orientation(_jpeg_with_exif(_tiff(">", 3, typ=4))) == 3
    assert jpeg_exif_orientation(_jpeg_with_exif(_tiff("<", 9))) == 1  # out of range
    assert jpeg_exif_orientation(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00H\x00H\x00\x00\xff\xd9") == 1
    assert jpeg_exif_orientation(b"\xff\xd8\xff\xe1\x00\x08Exif") == 1  # truncated
    assert jpeg_exif_orientation(b"\x89PNG") == 1
    assert jpeg_exif_orientation(b"") == 1


# --------------------------------------------------------------------------- strokes


def test_grouped_highlighters_land_inside_the_page(samples):
    """33 short-format highlighters of the pasted group carry a #6 translation of
    (-488.6, 627.5) canvas; without it they would sit 266 pt right and 342 pt up."""
    page = _read(samples, "Test9").pages[1]
    group = [s for s in page.strokes if s.kind == "highlighter" and round(s.width, 4) in (3.3194, 5.394)]
    assert len(group) == 33
    for s in group:
        assert s.color[3] == pytest.approx(0.5)
        assert s.pen == "ballpoint"
        for p in s.points:
            assert 0 <= p.x <= page.width and 0 <= p.y <= page.height
    x0 = min(p.x for s in group for p in s.points)
    y0 = min(p.y for s in group for p in s.points)
    x1 = max(p.x for s in group for p in s.points)
    y1 = max(p.y for s in group for p in s.points)
    assert 50 < x0 < 60 and 660 < y0 < 670 and 140 < x1 < 150 and 750 < y1 < 760
    # every highlighter of the file uses W/2 pt: 6.6388, 10.788, 12, 36 -> 3.3194, 5.394, 6, 18
    assert {round(s.width, 4) for s in page.strokes if s.kind == "highlighter"} == {3.3194, 5.394, 6.0, 18.0}


def test_multi_block_ribbon_strokes_decode(samples):
    for name, prefix, index, points in (("Test6", "A37FF8B6", 4, 1439), ("Test7", "308EE367", 3, 1699)):
        path = _sample(samples, name)
        frames = []
        for _tomb, kind, body in _content_records(path, prefix):
            if kind == 7:
                frame = bytes(protobuf.get(body, 2).value)
                if len(applelz4.split_blocks(frame)) > 2:
                    frames.append(frame)
        assert len(frames) == 1, name
        blocks = applelz4.split_blocks(frames[0])
        assert [b[0] for b in blocks[:-1]] == [b"bv41"] * (len(blocks) - 1) and blocks[-1][0] == b"bv4$"
        assert len(blocks) - 1 >= 4
        page = _read(samples, name).pages[index]
        big = max(page.strokes, key=lambda s: len(s.points))
        assert len(big.points) == points
        assert big.pen == "fountain" and big.controls is None
        assert len({round(p.width, 3) for p in big.points}) > 1  # per-point radii kept


def test_stored_block_stroke_is_read(samples):
    """Test9 p2 record with a 'bv4-' (stored) frame: 8-byte header, a 7-float highlighter dot."""
    path = _sample(samples, "Test9")
    stored = [body for _tomb, kind, body in _content_records(path, "D298AC1F")
              if kind == 7 and bytes(protobuf.get(body, 2).value)[:4] == b"bv4-"]
    assert len(stored) == 1
    body = stored[0]
    frame = bytes(protobuf.get(body, 2).value)
    assert len(frame) == 127
    geo = tpl.decode(applelz4.decompress(frame))
    assert isinstance(geo, tpl.FlatStroke) and len(geo.quads) == 3
    offset = _msg(body, 6)
    dx, dy = _f32(offset, 1), _f32(offset, 2)
    assert (dx, dy) == pytest.approx((-488.608, 627.542), abs=0.001)
    page = _read(samples, "Test9").pages[1]
    scale = page.width / 1091.3466796875
    expected = ((geo.start[0] + dx) * scale, (geo.start[1] + dy) * scale)
    hits = [s for s in page.strokes
            if abs(s.points[0].x - expected[0]) < 0.01 and abs(s.points[0].y - expected[1]) < 0.01]
    assert len(hits) == 1
    s = hits[0]
    assert s.kind == "highlighter" and len(s.points) == 4 and s.is_bezier
    assert s.width == pytest.approx(geo.width / 2.0)


def test_markers_via_ribbon_flags(samples):
    page = _read(samples, "Test9").pages[1]
    markers = [s for s in page.strokes if s.pen == "marker"]
    assert len(markers) == 3
    for s in markers:
        assert s.width == pytest.approx(18.0 / K, abs=0.01)  # r = 9 canvas units, 2 r = 9.82 pt
        assert s.color[:3] == pytest.approx((0.925, 0.325, 0.294), abs=0.01)
    # the same strokes without their #20 tag are still markers: the flags 4/5 ribbon layout
    path = _sample(samples, "Test9")
    tagged = [body for _tomb, kind, body in _content_records(path, "D298AC1F")
              if kind == 7 and protobuf.get(body, 20) is not None and protobuf.get(body, 20).value]
    assert len(tagged) == 3
    for body in tagged:
        stripped = protobuf.encode_message([f for f in body if f.number != 20])
        doc = _one_stroke_document(stripped)
        assert len(doc.pages[0].strokes) == 1
        assert doc.pages[0].strokes[0].pen == "marker"
        assert doc.pages[0].strokes[0].width == pytest.approx(18.0 / K, abs=0.01)


def _one_stroke_document(stroke_body: bytes) -> Document:
    """A minimal container (no events) holding one stroke record pair on one page."""
    pb = protobuf
    meta = (pb.field_bytes(1, "E0000000-0000-4000-8000-000000000001") + pb.field_message(2, pb.field_varint(1, 1))
            + pb.field_varint(8, 1) + pb.field_varint(9, 1) + pb.field_varint(14, 5381) + pb.field_varint(16, 24))
    content = pb.field_message(7, stroke_body)
    notes = pb.encode_records([meta, content])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("index.notes.pb", pb.encode_records([pb.field_bytes(1, "50000000-0000-4000-8000-000000000001")
                                                        + pb.field_bytes(2, "notes/50000000-0000-4000-8000-000000000001")]))
        z.writestr("notes/50000000-0000-4000-8000-000000000001", notes)
    return read_goodnotes(buf.getvalue())


def test_shape_geometry_forms(samples):
    """#9.#2 is one quadratic Bezier (control point elevated to cubic handles); #9.#1 with two
    identical points is a dot; first == last is a closed polygon."""
    page = _read(samples, "Test9").pages[1]
    scale = page.width / 1091.3466796875
    # rec 65: P0 (795.69, 1263.84), C (793.76, 1277.88), P1 (800.68, 1290.97) canvas
    p0 = (795.69 * scale, 1263.84 * scale)
    quad = [s for s in page.strokes if len(s.points) == 2 and s.is_bezier
            and abs(s.points[0].x - p0[0]) < 0.02 and abs(s.points[0].y - p0[1]) < 0.02]
    assert len(quad) == 1
    s = quad[0]
    c1, c2 = s.controls[0]
    # both handles must come from the same quadratic control point
    qx1 = s.points[0].x + 1.5 * (c1.x - s.points[0].x)
    qx2 = s.points[1].x + 1.5 * (c2.x - s.points[1].x)
    qy1 = s.points[0].y + 1.5 * (c1.y - s.points[0].y)
    qy2 = s.points[1].y + 1.5 * (c2.y - s.points[1].y)
    assert (qx1, qy1) == pytest.approx((qx2, qy2), abs=1e-6)
    assert (qx1, qy1) == pytest.approx((793.76 * scale, 1277.88 * scale), abs=0.02)
    assert (s.points[1].x, s.points[1].y) == pytest.approx((800.68 * scale, 1290.97 * scale), abs=0.02)
    # the curve's midpoint is where the export's InkList midpoint sits (434.17, 696.90), not at C
    mid = (0.125 * s.points[0].x + 0.375 * c1.x + 0.375 * c2.x + 0.125 * s.points[1].x,
           0.125 * s.points[0].y + 0.375 * c1.y + 0.375 * c2.y + 0.125 * s.points[1].y)
    assert mid == pytest.approx((434.17, 696.90), abs=0.05)
    # rec 209: a dot stored as two identical points (795.2, 1282.6)
    dots = [s for s in page.strokes if len(s.points) == 2 and s.pen != "pencil"
            and (s.points[0].x, s.points[0].y) == (s.points[1].x, s.points[1].y)]
    assert len(dots) == 1
    assert (dots[0].points[0].x, dots[0].points[0].y) == pytest.approx((795.2 * scale, 1282.6 * scale), abs=0.05)
    # rec 39: a closed 4-point polygon (triangle, first == last) with straight sides: its
    # cubic handles sit at the thirds of every segment so no Bezier fit can bow them
    closed = [s for s in page.strokes if len(s.points) == 4
              and (s.points[0].x, s.points[0].y) == (s.points[-1].x, s.points[-1].y)]
    assert len(closed) == 1
    assert_straight_handles(closed[0])
    # Test7 p2 red triangle: 4 points closed, exported as (29.12, 213.97)-(197.99, 397.60) with W/2 = 2.73
    t7 = _read(samples, "Test7").pages[1]
    tri = [s for s in t7.strokes if s.kind != "fill" and len(s.points) == 4
           and (s.points[0].x, s.points[0].y) == (s.points[-1].x, s.points[-1].y)]
    assert len(tri) == 1
    assert tuple(round(v, 2) for v in tri[0].bbox()) == (29.12, 213.97, 197.99, 397.6)
    assert_straight_handles(tri[0])


def assert_straight_handles(s) -> None:
    assert s.is_bezier and len(s.controls) == len(s.points) - 1
    for (p, q), (c1, c2) in zip(zip(s.points, s.points[1:]), s.controls):
        assert (c1.x, c1.y) == pytest.approx((p.x + (q.x - p.x) / 3, p.y + (q.y - p.y) / 3), abs=1e-6)
        assert (c2.x, c2.y) == pytest.approx((p.x + 2 * (q.x - p.x) / 3, p.y + 2 * (q.y - p.y) / 3), abs=1e-6)


def test_stroke_widths_and_counts(samples):
    """Per page: strokes == live auto-shapes + live ink sub-paths, nothing dropped but tombstones."""
    expected = {"Test6": [13, 1, 3, 0, 1], "Test7": [14, 4, 1, 5], "Test8": [2, 2, 1, 1],
                "Test9": [0, 123, 0, 11, 0, 22, 6]}
    for name, counts in expected.items():
        doc = _read(samples, name)
        assert [len([s for s in p.strokes if s.kind != "fill"]) for p in doc.pages] == counts, name
        path = _sample(samples, name)
        members = _notes_members(path)
        tombstoned = live = 0
        for member in members.values():
            if not member:
                continue
            tomb = False
            for fields in (protobuf.decode_message(r) for r in protobuf.decode_records(member)):
                if _is_metadata(fields):
                    f3 = protobuf.get(fields, 3)
                    tomb = f3 is not None and f3.value == 1
                    continue
                if fields[0].number == 7:
                    if tomb:
                        tombstoned += 1
                    else:
                        live += 1
                tomb = False
        assert {"Test6": (22, 4), "Test7": (36, 12), "Test8": (10, 4), "Test9": (186, 24)}[name] == (live + tombstoned, tombstoned)
    # Test7 p1: six line shapes with W = 1.559 .. 24 -> W/2 pt
    lines = [s for s in _read(samples, "Test7").pages[0].strokes if len(s.points) == 2]
    assert sorted(round(s.width, 3) for s in lines) == [0.78, 2.339, 2.728, 5.197, 6.0, 12.0]


# --------------------------------------------------------------------------- raster comparison


def _export_ink_bbox(pymupdf, export_page, paper_page, threshold: int = 16):
    """Pixels (72 dpi) where the export differs from the bound paper PDF -> bbox in pt."""
    a = export_page.get_pixmap(alpha=False)
    b = paper_page.get_pixmap(alpha=False)
    if (a.width, a.height) != (b.width, b.height):
        return None
    sa, sb = a.samples, b.samples
    xs: List[int] = []
    ys: List[int] = []
    for y in range(a.height):
        row = y * a.stride
        for x in range(a.width):
            i = row + x * 3
            if max(abs(sa[i] - sb[i]), abs(sa[i + 1] - sb[i + 1]), abs(sa[i + 2] - sb[i + 2])) > threshold:
                xs.append(x)
                ys.append(y)
    if not xs:
        return ()
    return (float(min(xs)), float(min(ys)), float(max(xs) + 1), float(max(ys) + 1))


def test_ink_bounding_boxes_against_the_exports(samples):
    """Rasterise each export page and its paper PDF; the pixels that differ are the content.
    Non-pencil strokes + images must lie inside that area (pencil rasters can be too faint to
    detect) and the area inside strokes + images + text frames, each within 12 pt (pencil
    rasters blur up to 9 pt; text frames exceed the glyphs).  Only stock-paper pages are
    compared: GoodNotes redraws inserted user PDFs differently."""
    pymupdf = pytest.importorskip("pymupdf")
    tolerance = 12.0
    compared = 0
    for name in NEW_SAMPLES:
        doc = _read(samples, name)
        export = pymupdf.open(str(_export(samples, name)))
        assert export.page_count == len(doc.pages)
        for i, page in enumerate(doc.pages):
            if not page.template_is_builtin or page.background is None:
                continue
            paper = pymupdf.open(stream=doc.pdfs[page.background.pdf_id], filetype="pdf")
            theirs = _export_ink_bbox(pymupdf, export[i], paper[page.background.page_index])
            if theirs is None:
                continue
            ink = _bbox(page, with_texts=False, with_pencil=False)
            everything = _bbox(page, with_texts=True)
            if theirs == ():
                assert ink is None, (name, i)
                continue
            assert everything is not None, (name, i, theirs)
            if ink is not None:
                assert ink[0] >= theirs[0] - tolerance and ink[1] >= theirs[1] - tolerance, (name, i, ink, theirs)
                assert ink[2] <= theirs[2] + tolerance and ink[3] <= theirs[3] + tolerance, (name, i, ink, theirs)
            assert theirs[0] >= everything[0] - tolerance and theirs[1] >= everything[1] - tolerance, (name, i, everything, theirs)
            assert theirs[2] <= everything[2] + tolerance and theirs[3] <= everything[3] + tolerance, (name, i, everything, theirs)
            compared += 1
    assert compared >= 15
