"""GoodNotes ``.goodnotes`` -> :class:`gnnote.model.Document` (tolerant reader).

Byte layout implemented here (``docs/goodnotes-container.md``, ``goodnotes-stroke.md``,
``goodnotes-elements.md``, their critic additions, and the schema-25/35 supplements
``goodnotes-v35-binding.md``, ``goodnotes-v35-elements.md``, ``goodnotes-v35-strokes.md``):

Container
    A plain ZIP.  ``index.notes.pb``, ``index.attachments.pb`` and ``index.events.pb`` are
    varint-length-prefixed protobuf record streams (``gnnote.protobuf.decode_records``).
    ``index.notes.pb`` lists one ``{#1 N, #2 "notes/" + N}`` record per page (``N`` = notes
    layer UUID) in no particular order, ``index.attachments.pb`` one ``{#1 A, #2 member}`` per
    attachment (PDF paper templates / imported PDFs, PNG / JPEG rasters, sticker PDFs, M4A
    audio; no type stored).  The member is usually ``"attachments/" + A`` but a PDF inserted as
    a page may be stored under a different ("storage") id.

Event log (``index.events.pb``)
    Every record is ``{#1 entity UUID, #E {body}}`` where the field number ``E`` is the event
    type.  Used here: ``#30`` document created (``#2.#1`` title), ``#31`` renamed, ``#2``
    template (``#2`` T, ``#4`` attachment A, ``#5`` 1-based PDF page, ``#8 {#1 f32 canvas W,
    #2 f32 canvas H}``, ``#9`` catalogue name, ``#18.#3`` present only on visibly ruled / grid /
    dotted papers), ``#54`` page created (``#2`` page UUID P, ``#3.#1`` template T, ``#4.#1``
    order key, arbitrary printable ASCII compared bytewise), ``#3`` page re-bound to another
    template (``#2`` P, ``#3.#1`` T; the last event in log order wins), ``#55`` page reordered
    (``#2`` P, ``#3.#1`` new key), ``#56`` page deleted (``#2`` P), ``#6`` attachment added
    (``#1`` attachment id, ``#2`` storage id = ZIP member name, ``#5`` byte size).
    Page -> paper binding: ``N = P + 1`` as a 128-bit integer *with carry* (a page UUID ending
    in ``F`` has a notes UUID ending in ``0`` with the previous digit incremented), so events
    are keyed by the exact ``P`` computed from ``N``; hand-made files that only bumped the last
    digit are still found through a 31-hex-digit prefix match.

Page geometry
    Page size = the bound PDF page's MediaBox (``gnnote.pdfutil.pdf_info``, rotation applied);
    canvas size = ``#2.#8`` (= MediaBox x 132/72 in app-written files); all element geometry is
    in canvas units, origin top-left, y down, ``pt = canvas * page_width_pt / canvas_width``.

Page content (``notes/<N>``)
    A record stream of (metadata, content) pairs.  Metadata: ``{#1 element UUID, #2 clock,
    [#3 1 = tombstone], [#4 attachment UUID (images)], #8 device, #9 counter, #14 5381, #16
    schema (24, 25 or 35)}``.  The content record has exactly one top-level field whose number
    is the kind:

    * ``#7`` ink stroke ``{#1 UUID, #2 Apple-LZ4 frame -> TPL image (gnnote.applelz4 / gnnote.tpl),
      [#3 tool: absent flat/ball pen, 1 or 4 ribbon, 5 pencil], #4 {#1..#4 f32 RGBA, 0.0
      omitted}, [#5 1 highlighter], #6 "" | {#1 f32 dx, #2 f32 dy} lasso / group offset added to
      every point, [#9 auto-shape geometry: #1 points (2 = line, more = polyline, first == last
      = closed polygon, two identical = dot) / #2 {#1 P0, #2 C, #3 P1} one quadratic Bezier /
      #3 {centre, size} rectangle / #4 {centre, semi-axes, rotation} ellipse, #15 f32 W],
      [#20 {#1 ""} marker], #21 schema}``.  Flat format: quadratic Bezier segments, turned into
      the exact cubic chain ``c1 = P0 + 2/3 (C - P0)``, ``c2 = P1 + 2/3 (C - P1)``; width ``W / 2``
      pt.  Ribbon: per-point radius ``r`` -> width ``2 r`` canvas units; flags 4/5 (constant-radius
      band with elliptical caps) and ``#20`` both mean the marker tool.  Pencil: width ``W / 2``
      pt too (see ``PENCIL_WIDTH_FACTOR``).  Empty geometry (tombstones, shapes) is skipped.
      Multi-block and stored (``bv4-``) LZ4 frames are handled by ``gnnote.applelz4``.
    * ``#9`` shape fill ``{#1 UUID, #2 bbox, #4 geometry (same sub-messages as #7.#9), #5 UUID of
      the outline stroke, #7 RGBA fill colour (alpha 0.1), [#14 1 erased]}`` -> ``Stroke(kind=
      "fill")`` whose ``outline`` is the closed polygon, placed right after its outline stroke;
      erased fills (``#14``, NaN bbox) and fills without a live outline on the page are dropped.
    * ``#1`` image ``{#1 UUID, #2 rect(top-left, size), #3 rect(centre, size) crop [+ #3.#3
      rotation rad], #4 attachment UUID}``; bytes from ``attachments/<A>``: PNG, JPEG or a
      one-page vector PDF (die-cut sticker, ``Image.fmt = "pdf"``).  ``#2`` is the *displayed*
      size: for a JPEG with EXIF orientation 5-8 the raw pixels are rotated by 90 degrees to
      fill it, reported as ``Image.rotation`` 90 / 270 with the box kept as stored.
    * ``#8`` text box ``{#1 UUID, #2 outer rect, #3 text frame (= #2 inset by #10), #6 RTF
      (gnnote.rtf.parse_rtf; ``\\fsN`` half-points in canvas units), #10 f32 padding}``.
    * ``#21`` text element (schema 35: typed text and sticker letters) ``{#1 UUID, #2 35, #20
      {#1 origin, #2 f32 rotation rad, #3 f32 scale}, #21 {#2 fixed box size}, #32 {#1 {#2
      Apple-LZ4 frame -> {#1 run {#1 text, #2 {#3 RGBA, #30 family, #40 f32 size}, #3 {#4 2 =
      centred}}}}, #2 content size, #5 {#1 default style}, #10 insets}}``; frame = (origin +
      inset) x k, size = (box - 2 inset) x k x scale, font size = run size (or the default) x
      k x scale, rotation about the frame origin.
    * anything else (``#20`` sticky note, ``#22`` newer shape records, ...) -> one warning per
      kind, skipped.
"""
from __future__ import annotations

import io
import math
import re
import statistics
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .. import applelz4, protobuf, tpl
from ..model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from ..pdfutil import pdf_info
from ..protobuf import Field
from ..rtf import parse_rtf

__all__ = ["read_goodnotes", "CANVAS_PER_POINT", "DEFAULT_PAGE_SIZE", "BUILTIN_TEMPLATE_RE",
           "page_uuid_of_notes", "jpeg_exif_orientation"]

