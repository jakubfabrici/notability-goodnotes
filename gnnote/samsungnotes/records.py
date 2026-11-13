"""Samsung Notes ``.sdocx`` records (the S-Pen SDK binary layout), decoded defensively.

Every structure is little-endian.  The recurring primitives:

* *bitfield* -- a u8 byte count (0 to 4) and that many bytes, little-endian;
* *short string* -- a u16 count of UTF-16LE code units (object UUIDs: a u16 count of UTF-8
  bytes); *long string* -- the same with a u32 count;
* *timestamp* -- an i64 count of microseconds since 1970;
* *frame* -- a u32 size that includes the size field itself, a u16 data type, a u32 *flex
  offset* (from the frame's first byte), a property bitfield and a field bitfield.  Fixed
  fields follow; the optional fields the field bitfield announces start at the flex offset,
  in bit order.

An object (stroke, shape, text box, image, line, ...) is a chain of frames, one per class of
the app's object hierarchy: the *object base* (data type 0: UUID, modification time,
bounding rectangle, rotation, page index), then for a stroke the stroke frame (type 1); for
a shape the shape-base frame (type 6: line colour and style), the shape frame (type 7:
outline path, fill, text) and, for a text box or an image, a last frame of type 2 or 3; for a
line the shape-base frame and the line frame (type 8).

The layouts come from two MIT-licensed readers: cable729/inkterop
``core/src/inkterop/formats/sdocx.py`` (commit f16eb8d2a425637aab629e6cc90c00a18f17009f),
whose stroke, layer, page and string-registry decoding this module ports, and
squ1dd13/sdocx2pdf (Rust, commit 372ef723a6857b2b878ee9a1dfb9520a8f83edf8), the reference
for the object base, shapes, lines, images, text, the page's optional fields, the media
registry and the end tag.  Both were checked against the public sample notes
(``docs/samsungnotes.md``).

Defences: every read is checked against the bytes that are really there before anything is
allocated (:class:`SdocxError`, a :class:`ValueError`, otherwise); counts are bounded by the
remaining bytes; objects nested in text are limited to :data:`MAX_DEPTH` levels; one
document decodes at most :data:`MAX_OBJECTS` objects, and stroke events only while the ink
point budget allows them (:class:`Limits`): delta-compressed events take 8 bytes each and
compress very well, so they are charged before they are decoded.  Rich-text attributes are
kept only for the kinds gnnote uses and only up to :data:`MAX_TEXT_ATTRIBUTES` per text and
:data:`MAX_DOC_ATTRIBUTES` per document; the rest are stepped over without being stored.
Typed text is kept while the document's text allowance (:attr:`Limits.text_units`, at most
:data:`MAX_DOC_TEXT_UNITS` UTF-16 units) lasts: a text that does not fit keeps what does and
steps over the rest, counted in :attr:`TextBody.text_dropped` and :attr:`Limits.text_dropped`
(2 million repeated units compress to about 4 KB).
"""
from __future__ import annotations

import struct
import sys
from array import array
from dataclasses import dataclass, field
from itertools import accumulate, islice
from typing import Dict, List, Optional, Sequence, Tuple

from ..readutil import PointBudget

__all__ = [
    "SdocxError", "Cursor", "Limits", "ObjectBase", "Stroke", "Segment", "Shape", "Line",
    "TextSpan", "TextBody", "InlineObject", "OtherObject", "Layer", "PdfPlacement", "Page",
    "NoteDoc", "EndTag", "units_to_text", "fixed_point_delta", "fixed_small_delta", "parse_object", "parse_page",
    "parse_page_list", "parse_note", "parse_media_info", "parse_end_tag", "PAGE_END", "END_TAG_IDENTS",
    "MAX_DEPTH", "MAX_OBJECTS", "MAX_TEXT_CHARS", "MAX_DOC_TEXT_UNITS", "MAX_TEXT_ATTRIBUTES", "MAX_DOC_ATTRIBUTES",
    "MAX_FONT_NAME",
    "SPAN_COLOUR", "SPAN_SIZE", "SPAN_FONT", "SPAN_BOLD", "SPAN_ITALIC", "SPAN_UNDERLINE", "SPAN_LINK",
    "SPAN_BACKGROUND", "SPAN_STRIKE", "PARA_INDENT", "PARA_ALIGN", "PARA_LINE_SPACING", "PARA_BULLET",
    "PARA_SPACE_BEFORE", "PARA_SPACE_AFTER",
]

PAGE_END = b"Page for SAMSUNG S-Pen SDK"
END_TAG_IDENTS = (b"Document for S-Pen SDK", b"Document for SAMSUNG S-Pen SDK",
                  b"Document for S-Pen PAINTING SDK", b"Document for SAMSUNG S-Pen PAINTING SDK")
HASH_BYTES = 32
MAX_DEPTH = 4  # objects inside text inside objects ...; real notes use one level
MAX_OBJECTS = 2_000_000  # objects one document may decode (real notes: a few thousand per page)
MAX_TEXT_CHARS = 2_000_000  # UTF-16 units of one text
MAX_DOC_TEXT_UNITS = 8_000_000  # UTF-16 units of typed text one document keeps, all its texts together
MAX_PATH_SEGMENTS = 100_000
MAX_TEXT_ATTRIBUTES = 100_000  # character spans and paragraph attributes kept per text (samples: < 400)
MAX_DOC_ATTRIBUTES = 500_000  # ... kept by one document, all its texts together
MAX_FONT_NAME = 64  # UTF-16 units of a font name kept
ATTRIBUTE_DATA = 8  # bytes of an attribute's own data kept (a u32 / f32 value or two); font names: their units

# Rich-text attribute kinds gnnote uses (:class:`TextSpan`); records of other kinds are skipped.
SPAN_COLOUR, SPAN_SIZE, SPAN_FONT, SPAN_BOLD, SPAN_ITALIC, SPAN_UNDERLINE = 1, 3, 4, 5, 6, 7
SPAN_LINK, SPAN_BACKGROUND, SPAN_STRIKE = 9, 17, 20
PARA_INDENT, PARA_ALIGN, PARA_LINE_SPACING, PARA_BULLET, PARA_SPACE_BEFORE, PARA_SPACE_AFTER = 2, 3, 4, 5, 8, 9
SPAN_KINDS = frozenset((SPAN_COLOUR, SPAN_SIZE, SPAN_FONT, SPAN_BOLD, SPAN_ITALIC, SPAN_UNDERLINE, SPAN_LINK,
                        SPAN_BACKGROUND, SPAN_STRIKE))
