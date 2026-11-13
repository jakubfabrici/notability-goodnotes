"""Model -> Stylus Labs Write ``.svgz`` writer (``docs/styluswrite.md`` section 3).

Output: Write's own document skeleton, gzip-compressed (deterministic: no time stamp), as
documented by the ``styluslabs/templates`` README and seen in documents Write saved:

* ``svg#write-document`` with the grey ``rect#write-doc-background`` and ``defs#write-defs``
  holding the ``text/writeconfig`` block (``docFormatVersion`` 2, the view offset of page 1);
* one ``svg.write-page`` per page (``x = 10``, pages stacked downwards 20 units apart so that a
  browser shows them one below the other), with ``g.write-content write-v3`` carrying the Page
  Setup attributes (``width``, ``height``, ``xruling``, ``yruling``, ``marginLeft``,
  ``papercolor``, alpha-first ``rulecolor``);
* ``g.ruleline`` first: ``rect.pagerect`` plus the standard rules (``yrule_1`` and the other
  horizontal lines, vertical lines for grid paper; class ``write-std-ruling``; none on a page
  beyond :data:`MAX_RULES` per direction or once the file has :data:`MAX_TOTAL_RULES`, which
  leaves the ruling to the Page Setup attributes) or, for dotted paper, a dot ``<pattern>`` the
  way Write's custom templates draw backgrounds;
* then the editable content: images, ink, text.

Ink (units: :data:`gnnote.styluswrite.PT_PER_UNIT`, coordinates rounded to 0.001 unit through
integer thousandths so long strokes do not drift):

* constant-width strokes -> ``path.write-stroke-pen`` centre lines ``M x y l dx dy ...`` with
  round caps and joins;
* strokes whose width varies (``Options.pressure``, on by default) -> ``path.write-flat-pen``:
  the filled outline with the left side forward and the right side backward (2 n vertices, no
  ``Z``), ``stroke-width`` = the nominal width, exactly what the reader inverts;
* Bezier strokes are flattened to about 1 pt; highlighters keep their colour with
  ``stroke-opacity`` (their alpha, 0.5 for opaque ones), since Write's own highlighter encoding
  is unknown; shape fills become filled ``<path>`` elements;
* highlighters and pens translucent enough to pass for one (alpha <= 0.6) carry
  ``data-gnnote-kind`` (:data:`gnnote.styluswrite.KIND_ATTRIBUTE`), which the reader trusts over
  its opacity rule, so both come back as what they were.

Images are embedded PNG / JPEG data URIs (rotation as a ``matrix``), text boxes are ``<text>``
elements with an absolute baseline on every line (empty lines are left out; Write shows such
text but cannot edit it; at most :data:`MAX_TEXT_CHARS` characters of text per file).  Strokes
longer than :data:`MAX_PATH_POINTS` points are split into joined pieces so that no path exceeds
what readers take.  PDF page backgrounds cannot be carried: the standard library has no PDF
rasteriser or PDF-to-SVG converter.

The files have not been opened in Write yet (``docs/styluswrite.md`` lists what to check).
"""
from __future__ import annotations

import base64
import gzip
import math
import re
import statistics
from typing import Any, Iterator, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape, quoteattr

from ..codecutil import (MAX_COORD_PT, MAX_SIZE_PT, Counter, clamp_rgba, image_pixel_size, is_finite, sniff_image,
                         stroke_polyline, to_byte)
from ..model import RGBA, Document, Image, Page, Point, Stroke, TextBox, TextRun
from . import (DEFAULT_PAGE_UNITS, DEFAULT_PAPER_COLOR, DEFAULT_RULE_COLOR, GRID_PITCH, KIND_ATTRIBUTE, LINED_PITCH,
               PAGE_GAP, PAGE_POSITION, PT_PER_UNIT)

__all__ = ["write_write_svg", "build_svg", "outline_vertices"]

