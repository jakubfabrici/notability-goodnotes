"""The OneNote reader's budgets on crafted files that are small but expensive: millions of tiny
structures, one structure used from many places (object groups shared by many pages, one
stroke, picture or paragraph repeated).  Every such file must cost time and memory in
proportion to its size, end with a warning and keep what was read before the budget ran
out -- without the sample repositories."""
from __future__ import annotations

import struct
import time
import tracemalloc
import uuid
from typing import Any, Callable, Dict, Tuple

import pytest

from gnnote.model import Document
from gnnote.onenote import common, package, read_onenote, reader
from gnnote.onenote import schema as S
from tests.onenote_builder import Blob, Cell, Space, build_section, data_element, ink_dimensions, ink_path, utf16z
from tests.test_onenote_synthetic import _png, _section

MB = 1024 * 1024


def _measured(read: Callable[[], Any]) -> Tuple[Any, float, int]:
    tracemalloc.start()
    started = time.monotonic()
    try:
        try:
            result = read()
        except ValueError as exc:
            result = exc
    finally:
        elapsed = time.monotonic() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    return result, elapsed, peak


def _outline_page(n: int, text: str = "x") -> Dict[str, Any]:
    objects: Dict[str, Any] = {}
    for i in range(n):
        objects[f"o{i}"] = (S.OUTLINE_NODE, {S.ELEMENT_CHILD_NODES: [f"e{i}"]})
        objects[f"e{i}"] = (S.OUTLINE_ELEMENT_NODE, {S.CONTENT_CHILD_NODES: [f"t{i}"]})
        objects[f"t{i}"] = (S.RICH_TEXT_NODE, {S.RICH_EDIT_TEXT_UNICODE: utf16z(text)})
    return objects


def _shared_pages(page_objects: Dict[str, Any], copies: int) -> bytes:
    """A section whose first page holds ``page_objects`` and whose ``copies`` other pages reuse
    that page's object group (each page reads it again)."""
    objects = {"manifest": (S.PAGE_MANIFEST_NODE, {S.CONTENT_CHILD_NODES: ["page"]}),
               "page": (S.PAGE_NODE, {S.ELEMENT_CHILD_NODES: [n for n in page_objects if n.startswith("o")]})}
    objects.update(page_objects)
    pages = ["page"] + [f"copy{i}" for i in range(copies)]
    spaces = {"section": Space(roots={1: "sec"}, objects={
        "sec": (S.SECTION_NODE, {S.ELEMENT_CHILD_NODES: ["series"]}),
        "series": (S.PAGE_SERIES_NODE, {S.CHILD_GRAPH_SPACE_ELEMENT_NODES: [Cell(name) for name in pages]}),
    }), "page": Space(roots={1: "manifest"}, objects=objects)}
    for name in pages[1:]:
        spaces[name] = Space(roots={1: "manifest"}, share="page")
    return build_section(spaces, "section")


# --------------------------------------------------------------------------- the budget itself


def test_budget_counts_down_and_stays_exhausted() -> None:
    budget = common.Budget(3)
    budget.spend(2)
    with pytest.raises(common.TooLarge):
        budget.spend(2)
    assert budget.exhausted and isinstance(common.TooLarge("x"), ValueError)


# --------------------------------------------------------------------------- dense structures


def test_many_tiny_stream_objects_cost_no_memory() -> None:
    page: Dict[str, Any] = {"o0": (S.OUTLINE_NODE, {})}
    base = _section([({S.ELEMENT_CHILD_NODES: ["o0"]}, page, {})])
    tiny = struct.pack("<H", 0x3F << 3)  # an empty stream object: two bytes
    index = data_element(999_000, 0x01, [tiny * 100_000])  # a second storage index with 100 000 entries
    end = len(base) - 3  # the data element package's end marker
    assert base[end] == (0x15 << 2) | 1
    doc, elapsed, peak = _measured(lambda: read_onenote(base[:end] + index + base[end:]))
    assert isinstance(doc, Document) and len(doc.pages) == 1
    assert peak < 4 * MB, peak  # a tree of them took 18 MB
    assert elapsed < 10.0, elapsed