PARA_KINDS = frozenset((PARA_INDENT, PARA_ALIGN, PARA_LINE_SPACING, PARA_BULLET, PARA_SPACE_BEFORE,
                        PARA_SPACE_AFTER))
OBJECT_TYPES = {1: "stroke", 2: "text box", 3: "image", 4: "container", 7: "shape", 8: "line",
                10: "voice recording", 11: "formula", 13: "web link", 14: "painting", 16: "video",
                17: "link", 19: "generic object", 20: "plot", 21: "maths object", 22: "table",
                23: "code block", 24: "attached file", 100: "stroke group"}

Rect = Tuple[float, float, float, float]  # x0, y0, x1, y1 in page units
XY = Tuple[float, float]


_U32 = next((code for code in ("I", "L") if array(code).itemsize == 4), None)
_UTF32 = "utf-32-le" if sys.byteorder == "little" else "utf-32-be"


class SdocxError(ValueError):
    """A record is truncated, points outside its data or contradicts itself."""


class Limits:
    """Costs shared by everything one document decodes: objects, rich-text attributes kept
    (:attr:`attributes`), UTF-16 units of typed text kept (:attr:`text_units`; the units
    stepped over for lack of room are counted in :attr:`text_dropped`) and the ink points
    (:attr:`budget`, shared with the reader) that stroke events are decoded into."""

    __slots__ = ("objects", "exhausted", "budget", "attributes", "text_units", "text_dropped")

    def __init__(self, objects: Optional[int] = None, budget: Optional[PointBudget] = None,
                 attributes: Optional[int] = None, text_units: Optional[int] = None):
        self.objects = MAX_OBJECTS if objects is None else objects
        self.exhausted = False
        self.budget = PointBudget() if budget is None else budget
        self.attributes = MAX_DOC_ATTRIBUTES if attributes is None else attributes
        self.text_units = MAX_DOC_TEXT_UNITS if text_units is None else max(0, text_units)
        self.text_dropped = 0

    def take_object(self) -> None:
        self.objects -= 1
        if self.objects < 0:
            self.exhausted = True
            raise SdocxError("the note holds more objects than gnnote reads")


class Cursor:
    """A read position in ``data[start:end]``; every read is bounds-checked."""

    __slots__ = ("data", "start", "pos", "end")

    def __init__(self, data: bytes, pos: int = 0, end: Optional[int] = None):
        size = len(data)
        end = size if end is None else end
        if not 0 <= pos <= end <= size:
            raise SdocxError(f"range {pos}..{end} lies outside the {size}-byte record")
        self.data = data
        self.start = pos
        self.pos = pos
        self.end = end

    def remaining(self) -> int:
        return self.end - self.pos

    def need(self, n: int) -> None:
        if n < 0 or self.pos + n > self.end:
            raise SdocxError(f"record truncated: {n} bytes needed at offset {self.pos}, "
                             f"{max(0, self.end - self.pos)} left")

    def seek(self, pos: int) -> None:
        """Move to the absolute offset ``pos``, which must lie inside this cursor's range."""
        if not self.start <= pos <= self.end:
            raise SdocxError(f"offset {pos} lies outside the record ({self.start}..{self.end})")
        self.pos = pos

    def skip(self, n: int) -> None:
        self.need(n)
        self.pos += n

    def take(self, n: int) -> bytes:
        self.need(n)
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def sub(self, n: int) -> "Cursor":
        """A cursor over the next ``n`` bytes; this one moves past them."""
        self.need(n)
        out = Cursor(self.data, self.pos, self.pos + n)
        self.pos += n
        return out

    def _one(self, fmt: str, size: int):  # type: ignore[no-untyped-def]
        self.need(size)
        value = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += size
        return value

    def u8(self) -> int:
        return self._one("<B", 1)

    def u16(self) -> int:
        return self._one("<H", 2)

    def i16(self) -> int:
        return self._one("<h", 2)

    def u32(self) -> int:
        return self._one("<I", 4)

    def i32(self) -> int:
        return self._one("<i", 4)

    def i64(self) -> int:
        return self._one("<q", 8)

    def f32(self) -> float:
        return self._one("<f", 4)

    def f64(self) -> float:
        return self._one("<d", 8)

    def array(self, code: str, n: int) -> Tuple:  # type: ignore[type-arg]
        size = struct.calcsize("<" + code) * n
        self.need(size)
        out = struct.unpack_from(f"<{n}{code}", self.data, self.pos)
        self.pos += size
        return out

    def count(self, min_item: int, wide: bool = True) -> int:
        """A u32 (``wide``) or u16 element count, checked against the bytes left: every
        element takes at least ``min_item`` bytes, so a damaged count fails here instead of
        looping over data that is not there."""
        n = self.u32() if wide else self.u16()
        if n * min_item > self.remaining():
            raise SdocxError(f"a list of {n} elements does not fit in the {self.remaining()} bytes left")
        return n

    def bitfield(self) -> int:
        n = self.u8()
        if n > 4:
            raise SdocxError(f"bitfield of {n} bytes (at most 4) at offset {self.pos - 1}")
        return int.from_bytes(self.take(n), "little")

    def utf16(self, units: int) -> str:
        return self.take(2 * units).decode("utf-16-le", "replace")

    def utf16_units(self, units: int) -> str:
        """``units`` UTF-16 code units, one character each (surrogates stay unpaired), so that
        offsets counted in code units index the string directly (:func:`units_to_text`).
        Widened to UTF-32 and decoded in one step: no Python object per unit."""
        raw = self.take(2 * units)
        wide = array("H")
        wide.frombytes(raw)
        if sys.byteorder == "big":
            wide.byteswap()
        if _U32 is None:  # (no 4-byte array type: never seen)
            return "".join(map(chr, wide))
        return array(_U32, wide).tobytes().decode(_UTF32, "surrogatepass")

    def short_str(self) -> str:
        return self.utf16(self.u16())

    def long_str(self) -> str:
        n = self.u32()
        if n > MAX_TEXT_CHARS:
            raise SdocxError(f"text of {n} characters is longer than gnnote reads")
        return self.utf16(n)

    def short_utf8(self) -> str:
        return self.take(self.u16()).decode("utf-8", "replace")

    def long_utf8(self) -> str:
        return self.take(self.u32()).decode("utf-8", "replace")

    def point(self) -> XY:
        return self._one("<d", 8), self._one("<d", 8)

    def rect(self) -> Rect:
        x0, y0, x1, y1 = self.array("d", 4)
        return x0, y0, x1, y1

    def rect_f32(self) -> Rect:
        x0, y0, x1, y1 = self.array("f", 4)
        return x0, y0, x1, y1

    def rect_i32(self) -> Rect:
        x0, y0, x1, y1 = self.array("i", 4)
        return float(x0), float(y0), float(x1), float(y1)


