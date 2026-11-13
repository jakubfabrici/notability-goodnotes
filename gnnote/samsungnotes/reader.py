"""Samsung Notes ``.sdocx`` -> :class:`gnnote.model.Document` (tolerant reader).

An ``.sdocx`` file is a ZIP archive (``docs/samsungnotes.md``)::

    note.note             document header: page size, title and body text, the string
                          registry that pen names are stored in, ...
    pageIdInfo.dat        the page order (page UUIDs)
    <uuid>.page           one page: size, paper, imported-PDF placements, layers of objects
    media/mediaInfo.dat   bind id -> file name of everything in media/
    media/*               pictures, imported PDFs, Samsung's own ``.spi`` page previews
    end_tag.bin           page model (paged or one endless page), lock state, ...

The Windows app keeps unexported notes as folders with the same members; a ZIP of such a
folder (the members under one top folder) is read too.  :mod:`.records` decodes the binary
records; this module maps them onto the model.

Mapping (``W`` = the page's width in canvas units; ``s = 600 / W``):

* Pages are 600 pt wide and ``H x s`` pt high (Samsung's own PDF export uses the same
  scale), in ``pageIdInfo.dat`` order.  Paper templates 1-3 become lined, 4-6 grid and 7-9
  dotted paper; an imported PDF page that fills the page becomes its background.  The empty
  page the app keeps after the last one of a paged note is dropped, as the app's PDF export
  does.  A pageless note keeps its one page record as one long page, which its body text
  makes longer when needed; pageless notes and landscape pages (no sample has either) get
  a warning.
* Strokes become polylines in pt.  Colours are stored BGRA.  ``Marker3`` / ``Marker4`` /
  ``StraightMarker`` / ``StraightHighlighter`` pens are highlighters.  Widths follow the
  rendering model of sdocx2pdf: pressure pens (``FountainPen``, ``InkPen2``, ``Pencil2``)
  draw ``pen size x clamp(pressure, 0.4, 0.7)`` at each point, other pens
  ``0.45 x pen size``; highlighters are as wide as their pen size (measured on the app's
  PDF export) and straight-only ones run from the first to the last point.  Eraser strokes
  and objects without the visible flag are skipped; a stroke whose optional fields gnnote
  cannot read is skipped with a warning instead of failing the file.
* Shapes and lines become strokes along their outline paths (stored already rotated);
  solid fills become ``kind="fill"`` strokes.
* Pictures (page objects and pictures in the typed text) become :class:`Image` objects;
  rotation is clockwise about the picture's centre, as in the model.
* Typed text: text-box objects become :class:`TextBox` objects; the note's body text (which
  flows across the pages in the app) is laid out by gnnote, one text box per paragraph, see
  :class:`_BodyLayout`.  Font sizes, margins, spacings and indents are stored in units of a
  360-unit-wide page (``pt = value x 600 / 360``).  Objects anchored in the text of a text
  box, a shape or the title are counted, not converted.  Text boxes are budgeted
  (:data:`MIN_TEXT_BOXES` plus one per :data:`TEXT_BOX_BYTES` bytes of the file, at most
  :data:`MAX_TEXT_BOXES`), and the body text adds at most :data:`MAX_EXTRA_PAGES` pages.
  All typed text together is budgeted too: :data:`MIN_TEXT_UNITS` UTF-16 units, or
  :data:`TEXT_UNITS_PER_BYTE` per byte of the file, at most ``records.MAX_DOC_TEXT_UNITS``;
  the text beyond that is not converted (with a warning that counts it).
"""
from __future__ import annotations

import bisect
import math
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from ..codecutil import MAX_COORD_PT, MAX_SIZE_PT, ensure_bytes
from ..geometry import MAX_FLATTEN_STEPS
from ..model import RGBA, Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from ..readutil import (PageNotes, PointBudget, ZipBundle, image_format, image_pixel_size, quad_to_cubic,
                        straight_controls)
from . import records as R

__all__ = ["read_sdocx", "find_note_root", "crop_png", "PAGE_WIDTH_PT", "DP_PAGE_WIDTH"]

PAGE_WIDTH_PT = 600.0  # every page is this wide (Samsung's PDF export uses the same width)
DP_PAGE_WIDTH = 360.0  # font sizes and text margins are in units of a page this wide
MAX_PAGES = 10_000
MAX_EXTRA_PAGES = 1_000  # pages the body-text layout may add after the note's last page
# Text boxes (text-box objects and body-text paragraphs) one note may give: MIN_TEXT_BOXES and
# one more per TEXT_BOX_BYTES bytes of the file, at most MAX_TEXT_BOXES (a few KB of repeated
# "x\n" would otherwise become tens of thousands of text boxes, which some writers turn into
# gigabytes)
MIN_TEXT_BOXES = 2_000
TEXT_BOX_BYTES = 8
MAX_TEXT_BOXES = 20_000
# UTF-16 units of typed text one note may keep, all its texts together: TEXT_UNITS_PER_BYTE per
# byte of the file, at least MIN_TEXT_UNITS, at most records.MAX_DOC_TEXT_UNITS (2 million
# repeated units compress to about 4 KB; real notes hold well under one unit per byte)
MIN_TEXT_UNITS = 1_000_000
TEXT_UNITS_PER_BYTE = 64
MIN_PAGE_UNITS = 16  # a page narrower than this many canvas units is damage
MAX_PAGE_UNITS = 1_000_000
MAX_PAGE_RATIO = 1_000.0  # height / width beyond this is damage (pageless notes stay far below)
MIN_PAGE_HEIGHT_PT = 36.0  # a page lower than this (once 600 pt wide) is damage
PEN_PREFIX = "com.samsung.android.sdk.pen.pen.preload."
HIGHLIGHTER_PENS = ("Marker3", "Marker4", "StraightMarker", "StraightHighlighter")
STRAIGHT_PENS = ("StraightMarker", "StraightHighlighter")
PRESSURE_PENS = ("FountainPen", "InkPen2", "Pencil2")
PEN_KINDS = {"FountainPen": "fountain", "ObliquePen": "fountain", "InkPen2": "ballpoint", "Pencil2": "pencil",
             "BrushPen": "brush", "Marker3": "marker", "Marker4": "marker", "StraightMarker": "marker",
             "StraightHighlighter": "marker"}
PRESSURE_CLAMP = (0.4, 0.7)  # sdocx2pdf's pressure -> width law: width = pen size x clamp(pressure)
FIXED_PRESSURE = 0.45  # sdocx2pdf: the pressure of pens that ignore it
HIGHLIGHTER_FACTOR = 1.0  # highlighter width / pen size, measured on the app's PDF export (sample 04)
DEFAULT_PEN_UNITS = 2.0  # a stroke without a pen size (never seen) gets this many canvas units
DEFAULT_LINE_UNITS = 2.0  # sdocx2pdf's default line width of a shape without a line style
PAPER = {1: "lined", 2: "lined", 3: "lined", 4: "grid", 5: "grid", 6: "grid", 7: "dotted", 8: "dotted",
         9: "dotted"}
KNOWN_TEMPLATES = set(PAPER) | {0, 16}  # 0 none, 16 the page of an imported PDF
MAX_CROP_PIXELS = 4_000_000  # PNG pictures up to this size are cropped; larger ones are shown whole
MAX_CROP_WORK = 24_000_000  # pixels the picture crops of one note may decode and copy together
MAX_SHARED_IMAGE_BYTES = 256 * 1024 * 1024  # picture bytes pictures may reference beyond the file's own size
FILL_SPACING = 2.0  # pt between the vertices of a flattened curved fill outline
MAX_FILL_SAMPLES = 2_000  # vertices the curves of one fill outline get at most (plus one per segment)
ARC_SEGMENT_DEGREES = 90.0
ELLIPSE_KAPPA = 4.0 / 3.0 * (math.sqrt(2.0) - 1.0)
BLACK: RGBA = (0.0, 0.0, 0.0, 1.0)
SPI_NOTE = "Samsung's SPI picture format"

# Body text layout (units: pt unless named dp; 1 dp = 600 / 360 pt).  Calibrated against the
# app's PDF exports of the public sample notes; see docs/samsungnotes.md.
DEFAULT_FONT_DP = 15.0
DEFAULT_LINE_SPACING = 1.327  # line pitch in font sizes without a paragraph value (Roboto's height)
LINE_DESCENT = 0.35  # the baseline sits this many font sizes above the bottom of its line
BLOCK_GAP = 0.35  # font sizes of space below a picture or table in the text
STORED_SHIFT = 4.0  # font sizes a table or code block may sit below gnnote's flow at its stored position
WRITER_ASCENT = 0.952  # gnnote's writers put the first baseline this far below a text box's top
MAX_FONT_DP = 500.0
MAX_SPACING_DP = 1_000.0
OBJECT_CHAR = "\ufffc"  # the placeholder character of an object in a text
MAX_STYLE_WORK = 20_000_000  # UTF-16 units all spans and paragraph attributes may cover together

# Span and paragraph attribute kinds of the rich text (records.TextSpan.kind)
SPAN_COLOUR, SPAN_SIZE, SPAN_FONT, SPAN_BOLD, SPAN_ITALIC, SPAN_UNDERLINE = (
    R.SPAN_COLOUR, R.SPAN_SIZE, R.SPAN_FONT, R.SPAN_BOLD, R.SPAN_ITALIC, R.SPAN_UNDERLINE)
