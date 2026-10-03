"""OneNote reader on synthetic sections (tests/onenote_builder.py): exact unit mapping of ink,
highlighter rules, nested containers, skipped pages, encryption, text, pictures, fragments,
page growth and damaged objects -- without the sample repositories."""
from __future__ import annotations

import struct
import zlib
from typing import Any, Dict, List, Optional, Tuple

import pytest

from gnnote.convert import Options, convert
from gnnote.notability.reader import read_note
from gnnote.onenote import read_onenote
from gnnote.onenote import schema as S
from tests.onenote_builder import Blob, Cell, Space, build_section, ink_dimensions, ink_path, utf16z

K = 72.0 / 2540.0  # pt per HIMETRIC


def _png(width: int = 4, height: int = 3) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _stroke(prefix: str, xs: List[int], ys: List[int], pressure: Optional[List[int]] = None,
            pen: Optional[Dict[int, Any]] = None, data_props: Optional[Dict[int, Any]] = None) -> Dict[str, Any]:
    pen_props: Dict[int, Any] = {S.INK_DIMENSIONS: ink_dimensions(pressure is not None), S.INK_WIDTH: 100.0,
                                 S.INK_HEIGHT: 100.0}
    pen_props.update(pen or {})
    channels = [xs, ys] + ([pressure] if pressure is not None else [])
    data: Dict[int, Any] = {S.INK_STROKES: [f"{prefix}stroke"]}
    data.update(data_props or {})
    return {
        f"{prefix}stroke": (S.INK_STROKE_NODE, {S.INK_PATH: ink_path(*channels), S.INK_STROKE_PROPERTIES: f"{prefix}pen"}),
        f"{prefix}pen": (S.STROKE_PROPERTIES_NODE, pen_props),
        f"{prefix}data": (S.INK_DATA_NODE, data),
    }


Page = Tuple[Dict[int, Any], Dict[str, Any], Dict[int, Any]]  # page props, objects, metadata props


def _section(pages: List[Page], title: str = "Synthetic", **kwargs: Any) -> bytes:
    spaces = {"section": Space(roots={1: "sec", 2: "secmeta"}, objects={
        "sec": (S.SECTION_NODE, {S.ELEMENT_CHILD_NODES: ["series"]}),
        "secmeta": (S.SECTION_METADATA, {S.SECTION_DISPLAY_NAME: utf16z(title + ".one")}),
        "series": (S.PAGE_SERIES_NODE, {S.CHILD_GRAPH_SPACE_ELEMENT_NODES: [Cell(f"page{i}") for i in range(len(pages))]}),
    })}
    for i, (page_props, objects, meta) in enumerate(pages):
        objs: Dict[str, Any] = {"manifest": (S.PAGE_MANIFEST_NODE, {S.CONTENT_CHILD_NODES: ["page"]}),
                                "page": (S.PAGE_NODE, page_props),
                                "meta": (S.PAGE_METADATA, {S.CACHED_TITLE_STRING: utf16z(f"Page {i}"), **meta})}
        objs.update(objects)
        spaces[f"page{i}"] = Space(roots={1: "manifest", 2: "meta"}, objects=objs)
    return build_section(spaces, "section", **kwargs)


def _ink_page(container: Dict[int, Any], **stroke: Any) -> Page:
    objects = _stroke("", **stroke)
    objects["ink"] = (S.INK_CONTAINER, {S.INK_DATA: "data", **container})
    return {S.ELEMENT_CHILD_NODES: ["ink"]}, objects, {}


def test_ink_units_offsets_scaling_pressure_and_colour() -> None:
    page = _ink_page({S.OFFSET_FROM_PARENT_HORIZ: 2.0, S.OFFSET_FROM_PARENT_VERT: 3.0, S.INK_SCALING_Y: 2.0},
                     xs=[0, 1000, 2000], ys=[0, 500, 1000], pressure=[0, 16384, 32767],
                     pen={S.INK_COLOR: 0x00FF8000, S.INK_TRANSPARENCY: 51})
    doc = read_onenote(_section([page]))
    assert doc.title == "Synthetic"
    (stroke,) = doc.pages[0].strokes
    assert [(p.x, p.y) for p in stroke.points] == pytest.approx(
        [(72 + x * K, 108 + 2 * y * K) for x, y in ((0, 0), (1000, 500), (2000, 1000))])
    assert stroke.width == pytest.approx(100 * K)
    factors = [p.width / stroke.width for p in stroke.points]
    assert factors == pytest.approx([0.25, 1.5 * 16384 / 32767 + 0.25, 1.75])
    assert stroke.color == pytest.approx((0.0, 128 / 255, 1.0, 1 - 51 / 255))  # COLORREF 0x00BBGGRR
    assert stroke.kind == "pen"
    assert any("pressure" in w for w in doc.warnings)