@dataclass
class Frame:
    """One frame of an object: ``cur`` is positioned after the bitfields, ``start`` and
    ``end`` delimit the frame in the record."""

    cur: Cursor
    start: int
    flex: int
    props: int
    fields: int

    def to_flex(self) -> bool:
        """Move to the optional fields; ``False`` (and no fields) when the frame has none."""
        if self.flex == 0:
            self.fields = 0
            return False
        self.cur.seek(self.start + self.flex)
        return True


def read_frame(cur: Cursor, data_type: int) -> Frame:
    """The frame at ``cur`` (which moves past it) whose data type must be ``data_type``."""
    start = cur.pos
    size = cur.u32()
    if size < 4:
        raise SdocxError(f"frame size {size} at offset {start} is smaller than its size field")
    cur.seek(start)
    body = cur.sub(size)
    body.skip(4)
    found = body.u16()
    if found != data_type:
        raise SdocxError(f"expected a frame of data type {data_type}, found {found}")
    flex = body.u32()
    props = body.bitfield()
    fields = body.bitfield()
    return Frame(body, start, flex, props, fields)


def first_unknown(fields: int, known: int) -> int:
    """The lowest bit of ``fields`` not in ``known`` (32 when there is none).  Optional fields
    are stored in bit order, so the ones below it can still be read."""
    unknown = fields & ~known
    return (unknown & -unknown).bit_length() - 1 if unknown else 32


def _bits(*numbers: int) -> int:
    out = 0
    for n in numbers:
        out |= 1 << n
    return out


# --------------------------------------------------------------------------- fixed point


def fixed_point_delta(v: int) -> float:
    """A compressed point-coordinate delta: sign bit, 10-bit integer part, 5-bit fraction."""
    a = ((v & 0x7FFF) >> 5) + (v & 0x1F) / 32.0
    return -a if v & 0x8000 else a


def fixed_small_delta(v: int) -> float:
    """A compressed pressure / tilt / orientation delta: sign bit, 3-bit integer, 12-bit fraction."""
    a = ((v & 0x7FFF) >> 12) + (v & 0xFFF) / 4096.0
    return -a if v & 0x8000 else a


_POINT_DELTA = [fixed_point_delta(v) for v in range(65536)]
_SMALL_DELTA = [fixed_small_delta(v) for v in range(65536)]


def _running(start: float, deltas: Sequence[int], table: List[float], step: int = 1, offset: int = 0) -> "array[float]":
    """``start`` followed by the running sums of the decoded deltas (8 bytes per value)."""
    return array("d", accumulate(map(table.__getitem__, islice(deltas, offset, None, step)), initial=start))


# --------------------------------------------------------------------------- object base


@dataclass
class ObjectBase:
    """The object-base frame every object starts with."""

    format_version: int = 0
    uuid: str = ""
    rect: Rect = (0.0, 0.0, 0.0, 0.0)
    angle: float = 0.0  # degrees
    page_index: Optional[int] = None
    props: int = 0  # bit 3: the object is visible
    integers: Dict[str, int] = field(default_factory=dict)  # the extra bundle's integers
    partial: bool = False  # optional fields after an unknown one were not read

    @property
    def visible(self) -> bool:
        return bool(self.props & 8)


_BASE_KNOWN = _bits(0, 1, 2, 3, 4, 5, 6, 7, 8, 13, 14, 15, 16, 17, 18, 19, 20, 21)


def _bundle(cur: Cursor) -> Dict[str, int]:
    """A key-value bundle; returns its integers (the only part gnnote uses)."""
    flags = cur.u8()
    if flags & ~0x0F:
        raise SdocxError(f"bundle with unknown map flags 0x{flags:x}")
    integers: Dict[str, int] = {}
    if flags & 1:
        for _ in range(cur.u16()):
            cur.short_utf8()
            cur.short_str()
    if flags & 2:
        for _ in range(cur.u16()):
            key = cur.short_utf8().rstrip("\0")
            integers[key] = cur.u32()
    if flags & 4:
        for _ in range(cur.u16()):
            cur.short_utf8()
            for _ in range(cur.u16()):
                cur.short_str()
    if flags & 8:
        for _ in range(cur.u16()):
            cur.short_utf8()
            cur.skip(cur.u32())
    return integers


def parse_base(cur: Cursor) -> ObjectBase:
    fr = read_frame(cur, 0)
    c = fr.cur
    base = ObjectBase(props=fr.props)
    base.format_version = c.u32()
    base.uuid = c.short_utf8()
    c.i64()  # modification time
    base.rect = c.rect()
    c.u32()  # creation counter
    c.u8()  # resize mode
    if not fr.to_flex():
        return base
    fields = fr.fields
    limit = first_unknown(fields, _BASE_KNOWN)
    base.partial = limit < 32

    def has(bit: int) -> bool:
        return bit < limit and bool(fields >> bit & 1)

    if has(0):
        base.angle = c.f32()
    if has(1):
        c.skip(16 * c.u16())
    if has(2):
        c.short_str()
    if has(3):
        _bundle(c)
    if has(4):
        c.short_str()
    if has(5):
        base.integers = _bundle(c)
    if has(6):
        c.u32()  # attached file id
    if has(7):
        c.skip(8)  # minimum size
    if has(8):
        c.skip(8)  # maximum size
    if has(13):
        c.i64()  # append time
    if has(14):
        c.skip(8)  # width and height of the page the object was made on
    if has(15):
        c.u8()  # layout type
    if has(16):
        c.skip(20)
    if has(17):
        c.u32()  # thumbnail id
    if has(18):
        c.skip(16)  # pivot
    if has(19):
        c.short_str()  # group id
    if has(20):
        base.page_index = c.u32()
    return base


# --------------------------------------------------------------------------- strokes