def _native(nodes: bytes) -> bytes:
    """A desktop-format file whose root file node list holds ``nodes`` (and nothing else)."""
    magic, footer = struct.pack("<Q", 0xA4567AB1F5F7F4C4), struct.pack("<Q", 0x8BC215C38233BA4B)
    fragment = magic + struct.pack("<II", 0x10, 0) + nodes + struct.pack("<QI", (1 << 64) - 1, 0) + footer
    head = bytearray(1024)
    head[0:16] = uuid.UUID("7B5C52E4-D88C-4DA7-AEB1-5378D02996D3").bytes_le
    head[48:64] = uuid.UUID("109ADD3F-911B-49F5-A5D0-1791EDC8AED8").bytes_le
    struct.pack_into("<QI", head, 160, (1 << 64) - 1, 0)  # no transaction log
    struct.pack_into("<QI", head, 172, 1024, len(fragment))
    struct.pack_into("<Q", head, 196, 1024 + len(fragment))
    return bytes(head) + fragment


def test_many_tiny_file_nodes_cost_no_memory() -> None:
    data = _native(struct.pack("<I", 0x3FE | 4 << 10) * 100_000)  # 100 000 empty file nodes
    error, elapsed, peak = _measured(lambda: read_onenote(data))
    assert isinstance(error, ValueError) and "no root object space" in str(error)
    assert peak < 4 * MB, peak  # a list of them took 18 MB
    assert elapsed < 10.0, elapsed


def test_page_beyond_its_memory_budget_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(package, "MAX_SPACE_ITEMS", 5_000)
    big = ({S.ELEMENT_CHILD_NODES: [f"o{i}" for i in range(2_000)]},
           {f"o{i}": (S.OUTLINE_NODE, {}) for i in range(2_000)}, {})
    small = ({S.ELEMENT_CHILD_NODES: ["t"]}, {"t": (S.OUTLINE_NODE, {})}, {})
    doc = read_onenote(_section([big, small]))
    assert len(doc.pages) == 1  # the small page survives the big one
    assert "Part of a OneNote page is too large or complex to read and was skipped" in doc.warnings


# --------------------------------------------------------------------------- repeated structures


def test_object_group_shared_by_many_pages_stops_at_the_work_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "MIN_WORK", 150_000)
    data = _shared_pages(_outline_page(500), copies=300)
    started = time.monotonic()
    doc = read_onenote(data)
    elapsed = time.monotonic() - started
    assert 1 <= len(doc.pages) < 301
    assert "The OneNote file is too large or complex to read completely; the rest was skipped" in doc.warnings
    assert elapsed < 10.0, elapsed


def test_one_stroke_used_many_times_is_capped_by_the_section_size() -> None:
    xs = list(range(0, 2000, 2))
    objects = {
        "ink": (S.INK_CONTAINER, {S.INK_DATA: "data"}),
        "data": (S.INK_DATA_NODE, {S.INK_STROKES: ["stroke"] * 2_000}),
        "stroke": (S.INK_STROKE_NODE, {S.INK_PATH: ink_path(xs, xs), S.INK_STROKE_PROPERTIES: "pen"}),
        "pen": (S.STROKE_PROPERTIES_NODE, {S.INK_DIMENSIONS: ink_dimensions()}),
    }
    data = _section([({S.ELEMENT_CHILD_NODES: ["ink"]}, objects, {})])
    doc = read_onenote(data)
    points = sum(len(s.points) for p in doc.pages for s in p.strokes)
    assert 0 < points <= len(data) // 2  # not 2 000 x 1 000
    assert any("more ink than gnnote reads" in w for w in doc.warnings)


def test_one_picture_used_many_times_is_capped_by_the_section_size() -> None:
    picture = _png(64, 64) + bytes(20_000)  # a PNG with trailing padding: 20 KB
    objects: Dict[str, Any] = {"pic": Blob(picture)}
    for i in range(200):
        objects[f"img{i}"] = (S.IMAGE_NODE, {S.PICTURE_CONTAINER_REF: "pic", S.PICTURE_WIDTH: 4.0,
                                            S.PICTURE_HEIGHT: 4.0})
    data = _section([({S.ELEMENT_CHILD_NODES: [f"img{i}" for i in range(200)]}, objects, {})])
    doc = read_onenote(data)
    images = doc.pages[0].images
    assert 1 <= len(images) <= len(data) // len(picture)
    assert all(image.data is images[0].data for image in images)  # one copy of the bytes
    assert any("pictures exceed" in w for w in doc.warnings)


def test_one_paragraph_read_on_many_pages_is_capped_by_the_section_size() -> None:
    data = _shared_pages(_outline_page(1, text="lorem ipsum " * 500), copies=100)  # 12 KB of text
    doc = read_onenote(data)
    text = sum(len(t.text) for p in doc.pages for t in p.texts)
    assert 0 < text * 2 <= len(data)  # not 101 copies
    assert any("paragraph(s) of text were dropped" in w for w in doc.warnings)
