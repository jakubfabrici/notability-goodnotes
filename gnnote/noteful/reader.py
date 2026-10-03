"""Noteful ``.noteful`` -> :class:`gnnote.model.Document` (tolerant reader).

Layout (``docs/noteful.md``; every structure below is a TTV record, :mod:`gnnote.noteful.ttv`):

Container
    ``AA BB CC DE``, raw blobs, the root record, a 16-byte trailer ``AA BB CC DE 00000000
    u32 root offset, u32 root length``.  Root: ``0x0001`` f32 format version, ``0x0002``
    [notebook UUID], ``0x0003`` [embedded file UUIDs], ``0x0004`` [annotation UUIDs],
    ``0x000a`` [blob names], ``0x000b`` [u64 offsets], ``0x000c`` [u64 lengths].  Blob names
    are ``n:<uuid>`` (title, thumbnail), ``d:<uuid>`` (pages, layers, bookmarks) and plain
    UUIDs (embedded files: thumbnail JPEG, PDFs, images; annotation records: one per page).

Collections (pages, layers, bookmarks, objects)
    ``0x0001`` [keys], ``0x0002`` [u64 stamps], ``0x0003`` [bool present], ``0x0000``
    [records]; a value whose key is marked not present is a deleted entry and is skipped.

Page (``d:`` ``0x0002``)
    ``0x0001`` UUID, ``0x0002`` {``0x0000`` annotation UUID or "", ``0x0002`` [image file
    UUIDs]}, ``0x0004`` background, ``0x0005`` ordering tag (pages sort by it as plain
    ASCII strings).  Background ``0x0000`` 1 = a PDF page (``0x0001`` display size,
    ``0x0003`` 0-based page index, ``0x0004`` PDF file UUID); 2 = a paper template
    (``0x0001`` size, ``0x0003`` UUID of the template rendered by the app as a one-page PDF,
    ``0x0006`` JSON parameters).  Page size in pt = display size x 72/132.

Annotation (one per page)
    ``0x0001`` object version (280 / 288), ``0x0002`` the ink blob, ``0x0005`` the objects.
    Ink blob records: ``F1 02`` style (4 x f64 RGBA, u16 blend 0 pen / 1 multiply =
    highlighter, u64 dash 0 / 1 dashed / 2 dotted), ``F1 01`` stroke (8-byte id, u16 1 =
    per-point radius, u64 save time, u64 creation time, u64 z key, u32 0, f64 nominal radius,
    u32 0, u32 point count N; N <= 4: N x f32 (x, y[, r]); N > 4: f32 (min, span) per
    dimension, then N x u16 with value = min + q / 65535 x span).  Width in pt = 2 r x 72/132.
    Objects: ``0x0001`` UUID, ``0x0002`` {``0x0001`` [centre x, centre y, w, h, rotation
    rad], ``0x0002`` flip bits}, ``0x0005`` z key, ``0x0006`` data, ``0x0008`` opacity.
    Data ``0x0001`` type: 1 image, 2 text box, 3 rectangle, 6 ellipse, 12 polygon, 20 line,
    21 cubic Bezier.  Rotation turns the box about its centre, clockwise on the page.

Mapping to the model
    * ink -> polyline strokes (``controls`` None); blend 1 -> ``kind = "highlighter"`` with
      the stored alpha halved (the app draws multiply at 50 %); dash patterns -> solid.
    * shapes -> strokes with exact cubic ``controls`` (straight sides as thirds handles,
      ellipses as 16 arcs, the Bezier type as is); a fill -> ``kind = "fill"`` with the
      flattened outline, placed before its outline stroke; an arrow head -> one more stroke
      tracing the filled triangle the app draws (the shaft is shortened to its base).
    * text boxes -> :class:`TextBox` with runs; the box loses the (5, 2)-unit inset and its
      rotation pivot moves from the centre to the text frame's top-left corner.
    * images -> :class:`Image`; the box is the whole picture at the scale shown (a crop is
      undone, a flip is dropped; both warn).
    * the model keeps ink, images and text in separate lists: when the file stacks them
      otherwise than images < ink < text, a warning says so.  Layers are merged; bookmarks,
      unknown collections and unsupported embedded files (audio) are dropped with warnings.

Only a missing magic, an unreadable root index or a file without any page information
raises ``ValueError``; everything else damaged or unknown becomes a warning.
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .. import pdfutil
from ..geometry import flatten_bezier
from ..model import RGBA, Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from . import (
    A4_UNITS, ARROW_HALF_WIDTH, ARROW_LENGTH, BACKGROUND_PDF, BACKGROUND_TEMPLATE, BLEND_MULTIPLY,
    BLEND_NORMAL, CMD_CLOSE, CMD_CUBIC, CMD_LINE, CMD_MOVE, CMD_QUAD, HIGHLIGHTER_OPACITY, INK_STROKE,
    INK_STYLE, KNOWN_OBJECT_VERSIONS, MAGIC, OBJ_BEZIER, OBJ_ELLIPSE, OBJ_IMAGE, OBJ_LINE, OBJ_RECTANGLE,
    OBJ_TEXT, POINTS_PER_UNIT, SHAPE_TYPES, TEXT_INSET, TRAILER_SIZE, looks_like_noteful,
)
from . import ttv
from .ttv import Record

__all__ = ["read_noteful", "decode_ink", "MAX_PAGES", "MAX_STROKE_POINTS", "MAX_INK_POINTS", "MAX_SHAPE_POINTS"]

K = POINTS_PER_UNIT
MAX_PAGES = 10_000  # pages beyond this are dropped with a warning
MAX_STROKE_POINTS = 1_000_000  # a stroke with more points is damage
MAX_INK_POINTS = 10_000_000  # points per document (ink, shape outlines, fills); further ones are skipped
MAX_SHAPE_POINTS = 100_000  # a shape outline with more points is damage
MAX_FILL_SAMPLES = 2_000  # vertices of one flattened fill outline (large shapes get a coarser spacing)
MAX_COORD = 1e9  # units; coordinates beyond this (about 7 km) are damage
MAX_SHARED_IMAGE_BYTES = 256 * 1024 * 1024  # picture bytes images may reference beyond the file's own size
ELLIPSE_ARCS = 16  # cubic arcs per ellipse (anchors every 22.5 degrees keep the bbox exact to 2 %)
FILL_SPACING = 1.0  # pt between the vertices of a flattened fill outline
HAIRLINE = 0.5  # pt; the width of a stroke whose stored width is zero
BLACK: RGBA = (0.0, 0.0, 0.0, 1.0)
DEFAULT_FONT = "Helvetica"
DEFAULT_TEXT_SIZE = 22.0  # units (12 pt)

XY = Tuple[float, float]
Cubic = Tuple[XY, XY, XY]  # (c1, c2, end)

# counted lossy steps: one warning line each, with the document-wide count
_COUNTED = {
    "dash": "{n} dashed or dotted strokes and shape outlines were drawn solid (the model has no dash patterns)",
    "blend": "{n} strokes with an unknown blend mode were read as pen strokes",
    "stroke_bad": "{n} strokes with non-finite or out-of-range coordinates were skipped",
    "stroke_limit": "{n} strokes and shapes beyond the point limit were skipped",
    "object_box": "{n} objects without a usable position or size were skipped",
    "shape_path": "{n} shapes with an unreadable outline were skipped or shortened",
    "text_bad": "{n} text boxes without readable text were skipped",
    "text_format": "{n} text boxes have formatting that could not be fully decoded (their text is kept)",
    "text_background": "{n} text box background colours were dropped",
    "strike": "{n} text runs lost their strikethrough",
    "image_missing": "{n} images whose picture file is missing were skipped",
    "image_shared": "{n} images were skipped: they reference the same pictures too often",
    "image_format": "{n} images that are neither PNG, JPEG nor PDF were skipped",
    "crop": "{n} cropped images were placed whole at the size and position they are shown at "
            "(the model has no crop, so the hidden parts show again)",
    "flip": "{n} mirrored images were placed unmirrored (the model has no image flip)",
    "image_opacity": "{n} translucent images were made opaque",
    "zorder": "The stacking of ink, images and text boxes was simplified on {n} pages "
              "(images below ink, text boxes above)",
}


# --------------------------------------------------------------------------- small helpers


def _collection(rec: Optional[Record], int_keys: bool = False) -> List[Record]:
    """The live values of a keyed collection (entries whose key is marked absent are dropped)."""
    if rec is None:
        return []
    values = rec.records(0x0000) or []
    keys_entry = rec.entry(0x0001)
    keys = keys_entry.value if keys_entry is not None and keys_entry.is_list else []
    present = rec.booleans(0x0003) or []
    deleted = set()
    for key, alive in zip(keys, present):
        if not alive and isinstance(key, (int, str)):
            deleted.add(key)
    out = []
    for value in values:
        key = value.integer(0x0002) if int_keys else value.string(0x0001)
        if key is not None and key in deleted:
            continue
        out.append(value)
    return out


def _clamp_rgba(values: Sequence[float]) -> Optional[RGBA]:
    """3 or 4 components -> a colour clamped to 0..1 (``None`` when unusable)."""
    if len(values) < 3:
        return None
    comps = list(values[:4]) + [1.0] * (4 - min(4, len(values)))
    if not all(math.isfinite(c) for c in comps):
        return None
    r, g, b, a = (min(1.0, max(0.0, float(c))) for c in comps)
    return (r, g, b, a)


def _rgba(rec: Optional[Record]) -> Optional[RGBA]:
    """``{0x0000 [f32 r, g, b, a]}`` -> a clamped colour (``None`` when unusable)."""
    values = rec.numbers(0x0000) if rec is not None else None
    return _clamp_rgba(values) if values else None


def _with_alpha(color: RGBA, factor: float) -> RGBA:
    return (color[0], color[1], color[2], color[3] * factor)


def _sniff_image(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:64].lstrip()[:5] == b"%PDF-":
        return "pdf"
    return None


def _finite(*values: float) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def _in_range(values: Sequence[float]) -> bool:
    """All finite and within +-MAX_COORD (NaN fails every comparison)."""
    return bool(values) and -MAX_COORD <= min(values) and max(values) <= MAX_COORD \
        and all(map(math.isfinite, values))


def _template_paper(params: Optional[str]) -> str:
    """Best-effort paper style from a template's JSON parameters (only "Blank" was sampled)."""
    if not params:
        return "plain"
    try:
        spec = json.loads(params)
    except (ValueError, RecursionError):
        return "plain"
    if not isinstance(spec, dict):
        return "plain"
    name = str(spec.get("name", "")).lower()
    if "dot" in name:
        return "dotted"
    if "grid" in name or "square" in name or "graph" in name:
        return "grid"
    if any(word in name for word in ("line", "ruled", "narrow", "wide", "college")):
        return "lined"
    return "plain"