@dataclass
class Stroke:
    """A pen stroke (object type 1), in page units.  ``xs``, ``ys`` and ``pressures`` are
    ``array('d')``; they stay empty when the stroke is skipped anyway (an eraser, unknown
    fields) or when decoding them would exceed the ink point budget (:attr:`over_budget`)."""

    base: ObjectBase
    xs: Sequence[float]
    ys: Sequence[float]
    pressures: Sequence[float]
    tool_type: int = 0  # 0 unknown, 1 finger, 2 S Pen, 3 mouse, 4 eraser
    pen_name: str = ""
    color_bgra: bytes = b"\x00\x00\x00\xff"
    pen_size: Optional[float] = None
    eraser: bool = False
    fixed_width: bool = False
    fixed_opacity: bool = False
    line_type: int = 0  # 0 continuous, 1 dashed, 2 dotted
    has_tilt: bool = False
    unknown_fields: int = 0  # field bits gnnote cannot read: the stroke's style is unknown
    event_count: int = 0
    over_budget: bool = False  # the events were not decoded: the ink point budget is spent
    rainbow: bool = False  # property bit 12: drawn in changing colours
    colour_type: int = 0  # field bit 25: 0 a colour, 1 a gradient, 2 a pattern
    gradient_colours: int = 0  # field bit 24: number of gradient colours


# bits 1-4 and 7-17 per inkterop; 18-25 per sdocx2pdf
_STROKE_KNOWN = _bits(1, 2, 3, 4, *range(7, 26))
_STROKE_FIELD_SIZES = {4: 4, 8: 4, 9: 4, 10: 4, 11: 4, 12: 4, 13: 4, 15: 4, 16: 2, 17: 4,
                       18: 4, 19: 4, 20: 4, 21: 4, 22: 4, 23: 4, 25: 2}


def _events_size(count: int, compressed: bool, has_tilt: bool) -> int:
    """Bytes the events of a stroke take (see :func:`_events`)."""
    if count == 0:
        return 0
    if not compressed:
        return count * (16 + 4 + 4 + (8 if has_tilt else 0))
    n = count - 1
    return 16 + 4 * n + 4 + 2 * n + 4 + 2 * n + ((4 + 2 * n) * 2 if has_tilt else 0)


def _events(c: Cursor, count: int, compressed: bool,
            has_tilt: bool) -> Tuple["array[float]", "array[float]", "array[float]"]:
    if count == 0:
        return array("d"), array("d"), array("d")
    if not compressed:
        xy = c.array("d", 2 * count)
        pressures = array("d", c.array("f", count))
        c.skip(4 * count)  # timestamps (ms)
        if has_tilt:
            c.skip(8 * count)  # tilt and orientation (radians)
        return array("d", xy[0::2]), array("d", xy[1::2]), pressures
    n = count - 1
    x, y = c.point()
    dxy = c.array("H", 2 * n)
    p = c.f32()
    dp = c.array("H", n)
    c.u32()  # first timestamp
    c.skip(2 * n)  # timestamp deltas
    if has_tilt:
        c.skip(4 + 2 * n + 4 + 2 * n)  # tilt and orientation, first value + deltas
    # x then y deltas, interleaved; applied x first (inkterop and sdocx2pdf agree)
    xs = _running(x, dxy, _POINT_DELTA, 2, 0)
    ys = _running(y, dxy, _POINT_DELTA, 2, 1)
    return xs, ys, _running(p, dp, _SMALL_DELTA)


def parse_stroke(c: Cursor, base: ObjectBase, strings: Dict[int, str], limits: Optional[Limits] = None) -> Stroke:
    """The stroke frame.  The events are decoded last, and only for a stroke the reader can
    use and the ink point budget of ``limits`` still allows (:attr:`Stroke.over_budget`)."""
    fr = read_frame(c, 1)
    w = fr.cur
    props = fr.props
    count = w.u16()
    compressed, has_tilt = bool(props & 1), bool(props & 4)
    events_at = w.pos
    w.skip(_events_size(count, compressed, has_tilt))
    stroke = Stroke(base, array("d"), array("d"), array("d"), has_tilt=has_tilt, event_count=count)
    stroke.eraser = bool(props >> 3 & 1)
    stroke.fixed_width = bool(props >> 4 & 1)
    stroke.fixed_opacity = bool(props >> 11 & 1)
    stroke.rainbow = bool(props >> 12 & 1)
    stroke.tool_type = w.u16()
    if fr.to_flex():
        fields = fr.fields
        stroke.unknown_fields = fields & ~_STROKE_KNOWN
        if not stroke.unknown_fields:  # else the field layout is unknown: the caller skips the stroke
            _stroke_fields(w, stroke, fields, strings)
    if stroke.unknown_fields or stroke.eraser or stroke.tool_type == 4:
        return stroke  # skipped by the reader: nothing to decode
    if not (limits.budget if limits is not None else PointBudget()).take(count):
        stroke.over_budget = True
        return stroke
    w.seek(events_at)
    stroke.xs, stroke.ys, stroke.pressures = _events(w, count, compressed, has_tilt)
    return stroke


def _stroke_fields(w: Cursor, stroke: Stroke, fields: int, strings: Dict[int, str]) -> None:
    if fields & 2:
        w.u32()  # advanced pen settings (string id)
    if fields & 4:
        stroke.color_bgra = w.take(4)
    if fields & 8:
        stroke.pen_size = w.f32()
    if fields & 16:
        w.u32()  # meaning unknown
    if fields & 128:
        stroke.pen_name = strings.get(w.u32(), "")
    for bit in range(8, 26):
        if not fields >> bit & 1:
            continue
        if bit == 14:
            stroke.line_type = w.u16()
        elif bit == 24:
            stroke.gradient_colours = w.u16()
            w.skip(4 * stroke.gradient_colours)
        elif bit == 25:
            stroke.colour_type = w.u16()
        else:
            w.skip(_STROKE_FIELD_SIZES[bit])


# --------------------------------------------------------------------------- shapes and lines


@dataclass
class Segment:
    """One path segment: ``kind`` 1 move, 2 line, 3 quadratic, 4 cubic, 5 arc, 6 close, 7 oval;
    ``points`` the control and end points (an arc: its oval's corners, then start and sweep
    angles in degrees as a point)."""

    kind: int
    points: List[XY]


def _path(c: Cursor) -> List[Segment]:
    n = c.u32()
    if n > MAX_PATH_SEGMENTS or n > c.remaining():
        raise SdocxError(f"path of {n} segments is damaged or too long")
    out: List[Segment] = []
    for _ in range(n):
        kind = c.u8()
        if kind in (1, 2):
            out.append(Segment(kind, [c.point()]))
        elif kind == 3:
            out.append(Segment(kind, [c.point(), c.point()]))
        elif kind == 4:
            out.append(Segment(kind, [c.point(), c.point(), c.point()]))
        elif kind == 5:
            x0, y0, x1, y1 = c.rect()
            out.append(Segment(kind, [(x0, y0), (x1, y1), (c.f64(), c.f64())]))
        elif kind == 6:
            out.append(Segment(kind, []))
        elif kind == 7:
            x0, y0, x1, y1 = c.rect()
            out.append(Segment(kind, [(x0, y0), (x1, y1)]))
        else:
            raise SdocxError(f"unknown path segment type {kind}")
    return out


