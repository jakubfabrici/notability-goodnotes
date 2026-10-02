"""Model -> ``.goodnotes`` writer (``design.md`` section 4.4).

The container is synthesised from scratch, mirroring the shapes GoodNotes 6 writes
(``docs/goodnotes-container.md`` sections 10.8, 11, 13, 14 and its Critic additions,
``docs/goodnotes-stroke.md`` section 8 and Critic additions, ``docs/goodnotes-elements.md``
sections 2.5, 3.5 and 4.4).  Nothing is cloned from a template file.

ZIP members, in this order, all deflated::

    index.search.pb        0 bytes (GoodNotes rebuilds its search index)
    index.notes.pb         one record {#1 N, #2 "notes/" + N} per page, display order
    notes/<N>              element pairs, or 0 bytes for an empty page
    index.events.pb        the event log (below)
    thumbnail.jpg          a small white baseline JPEG (constants.THUMBNAIL_JPEG)
    index.attachments.pb   one record {#1 A, #2 "attachments/" + A} per attachment
    attachments/<A>        paper / user PDFs and raster images, bytes as-is
    schema.pb              08 18  ({#1 24})

Identifiers: every UUID is an uppercase 36-character UUID4 string.  A page has two: the
page entity ``P`` (its last hex digit is 0-E) and its notes layer ``N`` = ``P`` with the
last hex digit + 1.  One random 63-bit device id is shared by every event and every element
metadata record; the event sequence number is seeded with the current time in ms and grows
by one per event; the timestamps are float64 ms since the epoch.  Clock registers are
``{#1 version, #2 random uint32}``: version 1 inside events, version 2 on elements
(identical in metadata ``#2`` and content ``#15``); the text-box register has no version.

Event log (record stream, each record ``{#1 entity UUID, #<type> {body}}``), in order:

* ``#30`` document created: ``#1 D, #2 {title, clock}, #3 {constant UUID, clock},
  #6 {"P", clock}, #7 {constant UUID, clock}, #9 "auto", #10 ts, #11 uuid, #13 dev,
  #14 seq, #17 "", #18 "", #19 {#2 clock}, #20 24``.
* ``#6`` per attachment: ``#1 A, #2 A, #5 byte size, #6 D, #10, #11, #12 ({#1 1, #2 1} for a
  PDF, "" for a raster), #14 dev, #15 seq, #16 24``.
* ``#2`` per template (one per distinct (PDF, page)): ``#1 D, #2 T, #4 A, #5 1-based PDF page,
  #6 1, #8 {#1 f32 canvas W, #2 f32 canvas H}, #9 name, #10, #11, #12 {#2 clock},
  #13 {#2 clock}, #15 dev, #16 seq, #17 {#1 1, #2 clock}, #19 {#2 clock}, #21 24`` (the blank
  paper variant: no ``#7``, no ``#18``; rulings live in the PDF itself).
* ``#54`` per page: ``#1 D, #2 P, #3 {#1 T, #2 clock}, #4 {#1 order key, #2 clock}, #10, #11,
  #13 dev, #14 seq, #15 24, #17 {<48-byte colour block>, #2 clock}``; the order key is
  ``"43" + base36(index).rjust(4, "0")`` so ASCII order equals page order.
* ``#105`` per page (search bookkeeping, notes form): ``#1 1, #2 D, #4 N, #6 "auto", #10,
  #11, #13 dev, #14 seq, #15 24``.
* ``#10`` current page = the first page: ``#1 D, #2 P, #3 "PagingViewServiceUpdater:" +
  uuid, #10, #11, #13, #14, #15 24``.
* ``#102`` per non-empty page: ``#1 N, #10, #11, #13, #14, #15 24, #16 D``.

Page content (``notes/<N>``): a record stream of (metadata, content) pairs in z-order
(images, then strokes, then text boxes).  Metadata = ``{#1 E, #2 clock, [#4 A for images],
#8 dev, #9 element counter, #14 5381, #16 24}``.  Content records have exactly one top-level
field whose number is the kind:

* stroke ``#7 {#1 E, #2 Apple-LZ4 frame of a flat TPL image, #4 colour, [#5 1 highlighter],
  #6 "", #7 {#1 {#1 draw index, #2 nonce}}, #9 "", #15 clock, #20 "", #21 24}``.  ``#3`` is
  never written (flat format).  The colour is ``{#1..#4 fixed32 RGBA}`` with 0.0 components
  omitted; highlighters get alpha 0.5.  The TPL image is ``tpl.FlatStroke``: ``W = 2 x width
  in pt`` (median anchor width), the start point and quadratic segments ``(cx, cy, ex, ey)``
  in canvas units (points x 132/72).  Polylines use straight quads with the control at the
  segment midpoint; cubic Bezier input is approximated by ``C = (3(c1 + c2) - P0 - P1) / 4``,
  halving the cubic while the maximum deviation exceeds 0.3 canvas units (depth <= 4).  A
  single point becomes a 0.3-unit dash.
* image ``#1 {#1 E, #2 rect(x, y, w, h), #3 rect(x + w/2, y + h/2, w, h), #4 A,
  #5 {#1 {#1 1, #2 nonce}}, #15 clock, #18 24}`` with ``rect = {#1 {f32 x, f32 y},
  #2 {f32 w, f32 h}}`` in canvas units; the raster is an attachment.
* text ``#8 {#1 E, #2 outer rect, #3 text frame (outer inset by 10), #4 {#1 1.0, #4 1.0},
  #5 {#1 {#1 1, #2 nonce}}, #6 RTF, #7 {1, 1, 1}, #9 {1, 1, 1}, #10 f32 10.0, #15 {#2 nonce},
  #18 {#2 f32 5.0}, #19 {#4 f32 0.2}, #20 ".tb-0", #21 "", #27 24}`` with the RTF from
  ``gnnote.rtf.make_rtf`` (font sizes in canvas units).

Paper: a page with a ``PdfBackground`` attaches ``doc.pdfs[pdf_id]`` once per ``pdf_id`` and
binds ``#2.#5 = page_index + 1`` (shared multi-page attachment); the canvas size is that
PDF page's size x 132/72.  Every other page gets a one-page paper PDF from
``pdfutil.make_paper_pdf(width, height, page.paper)``; pages of equal size and style share
one attachment and one template.
"""
from __future__ import annotations