MAX_PAGE_UNITS = 1e6
MAX_RULES = 2000  # rule lines per page direction; a denser page keeps only its Page Setup ruling attributes
MAX_TOTAL_RULES = 200_000  # explicit rule lines per file (output size); later pages keep only the attributes
HIGHLIGHTER_ALPHA = 0.5  # opacity of an opaque highlighter
MAX_HIGHLIGHTER_ALPHA = 0.6  # the reader takes translucent ink up to this opacity for a highlighter
WIDTH_TOLERANCE = 0.01  # relative width spread still written as a constant-width pen
MAX_PATH_POINTS = 50_000  # longer strokes are split into several paths (the reader takes 200 000 vertices)
MAX_TEXT_CHARS = 8_000_000  # characters of text written per file, each line counting LINE_COST more
LINE_COST = 32  # what a written text line costs on top of its characters (its tspan)
TEXT_ASCENT = 0.952  # first baseline below a text box's top, in em (as gnnote.pdf.text)
TEXT_LINE = 1.1646  # baseline distance in em
RULE_STROKE = "#0000FF"  # the colour of DEFAULT_RULE_COLOR without its alpha
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")

_MESSAGES = {
    "pdf_background": "{n} pages show a PDF background, which a Write file cannot hold without converting the PDF "
                      "(gnnote has no PDF rasteriser); only the ink and the paper style were written",
    "highlighter": "{n} highlighter strokes were written as semi-transparent pen strokes (Write's own highlighter "
                   "encoding is not known)",
    "pencil": "{n} pencil strokes were written as Write pen strokes",
    "split": "{n} strokes longer than " + str(MAX_PATH_POINTS) + " points were split into several Write strokes",
    "text": "{n} text boxes were written as SVG text, which Write displays but cannot edit",
    "text_empty": "{n} empty text boxes were skipped",
    "text_limit": "{n} text boxes were cut short or skipped: a file holds at most " + str(MAX_TEXT_CHARS) + " characters "
                  "of text (each line counting as " + str(LINE_COST) + " more)",
    "image_format": "{n} images are neither PNG nor JPEG and were skipped",
    "image_empty": "{n} images without data were skipped",
    "invalid": "{n} strokes, images or text boxes with invalid coordinates were skipped",
    "page_size": "{n} pages had no valid size; Write's default page size was used",
    "rules_limit": "{n} pages were written without explicit rule lines (more than " + str(MAX_RULES) + " per "
                   "direction on a page, or " + str(MAX_TOTAL_RULES) + " per file); their ruling is kept only in the "
                   "Page Setup attributes xruling and yruling, which gnnote reads back but plain SVG viewers ignore",
}


def _opt(options: Any, name: str, default: Any) -> Any:
    value = getattr(options, name, None) if options is not None else None
    return default if value is None else value


def _num(v: float) -> str:
    """A compact decimal with at most three places, as Write prints coordinates."""
    s = f"{float(v):.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _milli(v: int) -> str:
    """Integer thousandths -> decimal text (exact, no float rounding)."""
    sign = "-" if v < 0 else ""
    q, r = divmod(abs(v), 1000)
    return f"{sign}{q}" if r == 0 else f"{sign}{q}.{r:03d}".rstrip("0")


def _units(v: float) -> float:
    return v / PT_PER_UNIT


def _hex(color: Sequence[float]) -> str:
    r, g, b, _a = clamp_rgba(color)
    return "#%02X%02X%02X" % (to_byte(r), to_byte(g), to_byte(b))


def _text(value: str) -> str:
    return escape(_INVALID_XML.sub("", value))


def _attr(value: str) -> str:
    return quoteattr(_INVALID_XML.sub("", value)).replace("\n", "&#10;").replace("\r", "&#13;")


def _path_data(points: Sequence[Tuple[float, float]]) -> str:
    """``M x y l dx dy ...`` from points in units, through integer thousandths."""
    xs = [int(round(x * 1000.0)) for x, _ in points]
    ys = [int(round(y * 1000.0)) for _, y in points]
    parts = [f"M{_milli(xs[0])} {_milli(ys[0])}"]
    if len(points) > 1:
        deltas = [f"{_milli(xs[i] - xs[i - 1])} {_milli(ys[i] - ys[i - 1])}" for i in range(1, len(points))]
        parts.append("l" + " ".join(deltas))
    return " ".join(parts)