@dataclass
class ShapeStyle:
    """What the shape-base frame says about the outline."""

    line_color: Optional[bytes] = None  # BGRA, solid colour
    line_color_type: int = 0  # 0 solid, 1 gradient, 2 none
    line_width: Optional[float] = None  # page units
    dash_type: int = 0
    begin_arrow: int = 0
    end_arrow: int = 0
    begin_arrow_size: int = 0
    end_arrow_size: int = 0


def _shape_base(c: Cursor) -> ShapeStyle:
    fr = read_frame(c, 6)
    w = fr.cur
    w.skip(16 * w.count(16))  # points of connection
    w.u32()  # size of the connection points
    for _ in range(w.count(20)):
        w.skip(16)
        for _ in range(w.count(2)):
            w.short_utf8()
    w.u8()
    style = ShapeStyle()
    if not fr.to_flex():
        return style
    fields = fr.fields
    limit = first_unknown(fields, _bits(2, 3, 7))
    if 2 < limit and fields & 4:
        eff = w.sub(w.u32())
        eff.bitfield()
        style.line_color_type = eff.u8()
        style.line_color = eff.take(4)
    if 3 < limit and fields & 8:
        size = w.u32()
        if size != 12:
            raise SdocxError(f"line style of {size} bytes (12 expected)")
        style.line_width = w.f32()
        _compound, style.dash_type, _cap, _join, style.begin_arrow, style.begin_arrow_size, \
            style.end_arrow, style.end_arrow_size = w.take(8)
    return style


@dataclass(slots=True)
class TextSpan:
    """A character-range attribute of a text: ``kind`` 1 colour, 3 font size, 4 font name,
    5 bold, 6 italic, 7 underline, 9 link, 17 background colour, 20 strikethrough, ...;
    ``start``/``end`` count UTF-16 units; ``data`` the first :data:`ATTRIBUTE_DATA` bytes of
    the attribute's own data (of a font name: its u16 length and first :data:`MAX_FONT_NAME`
    units).  A paragraph attribute (``kind`` 2 indent, 3 alignment, 4 line spacing, 5 bullet,
    8 space before, 9 space after, ...) has the same shape, but ``start``/``end`` count
    paragraphs.  Only the kinds gnnote uses (:data:`SPAN_KINDS`, :data:`PARA_KINDS`) and
    non-empty ranges are kept."""

    kind: int
    start: int
    end: int
    data: bytes


@dataclass
class InlineObject:
    """An object anchored in a text (an image, a table, ...) at UTF-16 position ``index``;
    ``layout`` 0 block, 1 inline, 2 / 3 block with a small / medium margin."""

    index: int
    obj: "AnyObject"
    layout: int = 0
    constraint: int = 0  # 0 normal, 1 / 2 the object may run over a page break


@dataclass
class TextBody:
    """The text of a text box, of ``note.note``'s title or body, or inside a shape.  ``text``
    holds one character per UTF-16 code unit (:func:`units_to_text` turns it into text)."""

    text: str
    spans: List[TextSpan]
    paragraphs: List[TextSpan]
    margins: Tuple[float, float, float, float]  # left, top, right, bottom (page units)
    inline: List[InlineObject]
    inline_failed: int = 0  # inline objects that could not be decoded
    attributes_dropped: int = 0  # spans and paragraph attributes beyond the budgets (not kept)
    text_dropped: int = 0  # UTF-16 units at the end of the text not kept (the document's text allowance ran out)


def units_to_text(units: str) -> str:
    """A string of UTF-16 code units (:meth:`Cursor.utf16_units`) as ordinary text."""
    return units.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


_ATTRIBUTE_HEAD = struct.Struct("<HIII")  # record size, kind, start, end


def _attributes(w: Cursor, head: int, kinds: frozenset, keep: int, out: List[TextSpan], what: str) -> int:
    """A list of rich-text attribute records (u32 count; each a u16 size that counts ``head``
    bytes of kind, start, end, ... and the attribute's data).  Records of ``kinds`` with a
    non-empty range are appended to ``out`` while ``keep`` allows (at most ``keep`` of them);
    the others are stepped over without being stored.  Returns how many were not kept for
    lack of room."""
    n = w.count(2 + head)
    data, pos, end = w.data, w.pos, w.end
    unpack = _ATTRIBUTE_HEAD.unpack_from
    dropped = 0
    for _ in range(n):
        if pos + 2 + head > end:
            raise SdocxError(f"{what} truncated at offset {pos}")
        size, kind, start, stop = unpack(data, pos)
        following = pos + 2 + size
        if size < head or following > end:
            raise SdocxError(f"{what} of {size} bytes at offset {pos}")
        if kind in kinds and stop > start:
            if keep > len(out):
                first = pos + 2 + head
                if kind == SPAN_FONT and head == 16:
                    room = 2 + 2 * MAX_FONT_NAME
                else:
                    room = ATTRIBUTE_DATA
                out.append(TextSpan(kind, start, stop, data[first:min(following, first + room)]))
            else:
                dropped += 1
        pos = following
    w.pos = pos
    return dropped


def _text_common(c: Cursor, format_version: int, ctx: "_Ctx", depth: int) -> TextBody:
    w = c.sub(c.u32())
    n = w.u32()
    if n > MAX_TEXT_CHARS:
        raise SdocxError(f"text of {n} characters is longer than gnnote reads")
    w.need(2 * n)
    limits = ctx.limits
    kept = min(n, limits.text_units)  # the document's text allowance; the rest is stepped over
    text = w.utf16_units(kept)
    w.skip(2 * (n - kept))
    limits.text_units -= kept
    limits.text_dropped += n - kept
    room = max(0, min(MAX_TEXT_ATTRIBUTES, limits.attributes))
    spans: List[TextSpan] = []
    dropped = _attributes(w, 16, SPAN_KINDS, room, spans, "text span")
    paragraphs: List[TextSpan] = []
    dropped += _attributes(w, 12, PARA_KINDS, room - len(spans), paragraphs, "paragraph attribute")
    limits.attributes -= len(spans) + len(paragraphs)
    margins = w.array("f", 4)
    w.u8()  # vertical gravity
    w.skip(8 * w.u16())  # section data
    body = TextBody(text, spans, paragraphs, (margins[0], margins[1], margins[2], margins[3]), [],
                    attributes_dropped=dropped, text_dropped=n - kept)
    if format_version >= 2035 and w.remaining() >= 8:
        flags = w.u32()
        w.u32()
        if flags & 1:
            for _ in range(w.count(12)):
                item = w.sub(w.u32())
                size = item.u32()
                obj_type = item.u32()
                inner = item.sub(size)
                index = item.u32() if item.remaining() >= 4 else -1
                layout = item.u32() if item.remaining() >= 4 else 0
                constraint = item.u32() if item.remaining() >= 4 else 0
                try:
                    obj = parse_object(inner, obj_type, ctx.strings, ctx.limits, depth + 1)
                except SdocxError:
                    if ctx.limits.exhausted:
                        raise
                    body.inline_failed += 1
                    continue
                body.inline.append(InlineObject(index, obj, layout, constraint))
    return body


