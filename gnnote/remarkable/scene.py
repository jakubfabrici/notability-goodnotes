"""reMarkable v6 page files (``reMarkable .lines file, version=6``): bytes -> scene.

The format (``docs/remarkable.md`` section 2; the block layout follows rmscene, MIT):

* a 43-byte ASCII header, then blocks ``u32 length, u8 0, u8 min_version, u8 version,
  u8 type`` followed by ``length`` bytes;
* inside a block, values are tagged with a varuint ``index << 4 | type`` (type ``0xF`` a CRDT
  id = ``u8`` + varuint, ``0xC`` a sub-block = ``u32`` length + content, ``0x8`` / ``0x4`` /
  ``0x1`` eight, four or one bytes); "last-write-wins" values are sub-blocks holding a
  timestamp id (1) and the value (2);
* the scene is a tree of groups (layers, and groups anchored to typed text) whose children
  are CRDT sequences: every item names its left and right neighbour, and the order is the
  topological order of those links (concurrent inserts: higher author id first).

Blocks read: scene tree nodes (0x01), node properties (0x02: label, visibility, text anchor),
glyph ranges (0x03: text highlights), group items (0x04), lines (0x05: tool, colour,
thickness, points, RGBA), root text (0x07), scene info (0x0D: paper size).  Unknown blocks,
unknown fields and bytes a block leaves unread (newer firmware) are skipped.  Only
:class:`SceneError` (a :class:`ValueError`) escapes, for data that is not a v6 page.
"""
from __future__ import annotations

import heapq
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from ..readutil import PointBudget

__all__ = ["SceneError", "Scene", "Group", "Item", "Line", "LinePoint", "Glyph", "RootText", "Paragraph",
           "parse_scene", "crdt_order", "HEADER_PREFIX", "HEADER_V6", "ROOT_ID", "END_ID"]

HEADER_PREFIX = b"reMarkable .lines file, version="
HEADER_V6 = b"reMarkable .lines file, version=6          "
CrdtId = Tuple[int, int]
END_ID: CrdtId = (0, 0)
ROOT_ID: CrdtId = (0, 1)

TAG_ID, TAG_LENGTH4, TAG_BYTE8, TAG_BYTE4, TAG_BYTE1 = 0xF, 0xC, 0x8, 0x4, 0x1
BLOCK_SCENE_TREE, BLOCK_TREE_NODE, BLOCK_GLYPH, BLOCK_GROUP_ITEM, BLOCK_LINE = 0x01, 0x02, 0x03, 0x04, 0x05
BLOCK_TEXT_ITEM, BLOCK_ROOT_TEXT, BLOCK_TOMBSTONE, BLOCK_SCENE_INFO = 0x06, 0x07, 0x08, 0x0D

MAX_BLOCKS = 2_000_000
MAX_ITEMS_PER_GROUP = 2_000_000
MAX_POINTS_PER_LINE = 1_000_000
MAX_TEXT_CHARS = 100_000  # characters of typed text per page (deleted runs included)
MAX_GLYPH_RECTS = 100_000
MAX_DEPTH = 64


class SceneError(ValueError):
    """The data is not a reMarkable v6 page."""


class _Bad(Exception):
    """A block or value does not have the expected shape (the block is skipped)."""


@dataclass
class LinePoint:
    x: float
    y: float
    width: float  # rendered width in canvas units (the stored value / 4 for version-2 points)
    pressure: float  # 0..1


@dataclass
class Line:
    tool: int
    color: int
    thickness: float
    points: List[LinePoint]
    rgba: Optional[Tuple[int, int, int, int]] = None


@dataclass
class Glyph:
    """A text highlight: the highlighted text and its rectangles (canvas units)."""

    color: int
    text: str
    rects: List[Tuple[float, float, float, float]]
    rgba: Optional[Tuple[int, int, int, int]] = None


@dataclass
class Item:
    item_id: CrdtId
    left_id: CrdtId
    right_id: CrdtId
    deleted: int
    value: Union[Line, Glyph, CrdtId, None]  # a child group is referenced by its node id


@dataclass
class Group:
    node_id: CrdtId
    label: str = ""
    visible: bool = True
    anchor_id: Optional[CrdtId] = None
    anchor_type: Optional[int] = None
    anchor_threshold: Optional[float] = None
    anchor_origin_x: Optional[float] = None
    items: Dict[CrdtId, Item] = field(default_factory=dict)