import hashlib
import io
import math
import random
import time
import uuid as _uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import applelz4, pdfutil, rtf, tpl
from .. import protobuf as pb
from ..model import RGBA, Document, Image, Page, Stroke, TextBox, TextRun
from . import constants as C

XY = Tuple[float, float]
Quad = Tuple[float, float, float, float]

QUAD_TOLERANCE = 0.3  # canvas units; max deviation of a quadratic from its cubic
QUAD_MAX_DEPTH = 4  # at most 2**4 quads per cubic segment
DASH_LENGTH = 0.3  # canvas units; a single point becomes this long
DEFAULT_PAGE_SIZE = (455.04, 588.45)  # GoodNotes "standard" paper, used for an empty document
_SQRT3_36 = math.sqrt(3.0) / 36.0


# ---------------------------------------------------------------------------------------
# options access (gnnote.convert.Options is not imported; any object with attributes works)

def _opt(options: Any, name: str, default: Any) -> Any:
    value = getattr(options, name, None) if options is not None else None
    return default if value is None else value


# ---------------------------------------------------------------------------------------
# identifiers, clocks, sequence numbers


class _Ids:
    """Random identifiers of one output file: UUIDs, device id, sequence, nonces."""

    def __init__(self, seed: Optional[int] = None):
        self.rng = random.Random(seed)
        self.device_id = self.rng.getrandbits(63) or 1
        self.now_ms = float(int(time.time() * 1000.0))
        self.seq = int(self.now_ms)
        self.element_counter = 0

    def uuid(self) -> str:
        return str(_uuid.UUID(int=self.rng.getrandbits(128), version=4)).upper()

    def page_uuids(self) -> Tuple[str, str]:
        """``(P, N)``: P ends in 0-E, N = P with the last hex digit + 1."""
        p = self.uuid()
        last = self.rng.randrange(15)  # 0..14 -> never 'F'
        p = p[:-1] + "%X" % last
        n = p[:-1] + "%X" % (last + 1)
        return p, n

    def nonce(self) -> int:
        return self.rng.getrandbits(32)

    def clock(self, version: int) -> bytes:
        return pb.field_varint(1, version) + pb.field_varint(2, self.nonce())

    def versionless_clock(self) -> bytes:
        return pb.field_varint(2, self.nonce())

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def next_element(self) -> int:
        self.element_counter += 1
        return self.element_counter


# ---------------------------------------------------------------------------------------
# building blocks


@dataclass
class _Attachment:
    uuid: str
    data: bytes
    is_pdf: bool


@dataclass
class _Template:
    uuid: str
    attachment: _Attachment
    pdf_page: int  # 1-based
    canvas_w: float
    canvas_h: float
    name: str
    page_w: float  # points
    page_h: float


@dataclass
class _PageOut:
    index: int
    page: Page
    entity: str  # P
    notes: str  # N
    template: _Template
    content: bytes = b""


