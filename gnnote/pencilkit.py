"""Apple PencilKit ``PKDrawing`` data -> neutral strokes (read only, standard library only).

``PKDrawing.dataRepresentation()`` is how PencilKit-based apps store ink: CollaNote 1.x pages
(the ``drawing`` field of a ``.cpage``), Apple Notes and Freeform drawings, and third-party
apps.  This module decodes such a blob into a small app-neutral structure
(:class:`PKStroke` / :class:`PKPoint`) and converts those into :class:`gnnote.model.Stroke`
objects (:func:`to_model_stroke`).  It knows nothing about any particular app.

Byte layout (inkterop's ``docs/formats/pencilkit.md``, validated there against Apple's framework,
plus the additions found on 854 real iOS drawings, see ``docs/collanote.md`` section 5):

Container
    ``b"wrd\\xf0"`` magic, a little-endian ``u16`` container version (1; 2 for drawings that use
    the iOS 17 inks), then one protobuf message (wire format, ``gnnote.protobuf``).

Drawing (top-level message)
    ``#4`` (repeated) ink table ``{#1 colour {#1 r, #2 g, #3 b, #4 a} f32, #2 ink identifier
    ("com.apple.ink.pen" ...), #3 3, [#4 variant string, e.g. "fixed-width" (version 2)], #8
    f64}``; ``#5`` (repeated) strokes in z-order; ``#1`` / ``#2`` / ``#3`` / ``#6`` / ``#7`` /
    ``#8`` replica, CRDT and cached-bounds bookkeeping (ignored).

Stroke (``#5``)
    ``{#1 uuid, #2 / #3 CRDT ids, #4 ink-table index (varint), #5 path, #6 renderBounds {#1 x,
    #2 y, #3 w, #4 h} f32, [#7 transform {#1 a, #2 b, #3 c, #4 d, #5 tx, #6 ty} f32], #8 opaque}``.
    ``#7`` is the ``CGAffineTransform`` a lasso move leaves on the stroke; positions are
    ``(a x + c y + tx, b x + d y + ty)``.  A stroke without a path (``#5``) is a deleted stroke
    (a CRDT tombstone) and is skipped.

Path (``#5.#5``)
    ``{#1 uuid, #2 creation date (f64, seconds since 2001-01-01), #3 point count, #4 per-point
    channel mask, #5 constant channel mask, #6 constant block, #7 point records, [#9 0]}``.
    The masks are complementary (``#4 | #5 == 0x7FF``, ``#4 & #5 == 0``).  A channel set in
    ``#5`` is stored once in the constant block; the others are stored per point.  Blocks hold
    the channels of their mask in ascending bit order, packed little-endian without padding:

    ====  ===============  =====  ==========================================
    bit   channel          wire   value
    ====  ===============  =====  ==========================================
    0     location         2 f32  x, y (canvas points)
    1     time offset      f32    seconds since the path's creation date
    2     width            f32    size.width
    3     aspect           u16    size.height = width * v / 1000
    4     (unknown)        u16    always 0
    5     force            u16    v / 1000
    6     azimuth          u16    2 pi v / 65535 - pi (radians)
    7     altitude         u16    (1 - v / 65535) * pi / 2 (radians)
    8     opacity          u16    2 v / 65535
    9     secondary width  f32    width * secondaryScale
    10    (unknown)        u16    always 0
    ====  ===============  =====  ==========================================

Hardening: at most :data:`MAX_STROKES` strokes, :data:`MAX_POINTS` points and
:data:`MAX_INKS` inks per drawing; a record count must match its byte length exactly.  A blob
that is not a PKDrawing raises :class:`ValueError` (and nothing else); a single stroke that
cannot be decoded is skipped and counted in :attr:`PKDrawing.damaged`.
"""
from __future__ import annotations

import math
import statistics
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import protobuf
from .model import Point, Stroke

__all__ = [
    "MAGIC", "CF_EPOCH_OFFSET", "MAX_STROKES", "MAX_POINTS", "MAX_INKS",
    "PKInk", "PKPoint", "PKStroke", "PKDrawing",
    "is_pkdrawing", "parse_pkdrawing", "decode_pkdrawing", "ink_kind", "to_model_stroke", "to_model_strokes",
]

