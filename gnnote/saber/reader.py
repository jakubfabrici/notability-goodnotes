"""Saber ``.sba`` / ``.sbn2`` / legacy ``.sbn`` -> :class:`gnnote.model.Document` (tolerant reader).

Containers (``docs/saber.md`` section 1): a ``.sba`` is a ZIP holding the note (the first
member whose name ends in ``sbn2`` or ``sbn``, normally ``main.sbn2``) and its assets
``<note>.<N>``; a ``.sbn2`` is the bare BSON note (its assets live in sibling files that are not
part of the input); a ``.sbn`` (versions up to 12) is the same structure as JSON, with inline
base64 assets.  BSON is decoded by :mod:`gnnote.saber.bson` with depth and value limits, ZIP
members are size-checked before inflating.

Mapping (section 2):

* Units: Saber pages are 1000 units wide.  A page whose background is a PDF page keeps that
  PDF page's size in pt (scale = PDF width / page width); every other page is read at
  ``PT_PER_UNIT`` = 0.595 pt per unit (a 1000-unit page is A4 wide, as Saber's own A4 import
  makes it).  Origin top-left, y down.
* Strokes: points are float32 ``(x, y[, pressure])`` (BSON binary) or ``{x, y, p}`` maps
  (JSON), plus the stroke offset ``ox`` / ``oy``.  The rendered width follows perfect-freehand
  (Saber's ink engine): ``size * (1 - thinning * (1 - 2 * pressure))`` with Saber's defaults
  ``size`` 10 and ``thinning`` 0.5, so ``size * (0.5 + pressure)``; strokes without pressure
  are ``size`` wide.  ``Highlighter`` -> highlighter, ``Pencil`` -> ``pen = "pencil"``; shape
  strokes (``circle``, ``rect``) become exact Bezier outlines.  Colours are ARGB integers.
* Images: PNG / JPEG at their ``x y w h`` box (assets by index), PDF images (page 1) as
  ``Image(fmt="pdf")``; a page background ``b`` is a PDF page (``PdfBackground``) or a raster
  fitted to the page with its Flutter ``BoxFit``.
* Page text (one Quill delta per page) -> one :class:`TextBox` per page at Saber's text
  position (top padding 1.2 x line height, side padding 0.5 x line height, font size = line
  height), bold / italic / underline runs kept (warning: layout approximated).
* The note's background pattern ``p`` -> ``Page.paper`` of every page (college / lined /
  cornell / staffs / tablature -> lined, grid -> grid, dots -> dotted); a background colour ``b``
  other than white becomes a generated paper PDF of that colour.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import pdfutil
from ..codecutil import BoundedZip, Counter, ensure_bytes, image_pixel_size, sniff_image
from ..model import RGBA, Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from . import (APPROXIMATED_PATTERNS, BOX_FIT, DEFAULT_LINE_HEIGHT, DEFAULT_PAGE_SIZE,
               DEFAULT_PRESSURE_ENABLED, DEFAULT_SIZE, DEFAULT_THINNING,
               FORMAT_VERSION, HIGHLIGHTER, PAPER_FOR_PATTERN, PEN_NAMES, PT_PER_UNIT, TEXT_SIDE,
               TEXT_TOP)
from .bson import BsonError, decode

__all__ = ["read_saber", "width_for_pressure", "MAX_NOTE_BYTES", "MAX_PAGES", "MAX_POINTS_PER_STROKE",
           "MAX_TOTAL_POINTS"]

MAX_NOTE_BYTES = 256 * 1024 * 1024  # the BSON / JSON note itself
MAX_PAGES = 10_000
MAX_POINTS_PER_STROKE = 200_000
MAX_TOTAL_POINTS = 5_000_000
MAX_PAGE_SIDE = 1e6  # units
KAPPA = 0.5522847498307936  # cubic Bezier handle length of a quarter circle
DROPPED_TOOLS = ("Eraser", "LaserPointer", "Select", "TextEditingTool")

_MESSAGES = {
    "asset_missing": "{n} images or page backgrounds are stored next to the .sbn2 file and are not part "
                     "of it (export the note as .sba to keep them)",
    "image_format": "{n} images are neither PNG, JPEG nor PDF and were skipped",
    "image_pdf_page": "{n} PDF images show a PDF page other than the first and were skipped",
    "image_crop": "{n} image crops are not applied; the full image is shown in the crop's frame",
    "image_box": "{n} images without a valid position or size were skipped",
    "text": "Saber's page text was placed in one text box per page; its layout is approximated",
    "text_embeds": "{n} embedded objects in page text (formulas, images) were dropped",
    "tools": "{n} strokes of the eraser, laser pointer or selection tools were dropped",
    "points": "{n} stroke points with invalid coordinates were dropped",
    "stroke_limit": "{n} stroke points beyond " + str(MAX_POINTS_PER_STROKE) + " per stroke were dropped",
    "total_limit": "{n} strokes beyond " + str(MAX_TOTAL_POINTS) + " points per file were dropped",
    "patterns": "The Cornell, music-staff or tablature paper pattern was approximated as lined paper",
    "page_size": "{n} pages had no valid size; Saber's default 1000 x 1400 page was used",
    "pdf_missing": "{n} PDF page backgrounds could not be read; plain paper was used",
}


def width_for_pressure(size: float, thinning: float, pressure: float) -> float:
    """perfect-freehand's stroke diameter at ``pressure`` (0..1) with identity easing."""
    p = min(1.0, max(0.0, pressure)) if math.isfinite(pressure) else 0.5
    return max(size * (1.0 - thinning * (1.0 - 2.0 * p)), size * 0.01)


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    v = float(value)
    return v if math.isfinite(v) else default