@dataclass
class _Context:
    doc: Document
    ids: _Ids
    ribbon: bool
    attachments: List[_Attachment] = field(default_factory=list)
    templates: List[_Template] = field(default_factory=list)
    pdf_attachments: Dict[str, _Attachment] = field(default_factory=dict)
    raster_attachments: Dict[str, _Attachment] = field(default_factory=dict)
    pdf_infos: Dict[str, Optional[pdfutil.PdfInfo]] = field(default_factory=dict)
    template_cache: Dict[Tuple[Any, ...], _Template] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        self.doc.warn(message)


def _point(x: float, y: float) -> bytes:
    return pb.field_fixed32(1, x) + pb.field_fixed32(2, y)


def _rect(x: float, y: float, w: float, h: float) -> bytes:
    return pb.field_message(1, _point(x, y)) + pb.field_message(2, _point(w, h))


def _colour(colour: RGBA) -> bytes:
    """``{#1 R, #2 G, #3 B, #4 A}`` fixed32, components equal to 0.0 omitted."""
    out = b""
    for number, value in enumerate(colour[:4], 1):
        value = min(1.0, max(0.0, float(value)))
        if value != 0.0:
            out += pb.field_fixed32(number, value)
    return out


def _base36(n: int) -> str:
    if n == 0:
        return "0"
    digits = ""
    while n:
        n, r = divmod(n, 36)
        digits = C.BASE36_DIGITS[r] + digits
    return digits


def order_key(index: int) -> str:
    """ASCII-sortable page order key for the 0-based page ``index``."""
    return C.ORDER_KEY_PREFIX + _base36(index + 1).rjust(4, "0")


# ---------------------------------------------------------------------------------------
# geometry: model strokes -> flat quadratic segments in canvas units


def _cubic_to_quads(p0: XY, c1: XY, c2: XY, p1: XY, tolerance: float = QUAD_TOLERANCE,
                    depth: int = 0) -> List[Quad]:
    """Approximate one cubic by quadratics (``design.md`` 4.4).

    The single-quad approximation uses the control ``C = (3(c1 + c2) - P0 - P1) / 4``; its
    maximum deviation from the cubic is ``sqrt(3)/36 * |P1 - 3 c2 + 3 c1 - P0|``.  While that
    exceeds ``tolerance`` the cubic is halved (de Casteljau at t = 1/2), at most
    ``QUAD_MAX_DEPTH`` times.
    """
    dx = p1[0] - 3.0 * c2[0] + 3.0 * c1[0] - p0[0]
    dy = p1[1] - 3.0 * c2[1] + 3.0 * c1[1] - p0[1]
    error = _SQRT3_36 * math.hypot(dx, dy)
    if error <= tolerance or depth >= QUAD_MAX_DEPTH:
        cx = (3.0 * (c1[0] + c2[0]) - p0[0] - p1[0]) / 4.0
        cy = (3.0 * (c1[1] + c2[1]) - p0[1] - p1[1]) / 4.0
        return [(cx, cy, p1[0], p1[1])]
    m01 = ((p0[0] + c1[0]) / 2.0, (p0[1] + c1[1]) / 2.0)
    m12 = ((c1[0] + c2[0]) / 2.0, (c1[1] + c2[1]) / 2.0)
    m23 = ((c2[0] + p1[0]) / 2.0, (c2[1] + p1[1]) / 2.0)
    m012 = ((m01[0] + m12[0]) / 2.0, (m01[1] + m12[1]) / 2.0)
    m123 = ((m12[0] + m23[0]) / 2.0, (m12[1] + m23[1]) / 2.0)
    mid = ((m012[0] + m123[0]) / 2.0, (m012[1] + m123[1]) / 2.0)
    return (_cubic_to_quads(p0, m01, m012, mid, tolerance, depth + 1)
            + _cubic_to_quads(mid, m123, m23, p1, tolerance, depth + 1))


