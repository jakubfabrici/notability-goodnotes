"""MyScript Notes / Nebo ``.nebo`` -> :class:`gnnote.model.Document` (tolerant reader).

A ``.nebo`` package is a ZIP (``docs/nebo.md``)::

    meta.json                 document metadata: pageTitle, pageExtent [x, y, w, h] in millimetres
    rel.json                  {"pages": {"<id>": {...}, ...}}: the pages in order
    index.bdom                document layout (MyScript's binary DOM, not decoded)
    pages/<id>/ink.bink       the page's ink (BINK, :mod:`gnnote.nebo.bink`)
    pages/<id>/page.bdom      the page's layout: typed text, typeset shapes, math (not decoded)
    pages/<id>/meta.json      the page's pageExtent
    pages/<id>/style.css      the pen classes the ink's tags refer to (``.pen-025 {-myscript-pen-width:0.35}``)

Mapping:

* Pages follow ``rel.json``; a page is ``pageExtent`` (page, then document, then
  ``raw-content.page-size``, then a Kobo ``iink-user-metadata`` geometry, else A4) converted
  from millimetres to points (x 72/25.4).  A page is enlarged when ink lies outside it
  (MyScript pages scroll), with one warning.
* Every live BINK stroke becomes a polyline :class:`Stroke`.  Its style is the CSS cascade of
  the page's ``style.css`` (``ink`` / ``stroke`` defaults, then the classes named by the
  stroke's tags, e.g. ``.pen-025``) and the ``.STYLE`` declarations tagged on it: ``color``
  (``#RRGGBBAA``), ``-myscript-pen-width`` (mm) and ``-myscript-pen-pressure-sensitivity``.
  ``HIGHLIGHT_STROKES``, a ``Highlighter`` brush or a ``highlighter-*`` tag make it a
  highlighter.
* Widths: the stylesheet width of the pen class; when the stroke carries real pressure
  (force bytes that vary) and the sensitivity is above 0, each point gets inkterop's fitted
  law ``w = base x (1 + s x 2.43 x (force - 0.29))`` with ``base`` the width the class name
  encodes (``pen-025`` = 0.25 mm), clamped to 0.2 .. 3 x base.
* Not converted (one warning each): the layout data (typed and converted text, typeset
  shapes and math), embedded objects (images), and anything the size guards refused.
"""
from __future__ import annotations

import bisect
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..model import RGBA, Document, Page, Point, Stroke
from ..readutil import PointBudget, ZipBundle, load_json, num
from .bink import MAX_TAG_WORK, BinkError, BinkInk, BinkTag, parse_bink

__all__ = ["read_nebo", "parse_css", "parse_declarations", "MM_TO_PT", "A4_MM"]