def test_ignore_pressure_and_missing_colour() -> None:
    page = _ink_page({}, xs=[0, 100], ys=[0, 0], pressure=[100, 30000], pen={S.INK_IGNORE_PRESSURE: True})
    (stroke,) = read_onenote(_section([page])).pages[0].strokes
    assert [p.width for p in stroke.points] == pytest.approx([100 * K, 100 * K])
    assert stroke.color == (0.0, 0.0, 0.0, 1.0)


@pytest.mark.parametrize("pen, kind", [
    ({S.INK_RASTER_OPERATION: 9, S.INK_PEN_TIP: 1, S.INK_TRANSPARENCY: 127}, "highlighter"),
    ({S.INK_RASTER_OPERATION: 9}, "highlighter"),
    ({S.INK_PEN_TIP: 1, S.INK_TRANSPARENCY: 127}, "highlighter"),  # no raster operation recorded
    ({S.INK_RASTER_OPERATION: 13, S.INK_PEN_TIP: 1, S.INK_TRANSPARENCY: 127}, "pen"),  # copy pen
    ({S.INK_PEN_TIP: 1}, "pen"),  # opaque rectangle tip
])
def test_highlighter_rules(pen: Dict[int, Any], kind: str) -> None:
    pen = {S.INK_WIDTH: 56.0, S.INK_HEIGHT: 400.0, **pen}
    (stroke,) = read_onenote(_section([_ink_page({}, xs=[0, 500], ys=[0, 0], pen=pen)])).pages[0].strokes
    assert stroke.kind == kind
    assert stroke.width == pytest.approx(400 * K)


def test_nested_containers_use_their_own_offsets() -> None:
    objects = {**_stroke("a", xs=[0, 10], ys=[0, 0]), **_stroke("b", xs=[0, 10], ys=[0, 0])}
    objects["outer"] = (S.INK_CONTAINER, {S.OFFSET_FROM_PARENT_HORIZ: 1.0, S.OFFSET_FROM_PARENT_VERT: 1.0,
                                          S.INK_DATA: "adata", S.CONTENT_CHILD_NODES: ["inner", "inherit"]})
    objects["inner"] = (S.INK_CONTAINER, {S.OFFSET_FROM_PARENT_HORIZ: 5.0, S.OFFSET_FROM_PARENT_VERT: 6.0,
                                          S.INK_DATA: "bdata"})
    objects["inherit"] = (S.INK_CONTAINER, {S.INK_DATA: "bdata"})
    doc = read_onenote(_section([({S.ELEMENT_CHILD_NODES: ["outer"]}, objects, {})]))
    starts = [(s.points[0].x, s.points[0].y) for s in doc.pages[0].strokes]
    assert starts == [(36.0, 36.0), (180.0, 216.0), (36.0, 36.0)]


def test_conflict_and_deleted_pages_are_skipped() -> None:
    normal = _ink_page({}, xs=[0, 10], ys=[0, 10])
    conflict = ({**normal[0], S.IS_CONFLICT_PAGE: True}, normal[1], {})
    deleted = (normal[0], normal[1], {S.IS_DELETED_GRAPH_SPACE_CONTENT: b"\x01"})
    doc = read_onenote(_section([normal, conflict, deleted]))
    assert len(doc.pages) == 1
    assert any("1 conflict page" in w for w in doc.warnings)
    assert any("1 page(s) marked as deleted" in w for w in doc.warnings)


def test_encrypted_section_is_refused_before_decoding() -> None:
    data = _section([_ink_page({}, xs=[0, 10], ys=[0, 10])], extra_root_roles={"section": {3: "sec"}})
    with pytest.raises(ValueError, match="password protected"):
        read_onenote(data)


