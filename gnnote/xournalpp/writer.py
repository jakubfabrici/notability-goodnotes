"""Model -> Xournal++ ``.xopp`` writer (``docs/xournalpp.md`` section 3).

Output: gzip-compressed XML, file version 4 (what every released Xournal++ writes; readers
from Xournal++ 1.0 on open it).  A document whose pages show a background PDF is written as
the ZIP-packaged variant instead (``mimetype``, ``META-INF/version``, ``content.xml``,
``attachments/bg.pdf``), the only single-file form that carries the PDF; Xournal++ 1.2 and
later read it.  The shape mirrors inkterop's writer, which passed an open check in Xournal++
1.3.5, plus the elements the reader understands:

* page: ``<page width height>`` in pt, ``<background type="solid" color="#ffffffff"
  style=plain|ruled|graph|dotted>`` from ``Page.paper``, or ``<background type="pdf"
  domain="attach" filename="attachments/bg.pdf" pageno=N>`` (``domain`` / ``filename`` on the
  first PDF page only, as Xournal++ writes them); one ``<layer>`` with images, then strokes,
  then text.
* stroke: ``tool="pen"`` or ``"highlighter"`` (alpha byte 7f, as Xournal++ writes it),
  ``color="#rrggbbaa"``, ``width`` = one value for a constant width, else the nominal width
  followed by the width of every point except the last; Bezier chains are flattened to about
  1 pt; a single point becomes two identical points (Xournal++ ignores one-point strokes).  A
  shape fill (``Stroke.kind == "fill"``) becomes the ``fill`` attribute (alpha x 255) of the
  outline stroke next to it when its bounding box and colour match, else its own stroke with
  a 0.1 pt outline.
* ``<image left top right bottom>`` base64 PNG / JPEG; PDF images become ``<teximage
  text="">`` (Xournal++ renders a teximage's PDF); ``<text font size x y color>`` with
  ``align`` and, for long lines, ``wrap``.  A rotation is written as a file-version-5
  ``matrix`` attribute next to the version-4 box, which released Xournal++ (1.3.8) ignores.
"""
from __future__ import annotations

import base64
import gzip
import io
import math
import re
import statistics
import time
import zipfile
from collections import Counter as _Tally
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

from .. import __version__, pdfutil
from ..codecutil import (Counter, bbox_matches, clamp_rgba, image_pixel_size, is_finite, sniff_image,
                         stroke_polyline, to_byte)
from ..model import Document, Image, Page, Point, Stroke, TextBox
from . import (DEFAULT_PAGE_SIZE, FILE_VERSION, FILL_OUTLINE_WIDTH, HIGHLIGHTER_ALPHA, PACKAGE_CONTENT,
               PACKAGE_MIMETYPE, PACKAGE_PDF, PACKAGE_VERSION, STYLE_FOR_PAPER)

__all__ = ["write_xopp", "build_xml"]

MAX_PAGE_SIDE = 1e6
TEXT_CHAR_EM = 0.5  # average glyph width in ems used to decide whether text needs wrapping
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")

_MESSAGES = {
    "pdf_other": "{n} pages show another background PDF than the first one; a Xournal++ file holds "
                 "one background PDF, so they got plain paper",
    "pdf_missing": "{n} pages refer to a background PDF that is missing or unreadable; plain paper was used",
    "pdf_page": "{n} pages refer to a page their background PDF does not have; plain paper was used",
    "image_format": "{n} images are neither PNG, JPEG nor PDF and were skipped",
    "image_empty": "{n} images without data were skipped",
    "rotated": "{n} rotated images or text boxes are shown unrotated by Xournal++ up to 1.3.8 (the "
               "rotation is stored for newer versions)",
    "fill_alone": "{n} shape fills have no matching outline stroke and got their own stroke with a hairline outline",
    "pencil": "{n} pencil strokes were written as Xournal++ pen strokes",
    "invalid": "{n} strokes, images or text boxes with invalid coordinates were skipped",
    "text_runs": "{n} text boxes mixed several fonts, sizes or colours; Xournal++ text has one style, "
                 "the first run's was used",
    "text_empty": "{n} empty text boxes were skipped",
    "page_size": "{n} pages had no valid size; A4 was used",
}