@dataclass
class Paragraph:
    start_id: CrdtId  # the newline that opens it, END_ID for the first paragraph
    style: int
    text: str
    char_ids: List[CrdtId]
    runs: List[Tuple[str, bool, bool]]  # (text, bold, italic)


@dataclass
class RootText:
    items: List[Tuple[CrdtId, CrdtId, CrdtId, int, Union[str, int]]]  # (id, left, right, deleted, value)
    styles: Dict[CrdtId, int]
    pos_x: float
    pos_y: float
    width: float

    def paragraphs(self) -> List[Paragraph]:
        """The text in reading order, split at newlines (CRDT order, deleted characters kept as ids)."""
        chars: Dict[CrdtId, Tuple[CrdtId, CrdtId, Union[str, int]]] = {}
        budget = MAX_TEXT_CHARS
        for item_id, left, right, deleted, value in self.items:
            if deleted > 0:
                values: List[Union[str, int]] = [""] * min(deleted, budget)
            elif isinstance(value, int):
                values = [value]
            else:
                values = list(value[:budget])
            budget -= len(values)
            if not values:
                continue
            a, b = item_id
            ids = [(a, b + i) for i in range(len(values))]
            for i, (cid, v) in enumerate(zip(ids, values)):
                chars[cid] = (left if i == 0 else ids[i - 1], right if i == len(ids) - 1 else ids[i + 1], v)
            if budget <= 0:
                break
        order = crdt_order({cid: (left, right) for cid, (left, right, _v) in chars.items()})
        paragraphs: List[Paragraph] = []
        bold = italic = False
        i = 0
        while i < len(order):
            # A paragraph opens at a newline (its id carries the paragraph style); only the
            # text before the first newline belongs to the END_ID paragraph.
            start = END_ID
            if chars[order[i]][2] == "\n":
                start = order[i]
                i += 1
            current = Paragraph(start, self.styles.get(start, 1), "", [], [])
            while i < len(order) and chars[order[i]][2] != "\n":
                cid = order[i]
                value = chars[cid][2]
                i += 1
                if isinstance(value, int):
                    if value in (1, 2):
                        bold = value == 1
                    elif value in (3, 4):
                        italic = value == 3
                    continue
                current.char_ids.append(cid)
                if value:
                    current.text += value
                    if current.runs and current.runs[-1][1:] == (bold, italic):
                        text, b, it = current.runs[-1]
                        current.runs[-1] = (text + value, b, it)
                    else:
                        current.runs.append((value, bold, italic))
            paragraphs.append(current)
        return paragraphs


@dataclass
class Scene:
    groups: Dict[CrdtId, Group] = field(default_factory=dict)
    parents: Dict[CrdtId, CrdtId] = field(default_factory=dict)
    text: Optional[RootText] = None
    paper_size: Optional[Tuple[int, int]] = None
    unreadable_blocks: int = 0
    skipped_points: bool = False  # the point budget refused a line

    def group(self, node_id: CrdtId) -> Group:
        found = self.groups.get(node_id)
        if found is None:
            found = self.groups[node_id] = Group(node_id)
        return found

    def walk(self) -> Iterator[Tuple[Union[Line, Glyph], List[Group]]]:
        """Lines and glyphs in drawing order with the chain of groups above them (root first).

        Only groups reachable from the root through live group items are visited: a layer
        whose group item was deleted is gone, whatever its lines still hold."""
        seen = set()

        def visit(group: Group, chain: List[Group]) -> Iterator[Tuple[Union[Line, Glyph], List[Group]]]:
            if group.node_id in seen or len(chain) > MAX_DEPTH:
                return
            seen.add(group.node_id)
            items = group.items
            for item_id in crdt_order({k: (v.left_id, v.right_id) for k, v in items.items()}):
                value = items[item_id].value
                if isinstance(value, (Line, Glyph)):
                    yield value, chain
                elif isinstance(value, tuple) and value in self.groups:
                    child = self.groups[value]
                    yield from visit(child, chain + [child])

        yield from visit(self.group(ROOT_ID), [])