CANVAS_PER_POINT = 132.0 / 72.0  # GoodNotes canvas units per PDF point
DEFAULT_PAGE_SIZE = (455.04, 588.45)  # the GoodNotes "standard" paper, used when nothing else is known
# Pencil (tool 25) width in pt per TPL width word W.  goodnotes-stroke.md guessed 2.5 from a
# 3.8976-pt stroked path in Test5.pdf page 2, but that path is the W = 7.795 ball-pen stroke of
# the same page (7.795 / 2 = 3.8976); the pencil strokes of the exports are rasters whose
# visible width is 0.4-0.9 W depending on force (goodnotes-v35-strokes.md section 5.3), so W / 2
# like every other non-ribbon format stays the best constant.  Medium confidence.
PENCIL_WIDTH_FACTOR = 0.5
ELLIPSE_SAMPLES = 64
QUADRATIC_FILL_SAMPLES = 16
DEFAULT_TEXT_INSET = 10.0  # canvas units, #8.#10 and #21.#32.#10
DEFAULT_TEXT_SIZE = 24.0  # canvas units (\fs48 / #32.#5.#1.#40)
DEFAULT_TEXT_FONT = "Helvetica Neue"
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
BUILTIN_PRODUCERS = ("svg2pdf", "gnnote")  # GoodNotes' catalogue papers, gnnote-generated papers
BUILTIN_TEMPLATE_RE = re.compile(
    r"^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}_[a-z0-9]+_\d+_\d+ - .+$"
)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.S)
_RECT_PATH_RE = re.compile(
    rb"([-\d.]+) ([-\d.]+) m\s+([-\d.]+) ([-\d.]+) l\s+([-\d.]+) ([-\d.]+) l\s+([-\d.]+) ([-\d.]+) l\s+h\s+f"
)
# ``x y w h re`` rectangles: pdfutil.make_paper_pdf draws every rule this way (svg2pdf never does)
_RECT_OP_RE = re.compile(rb"(?<![-\d.])([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) re\b")
MAX_MEMBER_BYTES = 256 * 1024 * 1024  # declared (decompressed) size above which a ZIP member is skipped
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # decompressed bytes one container may hand out in total
_NOT_SET = -404  # sentinel in #21 styles: "inherit from the default style"

XY = Tuple[float, float]
RGBA = Tuple[float, float, float, float]


# --------------------------------------------------------------------------- protobuf helpers


def _decode(data: bytes) -> Optional[List[Field]]:
    return protobuf.try_decode_message(data)


def _msg(fields: Optional[Sequence[Field]], number: int) -> Optional[List[Field]]:
    if fields is None:
        return None
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_LEN:
        return None
    return _decode(bytes(f.value))


def _bytes(fields: Optional[Sequence[Field]], number: int) -> Optional[bytes]:
    if fields is None:
        return None
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_LEN:
        return None
    return bytes(f.value)


def _str(fields: Optional[Sequence[Field]], number: int) -> Optional[str]:
    raw = _bytes(fields, number)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _uuid(fields: Optional[Sequence[Field]], number: int) -> Optional[str]:
    s = _str(fields, number)
    if s is not None and UUID_RE.match(s):
        return s.upper()
    return None


def _int(fields: Optional[Sequence[Field]], number: int) -> Optional[int]:
    if fields is None:
        return None
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_VARINT:
        return None
    return int(f.value)


def _signed(fields: Optional[Sequence[Field]], number: int) -> Optional[int]:
    """Varint read as a 64-bit two's complement integer (``#21`` styles store -404 / -60 so)."""
    v = _int(fields, number)
    if v is None:
        return None
    return v - (1 << 64) if v >= (1 << 63) else v


def _f32(fields: Optional[Sequence[Field]], number: int, default: float = 0.0) -> float:
    if fields is None:
        return default
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_FIXED32:
        return default
    v = protobuf.fixed32_float(f)
    return v if math.isfinite(v) else default


def _has_nan(fields: Optional[Sequence[Field]], number: int) -> bool:
    if fields is None:
        return False
    f = protobuf.get(fields, number)
    return f is not None and f.wire_type == protobuf.WIRE_FIXED32 and math.isnan(protobuf.fixed32_float(f))


def _point(fields: Optional[Sequence[Field]]) -> Optional[XY]:
    if fields is None:
        return None
    if protobuf.get(fields, 1) is None and protobuf.get(fields, 2) is None:
        return None
    return (_f32(fields, 1), _f32(fields, 2))


def _rect(fields: Optional[Sequence[Field]]) -> Optional[Tuple[float, float, float, float]]:
    """``{#1 {x, y}, #2 {w, h}}`` -> (x, y, w, h)."""
    if fields is None:
        return None
    origin = _point(_msg(fields, 1))
    size = _point(_msg(fields, 2))
    if origin is None or size is None:
        return None
    return (origin[0], origin[1], size[0], size[1])


def _rect_is_nan(fields: Optional[Sequence[Field]]) -> bool:
    """True when any coordinate of a rect is NaN (erased shape fills store a NaN bbox)."""
    if fields is None:
        return False
    for sub in (_msg(fields, 1), _msg(fields, 2)):
        if _has_nan(sub, 1) or _has_nan(sub, 2):
            return True
    return False


def _uuid_key(u: str) -> str:
    """Events are keyed by the exact (upper-case) page UUID ``P``."""
    return u.upper()


def _prefix_key(u: str) -> str:
    """Fallback key for hand-made files: the first 31 hex digits (everything but the last)."""
    return u.upper()[:35]


def page_uuid_of_notes(n: str) -> str:
    """Notes-layer UUID ``N`` -> page UUID ``P = N - 1`` as a 128-bit integer (carry-aware).

    GoodNotes derives ``N`` by incrementing ``P`` as a whole number, so ``...E44F`` becomes
    ``...E450`` and a page UUID ending in ``F`` is only found this way.
    """
    try:
        v = (int(n.replace("-", ""), 16) - 1) % (1 << 128)
    except ValueError:
        return n.upper()  # hand-made file with a non-UUID key: only the prefix match can bind it
    h = "%032X" % v
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# --------------------------------------------------------------------------- event log model


@dataclass
class _Template:
    uuid: str
    attachment: Optional[str] = None
    pdf_page: int = 1  # 1-based
    canvas: Optional[XY] = None
    name: str = ""
    lined: bool = False  # #18.#3 present: the paper PDF draws rules / a grid / dots


@dataclass
class _PageEvent:
    uuid: str
    template: Optional[str] = None
    order_key: Optional[str] = None