def outline_vertices(points: Sequence[Point]) -> List[Tuple[float, float]]:
    """Write's variable-width outline of a centre line in units: the left side forward and
    the right side backward, so that vertex *i* faces vertex *2n - 1 - i* across the line
    (their midpoint is point *i*, their distance its width).  The direction at a point is the
    chord between its nearest distinct neighbours, so repeated points keep a direction."""
    n = len(points)
    prev_distinct = [0] * n  # index of the nearest earlier point at another position (or 0)
    next_distinct = [n - 1] * n
    for i in range(1, n):
        a, b = points[i - 1], points[i]
        prev_distinct[i] = i - 1 if (a.x, a.y) != (b.x, b.y) else prev_distinct[i - 1]
    for i in range(n - 2, -1, -1):
        a, b = points[i + 1], points[i]
        next_distinct[i] = i + 1 if (a.x, a.y) != (b.x, b.y) else next_distinct[i + 1]
    left: List[Tuple[float, float]] = []
    right: List[Tuple[float, float]] = []
    for i, p in enumerate(points):
        a, b = points[prev_distinct[i]], points[next_distinct[i]]
        tx, ty = b.x - a.x, b.y - a.y
        length = math.hypot(tx, ty)
        if length == 0:  # a dot: any direction
            tx, ty, length = 1.0, 0.0, 1.0
        nx, ny = -ty / length, tx / length
        half = p.width / 2.0
        left.append((p.x + nx * half, p.y + ny * half))
        right.append((p.x - nx * half, p.y - ny * half))
    return left + right[::-1]


_STYLE_SUFFIX = re.compile(r"[-_ ](Bold|BoldItalic|BoldOblique|Italic|Oblique|Regular|Roman|Medium|Light|Book)$",
                           re.I)


def _family(font: Optional[str]) -> Tuple[Optional[str], bool, bool]:
    """A PostScript-style font name (``"HelveticaNeue-Bold"``) -> ``(family, bold, italic)``."""
    name = (font or "").strip()
    if not name:
        return None, False, False
    style = ""
    for _ in range(3):  # "Arial Bold Italic": style words are taken off one by one
        m = _STYLE_SUFFIX.search(name)
        if m is None or m.start() == 0:
            break
        style += m.group(1).lower()
        name = name[:m.start()]
    return name, "bold" in style, "italic" in style or "oblique" in style


def _advance(text: str, widths: Sequence[int]) -> float:
    """The advance of ``text`` in em, from Helvetica's widths (0.556 em for other characters)."""
    total = 0.0
    for ch in text:
        code = ord(ch) - 32
        width = widths[code] if 0 <= code < len(widths) else 0
        total += width / 1000.0 if width else 0.556
    return total


class _Line:
    """A line of :func:`_wrap_lines` while it is built: ``(style, segments)`` pairs, each segment
    ``[text, start, end]`` a slice of a run's text that grows while the words of the run follow
    each other, so a line costs an object per run, not per word, and is joined once."""

    __slots__ = ("parts", "ink", "last_word")

    def __init__(self) -> None:
        self.parts: List[Tuple[TextRun, List[List[Any]]]] = []
        self.ink = False  # whether the line holds anything but white space
        self.last_word: Optional[Tuple[int, int, int]] = None  # (part, segment, end) of the last word

    def put(self, run: TextRun, text: str, start: int, end: int, word: bool) -> None:
        """Add ``text[start:end]``, a word (``word``) or a run of spaces, in the style of ``run``."""
        if self.parts and _same_style(self.parts[-1][0], run):
            segments = self.parts[-1][1]
            last = segments[-1]
            if last[0] is text and last[2] == start:
                last[2] = end
            else:
                segments.append([text, start, end])
        else:
            self.parts.append((run, [[text, start, end]]))
        if word:
            self.last_word = (len(self.parts) - 1, len(self.parts[-1][1]) - 1, end)

    def strip_end(self) -> None:
        """Drop the spaces after the last word (they may span several runs)."""
        if self.last_word is None:
            self.parts = []
            return
        part, segment, end = self.last_word
        del self.parts[part + 1:]
        segments = self.parts[part][1]
        del segments[segment + 1:]
        segments[segment][2] = end

    def runs(self) -> List[TextRun]:
        out = []
        for run, segments in self.parts:
            text = "".join(t[a:b] for t, a, b in segments)
            if text:
                out.append(TextRun(text, run.bold, run.italic, run.underline, run.font, run.size, run.color))
        return out