MM_TO_PT = 72.0 / 25.4
A4_MM = (210.0, 297.0)
MAX_PAGES = 10_000
MAX_PAGE_MM = 100_000.0  # a page side beyond 100 m is damage
GROWTH_TOLERANCE_MM = 1.0  # ink this far outside the page does not enlarge it
GROWTH_MARGIN_MM = 5.0  # margin kept around ink that enlarges a page
MAX_CSS_BYTES = 1 << 20
MAX_CSS_RULES = 20_000
DEFAULT_PEN_MM = 0.625  # Nebo's default pen ("0.35" in the app) when the stylesheet is missing
DEFAULT_HIGHLIGHTER_MM = 5.0
DEFAULT_SENSITIVITY = 0.8  # the app's default pressure sensitivity (inkterop, inferred)
WIDTH_LAW = (2.43, 0.29)  # rendered = base * (1 + s * 2.43 * (force - 0.29)), inkterop's fit
WIDTH_LAW_CLAMP = (0.2, 3.0)
BLACK: RGBA = (0.0, 0.0, 0.0, 1.0)

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)")
_HEX_RE = re.compile(r"#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_CLASS_WIDTH_RE = re.compile(r"^(?:pen|brush)-(\d{2,4})$")
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[xX×,]\s*(\d+(?:\.\d+)?)")


# --------------------------------------------------------------------------- CSS


def parse_declarations(text: str) -> Dict[str, str]:
    """``"color:#000000ff; -myscript-pen-width: 0.35"`` -> ``{"color": ..., ...}`` (lower-case keys)."""
    out: Dict[str, str] = {}
    for part in _COMMENT_RE.sub("", text).split(";"):
        key, sep, value = part.partition(":")
        if sep and key.strip():
            out[key.strip().lower()] = value.strip()
    return out


def parse_css(text: str) -> Dict[str, Dict[str, str]]:
    """Selector -> declarations of a (MyScript) stylesheet; later rules win per property."""
    rules: Dict[str, Dict[str, str]] = {}
    count = 0
    for match in _RULE_RE.finditer(_COMMENT_RE.sub("", text[:MAX_CSS_BYTES])):
        count += 1
        if count > MAX_CSS_RULES:
            break
        declarations = parse_declarations(match.group(2))
        for selector in match.group(1).split(","):
            selector = selector.strip()
            if selector:
                rules.setdefault(selector, {}).update(declarations)
    return rules


def _css_number(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    m = _NUMBER_RE.search(value)
    return num(float(m.group())) if m else None


def _css_color(value: Optional[str]) -> Optional[RGBA]:
    if not value:
        return None
    m = _HEX_RE.search(value)
    if m is None:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h) + "ff"
    elif len(h) == 6:
        h += "ff"
    r, g, b, a = (int(h[i:i + 2], 16) / 255.0 for i in range(0, 8, 2))
    return (r, g, b, a)


# --------------------------------------------------------------------------- geometry


def _box(value: Any) -> Optional[Tuple[float, float, float, float]]:
    """A ``[x, y, width, height]`` millimetre box, or None when unusable."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    x, y, w, h = (num(v) for v in value)
    if x is None or y is None or w is None or h is None:
        return None
    if not (0 < w <= MAX_PAGE_MM and 0 < h <= MAX_PAGE_MM) or abs(x) > MAX_PAGE_MM or abs(y) > MAX_PAGE_MM:
        return None
    return x, y, w, h


def _kobo_box(meta: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    """Page box from a Kobo notebook's ``iink-user-metadata.kobo`` geometry (pixels) and dpi."""
    user = meta.get("iink-user-metadata")
    kobo = user.get("kobo") if isinstance(user, dict) else None
    if not isinstance(kobo, dict):
        return None
    dpi = num(kobo.get("dpi"))
    geometry = kobo.get("geometry")
    size: Optional[Tuple[Optional[float], Optional[float]]] = None
    if isinstance(geometry, (list, tuple)) and len(geometry) in (2, 4):
        size = (num(geometry[-2]), num(geometry[-1]))
    elif isinstance(geometry, dict):
        size = (num(geometry.get("width")), num(geometry.get("height")))
    elif isinstance(geometry, str):
        m = _SIZE_RE.search(geometry)
        if m:
            size = (num(float(m.group(1))), num(float(m.group(2))))
    if size is None or dpi is None or not 10 <= dpi <= 10_000:
        return None
    w, h = size
    if w is None or h is None:
        return None
    return _box([0.0, 0.0, w * 25.4 / dpi, h * 25.4 / dpi])


# --------------------------------------------------------------------------- reader


class _Reader:
    def __init__(self, data: bytes):
        self.bundle = ZipBundle(data, "MyScript Notes")
        names = self.bundle.names
        if not (self.bundle.has("rel.json") or any(n.startswith("pages/") for n in names)):
            raise ValueError("not a MyScript Notes file: neither rel.json nor a pages/ folder found")
        self.doc = Document(source_format="nebo")
        self.budget = PointBudget()
        self.grown = 0
        self.tag_work = 0
        self.tag_work_exceeded = False

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    def load(self, name: str) -> Dict[str, Any]:
        value = load_json(self.bundle.read(name))
        return value if isinstance(value, dict) else {}

    def page_ids(self, rel: Dict[str, Any]) -> List[str]:
        ids: List[str] = []
        pages = rel.get("pages")
        if isinstance(pages, dict):
            ids = [str(k) for k in pages]
        elif isinstance(pages, list):
            for entry in pages:
                if isinstance(entry, str):
                    ids.append(entry)
                elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    ids.append(entry["id"])
        ids = [i for i in ids if i and "/" not in i and len(i) <= 256]
        if not ids:
            seen = set()
            for name in self.bundle.names:
                parts = name.split("/")
                if len(parts) >= 3 and parts[0] == "pages" and parts[1] and parts[1] not in seen:
                    seen.add(parts[1])
                    ids.append(parts[1])
        unique = list(dict.fromkeys(ids))
        if len(unique) > MAX_PAGES:
            self.warn(f"The note lists {len(unique)} pages; only the first {MAX_PAGES} are read")
            unique = unique[:MAX_PAGES]
        return unique

    def read(self) -> Document:
        meta = self.load("meta.json")
        rel = self.load("rel.json")
        title = meta.get("pageTitle") or meta.get("automaticPageTitle")
        if isinstance(title, str) and title.strip():
            self.doc.title = title.strip()
        for number, page_id in enumerate(self.page_ids(rel), 1):
            self.doc.pages.append(self.page(number, page_id, meta))
        self.report(rel)
        return self.doc

    # -- one page --------------------------------------------------------------------------

    def extent(self, page_meta: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[float, float, float, float]:
        raw = page_meta.get("raw-content")
        candidates = (page_meta.get("pageExtent"), meta.get("pageExtent"),
                      raw.get("page-size") if isinstance(raw, dict) else None)
        for candidate in candidates:
            box = _box(candidate)
            if box is not None:
                return box
        box = _kobo_box(meta)
        if box is not None:
            return box
        self.warn("Page size not recorded in the note; A4 assumed")
        return 0.0, 0.0, A4_MM[0], A4_MM[1]

    def page(self, number: int, page_id: str, meta: Dict[str, Any]) -> Page:
        prefix = f"pages/{page_id}/"
        page_meta = self.load(prefix + "meta.json")
        x0, y0, w, h = self.extent(page_meta, meta)
        strokes: List[Tuple[List[Tuple[float, float, float]], Stroke]] = []
        raw = self.bundle.read(prefix + "ink.bink")
        if raw is not None:
            try:
                ink = parse_bink(raw, self.budget)
            except BinkError:
                self.warn(f"Page {number}: ink.bink is not MyScript ink; the page's ink was skipped")
                ink = None
            if ink is not None:
                for problem in ink.problems:
                    self.warn(f"Page {number}: {problem}")
                css_raw = self.bundle.read(prefix + "style.css")
                css = parse_css(css_raw.decode("utf-8", "replace")) if css_raw else {}
                strokes = self.strokes(ink, css)
        # Enlarge the page to the ink that lies outside it (MyScript pages scroll).
        xs = [x for pts, _ in strokes for x, _y, _w in pts]
        ys = [y for pts, _ in strokes for _x, y, _w in pts]
        if xs:
            lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
            nx0, ny0, nx1, ny1 = x0, y0, x0 + w, y0 + h
            if lo_x < x0 - GROWTH_TOLERANCE_MM:
                nx0 = lo_x - GROWTH_MARGIN_MM
            if lo_y < y0 - GROWTH_TOLERANCE_MM:
                ny0 = lo_y - GROWTH_MARGIN_MM
            if hi_x > x0 + w + GROWTH_TOLERANCE_MM:
                nx1 = hi_x + GROWTH_MARGIN_MM
            if hi_y > y0 + h + GROWTH_TOLERANCE_MM:
                ny1 = hi_y + GROWTH_MARGIN_MM
            if (nx0, ny0, nx1, ny1) != (x0, y0, x0 + w, y0 + h):
                self.grown += 1
                x0, y0, w, h = nx0, ny0, nx1 - nx0, ny1 - ny0
        page = Page(width=w * MM_TO_PT, height=h * MM_TO_PT)
        for pts, stroke in strokes:
            stroke.points = [Point((x - x0) * MM_TO_PT, (y - y0) * MM_TO_PT, width) for x, y, width in pts]
            page.strokes.append(stroke)
        return page

    def coverage(self, ink: BinkInk) -> Dict[int, List[BinkTag]]:
        """Stroke record -> the tags covering it, in table order (bounded work)."""
        records = sorted(s.record for s in ink.strokes)
        covering: Dict[int, List[BinkTag]] = {r: [] for r in records}
        for tag in ink.tags:
            lo = bisect.bisect_left(records, tag.first)
            hi = bisect.bisect_right(records, tag.last)
            if hi <= lo:
                continue
            self.tag_work += hi - lo
            if self.tag_work > MAX_TAG_WORK:
                self.tag_work_exceeded = True
                break
            for record in records[lo:hi]:
                covering[record].append(tag)
        return covering

    def strokes(self, ink: BinkInk, css: Dict[str, Dict[str, str]]
                ) -> List[Tuple[List[Tuple[float, float, float]], Stroke]]:
        covering = self.coverage(ink)
        defaults: Dict[str, str] = {}
        defaults.update(css.get("ink", {}))
        defaults.update(css.get("stroke", {}))
        out = []
        for raw in ink.strokes:
            tags = covering.get(raw.record, [])
            names = [t.name for t in tags]
            own: Dict[str, str] = {}  # what the stroke's tags say, without the page defaults
            for tag in tags:
                if tag.name != ".STYLE":
                    own.update(css.get("." + tag.name, {}))
            for tag in tags:
                if tag.name == ".STYLE" and tag.text:
                    own.update(parse_declarations(tag.text.strip().strip('"')))
            out.append(self.stroke(raw.xs, raw.ys, raw.force, names, own, defaults))
        return out

    def stroke(self, xs: Sequence[float], ys: Sequence[float], force: Optional[bytes], names: List[str],
               own: Dict[str, str], defaults: Dict[str, str]) -> Tuple[List[Tuple[float, float, float]], Stroke]:
        props = dict(defaults)
        props.update(own)
        brush = props.get("-myscript-pen-brush", "").lower()
        highlighter = ("HIGHLIGHT_STROKES" in names or "highlighter" in brush
                       or any(n.lower().startswith("highlighter") for n in names))
        name_mm: Optional[float] = None
        for name in names:
            m = _CLASS_WIDTH_RE.match(name)
            if m:
                name_mm = int(m.group(1)) / 100.0
                break
        # The page's default pen width ("ink" / "stroke" rules) is a pen width: a highlighter
        # without its own width class falls back to the highlighter default instead.
        width_mm = _css_number((own if highlighter else props).get("-myscript-pen-width"))
        if width_mm is None or not 0 < width_mm <= 100:
            width_mm = name_mm or (DEFAULT_HIGHLIGHTER_MM if highlighter else DEFAULT_PEN_MM)
        sensitivity = _css_number(props.get("-myscript-pen-pressure-sensitivity"))
        if sensitivity is None:
            sensitivity = 0.0 if highlighter else DEFAULT_SENSITIVITY
        sensitivity = max(0.0, min(sensitivity, 2.0))
        color = _css_color(props.get("color")) or BLACK
        n = len(xs)
        if force is not None and n > 1 and sensitivity > 0 and max(force) - min(force) > 1:
            base = (name_mm or width_mm) * MM_TO_PT
            k, f0 = WIDTH_LAW
            lo, hi = WIDTH_LAW_CLAMP
            widths = [base * min(max(1.0 + sensitivity * k * (f / 255.0 - f0), lo), hi) for f in force]
        else:
            widths = [width_mm * MM_TO_PT] * n
        pen: Optional[str] = None
        if "fountain" in brush:
            pen = "fountain"
        elif "calligraph" in brush:
            pen = "brush"
        elif "pencil" in brush:
            pen = "pencil"
        stroke = Stroke(points=[], color=color, kind="highlighter" if highlighter else "pen", pen=pen,
                        width=width_mm * MM_TO_PT)
        return list(zip(xs, ys, widths)), stroke

    # -- what is not converted -----------------------------------------------------------

    def report(self, rel: Dict[str, Any]) -> None:
        if self.grown:
            self.warn(f"{self.grown} page(s) were enlarged to fit ink drawn outside the page")
        if self.tag_work_exceeded:
            self.warn("The pen style tables are too large; some strokes use the default pen")
        if self.budget.exhausted:
            self.warn("The note holds more ink points than the converter reads; the remaining strokes were skipped")
        if any(n.endswith(".bdom") for n in self.bundle.names):
            self.warn("Typed and converted text, typeset shapes and math are stored in MyScript's undecoded "
                      "layout data and are not converted; handwriting is")
        objects = self.bundle.files("objects/")
        listed = rel.get("objects")
        count = max(len(objects), len(listed) if isinstance(listed, (list, dict)) else 0)
        if count:
            self.warn(f"{count} embedded object(s) (images or files) were not converted: their placement is "
                      f"stored in the undecoded layout data")
        for message in (self.bundle.size_warning("MyScript Notes"), self.bundle.failure_warning("MyScript Notes")):
            if message:
                self.warn(message)


def read_nebo(data: bytes) -> Document:
    """Parse a MyScript Notes / Nebo ``.nebo`` package given as bytes.

    Raises :class:`ValueError` when the data is not a ``.nebo`` package (not a ZIP, or no
    ``rel.json`` and no ``pages/`` folder).  Everything else is tolerated: what cannot be
    read is skipped with a line on ``Document.warnings``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_nebo expects bytes")
    return _Reader(bytes(data)).read()
