"""OneNote ink -> :class:`gnnote.model.Stroke`.

[MS-ONE] does not document ink; the layout below was reverse-engineered by others and
re-verified on real files (``docs/onenote.md`` lists the facts and their sources):

* an InkContainer (on a page, possibly nested) carries the placement: ``OffsetFromParentHoriz
  / Vert`` in half-inches and ``InkScalingX / Y`` (default 1.0); its ``InkData`` object lists
  the strokes;
* a stroke's ``InkPath`` is a stream of 7-bit little-endian varints: the first is twice the
  number of values, every further one is ``±(u >> 1)`` with the sign in bit 0.  The values
  are dimension-major (all X, then all Y, then any further channel) and within a channel the
  first value is absolute and the rest are deltas;
* the stroke's properties name the channels (``InkDimensions``: 32-byte entries of packet
  GUID, minimum, maximum, unit, resolution) and the pen: ``InkWidth`` / ``InkHeight``
  (HIMETRIC = 0.01 mm), ``InkColor`` (COLORREF 0x00BBGGRR, absent = black),
  ``InkTransparency`` (0 or absent = opaque), ``InkRasterOperation`` 9 (mask pen) for the
  highlighter, ``InkPenTip`` (1 = rectangle) and ``InkIgnorePressure``;
* page position in points: ``36 * offset + 72 / 2540 * scaling * value``.

Pressure (the Tablet PC ``NORMAL_PRESSURE`` channel, 0..32767) modulates the width with the
factor ``1.5 * p + 0.25`` (``p`` = normalised pressure), the rule WPF's ink renderer uses;
OneNote's own curve is undocumented, so widths are an approximation.
"""
from __future__ import annotations

import struct
from itertools import accumulate, islice, repeat
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..model import Point, Stroke
from .common import Damaged, ObjectSpace, guid_bytes, p_bool, p_bytes, p_f32, p_ref, p_refs, p_u8, p_u32
from . import schema as S

__all__ = ["InkBudget", "InkTransform", "decode_path", "ink_bounds", "ink_data_strokes", "MAX_POINTS"]

GUID_X = guid_bytes("{598A6A8F-52C0-4BA0-93AF-AF357411A561}")
GUID_Y = guid_bytes("{B53F9F75-04E0-4498-A7EE-C30DBB5A9011}")
GUID_PRESSURE = guid_bytes("{7307502D-F9F4-4E18-B3F2-2CE1B1A3610C}")

MAX_POINTS = 4_000_000  # points one document may hold (a dense section has ~10^5)
BYTES_PER_POINT = 2  # a point needs at least a byte per coordinate; real files use 11 or more
STROKE_COST = 2  # what a stroke costs on top of its points (its own memory, in points)
RASTER_OP_MASK_PEN = 9
PEN_TIP_RECTANGLE = 1
MIN_WIDTH_PT = 0.1
MAX_WIDTH_PT = 200.0
DEFAULT_WIDTH_PT = 1.0


class OverBudget(Damaged):
    """An ink path announces more values than the document's point budget has left."""


