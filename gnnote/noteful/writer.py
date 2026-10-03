"""Model -> ``.noteful`` writer.

The file is synthesised from scratch in the generation notesconverter's author imported on
iPads and Macs (Noteful 1.4.25 and 1.4.33): format version 1.18, object version 280, with
the record shapes and entry order of the app's own files (``docs/noteful.md``).

Container
    ``AA BB CC DE``, then the blobs in the app's order: ``n:<N>`` (header), the thumbnail
    JPEG, ``d:<N>`` (pages, one layer "Layer 1", empty bookmarks), the other embedded files
    (PDFs, paper, pictures), one annotation record per page with content; then the root
    record (format version, notebook UUID, file UUIDs, annotation UUIDs, blob names, offsets,
    lengths) and the trailer ``AA BB CC DE 00000000 u32 root offset, u32 root length``.

Identifiers and time
    UUIDs are 32 upper-case hex digits; stroke ids are 8 random bytes, unique per file.
    Both come from ``random.Random(options.random_seed)``.  Every timestamp is "now" in
    microseconds since 2001 plus a running counter, so z keys grow in drawing order; with a
    ``random_seed`` the clock is fixed (``options.timestamp``, Unix seconds, or
    :data:`FIXED_TIME`) and the output is byte-for-byte reproducible.

Pages
    A page with a :class:`~gnnote.model.PdfBackground` becomes a PDF page (background type 1,
    0-based page index); each ``pdf_id`` is stored once.  Every other page gets a one-page
    paper PDF from :func:`gnnote.pdfutil.make_paper_pdf` (pages of one size and style share
    it).  Page size in units = pt x 132/72.  Pages sort by ordering tags generated in
    ASCII order.

Content, in drawing order (images, then strokes and fills in model order, then text boxes)
    * ink: Bezier strokes are flattened (:func:`gnnote.geometry.flatten_bezier`, 1 pt), then
      written as ``F1 01`` stroke records (radius = width / 2 x 132/72; per-point radii when
      the widths vary) after an ``F1 02`` style record whenever colour or tool change; a
      highlighter is blend 1 with alpha 1.0 (the app adds the 50 % opacity itself).
    * ``kind == "fill"``: a filled polygon object (type 12) per outline ring, coded like the
      fill of the app's filled ellipse (fill record, a zero-width outline).
    * text boxes: type-2 objects with rich text (runs, font family, size, bold, italic,
      underline, colour, alignment); the box gains the (5, 2)-unit inset and its rotation
      pivot moves from the text frame's top-left corner to the box centre.
    * images: PNG and JPEG become type-1 objects listed in the page's files-on-page list
      (identical pictures share one file); PDF images have no Noteful equivalent and are
      dropped with a warning.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
import struct
import time
import uuid as _uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import pdfutil
from ..geometry import flatten_bezier
from ..goodnotes.constants import THUMBNAIL_JPEG
from ..model import RGBA, Document, Image, Page, Stroke, TextBox, TextRun
from ..notability.writer import image_pixel_size, jpeg_exif_orientation, sanitise_text
from . import (
    A4_UNITS, APPLE_EPOCH, BACKGROUND_PDF, BLEND_MULTIPLY, BLEND_NORMAL, FORMAT_VERSION, INK_STROKE,
    INK_STYLE, MAGIC, OBJ_IMAGE, OBJ_POLYGON, OBJ_TEXT, OBJECT_VERSION, TEXT_INSET, UNITS_PER_POINT,
)
from . import ttv
from .ttv import BOOL, BYTES, DATE, F32, F64, I32, LIST, RECORD, SIZE, STAMPED, STRING, U16, U32, U64, U64_ALT

__all__ = ["write_noteful", "FIXED_TIME", "order_tag", "font_family"]

U = UNITS_PER_POINT
FIXED_TIME = 1767225600.0  # 2026-01-01T00:00:00Z: the clock of reproducible output
FLATTEN_SPACING = 1.0  # pt between the points of a flattened Bezier stroke
MAX_PAGE_SIDE_PT = 1e6
MAX_F32 = 3.0e38
SPAN_EPSILON = 9.999999747378752e-06  # float32(1e-5): the app's span of a zero-extent dimension
HAIRLINE = 0.5  # pt; the width of a stroke whose width is zero or unknown
MIN_LINE_PITCH = 1.2  # font sizes per line a text box is at least given (the app's export: 1.24 .. 1.33)
FILL_BACKGROUND_EXTRA = 0.5  # 0x0003 of the app's (filled, outline-less) ellipse; meaning unknown
DEFAULT_FONT = "Helvetica"
TAG_ALPHABET = "+/0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"  # ASCII order
TAG_START = 2 ** 34  # "+E+++++", the first ordering tag notesconverter writes
TAG_STRIDE = 100_000_000
_ALIGN = {"center": 1, "centre": 1, "right": 2}
_TEXT_KEY_ORDER = (1, 10, 2, 3, 4, 6, 8, 12, 9)  # the app's key order inside one chunk
_TEXT_DEFAULTS: Dict[int, Any] = {1: DEFAULT_FONT, 2: False, 3: False, 4: 0, 6: (0.0, 0.0, 0.0, 1.0), 8: 0}
_TEXT_LIST = {1: "str", 2: "bool", 3: "bool", 4: "int", 6: "color", 8: "int", 9: "record", 10: "float", 12: "float"}

_COUNTED = {
    "stroke_bad": "{n} strokes with coordinates that are not finite or too large were skipped",
    "fill_bad": "{n} shape fills without a usable outline were skipped",
    "text_empty": "{n} empty text boxes were skipped",
    "text_bad": "{n} text boxes with an invalid position or size were skipped",
    "text_runs": "{n} text boxes had runs that did not cover their text; their formatting was flattened",
    "image_pdf": "{n} PDF images (vector stickers) were dropped (Noteful pictures must be PNG or JPEG)",
    "image_format": "{n} images that are neither PNG nor JPEG were dropped",
    "image_bad": "{n} images without data or with an invalid position were skipped",
    "image_exif": "{n} photos carry an EXIF orientation; whether Noteful applies it on top of the "
                  "written rotation is unverified",
    "pdf_stretch": "{n} pages differ in shape from their PDF page; Noteful stretches the PDF to the page",
}


def _opt(options: Any, name: str, default: Any) -> Any:
    value = getattr(options, name, None) if options is not None else None
    return default if value is None else value


def order_tag(index: int, count: int) -> str:
    """Ordering tag of page ``index`` (0-based) out of ``count``: 7 characters whose ASCII
    order is the page order."""
    stride = max(1, min(TAG_STRIDE, (64 ** 7 - 1 - TAG_START) // (count + 1)))
    value = TAG_START + (index + 1) * stride
    chars = []
    for _ in range(7):
        value, digit = divmod(value, 64)
        chars.append(TAG_ALPHABET[digit])
    return "".join(reversed(chars))


def font_family(name: Optional[str]) -> str:
    """A run's font as the family name Noteful stores ("HelveticaNeue-Bold" -> "Helvetica Neue";
    bold and italic travel as flags)."""
    if not name or not name.strip():
        return DEFAULT_FONT
    base = name.strip()
    if " " not in base:
        base = base.split("-")[0] if "-" in base[1:] else base
        for suffix in ("PSMT", "MT", "PS"):
            if base.endswith(suffix) and len(base) > len(suffix) and base[-len(suffix) - 1].islower():
                base = base[:-len(suffix)]
                break
        base = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)
    return base or DEFAULT_FONT


def _f32(value: float) -> float:
    return struct.unpack(">f", struct.pack(">f", value))[0]


def _clamp_rgba(color: Sequence[float]) -> RGBA:
    comps = [float(c) for c in tuple(color)[:4]] + [1.0] * (4 - min(4, len(tuple(color))))
    r, g, b, a = (min(1.0, max(0.0, c)) if math.isfinite(c) else (1.0 if i == 3 else 0.0)
                  for i, c in enumerate(comps))
    return (r, g, b, a)


def _sniff(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data.lstrip()[:5] == b"%PDF-":
        return "pdf"
    return None


def _rgba_record(color: Sequence[float]) -> bytes:
    return ttv.encode([(0x0000, LIST | F32, list(color))])


def _u64_record(value: int) -> bytes:
    return ttv.encode([(0x0000, U64, value)])


def _collection(keys: Sequence[Any], values: Sequence[bytes], stamps: Sequence[int], key_type: int = STRING) -> bytes:
    """A keyed collection: keys, stamps, present flags, values (pages, layers, objects)."""
    return ttv.encode([(0x0001, LIST | key_type, list(keys)), (0x0002, LIST | U64, list(stamps)),
                       (0x0003, LIST | BOOL, [True] * len(keys)), (0x0000, LIST | RECORD, list(values))])


class _Ids:
    """Random identifiers and the clock of one output file."""

    def __init__(self, seed: Optional[int], now: float):
        self.rng = random.Random(seed)
        self.base = max(0, int(round((now - APPLE_EPOCH) * 1e6)))
        self.counter = 0
        self.stroke_ids: set = set()

    def uuid(self) -> str:
        return _uuid.UUID(int=self.rng.getrandbits(128), version=4).hex.upper()

    def stroke_id(self) -> Tuple[int, int, int]:
        while True:
            v = self.rng.getrandbits(64)
            if v and v not in self.stroke_ids:
                self.stroke_ids.add(v)
                return v >> 48, (v >> 16) & 0xFFFFFFFF, v & 0xFFFF

    def tick(self) -> int:
        """A fresh timestamp (microseconds since 2001), larger than every earlier one."""
        self.counter += 1
        return self.base + self.counter


class _Writer:
    def __init__(self, doc: Document, options: Any):
        self.doc = doc
        seed = _opt(options, "random_seed", None)
        clock = _opt(options, "timestamp", None)
        if clock is None:
            clock = FIXED_TIME if seed is not None else time.time()
        self.ids = _Ids(seed, float(clock))
        self.title = sanitise_text(str(_opt(options, "title", None) or doc.title or "Untitled"))
        self.files: List[Tuple[str, bytes]] = []  # embedded files in blob order
        self.pdf_files: Dict[str, str] = {}  # model pdf_id -> file UUID
        self.paper_files: Dict[Tuple[float, float, str], str] = {}
        self.image_files: Dict[str, str] = {}  # sha1 of the picture -> file UUID
        self.pdf_infos: Dict[str, Optional[pdfutil.PdfInfo]] = {}
        self.annotations: List[Tuple[str, bytes]] = []
        self.counts: Dict[str, int] = {}

    # -- bookkeeping ------------------------------------------------------------------------

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    def count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def add_file(self, data: bytes) -> str:
        name = self.ids.uuid()
        self.files.append((name, bytes(data)))
        return name

    # -- pages and backgrounds --------------------------------------------------------------

    def _page_size(self, page: Page, number: int) -> Tuple[float, float]:
        w, h = page.width, page.height
        if isinstance(w, (int, float)) and isinstance(h, (int, float)) and math.isfinite(w) and math.isfinite(h) \
                and 0 < w <= MAX_PAGE_SIDE_PT and 0 < h <= MAX_PAGE_SIDE_PT:
            return float(w), float(h)
        self.warn(f"Page {number} has no valid size; A4 was used")
        return A4_UNITS[0] / U, A4_UNITS[1] / U

    def _pdf_info(self, pdf_id: str, data: bytes) -> Optional[pdfutil.PdfInfo]:
        if pdf_id not in self.pdf_infos:
            info: Optional[pdfutil.PdfInfo] = None
            try:
                info = pdfutil.pdf_info(data)
            except Exception as exc:  # noqa: BLE001 - a broken PDF only costs its pages' backgrounds
                self.warn(f"PDF {pdf_id!r} could not be read ({exc}); paper was generated instead")
            self.pdf_infos[pdf_id] = info
        return self.pdf_infos[pdf_id]

    def _background(self, page: Page, number: int, w: float, h: float) -> Tuple[str, int]:
        """(file UUID, 0-based page index) of the PDF page shown behind ``page``."""
        bg = page.background
        if bg is not None:
            data = self.doc.pdfs.get(bg.pdf_id)
            info = self._pdf_info(bg.pdf_id, data) if data else None
            if not data:
                self.warn(f"Page {number}: its PDF background {bg.pdf_id!r} is missing; paper was generated instead")
            elif info is not None:
                index = int(bg.page_index)
                if 0 <= index < len(info.pages):
                    name = self.pdf_files.get(bg.pdf_id)
                    if name is None:
                        name = self.pdf_files[bg.pdf_id] = self.add_file(data)
                    pdf_page = info.pages[index]
                    if pdf_page.width > 0 and pdf_page.height > 0 and \
                            abs(pdf_page.width / pdf_page.height - w / h) > 0.01 * (w / h):
                        self.count("pdf_stretch")
                    return name, index
                self.warn(f"Page {number}: PDF page {index + 1} does not exist in {bg.pdf_id!r}; "
                          "paper was generated instead")
        style = page.paper if page.paper in pdfutil.PAPER_STYLES else ("lined" if page.paper == "ruled" else "plain")
        if page.paper not in pdfutil.PAPER_STYLES and page.paper not in ("ruled", "", None):
            self.warn(f"Unknown paper style {page.paper!r}; plain paper was used")
        key = (round(w, 3), round(h, 3), style)
        name = self.paper_files.get(key)
        if name is None:
            name = self.paper_files[key] = self.add_file(pdfutil.make_paper_pdf(w, h, style))
        return name, 0

    # -- objects ----------------------------------------------------------------------------

    def _object(self, data: bytes, box: Sequence[float], z: int, flip: int = 0) -> Tuple[str, bytes]:
        ids = self.ids
        name = ids.uuid()
        box_entries: List[Tuple[Any, ...]] = [(0x0001, LIST | F64, list(box))]
        if flip:
            box_entries.append((0x0002, I32, flip))
        rec = ttv.encode([
            (0x0001, STRING, name),
            (0x0002, RECORD | STAMPED, ttv.encode(box_entries), ids.tick()),
            (0x0004, U32 | STAMPED, 0, ids.tick()),
            (0x0005, U64 | STAMPED, z, ids.tick()),
            (0x0006, RECORD, data),
            (0x0007, STRING | STAMPED, "", ids.tick()),
            (0x0008, F64 | STAMPED, 1.0, ids.tick()),
            (0x0009, U16 | STAMPED, 0, ids.tick()),
            (0x000a, U64, ids.base),
        ])
        return name, rec

    def _image(self, image: Image, files_on_page: List[str]) -> Optional[Tuple[str, bytes]]:
        data = bytes(image.data or b"")
        fmt = _sniff(data) if data else None
        if not data:
            self.count("image_bad")
            return None
        if fmt == "pdf" or (fmt is None and (image.fmt or "").lower() == "pdf"):
            self.count("image_pdf")
            return None
        if fmt is None:
            self.count("image_format")
            return None
        x, y, w, h = (float(v) for v in (image.x, image.y, image.w, image.h))
        rotation = float(image.rotation or 0.0)
        if not all(math.isfinite(v) for v in (x, y, w, h, rotation)) or w <= 0 or h <= 0 \
                or max(abs(x), abs(y), w, h) > MAX_PAGE_SIDE_PT:
            self.count("image_bad")
            return None
        if fmt == "jpeg" and (jpeg_exif_orientation(data) or 1) != 1:
            self.count("image_exif")
        key = hashlib.sha1(data).hexdigest()
        name = self.image_files.get(key)
        if name is None:
            name = self.image_files[key] = self.add_file(data)
        if name not in files_on_page:
            files_on_page.append(name)
        pixels = image_pixel_size(data) or (max(1, int(round(w))), max(1, int(round(h))))
        bw, bh = w * U, h * U
        ts = self.ids.tick()
        body = ttv.encode([
            (0x0016, U64, OBJECT_VERSION),
            (0x0001, U64, OBJ_IMAGE),
            (0x0002, SIZE | STAMPED, (bw, bh), ts),
            (0x000a, STRING, name),
            (0x000b, SIZE, (float(pixels[0]), float(pixels[1]))),
            (0x0014, F64 | STAMPED, 0.0, ts),
        ])
        box = [(x + w / 2.0) * U, (y + h / 2.0) * U, bw, bh, math.radians(rotation)]
        return self._object(body, box, self.ids.tick())

    def _stroke_record(self, thickness: float, color: Optional[RGBA]) -> bytes:
        entries: List[Tuple[Any, ...]] = []
        if color is not None:
            entries.append((0x0007, RECORD, _rgba_record(color)))
        entries += [(0x0002, F64, thickness), (0x0003, RECORD, _u64_record(0)), (0x0004, I32, 1),
                    (0x0005, I32, 1), (0x0006, I32, 0), (0x0008, RECORD, _u64_record(0))]
        return ttv.encode(entries)

    def _fill(self, stroke: Stroke) -> List[Tuple[str, bytes]]:
        rings = [ring for ring in (stroke.outline or []) if ring] or ([stroke.points] if stroke.points else [])
        color = _clamp_rgba(stroke.color)
        out = []
        for ring in rings:
            pts: List[Tuple[float, float]] = []
            for p in ring:
                x, y = float(p.x) * U, float(p.y) * U
                if not (math.isfinite(x) and math.isfinite(y)) or max(abs(x), abs(y)) > MAX_PAGE_SIDE_PT * U:
                    pts = []
                    break
                if not pts or (x, y) != pts[-1]:
                    pts.append((x, y))
            if len(pts) >= 2 and pts[0] == pts[-1]:
                pts.pop()
            xs, ys = [x for x, _ in pts], [y for _, y in pts]
            if len(pts) < 3 or (max(xs) - min(xs) <= 0 and max(ys) - min(ys) <= 0):
                self.count("fill_bad")
                continue
            x0, y0 = min(xs), min(ys)
            w, h = max(xs) - x0, max(ys) - y0
            flat = [v for x, y in pts for v in (x - x0, y - y0)]
            commands = [0] + [1] * (len(pts) - 1) + [4]
            ts = self.ids.tick()
            background = ttv.encode([(0x0001, RECORD, _rgba_record(color)), (0x0002, BOOL, False),
                                     (0x0003, F64, FILL_BACKGROUND_EXTRA)])
            body = ttv.encode([
                (0x0016, U64, OBJECT_VERSION),
                (0x0001, U64, OBJ_POLYGON),
                (0x0002, SIZE | STAMPED, (w, h), ts),
                (0x0005, RECORD | STAMPED, background, ts),
                (0x0007, RECORD | STAMPED, self._stroke_record(0.0, None), ts),
                (0x000d, RECORD | STAMPED, ttv.encode([(0x0001, LIST | F64, flat), (0x0002, LIST | I32, commands)]),
                 ts),
            ])
            out.append(self._object(body, [x0 + w / 2.0, y0 + h / 2.0, w, h, 0.0], self.ids.tick()))
        if not rings:
            self.count("fill_bad")
        return out

    def _rich_text(self, runs: Sequence[TextRun], box: TextBox) -> bytes:
        strings: List[str] = []
        counts: List[int] = []
        keys: List[int] = []
        lists: Dict[str, List[Any]] = {"str": [], "bool": [], "color": [], "int": [], "record": [], "float": []}
        previous = dict(_TEXT_DEFAULTS)
        align = _ALIGN.get((box.align or "left").lower(), 0)
        default_size = box.size if isinstance(box.size, (int, float)) and math.isfinite(box.size) and box.size > 0 \
            else 12.0
        for run in runs:
            size = run.size if isinstance(run.size, (int, float)) and run.size and math.isfinite(run.size) \
                and run.size > 0 else default_size
            attrs: Dict[int, Any] = {
                1: font_family(run.font), 10: float(size) * U, 2: bool(run.bold), 3: bool(run.italic),
                4: 1 if run.underline else 0, 6: _clamp_rgba(run.color or box.color or (0.0, 0.0, 0.0, 1.0)),
                8: align,
            }
            if not strings:
                attrs[12] = 0.0  # additional line spacing and an always-empty record: first chunk only
                attrs[9] = ttv.encode([(0x0001, LIST | RECORD, [])])
            delta = [k for k in _TEXT_KEY_ORDER if k in attrs and (k == 10 or previous.get(k) != attrs[k])]
            strings.append(run.text)
            counts.append(len(delta))
            for k in delta:
                keys.append(k)
                value = attrs[k]
                lists[_TEXT_LIST[k]].append(_rgba_record(value) if k == 6 else value)
            previous.update(attrs)
        return ttv.encode([
            (0x0001, U64, self.ids.base),
            (0x0002, LIST | STRING, strings),
            (0x0003, LIST | U64, counts),
            (0x0004, LIST | I32, keys),
            (0x0005, LIST | STRING, lists["str"]),
            (0x0006, LIST | BOOL, lists["bool"]),
            (0x0007, LIST | RECORD, lists["color"]),
            (0x0008, LIST | U64, lists["int"]),
            (0x0009, LIST | RECORD, lists["record"]),
            (0x000a, LIST | F64, lists["float"]),
        ])

    def _text(self, box: TextBox) -> Optional[Tuple[str, bytes]]:
        text = sanitise_text(box.text or "")
        runs = [TextRun(sanitise_text(r.text), r.bold, r.italic, r.underline, r.font, r.size, r.color)
                for r in box.runs if r.text]
        if runs and "".join(r.text for r in runs) != text:
            self.count("text_runs")
            runs = []
        if not runs:
            runs = [TextRun(text, size=box.size, color=box.color)]
        if not text:
            self.count("text_empty")
            return None
        size = max([r.size for r in runs if isinstance(r.size, (int, float)) and r.size and r.size > 0]
                   + [box.size if isinstance(box.size, (int, float)) and box.size > 0 else 12.0])
        lines = text.split("\n")
        x, y = float(box.x), float(box.y)
        w = float(box.w) if box.w and box.w > 0 else max(len(line) for line in lines) * size * 0.6
        h = max(float(box.h) if box.h and box.h > 0 else 0.0, len(lines) * size * MIN_LINE_PITCH)
        rotation = float(box.rotation or 0.0)
        if not all(math.isfinite(v) for v in (x, y, w, h, size, rotation)) \
                or max(abs(x), abs(y), w, h) > MAX_PAGE_SIDE_PT:
            self.count("text_bad")
            return None
        iw, ih = max(w, 1.0) * U, h * U
        bw, bh = iw + 2.0 * TEXT_INSET[0], ih + 2.0 * TEXT_INSET[1]
        theta = math.radians(rotation)
        c, s = math.cos(theta), math.sin(theta)
        # the model turns the text frame about its top-left corner, Noteful the box about its centre
        cx = x * U + (iw / 2.0) * c - (ih / 2.0) * s
        cy = y * U + (iw / 2.0) * s + (ih / 2.0) * c
        ts = self.ids.tick()
        zero = _u64_record(0)
        body = ttv.encode([
            (0x0016, U64, OBJECT_VERSION),
            (0x0001, U64, OBJ_TEXT),
            (0x0002, SIZE | STAMPED, (bw, bh), ts),
            (0x0004, RECORD, self._rich_text(runs, box)),
            (0x0009, RECORD | STAMPED, zero, ts),
            (0x0013, RECORD | STAMPED, zero, ts),
            (0x0014, F64 | STAMPED, 0.0, ts),
            (0x0015, F64 | STAMPED, 0.0, ts),
        ])
        return self._object(body, [cx, cy, bw, bh, theta], self.ids.tick())

    # -- ink --------------------------------------------------------------------------------

    def _ink(self, stroke: Stroke, out: bytearray, style: List[Any]) -> None:
        points = list(stroke.points or [])
        if not points:
            return
        if stroke.controls is not None and len(points) >= 2 and len(stroke.controls) == len(points) - 1:
            points = flatten_bezier(points, stroke.controls, FLATTEN_SPACING)
        coords: List[Tuple[float, float, float]] = []
        for p in points:
            x, y = float(p.x) * U, float(p.y) * U
            width = float(p.width) if p.width is not None else 0.0
            r = max(0.0, width) / 2.0 * U if math.isfinite(width) else 0.0
            if not (math.isfinite(x) and math.isfinite(y)) or max(abs(x), abs(y), r) > MAX_F32:
                self.count("stroke_bad")
                return
            coords.append((x, y, r))
        if len(coords) == 1:
            coords.append(coords[0])  # a dot is two identical points, as the app writes it
        radii = [c[2] for c in coords]
        variable = max(radii) - min(radii) > 1e-6 * max(1.0, max(radii))
        nominal_pt = float(stroke.width) if stroke.width and math.isfinite(stroke.width) and stroke.width > 0 else 0.0
        if variable:
            nominal = nominal_pt / 2.0 * U or sorted(radii)[len(radii) // 2]
        else:
            nominal = radii[0] or nominal_pt / 2.0 * U or HAIRLINE / 2.0 * U
        if nominal <= 0 or nominal > MAX_F32:
            nominal = HAIRLINE / 2.0 * U
        highlighter = stroke.kind == "highlighter"
        r, g, b, a = _clamp_rgba(stroke.color)
        key = (r, g, b, 1.0 if highlighter else a, BLEND_MULTIPLY if highlighter else BLEND_NORMAL)
        if style[0] != key:
            out += struct.pack(">H4dHQ", INK_STYLE, *key, 0)
            style[0] = key
        ids = self.ids
        n = len(coords)
        dims = 3 if variable else 2
        id0, id1, id2 = ids.stroke_id()
        out += struct.pack(">HHIHH", INK_STROKE, id0, id1, id2, 1 if variable else 0)
        out += struct.pack(">QQQ", ids.base, ids.tick(), ids.tick())
        out += struct.pack(">IdII", 0, nominal, 0, n)
        if n <= 4:
            out += struct.pack(f">{n * dims}f", *(v for c in coords for v in c[:dims]))
            return
        quantised: List[List[int]] = []
        for k in range(dims):
            values = [c[k] for c in coords]
            lo = _f32(min(values))
            span = _f32(max(max(values) - lo, SPAN_EPSILON))
            out += struct.pack(">ff", lo, span)
            scale = 65535.0 / span
            quantised.append([min(65535, max(0, int(round((v - lo) * scale)))) for v in values])
        out += struct.pack(f">{n * dims}H", *(q[i] for i in range(n) for q in quantised))

    # -- annotation -------------------------------------------------------------------------

    def _content(self, page: Page, number: int) -> Tuple[str, List[str]]:
        """(annotation UUID or "", files on the page) of one page; records the annotation."""
        objects: List[Tuple[str, bytes]] = []
        files_on_page: List[str] = []
        for image in page.images:
            try:
                made = self._image(image, files_on_page)
            except (ValueError, TypeError, OverflowError) as exc:
                self.warn(f"Page {number}: an image was skipped ({exc})")
                continue
            if made is not None:
                objects.append(made)
        ink = bytearray()
        style: List[Any] = [None]
        for stroke in page.strokes:
            try:
                if stroke.kind == "fill":
                    objects += self._fill(stroke)
                else:
                    self._ink(stroke, ink, style)
            except (ValueError, TypeError, OverflowError) as exc:
                self.warn(f"Page {number}: a stroke was skipped ({exc})")
        for box in page.texts:
            try:
                made = self._text(box)
            except (ValueError, TypeError, OverflowError) as exc:
                self.warn(f"Page {number}: a text box was skipped ({exc})")
                continue
            if made is not None:
                objects.append(made)
        if not ink and not objects:
            return "", files_on_page
        name = self.ids.uuid()
        record = ttv.encode([
            (0x0001, U64, OBJECT_VERSION),
            (0x0002, BYTES, bytes(ink)),
            (0x0003, LIST | U64, []),
            (0x0004, LIST | U64, []),
            (0x0005, RECORD, _collection([k for k, _ in objects], [v for _, v in objects],
                                         [self.ids.tick() for _ in objects])),
        ])
        self.annotations.append((name, record))
        return name, files_on_page

    # -- notebook ---------------------------------------------------------------------------

    def build(self) -> bytes:
        ids = self.ids
        doc = self.doc
        meta = ids.uuid()
        thumbnail = self.add_file(THUMBNAIL_JPEG)
        pages = list(doc.pages)
        if not pages:
            self.warn("The document has no pages; one empty page was written")
            pages = [Page(A4_UNITS[0] / U, A4_UNITS[1] / U)]
        page_keys: List[str] = []
        page_values: List[bytes] = []
        page_stamps: List[int] = []
        first_link = ""
        for index, page in enumerate(pages):
            number = index + 1
            w, h = self._page_size(page, number)
            pdf_name, pdf_index = self._background(page, number, w, h)
            annotation, files_on_page = self._content(page, number)
            background = ttv.encode([
                (0x0000, U16, BACKGROUND_PDF),
                (0x0001, SIZE, (w * U, h * U)),
                (0x0002, BOOL, False),
                (0x0003, U64, pdf_index),
                (0x0004, STRING, pdf_name),
                (0x0005, U64, 0),
            ])
            resources = ttv.encode([(0x0000, STRING, annotation), (0x0002, LIST | STRING, files_on_page),
                                    (0x0003, LIST | STRING, [])])
            page_uuid, link = ids.uuid(), ids.uuid()
            first_link = first_link or link
            page_keys.append(page_uuid)
            page_stamps.append(ids.tick())
            page_values.append(ttv.encode([
                (0x0001, STRING, page_uuid),
                (0x0002, RECORD, resources),
                (0x0004, RECORD | STAMPED, background, ids.tick()),
                (0x0005, STRING | STAMPED, order_tag(index, len(pages)), ids.tick()),
                (0x0006, STRING, link),
                (0x0007, BOOL | STAMPED, False, ids.tick()),
                (0x0009, BOOL | STAMPED, False, ids.tick()),
            ]))
        for key, template in _COUNTED.items():
            if self.counts.get(key):
                self.warn(template.format(n=self.counts[key]))

        empty = _collection([], [], [])
        layer = ttv.encode([
            (0x0001, STRING | STAMPED, "Layer 1", ids.tick()),
            (0x0002, U32, 0),
            (0x0003, STRING | STAMPED, "0", ids.tick()),
            (0x0004, BOOL | STAMPED, False, 0),
            (0x0005, BOOL | STAMPED, False, 0),
            (0x0006, F32 | STAMPED, 1.0, ids.tick()),
            (0x0007, BOOL | STAMPED, False, ids.tick()),
        ])
        notebook = ttv.encode([
            (0x0001, STRING, meta),
            (0x0002, RECORD, _collection(page_keys, page_values, page_stamps)),
            (0x0003, RECORD, _collection([0], [layer], [ids.tick()], key_type=U32)),
            (0x0007, RECORD, empty),
            (0x0004, RECORD, empty),
            (0x0005, RECORD, empty),
            (0x0006, STRING, ids.uuid()),
            (0x0008, U32 | STAMPED, 0, ids.tick()),
            (0x0009, U64, 0),
            (0x000a, RECORD, empty),
        ])
        seconds = ids.base / 1e6
        header = ttv.encode([
            (0x0001, STRING, meta),
            (0x0002, U64, 0),
            (0x0003, STRING | STAMPED, self.title, ids.tick()),
            (0x0004, STRING | STAMPED, "", ids.tick()),
            (0x0005, DATE, seconds),
            (0x0006, RECORD, ttv.encode([(tag, BOOL | STAMPED, False, 0) for tag in range(1, 7)])),
            (0x0007, RECORD | STAMPED, ttv.encode([(0x0001, STRING, thumbnail), (0x0002, STRING, first_link)]),
             ids.tick()),
            (0x0008, STRING | STAMPED, "", ids.tick()),
            (0x0009, STRING | STAMPED, "", ids.tick()),
            (0x000a, STRING, ids.uuid()),
            (0x000b, RECORD | STAMPED, ttv.encode([(0x0000, U64, 0),
                                                   (0x0001, RECORD, _rgba_record((0.0, 0.0, 0.0, 0.0)))]),
             ids.tick()),
            (0x000c, DATE, seconds),
            (0x000d, U64_ALT, 0),
            (0x000e, U64, 0),
            (0x000f, STRING | STAMPED, "", ids.tick()),
        ])

        buf = bytearray(MAGIC)
        index: List[Tuple[str, int, int]] = []

        def put(name: str, body: bytes) -> None:
            index.append((name, len(buf), len(body)))
            buf.extend(body)

        put("n:" + meta, header)
        put(self.files[0][0], self.files[0][1])  # the thumbnail
        put("d:" + meta, notebook)
        for name, data in self.files[1:]:
            put(name, data)
        for name, data in self.annotations:
            put(name, data)
        entries: List[Tuple[Any, ...]] = [
            (0x0001, F32, FORMAT_VERSION),
            (0x0002, LIST | STRING, [meta]),
            (0x0003, LIST | STRING, [name for name, _ in self.files]),
        ]
        if self.annotations:
            entries.append((0x0004, LIST | STRING, [name for name, _ in self.annotations]))
        entries += [
            (0x000a, LIST | STRING, [name for name, _, _ in index]),
            (0x000b, LIST | U64, [start for _, start, _ in index]),
            (0x000c, LIST | U64, [length for _, _, length in index]),
        ]
        root = ttv.encode(entries)
        root_start = len(buf)
        if root_start + len(root) > 0xFFFFFFFF:
            raise ValueError("the notebook is too large for a Noteful file (4 GiB)")
        buf += root
        buf += MAGIC + b"\x00\x00\x00\x00" + struct.pack(">II", root_start, len(root))
        return bytes(buf)


def write_noteful(doc: Document, options: Any = None) -> bytes:
    """Serialise ``doc`` as a ``.noteful`` file (see the module docstring).

    ``options`` is duck-typed (:class:`gnnote.convert.Options` or anything with a ``title``);
    an extra ``random_seed`` attribute makes the output reproducible (with ``timestamp``,
    Unix seconds, as its clock).  Lossy steps are reported through ``doc.warn``.
    """
    return _Writer(doc, options).build()

