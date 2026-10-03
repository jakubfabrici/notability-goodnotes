"""MyScript BINK ink (``pages/<id>/ink.bink`` inside ``.nebo`` packages): bytes -> strokes and tags.

Byte layout (``docs/nebo.md`` section 2; all little-endian and byte-packed, ``str`` is a
``u32`` length followed by UTF-8 bytes)::

    "BINK\\0"  u32 version (5)  u8 0  u32 1
    u32 nchannels; per channel: str name ("X" "Y" "F" "T"), 4-byte type, u32 has_unit, [str unit]
    u32 layout_len + layout table (skipped)
    u32 precision_x (1000)  u32 precision_y (1000)  u32 3  u8 0
    u32 nrecords; then nrecords stroke records:
      u32 0xFFFFFFFF                      an erased stroke (counts as a record)
      u32 0x80000000 packed stroke:       u64 t0 (us), f32 x0, f32 y0 (mm), u16 tilt,
                                          u16 0x0C49, u16 0, u32 n, i16 dx[n], i16 dy[n], u8 force[n]
      u32 0          plain stroke (Kobo): u64 t0, u32 pen, u32 n, n x (f32 x, f32 y) mm,
                                          n x u32 force, n x u32 time    [inferred, no sample]
    tag table: u32 0, u32 count, u8 0, then count records ("u32 kind, u32 id, u32 0" before
      every record but the first): str name, u32 ngroups, ngroups x (u16 3, u16 start sample,
      u32 first record, u16 namespace, u16 end sample, u32 last record), str payload

A packed stroke's points are ``x0 + cumsum(dx) / u`` with ``u = precision / 2`` delta units
per millimetre (500: one unit is 2 um, the scale inkterop verified by overlay on the app's
own SVG export and kollate on Kobo devices).  A tag covers the stroke records ``first ..
last`` inclusive (erased records count); its name is a pen class (``pen-025``,
``brush-0500``), ``.STYLE`` with a CSS payload, a grouping marker (``HIGHLIGHT_STROKES``) or
recognition output (``CHAR``, ``WORD``, ``DIAGRAM`` with JSON).

When the header cannot be parsed (Kobo files are described as declaring "one or two stroke
formats"), the decoder looks for the first record carrying the constant pen field 0x0C49
and reads records back to back until one is not plausible.  Nothing here raises except
:class:`BinkError` for data that does not start with the magic.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from itertools import accumulate
from typing import List, Optional, Tuple

from ..readutil import PointBudget

__all__ = ["BinkError", "BinkStroke", "BinkTag", "BinkInk", "parse_bink", "MAGIC", "DEFAULT_UNITS_PER_MM"]

MAGIC = b"BINK\x00"
DEFAULT_UNITS_PER_MM = 500.0  # packed delta units per millimetre at the usual precision 1000
FLAG_PACKED = 0x80000000
FLAG_PLAIN = 0x00000000
FLAG_ERASED = 0xFFFFFFFF
PEN_MARKER = 0x0C49  # constant "pen" / orientation field of every observed stroke

MAX_RECORDS = 200_000  # stroke records per page
MAX_POINTS_PER_STROKE = 1_000_000
MAX_TAGS = 100_000
MAX_GROUPS = 10_000  # span groups in one tag
MAX_TAG_WORK = 5_000_000  # (tag, stroke) pairs the caller may expand
MAX_COORD_MM = 100_000.0  # 100 m: anything further is damage
SCAN_WINDOW = 1 << 16  # bytes searched for the first stroke when the header is unreadable


class BinkError(ValueError):
    """The data is not BINK ink."""


class _Truncated(Exception):
    pass


@dataclass
class BinkStroke:
    record: int  # record index; tags address strokes by it (erased records count)
    xs: List[float]  # millimetres, page top-left origin, y down
    ys: List[float]
    force: Optional[bytes] = None  # one byte per point (255 = no pressure sensor); None for plain strokes
    t0_us: int = 0


@dataclass
class BinkTag:
    name: str
    first: int
    last: int
    text: str = ""


@dataclass
class BinkInk:
    version: int = 0
    channels: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    precision: Tuple[int, int] = (0, 0)
    units_per_mm: Tuple[float, float] = (DEFAULT_UNITS_PER_MM, DEFAULT_UNITS_PER_MM)
    records: int = 0  # stroke records read (erased ones included)
    strokes: List[BinkStroke] = field(default_factory=list)
    erased: int = 0
    unusable: int = 0  # records skipped for non-finite or far-away coordinates
    tags: List[BinkTag] = field(default_factory=list)
    scanned: bool = False  # True when the header was not understood and records were located by scanning
    problems: List[str] = field(default_factory=list)  # one human-readable line per defect


class _Cursor:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def need(self, n: int) -> None:
        if n < 0 or self.pos + n > len(self.buf):
            raise _Truncated()

    def unpack(self, fmt: str, size: int) -> tuple:
        self.need(size)
        values = struct.unpack_from(fmt, self.buf, self.pos)
        self.pos += size
        return values

    def u8(self) -> int:
        return self.unpack("<B", 1)[0]

    def u16(self) -> int:
        return self.unpack("<H", 2)[0]

    def u32(self) -> int:
        return self.unpack("<I", 4)[0]

    def u64(self) -> int:
        return self.unpack("<Q", 8)[0]

    def f32(self) -> float:
        return self.unpack("<f", 4)[0]

    def raw(self, n: int) -> bytes:
        self.need(n)
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def string(self, limit: int) -> str:
        n = self.u32()
        if n > limit:
            raise _Truncated()
        return self.raw(n).decode("utf-8", "replace")


def _units(precision: int) -> float:
    """Delta units per millimetre for a header precision (2 um at the observed 1000)."""
    if 100 <= precision <= 1_000_000:
        return precision / 2.0
    return DEFAULT_UNITS_PER_MM


def _plausible(v: float) -> bool:
    return math.isfinite(v) and abs(v) <= MAX_COORD_MM


def _read_header(c: _Cursor, ink: BinkInk) -> int:
    """Parse the header; returns the announced record count (raises _Truncated)."""
    c.pos = len(MAGIC)
    ink.version = c.u32()
    c.u8()
    c.u32()
    nchannels = c.u32()
    if nchannels > 64:
        raise _Truncated()
    for _ in range(nchannels):
        name = c.string(64)
        c.raw(4)  # channel type tag
        has_unit = c.u32()
        unit = c.string(64) if has_unit == 1 else None
        if has_unit > 1:
            raise _Truncated()
        ink.channels.append((name, unit))
    layout = c.u32()
    if layout > 4096:
        raise _Truncated()
    c.raw(layout)
    ink.precision = (c.u32(), c.u32())
    ink.units_per_mm = (_units(ink.precision[0]), _units(ink.precision[1]))
    c.u32()
    c.u8()
    count = c.u32()
    if count > MAX_RECORDS:
        raise _Truncated()
    return count


def _record_at(buf: bytes, pos: int) -> bool:
    """Whether a packed or plain stroke record with the 0x0C49 pen field starts at ``pos``."""
    if pos + 30 > len(buf):
        return False
    flags = struct.unpack_from("<I", buf, pos)[0]
    if flags == FLAG_PACKED:
        x0, y0 = struct.unpack_from("<ff", buf, pos + 12)
        pen = struct.unpack_from("<H", buf, pos + 22)[0]
        n = struct.unpack_from("<I", buf, pos + 26)[0]
        return pen == PEN_MARKER and _plausible(x0) and _plausible(y0) and 0 < n <= MAX_POINTS_PER_STROKE \
            and pos + 30 + 5 * n <= len(buf)
    if flags == FLAG_PLAIN:
        pen, n = struct.unpack_from("<II", buf, pos + 12)
        return pen == PEN_MARKER and 0 < n <= MAX_POINTS_PER_STROKE and pos + 20 + 16 * n <= len(buf)
    return False


def _read_stroke(c: _Cursor, ink: BinkInk, record: int, budget: Optional[PointBudget]) -> Optional[str]:
    """Read one record at the cursor.  Returns None when fine, else why reading must stop."""
    start = c.pos
    flags = c.u32()
    if flags == FLAG_ERASED:
        ink.erased += 1
        return None
    if flags == FLAG_PACKED:
        t0 = c.u64()
        x0, y0 = c.f32(), c.f32()
        c.raw(6)  # u16 tilt, u16 pen (0x0C49), u16 0
        n = c.u32()
        if not 0 < n <= MAX_POINTS_PER_STROKE:
            c.pos = start
            return "a stroke record is damaged (implausible point count)"
        c.need(5 * n)
        if not (_plausible(x0) and _plausible(y0)):
            c.pos += 5 * n
            ink.unusable += 1
            return None
        if budget is not None and not budget.take(n):
            c.pos += 5 * n
            return None
        dx = struct.unpack_from(f"<{n}h", c.buf, c.pos)
        dy = struct.unpack_from(f"<{n}h", c.buf, c.pos + 2 * n)
        force = bytes(c.buf[c.pos + 4 * n:c.pos + 5 * n])
        c.pos += 5 * n
        ux, uy = ink.units_per_mm
        xs = [x0 + d / ux for d in accumulate(dx)]
        ys = [y0 + d / uy for d in accumulate(dy)]
        ink.strokes.append(BinkStroke(record, xs, ys, force, t0))
        return None
    if flags == FLAG_PLAIN:
        t0 = c.u64()
        c.u32()  # pen field
        n = c.u32()
        if not 0 < n <= MAX_POINTS_PER_STROKE:
            c.pos = start
            return "a stroke record is damaged (implausible point count)"
        c.need(16 * n)
        coords = struct.unpack_from(f"<{2 * n}f", c.buf, c.pos)
        c.pos += 16 * n  # x/y pairs, then the force and time channels (not used)
        if not all(_plausible(v) for v in coords):
            ink.unusable += 1
            return None
        if budget is not None and not budget.take(n):
            return None
        ink.strokes.append(BinkStroke(record, list(coords[0::2]), list(coords[1::2]), None, t0))
        return None
    c.pos = start
    return f"a stroke record of an unknown type (0x{flags:08x})"


def _read_tags(c: _Cursor, ink: BinkInk) -> None:
    c.u32()
    count = c.u32()
    c.u8()
    if count > MAX_TAGS:
        raise _Truncated()
    for i in range(count):
        if i > 0:
            c.raw(12)  # u32 kind, u32 id, u32 0
        name = c.string(4096)
        groups = c.u32()
        if groups > MAX_GROUPS:
            raise _Truncated()
        spans = []
        for _ in range(groups):
            _, _, first, _, _, last = c.unpack("<HHIHHI", 16)
            spans.append((min(first, last), max(first, last)))
        text = c.string(1 << 20)
        for first, last in spans:
            ink.tags.append(BinkTag(name, first, last, text))


def parse_bink(data: bytes, budget: Optional[PointBudget] = None) -> BinkInk:
    """Decode one ``ink.bink``.  Defects become lines in :attr:`BinkInk.problems`."""
    data = bytes(data)
    if not data.startswith(MAGIC):
        raise BinkError("not BINK ink (bad magic)")
    ink = BinkInk()
    c = _Cursor(data)
    count: Optional[int]
    try:
        count = _read_header(c, ink)
    except _Truncated:
        count = None
    if count:
        # The header parsed; a known record type must follow it, else it was a variant whose
        # length we misjudged and the scan below locates the records instead.
        flags = struct.unpack_from("<I", data, c.pos)[0] if c.pos + 4 <= len(data) else None
        if flags not in (FLAG_PACKED, FLAG_PLAIN, FLAG_ERASED):
            count = None
    if count is None:
        ink.scanned = True
        ink.version, ink.channels, ink.precision = 0, [], (0, 0)  # whatever the failed header parse left
        ink.units_per_mm = (DEFAULT_UNITS_PER_MM, DEFAULT_UNITS_PER_MM)
        found = next((p for p in range(len(MAGIC), min(len(data), SCAN_WINDOW)) if _record_at(data, p)), None)
        if found is None:
            if len(data) > 64:
                ink.problems.append("ink header not understood and no stroke found")
            return ink
        c.pos = found
    if ink.version not in (0, 5):
        ink.problems.append(f"BINK version {ink.version} (the decoder was written for version 5)")
    if ink.precision[0] not in (0, 1000) or ink.precision[1] not in (0, 1000):
        ink.problems.append(f"BINK precision {ink.precision[0]} x {ink.precision[1]} differs from the 1000 of every "
                            f"known file; coordinates assume 2/precision mm per unit (unverified)")
    record = 0
    stop: Optional[str] = None
    try:
        while count is None or record < count:
            if count is None and not (_record_at(data, c.pos) or _first_is_erased(data, c.pos)):
                break  # scan mode: the record stream ends at the first implausible record
            stop = _read_stroke(c, ink, record, budget)
            if stop is not None:
                break
            record += 1
            if record >= MAX_RECORDS:
                break
    except _Truncated:
        stop = "the ink data is truncated"
    ink.records = record
    if ink.unusable:
        ink.problems.append("stroke records with unusable coordinates were skipped")
    if stop is not None:
        ink.problems.append(stop + "; the rest of the page's ink was not read")
        return ink  # the tag table cannot be located after an unreadable record
    try:
        _read_tags(c, ink)
    except _Truncated:
        ink.problems.append("pen style table truncated; strokes without a readable style use the default pen")
    return ink


def _first_is_erased(buf: bytes, pos: int) -> bool:
    return pos + 4 <= len(buf) and struct.unpack_from("<I", buf, pos)[0] == FLAG_ERASED