# --------------------------------------------------------------------------- shape geometry


@dataclass
class _Sub:
    """One subpath in the object's box frame (units, top-left origin, before rotation)."""

    start: XY
    segs: List[Cubic] = field(default_factory=list)
    closed: bool = False

    def end(self) -> XY:
        return self.segs[-1][2] if self.segs else self.start

    def line_to(self, p: XY) -> None:
        a = self.end()
        self.segs.append(((a[0] + (p[0] - a[0]) / 3.0, a[1] + (p[1] - a[1]) / 3.0),
                          (a[0] + 2.0 * (p[0] - a[0]) / 3.0, a[1] + 2.0 * (p[1] - a[1]) / 3.0), p))

    def quad_to(self, c: XY, p: XY) -> None:
        a = self.end()
        self.segs.append(((a[0] + 2.0 / 3.0 * (c[0] - a[0]), a[1] + 2.0 / 3.0 * (c[1] - a[1])),
                          (p[0] + 2.0 / 3.0 * (c[0] - p[0]), p[1] + 2.0 / 3.0 * (c[1] - p[1])), p))

    def close(self) -> None:
        self.closed = True
        e = self.end()
        if self.segs and (abs(e[0] - self.start[0]) > 1e-9 or abs(e[1] - self.start[1]) > 1e-9):
            self.line_to(self.start)


def _rect_path(w: float, h: float, radius: float) -> _Sub:
    r = min(max(0.0, radius), w / 2.0, h / 2.0)
    if r < 0.01:
        sub = _Sub((0.0, 0.0))
        for p in ((w, 0.0), (w, h), (0.0, h)):
            sub.line_to(p)
        sub.close()
        return sub
    k = 4.0 / 3.0 * math.tan(math.pi / 8.0) * r  # quarter-circle handle length
    sub = _Sub((r, 0.0))
    sub.line_to((w - r, 0.0))
    sub.segs.append(((w - r + k, 0.0), (w, r - k), (w, r)))
    sub.line_to((w, h - r))
    sub.segs.append(((w, h - r + k), (w - r + k, h), (w - r, h)))
    sub.line_to((r, h))
    sub.segs.append(((r - k, h), (0.0, h - r + k), (0.0, h - r)))
    sub.line_to((0.0, r))
    sub.segs.append(((0.0, r - k), (r - k, 0.0), (r, 0.0)))
    sub.closed = True
    return sub


