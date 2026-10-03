"""Xournal++ ``.xopp`` / Xournal ``.xoj`` -> :class:`gnnote.model.Document` (tolerant reader).

Containers (``docs/xournalpp.md`` section 1): gzip-compressed XML (what every Xournal++
release writes), plain XML, or the ZIP-packaged variant (``content.xml`` plus
``attachments/``).  The XML is parsed with expat and a hand-written tree builder that refuses
any document type declaration or entity declaration (so entity expansion is impossible),
limits the nesting depth and the element count, and hands every finished ``<page>`` over to
the converter so its subtree can be freed.  A damaged or truncated file keeps everything that
was parsed before the damage, with a warning (Xournal++ recovers the same way).

Mapping (section 2):

* ``<page width height>``: points, top-left origin; content needs no transform.
* ``<background>``: ``solid`` -> ``Page.paper`` (plain / ruled, lined, staves -> lined /
  graph, isograph -> grid / dotted, isodotted -> dotted) plus, for a colour other than white,
  a generated one-page paper PDF so the colour survives; ``pdf`` -> :class:`PdfBackground`
  when the PDF is inside the file (ZIP variant), else plain paper and one warning (a gzip file
  only *references* ``<name>.xopp.bg.pdf`` or an absolute path); ``pixmap`` -> a full-page
  image below the content (attached, cloned from an earlier page, or a warning).
* ``<stroke tool color width [fill]>x y x y ...</stroke>``: ``width`` is the nominal width
  followed by one width per point except the last (the segment from point *i* to *i + 1* is
  drawn with width *i*); the model's last point repeats the previous width.  Fewer widths
  than points truncate the stroke and non-positive or NaN widths split it, exactly like
  Xournal++'s loader.  ``tool="highlighter"`` -> highlighter, ``tool="eraser"`` (whiteout
  strokes) is dropped with a warning, ``fill="N"`` adds a filled shape
  (``Stroke(kind="fill")``, alpha ``N / 255``) right after its outline stroke.
* ``<text>`` -> :class:`TextBox` (size, colour, font family, bold / italic from the Pango
  font name, ``align``; the box size is estimated); ``<image>`` -> :class:`Image` (PNG / JPEG,
  inline base64 or a ZIP attachment); ``<teximage>`` (LaTeX) -> a PDF :class:`Image`;
  ``<link>`` -> plain text.  File version 5 ``matrix`` attributes become rotations.
* Layers are merged in order (one warning); audio is dropped (one warning).
"""
from __future__ import annotations

import base64
import binascii
import math
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.parsers import expat

from .. import pdfutil
from ..codecutil import (BoundedZip, Counter, ensure_bytes, estimate_text_extent, image_pixel_size,
                         inflate_limited, sniff_image)
from ..model import RGBA, Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from . import (APPROXIMATED_STYLES, BACKGROUND_COLORS, DEFAULT_PAGE_SIZE, FILL_OUTLINE_WIDTH,
               NAMED_COLORS, PACKAGE_CONTENT, PAPER_FOR_STYLE)

__all__ = ["read_xopp", "MAX_XML_BYTES", "MAX_DEPTH", "MAX_ELEMENTS", "MAX_PAGES",
           "MAX_POINTS_PER_STROKE", "MAX_TOTAL_POINTS"]

MAX_XML_BYTES = 256 * 1024 * 1024  # inflated XML above this is refused
MAX_DEPTH = 64  # element nesting (a real file nests 5 deep)
MAX_ELEMENTS = 2_000_000  # XML elements per file
MAX_PAGES = 10_000
MAX_POINTS_PER_STROKE = 200_000
MAX_TOTAL_POINTS = 5_000_000
MAX_PAGE_SIDE = 1e6  # pt; anything larger (or not positive) is damage
ROOT_TAGS = ("xournal", "MrWriter")
TEXT_TAGS = frozenset(("stroke", "text", "image", "teximage", "link", "title"))
BOILERPLATE_TITLES = ("Xournal++ document", "Xournal document")