SPAN_LINK, SPAN_BACKGROUND, SPAN_STRIKE = R.SPAN_LINK, R.SPAN_BACKGROUND, R.SPAN_STRIKE
PARA_INDENT, PARA_ALIGN, PARA_LINE_SPACING, PARA_BULLET, PARA_SPACE_BEFORE, PARA_SPACE_AFTER = (
    R.PARA_INDENT, R.PARA_ALIGN, R.PARA_LINE_SPACING, R.PARA_BULLET, R.PARA_SPACE_BEFORE, R.PARA_SPACE_AFTER)
INDENT_DP = 16.0  # one indent level (measured on the app's PDF export of sample 01)
MAX_INDENT_LEVEL = 20

XY = Tuple[float, float]

_COUNTED = {
    "eraser": "{n} eraser stroke(s) were skipped",
    "unknown-fields": "{n} stroke(s) store settings of a newer Samsung Notes version and were skipped",
    "no-points": "{n} stroke(s) without usable points were skipped",
    "no-size": "{n} stroke(s) without a pen size were drawn with a default pen size",
    "dashed": "{n} dashed or dotted stroke(s) or line(s) are drawn solid",
    "colour-effects": "{n} stroke(s) with a gradient, pattern or rainbow colour are drawn in one solid colour",
    "shapes": "{n} shape(s) and line(s) were converted to ink strokes",
    "fills": "{n} shape fill(s) were converted; fills were not checked against a note from the app",
    "unfilled": "{n} shape fill(s) with a gradient, picture or pattern were not converted",
    "arcs": "{n} shape(s) with arcs or ovals were drawn by gnnote's own construction, which was not "
            "checked against a note from the app",
    "rotated": "{n} rotated shape(s) were drawn along their stored outline; rotated shapes were not checked "
               "against a note from the app",
    "arrows": "{n} arrowhead(s) of lines were not converted",
    "bent-lines": "{n} bent or curved connector line(s) were drawn straight from their start to their end",
    "flipped": "{n} flipped picture(s) are shown unflipped",
    "borders": "{n} picture frame(s) or border(s) were not drawn",
    "cropped": "{n} picture crop(s) were not applied (JPEG or very large pictures); the whole picture is "
               "shown at its uncropped size",
    "spi": "{n} picture(s) in " + SPI_NOTE + " were not converted",
    "missing-media": "{n} picture(s) or PDF(s) missing from the note's media folder were skipped",
    "image-shared": "{n} picture(s) were skipped: they reference the same pictures too often",
    "bad-media": "{n} picture(s) that are not PNG or JPEG were skipped",
    "text-boxes": "{n} text box(es) were converted; their layout was not checked against a note from the app",
    "text-box-budget": "{n} text box(es) were skipped: the note holds more typed text than gnnote reads",
    "landscape": "{n} landscape page(s) were made 600 pt wide like the others; how the app scales landscape "
                 "pages was not checked against a note from the app",
    "hidden-layers": "{n} hidden layer(s) were not converted",
    "custom-objects": "{n} sticky note(s) or other custom page object(s) were not converted",
    "grouped": "{n} grouped object(s) were not converted",
    "damaged": "{n} damaged object(s) were skipped",
    "invisible": "{n} object(s) marked as invisible were not converted",
    "bad-geometry": "{n} object(s) with coordinates outside any page were skipped",
}


def find_note_root(names: Sequence[str]) -> List[str]:
    """The folders (``""`` = the archive's top level, else ``"name/"``) that hold a note:
    ``note.note`` plus ``pageIdInfo.dat`` or ``end_tag.bin``, at the top or one folder down."""
    files = {n for n in names if not n.endswith("/")}
    roots: List[str] = []
    for name in names:
        if name.endswith("/") or name.rsplit("/", 1)[-1] != "note.note":
            continue
        prefix = name[:-len("note.note")]
        if prefix.count("/") > 1 or prefix.startswith("__MACOSX/") or prefix in roots:
            continue
        if prefix + "pageIdInfo.dat" in files or prefix + "end_tag.bin" in files:
            roots.append(prefix)
    return roots


def _finite(*values: float) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def _bgra(raw: Optional[bytes], default: RGBA = BLACK) -> RGBA:
    if raw is None or len(raw) != 4:
        return default
    b, g, r, a = raw
    return r / 255.0, g / 255.0, b / 255.0, a / 255.0


def _pen_short(name: str) -> str:
    return name[len(PEN_PREFIX):] if name.startswith(PEN_PREFIX) else name.rsplit(".", 1)[-1]


@dataclass
class _Png:
    """A decoded PNG picture (:func:`gnnote.pdf.images.decode_png`) and the chunks a crop of
    it keeps: colour management and pixel size as they are, the colour-key transparency
    (``tRNS``) of grey and RGB pictures in the decoded sample depth."""

    width: int
    height: int
    channels: int
    bits: int
    samples: bytes
    extras: List[Tuple[bytes, bytes]]


_KEPT_CHUNKS = (b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"pHYs")
MAX_PNG_CHUNKS = 1_000  # chunks before the picture data that are looked at for a crop