def _ellipse_path(w: float, h: float) -> _Sub:
    cx, cy, rx, ry = w / 2.0, h / 2.0, w / 2.0, h / 2.0
    step = 2.0 * math.pi / ELLIPSE_ARCS
    k = 4.0 / 3.0 * math.tan(step / 4.0)

    def at(a: float) -> Tuple[XY, XY]:
        return (cx + rx * math.cos(a), cy + ry * math.sin(a)), (-rx * math.sin(a), ry * math.cos(a))

    start, _ = at(0.0)
    sub = _Sub(start)
    for i in range(ELLIPSE_ARCS):
        (p0, d0), (p1, d1) = at(i * step), at((i + 1) * step)
        if i == ELLIPSE_ARCS - 1:
            p1 = start  # closed exactly
        sub.segs.append(((p0[0] + k * d0[0], p0[1] + k * d0[1]), (p1[0] - k * d1[0], p1[1] - k * d1[1]), p1))
    sub.closed = True
    return sub


def _point_path(rec: Optional[Record], sx: float, sy: float) -> Tuple[List[_Sub], bool]:
    """``0x000d`` {``0x0001`` [f64 x, y, ...], ``0x0002`` [i32 commands]} -> subpaths.

    Points are relative to the box's top-left corner in the object's data size; ``sx`` /
    ``sy`` scale them onto the box.  Returns ``(subpaths, damaged)``.
    """
    if rec is None:
        return [], True
    coords = rec.numbers(0x0001) or []
    cmds = rec.integers(0x0002) or []
    pts: List[XY] = []
    if len(coords) > 2 * MAX_SHAPE_POINTS:
        return [], True
    for i in range(0, len(coords) - 1, 2):
        x, y = coords[i], coords[i + 1]
        if not _finite(x, y) or abs(x) > MAX_COORD or abs(y) > MAX_COORD:
            return [], True
        pts.append((x * sx, y * sy))
    subs: List[_Sub] = []
    cur: Optional[_Sub] = None
    i = 0
    damaged = False

    def take(n: int) -> Optional[List[XY]]:
        nonlocal i
        if i + n > len(pts):
            return None
        out = pts[i:i + n]
        i += n
        return out

    for cmd in cmds:
        if cmd == CMD_CLOSE:
            if cur is not None:
                cur.close()
                cur = None
            continue
        need = {CMD_MOVE: 1, CMD_LINE: 1, CMD_QUAD: 2, CMD_CUBIC: 3}.get(cmd)
        got = take(need) if need is not None else None
        if got is None:
            damaged = True
            break
        if cmd == CMD_MOVE or cur is None:
            # a segment without a current point only moves there
            cur = _Sub(got[-1])
            subs.append(cur)
        elif cmd == CMD_LINE:
            cur.line_to(got[0])
        elif cmd == CMD_QUAD:
            cur.quad_to(got[0], got[1])
        else:
            cur.segs.append((got[0], got[1], got[2]))
    return [s for s in subs if s.segs], damaged


def _cubic_at(p0: XY, seg: Cubic, t: float) -> XY:
    c1, c2, p1 = seg
    mt = 1.0 - t
    a, b, c, d = mt * mt * mt, 3.0 * mt * mt * t, 3.0 * mt * t * t, t * t * t
    return (a * p0[0] + b * c1[0] + c * c2[0] + d * p1[0], a * p0[1] + b * c1[1] + c * c2[1] + d * p1[1])


def _split_first(p0: XY, seg: Cubic, t: float) -> Cubic:
    """The part ``[0, t]`` of a cubic (de Casteljau)."""
    c1, c2, p1 = seg

    def lerp(a: XY, b: XY) -> XY:
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    m01, m12, m23 = lerp(p0, c1), lerp(c1, c2), lerp(c2, p1)
    m012, m123 = lerp(m01, m12), lerp(m12, m23)
    return (m01, m012, lerp(m012, m123))


@dataclass
class _Head:
    path: List[XY]  # box frame, units
    width: float  # units


def _arrow_head(sub: _Sub, thickness: float) -> Optional[_Head]:
    """Shorten ``sub``'s end by the arrow head and return the head's tracing path.

    The app fills a triangle 4.5 line widths long and wide whose tip is the path's end point
    and whose base is centred where the shortened shaft ends.  The model has no filled
    triangles, so the head becomes one polyline that runs around the triangle inset by half
    its width, then along its axis: drawn at that width, its outer edge is the triangle's.
    """
    if not sub.segs or thickness <= 0:
        return None
    tip = sub.end()
    p0 = sub.segs[-2][2] if len(sub.segs) > 1 else sub.start
    length = ARROW_LENGTH * thickness

    def dist(t: float) -> float:
        p = _cubic_at(p0, sub.segs[-1], t)
        return math.hypot(p[0] - tip[0], p[1] - tip[1])

    if dist(0.0) <= length:
        base = p0
        sub.segs.pop()  # the whole last segment lies under the head
    else:
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if dist(mid) > length:
                lo = mid
            else:
                hi = mid
        sub.segs[-1] = _split_first(p0, sub.segs[-1], lo)
        base = sub.segs[-1][2]
    ax, ay = tip[0] - base[0], tip[1] - base[1]
    axis = math.hypot(ax, ay)
    if axis < 1e-9:
        return None
    nx, ny = -ay / axis, ax / axis
    half = ARROW_HALF_WIDTH * thickness
    a = (base[0] + half * nx, base[1] + half * ny)
    c = (base[0] - half * nx, base[1] - half * ny)
    # inset the triangle (a, tip, c) by d towards its incentre
    la = math.hypot(tip[0] - c[0], tip[1] - c[1])  # side opposite a
    lt = math.hypot(c[0] - a[0], c[1] - a[1])  # opposite the tip
    lc = math.hypot(a[0] - tip[0], a[1] - tip[1])  # opposite c
    perimeter = la + lt + lc
    area2 = abs((tip[0] - a[0]) * (c[1] - a[1]) - (tip[1] - a[1]) * (c[0] - a[0]))
    if perimeter <= 0 or area2 <= 0:
        return None
    inr = area2 / perimeter
    ix = (la * a[0] + lt * tip[0] + lc * c[0]) / perimeter
    iy = (la * a[1] + lt * tip[1] + lc * c[1]) / perimeter
    d = min(thickness / 2.0, 0.9 * inr)
    f = (inr - d) / inr

    def inset(p: XY) -> XY:
        return (ix + (p[0] - ix) * f, iy + (p[1] - iy) * f)

    a2, t2, c2 = inset(a), inset(tip), inset(c)
    g2 = ((a2[0] + c2[0]) / 2.0, (a2[1] + c2[1]) / 2.0)
    return _Head([g2, a2, t2, c2, g2, t2], 2.0 * d)


