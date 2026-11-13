"""Stylus Labs Write ``.svgz`` / ``.svg`` -> :class:`gnnote.model.Document` (tolerant reader).

Containers (``docs/styluswrite.md`` section 1): gzip-compressed SVG (``.svgz``, Write's default;
a zlib stream is accepted too), plain SVG, and the single-file HTML Write also saves (``.htm``,
and the Write 2 ``.html`` with its ``text/writeconfig`` header), all detected from the content.
:mod:`gnnote.styluswrite.container` inflates leniently: a wrong or missing gzip trailer keeps
the whole document, damage inside the stream keeps what came before it.  The XML is parsed with
expat into an :mod:`xml.etree.ElementTree` tree; an internal DTD subset or any entity
declaration is refused before it can expand, a plain ``<!DOCTYPE ...>`` naming an external DTD
(Write 2 HTML) is tolerated and never fetched.  Nesting depth, element count, inflated size,
path work, generated papers and inferred text lines are bounded; a damaged or truncated file
keeps what was parsed before the damage.

Mapping (section 4), Write units (1/150 inch, :data:`gnnote.styluswrite.PT_PER_UNIT`) -> pt:

* Pages: every ``g.write-content`` (Write 3) or ``g#page_N`` with ruling attributes (Write 2),
  in document order.  Size: the group's ``width`` / ``height`` (the Page Setup values), else
  its ``svg``'s, else ``rect.pagerect``'s, else Write's default.
* Paper: Write's standard ruling (``g.ruleline.write-std-ruling``, or none as in Write 2) ->
  ``Page.paper`` lined / grid from ``xruling`` / ``yruling`` and a generated stock-paper PDF
  (:mod:`gnnote.styluswrite.paper`) drawing what Write draws: ``papercolor``, the rules in the
  alpha-first ``rulecolor``, the red margin at ``marginLeft``.  A custom ruling made of circles
  (a dot ``<pattern>``) is a dotted paper with the dots where SVG puts them; any other custom
  ruling is reported once and only its paper colour is kept.  Images in ``g.ruleline`` become
  page images (pdftowrite's raster pages, in a nested ``svg`` whose ``viewBox`` is mapped, often
  drawn by cairo's ``<use>`` of an image kept in ``<defs>``).
* Definitions (``defs``, ``symbol``, ``clipPath``, ``mask``, ...) are never drawn where they
  stand; ``<use>`` draws its target as a child of itself (transform, then ``x`` / ``y``; a
  ``symbol`` / ``svg`` maps its ``viewBox``), bounded in depth and in reused elements and data per file.
* ``path.write-stroke-pen`` and other stroked shapes -> constant-width strokes (one per
  subpath; ``Z`` closes the line); ``path.write-flat-pen`` -> the outline's paired vertices
  (*i* with *n - 1 - i*) give the centre line (midpoints) and the per-point width (distance);
  an outline without that shape stays a filled shape.  Other filled shapes (pasted SVG art,
  rectangles, circles, ...) -> ``Stroke(kind="fill")`` with one ring per subpath.  Curves and
  arcs are flattened to about 0.1 pt.  Transforms (``translate``, ``scale``, ``rotate``,
  ``skewX`` / ``skewY``, ``matrix``, nested ``svg`` offsets) are applied; widths scale by the
  transform's mean scale.  gnnote's own ``data-gnnote-kind`` mark says whether a stroke is a pen
  or a highlighter; without it a translucent stroke (alpha <= 0.6, a ``highlight`` class or
  ``mix-blend-mode: multiply``) is a highlighter; Write's own highlighter encoding is not
  documented anywhere and no sample has one.  A subpath that is only a moveto draws nothing.
* ``<image>`` with a PNG / JPEG data URI -> :class:`Image` (``preserveAspectRatio`` honoured,
  rotation kept); ``<text>`` -> :class:`TextBox` (lines from the baselines, empty lines put
  back from baseline gaps, size estimated, one run per styled chunk).
* Bookmarks are dropped and hand-drawn links keep their ink (one warning each).
"""
from __future__ import annotations

import base64
import binascii
import itertools
import math
import re
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple
from xml.etree import ElementTree as ET
from xml.parsers import expat

from ..codecutil import (MAX_COORD_PT, MAX_SIZE_PT, Counter, ensure_bytes, estimate_text_extent, image_pixel_size,
                         sniff_image)
from ..model import RGBA, Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from ..readutil import MAX_POINTS
from . import (CLASS_BOOKMARK, CLASS_CONTENT, CLASS_FLAT_PEN, CLASS_HYPERREF, CLASS_PAGE, CLASS_RULELINE,
               CLASS_STD_RULING, CLASS_STROKE_PEN, DEFAULT_PAGE_UNITS, DEFAULT_RULE_COLOR, KIND_ATTRIBUTE,
               PT_PER_UNIT)
from . import svgpath
from .container import inflate, is_zlib_header
from .paper import MAX_PRIMITIVES as PAPER_MAX_PRIMITIVES
from .paper import Dot, DotGrid, Paper, blend, paper_pdf
from .svgpath import IDENTITY, Matrix

__all__ = ["read_write_svg", "parse_color", "parse_write_color", "MAX_XML_BYTES", "MAX_DEPTH", "MAX_ELEMENTS",
           "MAX_PAGES", "MAX_POINTS_PER_PATH", "MAX_TOTAL_POINTS", "MAX_IMAGE_BYTES", "STROKE_COST", "MAX_USE_DEPTH",
           "MAX_USE_ELEMENTS", "MAX_USE_BYTES", "MAX_TOTAL_IMAGE_BYTES", "MAX_PROPERTY_CHARS"]

MAX_XML_BYTES = 256 * 1024 * 1024  # inflated XML above this is refused
MAX_DEPTH = 128  # element nesting (Write's own documents nest 6 deep; pasted art a little more)
MAX_ELEMENTS = 2_000_000  # XML elements per file
MAX_PAGES = 10_000
MAX_POINTS_PER_PATH = 200_000  # vertices one path may produce (curves flattened)
MAX_TOTAL_POINTS = MAX_POINTS  # vertices per file (readutil's document-wide budget)
STROKE_COST = 16  # what each stroke or ring costs on top of its vertices, in points of that budget
MAX_IMAGE_BYTES = 64 * 1024 * 1024  # one decoded image
MAX_TOTAL_IMAGE_BYTES = 256 * 1024 * 1024  # decoded image data per file, a reused image counting each time
MAX_PAGE_UNITS = 1e6  # page sides beyond this (or not positive) are damage
CURVE_TOLERANCE_PT = 0.1  # how closely flattened curves and arcs follow the original
HIGHLIGHTER_MAX_ALPHA = 0.6  # translucent ink at or below this opacity is read as a highlighter
TEXT_ASCENT = 0.952  # first baseline below a text box's top, in em (gnnote.pdf.text.ASCENT)
TEXT_LINE = 1.1646  # baseline-to-baseline distance in em (gnnote.pdf.text.LINE)
SAME_X = 1e-3  # text positions closer than this (units) start lines at the same x
DEFAULT_FONT_UNITS = 16.0  # SVG's initial font size ("medium")
BOILERPLATE_TITLES = ("Handwritten Document",)  # Write 2's fixed HTML title
MAX_STYLE_DECLARATIONS = 256  # declarations read from one style attribute
MAX_PROPERTY_CHARS = 1024  # a longer presentation value (colour, length, font, ...) is ignored as invalid
MAX_PAPER_PDFS = 32  # different generated paper PDFs per file
MAX_TOTAL_PAPER_PRIMITIVES = 250_000  # rules and dots drawn into generated papers per file
MAX_BLANK_LINES = 50  # empty lines one baseline gap of a text may stand for
MAX_TOTAL_BLANK_LINES = 100_000  # empty lines inferred from baseline gaps per file
MAX_USE_DEPTH = 4  # <use> elements nested in the content another <use> draws
MAX_USE_ELEMENTS = 1_000_000  # elements drawn again by <use> per file (each instance costs its target's size)
MAX_USE_BYTES = 16 * 1024 * 1024  # characters of attribute values and text drawn again by <use> per file
MAX_CLASSES = 64  # class names read from one class attribute
COLLAPSE_CHUNK = 64 * 1024  # characters of a text chunk split into words at a time (see _collapse)

UNITS_PER_INCH = 72.0 / PT_PER_UNIT
# length unit -> Write units, derived from PT_PER_UNIT (CSS px are taken as Write units, as Write does)
SVG_UNITS = {"": 1.0, "px": 1.0, "in": UNITS_PER_INCH, "cm": UNITS_PER_INCH / 2.54, "mm": UNITS_PER_INCH / 25.4,
             "pt": 1.0 / PT_PER_UNIT, "pc": 12.0 / PT_PER_UNIT}
# matched with fullmatch on the stripped value: one ambiguous run of white space only (two adjacent
# ``\s*`` around an empty unit would backtrack against each other, quadratic in the run's length)
_LENGTH = re.compile(r"([-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)\s*([a-zA-Z%]*)")
_PAGE_ID = re.compile(r"page_\d+$")
_HEX = re.compile(r"#([0-9a-fA-F]+)$")
_FUNC_COLOR = re.compile(r"(rgba?)\(([^()]*)\)$", re.I)
_PAINT_URL = re.compile(r"url\(\s*['\"]?#([^)'\"\s]+)['\"]?\s*\)")
_NUMBER_ITER = re.compile(r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")
_WORD = re.compile(r"\S+")  # \s is what str.split() splits at
# the first item of an x / y / dy list: two disjoint classes, so nothing backtracks
_LIST_HEAD = re.compile(r"[\s,]*([^\s,]+)")

XML_SPACE = " \t\n\r"  # SVG's white space; a no-break space is not white space to SVG
TEXT_TAGS = frozenset(("text", "tspan", "title"))
CONTAINER_TAGS = frozenset(("g", "a", "switch"))
SHAPE_TAGS = frozenset(("path", "rect", "circle", "ellipse", "line", "polyline", "polygon"))
# never rendered where they stand: definitions (drawn only when referenced) and resources
NOT_RENDERED = frozenset(("defs", "symbol", "clipPath", "mask", "pattern", "marker", "linearGradient",
                          "radialGradient", "filter", "cursor", "view", "font", "font-face"))
SILENT_TAGS = NOT_RENDERED | frozenset(("style", "script", "title", "desc", "metadata", "animate",
                                        "animateTransform", "animateMotion", "set"))
INHERITED = ("stroke", "fill", "stroke-width", "stroke-opacity", "fill-opacity", "color", "font-size",
             "font-family", "font-weight", "font-style", "text-anchor", "visibility", "text-decoration",
             "mix-blend-mode")

# CSS / SVG colour keywords most likely to appear in pasted art (Write itself writes #RRGGBB).
NAMED_COLORS: Dict[str, Tuple[int, int, int]] = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "lime": (0, 255, 0),
    "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0), "cyan": (0, 255, 255),
    "aqua": (0, 255, 255), "magenta": (255, 0, 255), "fuchsia": (255, 0, 255), "gray": (128, 128, 128),
    "grey": (128, 128, 128), "silver": (192, 192, 192), "maroon": (128, 0, 0), "olive": (128, 128, 0),
    "navy": (0, 0, 128), "purple": (128, 0, 128), "teal": (0, 128, 128), "orange": (255, 165, 0),
    "brown": (165, 42, 42), "pink": (255, 192, 203), "darkgray": (169, 169, 169), "darkgrey": (169, 169, 169),
    "lightgray": (211, 211, 211), "lightgrey": (211, 211, 211), "darkblue": (0, 0, 139),
    "darkred": (139, 0, 0), "darkgreen": (0, 100, 0), "gold": (255, 215, 0), "violet": (238, 130, 238),
    "indigo": (75, 0, 130),
}