def _opt(options: Any, name: str, default: Any) -> Any:
    value = getattr(options, name, None) if options is not None else None
    return default if value is None else value


def _fmt(v: float) -> str:
    """Compact decimal (4 places) the way Xournal++ prints coordinates."""
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _hex(color: Tuple[float, float, float, float], alpha: Optional[int] = None) -> str:
    r, g, b, a = clamp_rgba(color)
    av = to_byte(a) if alpha is None else alpha
    return "#%02x%02x%02x%02x" % (to_byte(r), to_byte(g), to_byte(b), av)


def _text(value: str) -> str:
    """XML character data: invalid characters removed, ``\\r`` kept as a character reference."""
    return escape(_INVALID_XML.sub("", value)).replace("\r", "&#13;")


def _attr(value: str) -> str:
    return quoteattr(_INVALID_XML.sub("", value)).replace("\r", "&#13;").replace("\n", "&#10;")


def _font_name(box: TextBox) -> str:
    """A Pango font description from the first run that names a font (``"Sans"`` default)."""
    run = next((r for r in box.runs if r.font), None)
    bold = any(r.bold for r in box.runs[:1])
    italic = any(r.italic for r in box.runs[:1])
    name = (run.font if run is not None else "") or "Sans"
    name = name.strip()
    m = re.fullmatch(r"([^-]+)-(\w+)", name)
    if m and re.search(r"bold|italic|oblique|regular|light|medium|black|heavy|semibold|book|roman",
                       m.group(2), re.I):
        family, style = m.group(1), m.group(2)
        bold = bold or "bold" in style.lower() or "black" in style.lower() or "heavy" in style.lower()
        italic = italic or "italic" in style.lower() or "oblique" in style.lower()
        name = family
    lowered = name.lower()
    if bold and "bold" not in lowered:
        name += " Bold"
    if italic and "italic" not in lowered and "oblique" not in lowered:
        name += " Italic"
    return name