@dataclass
class Shape:
    """A shape (object type 7), a text box (type 2) or an image (type 3), in page units."""

    base: ObjectBase
    obj_type: int
    style: ShapeStyle
    shape_type: int = 0
    rect: Rect = (0.0, 0.0, 0.0, 0.0)  # the unrotated box
    angle: float = 0.0  # degrees, clockwise
    flip_h: bool = False
    flip_v: bool = False
    path: List[Segment] = field(default_factory=list)
    fill_color: Optional[bytes] = None  # BGRA, a solid fill
    fill_kind: int = 0  # 0 none, 1 colour, 2 image, 3 pattern, 4 background
    image_id: Optional[int] = None  # media bind id of an image fill
    text: Optional[TextBody] = None
    crop: Optional[Rect] = None  # image: the visible part, in source pixels
    original_rect: Optional[Rect] = None  # image: the uncropped box
    border: bool = False  # image: a frame or border is drawn around it


_SHAPE_KNOWN = _bits(*range(0, 25))
_SHAPE_FIELD_SIZES = {1: 1, 2: 4, 3: 4, 4: 4, 6: 4, 7: 4, 8: 2, 10: 4, 11: 4, 22: 1, 12: 1, 13: 1, 14: 1,
                      15: 1, 16: 16, 17: 4, 18: 16, 19: 16, 20: 4, 21: 4, 23: 4, 24: 4}
_SHAPE_ORDER = (*range(0, 12), 22, *range(12, 22), 23, 24)  # bit 22 is stored before bit 12


def _fill(c: Cursor, shape: Shape) -> None:
    c.u32()  # size of the effect
    kind = c.u8()
    shape.fill_kind = kind
    if kind == 1:
        props = c.bitfield()
        color = c.take(4)
        c.u8()  # gradient type
        c.i16()  # angle
        c.skip(8)  # radial gradient centre
        c.skip(8 * c.u8())  # gradient stops
        if not props & 1:  # solid colour (else gradient)
            shape.fill_color = color
    elif kind == 2:
        c.u8()  # image type
        image = c.i32()
        shape.image_id = image if image >= 0 else None
        c.skip(16 + 8 + 4 + 4 + 4 + 1 + 16 + 4)  # stretch, tiling, alpha, rotatable, nine-patch
    elif kind == 3:
        c.skip(16)  # pattern, foreground and background colours
    elif kind == 4:
        c.f32()  # transparency
    else:
        raise SdocxError(f"unknown fill effect {kind}")


def parse_shape(c: Cursor, base: ObjectBase, obj_type: int, ctx: "_Ctx", depth: int) -> Shape:
    style = _shape_base(c)
    fr = read_frame(c, 7)
    w = fr.cur
    shape = Shape(base, obj_type, style)
    shape.flip_h = bool(fr.props & 1)
    shape.flip_v = bool(fr.props & 2)
    shape.shape_type = w.u32()
    shape.rect = w.rect()
    shape.angle = w.f32()
    path_size = w.u32()
    if path_size:
        shape.path = _path(w.sub(path_size))
    w.skip(16 * w.u8())  # control points
    if obj_type == 7:
        w.rect()  # original drawn rectangle
    if fr.to_flex():
        fields = fr.fields
        limit = first_unknown(fields, _SHAPE_KNOWN)
        for bit in _SHAPE_ORDER:
            if bit >= limit:
                break
            if not fields >> bit & 1:
                continue
            if bit == 0:
                shape.text = _text_common(w, base.format_version, ctx, depth)
            elif bit == 5:
                _fill(w, shape)
            elif bit == 9:
                w.short_str()  # hint text
            else:
                w.skip(_SHAPE_FIELD_SIZES[bit])
    if obj_type in (2, 3):
        last = read_frame(c, obj_type)
        if last.to_flex() and obj_type == 3:
            _image_fields(last, shape)
    return shape


def _image_fields(fr: Frame, shape: Shape) -> None:
    w = fr.cur
    fields = fr.fields
    known = _bits(1, 3, 4, 5, 9, 10, 11, 12, 17, 18)
    limit = first_unknown(fields, known)
    sizes = {3: 4, 10: 16, 11: 32, 12: 4, 18: 4}
    border_type: Optional[int] = None
    border_width = 0.0
    for bit in range(0, 19):
        if bit >= limit:
            break
        if not fields >> bit & 1:
            continue
        if bit == 1:
            shape.crop = w.rect_i32()
        elif bit == 4:
            border_width = w.f32()
        elif bit == 5:
            border_type = w.u16()  # 0 none, 1 square, 2 shadow, 3 dotted, 4 a picture frame
        elif bit == 9:
            w.u32()  # media bind id of a picture frame
            shape.border = True
        elif bit == 17:
            shape.original_rect = w.rect()
        else:
            w.skip(sizes[bit])
    if border_type is not None:
        shape.border = shape.border or border_type != 0
    elif border_width > 0.0:  # a width without a type (never seen): taken as a border
        shape.border = True


@dataclass
class Line:
    """A straight or bent line (object type 8), in page units."""

    base: ObjectBase
    style: ShapeStyle
    start: XY
    end: XY
    path: List[Segment] = field(default_factory=list)
    control_points: int = 0


def parse_line(c: Cursor, base: ObjectBase) -> Line:
    style = _shape_base(c)
    fr = read_frame(c, 8)
    w = fr.cur
    w.u8()  # connector type (straight, bent, curved)
    w.u8()  # start direction
    controls = w.u8()
    w.skip(16 * controls)
    start = w.point()
    end = w.point()
    w.rect()  # original drawn rectangle
    w.rect()  # original rectangle
    w.f32()  # original angle
    line = Line(base, style, start, end, control_points=controls)
    if fr.to_flex():
        fields = fr.fields
        limit = first_unknown(fields, _bits(0, 1, 2, 3))
        for bit in range(3):
            if bit < limit and fields >> bit & 1:
                w.u32()
        if 3 < limit and fields & 8:
            line.path = _path(w)
    return line


