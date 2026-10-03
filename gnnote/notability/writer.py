"""Notability ``.note`` writer: ``write_note(doc, options) -> bytes``.

The output mirrors a note saved by Notability 10.4 (``sessionFormatVersion`` 5), the format
the project's one-page proof of concept proved current Notability opens and edits
(``docs/notability-format.md`` Parts 1-7, 9, 12).

ZIP container (deflated, member order irrelevant)::

    <Name>/Session.plist             GLKeyedArchiver object graph, root NoteTakingSession
    <Name>/metadata.plist            NSKeyedArchiver SessionInfo
    <Name>/Recordings/library.plist  XML plist {application version, library-format-version, recordings {}}
    <Name>/thumb.png thumb2x.png thumb3x.png thumb6x.png   white RGB PNGs 48x63 / 96x126 / 144x189 / 288x378
    <Name>/Assets/ Images/ PDFs/ HandwritingIndex/ Recordings/   empty directory entries
    <Name>/PDFs/<UUID>.pdf           one per distinct PDF background (PDF-backed pages only)
    <Name>/Images/Image .jpg, Image 1.png, ...                     one per image

Document coordinate system: one vertical strip of pages, y down, unit = document points,
page width ``W = pageWidthInDocumentCoordsKey``.  A plain page occupies ``H = 1.3125 * W``,
a PDF-backed page ``ceil(h * W / w)`` for a ``w x h`` pt PDF page; page ``i`` starts at the
sum of the slots before it.  Notability bottom-aligns the PDF content inside its rounded-up
slot (notesconverter, verified against the app's PDF export), so PDF-page content starts at
``slot_start + (ceil(h * s) - h * s)``.  Model coordinates (pt, origin top-left) map with
``x_doc = x_off + x * s``, ``y_doc = y_off + y * s`` where ``s = W / page_width`` and ``y_off``
includes that gap (plain pages whose height would overflow the slot are scaled uniformly to fit
and centred horizontally).

Ink (``richText['Handwriting Overlay'].SpatialHash``, class ``InkedSpatialHash``), all arrays
little-endian and exactly sized::

    curvesnumpoints         int32 per curve: n_i = 1 + 3 k_i (k_i cubic Bezier segments)
    curvespoints            float32 x, y per point: anchor, (c1, c2, anchor) x k_i, all curves concatenated
    curvesfractionalwidths  float32 per anchor (k_i + 1 per curve): width multiplier, clipped to [0.25, 4]
    curveswidth             float32 per curve: base width in document points (median anchor width)
    curvescolors            4 x uint8 RGBA per curve (highlighter alpha forced to 0x6b)
    curvesstyles            uint8 per curve: 3 pen, 4 highlighter
    eventTokens             int32 per curve, always -1 (no recording)
    numcurves / numpoints / numfractionalwidths   the three counts (UIDs to ints, as the template)

Pages: ``richText.pageLayoutArray`` is empty for all-plain notes; as soon as one page is
PDF-backed every page gets an entry in order: ``{kPageLayoutPDFPageNumberKey: INT64_MAX}``
for plain pages, ``{kPageLayoutPDFIsOriginalPageKey: True, kPageLayoutPDFPageNumberKey: 1-based,
kPageLayoutPDFFileKey: PDFFile}`` for PDF pages, with one ``PDFFile`` object per PDF file in
``richText.pdfFiles``.

Media: ``richText.mediaObjects`` holds ``ImageMediaObject`` (Figure -> ImageObject -> GLSnapshot
pointing at ``Images/...``) and ``TextBlockMediaObject`` (a nested ``FormattedString`` text
store with the attributed string) in the field layout of Notability 10.2.4 output.

What the model carries that Notability cannot hold is dropped with one counted warning each:
``Stroke.kind == "fill"`` (no filled shapes) and ``Image.fmt == "pdf"`` (vector stickers; images
must be PNG or JPEG).  ``Image.rotation`` and ``TextBox.rotation`` go to ``rotationDegrees``
(radians, clockwise, about the object's centre); a text box rotates about its top-left corner
in the model, so its origin is moved to where the centre-pivot rotation lands the same corner.
A JPEG with an EXIF orientation other than 1 is written byte for byte; whether Notability
applies that orientation on top of ``rotationDegrees`` is unverified (warning).
``TextBox.align`` becomes the text store's ``formattedStringTextAlignmentKey`` (0 left,
1 centre, 2 right).

Only the standard library is used.
"""
from __future__ import annotations

import dataclasses
import io
import math
import statistics
import struct
import time
import unicodedata
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import geometry
from ..model import Document, Image, Page, Point, Stroke, TextBox
from . import x_inset
from .archivebuilder import (
    INT64_MAX, ArchiveBuilder, color_string, point_string, range_string, rect_string, size_string,
)

__all__ = ["write_note", "LEGACY_ASPECT", "DEFAULT_PAGE_WIDTH", "white_png", "image_pixel_size",
           "jpeg_exif_orientation", "exif_rotation"]