def stroke_to_flat(stroke: Stroke, sx: float, sy: float, width: float) -> tpl.FlatStroke:
    """Convert a model stroke (points in pt) to a ``FlatStroke`` in canvas units.

    ``sx`` / ``sy`` are the point -> canvas scale factors, ``width`` the GoodNotes ``W``.
    Bezier strokes (``controls`` present and consistent) keep their curvature through the
    cubic -> quadratic approximation; everything else is written as a polyline.
    """
    pts: List[XY] = [(p.x * sx, p.y * sy) for p in stroke.points]
    if not pts:
        raise ValueError("a stroke needs at least one point")
    controls = stroke.controls
    if controls is not None and len(pts) >= 2 and len(controls) == len(pts) - 1:
        quads: List[Quad] = []
        for i, (c1, c2) in enumerate(controls):
            quads += _cubic_to_quads(pts[i], (c1.x * sx, c1.y * sy), (c2.x * sx, c2.y * sy), pts[i + 1])
        flat = tpl.FlatStroke(width=float(width), start=pts[0], quads=quads)
    else:
        deduped: List[XY] = []
        for p in pts:
            if not deduped or p != deduped[-1]:
                deduped.append(p)
        flat = tpl.FlatStroke.from_polyline(deduped, width, min_segment=DASH_LENGTH)
    # a stroke whose geometry collapses to one point is invisible: write the dash instead
    x0, y0 = flat.start
    if all(abs(cx - x0) < 1e-6 and abs(cy - y0) < 1e-6 and abs(ex - x0) < 1e-6 and abs(ey - y0) < 1e-6
           for cx, cy, ex, ey in flat.quads):
        flat = tpl.FlatStroke.from_polyline([(x0, y0)], width, min_segment=DASH_LENGTH)
    return flat


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    if n % 2:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def _stroke_width_pt(stroke: Stroke, ctx: _Context) -> float:
    widths = [p.width for p in stroke.points if p.width is not None and p.width > 0]
    width = _median(widths) if widths else 0.0
    if width <= 0:
        width = stroke.width if stroke.width and stroke.width > 0 else 1.0
    if widths and max(widths) - min(widths) > 0.05 * max(widths):
        if ctx.ribbon:
            ctx.warn("Variable-width strokes are written with a constant width (ribbon pen output "
                     "is not available in this version).")
        else:
            ctx.warn("Variable-width strokes are written with a constant (median) width; "
                     "GoodNotes' flat pen has no per-point width.")
    return float(width)


# ---------------------------------------------------------------------------------------
# element records


def _metadata_record(ctx: _Context, element: str, clock: bytes, attachment: Optional[str] = None) -> bytes:
    fields = pb.field_bytes(1, element) + pb.field_message(2, clock)
    if attachment is not None:
        fields += pb.field_bytes(4, attachment)
    fields += (pb.field_varint(8, ctx.ids.device_id)
               + pb.field_varint(9, ctx.ids.next_element())
               + pb.field_varint(14, C.ELEMENT_MAGIC)
               + pb.field_varint(16, C.SCHEMA_VERSION))
    return fields


def _stroke_records(ctx: _Context, stroke: Stroke, sx: float, sy: float, draw_index: int) -> List[bytes]:
    width_pt = _stroke_width_pt(stroke, ctx)
    if stroke.pen == "pencil":
        ctx.warn("Pencil strokes are written as GoodNotes ball-pen strokes.")
    flat = stroke_to_flat(stroke, sx, sy, width_pt * C.WIDTH_PER_POINT)
    frame = applelz4.compress(tpl.encode_flat(flat))
    colour = tuple(float(c) for c in stroke.color) + (1.0,) * (4 - len(stroke.color))
    highlighter = stroke.kind == "highlighter"
    if highlighter:
        colour = (colour[0], colour[1], colour[2], C.HIGHLIGHTER_ALPHA)
    element = ctx.ids.uuid()
    clock = ctx.ids.clock(C.ELEMENT_CLOCK_VERSION)
    body = pb.field_bytes(1, element) + pb.field_bytes(2, frame) + pb.field_message(4, _colour(colour))
    if highlighter:
        body += pb.field_varint(5, 1)
    body += (pb.field_bytes(6, b"")
             + pb.field_message(7, pb.field_message(1, pb.field_varint(1, draw_index)
                                                    + pb.field_varint(2, ctx.ids.nonce())))
             + pb.field_bytes(9, b"")
             + pb.field_message(15, clock)
             + pb.field_bytes(20, b"")
             + pb.field_varint(21, C.SCHEMA_VERSION))
    return [_metadata_record(ctx, element, clock), pb.field_message(C.CONTENT_STROKE, body)]


def _raster_attachment(ctx: _Context, data: bytes) -> _Attachment:
    key = hashlib.sha1(data).hexdigest()
    att = ctx.raster_attachments.get(key)
    if att is None:
        att = _Attachment(ctx.ids.uuid(), bytes(data), False)
        ctx.raster_attachments[key] = att
        ctx.attachments.append(att)
    return att


def _image_records(ctx: _Context, image: Image, sx: float, sy: float) -> List[bytes]:
    if not image.data:
        ctx.warn("An image without data was skipped.")
        return []
    if image.rotation and abs(float(image.rotation)) % 360.0 > 1e-6:
        ctx.warn("Image rotation is not representable in GoodNotes and was dropped.")
    if not (image.data.startswith(b"\x89PNG") or image.data.startswith(b"\xff\xd8")):
        ctx.warn("An image is neither PNG nor JPEG; GoodNotes may not display it.")
    att = _raster_attachment(ctx, image.data)
    x, y, w, h = image.x * sx, image.y * sy, image.w * sx, image.h * sy
    element = ctx.ids.uuid()
    clock = ctx.ids.clock(C.ELEMENT_CLOCK_VERSION)
    body = (pb.field_bytes(1, element)
            + pb.field_message(2, _rect(x, y, w, h))
            + pb.field_message(3, _rect(x + w / 2.0, y + h / 2.0, w, h))
            + pb.field_bytes(4, att.uuid)
            + pb.field_message(5, pb.field_message(1, ctx.ids.clock(1)))
            + pb.field_message(15, clock)
            + pb.field_varint(18, C.SCHEMA_VERSION))
    return [_metadata_record(ctx, element, clock, att.uuid), pb.field_message(C.CONTENT_IMAGE, body)]