def test_text_outline_with_runs_and_a_picture() -> None:
    text = "Hello bold world"
    objects: Dict[str, Any] = {
        "outline": (S.OUTLINE_NODE, {S.OFFSET_FROM_PARENT_HORIZ: 1.0, S.OFFSET_FROM_PARENT_VERT: 2.0,
                                     S.LAYOUT_MAX_WIDTH: 10.0, S.ELEMENT_CHILD_NODES: ["oe1", "oe2"]}),
        "oe1": (S.OUTLINE_ELEMENT_NODE, {S.CONTENT_CHILD_NODES: ["rt"]}),
        "rt": (S.RICH_TEXT_NODE, {S.RICH_EDIT_TEXT_UNICODE: utf16z(text),
                                  S.TEXT_RUN_INDEX: struct.pack("<II", 6, 10),
                                  S.TEXT_RUN_FORMATTING: ["plain", "bold", "plain"],
                                  S.PARAGRAPH_ALIGNMENT: 1}),
        "plain": (S.PARAGRAPH_STYLE, {S.FONT: utf16z("Calibri"), S.FONT_SIZE: 22}),
        "bold": (S.PARAGRAPH_STYLE, {S.FONT: utf16z("Calibri"), S.FONT_SIZE: 22, S.BOLD: True,
                                     S.FONT_COLOR: 0x000000FF}),
        "oe2": (S.OUTLINE_ELEMENT_NODE, {S.CONTENT_CHILD_NODES: ["img"]}),
        "img": (S.IMAGE_NODE, {S.PICTURE_CONTAINER_REF: "pic", S.PICTURE_WIDTH: 4.0, S.PICTURE_HEIGHT: 3.0}),
        "pic": Blob(_png()),
        "free": (S.IMAGE_NODE, {S.PICTURE_CONTAINER_REF: "pic2", S.OFFSET_FROM_PARENT_HORIZ: 10.0,
                                S.OFFSET_FROM_PARENT_VERT: 1.0}),
        "pic2": Blob(_png(8, 6), length_prefix=False),
        "table": (S.TABLE_NODE, {}),
        "oe3": (S.OUTLINE_ELEMENT_NODE, {S.CONTENT_CHILD_NODES: ["table"]}),
    }
    objects["outline"][1][S.ELEMENT_CHILD_NODES].append("oe3")
    doc = read_onenote(_section([({S.ELEMENT_CHILD_NODES: ["outline", "free"]}, objects, {})]))
    page = doc.pages[0]
    (box,) = page.texts
    assert (box.x, box.y, box.w) == (36.0, 72.0, 360.0) and box.text == text and box.align == "center"
    assert [(r.text, r.bold, r.size) for r in box.runs] == [("Hello ", False, 11.0), ("bold", True, 11.0),
                                                            (" world", False, 11.0)]
    assert box.runs[1].color == (1.0, 0.0, 0.0, 1.0)
    inline, free = page.images
    assert (inline.x, inline.w, inline.h) == (36.0, 144.0, 108.0) and inline.y == pytest.approx(72 + 11 * 1.25)
    assert inline.data == _png() and free.data == _png(8, 6)  # BLOB with and without length prefix
    assert (free.x, free.y, free.w, free.h) == (360.0, 36.0, 6.0, 4.5)  # 8 x 6 px at 96 dpi
    assert any("1 table(s) were dropped" in w for w in doc.warnings)


def test_fragmented_elements_give_the_same_document() -> None:
    page = _ink_page({S.OFFSET_FROM_PARENT_HORIZ: 1.0}, xs=list(range(0, 3000, 30)), ys=list(range(100)))
    whole = read_onenote(_section([page]))
    split = read_onenote(_section([page], fragment=97))
    assert [(p.x, p.y) for p in split.pages[0].strokes[0].points] == \
        [(p.x, p.y) for p in whole.pages[0].strokes[0].points]


def test_page_grows_with_content_and_negative_content_moves_onto_it() -> None:
    far = _ink_page({}, xs=[0, 60000], ys=[0, 80000])  # 1701 x 2268 pt
    doc = read_onenote(_section([far, _ink_page({S.OFFSET_FROM_PARENT_HORIZ: -10.0}, xs=[0, 100], ys=[0, 0]),
                                 ({S.PAGE_WIDTH: 30.0, S.PAGE_HEIGHT: 40.0}, {}, {})]))
    big, shifted, sized = doc.pages
    assert big.width == pytest.approx(60000 * K + 36) and big.height == pytest.approx(80000 * K + 36)
    assert shifted.strokes[0].points[0].x == pytest.approx(36.0)
    assert (sized.width, sized.height) == (30 * 36.0, 40 * 36.0)
    assert not any("much larger" in w for w in doc.warnings)
    huge = read_onenote(_section([_ink_page({}, xs=[0, 300000], ys=[0, 10])]))
    assert any("much larger" in w for w in huge.warnings)