@dataclass
class OtherObject:
    """An object gnnote does not convert (voice recording, table, ...)."""

    base: ObjectBase
    obj_type: int


AnyObject = object  # Stroke | Shape | Line | OtherObject


@dataclass
class _Ctx:
    strings: Dict[int, str]
    limits: Limits


def parse_object(c: Cursor, obj_type: int, strings: Dict[int, str], limits: Limits, depth: int = 0) -> AnyObject:
    """One object's frames (without the trailing hash).  :class:`SdocxError` when damaged."""
    if depth > MAX_DEPTH:
        raise SdocxError("objects nested too deeply")
    limits.take_object()
    base = parse_base(c)
    if obj_type == 1:
        return parse_stroke(c, base, strings, limits)
    if obj_type in (2, 3, 7):
        return parse_shape(c, base, obj_type, _Ctx(strings, limits), depth)
    if obj_type == 8:
        return parse_line(c, base)
    return OtherObject(base, obj_type)


# --------------------------------------------------------------------------- pages


@dataclass
class Layer:
    name: str = ""
    visible: bool = True
    objects: List[AnyObject] = field(default_factory=list)
    failed: Dict[str, int] = field(default_factory=dict)  # object kind -> objects that could not be decoded
    grouped: int = 0  # objects with children (not decoded)


@dataclass
class PdfPlacement:
    """A page of an imported PDF drawn into ``rect`` (page units)."""

    bind_id: int
    page_index: int
    rect: Rect


@dataclass
class Page:
    width: int = 0
    height: int = 0
    orientation: int = 0
    uuid: str = ""
    format_version: int = 0
    template_uri: str = ""
    template_type: Optional[int] = None
    background_color: Optional[bytes] = None  # BGRA
    background_image: Optional[int] = None
    pdfs: List[PdfPlacement] = field(default_factory=list)
    custom_objects: int = 0  # sticky notes and other custom page objects (field bit 18), not decoded
    layers: List[Layer] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)  # short English notes


_PAGE_KNOWN = _bits(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 18)


def _page_fields(c: Cursor, page: Page, fields: int) -> None:
    page.custom_objects = fields >> 18 & 1  # at least one, even when its count cannot be read
    limit = first_unknown(fields, _PAGE_KNOWN)
    if limit < 32:
        page.problems.append("the page holds settings of a newer app version; some were not read")

    def has(bit: int) -> bool:
        return bit < limit and bool(fields >> bit & 1)

    if has(0):
        c.rect()  # drawn rectangle
    if has(1):
        for _ in range(c.u16()):
            c.short_str()  # tags
    if has(2):
        page.template_uri = c.short_str()
    if has(3):
        page.background_image = c.i32()
    if has(4):
        c.u32()  # background image mode
    if has(5):
        page.background_color = c.take(4)
    if has(6):
        c.u32()  # background width
    if has(7):
        c.u32()  # background rotation
    if has(8):
        for _ in range(c.u16()):
            bind_id = c.u32()
            index = c.u32()
            rect = c.rect() if page.format_version < 2034 else c.rect_i32()
            page.pdfs.append(PdfPlacement(bind_id, index, rect))
    if has(9):
        page.template_type = c.u32()
    if has(10):
        count = c.u32()
        size = c.u16()
        c.skip(count * size)  # canvas cache entries
    if has(11):
        c.u32()  # imported data height
    if has(12):
        c.u32()  # theme
    if has(15):
        c.i64()  # time the handwriting was last recognised
    if has(16):
        for _ in range(c.count(4)):
            c.skip(c.u32())  # handwriting recognition data
    if has(18):
        page.custom_objects = c.count(8)  # each: u32 type, u32 size, its data


def _parse_layer(c: Cursor, strings: Dict[int, str], limits: Limits) -> Layer:
    start = c.pos
    size = c.u32()
    if size < 4:
        raise SdocxError(f"layer header of {size} bytes")
    c.seek(start)
    head = c.sub(size)
    head.skip(4)
    flex = head.u32()  # from the start of the page, unlike object frames
    props = head.bitfield()
    fields = head.bitfield()
    head.u32()  # layer id
    layer = Layer(visible=not props & 1)
    if flex and fields & 4:
        head.seek(flex)
        if fields & 1:
            head.u8()  # alpha
        if fields & 2:
            head.skip(4)  # background colour
        layer.name = head.short_str()
    count = c.u32()
    if count * 7 > c.remaining():
        raise SdocxError(f"layer lists {count} objects, more than its bytes can hold")
    for _ in range(count):
        obj_type = c.u8()
        children = c.u16()
        payload = c.sub(c.u32())
        if children:
            limits.take_object()  # counted too, so that a flood of them stays bounded
            layer.grouped += 1
            continue
        if payload.remaining() < HASH_BYTES:
            raise SdocxError("object too short for its hash")
        payload.end -= HASH_BYTES  # the trailing sha256(uuid + modification time)
        try:
            layer.objects.append(parse_object(payload, obj_type, strings, limits))
        except SdocxError:
            if limits.exhausted:
                raise
            kind = OBJECT_TYPES.get(obj_type, f"type-{obj_type}")
            layer.failed[kind] = layer.failed.get(kind, 0) + 1
    c.skip(HASH_BYTES)  # layer hash
    return layer


def parse_page(data: bytes, strings: Dict[int, str], limits: Limits) -> Page:
    """A ``<uuid>.page`` member.  :class:`SdocxError` when its header is unreadable; damage
    further in leaves the layers read so far and a note in :attr:`Page.problems`."""
    c = Cursor(data)
    page_end = c.u32()
    flex = c.u32()
    c.bitfield()  # properties (bit 0: text only)
    fields = c.bitfield()
    page = Page()
    page.orientation = c.u32()
    page.width = c.u32()
    page.height = c.u32()
    c.u32()  # offset x
    c.u32()  # offset y
    page.uuid = c.short_str()
    c.i64()  # modification time
    page.format_version = c.u32()
    c.u32()  # minimum format version
    if not 0 < page_end <= len(data):
        raise SdocxError(f"page end offset {page_end} lies outside the {len(data)}-byte page")
    if flex and fields:
        try:
            c.seek(flex)
            _page_fields(Cursor(data, flex, page_end), page, fields)
        except SdocxError:
            page.problems.append("its settings (paper, imported PDF pages) are damaged and were ignored")
    c.seek(page_end)
    try:
        n_layers = c.u16()
        c.u16()  # current layer
        for _ in range(n_layers):
            page.layers.append(_parse_layer(c, strings, limits))
        c.skip(HASH_BYTES)
        if data[c.pos:] != PAGE_END:
            page.problems.append("its end marker is missing")
    except SdocxError:
        if limits.exhausted:
            raise
        page.problems.append("its content is damaged; what could be read was kept")
    return page


