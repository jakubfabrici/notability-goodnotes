"""Troy Hanson ``tpl`` images: the geometry payload of GoodNotes ink strokes.

A stroke's ``#2`` field holds an Apple LZ4 frame (``gnnote.applelz4``); decompressed it is one
TPL image (``docs/goodnotes-stroke.md`` sections 2, 3, 5 and 8)::

    0   't' 'p' 'l'
    3   flags: bit 0 = big-endian (always 0 in GoodNotes files; big-endian images are rejected)
    4   u32 LE total image length (header + format + values; must equal the LZ4 decoded size)
    8   format string, ASCII, NUL-terminated
    ... packed values in format order, little-endian, no padding

Format characters: ``c`` int8, ``j``/``v`` int16/uint16, ``i``/``u`` int32/uint32, ``I``/``U``
int64/uint64, ``f`` float64, ``A(x)`` = u32 count followed by ``count`` elements of ``x``,
``S(...)`` = a packed struct with no count.  Every coordinate, radius and width is stored as a
``u`` whose bits are an IEEE-754 float32.

The five format strings GoodNotes writes (census of 5677 strokes) and their typed decoders:

* ``vuA(v)A(S(uu))A(S(uuuu))vA(f)`` and the older ``vuA(v)A(S(uu))A(S(uuuu))`` -> FlatStroke
  (constant width ``W``, flags ``[0, 1, 1, ...]``, one start point per 0-flag, one quadratic
  Bezier segment ``(cx, cy, ex, ey)`` per 1-flag, then ``v = 1`` and an empty ``A(f)`` dash array).
* ``vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)`` and the ``vu...`` variant with a leading width
  word -> RibbonStroke (per-point radius ``r``; flags 0/2/4 take one start tuple from the
  start pool, flags 1/3/5 take one panel = two points from the panel pool; the remaining
  seven arrays are a pre-built CGPath outline: commands per panel, command codes, MoveTo
  points, an always-empty pool, cubic control/end points, arcs ``(cx, cy, r, a0, a1)`` and
  per-arc clockwise flags).
* ``vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)`` -> PencilStroke
  (tool 25: ``W``, flags, start tuples ``(x, y, a, b, c)`` and segment tuples
  ``(seed, x1, y1, a, b, c, x2, y2, a, b, c)`` where ``a, b, c`` are pi/6, pi/3, 0 in GoodNotes 5
  files and vary (tilt / force data) in GoodNotes 6 files).

An image whose flag array is empty (the 62-byte "tombstone" written for erased elements)
decodes to ``None``.  Unknown format strings raise ``ValueError`` rather than being guessed.

``encode_flat`` reproduces GoodNotes' byte shape exactly: ``encode_flat(decode(x)) == x`` for
every app-written flat stroke in the reference corpus.  ``FlatStroke.from_polyline`` applies
the writer recipe of section 8 (odd point count, control point at the segment midpoint).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple, Union

MAGIC = b"tpl\x00"
_FLAG_BIGENDIAN = 0x01

FLAT_FORMAT = "vuA(v)A(S(uu))A(S(uuuu))vA(f)"
FLAT_FORMAT_SHORT = "vuA(v)A(S(uu))A(S(uuuu))"
RIBBON_FORMAT = "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"
RIBBON_FORMAT_WIDTH = "vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"
PENCIL_FORMAT = "vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)"
KNOWN_FORMATS = (FLAT_FORMAT, FLAT_FORMAT_SHORT, RIBBON_FORMAT, RIBBON_FORMAT_WIDTH, PENCIL_FORMAT)

_SCALARS = {"c": "<b", "j": "<h", "v": "<H", "i": "<i", "u": "<I", "I": "<q", "U": "<Q", "f": "<d"}

XY = Tuple[float, float]
Quad = Tuple[float, float, float, float]
XYR = Tuple[float, float, float]


def f32_from_bits(u: int) -> float:
    return struct.unpack("<f", struct.pack("<I", u))[0]


def f32_to_bits(f: float) -> int:
    return struct.unpack("<I", struct.pack("<f", f))[0]


# --------------------------------------------------------------------------- generic image


def parse_format(fmt: str) -> list:
    """Parse a format string into nodes: ``'v'`` or ``('A', [nodes])`` or ``('S', [nodes])``."""

    def parse(pos: int, closing: bool) -> Tuple[list, int]:
        nodes: list = []
        while pos < len(fmt):
            ch = fmt[pos]
            pos += 1
            if ch == ")":
                if not closing:
                    raise ValueError(f"unbalanced ')' in TPL format {fmt!r}")
                return nodes, pos
            if ch in "AS":
                if pos >= len(fmt) or fmt[pos] != "(":
                    raise ValueError(f"'{ch}' without '(' in TPL format {fmt!r}")
                sub, pos = parse(pos + 1, True)
                if not sub:
                    raise ValueError(f"empty '{ch}()' in TPL format {fmt!r}")
                nodes.append((ch, sub))
            elif ch in _SCALARS:
                nodes.append(ch)
            else:
                raise ValueError(f"unknown TPL format character {ch!r} in {fmt!r}")
        if closing:
            raise ValueError(f"unterminated '(' in TPL format {fmt!r}")
        return nodes, pos

    nodes, _ = parse(0, False)
    return nodes


def _decode_node(node: Any, data: bytes, pos: int) -> Tuple[Any, int]:
    if isinstance(node, str):
        code = _SCALARS[node]
        size = struct.calcsize(code)
        if pos + size > len(data):
            raise ValueError("TPL image truncated inside a scalar")
        return struct.unpack_from(code, data, pos)[0], pos + size
    kind, sub = node
    if kind == "S":
        values = []
        for n in sub:
            v, pos = _decode_node(n, data, pos)
            values.append(v)
        return tuple(values), pos
    if pos + 4 > len(data):
        raise ValueError("TPL image truncated inside an array count")
    count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    items: list = []
    for _ in range(count):
        if len(sub) == 1:
            v, pos = _decode_node(sub[0], data, pos)
        else:
            parts = []
            for n in sub:
                x, pos = _decode_node(n, data, pos)
                parts.append(x)
            v = tuple(parts)
        items.append(v)
    return items, pos


def _encode_node(node: Any, value: Any) -> bytes:
    if isinstance(node, str):
        try:
            return struct.pack(_SCALARS[node], value)
        except struct.error as exc:
            raise ValueError(f"TPL value {value!r} does not fit format {node!r}") from exc
    kind, sub = node
    if kind == "S":
        if len(value) != len(sub):
            raise ValueError("TPL struct value has the wrong arity")
        return b"".join(_encode_node(n, x) for n, x in zip(sub, value))
    out = bytearray(struct.pack("<I", len(value)))
    for item in value:
        if len(sub) == 1:
            out += _encode_node(sub[0], item)
        else:
            if len(item) != len(sub):
                raise ValueError("TPL struct array element has the wrong arity")
            out += b"".join(_encode_node(n, x) for n, x in zip(sub, item))
    return bytes(out)


@dataclass
class TplImage:
    """A decoded image: the format string and one Python value per top-level format node."""

    fmt: str
    values: List[Any]


def decode_image(data: bytes) -> TplImage:
    """Parse any little-endian TPL image generically.  Raises ``ValueError`` when malformed."""
    data = bytes(data)
    if len(data) < 9 or data[:3] != b"tpl":
        raise ValueError("not a TPL image (missing 'tpl' magic)")
    if data[3] & _FLAG_BIGENDIAN:
        raise ValueError("big-endian TPL images are not supported")
    (size,) = struct.unpack_from("<I", data, 4)
    if size != len(data):
        raise ValueError(f"TPL size field {size} does not match the image length {len(data)}")
    end = data.find(b"\x00", 8)
    if end < 0:
        raise ValueError("TPL format string is not NUL-terminated")
    fmt = data[8:end].decode("ascii", errors="strict")
    nodes = parse_format(fmt)
    pos = end + 1
    values: List[Any] = []
    for node in nodes:
        v, pos = _decode_node(node, data, pos)
        values.append(v)
    if pos != len(data):
        raise ValueError(f"TPL image has {len(data) - pos} trailing bytes")
    return TplImage(fmt, values)


def encode_image(image: TplImage) -> bytes:
    """Serialise an image; ``encode_image(decode_image(x)) == x`` for every valid ``x``."""
    nodes = parse_format(image.fmt)
    if len(nodes) != len(image.values):
        raise ValueError(f"TPL format {image.fmt!r} needs {len(nodes)} values, got {len(image.values)}")
    body = b"".join(_encode_node(n, v) for n, v in zip(nodes, image.values))
    fmt_bytes = image.fmt.encode("ascii") + b"\x00"
    return MAGIC + struct.pack("<I", 8 + len(fmt_bytes) + len(body)) + fmt_bytes + body


# --------------------------------------------------------------------------- typed strokes


@dataclass
class FlatStroke:
    """Constant-width stroke: ``start`` then quadratic segments ``(cx, cy, ex, ey)``.

    ``width`` is GoodNotes' ``W`` (full width, canvas units; ``W / 2`` points on the page).
    ``flags`` / ``extra_starts`` / ``version`` / ``has_trailer`` / ``trailer_word`` / ``dash``
    carry the raw words needed for a byte-exact re-encode; a writer leaves them at their
    defaults.  Multi-sub-path strokes (several 0-flags) keep their later start points in
    ``extra_starts``; ``subpaths()`` applies the flag-driven consumption rule.
    """

    width: float
    start: XY
    quads: List[Quad]
    flags: Optional[List[int]] = None
    extra_starts: List[XY] = field(default_factory=list)
    version: int = 2
    has_trailer: bool = True
    trailer_word: int = 1
    dash: List[float] = field(default_factory=list)

    @property
    def starts(self) -> List[XY]:
        return [self.start] + list(self.extra_starts)

    def effective_flags(self) -> List[int]:
        if self.flags is not None:
            return list(self.flags)
        return [0] + [1] * len(self.quads)

    def subpaths(self) -> List[Tuple[XY, List[Quad]]]:
        """Split into ``(start, quads)`` sub-paths following the flag array."""
        starts = self.starts
        out: List[Tuple[XY, List[Quad]]] = []
        si = qi = 0
        for flag in self.effective_flags():
            if flag == 0:
                if si >= len(starts):
                    raise ValueError("flat stroke has more 0-flags than start points")
                out.append((starts[si], []))
                si += 1
            else:
                if qi >= len(self.quads):
                    raise ValueError("flat stroke has more 1-flags than quads")
                if not out:
                    raise ValueError("flat stroke begins with a continuation flag")
                out[-1][1].append(self.quads[qi])
                qi += 1
        if si != len(starts) or qi != len(self.quads):
            raise ValueError("flat stroke flag array does not consume all points")
        return out

    def anchors(self) -> List[XY]:
        """On-curve points of the first sub-path: start + every segment end."""
        start, quads = self.subpaths()[0]
        return [start] + [(ex, ey) for _cx, _cy, ex, ey in quads]

    def polyline(self) -> List[XY]:
        """All stored points in order (control points included), the legacy 'centre-line' view."""
        out: List[XY] = []
        for start, quads in self.subpaths():
            out.append(start)
            for cx, cy, ex, ey in quads:
                out.append((cx, cy))
                out.append((ex, ey))
        return out

    def cubic_controls(self) -> List[Tuple[XY, XY]]:
        """Degree-elevated cubic handles ``(c1, c2)`` per segment of the first sub-path."""
        start, quads = self.subpaths()[0]
        out: List[Tuple[XY, XY]] = []
        px, py = start
        for cx, cy, ex, ey in quads:
            c1 = (px + 2.0 / 3.0 * (cx - px), py + 2.0 / 3.0 * (cy - py))
            c2 = (ex + 2.0 / 3.0 * (cx - ex), ey + 2.0 / 3.0 * (cy - ey))
            out.append((c1, c2))
            px, py = ex, ey
        return out

    @classmethod
    def from_polyline(cls, points: Sequence[XY], width: float, min_segment: float = 0.3) -> "FlatStroke":
        """Writer recipe for a polyline (design 4.4): every segment ``p_i -> p_i+1`` becomes one
        straight quad whose control is the segment midpoint, so N points store ``2N - 1`` points
        (always odd).  A single point becomes a ``min_segment``-long dash."""
        pts = [(float(x), float(y)) for x, y in points]
        if not pts:
            raise ValueError("a stroke needs at least one point")
        if len(pts) == 1:
            x, y = pts[0]
            pts.append((x + min_segment, y))
        quads = [((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0, q[0], q[1]) for p, q in zip(pts, pts[1:])]
        return cls(width=float(width), start=pts[0], quads=quads)

    @classmethod
    def from_point_pairs(cls, points: Sequence[XY], width: float, min_segment: float = 0.3) -> "FlatStroke":
        """The literal section 8 recipe: odd point count (the last point is duplicated when even),
        then consecutive point pairs become quads ``(p1, p2)``.  Every odd point acts as the
        quadratic control, which is only right for dense app-like point lists; prefer
        ``from_polyline`` for real polylines."""
        pts = [(float(x), float(y)) for x, y in points]
        if not pts:
            raise ValueError("a stroke needs at least one point")
        if len(pts) == 1:
            x, y = pts[0]
            pts = [(x, y), (x + min_segment, y)]
        if len(pts) % 2 == 0:
            pts.append(pts[-1])
        quads = [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(1, len(pts), 2)]
        return cls(width=float(width), start=pts[0], quads=quads)

    @classmethod
    def from_quadratics(cls, start: XY, quads: Sequence[Quad], width: float) -> "FlatStroke":
        """Build from explicit quadratic Bezier segments ``(cx, cy, ex, ey)``."""
        return cls(width=float(width), start=(float(start[0]), float(start[1])),
                   quads=[tuple(float(v) for v in q) for q in quads])  # type: ignore[misc]


@dataclass
class RibbonStroke:
    """Per-point-radius stroke (``#3 = 1`` fountain / brush / marker family).

    ``points`` are ``(x, y, r)`` on-curve points in drawing order, ``r`` the half-width in
    canvas units; ``subpaths`` splits them at every pen-down flag.  ``start_extra`` /
    ``panel_extra`` hold the floats beyond ``(x, y, r)`` of the 4/9-float variants.  The CGPath
    pools (``panel_counts`` ... ``arc_flags``) are kept raw (floats already converted).
    """

    points: List[XYR]
    subpaths: List[List[XYR]]
    flags: List[int]
    width: Optional[float] = None
    version: int = 2
    start_extra: List[List[float]] = field(default_factory=list)
    panel_extra: List[List[float]] = field(default_factory=list)
    panel_counts: List[int] = field(default_factory=list)
    commands: List[int] = field(default_factory=list)
    moves: List[float] = field(default_factory=list)
    pool7: List[int] = field(default_factory=list)
    cubics: List[float] = field(default_factory=list)
    arcs: List[float] = field(default_factory=list)
    arc_flags: List[int] = field(default_factory=list)
    image: Optional[TplImage] = None


@dataclass
class PencilStroke:
    """Tool-25 (pencil) stroke: ``points`` ``(x, y)``, ``attrs`` three floats per point, ``width``.

    ``seeds`` holds the leading uint32 of every segment tuple; ``trailing`` the five raw arrays
    after the segments (empty except on a few strokes)."""

    width: float
    points: List[XY]
    attrs: List[Tuple[float, float, float]]
    subpaths: List[List[XY]]
    flags: List[int]
    seeds: List[int] = field(default_factory=list)
    version: int = 1
    trailing: List[Any] = field(default_factory=list)
    image: Optional[TplImage] = None


Stroke = Union[FlatStroke, RibbonStroke, PencilStroke]


def _ints(values: Any, what: str) -> List[int]:
    if not isinstance(values, list) or not all(isinstance(v, int) for v in values):
        raise ValueError(f"TPL {what} is not an integer array")
    return values


def _decode_flat(image: TplImage) -> Optional[FlatStroke]:
    short = image.fmt == FLAT_FORMAT_SHORT
    v = image.values
    version = v[0]
    width = f32_from_bits(v[1])
    flags = _ints(v[2], "flag array")
    starts = [(f32_from_bits(x), f32_from_bits(y)) for x, y in v[3]]
    quads = [tuple(f32_from_bits(b) for b in q) for q in v[4]]
    if not flags and not starts and not quads:
        return None
    if not starts:
        raise ValueError("flat stroke without a start point")
    stroke = FlatStroke(width=width, start=starts[0], quads=quads, flags=flags,  # type: ignore[arg-type]
                        extra_starts=starts[1:], version=version, has_trailer=not short)
    if not short:
        stroke.trailer_word = v[5]
        stroke.dash = list(v[6])
    stroke.subpaths()  # validates the flag/point consistency
    return stroke


def _stride(total: int, count: int, what: str) -> int:
    if count == 0:
        if total:
            raise ValueError(f"ribbon stroke has {what} data but no matching flags")
        return 0
    if total % count:
        raise ValueError(f"ribbon stroke {what} pool ({total}) is not a multiple of its flag count ({count})")
    return total // count


def _decode_ribbon(image: TplImage) -> Optional[RibbonStroke]:
    v = image.values
    off = 1 if image.fmt == RIBBON_FORMAT_WIDTH else 0
    width = f32_from_bits(v[1]) if off else None
    flags = _ints(v[1 + off], "flag array")
    start_pool = [f32_from_bits(u) for u in v[2 + off]]
    panel_pool = [f32_from_bits(u) for u in v[3 + off]]
    if not flags:
        return None
    n_start = sum(1 for f in flags if f % 2 == 0)
    n_panel = len(flags) - n_start
    s_stride = _stride(len(start_pool), n_start, "start")
    p_stride = _stride(len(panel_pool), n_panel, "panel")
    if n_start and s_stride < 3:
        raise ValueError("ribbon start tuples are shorter than (x, y, r)")
    if n_panel and p_stride < 6:
        raise ValueError("ribbon panel tuples are shorter than two (x, y, r) points")
    points: List[XYR] = []
    subpaths: List[List[XYR]] = []
    start_extra: List[List[float]] = []
    panel_extra: List[List[float]] = []
    si = pi = 0
    for flag in flags:
        if flag % 2 == 0:
            tup = start_pool[si * s_stride:(si + 1) * s_stride]
            si += 1
            pt = (tup[0], tup[1], tup[2])
            start_extra.append(list(tup[3:]))
            subpaths.append([pt])
            points.append(pt)
        else:
            if not subpaths:
                raise ValueError("ribbon stroke begins with a continuation flag")
            tup = panel_pool[pi * p_stride:(pi + 1) * p_stride]
            pi += 1
            p1 = (tup[0], tup[1], tup[2])
            p2 = (tup[3], tup[4], tup[5])
            panel_extra.append(list(tup[6:]))
            subpaths[-1].extend((p1, p2))
            points.extend((p1, p2))
    stroke = RibbonStroke(points=points, subpaths=subpaths, flags=flags, width=width, version=v[0],
                          start_extra=start_extra, panel_extra=panel_extra,
                          panel_counts=_ints(v[4 + off], "panel counts"),
                          commands=_ints(v[5 + off], "commands"),
                          moves=[f32_from_bits(u) for u in v[6 + off]],
                          pool7=_ints(v[7 + off], "pool 7"),
                          cubics=[f32_from_bits(u) for u in v[8 + off]],
                          arcs=[f32_from_bits(u) for u in v[9 + off]],
                          arc_flags=_ints(v[10 + off], "arc flags"), image=image)
    return stroke


def _decode_pencil(image: TplImage) -> Optional[PencilStroke]:
    v = image.values
    width = f32_from_bits(v[1])
    flags = _ints(v[2], "flag array")
    starts = v[3]
    segments = v[4]
    if not flags:
        return None
    points: List[XY] = []
    attrs: List[Tuple[float, float, float]] = []
    subpaths: List[List[XY]] = []
    seeds: List[int] = []
    si = pi = 0
    for flag in flags:
        if flag % 2 == 0:
            if si >= len(starts):
                raise ValueError("pencil stroke has more pen-down flags than start tuples")
            t = [f32_from_bits(u) for u in starts[si]]
            si += 1
            points.append((t[0], t[1]))
            attrs.append((t[2], t[3], t[4]))
            subpaths.append([(t[0], t[1])])
        else:
            if pi >= len(segments) or not subpaths:
                raise ValueError("pencil stroke flag array does not match its segment tuples")
            raw = segments[pi]
            pi += 1
            seeds.append(raw[0])
            t = [f32_from_bits(u) for u in raw[1:]]
            for k in (0, 5):
                points.append((t[k], t[k + 1]))
                attrs.append((t[k + 2], t[k + 3], t[k + 4]))
                subpaths[-1].append((t[k], t[k + 1]))
    if si != len(starts) or pi != len(segments):
        raise ValueError("pencil stroke flag array does not consume all tuples")
    return PencilStroke(width=width, points=points, attrs=attrs, subpaths=subpaths, flags=flags,
                        seeds=seeds, version=v[0], trailing=list(v[5:]), image=image)


def decode(data: bytes) -> Optional[Stroke]:
    """Decode a stroke geometry image; ``None`` for an element without points.

    Raises ``ValueError`` for malformed images and for format strings outside the five
    GoodNotes writes (the caller turns that into a warning and skips the element).
    """
    image = decode_image(data)
    if image.fmt in (FLAT_FORMAT, FLAT_FORMAT_SHORT):
        return _decode_flat(image)
    if image.fmt in (RIBBON_FORMAT, RIBBON_FORMAT_WIDTH):
        return _decode_ribbon(image)
    if image.fmt == PENCIL_FORMAT:
        return _decode_pencil(image)
    raise ValueError(f"unsupported GoodNotes stroke format {image.fmt!r}")


def encode_flat(stroke: FlatStroke) -> bytes:
    """Serialise a FlatStroke exactly as GoodNotes does (section 8 recipe).

    Layout: ``tpl\\0 u32 size "vuA(v)A(S(uu))A(S(uuuu))vA(f)\\0" <H version> <I bits(W)>
    <I N+1> <H flags...> <I starts> <I bits(x0)><I bits(y0)> <I N> N x 4<I bits> <H 1> <I 0>``.
    """
    flags = stroke.effective_flags()
    starts = stroke.starts
    if flags.count(0) != len(starts) or len(flags) - flags.count(0) != len(stroke.quads):
        raise ValueError("flat stroke flags do not match its start points and quads")
    if any(f not in (0, 1) for f in flags):
        raise ValueError("flat stroke flags must be 0 or 1")
    if not stroke.quads and not stroke.flags:
        raise ValueError("flat stroke needs at least one segment")
    for q in stroke.quads:
        if len(q) != 4:
            raise ValueError("flat stroke quads must be (cx, cy, ex, ey)")
    for v in (stroke.width, *starts[0]):
        if not math.isfinite(v):
            raise ValueError("flat stroke values must be finite")
    values: List[Any] = [
        stroke.version,
        f32_to_bits(stroke.width),
        flags,
        [(f32_to_bits(x), f32_to_bits(y)) for x, y in starts],
        [tuple(f32_to_bits(c) for c in q) for q in stroke.quads],
    ]
    fmt = FLAT_FORMAT_SHORT
    if stroke.has_trailer:
        fmt = FLAT_FORMAT
        values += [stroke.trailer_word, [float(d) for d in stroke.dash]]
    return encode_image(TplImage(fmt, values))


def encode_empty_flat(width: float = 0.0) -> bytes:
    """The 62-byte pointless image GoodNotes writes for erased elements (readers skip it)."""
    return encode_image(TplImage(FLAT_FORMAT, [2, f32_to_bits(width), [], [], [], 1, []]))
