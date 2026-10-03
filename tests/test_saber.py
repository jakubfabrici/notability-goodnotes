"""Saber codec (``gnnote.saber``): the BSON codec against Saber's own files, synthetic round
trips of every element type, Saber's example notes (external test data, pinned in
``tests/conftest.py``) and inkterop's CC0 fixture, conversions of the GoodNotes and Notability
samples to ``.sba`` and back, hardening and the registry, sniffing and CLI plumbing."""
from __future__ import annotations

import io
import json
import math
import random
import struct
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from gnnote import formats
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.goodnotes.writer import write_goodnotes
from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from gnnote.notability.reader import read_note
from gnnote.notability.writer import write_note
from gnnote.saber import PT_PER_UNIT
from gnnote.saber import bson
from gnnote.saber import reader as sr
from gnnote.saber.reader import read_saber, width_for_pressure
from gnnote.saber.writer import build_note, pressure_for_width, write_saber
from tests.sample_docs import BACKGROUND_PDF, STICKER_PDF, full_document, png, stats

TOL = 0.01  # pt


def _note(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return bson.decode(zf.read("main.sbn2"))


def _sba(main: bytes, assets: Dict[int, bytes] = {}) -> bytes:  # noqa: B006 - read only
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.sbn2", main)
        for index, data in assets.items():
            zf.writestr(f"main.sbn2.{index}", data)
    return buf.getvalue()


def _with_siblings(path: Path) -> bytes:
    """A .sbn2 sample plus its sibling asset files (``<name>.sbn2.N``) as one .sba archive."""
    siblings = {int(p.name.rsplit(".", 1)[1]): p.read_bytes() for p in path.parent.glob(path.name + ".*")
                if p.name.rsplit(".", 1)[1].isdigit()}
    return _sba(path.read_bytes(), siblings) if siblings else path.read_bytes()


# --------------------------------------------------------------------------- BSON


def test_bson_round_trips_sabers_own_files_byte_for_byte(samples):
    files = [p for p in samples.saber_files() if p.suffix == ".sbn2"]
    for path in files:
        data = path.read_bytes()
        assert bson.encode(bson.decode(data)) == data, path.name
    fixture = samples.inkterop_fixture("saber", "saber-mac-pens-text.sba")
    with zipfile.ZipFile(fixture) as zf:
        main_bytes = zf.read("main.sbn2")
    assert bson.encode(bson.decode(main_bytes)) == main_bytes


def test_bson_types_and_unsigned_colours():
    doc = {"d": 1.5, "s": "äb\ud800", "o": {"x": None}, "a": [1, True], "b": b"\x00\x01", "i": 7,
           "big": 0xFF000000, "neg": -(2 ** 40)}
    raw = bson.encode(doc)
    back = bson.decode(raw)
    assert back == {"d": 1.5, "s": "äb�", "o": {"x": None}, "a": [1, True], "b": b"\x00\x01", "i": 7,
                    "big": 0xFF000000, "neg": -(2 ** 40)}
    assert b"\x12big\x00" in raw and b"\x10i\x00" in raw  # int64 above 2**31, int32 below
    with pytest.raises(bson.BsonError):
        bson.encode({"x": float("nan")})
    with pytest.raises(bson.BsonError):
        bson.encode({"x": object()})


@pytest.mark.parametrize("data", [b"", b"\x05\x00\x00\x00", b"\x10\x00\x00\x00\x10v\x00\x01\x00\x00\x00\x00",
                                  b"\x0d\x00\x00\x00\x02s\x00\xff\xff\xff\x7fx\x00",
                                  b"\x0c\x00\x00\x00\x99v\x00\x01\x00\x00\x00\x00",
                                  b"\xff\xff\xff\x7f\x00"])
def test_bson_rejects_malformed_documents(data: bytes):
    with pytest.raises(bson.BsonError):
        bson.decode(data)


def test_bson_limits(monkeypatch):
    nested: dict = {}
    for _ in range(40):
        nested = {"n": nested}
    with pytest.raises(bson.BsonError, match="nested deeper"):
        bson.decode(bson.encode(nested))
    monkeypatch.setattr(bson, "MAX_VALUES", 10)
    with pytest.raises(bson.BsonError, match="more than 10 values"):
        bson.decode(bson.encode({"a": list(range(20))}))


# --------------------------------------------------------------------------- width law


def test_width_law_round_trips():
    for p in (0.0, 0.2, 0.5, 0.77, 1.0):
        w = width_for_pressure(10.0, 0.5, p)
        assert w == pytest.approx(10.0 * (0.5 + p))
        assert pressure_for_width(w, 10.0) == pytest.approx(p)
    assert width_for_pressure(10.0, 0.5, float("nan")) == pytest.approx(10.0)
    assert pressure_for_width(100.0, 10.0) == 1.0 and pressure_for_width(0.0, 10.0) == 0.0


# --------------------------------------------------------------------------- synthetic round trip


def test_every_element_type_round_trips():
    doc = full_document()
    data = write_saber(doc)
    back = read_saber(data)
    assert len(back.pages) == 4
    # plain pages are written 1000 units wide and read at 0.595 pt per unit; PDF pages keep their size
    f = (1000.0 * PT_PER_UNIT) / 455.04
    assert (back.pages[0].width, back.pages[0].height) == pytest.approx((595.0, 588.45 * f))
    assert (back.pages[1].width, back.pages[1].height) == pytest.approx((612.0, 792.0))
    p1, b1 = doc.pages[0], back.pages[0]
    assert [s.kind for s in b1.strokes] == [s.kind for s in p1.strokes if s.kind != "fill"]
    pressure = b1.strokes[0]
    src = p1.strokes[0].points
    assert [c for p in pressure.points for c in (p.x, p.y)] == pytest.approx(
        [c * f for p in src for c in (p.x, p.y)], abs=TOL)
    assert [p.width for p in pressure.points] == pytest.approx([p.width * f for p in src], rel=1e-5)
    assert pressure.color == pytest.approx(p1.strokes[0].color, abs=1 / 255) and pressure.pen == "fountain"
    constant = b1.strokes[1]
    assert {round(p.width / f, 5) for p in constant.points} == {1.0} and constant.pen == "ballpoint"
    highlighter = b1.strokes[2]
    assert highlighter.kind == "highlighter" and highlighter.color[3] == pytest.approx(0.5, abs=1 / 255)
    assert b1.strokes[4].points[0].x == pytest.approx(120 * f, abs=TOL) and len(b1.strokes[4].points) == 1
    assert b1.strokes[5].pen == "pencil"
    # images: boxes kept, the rotation dropped, the PDF sticker as a Saber PDF image
    assert [im.fmt for im in b1.images] == ["png", "jpeg", "pdf"]
    for got, want in zip(b1.images, p1.images):
        assert (got.x, got.y, got.w, got.h) == pytest.approx((want.x * f, want.y * f, want.w * f, want.h * f), abs=TOL)
        assert got.data == want.data and got.rotation == 0.0
    assert b1.images[2].data == STICKER_PDF
    # text boxes become the page's Quill text: one box, both texts, bold run kept
    (text,) = b1.texts
    assert text.text == "Hello <ink> & text\nTurned\nand bold"
    assert any(r.bold and r.text == "Turned\nand bold" for r in text.runs)
    assert text.y == pytest.approx(480 * f, abs=0.8 * text.size)
    # PDF backgrounds keep their page; the PDF is stored once
    assert [p.background.page_index if p.background else None for p in back.pages] == [None, 1, 0, None]
    assert back.pdfs[back.pages[1].background.pdf_id] == BACKGROUND_PDF
    assert len({p.background.pdf_id for p in back.pages if p.background}) == 1
    joined = " | ".join(doc.warnings)
    for needle in ("2 shape fills were dropped", "1 image rotations", "page text", "one paper pattern"):
        assert needle in joined


def test_note_shape_mirrors_the_app():
    note = _note(write_saber(full_document()))
    assert list(note) == ["v", "ni", "b", "p", "l", "lt", "z", "c"]
    assert note["v"] == 19 and note["ni"] == 5 and note["p"] == "lined" and note["b"] is None
    assert isinstance(note["l"], int) and isinstance(note["lt"], int)
    page = note["z"][0]
    assert list(page) == ["w", "h", "s", "i", "q"] and page["w"] == 1000.0 and isinstance(page["h"], float)
    assert list(note["z"][3]) == ["w", "h"]  # no empty lists, as the app writes
    stroke = page["s"][0]
    assert list(stroke) == ["shape", "p", "i", "ty", "pe", "c", "s", "sl", "sp"]
    assert stroke["ty"] == "fountainPen" and stroke["pe"] is True and all(len(p) == 12 for p in stroke["p"])
    assert page["s"][1]["ty"] == "ballpointPen" and all(len(p) == 8 for p in page["s"][1]["p"])
    pencil = page["s"][5]
    assert pencil["ty"] == "Pencil" and (pencil["sl"], pencil["ts"], pencil["te"]) == (0.1, 1.0, 1.0)
    assert all(s["c"] >= 0 for s in page["s"])  # unsigned ARGB
    assert page["s"][1]["c"] == 0xFF000000
    image = page["i"][0]
    assert list(image)[:9] == ["id", "e", "i", "v", "f", "x", "y", "w", "h"] and image["e"] == ".png"
    assert isinstance(image["a"], int) and isinstance(image["id"], int)
    assert page["i"][2]["e"] == ".pdf" and page["i"][2]["pdfi"] == 0 and page["i"][2]["nw"] == 40.0
    bg = note["z"][1]["b"]
    assert bg["e"] == ".pdf" and bg["pdfi"] == 1 and (bg["nw"], bg["nh"]) == (612.0, 792.0)
    assert note["z"][2]["b"]["a"] == bg["a"]  # one asset for the shared PDF
    raw = write_saber(full_document())
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        assert zf.namelist() == ["main.sbn2", "main.sbn2.0", "main.sbn2.1", "main.sbn2.2", "main.sbn2.3"]
        assert zf.read("main.sbn2.3") == BACKGROUND_PDF
        main_bytes = zf.read("main.sbn2")
    assert b"\x12c\x00" in main_bytes  # 0xFF...... colours as int64, like Dart
    options = Options()
    options.random_seed = 3  # type: ignore[attr-defined]
    assert write_saber(full_document(), options) == write_saber(full_document(), options)


def test_wide_width_ranges_keep_the_widest_point():
    widths = [0.2, 1.0, 2.0, 1.0, 0.2]
    stroke = Stroke([Point(10 * i, 0, w) for i, w in enumerate(widths)], width=1.0)
    doc = Document(pages=[Page(1000 * PT_PER_UNIT, 800, strokes=[stroke])])
    back = read_saber(write_saber(doc)).pages[0].strokes[0]
    got = [p.width for p in back.points]
    assert got[2] == pytest.approx(2.0, rel=1e-5) and min(got) == pytest.approx(2.0 / 3.0, rel=1e-5)
    assert any("3:1" in w for w in doc.warnings)


def test_text_lines_follow_the_boxes():
    boxes = [TextBox(30, 300, 200, 20, "lower", size=12.0), TextBox(30, 100, 200, 40, "upper\nline", size=12.0,
                                                                    runs=[TextRun("upper\nline", italic=True)])]
    note = _note(write_saber(Document(pages=[Page(595.0, 833.0, texts=boxes)])))
    ops = note["z"][0]["q"]
    text = "".join(op["insert"] for op in ops)
    assert text.endswith("\n") and text.index("upper") < text.index("lower")
    line_height = note["l"]
    assert text[:text.index("upper")].count("\n") == round(100 / PT_PER_UNIT / line_height - 1.2)
    assert any(op.get("attributes") == {"italic": True} for op in ops)


def test_invalid_geometry_is_skipped():
    page = Page(float("nan"), 100, strokes=[Stroke([Point(float("inf"), 0, 1)]), Stroke([Point(0, 0, 1)])],
                images=[Image(0, 0, 10, 10, b"GIF89a....")])
    doc = Document(pages=[page])
    back = read_saber(write_saber(doc))
    assert len(back.pages[0].strokes) == 1 and not back.pages[0].images
    joined = " | ".join(doc.warnings)
    assert "invalid coordinates" in joined and "no valid size" in joined and "neither PNG" in joined


# --------------------------------------------------------------------------- reader specifics


def test_reader_defaults_offsets_shapes_and_tools():
    point = struct.pack("<3f", 10.0, 20.0, 1.0)
    note = {"v": 19, "ni": 0, "b": 0xFFFFF0C0, "p": "cornell", "l": 40, "z": [{
        "w": 1000.0, "h": 1400.0,
        "s": [{"shape": None, "p": [point, struct.pack("<2f", 30.0, 40.0)], "i": 0, "ty": "Pen"},
              {"shape": "circle", "cx": 100.0, "cy": 100.0, "r": 50.0, "i": 0, "ty": "ShapePen", "pe": False,
               "c": 0xFFFF0000, "s": 4.0},
              {"shape": "rect", "rl": 10.0, "rt": 10.0, "rw": 100.0, "rh": 50.0, "i": 0, "ty": "ShapePen", "s": 2.0},
              {"shape": None, "p": [point], "i": 0, "ty": "Eraser"},
              {"shape": None, "p": [point], "i": 0, "ty": "Highlighter", "pe": False, "c": 0x64FFFF00, "s": 50.0}],
        "q": [{"insert": "\n\nTitle\n", "attributes": {"header": 1}}, {"insert": "body"},
              {"insert": {"formula": "x"}}, {"insert": "\n"}]}], "c": 0}
    doc = read_saber(bson.encode(note))
    page = doc.pages[0]
    assert (page.width, page.height) == pytest.approx((595.0, 833.0))
    pen, circle, rect, highlighter = page.strokes
    # size defaults to 10, pressure 1.0 -> 15 units; the pressure-less point keeps the size
    assert [p.width for p in pen.points] == pytest.approx([15 * PT_PER_UNIT, 10 * PT_PER_UNIT])
    assert pen.color == (0.0, 0.0, 0.0, 1.0) and pen.pen == "fountain"
    assert circle.controls is not None and len(circle.points) == 5 and circle.color == pytest.approx((1, 0, 0, 1))
    xs = [p.x for p in circle.points]
    assert (min(xs), max(xs)) == pytest.approx((50 * PT_PER_UNIT, 150 * PT_PER_UNIT))
    assert rect.controls is not None and [(p.x, p.y) for p in rect.points][2] == pytest.approx(
        (110 * PT_PER_UNIT, 60 * PT_PER_UNIT))
    assert highlighter.kind == "highlighter" and highlighter.color[3] == pytest.approx(0x64 / 255)
    assert page.paper == "lined" and page.template_is_builtin  # cornell -> lined, yellowish paper PDF
    text = page.texts[0]
    assert text.text == "Title\nbody" and text.runs[0].bold
    assert text.y == pytest.approx((1.2 + 2) * 40 * PT_PER_UNIT) and text.size == pytest.approx(40 * PT_PER_UNIT)
    joined = " | ".join(doc.warnings)
    for needle in ("eraser", "embedded objects", "Cornell", "page text"):
        assert needle in joined


def test_reader_backgrounds_and_box_fit():
    pdf_note = {"v": 19, "z": [{"w": 1000.0, "h": 1294.1, "b": {"id": 0, "e": ".pdf", "i": 0, "f": 1, "x": 0.0,
                                                               "y": 0.0, "w": 1000.0, "h": 1294.1, "nw": 612.0,
                                                               "nh": 792.0, "a": 0, "pdfi": 1}}]}
    doc = read_saber(_sba(bson.encode(pdf_note), {0: BACKGROUND_PDF}))
    page = doc.pages[0]
    assert page.background == PdfBackground("saber-asset-0.pdf", 1) and page.width == pytest.approx(612.0)
    image = png(200, 100)
    cover = {"v": 19, "z": [{"w": 1000.0, "h": 1000.0, "b": {"e": ".png", "f": 2, "x": 0.0, "y": 0.0, "w": 9.0,
                                                            "h": 9.0, "a": 0}}]}
    page = read_saber(_sba(bson.encode(cover), {0: image})).pages[0]
    bg = page.images[0]
    assert (bg.w, bg.h) == pytest.approx((2000 * PT_PER_UNIT, 1000 * PT_PER_UNIT))  # cover: height fills
    assert bg.x == pytest.approx(-500 * PT_PER_UNIT)
    bare = read_saber(bson.encode(cover))  # a bare .sbn2: the asset is a sibling file
    assert not bare.pages[0].images and any("export the note as .sba" in w for w in bare.warnings)


def test_reader_legacy_json_formats():
    old = [{"i": 1, "p": [{"x": 1, "y": 2, "p": 0.5}, {"x": 3, "y": 4}], "ty": "Pen"}]
    doc = read_saber(json.dumps(old).encode())
    assert len(doc.pages) == 2 and len(doc.pages[1].strokes) == 1
    v6 = {"v": 6, "s": [{"i": 0, "p": [{"x": 0, "y": 0, "p": 0.5}], "s": 3.0}],
          "i": [{"i": 0, "x": 0, "y": 0, "w": 10, "h": 10, "b": list(png())}], "z": [{"w": 1000, "h": 1400}]}
    doc = read_saber(json.dumps(v6).encode())
    assert len(doc.pages) == 1 and len(doc.pages[0].strokes) == 1 and doc.pages[0].images[0].data == png()
    with pytest.raises(ValueError, match="not a Saber"):
        read_saber(json.dumps([1, 2, 3]).encode())
    with pytest.raises(ValueError, match="not a Saber"):
        read_saber(json.dumps({"type": "excalidraw"}).encode())


# --------------------------------------------------------------------------- Saber's example notes

# folder/name -> (pages, strokes, highlighters, images, page texts) with the sibling assets
# zipped in (as Saber's own .sba export holds them); counted from the pinned commit's BSON.
SABER_EXPECTED: Dict[str, Tuple[int, int, int, int, int]] = {
    "demo_notes/Annotate images and diagrams.sbn2": (2, 6, 0, 1, 0),
    "demo_notes/CM Welcome Back.sbn2": (2, 205, 5, 0, 0),
    "demo_notes/Coding review 1.sbn2": (2, 314, 7, 0, 0),
    "demo_notes/Courses Year 3 Sem 2 Revision.sbn2": (2, 575, 0, 1, 0),
    "demo_notes/DistSystems MapReduce.sbn2": (2, 468, 0, 0, 0),
    "demo_notes/Golden ratio.sbn2": (2, 125, 0, 0, 0),
    "demo_notes/HG Week 6.sbn2": (2, 396, 0, 0, 0),
    "demo_notes/HG Week 7.sbn2": (2, 267, 2, 0, 0),
    "demo_notes/Import PDFs.sbn2": (2, 0, 0, 1, 0),
    "demo_notes/Metric Spaces Week 1.sbn2": (2, 454, 4, 0, 0),
    "demo_notes/Oatmeal mugcake recipe.sbn2": (2, 2, 0, 0, 1),
    "demo_notes/Third year projects.sbn2": (2, 305, 5, 0, 0),
    "demo_notes/Topology week 1.sbn2": (2, 366, 2, 0, 0),
    "demo_notes/Uni Y3 course unit selection.sbn2": (2, 0, 0, 0, 1),
    "demo_notes/You can type notes too!.sbn2": (3, 0, 0, 0, 2),
    "sbn_examples/v11_image_svg.sbn": (1, 0, 0, 0, 0),
    "sbn_examples/v17_squiggles.sbn2": (2, 9, 0, 0, 0),
    "sbn_examples/v18_highlighter.sbn2": (2, 10, 10, 0, 0),
    "sbn_examples/v18_pencil.sbn2": (2, 23, 0, 0, 1),
    "sbn_examples/v19_pens.sbn2": (2, 16, 0, 0, 1),
    "sbn_examples/v19_quill_languages.sbn2": (3, 0, 0, 0, 1),
    "sbn_examples/v19_separate_assets.sbn2": (2, 0, 0, 1, 0),
    "sbn_examples/v19_shape_strokes.sbn2": (2, 8, 4, 0, 0),
    "sbn_examples/v6_stress_test_179.sbn": (2, 776, 0, 2, 1),
    "sbn_examples/v9_low_point_count.sbn": (1, 3, 0, 0, 0),
    "sbn_examples/v9_quill.sbn": (1, 0, 0, 0, 1),
    "sbn_examples/v9_single_stroke.sbn": (2, 1, 0, 0, 0),
}


def _summary(doc: Document) -> Tuple[int, int, int, int, int]:
    strokes = [s for p in doc.pages for s in p.strokes]
    return (len(doc.pages), len(strokes), sum(1 for s in strokes if s.kind == "highlighter"),
            sum(len(p.images) for p in doc.pages), sum(len(p.texts) for p in doc.pages))


def test_saber_example_notes(samples):
    files = samples.saber_files()
    pinned = samples.at_pinned_commit("saber") is not False
    checked = 0
    for path in files:
        key = f"{path.parent.name}/{path.name}"
        doc = read_saber(_with_siblings(path))
        expected = SABER_EXPECTED.get(key) if pinned else None
        if expected is not None:
            assert _summary(doc) == expected, key
            checked += 1
        for page in doc.pages:
            assert page.width > 0 and page.height > 0
            for stroke in page.strokes:
                assert stroke.points and all(math.isfinite(p.x) and p.width > 0 for p in stroke.points), key
        back = read_saber(write_saber(doc))
        assert _summary(back)[:4] == _summary(doc)[:4], key
    if pinned:
        assert checked == len(SABER_EXPECTED)


def test_saber_example_details(samples):
    root = samples.repo("saber") / "test"
    pens = read_saber((root / "sbn_examples" / "v19_pens.sbn2").read_bytes())
    first = pens.pages[0].strokes[0]  # "Pen", size 19, pressure points
    raw = bson.decode((root / "sbn_examples" / "v19_pens.sbn2").read_bytes())["z"][0]["s"][0]
    x, y, p = struct.unpack("<3f", raw["p"][0])
    assert (first.points[0].x, first.points[0].y) == pytest.approx((x * PT_PER_UNIT, y * PT_PER_UNIT))
    assert first.points[0].width == pytest.approx(19 * (0.5 + p) * PT_PER_UNIT, rel=1e-6)
    assert pens.pages[0].paper == "grid"
    pdfs = read_saber(_with_siblings(root / "demo_notes" / "Import PDFs.sbn2"))
    assert pdfs.pages[0].images[0].fmt == "png" and pdfs.pages[0].height == pytest.approx(1413.4453781512605 * 0.595)


def test_inkterop_fixture(samples):
    doc = read_saber(samples.inkterop_fixture("saber", "saber-mac-pens-text.sba").read_bytes())
    assert _summary(doc) == (2, 4, 1, 0, 1)
    strokes = doc.pages[0].strokes
    assert any(len(s.points) == 1 for s in strokes)  # the dot
    assert any(s.pen == "pencil" for s in strokes)
    back = read_saber(write_saber(doc))
    assert _summary(back) == _summary(doc)


# --------------------------------------------------------------------------- GoodNotes / Notability samples


def _ink_count(doc: Document) -> List[int]:
    return [sum(1 for s in p.strokes if s.kind != "fill") for p in doc.pages]


def test_goodnotes_samples_to_saber_and_back(samples):
    for path in samples.goodnotes_files():
        source = read_goodnotes(path.read_bytes())
        sba = read_saber(write_saber(source))
        assert len(sba.pages) == len(source.pages), path.name
        assert _ink_count(sba) == _ink_count(source), path.name
        assert [len(p.images) for p in sba.pages] == [len(p.images) for p in source.pages], path.name
        assert [min(1, len(p.texts)) for p in sba.pages] == [min(1, len(p.texts)) for p in source.pages], path.name
        again = read_goodnotes(write_goodnotes(sba))
        assert _ink_count(again) == _ink_count(sba) and stats(again)[2] == stats(sba)[2], path.name


def test_notability_samples_to_saber_and_back(samples):
    for path in samples.note_files():
        source = read_note(path.read_bytes())
        sba = read_saber(write_saber(source))
        assert len(sba.pages) == len(source.pages), path.name
        assert _ink_count(sba) == _ink_count(source), path.name
        assert stats(sba)[2] == stats(source)[2], path.name
        again = read_note(write_note(sba))
        assert stats(again)[1:3] == stats(sba)[1:3], path.name


# --------------------------------------------------------------------------- hardening


def test_archive_limits(monkeypatch):
    data = write_saber(full_document())
    from gnnote import codecutil
    monkeypatch.setattr(codecutil, "MAX_TOTAL_BYTES", len(build_note(full_document())[0]) + 100)
    doc = read_saber(data)
    assert doc.pages and any("inflates above" in w for w in doc.warnings)
    monkeypatch.setattr(sr, "MAX_NOTE_BYTES", 100)
    with pytest.raises(ValueError, match="could not be read|larger than"):
        read_saber(data)


def test_point_and_page_limits(monkeypatch):
    monkeypatch.setattr(sr, "MAX_POINTS_PER_STROKE", 5)
    monkeypatch.setattr(sr, "MAX_TOTAL_POINTS", 12)
    monkeypatch.setattr(sr, "MAX_PAGES", 2)
    pts = [struct.pack("<2f", float(i), float(i)) for i in range(8)]
    note = {"v": 19, "z": [{"w": 1000.0, "h": 1400.0, "s": [{"p": pts, "ty": "Pen", "pe": False}] * 4}] * 3}
    doc = read_saber(bson.encode(note))
    assert len(doc.pages) == 2 and [len(s.points) for s in doc.pages[0].strokes] == [5, 5]
    joined = " | ".join(doc.warnings)
    assert "per stroke" in joined and "per file" in joined and "first 2 pages" in joined


def test_hostile_values_are_tolerated():
    note = {"v": 99, "p": 5, "l": -3, "z": [{"w": -1.0, "h": 1e308,
                                             "s": [{"p": [b"\x00", struct.pack("<2f", float("nan"), 1.0)],
                                                    "c": "red", "s": -4.0, "pe": "yes"}, "junk"],
                                             "i": [{"x": 1.0, "y": 1.0, "w": 5.0, "h": 5.0, "a": 99},
                                                   {"x": "a"}], "q": "text", "b": {"e": ".png"}}, 7]}
    doc = read_saber(bson.encode(note))
    assert len(doc.pages) == 2
    assert all(math.isfinite(p.width) for p in doc.pages)
    assert any("newer than" in w for w in doc.warnings)


def test_deeply_nested_json_is_a_value_error():
    with pytest.raises(ValueError):
        read_saber(b"[" * 100_000 + b"]" * 100_000)


@pytest.mark.parametrize("data", [b"", b"PK\x03\x04junk", b"\x00\x00\x00\x00", b"hello", b"{\"v\": 1",
                                  b"\x0c\x00\x00\x00\x99v\x00\x01\x00\x00\x00\x00"])
def test_not_a_saber_file(data: bytes):
    with pytest.raises(ValueError):
        read_saber(data)


def test_zip_without_a_note_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "x")
    with pytest.raises(ValueError, match="no .sbn2"):
        read_saber(buf.getvalue())


