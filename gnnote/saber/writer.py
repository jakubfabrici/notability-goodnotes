"""Model -> Saber ``.sba`` writer (``docs/saber.md`` section 3).

The output is a ``.sba`` archive (what Saber's "Import note" and its own export use): a ZIP
with ``main.sbn2`` (BSON, format version 19) and the assets ``main.sbn2.0``, ``.1`` ...  The
document shape mirrors inkterop's Saber writer, which opened in Saber for Mac after its
round-trip output became byte-identical to the app's own ``main.sbn2``: the same top-level
keys in the same order (``v ni b p l lt z c``), pages ``{w, h[, s][, i][, q][, b]}`` without
empty lists, strokes ``{shape, p, i, ty, pe, c, ...options}``, ARGB colours as *unsigned*
integers (int64 above 2**31, as Dart's encoder stores them; the signed form crashed the app),
and the Pencil options ``sl``/``ts``/``te``.

* Pages are 1000 units wide (Saber's page width): ``k = 1000 / page width in pt`` units per pt.
* Strokes: constant widths -> ``ballpointPen`` without pressure (8-byte points, ``s`` = width);
  varying widths -> ``fountainPen`` with pressure (12-byte points) encoded through
  perfect-freehand's law ``width = s * (0.5 + pressure)`` (thinning 0.5, Saber's default): ``s``
  is the middle of the stroke's width range, which covers ratios up to 3:1 exactly (wider
  ranges keep the widest point and clamp the thinnest at a third of it); highlighters ->
  ``Highlighter`` (the model's alpha, Saber's 100/255 for an opaque one); pencil ->
  ``Pencil`` with the app's options.  ``sl`` (streamline) 0 for pens so Saber draws through the
  stored points; Bezier chains are flattened to about 1 pt.
* Images: PNG / JPEG assets with their box; PDF images as Saber PDF images (``e = ".pdf"``,
  ``pdfi`` 0).  Rotations are dropped (Saber images cannot rotate).
* Backgrounds: a page showing a PDF page gets a PDF background image (``b``, ``pdfi`` = page);
  stock paper becomes the note's one background pattern ``p`` (the most common paper).
* Text boxes become the page's Quill text (Saber has no positioned text): sorted top to bottom,
  each starting on the text line nearest its top (blank lines in between), bold / italic /
  underline kept; the note's line height ``l`` follows the median text size.
* Shape fills are dropped (Saber has no filled shapes).
"""
from __future__ import annotations

import hashlib
import io
import statistics
import struct
import time
import zipfile
from collections import Counter as _Tally
from typing import Any, Dict, List, Optional, Tuple

from .. import pdfutil
from ..codecutil import Counter, clamp_rgba, image_pixel_size, is_finite, sniff_image, stroke_polyline, to_byte
from ..model import RGBA, Document, Image, Page, Stroke, TextBox
from . import (BALLPOINT_PEN, DEFAULT_LINE_HEIGHT, DEFAULT_LINE_THICKNESS, DEFAULT_THINNING, FORMAT_VERSION,
               FOUNTAIN_PEN, HIGHLIGHTER, HIGHLIGHTER_ALPHA, MAIN_MEMBER, PAGE_WIDTH, PATTERN_FOR_PAPER, PENCIL,
               TEXT_TOP)
from .bson import encode

__all__ = ["write_saber", "build_note", "pressure_for_width"]

DEFAULT_PAGE_PT = (595.0, 833.0)  # a 1000 x 1400 page at 0.595 pt per unit
MAX_PAGE_SIDE_PT = 1e6
MAX_RATIO = (1.0 + DEFAULT_THINNING) / (1.0 - DEFAULT_THINNING)  # widest / thinnest width one stroke can hold

_MESSAGES = {
    "fills": "{n} shape fills were dropped (Saber has no filled shapes)",
    "image_rotation": "{n} image rotations were dropped (Saber images cannot be rotated)",
    "image_format": "{n} images are neither PNG, JPEG nor PDF and were skipped",
    "image_empty": "{n} images without data or with an invalid box were skipped",
    "image_pdf": "{n} PDF images could not be measured and were skipped",
    "text": "Text boxes were moved into Saber's page text (one flowing text per page in the note's text "
            "size); their horizontal position, size, colour, alignment and rotation are not kept",
    "pdf_missing": "{n} pages refer to a background PDF that is missing or unreadable; plain paper was used",
    "paper": "Saber has one paper pattern per note; {n} pages with another paper got the most common one",
    "invalid": "{n} strokes with invalid coordinates were skipped",
    "taper": "{n} strokes vary in width more than Saber's pressure range (3:1); their thinnest parts were widened",
    "page_size": "{n} pages had no valid size; Saber's default page size was used",
}


