"""Model -> Excalidraw ``.excalidraw`` writer (``docs/excalidraw.md`` section 3).

The scene is ``{"type": "excalidraw", "version": 2, "source", "elements", "appState",
"files"}`` (JSON, two-space indent like Excalidraw's own export).  Element fields mirror
inkterop's writer, whose output loaded through ``@excalidraw/excalidraw`` 0.18.0's file-open
path with no element dropped and rendered like the source after its width fix.

* Pages are stacked top to bottom (80 px apart) as **frames** named "Page N"; every element
  of a page carries the frame's id in ``frameId``.  pt / 0.75 = px.
* Ink -> ``freedraw``: points relative to the first one, ``strokeWidth`` = widest width /
  8.08 and per-point ``pressures`` through the inverse freedraw law, so Excalidraw draws the
  model's widths (a stroke spans at most 1 : 3.08; thinner parts are widened, warning);
  ``simulatePressure`` false; ``opacity`` = alpha; a one-point stroke gets the second point
  Excalidraw adds to its own dots.  Highlighters and pen names are kept in ``customData``.
* Shape fills -> a closed ``line`` (``polygon``) with ``backgroundColor`` and a transparent
  stroke.
* Images: PNG / JPEG as ``image`` elements whose ``fileId`` (SHA-1 of the bytes) points into
  ``files`` (data URLs), rotation as ``angle``.  PDF images are dropped (Excalidraw cannot
  show PDF), with a warning.
* Text boxes -> ``text`` (Helvetica unless the model names one of Excalidraw's fonts), wrapped
  at the box width (``autoResize`` false, ``originalText`` unwrapped); the model's top-left
  rotation pivot is moved to Excalidraw's centre pivot.
* Page paper patterns and PDF backgrounds are dropped (Excalidraw has neither), with a warning.
* Identifiers, seeds and nonces are random; ``Options.random_seed`` makes them (and the
  ``updated`` stamps) reproducible.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .. import __version__
from ..codecutil import Counter, clamp_rgba, is_finite, sniff_image, stroke_polyline, to_byte
from ..model import Document, Image, Page, Stroke, TextBox
from ..notability.writer import text_origin_for_centre_pivot
from . import (DEFAULT_FONT_FAMILY, FONT_NAMES, LINE_HEIGHT, MAX_FACTOR, MIN_FACTOR, PAGE_GAP_PX, PT_PER_PX,
               pressure_for_factor)

__all__ = ["write_excalidraw", "build_scene"]

MAX_PAGE_SIDE_PT = 1e6
DEFAULT_PAGE_PT = (595.28, 841.89)
CHAR_EM = 0.55  # average glyph width (ems) used to wrap text at the box width
_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_-"
_SURROGATES = re.compile("[\ud800-\udfff]")

_MESSAGES = {
    "pdf_background": "{n} PDF page backgrounds were dropped (Excalidraw cannot show PDF pages)",
    "paper": "{n} pages lost their ruled, grid or dotted paper (Excalidraw has no paper)",
    "pdf_image": "{n} PDF images were dropped (Excalidraw cannot show PDF)",
    "image_format": "{n} images are neither PNG nor JPEG, or have no data or box, and were skipped",
    "taper": "{n} strokes vary in width more than Excalidraw's pressure range (about 3:1); their thinnest "
             "parts were widened",
    "text_style": "{n} text boxes used bold, italic, underlined or mixed text; Excalidraw text has one plain style",
    "invalid": "{n} strokes or text boxes with invalid coordinates were skipped",
    "page_size": "{n} pages had no valid size; A4 was used",
}


def _opt(options: Any, name: str, default: Any) -> Any:
    value = getattr(options, name, None) if options is not None else None
    return default if value is None else value


def _hex(color: Tuple[float, float, float, float]) -> str:
    r, g, b, _a = clamp_rgba(color)
    return "#%02x%02x%02x" % (to_byte(r), to_byte(g), to_byte(b))


def _r(v: float) -> float:
    """Coordinates rounded to 1/10000 px (0.000075 pt) to keep the JSON compact."""
    out = round(float(v), 4)
    return 0.0 if out == 0 else out


def _wrap(text: str, width_px: float, font_px: float) -> str:
    """Greedy word wrap at ``width_px`` with an average glyph width (Excalidraw re-wraps
    ``originalText`` with real metrics when the text is edited)."""
    limit = max(1, int(width_px / (CHAR_EM * font_px)))
    lines: List[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = word if not line else line + " " + word
            if len(candidate) <= limit or not line:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return "\n".join(lines)


class _Writer:
    def __init__(self, doc: Document, options: Any):
        self.doc = doc
        self.options = options
        self.counts = Counter(_MESSAGES)
        seed = _opt(options, "random_seed", None)
        self.rng = random.Random(seed)
        self.updated = 1 if seed is not None else int(time.time() * 1000)
        self.paper_mode = str(_opt(options, "paper", "plain"))
        self.files: Dict[str, Dict[str, Any]] = {}

    def new_id(self) -> str:
        return "".join(self.rng.choice(_ID_ALPHABET) for _ in range(21))

    def base(self, kind: str, x: float, y: float, w: float, h: float, frame: Optional[str], *,
             stroke: str = "#1e1e1e", stroke_width: float = 1.0, opacity: float = 1.0,
             background: str = "transparent", angle: float = 0.0) -> Dict[str, Any]:
        return {
            "id": self.new_id(), "type": kind, "x": _r(x), "y": _r(y), "width": _r(w), "height": _r(h),
            "angle": angle, "strokeColor": stroke, "backgroundColor": background, "fillStyle": "solid",
            "strokeWidth": stroke_width, "strokeStyle": "solid", "roughness": 0,
            "opacity": int(round(min(1.0, max(0.0, opacity)) * 100)), "groupIds": [], "frameId": frame,
            "roundness": None, "seed": self.rng.randrange(1, 2 ** 31), "version": 1,
            "versionNonce": self.rng.randrange(1, 2 ** 31), "isDeleted": False, "boundElements": None,
            "updated": self.updated, "link": None, "locked": False,
        }

    # -- elements ---------------------------------------------------------------------------

    def freedraw(self, stroke: Stroke, ox: float, oy: float, frame: str) -> Optional[Dict[str, Any]]:
        pts = stroke_polyline(stroke)
        if not pts:
            if stroke.points:
                self.counts.add("invalid")
            return None
        xs = [p.x / PT_PER_PX + ox for p in pts]
        ys = [p.y / PT_PER_PX + oy for p in pts]
        widths = [p.width / PT_PER_PX for p in pts]
        if not all(is_finite(v) for v in xs + ys + widths):
            self.counts.add("invalid")
            return None
        x0, y0 = xs[0], ys[0]
        rel = [[_r(x - x0), _r(y - y0)] for x, y in zip(xs, ys)]
        if len(rel) == 1:  # Excalidraw stores a dot with a second point 0.0001 px away
            rel.append([0.0001, 0.0001])
            widths.append(widths[0])
        stroke_width = max(widths) / MAX_FACTOR
        if min(widths) < max(widths) * (MIN_FACTOR / MAX_FACTOR) * (1.0 - 1e-9):
            self.counts.add("taper")
        pressures = [round(pressure_for_factor(w / stroke_width), 4) for w in widths]
        color = clamp_rgba(stroke.color)
        el = self.base("freedraw", x0, y0, max(p[0] for p in rel) - min(p[0] for p in rel),
                       max(p[1] for p in rel) - min(p[1] for p in rel), frame,
                       stroke=_hex(color), stroke_width=_r(stroke_width), opacity=color[3])
        el.update({"points": rel, "pressures": pressures, "simulatePressure": False,
                   "lastCommittedPoint": rel[-1]})
        hints: Dict[str, str] = {}
        if stroke.kind == "highlighter":
            hints["kind"] = "highlighter"
        if stroke.pen:
            hints["pen"] = str(stroke.pen)
        if hints:
            el["customData"] = {"gnnote": hints}
        return el

    def fill(self, stroke: Stroke, ox: float, oy: float, frame: str) -> Optional[Dict[str, Any]]:
        ring = [p for p in ((stroke.outline or [[]])[0] or stroke.points) if is_finite(p.x, p.y)]
        if len(ring) < 3:
            return None
        xs = [p.x / PT_PER_PX + ox for p in ring]
        ys = [p.y / PT_PER_PX + oy for p in ring]
        x0, y0 = xs[0], ys[0]
        rel = [[_r(x - x0), _r(y - y0)] for x, y in zip(xs, ys)]
        if rel[-1] != rel[0]:
            rel.append(list(rel[0]))
        color = clamp_rgba(stroke.color)
        el = self.base("line", x0, y0, max(p[0] for p in rel) - min(p[0] for p in rel),
                       max(p[1] for p in rel) - min(p[1] for p in rel), frame,
                       stroke="transparent", background=_hex(color), opacity=color[3])
        el.update({"points": rel, "lastCommittedPoint": None, "startBinding": None, "endBinding": None,
                   "startArrowhead": None, "endArrowhead": None, "polygon": True})
        return el

    def image(self, image: Image, ox: float, oy: float, frame: str) -> Optional[Dict[str, Any]]:
        data = bytes(image.data or b"")
        if not data or not (is_finite(image.x, image.y, image.w, image.h) and image.w > 0 and image.h > 0):
            self.counts.add("image_format")
            return None
        kind = sniff_image(data)
        if kind == "pdf":
            self.counts.add("pdf_image")
            return None
        if kind not in ("png", "jpeg"):
            self.counts.add("image_format")
            return None
        mime = "image/png" if kind == "png" else "image/jpeg"
        file_id = hashlib.sha1(data).hexdigest()
        if file_id not in self.files:
            self.files[file_id] = {"mimeType": mime, "id": file_id,
                                   "dataURL": f"data:{mime};base64," + base64.b64encode(data).decode("ascii"),
                                   "created": self.updated, "lastRetrieved": self.updated}
        el = self.base("image", image.x / PT_PER_PX + ox, image.y / PT_PER_PX + oy, image.w / PT_PER_PX,
                       image.h / PT_PER_PX, frame, stroke="transparent",
                       angle=math.radians(float(image.rotation or 0.0) % 360.0))
        el.update({"fileId": file_id, "status": "saved", "scale": [1, 1], "crop": None})
        return el

    def text(self, box: TextBox, ox: float, oy: float, frame: str) -> Optional[Dict[str, Any]]:
        content = box.text or "".join(r.text for r in box.runs)
        if not content.strip():
            return None
        if not is_finite(box.x, box.y):
            self.counts.add("invalid")
            return None
        size = box.size if is_finite(box.size) and box.size > 0 else 12.0
        color = box.color
        font = None
        if box.runs:
            first = box.runs[0]
            size = first.size if first.size and is_finite(first.size) and first.size > 0 else size
            color = first.color or color
            font = first.font
            styled = any(r.bold or r.italic or r.underline for r in box.runs)
            mixed = len({(r.size, tuple(r.color) if r.color else None, r.font) for r in box.runs if r.text.strip()}) > 1
            if styled or mixed:
                self.counts.add("text_style")
        family = next((fid for fid, name in FONT_NAMES.items() if font and name.lower() == font.lower()),
                      DEFAULT_FONT_FAMILY)
        font_px = size / PT_PER_PX
        width_px = box.w / PT_PER_PX if is_finite(box.w) and box.w > 0 else 0.0
        longest = max(len(line) for line in content.split("\n"))
        auto = width_px <= 0 or longest * CHAR_EM * font_px <= width_px
        shown = content if auto else _wrap(content, width_px, font_px)
        if auto:
            width_px = max(width_px, longest * CHAR_EM * font_px, 1.0)
        height_px = max((shown.count("\n") + 1) * font_px * LINE_HEIGHT,
                        box.h / PT_PER_PX if is_finite(box.h) and box.h > 0 else 0.0)
        theta = math.radians(float(box.rotation or 0.0) % 360.0)
        x, y = box.x / PT_PER_PX, box.y / PT_PER_PX
        if abs(math.sin(theta)) > 1e-12 or math.cos(theta) < 0:
            x, y = text_origin_for_centre_pivot((x, y), (width_px, height_px), (0.0, 0.0), theta)
        rgba = clamp_rgba(color)
        el = self.base("text", x + ox, y + oy, width_px, height_px, frame, stroke=_hex(rgba), opacity=rgba[3],
                       angle=theta)
        el.update({"text": shown, "fontSize": _r(font_px), "fontFamily": family,
                   "textAlign": box.align if box.align in ("left", "center", "right") else "left",
                   "verticalAlign": "top", "containerId": None, "originalText": content,
                   "autoResize": auto, "lineHeight": LINE_HEIGHT})
        return el

    # -- scene ----------------------------------------------------------------------------------

    def scene(self) -> Dict[str, Any]:
        pages = list(self.doc.pages)
        if not pages:
            self.doc.warn("The document has no pages; one empty page was written")
            pages = [Page(*DEFAULT_PAGE_PT)]
        elements: List[Dict[str, Any]] = []
        top = 0.0
        for index, page in enumerate(pages):
            w, h = page.width, page.height
            if not (is_finite(w, h) and 0 < w <= MAX_PAGE_SIDE_PT and 0 < h <= MAX_PAGE_SIDE_PT):
                self.counts.add("page_size")
                w, h = DEFAULT_PAGE_PT
            if page.background is not None and (not page.template_is_builtin or self.paper_mode == "pdf"):
                self.counts.add("pdf_background")
            elif (page.paper or "plain") != "plain":
                self.counts.add("paper")
            frame = self.base("frame", 0.0, top, w / PT_PER_PX, h / PT_PER_PX, None, stroke="#bbb")
            frame["name"] = f"Page {index + 1}"
            elements.append(frame)
            fid = frame["id"]
            for image in page.images:
                el = self.image(image, 0.0, top, fid)
                if el is not None:
                    elements.append(el)
            for stroke in page.strokes:
                el = self.fill(stroke, 0.0, top, fid) if stroke.kind == "fill" else self.freedraw(stroke, 0.0, top, fid)
                if el is not None:
                    elements.append(el)
            for box in page.texts:
                el = self.text(box, 0.0, top, fid)
                if el is not None:
                    elements.append(el)
            top += h / PT_PER_PX + PAGE_GAP_PX
        self.counts.flush(self.doc)
        return {"type": "excalidraw", "version": 2, "source": f"gnnote {__version__}", "elements": elements,
                "appState": {"gridSize": 20, "viewBackgroundColor": "#ffffff"}, "files": self.files}


def build_scene(doc: Document, options: Any = None) -> Dict[str, Any]:
    """The scene as a dict (what :func:`write_excalidraw` serialises)."""
    return _Writer(doc, options).scene()


def write_excalidraw(doc: Document, options: Any = None) -> bytes:
    """Serialise ``doc`` as an Excalidraw ``.excalidraw`` scene (UTF-8 JSON, see the module
    docstring).  ``options`` is duck-typed: ``paper`` and ``random_seed`` are honoured; lossy
    steps are reported through ``doc.warn``."""
    text = json.dumps(build_scene(doc, options), indent=2, ensure_ascii=False)
    return _SURROGATES.sub("\ufffd", text).encode("utf-8")