class _Writer:
    def __init__(self, doc: Document, options: Any):
        self.doc = doc
        self.options = options
        self.counts = Counter(_MESSAGES)
        self.paper_mode = str(_opt(options, "paper", "plain"))
        self.pdf_id: Optional[str] = None
        self.pdf_pages = 0
        self.pdf_named = False  # the first PDF page carries domain / filename

    # -- background PDF ---------------------------------------------------------------------

    def carried_pdf(self, page: Page) -> Optional[str]:
        bg = page.background
        if bg is None or (page.template_is_builtin and self.paper_mode != "pdf"):
            return None
        return bg.pdf_id

    def choose_pdf(self, pages: List[Page]) -> None:
        """The one background PDF of the output: the one most pages show."""
        tally = _Tally(pid for pid in (self.carried_pdf(p) for p in pages) if pid is not None)
        for pdf_id, _n in tally.most_common():
            data = self.doc.pdfs.get(pdf_id)
            if not data:
                continue
            try:
                info = pdfutil.pdf_info(data)
            except (ValueError, TypeError, OverflowError, RecursionError):
                continue
            if info.pages:
                self.pdf_id, self.pdf_pages = pdf_id, len(info.pages)
                return

    def background(self, page: Page) -> str:
        pdf_id = self.carried_pdf(page)
        if pdf_id is not None:
            if pdf_id != self.pdf_id:
                self.counts.add("pdf_other" if self.pdf_id is not None and pdf_id in self.doc.pdfs
                                else "pdf_missing")
            elif not 0 <= int(page.background.page_index) < self.pdf_pages:  # type: ignore[union-attr]
                self.counts.add("pdf_page")
            else:
                number = int(page.background.page_index) + 1  # type: ignore[union-attr]
                if not self.pdf_named:
                    self.pdf_named = True
                    return (f'<background type="pdf" domain="attach" filename="{PACKAGE_PDF}" '
                            f'pageno="{number}"/>')
                return f'<background type="pdf" pageno="{number}"/>'
        style = STYLE_FOR_PAPER.get((page.paper or "plain").lower(), "plain")
        return f'<background type="solid" color="#ffffffff" style="{style}"/>'

    # -- elements --------------------------------------------------------------------------------

    def stroke_attrs(self, stroke: Stroke, pts: List[Point]) -> Tuple[str, str, str]:
        """``(tool, color, width)`` attribute values of an ink stroke."""
        highlighter = stroke.kind == "highlighter"
        tool = "highlighter" if highlighter else "pen"
        color = _hex(stroke.color, HIGHLIGHTER_ALPHA if highlighter else None)
        widths = [p.width for p in pts]
        if max(widths) - min(widths) <= 1e-6 * max(1.0, max(widths)):
            return tool, color, _fmt(widths[0])
        nominal = stroke.width if is_finite(stroke.width) and stroke.width > 0 else statistics.median(widths)
        return tool, color, " ".join([_fmt(nominal)] + [_fmt(w) for w in widths[:-1]])

    def strokes(self, page: Page) -> List[str]:
        """XML of a page's strokes, with every fill merged into its outline when possible."""
        items: List[Dict[str, Any]] = []
        for stroke in page.strokes:
            if stroke.kind == "fill":
                polygon = [p for p in ((stroke.outline or [[]])[0] or stroke.points) if is_finite(p.x, p.y)]
                if len(polygon) < 3:
                    continue
                items.append({"fill": stroke, "polygon": polygon})
                continue
            pts = stroke_polyline(stroke)
            if not pts:
                if stroke.points:
                    self.counts.add("invalid")
                continue
            if stroke.pen == "pencil":
                self.counts.add("pencil")
            if len(pts) == 1:
                pts = [pts[0], Point(pts[0].x, pts[0].y, pts[0].width)]
            items.append({"stroke": stroke, "points": pts, "fill_alpha": None})
        # merge fills into the neighbouring outline stroke (GoodNotes stores both orders)
        for i, item in enumerate(items):
            if "fill" not in item:
                continue
            fill: Stroke = item["fill"]
            for j in (i - 1, i + 1):
                if not 0 <= j < len(items) or "stroke" not in items[j]:
                    continue
                parent = items[j]
                if parent["fill_alpha"] is None and self.same_rgb(parent["stroke"], fill) \
                        and bbox_matches(item["polygon"], parent["points"]):
                    parent["fill_alpha"] = clamp_rgba(fill.color)[3]
                    item["merged"] = True
                    break
        out: List[str] = []
        for item in items:
            if "stroke" in item:
                tool, color, width = self.stroke_attrs(item["stroke"], item["points"])
                extra = ""
                if item["fill_alpha"] is not None:
                    extra = f' fill="{to_byte(item["fill_alpha"])}"'
                coords = " ".join(f"{_fmt(p.x)} {_fmt(p.y)}" for p in item["points"])
                out.append(f'<stroke tool="{tool}" color="{color}" width="{width}"{extra} '
                           f'capStyle="round">{coords}</stroke>')
            elif not item.get("merged"):
                fill = item["fill"]
                polygon = item["polygon"] + [item["polygon"][0]]
                r, g, b, a = clamp_rgba(fill.color)
                self.counts.add("fill_alone")
                coords = " ".join(f"{_fmt(p.x)} {_fmt(p.y)}" for p in polygon)
                out.append(f'<stroke tool="pen" color="{_hex((r, g, b, 1.0))}" width="{_fmt(FILL_OUTLINE_WIDTH)}" '
                           f'fill="{to_byte(a)}" capStyle="round">{coords}</stroke>')
        return out

    @staticmethod
    def same_rgb(a: Stroke, b: Stroke) -> bool:
        ca, cb = clamp_rgba(a.color), clamp_rgba(b.color)
        return all(abs(x - y) <= 1.5 / 255.0 for x, y in zip(ca[:3], cb[:3]))

    def matrix(self, image: Image, natural: Optional[Tuple[float, float]]) -> str:
        """`` matrix="..."`` for a rotated image (empty when unrotated or unmeasurable)."""
        rotation = float(image.rotation or 0.0)
        theta = math.radians(rotation % 360.0) if is_finite(rotation) else 0.0
        if abs(math.sin(theta)) < 1e-9 and math.cos(theta) > 0:
            return ""
        self.counts.add("rotated")
        if natural is None or not (natural[0] > 0 and natural[1] > 0):
            return ""
        sx, sy = image.w / natural[0], image.h / natural[1]
        c, s = math.cos(theta), math.sin(theta)
        cx, cy = image.x + image.w / 2.0, image.y + image.h / 2.0
        x0 = cx - c * image.w / 2.0 + s * image.h / 2.0
        y0 = cy - s * image.w / 2.0 - c * image.h / 2.0
        values = (c * sx, s * sx, -s * sy, c * sy, x0, y0)
        return ' matrix="' + " ".join(_fmt(v) if abs(v) >= 1e-4 else f"{v:.10g}" for v in values) + '"'

    def image(self, image: Image) -> Optional[str]:
        data = bytes(image.data or b"")
        if not data:
            self.counts.add("image_empty")
            return None
        if not (is_finite(image.x, image.y, image.w, image.h) and image.w > 0 and image.h > 0):
            self.counts.add("invalid")
            return None
        fmt = sniff_image(data)
        box = (f'left="{_fmt(image.x)}" top="{_fmt(image.y)}" right="{_fmt(image.x + image.w)}" '
               f'bottom="{_fmt(image.y + image.h)}"')
        payload = base64.b64encode(data).decode("ascii")
        if fmt in ("png", "jpeg"):
            pixels = image_pixel_size(data)
            natural = (float(pixels[0]), float(pixels[1])) if pixels else None
            return f"<image {box}{self.matrix(image, natural)}>{payload}</image>"
        if fmt == "pdf":
            natural = None
            try:
                info = pdfutil.pdf_info(data)
                if info.pages:
                    natural = (info.pages[0].width, info.pages[0].height)
            except (ValueError, TypeError, OverflowError, RecursionError):
                natural = None
            return f'<teximage text="" {box}{self.matrix(image, natural)}>{payload}</teximage>'
        self.counts.add("image_format")
        return None

    def text(self, box: TextBox) -> Optional[str]:
        content = box.text or "".join(r.text for r in box.runs)
        if not content.strip():
            self.counts.add("text_empty")
            return None
        if not is_finite(box.x, box.y):
            self.counts.add("invalid")
            return None
        size = box.size if is_finite(box.size) and box.size > 0 else 12.0
        color = box.color
        if box.runs:
            first = box.runs[0]
            size = first.size if first.size and is_finite(first.size) and first.size > 0 else size
            color = first.color or color
            styles = {(r.font, r.size, tuple(r.color) if r.color else None, r.bold, r.italic) for r in box.runs
                      if r.text.strip()}
            if len(styles) > 1:
                self.counts.add("text_runs")
        attrs = (f'font={_attr(_font_name(box))} size="{_fmt(size)}" x="{_fmt(box.x)}" y="{_fmt(box.y)}" '
                 f'color="{_hex(color)}"')
        rotation = float(box.rotation or 0.0)
        theta = math.radians(rotation % 360.0) if is_finite(rotation) else 0.0
        if abs(math.sin(theta)) > 1e-9 or math.cos(theta) < 0:
            self.counts.add("rotated")
            c, s = math.cos(theta), math.sin(theta)
            attrs += ' matrix="' + " ".join(f"{v:.10g}" for v in (c, s, -s, c, box.x, box.y)) + '"'
        longest = max(len(line) for line in content.split("\n"))
        if is_finite(box.w) and box.w > 0 and longest * size * TEXT_CHAR_EM > box.w * 1.05:
            attrs += f' wrap="{_fmt(box.w)}"'
        if box.align in ("center", "right"):
            attrs += f' align="{box.align}"'
        return f"<text {attrs}>{_text(content)}</text>"

    # -- document ------------------------------------------------------------------------------------

    def page_xml(self, page: Page, out: List[str]) -> None:
        w, h = page.width, page.height
        if not (is_finite(w, h) and 0 < w <= MAX_PAGE_SIDE and 0 < h <= MAX_PAGE_SIDE):
            self.counts.add("page_size")
            w, h = DEFAULT_PAGE_SIZE
        out.append(f'<page width="{_fmt(w)}" height="{_fmt(h)}">')
        out.append(self.background(page))
        body: List[str] = []
        for image in page.images:
            xml = self.image(image)
            if xml:
                body.append(xml)
        body += self.strokes(page)
        for box in page.texts:
            xml = self.text(box)
            if xml:
                body.append(xml)
        if body:
            out.append("<layer>")
            out += body
            out.append("</layer>")
        else:
            out.append("<layer/>")
        out.append("</page>")

    def xml(self) -> str:
        pages = list(self.doc.pages)
        if not pages:
            self.doc.warn("The document has no pages; one empty page was written")
            pages = [Page(*DEFAULT_PAGE_SIZE)]
        self.choose_pdf(pages)
        title = str(_opt(self.options, "title", None) or self.doc.title or "Untitled")
        out = ['<?xml version="1.0" standalone="no"?>',
               f'<xournal creator="gnnote {__version__}" fileversion="{FILE_VERSION}">',
               f"<title>{_text(title)}</title>"]
        for page in pages:
            self.page_xml(page, out)
        out.append("</xournal>")
        self.counts.flush(self.doc)
        return "\n".join(out) + "\n"