def crdt_order(links: Dict[CrdtId, Tuple[CrdtId, CrdtId]]) -> List[CrdtId]:
    """Order CRDT sequence items by their left/right links (Kahn's algorithm).

    A link to an unknown item or to ``END_ID`` points at the start (left) or the end (right).
    Ready items come out by (higher author first, then lower counter).  Items caught in a
    cycle are appended in id order instead of being lost.
    """
    if not links:
        return []
    start, end = (-1, -1), (-2, -2)  # sentinels no real id can equal
    indegree: Dict[CrdtId, int] = {start: 0, end: 0}
    after: Dict[CrdtId, List[CrdtId]] = {start: [], end: []}
    for item in links:
        indegree.setdefault(item, 0)
        after.setdefault(item, [])
    for item, (left, right) in links.items():
        lo = left if left in links and left != item else start
        hi = right if right in links and right != item else end
        after[lo].append(item)
        indegree[item] += 1
        after[item].append(hi)
        indegree[hi] += 1

    def key(node: CrdtId) -> Tuple[int, int, int]:
        if node == start:
            return (0, 0, 0)
        if node == end:
            return (2, 0, 0)
        return (1, -node[0], node[1])

    ready = [(key(n), n) for n, d in indegree.items() if d == 0]
    heapq.heapify(ready)
    out: List[CrdtId] = []
    while ready:
        _, node = heapq.heappop(ready)
        if node == end:
            break
        if node != start:
            out.append(node)
        for nxt in after[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready, (key(nxt), nxt))
    if len(out) < len(links):
        placed = set(out)
        out += sorted(n for n in links if n not in placed)
    return out


# --------------------------------------------------------------------------- byte reading


class _Stream:
    __slots__ = ("buf", "pos", "end")

    def __init__(self, buf: bytes, pos: int, end: int):
        self.buf = buf
        self.pos = pos
        self.end = end

    def need(self, n: int) -> None:
        if n < 0 or self.pos + n > self.end:
            raise _Bad()

    def unpack(self, fmt: str, size: int) -> Tuple[Any, ...]:
        self.need(size)
        values = struct.unpack_from(fmt, self.buf, self.pos)
        self.pos += size
        return values

    def u8(self) -> int:
        self.need(1)
        self.pos += 1
        return self.buf[self.pos - 1]

    def varuint(self) -> int:
        result = shift = 0
        while True:
            byte = self.u8()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 63:
                raise _Bad()

    def crdt_id(self) -> CrdtId:
        return self.u8(), self.varuint()

    def remaining(self) -> int:
        return self.end - self.pos

    def peek_tag(self) -> Optional[Tuple[int, int]]:
        if self.pos >= self.end:
            return None
        saved = self.pos
        try:
            x = self.varuint()
        except _Bad:
            return None
        finally:
            self.pos = saved
        return x >> 4, x & 0xF

    def tag(self, index: int, kind: int) -> None:
        x = self.varuint()
        if x >> 4 != index or x & 0xF != kind:
            raise _Bad()

    def has(self, index: int, kind: int) -> bool:
        return self.peek_tag() == (index, kind)

    def skip_field(self) -> None:
        x = self.varuint()
        kind = x & 0xF
        if kind == TAG_ID:
            self.crdt_id()
        elif kind == TAG_LENGTH4:
            n = self.unpack("<I", 4)[0]
            self.need(n)
            self.pos += n
        elif kind in (TAG_BYTE8, TAG_BYTE4, TAG_BYTE1):
            self.need(kind)
            self.pos += kind
        else:
            raise _Bad()

    def sub(self, index: int) -> "_Stream":
        """Consume a tagged sub-block header; returns a stream over its content (the caller
        continues after it with :meth:`after`)."""
        self.tag(index, TAG_LENGTH4)
        n = self.unpack("<I", 4)[0]
        self.need(n)
        return _Stream(self.buf, self.pos, self.pos + n)

    def after(self, sub: "_Stream") -> None:
        self.pos = sub.end

    # tagged values
    def read_id(self, index: int) -> CrdtId:
        self.tag(index, TAG_ID)
        return self.crdt_id()

    def read_int(self, index: int) -> int:
        self.tag(index, TAG_BYTE4)
        return self.unpack("<I", 4)[0]

    def read_float(self, index: int) -> float:
        self.tag(index, TAG_BYTE4)
        return self.unpack("<f", 4)[0]

    def read_double(self, index: int) -> float:
        self.tag(index, TAG_BYTE8)
        return self.unpack("<d", 8)[0]

    def read_byte(self, index: int) -> int:
        self.tag(index, TAG_BYTE1)
        return self.u8()

    def read_string(self, index: int) -> str:
        sub = self.sub(index)
        n = sub.varuint()
        sub.u8()  # "is ascii" flag
        sub.need(n)
        text = sub.buf[sub.pos:sub.pos + n].decode("utf-8", "replace")
        self.after(sub)
        return text

    def lww(self, index: int, reader: str) -> Any:
        sub = self.sub(index)
        sub.read_id(1)
        value = getattr(sub, reader)(2)
        self.after(sub)
        return value