def _text_records(ctx: _Context, box: TextBox, sx: float, sy: float) -> List[bytes]:
    text = box.text or ""
    if not text.strip() and not box.runs:
        ctx.warn("An empty text box was skipped.")
        return []
    scale = (sx + sy) / 2.0  # font sizes: pt -> canvas units
    size_canvas = (box.size if box.size and box.size > 0 else 12.0) * scale
    runs = [TextRun(r.text, r.bold, r.italic, r.underline, r.font,
                    r.size * scale if r.size else None, r.color) for r in box.runs]
    half_points = max(1, int(round(2.0 * size_canvas)))
    colour = tuple(float(c) for c in box.color) + (1.0,) * (4 - len(box.color))
    payload = rtf.make_rtf(text, runs, size_half_points=half_points, color=colour)  # type: ignore[arg-type]
    max_half = max([half_points] + [int(round(2.0 * r.size)) for r in runs if r.size])
    lines = text.count("\n") + 1
    fx, fy = box.x * sx, box.y * sy
    fw = box.w * sx if box.w and box.w > 0 else 0.0
    fh = box.h * sy if box.h and box.h > 0 else 0.0
    fh = max(fh, lines * rtf.line_height(max_half))
    if fw <= 0:
        fw = max(1.0, max(len(line) for line in text.split("\n")) * size_canvas * 0.6)
    pad = C.TEXT_PADDING
    element = ctx.ids.uuid()
    clock = ctx.ids.versionless_clock()
    body = (pb.field_bytes(1, element)
            + pb.field_message(2, _rect(fx - pad, fy - pad, fw + 2 * pad, fh + 2 * pad))
            + pb.field_message(3, _rect(fx, fy, fw, fh))
            + pb.field_message(4, pb.field_fixed32(1, 1.0) + pb.field_fixed32(4, 1.0))
            + pb.field_message(5, pb.field_message(1, ctx.ids.clock(1)))
            + pb.field_bytes(6, payload)
            + pb.field_message(7, _point(1.0, 1.0) + pb.field_fixed32(3, 1.0))
            + pb.field_message(9, _point(1.0, 1.0) + pb.field_fixed32(3, 1.0))
            + pb.field_fixed32(10, pad)
            + pb.field_message(15, clock)
            + pb.field_message(18, pb.field_fixed32(2, 5.0))
            + pb.field_message(19, pb.field_fixed32(4, 0.2))
            + pb.field_bytes(20, ".tb-0")
            + pb.field_bytes(21, b"")
            + pb.field_varint(27, C.SCHEMA_VERSION))
    return [_metadata_record(ctx, element, clock), pb.field_message(C.CONTENT_TEXT, body)]


def _page_content(ctx: _Context, out: _PageOut) -> bytes:
    page = out.page
    tmpl = out.template
    sx = C.CANVAS_PER_POINT * (tmpl.page_w / page.width if page.width > 0 else 1.0)
    sy = C.CANVAS_PER_POINT * (tmpl.page_h / page.height if page.height > 0 else 1.0)
    records: List[bytes] = []
    for image in page.images:
        try:
            records += _image_records(ctx, image, sx, sy)
        except (ValueError, TypeError) as exc:
            ctx.warn(f"Page {out.index + 1}: an image was skipped ({exc}).")
    draw_index = 0
    for stroke in page.strokes:
        if not stroke.points:
            if stroke.outline:
                ctx.warn("A filled-shape stroke without centre-line points was skipped.")
            continue
        draw_index += 1
        try:
            records += _stroke_records(ctx, stroke, sx, sy, draw_index)
        except (ValueError, TypeError) as exc:
            ctx.warn(f"Page {out.index + 1}: a stroke was skipped ({exc}).")
    for box in page.texts:
        try:
            records += _text_records(ctx, box, sx, sy)
        except (ValueError, TypeError) as exc:
            ctx.warn(f"Page {out.index + 1}: a text box was skipped ({exc}).")
    return pb.encode_records(records)


# ---------------------------------------------------------------------------------------
# paper / PDF backgrounds