_TOKEN = re.compile(r"\n| +|[^ \n]+")  # a hard break, a run of spaces or a word


def _wrap_lines(runs: Sequence[TextRun], width_pt: float) -> Iterator[Tuple[int, List[TextRun]]]:
    """``(line number, runs)`` of every line that is not empty: hard breaks at ``\\n`` and, since
    SVG text does not wrap, greedy word wraps at ``width_pt`` measured with Helvetica's widths
    (12 % overflow allowed, as the PDF writer does); the lines after the last one with a visible
    character are dropped.  Lazy: words are visited one by one and a line is joined as soon as
    it is complete, so memory stays linear in the text however many words it has."""
    from ..pdf.text import HELVETICA_WIDTHS  # imported only when a document has text

    limit = width_pt * 1.12 + 0.01 if width_pt > 0 else float("inf")
    texts = [run.text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ") for run in runs]
    last = (-1, 0)  # the run with the last visible character and the end of that character
    for i in range(len(texts) - 1, -1, -1):
        end = len(texts[i].rstrip())
        if end:
            last = (i, end)
            break
    past = last[0] < 0  # whether the last visible character is on the line: a break then ends the text
    number, line = 0, _Line()
    width = 0.0
    wrapped = False  # the current line starts at a soft wrap (its leading spaces are dropped)
    for i, (run, text) in enumerate(zip(runs, texts)):
        size = run.size or 12.0
        for m in _TOKEN.finditer(text):
            start, end = m.span()
            if text[start] == "\n":
                if past:
                    break
                if line.parts:
                    yield number, line.runs()
                number, line = number + 1, _Line()
                width, wrapped = 0.0, False
                continue
            word = m.group()
            advance = _advance(word, HELVETICA_WIDTHS) * size
            if word[0] == " ":
                if not (wrapped and not line.parts):
                    line.put(run, text, start, end, False)
                    width += advance
                continue
            if width + advance > limit and line.ink:
                line.strip_end()  # the break replaces the spaces
                if past:
                    break
                yield number, line.runs()
                number, line = number + 1, _Line()
                width, wrapped = 0.0, True
            line.put(run, text, start, end, True)
            line.ink = line.ink or bool(word.strip())
            width += advance
            if i == last[0] and end >= last[1]:
                past = True
        else:
            continue
        break  # the text ended at a break after its last visible character
    if line.parts:
        runs_out = line.runs()
        if runs_out:
            yield number, runs_out


def _wrap(runs: Sequence[TextRun], width_pt: float) -> List[List[TextRun]]:
    """:func:`_wrap_lines` as a list of every line, empty lines inside as ``[]``."""
    lines: List[List[TextRun]] = []
    for number, line in _wrap_lines(runs, width_pt):
        lines.extend([] for _ in range(number - len(lines)))
        lines.append(line)
    return lines or [[]]


def _cut(runs: Sequence[TextRun], chars: int) -> List[TextRun]:
    """The first ``chars`` characters of ``runs``."""
    out: List[TextRun] = []
    for run in runs:
        if chars <= 0:
            break
        if len(run.text) > chars:
            run = TextRun(run.text[:chars], run.bold, run.italic, run.underline, run.font, run.size, run.color)
        out.append(run)
        chars -= len(run.text)
    return out


def _same_style(a: TextRun, b: TextRun) -> bool:
    return (a.bold, a.italic, a.underline, a.font, a.size, a.color) == (b.bold, b.italic, b.underline, b.font, b.size,
                                                                        b.color)


class _Writer:
    def __init__(self, doc: Document, options: Any):
        self.doc = doc
        self.options = options
        self.counts = Counter(_MESSAGES)
        self.pressure = bool(_opt(options, "pressure", True))
        self.paper_mode = str(_opt(options, "paper", "plain"))
        self.rules_left = MAX_TOTAL_RULES
        self.text_left = MAX_TEXT_CHARS

    # -- page frame --------------------------------------------------------------------------

    def size(self, page: Page) -> Tuple[float, float]:
        w, h = page.width, page.height
        if is_finite(w, h) and w > 0 and h > 0:
            wu, hu = _units(w), _units(h)
            if wu <= MAX_PAGE_UNITS and hu <= MAX_PAGE_UNITS:
                return wu, hu
        self.counts.add("page_size")
        return DEFAULT_PAGE_UNITS

    def ruling(self, page: Page) -> Tuple[str, float, float]:
        """``(paper, xruling, yruling)`` from the paper hint; a PDF background is reported."""
        bg = page.background
        if bg is not None and (not page.template_is_builtin or self.paper_mode == "pdf"):
            self.counts.add("pdf_background")
        paper = (page.paper or "plain").lower()
        if paper in ("lined", "ruled"):
            return "lined", 0.0, LINED_PITCH
        if paper in ("grid", "dotted"):
            return paper, GRID_PITCH, GRID_PITCH
        return "plain", 0.0, 0.0

    def ruleline(self, index: int, paper: str, xr: float, yr: float, w: float, h: float) -> List[str]:
        pagerect = (f'<rect class="pagerect" fill="{DEFAULT_PAPER_COLOR}" stroke="none" x="0" y="0" '
                    f'width="{_num(w)}" height="{_num(h)}" />')
        if paper == "dotted":
            pid = f"gnnote-dots-{index + 1}"
            half = _num(xr / 2.0)
            return [f'<g class="ruleline write-scale-down" color="{RULE_STROKE}" shape-rendering="crispEdges">',
                    f'<pattern id="{pid}" x="{half}" y="{half}" width="{_num(xr)}" height="{_num(yr)}" '
                    'patternUnits="userSpaceOnUse">',
                    f'<circle fill="currentColor" stroke="none" cx="{half}" cy="{half}" r="1.5" />',
                    "</pattern>",
                    pagerect,
                    f'<rect fill="url(#{pid})" stroke="none" x="0" y="0" width="{_num(w)}" height="{_num(h)}" />',
                    "</g>"]
        out = [f'<g class="ruleline write-std-ruling write-scale-down" fill="none" stroke="{RULE_STROKE}" '
               'stroke-width="1" shape-rendering="crispEdges" vector-effect="non-scaling-stroke">', pagerect]
        # explicit rules for viewers that do not compute them from xruling / yruling (gnnote's
        # reader does): all of a page's rules or none, within the per-page and per-file limits
        ny = int(h / yr) + 1 if yr > 0 else 0  # upper bounds of the counts below
        nx = int(w / xr) + 1 if xr > 0 else 0
        if (yr > 0 and h / yr > MAX_RULES) or (xr > 0 and w / xr > MAX_RULES) or ny + nx > self.rules_left:
            self.counts.add("rules_limit")
        else:
            before = len(out)
            k = 1
            while yr > 0 and k * yr < h:
                cls = ' class="yrule_1"' if k == 1 else ""
                out.append(f'<path{cls} d="M0 {_num(k * yr)} l{_num(w)} 0" />')
                k += 1
            k = 1
            while xr > 0 and k * xr < w:
                out.append(f'<path d="M{_num(k * xr)} 0 l0 {_num(h)}" />')
                k += 1
            self.rules_left -= len(out) - before
        out.append("</g>")
        return out

    # -- ink ------------------------------------------------------------------------------------

    def stroke(self, stroke: Stroke) -> Optional[str]:
        if stroke.kind == "fill":
            return self.fill(stroke)
        pts = stroke_polyline(stroke)
        if not pts:
            if stroke.points:
                self.counts.add("invalid")
            return None
        if not all(abs(p.x) <= MAX_COORD_PT and abs(p.y) <= MAX_COORD_PT for p in pts):
            self.counts.add("invalid")
            return None
        if stroke.pen == "pencil":
            self.counts.add("pencil")
        if stroke.kind == "highlighter":
            self.counts.add("highlighter")
        clean = [Point(_units(p.x), _units(p.y), _units(min(MAX_SIZE_PT, p.width))) for p in pts]
        if len(clean) <= MAX_PATH_POINTS:
            return self.pen_path(stroke, clean)
        # a path this long would be shortened by readers that bound a path's vertices (gnnote's
        # own reader takes 200 000): consecutive pieces that share their end points
        self.counts.add("split")
        step = MAX_PATH_POINTS - 1
        return "\n".join(self.pen_path(stroke, clean[i:i + MAX_PATH_POINTS]) for i in range(0, len(clean) - 1, step))

    def pen_path(self, stroke: Stroke, clean: List[Point]) -> str:
        """One ``write-flat-pen`` outline or ``write-stroke-pen`` centre line (points in units)."""
        widths = [p.width for p in clean]
        r, g, b, alpha = clamp_rgba(stroke.color)
        extra = ""
        if stroke.kind == "highlighter":
            alpha = alpha if alpha <= MAX_HIGHLIGHTER_ALPHA else HIGHLIGHTER_ALPHA
            extra = f' {KIND_ATTRIBUTE}="highlighter"'
        elif float(_num(alpha)) <= MAX_HIGHLIGHTER_ALPHA:  # the opacity rule would read a highlighter
            extra = f' {KIND_ATTRIBUTE}="pen"'
        color = _hex((r, g, b, 1.0))
        varies = max(widths) - min(widths) > WIDTH_TOLERANCE * max(widths)
        if self.pressure and varies and len(clean) > 1:
            if alpha < 1.0:
                extra += f' fill-opacity="{_num(alpha)}"'
            nominal = _units(stroke.width) if is_finite(stroke.width) and 0 < stroke.width <= MAX_SIZE_PT \
                else max(widths)
            return (f'<path class="write-flat-pen" fill="{color}"{extra} stroke-width="{_num(nominal)}" '
                    f'd="{_path_data(outline_vertices(clean))}" />')
        if alpha < 1.0:
            extra += f' stroke-opacity="{_num(alpha)}"'
        width = statistics.median(widths)
        line = [(p.x, p.y) for p in clean]
        if len(line) == 1:
            line.append(line[0])  # a dot: a zero-length segment with round caps
        return (f'<path class="write-stroke-pen" fill="none" stroke="{color}"{extra} stroke-width="{_num(width)}" '
                f'stroke-linecap="round" stroke-linejoin="round" d="{_path_data(line)}" />')

    def fill(self, stroke: Stroke) -> Optional[str]:
        rings = stroke.outline or [stroke.points]
        parts: List[str] = []
        for ring in rings:
            pts = [(_units(p.x), _units(p.y)) for p in ring if is_finite(p.x, p.y)
                   and abs(p.x) <= MAX_COORD_PT and abs(p.y) <= MAX_COORD_PT]
            if len(pts) >= 3:
                parts.append(_path_data(pts) + " Z")
        if not parts:
            self.counts.add("invalid")
            return None
        r, g, b, alpha = clamp_rgba(stroke.color)
        opacity = f' fill-opacity="{_num(alpha)}"' if alpha < 1.0 else ""
        return f'<path fill="{_hex((r, g, b, 1.0))}"{opacity} d="{" ".join(parts)}" />'

    # -- images and text ------------------------------------------------------------------------

    def image(self, image: Image) -> Optional[str]:
        data = bytes(image.data or b"")
        if not data:
            self.counts.add("image_empty")
            return None
        fmt = sniff_image(data)
        if fmt not in ("png", "jpeg"):
            self.counts.add("image_format")
            return None
        if not (is_finite(image.x, image.y, image.w, image.h) and image.w > 0 and image.h > 0
                and max(abs(image.x), abs(image.y), image.w, image.h) <= MAX_COORD_PT):
            self.counts.add("invalid")
            return None
        x, y, w, h = _units(image.x), _units(image.y), _units(image.w), _units(image.h)
        extra = ""
        pixels = image_pixel_size(data)
        if pixels and abs((pixels[0] / pixels[1]) / (w / h) - 1.0) > 0.01:
            extra += ' preserveAspectRatio="none"'
        rotation = float(image.rotation or 0.0) if is_finite(image.rotation or 0.0) else 0.0
        theta = math.radians(rotation % 360.0)
        if abs(math.sin(theta)) > 1e-9 or math.cos(theta) < 0:
            c, s = math.cos(theta), math.sin(theta)
            cx, cy = x + w / 2.0, y + h / 2.0
            values = (c, s, -s, c, cx - c * cx + s * cy, cy - s * cx - c * cy)
            extra = ' transform="matrix(' + " ".join(f"{v:.6g}" for v in values) + ')"' + extra
        mime = "image/png" if fmt == "png" else "image/jpeg"
        payload = base64.b64encode(data).decode("ascii")
        return (f'<image{extra} x="{_num(x)}" y="{_num(y)}" width="{_num(w)}" height="{_num(h)}" '
                f'xlink:href="data:{mime};base64,{payload}" />')

    def text(self, box: TextBox) -> Optional[str]:
        runs = [r for r in box.runs if r.text]
        if not runs or "".join(r.text for r in runs).strip() != (box.text or "").strip() and box.text:
            first_run = runs[0] if runs else None
            runs = [TextRun(box.text or "", first_run.bold, first_run.italic, first_run.underline, first_run.font,
                            first_run.size, first_run.color) if first_run else TextRun(box.text or "")]
        if not "".join(r.text for r in runs).strip():
            self.counts.add("text_empty")
            return None
        if not (is_finite(box.x, box.y) and max(abs(box.x), abs(box.y)) <= MAX_COORD_PT):
            self.counts.add("invalid")
            return None
        if self.text_left <= LINE_COST:
            self.counts.add("text_limit")
            return None
        cut = sum(len(r.text) for r in runs) > self.text_left
        if cut:  # nothing beyond the budget can be written: never wrap it
            runs = _cut(runs, self.text_left)
        size = box.size if is_finite(box.size) and 0 < box.size <= MAX_SIZE_PT else 12.0
        runs = [TextRun(r.text, r.bold, r.italic, r.underline, r.font,
                        r.size if r.size and is_finite(r.size) and 0 < r.size <= MAX_SIZE_PT else size,
                        r.color or box.color) for r in runs]
        first = runs[0]
        size_u = _units(first.size or size)
        color = first.color or box.color
        w = _units(box.w) if is_finite(box.w) and 0 < box.w <= MAX_COORD_PT else 0.0
        x, top = _units(box.x), _units(box.y)
        anchor, ax = "", x
        if box.align == "center":
            anchor, ax = ' text-anchor="middle"', x + w / 2.0
        elif box.align == "right":
            anchor, ax = ' text-anchor="end"', x + w
        family, _bold, _italic = _family(first.font)
        attrs = f'x="{_num(ax)}" y="{_num(top + TEXT_ASCENT * size_u)}" font-size="{_num(size_u)}"'
        if family:
            attrs += f" font-family={_attr(family + ', sans-serif')}"
        attrs += f' fill="{_hex(color)}"'
        alpha = clamp_rgba(color)[3]
        if alpha < 1.0:
            attrs += f' fill-opacity="{_num(alpha)}"'
        attrs += anchor
        rotation = float(box.rotation or 0.0) if is_finite(box.rotation or 0.0) else 0.0
        if abs(math.sin(math.radians(rotation))) > 1e-9 or math.cos(math.radians(rotation)) < 0:
            attrs += f' transform="rotate({_num(rotation % 360.0)} {_num(x)} {_num(top)})"'
        body: List[str] = []
        baseline = top + TEXT_ASCENT * size_u
        # an empty line draws nothing and is not written: the lines after it keep their own y
        for k, line in _wrap_lines(runs, box.w if w > 0 else 0.0):
            chars = sum(len(r.text) for r in line)
            if chars + LINE_COST > self.text_left:
                cut = True
                line = _cut(line, self.text_left - LINE_COST)
                chars = sum(len(r.text) for r in line)
                if not line:
                    break
            self.text_left -= chars + LINE_COST
            inner = "".join(self.run(run, first, color) for run in line)
            body.append(f'<tspan x="{_num(ax)}" y="{_num(baseline + k * TEXT_LINE * size_u)}">{inner}</tspan>')
            if cut and self.text_left <= LINE_COST:
                break
        if cut:
            self.counts.add("text_limit")
        if not body:
            return None
        self.counts.add("text")
        return f'<text xml:space="preserve" {attrs}>{"".join(body)}</text>'

    def run(self, run: TextRun, first: TextRun, color: RGBA) -> str:
        family, bold, italic = _family(run.font)
        attrs = ""
        if run.bold or bold:
            attrs += ' font-weight="bold"'
        if run.italic or italic:
            attrs += ' font-style="italic"'
        if run.underline:
            attrs += ' text-decoration="underline"'
        if family and family != _family(first.font)[0]:
            attrs += f" font-family={_attr(family + ', sans-serif')}"
        if run.size and run.size != first.size:
            attrs += f' font-size="{_num(_units(run.size))}"'
        if run.color and _hex(run.color) != _hex(color):
            attrs += f' fill="{_hex(run.color)}"'
        return f"<tspan{attrs}>{_text(run.text)}</tspan>" if attrs else _text(run.text)

    # -- document ----------------------------------------------------------------------------------

    def page(self, index: int, page: Page, y: float, out: List[str]) -> float:
        w, h = self.size(page)
        paper, xr, yr = self.ruling(page)
        out.append(f'<svg class="write-page" color-interpolation="linearRGB" x="{_num(PAGE_POSITION)}" '
                   f'y="{_num(y)}" width="{_num(w)}px" height="{_num(h)}px">')
        out.append(f'<g class="write-content write-v3" width="{_num(w)}" height="{_num(h)}" xruling="{_num(xr)}" '
                   f'yruling="{_num(yr)}" marginLeft="0" papercolor="{DEFAULT_PAPER_COLOR}" '
                   f'rulecolor="{DEFAULT_RULE_COLOR}">')
        out += self.ruleline(index, paper, xr, yr, w, h)
        for image in page.images:
            xml = self.image(image)
            if xml:
                out.append(xml)
        for stroke in page.strokes:
            xml = self.stroke(stroke)
            if xml:
                out.append(xml)
        for box in page.texts:
            xml = self.text(box)
            if xml:
                out.append(xml)
        out.append("</g>")
        out.append("</svg>")
        return h

    def svg(self) -> str:
        pages = list(self.doc.pages)
        if not pages:
            self.doc.warn("The document has no pages; one empty page was written")
            pages = [Page(DEFAULT_PAGE_UNITS[0] * PT_PER_UNIT, DEFAULT_PAGE_UNITS[1] * PT_PER_UNIT)]
        out = ['<svg id="write-document" xmlns="http://www.w3.org/2000/svg" '
               'xmlns:xlink="http://www.w3.org/1999/xlink">',
               '<rect id="write-doc-background" width="100%" height="100%" fill="#808080" />',
               '<defs id="write-defs">',
               '<script type="text/writeconfig">',
               '  <int name="docFormatVersion" value="2" />',
               '  <int name="pageNum" value="0" />',
               f'  <float name="xOffset" value="{_num(-PAGE_POSITION)}" />',
               f'  <float name="yOffset" value="{_num(-PAGE_POSITION)}" />',
               "</script>",
               "</defs>"]
        y = PAGE_POSITION
        for index, page in enumerate(pages):
            y += self.page(index, page, y, out) + PAGE_GAP
        out.append("</svg>")
        self.counts.flush(self.doc)
        return "\n".join(out) + "\n"


def build_svg(doc: Document, options: Any = None) -> str:
    """The uncompressed SVG text :func:`write_write_svg` compresses."""
    return _Writer(doc, options).svg()


def write_write_svg(doc: Document, options: Any = None) -> bytes:
    """Serialise ``doc`` as a gzip-compressed Stylus Labs Write document (``.svgz``).

    ``options`` is duck-typed: ``pressure`` (``False`` writes every stroke with a constant
    width) and ``paper`` (``"pdf"`` reports stock-paper PDFs as dropped too) are honoured.
    Lossy steps are reported through ``doc.warn``.
    """
    payload = build_svg(doc, options).encode("utf-8")
    return gzip.compress(payload, compresslevel=9, mtime=0)
