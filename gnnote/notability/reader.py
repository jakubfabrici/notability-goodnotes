"""Notability ``.note`` -> :class:`gnnote.model.Document` (tolerant reader).

Container (``docs/notability-format.md`` Part 1): a ``.note`` is a ZIP whose members live under
one folder ``<Name>/``.  The member ending in ``Session.plist`` locates the bundle root; its
siblings are ``metadata.plist`` (title, recording flag), ``PDFs/<UUID>.pdf`` (imported PDFs and
paper templates), ``Images/Image N.jpg|png`` (raster media), ``Recordings/`` (audio, dropped
with a warning) and ``HandwritingIndex/index.plist`` (recognition cache, ignored).

Object graph (Parts 3-7): ``Session.plist`` is a GLKeyedArchiver archive whose root
``NoteTakingSession`` holds ``richText`` (a ``FormattedString``) with

* ``reflowState.pageWidthInDocumentCoordsKey`` = ``W``, the page width in document units
  (fallback: ``paperSizingBehavior 'lockedWidth:<W>:...'`` of the paper layout model, else 565);
* ``pageLayoutArray`` (or the 11.7+ paper layout model's copy): one NSDictionary per page, either
  ``{kPageLayoutPDFPageNumberKey: INT64_MAX}`` for a blank page or a PDF page referencing a
  ``PDFFile`` (``kPageLayoutPDFFileKey``) or a file name (``kPageLayoutPDFFileNameKey``);
* ``'Handwriting Overlay'.SpatialHash``: the ``InkedSpatialHash`` with the parallel ink arrays
  (little-endian): ``curvesnumpoints`` ``<Ni`` (``n_i = 1 + 3k_i``), ``curvespoints`` ``<2Pf``
  (anchor, then ``(c1, c2, anchor) * k_i`` per curve), ``curvesfractionalwidths`` ``<Ff``
  (``k_i + 1`` per curve, one per anchor), ``curveswidth`` ``<Nf`` (base width, document units),
  ``curvescolors`` (4 x uint8 RGBA per curve), ``curvesstyles`` (uint8 per curve, 3 pen /
  4 highlighter / 5 pencil; may be absent or shorter than N), ``dashStyles`` / ``shapes`` (bplist
  blobs, reported only) and ``groupsArrays`` (Notability 16 ink groups: bplists holding nested
  keyed archives of the same shape plus a CGAffineTransform ``[a, b, c, d, tx, ty]``);
* ``mediaObjects``: ``ImageMediaObject`` (``documentOrigin`` ``'{x, y}'``, ``unscaledContentSize``
  ``'{w, h}'``, ``rotationDegrees`` in radians, image path under ``figure``), ``TextBlockMediaObject``
  (``textStore.attributedString`` = ``{stringKey, subRangesKey}``), ``CanvasMediaObject`` stickies
  and ``MathMediaObject`` (both dropped with a warning);
* ``attributedString``: the flow text typed directly on the page (no stored position).

Coordinates (Part 5): one continuous y-down space; pages are stacked without gaps.  A plain
page is ``H = 1.3125 * W`` document units tall (``floor(W * 11 / 8.5)`` for ``staticWidth`` +
``letter``) and maps to 612 x 803.25 pt; a PDF page occupies ``ceil(h * W / w)`` units with the
PDF content bottom-aligned inside the slot and maps to the PDF page's own size.  An element
belongs to the slot containing the y of its first anchor / origin; content below the last laid
out page lies on implicit plain pages.
"""
from __future__ import annotations

import io
import math
import plistlib
import re
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from .keyedarchive import (
    Archive, color_from_uicolor, load_archive, parse_color_string, parse_point,
    parse_range, parse_rect,
)

__all__ = ["read_note", "PLAIN_PAGE_WIDTH_PT", "PLAIN_PAGE_HEIGHT_PT", "LEGACY_ASPECT"]

LEGACY_ASPECT = 1.3125  # plain page height / width in document units (803.25 / 612)
PLAIN_PAGE_WIDTH_PT = 612.0
PLAIN_PAGE_HEIGHT_PT = PLAIN_PAGE_WIDTH_PT * LEGACY_ASPECT  # 803.25
DEFAULT_PAGE_WIDTH = 565.0  # W used when the note never recorded one
BLANK_PAGE_MARKERS = (0x7FFFFFFF, 0x7FFFFFFFFFFFFFFF)
FLOW_TEXT_MARGIN_PT = 36.0

STYLE_PEN = 3
STYLE_HIGHLIGHTER = 4
STYLE_PENCIL = 5