@dataclass
class _Events:
    title: Optional[str] = None
    templates: Dict[str, _Template] = field(default_factory=dict)
    pages: Dict[str, _PageEvent] = field(default_factory=dict)  # keyed by _uuid_key(P)
    deleted: set = field(default_factory=set)  # _uuid_key(P)
    attachment_sizes: Dict[str, int] = field(default_factory=dict)
    aliases: Dict[str, str] = field(default_factory=dict)  # attachment id -> storage id (#6)
    _prefixes: Optional[Dict[str, _PageEvent]] = None  # lazily built fallback index

    def page_for_notes(self, n: str) -> Optional[_PageEvent]:
        """The ``#54``/``#3``/``#55`` state of the page whose notes layer is ``n``.

        Exact match on ``P = N - 1`` first; the 31-hex-digit prefix match (what hand-made files
        and the reference parsers rely on) only as a fallback.
        """
        ev = self.pages.get(_uuid_key(page_uuid_of_notes(n)))
        if ev is None:
            if self._prefixes is None:
                self._prefixes = {}
                for p, page in self.pages.items():
                    self._prefixes.setdefault(_prefix_key(p), page)
            ev = self._prefixes.get(_prefix_key(n))
        return ev

    def is_deleted(self, n: str) -> bool:
        if _uuid_key(page_uuid_of_notes(n)) in self.deleted:
            return True
        return any(_prefix_key(d) == _prefix_key(n) for d in self.deleted)


def _parse_events(data: bytes, doc: Document) -> _Events:
    ev = _Events()
    try:
        records = protobuf.decode_records(data)
    except ValueError:
        doc.warn("index.events.pb is truncated; page order and paper bindings may be incomplete")
        records = _salvage_records(data)
    for rec in records:
        fields = _decode(rec)
        if not fields:
            continue
        for f in fields:
            if f.number == 1 or f.wire_type != protobuf.WIRE_LEN:
                continue
            body = _decode(bytes(f.value))
            if body is None:
                continue
            kind = f.number
            if kind in (30, 31):
                name = _msg(body, 2)
                title = _str(name, 1) if name else None
                if title is not None:
                    ev.title = title
            elif kind == 2:
                t = _uuid(body, 2)
                if t is None:
                    continue
                tmpl = _Template(uuid=t, attachment=_uuid(body, 4), pdf_page=_int(body, 5) or 1,
                                 name=_str(body, 9) or "")
                size = _msg(body, 8)
                if size is not None:
                    w, h = _f32(size, 1), _f32(size, 2)
                    if w > 0 and h > 0:
                        tmpl.canvas = (w, h)
                # #7 / #18.#2 are the writing-guide spacing GoodNotes attaches even to blank
                # "White" papers; only #18.#3 coincides with a PDF that draws lines (binding doc 8.5)
                layout = _msg(body, 18)
                tmpl.lined = layout is not None and protobuf.get(layout, 3) is not None
                ev.templates[t] = tmpl
            elif kind in (54, 3):
                # 54 = page created, 3 = page re-bound to another paper; the later event wins
                p = _uuid(body, 2)
                if p is None:
                    continue
                page = ev.pages.setdefault(_uuid_key(p), _PageEvent(uuid=p))
                tref = _msg(body, 3)
                if tref is not None:
                    page.template = _uuid(tref, 1) or page.template
                if kind == 54:
                    key = _msg(body, 4)
                    if key is not None and _str(key, 1) is not None:
                        page.order_key = _str(key, 1)
            elif kind == 55:
                p = _uuid(body, 2)
                key = _msg(body, 3)
                if p is not None and key is not None and _str(key, 1) is not None:
                    ev.pages.setdefault(_uuid_key(p), _PageEvent(uuid=p)).order_key = _str(key, 1)
            elif kind == 56:
                p = _uuid(body, 2)
                if p is not None:
                    ev.deleted.add(_uuid_key(p))
            elif kind == 6:
                a = _uuid(body, 1)
                storage = _uuid(body, 2)
                size = _int(body, 5)
                if a is not None and size is not None:
                    ev.attachment_sizes[a] = size
                if a is not None and storage is not None and storage != a:
                    ev.aliases[a] = storage
            break
    return ev


def _salvage_records(data: bytes) -> List[bytes]:
    """Read as many leading records as possible from a damaged stream (at most MAX_RECORDS)."""
    out: List[bytes] = []
    pos = 0
    try:
        while pos < len(data) and len(out) < protobuf.MAX_RECORDS:
            length, pos = protobuf.read_varint(data, pos)
            if pos + length > len(data):
                break
            out.append(data[pos:pos + length])
            pos += length
    except ValueError:
        pass
    return out


def _index_pairs(data: bytes, prefix: str, doc: Document, what: str) -> List[Tuple[str, str]]:
    """``index.notes.pb`` / ``index.attachments.pb`` -> [(uuid, member name)] in file order."""
    try:
        records = protobuf.decode_records(data)
    except ValueError:
        doc.warn(f"{what} is truncated; reading what could be decoded")
        records = _salvage_records(data)
    out: List[Tuple[str, str]] = []
    for rec in records:
        fields = _decode(rec)
        if not fields:
            continue
        u = _uuid(fields, 1)
        member = _str(fields, 2)
        if member is None and u is not None:
            member = prefix + u
        if u is None and member is not None and member.startswith(prefix):
            u = member[len(prefix):].upper()
        if u is not None and member is not None:
            out.append((u, member))
    return out


# --------------------------------------------------------------------------- paper classification


def _paper_style(pdf: bytes, lined_hint: bool) -> str:
    """Guess plain / lined / grid / dotted from a built-in template's content streams.

    Rules are thin rectangles, drawn either as ``m l l l h f`` paths (GoodNotes' svg2pdf
    catalogue) or as ``x y w h re`` operators (papers generated by :func:`pdfutil.make_paper_pdf`).
    """
    horizontal = vertical = curves = 0
    for m in _STREAM_RE.finditer(pdf):
        raw = m.group(1)
        try:
            text = zlib.decompressobj().decompress(raw.strip(b"\r\n"), MAX_MEMBER_BYTES)
        except zlib.error:
            text = raw
        curves += len(re.findall(rb"\bc\b", text))
        sizes: List[XY] = []
        for rm in _RECT_PATH_RE.finditer(text):
            try:
                xs = [float(rm.group(i)) for i in (1, 3, 5, 7)]
                ys = [float(rm.group(i)) for i in (2, 4, 6, 8)]
            except ValueError:
                continue
            sizes.append((max(xs) - min(xs), max(ys) - min(ys)))
        for rm in _RECT_OP_RE.finditer(text):
            try:
                sizes.append((abs(float(rm.group(3))), abs(float(rm.group(4)))))
            except ValueError:
                continue
        for w, h in sizes:
            if 0 < h <= 2.0 and w > 10 * h:
                horizontal += 1
            elif 0 < w <= 2.0 and h > 10 * w:
                vertical += 1
    if curves >= 40:
        return "dotted"
    if horizontal >= 3 and vertical >= 3:
        return "grid"
    if horizontal >= 3 or (lined_hint and horizontal == 0 and vertical == 0 and curves == 0):
        return "lined"
    return "plain"


def _straight_controls(points: Sequence[Point]) -> List[Tuple[Point, Point]]:
    """Cubic handles at the thirds of every segment: the exact Bezier form of a polyline."""
    out: List[Tuple[Point, Point]] = []
    for p, q in zip(points, points[1:]):
        dx, dy = q.x - p.x, q.y - p.y
        out.append((Point(p.x + dx / 3.0, p.y + dy / 3.0, p.width),
                    Point(p.x + 2.0 * dx / 3.0, p.y + 2.0 * dy / 3.0, q.width)))
    return out