def test_fuzzed_files_raise_only_value_error():
    seeds = [write_saber(full_document()), build_note(full_document())[0],
             json.dumps({"v": 9, "z": [{"w": 1000, "h": 1400, "s": [{"p": [{"x": 1, "y": 2, "p": 0.3}]}]}]}).encode()]
    rng = random.Random(4321)
    for seed in seeds:
        for _ in range(60):
            data = bytearray(seed)
            for _ in range(rng.randint(1, 12)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            if rng.random() < 0.3:
                data = data[: rng.randrange(1, len(data))]
            try:
                doc = read_saber(bytes(data))
            except ValueError:
                continue
            assert isinstance(doc, Document)


# --------------------------------------------------------------------------- registry, sniffing, CLI


def test_registry_entry_and_sniffing(samples):
    fmt = formats.get("saber")
    assert (fmt.name, fmt.extension, fmt.input_extensions) == ("Saber", ".sba", (".sba", ".sbn2", ".sbn"))
    sba = write_saber(full_document())
    main_bytes = build_note(full_document())[0]
    legacy = json.dumps({"v": 9, "ni": 0, "z": [{"w": 1000, "h": 1400}]}).encode()
    for data in (sba, main_bytes, legacy):
        assert detect_format("renamed.bin", data) == "saber"
    assert detect_format("x.sbn", b"garbage") == "saber"
    assert not fmt.sniff(json.dumps({"type": "excalidraw", "version": 2}).encode(), None)
    note = write_note(Document(pages=[Page(100, 100)]))
    assert detect_format("x.note", note) == "notability"
    for path in samples.saber_files():
        assert detect_format("renamed.bin", path.read_bytes()) == "saber", path.name


def test_convert_api_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    note = write_note(full_document())
    result = convert(note, "Every.note", Options(target="saber"))
    assert result.filename == "Every.sba" and result.target_format == "saber"
    assert read_saber(result.data).pages
    assert main(["formats"]) == 0
    listed = capsys.readouterr().out
    assert "saber" in listed and ".sba, .sbn2, .sbn" in listed
    src = tmp_path / "Every.sba"
    src.write_bytes(write_saber(full_document()))
    assert main(["convert", str(src), "--to", "goodnotes"]) == 0
    assert "saber -> goodnotes" in capsys.readouterr().out
    back = read_goodnotes((tmp_path / "Every.goodnotes").read_bytes())
    assert len(back.pages) == 4
    assert main(["convert", str(src), "--to", "xournalpp", "-o", str(tmp_path / "x.xopp")]) == 0
    assert (tmp_path / "x.xopp").is_file()