# ----------------------------------------------------------------------------------
# Internal records
# ----------------------------------------------------------------------------------


@dataclass
class _Curve:
    points: List[Tuple[float, float]]  # document units, absolute
    width: float  # base width, document units
    fractional: List[float]  # one per anchor
    rgba: Tuple[int, int, int, int]
    style: Optional[int]  # None when the file carries no style for this curve
    dashed: bool = False
    bezier: bool = True  # False when n_i was not 1 + 3k (treated as a polyline)


@dataclass
class _Slot:
    y0: float  # document units
    height: float  # document units occupied
    width_pt: float
    height_pt: float
    scale: float  # pt per document unit
    top_gap: float = 0.0  # document units of empty space above a bottom-aligned PDF page
    background: Optional[PdfBackground] = None
    builtin: bool = False


@dataclass
class _Layout:
    width: float  # W
    plain_height: float  # H, document units
    slots: List[_Slot] = field(default_factory=list)  # explicit layout entries
    template: Optional[Tuple[str, float, float]] = None  # (pdf_id, w, h) paper template PDF

    def plain_slot(self, y0: float) -> _Slot:
        scale = PLAIN_PAGE_WIDTH_PT / self.width
        if self.template is not None:
            pdf_id, w, h = self.template
            return _Slot(y0, self.plain_height, w, h, w / self.width,
                         background=PdfBackground(pdf_id, 0), builtin=True)
        return _Slot(y0, self.plain_height, PLAIN_PAGE_WIDTH_PT, self.plain_height * scale, scale)

    def end(self) -> float:
        return self.slots[-1].y0 + self.slots[-1].height if self.slots else 0.0

    def index_for(self, y: float) -> int:
        """Page index of document coordinate ``y`` (content above page 1 belongs to page 1)."""
        if y < 0:
            return 0
        for i, slot in enumerate(self.slots):
            if y < slot.y0 + slot.height:
                return i
        return len(self.slots) + int(math.floor((y - self.end()) / self.plain_height))

    def slot(self, index: int) -> _Slot:
        if index < len(self.slots):
            return self.slots[index]
        y0 = self.end() + (index - len(self.slots)) * self.plain_height
        return self.plain_slot(y0)


class _Bundle:
    """ZIP access relative to the folder holding ``Session.plist``."""

    def __init__(self, data: bytes):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("not a Notability note: not a ZIP archive") from exc
        names = self.zip.namelist()
        sessions = [n for n in names if n == "Session.plist" or n.endswith("/Session.plist")]
        if not sessions:
            raise ValueError("not a Notability note: Session.plist not found")
        sessions.sort(key=lambda n: (n.count("/"), len(n)))
        self.session_name = sessions[0]
        self.root = self.session_name[: -len("Session.plist")]
        self.names = set(names)
        self._lower = {n.lower(): n for n in names}

    def read(self, relative: str) -> Optional[bytes]:
        name = self.root + relative
        if name not in self.names:
            alt = self._lower.get(name.lower())
            if alt is None:
                return None
            name = alt
        try:
            return self.zip.read(name)
        except Exception:  # noqa: BLE001 - corrupt member
            return None

    def members(self, prefix: str) -> List[str]:
        full = self.root + prefix
        return [n[len(self.root):] for n in self.names if n.startswith(full) and not n.endswith("/")]

    @property
    def folder(self) -> str:
        return self.root.rstrip("/").split("/")[-1] if self.root else ""


# ----------------------------------------------------------------------------------
# PDF page sizes (pdfutil with a regex fallback)
# ----------------------------------------------------------------------------------


def _pdf_page_sizes(data: bytes) -> List[Tuple[float, float]]:
    """(width, height) in pt per page, display orientation; empty when unreadable."""
    try:
        from .. import pdfutil  # imported lazily: the module is developed in parallel
    except Exception:  # noqa: BLE001
        pdfutil = None
    if pdfutil is not None:
        try:
            info = pdfutil.pdf_info(data)
            return [(float(p.width), float(p.height)) for p in info.pages]
        except Exception:  # noqa: BLE001 - fall back to the regex scan
            pass
    return _regex_page_sizes(data)


_MEDIABOX_RE = re.compile(
    rb"/MediaBox\s*\[\s*([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s*\]")