# --------------------------------------------------------------------------- image helpers


def _sniff_image(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data.lstrip()[:5] == b"%PDF-":
        return "pdf"
    return None


def jpeg_exif_orientation(data: bytes) -> int:
    """EXIF ``Orientation`` (1..8) of a JPEG; 1 when there is no readable APP1 EXIF segment.

    The marker segments before the first scan are walked; the ``Exif\\0\\0`` APP1 payload is a
    TIFF header (``II`` / ``MM`` byte order, 0x2A, IFD0 offset) whose IFD0 entries are
    ``(tag u16, type u16, count u32, value u32)``; tag 0x0112 is the orientation (SHORT).
    """
    try:
        if data[:2] != b"\xff\xd8":
            return 1
        pos = 2
        n = len(data)
        while pos + 4 <= n:
            if data[pos] != 0xFF:
                return 1
            marker = data[pos + 1]
            if marker == 0xFF:  # fill byte
                pos += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # stand-alone markers
                pos += 2
                continue
            if marker in (0xDA, 0xD9):  # start of scan / end of image: no EXIF before it
                return 1
            length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            if length < 2:
                return 1
            segment = data[pos + 4:pos + 2 + length]
            if marker == 0xE1 and segment[:6] == b"Exif\x00\x00":
                return _tiff_orientation(segment[6:])
            pos += 2 + length
    except (struct.error, IndexError, ValueError):
        pass
    return 1


def _tiff_orientation(tiff: bytes) -> int:
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return 1
    if struct.unpack(endian + "H", tiff[2:4])[0] != 42:
        return 1
    ifd = struct.unpack(endian + "I", tiff[4:8])[0]
    count = struct.unpack(endian + "H", tiff[ifd:ifd + 2])[0]
    for i in range(count):
        entry = ifd + 2 + 12 * i
        tag, typ, _n = struct.unpack(endian + "HHI", tiff[entry:entry + 8])
        if tag == 0x0112:
            if typ == 3:
                value = struct.unpack(endian + "H", tiff[entry + 8:entry + 10])[0]
            elif typ == 4:
                value = struct.unpack(endian + "I", tiff[entry + 8:entry + 12])[0]
            else:
                return 1
            return value if 1 <= value <= 8 else 1
    return 1


# EXIF orientation -> (clockwise rotation of the raw pixels in degrees, mirrored)
_EXIF_ROTATION = {1: (0.0, False), 2: (0.0, True), 3: (180.0, False), 4: (180.0, True),
                  5: (270.0, True), 6: (90.0, False), 7: (90.0, True), 8: (270.0, False)}


# --------------------------------------------------------------------------- page assembly


class _Reader:
    def __init__(self, data: bytes):
        self.doc = Document(source_format="goodnotes")
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, NotImplementedError, OSError, ValueError) as exc:
            # zipfile raises NotImplementedError for unsupported ZIP features / compression
            raise ValueError(f"not a .goodnotes file (not a readable ZIP archive: {exc})") from exc
        self.names = set(self.zip.namelist())
        self._budget = MAX_TOTAL_BYTES
        self.attachments: Dict[str, str] = {}  # attachment UUID -> member name
        self.aliases: Dict[str, str] = {}  # attachment id -> storage id (from #6 events)
        self._pdf_cache: Dict[str, Optional[object]] = {}
        self._att_cache: Dict[str, Optional[bytes]] = {}
        self._pencil_warned = False

    # -- members -----------------------------------------------------------------

    def member(self, name: str) -> Optional[bytes]:
        if name not in self.names:
            return None
        try:
            declared = self.zip.getinfo(name).file_size
        except KeyError:
            return None
        # Decompression-bomb guard: the central directory declares the inflated size, so a
        # member is refused before a single byte is inflated.
        if declared > MAX_MEMBER_BYTES:
            self.doc.warn(f"ZIP member {name} declares {declared} bytes, above the "
                          f"{MAX_MEMBER_BYTES // (1024 * 1024)} MB limit, and was skipped")
            return None
        if declared > self._budget:
            self.doc.warn(f"ZIP member {name} was skipped: the archive inflates to more than "
                          f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB in total")
            return None
        self._budget -= declared
        try:
            return self.zip.read(name)
        except (zipfile.BadZipFile, zlib.error, KeyError, RuntimeError, EOFError, OSError,
                MemoryError):
            self.doc.warn(f"ZIP member {name} is damaged and was skipped")
            return None

    def attachment_member(self, uuid: str) -> str:
        """``index.attachments.pb`` entry, else the ``#6`` storage alias, else ``attachments/<id>``."""
        member = self.attachments.get(uuid)
        if member is None:
            alias = self.aliases.get(uuid)
            if alias is not None:
                member = self.attachments.get(alias, "attachments/" + alias)
            else:
                member = "attachments/" + uuid
        return member

    def attachment(self, uuid: str) -> Optional[bytes]:
        if uuid not in self._att_cache:
            self._att_cache[uuid] = self.member(self.attachment_member(uuid))
        return self._att_cache[uuid]

    def pdf(self, uuid: str):
        if uuid not in self._pdf_cache:
            info = None
            data = self.attachment(uuid)
            if data is not None and data.lstrip()[:5] == b"%PDF-":
                try:
                    info = pdf_info(data)
                except (ValueError, TypeError) as exc:
                    self.doc.warn(f"attachment {uuid} is not a readable PDF ({exc})")
                else:
                    for w in info.warnings:
                        self.doc.warn(f"attachment {uuid}: {w}")
            self._pdf_cache[uuid] = info
        return self._pdf_cache[uuid]

    # -- document ----------------------------------------------------------------

    def read(self) -> Document:
        doc = self.doc
        notes_index = self.member("index.notes.pb")
        attachments_index = self.member("index.attachments.pb")
        events_data = self.member("index.events.pb")
        if notes_index is None and not any(n.startswith("notes/") for n in self.names):
            raise ValueError("not a .goodnotes file (no index.notes.pb and no notes/ members)")

        if attachments_index is not None:
            for u, member in _index_pairs(attachments_index, "attachments/", doc, "index.attachments.pb"):
                self.attachments[u] = member
        for name in sorted(self.names):
            if name.startswith("attachments/"):
                self.attachments.setdefault(name[len("attachments/"):].upper(), name)

        if events_data is not None:
            events = _parse_events(events_data, doc)
        else:
            events = _Events()
            doc.warn("index.events.pb is missing; page order, paper and canvas size are guessed")
        self.aliases = dict(events.aliases)
        doc.title = events.title or "Untitled"
        for a, size in events.attachment_sizes.items():
            member = self.attachment_member(a)
            if member in self.names:
                try:
                    actual = self.zip.getinfo(member).file_size
                except KeyError:
                    continue
                if actual != size:
                    doc.warn(f"attachment {a} has {actual} bytes but its event says {size}")

        pages = self._page_list(notes_index, events)
        for n_uuid, member in pages:
            page = self._build_page(n_uuid, member, events)
            doc.pages.append(page)
        if not doc.pages:
            doc.warn("the notebook has no pages")
        return doc

    def _page_list(self, notes_index: Optional[bytes], events: _Events) -> List[Tuple[str, str]]:
        entries: List[Tuple[str, str]] = []
        if notes_index is not None:
            entries = _index_pairs(notes_index, "notes/", self.doc, "index.notes.pb")
        if not entries:
            entries = [(n[len("notes/"):].upper(), n) for n in sorted(self.names) if n.startswith("notes/")]
            if entries and notes_index is not None:
                self.doc.warn("index.notes.pb lists no pages; using the notes/ members in name order")
        kept: List[Tuple[str, str]] = []
        for u, member in entries:
            if events.is_deleted(u):
                continue
            kept.append((u, member))
        # Display order = bytewise order of the #54/#55 keys (index.notes.pb order is arbitrary);
        # only pages without any key keep their index position, after the keyed ones.
        keyed = []
        for i, (u, member) in enumerate(kept):
            ev = events.page_for_notes(u)
            key = ev.order_key if ev is not None else None
            keyed.append(((0, key.encode("utf-8", "replace"), i) if key is not None else (1, b"", i), (u, member)))
        keyed.sort(key=lambda item: item[0])
        return [entry for _k, entry in keyed]

    def _build_page(self, n_uuid: str, member: str, events: _Events) -> Page:
        doc = self.doc
        ev = events.page_for_notes(n_uuid)
        tmpl = events.templates.get(ev.template) if ev is not None and ev.template else None
        width, height = DEFAULT_PAGE_SIZE
        background: Optional[PdfBackground] = None
        builtin = False
        paper = "plain"
        canvas: Optional[XY] = tmpl.canvas if tmpl is not None else None
        label = f"page {n_uuid[:8]}"

        if tmpl is None:
            doc.warn(f"{label}: no template event; page size and paper are guessed")
        elif tmpl.attachment is None:
            doc.warn(f"{label}: template {tmpl.uuid[:8]} names no attachment; page size guessed")
        else:
            info = self.pdf(tmpl.attachment)
            data = self.attachment(tmpl.attachment)
            if info is None:
                if data is None:
                    doc.warn(f"{label}: paper attachment {tmpl.attachment[:8]} is missing; page size guessed")
                else:
                    doc.warn(f"{label}: paper attachment {tmpl.attachment[:8]} is not a PDF; page size guessed")
            else:
                idx = tmpl.pdf_page - 1
                if idx < 0 or idx >= len(info.pages):
                    doc.warn(f"{label}: PDF page {tmpl.pdf_page} does not exist in attachment "
                             f"{tmpl.attachment[:8]} ({len(info.pages)} pages); using the last page")
                    idx = max(0, min(len(info.pages) - 1, idx))
                pg = info.pages[idx]
                width, height = pg.width, pg.height
                doc.pdfs.setdefault(tmpl.attachment, data)
                background = PdfBackground(tmpl.attachment, idx)
                # GoodNotes' own papers are 1-page svg2pdf files with a catalogue name; papers
                # generated by gnnote's writer (producer "gnnote") are stock paper too, so a
                # notebook written by gnnote reads back with plain pages instead of PDF-backed ones.
                builtin = (len(info.pages) == 1 and info.producer in BUILTIN_PRODUCERS
                           and info.creator in ("", "gnnote")
                           and bool(BUILTIN_TEMPLATE_RE.match(tmpl.name)))
                if builtin:
                    paper = _paper_style(data, tmpl.lined)
        if canvas is None and tmpl is not None and tmpl.attachment is not None and background is not None:
            doc.warn(f"{label}: template event carries no canvas size; assuming 132/72 units per point")
        if background is None and canvas is not None:
            width, height = canvas[0] / CANVAS_PER_POINT, canvas[1] / CANVAS_PER_POINT
        scale = 1.0 / CANVAS_PER_POINT
        if canvas is not None and canvas[0] > 0:
            scale = width / canvas[0]
            if canvas[1] > 0 and abs(height / canvas[1] - scale) > 0.01 * scale:
                doc.warn(f"{label}: canvas aspect differs from the paper PDF; using the horizontal scale")

        page = Page(width=width, height=height, background=background, paper=paper,
                    template_is_builtin=builtin)
        if member not in self.names:
            doc.warn(f"{label}: ink layer {member} is missing from the archive; page read as empty")
        content = self.member(member)
        if content:
            _PageParser(self, page, content, scale, label).run()
        return page


# --------------------------------------------------------------------------- page content


@dataclass
class _Meta:
    uuid: str
    tombstone: bool = False
    attachment: Optional[str] = None


@dataclass
class _Fill:
    parent: str  # UUID of the outline stroke
    stroke: Stroke
    order: int  # position in the record stream


class _PageParser:
    def __init__(self, reader: _Reader, page: Page, data: bytes, scale: float, label: str):
        self.reader = reader
        self.doc = reader.doc
        self.page = page
        self.data = data
        self.scale = scale
        self.label = label
        self.pending: Optional[_Meta] = None
        self.unknown_kinds: set = set()
        self.stroke_end: Dict[str, int] = {}  # element UUID -> index after its last stroke
        self.fills: List[_Fill] = []

    def run(self) -> None:
        try:
            records = protobuf.decode_records(self.data)
        except ValueError:
            self.doc.warn(f"{self.label}: ink layer is truncated; reading what could be decoded")
            records = _salvage_records(self.data)
        for rec in records:
            fields = _decode(rec)
            if not fields:
                continue
            meta = self._as_metadata(fields)
            if meta is not None:
                self.pending = meta
                continue
            if len(fields) != 1 or fields[0].wire_type != protobuf.WIRE_LEN:
                self.doc.warn(f"{self.label}: unrecognised record skipped")
                continue
            kind = fields[0].number
            body = _decode(bytes(fields[0].value))
            meta = self.pending
            self.pending = None
            if body is None:
                self.doc.warn(f"{self.label}: element record #{kind} could not be decoded")
                continue
            if meta is not None and meta.tombstone:
                continue
            try:
                if kind == 7:
                    self._stroke(body, meta)
                elif kind == 9:
                    self._fill(body, meta)
                elif kind == 1:
                    self._image(body, meta)
                elif kind == 8:
                    self._text(body)
                elif kind == 21:
                    self._text35(body)
                else:
                    if kind not in self.unknown_kinds:
                        self.unknown_kinds.add(kind)
                        self.doc.warn(f"{self.label}: unsupported element kind #{kind} skipped")
            except Exception as exc:  # noqa: BLE001 - tolerant reader: never fail on one element
                self.doc.warn(f"{self.label}: element #{kind} skipped ({exc.__class__.__name__}: {exc})")
        self._place_fills()

    @staticmethod
    def _as_metadata(fields: Sequence[Field]) -> Optional[_Meta]:
        u = _uuid(fields, 1)
        if u is None:
            return None
        if not any(f.number in (8, 9, 16) and f.wire_type == protobuf.WIRE_VARINT for f in fields):
            return None
        return _Meta(uuid=u, tombstone=_int(fields, 3) == 1, attachment=_uuid(fields, 4))

    # -- geometry helpers --------------------------------------------------------

    def _pt(self, x: float, y: float, width_pt: float, offset: XY) -> Point:
        s = self.scale
        return Point((x + offset[0]) * s, (y + offset[1]) * s, width_pt)

    def _add_stroke(self, points: List[Point], controls: Optional[List[Tuple[Point, Point]]],
                    color: RGBA, kind: str, pen: Optional[str], width: float,
                    outline: Optional[List[List[Point]]] = None) -> None:
        if not points:
            return
        if controls is not None and len(controls) != len(points) - 1:
            controls = None
        coords = [v for p in points for v in (p.x, p.y, p.width)]
        if controls:
            coords += [v for c1, c2 in controls for v in (c1.x, c1.y, c2.x, c2.y)]
        if not all(math.isfinite(v) for v in coords):
            self.doc.warn(f"{self.label}: stroke with non-finite coordinates skipped")
            return
        if width <= 0:
            width = 0.5  # a visible hairline instead of an invisible stroke
            for p in points:
                p.width = width
        self.page.strokes.append(Stroke(points=points, color=color, kind=kind, pen=pen,
                                        width=width, controls=controls, outline=outline))

    # -- strokes -----------------------------------------------------------------

    def _stroke(self, body: Sequence[Field], meta: Optional[_Meta]) -> None:
        uuid = _uuid(body, 1) or (meta.uuid if meta is not None else None)
        before = len(self.page.strokes)
        self._stroke_body(body)
        if uuid is not None and len(self.page.strokes) > before:
            self.stroke_end[uuid] = len(self.page.strokes)

    def _stroke_body(self, body: Sequence[Field]) -> None:
        color = self._color(_msg(body, 4))
        kind = "highlighter" if _int(body, 5) == 1 else "pen"
        tool = _int(body, 3)
        pen = "ballpoint" if tool is None else ("fountain" if tool in (1, 4) else ("pencil" if tool == 5 else None))
        marker = _msg(body, 20)
        if marker and protobuf.get(marker, 1) is not None:
            pen = "marker"
        offset = _point(_msg(body, 6)) or (0.0, 0.0)

        shape = _msg(body, 9)
        if shape and self._shape(shape, color, kind, pen, offset):
            return

        frame_field = protobuf.get(body, 2)
        if frame_field is None or frame_field.wire_type != protobuf.WIRE_LEN:
            self.doc.warn(f"{self.label}: stroke without geometry skipped")
            return
        frame = bytes(frame_field.value)
        if not frame:
            return
        if not applelz4.is_apple_lz4(frame):
            self.doc.warn(f"{self.label}: stroke geometry is not an Apple LZ4 frame; stroke skipped")
            return
        try:
            geo = tpl.decode(applelz4.decompress(frame))
        except ValueError as exc:
            self.doc.warn(f"{self.label}: stroke geometry could not be decoded ({exc}); stroke skipped")
            return
        if geo is None:
            return  # erased element: no points
        if isinstance(geo, tpl.FlatStroke):
            self._flat(geo, color, kind, pen, offset)
        elif isinstance(geo, tpl.RibbonStroke):
            if pen != "marker" and any(flag in (4, 5) for flag in geo.flags):
                pen = "marker"  # constant-radius band with elliptical caps (strokes doc section 6)
            self._ribbon(geo, color, kind, pen or "fountain", offset)
        elif isinstance(geo, tpl.PencilStroke):
            if not self.reader._pencil_warned:
                self.reader._pencil_warned = True
                self.doc.warn("pencil strokes are approximated: constant width, tilt data dropped")
            self._pencil(geo, color, kind, "pencil", offset)

    def _flat(self, geo: tpl.FlatStroke, color: RGBA, kind: str, pen: Optional[str], offset: XY) -> None:
        width_pt = geo.width / 2.0
        try:
            subpaths = geo.subpaths()
        except ValueError as exc:
            self.doc.warn(f"{self.label}: flat stroke flags are inconsistent ({exc}); stroke skipped")
            return
        for start, quads in subpaths:
            points = [self._pt(start[0], start[1], width_pt, offset)]
            if not quads:
                self._add_stroke(points, None, color, kind, pen, width_pt)
                continue
            controls: List[Tuple[Point, Point]] = []
            px, py = start
            for cx, cy, ex, ey in quads:
                c1, c2 = _elevate((px, py), (cx, cy), (ex, ey))
                controls.append((self._pt(c1[0], c1[1], width_pt, offset), self._pt(c2[0], c2[1], width_pt, offset)))
                points.append(self._pt(ex, ey, width_pt, offset))
                px, py = ex, ey
            self._add_stroke(points, controls, color, kind, pen, width_pt)

    def _ribbon(self, geo: tpl.RibbonStroke, color: RGBA, kind: str, pen: Optional[str], offset: XY) -> None:
        subpaths = geo.subpaths or ([geo.points] if geo.points else [])
        for sub in subpaths:
            points = [self._pt(x, y, 2.0 * r * self.scale, offset) for x, y, r in sub]
            if not points:
                continue
            width = statistics.median(p.width for p in points)
            self._add_stroke(points, None, color, kind, pen, width)

    def _pencil(self, geo: tpl.PencilStroke, color: RGBA, kind: str, pen: str, offset: XY) -> None:
        width_pt = PENCIL_WIDTH_FACTOR * geo.width
        subpaths = geo.subpaths or ([geo.points] if geo.points else [])
        for sub in subpaths:
            points = [self._pt(x, y, width_pt, offset) for x, y in sub]
            self._add_stroke(points, None, color, kind, pen, width_pt)

    @staticmethod
    def _color(fields: Optional[Sequence[Field]], default: RGBA = (0.0, 0.0, 0.0, 1.0)) -> RGBA:
        if fields is None:
            return default
        comps = [max(0.0, min(1.0, _f32(fields, i))) for i in (1, 2, 3, 4)]
        return (comps[0], comps[1], comps[2], comps[3])

    # -- auto-shapes (#7.#9) and shape fills (#9) ------------------------------

    @staticmethod
    def _shape_geometry(shape: Sequence[Field]) -> Optional[Tuple[str, List[XY]]]:
        """``#9`` / fill ``#4`` geometry -> (form, canvas points).

        ``ellipse`` / ``rect`` / ``polygon`` give the closed or open point list to draw;
        ``quadratic`` gives ``[P0, C, P1]`` of one quadratic Bezier (control point ``C``).
        """
        ellipse = _msg(shape, 4)
        if ellipse is not None:
            centre = _point(_msg(ellipse, 1))
            radii = _point(_msg(ellipse, 2))
            if centre is None or radii is None:
                return None
            theta = _f32(ellipse, 3)
            ct, st = math.cos(theta), math.sin(theta)
            pts: List[XY] = []
            for i in range(ELLIPSE_SAMPLES):
                t = 2.0 * math.pi * i / ELLIPSE_SAMPLES
                ex, ey = radii[0] * math.cos(t), radii[1] * math.sin(t)
                pts.append((centre[0] + ex * ct - ey * st, centre[1] + ex * st + ey * ct))
            pts.append(pts[0])  # closed exactly (no rounding gap at 2 pi)
            return "ellipse", pts
        rect = _msg(shape, 3)
        if rect is not None:
            centre = _point(_msg(rect, 1))
            size = _point(_msg(rect, 2))
            if centre is None or size is None:
                return None
            hw, hh = size[0] / 2.0, size[1] / 2.0
            cx, cy = centre
            return "rect", [(cx - hw, cy - hh), (cx + hw, cy - hh), (cx + hw, cy + hh), (cx - hw, cy + hh), (cx - hw, cy - hh)]
        curve = _msg(shape, 2)
        if curve is not None:
            p0, c, p1 = _point(_msg(curve, 1)), _point(_msg(curve, 2)), _point(_msg(curve, 3))
            if p0 is not None and c is not None and p1 is not None:
                return "quadratic", [p0, c, p1]
            # tolerate a repeated-#1 point list inside #2 (older guess of the layout)
            pts = _point_list(curve)
            return ("polygon", pts) if pts else None
        line = _msg(shape, 1)
        if line is not None:
            pts = _point_list(line)
            return ("polygon", pts) if pts else None
        return None

    def _shape(self, shape: Sequence[Field], color: RGBA, kind: str, pen: Optional[str], offset: XY) -> bool:
        """Emit the recognised shape as a stroke; False when ``#9`` holds no geometry."""
        geometry = self._shape_geometry(shape)
        if geometry is None:
            return False
        form, pts = geometry
        width_pt = _f32(shape, 15) / 2.0
        if width_pt <= 0:
            width_pt = 1.0
        if form == "quadratic":
            p0, c, p1 = pts
            c1, c2 = _elevate(p0, c, p1)
            points = [self._pt(p0[0], p0[1], width_pt, offset), self._pt(p1[0], p1[1], width_pt, offset)]
            controls = [(self._pt(c1[0], c1[1], width_pt, offset), self._pt(c2[0], c2[1], width_pt, offset))]
            self._add_stroke(points, controls, color, kind, pen, width_pt)
            return True
        points = [self._pt(x, y, width_pt, offset) for x, y in pts]
        controls = None
        if form in ("rect", "polygon") and len(points) >= 2:
            # Straight sides: exact cubic handles at the thirds, so the Notability writer does
            # not round the corners / bow the sides with its Catmull-Rom fit (design.md 4.1).
            controls = _straight_controls(points)
        self._add_stroke(points, controls, color, kind, pen, width_pt)
        return True

    def _fill(self, body: Sequence[Field], meta: Optional[_Meta]) -> None:
        """Top-level ``#9``: the translucent fill of a closed auto-shape (kept for ``_place_fills``)."""
        if _int(body, 14) == 1 or _rect_is_nan(_msg(body, 2)):
            return  # erased together with its outline
        parent = _uuid(body, 5)
        geometry_msg = _msg(body, 4)
        geometry = self._shape_geometry(geometry_msg) if geometry_msg is not None else None
        if parent is None or geometry is None:
            self.doc.warn(f"{self.label}: shape fill without geometry or outline reference skipped")
            return
        form, pts = geometry
        if form == "quadratic":
            p0, c, p1 = pts
            pts = [_quadratic_at(p0, c, p1, i / QUADRATIC_FILL_SAMPLES) for i in range(QUADRATIC_FILL_SAMPLES + 1)]
        if len(pts) < 2:
            return
        if pts[0] != pts[-1]:
            pts = pts + [pts[0]]
        offset = _point(_msg(body, 6)) or (0.0, 0.0)
        color = self._color(_msg(body, 7))
        polygon = [self._pt(x, y, 0.0, offset) for x, y in pts]
        if not all(math.isfinite(v) for p in polygon for v in (p.x, p.y)):
            self.doc.warn(f"{self.label}: shape fill with non-finite coordinates skipped")
            return
        stroke = Stroke(points=list(polygon), color=color, kind="fill", pen=None, width=0.0,
                        controls=None, outline=[polygon])
        self.fills.append(_Fill(parent=parent, stroke=stroke, order=len(self.fills)))

    def _place_fills(self) -> None:
        """Insert every kept fill right after the last stroke of its outline element."""
        placed = []
        for fill in self.fills:
            pos = self.stroke_end.get(fill.parent)
            if pos is None:
                self.doc.warn(f"{self.label}: shape fill whose outline stroke is missing was dropped")
                continue
            placed.append((pos, fill.order, fill.stroke))
        # highest position first so earlier indices stay valid; equal positions in stream order
        for pos, _order, stroke in sorted(placed, key=lambda item: (item[0], item[1]), reverse=True):
            self.page.strokes.insert(pos, stroke)

    # -- images ------------------------------------------------------------------

    def _image(self, body: Sequence[Field], meta: Optional[_Meta]) -> None:
        rect = _rect(_msg(body, 2))
        if rect is None:
            self.doc.warn(f"{self.label}: image without a placement rectangle skipped")
            return
        attachment = _uuid(body, 4) or (meta.attachment if meta is not None else None)
        data = self.reader.attachment(attachment) if attachment else None
        if data is None:
            self.doc.warn(f"{self.label}: image attachment {attachment[:8] if attachment else '?'} is missing; image skipped")
            return
        fmt = _sniff_image(data)
        if fmt is None:
            self.doc.warn(f"{self.label}: image attachment {attachment[:8]} is neither PNG, JPEG nor PDF; image skipped")
            return
        rotation = 0.0
        if fmt == "jpeg":
            # GoodNotes shows the photo with its EXIF orientation applied and stores the
            # displayed size in #2; the raw pixels must be turned to fill the box.
            orientation = jpeg_exif_orientation(data)
            rotation, mirrored = _EXIF_ROTATION.get(orientation, (0.0, False))
            if mirrored:
                self.doc.warn(f"{self.label}: EXIF orientation {orientation} mirrors the photo; only its rotation is applied")
        crop_fields = _msg(body, 3)
        crop = _rect(crop_fields)
        if crop is not None:
            if abs(crop[2] - rect[2]) > 0.01 or abs(crop[3] - rect[3]) > 0.01:
                self.doc.warn(f"{self.label}: image crop rectangle ignored (whole image shown)")
            if crop_fields is not None and protobuf.get(crop_fields, 3) is not None:
                rad = _f32(crop_fields, 3)
                if abs(rad) > 1e-6:
                    rotation = (rotation + math.degrees(rad)) % 360.0
                    self.doc.warn(f"{self.label}: image rotation of {math.degrees(rad):.1f} degrees applied about the box centre (unverified)")
        s = self.scale
        self.page.images.append(Image(x=rect[0] * s, y=rect[1] * s, w=rect[2] * s, h=rect[3] * s,
                                      data=data, fmt=fmt, rotation=rotation))

    # -- text boxes --------------------------------------------------------------

    def _text(self, body: Sequence[Field]) -> None:
        outer = _rect(_msg(body, 2))
        inner = _rect(_msg(body, 3))
        padding = _f32(body, 10, DEFAULT_TEXT_INSET)
        if inner is None and outer is not None:
            inner = (outer[0] + padding, outer[1] + padding, max(0.0, outer[2] - 2 * padding),
                     max(0.0, outer[3] - 2 * padding))
        if inner is None:
            self.doc.warn(f"{self.label}: text box without a frame skipped")
            return
        rtf = _bytes(body, 6) or b""
        text, runs = parse_rtf(rtf)
        s = self.scale
        for run in runs:
            if run.size is not None:
                run.size = run.size * s
        size = next((r.size for r in runs if r.size is not None), None)
        if size is None:
            size = DEFAULT_TEXT_SIZE * s  # GoodNotes' default \fs48
        color = next((r.color for r in runs if r.color is not None), None) or (0.0, 0.0, 0.0, 1.0)
        if not runs and text:
            runs = [TextRun(text, size=size)]
        self.page.texts.append(TextBox(x=inner[0] * s, y=inner[1] * s, w=inner[2] * s, h=inner[3] * s,
                                       text=text, runs=runs, color=color, size=size))

    def _text35(self, body: Sequence[Field]) -> None:
        """Schema-35 text element (``#21``): LZ4-framed runs inside ``#32.#1.#2``."""
        transform = _msg(body, 20)
        origin = _point(_msg(transform, 1))
        if origin is None:
            self.doc.warn(f"{self.label}: text element without a position skipped")
            return
        theta = _f32(transform, 2)
        box_scale = _f32(transform, 3, 1.0)
        if box_scale <= 0:
            box_scale = 1.0
        content = _msg(body, 32)
        if content is None:
            self.doc.warn(f"{self.label}: text element without content skipped")
            return
        inset_msg = _msg(content, 10)
        insets = [_f32(inset_msg, i, DEFAULT_TEXT_INSET) for i in (1, 2, 3, 4)]
        size_msg = _msg(body, 21)
        box = _point(_msg(size_msg, 2))
        if box is None:
            content_size = _point(_msg(content, 2))
            if content_size is not None:  # auto-sized box: content + insets
                box = (content_size[0] + insets[0] + insets[2], content_size[1] + insets[1] + insets[3])
            else:
                box = (insets[0] + insets[2], insets[1] + insets[3])
        default_style = _msg(_msg(content, 5), 1)
        default_font = _str(default_style, 30) or DEFAULT_TEXT_FONT
        default_size = _f32(default_style, 40)
        if default_size <= 0:
            default_size = DEFAULT_TEXT_SIZE
        default_color = self._text_color(_msg(default_style, 3), (0.0, 0.0, 0.0, 1.0))

        blob = _bytes(_msg(content, 1), 2)
        if blob is None:
            self.doc.warn(f"{self.label}: text element without text runs skipped")
            return
        if applelz4.is_apple_lz4(blob):
            try:
                blob = applelz4.decompress(blob)
            except ValueError as exc:
                self.doc.warn(f"{self.label}: text runs could not be decompressed ({exc}); text skipped")
                return
        runs_msg = _decode(blob)
        if runs_msg is None:
            self.doc.warn(f"{self.label}: text runs could not be decoded; text skipped")
            return
        k = self.scale
        runs: List[TextRun] = []
        align = "left"
        for f in protobuf.get_all(runs_msg, 1):
            if f.wire_type != protobuf.WIRE_LEN:
                continue
            run = _decode(bytes(f.value))
            if run is None:
                continue
            text = _str(run, 1) or ""
            style = _msg(run, 2)
            size = _f32(style, 40)
            if size <= 0:  # -404 (inherit) or absent
                size = default_size
            family = _str(style, 30) or default_font
            color = self._text_color(_msg(style, 3), default_color)
            # #60 is an unexplained style value: -60 by default, -30 on runs GoodNotes exports in
            # the bold face of the family (sticker letters); treated as a weight hint. Low confidence.
            bold = _signed(style, 60) == -30
            paragraph = _msg(run, 3)
            if _int(paragraph, 4) == 2:
                align = "center"
            runs.append(TextRun(text, bold=bold, font=family, size=size * k * box_scale, color=color))
        text = "".join(r.text for r in runs)
        size_pt = runs[0].size if runs and runs[0].size else default_size * k * box_scale
        color = runs[0].color if runs and runs[0].color else default_color
        width = max(0.0, box[0] - insets[0] - insets[2]) * k * box_scale
        height = max(0.0, box[1] - insets[1] - insets[3]) * k * box_scale
        self.page.texts.append(TextBox(x=(origin[0] + insets[0]) * k, y=(origin[1] + insets[1]) * k,
                                       w=width, h=height, text=text, runs=runs, color=color,
                                       size=size_pt, rotation=math.degrees(theta), align=align))

    @staticmethod
    def _text_color(fields: Optional[Sequence[Field]], default: RGBA) -> RGBA:
        """``{#1 R, #2 G, #3 B, #4 A}``; a message carrying only an alpha means "default"."""
        if fields is None or all(protobuf.get(fields, i) is None for i in (1, 2, 3)):
            return default
        comps = [max(0.0, min(1.0, _f32(fields, i, 1.0 if i == 4 else 0.0))) for i in (1, 2, 3, 4)]
        return (comps[0], comps[1], comps[2], comps[3])


def _point_list(container: Sequence[Field]) -> List[XY]:
    """Repeated ``{#1 x, #2 y}`` messages (any field number) -> points."""
    pts: List[XY] = []
    for f in container:
        if f.wire_type != protobuf.WIRE_LEN:
            continue
        p = _point(_decode(bytes(f.value)))
        if p is not None:
            pts.append(p)
    return pts


def _elevate(p0: XY, c: XY, p1: XY) -> Tuple[XY, XY]:
    """Quadratic Bezier (P0, C, P1) -> the cubic handles of the identical curve."""
    c1 = (p0[0] + 2.0 / 3.0 * (c[0] - p0[0]), p0[1] + 2.0 / 3.0 * (c[1] - p0[1]))
    c2 = (p1[0] + 2.0 / 3.0 * (c[0] - p1[0]), p1[1] + 2.0 / 3.0 * (c[1] - p1[1]))
    return c1, c2


def _quadratic_at(p0: XY, c: XY, p1: XY, t: float) -> XY:
    u = 1.0 - t
    return (u * u * p0[0] + 2.0 * u * t * c[0] + t * t * p1[0],
            u * u * p0[1] + 2.0 * u * t * c[1] + t * t * p1[1])


# --------------------------------------------------------------------------- public API


def read_goodnotes(data: bytes) -> Document:
    """Parse a ``.goodnotes`` file into a :class:`Document`.

    Raises ``ValueError`` only when ``data`` is not a GoodNotes container at all (not a ZIP,
    or a ZIP without a page index and without ``notes/`` members).  Everything else that is
    damaged or unknown is skipped with a line on ``Document.warnings``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_goodnotes expects bytes")
    return _Reader(bytes(data)).read()
