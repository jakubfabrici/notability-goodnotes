"""Excalidraw ``.excalidraw`` -> :class:`gnnote.model.Document` (tolerant reader).

Pages (``docs/excalidraw.md`` section 2): every frame (``frame`` / ``magicframe``) is a page,
ordered top to bottom then left to right, holding the elements whose ``frameId`` names it (or,
without one, whose centre lies inside it).  Elements outside every frame, or every element of
a scene without frames, form one more page: their bounding box plus a 20 px margin.  Units:
CSS px x 0.75 = pt, relative to the page's top-left corner; ``angle`` (radians, clockwise about
the element's centre) is applied to the geometry, or kept as the rotation of images and texts.

Elements:

* ``freedraw`` -> a polyline stroke; widths from the freedraw law (``thickness_factor`` of each
  pressure x ``strokeWidth``; speed-simulated pressure: 6.9 x ``strokeWidth``), colour
  ``strokeColor`` with ``opacity``.  ``customData.gnnote`` (written by gnnote) restores the
  highlighter kind and pen name.
* ``line`` / ``arrow`` -> polyline strokes of width ``strokeWidth`` (arrowheads dropped);
  ``rectangle`` / ``diamond`` / ``stickynote`` / ``ellipse`` -> outlines (straight cubic sides,
  four cubic arcs for an ellipse).  A ``backgroundColor`` on a closed shape adds a shape fill
  (``Stroke(kind="fill")``) right after the outline.
* ``text`` -> :class:`TextBox` (``originalText``, size, colour, font, alignment; the rotation
  is moved from the box centre to the model's top-left pivot); ``image`` -> :class:`Image`
  (PNG / JPEG from the ``files`` data URLs).
* Deleted elements are ignored; embeds and unknown types are dropped (one warning).
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .. import pdfutil
from ..codecutil import MAX_SIZE_PT, Counter, ensure_bytes, sniff_image
from ..model import RGBA, Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from . import DEFAULT_STROKE_COLOR, FONT_NAMES, LINE_HEIGHT, PT_PER_PX, SIMULATED_FACTOR, thickness_factor

__all__ = ["read_excalidraw", "MAX_SCENE_BYTES", "MAX_ELEMENTS", "MAX_POINTS_PER_ELEMENT", "MAX_TOTAL_POINTS",
           "MAX_FILE_BYTES", "MAX_FILES_BYTES"]

MAX_SCENE_BYTES = 256 * 1024 * 1024
MAX_ELEMENTS = 500_000
MAX_POINTS_PER_ELEMENT = 200_000
MAX_TOTAL_POINTS = 5_000_000
MAX_FILE_BYTES = 256 * 1024 * 1024  # one decoded image
MAX_FILES_BYTES = 1024 * 1024 * 1024  # all decoded images
MAX_PAGES = 10_000
MAX_COORD = 1e7  # px; coordinates beyond this are damage
MAX_SIZE = MAX_SIZE_PT / PT_PER_PX  # px; stroke widths and font sizes are clamped to this
PAGE_MARGIN_PX = 20.0
FRAME_TYPES = ("frame", "magicframe")
CLOSED_SHAPES = ("rectangle", "diamond", "ellipse", "stickynote")
DROPPED_TYPES = ("embeddable", "iframe", "selection", "laser")
KAPPA = 0.5522847498307936
DEFAULT_PAGE_PX = (595.28 / PT_PER_PX, 841.89 / PT_PER_PX)
_HEX_RE = re.compile(r"#?([0-9a-fA-F]{3,8})")

_MESSAGES = {
    "unsupported": "{n} elements of an unsupported kind (embeds, web frames) were dropped",
    "arrowheads": "{n} arrowheads were dropped (the arrow lines are kept)",
    "rough": "{n} hand-drawn (rough) or rounded shapes and lines are drawn as clean geometry",
    "hatched": "{n} hatched or zigzag shape fills became solid fills",
    "dashed": "{n} dashed or dotted lines are drawn solid",
    "image_format": "{n} images are neither PNG nor JPEG (or their data is missing) and were skipped",
    "image_crop": "{n} image crops or flips are not applied",
    "invalid": "{n} elements with invalid coordinates were skipped",
    "points": "{n} points beyond " + str(MAX_POINTS_PER_ELEMENT) + " per element were dropped",
    "total_limit": "{n} elements beyond " + str(MAX_TOTAL_POINTS) + " points per file were dropped",
    "color": "{n} unknown colours were replaced by black",
    "text_empty": "{n} empty text elements were skipped",
}


def _num(value: Any, default: float = 0.0) -> float:
    """A finite JSON number as float; ``default`` for anything else (an integer too large for
    a float included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        v = float(value)
    except OverflowError:
        return default
    return v if math.isfinite(v) else default


def _angle(el: Dict[str, Any]) -> float:
    """The element's ``angle`` (radians) reduced to one turn."""
    return math.fmod(_num(el.get("angle")), 2.0 * math.pi)


def _size(value: Any, default: float) -> float:
    """A positive stroke width or font size in px (``default`` when missing or not positive),
    clamped to ``MAX_SIZE``."""
    v = _num(value, default)
    return min(v if v > 0 else default, MAX_SIZE)


def _coord(value: Any) -> float:
    """A coordinate in px: finite and within ``MAX_COORD``, else NaN."""
    v = _num(value, math.nan)
    return v if abs(v) <= MAX_COORD else math.nan


def parse_color(value: Any) -> Optional[Tuple[float, float, float, float]]:
    """``#rgb`` / ``#rrggbb`` / ``#rrggbbaa`` -> RGBA; ``None`` for ``transparent`` / unknown."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower() in ("", "transparent", "none"):
        return None
    m = _HEX_RE.fullmatch(text)
    if not m:
        return None
    digits = m.group(1)
    if len(digits) in (3, 4):
        digits = "".join(c * 2 for c in digits)
    if len(digits) == 6:
        digits += "ff"
    if len(digits) != 8:
        return None
    v = int(digits, 16)
    return ((v >> 24) & 255) / 255.0, ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0


def _rotate(x: float, y: float, cx: float, cy: float, cos_t: float, sin_t: float) -> Tuple[float, float]:
    dx, dy = x - cx, y - cy
    return cx + dx * cos_t - dy * sin_t, cy + dx * sin_t + dy * cos_t


class _PageSpec:
    def __init__(self, x: float, y: float, w: float, h: float):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.elements: List[Dict[str, Any]] = []

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.doc = Document(title="Untitled")
        self.counts = Counter(_MESSAGES)
        self.files: Dict[str, Any] = {}
        self.file_cache: Dict[str, Optional[bytes]] = {}
        self.file_budget = MAX_FILES_BYTES
        self.total_points = 0
        self.paper_rgb: Optional[Tuple[int, int, int]] = None

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    # -- scene ------------------------------------------------------------------------------

    def scene(self) -> Dict[str, Any]:
        if len(self.data) > MAX_SCENE_BYTES:
            raise ValueError(f"Excalidraw file is larger than {MAX_SCENE_BYTES // (1024 * 1024)} MB; refused")
        try:
            scene = json.loads(self.data.decode("utf-8-sig", "replace"))
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"not an Excalidraw file: unreadable JSON ({exc.__class__.__name__})") from None
        if not isinstance(scene, dict) or scene.get("type") not in ("excalidraw", "excalidraw/clipboard"):
            raise ValueError('not an Excalidraw file: no "type": "excalidraw"')
        if not isinstance(scene.get("elements", []), list):
            raise ValueError("not an Excalidraw file: elements is not a list")
        return scene

    def read(self) -> Document:
        scene = self.scene()
        files = scene.get("files")
        self.files = files if isinstance(files, dict) else {}
        app = scene.get("appState") if isinstance(scene.get("appState"), dict) else {}
        background = parse_color(app.get("viewBackgroundColor"))
        if background is not None:
            rgb = tuple(int(round(c * 255)) for c in background[:3])
            if rgb != (255, 255, 255):
                self.paper_rgb = rgb  # type: ignore[assignment]
        elements = [e for e in scene.get("elements") or [] if isinstance(e, dict) and e.get("isDeleted") is not True]
        if len(elements) > MAX_ELEMENTS:
            self.warn(f"Only the first {MAX_ELEMENTS} elements were read")
            elements = elements[:MAX_ELEMENTS]
        pages = self.layout(elements)
        for spec in pages:
            page = Page(spec.w * PT_PER_PX, spec.h * PT_PER_PX)
            self.paper(page)
            for element in spec.elements:
                try:
                    self.element(page, spec, element)
                except Exception as exc:  # noqa: BLE001 - tolerant reader: never fail on one element
                    self.warn(f"An element of type {str(element.get('type'))[:30]!r} was skipped "
                              f"({exc.__class__.__name__}: {exc})")
            self.doc.pages.append(page)
        self.counts.flush(self.doc)
        return self.doc

    def paper(self, page: Page) -> None:
        if self.paper_rgb is None:
            return
        pid = f"excalidraw-paper-{round(page.width, 2)}x{round(page.height, 2)}.pdf"
        if pid not in self.doc.pdfs:
            self.doc.pdfs[pid] = pdfutil.make_paper_pdf(page.width, page.height, "plain",
                                                        color=tuple(c / 255.0 for c in self.paper_rgb))
        page.background = PdfBackground(pid, 0)
        page.template_is_builtin = True

    # -- layout -----------------------------------------------------------------------------

    def bounds(self, el: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
        """Unrotated bounding box ``(x0, y0, x1, y1)`` in px; ``None`` when invalid."""
        x, y = _coord(el.get("x")), _coord(el.get("y"))
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        points = el.get("points")
        if el.get("type") in ("freedraw", "line", "arrow") and isinstance(points, list) and points:
            xs, ys = [], []
            for p in points[:MAX_POINTS_PER_ELEMENT]:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    px, py = _coord(p[0]), _coord(p[1])
                    if math.isfinite(px) and math.isfinite(py):
                        xs.append(px)
                        ys.append(py)
            if not xs:
                return None
            return x + min(xs), y + min(ys), x + max(xs), y + max(ys)
        w, h = _coord(el.get("width")), _coord(el.get("height"))
        w = w if math.isfinite(w) else 0.0
        h = h if math.isfinite(h) else 0.0
        return x, y, x + max(0.0, w), y + max(0.0, h)

    def layout(self, elements: List[Dict[str, Any]]) -> List[_PageSpec]:
        frames: List[Tuple[Dict[str, Any], _PageSpec]] = []
        for el in elements:
            if el.get("type") in FRAME_TYPES:
                box = self.bounds(el)
                if box is not None and box[2] - box[0] > 1 and box[3] - box[1] > 1:
                    frames.append((el, _PageSpec(box[0], box[1], box[2] - box[0], box[3] - box[1])))
        frames.sort(key=lambda f: (round(f[1].y, 3), round(f[1].x, 3)))
        frames = frames[:MAX_PAGES - 1]
        by_id = {str(f[0].get("id")): f[1] for f in frames}
        loose: List[Dict[str, Any]] = []
        for el in elements:
            if el.get("type") in FRAME_TYPES:
                continue
            spec = by_id.get(str(el.get("frameId"))) if el.get("frameId") is not None else None
            if spec is None and frames:
                box = self.bounds(el)
                if box is not None:
                    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
                    spec = next((s for _f, s in frames if s.contains(cx, cy)), None)
            if spec is None:
                loose.append(el)
            else:
                spec.elements.append(el)
        pages = [s for _f, s in frames]
        if loose or not pages:
            boxes = [b for b in (self.bounds(el) for el in loose) if b is not None]
            if boxes:
                x0 = min(b[0] for b in boxes) - PAGE_MARGIN_PX
                y0 = min(b[1] for b in boxes) - PAGE_MARGIN_PX
                x1 = max(b[2] for b in boxes) + PAGE_MARGIN_PX
                y1 = max(b[3] for b in boxes) + PAGE_MARGIN_PX
                spec = _PageSpec(x0, y0, max(x1 - x0, 1.0), max(y1 - y0, 1.0))
            else:
                spec = _PageSpec(0.0, 0.0, *DEFAULT_PAGE_PX)
            spec.elements = loose
            pages.append(spec)
        return pages

    # -- elements -----------------------------------------------------------------------------

    def style(self, el: Dict[str, Any], key: str = "strokeColor") -> Optional[RGBA]:
        raw = el.get(key)
        if raw is None and key == "strokeColor":
            raw = DEFAULT_STROKE_COLOR  # what Excalidraw's restore() fills in
        color = parse_color(raw)
        if color is None:
            if isinstance(raw, str) and raw.strip().lower() not in ("", "transparent", "none"):
                self.counts.add("color")
                color = (0.0, 0.0, 0.0, 1.0)
            else:
                return None
        opacity = min(100.0, max(0.0, _num(el.get("opacity"), 100.0))) / 100.0
        return color[0], color[1], color[2], color[3] * opacity

    def element(self, page: Page, spec: _PageSpec, el: Dict[str, Any]) -> None:
        kind = el.get("type")
        if kind in ("freedraw", "line", "arrow"):
            self.linear(page, spec, el)
        elif kind in CLOSED_SHAPES:
            self.shape(page, spec, el)
        elif kind == "text":
            self.text(page, spec, el)
        elif kind == "image":
            self.image(page, spec, el)
        elif kind not in FRAME_TYPES:
            self.counts.add("unsupported")

    def transform(self, spec: _PageSpec, el: Dict[str, Any], cx: float, cy: float):
        """A function mapping absolute px to page pt with the element's rotation about (cx, cy)."""
        angle = _angle(el)
        cos_t, sin_t = math.cos(angle), math.sin(angle)

        def to_page(x: float, y: float) -> Tuple[float, float]:
            if angle:
                x, y = _rotate(x, y, cx, cy, cos_t, sin_t)
            return (x - spec.x) * PT_PER_PX, (y - spec.y) * PT_PER_PX
        return to_page

    def linear(self, page: Page, spec: _PageSpec, el: Dict[str, Any]) -> None:
        box = self.bounds(el)
        raw = el.get("points")
        if box is None or not isinstance(raw, list) or not raw:
            self.counts.add("invalid")
            return
        if len(raw) > MAX_POINTS_PER_ELEMENT:
            self.counts.add("points", len(raw) - MAX_POINTS_PER_ELEMENT)
            raw = raw[:MAX_POINTS_PER_ELEMENT]
        if self.total_points + len(raw) > MAX_TOTAL_POINTS:
            self.counts.add("total_limit")
            return
        self.total_points += len(raw)
        x0, y0 = _num(el.get("x")), _num(el.get("y"))
        to_page = self.transform(spec, el, (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        stroke_width = _size(el.get("strokeWidth"), 1.0)
        kind = el.get("type")
        widths: List[float]
        pressures = el.get("pressures")
        if kind == "freedraw":
            options = el.get("strokeOptions") if isinstance(el.get("strokeOptions"), dict) else {}
            if isinstance(pressures, list) and len(pressures) == len(raw) and el.get("simulatePressure") is not True \
                    and options.get("variability") != "constant":
                widths = [stroke_width * thickness_factor(_num(p, 0.5)) for p in pressures]
            elif el.get("simulatePressure") is True and options.get("variability") != "constant":
                widths = [stroke_width * SIMULATED_FACTOR] * len(raw)
            else:
                widths = [stroke_width * thickness_factor(0.5)] * len(raw)
        else:
            widths = [stroke_width] * len(raw)
            if el.get("strokeStyle") in ("dashed", "dotted"):
                self.counts.add("dashed")
            if _num(el.get("roughness")) > 0 or el.get("roundness"):
                self.counts.add("rough")
            if kind == "arrow" and (el.get("startArrowhead") or el.get("endArrowhead", "arrow")):
                self.counts.add("arrowheads")
        widths = [min(w, MAX_SIZE) for w in widths]
        points: List[Point] = []
        for p, w in zip(raw, widths):
            if not (isinstance(p, (list, tuple)) and len(p) >= 2):
                continue
            px, py = _coord(p[0]), _coord(p[1])
            if not (math.isfinite(px) and math.isfinite(py)):
                continue
            x, y = to_page(x0 + px, y0 + py)
            points.append(Point(x, y, w * PT_PER_PX))
        if not points:
            self.counts.add("invalid")
            return
        color = self.style(el)
        custom = el.get("customData") if isinstance(el.get("customData"), dict) else {}
        hints = custom.get("gnnote") if isinstance(custom.get("gnnote"), dict) else {}
        stroke_kind = "highlighter" if hints.get("kind") == "highlighter" else "pen"
        pen = hints.get("pen") if isinstance(hints.get("pen"), str) else None
        nominal = (max(widths) if widths else stroke_width) * PT_PER_PX
        if color is not None:
            page.strokes.append(Stroke(points, color=color, kind=stroke_kind, pen=pen, width=nominal))
        explicit = (points[0].x, points[0].y) == (points[-1].x, points[-1].y)
        if kind == "line" and len(points) >= 3 and (explicit or el.get("polygon") is True):
            self.fill(page, el, points[:-1] if explicit else points)

    def fill(self, page: Page, el: Dict[str, Any], polygon: List[Point]) -> None:
        color = self.style(el, "backgroundColor")
        if color is None or len(polygon) < 3:
            return
        if el.get("fillStyle") not in (None, "solid"):
            self.counts.add("hatched")
        ring = [Point(p.x, p.y, 0.0) for p in polygon]
        page.strokes.append(Stroke(list(ring), color=color, kind="fill", width=0.0, outline=[ring]))

    def shape(self, page: Page, spec: _PageSpec, el: Dict[str, Any]) -> None:
        box = self.bounds(el)
        if box is None:
            self.counts.add("invalid")
            return
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        to_page = self.transform(spec, el, cx, cy)
        width = _size(el.get("strokeWidth"), 1.0) * PT_PER_PX
        if el.get("strokeStyle") in ("dashed", "dotted"):
            self.counts.add("dashed")
        if _num(el.get("roughness")) > 0 or el.get("roundness"):
            self.counts.add("rough")
        kind = el.get("type")
        controls: List[Tuple[Point, Point]] = []
        if kind == "ellipse":
            rx, ry = (x1 - x0) / 2.0, (y1 - y0) / 2.0
            kx, ky = KAPPA * rx, KAPPA * ry
            raw = [(cx + rx, cy), (cx, cy + ry), (cx - rx, cy), (cx, cy - ry), (cx + rx, cy)]
            handles = [((cx + rx, cy + ky), (cx + kx, cy + ry)), ((cx - kx, cy + ry), (cx - rx, cy + ky)),
                       ((cx - rx, cy - ky), (cx - kx, cy - ry)), ((cx + kx, cy - ry), (cx + rx, cy - ky))]
            anchors = [Point(*to_page(x, y), width) for x, y in raw]
            controls = [(Point(*to_page(*a), width), Point(*to_page(*b), width)) for a, b in handles]
            samples = [to_page(cx + rx * math.cos(2 * math.pi * i / 64), cy + ry * math.sin(2 * math.pi * i / 64))
                       for i in range(64)]
            polygon = [Point(x, y, 0.0) for x, y in samples]
        else:
            if kind == "diamond":
                raw = [(cx, y0), (x1, cy), (cx, y1), (x0, cy), (cx, y0)]
            else:
                raw = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            anchors = [Point(*to_page(x, y), width) for x, y in raw]
            for a, b in zip(anchors, anchors[1:]):
                controls.append((Point(a.x + (b.x - a.x) / 3.0, a.y + (b.y - a.y) / 3.0, width),
                                 Point(a.x + 2 * (b.x - a.x) / 3.0, a.y + 2 * (b.y - a.y) / 3.0, width)))
            polygon = [Point(p.x, p.y, 0.0) for p in anchors[:-1]]
        color = self.style(el)
        if color is not None:
            page.strokes.append(Stroke(anchors, color=color, width=width, controls=controls))
        self.fill(page, el, polygon)

    # -- text and images ----------------------------------------------------------------------

    def text(self, page: Page, spec: _PageSpec, el: Dict[str, Any]) -> None:
        content = el.get("originalText") if isinstance(el.get("originalText"), str) and el.get("originalText") \
            else el.get("text")
        if not isinstance(content, str) or not content.strip():
            self.counts.add("text_empty")
            return
        box = self.bounds(el)
        if box is None:
            self.counts.add("invalid")
            return
        x0, y0, x1, y1 = box
        size_px = _size(el.get("fontSize"), 20.0)
        line_height = _num(el.get("lineHeight"), LINE_HEIGHT)
        if not 0 < line_height <= 10:
            line_height = LINE_HEIGHT
        lines = content.count("\n") + 1
        w = max(x1 - x0, 1.0)
        h = min(max(y1 - y0, lines * size_px * line_height), MAX_COORD)
        angle = _angle(el)
        cx, cy = x0 + w / 2.0, y0 + h / 2.0
        # Excalidraw turns the box about its centre, the model about its top-left corner: the
        # model's corner is where the centre rotation puts Excalidraw's corner.
        tx, ty = _rotate(x0, y0, cx, cy, math.cos(angle), math.sin(angle)) if angle else (x0, y0)
        color = self.style(el) or (0.0, 0.0, 0.0, 1.0)
        family = el.get("fontFamily")
        font = FONT_NAMES.get(family if isinstance(family, int) and not isinstance(family, bool) else -1, "Helvetica")
        size = size_px * PT_PER_PX
        align = el.get("textAlign") if el.get("textAlign") in ("left", "center", "right") else "left"
        page.texts.append(TextBox((tx - spec.x) * PT_PER_PX, (ty - spec.y) * PT_PER_PX, w * PT_PER_PX, h * PT_PER_PX,
                                  content, runs=[TextRun(content, font=font, size=size, color=color)],
                                  color=color, size=size, rotation=math.degrees(angle), align=align))

    def file_bytes(self, file_id: Any) -> Optional[bytes]:
        key = str(file_id)
        if key in self.file_cache:
            return self.file_cache[key]
        entry = self.files.get(key)
        data: Optional[bytes] = None
        url = entry.get("dataURL") if isinstance(entry, dict) else None
        if isinstance(url, str) and "," in url:
            head, _, payload = url.partition(",")
            if len(payload) * 3 // 4 <= min(MAX_FILE_BYTES, self.file_budget):
                try:
                    data = base64.b64decode(payload, validate=False) if ";base64" in head else \
                        payload.encode("latin-1", "replace")
                except (binascii.Error, ValueError):
                    data = None
                if data is not None:
                    self.file_budget -= len(data)
        self.file_cache[key] = data
        return data

    def image(self, page: Page, spec: _PageSpec, el: Dict[str, Any]) -> None:
        box = self.bounds(el)
        if box is None or box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
            self.counts.add("invalid")
            return
        data = self.file_bytes(el.get("fileId"))
        fmt = sniff_image(data) if data else None
        if fmt not in ("png", "jpeg"):
            self.counts.add("image_format")
            return
        scale = el.get("scale")
        flipped = isinstance(scale, list) and any(_num(v, 1.0) < 0 for v in scale[:2])
        if el.get("crop") or flipped:
            self.counts.add("image_crop")
        x0, y0, x1, y1 = box
        page.images.append(Image((x0 - spec.x) * PT_PER_PX, (y0 - spec.y) * PT_PER_PX, (x1 - x0) * PT_PER_PX,
                                 (y1 - y0) * PT_PER_PX, data, fmt=fmt, rotation=math.degrees(_angle(el))))


def read_excalidraw(data: bytes) -> Document:
    """Parse an Excalidraw ``.excalidraw`` scene given as bytes.

    Raises :class:`ValueError` when the data is not an Excalidraw scene (not JSON, no
    ``"type": "excalidraw"``, larger than the limit); everything else that is damaged or
    unsupported becomes a line on ``Document.warnings``.
    """
    return _Reader(ensure_bytes(data, "read_excalidraw")).read()