_ROTATE_RE = re.compile(rb"/Rotate\s+(-?\d+)")
_PAGE_RE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def _regex_page_sizes(data: bytes) -> List[Tuple[float, float]]:
    """Last-resort page sizes: the MediaBox nearest to each uncompressed ``/Type /Page``."""
    sizes: List[Tuple[float, float]] = []
    for m in _PAGE_RE.finditer(data):
        window = data[max(0, m.start() - 1500): m.end() + 1500]
        box = _MEDIABOX_RE.search(window)
        if not box:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in box.groups())
        except ValueError:
            continue
        w, h = abs(x1 - x0), abs(y1 - y0)
        rot = _ROTATE_RE.search(window)
        if rot and (int(rot.group(1)) // 90) % 2 == 1:
            w, h = h, w
        if w > 0 and h > 0:
            sizes.append((w, h))
    if not sizes:
        for b in _MEDIABOX_RE.findall(data):
            try:
                x0, y0, x1, y1 = (float(v) for v in b)
            except ValueError:
                continue
            if abs(x1 - x0) > 0 and abs(y1 - y0) > 0:
                sizes.append((abs(x1 - x0), abs(y1 - y0)))
    return sizes


# ----------------------------------------------------------------------------------
# Raster sniffing
# ----------------------------------------------------------------------------------


def _image_format(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def _image_pixel_size(data: bytes) -> Optional[Tuple[int, int]]:
    fmt = _image_format(data)
    try:
        if fmt == "png" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if fmt == "gif" and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return w, h
        if fmt == "jpeg":
            pos = 2
            while pos + 9 < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                marker = data[pos + 1]
                if marker == 0xFF:
                    pos += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    pos += 2
                    continue
                length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
                    return w, h
                pos += 2 + length
    except struct.error:
        return None
    return None


# ----------------------------------------------------------------------------------
# Ink decoding
# ----------------------------------------------------------------------------------


def _unpack_floats(raw: bytes) -> Tuple[float, ...]:
    n = len(raw) // 4
    return struct.unpack(f"<{n}f", raw[: 4 * n]) if n else ()


def _unpack_ints(raw: bytes) -> Tuple[int, ...]:
    n = len(raw) // 4
    return struct.unpack(f"<{n}i", raw[: 4 * n]) if n else ()


def _dash_indices(archive: Archive, hash_obj: Any, doc: Document) -> set:
    raw = archive.data(archive.get(hash_obj, "dashStyles"))
    if not raw:
        return set()
    try:
        pl = plistlib.loads(raw)
    except Exception:  # noqa: BLE001
        doc.warn("Unreadable dash style table ignored")
        return set()
    patterns = pl.get("objectPatterns") if isinstance(pl, dict) else None
    out = set()
    if isinstance(patterns, dict):
        for key, value in patterns.items():
            try:
                idx = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and value.get("pattern", 0):
                out.add(idx)
    return out


def _shape_count(archive: Archive, hash_obj: Any) -> int:
    raw = archive.data(archive.get(hash_obj, "shapes"))
    if not raw:
        return 0
    try:
        pl = plistlib.loads(raw)
    except Exception:  # noqa: BLE001
        return 0
    shapes = pl.get("shapes") if isinstance(pl, dict) else None
    return len(shapes) if isinstance(shapes, list) else 0


def _decode_hash(archive: Archive, hash_obj: Any, doc: Document, label: str) -> List[_Curve]:
    """Decode the parallel arrays of one ``InkedSpatialHash`` (or a group's nested copy)."""
    hash_obj = archive.deref(hash_obj)
    if not isinstance(hash_obj, dict):
        return []
    nc = archive.integer(archive.get(hash_obj, "numcurves", 0))
    if nc <= 0:
        return []
    numpoints = _unpack_ints(archive.data(archive.get(hash_obj, "curvesnumpoints")))
    points = _unpack_floats(archive.data(archive.get(hash_obj, "curvespoints")))
    widths = _unpack_floats(archive.data(archive.get(hash_obj, "curveswidth")))
    fractional = _unpack_floats(archive.data(archive.get(hash_obj, "curvesfractionalwidths")))
    colors = archive.data(archive.get(hash_obj, "curvescolors"))
    styles = archive.data(archive.get(hash_obj, "curvesstyles"))
    dashed = _dash_indices(archive, hash_obj, doc)
    if len(numpoints) < nc:
        doc.warn(f"{label}: curvesnumpoints holds {len(numpoints)} of {nc} curves; the rest is skipped")
        nc = len(numpoints)
    total_points = len(points) // 2
    out: List[_Curve] = []
    p = f = 0
    truncated = False
    for i in range(nc):
        n = numpoints[i]
        if n <= 0:
            continue
        if p + n > total_points:
            truncated = True
            break
        k, rem = divmod(n - 1, 3)
        anchors = k + 1 if rem == 0 else n
        pts = [(points[2 * (p + j)], points[2 * (p + j) + 1]) for j in range(n)]
        fw = list(fractional[f: f + anchors])
        if len(fw) < anchors:
            fw += [fw[-1] if fw else 1.0] * (anchors - len(fw))
        width = widths[i] if i < len(widths) else 1.0
        if 4 * i + 4 <= len(colors):
            rgba = tuple(colors[4 * i: 4 * i + 4])
        else:
            rgba = (0, 0, 0, 255)
        style = styles[i] if i < len(styles) else None
        out.append(_Curve(pts, float(width), fw, rgba, style, dashed=i in dashed, bezier=rem == 0))  # type: ignore[arg-type]
        p += n
        f += anchors
    if truncated:
        doc.warn(f"{label}: curvespoints shorter than curvesnumpoints announces; trailing curves skipped")
    if any(not c.bezier for c in out):
        doc.warn(f"{label}: curves whose point count is not 1 + 3k were read as polylines")
    return out


def _apply_transform(curves: Sequence[_Curve], transform: Optional[Sequence[float]]) -> None:
    if not transform or len(transform) < 6:
        return
    try:
        a, b, c, d, tx, ty = (float(v) for v in transform[:6])
    except (TypeError, ValueError):
        return
    scale = math.hypot(a, b) or 1.0
    for curve in curves:
        curve.points = [(a * x + c * y + tx, b * x + d * y + ty) for x, y in curve.points]
        curve.width *= scale


def _collect_curves(archive: Archive, hash_obj: Any, doc: Document, label: str, depth: int,
                    stats: Dict[str, int]) -> List[Tuple[float, _Curve]]:
    """Top-level curves (z = index) plus grouped curves (z = group index + 0.5), untransformed."""
    hash_obj = archive.deref(hash_obj)
    if not isinstance(hash_obj, dict):
        return []
    out: List[Tuple[float, _Curve]] = [(float(i), c) for i, c in enumerate(_decode_hash(archive, hash_obj, doc, label))]
    stats["shapes"] += _shape_count(archive, hash_obj)
    if depth > 8:
        doc.warn("Ink groups nested deeper than 8 levels were skipped")
        return out
    for gi, blob in enumerate(archive.array(archive.get(hash_obj, "groupsArrays"))):
        raw = archive.data(blob)
        if not raw:
            continue
        try:
            group = plistlib.loads(raw)
        except Exception:  # noqa: BLE001
            doc.warn(f"{label}: unreadable ink group {gi} skipped")
            continue
        ink = group.get("inkGroup") if isinstance(group, dict) else None
        if not isinstance(ink, dict):
            continue
        index = group.get("index")
        z = (float(index) if isinstance(index, (int, float)) and not isinstance(index, bool) else float(len(out))) + 0.5
        transform = ink.get("transform")
        members: List[_Curve] = []
        for obj in ink.get("inkGroupObjects") or []:
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            payload = obj.get("object")
            if kind == 1 and isinstance(payload, (bytes, bytearray)):
                try:
                    nested = load_archive(bytes(payload))
                except Exception:  # noqa: BLE001
                    doc.warn(f"{label}: unreadable grouped ink archive skipped")
                    continue
                stats["groups"] += 1
                for _, curve in _collect_curves(nested, nested.root, doc, f"{label} group {gi}", depth + 1, stats):
                    members.append(curve)
            elif kind == 2:
                stats["shapes"] += 1
        _apply_transform(members, transform if isinstance(transform, (list, tuple)) else None)
        out.extend((z, c) for c in members)
    return out


def _curve_to_stroke(curve: _Curve, slot: _Slot) -> Stroke:
    scale = slot.scale
    base = curve.width * scale
    r, g, b, a = curve.rgba
    color = (r / 255.0, g / 255.0, b / 255.0, a / 255.0)
    style = curve.style
    if style == STYLE_HIGHLIGHTER or (style is None and a < 255):
        kind = "highlighter"
    else:
        kind = "pen"
    pen = "pencil" if style == STYLE_PENCIL else None

    def to_pt(xy: Tuple[float, float], width: float) -> Point:
        return Point(xy[0] * scale, (xy[1] - slot.y0 - slot.top_gap) * scale, width)

    fw = curve.fractional
    if curve.bezier:
        k = (len(curve.points) - 1) // 3
        anchors: List[Point] = []
        controls: List[Tuple[Point, Point]] = []
        for s in range(k + 1):
            w = base * (fw[s] if s < len(fw) else 1.0)
            anchors.append(to_pt(curve.points[3 * s], w))
        for s in range(k):
            c1 = to_pt(curve.points[3 * s + 1], anchors[s].width)
            c2 = to_pt(curve.points[3 * s + 2], anchors[s + 1].width)
            controls.append((c1, c2))
        return Stroke(anchors, color=color, kind=kind, pen=pen, width=base, controls=controls)
    pts = [to_pt(xy, base * (fw[j] if j < len(fw) else 1.0)) for j, xy in enumerate(curve.points)]
    return Stroke(pts, color=color, kind=kind, pen=pen, width=base, controls=None)


# ----------------------------------------------------------------------------------
# Text
# ----------------------------------------------------------------------------------


def _utf16_slice(text: str, location: int, length: int) -> str:
    encoded = text.encode("utf-16-le", "surrogatepass")
    start = max(0, 2 * location)
    end = max(start, 2 * (location + length))
    return encoded[start:end].decode("utf-16-le", "ignore")


def _style_from_font(name: Optional[str]) -> Tuple[bool, bool]:
    if not name:
        return False, False
    lowered = name.lower()
    suffix = lowered.rsplit("-", 1)[-1] if "-" in lowered else ""
    bold = "bold" in suffix or lowered.endswith("bold") or "black" in suffix or "heavy" in suffix
    italic = "italic" in suffix or "oblique" in suffix
    return bold, italic


def _runs_from_attributed(archive: Archive, attributed: Any) -> Tuple[str, List[TextRun]]:
    d = archive.dictionary(attributed)
    text = archive.string(d.get("stringKey")) or ""
    runs: List[TextRun] = []
    for sub in archive.array(d.get("subRangesKey")):
        sd = archive.dictionary(sub)
        rng = parse_range(archive.string(sd.get("subRangeRangeKey")))
        if rng is None:
            continue
        chunk = _utf16_slice(text, rng[0], rng[1])
        if not chunk:
            continue
        font = archive.dictionary(sd.get("subRangeFontKey"))
        font_name = archive.string(font.get("NSFontNameAttribute"))
        size_val = font.get("NSFontSizeAttribute")
        size = archive.number(size_val) if size_val is not None else None
        other = archive.dictionary(sd.get("subRangeOtherAttributesKey"))
        underline = archive.integer(other.get("NSUnderline", 0)) != 0
        color = parse_color_string(archive.string(sd.get("subRangeColorCrossPlatformKey")))
        if color is None:
            color = color_from_uicolor(archive, sd.get("subRangeColorKey"))
        bold, italic = _style_from_font(font_name)
        runs.append(TextRun(chunk, bold=bold, italic=italic, underline=underline, font=font_name,
                            size=size, color=color))
    if not runs and text:
        runs.append(TextRun(text))
    return text, runs


def _scale_runs(runs: List[TextRun], scale: float) -> None:
    for run in runs:
        if run.size is not None:
            run.size = run.size * scale


# ----------------------------------------------------------------------------------
# The reader
# ----------------------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes):
        self.bundle = _Bundle(data)
        self.doc = Document(source_format="notability")
        session = self.bundle.read("Session.plist")
        if session is None:
            raise ValueError("not a Notability note: Session.plist unreadable")
        try:
            self.archive = load_archive(session)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"not a Notability note: Session.plist is not a keyed archive ({exc})") from exc
        self.root = self.archive.root if isinstance(self.archive.root, dict) else {}
        self.rich = self.archive.get(self.root, "richText")
        self.pdf_sizes: Dict[str, List[Tuple[float, float]]] = {}
        self.stats = {"groups": 0, "shapes": 0}

    # -- helpers --------------------------------------------------------------------

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    def paper_attributes(self) -> Dict[str, Any]:
        model = self.archive.get(self.root, "NBNoteTakingSessionDocumentPaperLayoutModelKey")
        attrs = self.archive.get(model, "documentPaperAttributes")
        return attrs if isinstance(attrs, dict) else {}

    def title(self) -> str:
        meta = self.bundle.read("metadata.plist")
        if meta:
            try:
                m = load_archive(meta)
                name = m.string(m.get(m.root, "noteName"))
                if name:
                    return name
            except Exception:  # noqa: BLE001
                pass
        for key in ("name", "packagePath"):
            name = self.archive.string(self.root.get(key)) if isinstance(self.root, dict) else None
            if name:
                return name
        return self.bundle.folder or "Untitled"

    # -- PDFs -----------------------------------------------------------------------

    def load_pdf(self, file_name: Optional[str]) -> Optional[str]:
        """Store ``PDFs/<file_name>`` in the document; returns its ``pdf_id`` or None."""
        if not file_name:
            return None
        pdf_id = file_name[:-4] if file_name.lower().endswith(".pdf") else file_name
        if pdf_id in self.doc.pdfs:
            return pdf_id
        data = self.bundle.read("PDFs/" + file_name)
        if data is None:
            # Some files store the PDF directly under the bundle root.
            data = self.bundle.read(file_name)
        if data is None:
            self.warn(f"PDF {file_name} referenced by the page layout is missing from the note")
            return None
        sizes = _pdf_page_sizes(data)
        if not sizes:
            self.warn(f"PDF {file_name} has no readable page; its pages are treated as plain paper")
            return None
        self.doc.pdfs[pdf_id] = data
        self.pdf_sizes[pdf_id] = sizes
        return pdf_id

    # -- layout ---------------------------------------------------------------------

    def page_width(self, attrs: Dict[str, Any]) -> float:
        reflow = self.archive.get(self.rich, "reflowState")
        width = self.archive.get(reflow, "pageWidthInDocumentCoordsKey")
        if isinstance(width, (int, float)) and not isinstance(width, bool) and width > 0:
            return float(width)
        sizing = self.archive.string(attrs.get("paperSizingBehavior")) or ""
        if sizing.startswith("lockedWidth:"):
            try:
                return float(sizing.split(":")[1])
            except (IndexError, ValueError):
                pass
        self.warn(f"Page width not recorded in the note; assuming {DEFAULT_PAGE_WIDTH:g} document units")
        return DEFAULT_PAGE_WIDTH

    def build_layout(self) -> _Layout:
        attrs = self.paper_attributes()
        width = self.page_width(attrs)
        sizing = self.archive.string(attrs.get("paperSizingBehavior")) or ""
        paper_size = (self.archive.string(attrs.get("paperSize")) or "").lower()
        if sizing == "staticWidth" and paper_size == "letter":
            plain_h = float(math.floor(width * 11.0 / 8.5))
        else:
            plain_h = width * LEGACY_ASPECT
        layout = _Layout(width, plain_h)

        # Paper template PDF (11.7+): every plain page shows page 1 of that PDF.
        ident = self.archive.string(attrs.get("paperIdentifier")) or ""
        if ident.startswith("TemplatePDF:"):
            parts = ident.split(":")
            if len(parts) >= 2 and parts[1]:
                pdf_id = self.load_pdf(parts[1] + ".pdf")
                if pdf_id is not None:
                    w, h = self.pdf_sizes[pdf_id][0]
                    layout.template = (pdf_id, w, h)

        entries = self.archive.array(self.archive.get(self.rich, "pageLayoutArray"))
        if not entries:
            model = self.archive.get(self.root, "NBNoteTakingSessionDocumentPaperLayoutModelKey")
            entries = self.archive.array(self.archive.get(model, "pageLayoutArray"))
        y = 0.0
        for entry in entries:
            d = self.archive.dictionary(entry)
            num_raw = d.get("kPageLayoutPDFPageNumberKey")
            num = self.archive.integer(num_raw, -1) if num_raw is not None else -1
            file_name = self.archive.string(d.get("kPageLayoutPDFFileNameKey"))
            if file_name is None:
                file_name = self.archive.string(self.archive.get(d.get("kPageLayoutPDFFileKey"), "pdfFileName"))
            slot: Optional[_Slot] = None
            if num not in BLANK_PAGE_MARKERS and num >= 1 and file_name:
                pdf_id = self.load_pdf(file_name)
                if pdf_id is not None:
                    sizes = self.pdf_sizes[pdf_id]
                    if num - 1 < len(sizes):
                        w, h = sizes[num - 1]
                        exact = h * width / w if w > 0 else plain_h
                        occupied = float(math.ceil(exact))
                        slot = _Slot(y, occupied, w, h, w / width if w > 0 else PLAIN_PAGE_WIDTH_PT / width,
                                     top_gap=occupied - exact, background=PdfBackground(pdf_id, num - 1))
                    else:
                        self.warn(f"Page layout refers to page {num} of {file_name}, which has only {len(sizes)} pages")
            elif num not in BLANK_PAGE_MARKERS and num >= 1 and not file_name:
                self.warn("Page layout entry without a PDF reference treated as a plain page")
            if slot is None:
                slot = layout.plain_slot(y)
            layout.slots.append(slot)
            y += slot.height
        # PDFs imported but not shown on any page are not carried along.
        for pf in self.archive.array(self.archive.get(self.rich, "pdfFiles")):
            name = self.archive.string(self.archive.get(pf, "pdfFileName"))
            if name:
                pdf_id = name[:-4] if name.lower().endswith(".pdf") else name
                if pdf_id not in self.doc.pdfs and self.archive.integer(self.archive.get(pf, "type", 0)) != 1:
                    self.warn(f"Imported PDF {name} is not shown on any page and was dropped")
        return layout

    # -- content --------------------------------------------------------------------

    def read(self) -> Document:
        doc = self.doc
        doc.title = self.title()
        if not isinstance(self.rich, dict):
            self.warn("Note has no richText; nothing to read")
            doc.pages.append(Page(PLAIN_PAGE_WIDTH_PT, PLAIN_PAGE_HEIGHT_PT))
            return doc
        layout = self.build_layout()
        a = self.archive

        # Ink
        overlay = a.get(self.rich, "Handwriting Overlay")
        hash_obj = a.get(overlay, "SpatialHash")
        curves = _collect_curves(a, hash_obj, doc, "ink", 0, self.stats) if isinstance(hash_obj, dict) else []
        curves.sort(key=lambda zc: zc[0])
        placed: Dict[int, List[Any]] = {}
        max_index = -1
        for _, curve in curves:
            if not curve.points:
                continue
            index = layout.index_for(curve.points[0][1])
            placed.setdefault(index, []).append(("stroke", curve))
            max_index = max(max_index, index)
        if any(c.dashed for _, c in curves):
            self.warn("Dashed or dotted strokes are drawn solid")
        if self.stats["shapes"]:
            self.warn(f"{self.stats['shapes']} vector shape(s) are not converted (shape objects are not ink curves)")

        # Media objects
        for media in a.array(a.get(self.rich, "mediaObjects")):
            cls = a.classname(media) or ""
            origin = parse_point(a.string(a.get(media, "documentOrigin")))
            if origin is None:
                origin = parse_point(a.string(a.get(media, "documentContentOrigin")))
            if cls.endswith("ImageMediaObject"):
                item = self.read_image(media, origin)
            elif cls.endswith("TextBlockMediaObject"):
                item = self.read_textbox(media, origin)
            elif cls.endswith("MathMediaObject"):
                self.warn("Math (LaTeX) objects are not converted")
                continue
            elif cls.endswith("CanvasMediaObject"):
                self.warn("Sticky notes (canvas objects) are not converted")
                continue
            else:
                self.warn(f"Media object of class {cls or 'unknown'} is not converted")
                continue
            if item is None:
                continue
            index = layout.index_for(origin[1] if origin else 0.0)
            placed.setdefault(index, []).append(item)
            max_index = max(max_index, index)

        # Pages
        page_count = max(len(layout.slots), max_index + 1, 1)
        slots = [layout.slot(i) for i in range(page_count)]
        for i, slot in enumerate(slots):
            page = Page(slot.width_pt, slot.height_pt, background=slot.background,
                        template_is_builtin=slot.builtin)
            for kind, payload in placed.get(i, []):
                if kind == "stroke":
                    page.strokes.append(_curve_to_stroke(payload, slot))
                elif kind == "image":
                    page.images.append(self.place_image(payload, slot))
                elif kind == "text":
                    page.texts.append(self.place_text(payload, slot))
            doc.pages.append(page)

        self.read_flow_text(doc.pages[0], slots[0])
        self.report_recordings()
        return doc

    # -- images ---------------------------------------------------------------------

    def read_image(self, media: Any, origin: Optional[Tuple[float, float]]) -> Optional[Tuple[str, Dict[str, Any]]]:
        a = self.archive
        figure = a.get(media, "figure")
        snapshot = a.get(a.get(figure, "FigureBackgroundObjectKey"), "kImageObjectSnapshotKey")
        path = a.string(a.get(snapshot, "relativePath"))
        if not path:
            path = a.string(a.get(a.get(media, "contentSnapshot"), "relativePath"))
        if not path:
            self.warn("Image without a file reference skipped")
            return None
        data = self.bundle.read(path)
        if data is None and not path.startswith("Images/"):
            data = self.bundle.read("Images/" + path)
        if data is None:
            self.warn(f"Image file {path} is missing from the note; image skipped")
            return None
        fmt = _image_format(data)
        if fmt not in ("png", "jpeg"):
            self.warn(f"Image file {path} is not PNG or JPEG; image skipped")
            return None
        size = parse_point(a.string(a.get(media, "unscaledContentSize")))
        if origin is None or size is None:
            self.warn(f"Image {path} has no position or size; image skipped")
            return None
        crop = parse_rect(a.string(a.get(figure, "FigureCropRectKey")))
        pixels = _image_pixel_size(data)
        if crop is not None and pixels is not None:
            cx, cy, cw, ch = crop
            if abs(cx) > 0.5 or abs(cy) > 0.5 or abs(cw - pixels[0]) > 0.5 or abs(ch - pixels[1]) > 0.5:
                self.warn("Image crops are not applied; the full image is shown in the crop's frame")
        if a.boolean(a.get(media, "isFlippedHorizontal")) or a.boolean(a.get(media, "isFlippedVertical")):
            self.warn("Image flips are not applied")
        rotation = math.degrees(a.number(a.get(media, "rotationDegrees"), 0.0))
        return ("image", {"origin": origin, "size": size, "data": data, "fmt": fmt, "rotation": rotation})

    def place_image(self, payload: Dict[str, Any], slot: _Slot) -> Image:
        ox, oy = payload["origin"]
        w, h = payload["size"]
        s = slot.scale
        return Image(ox * s, (oy - slot.y0 - slot.top_gap) * s, w * s, h * s, payload["data"],
                     fmt=payload["fmt"], rotation=payload["rotation"])

    # -- text -----------------------------------------------------------------------

    def read_textbox(self, media: Any, origin: Optional[Tuple[float, float]]) -> Optional[Tuple[str, Dict[str, Any]]]:
        a = self.archive
        store = a.get(media, "textStore")
        text, runs = _runs_from_attributed(a, a.get(store, "attributedString"))
        size = parse_point(a.string(a.get(media, "unscaledContentSize")))
        if origin is None or size is None:
            self.warn("Text box without a position or size skipped")
            return None
        if not text:
            return None
        if a.number(a.get(media, "rotationDegrees"), 0.0):
            self.warn("Rotated text boxes are placed unrotated")
        return ("text", {"origin": origin, "size": size, "text": text, "runs": runs})

    def place_text(self, payload: Dict[str, Any], slot: _Slot) -> TextBox:
        ox, oy = payload["origin"]
        w, h = payload["size"]
        s = slot.scale
        runs: List[TextRun] = payload["runs"]
        _scale_runs(runs, s)
        first = runs[0] if runs else None
        color = first.color if first and first.color else (0.0, 0.0, 0.0, 1.0)
        size = first.size if first and first.size else 12.0 * s
        return TextBox(ox * s, (oy - slot.y0 - slot.top_gap) * s, w * s, h * s, payload["text"],
                       runs=runs, color=color, size=size)

    def read_flow_text(self, page: Page, slot: _Slot) -> None:
        a = self.archive
        text, runs = _runs_from_attributed(a, a.get(self.rich, "attributedString"))
        if not text.strip():
            return
        s = slot.scale
        _scale_runs(runs, s)
        first = runs[0] if runs else None
        size = first.size if first and first.size else 12.0 * s
        color = first.color if first and first.color else (0.0, 0.0, 0.0, 1.0)
        lines = text.count("\n") + 1
        width = max(1.0, page.width - 2 * FLOW_TEXT_MARGIN_PT)
        height = max(size * 1.4, lines * size * 1.4)
        page.texts.insert(0, TextBox(FLOW_TEXT_MARGIN_PT, FLOW_TEXT_MARGIN_PT, width, height, text,
                                     runs=runs, color=color, size=size))
        self.warn("Typed page text was placed in one text box at the top of page 1; its layout is approximated")

    # -- misc -----------------------------------------------------------------------

    def report_recordings(self) -> None:
        has_recording = False
        meta = self.bundle.read("metadata.plist")
        if meta:
            try:
                m = load_archive(meta)
                has_recording = m.boolean(m.get(m.root, "noteHasRecordingKey"))
            except Exception:  # noqa: BLE001
                has_recording = False
        if not has_recording:
            has_recording = any(n.lower().endswith((".m4a", ".mp3", ".aac", ".wav"))
                                for n in self.bundle.members("Recordings/"))
        if not has_recording:
            manager = self.archive.get(self.root, "contentPlaybackEventManager")
            has_recording = self.archive.integer(self.archive.get(manager, "NBCPTimeManagerSOANumEventsKey", 0)) > 0
        if has_recording:
            self.warn("Audio recordings and their stroke timeline are not converted")


def read_note(data: bytes) -> Document:
    """Parse a ``.note`` file given as bytes.

    Raises :class:`ValueError` when the data is not a Notability note (no ZIP, no
    ``Session.plist``).  Everything else is tolerated: unknown or damaged content is skipped
    with a line on ``Document.warnings``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_note expects bytes")
    return _Reader(bytes(data)).read()