_MESSAGES = {
    "links": "{n} hand-drawn links were converted as plain ink (their link targets are dropped)",
    "bookmarks": "{n} bookmarks were dropped",
    "flat_fallback": "{n} variable-width strokes did not have Write's paired outline and were kept as filled shapes",
    "text": "{n} SVG texts became text boxes; Write cannot edit them and their size is estimated",
    "text_empty": "{n} empty texts were skipped",
    "image_format": "{n} images are neither PNG nor JPEG and were skipped",
    "image_external": "{n} images are linked to files outside the document and were skipped",
    "image_data": "{n} images without readable data were skipped",
    "image_limit": "{n} images beyond the limit of " + str(MAX_TOTAL_IMAGE_BYTES // (1024 * 1024)) + " MB of image "
                   "data per file (a reused image counting each time it is drawn) were skipped",
    "image_slice": "{n} images cropped to their box (preserveAspectRatio slice) are shown uncropped",
    "sheared": "{n} sheared or mirrored images or texts were approximated by a rotation",
    "paint_server": "{n} shapes painted with a gradient or pattern were skipped",
    "use": "{n} reused shapes (<use> elements) that refer to a missing, external, non-drawing or circular target "
           "were skipped",
    "use_limit": "{n} reused shapes (<use> elements) beyond the limits of " + str(MAX_USE_DEPTH) + " nested reuses, "
                 + str(MAX_DEPTH) + " nesting levels, " + str(MAX_USE_ELEMENTS) + " reused elements or "
                 + str(MAX_USE_BYTES // (1024 * 1024)) + " MB of reused attribute and text data per file were skipped",
    "clipped": "{n} clipped or masked elements are shown unclipped",
    "invalid": "{n} shapes, images or texts with invalid or out-of-range coordinates were skipped",
    "path_error": "{n} paths have damaged path data; the part before the damage was kept",
    "transform": "{n} invalid transforms were ignored",
    "color": "{n} unknown colours were ignored",
    "long_value": "{n} style values longer than " + str(MAX_PROPERTY_CHARS) + " characters were ignored",
    "point_limit": "{n} shapes beyond the limit of " + str(MAX_TOTAL_POINTS) + " points or path commands per file "
                   "were dropped",
    "path_limit": "{n} paths with more than " + str(MAX_POINTS_PER_PATH) + " points were shortened",
    "stroke_limit": "{n} strokes and outlines beyond the limit of " + str(MAX_TOTAL_POINTS) + " points per file "
                    "(each stroke counting as " + str(STROKE_COST) + " more) were dropped",
    "page_size": "{n} pages had no valid size; Write's default page size was used",
    "custom_background": "{n} pages have a background drawn by a custom template or an imported PDF page; "
                         "it was not converted (only the paper colour, and Write's standard ruling if the page "
                         "uses it, are kept)",
    "custom_background_images": "{n} pages have a background drawn by a custom template or an imported PDF page; "
                                "only its images were converted (with the paper colour, and Write's standard "
                                "ruling if the page uses it)",
    "paper_dense": "{n} pages have rulings or dot grids too fine for their size (more than "
                   + str(PAPER_MAX_PRIMITIVES) + " rules and dots); their paper was drawn without them",
    "paper_limit": "{n} pages kept only their paper style: the file has more different papers than gnnote draws "
                   "(" + str(MAX_PAPER_PDFS) + " papers, " + str(MAX_TOTAL_PAPER_PRIMITIVES) + " rules and dots)",
    "unknown": "{n} unknown SVG elements were ignored",
    "foreign_object": "{n} embedded HTML objects (foreignObject) were skipped",
}


# --------------------------------------------------------------------------------------
# small value parsers


def _local(name: str) -> str:
    """``"svg:path"`` / ``"xlink:href"`` -> ``"path"`` / ``"href"`` (namespaces are not processed)."""
    return name.rsplit(":", 1)[-1]


def _words(text: str, limit: int) -> List[str]:
    """The first ``limit`` white-space separated words of ``text``.  The rest is not split:
    ``str.split`` makes an object per word, about 20 bytes per character of a long list."""
    return [m.group() for m in itertools.islice(_WORD.finditer(text), limit)]


def _classes(el: ET.Element) -> List[str]:
    """The element's first :data:`MAX_CLASSES` class names."""
    return _words(el.get("class") or "", MAX_CLASSES)


def _aspect_ratio(el: ET.Element) -> List[str]:
    """The words of ``preserveAspectRatio`` (``defer`` dropped; ``xMidYMid meet`` when absent
    or empty).  Only the first three words are read: a longer value is invalid anyway."""
    words = _words(el.get("preserveAspectRatio") or "", 3)
    if words and words[0] == "defer":
        words = words[1:]
    return words[:2] or ["xMidYMid", "meet"]


def _number(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    try:
        v = float(text.strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _length(text: Optional[str]) -> Optional[float]:
    """An SVG length in Write units (``"900px"``, ``"12"``, ``"5mm"``); ``None`` for percentages,
    keywords and anything unparsable or non-finite."""
    if not text:
        return None
    m = _LENGTH.fullmatch(text.strip())
    if m is None:
        return None
    unit = m.group(2).lower()
    if unit not in SVG_UNITS:
        return None
    try:
        v = float(m.group(1)) * SVG_UNITS[unit]
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _opacity(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    t = text.strip()
    try:
        v = float(t[:-1]) / 100.0 if t.endswith("%") else float(t)
    except ValueError:
        return None
    return min(1.0, max(0.0, v)) if math.isfinite(v) else None


def parse_write_color(text: Optional[str]) -> Optional[RGBA]:
    """Write's ``papercolor`` / ``rulecolor``: ``#RRGGBB`` or alpha-first ``#AARRGGBB``."""
    m = _HEX.match((text or "").strip())
    if m is None or len(m.group(1)) not in (6, 8):
        return None
    v = int(m.group(1), 16)
    if len(m.group(1)) == 6:
        return ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0, 1.0
    return ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0, ((v >> 24) & 255) / 255.0


def parse_color(text: Optional[str]) -> Optional[RGBA]:
    """A CSS colour (``#rgb``, ``#rgba``, ``#rrggbb``, ``#rrggbbaa``, ``rgb()`` / ``rgba()``, the
    common keywords, ``transparent``); ``None`` when it is not one."""
    t = (text or "").strip()
    m = _HEX.match(t)
    if m is not None:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            h += "ff"
        if len(h) != 8:
            return None
        v = int(h, 16)
        return ((v >> 24) & 255) / 255.0, ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0
    low = t.lower()
    if low in NAMED_COLORS:
        r, g, b = NAMED_COLORS[low]
        return r / 255.0, g / 255.0, b / 255.0, 1.0
    if low == "transparent":
        return 0.0, 0.0, 0.0, 0.0
    m = _FUNC_COLOR.match(t)
    if m is None:
        return None
    parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(2).strip()) if p.strip()]
    if len(parts) not in (3, 4):
        return None
    out: List[float] = []
    for i, p in enumerate(parts):
        try:
            if p.endswith("%"):
                v = float(p[:-1]) / 100.0
            else:
                v = float(p) / (255.0 if i < 3 else 1.0)
        except ValueError:
            return None
        if not math.isfinite(v):
            return None
        out.append(min(1.0, max(0.0, v)))
    if len(out) == 3:
        out.append(1.0)
    return out[0], out[1], out[2], out[3]


PROPERTIES = INHERITED + ("opacity", "display", "clip-path", "mask")  # what _style reads


def _style(el: ET.Element, counts: Optional[Counter] = None) -> Dict[str, str]:
    """Presentation attributes overridden by the ``style`` attribute's declarations.  A value
    longer than :data:`MAX_PROPERTY_CHARS` (white space around it aside) is never a valid one;
    it is ignored, as CSS ignores invalid values, and counted in ``counts`` when given.  This
    bounds what the shapes and texts inheriting a value parse again each."""
    props: Dict[str, str] = {}
    for key in PROPERTIES:
        value = el.get(key)
        if value is not None:
            if len(value) > MAX_PROPERTY_CHARS:
                value = value.strip()
                if len(value) > MAX_PROPERTY_CHARS:
                    if counts is not None:
                        counts.add("long_value")
                    continue
            props[key] = value
    style = el.get("style")
    if style:
        for decl in style.split(";", MAX_STYLE_DECLARATIONS)[:MAX_STYLE_DECLARATIONS]:
            key, sep, value = decl.partition(":")
            if sep:
                key = key.strip().lower()
                if key not in PROPERTIES:
                    continue
                value = value.replace("!important", "").strip()
                if len(value) > MAX_PROPERTY_CHARS:
                    if counts is not None:
                        counts.add("long_value")
                    continue
                props[key] = value
    return props


def _decompose(m: Matrix) -> Tuple[float, float, float, bool]:
    """``(scale_x, scale_y, rotation_degrees_clockwise, sheared_or_mirrored)`` of a matrix."""
    a, b, c, d = m[0], m[1], m[2], m[3]
    sx, sy = math.hypot(a, b), math.hypot(c, d)
    det = a * d - b * c
    norm = sx * sy
    sheared = norm > 0 and (abs(a * c + b * d) > 1e-3 * norm or det < 0)
    return sx, sy, math.degrees(math.atan2(b, a)), sheared


# --------------------------------------------------------------------------------------
# XML -> ElementTree (no DTDs, bounded)


class _Refused(Exception):
    """The XML declares entities or an internal DTD subset."""


class _Limit(Exception):
    """A resource limit stopped parsing."""


class _TreeParser:
    """expat -> :class:`xml.etree.ElementTree.TreeBuilder` with the limits of the module."""

    def __init__(self) -> None:
        self.builder = ET.TreeBuilder()
        self.stack: List[str] = []
        self.elements = 0
        self.root_tag: Optional[str] = None
        self.problem: Optional[str] = None

    def _start(self, tag: str, attrs: Dict[str, str]) -> None:
        self.elements += 1
        if self.elements > MAX_ELEMENTS:
            raise _Limit(f"more than {MAX_ELEMENTS} XML elements")
        if len(self.stack) >= MAX_DEPTH:
            raise _Limit(f"XML nested deeper than {MAX_DEPTH} levels")
        name = _local(tag)
        clean: Dict[str, str] = {}
        for key, value in attrs.items():
            if key == "xmlns" or key.startswith("xmlns:"):
                continue
            local = _local(key)
            if local not in clean or ":" not in key:  # a plain attribute wins over a prefixed one
                clean[local] = value
        if self.root_tag is None:
            self.root_tag = name
        self.builder.start(name, clean)
        self.stack.append(name)

    def _end(self, tag: str) -> None:
        self.builder.end(self.stack.pop())

    def _data(self, text: str) -> None:
        if self.stack and self.stack[-1] in TEXT_TAGS:
            self.builder.data(text)

    @staticmethod
    def _doctype(name: str, sysid: Optional[str], pubid: Optional[str], has_internal_subset: int) -> None:
        if has_internal_subset:
            raise _Refused()

    @staticmethod
    def _refuse(*_args: object) -> None:
        raise _Refused()

    def parse(self, xml: bytes) -> Optional[ET.Element]:
        parser = expat.ParserCreate()
        parser.buffer_text = True
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.StartDoctypeDeclHandler = self._doctype
        parser.EntityDeclHandler = self._refuse
        parser.UnparsedEntityDeclHandler = self._refuse
        parser.ExternalEntityRefHandler = self._refuse
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._data
        try:
            parser.Parse(xml, True)
        except expat.ExpatError as exc:
            if self.root_tag is None:
                raise ValueError(f"not a Stylus Labs Write document: the XML could not be parsed ({exc})") from None
            self.problem = f"the XML is damaged or truncated at line {exc.lineno}"
        except _Limit as exc:
            self.problem = str(exc)
        if self.root_tag is None:
            raise ValueError("not a Stylus Labs Write document: no XML root element")
        while self.stack:  # close what a damaged file left open
            self.builder.end(self.stack.pop())
        return self.builder.close()


# --------------------------------------------------------------------------------------
# reader


class _Context:
    """What an element inherits: the matrix to page units, presentation properties, opacity."""

    __slots__ = ("matrix", "props", "opacity")

    def __init__(self, matrix: Matrix, props: Dict[str, str], opacity: float):
        self.matrix = matrix
        self.props = props
        self.opacity = opacity


class _PageSvg:
    """What the pages read from the ``svg`` element around their content (its size, ``viewBox``
    and inherited presentation properties), parsed once however many pages share it: Write 2
    puts every ``g#page_N`` straight into the root ``svg``."""

    __slots__ = ("size", "viewbox", "props")

    def __init__(self, size: Optional[Tuple[float, float]], viewbox: Optional[Tuple[List[float], List[str]]],
                 props: Dict[str, str]):
        self.size = size
        self.viewbox = viewbox
        self.props = props


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.doc = Document(title="")
        self.counts = Counter(_MESSAGES)
        self.points_left = MAX_TOTAL_POINTS
        self.paper_pdfs: Dict[Paper, str] = {}  # generated papers, shared by pages that look alike
        self.paper_primitives_left = MAX_TOTAL_PAPER_PRIMITIVES
        self.page: Optional[Page] = None
        self.blank_lines_left = MAX_TOTAL_BLANK_LINES
        self.link_depth = 0  # > 0 inside a hand-drawn link (counted once)
        self.root: Optional[ET.Element] = None
        self.ids: Optional[Dict[str, ET.Element]] = None  # id -> element, built at the first <use>
        self.use_left = MAX_USE_ELEMENTS
        self.use_bytes_left = MAX_USE_BYTES
        self.use_chain: Tuple[ET.Element, ...] = ()  # targets of the <use> elements being drawn
        self.use_targets: Dict[ET.Element, _Target] = {}  # see examine()
        # see image_data(): (data, format) or (None, the reason to count), and the pixel size
        self.decoded: Dict[ET.Element, Tuple[Optional[bytes], str, Optional[Tuple[int, int]]]] = {}
        self.image_bytes_left = MAX_TOTAL_IMAGE_BYTES
        self.walk_depth = 0  # containers and <use> instances being walked
        self.page_svgs: Dict[ET.Element, _PageSvg] = {}  # see page_svg()

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    # -- container --------------------------------------------------------------------

    def xml(self) -> bytes:
        data = self.data
        if data[:2] == b"\x1f\x8b" or is_zlib_header(data):
            xml, problem = inflate(data, MAX_XML_BYTES)
            if problem == "too large":
                raise ValueError(f"Stylus Labs Write document inflates to more than {MAX_XML_BYTES // (1024 * 1024)} "
                                 "MB; refused")
            if problem == "damaged":
                if not xml.strip():
                    raise ValueError("not a Stylus Labs Write document: the compressed stream is damaged")
                self.warn("The compressed file is truncated or damaged; only its readable part was converted")
            elif problem == "trailer":
                self.warn("The compressed file's gzip checksum or length does not match its content; "
                          "the content was converted as it is")
            return xml
        if len(data) > MAX_XML_BYTES:
            raise ValueError(f"Stylus Labs Write document is larger than {MAX_XML_BYTES // (1024 * 1024)} MB; refused")
        if data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] != b"<" and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
            raise ValueError("not a Stylus Labs Write document: neither SVG nor gzip-compressed SVG")
        return data

    def read(self) -> Document:
        parser = _TreeParser()
        try:
            root = parser.parse(self.xml())
        except _Refused:
            raise ValueError("not a Stylus Labs Write document: XML entity declarations and internal DTD "
                             "subsets are not allowed") from None
        assert root is not None
        self.root = root
        pages = self.find_pages(root)
        if not pages and not self.is_write(root):
            raise ValueError("not a Stylus Labs Write document: an SVG without Write's pages "
                             "(no g.write-content group)")
        if parser.problem:
            self.warn(f"The file is damaged ({parser.problem}); the content before the damage was converted")
        self.doc.title = self.title(root)
        if len(pages) > MAX_PAGES:
            self.warn(f"The document has more than {MAX_PAGES} pages; the pages after page {MAX_PAGES} were dropped")
            pages = pages[:MAX_PAGES]
        for content, page_svg in pages:
            self.doc.pages.append(self.read_page(content, page_svg))
        if not self.doc.pages:
            self.warn("The document has no pages")
        self.counts.flush(self.doc)
        return self.doc

    # -- structure ------------------------------------------------------------------------

    @staticmethod
    def is_content(el: ET.Element) -> bool:
        if el.tag != "g":
            return False
        if CLASS_CONTENT in _classes(el):
            return True
        # Write 2: <g id="page_1" width height xruling yruling marginLeft papercolor rulecolor>
        return bool(_PAGE_ID.match(el.get("id") or "")) and any(
            el.get(k) is not None for k in ("xruling", "yruling", "papercolor", "rulecolor"))

    def find_pages(self, root: ET.Element) -> List[Tuple[ET.Element, Optional[ET.Element]]]:
        """``(content group, its nearest svg element)`` of every page, in document order."""
        pages: List[Tuple[ET.Element, Optional[ET.Element]]] = []
        stack: List[Tuple[ET.Element, Optional[ET.Element]]] = [(root, None)]
        while stack:
            el, svg = stack.pop()
            if self.is_content(el):
                pages.append((el, svg))
                continue
            inner = el if el.tag == "svg" else svg
            stack.extend((child, inner) for child in reversed(el))
        return pages

    @staticmethod
    def is_write(root: ET.Element) -> bool:
        """Write's own markers without pages (an empty or damaged document)."""
        if root.get("id") == "write-document" or CLASS_PAGE in _classes(root):
            return True
        return any(el.tag == "script" and (el.get("type") or "").strip() == "text/writeconfig"
                   for el in root.iter("script"))

    @staticmethod
    def title(root: ET.Element) -> str:
        candidates = [c for c in root if c.tag == "title"]
        for head in (c for c in root if c.tag == "head"):
            candidates += [c for c in head if c.tag == "title"]
        for el in candidates:
            text = " ".join(_words("".join(el.itertext()), 200))  # 200 words are more than the 200 characters kept
            if text and text not in BOILERPLATE_TITLES:
                return text[:200]
        return ""

    # -- pages --------------------------------------------------------------------------------

    @staticmethod
    def size_of(el: ET.Element) -> Optional[Tuple[float, float]]:
        """The element's ``width`` and ``height`` when both are valid page sides."""
        w, h = _length(el.get("width")), _length(el.get("height"))
        if w is not None and h is not None and 0 < w <= MAX_PAGE_UNITS and 0 < h <= MAX_PAGE_UNITS:
            return w, h
        return None

    def page_svg(self, svg: ET.Element) -> _PageSvg:
        """The page ``svg``'s size, ``viewBox`` and inherited properties, parsed at its first page
        only (re-parsing a shared ``svg`` with long attribute values page by page would cost
        pages x length)."""
        info = self.page_svgs.get(svg)
        if info is None:
            props = {k: v for k, v in _style(svg, self.counts).items() if k in INHERITED}
            info = _PageSvg(self.size_of(svg), self.viewbox_spec(svg), props)
            self.page_svgs[svg] = info
        return info

    def page_size(self, content: ET.Element, page_svg: Optional[ET.Element]) -> Tuple[float, float]:
        size = self.size_of(content)
        if size is None and page_svg is not None:
            size = self.page_svg(page_svg).size
        if size is None:
            for el in content.iter("rect"):
                if "pagerect" in _classes(el):
                    size = self.size_of(el)
                    break
        if size is not None:
            return size
        self.counts.add("page_size")
        return DEFAULT_PAGE_UNITS

    def page_matrix(self, page_svg: Optional[ET.Element], w: float, h: float) -> Matrix:
        """Write units -> pt for the page, with a ``viewBox`` on the page ``svg`` honoured."""
        m: Matrix = (PT_PER_UNIT, 0.0, 0.0, PT_PER_UNIT, 0.0, 0.0)
        if page_svg is None:
            return m
        return svgpath.multiply(m, self.viewbox_matrix(self.page_svg(page_svg).viewbox, w, h))

    def viewbox(self, svg: ET.Element, w: float, h: float) -> Matrix:
        return self.viewbox_matrix(self.viewbox_spec(svg), w, h)

    @staticmethod
    def viewbox_spec(svg: ET.Element) -> Optional[Tuple[List[float], List[str]]]:
        """A valid ``viewBox`` with the words of ``preserveAspectRatio``, or ``None``."""
        box = svgpath.parse_numbers(svg.get("viewBox") or "", 4)
        if not box or len(box) != 4 or not (box[2] > 0 and box[3] > 0) or not all(math.isfinite(v) for v in box):
            return None
        return box, _aspect_ratio(svg)

    @staticmethod
    def viewbox_matrix(spec: Optional[Tuple[List[float], List[str]]], w: float, h: float) -> Matrix:
        """The viewport transform of :meth:`viewbox_spec`'s result for a ``w`` x ``h`` viewport."""
        if spec is None:
            return IDENTITY
        box, par = spec
        sx, sy = w / box[2], h / box[3]
        if par and par[0] == "none":
            return sx, 0.0, 0.0, sy, -box[0] * sx, -box[1] * sy
        s = max(sx, sy) if "slice" in par else min(sx, sy)
        align = par[0] if par else "xMidYMid"
        fx = 0.0 if "xMin" in align else (1.0 if "xMax" in align else 0.5)
        fy = 0.0 if "YMin" in align else (1.0 if "YMax" in align else 0.5)
        return s, 0.0, 0.0, s, -box[0] * s + (w - box[2] * s) * fx, -box[1] * s + (h - box[3] * s) * fy

    def read_page(self, content: ET.Element, page_svg: Optional[ET.Element]) -> Page:
        w, h = self.page_size(content, page_svg)
        page = Page(w * PT_PER_UNIT, h * PT_PER_UNIT)
        self.page = page
        matrix = self.page_matrix(page_svg, w, h)
        # presentation attributes of the page svg are inherited too
        props = dict(self.page_svg(page_svg).props) if page_svg is not None else {}
        ctx = self.context(content, _Context(matrix, props, 1.0))
        ruling: Optional[_Ruling] = None
        if ctx is not None:
            for child in content:
                if CLASS_RULELINE in _classes(child):
                    if ruling is None and child.tag == "g":
                        ruling = self.background(child, ctx, page)
                    continue  # Write 2 rule lines are classed paths among the strokes
                self.walk(child, ctx)
        self.paper(page, content, ruling)
        return page

    def paper(self, page: Page, content: ET.Element, ruling: Optional["_Ruling"]) -> None:
        """Page Setup and ``g.ruleline`` -> ``Page.paper`` plus a generated paper PDF (marked as
        stock paper), so that writers which draw paper keep the rules where the handwriting
        sits.  Write's standard ruling (``write-std-ruling``, or no ``g.ruleline`` as in Write 2)
        is drawn from ``xruling``, ``yruling``, ``marginLeft``, ``papercolor`` and the
        alpha-first ``rulecolor``; a custom ruling made only of circles is a dot paper drawn as
        found; any other custom ruling is reported and only its paper colour is kept."""
        w, h = page.width, page.height
        paper_rgba = parse_write_color(content.get("papercolor")) or (ruling.page_fill if ruling else None) \
            or (1.0, 1.0, 1.0, 1.0)
        paper_rgb = blend(paper_rgba, (1.0, 1.0, 1.0))
        if ruling is None or ruling.standard:
            rule = parse_write_color(content.get("rulecolor")) or parse_write_color(DEFAULT_RULE_COLOR)
            assert rule is not None
            visible = 1.0 if rule[3] > 0 else 0.0  # fully transparent rules are no ruling at all
            xr = max(0.0, _number(content.get("xruling")) or 0.0) * PT_PER_UNIT * visible
            yr = max(0.0, _number(content.get("yruling")) or 0.0) * PT_PER_UNIT * visible
            margin = max(0.0, _number(content.get("marginLeft")) or 0.0) * PT_PER_UNIT * visible
            if xr > 0 and yr > 0:
                page.paper = "grid"
            elif yr > 0:
                page.paper = "lined"  # vertical rules alone have no counterpart: plain paper
            if ruling is not None and (ruling.custom or ruling.grids or ruling.dots or ruling.dense):
                self.counts.add(ruling.custom_message())  # drawn on top of the standard ruling
            spec = Paper(w, h, paper_rgb, blend(rule, paper_rgb), PT_PER_UNIT, xr, yr, margin,
                         blend((1.0, 0.0, 0.0, rule[3]), paper_rgb))
        elif ruling.custom or ruling.rules:
            self.counts.add(ruling.custom_message())
            spec = Paper(w, h, paper_rgb)
        else:
            if ruling.grids or ruling.dots or ruling.dense:
                page.paper = "dotted"
            if ruling.dense:  # more dots than a paper may draw: the paper colour only
                self.counts.add("paper_dense")
                ruling.grids, ruling.dots = [], []
            grids = tuple(DotGrid(*g[:9], blend(g[9], paper_rgb)) for g in ruling.grids)  # type: ignore[misc]
            dots = tuple(Dot(x, y, r, blend(c, paper_rgb)) for x, y, r, c in ruling.dots)
            spec = Paper(w, h, paper_rgb, grids=grids, dots=dots)
        self.paper_background(page, spec)

    def paper_background(self, page: Page, spec: Paper) -> None:
        """Draw ``spec`` as the page's stock paper within the file's paper budgets."""
        if spec.is_blank():
            return
        count = spec.primitives()
        if count > PAPER_MAX_PRIMITIVES:  # rules or dots too fine for the page: its colour only
            self.counts.add("paper_dense")
            spec = Paper(spec.width, spec.height, spec.color)
            if spec.is_blank():
                return
            count = 0
        pdf_id = self.paper_pdfs.get(spec)
        if pdf_id is None:
            if len(self.paper_pdfs) >= MAX_PAPER_PDFS or count > self.paper_primitives_left:
                self.counts.add("paper_limit")
                return
            try:
                pdf = paper_pdf(spec)
            except ValueError:
                return
            self.paper_primitives_left -= count
            pdf_id = f"styluswrite-paper-{len(self.paper_pdfs) + 1}.pdf"
            self.doc.pdfs[pdf_id] = pdf
            self.paper_pdfs[spec] = pdf_id
        page.background = PdfBackground(pdf_id, 0)
        page.template_is_builtin = True

    def background(self, ruleline: ET.Element, parent: _Context, page: Page) -> "_Ruling":
        """What ``g.ruleline`` draws.  Images become page images (pdftowrite's raster pages);
        straight rule lines, the page rectangle, circles (one by one or as a ``<pattern>``
        filling a rectangle) are recorded; anything else makes the ruling custom.  A nested
        ``svg`` (pdftowrite's ``svg.page-background``) maps its ``viewBox`` as it does in
        the content.  Definitions (``defs``, ``symbol``, ``clipPath``, ...) draw nothing where
        they stand: they are only searched for patterns, and what they define is drawn where a
        ``<use>`` refers to it (cairo's, and so pdftowrite's, way of placing images and
        glyphs); a text that paints nothing (pdftowrite's invisible text layer) is skipped."""
        ruling = _Ruling(CLASS_STD_RULING in _classes(ruleline))
        ctx = self.context(ruleline, parent)
        if ctx is None:
            return ruling
        images = len(page.images)
        patterns: Dict[str, Tuple[ET.Element, _Context]] = {}
        fills: List[Tuple[ET.Element, _Context, str]] = []
        # (element, parent context, the targets of the <use> elements drawing it, rendered)
        stack: List[Tuple[ET.Element, _Context, Tuple[ET.Element, ...], bool]] = [
            (child, ctx, (), True) for child in reversed(list(ruleline))]
        while stack:
            el, outer, chain, rendered = stack.pop()
            tag = el.tag
            if tag in ("style", "script", "title", "desc", "metadata") or not isinstance(tag, str):
                continue
            inner = self.context(el, outer, quiet=not rendered)
            if inner is None:
                continue
            if tag == "pattern":
                if el.get("id"):
                    patterns.setdefault(el.get("id") or "", (el, inner))  # the first one, as SVG resolves it
            elif not rendered or tag in NOT_RENDERED:
                stack.extend((child, inner, chain, False) for child in reversed(list(el)))
            elif tag == "use":
                found = self.instance(el, inner, chain, images_only=ruling.custom)
                if found is not None:
                    targets, ictx, ichain = found
                    stack.extend((child, ictx, ichain, True) for child in reversed(targets))
            elif tag == "image":
                self.guarded(self.image, el, inner)
            elif tag in ("g", "a", "svg"):
                if tag == "svg":
                    inner = self.nested_svg(el, inner)
                stack.extend((child, inner, chain, True) for child in reversed(list(el)))
            elif tag == "rect" and "pagerect" in _classes(el):
                ruling.page_fill = parse_color(inner.props.get("fill"))
            elif tag in SHAPE_TAGS and _PAINT_URL.match((inner.props.get("fill") or "").strip()):
                url = _PAINT_URL.match((inner.props.get("fill") or "").strip())
                assert url is not None
                fills.append((el, inner, url.group(1)))
            elif tag == "path" and self.is_rule(el.get("d") or ""):
                ruling.rules += 1
            elif tag in ("circle", "ellipse") and self.ruling_dot(el, inner, ruling):
                pass
            elif tag == "text" and _unpainted(el, inner):
                pass
            else:
                ruling.custom = True
        # each pattern's circles are read once, however many rectangles it fills
        tiles: Dict[str, Optional[_DotTile]] = {}
        for el, inner, pid in fills:
            if pid not in tiles:
                tiles[pid] = self.dot_tile(patterns.get(pid))
            if not self.dot_pattern(tiles[pid], el, inner, page, ruling):
                ruling.custom = True
        ruling.images = len(page.images) - images
        return ruling

    def dot_style(self, el: ET.Element, ctx: _Context) -> Optional[Tuple[float, Optional[RGBA]]]:
        """``(radius, colour)`` of a circle drawn as a dot (a stroke widens it; a colour of
        ``None`` draws nothing); ``None`` when the radius is invalid."""
        r = _circle_radius(el)
        if r is None:
            return None
        color = self.paint(ctx, "fill", "black")
        stroke = self.paint(ctx, "stroke", "none")
        width = _length(ctx.props.get("stroke-width"))
        if stroke is not None and stroke[3] > 0 and (width is None or width > 0):
            r += (1.0 if width is None else width) / 2.0
            color = color if color is not None and color[3] > 0 else stroke
        return r, color

    def ruling_dot(self, el: ET.Element, ctx: _Context, ruling: "_Ruling") -> bool:
        """A circle of the ruling -> one dot (pt); ``False`` when it is not a valid circle."""
        style = self.dot_style(el, ctx)
        if style is None:
            return False
        r, color = style
        if color is None:
            return True  # draws nothing
        m = ctx.matrix
        x, y = svgpath.apply(m, _length(el.get("cx")) or 0.0, _length(el.get("cy")) or 0.0)
        r *= svgpath.scale_of(m)
        if not all(math.isfinite(v) and abs(v) <= MAX_COORD_PT for v in (x, y, r)):
            return False
        if ruling.dense:
            return True
        if r > 0 and color[3] > 0:
            ruling.dots.append((x, y, r, color))
            ruling.charge(1)
        return True

    def dot_tile(self, entry: Optional[Tuple[ET.Element, _Context]]) -> Optional["_DotTile"]:
        """A ``<pattern>`` made only of circles -> its tile (user units); ``None`` for any other
        pattern (or a missing one)."""
        if entry is None:
            return None
        pattern, pctx = entry
        if pattern.get("viewBox") or (pattern.get("patternTransform") or "").strip() or pattern.get("href") \
                or (pattern.get("patternContentUnits") or "userSpaceOnUse").strip() != "userSpaceOnUse":
            return None
        circles: List[Tuple[float, float, float, RGBA]] = []
        for child in pattern.iter():
            if child is pattern or child.tag in ("title", "desc"):
                continue
            if child.tag not in ("circle", "ellipse") or child.get("transform"):
                return None
            cctx = self.context(child, pctx)
            if cctx is None:
                continue
            style = self.dot_style(child, cctx)
            if style is None:
                return None
            r, color = style
            if color is not None and r > 0 and color[3] > 0:
                circles.append((_length(child.get("cx")) or 0.0, _length(child.get("cy")) or 0.0, r, color))
        tile = tuple(_length(pattern.get(k)) for k in ("x", "y", "width", "height"))
        user_space = (pattern.get("patternUnits") or "objectBoundingBox").strip() == "userSpaceOnUse"
        return _DotTile(tile, user_space, circles)  # type: ignore[arg-type]

    def dot_pattern(self, tile: Optional["_DotTile"], el: ET.Element, ctx: _Context, page: Page,
                    ruling: "_Ruling") -> bool:
        """A rectangle filled with a ``<pattern>`` of circles (Write's dot templates, gnnote's
        dot paper) -> dot grids clipped to the rectangle and the page; ``False`` for any other
        pattern or fill (a custom background).  Every circle of every filled rectangle is
        charged to the ruling's primitive budget; beyond it the ruling is dense and the
        remaining rectangles are only checked."""
        if tile is None or el.tag != "rect":
            return False
        m = ctx.matrix
        if abs(m[1]) > 1e-9 * abs(m[0]) or abs(m[2]) > 1e-9 * abs(m[3]) or not (m[0] > 0 and m[3] > 0):
            return False  # rotated, sheared or mirrored: not a page ruling
        if ruling.dense:
            return True  # too many dots already: the paper is drawn without them
        box = [_length(el.get(k)) for k in ("x", "y", "width", "height")]
        rx, ry, rw, rh = (v or 0.0 for v in box)  # missing (or percentage) sizes draw nothing
        px, py, pw, ph = (v or 0.0 for v in tile.box)
        if not tile.user_space:
            px, py, pw, ph = rx + px * rw, ry + py * rh, pw * rw, ph * rh
        if not (rw > 0 and rh > 0 and pw > 0 and ph > 0):
            return True  # nothing is drawn
        sx, sy = m[0], m[3]
        left, right = max(0.0, sx * rx + m[4]), min(page.width, sx * (rx + rw) + m[4])
        top, bottom = max(0.0, sy * ry + m[5]), min(page.height, sy * (ry + rh) + m[5])
        for cx, cy, r, color in tile.circles:
            grid = (sx * (px + cx) + m[4], sy * (py + cy) + m[5], sx * pw, sy * ph, r * math.sqrt(sx * sy),
                    left, top, right, bottom, color)
            if not all(math.isfinite(v) for v in grid[:9]):  # type: ignore[arg-type]
                return False
            count = DotGrid(*grid[:9], (0.0, 0.0, 0.0)).count()  # type: ignore[arg-type]
            if count:
                ruling.grids.append(grid)
            ruling.charge(max(1, count))  # a grid that draws nothing still cost its work
            if ruling.dense:
                return True
        return True

    @staticmethod
    def is_rule(d: str) -> bool:
        """A single straight line, as Write draws rule lines and margins (``M0 40 l900 0``)."""
        if len(d) > 200:
            return False
        path = svgpath.parse_path(d, max_points=8)
        return not path.error and len(path.subpaths) == 1 and len(path.subpaths[0].points) == 2 \
            and not path.subpaths[0].curved

    # -- walking --------------------------------------------------------------------------------

    def context(self, el: ET.Element, parent: _Context, quiet: bool = False) -> Optional[_Context]:
        """The context ``el`` establishes for itself and its children; ``None`` when it is not
        rendered (``display: none``).  ``quiet`` counts no warning (for definitions that are
        only searched)."""
        props = _style(el, None if quiet else self.counts)
        if (props.get("display") or "").strip() == "none":
            return None
        matrix = parent.matrix
        text = el.get("transform")
        if text is not None:
            t = svgpath.parse_transform(text)
            if t is None:
                if not quiet:
                    self.counts.add("transform")
            else:
                matrix = svgpath.multiply(matrix, t)
        opacity = parent.opacity
        own = _opacity(props.get("opacity"))
        if own is not None:
            opacity *= own
        if ("clip-path" in props or "mask" in props) and not quiet:
            if props.get("clip-path", "none").strip() != "none" or props.get("mask", "none").strip() != "none":
                self.counts.add("clipped")
        inherited = parent.props
        changes = {k: v for k, v in props.items() if k in INHERITED}
        if changes:
            inherited = dict(inherited)
            inherited.update(changes)
        return _Context(matrix, inherited, opacity)

    def walk(self, el: ET.Element, parent: _Context) -> None:
        tag = el.tag
        classes = _classes(el)
        if CLASS_BOOKMARK in classes:
            self.counts.add("bookmarks")
            return
        if tag in SILENT_TAGS or not isinstance(tag, str):
            return
        if tag == "foreignObject":
            self.counts.add("foreign_object")
            return
        ctx = self.context(el, parent)
        if ctx is None:
            return
        if tag == "use":
            found = self.instance(el, ctx, self.use_chain)
            if found is not None:
                targets, ictx, chain = found
                outer, self.use_chain = self.use_chain, chain
                self.walk_children(targets, ictx)
                self.use_chain = outer
            return
        if tag in CONTAINER_TAGS or tag == "svg":
            link = CLASS_HYPERREF in classes or tag == "a"
            if link and not self.link_depth:
                self.counts.add("links")  # Write wraps a link's ink in g.hyperref with an <a> hit area
            if tag == "svg":
                ctx = self.nested_svg(el, ctx)
            self.link_depth += link
            self.walk_children(list(el), ctx)
            self.link_depth -= link
            return
        if (ctx.props.get("visibility") or "").strip() in ("hidden", "collapse"):
            return
        if tag in SHAPE_TAGS:
            self.guarded(self.shape, el, ctx, classes)
        elif tag == "image":
            self.guarded(self.image, el, ctx)
        elif tag == "text":
            self.guarded(self.text, el, ctx)
        else:
            self.counts.add("unknown")

    def walk_children(self, children: List[ET.Element], ctx: _Context) -> None:
        """Walk the children of a container or ``<use>`` instance.  Without ``<use>`` the XML
        depth limit bounds the recursion; instances add their targets' depth, so the walk
        stops at :data:`MAX_DEPTH` levels."""
        if self.walk_depth >= MAX_DEPTH:
            self.counts.add("use_limit")
            return
        self.walk_depth += 1
        for child in children:
            if self.is_content(child):
                continue  # never a page inside a page
            self.walk(child, ctx)
        self.walk_depth -= 1

    def element_by_id(self, ident: str) -> Optional[ET.Element]:
        """The document's first element with this ``id`` (the map is built at the first call)."""
        if self.ids is None:
            self.ids = {}
            if self.root is not None:
                for el in self.root.iter():
                    key = el.get("id")
                    if key and key not in self.ids:
                        self.ids[key] = el
        return self.ids.get(ident)

    def instance(self, use: ET.Element, ctx: _Context, chain: Tuple[ET.Element, ...], images_only: bool = False
                 ) -> Optional[Tuple[List[ET.Element], _Context, Tuple[ET.Element, ...]]]:
        """What a ``<use>`` (whose own context is ``ctx``) draws, as SVG renders it: the
        referenced element as if it were a child of the ``use``, after the use's transform and
        a translation by its ``x`` / ``y``; a ``symbol`` or ``svg`` target maps its ``viewBox``
        into the use's ``width`` / ``height`` (else its own) and gives its children.  Returns
        ``(elements, their parent context, chain)`` with the target appended to ``chain``, or
        ``None`` when nothing is drawn: a reference to another file, to a missing element, to
        one that draws nothing (definitions, a page), to the use itself or an element holding
        it, to a target already being drawn (all counted as ``use``), more than
        :data:`MAX_USE_DEPTH` reuses deep or beyond :data:`MAX_USE_ELEMENTS` reused elements
        or :data:`MAX_USE_BYTES` reused characters per file (``use_limit``; the first instance
        over a budget ends all reuse), and with ``images_only`` a target holding no image (a
        glyph on a background that is custom anyway).  Each target is examined once and each
        instance is charged its target's element count and the length of its attribute values
        and texts, which drawing it parses again (an image's data excepted: it is decoded and
        measured once per element)."""
        href = (use.get("href") or "").strip()
        target = self.element_by_id(href[1:].strip()) if href.startswith("#") else None
        if target is None or not isinstance(target.tag, str) or (target.tag in SILENT_TAGS and target.tag != "symbol") \
                or self.is_content(target) or any(t is target for t in chain):
            self.counts.add("use")
            return None
        if len(chain) >= MAX_USE_DEPTH:
            self.counts.add("use_limit")
            return None
        info = self.use_targets.get(target)
        if info is None:
            info = self.examine(target)
            if info is None:
                self.counts.add("use_limit")
                return None
        if use in info.uses:  # the target is the use or holds it: a cycle
            self.counts.add("use")
            return None
        if images_only and not info.has_image and not info.uses:
            return None
        if info.size > self.use_left or info.chars > self.use_bytes_left:
            self.use_left = self.use_bytes_left = 0
            self.counts.add("use_limit")
            return None
        self.use_left -= info.size
        self.use_bytes_left -= info.chars
        chain = chain + (target,)
        x, y = _length(use.get("x")) or 0.0, _length(use.get("y")) or 0.0
        base = _Context(svgpath.multiply(ctx.matrix, svgpath.translate(x, y)), ctx.props, ctx.opacity)
        if target.tag not in ("symbol", "svg"):
            return [target], base, chain
        inner = self.context(target, base)
        if inner is None:
            return None
        m = inner.matrix
        if target.tag == "svg":
            m = svgpath.multiply(m, svgpath.translate(_length(target.get("x")) or 0.0, _length(target.get("y")) or 0.0))
        sizes = []
        for key in ("width", "height"):
            v = _length(use.get(key))
            sizes.append(v if v is not None else _length(target.get(key)))
        w, h = sizes
        if (w is not None and w <= 0) or (h is not None and h <= 0):
            return None  # an empty viewport draws nothing
        if w and h:
            m = svgpath.multiply(m, self.viewbox(target, w, h))
        return list(target), _Context(m, inner.props, inner.opacity), chain

    def examine(self, target: ET.Element) -> Optional["_Target"]:
        """What drawing a ``<use>`` target costs and holds (see :class:`_Target`), remembered;
        the examination is charged to the reuse budget, ``None`` beyond it."""
        size, chars, has_image, uses = 0, 0, False, set()
        for node in target.iter():
            size += 1
            if size > self.use_left:
                self.use_left = 0
                return None
            chars += len(node.text or "")
            if node is not target:  # the target's own tail is text of its parent
                chars += len(node.tail or "")
            image = node.tag == "image"
            for key, value in node.attrib.items():
                if not (image and key == "href"):  # image data is decoded and measured once
                    chars += len(value)
            if image:
                has_image = True
            elif node.tag == "use":
                uses.add(node)
        self.use_left -= size
        info = _Target(size, chars, has_image, frozenset(uses))
        self.use_targets[target] = info
        return info

    def guarded(self, handler: Callable[..., None], *args: object) -> None:
        """Run one element's handler; an arithmetic surprise in hostile numbers (an overflow
        the handler did not foresee) skips that element instead of failing the document."""
        try:
            handler(*args)
        except (ArithmeticError, ValueError):
            self.counts.add("invalid")

    def nested_svg(self, el: ET.Element, ctx: _Context) -> _Context:
        x, y = _length(el.get("x")) or 0.0, _length(el.get("y")) or 0.0
        m = svgpath.multiply(ctx.matrix, svgpath.translate(x, y))
        w, h = _length(el.get("width")), _length(el.get("height"))
        if w and h and w > 0 and h > 0:
            m = svgpath.multiply(m, self.viewbox(el, w, h))
        return _Context(m, ctx.props, ctx.opacity)

    # -- shapes -----------------------------------------------------------------------------------

    def geometry(self, el: ET.Element, tag: str, tolerance: float, budget: int) -> Optional[svgpath.PathData]:
        """The element's outline in its user units (at most ``budget`` points), or ``None``
        when it has none."""
        if tag == "path":
            return svgpath.parse_path(el.get("d"), tolerance, budget)
        if tag in ("polyline", "polygon"):
            # the numbers are taken in pairs; whatever stands between them is skipped
            data = svgpath.PathData()
            pts: List[Tuple[float, float]] = []
            numbers = _NUMBER_ITER.finditer(el.get("points") or "")
            for mx in numbers:
                my = next(numbers, None)
                if my is None:
                    break
                if len(pts) >= budget:
                    data.truncated = True
                    break
                pts.append((float(mx.group(0)), float(my.group(0))))
            if len(pts) > 1 or (pts and tag == "polygon"):  # one point of a polyline draws nothing
                data.subpaths.append(svgpath.Subpath(pts, closed=(tag == "polygon")))
            data.cost = len(pts)
            return data
        if tag == "line":
            if budget < 2:
                return svgpath.PathData(truncated=True)
            vals = [_length(el.get(k)) or 0.0 for k in ("x1", "y1", "x2", "y2")]
            return svgpath.PathData([svgpath.Subpath([(vals[0], vals[1]), (vals[2], vals[3])])])
        if tag == "rect":
            x, y = _length(el.get("x")) or 0.0, _length(el.get("y")) or 0.0
            w, h = _length(el.get("width")), _length(el.get("height"))
            if not (w and h and w > 0 and h > 0):
                return None
            rx, ry = _length(el.get("rx")), _length(el.get("ry"))
            rx = ry if rx is None else rx
            ry = rx if ry is None else ry
            rx, ry = min(max(rx or 0.0, 0.0), w / 2), min(max(ry or 0.0, 0.0), h / 2)
            if rx > 0 and ry > 0:
                d = (f"M{x + rx} {y} H{x + w - rx} A{rx} {ry} 0 0 1 {x + w} {y + ry} V{y + h - ry} "
                     f"A{rx} {ry} 0 0 1 {x + w - rx} {y + h} H{x + rx} A{rx} {ry} 0 0 1 {x} {y + h - ry} "
                     f"V{y + ry} A{rx} {ry} 0 0 1 {x + rx} {y} Z")
                return svgpath.parse_path(d, tolerance, budget)
            if budget < 4:
                return svgpath.PathData(truncated=True)
            return svgpath.PathData([svgpath.Subpath([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], closed=True)])
        if tag in ("circle", "ellipse"):
            cx, cy = _length(el.get("cx")) or 0.0, _length(el.get("cy")) or 0.0
            if tag == "circle":
                rx = ry = _length(el.get("r")) or 0.0
            else:
                rx, ry = _length(el.get("rx")) or 0.0, _length(el.get("ry")) or 0.0
            if not (rx > 0 and ry > 0):
                return None
            d = f"M{cx + rx} {cy} A{rx} {ry} 0 1 1 {cx - rx} {cy} A{rx} {ry} 0 1 1 {cx + rx} {cy} Z"
            return svgpath.parse_path(d, tolerance, budget)
        return None

    def paint(self, ctx: _Context, key: str, default: str) -> Optional[RGBA]:
        """The ``stroke`` / ``fill`` colour with its opacities applied; ``None`` for no paint."""
        value = (ctx.props.get(key) or default).strip()
        if value.startswith("url("):
            fallback = value[value.find(")") + 1:].strip()
            if not fallback or fallback == "none":
                self.counts.add("paint_server")
                return None
            value = fallback
        if value == "none":
            return None
        if value.lower() == "currentcolor":
            value = (ctx.props.get("color") or "black").strip()
        color = parse_color(value)
        if color is None:
            self.counts.add("color")
            color = parse_color(default) if default != "none" else None
            if color is None:
                return None
        alpha = color[3] * ctx.opacity
        own = _opacity(ctx.props.get(key + "-opacity"))
        if own is not None:
            alpha *= own
        return color[0], color[1], color[2], alpha

    def shape(self, el: ET.Element, ctx: _Context, classes: List[str]) -> None:
        tag = el.tag
        m = ctx.matrix
        scale = svgpath.scale_of(m)
        if not scale > 0 or not math.isfinite(scale):
            self.counts.add("invalid")
            return
        stroke_color = self.paint(ctx, "stroke", "none")
        fill_color = None if tag == "line" else self.paint(ctx, "fill", "black")
        if stroke_color is not None and stroke_color[3] <= 0:
            stroke_color = None
        if fill_color is not None and fill_color[3] <= 0:
            fill_color = None
        sw = _length(ctx.props.get("stroke-width"))
        stroke_width = 1.0 if sw is None else sw
        if stroke_width <= 0:
            stroke_color = None
        if stroke_color is None and fill_color is None:
            return  # invisible (e.g. the transparent hit area of a link)
        outputs = (stroke_color is not None) + (fill_color is not None)  # each makes its own points
        budget = min(MAX_POINTS_PER_PATH, self.points_left // outputs)
        geo = self.geometry(el, tag, CURVE_TOLERANCE_PT / scale, budget)
        if geo is None:
            return
        if geo.truncated:
            if budget < MAX_POINTS_PER_PATH:  # the file's budget ran out: drop the shape
                self.points_left = 0
                self.counts.add("point_limit")
                return
            self.counts.add("path_limit")
        if geo.error:
            self.counts.add("path_error")
        # vertices and commands that add none (svgpath charges Z and lone movetos) are paid from
        # the file's budget for the fill and for the stroke, and so is every stroke or ring made
        left = self.points_left - max(geo.cost, geo.point_count) * outputs
        per_subpath = STROKE_COST * outputs
        if left < per_subpath * len(geo.subpaths):
            keep = max(0, left // per_subpath)
            self.counts.add("stroke_limit", len(geo.subpaths) - keep)
            del geo.subpaths[keep:]
        self.points_left = max(0, left - per_subpath * len(geo.subpaths))
        if not geo.subpaths:
            return
        page = self.page
        assert page is not None
        subpaths = [[svgpath.apply(m, x, y) for x, y in sub.points] for sub in geo.subpaths]
        if not all(math.isfinite(v) and abs(v) <= MAX_COORD_PT for pts in subpaths for xy in pts for v in xy):
            self.counts.add("invalid")
            return
        # gnnote's own files say what a stroke is; otherwise a highlight class, multiply blending
        # or translucency (below) make a highlighter
        marked = (el.get(KIND_ATTRIBUTE) or "").strip().lower()
        highlight: Optional[bool] = {"pen": False, "highlighter": True}.get(marked)
        if highlight is None and (any("highlight" in c for c in classes)
                                  or (ctx.props.get("mix-blend-mode") or "").strip().lower() == "multiply"):
            highlight = True
        if CLASS_FLAT_PEN in classes and fill_color is not None:
            stroke = self.flat_pen(geo, subpaths, fill_color, stroke_width * scale, highlight)
            if stroke is not None:
                page.strokes.append(stroke)
                fill_color = None
                if stroke_color is None:
                    return
            else:
                self.counts.add("flat_fallback")
        if fill_color is not None:
            rings = [[Point(x, y, 0.0) for x, y in pts] for pts in subpaths if len(pts) >= 3]
            if rings:
                page.strokes.append(Stroke(list(rings[0]), color=fill_color, kind="fill", pen=None, width=0.0,
                                           outline=rings))
        if stroke_color is not None:
            width = min(MAX_SIZE_PT, stroke_width * scale)
            kind = _kind(highlight, stroke_color)
            pen = "ballpoint" if CLASS_STROKE_PEN in classes else None
            for sub, pts in zip(geo.subpaths, subpaths):
                if sub.closed and len(pts) > 1 and pts[0] != pts[-1]:
                    pts = pts + [pts[0]]
                page.strokes.append(Stroke([Point(x, y, width) for x, y in pts], color=stroke_color, kind=kind,
                                           pen=pen, width=width))

    def flat_pen(self, geo: svgpath.PathData, subpaths: List[List[Tuple[float, float]]], color: RGBA,
                 nominal: float, highlight: Optional[bool]) -> Optional[Stroke]:
        """Write's variable-width pen: one outline whose vertex *i* faces vertex *n - 1 - i*.  A
        shortened outline (over the point limit) has lost its return side, so its vertices no
        longer pair up: ``None``, and it stays a filled shape."""
        if len(geo.subpaths) != 1 or geo.subpaths[0].curved or geo.error or geo.truncated:
            return None
        pts = subpaths[0]
        if geo.subpaths[0].closed and len(pts) > 2 and pts[0] == pts[-1]:
            pts = pts[:-1]
        n = len(pts)
        if n < 2 or n % 2:
            return None
        points = []
        for i in range(n // 2):
            (ax, ay), (bx, by) = pts[i], pts[n - 1 - i]
            points.append(Point((ax + bx) / 2.0, (ay + by) / 2.0, min(MAX_SIZE_PT, math.hypot(ax - bx, ay - by))))
        return Stroke(points, color=color, kind=_kind(highlight, color), pen="fountain",
                      width=min(MAX_SIZE_PT, nominal))

    # -- images -----------------------------------------------------------------------------------

    def image_data(self, el: ET.Element) -> Tuple[Optional[bytes], str, Optional[Tuple[int, int]]]:
        """``(data, format, pixel size)`` of an ``<image>``'s PNG / JPEG ``data:`` URI, or
        ``(None, the reason to count, None)``; decoded and measured once per element (both
        scan the whole data), so a reused image shares its bytes and costs no more work."""
        cached = self.decoded.get(el)
        if cached is not None:
            return cached
        href = (el.get("href") or "").strip()
        header, comma, payload = href[5:].partition(",") if href[:5].lower() == "data:" else ("", "", "")
        result: Tuple[Optional[bytes], str, Optional[Tuple[int, int]]]
        if not comma:
            result = None, ("image_external" if href else "image_data"), None
        elif ";base64" not in header.lower():
            result = None, "image_format", None  # URL-encoded data is SVG or text, never PNG / JPEG
        elif len(payload) > MAX_IMAGE_BYTES * 4 // 3 + 4096:
            result = None, "image_data", None
        else:
            try:
                # b64decode(validate=False) skips white space and other characters outside the
                # alphabet itself (joining the words of payload.split() cost 20 bytes per character)
                data = base64.b64decode(payload.encode("ascii", "ignore"), validate=False)
            except (binascii.Error, ValueError):
                data = b""
            fmt = sniff_image(data)
            if fmt in ("png", "jpeg"):
                result = data, fmt, image_pixel_size(data)
            else:
                result = None, ("image_format" if data else "image_data"), None
        self.decoded[el] = result
        return result

    def image(self, el: ET.Element, ctx: _Context) -> None:
        data, fmt, pixels = self.image_data(el)
        if data is None:
            self.counts.add(fmt)
            return
        if len(data) > self.image_bytes_left:
            self.counts.add("image_limit")
            return
        x, y = _length(el.get("x")) or 0.0, _length(el.get("y")) or 0.0
        w, h = _length(el.get("width")), _length(el.get("height"))
        if w is None and h is None and pixels:
            w, h = float(pixels[0]), float(pixels[1])
        elif pixels and (w is None or h is None):
            w = w if w is not None else h * pixels[0] / pixels[1]  # type: ignore[operator]
            h = h if h is not None else w * pixels[1] / pixels[0]
        if not (w and h and w > 0 and h > 0):
            self.counts.add("invalid")
            return
        par = _aspect_ratio(el)
        if pixels and par and par[0] != "none":
            aspect = pixels[0] / pixels[1]
            if "slice" in par:
                self.counts.add("image_slice")
            else:  # meet: the picture is fitted into the box and aligned
                fw, fh = (w, w / aspect) if w / h < aspect else (h * aspect, h)
                fx = 0.0 if "xMin" in par[0] else (1.0 if "xMax" in par[0] else 0.5)
                fy = 0.0 if "YMin" in par[0] else (1.0 if "YMax" in par[0] else 0.5)
                x, y, w, h = x + (w - fw) * fx, y + (h - fh) * fy, fw, fh
        sx, sy, rotation, sheared = _decompose(ctx.matrix)
        if sheared:
            self.counts.add("sheared")
        cx, cy = svgpath.apply(ctx.matrix, x + w / 2.0, y + h / 2.0)
        bw, bh = w * sx, h * sy
        if not all(math.isfinite(v) and abs(v) <= MAX_COORD_PT for v in (cx, cy, bw, bh)) or not (bw > 0 and bh > 0):
            self.counts.add("invalid")
            return
        if abs(rotation) < 1e-9:
            rotation = 0.0
        assert self.page is not None
        self.image_bytes_left -= len(data)
        self.page.images.append(Image(cx - bw / 2.0, cy - bh / 2.0, bw, bh, data, fmt=fmt, rotation=rotation % 360.0))

    # -- text -------------------------------------------------------------------------------------

    def text(self, el: ET.Element, ctx: _Context) -> None:
        """``<text>`` -> :class:`TextBox`.  Lines come from the baselines: a ``tspan`` whose
        ``y`` or ``dy`` moves the baseline by at least half a line starts a new line (smaller
        moves are super- and subscripts), and a jump of about two or more line heights between
        lines adds the empty lines in between (an empty ``tspan`` has no characters, so its
        position is ignored, as browsers do).  A ``tspan`` before the line's first visible
        character places the line instead (white space drawn at the old place is dropped; with
        no ``x`` it stays, as it moves the text after it), except that with
        ``xml:space="preserve"`` a line of only spaces followed by a new line at the same ``x``
        is a line of its own (gnnote's writer writes such lines).  Every styled chunk becomes a
        run."""
        preserve = el.get("space") == "preserve"
        drawing = _drawing_tspans(el, preserve)
        size_units = self.font_units(ctx)
        y0 = _first_length(el.get("y"))
        lines: List[_TextLine] = [_TextLine(_first_length(el.get("x")), y0)]
        current_y = y0  # the baseline the next character sits on
        ink = False  # whether any line has visible text yet

        def add(text: Optional[str], nctx: _Context) -> None:
            nonlocal ink
            if text:  # SVG white space handling (xml:space default or preserve)
                text = text.replace("\t", " ").replace("\r", " ").replace("\n", " ") if preserve else _collapse(text)
                if text:
                    line = lines[-1]
                    line.runs.append(self.run(text, nctx))
                    if not line.ink and text.strip(" "):  # a no-break space is a drawn character
                        line.ink = ink = True

        def place(line: _TextLine, baseline: float, x: Optional[float]) -> None:
            """Nothing visible is on ``line`` yet: it starts at ``x`` (if given) on ``baseline``."""
            line.baseline = baseline
            if x is not None and x != line.x:
                line.x = x
                line.runs = []  # white space only, drawn at the old place

        def move(tspan: ET.Element, nctx: _Context) -> None:
            nonlocal current_y
            line = lines[-1]
            blank = not (line.ink if preserve else ink)  # nothing visible drawn on the line yet
            x = _list_head(tspan.get("x"))
            y, dy = _list_head(tspan.get("y")), _first_offset(tspan.get("dy"), self.font_units(nctx))
            if y is None and not dy:
                if x is not None and blank:
                    place(line, line.baseline, x)
                return
            current_y = (current_y if y is None else y) + (dy or 0.0)
            far = abs(current_y - line.baseline) >= 0.5 * TEXT_LINE * self.font_units(nctx)  # else a baseline shift
            if blank and not (far and preserve and line.runs and x is not None and abs(x - line.x) <= SAME_X):
                place(line, current_y, x)
            elif far:
                lines.append(_TextLine(line.x if x is None else x, current_y))

        def collect(node: ET.Element, nctx: _Context) -> None:
            add(node.text, nctx)
            for child in node:
                if child.tag == "tspan":
                    inner = self.context(child, nctx)
                    if inner is not None and child in drawing:
                        move(child, inner)
                        collect(child, inner)
                add(child.tail, nctx)

        collect(el, ctx)
        if not preserve:  # collapse the spaces at line ends, as SVG does (not the no-break ones)
            for line in lines:
                if line.runs:
                    line.runs[0].text = line.runs[0].text.lstrip(" ")
                    line.runs[-1].text = line.runs[-1].text.rstrip(" ")
        for line in lines:
            line.runs = [r for r in line.runs if r.text]
        while lines and not lines[0].runs:
            lines.pop(0)
        while lines and not lines[-1].runs:
            lines.pop()
        if not lines:
            self.counts.add("text_empty")
            return
        rows = self.blank_lines(lines, TEXT_LINE * size_units)
        runs: List[TextRun] = []
        for i, row in enumerate(rows):
            if i:
                last = runs[-1] if runs else row[0] if row else TextRun("")
                runs.append(TextRun("\n", **self.run_style(last)))
            runs.extend(row)
        text = "".join(r.text for r in runs)
        if not text.strip():
            self.counts.add("text_empty")
            return
        sx, sy, rotation, sheared = _decompose(ctx.matrix)
        if sheared:
            self.counts.add("sheared")
        scale = math.sqrt(sx * sy)
        size = min(MAX_SIZE_PT, size_units * scale)
        if not (size > 0 and math.isfinite(size)):
            self.counts.add("invalid")
            return
        w, h = estimate_text_extent(text, size)
        x, y = lines[0].x, lines[0].baseline
        anchor = (ctx.props.get("text-anchor") or "start").strip()
        align = {"middle": "center", "end": "right"}.get(anchor, "left")
        local_w = w / scale
        lx = x - (local_w / 2.0 if align == "center" else local_w if align == "right" else 0.0)
        tx, ty = svgpath.apply(ctx.matrix, lx, y - TEXT_ASCENT * size_units)
        if not all(math.isfinite(v) and abs(v) <= MAX_COORD_PT for v in (tx, ty, w, h)):
            self.counts.add("invalid")
            return
        color = runs[0].color if runs[0].color else (0.0, 0.0, 0.0, 1.0)
        assert self.page is not None
        self.page.texts.append(TextBox(tx, ty, w, h, text, runs=runs, color=color, size=size,
                                       rotation=rotation % 360.0 if abs(rotation) > 1e-9 else 0.0, align=align))
        self.counts.add("text")

    def blank_lines(self, lines: List["_TextLine"], nominal: float) -> List[List[TextRun]]:
        """The rows of a text: its lines with the empty lines their baselines skip put back.  A
        gap of about two line heights is one empty line.  The line height is the font's
        (``TEXT_LINE`` em, what gnnote's writer uses) when every gap is a whole number of it,
        else the text's own smallest line distance (two gaps or more), else the font's.
        Inferred empty lines are bounded per gap and per file."""
        gaps = [b.baseline - a.baseline for a, b in zip(lines, lines[1:])]
        positive = [g for g in gaps if g > 0]
        whole = nominal > 0 and all(abs(g / nominal - round(g / nominal)) < 0.02 for g in positive if g < 1e6 * nominal)
        base = nominal if whole or len(positive) < 2 else min(positive)
        rows: List[List[TextRun]] = [lines[0].runs]
        for gap, line in zip(gaps, lines[1:]):
            ratio = gap / base if base > 0 else 0.0
            blanks = min(MAX_BLANK_LINES, int(ratio + 0.25) - 1) if 1.75 <= ratio < math.inf else 0
            blanks = min(blanks, self.blank_lines_left)
            self.blank_lines_left -= blanks
            rows.extend([] for _ in range(blanks))
            rows.append(line.runs)
        return rows

    def font_units(self, ctx: _Context) -> float:
        size = _length(ctx.props.get("font-size"))
        return size if size is not None and size > 0 else DEFAULT_FONT_UNITS

    def run(self, text: str, ctx: _Context) -> TextRun:
        scale = svgpath.scale_of(ctx.matrix)
        weight = (ctx.props.get("font-weight") or "").strip().lower()
        bold = weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600)
        italic = (ctx.props.get("font-style") or "").strip().lower() in ("italic", "oblique")
        underline = "underline" in (ctx.props.get("text-decoration") or "")
        family = (ctx.props.get("font-family") or "").split(",")[0].strip().strip("'\"") or None
        color = self.paint(ctx, "fill", "black")
        return TextRun(text.replace("\n", " "), bold=bold, italic=italic, underline=underline, font=family,
                       size=min(MAX_SIZE_PT, self.font_units(ctx) * scale) or None, color=color)

    @staticmethod
    def run_style(run: TextRun) -> Dict[str, object]:
        return {"bold": run.bold, "italic": run.italic, "underline": run.underline, "font": run.font,
                "size": run.size, "color": run.color}


class _Target:
    """A ``<use>`` target: its element count and the characters of its attribute values and
    texts (an image's data aside), which each instance is charged; whether it holds an image;
    the ``<use>`` elements in it."""

    __slots__ = ("size", "chars", "has_image", "uses")

    def __init__(self, size: int, chars: int, has_image: bool, uses: FrozenSet[ET.Element]):
        self.size = size
        self.chars = chars
        self.has_image = has_image
        self.uses = uses


class _Ruling:
    """What a page's ``g.ruleline`` draws (see :meth:`_Reader.background`)."""

    __slots__ = ("standard", "custom", "rules", "dots", "grids", "dense", "page_fill", "primitives", "images")

    def __init__(self, standard: bool):
        self.standard = standard  # class write-std-ruling: Write draws it from the Page Setup values
        self.custom = False  # content gnnote cannot reproduce
        self.rules = 0  # straight rule lines
        self.dots: List[Tuple[float, float, float, RGBA]] = []  # x, y, r (pt), colour
        self.grids: List[Tuple[float, float, float, float, float, float, float, float, float, RGBA]] = []
        self.dense = False  # more dots than a paper may draw
        self.page_fill: Optional[RGBA] = None  # rect.pagerect's fill
        self.primitives = 0  # dots (and dot grids examined) so far
        self.images = 0  # page images it gave

    def custom_message(self) -> str:
        """The warning for a ruling gnnote cannot reproduce: its images were kept or not."""
        return "custom_background_images" if self.images else "custom_background"

    def charge(self, count: int) -> None:
        """Count ``count`` dots; beyond :data:`PAPER_MAX_PRIMITIVES` the ruling is dense and
        what was collected is dropped (the paper is drawn without dots)."""
        self.primitives += count
        if self.primitives > PAPER_MAX_PRIMITIVES:
            self.dense = True
            self.dots, self.grids = [], []


class _DotTile:
    """A ``<pattern>`` of circles: its ``x, y, width, height`` (``None`` when absent), whether
    they are in user space, and its circles ``(cx, cy, r, colour)`` in the tile."""

    __slots__ = ("box", "user_space", "circles")

    def __init__(self, box: Tuple[Optional[float], ...], user_space: bool,
                 circles: List[Tuple[float, float, float, RGBA]]):
        self.box = box
        self.user_space = user_space
        self.circles = circles


def _kind(highlight: Optional[bool], color: RGBA) -> str:
    """``"highlighter"`` or ``"pen"``: what the mark or class says (``highlight`` not ``None``),
    else translucent ink (alpha at most :data:`HIGHLIGHTER_MAX_ALPHA`) is a highlighter."""
    if highlight is None:
        highlight = color[3] <= HIGHLIGHTER_MAX_ALPHA
    return "highlighter" if highlight else "pen"


def _paint_alpha(ctx: _Context, key: str, default: str) -> float:
    """How opaque the ``fill`` / ``stroke`` of ``ctx`` paints (1 for a paint server or a colour
    that is not understood: they may paint)."""
    value = (ctx.props.get(key) or default).strip()
    if value == "none":
        return 0.0
    if value.lower() == "currentcolor":
        value = (ctx.props.get("color") or "black").strip()
    color = parse_color(value)
    if color is None:
        return 1.0
    own = _opacity(ctx.props.get(key + "-opacity"))
    return color[3] * ctx.opacity * (1.0 if own is None else own)


def _unpainted(text: ET.Element, ctx: _Context) -> bool:
    """Whether a ``<text>`` (whose context is ``ctx``) paints nothing: neither it nor any of its
    ``tspan`` elements has a visible fill or stroke (pdftowrite's text layer, kept for search)."""
    stack: List[Tuple[ET.Element, _Context]] = [(text, ctx)]
    while stack:
        node, nctx = stack.pop()
        if _paint_alpha(nctx, "fill", "black") > 0 or _paint_alpha(nctx, "stroke", "none") > 0:
            return False
        for child in node:
            if isinstance(child.tag, str):
                props = _style(child)
                if (props.get("display") or "").strip() == "none":
                    continue
                changes = {k: v for k, v in props.items() if k in INHERITED}
                opacity = _opacity(props.get("opacity"))
                stack.append((child, _Context(nctx.matrix, {**nctx.props, **changes},
                                              nctx.opacity * (1.0 if opacity is None else opacity))))
    return True


def _circle_radius(el: ET.Element) -> Optional[float]:
    """The radius of a ``circle`` (an ``ellipse``'s mean radius); ``None`` when invalid."""
    if el.tag == "circle":
        r = _length(el.get("r"))
    else:
        rx, ry = _length(el.get("rx")), _length(el.get("ry"))
        r = None if rx is None or ry is None else (rx + ry) / 2.0
    return r if r is not None and r >= 0 and math.isfinite(r) else None


class _TextLine:
    """One line of an SVG text while it is read: where it starts, its styled runs and whether
    one of them has a visible character."""

    __slots__ = ("x", "baseline", "runs", "ink")

    def __init__(self, x: float, baseline: float):
        self.x = x
        self.baseline = baseline
        self.runs: List[TextRun] = []
        self.ink = False


def _drawing_tspans(text: ET.Element, preserve: bool) -> Set[ET.Element]:
    """The elements inside a ``<text>`` whose characters (their own and their descendants')
    include one that is drawn: XML white space only counts with ``xml:space="preserve"``,
    default white space handling collapses it (a no-break space is always drawn).  One pass from the leaves up, so nested ``tspan``
    elements do not scan their content once per level."""
    def drawn(chunk: Optional[str]) -> bool:
        if not chunk:
            return False
        return preserve or bool(chunk.strip(XML_SPACE))

    found: Set[ET.Element] = set()
    for node in reversed(list(text.iter())):  # children before their parents
        if drawn(node.text) or any(child in found or drawn(child.tail) for child in node):
            found.add(node)
    return found


def _collapse(text: str) -> str:
    """SVG's default white space handling of a text chunk: newlines removed, then every run of
    spaces, tabs and carriage returns one space.  Only this XML white space collapses: a
    no-break space (or any other Unicode space) is a character SVG draws.  The text is split
    into words :data:`COLLAPSE_CHUNK` characters at a time (cut at a space), so a long text
    never holds an object per word."""
    text = text.replace("\n", "")
    if not text:
        return text
    edges = (" " if text[0] in XML_SPACE else "", " " if text[-1] in XML_SPACE else "")
    text = text.replace("\t", " ").replace("\r", " ")
    pieces: List[str] = []
    start, n = 0, len(text)
    while start < n:
        end = text.find(" ", start + COLLAPSE_CHUNK) if n - start > COLLAPSE_CHUNK else n
        end = n if end < 0 else end
        chunk = text[start:end]
        # str.split() splits at XML white space only in ASCII text (the other ASCII spaces are no XML characters)
        piece = " ".join(chunk.split() if chunk.isascii() else filter(None, chunk.split(" ")))
        if piece:
            pieces.append(piece)
        start = end
    if not pieces:
        return " "
    return edges[0] + " ".join(pieces) + edges[1]


def _list_head(text: Optional[str]) -> Optional[float]:
    """The first value of an ``x`` / ``y`` list (``"10 20 30"``) in Write units, else ``None``."""
    m = _LIST_HEAD.match(text or "")
    return _length(m.group(1)) if m else None


def _first_length(text: Optional[str]) -> float:
    """:func:`_list_head`, 0 when there is none."""
    value = _list_head(text)
    return 0.0 if value is None else value


def _first_offset(text: Optional[str], font_units: float) -> Optional[float]:
    """The first value of a ``dx`` / ``dy`` list in Write units (``em`` relative to the font)."""
    m = _LIST_HEAD.match(text or "")
    if m is None:
        return None
    token = m.group(1)
    if token.endswith("em"):
        value = _number(token[:-2])
        return None if value is None else value * font_units
    return _length(token)


def read_write_svg(data: bytes) -> Document:
    """Read a Stylus Labs Write document (``.svgz``, ``.svg`` or Write's single-file HTML).

    Raises :class:`ValueError` only for input that is not a Write document (an ordinary SVG
    included); damage and unsupported content become warnings on the returned document.
    """
    return _Reader(ensure_bytes(data, "read_write_svg")).read()