# --------------------------------------------------------------------------- blocks


def _scene_tree(s: _Stream, scene: Scene) -> None:
    tree_id = s.read_id(1)
    s.read_id(2)
    s.read_byte(3)
    sub = s.sub(4)
    parent = sub.read_id(1)
    scene.group(tree_id)
    scene.parents.setdefault(tree_id, parent)


def _tree_node(s: _Stream, scene: Scene) -> None:
    group = scene.group(s.read_id(1))
    group.label = s.lww(2, "read_string")
    group.visible = bool(s.lww(3, "read_byte"))
    while s.remaining() > 0:
        tag = s.peek_tag()
        if tag == (7, TAG_LENGTH4):
            group.anchor_id = s.lww(7, "read_id")
        elif tag == (8, TAG_LENGTH4):
            group.anchor_type = s.lww(8, "read_byte")
        elif tag == (9, TAG_LENGTH4):
            group.anchor_threshold = s.lww(9, "read_float")
        elif tag == (10, TAG_LENGTH4):
            group.anchor_origin_x = s.lww(10, "read_float")
        else:
            s.skip_field()


def _points(sub: _Stream, version: int, budget: Optional[PointBudget], scene: Scene) -> List[LinePoint]:
    size = 24 if version == 1 else 14
    n = sub.remaining() // size
    if sub.remaining() % size or n > MAX_POINTS_PER_LINE:
        raise _Bad()
    if budget is not None and not budget.take(n):
        scene.skipped_points = True
        return []
    out: List[LinePoint] = []
    buf, pos = sub.buf, sub.pos
    if version == 1:
        for x, y, _speed, _direction, width, pressure in struct.iter_unpack("<6f", buf[pos:pos + n * size]):
            out.append(LinePoint(x, y, width, pressure))
    else:
        for x, y, _speed, width, _direction, pressure in struct.iter_unpack("<ffHHBB", buf[pos:pos + n * size]):
            out.append(LinePoint(x, y, width / 4.0, pressure / 255.0))
    return out


def _rgba(s: _Stream, index: int) -> Optional[Tuple[int, int, int, int]]:
    """An RGBA colour stored as a little-endian u32 (B, G, R, A in memory), when present."""
    while s.remaining() > 0:
        tag = s.peek_tag()
        if tag == (index, TAG_BYTE4):
            packed = s.read_int(index)
            return (packed >> 16) & 255, (packed >> 8) & 255, packed & 255, (packed >> 24) & 255
        if tag is None:
            return None
        s.skip_field()
    return None


def _line(s: _Stream, version: int, budget: Optional[PointBudget], scene: Scene) -> Line:
    tool = s.read_int(1)
    color = s.read_int(2)
    thickness = s.read_double(3)
    s.read_float(4)  # starting length (texture phase)
    sub = s.sub(5)
    points = _points(sub, version, budget, scene)
    s.after(sub)
    return Line(tool, color, thickness, points, _rgba(s, 8))


def _glyph(s: _Stream) -> Glyph:
    if s.has(2, TAG_BYTE4):
        s.read_int(2)
    if s.has(3, TAG_BYTE4):
        s.read_int(3)
    color = s.read_int(4)
    text = s.read_string(5)
    sub = s.sub(6)
    n = sub.varuint()
    if n > MAX_GLYPH_RECTS:
        raise _Bad()
    rects = [sub.unpack("<4d", 32) for _ in range(n)]
    s.after(sub)
    return Glyph(color, text, [tuple(r) for r in rects], _rgba(s, 10))  # type: ignore[misc]


def _item(s: _Stream, block_type: int, version: int, budget: Optional[PointBudget], scene: Scene) -> None:
    parent = s.read_id(1)
    item_id = s.read_id(2)
    left = s.read_id(3)
    right = s.read_id(4)
    deleted = s.read_int(5)
    value: Union[Line, Glyph, CrdtId, None] = None
    if s.has(6, TAG_LENGTH4):
        sub = s.sub(6)
        sub.u8()  # item type
        if block_type == BLOCK_LINE:
            value = _line(sub, version, budget, scene)
        elif block_type == BLOCK_GLYPH:
            value = _glyph(sub)
        elif block_type == BLOCK_GROUP_ITEM:
            value = sub.read_id(2)
        s.after(sub)
    group = scene.group(parent)
    if len(group.items) < MAX_ITEMS_PER_GROUP:
        group.items[item_id] = Item(item_id, left, right, deleted, value)