LEGACY_ASPECT = 1.3125  # plain page height / width (803.25 / 612)
DEFAULT_PAGE_WIDTH = 574.0  # pageWidthInDocumentCoordsKey of the 10.4 template
BUNDLE_VERSION = "10.4"
APP_BUILD = "4631"  # Recordings/library.plist 'application version' of Notability 10.4
STYLE_PEN = 3
STYLE_HIGHLIGHTER = 4
HIGHLIGHTER_ALPHA = 0x6B
MIN_FRACTION, MAX_FRACTION = 0.25, 4.0
THUMB_SIZES = (("thumb.png", 48, 63), ("thumb2x.png", 96, 126), ("thumb3x.png", 144, 189), ("thumb6x.png", 288, 378))
TEXT_PAD_X, TEXT_PAD_Y = 5.0, 2.0  # text inset inside a Notability text box (doc units)
DEFAULT_FONT = "HelveticaNeue"
TEXT_ALIGNMENT = {"left": 0, "center": 1, "right": 2}  # formattedStringTextAlignmentKey


# ---------------------------------------------------------------------------------------
# options access

def _opt(options: Any, name: str, default: Any) -> Any:
    value = getattr(options, name, None) if options is not None else None
    return default if value is None else value


# ---------------------------------------------------------------------------------------
# page layout

@dataclass
class _Slot:
    page: Page
    kind: str  # "plain" | "pdf"
    y_offset: float  # document y of the page's content origin (slot start + top gap)
    height: float  # slot height occupied in the document
    scale: float
    x_offset: float = 0.0  # extra document x shift (centring of a scaled-down plain page)
    pdf_key: Optional[str] = None  # key into the PDF file table
    x0: float = 0.0  # document x of the paper's left edge (``x_inset(W)``, negative)

    def doc_x(self, x_pt: float) -> float:
        """Document x of page coordinate ``x_pt`` (pt)."""
        return self.x0 + self.x_offset + x_pt * self.scale
    pdf_page: int = 1  # 1-based page inside the PDF


def _pdf_page_size(data: bytes, index: int) -> Optional[Tuple[float, float]]:
    """``(w, h)`` pt of page ``index`` (0-based) via :mod:`gnnote.pdfutil`; ``None`` when unknown."""
    try:
        from .. import pdfutil
        info = pdfutil.pdf_info(data)
        pages = info.pages
    except Exception:  # noqa: BLE001 - tolerant: fall back to a plain page
        return None
    if not pages:
        return None
    if index < 0 or index >= len(pages):
        return None
    page = pages[index]
    if page.width <= 0 or page.height <= 0:
        return None
    return float(page.width), float(page.height)


def _layout(doc: Document, options: Any, width: float) -> Tuple[List[_Slot], Dict[str, bytes]]:
    """Assign every page a slot and collect the PDF files that must be written."""
    paper_mode = str(_opt(options, "paper", "plain"))
    plain_h = LEGACY_ASPECT * width
    slots: List[_Slot] = []
    pdf_files: Dict[str, bytes] = {}
    y = 0.0
    generated: Dict[Tuple[float, float, str], str] = {}
    for index, page in enumerate(doc.pages):
        page_w = float(page.width) if page.width and page.width > 0 else 612.0
        page_h = float(page.height) if page.height and page.height > 0 else page_w * LEGACY_ASPECT
        slot: Optional[_Slot] = None
        bg = page.background
        wants_pdf = bg is not None and (paper_mode == "pdf" or not page.template_is_builtin)
        if wants_pdf and bg is not None:
            data = doc.pdfs.get(bg.pdf_id)
            if data is None:
                doc.warn(f"Page {index + 1}: PDF background {bg.pdf_id!r} is missing; written as plain paper")
            else:
                size = _pdf_page_size(data, bg.page_index)
                if size is None:
                    doc.warn(f"Page {index + 1}: PDF background page could not be measured; written as plain paper")
                else:
                    w, h = size
                    scale = width / w
                    occupied = float(math.ceil(h * scale))
                    slot = _Slot(page, "pdf", y + (occupied - h * scale), occupied, scale,
                                 pdf_key=bg.pdf_id, pdf_page=bg.page_index + 1)
                    pdf_files.setdefault(bg.pdf_id, data)
        elif paper_mode == "pdf":
            # Every page PDF-backed, as requested: synthesise the paper for pages without one.
            style = page.paper if page.paper in ("plain", "lined", "grid", "dotted") else "plain"
            key = (round(page_w, 3), round(page_h, 3), style)
            pdf_id = generated.get(key)
            if pdf_id is None:
                try:
                    from .. import pdfutil
                    data = pdfutil.make_paper_pdf(page_w, page_h, style)
                except Exception:  # noqa: BLE001
                    data = None
                if data is not None:
                    pdf_id = f"paper-{len(generated) + 1}"
                    generated[key] = pdf_id
                    pdf_files[pdf_id] = data
            if pdf_id is not None:
                scale = width / page_w
                occupied = float(math.ceil(page_h * scale))
                slot = _Slot(page, "pdf", y + (occupied - page_h * scale), occupied, scale,
                             pdf_key=pdf_id, pdf_page=1)
            else:
                doc.warn(f"Page {index + 1}: paper PDF could not be generated; written as plain paper")
        if slot is None:
            scale = width / page_w
            x_off = 0.0
            if page_h * scale > plain_h + 1e-6:
                scale = plain_h / page_h
                x_off = (width - page_w * scale) / 2.0
                doc.warn("Pages taller than Notability's plain page were scaled down to fit (use paper=\"pdf\" to keep the size)")
            slot = _Slot(page, "plain", y, plain_h, scale, x_offset=x_off)
        slot.x0 = x_inset(width)
        slots.append(slot)
        y += slot.height
    return slots, pdf_files