MAGIC = b"wrd\xf0"
CF_EPOCH_OFFSET = 978307200.0  # seconds from 1970-01-01 to 2001-01-01 (CFAbsoluteTime epoch)
MAX_STROKES = 200_000  # stroke records per drawing (tombstones included)
MAX_POINTS = 2_000_000  # points per drawing
MAX_INKS = 10_000  # ink-table entries per drawing
_MAX_SUBFIELDS = 4_096  # fields of one ink / stroke / path message (real ones have about ten)
_FULL_MASK = 0x7FF
_RGBA = Tuple[float, float, float, float]

# bit -> (attribute, struct code, decoder of the raw value)
_CHANNELS: Tuple[Tuple[int, str, str], ...] = (
    (0, "location", "ff"),
    (1, "time", "f"),
    (2, "width", "f"),
    (3, "aspect", "H"),
    (4, "unknown4", "H"),
    (5, "force", "H"),
    (6, "azimuth", "H"),
    (7, "altitude", "H"),
    (8, "opacity", "H"),
    (9, "secondary_width", "f"),
    (10, "unknown10", "H"),
)
_DECODERS: Dict[str, Callable[[float], float]] = {
    "aspect": lambda v: v / 1000.0,
    "force": lambda v: v / 1000.0,
    "azimuth": lambda v: 2.0 * math.pi * v / 65535.0 - math.pi,
    "altitude": lambda v: (1.0 - v / 65535.0) * math.pi / 2.0,
    "opacity": lambda v: 2.0 * v / 65535.0,
}

# PencilKit ink identifier -> (model kind, model pen name).  pen / pencil / marker occur in real
# drawings; the others are the remaining PKInkType cases, not yet seen serialised.
_INKS: Dict[str, Tuple[str, Optional[str]]] = {
    "com.apple.ink.pen": ("pen", None),
    "com.apple.ink.monoline": ("pen", None),
    "com.apple.ink.fountainpen": ("pen", "fountain"),
    "com.apple.ink.pencil": ("pen", "pencil"),
    "com.apple.ink.crayon": ("pen", "pencil"),
    "com.apple.ink.watercolor": ("pen", "brush"),
    "com.apple.ink.marker": ("highlighter", "marker"),
}


@dataclass
class PKInk:
    """One ink-table entry: identifier (``com.apple.ink.pen`` ...), colour and variant."""

    identifier: str
    color: _RGBA = (0.0, 0.0, 0.0, 1.0)
    variant: str = ""  # ink field #4 ("fixed-width" on the iOS 17 inks of version-2 drawings)


@dataclass(slots=True)
class PKPoint:
    """One stored point of a stroke path (``PKStrokePoint``); lengths in canvas points."""

    x: float
    y: float
    time: float = 0.0  # seconds since the stroke path's creation date
    width: float = 0.0  # size.width
    height: float = 0.0  # size.height
    force: float = 0.0
    azimuth: float = 0.0  # radians, -pi .. pi
    altitude: float = math.pi / 2  # radians, pi / 2 = perpendicular
    opacity: float = 1.0
    secondary_scale: float = 1.0


@dataclass
class PKStroke:
    """A decoded stroke.  ``points`` already include ``transform``."""

    ink: str  # ink identifier, e.g. "com.apple.ink.pen" ("" when the table entry has none)
    color: _RGBA
    points: List[PKPoint]
    transform: Optional[Tuple[float, float, float, float, float, float]] = None  # (a, b, c, d, tx, ty) as stored
    render_bounds: Optional[Tuple[float, float, float, float]] = None  # (x, y, w, h) as stored
    created: Optional[float] = None  # CFAbsoluteTime (seconds since 2001-01-01) of the path
    ink_variant: str = ""


@dataclass
class PKDrawing:
    """Everything :func:`parse_pkdrawing` recovers from one blob."""

    version: int
    inks: List[Optional[PKInk]] = field(default_factory=list)  # ``None`` for an undecodable entry
    strokes: List[PKStroke] = field(default_factory=list)
    tombstones: int = 0  # stroke records without a path (deleted strokes), skipped
    damaged: int = 0  # stroke records that could not be decoded, skipped
    empty: int = 0  # strokes whose path holds no point, skipped

    @property
    def known_version(self) -> bool:
        return self.version in (1, 2)