_NUMBER_RE = re.compile(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
_STYLE_WORDS = {
    "bold": "bold", "heavy": "bold", "black": "bold", "ultra-bold": "bold", "extra-bold": "bold",
    "semi-bold": "bold", "demi-bold": "bold", "italic": "italic", "oblique": "italic",
    "light": "", "ultra-light": "", "thin": "", "medium": "", "book": "", "regular": "",
    "normal": "", "condensed": "", "semi-condensed": "", "expanded": "", "small-caps": "",
}


# --------------------------------------------------------------------------------------
# attribute parsing


def _number(value: Optional[str]) -> Optional[float]:
    """The leading number of an attribute value (Xournal++ wrote ``"1ll"`` in some versions);
    ``nan`` / ``inf`` are numbers too.  ``None`` when there is none."""
    if value is None:
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    m = _NUMBER_RE.match(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _finite(value: Optional[str], default: float) -> float:
    v = _number(value)
    return v if v is not None and math.isfinite(v) else default


def _number_list(text: Optional[str]) -> List[float]:
    """Whitespace-separated numbers; parsing stops at the first token that is not one, as in
    Xournal++'s loader."""
    out: List[float] = []
    if not text:
        return out
    for token in text.split():
        try:
            out.append(float(token))
        except ValueError:
            break
    return out


def _hex_rgba(text: str) -> Optional[RGBA]:
    digits = text[1:]
    if not re.fullmatch(r"[0-9a-fA-F]+", digits or "x"):
        return None
    if len(digits) == 6:
        digits += "ff"
    if len(digits) != 8:
        return None
    v = int(digits, 16)
    return ((v >> 24) & 255) / 255.0, ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0


def parse_color(value: Optional[str], default: RGBA, background: bool = False) -> Optional[RGBA]:
    """``#RRGGBBAA`` / ``#RRGGBB`` / Xournal's colour names -> RGBA; ``None`` when unknown."""
    text = (value or "").strip()
    if not text:
        return default
    if background and text in BACKGROUND_COLORS:
        r, g, b = BACKGROUND_COLORS[text]
        return r / 255.0, g / 255.0, b / 255.0, 1.0
    if text.startswith("#"):
        return _hex_rgba(text)
    if text in NAMED_COLORS:
        r, g, b = NAMED_COLORS[text]
        return r / 255.0, g / 255.0, b / 255.0, 1.0
    return None


def _font(name: str) -> Tuple[str, bool, bool]:
    """Pango-style font name (``"Times New Roman, Bold"``, ``"Sans Bold Italic"``) ->
    ``(family, bold, italic)``."""
    tokens = [t for t in re.split(r"[\s,]+", name.strip()) if t]
    bold = italic = False
    while tokens and tokens[-1].lower() in _STYLE_WORDS:
        style = _STYLE_WORDS[tokens.pop().lower()]
        bold = bold or style == "bold"
        italic = italic or style == "italic"
    family = " ".join(tokens) or "Sans"
    return family, bold, italic


def _area(points: Sequence[Point]) -> float:
    """Absolute shoelace area of the closed polygon through ``points`` (0 for a line)."""
    total = 0.0
    for a, b in zip(points, list(points[1:]) + list(points[:1])):
        total += a.x * b.y - b.x * a.y
    return abs(total) / 2.0


Matrix = Tuple[float, float, float, float, float, float]  # xx yx xy yy x0 y0 (cairo order)


def _matrix(value: Optional[str]) -> Optional[Matrix]:
    nums = _number_list(value)
    if len(nums) < 6 or not all(math.isfinite(v) for v in nums[:6]):
        return None
    return nums[0], nums[1], nums[2], nums[3], nums[4], nums[5]


def _decompose(m: Matrix, w: float, h: float) -> Tuple[float, float, float, float, float, bool, bool]:
    """Box of local size ``w`` x ``h`` under matrix ``m`` -> ``(x, y, width, height,
    rotation_degrees_clockwise, sheared, mirrored)`` with ``(x, y)`` the unrotated box's
    top-left corner (the model rotates images about their centre)."""
    xx, yx, xy, yy, x0, y0 = m
    width = math.hypot(xx, yx) * w
    height = math.hypot(xy, yy) * h
    cx = x0 + (xx * w + xy * h) / 2.0
    cy = y0 + (yx * w + yy * h) / 2.0
    rotation = math.degrees(math.atan2(yx, xx))
    det = xx * yy - yx * xy
    norm = math.hypot(xx, yx) * math.hypot(xy, yy)
    sheared = norm > 0 and abs(xx * xy + yx * yy) > 1e-3 * norm
    return cx - width / 2.0, cy - height / 2.0, width, height, rotation, sheared, det < 0


# --------------------------------------------------------------------------------------
# XML -> small element tree (no DTDs, bounded)


class _Refused(Exception):
    """The XML carries a document type or entity declaration."""


class _NotXournal(Exception):
    """The root element is not <xournal> / <MrWriter>."""


class _Limit(Exception):
    """A resource limit stopped parsing."""


class _Node:
    __slots__ = ("tag", "attrib", "children", "parts", "closed")

    def __init__(self, tag: str, attrib: Dict[str, str]):
        self.tag = tag
        self.attrib = attrib
        self.children: List["_Node"] = []
        self.parts: List[str] = []
        self.closed = False

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def child(self, tag: str) -> Optional["_Node"]:
        for c in self.children:
            if c.tag == tag:
                return c
        return None


class _TreeParser:
    """expat -> :class:`_Node` tree.  Every closed ``<page>`` (a child of the root) is passed
    to ``on_page`` and dropped from the tree; :meth:`finish` passes an unclosed one too."""

    def __init__(self, on_page: Callable[[_Node], None], on_title: Callable[[str], None],
                 on_other: Callable[[_Node], None]):
        self.on_page = on_page
        self.on_title = on_title
        self.on_other = on_other
        self.stack: List[_Node] = []
        self.root: Optional[_Node] = None
        self.elements = 0
        self.problem: Optional[str] = None

    def _start(self, tag: str, attrib: Dict[str, str]) -> None:
        if self.root is None:
            if tag not in ROOT_TAGS:
                raise _NotXournal(tag)
            self.root = _Node(tag, attrib)
            self.stack.append(self.root)
            return
        self.elements += 1
        if self.elements > MAX_ELEMENTS:
            raise _Limit(f"more than {MAX_ELEMENTS} XML elements")
        if len(self.stack) >= MAX_DEPTH:
            raise _Limit(f"XML nested deeper than {MAX_DEPTH} levels")
        node = _Node(tag, attrib)
        parent = self.stack[-1]
        if parent is not self.root:  # children of the root are handed over when they close
            parent.children.append(node)
        self.stack.append(node)

    def _end(self, tag: str) -> None:
        node = self.stack.pop()
        node.closed = True
        if len(self.stack) == 1:  # a direct child of the root
            if node.tag == "page":
                self.on_page(node)
            elif node.tag == "title":
                self.on_title(node.text)
            else:
                self.on_other(node)

    def _data(self, text: str) -> None:
        if self.stack and self.stack[-1].tag in TEXT_TAGS:
            self.stack[-1].parts.append(text)

    @staticmethod
    def _refuse(*_args: object) -> None:
        raise _Refused()

    def parse(self, xml: bytes) -> None:
        parser = expat.ParserCreate()
        parser.buffer_text = True
        parser.ordered_attributes = False
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.StartDoctypeDeclHandler = self._refuse
        parser.EntityDeclHandler = self._refuse
        parser.UnparsedEntityDeclHandler = self._refuse
        parser.ExternalEntityRefHandler = self._refuse
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._data
        try:
            parser.Parse(xml, True)
        except expat.ExpatError as exc:
            if self.root is None:
                raise ValueError(f"not a Xournal++ file: the XML could not be parsed ({exc})") from None
            self.problem = f"the XML is damaged or truncated at line {exc.lineno}"
        except _Limit as exc:
            self.problem = str(exc)
        except RecursionError:  # pragma: no cover - expat does not recurse; defensive
            self.problem = "the XML is nested too deeply"
        if self.root is None:
            raise ValueError("not a Xournal++ file: no XML root element")

    def finish(self) -> None:
        """Hand over the page that was still open when parsing stopped."""
        if len(self.stack) >= 2 and self.stack[1].tag == "page" and not self.stack[1].closed:
            self.on_page(self.stack[1])
        self.stack.clear()


# --------------------------------------------------------------------------------------
# reader


_MESSAGES = {
    "eraser": "{n} eraser (whiteout) strokes were dropped; ink they covered is visible again",
    "short": "{n} strokes with fewer than two points were skipped (Xournal++ does not draw them either)",
    "truncated": "{n} strokes had fewer widths than points and were shortened, as Xournal++ does",
    "dropped_points": "{n} stroke points with invalid coordinates were dropped",
    "bad_width": "{n} strokes had an invalid width; 1 pt was used",
    "dashed": "{n} dashed or dotted strokes are drawn solid",
    "layers": "{n} pages had several layers; they were merged into one",
    "audio": "Audio recordings and their links to strokes or text ({n} elements) are not converted",
    "latex": "{n} LaTeX formulas became images (their LaTeX source is not kept)",
    "links": "{n} links became plain text (the link targets are dropped)",
    "image_format": "{n} images are neither PNG nor JPEG and were skipped",
    "image_data": "{n} images without readable data were skipped",
    "image_size": "{n} rotated images or formulas could not be measured and were placed unrotated",
    "sheared": "{n} sheared or mirrored images or texts were approximated by a rotation",
    "text_empty": "{n} empty text elements were skipped",
    "color": "{n} unknown colours were replaced by black",
    "stroke_limit": "{n} stroke points beyond " + str(MAX_POINTS_PER_STROKE) + " per stroke were dropped",
    "total_limit": "{n} strokes beyond " + str(MAX_TOTAL_POINTS) + " points per file were dropped",
    "unknown": "{n} unknown elements were ignored",
    "page_size": "{n} pages had no valid size; A4 was used",
    "styles": "{n} pages use an isometric or music-staff paper style, approximated as grid, dotted or lined paper",
    "bg_image_missing": "{n} page background images are stored outside the file and were dropped",
    "bg_image_format": "{n} page background images are neither PNG nor JPEG and were dropped",
    "pdf_page": "{n} pages refer to a page the background PDF does not have; plain paper was used",
}


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.doc = Document(title="Untitled")
        self.counts = Counter(_MESSAGES)
        self.zip: Optional[BoundedZip] = None
        self.total_points = 0
        self.pdf_id: Optional[str] = None  # the document's background PDF (one per document)
        self.pdf_parsed = False  # a <background type="pdf"> with a file name was seen
        self.pdf_missing: Optional[str] = None  # why the background PDF is unavailable
        self.pdf_pages: Optional[int] = None
        self.page_backgrounds: List[Optional[bytes]] = []  # pixmap background per page (for clones)
        self.paper_pdfs: Dict[Tuple[float, float, str, Tuple[int, int, int]], str] = {}

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    # -- container --------------------------------------------------------------------

    def xml(self) -> bytes:
        data = self.data
        if data[:2] == b"\x1f\x8b":
            xml, problem = inflate_limited(data, 47, MAX_XML_BYTES)
            if problem == "too large":
                raise ValueError(f"Xournal++ file inflates to more than {MAX_XML_BYTES // (1024 * 1024)} MB; refused")
            if problem == "truncated":
                if not xml:
                    raise ValueError("not a Xournal++ file: the gzip stream is damaged")
                self.warn("The compressed file is truncated or damaged; only its readable part was converted")
            return xml
        if data[:4] == b"PK\x03\x04" or data[:4] == b"PK\x05\x06":
            self.zip = BoundedZip(data, "Xournal++", max_member=MAX_XML_BYTES)
            if PACKAGE_CONTENT not in self.zip.names:
                raise ValueError("not a Xournal++ file: the ZIP archive has no content.xml")
            xml = self.zip.read(PACKAGE_CONTENT)
            if xml is None:
                self.zip.report(self.doc)
                raise ValueError("Xournal++ file: content.xml could not be read (too large or damaged)")
            return xml
        stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
        if stripped[:1] == b"<":
            if len(data) > MAX_XML_BYTES:
                raise ValueError(f"Xournal++ file is larger than {MAX_XML_BYTES // (1024 * 1024)} MB; refused")
            return data
        raise ValueError("not a Xournal++ file: neither gzip-compressed XML, XML nor a ZIP package")

    def read(self) -> Document:
        xml = self.xml()
        parser = _TreeParser(self.page, self.title, self.top_level)
        try:
            parser.parse(xml)
        except _Refused:
            raise ValueError("not a Xournal++ file: XML document type or entity declarations are "
                             "not allowed") from None
        except _NotXournal as exc:
            raise ValueError(f"not a Xournal++ file: the root element is <{exc}>, not <xournal>") from None
        parser.finish()
        if parser.problem:
            self.warn(f"The file is damaged ({parser.problem}); the content before the damage was converted")
        if not self.doc.pages:
            self.warn("The file has no pages")
        if self.pdf_missing:
            self.warn(self.pdf_missing)
        self.counts.flush(self.doc)
        if self.zip is not None:
            self.zip.report(self.doc)
        return self.doc

    # -- top level ----------------------------------------------------------------------

    def title(self, text: str) -> None:
        text = " ".join(text.split())
        if text and not text.startswith(BOILERPLATE_TITLES):
            self.doc.title = text[:200]

    def top_level(self, node: _Node) -> None:
        if node.tag == "audio":
            self.counts.add("audio")
        elif node.tag not in ("preview", "title"):
            self.counts.add("unknown")

    # -- pages ----------------------------------------------------------------------------

    def page(self, node: _Node) -> None:
        if len(self.doc.pages) >= MAX_PAGES:
            self.warn(f"Only the first {MAX_PAGES} pages were read")
            return
        w = _number(node.attrib.get("width"))
        h = _number(node.attrib.get("height"))
        if not (w is not None and h is not None and 0 < w <= MAX_PAGE_SIDE and 0 < h <= MAX_PAGE_SIDE):
            self.counts.add("page_size")
            w, h = DEFAULT_PAGE_SIZE
        page = Page(float(w), float(h))
        index = len(self.doc.pages)
        self.doc.pages.append(page)
        background_image: Optional[bytes] = None
        bg = node.child("background")
        if bg is not None:
            try:
                background_image = self.background(page, bg)
            except Exception as exc:  # noqa: BLE001 - tolerant reader: never fail on one element
                self.warn(f"Page {index + 1}: its background could not be read ({exc.__class__.__name__}: {exc})")
        self.page_backgrounds.append(background_image)
        layers = [c for c in node.children if c.tag == "layer"]
        if sum(1 for layer in layers if layer.children) > 1:
            self.counts.add("layers")
        for layer in layers:
            for element in layer.children:
                if not element.closed:
                    continue  # cut off by damage
                try:
                    self.element(page, element)
                except Exception as exc:  # noqa: BLE001 - tolerant reader: never fail on one element
                    self.warn(f"Page {index + 1}: a <{element.tag}> element was skipped "
                              f"({exc.__class__.__name__}: {exc})")
        for child in node.children:
            if child.tag not in ("layer", "background"):
                self.counts.add("unknown")

    def element(self, page: Page, node: _Node) -> None:
        tag = node.tag
        if tag == "stroke":
            self.stroke(page, node)
        elif tag == "text":
            self.text(page, node)
        elif tag == "image":
            self.image(page, node)
        elif tag == "teximage":
            self.teximage(page, node)
        elif tag == "link":
            self.link(page, node)
        elif tag == "timestamp":
            self.counts.add("audio")
        else:
            self.counts.add("unknown")

    # -- backgrounds ----------------------------------------------------------------------

    def background(self, page: Page, bg: _Node) -> Optional[bytes]:
        kind = (bg.attrib.get("type") or "").strip()
        if kind == "pdf":
            self.pdf_background(page, bg)
            return None
        if kind == "pixmap":
            return self.pixmap_background(page, bg)
        if kind != "solid":
            if kind:
                self.warn(f"Unknown background type {kind!r}; plain paper was used")
            return None
        style = (bg.attrib.get("style") or "plain").strip()
        paper = PAPER_FOR_STYLE.get(style)
        if paper is None:
            self.warn(f"Unknown paper style {style!r}; plain paper was used")
            paper = "plain"
        elif style in APPROXIMATED_STYLES:
            self.counts.add("styles")
        page.paper = paper
        color = parse_color(bg.attrib.get("color"), (1.0, 1.0, 1.0, 1.0), background=True)
        if color is None:
            self.warn(f"Unknown background colour {bg.attrib.get('color')!r}; white was used")
            color = (1.0, 1.0, 1.0, 1.0)
        rgb = tuple(int(round(c * 255)) for c in color[:3])
        if rgb != (255, 255, 255):
            self.coloured_paper(page, paper, rgb)  # type: ignore[arg-type]
        return None

    def coloured_paper(self, page: Page, paper: str, rgb: Tuple[int, int, int]) -> None:
        """A non-white solid background becomes a generated paper PDF of that colour."""
        key = (round(page.width, 3), round(page.height, 3), paper, rgb)
        pdf_id = self.paper_pdfs.get(key)
        if pdf_id is None:
            pdf_id = f"xournalpp-paper-{len(self.paper_pdfs) + 1}.pdf"
            self.doc.pdfs[pdf_id] = pdfutil.make_paper_pdf(page.width, page.height, paper,
                                                           color=tuple(c / 255.0 for c in rgb))
            self.paper_pdfs[key] = pdf_id
        page.background = PdfBackground(pdf_id, 0)
        page.template_is_builtin = True

    def pdf_background(self, page: Page, bg: _Node) -> None:
        if not self.pdf_parsed:
            domain = (bg.attrib.get("domain") or "absolute").strip()
            filename = bg.attrib.get("filename") or ""
            if filename:
                self.pdf_parsed = True
                self.load_pdf(domain, filename)
        number = _number(bg.attrib.get("pageno"))
        page_index = int(number) - 1 if number is not None and math.isfinite(number) and number >= 1 else 0
        if self.pdf_id is None:
            if not self.pdf_parsed:
                self.pdf_missing = "A page refers to a background PDF the file never names; plain paper was used"
            return
        if self.pdf_pages is not None and page_index >= self.pdf_pages:
            self.counts.add("pdf_page")
            return
        page.background = PdfBackground(self.pdf_id, page_index)
        page.template_is_builtin = False

    def load_pdf(self, domain: str, filename: str) -> None:
        name = filename.replace("\\", "/")
        if domain == "attach" and self.zip is not None:
            data = self.zip.read(name)
            if data is None or not data.startswith(b"%PDF"):
                self.pdf_missing = (f"The background PDF {name} is missing from the package or is not a PDF; "
                                    "its pages got plain paper")
                return
            self.pdf_id = name.rsplit("/", 1)[-1] or "bg.pdf"
            self.doc.pdfs[self.pdf_id] = data
            try:
                self.pdf_pages = len(pdfutil.pdf_info(data).pages) or None
            except (ValueError, TypeError, OverflowError, RecursionError):
                self.pdf_pages = None
            return
        if domain == "attach":
            self.pdf_missing = (f"The background PDF is stored next to the Xournal++ file (\"<file>.xopp.{name}\") "
                                "and is not part of it; its pages got plain paper. Re-save the document in "
                                "Xournal++ with the PDF attached, or convert the PDF separately")
        else:
            self.pdf_missing = (f"The background PDF is only referenced by its path ({name!r}) and is not part "
                                "of the file; its pages got plain paper")

    def pixmap_background(self, page: Page, bg: _Node) -> Optional[bytes]:
        domain = (bg.attrib.get("domain") or "absolute").strip()
        filename = (bg.attrib.get("filename") or "").replace("\\", "/")
        data: Optional[bytes] = None
        if domain == "clone":
            number = _number(filename)
            if number is not None and math.isfinite(number) and 0 <= number < len(self.page_backgrounds):
                data = self.page_backgrounds[int(number)]
        elif domain == "attach" and self.zip is not None and filename:
            data = self.zip.read(filename)
        if not data:
            self.counts.add("bg_image_missing")
            return None
        fmt = sniff_image(data)
        if fmt not in ("png", "jpeg"):
            self.counts.add("bg_image_format")
            return None
        page.images.insert(0, Image(0.0, 0.0, page.width, page.height, data, fmt=fmt))
        return data

    # -- strokes ----------------------------------------------------------------------------

    def stroke(self, page: Page, node: _Node) -> None:
        a = node.attrib
        tool = (a.get("tool") or "pen").strip()
        if tool == "eraser":
            self.counts.add("eraser")
            return
        if (a.get("fn") or "").strip():
            self.counts.add("audio")
        if (a.get("style") or "").strip() not in ("", "plain"):
            self.counts.add("dashed")
        color = parse_color(a.get("color"), (0.0, 0.0, 0.0, 1.0))
        if color is None:
            self.counts.add("color")
            color = (0.0, 0.0, 0.0, 1.0)
        widths = _number_list(a.get("width") if a.get("width") is not None else "1")
        nominal = widths[0] if widths else 1.0
        values = widths[1:]
        if a.get("pressures") is not None:  # MrWriter keeps the per-point values separately
            values = _number_list(a.get("pressures"))
        if not (math.isfinite(nominal) and nominal > 0):
            self.counts.add("bad_width")
            nominal = 1.0
        coords = _number_list(node.text)
        points = [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
        if len(points) > MAX_POINTS_PER_STROKE:
            self.counts.add("stroke_limit", len(points) - MAX_POINTS_PER_STROKE)
            points = points[:MAX_POINTS_PER_STROKE]
        if self.total_points + len(points) > MAX_TOTAL_POINTS:
            self.counts.add("total_limit")
            return
        self.total_points += len(points)
        portions = self.portions(points, values, nominal)
        kind = "highlighter" if tool == "highlighter" else "pen"
        fill = _number(a.get("fill"))
        filled = fill is not None and math.isfinite(fill) and fill >= 0
        # a filled shape whose outline is a hairline is a fill alone (how the writer stores one)
        fill_only = filled and not values and nominal <= FILL_OUTLINE_WIDTH + 1e-9
        for portion in portions:
            if len(portion) < 2:
                self.counts.add("short")
                continue
            if not fill_only:
                page.strokes.append(Stroke(portion, color=color, kind=kind, width=float(nominal)))
            if filled and _area(portion) > 1e-6:
                alpha = min(255.0, fill) / 255.0  # type: ignore[operator]
                polygon = [Point(p.x, p.y, 0.0) for p in portion]
                if fill_only and len(polygon) > 3 and (polygon[0].x, polygon[0].y) == (polygon[-1].x, polygon[-1].y):
                    polygon.pop()  # the writer closes the ring explicitly
                page.strokes.append(Stroke(list(polygon), color=(color[0], color[1], color[2], alpha),
                                           kind="fill", width=0.0, outline=[polygon]))

    def portions(self, points: List[Tuple[float, float]], values: List[float],
                 nominal: float) -> List[List[Point]]:
        """Model point lists for one ``<stroke>`` (several when invalid widths split it)."""
        if len(points) < 2 or not values:
            return [self.finite_points([(x, y, nominal) for x, y in points])]
        if len(values) + 1 < len(points):
            self.counts.add("truncated")
            points = points[:len(values) + 1]
        z = list(values[:len(points) - 1])
        if all(math.isfinite(v) and v > 0 for v in z):
            spans = [(0, len(points) - 1)]
        else:
            # Segments with a zero, negative or NaN width are invisible; Xournal++ removes their
            # points and splits the stroke around them, so the visible ink is unchanged.
            spans = []
            i = 0
            n = len(points)
            while i < n - 1:
                if math.isfinite(z[i]) and z[i] > 0:
                    j = i
                    while j < n - 1 and math.isfinite(z[j]) and z[j] > 0:
                        j += 1
                    spans.append((i, j))  # positive run i..j-1 plus the point that ends it
                    i = j
                else:
                    i += 1
        out: List[List[Point]] = []
        for start, end in spans:
            run: List[Tuple[float, float, float]] = []
            for k in range(start, end + 1):
                width = z[k] if k < end else z[end - 1]
                run.append((points[k][0], points[k][1], width))
            out.append(self.finite_points(run))
        return out

    def finite_points(self, run: Sequence[Tuple[float, float, float]]) -> List[Point]:
        pts = [Point(x, y, w) for x, y, w in run if math.isfinite(x) and math.isfinite(y)]
        if len(pts) != len(run):
            self.counts.add("dropped_points", len(run) - len(pts))
        return pts

    # -- text ----------------------------------------------------------------------------------

    def text_box(self, node: _Node, content: str) -> Optional[TextBox]:
        a = node.attrib
        family, bold, italic = _font(a.get("font") or "Sans")
        size = _finite(a.get("size"), 12.0)
        if size <= 0:
            size = 12.0
        color = parse_color(a.get("color"), (0.0, 0.0, 0.0, 1.0))
        if color is None:
            self.counts.add("color")
            color = (0.0, 0.0, 0.0, 1.0)
        rotation = 0.0
        scale = 1.0
        matrix = _matrix(a.get("matrix"))
        if matrix is not None:
            xx, yx, xy, yy, x, y = matrix
            if math.hypot(xx, yx) > 0:
                scale = math.hypot(xx, yx)
            rotation = math.degrees(math.atan2(yx, xx))
            if abs(xx * xy + yx * yy) > 1e-3 * max(1e-12, scale * math.hypot(xy, yy)) or xx * yy - yx * xy < 0:
                self.counts.add("sheared")
        else:
            x = _finite(a.get("x"), 0.0)
            y = _finite(a.get("y"), 0.0)
        size *= scale
        w, h = estimate_text_extent(content, size)
        wrap = _number(a.get("wrap"))
        if wrap is not None and math.isfinite(wrap) and wrap > 0:
            w = wrap * scale  # the wrap width is in the text's own (unscaled) coordinates
        align = (a.get("align") or "left").strip().lower()
        if align not in ("left", "center", "right"):
            align = "left"
        run = TextRun(content, bold=bold, italic=italic, font=family, size=size, color=color)
        return TextBox(x, y, w, h, content, runs=[run], color=color, size=size, rotation=rotation, align=align)

    def text(self, page: Page, node: _Node) -> None:
        content = node.text
        if not content:
            self.counts.add("text_empty")
            return
        if (node.attrib.get("fn") or "").strip():
            self.counts.add("audio")
        box = self.text_box(node, content)
        if box is not None:
            page.texts.append(box)

    def link(self, page: Page, node: _Node) -> None:
        content = node.text
        if not content:
            self.counts.add("text_empty")
            return
        self.counts.add("links")
        box = self.text_box(node, content)
        if box is not None:
            page.texts.append(box)

    # -- images ----------------------------------------------------------------------------------

    def payload(self, node: _Node) -> Optional[bytes]:
        """Inline base64 data, or the ZIP member named by an ``<attachment path>`` child."""
        attachment = node.child("attachment")
        if attachment is not None and self.zip is not None:
            path = (attachment.attrib.get("path") or "").replace("\\", "/")
            return self.zip.read(path) if path else None
        text = node.text
        if not text.strip():
            return None
        try:
            return base64.b64decode("".join(text.split()), validate=False)
        except (binascii.Error, ValueError):
            return None

    def placed(self, node: _Node, natural: Optional[Tuple[float, float]]
               ) -> Optional[Tuple[float, float, float, float, float]]:
        """``(x, y, w, h, rotation)`` of an image-like element (legacy box or matrix)."""
        a = node.attrib
        matrix = _matrix(a.get("matrix"))
        if matrix is not None:
            if natural is None or not (natural[0] > 0 and natural[1] > 0):
                self.counts.add("image_size")
                return matrix[4], matrix[5], 0.0, 0.0, 0.0
            x, y, w, h, rotation, sheared, mirrored = _decompose(matrix, natural[0], natural[1])
            if sheared or mirrored:
                self.counts.add("sheared")
            return x, y, w, h, rotation
        left = _finite(a.get("left"), 0.0)
        top = _finite(a.get("top"), 0.0)
        right = _finite(a.get("right"), left)
        bottom = _finite(a.get("bottom"), top)
        return min(left, right), min(top, bottom), abs(right - left), abs(bottom - top), 0.0

    def image(self, page: Page, node: _Node) -> None:
        data = self.payload(node)
        if not data:
            self.counts.add("image_data")
            return
        fmt = sniff_image(data)
        if fmt not in ("png", "jpeg"):
            self.counts.add("image_format")
            return
        pixels = image_pixel_size(data)
        box = self.placed(node, (float(pixels[0]), float(pixels[1])) if pixels else None)
        if box is None:
            return
        x, y, w, h, rotation = box
        if w <= 0 or h <= 0:
            if pixels is None:
                self.counts.add("image_data")
                return
            w, h = float(pixels[0]), float(pixels[1])
        page.images.append(Image(x, y, w, h, data, fmt=fmt, rotation=rotation))

    def teximage(self, page: Page, node: _Node) -> None:
        data = self.payload(node)
        if not data:
            self.counts.add("image_data")
            return
        fmt = sniff_image(data)
        natural: Optional[Tuple[float, float]] = None
        if fmt == "pdf":
            try:
                info = pdfutil.pdf_info(data)
                if info.pages:
                    natural = (info.pages[0].width, info.pages[0].height)
            except (ValueError, TypeError, OverflowError, RecursionError):
                natural = None
        elif fmt == "png":
            pixels = image_pixel_size(data)
            natural = (float(pixels[0]), float(pixels[1])) if pixels else None
        else:
            self.counts.add("image_format")
            return
        if (node.attrib.get("text") or "").strip():
            self.counts.add("latex")
        box = self.placed(node, natural)
        if box is None:
            return
        x, y, w, h, rotation = box
        if (w <= 0 or h <= 0) and natural is not None:
            w, h = natural
        if w <= 0 or h <= 0:
            self.counts.add("image_data")
            return
        page.images.append(Image(x, y, w, h, data, fmt=fmt, rotation=rotation))


def read_xopp(data: bytes) -> Document:
    """Parse a Xournal++ ``.xopp`` or Xournal ``.xoj`` file given as bytes.

    Raises :class:`ValueError` when the data is not a Xournal++ document (not gzip / XML /
    ZIP, no ``<xournal>`` root, a DTD or entity declaration, or larger than the limits).
    Everything else that is damaged or unsupported becomes a line on ``Document.warnings``.
    """
    return _Reader(ensure_bytes(data, "read_xopp")).read()