def _paper_name(template_uuid: str, width: float, height: float) -> str:
    size = "a4" if abs(width - C.A4_SIZE[0]) < 1.0 and abs(height - C.A4_SIZE[1]) < 1.0 else "standard"
    return f"{template_uuid}_{size}_1_1{C.PAPER_NAME_SUFFIX}"


def _generated_template(ctx: _Context, page: Page, index: int) -> _Template:
    width = float(page.width) if page.width and page.width > 0 else DEFAULT_PAGE_SIZE[0]
    height = float(page.height) if page.height and page.height > 0 else DEFAULT_PAGE_SIZE[1]
    if (width, height) != (page.width, page.height):
        ctx.warn(f"Page {index + 1} has no valid size; the GoodNotes standard page size was used.")
    style = page.paper or "plain"
    if style not in pdfutil.PAPER_STYLES and style != "ruled":
        ctx.warn(f"Unknown paper style {style!r}; plain paper was used.")
        style = "plain"
    key = ("paper", round(width, 3), round(height, 3), style)
    tmpl = ctx.template_cache.get(key)
    if tmpl is None:
        pdf = pdfutil.make_paper_pdf(width, height, style)
        att = _Attachment(ctx.ids.uuid(), pdf, True)
        ctx.attachments.append(att)
        t_uuid = ctx.ids.uuid()
        tmpl = _Template(t_uuid, att, 1, width * C.CANVAS_PER_POINT, height * C.CANVAS_PER_POINT,
                         _paper_name(t_uuid, width, height), width, height)
        ctx.templates.append(tmpl)
        ctx.template_cache[key] = tmpl
    return tmpl


def _pdf_template(ctx: _Context, page: Page, index: int) -> Optional[_Template]:
    bg = page.background
    if bg is None:
        return None
    data = ctx.doc.pdfs.get(bg.pdf_id)
    if not data:
        ctx.warn(f"Page {index + 1}: its PDF background {bg.pdf_id!r} is missing; paper was generated instead.")
        return None
    if bg.pdf_id not in ctx.pdf_infos:
        try:
            info: Optional[pdfutil.PdfInfo] = pdfutil.pdf_info(data)
        except (ValueError, TypeError) as exc:
            info = None
            ctx.warn(f"PDF {bg.pdf_id!r} could not be read ({exc}); paper was generated instead.")
        if info is not None:
            for message in info.warnings:
                ctx.warn(f"PDF {bg.pdf_id!r}: {message}")
        ctx.pdf_infos[bg.pdf_id] = info
    info = ctx.pdf_infos[bg.pdf_id]
    if info is None:
        return None
    page_index = int(bg.page_index)
    if not 0 <= page_index < len(info.pages):
        ctx.warn(f"Page {index + 1}: PDF page {page_index + 1} does not exist in {bg.pdf_id!r}; "
                 "paper was generated instead.")
        return None
    key = ("pdf", bg.pdf_id, page_index)
    tmpl = ctx.template_cache.get(key)
    if tmpl is not None:
        return tmpl
    att = ctx.pdf_attachments.get(bg.pdf_id)
    if att is None:
        att = _Attachment(ctx.ids.uuid(), bytes(data), True)
        ctx.pdf_attachments[bg.pdf_id] = att
        ctx.attachments.append(att)
    pdf_page = info.pages[page_index]
    if pdf_page.rotation in (90, 270):
        ctx.warn(f"PDF {bg.pdf_id!r} page {page_index + 1} is rotated; GoodNotes' handling of "
                 "rotated PDF pages is unverified.")
    t_uuid = ctx.ids.uuid()
    # Carried PDFs always get a plain file name, even when they were the source app's stock
    # paper (e.g. Notability's template PDFs): only paper generated by this writer carries the
    # GoodNotes catalogue name (design.md 4.4), so a re-read classifies them as user PDFs.
    name = bg.pdf_id if bg.pdf_id.lower().endswith(".pdf") else f"{bg.pdf_id}.pdf"
    tmpl = _Template(t_uuid, att, page_index + 1, pdf_page.width * C.CANVAS_PER_POINT,
                     pdf_page.height * C.CANVAS_PER_POINT, name, pdf_page.width, pdf_page.height)
    ctx.templates.append(tmpl)
    ctx.template_cache[key] = tmpl
    return tmpl


def _resolve_template(ctx: _Context, page: Page, index: int) -> _Template:
    tmpl = _pdf_template(ctx, page, index)
    if tmpl is None:
        return _generated_template(ctx, page, index)
    if page.width > 0 and page.height > 0 and (
            abs(tmpl.page_w - page.width) > 0.5 or abs(tmpl.page_h - page.height) > 0.5):
        ctx.warn(f"Page {index + 1} ({page.width:g} x {page.height:g} pt) was scaled to its PDF page "
                 f"({tmpl.page_w:g} x {tmpl.page_h:g} pt).")
    return tmpl