def build_xml(doc: Document, options: Any = None) -> Tuple[str, Optional[bytes]]:
    """``(content XML, background PDF or None)``; ``write_xopp`` packages them."""
    writer = _Writer(doc, options)
    xml = writer.xml()
    pdf = doc.pdfs.get(writer.pdf_id) if writer.pdf_named and writer.pdf_id else None
    return xml, pdf


def write_xopp(doc: Document, options: Any = None) -> bytes:
    """Serialise ``doc`` as a Xournal++ ``.xopp`` (see the module docstring).

    ``options`` is duck-typed: ``paper`` ("pdf" also carries stock-paper PDFs), ``title`` and
    ``random_seed`` (fixes the ZIP timestamps of the packaged variant) are honoured.  Lossy steps
    are reported through ``doc.warn``.
    """
    xml, pdf = build_xml(doc, options)
    payload = xml.encode("utf-8")
    if pdf is None:
        return gzip.compress(payload, compresslevel=9, mtime=0)
    stamp = (1980, 1, 1, 0, 0, 0)  # ZIP time stamps start in 1980
    if _opt(options, "random_seed", None) is None:
        stamp = max(stamp, tuple(time.localtime()[:6]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data, method in (("mimetype", PACKAGE_MIMETYPE, zipfile.ZIP_STORED),
                                   ("META-INF/version", PACKAGE_VERSION, zipfile.ZIP_STORED),
                                   (PACKAGE_CONTENT, payload, zipfile.ZIP_DEFLATED),
                                   (PACKAGE_PDF, pdf, zipfile.ZIP_DEFLATED)):
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = method
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zf.writestr(info, data)
    return buf.getvalue()