# --------------------------------------------------------------------------- decoding


def is_pkdrawing(data: bytes) -> bool:
    """True when ``data`` starts like a PKDrawing (magic plus version)."""
    return len(data) >= 6 and bytes(data[:4]) == MAGIC


def _f32(fields: Sequence[protobuf.Field], number: int, default: float) -> float:
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_FIXED32:
        return default
    value = protobuf.fixed32_float(f)
    return value if math.isfinite(value) else default


def _varint(fields: Sequence[protobuf.Field], number: int) -> Optional[int]:
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_VARINT:
        return None
    return int(f.value)  # type: ignore[arg-type]


def _bytes(fields: Sequence[protobuf.Field], number: int) -> Optional[bytes]:
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_LEN:
        return None
    return f.value  # type: ignore[return-value]


def _clamp01(v: float) -> float:
    return 0.0 if not v > 0.0 else (1.0 if v > 1.0 else v)


def _ink(buf: bytes) -> PKInk:
    fields = protobuf.decode_message(buf, max_fields=_MAX_SUBFIELDS)
    identifier = (_bytes(fields, 2) or b"").decode("utf-8", "replace")
    color: _RGBA = (0.0, 0.0, 0.0, 1.0)
    raw = _bytes(fields, 1)
    if raw is not None:
        comps = protobuf.decode_message(raw, max_fields=_MAX_SUBFIELDS)
        # Apple writes all four components; a missing one is read as 0 (alpha: 1)
        color = (_clamp01(_f32(comps, 1, 0.0)), _clamp01(_f32(comps, 2, 0.0)),
                 _clamp01(_f32(comps, 3, 0.0)), _clamp01(_f32(comps, 4, 1.0)))
    variant = (_bytes(fields, 4) or b"").decode("utf-8", "replace")
    return PKInk(identifier, color, variant)