# ---------------------------------------------------------------------------------------
# ink

def _curve_geometry(stroke: Stroke, slot: _Slot, tolerance: float, doc: Document
                    ) -> Optional[Tuple[List[Point], List[Tuple[Point, Point]]]]:
    """Anchors + controls in document coordinates, or ``None`` for an empty stroke."""
    pts = [p for p in stroke.points if p.x == p.x and p.y == p.y]  # drop NaN
    if not pts:
        return None
    x0, y0, x1, y1 = min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts)
    s = slot.scale

    def to_doc(p: Point) -> Point:
        return Point(slot.doc_x(p.x), slot.y_offset + p.y * s, max(0.0, p.width) * s)

    if x1 - x0 <= 1e-9 and y1 - y0 <= 1e-9:
        # Empty bbox: a dot, rendered as a tiny segment (0.5 pt dash in document units).
        anchors, controls = geometry.polyline_to_bezier([to_doc(pts[0])])
        return anchors, controls
    if stroke.controls is not None:
        if len(stroke.controls) == len(pts) - 1 and len(pts) >= 2:
            anchors = [to_doc(p) for p in pts]
            controls = [(to_doc(c1), to_doc(c2)) for c1, c2 in stroke.controls]
            return anchors, controls
        doc.warn("Some Bezier strokes had inconsistent control handles and were re-fitted")
    poly = geometry.dedupe(pts)
    if tolerance > 0:
        poly = geometry.simplify(poly, tolerance)
    anchors, controls = geometry.polyline_to_bezier([to_doc(p) for p in poly])
    return anchors, controls


def _page_has_content(page: Page) -> bool:
    return bool(page.strokes or page.images or page.texts)


def _fractional_base(base: float, widths: Sequence[float]) -> float:
    """``curveswidth`` for a stroke whose anchors have ``widths``.

    The median keeps most fractions near 1, but Notability clips ``fractionalwidths`` to
    ``[MIN_FRACTION, MAX_FRACTION]``; when a taper would be clipped (fountain-pen ends thinner
    than a quarter of the median) the base is moved so the whole range fits, which is exact
    as long as ``max / min <= MAX_FRACTION / MIN_FRACTION``.  The clip stays for wider ranges.
    """
    if not widths:
        return base
    lo, hi = min(widths), max(widths)
    if lo >= base * MIN_FRACTION and hi <= base * MAX_FRACTION:
        return base
    if hi / lo <= MAX_FRACTION / MIN_FRACTION:
        # the smallest base that keeps the range inside the clip, favouring fractions <= 1
        return max(hi / MAX_FRACTION, min(base, lo / MIN_FRACTION))
    return base


def _build_ink(doc: Document, slots: Sequence[_Slot], options: Any) -> Dict[str, Any]:
    pressure = bool(_opt(options, "pressure", True))
    tolerance = float(_opt(options, "simplify", 0.0) or 0.0)
    points: List[float] = []
    numpoints: List[int] = []
    widths: List[float] = []
    fractions: List[float] = []
    colors: List[bytes] = []
    styles: List[int] = []
    tokens: List[int] = []
    fills = 0
    for slot in slots:
        for stroke in slot.page.strokes:
            if stroke.kind == "fill":
                fills += 1
                continue
            geom = _curve_geometry(stroke, slot, tolerance, doc)
            if geom is None:
                doc.warn("Empty strokes were dropped")
                continue
            anchors, controls = geom
            if len(anchors) < 2 or len(controls) != len(anchors) - 1:
                doc.warn("Empty strokes were dropped")
                continue
            ws = [a.width for a in anchors]
            base = float(statistics.median(ws)) if ws else 0.0
            if base <= 1e-6:
                base = max(float(stroke.width) * slot.scale, 0.3) if stroke.width and stroke.width > 0 else 0.3
            if pressure:
                base = _fractional_base(base, [w for w in ws if w > 0])
                frac = [min(MAX_FRACTION, max(MIN_FRACTION, (w / base) if w > 0 else 1.0)) for w in ws]
            else:
                frac = [1.0] * len(ws)
            flat: List[float] = [anchors[0].x, anchors[0].y]
            for i, (c1, c2) in enumerate(controls):
                a = anchors[i + 1]
                flat += [c1.x, c1.y, c2.x, c2.y, a.x, a.y]
            n = len(flat) // 2
            assert n == 1 + 3 * len(controls)
            r, g, b, a = (stroke.color + (1.0,) * 4)[:4]
            rgba = [max(0, min(255, int(round(float(c) * 255)))) for c in (r, g, b, a)]
            if stroke.kind == "highlighter":
                rgba[3] = HIGHLIGHTER_ALPHA
                styles.append(STYLE_HIGHLIGHTER)
                frac = [1.0] * len(ws)
            else:
                styles.append(STYLE_PEN)
            points += flat
            numpoints.append(n)
            widths.append(base)
            fractions += frac
            colors.append(bytes(rgba))
            tokens.append(-1)
    if fills:
        doc.warn(f"{fills} shape fills dropped (Notability has no filled shapes)")
    return {
        "curvespoints": struct.pack(f"<{len(points)}f", *points),
        "curvesnumpoints": struct.pack(f"<{len(numpoints)}i", *numpoints),
        "curveswidth": struct.pack(f"<{len(widths)}f", *widths),
        "curvesfractionalwidths": struct.pack(f"<{len(fractions)}f", *fractions),
        "curvescolors": b"".join(colors),
        "curvesstyles": bytes(styles),
        "eventTokens": struct.pack(f"<{len(tokens)}i", *tokens),
        "numcurves": len(numpoints),
        "numpoints": len(points) // 2,
        "numfractionalwidths": len(fractions),
    }