# ---------------------------------------------------------------------------------------
# event log


def _event(ctx: _Context, entity: str, number: int, body: bytes) -> bytes:
    return pb.field_bytes(1, entity) + pb.field_message(number, body)


def _stamp(ctx: _Context) -> bytes:
    """``#10`` timestamp + ``#11`` event UUID."""
    return pb.field_fixed64(10, ctx.ids.now_ms) + pb.field_bytes(11, ctx.ids.uuid())


def _trio(ctx: _Context, first: int) -> bytes:
    """device id, sequence number, schema at ``first``, ``first + 1``, ``first + 2``."""
    return (pb.field_varint(first, ctx.ids.device_id)
            + pb.field_varint(first + 1, ctx.ids.next_seq())
            + pb.field_varint(first + 2, C.SCHEMA_VERSION))


def _register(ctx: _Context, value: bytes) -> bytes:
    return value + pb.field_message(2, ctx.ids.clock(C.EVENT_CLOCK_VERSION))


def _events(ctx: _Context, doc_uuid: str, title: str, pages: List[_PageOut]) -> bytes:
    ids = ctx.ids
    records: List[bytes] = []
    # #30 document created
    body = (pb.field_bytes(1, doc_uuid)
            + pb.field_message(2, _register(ctx, pb.field_bytes(1, title)))
            + pb.field_message(3, _register(ctx, pb.field_bytes(1, C.DOCUMENT_CONSTANT_UUID)))
            + pb.field_message(6, _register(ctx, pb.field_bytes(1, C.ORIENTATION_PORTRAIT)))
            + pb.field_message(7, _register(ctx, pb.field_bytes(1, C.DOCUMENT_CONSTANT_UUID)))
            + pb.field_bytes(9, C.RECOGNITION_LANGUAGE)
            + _stamp(ctx)
            + pb.field_varint(13, ids.device_id)
            + pb.field_varint(14, ids.next_seq())
            + pb.field_bytes(17, b"")
            + pb.field_bytes(18, b"")
            + pb.field_message(19, pb.field_message(2, ids.clock(C.EVENT_CLOCK_VERSION)))
            + pb.field_varint(20, C.SCHEMA_VERSION))
    records.append(_event(ctx, doc_uuid, C.EVENT_DOCUMENT_CREATED, body))
    # #6 per attachment (PDFs first, each followed by its templates; then rasters)
    emitted_templates: set = set()

    def attachment_event(att: _Attachment) -> bytes:
        kind = pb.field_message(12, pb.field_varint(1, 1) + pb.field_varint(2, 1)) if att.is_pdf else pb.field_bytes(12, b"")
        body = (pb.field_bytes(1, att.uuid) + pb.field_bytes(2, att.uuid)
                + pb.field_varint(5, len(att.data)) + pb.field_bytes(6, doc_uuid)
                + _stamp(ctx) + kind + _trio(ctx, 14))
        return _event(ctx, att.uuid, C.EVENT_ATTACHMENT_ADDED, body)

    def template_event(tmpl: _Template) -> bytes:
        body = (pb.field_bytes(1, doc_uuid) + pb.field_bytes(2, tmpl.uuid)
                + pb.field_bytes(4, tmpl.attachment.uuid)
                + pb.field_varint(5, tmpl.pdf_page) + pb.field_varint(6, 1)
                + pb.field_message(8, _point(tmpl.canvas_w, tmpl.canvas_h))
                + pb.field_bytes(9, tmpl.name)
                + _stamp(ctx)
                + pb.field_message(12, pb.field_message(2, ids.clock(C.EVENT_CLOCK_VERSION)))
                + pb.field_message(13, pb.field_message(2, ids.clock(C.EVENT_CLOCK_VERSION)))
                + pb.field_varint(15, ids.device_id) + pb.field_varint(16, ids.next_seq())
                + pb.field_message(17, _register(ctx, pb.field_varint(1, 1)))
                + pb.field_message(19, pb.field_message(2, ids.clock(C.EVENT_CLOCK_VERSION)))
                + pb.field_varint(21, C.SCHEMA_VERSION))
        return _event(ctx, tmpl.uuid, C.EVENT_TEMPLATE_CREATED, body)

    for att in ctx.attachments:
        if not att.is_pdf:
            continue
        records.append(attachment_event(att))
        for tmpl in ctx.templates:
            if tmpl.attachment is att and tmpl.uuid not in emitted_templates:
                emitted_templates.add(tmpl.uuid)
                records.append(template_event(tmpl))
    for att in ctx.attachments:
        if not att.is_pdf:
            records.append(attachment_event(att))
    # #54 per page
    for out in pages:
        body = (pb.field_bytes(1, doc_uuid) + pb.field_bytes(2, out.entity)
                + pb.field_message(3, _register(ctx, pb.field_bytes(1, out.template.uuid)))
                + pb.field_message(4, _register(ctx, pb.field_bytes(1, order_key(out.index))))
                + _stamp(ctx) + _trio(ctx, 13)
                + pb.field_message(17, _register(ctx, C.PAGE_COLOUR_BLOCK)))
        records.append(_event(ctx, out.entity, C.EVENT_PAGE_CREATED, body))
    # #105 per page (notes form, no hash)
    for out in pages:
        body = (pb.field_varint(1, 1) + pb.field_bytes(2, doc_uuid) + pb.field_bytes(4, out.notes)
                + pb.field_bytes(6, C.RECOGNITION_LANGUAGE) + _stamp(ctx) + _trio(ctx, 13))
        records.append(_event(ctx, out.notes, C.EVENT_SEARCH_UPDATED, body))
    # #10 current page
    body = (pb.field_bytes(1, doc_uuid) + pb.field_bytes(2, pages[0].entity)
            + pb.field_bytes(3, C.PAGING_PREFIX + ids.uuid()) + _stamp(ctx) + _trio(ctx, 13))
    records.append(_event(ctx, doc_uuid, C.EVENT_CURRENT_PAGE, body))
    # #102 per non-empty page
    for out in pages:
        if out.content:
            body = pb.field_bytes(1, out.notes) + _stamp(ctx) + _trio(ctx, 13) + pb.field_bytes(16, doc_uuid)
            records.append(_event(ctx, out.notes, C.EVENT_NOTES_WRITTEN, body))
    return pb.encode_records(records)