def test_damaged_stroke_is_dropped_and_the_rest_kept() -> None:
    objects = {**_stroke("a", xs=[0, 10], ys=[0, 10]), **_stroke("b", xs=[0, 10], ys=[0, 10])}
    objects["bstroke"] = (S.INK_STROKE_NODE, {S.INK_PATH: b"\xc8\x01\x02", S.INK_STROKE_PROPERTIES: "bpen"})
    objects["a"] = (S.INK_CONTAINER, {S.INK_DATA: "adata"})
    objects["b"] = (S.INK_CONTAINER, {S.INK_DATA: "bdata"})
    doc = read_onenote(_section([({S.ELEMENT_CHILD_NODES: ["a", "b"]}, objects, {})]))
    assert len(doc.pages[0].strokes) == 1
    assert any("1 ink stroke(s) could not be decoded" in w for w in doc.warnings)


def test_inline_ink_words_flow_along_their_line() -> None:
    # "￼ ￼": two handwritten words separated by an ink space, written far away
    # (EmbeddedInkStart at 50 / 300 half-inches) and laid out in the outline at 1 / 1
    objects: Dict[str, Any] = {**_stroke("w1", xs=[63500, 64770], ys=[381000, 381635]),
                               **_stroke("w2", xs=[0, 1270], ys=[0, 635])}
    objects.update({
        "outline": (S.OUTLINE_NODE, {S.OFFSET_FROM_PARENT_HORIZ: 1.0, S.OFFSET_FROM_PARENT_VERT: 1.0,
                                     S.LAYOUT_MAX_WIDTH: 20.0, S.ELEMENT_CHILD_NODES: ["oe"]}),
        "oe": (S.OUTLINE_ELEMENT_NODE, {S.CONTENT_CHILD_NODES: ["rt"]}),
        "rt": (S.RICH_TEXT_NODE, {
            S.RICH_EDIT_TEXT_UNICODE: utf16z("￼￼￼"),
            S.TEXT_RUN_INDEX: struct.pack("<II", 1, 2),
            S.TEXT_RUN_DATA_OBJECT: ["w1data", None, "w2data"],
            S.TEXT_RUN_DATA: [
                {S.EMBEDDED_INK_START_X: 50.0, S.EMBEDDED_INK_START_Y: 300.0, S.EMBEDDED_INK_WIDTH: 1.0,
                 S.EMBEDDED_INK_HEIGHT: 0.5},
                {S.EMBEDDED_INK_SPACE_WIDTH: 0.5, S.EMBEDDED_INK_SPACE_HEIGHT: 0.5},
                {S.EMBEDDED_INK_START_X: 0.0, S.EMBEDDED_INK_START_Y: 0.0, S.EMBEDDED_INK_WIDTH: 1.0,
                 S.EMBEDDED_INK_HEIGHT: 0.5},
            ]}),
    })
    doc = read_onenote(_section([({S.ELEMENT_CHILD_NODES: ["outline"]}, objects, {})]))
    first, second = doc.pages[0].strokes
    assert (first.points[0].x, first.points[0].y) == pytest.approx((36.0, 36.0))
    assert (second.points[0].x, second.points[0].y) == pytest.approx((36.0 + 36 + 18, 36.0))
    assert doc.pages[0].texts == []  # placeholders only: no text box
    assert any("2 handwritten word(s)" in w for w in doc.warnings)
    assert doc.pages[0].height == pytest.approx(783.0)


def test_synthetic_section_converts_to_notability() -> None:
    data = _section([_ink_page({S.OFFSET_FROM_PARENT_HORIZ: 2.0}, xs=[0, 1000, 2000], ys=[0, 500, 0])])
    result = convert(data, "Synthetic.one", Options(target="notability"))
    assert (result.source_format, result.stats["strokes"]) == ("onenote", 1)
    back = read_note(result.data)
    assert back.title == "Synthetic" and sum(len(p.strokes) for p in back.pages) == 1