class InkBudget:
    """Counts decoded points across a document so a crafted file cannot exhaust memory.

    A document may hold ``MAX_POINTS`` points, and no more than its files could store
    without repeating themselves: :meth:`grant` adds one point per ``BYTES_PER_POINT`` bytes
    of every section read (a crafted file can make one stroke's data count many times).
    Without grants (``limit`` given) the budget is simply ``limit`` points.
    """

    def __init__(self, limit: Optional[int] = None):
        self.cap = MAX_POINTS if limit is None else limit
        self.left = 0 if limit is None else limit
        self.granted = self.left
        self.exhausted = False

    def grant(self, size: int) -> None:
        """Room for the points a section of ``size`` bytes can hold."""
        n = max(0, min(size // BYTES_PER_POINT, self.cap - self.granted))
        self.granted += n
        self.left += n

    def take(self, n: int) -> bool:
        if n > self.left:
            self.exhausted = True
            return False
        self.left -= n
        return True


def decode_path(raw: bytes, max_values: Optional[int] = None) -> List[int]:
    """The signed values of an ``InkPath`` (see the module docstring).

    :class:`Damaged` when the stream is malformed, announces more values than it holds or
    more than ``max_values``; nothing is decoded before the announced count is checked.
    """
    pos = 0
    first = shift = 0
    for b in raw:
        pos += 1
        first |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
        if shift > 63:
            raise Damaged("ink varint longer than 64 bits")
    else:
        return []
    count = first >> 1
    if count > len(raw) - pos:
        raise Damaged("ink path announces more values than it holds")
    if max_values is not None and count > max_values:
        raise OverBudget("ink path holds more values than the point budget allows")
    values: List[int] = []
    append = values.append
    v = shift = 0
    for b in raw[pos:]:
        if b & 0x80:
            v |= (b & 0x7F) << shift
            shift += 7
            if shift > 63:
                raise Damaged("ink varint longer than 64 bits")
        else:
            append(v | (b << shift))
            if len(values) == count:
                break
            v = shift = 0
    if len(values) < count:
        raise Damaged("ink path shorter than it announces")
    return [-(u >> 1) if u & 1 else (u >> 1) for u in values]


def _dimensions(raw: Optional[bytes]) -> List[Tuple[bytes, int, int]]:
    """InkDimensions entries as (packet GUID, minimum, maximum); X and Y when absent."""
    if not raw:
        return [(GUID_X, -(1 << 31), (1 << 31) - 1), (GUID_Y, -(1 << 31), (1 << 31) - 1)]
    dims = []
    for pos in range(0, len(raw) - len(raw) % 32, 32):
        lo, hi = struct.unpack_from("<ii", raw, pos + 16)
        dims.append((bytes(raw[pos:pos + 16]), lo, hi))
    return dims


class _Pen:
    """The rendering attributes of a StrokePropertiesNode, in model units."""

    __slots__ = ("width", "color", "highlighter", "ignore_pressure", "dims")

    def __init__(self, props: Dict[int, Any]):
        w = p_f32(props, S.INK_WIDTH) or 0.0
        h = p_f32(props, S.INK_HEIGHT) or 0.0
        width = min(max(abs(w), abs(h)) * S.HIMETRIC_PT, MAX_WIDTH_PT)
        self.width = max(width, MIN_WIDTH_PT) if width > 0 else DEFAULT_WIDTH_PT
        color = p_u32(props, S.INK_COLOR)
        if color is None or color & 0xFF000000:
            r = g = b = 0
        else:
            r, g, b = color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF
        transparency = p_u8(props, S.INK_TRANSPARENCY) or 0
        alpha = 1.0 - transparency / 255.0
        raster = p_u8(props, S.INK_RASTER_OPERATION)
        tip = p_u8(props, S.INK_PEN_TIP)
        self.highlighter = raster == RASTER_OP_MASK_PEN or (raster is None and tip == PEN_TIP_RECTANGLE and transparency > 0)
        self.color = (r / 255.0, g / 255.0, b / 255.0, alpha)
        self.ignore_pressure = p_bool(props, S.INK_IGNORE_PRESSURE)
        self.dims = _dimensions(p_bytes(props, S.INK_DIMENSIONS))


class InkTransform:
    """HIMETRIC ink values -> page points: ``x0 + sx * X * 72/2540``, likewise for y."""

    __slots__ = ("x0", "y0", "sx", "sy")

    def __init__(self, x0: float = 0.0, y0: float = 0.0, sx: float = 1.0, sy: float = 1.0):
        self.x0, self.y0, self.sx, self.sy = x0, y0, sx, sy


def ink_bounds(space: ObjectSpace, data_oid: Any) -> Optional[Tuple[int, int, int, int]]:
    """``InkBoundingBox`` of an InkDataNode: (x_min, y_min, x_max, y_max) in HIMETRIC."""
    node = space.get(data_oid)
    raw = p_bytes(node.props, S.INK_BOUNDING_BOX) if node is not None else None
    if raw is None or len(raw) != 16:
        return None
    x0, y0, x1, y1 = struct.unpack("<4i", raw)
    return (x0, y0, x1, y1) if x1 >= x0 and y1 >= y0 else None


def ink_data_strokes(space: ObjectSpace, data_oid: Any, transform: InkTransform, budget: InkBudget,
                     pens: Dict[Any, Any], stats: Dict[str, int]) -> List[Stroke]:
    """Every stroke of an InkDataNode, placed by ``transform``.

    ``pens`` caches decoded stroke properties per object (many strokes share one);
    ``stats`` counts what the caller reports: ``unreadable``, ``pressure``, ``highlighter``.
    """
    node = space.get(data_oid)
    if node is None or node.jcid != S.INK_DATA_NODE:
        return []
    strokes: List[Stroke] = []
    for sid in p_refs(node.props, S.INK_STROKES):
        stroke_node = space.get(sid)
        if stroke_node is None or stroke_node.jcid != S.INK_STROKE_NODE:
            stats["unreadable"] = stats.get("unreadable", 0) + 1
            continue
        pen_ref = p_ref(stroke_node.props, S.INK_STROKE_PROPERTIES)
        pen = pens.get(pen_ref)
        if pen is None:
            pen_obj = space.get(pen_ref)
            pen = pens[pen_ref] = _Pen(pen_obj.props if pen_obj is not None else {})
        raw = p_bytes(stroke_node.props, S.INK_PATH)
        if not raw:
            continue
        try:
            values = decode_path(raw, budget.left * max(1, len(pen.dims)))
        except OverBudget:
            budget.exhausted = True
            continue
        except Damaged:
            stats["unreadable"] = stats.get("unreadable", 0) + 1
            continue
        stroke = _stroke(values, pen, transform, budget, stats)
        if stroke is not None:
            strokes.append(stroke)
    return strokes


def _stroke(values: List[int], pen: _Pen, t: InkTransform, budget: InkBudget, stats: Dict[str, int]) -> Optional[Stroke]:
    dims = pen.dims
    n_dims = len(dims) or 1
    count = len(values) // n_dims
    guids = [d[0] for d in dims]
    if count == 0 or GUID_X not in guids or GUID_Y not in guids:
        stats["unreadable"] = stats.get("unreadable", 0) + 1
        return None
    if len(values) % n_dims:
        stats["unreadable"] = stats.get("unreadable", 0) + 1
        return None
    if not budget.take(count + STROKE_COST):
        return None

    def channel(index: int) -> Iterable[int]:  # absolute values, without copying the channel
        return accumulate(islice(values, index * count, (index + 1) * count))

    def pressure_widths(raw: Iterable[int], lo: int, span: float) -> Iterable[float]:
        for p in raw:
            q = (p - lo) / span
            q = 0.0 if q < 0.0 else (1.0 if q > 1.0 else q)
            yield base * (1.5 * q + 0.25)

    xs = channel(guids.index(GUID_X))
    ys = channel(guids.index(GUID_Y))
    widths: Iterable[float]
    base = pen.width
    if GUID_PRESSURE in guids and not pen.ignore_pressure:
        _g, lo, hi = dims[guids.index(GUID_PRESSURE)]
        span = float(hi - lo) if hi > lo else 32767.0
        widths = pressure_widths(channel(guids.index(GUID_PRESSURE)), lo if hi > lo else 0, span)
        stats["pressure"] = stats.get("pressure", 0) + 1
    else:
        widths = repeat(base, count)
    x0, y0, sx, sy = t.x0, t.y0, t.sx * S.HIMETRIC_PT, t.sy * S.HIMETRIC_PT
    points = [Point(x0 + sx * x, y0 + sy * y, w) for x, y, w in zip(xs, ys, widths)]
    kind = "highlighter" if pen.highlighter else "pen"
    if kind == "highlighter":
        stats["highlighter"] = stats.get("highlighter", 0) + 1
    return Stroke(points=points, color=pen.color, kind=kind, pen=None, width=base)