class _Frame:
    """Box frame (units, top-left origin, unrotated) -> page points."""

    def __init__(self, cx: float, cy: float, w: float, h: float, theta: float):
        self.cx, self.cy, self.w, self.h = cx, cy, w, h
        self.cos, self.sin = math.cos(theta), math.sin(theta)

    def xy(self, p: XY) -> XY:
        lx, ly = p[0] - self.w / 2.0, p[1] - self.h / 2.0
        return ((self.cx + lx * self.cos - ly * self.sin) * K, (self.cy + lx * self.sin + ly * self.cos) * K)

    def point(self, p: XY, width: float) -> Point:
        x, y = self.xy(p)
        return Point(x, y, width)


def _sub_stroke(sub: _Sub, frame: _Frame, width: float, color: RGBA) -> Stroke:
    anchors = [frame.point(sub.start, width)] + [frame.point(seg[2], width) for seg in sub.segs]
    controls = [(frame.point(seg[0], width), frame.point(seg[1], width)) for seg in sub.segs]
    return Stroke(points=anchors, color=color, kind="pen", width=width, controls=controls)


def _sub_polygon(sub: _Sub, frame: _Frame) -> List[Point]:
    """The subpath flattened (1 pt spacing; coarser for a large shape, so at most about
    ``MAX_FILL_SAMPLES`` vertices plus one per segment)."""
    anchors = [frame.point(sub.start, 0.0)] + [frame.point(seg[2], 0.0) for seg in sub.segs]
    controls = [(frame.point(seg[0], 0.0), frame.point(seg[1], 0.0)) for seg in sub.segs]
    length = sum(math.hypot(c1.x - p0.x, c1.y - p0.y) + math.hypot(c2.x - c1.x, c2.y - c1.y)
                 + math.hypot(p1.x - c2.x, p1.y - c2.y)
                 for p0, (c1, c2), p1 in zip(anchors, controls, anchors[1:]))
    return flatten_bezier(anchors, controls, max(FILL_SPACING, length / MAX_FILL_SAMPLES))


# --------------------------------------------------------------------------- ink


@dataclass
class InkResult:
    """What :func:`decode_ink` found in one ink blob."""

    strokes: List[Tuple[int, Stroke]] = field(default_factory=list)  # (z key, stroke) in blob order
    problem: Optional[str] = None  # why decoding stopped early
    dashed: int = 0
    unknown_blend: int = 0
    non_finite: int = 0
    over_limit: int = 0
    points: int = 0  # points decoded