def _integer(value: Any, default: Optional[int] = None) -> Optional[int]:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    return default


def _argb(value: Any, default: RGBA = (0.0, 0.0, 0.0, 1.0)) -> RGBA:
    v = _integer(value)
    if v is None:
        return default
    v &= 0xFFFFFFFF
    return (((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0, ((v >> 24) & 255) / 255.0)


def _bezier_circle(cx: float, cy: float, r: float, w: float) -> Tuple[List[Point], List[Tuple[Point, Point]]]:
    """A full circle as four cubic arcs (clockwise in y-down coordinates, starting at 3 o'clock)."""
    k = KAPPA * r
    anchors = [(cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r), (cx + r, cy)]
    handles = [((cx + r, cy + k), (cx + k, cy + r)), ((cx - k, cy + r), (cx - r, cy + k)),
               ((cx - r, cy - k), (cx - k, cy - r)), ((cx + k, cy - r), (cx + r, cy - k))]
    return ([Point(x, y, w) for x, y in anchors],
            [(Point(a[0], a[1], w), Point(b[0], b[1], w)) for a, b in handles])


def _straight(points: Sequence[Point]) -> List[Tuple[Point, Point]]:
    """Cubic handles at the thirds of every segment: the chain draws straight sides."""
    out = []
    for a, b in zip(points, points[1:]):
        out.append((Point(a.x + (b.x - a.x) / 3.0, a.y + (b.y - a.y) / 3.0, a.width),
                    Point(a.x + 2.0 * (b.x - a.x) / 3.0, a.y + 2.0 * (b.y - a.y) / 3.0, b.width)))
    return out


def _fit(fit: str, natural: Tuple[float, float], box: Tuple[float, float]) -> Tuple[float, float, float, float]:
    """``(x, y, w, h)`` of an image of ``natural`` size placed in ``box`` with a Flutter BoxFit
    (centred, as ``Alignment.center``)."""
    iw, ih = natural
    bw, bh = box
    if fit == "fill" or iw <= 0 or ih <= 0:
        return 0.0, 0.0, bw, bh
    sx, sy = bw / iw, bh / ih
    if fit == "contain":
        s = min(sx, sy)
    elif fit == "cover":
        s = max(sx, sy)
    elif fit == "fitWidth":
        s = sx
    elif fit == "fitHeight":
        s = sy
    elif fit == "scaleDown":
        s = min(1.0, sx, sy)
    else:  # none
        s = 1.0
    w, h = iw * s, ih * s
    return (bw - w) / 2.0, (bh - h) / 2.0, w, h


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.doc = Document(title="Untitled")
        self.counts = Counter(_MESSAGES)
        self.zip: Optional[BoundedZip] = None
        self.main: str = ""
        self.inline: Optional[List[Any]] = None  # pre-19 inline assets
        self.asset_cache: Dict[int, Optional[bytes]] = {}
        self.pdf_ids: Dict[int, str] = {}
        self.pdf_infos: Dict[str, Optional[pdfutil.PdfInfo]] = {}
        self.total_points = 0
        self.paper = "plain"
        self.line_height = float(DEFAULT_LINE_HEIGHT)
        self.paper_pdfs: Dict[Tuple[float, float, str, Tuple[int, int, int]], str] = {}
        self.background_rgb: Optional[Tuple[int, int, int]] = None

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    # -- container ----------------------------------------------------------------------

    def note(self) -> Any:
        data = self.data
        if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
            self.zip = BoundedZip(data, "Saber", max_member=MAX_NOTE_BYTES)
            candidates = [n for n in self.zip.names if n.lower().endswith(("sbn", "sbn2"))]
            if not candidates:
                raise ValueError("not a Saber file: the archive holds no .sbn2 / .sbn note")
            self.main = "main.sbn2" if "main.sbn2" in candidates else candidates[0]
            body = self.zip.read(self.main)
            if body is None:
                self.zip.report(self.doc)
                raise ValueError(f"Saber archive: {self.main} could not be read (too large or damaged)")
            data = body
        if len(data) > MAX_NOTE_BYTES:
            raise ValueError(f"Saber note is larger than {MAX_NOTE_BYTES // (1024 * 1024)} MB; refused")
        framed = len(data) >= 5 and int.from_bytes(data[:4], "little") == len(data) and data[-1:] == b"\x00"
        head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
        if not framed and head in (b"{", b"["):  # a BSON length may start with "{" too
            try:
                return json.loads(data.decode("utf-8", "replace"))
            except (ValueError, RecursionError) as exc:
                raise ValueError(f"not a Saber file: unreadable JSON ({exc.__class__.__name__})") from None
        try:
            return decode(data)
        except BsonError as exc:
            raise ValueError(f"not a Saber file: {exc}") from None

    def read(self) -> Document:
        root = self.note()
        if isinstance(root, list):  # the oldest notes are a bare list of strokes
            if not any(isinstance(item, dict) and "p" in item for item in root[:100]):
                raise ValueError("not a Saber file: a JSON list without strokes")
            root = {"s": root}
        if not isinstance(root, dict):
            raise ValueError("not a Saber file: the note is not an object")
        if not any(k in root for k in ("z", "s", "v")):
            raise ValueError("not a Saber file: no pages, strokes or version")
        version = _integer(root.get("v"), 0) or 0
        if version > FORMAT_VERSION:
            self.warn(f"The note uses Saber format version {version} (newer than {FORMAT_VERSION}); "
                      "it was read on a best-effort basis")
        assets = root.get("a")
        if isinstance(assets, list):
            self.inline = assets
        pattern = root.get("p") if isinstance(root.get("p"), str) else ""
        self.paper = PAPER_FOR_PATTERN.get(pattern, "plain")
        if pattern in APPROXIMATED_PATTERNS:
            self.counts.add("patterns")
        elif pattern not in PAPER_FOR_PATTERN:
            self.warn(f"Unknown Saber paper pattern {pattern!r}; plain paper was used")
        line = _number(root.get("l"), float(DEFAULT_LINE_HEIGHT))
        self.line_height = line if 4.0 <= line <= 1000.0 else float(DEFAULT_LINE_HEIGHT)
        bg = _integer(root.get("b"))
        if bg is not None:
            r, g, b, _a = _argb(bg)
            rgb = (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))
            if rgb != (255, 255, 255):
                self.background_rgb = rgb
        pages = root.get("z")
        page_list = pages if isinstance(pages, list) else []
        if len(page_list) > MAX_PAGES:
            self.warn(f"Only the first {MAX_PAGES} pages were read")
            page_list = page_list[:MAX_PAGES]
        fallback = (_number(root.get("w"), DEFAULT_PAGE_SIZE[0]), _number(root.get("h"), DEFAULT_PAGE_SIZE[1]))
        for index, pj in enumerate(page_list):
            try:
                self.doc.pages.append(self.page(index, pj, fallback))
            except Exception as exc:  # noqa: BLE001 - tolerant reader: never fail on one page
                self.warn(f"Page {index + 1} could not be read ({exc.__class__.__name__}: {exc})")
                self.doc.pages.append(Page(DEFAULT_PAGE_SIZE[0] * PT_PER_UNIT, DEFAULT_PAGE_SIZE[1] * PT_PER_UNIT))
        self.legacy(root, fallback)
        if not self.doc.pages:
            self.doc.pages.append(self.blank_page(fallback))
        self.counts.flush(self.doc)
        if self.zip is not None:
            self.zip.report(self.doc)
        return self.doc

    # -- assets -------------------------------------------------------------------------------

    def asset(self, index: Any) -> Optional[bytes]:
        i = _integer(index)
        if i is None or i < 0:
            return None
        if i in self.asset_cache:
            return self.asset_cache[i]
        data: Optional[bytes] = None
        if self.inline is not None:
            if i < len(self.inline):
                data = self.inline_bytes(self.inline[i])
        elif self.zip is not None:
            data = self.zip.read(f"{self.main}.{i}")
        else:
            self.counts.add("asset_missing")
        self.asset_cache[i] = data
        return data

    @staticmethod
    def inline_bytes(value: Any) -> Optional[bytes]:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            try:
                return base64.b64decode(value, validate=False)
            except (binascii.Error, ValueError):
                return None
        if isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 256
                                           for v in value):
            return bytes(value)
        return None

    def image_bytes(self, ij: Dict[str, Any]) -> Optional[bytes]:
        if "a" in ij:
            return self.asset(ij.get("a"))
        if "b" in ij:  # notes before version 11 inline the bytes
            return self.inline_bytes(ij.get("b"))
        return None

    def pdf_page_size(self, pdf_id: str, data: bytes, page: int) -> Optional[Tuple[float, float]]:
        if pdf_id not in self.pdf_infos:
            try:
                self.pdf_infos[pdf_id] = pdfutil.pdf_info(data)
            except (ValueError, TypeError, OverflowError, RecursionError):
                self.pdf_infos[pdf_id] = None
        info = self.pdf_infos[pdf_id]
        if info is None or not 0 <= page < len(info.pages):
            return None
        return info.pages[page].width, info.pages[page].height

    def pdf_id(self, index: Any, data: bytes) -> str:
        i = _integer(index, -1)
        key = i if i is not None and i >= 0 else -1 - len(self.pdf_ids)
        pid = self.pdf_ids.get(key)
        if pid is None:
            pid = f"saber-asset-{i}.pdf" if key >= 0 else f"saber-inline-{-key}.pdf"
            self.pdf_ids[key] = pid
            self.doc.pdfs[pid] = data
        return pid

    # -- pages ------------------------------------------------------------------------------------

    def size(self, pj: Dict[str, Any], fallback: Tuple[float, float]) -> Tuple[float, float]:
        w = _number(pj.get("w"), fallback[0])
        h = _number(pj.get("h"), fallback[1])
        if not (0 < w <= MAX_PAGE_SIDE and 0 < h <= MAX_PAGE_SIDE):
            self.counts.add("page_size")
            return DEFAULT_PAGE_SIZE
        return w, h

    def blank_page(self, fallback: Tuple[float, float]) -> Page:
        w, h = self.size({}, fallback)
        page = Page(w * PT_PER_UNIT, h * PT_PER_UNIT, paper=self.paper)
        self.coloured_paper(page)
        return page

    def coloured_paper(self, page: Page) -> None:
        if self.background_rgb is None or page.background is not None:
            return
        key = (round(page.width, 3), round(page.height, 3), page.paper, self.background_rgb)
        pdf_id = self.paper_pdfs.get(key)
        if pdf_id is None:
            pdf_id = f"saber-paper-{len(self.paper_pdfs) + 1}.pdf"
            self.doc.pdfs[pdf_id] = pdfutil.make_paper_pdf(page.width, page.height, page.paper,
                                                           color=tuple(c / 255.0 for c in self.background_rgb))
            self.paper_pdfs[key] = pdf_id
        page.background = PdfBackground(pdf_id, 0)
        page.template_is_builtin = True

    def page(self, index: int, pj: Any, fallback: Tuple[float, float]) -> Page:
        if isinstance(pj, list) and len(pj) >= 2:  # very old notes: [width, height]
            pj = {"w": pj[0], "h": pj[1]}
        if not isinstance(pj, dict):
            pj = {}
        w, h = self.size(pj, fallback)
        scale = PT_PER_UNIT
        background: Optional[PdfBackground] = None
        raster_bg: Optional[Tuple[bytes, str, Dict[str, Any]]] = None
        bg = pj.get("b")
        if isinstance(bg, dict):
            data = self.image_bytes(bg)
            if data:
                kind = sniff_image(data)
                if kind == "pdf":
                    pdf_page = _integer(bg.get("pdfi"), 0) or 0
                    pid = self.pdf_id(bg.get("a"), data)
                    size = self.pdf_page_size(pid, data, pdf_page)
                    if size is None:
                        size = (_number(bg.get("nw"), 0.0), _number(bg.get("nh"), 0.0))
                    if size[0] > 0 and size[1] > 0:
                        scale = size[0] / w
                        background = PdfBackground(pid, pdf_page)
                    else:
                        self.counts.add("pdf_missing")
                elif kind in ("png", "jpeg"):
                    raster_bg = (data, kind, bg)
                else:
                    self.counts.add("image_format")
        page = Page(w * scale, h * scale, paper=self.paper)
        if background is not None:
            page.background = background
        else:
            self.coloured_paper(page)
        if raster_bg is not None:
            data, kind, bg = raster_bg
            pixels = image_pixel_size(data)
            natural = (_number(bg.get("nw"), float(pixels[0]) if pixels else 0.0),
                       _number(bg.get("nh"), float(pixels[1]) if pixels else 0.0))
            fit_index = _integer(bg.get("f"), 1)
            fit = BOX_FIT[fit_index] if fit_index is not None and 0 <= fit_index < len(BOX_FIT) else "contain"
            x, y, bw, bh = _fit(fit, natural, (w, h))
            page.images.append(Image(x * scale, y * scale, bw * scale, bh * scale, data, fmt=kind))
        for sj in pj.get("s") or []:
            if isinstance(sj, dict):
                self.stroke(page, sj, scale)
        for ij in pj.get("i") or []:
            if isinstance(ij, dict):
                self.image(page, ij, scale)
        quill = pj.get("q")
        if isinstance(quill, list):
            self.text(page, quill, scale)
        return page

    def legacy(self, root: Dict[str, Any], fallback: Tuple[float, float]) -> None:
        """Notes before version 8 keep strokes and images at the top level with a page index."""
        for key in ("s", "i"):
            items = root.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                index = _integer(item.get("i"), 0) or 0
                if not 0 <= index < MAX_PAGES:
                    continue
                while len(self.doc.pages) <= index:
                    self.doc.pages.append(self.blank_page(fallback))
                page = self.doc.pages[index]
                scale = PT_PER_UNIT
                if key == "s":
                    self.stroke(page, item, scale)
                else:
                    self.image(page, item, scale)

    # -- strokes -------------------------------------------------------------------------------------

    def raw_points(self, sj: Dict[str, Any]) -> List[Tuple[float, float, Optional[float]]]:
        ox = _number(sj.get("ox"), 0.0)
        oy = _number(sj.get("oy"), 0.0)
        out: List[Tuple[float, float, Optional[float]]] = []
        raw = sj.get("p") if isinstance(sj.get("p"), list) else []
        if len(raw) > MAX_POINTS_PER_STROKE:
            self.counts.add("stroke_limit", len(raw) - MAX_POINTS_PER_STROKE)
            raw = raw[:MAX_POINTS_PER_STROKE]
        for p in raw:
            if isinstance(p, (bytes, bytearray)):
                n = len(p) // 4
                if n < 2:
                    continue
                values = struct.unpack_from(f"<{min(n, 3)}f", p)
                pressure = values[2] if n >= 3 else None
                out.append((values[0] + ox, values[1] + oy, pressure))
            elif isinstance(p, dict):
                x, y = p.get("x"), p.get("y")
                if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
                        and not isinstance(x, bool) and not isinstance(y, bool):
                    pr = p.get("p")
                    pressure = float(pr) if isinstance(pr, (int, float)) and not isinstance(pr, bool) else None
                    out.append((float(x) + ox, float(y) + oy, pressure))
        return out

    def stroke(self, page: Page, sj: Dict[str, Any], scale: float) -> None:
        tool = sj.get("ty") if isinstance(sj.get("ty"), str) else "fountainPen"
        if tool in DROPPED_TOOLS:
            self.counts.add("tools")
            return
        color = _argb(sj.get("c")) if "c" in sj else (0.0, 0.0, 0.0, 1.0)
        size = _number(sj.get("s"), DEFAULT_SIZE)
        if size <= 0:
            size = DEFAULT_SIZE
        thinning = _number(sj.get("t"), DEFAULT_THINNING)
        pressure_enabled = sj.get("pe", DEFAULT_PRESSURE_ENABLED) is not False
        kind = "highlighter" if tool == HIGHLIGHTER else "pen"
        pen = PEN_NAMES.get(tool)
        width = size * scale
        shape = sj.get("shape")
        if shape == "circle":
            cx, cy = _number(sj.get("cx"), 0.0) * scale, _number(sj.get("cy"), 0.0) * scale
            r = _number(sj.get("r"), 0.0) * scale
            if r > 0:
                anchors, controls = _bezier_circle(cx, cy, r, width)
                page.strokes.append(Stroke(anchors, color=color, kind=kind, pen=pen, width=width, controls=controls))
            return
        if shape == "rect":
            x0, y0 = _number(sj.get("rl"), 0.0) * scale, _number(sj.get("rt"), 0.0) * scale
            rw, rh = _number(sj.get("rw"), 0.0) * scale, _number(sj.get("rh"), 0.0) * scale
            if rw > 0 or rh > 0:
                corners = [Point(x0, y0, width), Point(x0 + rw, y0, width), Point(x0 + rw, y0 + rh, width),
                           Point(x0, y0 + rh, width), Point(x0, y0, width)]
                page.strokes.append(Stroke(corners, color=color, kind=kind, pen=pen, width=width,
                                           controls=_straight(corners)))
            return
        raw = self.raw_points(sj)
        if self.total_points + len(raw) > MAX_TOTAL_POINTS:
            self.counts.add("total_limit")
            return
        self.total_points += len(raw)
        # Without stored pressure (pressure disabled, or simulated from the drawing speed when
        # "sp" is true) a stroke is read at its nominal size: the speed is not recorded.
        points: List[Point] = []
        for x, y, pressure in raw:
            if not (math.isfinite(x) and math.isfinite(y)):
                self.counts.add("points")
                continue
            if pressure_enabled and pressure is not None:
                w = width_for_pressure(size, thinning, pressure)
            else:
                w = size  # no pressure (or simulated from speed): the nominal size
            points.append(Point(x * scale, y * scale, w * scale))
        if points:
            page.strokes.append(Stroke(points, color=color, kind=kind, pen=pen, width=width))

    # -- images ----------------------------------------------------------------------------------------

    def image(self, page: Page, ij: Dict[str, Any], scale: float) -> None:
        x, y = _number(ij.get("x"), float("nan")), _number(ij.get("y"), float("nan"))
        w, h = _number(ij.get("w"), 0.0), _number(ij.get("h"), 0.0)
        if not (math.isfinite(x) and math.isfinite(y)) or w <= 0 or h <= 0:
            self.counts.add("image_box")
            return
        data = self.image_bytes(ij)
        if not data:
            return  # missing assets are counted where they are looked up
        kind = sniff_image(data)
        if kind == "pdf":
            if (_integer(ij.get("pdfi"), 0) or 0) != 0:
                self.counts.add("image_pdf_page")
                return
        elif kind not in ("png", "jpeg"):
            self.counts.add("image_format")
            return
        nw, nh = _number(ij.get("nw"), 0.0), _number(ij.get("nh"), 0.0)
        sx, sy = _number(ij.get("sx"), 0.0), _number(ij.get("sy"), 0.0)
        sw, sh = _number(ij.get("sw"), 0.0), _number(ij.get("sh"), 0.0)
        if (sx or sy) or (sw and nw and abs(sw - nw) > 0.5) or (sh and nh and abs(sh - nh) > 0.5):
            self.counts.add("image_crop")
        page.images.append(Image(x * scale, y * scale, w * scale, h * scale, data, fmt=kind))

    # -- text ------------------------------------------------------------------------------------------

    def text(self, page: Page, ops: List[Any], scale: float) -> None:
        runs: List[TextRun] = []
        line_height = self.line_height
        size = line_height * scale
        for op in ops:
            if not isinstance(op, dict):
                continue
            insert = op.get("insert")
            if not isinstance(insert, str):
                if insert is not None:
                    self.counts.add("text_embeds")
                continue
            attrs = op.get("attributes") if isinstance(op.get("attributes"), dict) else {}
            bold = bool(attrs.get("bold")) or attrs.get("header") is not None
            runs.append(TextRun(insert, bold=bold, italic=bool(attrs.get("italic")),
                                underline=bool(attrs.get("underline")), size=size, color=(0.0, 0.0, 0.0, 1.0)))
        text = "".join(r.text for r in runs)
        stripped = text.lstrip("\n")
        leading = len(text) - len(stripped)
        body = stripped.rstrip("\n")
        if not body.strip():
            return
        # drop the leading blank lines and the trailing newline(s) from the runs
        trimmed: List[TextRun] = []
        skip, keep = leading, len(body)
        for run in runs:
            t = run.text
            cut = min(skip, len(t))
            t, skip = t[cut:], skip - cut
            t = t[:keep]
            keep -= len(t)
            if t:
                trimmed.append(TextRun(t, run.bold, run.italic, run.underline, run.font, run.size, run.color))
        lines = body.count("\n") + 1
        self.counts.add("text")
        page.texts.append(TextBox(TEXT_SIDE * line_height * scale, (TEXT_TOP + leading) * line_height * scale,
                                  max(1.0, page.width - 2 * TEXT_SIDE * line_height * scale),
                                  lines * line_height * scale, body, runs=trimmed, size=size))


def read_saber(data: bytes) -> Document:
    """Parse a Saber note (``.sba`` archive, ``.sbn2`` BSON or legacy ``.sbn`` JSON) given as bytes.

    Raises :class:`ValueError` when the data is not a Saber note; everything else that is
    damaged or unsupported becomes a line on ``Document.warnings``.
    """
    return _Reader(ensure_bytes(data, "read_saber")).read()