def parse_page_list(data: bytes) -> List[str]:
    """``pageIdInfo.dat``: the page UUIDs in page order."""
    c = Cursor(data)
    c.skip(HASH_BYTES)  # note.note's hash
    out = []
    for _ in range(c.u16()):
        out.append(c.short_str())
        c.skip(HASH_BYTES)
    return out


# --------------------------------------------------------------------------- note.note


@dataclass
class NoteDoc:
    format_version: int = 0
    width: int = 0
    height: int = 0
    title: Optional[TextBody] = None
    body: Optional[TextBody] = None
    body_rect: Optional[Rect] = None
    strings: Dict[int, str] = field(default_factory=dict)
    template_uri: str = ""
    font_size_delta: int = 0  # field bit 11: a change to the body text's font sizes (not applied)
    problems: List[str] = field(default_factory=list)


_NOTE_KNOWN = _bits(0, 1, 2, 3, 6, 7, 9, 10, 11)


def _note_text(raw: Cursor, ctx: _Ctx) -> Tuple[Optional[TextBody], Optional[Rect]]:
    """The title or body blob: a text-box object without its type byte."""
    if not raw.remaining():
        return None, None
    obj = parse_object(raw, 2, ctx.strings, ctx.limits)
    if isinstance(obj, Shape):
        return obj.text, obj.base.rect
    return None, None


def parse_note(data: bytes, limits: Limits) -> NoteDoc:
    """``note.note``: page size, title and body text, the string registry.  Raises
    :class:`SdocxError` only when the fixed header is unreadable."""
    c = Cursor(data)
    flex = c.u32()
    c.bitfield()  # properties
    fields = c.bitfield()
    note = NoteDoc()
    note.format_version = c.u32()
    c.short_str()  # document id
    c.u32()  # revision
    c.i64()  # created
    c.i64()  # modified
    note.width = c.u32()
    note.height = c.u32()
    c.u32()  # horizontal padding
    c.u32()  # vertical padding
    c.u32()  # minimum format version
    title_raw = c.sub(c.u32())  # (cursors, not copies: the body can be most of the member)
    body_raw = c.sub(c.u32())
    if flex and fields:
        try:
            c.seek(flex)
            limit = first_unknown(fields, _NOTE_KNOWN)

            def has(bit: int) -> bool:
                return bit < limit and bool(fields >> bit & 1)

            if has(0):
                c.short_str()  # app name
            if has(1):
                c.u32()
                c.u32()
                c.short_str()  # app version
            if has(2):
                c.short_str()
                c.short_str()
                c.short_str()
                c.u32()  # author
            if has(3):
                c.skip(16)  # latitude, longitude
            if has(6):
                note.template_uri = c.short_str()
            if has(7):
                c.u32()  # last edited page
            if has(9):
                c.skip(12)  # last edited page image id and time
            if has(10):
                registry = c.sub(c.u32())
                if registry.remaining():
                    for _ in range(registry.u16()):
                        key = registry.u32()
                        note.strings[key] = registry.short_str()
            elif fields >> 10 & 1:
                note.problems.append("the pen names could not be read (unknown settings before them)")
            if has(11):
                note.font_size_delta = c.i32()
        except SdocxError:
            note.problems.append("the pen names could not be read (damaged settings)")
    ctx = _Ctx(note.strings, limits)
    for raw, attr in ((title_raw, "title"), (body_raw, "body")):
        try:
            text, rect = _note_text(raw, ctx)
        except SdocxError:
            if limits.exhausted:
                raise
            note.problems.append(f"the note's {attr} text is damaged and was not converted")
            continue
        setattr(note, attr, text)
        if attr == "body":
            note.body_rect = rect
    return note


# --------------------------------------------------------------------------- media, end tag


def parse_media_info(data: bytes) -> Dict[int, str]:
    """``media/mediaInfo.dat``: media bind id -> file name in ``media/``."""
    out: Dict[int, str] = {}
    if len(data) < 4:
        return out
    newer = data.endswith(b"EOFX")
    if not newer and not data.endswith(b"EOF"):
        raise SdocxError("media registry without its end marker")
    c = Cursor(data, 0, len(data) - (4 if newer else 3))
    if newer:
        c.u32()  # format version
    for _ in range(c.u16()):
        entry = c.sub(c.u32()) if newer else c
        bind_id = entry.u32()
        name = entry.short_str()
        entry.skip(64 + 2 + 8)  # hash (hex), reference count, modification time
        if newer:
            entry.u8()  # attached
        out[bind_id] = name
    return out


@dataclass
class EndTag:
    pageless: bool = False
    landscape: bool = False
    width: int = 0
    height: float = 0.0
    app_name: str = ""
    app_version: str = ""
    locked: bool = False


def parse_end_tag(data: bytes) -> EndTag:
    """``end_tag.bin``: page model, size, app, lock state (:class:`SdocxError` when damaged)."""
    c = Cursor(data)
    size = c.u16()
    body = c.sub(min(size, c.remaining()))
    ident = next((i for i in END_TAG_IDENTS if body.data[body.pos:body.end].endswith(i)), None)
    if ident is None:
        raise SdocxError("end tag without its identifier")
    body.end -= len(ident)
    tag = EndTag()
    body.u32()  # format version
    body.short_str()  # note uuid
    body.i64()  # modified
    tag.landscape = bool(body.u32() & 2)
    body.short_str()  # cover image
    tag.width = body.u32()
    tag.height = body.f32()
    tag.app_name = body.short_str()
    major, minor = body.u32(), body.u32()
    patch = body.short_str()
    if major != 0xFFFFFFFF:
        tag.app_version = f"{major}.{minor}.{patch}".rstrip(".")
    body.u32()  # minimum format version
    body.i64()  # created
    body.u32()  # last viewed page
    tag.pageless = body.u16() == 1
    if body.remaining() >= 2:
        tag.locked = body.u16() != 0  # document type: 0 an unlocked note
    if body.remaining() >= 2:
        body.short_str()  # owner id
    if body.remaining() >= 4:
        body.skip(body.u32())
    if body.remaining() >= 4 and body.u32():
        tag.locked = True  # encryption data present
    return tag