# ---------------------------------------------------------------------------------------
# public entry points


def build_members(doc: Document, options: Any = None) -> List[Tuple[str, bytes]]:
    """Build the ZIP members ``[(name, bytes), ...]`` in GoodNotes' member order.

    ``write_goodnotes`` zips exactly this list; it is exposed for tests and tools that want
    to inspect the members without unzipping.
    """
    ids = _Ids(_opt(options, "random_seed", None))
    ribbon = bool(_opt(options, "ribbon", False))
    ctx = _Context(doc, ids, ribbon)
    if ribbon:
        ctx.warn("The experimental ribbon pen output is not available; strokes were written "
                 "with GoodNotes' constant-width pen format.")
    title = str(_opt(options, "title", None) or doc.title or "Untitled Notebook")
    pages = list(doc.pages)
    if not pages:
        ctx.warn("The document has no pages; one empty page was written.")
        pages = [Page(*DEFAULT_PAGE_SIZE)]

    outs: List[_PageOut] = []
    for index, page in enumerate(pages):
        tmpl = _resolve_template(ctx, page, index)
        entity, notes = ids.page_uuids()
        outs.append(_PageOut(index, page, entity, notes, tmpl))
    for out in outs:
        out.content = _page_content(ctx, out)

    doc_uuid = ids.uuid()
    members: List[Tuple[str, bytes]] = [(C.MEMBER_SEARCH_INDEX, b"")]
    members.append((C.MEMBER_NOTES_INDEX, pb.encode_records(
        pb.field_bytes(1, out.notes) + pb.field_bytes(2, C.NOTES_PREFIX + out.notes) for out in outs)))
    for out in outs:
        members.append((C.NOTES_PREFIX + out.notes, out.content))
    members.append((C.MEMBER_EVENTS, _events(ctx, doc_uuid, title, outs)))
    members.append((C.MEMBER_THUMBNAIL, C.THUMBNAIL_JPEG))
    members.append((C.MEMBER_ATTACHMENTS_INDEX, pb.encode_records(
        pb.field_bytes(1, att.uuid) + pb.field_bytes(2, C.ATTACHMENTS_PREFIX + att.uuid)
        for att in ctx.attachments)))
    for att in ctx.attachments:
        members.append((C.ATTACHMENTS_PREFIX + att.uuid, att.data))
    members.append((C.MEMBER_SCHEMA, C.SCHEMA_PB))
    return members


def write_goodnotes(doc: Document, options: Any = None) -> bytes:
    """Serialise ``doc`` as a ``.goodnotes`` ZIP (see the module docstring).

    ``options`` is duck-typed (``gnnote.convert.Options`` or anything with the attributes
    ``title`` and ``ribbon``; an unknown ``random_seed`` attribute makes the output
    reproducible).  Lossy steps are reported through ``doc.warn``.
    """
    members = build_members(doc, options)
    stamp = time.localtime()[:6]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zf.writestr(info, data)
    return buf.getvalue()