def _decode_png(data: bytes) -> Optional[_Png]:
    """``data`` decoded for cropping; ``None`` when it cannot be decoded or is above
    :data:`MAX_CROP_PIXELS`."""
    from ..pdf.images import ImageError, decode_png

    size = image_pixel_size(data)
    if size is None or size[0] * size[1] > MAX_CROP_PIXELS:
        return None
    try:
        width, height, channels, bits, samples = decode_png(data)
    except (ImageError, ValueError, zlib.error, IndexError, struct.error):
        return None
    if channels not in (1, 2, 3, 4) or bits not in (8, 16) or len(samples) < width * height * channels * bits // 8:
        return None
    extras: List[Tuple[bytes, bytes]] = []
    trns: Optional[bytes] = None
    colour_type = depth = -1
    pos = 8
    for _ in range(MAX_PNG_CHUNKS):
        if pos + 12 > len(data):
            break
        n = int.from_bytes(data[pos:pos + 4], "big")
        tag, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        if len(body) != n or tag in (b"IDAT", b"IEND"):
            break
        if tag == b"IHDR" and n >= 13:
            depth, colour_type = body[8], body[9]
        elif tag in _KEPT_CHUNKS:
            extras.append((tag, body))
        elif tag == b"tRNS":
            trns = body
        pos += 12 + n
    # the colour key of a grey or RGB picture (a palette's transparency is in the decoded alpha)
    if trns is not None and (colour_type, channels) in ((0, 1), (2, 3)) and len(trns) == 2 * channels:
        if depth < 8:  # sub-byte grey was scaled to 8 bits, and so is its key
            key = min(255, int.from_bytes(trns, "big") * (255 // ((1 << depth) - 1)))
            trns = key.to_bytes(2, "big")
        extras.append((b"tRNS", trns))
    return _Png(width, height, channels, bits, samples, extras)


def _crop_box(png: _Png, box: Tuple[int, int, int, int]) -> Optional[Tuple[int, int, int, int]]:
    x0, y0 = max(0, box[0]), max(0, box[1])
    x1, y1 = min(png.width, box[2]), min(png.height, box[3])
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _crop(png: _Png, box: Tuple[int, int, int, int]) -> bytes:
    """The pixels ``box`` (inside the picture: :func:`_crop_box`) of ``png`` as a new PNG."""
    x0, y0, x1, y1 = box
    pixel = png.channels * png.bits // 8
    stride = png.width * pixel
    samples = png.samples
    rows = b"".join(b"\0" + samples[y * stride + x0 * pixel:y * stride + x1 * pixel] for y in range(y0, y1))
    colour_type = {1: 0, 2: 4, 3: 2, 4: 6}[png.channels]

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", x1 - x0, y1 - y0, png.bits, colour_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + b"".join(chunk(t, b) for t, b in png.extras) + \
        chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")


def crop_png(data: bytes, box: Tuple[int, int, int, int]) -> Optional[bytes]:
    """The pixels ``box`` = (x0, y0, x1, y1) of a PNG as a new PNG; ``None`` when the picture
    cannot be decoded, is above :data:`MAX_CROP_PIXELS` or the box is empty."""
    png = _decode_png(data)
    inside = _crop_box(png, box) if png is not None else None
    return _crop(png, inside) if png is not None and inside is not None else None


def _rotate(x: float, y: float, cx: float, cy: float, degrees: float) -> XY:
    """``(x, y)`` turned ``degrees`` clockwise (y down) about ``(cx, cy)``."""
    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    dx, dy = x - cx, y - cy
    return cx + dx * c - dy * s, cy + dx * s + dy * c


# --------------------------------------------------------------------------- paths


def _arc_cubics(cx: float, cy: float, rx: float, ry: float, start: float, sweep: float) -> List[Tuple[XY, XY, XY]]:
    """Cubic Bezier pieces (c1, c2, end) of the elliptical arc from ``start`` sweeping
    ``sweep`` degrees (clockwise on screen for positive values), at most 90 degrees each."""
    sweep = max(-360.0, min(360.0, sweep))
    n = max(1, int(math.ceil(abs(sweep) / ARC_SEGMENT_DEGREES - 1e-9)))
    step = math.radians(sweep / n)
    k = 4.0 / 3.0 * math.tan(step / 4.0)
    a = math.radians(start)
    out = []
    for _ in range(n):
        b = a + step
        ca, sa, cb, sb = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
        c1 = (cx + rx * (ca - k * sa), cy + ry * (sa + k * ca))
        c2 = (cx + rx * (cb + k * sb), cy + ry * (sb - k * cb))
        out.append((c1, c2, (cx + rx * cb, cy + ry * sb)))
        a = b
    return out


class _Path:
    """Sub-paths of a shape outline as Bezier chains: ``(anchors, controls)`` per sub-path,
    one control pair per segment (straight segments get handles at the thirds)."""

    def __init__(self) -> None:
        self.subpaths: List[Tuple[List[XY], List[Tuple[XY, XY]]]] = []
        self.curved = False  # arcs or ovals, gnnote's own construction

    def _current(self) -> Optional[Tuple[List[XY], List[Tuple[XY, XY]]]]:
        return self.subpaths[-1] if self.subpaths else None

    def move(self, p: XY) -> None:
        self.subpaths.append(([p], []))

    def line(self, p: XY) -> None:
        cur = self._current()
        if cur is None:
            self.move(p)
            return
        q = cur[0][-1]
        cur[1].append(((q[0] + (p[0] - q[0]) / 3.0, q[1] + (p[1] - q[1]) / 3.0),
                       (q[0] + 2.0 * (p[0] - q[0]) / 3.0, q[1] + 2.0 * (p[1] - q[1]) / 3.0)))
        cur[0].append(p)

    def cubic(self, c1: XY, c2: XY, p: XY) -> None:
        cur = self._current()
        if cur is None:
            self.move(c1)
            cur = self._current()
            assert cur is not None
        cur[1].append((c1, c2))
        cur[0].append(p)

    def close(self) -> None:
        cur = self._current()
        if cur is not None and len(cur[0]) > 1 and cur[0][0] != cur[0][-1]:
            self.line(cur[0][0])
            self.subpaths.append(([cur[0][0]], []))  # drawing continues from the start point
        elif cur is not None:
            self.subpaths.append(([cur[0][0]], []))

    @classmethod
    def build(cls, segments: Sequence[R.Segment]) -> "_Path":
        path = cls()
        for seg in segments:
            pts = seg.points
            if seg.kind == 1:
                path.move(pts[0])
            elif seg.kind == 2:
                path.line(pts[0])
            elif seg.kind == 3:
                cur = path._current()
                start = cur[0][-1] if cur is not None else pts[0]
                c1, c2 = quad_to_cubic(start, pts[0], pts[1])
                path.cubic(c1, c2, pts[1])
            elif seg.kind == 4:
                path.cubic(pts[0], pts[1], pts[2])
            elif seg.kind == 5:  # arcTo(oval, start, sweep): a line to the arc's start, then the arc
                (x0, y0), (x1, y1), (start_deg, sweep_deg) = pts
                cx, cy, rx, ry = (x0 + x1) / 2.0, (y0 + y1) / 2.0, abs(x1 - x0) / 2.0, abs(y1 - y0) / 2.0
                if not _finite(start_deg, sweep_deg):
                    continue
                a = math.radians(start_deg)
                begin = (cx + rx * math.cos(a), cy + ry * math.sin(a))
                if path._current() is None:
                    path.move(begin)
                else:
                    path.line(begin)
                for c1, c2, end in _arc_cubics(cx, cy, rx, ry, start_deg, sweep_deg):
                    path.cubic(c1, c2, end)
                path.curved = True
            elif seg.kind == 6:
                path.close()
            elif seg.kind == 7:  # addOval: a closed sub-path of its own
                (x0, y0), (x1, y1) = pts
                cx, cy, rx, ry = (x0 + x1) / 2.0, (y0 + y1) / 2.0, abs(x1 - x0) / 2.0, abs(y1 - y0) / 2.0
                path.move((cx + rx, cy))
                for c1, c2, end in _arc_cubics(cx, cy, rx, ry, 0.0, 360.0):
                    path.cubic(c1, c2, end)
                path.subpaths.append(([(cx + rx, cy)], []))
                path.curved = True
        path.subpaths = [sp for sp in path.subpaths if len(sp[0]) >= 2]
        return path


# --------------------------------------------------------------------------- body text layout


@dataclass
class _Placed:
    """Something the body layout put on a page: a text chunk or an inline object."""

    page: int
    top: float  # pt from the page top
    height: float
    runs: List[TextRun] = field(default_factory=list)
    size: float = 12.0
    lead: float = 0.0  # extra leading the app puts above the chunk's first line
    indent: float = 0.0  # pt right of the left margin
    obj: Optional[R.InlineObject] = None

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


class _Styles:
    """Per-UTF-16-unit character styles of a text, from its spans (later spans win).

    Spans are applied with slice assignments; the total work is capped by
    :data:`MAX_STYLE_WORK` so that crafted overlapping spans cannot cost unbounded time
    (:attr:`truncated` then says formatting was partly ignored).  Styles change only at the
    ends of the spans applied (:attr:`edges`), so :meth:`runs` compares one unit per piece
    between two such ends, not every unit."""

    def __init__(self, body: R.TextBody, dp: float):
        n = len(body.text)
        self.bold = bytearray(n)
        self.italic = bytearray(n)
        self.underline = bytearray(n)
        self.colour: List[Optional[RGBA]] = [None] * n
        self.size: List[Optional[float]] = [None] * n
        self.font: List[Optional[str]] = [None] * n
        self.truncated = False
        edges = {0, n}
        work = 0
        for span in body.spans:
            lo, hi = max(0, span.start), min(n, span.end)
            if hi <= lo:
                continue
            work += hi - lo
            if work > MAX_STYLE_WORK:
                self.truncated = True
                break
            edges.add(lo)
            edges.add(hi)
            k = span.kind
            if k in (SPAN_BOLD, SPAN_ITALIC, SPAN_UNDERLINE):
                on = 1 if len(span.data) >= 4 and int.from_bytes(span.data[:4], "little") != 0 else 0
                target = self.bold if k == SPAN_BOLD else self.italic if k == SPAN_ITALIC else self.underline
                target[lo:hi] = bytes([on]) * (hi - lo)
            elif k == SPAN_COLOUR and len(span.data) >= 4:
                self.colour[lo:hi] = [_bgra(span.data[:4])] * (hi - lo)
            elif k == SPAN_SIZE:
                v = _f32(span.data)
                if v is not None and 1.0 <= v <= MAX_FONT_DP:
                    self.size[lo:hi] = [v * dp] * (hi - lo)
            elif k == SPAN_FONT:
                name = _span_string(span.data)
                if name:
                    self.font[lo:hi] = [name] * (hi - lo)
        self.edges = sorted(edges)  # the styles are the same from one edge up to the next

    def _key(self, i: int) -> tuple:  # type: ignore[type-arg]
        return self.bold[i], self.italic[i], self.underline[i], self.colour[i], self.size[i], self.font[i]

    def runs(self, text: str, start: int, end: int) -> List[TextRun]:
        """Runs of ``text[start:end]`` (one char per UTF-16 unit) with their styles."""
        runs: List[TextRun] = []
        edges = self.edges
        e = bisect.bisect_right(edges, start)
        lo = start
        while lo < end:
            key = self._key(lo)
            hi = min(end, edges[e]) if e < len(edges) else end
            while hi < end and self._key(hi) == key:  # the next piece looks the same: merge it
                e += 1
                hi = min(end, edges[e]) if e < len(edges) else end
            e += 1
            piece = R.units_to_text(text[lo:hi]).replace(OBJECT_CHAR, "")
            if piece:
                runs.append(TextRun(piece, bold=bool(key[0]), italic=bool(key[1]), underline=bool(key[2]),
                                    color=key[3], size=key[4], font=key[5]))
            lo = hi
        return runs


class _Full(Exception):
    """The body layout reached its page cap (``args[0]`` "pages") or its text-box cap
    ("text boxes"); what follows is not laid out."""


class _BodyLayout:
    """Lays the note's body text out on the pages, approximately as the app does.

    The app flows the body down the pages like a word processor and stores no line
    positions, so gnnote reproduces the flow: left/top/right margins from the text record,
    lines wrapped at the column width (Roboto, the app's font, has nearly Helvetica's
    advance widths), a line pitch of ``font size x line spacing`` (the paragraph's own value,
    else :data:`DEFAULT_LINE_SPACING`, the natural line height of Roboto) with the baseline
    :data:`LINE_DESCENT` font sizes above the line's bottom, the paragraph spacing before
    and after (also at a page top), and a line that does not fit moved to the next page.
    Pictures and tables in the text are blocks followed by :data:`BLOCK_GAP` font sizes of
    space; the page the app recorded for them is a lower bound for their page, and a table
    or code block (which gnnote does not convert) may run over a page break as in the app.
    Each paragraph (its lines on one page) becomes one text box at the position of its first
    line, so a target app that draws lines closer together than Samsung Notes does shifts
    text only inside a paragraph, never across the page.

    The layout stops before page ``max_pages`` (counted from 0) and after ``max_chunks`` text
    boxes; :attr:`dropped_paragraphs` and :attr:`dropped_objects` then count what was not
    laid out (a paragraph cut off part-way counts as dropped).
    """

    def __init__(self, body: R.TextBody, page_heights: List[float], unit_scale: float = 1.0,
                 max_pages: int = 1 << 62, max_chunks: int = 1 << 62):
        self.body = body
        self.max_pages = max(1, max_pages)
        self.max_chunks = max_chunks
        self.chunks = 0  # text boxes placed
        self.objects_placed = 0
        self.dropped_paragraphs = 0
        self.dropped_objects = 0
        self.full = ""  # why the layout stopped early: "pages" or "text boxes"
        self.text = body.text  # one char per UTF-16 unit
        self.dp = PAGE_WIDTH_PT / DP_PAGE_WIDTH
        self.unit_scale = unit_scale  # pt per canvas unit (inline objects)
        left, top, right, bottom = (max(0.0, min(v, 200.0)) if _finite(v) else 0.0 for v in body.margins)
        self.left, self.top, self.right, self.bottom = (left * self.dp, top * self.dp, right * self.dp,
                                                        bottom * self.dp)
        self.column = max(PAGE_WIDTH_PT / 4.0, PAGE_WIDTH_PT - self.left - self.right)
        self.heights = list(page_heights) or [PAGE_WIDTH_PT * math.sqrt(2.0)]
        self.styles = _Styles(body, self.dp)
        self.page = 0
        self.y = self.top
        self.at_top = True
        self.placed: List[_Placed] = []
        self._chunk: Optional[_Placed] = None
        # paragraph attribute kind -> value per paragraph (``None``: the default)
        count = self.text.count("\n") + 1
        self._para: Dict[int, List[Optional[float]]] = {
            k: [None] * count for k in (PARA_INDENT, PARA_LINE_SPACING, PARA_SPACE_BEFORE, PARA_SPACE_AFTER)}
        self._bullet = bytearray(count + 1)  # 1: a list item (one more, always 0, after the last)
        work = 0
        for p in body.paragraphs:
            if p.kind == PARA_BULLET:  # u32 list type (0: none), ...
                lo, hi = max(0, p.start), min(count, p.end)
                if hi > lo and len(p.data) >= 4 and int.from_bytes(p.data[:4], "little"):
                    work += hi - lo
                    if work > MAX_STYLE_WORK:
                        self.styles.truncated = True
                        break
                    self._bullet[lo:hi] = b"\1" * (hi - lo)
                continue
            if p.kind not in self._para:
                continue
            if p.kind == PARA_INDENT:  # u32 level, u32 (always 1 in the samples)
                level = int.from_bytes(p.data[:4], "little") if len(p.data) >= 4 else 0
                value = min(level, MAX_INDENT_LEVEL) * INDENT_DP * self.dp if level else None
            elif p.kind == PARA_LINE_SPACING:  # u32 type (1 = a multiple of the font size), f32 value
                value = _f32(p.data[4:8]) if len(p.data) >= 8 and int.from_bytes(p.data[:4], "little") == 1 \
                    else None
                value = value if value is not None and 0.5 <= value <= 5.0 else None
            else:
                value = _f32(p.data)
                value = value * self.dp if value is not None and 0.0 <= value <= MAX_SPACING_DP else None
            lo, hi = max(0, p.start), min(count, p.end)
            if value is None or hi <= lo:
                continue
            work += hi - lo
            if work > MAX_STYLE_WORK:
                self.styles.truncated = True
                break
            self._para[p.kind][lo:hi] = [value] * (hi - lo)

    def _para_value(self, index: int, kind: int, default: float) -> float:
        values = self._para[kind]
        value = values[index] if index < len(values) else None
        return default if value is None else value

    def _space_before(self, index: int) -> float:
        """The paragraph's space before; none between two list items (as the app does)."""
        if index and self._bullet[index] and self._bullet[index - 1]:
            return 0.0
        return self._para_value(index, PARA_SPACE_BEFORE, 0.0)

    def _space_after(self, index: int) -> float:
        if self._bullet[index] and self._bullet[index + 1]:
            return 0.0
        return self._para_value(index, PARA_SPACE_AFTER, 0.0)

    # -- helpers -----------------------------------------------------------------------

    def size_of(self, start: int, end: int) -> float:
        """The largest font size (pt) of ``text[start:end]`` (of the unit at ``start`` when empty)."""
        sizes = [v for v in self.styles.size[start:max(end, start + 1)] if v is not None]
        return max(sizes) if sizes else DEFAULT_FONT_DP * self.dp

    def wrap(self, start: int, end: int, size: float, column: float) -> List[Tuple[int, int]]:
        """Unit ranges of the lines ``text[start:end]`` wraps into at the ``column`` width."""
        if end <= start:
            return [(start, start)]
        lines: List[Tuple[int, int]] = []
        line_start = start
        width = 0.0  # of the line up to the last space
        word_start = start
        word = 0.0
        word_width: Optional[float] = None  # of the whole current word, measured on demand
        for i in range(start, end):
            ch = self.text[i]
            adv = _advance(ch) * size
            if ch == " ":
                width += word + adv
                word = 0.0
                word_start = i + 1
                word_width = None
                continue
            word += adv
            if width + word > column:
                if word_width is None:
                    j = i
                    while j < end and self.text[j] != " ":
                        j += 1
                    word_width = word + sum(_advance(c) * size for c in self.text[i + 1:j])
                if width > 0.0 and word_width <= column:  # the word moves to the next line
                    lines.append((line_start, word_start))
                    line_start = word_start
                    width = 0.0
                elif i > line_start:  # a word longer than a line (a URL) breaks before this char
                    lines.append((line_start, i))
                    line_start = word_start = i
                    width = 0.0
                    word = adv
        lines.append((line_start, end))
        return lines

    def _page_height(self, page: int) -> float:
        return self.heights[page] if page < len(self.heights) else self.heights[-1]

    def _next_page(self) -> None:
        self._flush()
        if self.page + 1 >= self.max_pages:
            raise _Full("pages")
        self.page += 1
        self.y = self.top
        self.at_top = True

    def _fits(self, height: float) -> bool:
        return self.at_top or self.y + height <= self._page_height(self.page) - self.bottom

    def _flush(self) -> None:
        chunk = self._chunk
        self._chunk = None
        if chunk is None:
            return
        while chunk.runs and not chunk.runs[-1].text.strip("\n"):
            chunk.runs.pop()
        if chunk.runs:
            chunk.runs[-1].text = chunk.runs[-1].text.rstrip("\n")
        if chunk.text.strip():
            self.placed.append(chunk)
            self.chunks += 1

    def _paragraphs(self) -> Iterator[Tuple[int, int, int]]:
        """``(start, end, index)`` of every paragraph (without its newline)."""
        text = self.text
        start = 0
        index = 0
        while True:
            end = text.find("\n", start)
            if end < 0:
                yield start, len(text), index
                return
            yield start, end, index
            start = end + 1
            index += 1

    def _has_text(self, start: int, end: int) -> bool:
        return bool(self.text[start:end].replace(OBJECT_CHAR, "").strip())

    # -- flow --------------------------------------------------------------------------

    def layout(self) -> List[_Placed]:
        inline: Dict[int, List[R.InlineObject]] = {}
        for item in self.body.inline:
            inline.setdefault(item.index, []).append(item)
        paragraphs = self._paragraphs()
        start = end = 0
        try:
            for start, end, index in paragraphs:
                self._paragraph(start, end, index, inline)
            start = end = len(self.text)
            self._flush()
            for items in inline.values():  # objects not anchored on an object character
                for obj in items:
                    self._block(obj, DEFAULT_FONT_DP * self.dp)
        except _Full as stop:
            self.full = str(stop.args[0])
            self._chunk = None
            self.dropped_paragraphs = int(self._has_text(start, end))
            self.dropped_paragraphs += sum(1 for lo, hi, _ in paragraphs if self._has_text(lo, hi))
            self.dropped_objects = len(self.body.inline) - self.objects_placed
        return self.placed

    def _paragraph(self, start: int, end: int, index: int, inline: Dict[int, List[R.InlineObject]]) -> None:
        self._flush()  # every paragraph is a text box of its own
        size = self.size_of(start, end)  # once per paragraph: it scans the whole paragraph
        objects = [obj for i in range(start, end) if self.text[i] == OBJECT_CHAR for obj in inline.pop(i, [])]
        for obj in objects:
            self._block(obj, size)
        content = "".join(ch for ch in self.text[start:end] if ch != OBJECT_CHAR)
        if objects and not content.strip():
            return
        pitch = size * self._para_value(index, PARA_LINE_SPACING, DEFAULT_LINE_SPACING)
        self.y += self._space_before(index)
        if not content.strip():  # an empty line
            if not self._fits(pitch):
                self._next_page()
            self.y += pitch + self._space_after(index)
            self.at_top = False
            return
        indent = min(self._para_value(index, PARA_INDENT, 0.0), self.column - PAGE_WIDTH_PT / 4.0)
        for lo, hi in self.wrap(start, end, size, self.column - indent):
            if not self._fits(pitch):
                self._next_page()
            runs = self.styles.runs(self.text, lo, hi)
            if self._chunk is None:
                if self.chunks >= self.max_chunks:
                    raise _Full("text boxes")
                lead = max(0.0, pitch - (LINE_DESCENT + WRITER_ASCENT) * size)
                self._chunk = _Placed(self.page, self.y, 0.0, size=size, lead=lead, indent=max(0.0, indent))
            # the lines of a paragraph join without a break (each keeps its trailing
            # space): the target app wraps the paragraph again at the box width
            self._chunk.runs.extend(runs)
            self._chunk.size = max(self._chunk.size, size)
            self.y += pitch
            self.at_top = False
            self._chunk.height = self.y - self._chunk.top
        self.y += self._space_after(index)

    def _block(self, item: R.InlineObject, size: float) -> None:
        """Place a picture / table / code block of the text as a block."""
        box = _object_box(item.obj)
        height = (box[3] - box[1]) * self.unit_scale if box is not None else 0.0
        height = max(0.0, min(height, 10.0 * self._page_height(self.page))) if _finite(height) else 0.0
        gap = BLOCK_GAP * size
        base = getattr(item.obj, "base", None)
        recorded = base.page_index if base is not None else None
        if recorded is not None and self.page < recorded < len(self.heights):
            while self.page < recorded:
                self._next_page()
        over_pages = item.constraint in (1, 2) and isinstance(item.obj, R.OtherObject)
        if not over_pages and not self._fits(height):
            self._next_page()
        if over_pages and recorded == self.page and box is not None:
            # a table or code block the app placed a little lower than gnnote's flow (it
            # keeps margins gnnote does not know): its stored position wins
            stored = box[1] * self.unit_scale
            if self.y < stored <= self.y + STORED_SHIFT * size \
                    and stored < self._page_height(self.page) - self.bottom:
                self.y = stored
        self.placed.append(_Placed(self.page, self.y, height, obj=item))
        self.objects_placed += 1
        self.y += height + gap
        self.at_top = False
        if over_pages:
            self._run_over()

    def _run_over(self) -> None:
        """Move past the page breaks that a block running over pages crossed: page by page
        while the note has pages, then in one step (the pages after the note's last one are
        as high as it), so the cost never depends on how many pages are skipped."""
        while self.y > self._page_height(self.page) - self.bottom:
            height = self._page_height(self.page)
            if height <= 0.0:
                return
            self._flush()
            if self.page >= len(self.heights) - 1:
                skip = max(1, math.ceil((self.y - (height - self.bottom)) / height))
                if self.page + skip >= self.max_pages:
                    raise _Full("pages")
                self.y -= skip * height
                self.page += skip
                return
            if self.page + 1 >= self.max_pages:
                raise _Full("pages")
            self.y -= height
            self.page += 1


def _f32(data: bytes) -> Optional[float]:
    if len(data) < 4:
        return None
    v = struct.unpack("<f", data[:4])[0]
    return v if math.isfinite(v) else None


def _span_string(data: bytes) -> Optional[str]:
    """A font-name span: u16 length + UTF-16LE text (as the other strings of the format);
    the record keeps the first :data:`records.MAX_FONT_NAME` units of it."""
    if len(data) < 2:
        return None
    n = min(int.from_bytes(data[:2], "little"), R.MAX_FONT_NAME)
    raw = data[2:2 + 2 * n]
    if len(raw) != 2 * n or not n:
        return None
    name = raw.decode("utf-16-le", "replace").strip("\0 ")
    return name if name and name.isprintable() else None


_WIDE = re.compile("[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]")


def _advance(ch: str) -> float:
    """Advance width of ``ch`` in em (Helvetica's, which Roboto nearly matches)."""
    from ..pdf.text import HELVETICA_WIDTHS

    code = ord(ch)
    if 32 <= code <= 255:
        w = HELVETICA_WIDTHS[code - 32]
        return (w or 556) / 1000.0
    if _WIDE.match(ch) or code >= 0x1F000:
        return 1.0
    return 0.556


def _straight(p0: XY, c1: XY, c2: XY, p1: XY) -> bool:
    """Whether a cubic segment's handles lie on its chord's line (within 0.01 pt): it then
    draws (and fills) as the straight edge ``p0``-``p1``."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        return c1 == p0 and c2 == p0
    return all(abs((c[0] - p0[0]) * dy - (c[1] - p0[1]) * dx) <= 0.01 * length for c in (c1, c2))


def _hull_length(p0: XY, c1: XY, c2: XY, p1: XY) -> float:
    """Length of the control polygon, an upper bound of the curve's length."""
    return math.dist(p0, c1) + math.dist(c1, c2) + math.dist(c2, p1)


def _cubic_at(p0: XY, c1: XY, c2: XY, p1: XY, t: float) -> XY:
    u = 1.0 - t
    a, b, c, d = u * u * u, 3.0 * u * u * t, 3.0 * u * t * t, t * t * t
    return a * p0[0] + b * c1[0] + c * c2[0] + d * p1[0], a * p0[1] + b * c1[1] + c * c2[1] + d * p1[1]


def _kind_name(obj: object) -> str:
    """What a decoded object is, in words (for warnings)."""
    if isinstance(obj, R.Stroke):
        return "stroke"
    if isinstance(obj, R.Line):
        return "line"
    if isinstance(obj, R.Shape):
        return {2: "text box", 3: "picture"}.get(obj.obj_type, "shape")
    obj_type = getattr(obj, "obj_type", 0)
    return R.OBJECT_TYPES.get(obj_type, f"object of type {obj_type}")


def _object_box(obj: object) -> Optional[Tuple[float, float, float, float]]:
    """The page-unit bounding box of a decoded object as drawn (paths are stored rotated)."""
    path = getattr(obj, "path", None)
    if path:
        pts = [p for seg in path for p in (seg.points if seg.kind not in (5,) else seg.points[:2])]
        pts = [p for p in pts if _finite(*p)]
        if pts:
            return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)
    base = getattr(obj, "base", None)
    if base is not None and _finite(*base.rect):
        x0, y0, x1, y1 = base.rect
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    return None