def decode_ink(blob: bytes, point_budget: int = MAX_INK_POINTS) -> InkResult:
    """Decode an annotation's ink blob into model strokes (pt), never raising.

    Records have no length field, so an unknown record tag ends the blob (``problem``);
    every stroke before it is kept.
    """
    out = InkResult()
    n = len(blob)
    p = 0
    color, blend, dash = BLACK, BLEND_NORMAL, 0
    while p < n:
        if n - p < 2:
            out.problem = "the ink data ends in the middle of a record"
            break
        (tag,) = struct.unpack_from(">H", blob, p)
        if tag == INK_STYLE:
            if n - p < 44:
                out.problem = "a style record of the ink data is cut off"
                break
            rgba = struct.unpack_from(">4d", blob, p + 2)
            blend, dash = struct.unpack_from(">HQ", blob, p + 34)
            color = _clamp_rgba(rgba) or BLACK
            p += 44
            continue
        if tag != INK_STROKE:
            out.problem = f"unknown ink record {tag:#06x}"
            break
        if n - p < 56:
            out.problem = "a stroke record of the ink data is cut off"
            break
        (layout,) = struct.unpack_from(">H", blob, p + 10)
        if layout not in (0, 1):
            out.problem = f"unknown point layout {layout} in the ink data"
            break
        (z,) = struct.unpack_from(">Q", blob, p + 28)
        (nominal,) = struct.unpack_from(">d", blob, p + 40)
        (count,) = struct.unpack_from(">I", blob, p + 52)
        dims = 3 if layout == 1 else 2
        size = count * 4 * dims if count <= 4 else 8 * dims + count * 2 * dims
        q = p + 56
        if size > n - q:
            out.problem = "the points of a stroke are cut off"
            break
        p = q + size
        if count == 0:
            continue
        if count > MAX_STROKE_POINTS or out.points + count > point_budget:
            out.over_limit += 1
            continue
        if count <= 4:
            vals = struct.unpack_from(f">{count * dims}f", blob, q)
            xs = [vals[i] for i in range(0, count * dims, dims)]
            ys = [vals[i] for i in range(1, count * dims, dims)]
            rs = [vals[i] for i in range(2, count * dims, dims)] if dims == 3 else None
        else:
            ranges = struct.unpack_from(f">{2 * dims}f", blob, q)
            if not _finite(*ranges):
                out.non_finite += 1
                continue
            raw = struct.unpack_from(f">{count * dims}H", blob, q + 8 * dims)
            x0, fx = ranges[0], ranges[1] / 65535.0
            y0, fy = ranges[2], ranges[3] / 65535.0
            xs = [x0 + v * fx for v in raw[0::dims]]
            ys = [y0 + v * fy for v in raw[1::dims]]
            rs = None
            if dims == 3:
                r0, fr = ranges[4], ranges[5] / 65535.0
                rs = [r0 + v * fr for v in raw[2::dims]]
        if not (_finite(nominal) and abs(nominal) <= MAX_COORD and _in_range(xs) and _in_range(ys)
                and (rs is None or _in_range(rs))):
            out.non_finite += 1
            continue
        out.points += count
        width = max(0.0, 2.0 * nominal * K)
        if rs is not None:
            widths = [max(0.0, 2.0 * r * K) for r in rs]
            if width <= 0:
                width = sorted(widths)[len(widths) // 2]
        else:
            widths = [width] * count
        if width <= 0 and max(widths) <= 0:
            width = HAIRLINE
            widths = [HAIRLINE] * count
        elif width <= 0:
            width = HAIRLINE
        points = [Point(x * K, y * K, w) for x, y, w in zip(xs, ys, widths)]
        kind = "pen"
        stroke_color = color
        if blend == BLEND_MULTIPLY:
            kind = "highlighter"
            stroke_color = _with_alpha(color, HIGHLIGHTER_OPACITY)
        elif blend != BLEND_NORMAL:
            out.unknown_blend += 1
        if dash:
            out.dashed += 1
        out.strokes.append((z, Stroke(points=points, color=stroke_color, kind=kind, pen=None, width=width)))
    return out


# --------------------------------------------------------------------------- rich text

# attribute key -> the typed value list it takes its value from
_TEXT_LISTS = {1: 0x0005, 2: 0x0006, 3: 0x0006, 4: 0x0008, 5: 0x0008, 6: 0x0007, 7: 0x0007, 8: 0x0008,
               9: 0x0009, 10: 0x000a, 11: 0x000a, 12: 0x000a, 13: 0x0005, 14: 0x0005}
_ALIGN = {0: "left", 1: "center", 2: "right"}


@dataclass
class _RichText:
    runs: List[TextRun]
    align: str
    damaged: bool
    struck: int


def _rich_text(rec: Record, opacity: float) -> _RichText:
    """Chunks with delta-encoded attributes (each chunk lists the keys that change; their
    values come, in order, from one typed list per value type) -> model runs."""
    strings = rec.strings(0x0002) or []
    counts = rec.integers(0x0003) or []
    keys = rec.integers(0x0004) or []
    queues = {
        0x0005: iter(rec.strings(0x0005) or []),
        0x0006: iter(rec.booleans(0x0006) or []),
        0x0007: iter(rec.records(0x0007) or []),
        0x0008: iter(rec.integers(0x0008) or []),
        0x0009: iter(rec.records(0x0009) or []),
        0x000a: iter(rec.numbers(0x000a) or []),
    }
    state: Dict[int, Any] = {1: DEFAULT_FONT, 2: False, 3: False, 4: 0, 5: 0, 6: None, 8: 0, 10: DEFAULT_TEXT_SIZE}
    damaged = len(counts) < len(strings) or sum(c for c in counts if c > 0) > len(keys)
    frozen = False
    struck = 0
    runs: List[TextRun] = []
    align: Optional[str] = None
    k = 0
    for i, chunk in enumerate(strings):
        n = counts[i] if i < len(counts) and counts[i] > 0 else 0
        if not frozen:
            for key in keys[k:k + n]:
                source = _TEXT_LISTS.get(key)
                value = next(queues[source], None) if source is not None else None
                if value is None:
                    frozen = damaged = True  # an unknown key or a list that ran out
                    break
                state[key] = value
        k += n
        text = chunk.replace("\u200b", "")
        if not text:
            continue
        color = _rgba(state[6]) if isinstance(state[6], Record) else None
        color = _with_alpha(color or BLACK, opacity)
        size = state[10] if isinstance(state[10], float) and math.isfinite(state[10]) and state[10] > 0 \
            else DEFAULT_TEXT_SIZE
        font = next((state[key] for key in (13, 1, 14) if isinstance(state.get(key), str) and state[key]), None)
        if state[5]:
            struck += 1
        if align is None:
            align = _ALIGN.get(state[8] if isinstance(state[8], int) else 0, "left")
        runs.append(TextRun(text=text, bold=bool(state[2]), italic=bool(state[3]), underline=bool(state[4]),
                            font=font, size=size * K, color=color))
    return _RichText(runs, align or "left", damaged, struck)


# --------------------------------------------------------------------------- reader


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.doc = Document(source_format="noteful")
        self.decoder = ttv.Decoder(data)
        self.blobs: Dict[str, Tuple[int, int]] = {}
        self.counts: Dict[str, int] = {}
        self.unknown_types: Dict[int, int] = {}
        self.unknown_versions: Set[int] = set()
        self.pdf_infos: Dict[str, Optional[pdfutil.PdfInfo]] = {}
        self.used_files: Set[str] = set()
        self.points = 0  # model points made so far (ink, shapes, fills), see MAX_INK_POINTS
        self.image_bytes = 0  # picture bytes referenced by the images read so far
        self.read_annotations: Set[str] = set()
        self._blob_cache: Dict[str, bytes] = {}

    # -- bookkeeping ------------------------------------------------------------------------

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    def count(self, key: str, n: int = 1) -> None:
        if n:
            self.counts[key] = self.counts.get(key, 0) + n

    def flush_counts(self) -> None:
        for key, template in _COUNTED.items():
            n = self.counts.get(key, 0)
            if n:
                self.warn(template.format(n=n))
        for otype, n in sorted(self.unknown_types.items()):
            self.warn(f"{n} objects of the unsupported Noteful type {otype} were skipped")
        if self.unknown_versions:
            versions = ", ".join(str(v) for v in sorted(self.unknown_versions))
            self.warn(f"Content records of object version {versions} are newer than the files this reader "
                      f"was checked against ({' / '.join(map(str, KNOWN_OBJECT_VERSIONS))}); read as those")

    def blob(self, name: Optional[str]) -> Optional[bytes]:
        """An embedded file's bytes (one shared copy per file, however often it is used)."""
        if not name:
            return None
        cached = self._blob_cache.get(name)
        if cached is None:
            span = self.blobs.get(name)
            if span is None:
                return None
            cached = self._blob_cache[name] = self.data[span[0]:span[0] + span[1]]
        return cached

    def blob_head(self, name: str, n: int) -> Optional[bytes]:
        span = self.blobs.get(name)
        return None if span is None else self.data[span[0]:span[0] + min(n, span[1])]

    def record(self, name: Optional[str], what: str) -> Optional[Record]:
        """The blob ``name`` decoded as a record; damage becomes one warning naming ``what``."""
        span = self.blobs.get(name) if name else None
        if span is None:
            return None
        before = len(self.decoder.errors)
        rec = self.decoder.decode(span[0], span[0] + span[1])
        if len(self.decoder.errors) > before:
            self.warn(f"{what} is damaged ({self.decoder.errors[before]}); the readable part was used")
        return rec

    # -- container --------------------------------------------------------------------------

    def _container(self) -> Record:
        data = self.data
        if not looks_like_noteful(data):
            raise ValueError("not a Noteful file (the AA BB CC DE magic is missing)")
        size = len(data)
        root_start, root_len = struct.unpack_from(">II", data, size - 8)
        if root_start < len(MAGIC) or root_len == 0 or root_start + root_len > size - TRAILER_SIZE:
            raise ValueError("not a Noteful file (its root index lies outside the file)")
        root = self.decoder.decode(root_start, root_start + root_len)
        names = root.strings(0x000a)
        starts = root.integers(0x000b)
        lengths = root.integers(0x000c)
        if not names or starts is None or lengths is None:
            raise ValueError("damaged Noteful file: the blob index is unreadable")
        if root.error:
            self.warn(f"The root index is damaged ({root.error}); the readable part was used")
        if not len(names) == len(starts) == len(lengths):
            self.warn("The blob index lists names, offsets and lengths of different counts; "
                      "only complete entries were used")
        outside = 0
        for name, start, length in zip(names, starts, lengths):
            if start < len(MAGIC) or start + length > size - TRAILER_SIZE:
                outside += 1
                continue
            self.blobs.setdefault(name, (start, length))
        if outside:
            self.warn(f"{outside} blobs of the index point outside the file and were ignored")
        return root

    # -- document ---------------------------------------------------------------------------

    def read(self) -> Document:
        doc = self.doc
        root = self._container()
        metas = root.strings(0x0002) or []
        meta = metas[0] if metas else next((n[2:] for n in self.blobs if n.startswith("d:")), None)
        header = self.record("n:" + meta, "The notebook header") if meta else None
        notebook = self.record("d:" + meta, "The page list") if meta else None
        if header is not None:
            doc.title = header.string(0x0003) or "Untitled"
            thumb = header.record(0x0007)
            if thumb is not None and thumb.string(0x0001):
                self.used_files.add(thumb.string(0x0001) or "")
        else:
            doc.title = "Untitled"
            self.warn("The notebook header is missing; the title is unknown")
        pages_rec = notebook.record(0x0002) if notebook is not None else None
        annotations = root.strings(0x0004) or []
        if pages_rec is None:
            if not annotations:
                raise ValueError("damaged Noteful file: it has no readable page list")
            self._salvage(annotations)
        else:
            self._pages(pages_rec, annotations)
        if notebook is not None:
            self._notebook_extras(notebook)
        self._unused_files(root.strings(0x0003) or [])
        self.flush_counts()
        if not doc.pages:
            self.warn("The notebook has no pages")
        return doc

    def _pages(self, pages_rec: Record, annotations: Sequence[str]) -> None:
        values = _collection(pages_rec)
        referenced = set()  # content records of every page, deleted ones included
        for value in pages_rec.records(0x0000) or []:
            resources = value.record(0x0002)
            if resources is not None:
                referenced.add(resources.string(0x0000))
        keyed = []
        for index, rec in enumerate(values):
            tag = rec.string(0x0005)
            keyed.append(((0, tag, index) if tag is not None else (1, "", index), rec))
        keyed.sort(key=lambda item: item[0])
        if len(keyed) > MAX_PAGES:
            self.warn(f"The notebook has {len(keyed)} pages; only the first {MAX_PAGES} were read")
            keyed = keyed[:MAX_PAGES]
        for number, (_key, rec) in enumerate(keyed, 1):
            label = f"Page {number}"
            try:
                page = self._page(rec, label)
            except Exception as exc:  # noqa: BLE001 - tolerant reader: one page never ends the file
                self.warn(f"{label} could not be read ({exc.__class__.__name__}: {exc}); a blank page stands in")
                page = Page(A4_UNITS[0] * K, A4_UNITS[1] * K)
            self.doc.pages.append(page)
        orphans = [a for a in annotations if a not in referenced]
        if orphans:
            self.warn(f"{len(orphans)} content records belong to no page and were skipped")

    def _salvage(self, annotations: Sequence[str]) -> None:
        """No page list: every content record becomes an A4 page, in index order."""
        pages = list(dict.fromkeys(annotations))[:MAX_PAGES]
        self.warn(f"The page list is unreadable; {len(pages)} pages were rebuilt from the content records "
                  "in file order (A4, without backgrounds)")
        for number, ann in enumerate(pages, 1):
            page = Page(A4_UNITS[0] * K, A4_UNITS[1] * K)
            try:
                self._annotation(page, ann, f"Page {number}")
            except Exception as exc:  # noqa: BLE001
                self.warn(f"Page {number}: its content could not be read ({exc.__class__.__name__}: {exc})")
            self.doc.pages.append(page)

    def _notebook_extras(self, notebook: Record) -> None:
        layers = _collection(notebook.record(0x0003), int_keys=True)
        if len(layers) > 1:
            self.warn(f"The notebook's {len(layers)} layers were merged into one")
        bookmarks = _collection(notebook.record(0x0004))
        if bookmarks:
            self.warn(f"{len(bookmarks)} page bookmarks were dropped")
        others = sum(len(_collection(notebook.record(tag))) for tag in (0x0005, 0x0007, 0x000a))
        if others:
            self.warn(f"{others} notebook entries of an unsupported kind (outlines, tags or recordings) were dropped")

    def _unused_files(self, files: Sequence[str]) -> None:
        unsupported = 0
        for name in dict.fromkeys(files):
            if name in self.used_files:
                continue
            head = self.blob_head(name, 64)
            if head is not None and _sniff_image(head) is None:
                unsupported += 1
        if unsupported:
            self.warn(f"{unsupported} embedded files of an unsupported kind (e.g. audio recordings) were dropped")

    # -- pages ------------------------------------------------------------------------------

    def _page(self, rec: Record, label: str) -> Page:
        bg = rec.record(0x0004)
        kind = bg.integer(0x0000) if bg is not None else None
        size = bg.size(0x0001) if bg is not None else None
        if size is not None and _finite(*size) and 0 < size[0] <= MAX_COORD and 0 < size[1] <= MAX_COORD:
            width, height = size
        else:
            self.warn(f"{label} has no valid size; A4 was assumed")
            width, height = A4_UNITS
        page = Page(width * K, height * K)
        if kind == BACKGROUND_PDF and bg is not None:
            self._pdf_background(page, bg.string(0x0004), bg.integer(0x0003, 0) or 0, label, template=False)
        elif kind == BACKGROUND_TEMPLATE and bg is not None:
            page.paper = _template_paper(bg.string(0x0006))
            self._pdf_background(page, bg.string(0x0003), 0, label, template=True)
        elif bg is not None:
            self.warn(f"{label}: unknown background kind {kind}; the page was read without its background")
        resources = rec.record(0x0002)
        ann = resources.string(0x0000) if resources is not None else None
        if ann:
            if ann in self.blobs:
                self._annotation(page, ann, label)
            else:
                self.warn(f"{label}: its content record is missing; the page was read without content")
        return page

    def _pdf_info(self, pdf_id: str, data: bytes) -> Optional[pdfutil.PdfInfo]:
        if pdf_id not in self.pdf_infos:
            info: Optional[pdfutil.PdfInfo] = None
            try:
                info = pdfutil.pdf_info(data)
            except Exception as exc:  # noqa: BLE001 - PdfError and anything a hostile PDF triggers
                self.warn(f"PDF {pdf_id} could not be read ({exc.__class__.__name__}: {exc}); "
                          "its pages were read without background")
            else:
                if not info.pages:
                    self.warn(f"PDF {pdf_id} has no pages; its pages were read without background")
                    info = None
                else:
                    for message in info.warnings:
                        self.warn(f"PDF {pdf_id}: {message}")
            self.pdf_infos[pdf_id] = info
        return self.pdf_infos[pdf_id]

    def _pdf_background(self, page: Page, pdf_id: Optional[str], index: int, label: str, template: bool) -> None:
        what = "paper template" if template else "PDF background"
        data = self.blob(pdf_id)
        if pdf_id is None or data is None:
            self.warn(f"{label}: its {what} file is missing; the page was read without background")
            return
        self.used_files.add(pdf_id)
        info = self._pdf_info(pdf_id, data)
        if info is None:
            return
        if not 0 <= index < len(info.pages):
            self.warn(f"{label}: page {index + 1} of PDF {pdf_id} does not exist ({len(info.pages)} pages); "
                      "its last page was used")
            index = len(info.pages) - 1
        self.doc.pdfs.setdefault(pdf_id, data)
        page.background = PdfBackground(pdf_id, index)
        if template:
            page.template_is_builtin = True
        elif len(info.pages) == 1 and info.producer == "gnnote" and info.creator in ("", "gnnote"):
            # paper generated by gnnote's writers is stock paper, not a user PDF
            from ..goodnotes.reader import _paper_style

            page.template_is_builtin = True
            page.paper = _paper_style(data, False)

    # -- page content -----------------------------------------------------------------------

    def _annotation(self, page: Page, ann: str, label: str) -> None:
        if ann in self.read_annotations:  # each page has its own record; never decode one twice
            self.warn(f"{label} shares its content record with an earlier page; it was read without content")
            return
        self.read_annotations.add(ann)
        rec = self.record(ann, f"{label}: the content record")
        if rec is None:
            return
        version = rec.integer(0x0001)
        if version is not None and version not in KNOWN_OBJECT_VERSIONS:
            self.unknown_versions.add(version)
        items: List[Tuple[int, int, str, Any]] = []  # (z key, order, kind, object)
        ink = rec.data(0x0002)
        if ink:
            result = decode_ink(ink, MAX_INK_POINTS - self.points)
            self.points += result.points
            if result.problem:
                self.warn(f"{label}: {result.problem}; the rest of the page's ink was skipped")
            self.count("dash", result.dashed)
            self.count("blend", result.unknown_blend)
            self.count("stroke_bad", result.non_finite)
            self.count("stroke_limit", result.over_limit)
            for z, stroke in result.strokes:
                items.append((z, len(items), "stroke", stroke))
        for obj in _collection(rec.record(0x0005)):
            z = obj.integer(0x0005, 0) or 0
            try:
                produced = self._object(obj)
            except Exception as exc:  # noqa: BLE001 - tolerant reader: one object never ends the page
                self.warn(f"{label}: an object was skipped ({exc.__class__.__name__}: {exc})")
                continue
            for kind, value in produced:
                items.append((z, len(items), kind, value))
        items.sort(key=lambda item: (item[0], item[1]))
        rank = {"image": 0, "stroke": 1, "text": 2}
        ranks = [rank[kind] for _z, _o, kind, _v in items]
        if any(a > b for a, b in zip(ranks, ranks[1:])):
            self.count("zorder")
        for _z, _o, kind, value in items:
            if kind == "stroke":
                page.strokes.append(value)
            elif kind == "image":
                page.images.append(value)
            else:
                page.texts.append(value)

    def _object(self, rec: Record) -> List[Tuple[str, Any]]:
        data = rec.record(0x0006)
        otype = data.integer(0x0001) if data is not None else None
        if data is None or otype is None:
            self.count("object_box")
            return []
        box_rec = rec.record(0x0002)
        box = box_rec.numbers(0x0001) if box_rec is not None else None
        if not box or len(box) < 4 or not _finite(*box[:5]) or any(abs(v) > MAX_COORD for v in box[:4]):
            self.count("object_box")
            return []
        cx, cy, w, h = box[0], box[1], abs(box[2]), abs(box[3])
        theta = box[4] if len(box) > 4 else 0.0
        opacity = rec.number(0x0008, 1.0)
        opacity = 1.0 if opacity is None else min(1.0, max(0.0, opacity))
        if otype == OBJ_TEXT:
            return self._text(data, cx, cy, w, h, theta, opacity)
        if otype == OBJ_IMAGE:
            flip = box_rec.integer(0x0002, 0) if box_rec is not None else 0
            return self._image(data, cx, cy, w, h, theta, flip or 0, opacity)
        if otype in SHAPE_TYPES:
            return self._shape(data, otype, cx, cy, w, h, theta, opacity)
        self.unknown_types[otype] = self.unknown_types.get(otype, 0) + 1
        return []

    def _shape(self, data: Record, otype: int, cx: float, cy: float, w: float, h: float, theta: float,
               opacity: float) -> List[Tuple[str, Any]]:
        stroke_rec = data.record(0x0007)
        fill_rec = data.record(0x0005)
        thickness = stroke_rec.number(0x0002, 0.0) if stroke_rec is not None else 0.0
        thickness = max(0.0, thickness or 0.0)
        line_color = _rgba(stroke_rec.record(0x0007)) if stroke_rec is not None else None
        fill_color = _rgba(fill_rec.record(0x0001)) if fill_rec is not None else None
        if fill_rec is not None and fill_rec.boolean(0x0002, False):
            # the flag: the "fill" colour is the outline's colour and nothing is filled
            line_color = line_color or fill_color
            fill_color = None
        dash = arrow = 0
        if stroke_rec is not None:
            dash_rec, arrow_rec = stroke_rec.record(0x0003), stroke_rec.record(0x0008)
            dash = dash_rec.integer(0x0000, 0) if dash_rec is not None else 0
            arrow = arrow_rec.integer(0x0000, 0) if arrow_rec is not None else 0
        if otype == OBJ_RECTANGLE:
            subs = [_rect_path(w, h, data.number(0x0014, 0.0) or 0.0)]
        elif otype == OBJ_ELLIPSE:
            subs = [_ellipse_path(w, h)]
        else:
            size = data.size(0x0002)
            sx = w / size[0] if size is not None and _finite(*size) and size[0] > 1e-9 else 1.0
            sy = h / size[1] if size is not None and _finite(*size) and size[1] > 1e-9 else 1.0
            subs, damaged = _point_path(data.record(0x000d), sx, sy)
            if damaged:
                self.count("shape_path")
            if not subs:
                return []
        # the point budget: anchors and handles of the outlines (+ an arrow head), then the fill
        outline_cost = sum(3 * len(s.segs) + 1 for s in subs) + 16 if thickness > 0 else 0
        if self.points + outline_cost > MAX_INK_POINTS:
            self.count("stroke_limit")
            return []
        self.points += outline_cost
        frame = _Frame(cx, cy, w, h, theta)
        out: List[Tuple[str, Any]] = []
        if fill_color is not None:
            polygons = [_sub_polygon(s, frame) for s in subs if s.closed]
            polygons = [poly for poly in polygons if len(poly) >= 3]
            fill_cost = sum(len(poly) for poly in polygons)
            if self.points + fill_cost > MAX_INK_POINTS:
                self.count("stroke_limit")
                polygons = []
            self.points += fill_cost if polygons else 0
            if polygons:
                out.append(("stroke", Stroke(points=list(polygons[0]), color=_with_alpha(fill_color, opacity),
                                             kind="fill", pen=None, width=0.0, outline=polygons)))
        if thickness > 0:
            color = _with_alpha(line_color or BLACK, opacity)
            width = thickness * K
            if dash:
                self.count("dash")
            head = _arrow_head(subs[-1], thickness) if arrow and otype in (OBJ_LINE, OBJ_BEZIER) \
                and not subs[-1].closed else None
            for sub in subs:
                if sub.segs:
                    out.append(("stroke", _sub_stroke(sub, frame, width, color)))
            if head is not None:
                trace = _Sub(head.path[0])
                for p in head.path[1:]:
                    trace.line_to(p)
                out.append(("stroke", _sub_stroke(trace, frame, head.width * K, color)))
        return out

    def _text(self, data: Record, cx: float, cy: float, w: float, h: float, theta: float,
              opacity: float) -> List[Tuple[str, Any]]:
        rich_rec = data.record(0x0004)
        if rich_rec is None:
            self.count("text_bad")
            return []
        rich = _rich_text(rich_rec, opacity)
        if rich.damaged:
            self.count("text_format")
        self.count("strike", rich.struck)
        text = "".join(run.text for run in rich.runs)
        if not text:
            return []
        background = data.record(0x0005)
        if background is not None and _rgba(background.record(0x0001)) is not None:
            self.count("text_background")
        iw = max(0.0, w - 2.0 * TEXT_INSET[0])
        ih = max(0.0, h - 2.0 * TEXT_INSET[1])
        c, s = math.cos(theta), math.sin(theta)
        lx, ly = -iw / 2.0, -ih / 2.0
        x, y = cx + lx * c - ly * s, cy + lx * s + ly * c
        first = rich.runs[0]
        box = TextBox(x=x * K, y=y * K, w=iw * K, h=ih * K, text=text, runs=rich.runs,
                      color=first.color or BLACK, size=first.size or DEFAULT_TEXT_SIZE * K,
                      rotation=math.degrees(theta), align=rich.align)
        return [("text", box)]

    def _image(self, data: Record, cx: float, cy: float, w: float, h: float, theta: float, flip: int,
               opacity: float) -> List[Tuple[str, Any]]:
        file_id = data.string(0x000a)
        raw = self.blob(file_id)
        if file_id is None or raw is None:
            self.count("image_missing")
            return []
        fmt = _sniff_image(raw)
        if fmt is None:
            self.count("image_format")
            return []
        if self.image_bytes + len(raw) > len(self.data) + MAX_SHARED_IMAGE_BYTES:
            self.count("image_shared")
            return []
        self.image_bytes += len(raw)
        self.used_files.add(file_id)
        if w <= 0 or h <= 0:
            self.count("object_box")
            return []
        fw, fh, lx, ly = w, h, 0.0, 0.0  # the whole picture: size and centre relative to the box centre
        natural = data.size(0x0002)
        crop = data.record(0x000c)
        coords = crop.numbers(0x0001) if crop is not None else None
        if coords and natural is not None and _finite(*natural) and natural[0] > 1e-6 and natural[1] > 1e-6:
            xs = [v for v in coords[0::2] if math.isfinite(v)]
            ys = [v for v in coords[1::2] if math.isfinite(v)]
            if len(xs) >= 2 and len(ys) >= 2:
                x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                cw, ch = x1 - x0, y1 - y0
                nw, nh = natural
                if cw > 1e-6 and ch > 1e-6:
                    sx, sy = w / cw, h / ch
                    fw, fh = nw * sx, nh * sy
                    lx = -w / 2.0 - x0 * sx + fw / 2.0
                    ly = -h / 2.0 - y0 * sy + fh / 2.0
                    eps = 1e-3 * max(nw, nh) + 1e-6
                    if x0 > eps or y0 > eps or x1 < nw - eps or y1 < nh - eps:
                        self.count("crop")
        if flip & 3:
            self.count("flip")
        if opacity < 0.999:
            self.count("image_opacity")
        c, s = math.cos(theta), math.sin(theta)
        mx, my = cx + lx * c - ly * s, cy + lx * s + ly * c
        image = Image(x=(mx - fw / 2.0) * K, y=(my - fh / 2.0) * K, w=fw * K, h=fh * K, data=raw, fmt=fmt,
                      rotation=math.degrees(theta))
        return [("image", image)]


# --------------------------------------------------------------------------- public API


def read_noteful(data: bytes) -> Document:
    """Parse a ``.noteful`` file into a :class:`Document`.

    Raises ``ValueError`` only when ``data`` is not a Noteful container (no magic, no root
    index, no page information at all); anything damaged or unknown inside it is skipped
    with a line on ``Document.warnings``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_noteful expects bytes")
    return _Reader(bytes(data)).read()