def pressure_for_width(width: float, size: float, thinning: float = DEFAULT_THINNING) -> float:
    """Inverse of perfect-freehand's ``width = size * (1 - thinning * (1 - 2 p))``, clamped to 0..1."""
    if size <= 0 or thinning <= 0:
        return 0.5
    p = (width / size - 1.0 + thinning) / (2.0 * thinning)
    return min(1.0, max(0.0, p))


def _opt(options: Any, name: str, default: Any) -> Any:
    value = getattr(options, name, None) if options is not None else None
    return default if value is None else value


def _argb(color: RGBA, alpha: Optional[int] = None) -> int:
    """Unsigned ARGB, as Saber stores colours (int64 when above 2**31)."""
    r, g, b, a = clamp_rgba(color)
    av = to_byte(a) if alpha is None else alpha
    return (av << 24) | (to_byte(r) << 16) | (to_byte(g) << 8) | to_byte(b)


class _Writer:
    def __init__(self, doc: Document, options: Any):
        self.doc = doc
        self.options = options
        self.counts = Counter(_MESSAGES)
        self.paper_mode = str(_opt(options, "paper", "plain"))
        self.assets: List[bytes] = []
        self.asset_index: Dict[str, int] = {}
        self.next_id = 0
        self.pdf_sizes: Dict[str, Optional[pdfutil.PdfInfo]] = {}
        self.line_height = DEFAULT_LINE_HEIGHT

    # -- helpers ------------------------------------------------------------------------------

    def asset(self, data: bytes) -> int:
        key = hashlib.sha1(data).hexdigest()
        index = self.asset_index.get(key)
        if index is None:
            index = len(self.assets)
            self.assets.append(bytes(data))
            self.asset_index[key] = index
        return index

    def new_id(self) -> int:
        i = self.next_id
        self.next_id += 1
        return i

    def pdf_info(self, key: str, data: bytes) -> Optional[pdfutil.PdfInfo]:
        if key not in self.pdf_sizes:
            try:
                info: Optional[pdfutil.PdfInfo] = pdfutil.pdf_info(data)
            except (ValueError, TypeError, OverflowError, RecursionError):
                info = None
            self.pdf_sizes[key] = info if info is not None and info.pages else None
        return self.pdf_sizes[key]

    # -- note-level choices ------------------------------------------------------------------------

    def carried_pdf(self, page: Page) -> Optional[str]:
        bg = page.background
        if bg is None or (page.template_is_builtin and self.paper_mode != "pdf"):
            return None
        return bg.pdf_id

    def pattern(self, pages: List[Page]) -> str:
        papers = [(p.paper or "plain").lower() for p in pages if self.carried_pdf(p) is None]
        papers = [p if p in PATTERN_FOR_PAPER else "plain" for p in papers]
        if not papers:
            return ""
        tally = _Tally(papers)
        best = max(tally, key=lambda k: (tally[k], -papers.index(k)))
        others = len(papers) - tally[best]
        if others:
            self.counts.add("paper", others)
        return PATTERN_FOR_PAPER[best]

    def choose_line_height(self, pages: List[Page]) -> int:
        sizes = []
        for page in pages:
            k = self.scale(page)
            for box in page.texts:
                size = box.runs[0].size if box.runs and box.runs[0].size else box.size
                if size and is_finite(size) and size > 0:
                    sizes.append(size * k)
        if not sizes:
            return DEFAULT_LINE_HEIGHT
        return int(min(100, max(20, round(statistics.median(sizes)))))

    @staticmethod
    def scale(page: Page) -> float:
        w = page.width if is_finite(page.width) and 0 < page.width <= MAX_PAGE_SIDE_PT else DEFAULT_PAGE_PT[0]
        return PAGE_WIDTH / w

    # -- strokes ------------------------------------------------------------------------------------

    def stroke(self, stroke: Stroke, k: float, page_index: int) -> Optional[Dict[str, Any]]:
        pts = stroke_polyline(stroke)
        if not pts:
            if stroke.points:
                self.counts.add("invalid")
            return None
        xs = [p.x * k for p in pts]
        ys = [p.y * k for p in pts]
        widths = [p.width * k for p in pts]
        if not all(is_finite(v) for v in xs + ys + widths) or not all(abs(v) < 3e38 for v in xs + ys):
            self.counts.add("invalid")
            return None
        w_min, w_max = min(widths), max(widths)
        constant = w_max - w_min <= 1e-6 * max(1.0, w_max)
        out: Dict[str, Any] = {"shape": None}
        if stroke.kind == "highlighter":
            tool, pressure = HIGHLIGHTER, False
            alpha = to_byte(clamp_rgba(stroke.color)[3])
            color = _argb(stroke.color, HIGHLIGHTER_ALPHA if alpha >= 250 else alpha)
            size = statistics.median(widths)
        elif stroke.pen == "pencil":
            tool, pressure, color = PENCIL, True, _argb(stroke.color)
            size = w_max if constant else (w_min + w_max) / 2.0
        elif constant:
            tool, pressure, color, size = BALLPOINT_PEN, False, _argb(stroke.color), w_max
        else:
            tool, pressure, color = FOUNTAIN_PEN, True, _argb(stroke.color)
            size = (w_min + w_max) / 2.0
        if pressure and not constant and w_max > MAX_RATIO * w_min * (1.0 + 1e-9):
            size = w_max / (1.0 + DEFAULT_THINNING)  # keep the widest point, clamp the thinnest
            self.counts.add("taper")
        size = max(size, 1e-3)
        if pressure:
            blobs = [struct.pack("<3f", x, y, pressure_for_width(w, size)) for x, y, w in zip(xs, ys, widths)]
        else:
            blobs = [struct.pack("<2f", x, y) for x, y in zip(xs, ys)]
        out.update({"p": blobs, "i": page_index, "ty": tool, "pe": pressure, "c": color, "s": float(size)})
        if tool == PENCIL:
            out.update({"sl": 0.1, "ts": 1.0, "te": 1.0})  # the app's Pencil options
        else:
            out["sl"] = 0.0  # draw through the stored points (no streamline lag)
        out["sp"] = False
        return out

    # -- images ---------------------------------------------------------------------------------------

    def image(self, image: Image, k: float, page_index: int) -> Optional[Dict[str, Any]]:
        data = bytes(image.data or b"")
        if not data or not (is_finite(image.x, image.y, image.w, image.h) and image.w > 0 and image.h > 0):
            self.counts.add("image_empty")
            return None
        rotation = float(image.rotation or 0.0) % 360.0
        if 1e-6 < rotation < 360.0 - 1e-6:
            self.counts.add("image_rotation")
        kind = sniff_image(data)
        entry: Dict[str, Any] = {"id": self.new_id(), "e": "", "i": page_index, "v": True, "f": 1,
                                 "x": image.x * k, "y": image.y * k, "w": image.w * k, "h": image.h * k}
        if kind in ("png", "jpeg"):
            entry["e"] = ".png" if kind == "png" else ".jpg"
            pixels = image_pixel_size(data)
            if pixels:
                entry.update({"sw": float(pixels[0]), "sh": float(pixels[1]),
                              "nw": float(pixels[0]), "nh": float(pixels[1])})
            entry["a"] = self.asset(data)
            return entry
        if kind == "pdf":
            info = self.pdf_info("image:" + hashlib.sha1(data).hexdigest(), data)
            if info is None:
                self.next_id -= 1
                self.counts.add("image_pdf")
                return None
            entry["e"] = ".pdf"
            entry.update({"nw": float(info.pages[0].width), "nh": float(info.pages[0].height),
                          "a": self.asset(data), "pdfi": 0})
            return entry
        self.next_id -= 1
        self.counts.add("image_format")
        return None

    def background(self, page: Page, k: float, page_index: int, w: float, h: float) -> Optional[Dict[str, Any]]:
        pdf_id = self.carried_pdf(page)
        if pdf_id is None:
            return None
        data = self.doc.pdfs.get(pdf_id)
        info = self.pdf_info(pdf_id, data) if data else None
        index = int(page.background.page_index) if page.background is not None else 0
        if info is None or not data or not 0 <= index < len(info.pages):
            self.counts.add("pdf_missing")
            return None
        pdf_page = info.pages[index]
        return {"id": self.new_id(), "e": ".pdf", "i": page_index, "v": True, "f": 1,
                "x": 0.0, "y": 0.0, "w": w, "h": h, "nw": float(pdf_page.width), "nh": float(pdf_page.height),
                "a": self.asset(data), "pdfi": index}

    # -- text -------------------------------------------------------------------------------------------

    def quill(self, texts: List[TextBox], k: float) -> List[Dict[str, Any]]:
        boxes = [b for b in texts if (b.text or "".join(r.text for r in b.runs)).strip() and is_finite(b.x, b.y)]
        if not boxes:
            return []
        self.counts.add("text")
        line_height = float(self.line_height)
        ops: List[Dict[str, Any]] = []
        line = 0
        for box in sorted(boxes, key=lambda b: (b.y, b.x)):
            target = int(round((box.y * k) / line_height - TEXT_TOP))
            if target > line:
                ops.append({"insert": "\n" * (target - line)})
                line = target
            runs = [r for r in box.runs if r.text]
            content = box.text or "".join(r.text for r in runs)
            if "".join(r.text for r in runs) != content:  # runs that do not spell the text: plain text
                runs = []
                ops.append({"insert": content})
            for run in runs:
                attrs = {key: True for key, on in (("bold", run.bold), ("italic", run.italic),
                                                    ("underline", run.underline)) if on}
                op: Dict[str, Any] = {"insert": run.text}
                if attrs:
                    op["attributes"] = attrs
                ops.append(op)
            if not content.endswith("\n"):
                ops.append({"insert": "\n"})
            line += content.count("\n") + (0 if content.endswith("\n") else 1)
        return ops

    # -- document ---------------------------------------------------------------------------------------

    def note(self) -> Dict[str, Any]:
        pages = list(self.doc.pages)
        if not pages:
            self.doc.warn("The document has no pages; one empty page was written")
            pages = [Page(*DEFAULT_PAGE_PT)]
        pattern = self.pattern(pages)
        self.line_height = self.choose_line_height(pages)
        out_pages: List[Dict[str, Any]] = []
        fills = 0
        for index, page in enumerate(pages):
            if not (is_finite(page.width, page.height) and 0 < page.width <= MAX_PAGE_SIDE_PT
                    and 0 < page.height <= MAX_PAGE_SIDE_PT):
                self.counts.add("page_size")
            k = self.scale(page)
            height_pt = page.height if is_finite(page.height) and 0 < page.height <= MAX_PAGE_SIDE_PT \
                else DEFAULT_PAGE_PT[1]
            w, h = PAGE_WIDTH, height_pt * k
            entry: Dict[str, Any] = {"w": float(w), "h": float(h)}
            strokes = []
            for stroke in page.strokes:
                if stroke.kind == "fill":
                    fills += 1
                    continue
                item = self.stroke(stroke, k, index)
                if item is not None:
                    strokes.append(item)
            images = [x for x in (self.image(im, k, index) for im in page.images) if x is not None]
            quill = self.quill(page.texts, k)
            background = self.background(page, k, index, w, h)
            if strokes:
                entry["s"] = strokes
            if images:
                entry["i"] = images
            if quill:
                entry["q"] = quill
            if background is not None:
                entry["b"] = background
            out_pages.append(entry)
        if fills:
            self.counts.add("fills", fills)
        self.counts.flush(self.doc)
        return {"v": FORMAT_VERSION, "ni": self.next_id, "b": None, "p": pattern, "l": int(self.line_height),
                "lt": DEFAULT_LINE_THICKNESS, "z": out_pages, "c": 0}


def build_note(doc: Document, options: Any = None) -> Tuple[bytes, List[bytes]]:
    """``(main.sbn2 BSON, assets)``; :func:`write_saber` zips them."""
    writer = _Writer(doc, options)
    note = writer.note()
    return encode(note), writer.assets


def write_saber(doc: Document, options: Any = None) -> bytes:
    """Serialise ``doc`` as a Saber ``.sba`` archive (see the module docstring).

    ``options`` is duck-typed: ``paper`` ("pdf" also carries stock-paper PDFs) and
    ``random_seed`` (fixes the ZIP timestamps, making the output reproducible) are honoured.
    Lossy steps are reported through ``doc.warn``.
    """
    main, assets = build_note(doc, options)
    stamp = (1980, 1, 1, 0, 0, 0) if _opt(options, "random_seed", None) is not None else time.localtime()[:6]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in [(MAIN_MEMBER, main)] + [(f"{MAIN_MEMBER}.{i}", a) for i, a in enumerate(assets)]:
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zf.writestr(info, data)
    return buf.getvalue()