# --------------------------------------------------------------------------- the reader


@dataclass
class _PageInfo:
    raw: Optional[R.Page]
    page: Page
    scale: float  # pt per canvas unit
    empty: bool  # nothing on the page record


class _Reader:
    def __init__(self, bundle: ZipBundle, root: str, roots: List[str], input_size: int = 0):
        self.bundle = bundle
        self.root = root
        self.roots = roots
        self.doc = Document(title="", source_format="samsungnotes")
        self.notes = PageNotes()
        self.counts: Dict[str, int] = {}
        self.budget = PointBudget()
        text_units = min(R.MAX_DOC_TEXT_UNITS, max(MIN_TEXT_UNITS, TEXT_UNITS_PER_BYTE * input_size))
        # stroke events and typed text are charged as they are decoded
        self.limits = R.Limits(budget=self.budget, text_units=text_units)
        self.media: Dict[int, str] = {}
        self.images_cache: Dict[str, Optional[Tuple[bytes, str]]] = {}
        self.input_size = input_size
        self.image_bytes = 0  # picture bytes handed to the model, every reference counted
        self.crops: Dict[Tuple[str, Tuple[int, int, int, int]], Optional[bytes]] = {}
        self.crop_pixels = MAX_CROP_WORK  # pixels picture crops may still decode and copy
        self.last_png: Optional[Tuple[str, Optional[_Png]]] = None  # the picture decoded last
        self.unconverted: Dict[Tuple[str, str], int] = {}  # (where, object kind) -> objects not converted
        self.lost: set = set()  # text formatting the model cannot hold
        self.text_boxes = min(MAX_TEXT_BOXES, MIN_TEXT_BOXES + input_size // TEXT_BOX_BYTES)  # still allowed
        self.landscape_note = False  # end_tag.bin's landscape flag

    def unconverted_object(self, where: str, kind: str) -> None:
        """Count an object that was not converted; ``where`` it was ("" on a page, "typed
        text", "text boxes" or "title")."""
        key = (where, kind)
        self.unconverted[key] = self.unconverted.get(key, 0) + 1

    def inline_lost(self, body: R.TextBody, where: str) -> None:
        """Count the objects anchored in a text that gnnote does not place: only the note's
        body text lays out its pictures."""
        for item in body.inline:
            self.unconverted_object(where, _kind_name(item.obj))
        if body.inline_failed:
            self.count("damaged", body.inline_failed)

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    def read_member(self, name: str) -> Optional[bytes]:
        return self.bundle.read(self.root + name)

    # -- top level ----------------------------------------------------------------------

    def read(self) -> Document:
        if len(self.roots) > 1:
            others = ", ".join(repr(r.rstrip("/")) for r in self.roots[1:4]) + (" ..." if len(self.roots) > 4 else "")
            self.warn(f"The archive holds {len(self.roots)} Samsung Notes notes; only "
                      f"{self.root.rstrip('/')!r} was converted (also: {others})")
        note = self._note()
        tag = self._end_tag()
        self._media()
        uuids = self._page_order()
        infos = self._pages(uuids, note, tag)
        if not infos and not self.limits.exhausted:
            infos.append(self._blank_page(note, tag))
        if note is not None:
            self._body(note, infos, tag)
        self._drop_ghost_page(infos, tag)
        self.doc.pages = [info.page for info in infos]
        self._report()
        return self.doc

    def _note(self) -> Optional[R.NoteDoc]:
        raw = self.read_member("note.note")
        if raw is None:
            self.warn("The note header (note.note) is missing or unreadable; pen names and typed text are lost")
            return None
        try:
            note = R.parse_note(raw, self.limits)
        except R.SdocxError as exc:
            self.warn(f"The note header (note.note) is damaged ({exc}); pen names and typed text are lost")
            return None
        for problem in note.problems:
            self.warn(f"Note header: {problem}")
        if note.font_size_delta:
            self.warn(f"The note changes the size of its typed text by {note.font_size_delta:+d} (a setting of the "
                      f"app); gnnote did not apply it")
        if note.title is not None:
            title = R.units_to_text(note.title.text).replace(OBJECT_CHAR, "").strip()
            self.doc.title = " ".join(title.split())[:500]
            self.inline_lost(note.title, "title")
        return note

    def _end_tag(self) -> R.EndTag:
        raw = self.read_member("end_tag.bin")
        if raw is None:
            return R.EndTag()
        try:
            tag = R.parse_end_tag(raw)
        except R.SdocxError:
            self.warn("The note's end tag (end_tag.bin) is damaged; the note is read as a paged note")
            return R.EndTag()
        if tag.locked:
            self.warn("The note is marked as locked; locked notes may hold encrypted content gnnote cannot read")
        if tag.pageless:
            self.warn("The note is a pageless note (one endless page); gnnote keeps it as one long page, which was "
                      "not checked against a note from the app")
        self.landscape_note = tag.landscape
        return tag

    def _media(self) -> None:
        raw = self.read_member("media/mediaInfo.dat")
        if raw is None:
            return
        try:
            self.media = R.parse_media_info(raw)
        except R.SdocxError:
            self.warn("The media list (media/mediaInfo.dat) is damaged; pictures and imported PDFs are lost")

    def _page_order(self) -> List[str]:
        listed: Optional[List[str]] = None
        raw = self.read_member("pageIdInfo.dat")
        if raw is not None:
            try:
                listed = R.parse_page_list(raw)
            except R.SdocxError:
                self.warn("The page list (pageIdInfo.dat) is damaged; pages follow the archive order")
        present = [n[len(self.root):-len(".page")] for n in self.bundle.files(self.root)
                   if n.endswith(".page") and "/" not in n[len(self.root):]]
        if listed is None:
            if raw is None:
                self.warn("The page list (pageIdInfo.dat) is missing; pages follow the archive order")
            return self._page_cap(present)
        out = []
        missing = 0
        for uuid in listed:
            if self.bundle.has(f"{self.root}{uuid}.page"):
                out.append(uuid)
            else:
                missing += 1
        if missing:
            self.warn(f"{missing} page(s) of the page list are missing from the archive")
        unlisted = len(set(present) - set(listed))
        if unlisted:
            self.warn(f"{unlisted} page file(s) not in the page list were ignored")
        return self._page_cap(out)

    def _page_cap(self, uuids: List[str]) -> List[str]:
        if len(uuids) > MAX_PAGES:
            self.warn(f"The note has {len(uuids)} pages; only the first {MAX_PAGES} were converted")
            return uuids[:MAX_PAGES]
        return uuids

    # -- pages --------------------------------------------------------------------------

    def _pages(self, uuids: List[str], note: Optional[R.NoteDoc], tag: R.EndTag) -> List[_PageInfo]:
        strings = note.strings if note is not None else {}
        fallback_w = (note.width if note is not None else 0) or tag.width
        infos: List[_PageInfo] = []
        for number, uuid in enumerate(uuids, 1):
            raw = self.read_member(f"{uuid}.page")
            page: Optional[R.Page] = None
            if raw is not None:
                try:
                    page = R.parse_page(raw, strings, self.limits)
                except R.SdocxError as exc:
                    if self.limits.exhausted:
                        self.warn("The note holds more objects than gnnote reads; the rest were skipped")
                        break
                    self.notes.add(number, f"the page is damaged ({exc}) and was left empty")
            else:
                self.notes.add(number, "the page could not be read and was left empty")
            raw = None  # the member's bytes are not needed once decoded
            width = page.width if page is not None and page.width else fallback_w
            height = page.height if page is not None and page.height else 0
            if not height and note is not None and tag.pageless:
                height = note.height
            if not height:
                height = round(width * math.sqrt(2.0))
            if not (MIN_PAGE_UNITS <= width <= MAX_PAGE_UNITS) or not (MIN_PAGE_UNITS <= height
                                                                         <= width * MAX_PAGE_RATIO) \
                    or height * PAGE_WIDTH_PT / width < MIN_PAGE_HEIGHT_PT:
                self.notes.add(number, "the page has no usable size; an A4-shaped page was used")
                width, height = 1080, 1527
            if width > height or (page is not None and page.orientation != 0):
                self.count("landscape")
            scale = PAGE_WIDTH_PT / width
            model = Page(width=PAGE_WIDTH_PT, height=height * scale)
            # a damaged page counts as not empty, so that it is never taken for the app's
            # empty last page and dropped
            info = _PageInfo(page, model, scale, page is not None and not page.problems)
            if page is not None:
                for problem in page.problems:
                    self.notes.add(number, problem)
                self._page_content(info, number)
                info.raw = None  # the decoded records are not needed any more
            infos.append(info)
            if self.limits.exhausted:
                self.warn("The note holds more objects than gnnote reads; the rest were skipped")
                break
        return infos

    def _blank_page(self, note: Optional[R.NoteDoc], tag: R.EndTag) -> _PageInfo:
        """The page made for a note without page records (its typed text still needs one)."""
        has_text = note is not None and note.body is not None and \
            bool(R.units_to_text(note.body.text).replace(OBJECT_CHAR, "").strip() or note.body.inline)
        self.warn("The note has no pages; its typed text was laid out on new pages" if has_text
                  else "The note has no pages; an empty page was made")
        width = (note.width if note is not None else 0) or tag.width
        if not MIN_PAGE_UNITS <= width <= MAX_PAGE_UNITS:
            width = 1080
        scale = PAGE_WIDTH_PT / width
        return _PageInfo(None, Page(width=PAGE_WIDTH_PT, height=PAGE_WIDTH_PT * math.sqrt(2.0)), scale, True)

    def _page_content(self, info: _PageInfo, number: int) -> None:
        raw = info.raw
        assert raw is not None
        page = info.page
        s = info.scale
        if raw.template_type is not None:
            page.paper = PAPER.get(raw.template_type, "plain")
            if raw.template_type not in KNOWN_TEMPLATES:
                self.count("templates")
        if raw.background_image is not None and raw.background_image >= 0:
            self.count("background-images")
        if raw.background_color is not None and min(raw.background_color[:3]) < 0xF0:
            self.count("page-colours")
        if raw.pdfs:
            info.empty = False
            self._pdf_background(info, number)
        if raw.custom_objects:
            info.empty = False
            self.count("custom-objects", raw.custom_objects)
        for layer in raw.layers:
            if layer.objects or layer.failed or layer.grouped:
                info.empty = False
            if not layer.visible:
                if layer.objects:
                    self.count("hidden-layers")
                continue
            if layer.grouped:
                self.count("grouped", layer.grouped)
            if layer.failed:
                self.count("damaged", sum(layer.failed.values()))
            objects = layer.objects
            objects.reverse()
            while objects:  # each record is released once converted (decoded events are large)
                self._object(objects.pop(), page, s, 0.0)

    def _pdf_background(self, info: _PageInfo, number: int) -> None:
        raw = info.raw
        assert raw is not None
        w_units = PAGE_WIDTH_PT / info.scale
        h_units = info.page.height / info.scale
        used = False
        for item in raw.pdfs:
            name = self.media.get(item.bind_id)
            x0, y0, x1, y1 = item.rect
            covers = _finite(x0, y0, x1, y1) and abs(x0) <= 0.02 * w_units and abs(y0) <= 0.02 * h_units \
                and abs(x1 - w_units) <= 0.02 * w_units and abs(y1 - h_units) <= 0.02 * h_units
            if used or not covers or name is None:
                self.count("pdf-placements")
                continue
            if name not in self.doc.pdfs:
                data = self.read_member("media/" + name)
                if data is None or image_format(data[:8]) != "pdf":
                    self.count("missing-media")
                    continue
                self.doc.pdfs[name] = data
            info.page.background = PdfBackground(name, int(item.page_index))
            used = True
            self.count("pdf-backgrounds")

    # -- objects ------------------------------------------------------------------------

    def _object(self, obj: object, page: Page, s: float, dy: float) -> None:
        base = getattr(obj, "base", None)
        if isinstance(base, R.ObjectBase) and not base.visible:
            self.count("invisible")
            return
        if isinstance(obj, R.Stroke):
            stroke = self._stroke(obj, s)
            if stroke is not None:
                page.strokes.append(stroke)
        elif isinstance(obj, R.Line):
            page.strokes.extend(self._line(obj, s, dy))
        elif isinstance(obj, R.Shape):
            if obj.text is not None:
                self.inline_lost(obj.text, "text boxes")
            if obj.obj_type == 3:
                image = self._image(obj, s, dy)
                if image is not None:
                    page.images.append(image)
                return
            page.strokes.extend(self._shape(obj, s, dy))
            if obj.obj_type == 2 or (obj.text is not None and R.units_to_text(obj.text.text).strip()):
                box = self._text_box(obj, s, dy)
                if box is not None:
                    page.texts.append(box)
        elif isinstance(obj, R.OtherObject):
            self.unconverted_object("", _kind_name(obj))

    def _pt(self, x: float, y: float, s: float, dy: float = 0.0) -> Optional[XY]:
        if not _finite(x, y):
            return None
        px, py = x * s, y * s + dy
        if abs(px) > MAX_COORD_PT or abs(py) > MAX_COORD_PT:
            return None
        return px, py

    def _stroke(self, st: R.Stroke, s: float) -> Optional[Stroke]:
        if st.unknown_fields:
            self.count("unknown-fields")
            return None
        if st.eraser or st.tool_type == 4:
            self.count("eraser")
            return None
        if st.over_budget:  # the budget refused the events: the report says so
            return None
        if st.rainbow or st.colour_type:  # (gradient colours alone are drawn as the solid colour)
            self.count("colour-effects")
        short = _pen_short(st.pen_name) if st.pen_name else ""
        highlighter = short in HIGHLIGHTER_PENS
        size = st.pen_size if st.pen_size is not None and _finite(st.pen_size) and st.pen_size > 0 else None
        if size is None:
            self.count("no-size")
            size = DEFAULT_PEN_UNITS
        size = min(size * s, MAX_SIZE_PT)
        coords = []
        for i, (x, y) in enumerate(zip(st.xs, st.ys)):
            p = self._pt(x, y, s)
            if p is not None:
                pressure = st.pressures[i] if i < len(st.pressures) else FIXED_PRESSURE
                coords.append((p, pressure))
        if not coords:
            self.count("no-points")
            return None
        if st.line_type:
            self.count("dashed")
        pressure_pen = short in PRESSURE_PENS and not (short == "InkPen2" and st.fixed_width) \
            and not (short == "Pencil2" and st.fixed_opacity)
        if highlighter and short in STRAIGHT_PENS:
            coords = [coords[0], coords[-1]] if len(coords) > 1 else coords
            width = size
            widths = [width] * len(coords)
        elif highlighter:
            width = size * HIGHLIGHTER_FACTOR
            widths = [width] * len(coords)
        elif pressure_pen:
            lo, hi = PRESSURE_CLAMP
            widths = [size * min(hi, max(lo, p)) if _finite(p) else size * FIXED_PRESSURE for _, p in coords]
            width = size * (lo + hi) / 2.0
            self.count("pressure")
        else:
            width = size * FIXED_PRESSURE
            widths = [width] * len(coords)
        points = [Point(x, y, w) for ((x, y), _), w in zip(coords, widths)]
        return Stroke(points=points, color=_bgra(st.color_bgra), kind="highlighter" if highlighter else "pen",
                      pen=PEN_KINDS.get(short), width=width)

    def _line_style(self, style: R.ShapeStyle, s: float) -> Tuple[RGBA, float, bool]:
        """Colour, width (pt) and visibility of a shape's outline: drawn when the shape has a
        line colour or a line style (the samples' shapes have only a style and the app draws
        them black); the other one then takes its default."""
        if style.line_color_type == 2 or (style.line_color is None and style.line_width is None):
            return BLACK, 0.0, False
        colour = _bgra(style.line_color) if style.line_color is not None else BLACK
        width = style.line_width if style.line_width is not None and _finite(style.line_width) \
            else DEFAULT_LINE_UNITS
        if style.dash_type:
            self.count("dashed")
        return colour, min(max(width, 0.0) * s, MAX_SIZE_PT), width > 0 and colour[3] > 0

    def _chain(self, anchors: List[XY], controls: List[Tuple[XY, XY]], width: float, colour: RGBA, s: float,
               dy: float) -> Optional[Stroke]:
        pts = [self._pt(x, y, s, dy) for x, y in anchors]
        ctl = [(self._pt(a[0], a[1], s, dy), self._pt(b[0], b[1], s, dy)) for a, b in controls]
        if any(p is None for p in pts) or any(a is None or b is None for a, b in ctl):
            self.count("bad-geometry")
            return None
        if not self.budget.take(len(pts)):
            return None
        points = [Point(p[0], p[1], width) for p in pts if p is not None]
        handles = [(Point(a[0], a[1], width), Point(b[0], b[1], width)) for a, b in ctl
                   if a is not None and b is not None]
        return Stroke(points=points, color=colour, width=width, controls=handles)

    def _shape(self, shape: R.Shape, s: float, dy: float) -> List[Stroke]:
        if not shape.path:
            return []
        path = _Path.build(shape.path)
        if not path.subpaths:
            return []
        colour, width, visible = self._line_style(shape.style, s)
        out: List[Stroke] = []
        if shape.fill_kind == 1 and shape.fill_color is not None and shape.fill_color[3] > 0:
            for anchors, controls in path.subpaths:
                if len(anchors) >= 3:
                    fill = self._fill(anchors, controls, _bgra(shape.fill_color), s, dy)
                    if fill is not None:
                        out.append(fill)
            if out:
                self.count("fills")
        elif shape.fill_kind in (1, 2, 3) and shape.obj_type == 7:
            self.count("unfilled")
        if visible:
            for anchors, controls in path.subpaths:
                stroke = self._chain(anchors, controls, width, colour, s, dy)
                if stroke is not None:
                    out.append(stroke)
        if path.curved:
            self.count("arcs")
        if out and _finite(shape.angle) and shape.angle % 360.0:
            self.count("rotated")
        if shape.obj_type == 7:
            self.count("shapes")
        return out

    def _fill(self, anchors: List[XY], controls: List[Tuple[XY, XY]], colour: RGBA, s: float,
              dy: float) -> Optional[Stroke]:
        """A closed sub-path as a fill polygon: straight segments as one edge, curves sampled
        about every :data:`FILL_SPACING` pt (coarser for a long outline, so that the curves
        give at most about :data:`MAX_FILL_SAMPLES` vertices), charged to the point budget."""
        pts = [self._pt(x, y, s, dy) for x, y in anchors]
        ctl = [(self._pt(a[0], a[1], s, dy), self._pt(b[0], b[1], s, dy)) for a, b in controls]
        if any(p is None for p in pts) or any(a is None or b is None for a, b in ctl):
            self.count("bad-geometry")
            return None
        segments = [(p0, c1, c2, p1) for p0, (c1, c2), p1 in zip(pts, ctl, pts[1:])]
        lengths = [0.0 if _straight(*seg) else _hull_length(*seg) for seg in segments]  # type: ignore[arg-type]
        spacing = max(FILL_SPACING, sum(lengths) / MAX_FILL_SAMPLES)
        steps = [max(1, min(MAX_FLATTEN_STEPS, int(math.ceil(n / spacing)))) for n in lengths]
        if not self.budget.take(sum(steps) + 2):
            return None
        first = pts[0]
        assert first is not None
        polygon = [Point(first[0], first[1], 0.0)]
        for (p0, c1, c2, p1), n in zip(segments, steps):
            for k in range(1, n + 1):
                x, y = _cubic_at(p0, c1, c2, p1, k / n)  # type: ignore[arg-type]
                polygon.append(Point(x, y, 0.0))
        if len(polygon) < 3:
            return None
        if (polygon[0].x, polygon[0].y) != (polygon[-1].x, polygon[-1].y):
            polygon.append(Point(polygon[0].x, polygon[0].y, 0.0))
        return Stroke(points=list(polygon), color=colour, kind="fill", width=0.0, outline=[list(polygon)])

    def _line(self, line: R.Line, s: float, dy: float) -> List[Stroke]:
        colour, width, visible = self._line_style(line.style, s)
        self.count("shapes")
        if line.style.begin_arrow or line.style.end_arrow:
            self.count("arrows", (line.style.begin_arrow != 0) + (line.style.end_arrow != 0))
        if not visible:
            return []
        if line.path:
            path = _Path.build(line.path)
            if path.curved:
                self.count("arcs")
            out = [stroke for anchors, controls in path.subpaths
                   if (stroke := self._chain(anchors, controls, width, colour, s, dy)) is not None]
            if out:
                return out
        if line.control_points:
            self.count("bent-lines")
        stroke = self._chain([line.start, line.end], [], width, colour, s, dy)
        if stroke is None:
            return []
        stroke.controls = straight_controls(stroke.points)
        return [stroke]

    def _media_image(self, image_id: Optional[int]) -> Optional[Tuple[str, bytes, str]]:
        name = self.media.get(image_id) if image_id is not None else None
        if name is None:
            self.count("missing-media")
            return None
        if name in self.images_cache:
            cached = self.images_cache[name]
            if cached is None:
                self.count("bad-media")
            return (name, *cached) if cached is not None else None
        data = self.read_member("media/" + name)
        result: Optional[Tuple[bytes, str]] = None
        if data is None:
            self.count("missing-media")
        else:
            fmt = image_format(data[:16])
            if fmt in ("png", "jpeg"):
                result = (data, fmt)
            elif name.lower().endswith(".spi"):
                self.count("spi")
            else:
                self.count("bad-media")
        self.images_cache[name] = result
        return (name, *result) if result is not None else None

    def _cropped(self, name: str, data: bytes, box: Tuple[int, int, int, int]) -> Optional[bytes]:
        """``box`` of the PNG ``data`` (media ``name``) as a new PNG, or ``None`` (then the
        whole picture is shown).  Each crop is made once however often it is used; each
        picture is decoded once in a row; decoding and copying share :data:`MAX_CROP_WORK`
        pixels per note, so that many cropped references cannot cost unbounded time."""
        key = (name, box)
        if key in self.crops:
            return self.crops[key]
        result: Optional[bytes] = None
        if self.last_png is None or self.last_png[0] != name:
            size = image_pixel_size(data)
            png: Optional[_Png] = None
            if size is not None and size[0] * size[1] <= min(MAX_CROP_PIXELS, self.crop_pixels):
                self.crop_pixels -= size[0] * size[1]
                png = _decode_png(data)
            self.last_png = (name, png)
        png = self.last_png[1]
        inside = _crop_box(png, box) if png is not None else None
        if png is not None and inside is not None:
            area = (inside[2] - inside[0]) * (inside[3] - inside[1])
            if area <= self.crop_pixels:
                self.crop_pixels -= area
                result = _crop(png, inside)
        self.crops[key] = result
        return result

    def _image(self, shape: R.Shape, s: float, dy: float) -> Optional[Image]:
        limit = self.input_size + MAX_SHARED_IMAGE_BYTES
        if self.image_bytes >= limit:
            self.count("image-shared")
            return None
        found = self._media_image(shape.image_id)
        if found is None:
            return None
        name, data, fmt = found
        x0, y0, x1, y1 = shape.rect
        if not _finite(x0, y0, x1, y1) or x1 <= x0 or y1 <= y0:
            self.count("bad-geometry")
            return None
        angle = shape.angle if _finite(shape.angle) else 0.0
        if shape.crop is not None and shape.original_rect is not None and _finite(*shape.original_rect):
            ox0, oy0, ox1, oy1 = shape.original_rect
            box = tuple(int(round(v)) if _finite(v) and abs(v) < 2 ** 31 else 0 for v in shape.crop)
            cropped = self._cropped(name, data, box) if fmt == "png" else None  # type: ignore[arg-type]
            if cropped is not None:
                data = cropped
            elif ox1 > ox0 and oy1 > oy0 and (ox0, oy0, ox1, oy1) != (x0, y0, x1, y1):
                self.count("cropped")
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                ocx, ocy = _rotate((ox0 + ox1) / 2.0, (oy0 + oy1) / 2.0, cx, cy, angle)
                hw, hh = (ox1 - ox0) / 2.0, (oy1 - oy0) / 2.0
                x0, y0, x1, y1 = ocx - hw, ocy - hh, ocx + hw, ocy + hh
        if shape.flip_h or shape.flip_v:
            self.count("flipped")
        if shape.border:
            self.count("borders")
        tl = self._pt(x0, y0, s, dy)
        br = self._pt(x1, y1, s, dy)
        if tl is None or br is None:
            self.count("bad-geometry")
            return None
        if self.image_bytes + len(data) > limit:
            self.count("image-shared")
            return None
        self.image_bytes += len(data)
        self.count("images")
        return Image(tl[0], tl[1], br[0] - tl[0], br[1] - tl[1], data, fmt=fmt,
                     rotation=round(angle % 360.0, 6))

    def _text_box(self, shape: R.Shape, s: float, dy: float) -> Optional[TextBox]:
        body = shape.text
        if body is None:
            return None
        text = R.units_to_text(body.text).replace(OBJECT_CHAR, "")
        if not text.strip():
            return None
        x0, y0, x1, y1 = shape.rect
        if not _finite(x0, y0, x1, y1) or x1 <= x0 or y1 <= y0:
            self.count("bad-geometry")
            return None
        tl = self._pt(x0, y0, s, dy)
        br = self._pt(x1, y1, s, dy)
        if tl is None or br is None:
            self.count("bad-geometry")
            return None
        if self.text_boxes <= 0:
            self.count("text-box-budget")
            return None
        self.text_boxes -= 1
        dp = PAGE_WIDTH_PT / DP_PAGE_WIDTH
        styles = _Styles(body, dp)
        runs = styles.runs(body.text, 0, len(body.text))
        left, top, right, bottom = (max(0.0, min(v, 200.0)) * dp if _finite(v) else 0.0 for v in body.margins)
        angle = shape.angle if _finite(shape.angle) else 0.0
        w = max(1.0, br[0] - tl[0] - left - right)
        h = max(1.0, br[1] - tl[1] - top - bottom)
        x, y = tl[0] + left, tl[1] + top
        if angle % 360.0:
            cx, cy = (tl[0] + br[0]) / 2.0, (tl[1] + br[1]) / 2.0
            x, y = _rotate(x, y, cx, cy, angle)
        size = next((r.size for r in runs if r.size), DEFAULT_FONT_DP * dp)
        colour = next((r.color for r in runs if r.color), BLACK)
        if styles.truncated or body.attributes_dropped:
            self.count("formatting")
        self._formatting(body)
        self.count("text-boxes")
        return TextBox(x, y, w, h, text, runs=runs, color=colour, size=size, rotation=round(angle % 360.0, 6))

    # -- typed body text ----------------------------------------------------------------

    def _formatting(self, body: R.TextBody) -> None:
        """Count the text formatting the model cannot hold (one warning per kind)."""
        for span in body.spans:
            on = len(span.data) >= 4 and int.from_bytes(span.data[:4], "little") != 0
            if span.kind == SPAN_STRIKE and on:
                self.lost.add("strikethrough")
            elif span.kind == SPAN_LINK and span.end > span.start:
                self.lost.add("links (the text is kept)")
            elif span.kind == SPAN_BACKGROUND and len(span.data) >= 4 and span.data[3]:
                self.lost.add("text highlight colours")
        if any(p.kind == PARA_BULLET for p in body.paragraphs):
            self.lost.add("list bullets and numbers")
        if any(p.kind == PARA_ALIGN and int.from_bytes(p.data[:4], "little") for p in body.paragraphs):
            self.lost.add("paragraph alignment (shown left-aligned)")

    def _body(self, note: R.NoteDoc, infos: List[_PageInfo], tag: R.EndTag) -> None:
        body = note.body
        if body is None or not infos:
            return
        text = R.units_to_text(body.text).replace(OBJECT_CHAR, "")
        if not text.strip() and not body.inline:
            return
        if body.inline_failed:
            self.count("damaged", body.inline_failed)
        self._formatting(body)
        scale = infos[0].scale
        pageless = tag.pageless and len(infos) == 1
        if pageless:  # one endless page: the text makes it longer instead of flowing onto new pages
            heights = [PAGE_WIDTH_PT * MAX_PAGE_RATIO]
            cap = 1
        else:
            heights = [info.page.height for info in infos]
            cap = len(infos) + MAX_EXTRA_PAGES
        layout = _BodyLayout(body, heights, scale, max_pages=cap, max_chunks=self.text_boxes)
        placed = layout.layout()
        self.text_boxes -= layout.chunks
        if layout.styles.truncated or body.attributes_dropped:
            self.count("formatting")
        before = len(infos)
        for item in placed:
            while item.page >= len(infos):  # (the layout keeps below ``cap``)
                last = infos[-1]
                infos.append(_PageInfo(None, Page(width=last.page.width, height=last.page.height,
                                                  paper=last.page.paper), last.scale, True))
            info = infos[item.page]
            if item.obj is None:
                info.page.texts.append(TextBox(layout.left + item.indent, item.top + item.lead,
                                               layout.column - item.indent,
                                               max(item.height - item.lead, item.size), item.text, runs=item.runs,
                                               size=item.size,
                                               color=next((r.color for r in item.runs if r.color), BLACK)))
                info.empty = False
                self.count("body-text")
                continue
            obj = item.obj.obj
            box = _object_box(obj)
            if box is None:
                continue
            dy = item.top - box[1] * scale
            info.empty = False
            if isinstance(obj, R.OtherObject):
                self.unconverted_object("typed text", _kind_name(obj))
                continue
            self._object(obj, info.page, scale, dy)
            self.count("inline")
        if pageless:
            page = infos[0].page
            bottom = max((item.top + item.height for item in placed), default=0.0) + layout.bottom
            if bottom > page.height:
                page.height = min(bottom, heights[0])
        if len(infos) > before:
            self.warn(f"The typed text needed {len(infos) - before} more page(s) than the note has; they were added")
        if layout.dropped_paragraphs or layout.dropped_objects:
            lost = [f"{layout.dropped_paragraphs} paragraph(s)"] if layout.dropped_paragraphs else []
            if layout.dropped_objects:
                lost.append(f"{layout.dropped_objects} picture(s) or other object(s)")
            why = ("the note would get more text boxes than gnnote reads" if layout.full == "text boxes"
                   else "it would make the endless page longer than gnnote reads" if pageless
                   else f"it would need more than {cap} pages")
            self.warn(f"The typed text is longer than gnnote reads ({why}); {' and '.join(lost)} of it were not "
                      f"converted")

    def _drop_ghost_page(self, infos: List[_PageInfo], tag: R.EndTag) -> None:
        """A paged note ends with an empty page the app adds for writing on; its PDF export
        leaves it out, and so does gnnote."""
        if tag.pageless or len(infos) < 2:
            return
        last = infos[-1]
        p = last.page
        if last.empty and not (p.strokes or p.images or p.texts or p.background is not None):
            infos.pop()

    # -- warnings -----------------------------------------------------------------------

    def _report(self) -> None:
        self.notes.emit(self.warn)
        c = self.counts
        for key, template in _COUNTED.items():
            n = c.get(key, 0)
            if n:
                self.warn(template.format(n=n))
        if self.landscape_note and not c.get("landscape"):
            self.warn("The note is marked as a landscape note; this was not checked against a note from the app")
        if c.get("pressure"):
            self.warn("Pen pressure was turned into per-point stroke widths with an approximate model; line "
                      "weights may differ from the app")
        if c.get("body-text") or c.get("inline"):
            self.warn("The typed text and the pictures in it were laid out by gnnote (the app stores no line "
                      "positions); line breaks and positions are approximate")
        if c.get("pdf-backgrounds"):
            self.warn(f"{c['pdf-backgrounds']} imported PDF page(s) became page backgrounds; this was not checked "
                      f"against a note from the app")
        if c.get("pdf-placements"):
            self.warn(f"{c['pdf-placements']} imported PDF page(s) that do not fill their page were not converted")
        if c.get("templates"):
            self.warn(f"{c['templates']} page(s) use a paper template gnnote does not draw (planner, to-do, ...); "
                      f"they get plain paper")
        if self.lost:
            self.warn("Text formatting without an equivalent in gnnote was dropped: " + ", ".join(sorted(self.lost)))
        if c.get("formatting"):
            self.warn("The typed text has more formatting than gnnote reads; some of it was ignored")
        if c.get("background-images"):
            self.warn(f"{c['background-images']} page background picture(s) were not converted")
        if c.get("page-colours"):
            self.warn(f"{c['page-colours']} page colour(s) were not converted")
        places = {"": "", "typed text": " in the typed text", "text boxes": " in text boxes or shapes",
                  "title": " in the note's title"}
        for (where, kind), n in sorted(self.unconverted.items()):
            self.warn(f"{n} {kind}(s){places[where]} were not converted")
        if self.budget.exhausted:
            self.warn("The note holds more ink points than gnnote reads; the remaining strokes were skipped")
        if self.limits.text_dropped:
            self.warn(f"The note holds more typed text than gnnote reads; {self.limits.text_dropped} character(s) "
                      f"(UTF-16 units) of it were not converted")
        for message in (self.bundle.size_warning("Samsung Notes"), self.bundle.failure_warning("Samsung Notes")):
            if message:
                self.warn(message)


def read_sdocx(data: bytes) -> Document:
    """Parse a Samsung Notes ``.sdocx`` note (or a ZIP of a note folder) given as bytes.

    Raises :class:`ValueError` when the data is not a readable Samsung Notes note; damage
    inside the note is tolerated with a line on ``Document.warnings``.
    """
    raw = ensure_bytes(data, "read_sdocx")
    bundle = ZipBundle(raw, "Samsung Notes")
    roots = find_note_root(bundle.names)
    if not roots:
        raise ValueError("not a Samsung Notes file: the archive has no note.note with pageIdInfo.dat or "
                         "end_tag.bin")
    return _Reader(bundle, roots[0], roots, len(raw)).read()