# ---------------------------------------------------------------------------------------
# images

def image_pixel_size(data: bytes) -> Optional[Tuple[int, int]]:
    """Pixel size of a PNG or JPEG (``None`` when unknown)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return (w, h) if w and h else None
    if data[:2] == b"\xff\xd8":
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            if marker == 0xFF:
                pos += 1
                continue
            length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
                return (w, h) if w and h else None
            pos += 2 + length
    return None


def jpeg_exif_orientation(data: bytes) -> Optional[int]:
    """The EXIF ``Orientation`` tag (1..8) of a JPEG, ``None`` when absent or unreadable.

    Only the first APP1 segment with an ``Exif`` header is read (TIFF header, IFD0, tag
    0x0112).  6 means "rotate 90 degrees clockwise to display", 8 "90 counter-clockwise",
    3 "180"; see :func:`exif_rotation`.
    """
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xDA, 0xD9):  # start of scan / end of image: no EXIF before the pixels
            return None
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        segment = data[pos + 4:pos + 2 + length]
        if marker == 0xE1 and segment[:6] == b"Exif\x00\x00":
            tiff = segment[6:]
            if tiff[:4] == b"II*\x00":
                order = "<"
            elif tiff[:4] == b"MM\x00*":
                order = ">"
            else:
                return None
            if len(tiff) < 8:
                return None
            ifd = struct.unpack(order + "I", tiff[4:8])[0]
            if ifd + 2 > len(tiff):
                return None
            count = struct.unpack(order + "H", tiff[ifd:ifd + 2])[0]
            for i in range(count):
                entry = tiff[ifd + 2 + 12 * i:ifd + 14 + 12 * i]
                if len(entry) < 12:
                    return None
                tag, kind, n = struct.unpack(order + "HHI", entry[:8])
                if tag == 0x0112 and kind == 3 and n >= 1:
                    value = struct.unpack(order + "H", entry[8:10])[0]
                    return value if 1 <= value <= 8 else None
            return None
        pos += 2 + length
    return None


def exif_rotation(orientation: Optional[int]) -> Optional[float]:
    """Clockwise display rotation in degrees implied by an EXIF orientation (mirrored
    orientations 2, 4, 5 and 7 have no pure rotation and give ``None``)."""
    return {1: 0.0, 3: 180.0, 6: 90.0, 8: 270.0}.get(orientation or 0)


def _image_format(image: Image) -> str:
    data = image.data
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:5] == b"%PDF-":
        return "pdf"
    fmt = (image.fmt or "").lower()
    return "png" if fmt == "png" else "jpeg" if fmt in ("jpeg", "jpg") else "pdf" if fmt == "pdf" else "png"


# ---------------------------------------------------------------------------------------
# text box rotation


def text_origin_for_centre_pivot(origin: Tuple[float, float], size: Tuple[float, float],
                                 pivot: Tuple[float, float], theta: float) -> Tuple[float, float]:
    """Origin of a box rotated about its centre that lands where the same box rotated about
    ``pivot`` (box-local, usually the text frame's top-left corner) would.

    ``theta`` is in radians, positive clockwise in the y-down document plane (the standard
    rotation matrix, as both apps use).  Rotating about the pivot keeps the pivot in place; its
    centre then sits at ``pivot + R(theta) * (centre - pivot)`` and the centre-pivot box must
    be placed so its centre is there.
    """
    w, h = size
    px, py = pivot
    cx, cy = w / 2.0 - px, h / 2.0 - py  # centre relative to the pivot, unrotated
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    rx, ry = cx * cos_t - cy * sin_t, cx * sin_t + cy * cos_t
    centre = (origin[0] + px + rx, origin[1] + py + ry)
    return centre[0] - w / 2.0, centre[1] - h / 2.0


# ---------------------------------------------------------------------------------------
# thumbnails

def white_png(width: int, height: int) -> bytes:
    """An 8-bit RGB PNG filled with white (zlib-compressed, one filter byte per row)."""
    raw = b"".join(b"\x00" + b"\xff" * (3 * width) for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------------------
# Session.plist

class _SessionBuilder:
    def __init__(self, doc: Document, options: Any, width: float):
        self.doc = doc
        self.options = options
        self.width = width
        self.b = ArchiveBuilder("GLKeyedArchiver", "$0")
        self.empty_array: Optional[Any] = None  # shared empty NSArray, as the template
        self.z_index = 0
        self.image_files: List[Tuple[str, bytes]] = []
        self.pdf_names: Dict[str, str] = {}
        self.pdf_images = 0  # Image.fmt == "pdf" stickers dropped
        self.exif_rotated = 0  # rotated JPEGs that also carry an EXIF orientation

    # -- helpers ----------------------------------------------------------------------

    def shared_empty(self) -> Any:
        if self.empty_array is None:
            self.empty_array = self.b.array([])
        return self.empty_array

    def attributed(self, text: str, subranges: Sequence[Any], string_uid: Any) -> Any:
        return self.b.dictionary([
            ("stringKey", string_uid),
            ("subRangesKey", self.b.array(list(subranges), mutable=True)),
        ])

    def empty_hash(self) -> Any:
        return self.spatial_hash({
            "curvespoints": b"", "curvesnumpoints": b"", "curveswidth": b"",
            "curvesfractionalwidths": b"", "curvescolors": b"", "curvesstyles": b"",
            "eventTokens": b"", "numcurves": 0, "numpoints": 0, "numfractionalwidths": 0,
        })

    def spatial_hash(self, arrays: Dict[str, Any]) -> Any:
        b = self.b
        fields = {
            "numcurves": b.add(int(arrays["numcurves"])),
            "numfractionalwidths": b.add(int(arrays["numfractionalwidths"])),
            "groupsArrays": b.array([], mutable=True),
            "curvespoints": arrays["curvespoints"],
            "curvescolors": arrays["curvescolors"],
            "eventTokens": arrays["eventTokens"],
            "curvesnumpoints": arrays["curvesnumpoints"],
            "curvesfractionalwidths": arrays["curvesfractionalwidths"],
            "curveswidth": arrays["curveswidth"],
            "curvesstyles": arrays["curvesstyles"],
            "bezierPathsDataDictionary": b.dictionary([]),
            "numpoints": b.add(int(arrays["numpoints"])),
        }
        return b.object("InkedSpatialHash", fields)

    def formatted_string(self, hash_uid: Any, reflow: Any, text: str, subranges: Sequence[Any],
                         layout: Any, pdf_files: Any, media: Any, alignment: int = 0) -> Any:
        """A ``FormattedString`` with the template's full key set (``alignment``: 0/1/2)."""
        b = self.b
        length = len(text.encode("utf-16-le")) // 2
        timestamp_sub = b.dictionary([
            ("subRangeOtherAttributesKey", b.dictionary([])),
            ("subRangeRangeKey", b.string(range_string(0, length))),
        ]) if length else None
        backing = b.object("NBAttributedString", {
            "NBAttributedBackingStringCodingKey": self.attributed(text, subranges, b.mutable_string(text)),
            "NBAttributedLayoutStringCodingKey": self.attributed(text, subranges, b.mutable_string(text)),
        })
        return b.object("FormattedString", {
            "formatVersion": 4,
            "pageLayoutArray": layout,
            "formattedStringTextAlignmentKey": int(alignment),
            "attributedString": self.attributed(text, subranges, b.string(text)),
            "NBAttributedBackingString": backing,
            "Handwriting Overlay": b.object("HandwritingObject", {"SpatialHash": hash_uid}),
            "reflowState": reflow,
            "didBecomeReflowable": True,
            "pdfFiles": pdf_files,
            "mediaObjects": media,
            "Handwriting Objects": b.array([], mutable=True),
            "recordingTimestampString": self.attributed(
                text, [timestamp_sub] if timestamp_sub is not None else [], b.mutable_string(text)),
        })

    # -- pages --------------------------------------------------------------------------

    def page_layout(self, slots: Sequence[_Slot], pdf_files: Dict[str, bytes]) -> Tuple[Any, Any]:
        b = self.b
        if not any(s.kind == "pdf" for s in slots) and (
                len(slots) <= 1 or _page_has_content(slots[-1].page)):
            # All-plain note whose page count follows from its content: the app writes no
            # layout (the path proven on the iPad).  Trailing blank pages would be lost that
            # way, so they get the blank entries the mixed case writes.
            return self.shared_empty(), self.shared_empty()
        file_uids: Dict[str, Any] = {}
        for key, _data in pdf_files.items():
            name = str(uuid.uuid4()).upper() + ".pdf"
            self.pdf_names[key] = name
            file_uids[key] = b.object("PDFFile", {
                "pageNumbers": ArchiveBuilder.NULL,
                "pdfFileName": b.string(name),
                "version_4_1_OrLater": True,
                "version": 2,
                "contentBoxVersion": 1,
                "highlights": self.shared_empty(),
            })
        entries = []
        for slot in slots:
            if slot.kind == "pdf" and slot.pdf_key in file_uids:
                entries.append(b.dictionary([
                    ("kPageLayoutPDFIsOriginalPageKey", True),
                    ("kPageLayoutPDFPageNumberKey", int(slot.pdf_page)),
                    ("kPageLayoutPDFFileKey", file_uids[slot.pdf_key]),
                ], mutable=False))
            else:
                entries.append(b.dictionary([("kPageLayoutPDFPageNumberKey", INT64_MAX)], mutable=False))
        return b.array(entries), b.array(list(file_uids.values()))

    # -- media --------------------------------------------------------------------------

    def _media_common(self, origin: Tuple[float, float], size: Tuple[float, float], rotation_rad: float) -> Dict[str, Any]:
        b = self.b
        black = color_string(0, 0, 0, 1)
        return {
            "unscaledContentSize": b.string(size_string(*size)),
            "captionFontColorCrossPlatform": b.string(black),
            "documentOrigin": b.string(point_string(*origin)),
            "isCaptionEnabled": False,
            "rotationDegrees": float(rotation_rad),
            "captionIsUnderlined": False,
            "documentContentOrigin": b.string(point_string(*origin)),
            "captionFieldText": ArchiveBuilder.NULL,
            "minDimension": 8.0,
            "textWrapMode": 1,
            "cornerMode": 1,
            "recordingEventID": -1,
            "captionFontName": b.string(DEFAULT_FONT),
            "assetsIdKey": b.mutable_string(str(uuid.uuid4()).upper()),
            "handwritingZIndecesKey": b.array([], mutable=True),
            "captionFontSize": 18.0,
            "zIndex": self._next_z(),
            "captionFontColor": b.uicolor(0, 0, 0, 1),
            "maxDimension": 1024.0,
        }

    def _next_z(self) -> int:
        z = self.z_index
        self.z_index += 1
        return z

    def image_object(self, image: Image, slot: _Slot) -> Optional[Any]:
        b = self.b
        if not image.data:
            self.doc.warn("Images without data were dropped")
            return None
        fmt = _image_format(image)
        if fmt == "pdf":
            self.pdf_images += 1
            return None
        rotation = float(image.rotation or 0.0)
        if fmt == "jpeg" and rotation % 360.0 > 1e-6 and (jpeg_exif_orientation(image.data) or 1) != 1:
            self.exif_rotated += 1
        ext = "png" if fmt == "png" else "jpg"
        index = len(self.image_files)
        name = f"Images/Image {'' if index == 0 else index}.{ext}"
        self.image_files.append((name, bytes(image.data)))
        pixels = image_pixel_size(image.data)
        s = slot.scale
        if pixels is None:
            self.doc.warn("Image pixel size could not be read; crop rectangle uses the displayed size")
            pixels = (max(1, int(round(image.w * s))), max(1, int(round(image.h * s))))
        origin = (slot.doc_x(image.x), slot.y_offset + image.y * s)
        size = (max(image.w * s, 1.0), max(image.h * s, 1.0))
        transparent = b.uicolor(0, 0, 0, 0)
        image_obj = b.object("ImageObject", {
            "kImageObjectSnapshotKey": b.object("GLSnapshot", {
                "saveAsJPEG": fmt != "png",
                "imageIsMissing": False,
                "relativePath": b.string(name),
            }),
            "fillColorCrossPlatform": b.string(color_string(0, 0, 0, 0)),
            "fillColor": transparent,
            "strokeAlpha": 1.0,
            "strokeWidth": 1.0,
            "strokeColor": b.uicolor(0, 0, 0, 1),
            "fillAlpha": 1.0,
            "strokeColorCrossPlatform": b.string(color_string(0, 0, 0, 1)),
            "rect": b.string(rect_string(0, 0, 0, 0)),
        })
        figure = b.object("Figure", {
            "kFigurePrimitiveObjectsArrayKey": b.array([], mutable=True),
            "FigureObjectTypeKey": 1,
            "FigureCanvasSizeKey": b.string(size_string(0, 0)),
            "FigureCropRectKey": b.string(rect_string(0, 0, pixels[0], pixels[1])),
            "$0": ArchiveBuilder.NULL,
            "FigureBackgroundObjectKey": image_obj,
        })
        fields = self._media_common(origin, size, math.radians(float(image.rotation or 0.0)))
        fields["figure"] = figure
        fields["indexable"] = False
        return b.object("ImageMediaObject", fields)

    def _font_name(self, run_font: Optional[str], bold: bool, italic: bool) -> str:
        """PostScript name for a run: the font's own style suffix merged with the run's flags."""
        base = run_font or DEFAULT_FONT
        if "-" in base:
            stem, suffix = base.rsplit("-", 1)
            sl = suffix.lower()
            has_bold, has_italic = "bold" in sl, ("italic" in sl or "oblique" in sl)
            if has_bold or has_italic:
                bold, italic = bold or has_bold, italic or has_italic
                base = stem
            elif not (bold or italic):
                return base  # a weight name such as Helvetica-Light stays as it is
            elif sl in ("regular", "roman", "book", "plain"):
                base = stem  # AvenirNext-Regular + bold -> AvenirNext-Bold
            # any other suffix (a weight): append the requested style, best effort
        if bold and italic:
            return base + "-BoldItalic"
        if bold:
            return base + "-Bold"
        if italic:
            return base + "-Italic"
        return base

    def text_subranges(self, box: TextBox, scale: float) -> Tuple[str, List[Any]]:
        b = self.b
        from ..model import TextRun
        text = box.text or ""
        runs = list(box.runs) if box.runs and "".join(r.text for r in box.runs) == text else []
        if not runs:
            if box.runs:
                self.doc.warn("Text runs did not cover the text box content; formatting of one box was flattened")
            runs = [TextRun(text)]
        clean = sanitise_text(text)
        if clean != text:
            self.doc.warn("Text containing unpaired surrogate characters was written with U+FFFD in their place")
            text = clean
            runs = [dataclasses.replace(r, text=sanitise_text(r.text)) for r in runs]
        subranges = []
        location = 0
        for run in runs:
            length = len(run.text.encode("utf-16-le")) // 2
            if length == 0:
                continue
            color = run.color or box.color or (0.0, 0.0, 0.0, 1.0)
            r, g, bl, a = (tuple(color) + (1.0,) * 4)[:4]
            size = float(run.size if run.size else box.size or 12.0) * scale
            size_value: Any = int(round(size)) if abs(size - round(size)) < 1e-6 else round(size, 2)
            subranges.append(b.dictionary([
                ("subRangeColorCrossPlatformKey", b.string(color_string(r, g, bl, a))),
                ("subRangeRangeKey", b.string(range_string(location, length))),
                ("subRangeFontKey", b.dictionary([
                    ("NSFontSizeAttribute", size_value),
                    ("NSFontNameAttribute", b.string(self._font_name(run.font, run.bold, run.italic))),
                ])),
                ("subRangeOtherAttributesKey", b.dictionary([("NSUnderline", 1 if run.underline else 0)])),
                ("subRangeColorKey", b.uicolor(r, g, bl, a)),
            ]))
            location += length
        return text, subranges

    def text_object(self, box: TextBox, slot: _Slot) -> Any:
        b = self.b
        s = slot.scale
        text, subranges = self.text_subranges(box, s)
        alignment = TEXT_ALIGNMENT.get((box.align or "left").lower(), 0)
        store = self.formatted_string(
            self.empty_hash(),
            b.object("NBReflowStateReflowable", {}),
            text, subranges,
            self.shared_empty(), self.shared_empty(), self.shared_empty(),
            alignment=alignment,
        )
        origin = (slot.doc_x(box.x) - TEXT_PAD_X, slot.y_offset + box.y * s - TEXT_PAD_Y)
        size = (max(box.w * s, 1.0) + 2 * TEXT_PAD_X, max(box.h * s, 1.0) + 2 * TEXT_PAD_Y)
        theta = math.radians(float(box.rotation or 0.0))
        if abs(theta) > 1e-9:
            origin = text_origin_for_centre_pivot(origin, size, (TEXT_PAD_X, TEXT_PAD_Y), theta)
        fields = self._media_common(origin, size, theta)
        fields["paperIndex"] = -1
        fields["lineStyle"] = 0
        fields["paperStyleObject"] = b.object("Notability.NBPaperStyle", {
            "paperColor": ArchiveBuilder.NULL,
            "lineStyle": 0,
            "paperImageIndex": -1,
        })
        fields["textStore"] = store
        return b.object("TextBlockMediaObject", fields)

    # -- root ---------------------------------------------------------------------------

    def build(self, slots: Sequence[_Slot], pdf_files: Dict[str, bytes], name: str, subject: str,
              created: float) -> bytes:
        b = self.b
        root = b.reserve()
        layout, pdf_array = self.page_layout(slots, pdf_files)
        ink = self.spatial_hash(_build_ink(self.doc, slots, self.options))
        media_uids = []
        for slot in slots:
            for image in slot.page.images:
                uid = self.image_object(image, slot)
                if uid is not None:
                    media_uids.append(uid)
            for box in slot.page.texts:
                media_uids.append(self.text_object(box, slot))
        if self.pdf_images:
            self.doc.warn(f"{self.pdf_images} PDF images dropped (Notability images must be PNG or JPEG)")
        if self.exif_rotated:
            self.doc.warn(f"{self.exif_rotated} rotated photos carry an EXIF orientation; Notability may apply "
                          "it on top of the written rotation (unverified)")
        media = b.array(media_uids) if media_uids else self.shared_empty()
        reflow = b.object("NBReflowStateLocked", {
            "pageWidthInDocumentCoordsKey": float(self.width),
            "nativeLayoutDeviceStringKey": b.string("iPad"),
        })
        rich = self.formatted_string(ink, reflow, "", [], layout, pdf_array, media)
        name_uid = b.mutable_string(name)
        b.object("NoteTakingSession", {
            "subject": b.string(subject),
            "NBNoteTakingSessionBundleVersionNumberKey": b.string(BUNDLE_VERSION),
            "NBNoteTakingSessionHandwritingLanguageKey": b.string("en_US"),
            "contentPlaybackEventManager": b.object("NBCPEventManager", {
                "NBCPTimeManagerSOATimestampsKey": b"",
                "NBCPTimeManagerSOANumEventsKey": 0,
                "NBCPTimeManagerSOARecordingIDsKey": b"",
                "NBCPTimeManagerSOADurationsKey": b"",
                "NBCPTimeManagerSOAEventIDsKey": b"",
            }),
            "NBPaperMinorVersionNumber": 1,
            "paperLineStyle": 0,
            "NBNoteTakingSessionMinorVersionNumberKey": 5,
            "packagePath": name_uid,
            "tags": b.string(""),
            "NBNoteTakingSessionIsHighlighterBehindTextKey": True,
            "sessionFormatVersion": 5,
            "paperIndex": 12,
            "NBNoteTakingSessionAllowHighlighterConversionKey": False,
            "NBPaperMajorVersionNumber": 1,
            "richText": rich,
            "isReadOnly": False,
            "creationDate": b.date(created),
            "name": name_uid,
        }, uid=root)
        b.set_root(root)
        return b.dumps()


# ---------------------------------------------------------------------------------------
# metadata.plist and library.plist

def _metadata_plist(name: str, subject: str, created: float, note_uuid: str) -> bytes:
    b = ArchiveBuilder("NSKeyedArchiver", "root")
    root = b.reserve()
    name_uid = b.mutable_string(name, as_bytes=False)
    creation = b.date(created)
    modified = b.date(created)
    per_type = b.dictionary([(8, creation), (2, modified), (16, creation), (1, creation), (4, creation)])
    b.object("SessionInfo", {
        "notePackagePath": name_uid,
        "noteLastChangeDatePerTypeKey": per_type,
        "noteCreationDateKey": creation,
        "associatedProductsKey": b.array([]),
        "uuidKey": b.string(note_uuid),
        "noteSubject": b.string(subject),
        "noteHasRecordingKey": False,
        "noteName": name_uid,
        "noteTags": b.string(""),
        "noteModifiedDateKey": modified,
    }, uid=root)
    b.set_root(root)
    return b.dumps()


def _library_plist() -> bytes:
    import plistlib
    return plistlib.dumps(
        {"application version": APP_BUILD, "library-format-version": "1.0", "recordings": {}},
        fmt=plistlib.FMT_XML, sort_keys=True,
    )


MAX_NAME_BYTES = 200  # bundle folder name; APFS / HFS+ allow 255 bytes, keep a margin for suffixes


def _note_name(doc: Document, options: Any) -> str:
    title = _opt(options, "title", None) or doc.title or "Untitled"
    title = unicodedata.normalize("NFC", sanitise_text(str(title)))
    cleaned = "".join(c for c in title if c not in '/\\:' and (ord(c) >= 32)).strip(" .")
    if len(cleaned.encode("utf-8")) > MAX_NAME_BYTES:
        cleaned = cleaned.encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore").rstrip(" .")
        doc.warn(f"Note title was shortened to {MAX_NAME_BYTES} bytes for the bundle folder name")
    return cleaned or "Untitled"


def sanitise_text(text: str) -> str:
    """Replace lone UTF-16 surrogates (unencodable in UTF-8 and UTF-16) with U+FFFD."""
    try:
        text.encode("utf-16-le")
        return text
    except UnicodeEncodeError:
        return "".join("\ufffd" if 0xD800 <= ord(c) <= 0xDFFF else c for c in text)


# ---------------------------------------------------------------------------------------
# public entry point

def write_note(doc: Document, options: Any = None) -> bytes:
    """Serialise ``doc`` as a Notability ``.note`` (ZIP bytes).

    ``options`` may be any object; the attributes ``paper`` ("plain" | "pdf"), ``pressure``
    (bool), ``simplify`` (float pt), ``title`` (str) and ``notability_page_width`` (float)
    are read with the defaults of ``docs/design.md`` when missing.
    """
    width = float(_opt(options, "notability_page_width", DEFAULT_PAGE_WIDTH) or DEFAULT_PAGE_WIDTH)
    if width <= 0:
        width = DEFAULT_PAGE_WIDTH
    if not doc.pages:
        doc.warn("The document has no pages; an empty note was written")
    slots, pdf_files = _layout(doc, options, width)
    name = _note_name(doc, options)
    subject = str(_opt(options, "subject", None) or "GoodNotes import")
    created = time.time()
    note_uuid = str(uuid.uuid4()).upper()

    session = _SessionBuilder(doc, options, width)
    session_bytes = session.build(slots, pdf_files, name, subject, created)
    metadata = _metadata_plist(name, subject, created, note_uuid)

    stamp = time.localtime(created)
    date_time = (max(1980, stamp.tm_year), stamp.tm_mon, stamp.tm_mday, stamp.tm_hour, stamp.tm_min, stamp.tm_sec)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def member(rel: str, data: bytes = b"", directory: bool = False) -> None:
            info = zipfile.ZipInfo(f"{name}/{rel}", date_time=date_time)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
            info.external_attr = (0o40755 << 16) | 0x10 if directory else (0o100644 << 16)
            z.writestr(info, data)

        for d in ("Assets/", "Images/", "PDFs/", "HandwritingIndex/", "Recordings/"):
            member(d, directory=True)
        member("Session.plist", session_bytes)
        member("metadata.plist", metadata)
        member("Recordings/library.plist", _library_plist())
        for fname, w, h in THUMB_SIZES:
            member(fname, white_png(w, h))
        for key, data in pdf_files.items():
            pdf_name = session.pdf_names.get(key)
            if pdf_name:
                member(f"PDFs/{pdf_name}", data)
        for rel, data in session.image_files:
            member(rel, data)
    return buf.getvalue()