class _Layout:
    """Struct and slot plan for one channel mask."""

    __slots__ = ("struct", "plan")

    def __init__(self, mask: int):
        codes: List[str] = []
        plan: List[Tuple[str, int, Optional[Callable[[float], float]]]] = []
        index = 0
        for bit, name, code in _CHANNELS:
            if not (mask >> bit) & 1:
                continue
            codes.append(code)
            plan.append((name, index, _DECODERS.get(name)))
            index += len(code)
        self.struct = struct.Struct("<" + "".join(codes))
        self.plan = plan

    def values(self, record: Tuple[float, ...]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for name, index, decode in self.plan:
            if name == "location":
                out["x"] = record[index]
                out["y"] = record[index + 1]
            else:
                value = record[index]
                out[name] = decode(value) if decode is not None else float(value)
        return out


_LAYOUTS: Dict[int, _Layout] = {}


def _layout(mask: int) -> _Layout:
    layout = _LAYOUTS.get(mask)
    if layout is None:
        layout = _LAYOUTS[mask] = _Layout(mask)
    return layout


def _point(values: Dict[str, float]) -> PKPoint:
    width = values.get("width", 0.0)
    secondary = values.get("secondary_width")
    return PKPoint(
        x=values.get("x", 0.0), y=values.get("y", 0.0), time=values.get("time", 0.0), width=width,
        height=width * values.get("aspect", 1.0), force=values.get("force", 0.0),
        azimuth=values.get("azimuth", 0.0), altitude=values.get("altitude", math.pi / 2),
        opacity=values.get("opacity", 1.0),
        secondary_scale=(secondary / width) if secondary is not None and width else 1.0,
    )


def _path(buf: bytes, budget: List[int]) -> Tuple[List[PKPoint], Optional[float]]:
    fields = protobuf.decode_message(buf, max_fields=_MAX_SUBFIELDS)
    count = _varint(fields, 3) or 0
    per_point = _varint(fields, 4)
    constant = _varint(fields, 5)
    if per_point is None or constant is None:
        raise ValueError("path without channel masks")
    if per_point | constant != _FULL_MASK or per_point & constant:
        raise ValueError(f"unexpected channel masks {per_point:#x} / {constant:#x}")
    if count > budget[0]:
        raise _TooLarge(f"more than {MAX_POINTS} points")
    const_raw = _bytes(fields, 6) or b""
    records = _bytes(fields, 7) or b""
    const_layout = _layout(constant)
    if len(const_raw) != const_layout.struct.size:
        raise ValueError("constant block size does not match its mask")
    const = const_layout.values(const_layout.struct.unpack(const_raw)) if const_layout.plan else {}
    layout = _layout(per_point)
    if len(records) != count * layout.struct.size:
        raise ValueError("point records do not match the point count")
    budget[0] -= count
    points: List[PKPoint] = []
    for record in layout.struct.iter_unpack(records):
        values = layout.values(record)
        if const:
            values.update(const)
        points.append(_point(values))
    created_field = protobuf.get(fields, 2)
    created = None
    if created_field is not None and created_field.wire_type == protobuf.WIRE_FIXED64:
        created = protobuf.fixed64_float(created_field)
        if not math.isfinite(created):
            created = None
    return points, created


class _TooLarge(ValueError):
    pass


def _transform(buf: bytes) -> Optional[Tuple[float, float, float, float, float, float]]:
    fields = protobuf.decode_message(buf, max_fields=_MAX_SUBFIELDS)
    # Apple writes all six values; a missing one falls back to the identity's.
    values = tuple(_f32(fields, i, 1.0 if i in (1, 4) else 0.0) for i in range(1, 7))
    return values  # type: ignore[return-value]


def _stroke(buf: bytes, inks: Sequence[Optional[PKInk]], budget: List[int]) -> Optional[PKStroke]:
    fields = protobuf.decode_message(buf, max_fields=_MAX_SUBFIELDS)
    path_field = protobuf.get(fields, 5)
    if path_field is None:
        return None  # deleted stroke (tombstone)
    if path_field.wire_type != protobuf.WIRE_LEN:
        raise ValueError("stroke path is not a message")
    path: bytes = path_field.value  # type: ignore[assignment]
    index = _varint(fields, 4) or 0
    if index >= len(inks) or inks[index] is None:
        raise ValueError(f"stroke refers to ink {index} of {len(inks)}")
    ink = inks[index]
    assert ink is not None
    points, created = _path(path, budget)
    bounds = None
    raw_bounds = _bytes(fields, 6)
    if raw_bounds is not None:
        b = protobuf.decode_message(raw_bounds, max_fields=_MAX_SUBFIELDS)
        bounds = (_f32(b, 1, 0.0), _f32(b, 2, 0.0), _f32(b, 3, 0.0), _f32(b, 4, 0.0))
    transform = None
    raw_transform = _bytes(fields, 7)
    if raw_transform is not None:
        transform = _transform(raw_transform)
        a, b_, c, d, tx, ty = transform
        if (a, b_, c, d, tx, ty) != (1.0, 0.0, 0.0, 1.0, 0.0, 0.0):
            scale = math.sqrt(abs(a * d - b_ * c))
            for p in points:
                x, y = p.x, p.y
                p.x = a * x + c * y + tx
                p.y = b_ * x + d * y + ty
                if scale != 1.0:
                    p.width *= scale
                    p.height *= scale
    return PKStroke(ink=ink.identifier, color=ink.color, points=points, transform=transform,
                    render_bounds=bounds, created=created, ink_variant=ink.variant)


def parse_pkdrawing(data: bytes) -> PKDrawing:
    """Decode a ``PKDrawing.dataRepresentation()`` blob.

    Raises :class:`ValueError` (only) when ``data`` is not a decodable PKDrawing or exceeds the
    limits; strokes that cannot be decoded are skipped and counted.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("parse_pkdrawing expects bytes")
    data = bytes(data)
    if not is_pkdrawing(data):
        raise ValueError("not a PKDrawing (missing the 'wrd\\xf0' header)")
    try:
        return _parse(data)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - the contract: nothing but ValueError escapes (MemoryError too)
        raise ValueError(f"unreadable PKDrawing ({exc.__class__.__name__})") from None


def _parse(data: bytes) -> PKDrawing:
    try:
        version = struct.unpack_from("<H", data, 4)[0]
        top = protobuf.decode_message(data[6:], max_fields=MAX_STROKES + MAX_INKS + _MAX_SUBFIELDS)
    except (ValueError, struct.error) as exc:
        raise ValueError(f"damaged PKDrawing: {exc}") from None
    drawing = PKDrawing(version=version)
    ink_fields = [f for f in top if f.number == 4]
    stroke_fields = [f for f in top if f.number == 5]
    if len(ink_fields) > MAX_INKS:
        raise ValueError(f"PKDrawing holds more than {MAX_INKS} inks")
    if len(stroke_fields) > MAX_STROKES:
        raise ValueError(f"PKDrawing holds more than {MAX_STROKES} strokes")
    for f in ink_fields:
        try:
            drawing.inks.append(_ink(f.value) if f.wire_type == protobuf.WIRE_LEN else None)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - a damaged ink only damages the strokes using it
            drawing.inks.append(None)
    budget = [MAX_POINTS]
    for f in stroke_fields:
        if f.wire_type != protobuf.WIRE_LEN:
            drawing.damaged += 1
            continue
        try:
            stroke = _stroke(f.value, drawing.inks, budget)  # type: ignore[arg-type]
        except _TooLarge:
            raise ValueError(f"PKDrawing holds more than {MAX_POINTS} points") from None
        except Exception:  # noqa: BLE001 - tolerant: one bad record must not lose the drawing
            drawing.damaged += 1
            continue
        if stroke is None:
            drawing.tombstones += 1
        elif not stroke.points:
            drawing.empty += 1
        else:
            drawing.strokes.append(stroke)
    return drawing


def decode_pkdrawing(data: bytes) -> List[PKStroke]:
    """The visible strokes of a PKDrawing blob, in z-order (see :func:`parse_pkdrawing`)."""
    return parse_pkdrawing(data).strokes


# --------------------------------------------------------------------------- model conversion


def ink_kind(identifier: str) -> Tuple[str, Optional[str], bool]:
    """``(kind, pen, known)`` for an ink identifier: marker -> highlighter, others -> pen."""
    found = _INKS.get((identifier or "").lower())
    if found is None:
        return "pen", None, False
    return found[0], found[1], True


def to_model_stroke(stroke: PKStroke, scale: float = 1.0, dx: float = 0.0, dy: float = 0.0,
                    max_abs: Optional[float] = None) -> Optional[Stroke]:
    """``stroke`` as a :class:`gnnote.model.Stroke` polyline.

    Point ``(x, y)`` becomes ``((x + dx) * scale, (y + dy) * scale)``; widths are scaled too.
    The stored points (the control points PencilKit draws its curve through) become the
    polyline.  A per-point opacity below 1 (pencil) lowers the stroke's alpha by its median.
    Non-finite points are dropped, and so are points whose ``|x + dx|`` or ``|y + dy|`` exceeds
    ``max_abs`` when given; ``None`` when no point is left.
    """
    pts: List[Point] = []
    widths: List[float] = []
    opacities: List[float] = []
    for p in stroke.points:
        if max_abs is not None and not (abs(p.x + dx) <= max_abs and abs(p.y + dy) <= max_abs):
            continue
        x = (p.x + dx) * scale
        y = (p.y + dy) * scale
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        w = p.width * scale if math.isfinite(p.width) and p.width > 0 else 0.0
        pts.append(Point(x, y, w))
        widths.append(w)
        opacities.append(p.opacity)
    if not pts:
        return None
    positive = [w for w in widths if w > 0]
    nominal = float(statistics.median(positive)) if positive else max(scale, 1e-3)
    for p in pts:
        if p.width <= 0:
            p.width = nominal
    kind, pen, _known = ink_kind(stroke.ink)
    r, g, b, a = stroke.color
    opacity = float(statistics.median(opacities)) if opacities else 1.0
    if math.isfinite(opacity) and opacity < 0.999:
        a = _clamp01(a * opacity)
    return Stroke(points=pts, color=(r, g, b, a), kind=kind, pen=pen, width=nominal)


def to_model_strokes(strokes: Sequence[PKStroke], scale: float = 1.0, dx: float = 0.0,
                     dy: float = 0.0, max_abs: Optional[float] = None) -> List[Stroke]:
    """:func:`to_model_stroke` over ``strokes``, dropping the ones without a usable point."""
    out: List[Stroke] = []
    for s in strokes:
        converted = to_model_stroke(s, scale, dx, dy, max_abs)
        if converted is not None:
            out.append(converted)
    return out
