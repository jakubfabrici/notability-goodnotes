"""reMarkable ``.rmdoc`` documents and v6 ``.rm`` pages -> :class:`gnnote.model.Document`.

An ``.rmdoc`` (the desktop app's export / import format) is a ZIP (``docs/remarkable.md``)::

    <doc>.metadata          JSON: visibleName, ...
    <doc>.content           JSON: fileType (notebook / pdf / epub), orientation, the pages
                            (cPages.pages[]: id, idx, template, redir, deleted; or pages[] with
                            redirectionPageMap in older files)
    <doc>.pagedata          template names, one per line (older files)
    <doc>.pdf / <doc>.epub  the annotated document
    <doc>/<page>.rm         one v6 scene per page that has content (:mod:`.scene`)

Mapping:

* Units: canvas units x 72/226 (226 dpi; reMarkable 2 canvas 1404 x 1872, Paper Pro
  1620 x 2160 as the page's scene info records).  x is centred on 0, y grows downwards.
* Notebook pages: the canvas (``orientation`` landscape swaps it), grown to the ink outside
  it (pages scroll) with a 48-unit margin.  The template name sets ``Page.paper``.
* Pages of a PDF (or of an EPUB with its PDF rendition in the bundle): the PDF page ``redir``
  points to, as background; ink maps with the PDF at 226 dpi and x centred on the page.
* Lines: per-point widths are the stored rendered widths (version 2: value / 4); colours from
  the line's RGBA when present, else the tool palette; highlighter and shader are
  highlighters; erasers and hidden layers are skipped; groups anchored to typed text are
  shifted to their (approximated) anchor.  Text highlights become highlighter strokes;
  typed text becomes one text box per page.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..model import RGBA, Document, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from ..readutil import PointBudget, ZipBundle, load_json
from .scene import Glyph, Group, Line, Paragraph, Scene, SceneError, parse_scene

__all__ = ["read_remarkable", "PT_PER_UNIT", "RM2_CANVAS", "PALETTE", "TOOL_NAMES"]

PT_PER_UNIT = 72.0 / 226.0
RM2_CANVAS = (1404, 1872)
GROWTH_MARGIN = 48.0  # canvas units kept around ink that grows a page
GROWTH_TOLERANCE = 8.0  # ink this far beyond the canvas edge (a pen resting on the bezel) does not grow it
MIN_WIDTH_UNITS = 0.5
MAX_COORD = 1_000_000.0  # canvas units; anything further is damage
MAX_PAGES = 10_000
TOP_ANCHOR = (0, 0xFFFFFFFFFFFE)  # groups drawn relative to the top of the typed text
BOTTOM_ANCHOR = (0, 0xFFFFFFFFFFFF)  # ... and to its bottom
ANCHOR_OFFSET = 33.6  # first text line below pos_y (measured on one page; see docs/remarkable.md)
LINE_HEIGHTS = {0: 70.0, 1: 70.0, 2: 150.0, 3: 70.0, 4: 35.0, 5: 35.0, 6: 35.0, 7: 35.0}  # rmc's values
TEXT_SIZES = {2: 22.0}  # pt by paragraph style; 11 pt otherwise
BULLETS = {4: "• ", 5: "◦ ", 6: "☐ ", 7: "☑ "}

# tool id -> (family, kind, pen name)
TOOL_NAMES: Dict[int, Tuple[str, str, Optional[str]]] = {
    0: ("paintbrush", "pen", "brush"), 12: ("paintbrush", "pen", "brush"),
    1: ("pencil", "pen", "pencil"), 14: ("pencil", "pen", "pencil"),
    2: ("ballpoint", "pen", "ballpoint"), 15: ("ballpoint", "pen", "ballpoint"),
    3: ("marker", "pen", "marker"), 16: ("marker", "pen", "marker"),
    4: ("fineliner", "pen", None), 17: ("fineliner", "pen", None),
    5: ("highlighter", "highlighter", None), 18: ("highlighter", "highlighter", None),
    6: ("eraser", "eraser", None), 8: ("eraser", "eraser", None),
    7: ("mechanical pencil", "pen", "pencil"), 13: ("mechanical pencil", "pen", "pencil"),
    21: ("calligraphy", "pen", "fountain"),
    23: ("shader", "highlighter", None),
}

# colour id -> RGB (the palette rmc and inkterop use; 9 = highlight yellow)
PALETTE: Dict[int, Tuple[int, int, int]] = {
    0: (0, 0, 0), 1: (144, 144, 144), 2: (255, 255, 255), 3: (251, 247, 25), 4: (0, 255, 0),
    5: (255, 192, 203), 6: (78, 105, 201), 7: (179, 62, 57), 8: (125, 125, 125), 9: (255, 237, 117),
    10: (161, 216, 125), 11: (139, 208, 229), 12: (183, 130, 205), 13: (247, 232, 81),
}


def _paper(template: str) -> Tuple[str, bool]:
    """Template name -> (Page.paper, known)."""
    name = template.strip().lower()
    if not name or name == "blank":
        return "plain", True
    if "dot" in name:
        return "dotted", True
    if "grid" in name or "square" in name or "checker" in name:
        return "grid", True
    if "line" in name or "ruled" in name:
        return "lined", True
    return "plain", False


def _color(line_color: int, rgba: Optional[Tuple[int, int, int, int]]) -> RGBA:
    if rgba is not None:
        r, g, b, a = rgba
        return (r / 255.0, g / 255.0, b / 255.0, a / 255.0)
    r, g, b = PALETTE.get(line_color, (0, 0, 0))
    return (r / 255.0, g / 255.0, b / 255.0, 1.0)


class _PageInput:
    def __init__(self, page_id: str, template: str = "", redir: Optional[int] = None):
        self.page_id = page_id
        self.template = template
        self.redir = redir


class _Reader:
    def __init__(self) -> None:
        self.doc = Document(source_format="remarkable")
        self.budget = PointBudget()
        self.counts: Dict[str, int] = {}
        self.unknown_templates: List[str] = []

    def count(self, what: str, n: int = 1) -> None:
        self.counts[what] = self.counts.get(what, 0) + n

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    # -- one page --------------------------------------------------------------------------

    def anchors(self, scene: Scene, canvas_h: float) -> Dict[Tuple[int, int], float]:
        """y of every text anchor (paragraph and character ids, top and bottom of the text)."""
        text = scene.text
        if text is None:
            return {TOP_ANCHOR: 0.0, BOTTOM_ANCHOR: canvas_h}
        y = text.pos_y + ANCHOR_OFFSET
        out: Dict[Tuple[int, int], float] = {TOP_ANCHOR: y}
        for paragraph in text.paragraphs():
            out[paragraph.start_id] = y
            for cid in paragraph.char_ids:
                out[cid] = y
            y += LINE_HEIGHTS.get(paragraph.style, 70.0)
        out[BOTTOM_ANCHOR] = y
        return out

    def page(self, number: int, scene: Optional[Scene], canvas: Tuple[float, float],
             pdf: Optional[Tuple[str, int, float, float]], template: str) -> Page:
        """Build one page.  ``canvas`` is the notebook canvas (display orientation); ``pdf``
        (pdf_id, page index, width pt, height pt) makes it a PDF-backed page."""
        items: List[Tuple[Any, float, float]] = []  # (line or glyph, dx, dy) in canvas units
        if scene is not None:
            if scene.unreadable_blocks:
                self.warn(f"Page {number}: {scene.unreadable_blocks} damaged block(s) skipped")
            if scene.skipped_points:
                self.count("budget")
            anchors = self.anchors(scene, canvas[1])
            for value, chain in scene.walk():
                if any(not g.visible for g in chain):
                    self.count("hidden")
                    continue
                dx, dy = self.offset(chain, anchors)
                items.append((value, dx, dy))
        strokes: List[Tuple[List[Tuple[float, float, float]], Stroke]] = []
        for value, dx, dy in items:
            if isinstance(value, Line):
                made = self.line(value, dx, dy)
                if made is not None:
                    strokes.append(made)
            elif isinstance(value, Glyph):
                strokes.extend(self.glyph(value, dx, dy))
        texts = self.text(scene) if scene is not None else None
        if pdf is not None:
            pdf_id, index, w_pt, h_pt = pdf
            ox, oy = -w_pt / 2.0 / PT_PER_UNIT, 0.0
            page = Page(width=w_pt, height=h_pt, background=PdfBackground(pdf_id, index))
        else:
            cw, ch = canvas
            x0, x1, y0, y1 = -cw / 2.0, cw / 2.0, 0.0, ch
            xs = [x for pts, _ in strokes for x, _y, _w in pts]
            ys = [y for pts, _ in strokes for _x, y, _w in pts]
            if xs:
                if min(xs) < x0 - GROWTH_TOLERANCE:
                    x0 = min(xs) - GROWTH_MARGIN
                if max(xs) > x1 + GROWTH_TOLERANCE:
                    x1 = max(xs) + GROWTH_MARGIN
                if min(ys) < y0 - GROWTH_TOLERANCE:
                    y0 = min(ys) - GROWTH_MARGIN
                if max(ys) > y1 + GROWTH_TOLERANCE:
                    y1 = max(ys) + GROWTH_MARGIN
            if (x0, x1, y0, y1) != (-cw / 2.0, cw / 2.0, 0.0, ch):
                self.count("grown")
            ox, oy = x0, y0
            page = Page(width=(x1 - x0) * PT_PER_UNIT, height=(y1 - y0) * PT_PER_UNIT)
            paper, known = _paper(template)
            page.paper = paper
            if not known and template not in self.unknown_templates:
                self.unknown_templates.append(template)
        for pts, stroke in strokes:
            stroke.points = [Point((x - ox) * PT_PER_UNIT, (y - oy) * PT_PER_UNIT, w * PT_PER_UNIT) for x, y, w in pts]
            stroke.width *= PT_PER_UNIT
            page.strokes.append(stroke)
        if texts is not None:
            box, (tx, ty) = texts
            box.x, box.y = (tx - ox) * PT_PER_UNIT, (ty - oy) * PT_PER_UNIT
            page.texts.append(box)
        return page

    def offset(self, chain: Sequence[Group], anchors: Dict[Tuple[int, int], float]) -> Tuple[float, float]:
        dx = dy = 0.0
        for group in chain:
            if group.anchor_id is None:
                continue
            self.count("anchored")
            dx += group.anchor_origin_x if group.anchor_origin_x is not None and math.isfinite(
                group.anchor_origin_x) else 0.0
            y = anchors.get(group.anchor_id)
            if y is None:
                self.count("lost-anchor")
            else:
                dy += y
        return dx, dy

    def line(self, line: Line, dx: float, dy: float) -> Optional[Tuple[List[Tuple[float, float, float]], Stroke]]:
        family, kind, pen = TOOL_NAMES.get(line.tool, ("pen", "pen", None))
        if kind == "eraser":
            self.count("erasers")
            return None
        pts = [(p.x + dx, p.y + dy, max(p.width, MIN_WIDTH_UNITS) if p.width < MAX_COORD else MIN_WIDTH_UNITS)
               for p in line.points
               if math.isfinite(p.x) and math.isfinite(p.y) and abs(p.x) <= MAX_COORD and abs(p.y) <= MAX_COORD]
        if not pts:
            if line.points:
                self.count("unusable")
            return None
        widths = sorted(w for _x, _y, w in pts)
        if line.tool not in TOOL_NAMES:
            self.count("unknown-tools")
        stroke = Stroke(points=[], color=_color(line.color, line.rgba), kind=kind, pen=pen,
                        width=widths[len(widths) // 2])
        return pts, stroke

    def glyph(self, glyph: Glyph, dx: float, dy: float) -> List[Tuple[List[Tuple[float, float, float]], Stroke]]:
        """A text highlight: one highlighter stroke through the middle of each rectangle."""
        out = []
        color = _color(glyph.color, glyph.rgba)
        for x, y, w, h in glyph.rects:
            if not all(math.isfinite(v) and abs(v) <= MAX_COORD for v in (x, y, w, h)) or h <= 0:
                continue
            mid = y + h / 2.0 + dy
            pts = [(x + dx, mid, h), (x + w + dx, mid, h)]
            out.append((pts, Stroke(points=[], color=color, kind="highlighter", width=h)))
            self.count("glyphs")
        return out

    def text(self, scene: Scene) -> Optional[Tuple[TextBox, Tuple[float, float]]]:
        """Typed text as one text box (layout approximated); position in canvas units."""
        text = scene.text
        if text is None or not all(math.isfinite(v) for v in (text.pos_x, text.pos_y, text.width)):
            return None
        paragraphs: List[Paragraph] = text.paragraphs()
        while paragraphs and not paragraphs[-1].text.strip():
            paragraphs.pop()
        if not any(p.text.strip() for p in paragraphs):
            return None
        runs: List[TextRun] = []
        lines: List[str] = []
        height = 0.0
        for index, paragraph in enumerate(paragraphs):
            size = TEXT_SIZES.get(paragraph.style, 11.0)
            bold_style = paragraph.style in (2, 3)
            prefix = BULLETS.get(paragraph.style, "")
            if index:
                runs.append(TextRun("\n", size=size))
            if prefix:
                runs.append(TextRun(prefix, size=size))
            for chunk, bold, italic in paragraph.runs:
                runs.append(TextRun(chunk, bold=bold or bold_style, italic=italic, size=size))
            lines.append(prefix + paragraph.text)
            height += LINE_HEIGHTS.get(paragraph.style, 70.0)
        self.count("texts")
        width = text.width if 0 < text.width < MAX_COORD else 936.0
        box = TextBox(0.0, 0.0, width * PT_PER_UNIT, height * PT_PER_UNIT, "\n".join(lines), runs=runs, size=11.0)
        return box, (text.pos_x, text.pos_y)

    # -- warnings ----------------------------------------------------------------------------

    def report(self) -> None:
        c = self.counts
        if c.get("grown"):
            self.warn(f"{c['grown']} page(s) were enlarged to fit ink drawn outside the canvas")
        if c.get("hidden"):
            self.warn(f"{c['hidden']} stroke(s) on hidden layers were skipped")
        if c.get("erasers"):
            self.warn(f"{c['erasers']} eraser stroke(s) were skipped")
        if c.get("unusable"):
            self.warn(f"{c['unusable']} stroke(s) with unusable coordinates were skipped")
        if c.get("unknown-tools"):
            self.warn(f"{c['unknown-tools']} stroke(s) of an unknown tool were drawn as pen strokes")
        if c.get("anchored"):
            self.warn("Strokes anchored to typed text are placed at an approximated position")
        if c.get("lost-anchor"):
            self.warn(f"{c['lost-anchor']} stroke group(s) refer to text that is not in the page; placed unshifted")
        if c.get("texts"):
            self.warn(f"Typed text on {c['texts']} page(s) was placed in one text box per page; its layout is "
                      f"approximated")
        if c.get("glyphs"):
            self.warn(f"{c['glyphs']} text highlight(s) were converted to highlighter strokes")
        if self.unknown_templates:
            shown = ", ".join(repr(t) for t in self.unknown_templates[:5])
            self.warn(f"Templates other than blank, lined, grid and dotted paper became plain paper ({shown})")
        if c.get("budget") or self.budget.exhausted:
            self.warn("The document holds more ink points than the converter reads; the remaining strokes were "
                      "skipped")


# --------------------------------------------------------------------------- containers


def _page_list(content: Dict[str, Any], pagedata: List[str]) -> Optional[List[_PageInput]]:
    """Pages of a ``.content`` in display order; ``None`` when it lists none."""
    cpages = content.get("cPages")
    if isinstance(cpages, dict) and isinstance(cpages.get("pages"), list):
        entries = []
        for position, entry in enumerate(cpages["pages"][:MAX_PAGES * 2]):
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            deleted = entry.get("deleted")
            if deleted and not (isinstance(deleted, dict) and not deleted.get("value")):
                continue
            def lww(key: str) -> Any:
                value = entry.get(key)
                return value.get("value") if isinstance(value, dict) else None
            idx = lww("idx")
            template = lww("template")
            redir = lww("redir")
            entries.append((idx if isinstance(idx, str) else "", position,
                            _PageInput(entry["id"], template if isinstance(template, str) else "",
                                       redir if isinstance(redir, int) and not isinstance(redir, bool) else None)))
        entries.sort(key=lambda e: (e[0], e[1]))
        return [e[2] for e in entries]
    pages = content.get("pages")
    if isinstance(pages, list):
        redirect = content.get("redirectionPageMap")
        out = []
        for i, page_id in enumerate(pages[:MAX_PAGES * 2]):
            if not isinstance(page_id, str):
                continue
            redir = redirect[i] if isinstance(redirect, list) and i < len(redirect) else None
            out.append(_PageInput(page_id, pagedata[i] if i < len(pagedata) else "",
                                  redir if isinstance(redir, int) and not isinstance(redir, bool) else None))
        return out
    return None


def _read_rmdoc(data: bytes, reader: _Reader) -> Document:
    bundle = ZipBundle(data, "reMarkable")
    names = bundle.names
    contents = [n for n in names if n.endswith(".content") and "/" not in n]
    pages_rm = [n for n in names if n.lower().endswith(".rm")]
    if not contents and not pages_rm:
        raise ValueError("not a reMarkable document: no .content file and no .rm pages")
    doc = reader.doc
    doc_id = contents[0][: -len(".content")] if contents else pages_rm[0].rsplit("/", 1)[0]
    content = load_json(bundle.read(doc_id + ".content"))
    content = content if isinstance(content, dict) else {}
    metadata = load_json(bundle.read(doc_id + ".metadata"))
    if isinstance(metadata, dict) and isinstance(metadata.get("visibleName"), str) and metadata["visibleName"].strip():
        doc.title = metadata["visibleName"].strip()
    raw_pagedata = bundle.read(doc_id + ".pagedata")
    pagedata = raw_pagedata.decode("utf-8", "replace").splitlines() if raw_pagedata else []
    pages = _page_list(content, pagedata)
    if pages is None:
        listed = [n for n in pages_rm if n.startswith(doc_id + "/")] or pages_rm
        pages = [_PageInput(n.rsplit("/", 1)[-1][:-3]) for n in listed]
        if pages:
            reader.warn("The document lists no pages; its page files were read in archive order")
    if len(pages) > MAX_PAGES:
        reader.warn(f"The document lists {len(pages)} pages; only the first {MAX_PAGES} are read")
        pages = pages[:MAX_PAGES]
    file_type = content.get("fileType") if isinstance(content.get("fileType"), str) else "notebook"
    landscape = content.get("orientation") == "landscape"
    pdf_id: Optional[str] = None
    pdf_sizes: List[Tuple[float, float]] = []
    if file_type in ("pdf", "epub"):
        pdf = bundle.read(doc_id + ".pdf")
        if pdf is not None and pdf.startswith(b"%PDF-"):
            try:
                from .. import pdfutil  # lazily: the PDF parser is large

                pdf_sizes = [(p.width, p.height) for p in pdfutil.pdf_info(pdf).pages]
            except Exception:  # noqa: BLE001 - an unreadable PDF must not stop the reader
                pdf_sizes = []
            if pdf_sizes:
                pdf_id = doc_id
                doc.pdfs[pdf_id] = pdf
            else:
                reader.warn("The document's PDF could not be read; its pages are plain paper")
        elif file_type == "epub":
            reader.warn("The EPUB this document annotates is not converted (no PDF rendition in the file); "
                        "its pages are plain paper")
        else:
            reader.warn("The PDF this document annotates is missing from the file; its pages are plain paper")
    for number, page in enumerate(pages, 1):
        raw = bundle.read(f"{doc_id}/{page.page_id}.rm")
        scene: Optional[Scene] = None
        if raw is not None:
            try:
                scene = parse_scene(raw, reader.budget)
            except SceneError as exc:
                reader.warn(f"Page {number}: {exc}; its ink was skipped")
        canvas = _canvas(scene, landscape)
        pdf_page = None
        if pdf_id is not None and page.redir is not None and 0 <= page.redir < len(pdf_sizes):
            w_pt, h_pt = pdf_sizes[page.redir]
            pdf_page = (pdf_id, page.redir, w_pt, h_pt)
        elif pdf_id is not None and page.redir is not None:
            reader.warn(f"Page {number}: PDF page {page.redir + 1} does not exist; the page is plain paper")
        doc.pages.append(reader.page(number, scene, canvas, pdf_page, page.template))
    for message in (bundle.size_warning("reMarkable"), bundle.failure_warning("reMarkable")):
        if message:
            reader.warn(message)
    reader.report()
    return doc


def _canvas(scene: Optional[Scene], landscape: bool) -> Tuple[float, float]:
    """The notebook canvas in display orientation (the scene info's paper size, else the
    reMarkable 2's 1404 x 1872)."""
    w, h = RM2_CANVAS
    if scene is not None and scene.paper_size is not None:
        pw, ph = scene.paper_size
        if 100 <= pw <= 100_000 and 100 <= ph <= 100_000:
            w, h = pw, ph
    if landscape and h > w or not landscape and w > h:
        w, h = h, w
    return float(w), float(h)


def read_remarkable(data: bytes) -> Document:
    """Parse a reMarkable ``.rmdoc`` document or a single v6 ``.rm`` page given as bytes.

    Raises :class:`ValueError` when the data is neither (or is a page of another format
    version).  Everything else is tolerated with a line on ``Document.warnings``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_remarkable expects bytes")
    data = bytes(data)
    reader = _Reader()
    if data.startswith(b"reMarkable"):
        try:
            scene = parse_scene(data, reader.budget)
        except SceneError as exc:
            raise ValueError(f"not a readable reMarkable page: {exc}") from exc
        reader.doc.pages.append(reader.page(1, scene, _canvas(scene, False), None, ""))
        reader.report()
        return reader.doc
    return _read_rmdoc(data, reader)