def _text_item(s: _Stream) -> Tuple[CrdtId, CrdtId, CrdtId, int, Union[str, int]]:
    sub = s.sub(0)
    item_id = sub.read_id(2)
    left = sub.read_id(3)
    right = sub.read_id(4)
    deleted = sub.read_int(5)
    value: Union[str, int] = ""
    if sub.has(6, TAG_LENGTH4):
        inner = sub.sub(6)
        n = inner.varuint()
        inner.u8()
        inner.need(n)
        value = inner.buf[inner.pos:inner.pos + n].decode("utf-8", "replace")
        inner.pos += n
        if inner.has(2, TAG_BYTE4):
            code = inner.read_int(2)
            value = code if not value else value
        sub.after(inner)
    s.after(sub)
    return item_id, left, right, deleted, value


def _root_text(s: _Stream, scene: Scene) -> None:
    s.read_id(1)
    body = s.sub(2)
    items_block = body.sub(1)
    inner = items_block.sub(1)
    count = inner.varuint()
    if count > MAX_TEXT_CHARS:
        raise _Bad()
    items = [_text_item(inner) for _ in range(count)]
    body.after(items_block)
    styles: Dict[CrdtId, int] = {}
    if body.has(2, TAG_LENGTH4):
        formats_block = body.sub(2)
        inner = formats_block.sub(1)
        count = inner.varuint()
        if count > MAX_TEXT_CHARS:
            raise _Bad()
        for _ in range(count):
            char_id = inner.crdt_id()
            inner.read_id(1)
            fmt = inner.sub(2)
            fmt.u8()
            styles[char_id] = fmt.u8()
            inner.after(fmt)
    s.after(body)
    pos = s.sub(3)
    pos_x, pos_y = pos.unpack("<dd", 16)
    s.after(pos)
    width = s.read_float(4)
    scene.text = RootText(items, styles, pos_x, pos_y, width)


def _scene_info(s: _Stream, scene: Scene) -> None:
    while s.remaining() > 0:
        tag = s.peek_tag()
        if tag == (5, TAG_LENGTH4):
            sub = s.sub(5)
            w, h = sub.unpack("<II", 8)
            s.after(sub)
            scene.paper_size = (w, h)
        else:
            s.skip_field()


def parse_scene(data: bytes, budget: Optional[PointBudget] = None) -> Scene:
    """Parse one v6 page.  :class:`SceneError` when ``data`` is not a v6 page; damaged or
    unknown blocks are skipped (counted in :attr:`Scene.unreadable_blocks`)."""
    data = bytes(data)
    if not data.startswith(HEADER_PREFIX):
        raise SceneError("not a reMarkable page (.rm) file")
    if not data.startswith(HEADER_V6):
        version = data[len(HEADER_PREFIX):len(HEADER_PREFIX) + 2].decode("ascii", "replace").strip()
        raise SceneError(f"reMarkable page format version {version} is not supported (only version 6)")
    scene = Scene()
    pos = len(HEADER_V6)
    blocks = 0
    while pos + 8 <= len(data) and blocks < MAX_BLOCKS:
        blocks += 1
        length, _zero, _min_version, version, block_type = struct.unpack_from("<IBBBB", data, pos)
        start, end = pos + 8, pos + 8 + length
        if end > len(data):
            scene.unreadable_blocks += 1
            break
        s = _Stream(data, start, end)
        try:
            if block_type == BLOCK_SCENE_TREE:
                _scene_tree(s, scene)
            elif block_type == BLOCK_TREE_NODE:
                _tree_node(s, scene)
            elif block_type in (BLOCK_GLYPH, BLOCK_GROUP_ITEM, BLOCK_LINE, BLOCK_TEXT_ITEM, BLOCK_TOMBSTONE):
                _item(s, block_type, version, budget, scene)
            elif block_type == BLOCK_ROOT_TEXT:
                _root_text(s, scene)
            elif block_type == BLOCK_SCENE_INFO:
                _scene_info(s, scene)
        except (_Bad, struct.error, OverflowError, MemoryError):
            scene.unreadable_blocks += 1
        pos = end
    return scene
