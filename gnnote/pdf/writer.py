"""Model -> PDF (``write_pdf``): pages with their backgrounds, images, text and ink.

Output layout (PDF 1.7, see ``docs/pdf.md``)::

    1 0 obj  << /Type /Catalog /Pages 2 0 R >>
    2 0 obj  << /Type /Pages /Kids [...] /Count n >>
    page     << /Type /Page /Parent 2 0 R /MediaBox [0 0 w h] /Resources << /XObject /ExtGState /Font >>
                /Contents c [/Annots [...]] >>
    ...      one Flate content stream per page, form / image XObjects, ExtGStates, fonts
    info     << /Title (...) /Producer (gnnote x.y.z) >>
    xref / trailer << /Size /Root /Info /ID >> / startxref / %%EOF

Coordinates: the model is top-left / y-down in pt, PDF user space bottom-left / y-up, so
every point is written as ``(x, h - y)``; matrices are built for images, forms and text.

Content of a page, bottom to top:

1. Background.  A page whose ``background`` names a PDF in ``Document.pdfs`` (user PDFs and
   stock paper alike) gets that PDF's page imported as a Form XObject: its decoded content
   streams joined into one stream, its inherited ``/Resources`` (and ``/Group``) copied with
   every referenced object renumbered, ``/BBox`` = its MediaBox, plus the normal appearance
   of every visible annotation on it (``/AP /N`` mapped onto ``/Rect`` as PDF 32000-1
   section 12.5.5 prescribes) so the page looks the way PDF apps show it.  It is drawn under
   ``cm`` = the source page's user space -> displayed space (MediaBox origin moved to 0, then
   ``/Rotate`` applied clockwise) scaled to the model page.  A page without one but with
   ``paper`` lined / grid / dotted gets ``pdfutil.make_paper_pdf`` imported the same way.
   Encrypted or unreadable PDFs leave the page blank with a warning.
2. Images.  JPEG passthrough and PNG as described in :mod:`gnnote.pdf.images`; ``fmt ==
   "pdf"`` stickers import their page 1 as a Form XObject.  The image is drawn into its box
   rotated by ``Image.rotation`` clockwise about the box centre.  For a JPEG whose EXIF
   orientation prescribes that same quarter turn (GoodNotes' convention, ``design.md`` 4.1)
   the box is the *displayed* box, so the raw pixels go into the box with width and height
   swapped and the turn makes them fill it upright; every other image fills the box and the
   box turns.
3. Text boxes (:mod:`gnnote.pdf.text`).
4. Ink in ``Page.strokes`` order.  ``kind == "fill"`` -> the ``outline`` rings filled
   (non-zero rule) with the colour's alpha in an ExtGState ``/ca``.  Other strokes depend on
   ``Options.pdf_ink``:

   * ``"flatten"`` (default): drawn into the page.  A stroke whose widths agree within 2 %
     is one path (``m`` + ``l`` or, for Bezier strokes, ``c``) stroked once with round caps
     and joins.  A variable-width stroke is cut into pieces whose width changes by at most
     5 % (cubic pieces split by de Casteljau), consecutive pieces of (almost) equal width
     become one subpath stroked with that width, and the round caps hide the joints.
     Highlighters use ``/CA`` = their alpha (0.5 when opaque) with ``/BM /Multiply``; a
     translucent variable-width stroke is drawn opaque inside a transparency group Form
     XObject that is painted with the alpha, so overlapping pieces do not darken.
   * ``"annotations"``: every stroke becomes ``<< /Type /Annot /Subtype /Ink /Rect /InkList
     [[x y ...]] /BS << /W median /S /S >> /C [r g b] /CA alpha /F 4 /P page /NM (...) /AP
     << /N form >> >>`` whose appearance stream draws exactly what flatten mode draws (the
     alpha and blend mode included, because viewers such as MuPDF paint ``/AP`` as it is);
     ``/InkList`` holds the flattened centre line in default user space.  Fills stay in the
     content.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from typing import Any, Dict, List, Optional, Tuple

from .. import __version__
from ..geometry import flatten_bezier
from ..model import Document, Image, Page, Point, Stroke
from ..pdfutil import make_paper_pdf
from .images import EXIF_ROTATION, ImageError, PdfImage, image_kind, jpeg_image, jpeg_info, png_image
from .objects import (Copier, Name, PageObj, PdfError, PdfFile, PdfWriter, Raw, Ref, Stream, fmt_num,
                      make_stream, mat_apply, mat_mul, text_string)
from .text import choose_font, text_box_ops

__all__ = ["write_pdf", "INK_MODES"]

INK_MODES = ("flatten", "annotations")
DEFAULT_PAGE = (612.0, 792.0)
MIN_WIDTH = 0.1  # pt; thinner strokes are drawn at this width
HIGHLIGHTER_ALPHA = 0.5
WIDTH_TOLERANCE = 0.02  # a stroke whose widths agree within 2 % is drawn as one path
PIECE_STEP = 0.05  # variable width: pieces change width by at most 5 %
INK_FLATTEN = 1.0  # pt between /InkList samples of Bezier strokes
MAX_ANNOTS = 100_000

_Pt = Tuple[float, float]


def _n(x: float) -> str:
    """A coordinate with at most three decimals (callers pass finite numbers)."""
    s = "%.3f" % x
    s = s.rstrip("0")
    if s[-1] == ".":
        s = s[:-1]
    return "0" if s == "-0" else s


def _c(x: float) -> str:
    return fmt_num(min(1.0, max(0.0, float(x))), 4)


LIMIT = 1e6  # pt; coordinates and sizes beyond this are treated as damage


def _finite(*values: float) -> bool:
    """Every value is a number of sensible magnitude (finite, |v| <= LIMIT)."""
    try:
        return all(math.isfinite(float(v)) and abs(float(v)) <= LIMIT for v in values)
    except (TypeError, ValueError, OverflowError):
        return False


def _ink_mode(options: Any) -> str:
    mode = str(getattr(options, "pdf_ink", "flatten") or "flatten").strip().lower()
    if mode not in INK_MODES:
        raise ValueError(f"pdf_ink must be one of {', '.join(INK_MODES)}; got {mode!r}")
    return mode


class _Resources:
    def __init__(self) -> None:
        self.xobject: Dict[str, Ref] = {}
        self.ext: Dict[str, Ref] = {}
        self.font: Dict[str, Ref] = {}

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        if self.xobject:
            d["XObject"] = dict(self.xobject)
        if self.ext:
            d["ExtGState"] = dict(self.ext)
        if self.font:
            d["Font"] = dict(self.font)
        return d


# ----------------------------------------------------------------------------------
# stroke geometry
# ----------------------------------------------------------------------------------


def _lerp(a: _Pt, b: _Pt, t: float) -> _Pt:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _split_cubic(p0: _Pt, c1: _Pt, c2: _Pt, p1: _Pt, t: float):
    a, b, c = _lerp(p0, c1, t), _lerp(c1, c2, t), _lerp(c2, p1, t)
    d, e = _lerp(a, b, t), _lerp(b, c, t)
    f = _lerp(d, e, t)
    return (p0, a, d, f), (f, e, c, p1)


class _Geometry:
    """A stroke as drawable pieces in PDF coordinates (y already flipped)."""

    def __init__(self, stroke: Stroke, page_h: float):
        pts = []
        for p in stroke.points:
            try:
                if abs(p.x) <= LIMIT and abs(p.y) <= LIMIT:  # False for NaN too
                    pts.append(p)
            except TypeError:
                continue
        self.ok = bool(pts)
        default_w = float(stroke.width) if _finite(stroke.width) and stroke.width > 0 else 1.0
        self.widths = [max(MIN_WIDTH, float(p.width) if _finite(p.width) and p.width > 0 else default_w) for p in pts]
        self.anchors: List[_Pt] = [(float(p.x), page_h - float(p.y)) for p in pts]
        self.controls: Optional[List[Tuple[_Pt, _Pt]]] = None
        ctrl = stroke.controls
        if ctrl is not None and len(ctrl) == len(stroke.points) - 1 and len(pts) == len(stroke.points) \
                and all(_finite(a.x, a.y, b.x, b.y) for a, b in ctrl):
            self.controls = [((float(a.x), page_h - float(a.y)), (float(b.x), page_h - float(b.y))) for a, b in ctrl]
        self.page_h = page_h
        self.stroke = stroke

    def median_width(self) -> float:
        return statistics.median(self.widths) if self.widths else 1.0

    def constant(self) -> bool:
        lo, hi = min(self.widths), max(self.widths)
        return hi - lo <= max(0.01, WIDTH_TOLERANCE * hi)

    def bbox(self) -> Tuple[float, float, float, float]:
        xs = [p[0] for p in self.anchors]
        ys = [p[1] for p in self.anchors]
        if self.controls:
            for a, b in self.controls:
                xs += [a[0], b[0]]
                ys += [a[1], b[1]]
        m = max(self.widths) / 2.0 + 1.0
        return min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m

    def pieces(self) -> List[Tuple[str, Tuple[_Pt, ...], float, float]]:
        """``("l", (p0, p1), w0, w1)`` or ``("c", (p0, c1, c2, p1), w0, w1)``."""
        a, w = self.anchors, self.widths
        out: List[Tuple[str, Tuple[_Pt, ...], float, float]] = []
        for i in range(len(a) - 1):
            if self.controls is not None:
                c1, c2 = self.controls[i]
                out.append(("c", (a[i], c1, c2, a[i + 1]), w[i], w[i + 1]))
            else:
                out.append(("l", (a[i], a[i + 1]), w[i], w[i + 1]))
        return out

    def runs(self) -> List[Tuple[float, List[str]]]:
        """Subpaths with their stroke width: ``[(width, [path operators]), ...]``."""
        a = self.anchors
        if len(a) == 1:  # a dot: a zero-length subpath, painted as a disc with round caps (8.5.3.2)
            x, y = a[0]
            return [(self.widths[0], [f"{_n(x)} {_n(y)} m", f"{_n(x)} {_n(y)} l"])]
        if self.constant():
            ops = [f"{_n(a[0][0])} {_n(a[0][1])} m"]
            for kind, pts, _w0, _w1 in self.pieces():
                ops.append(_piece_op(kind, pts))
            return [(self.median_width(), ops)]
        runs: List[Tuple[float, List[str]]] = []
        cur_w: Optional[float] = None
        for kind, pts, w0, w1 in self.pieces():
            for sub_kind, sub_pts, width in _subdivide(kind, pts, w0, w1):
                if cur_w is None or abs(width - cur_w) > PIECE_STEP * 0.5 * max(cur_w, width):
                    runs.append((width, [f"{_n(sub_pts[0][0])} {_n(sub_pts[0][1])} m"]))
                    cur_w = width
                runs[-1][1].append(_piece_op(sub_kind, sub_pts))
        return runs

    def polyline(self) -> List[_Pt]:
        """The centre line as points (Bezier strokes flattened every ~1 pt)."""
        if self.controls is None:
            return list(self.anchors)
        anchors = [Point(x, y, 1.0) for x, y in self.anchors]
        controls = [(Point(c1[0], c1[1], 1.0), Point(c2[0], c2[1], 1.0)) for c1, c2 in self.controls]
        return [(p.x, p.y) for p in flatten_bezier(anchors, controls, INK_FLATTEN)]


def _piece_op(kind: str, pts: Tuple[_Pt, ...]) -> str:
    if kind == "c":
        return f"{_n(pts[1][0])} {_n(pts[1][1])} {_n(pts[2][0])} {_n(pts[2][1])} {_n(pts[3][0])} {_n(pts[3][1])} c"
    return f"{_n(pts[1][0])} {_n(pts[1][1])} l"


def _subdivide(kind: str, pts: Tuple[_Pt, ...], w0: float, w1: float):
    """Split a piece so that each part's width differs from its neighbours by <= 5 %."""
    diff = abs(w1 - w0)
    step = max(0.02, PIECE_STEP * min(w0, w1))
    k = max(1, min(32, int(math.ceil(diff / step)))) if diff > 0 else 1
    if k == 1:
        yield kind, pts, (w0 + w1) / 2.0
        return
    if kind == "l":
        p0, p1 = pts
        for i in range(k):
            yield "l", (_lerp(p0, p1, i / k), _lerp(p0, p1, (i + 1) / k)), w0 + (w1 - w0) * (i + 0.5) / k
        return
    rest = pts
    for i in range(k):
        t = 1.0 / (k - i)
        if i == k - 1:
            left = rest
        else:
            left, rest = _split_cubic(rest[0], rest[1], rest[2], rest[3], t)
        yield "c", left, w0 + (w1 - w0) * (i + 0.5) / k


# ----------------------------------------------------------------------------------
# imported PDF pages
# ----------------------------------------------------------------------------------


class _Source:
    """One PDF of the document, parsed once; its pages become Form XObjects on demand."""

    def __init__(self, writer: "_Writer", data: bytes, label: str):
        self.writer = writer
        self.label = label
        self.pdf = PdfFile(data)
        self.pages = self.pdf.pages()
        if not self.pages:
            raise PdfError("no page found")
        self.copier = Copier(self.pdf, writer.w)
        self.forms: Dict[int, Optional[Tuple[Ref, PageObj]]] = {}

    def form(self, index: int) -> Optional[Tuple[Ref, PageObj]]:
        if index in self.forms:
            return self.forms[index]
        result: Optional[Tuple[Ref, PageObj]] = None
        if 0 <= index < len(self.pages):
            page = self.pages[index]
            try:
                result = (self._make_form(page), page)
            except (PdfError, RecursionError, ValueError, TypeError) as exc:
                self.writer.doc.warn(f"{self.label}: page {index + 1} could not be imported ({exc}); left blank")
        self.forms[index] = result
        return result

    def _make_form(self, page: PageObj) -> Ref:
        pdf, copier, w = self.pdf, self.copier, self.writer.w
        content = pdf.content_bytes(page)
        fd: Dict[str, Any] = {"Type": Name("XObject"), "Subtype": Name("Form"), "FormType": 1,
                              "BBox": list(page.box)}
        res = copier.convert(page.resources) if page.resources is not None else {}
        fd["Resources"] = res if isinstance(res, (dict, Ref)) else {}
        group = pdf.resolve(page.dict.get("Group"))
        if isinstance(group, dict):
            fd["Group"] = copier.convert(group)
        form = w.add(make_stream(fd, content))
        appearances = self._appearances(page)
        if appearances:
            xobjects: Dict[str, Any] = {"P": form}
            ops = ["q /P Do Q"]
            for k, (ap_ref, matrix) in enumerate(appearances):
                xobjects[f"A{k}"] = ap_ref
                ops.append(f"q {' '.join(_n6(v) for v in matrix)} cm /A{k} Do Q")
            form = w.add(make_stream({"Type": Name("XObject"), "Subtype": Name("Form"), "FormType": 1,
                                      "BBox": list(page.box), "Resources": {"XObject": xobjects}},
                                     "\n".join(ops).encode("ascii")))
        copier.flush()
        return form

    def _appearances(self, page: PageObj) -> List[Tuple[Ref, Tuple[float, ...]]]:
        """Visible annotations' normal appearances as ``(copied form, matrix)``.

        An annotation's ``/CA`` is not applied: with an appearance stream "the appearance
        stream shall specify any transparency" (PDF 32000-1, table 170), as viewers do.
        """
        pdf = self.pdf
        annots = pdf.resolve(page.dict.get("Annots"))
        if not isinstance(annots, list):
            return []
        out: List[Tuple[Ref, Tuple[float, ...]]] = []
        for item in annots[:MAX_ANNOTS]:
            ad = pdf.resolve(item)
            if not isinstance(ad, dict) or pdf.resolve(ad.get("Subtype")) == "Popup":
                continue
            flags = int(pdf.number(ad.get("F")) or 0)
            if flags & 2 or flags & 32:  # Hidden, NoView
                continue
            ap = pdf.dict_of(ad.get("AP"))
            normal = ap.get("N")
            target = pdf.resolve(normal)
            if isinstance(target, dict):
                state = pdf.resolve(ad.get("AS"))
                normal = target.get(str(state)) if isinstance(state, str) else None
                target = pdf.resolve(normal)
            if not isinstance(normal, Ref) or not isinstance(target, Stream):
                continue
            rect = pdf._box(ad.get("Rect"))
            bbox = pdf.numbers(target.dict.get("BBox"))
            if rect is None or len(bbox) < 4:
                continue
            m = pdf.numbers(target.dict.get("Matrix"))
            matrix = tuple(m[:6]) if len(m) >= 6 else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            corners = [mat_apply(matrix, x, y) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
            tx0, ty0 = min(c[0] for c in corners), min(c[1] for c in corners)
            tx1, ty1 = max(c[0] for c in corners), max(c[1] for c in corners)
            if tx1 - tx0 <= 1e-9 or ty1 - ty0 <= 1e-9:
                continue
            sx = (rect[2] - rect[0]) / (tx1 - tx0)
            sy = (rect[3] - rect[1]) / (ty1 - ty0)
            fit = (sx, 0.0, 0.0, sy, rect[0] - tx0 * sx, rect[1] - ty0 * sy)
            out.append((self.copier.ref_for(normal.num), fit))
        return out


def _n6(x: float) -> str:
    return fmt_num(x, 6)


# ----------------------------------------------------------------------------------
# the writer
# ----------------------------------------------------------------------------------


class _Writer:
    def __init__(self, doc: Document, options: Any):
        self.doc = doc
        self.mode = _ink_mode(options)
        self.w = PdfWriter()
        self.catalog = self.w.alloc()
        self.pages_ref = self.w.alloc()
        self.ext: Dict[Tuple[float, float, str], Tuple[str, Ref]] = {}
        self.images: Dict[Tuple[int, bytes], Optional[Ref]] = {}
        self.sources: Dict[Any, Optional[_Source]] = {}
        self.papers: Dict[Tuple[float, float, str], bytes] = {}
        self.form_count = 0
        self.counts: Dict[str, int] = {}
        texts = [t.text or "" for p in doc.pages for t in p.texts] + \
                [r.text or "" for p in doc.pages for t in p.texts for r in (t.runs or [])]
        self.font, font_warning = choose_font(texts)
        if font_warning:
            doc.warn(font_warning)
        self.font_ref: Optional[Ref] = self.w.alloc() if any(texts) else None

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    # -- shared resources ----------------------------------------------------------------

    def gs(self, res: _Resources, stroke_alpha: float, fill_alpha: float, blend: Optional[str] = None) -> str:
        key = (round(stroke_alpha, 4), round(fill_alpha, 4), blend or "")
        entry = self.ext.get(key)
        if entry is None:
            d: Dict[str, Any] = {"Type": Name("ExtGState"), "CA": key[0], "ca": key[1]}
            if blend:
                d["BM"] = Name(blend)
            entry = (f"Gs{len(self.ext) + 1}", self.w.add(d))
            self.ext[key] = entry
        res.ext[entry[0]] = entry[1]
        return entry[0]

    def _source(self, data: bytes, label: str) -> Optional[_Source]:
        """The parsed PDF ``data`` (identical bytes are parsed and copied once, whichever
        background or PDF image they belong to); ``None`` with a warning when unusable."""
        key = (len(data), hashlib.sha1(data).digest())
        if key in self.sources:
            return self.sources[key]
        src: Optional[_Source] = None
        try:
            src = _Source(self, data, label)
            if src.pdf.encrypted:
                self.doc.warn(f"{label} is encrypted; it was left out (blank background)")
                src = None
        except (PdfError, ValueError, TypeError, RecursionError) as exc:
            self.doc.warn(f"{label} could not be read ({exc}); it was left out (blank background)")
            src = None
        self.sources[key] = src
        return src

    # -- pages ----------------------------------------------------------------------------

    def write(self) -> bytes:
        doc = self.doc
        pages = list(doc.pages)
        if not pages:
            doc.warn("the document has no pages; one blank page was written")
            pages = [Page(*DEFAULT_PAGE)]
        page_refs = [self.page(page, index) for index, page in enumerate(pages)]
        if self.font_ref is not None:
            self.w.set(self.font_ref, self.font.font_object(self.w))
        if self.font.missing:
            doc.warn(f"{self.font.missing} characters are missing from the PDF font and were printed as '?'")
        self.w.set(self.pages_ref, {"Type": Name("Pages"), "Kids": page_refs, "Count": len(page_refs)})
        self.w.set(self.catalog, {"Type": Name("Catalog"), "Pages": self.pages_ref})
        title = (doc.title or "").strip() or "Untitled"
        info = self.w.add({"Title": text_string(title), "Producer": text_string(f"gnnote {__version__}")})
        self._report()
        return self.w.to_bytes(self.catalog, info)

    def _report(self) -> None:
        messages = {
            "image_unsupported": "{n} images are neither PNG, JPEG nor PDF and were left out",
            "image_failed": "{n} images could not be embedded and were left out",
            "image_empty": "{n} images without data were left out",
            "bad_geometry": "{n} strokes, images or text boxes with invalid coordinates were left out",
            "sticker_failed": "{n} PDF images could not be imported and were left out",
        }
        for key, text in messages.items():
            if self.counts.get(key):
                self.doc.warn(text.format(n=self.counts[key]))

    def page(self, page: Page, index: int) -> Ref:
        W = float(page.width) if _finite(page.width) and page.width > 0 else DEFAULT_PAGE[0]
        H = float(page.height) if _finite(page.height) and page.height > 0 else DEFAULT_PAGE[1]
        if (W, H) != (page.width, page.height):
            self.doc.warn(f"Page {index + 1} has no valid size; US Letter was used")
        ref = self.w.alloc()
        res = _Resources()
        ops: List[str] = []
        ops += self._background(page, index, W, H, res)
        for image in page.images:
            ops += self._image(image, H, res, index)
        for box in page.texts:
            if not _finite(box.x, box.y):
                self.count("bad_geometry")
                continue
            if self.font_ref is not None:
                box_ops = text_box_ops(box, self.font, "F1", H, lambda a, b, bm: self.gs(res, a, b, bm))
                if box_ops:
                    res.font["F1"] = self.font_ref
                    ops += box_ops
        annots: List[Ref] = []
        for k, stroke in enumerate(page.strokes):
            if stroke.kind == "fill":
                ops += self._fill(stroke, H, res)
                continue
            geo = _Geometry(stroke, H)
            if not geo.ok:
                self.count("bad_geometry")
                continue
            if self.mode == "annotations" and len(annots) < MAX_ANNOTS:
                annots.append(self._annotation(geo, ref, index, k))
            else:
                ops += self._stroke_ops(geo, res)
        content = self.w.add(make_stream({}, ("\n".join(ops) + "\n").encode("latin-1")))
        d: Dict[str, Any] = {"Type": Name("Page"), "Parent": self.pages_ref, "MediaBox": [0, 0, W, H],
                             "Resources": res.to_dict(), "Contents": content}
        if annots:
            d["Annots"] = annots
        self.w.set(ref, d)
        return ref

    # -- background ----------------------------------------------------------------------

    def _background(self, page: Page, index: int, W: float, H: float, res: _Resources) -> List[str]:
        bg = page.background
        form: Optional[Tuple[Ref, PageObj]] = None
        if bg is not None:
            data = self.doc.pdfs.get(bg.pdf_id)
            if not data:
                self.doc.warn(f"Page {index + 1}: its PDF background {bg.pdf_id!r} is missing; left blank")
            else:
                src = self._source(bytes(data), f"PDF {bg.pdf_id!r}")
                try:
                    page_index = int(bg.page_index)
                except (TypeError, ValueError):
                    page_index = -1
                if src is not None:
                    if not 0 <= page_index < len(src.pages):
                        self.doc.warn(f"Page {index + 1}: PDF page {page_index + 1} does not exist in "
                                      f"{bg.pdf_id!r}; left blank")
                    else:
                        form = src.form(page_index)
        elif page.paper in ("lined", "grid", "dotted"):
            key = (round(W, 3), round(H, 3), page.paper)
            paper = self.papers.get(key)
            if paper is None:
                try:
                    paper = self.papers[key] = make_paper_pdf(W, H, page.paper)
                except ValueError:
                    paper = self.papers[key] = b""
            src = self._source(paper, f"{page.paper} paper") if paper else None
            form = src.form(0) if src is not None else None
        if form is None:
            return []
        ref, src_page = form
        name = f"Bg{ref.num}"
        res.xobject[name] = ref
        sx = W / src_page.width if src_page.width > 0 else 1.0
        sy = H / src_page.height if src_page.height > 0 else 1.0
        m = mat_mul(src_page.matrix, (sx, 0.0, 0.0, sy, 0.0, 0.0))
        return ["q", " ".join(_n6(v) for v in m) + " cm", f"/{name} Do", "Q"]

    # -- images ---------------------------------------------------------------------------

    def _image_xobject(self, data: bytes, kind: str) -> Optional[Ref]:
        key = (len(data), hashlib.sha1(data).digest())
        if key in self.images:
            return self.images[key]
        ref: Optional[Ref] = None
        try:
            img: PdfImage = jpeg_image(data) if kind == "jpeg" else png_image(data)
            d = dict(img.dict)
            if img.smask is not None:
                sd, sdata = img.smask
                d["SMask"] = self.w.add(make_stream(sd, sdata, compress=False))
            ref = self.w.add(make_stream(d, img.data, compress=False))
        except (ImageError, ValueError) as exc:  # damaged / unsupported image data
            self.count("image_failed")
            if not self.counts.get("image_failed_logged"):
                self.count("image_failed_logged")
                self.doc.warn(f"an image could not be embedded: {exc}")
        self.images[key] = ref
        return ref

    def _image(self, image: Image, page_h: float, res: _Resources, index: int) -> List[str]:
        if not _finite(image.x, image.y, image.w, image.h) or image.w <= 0 or image.h <= 0:
            self.count("bad_geometry")
            return []
        data = bytes(image.data) if isinstance(image.data, (bytes, bytearray, memoryview)) else b""
        if not data:
            self.count("image_empty")
            return []
        kind = image_kind(data)
        if kind is None:
            self.count("image_unsupported")
            return []
        rotation = float(image.rotation or 0.0) % 360.0 if _finite(image.rotation or 0.0) else 0.0
        w, h = float(image.w), float(image.h)
        swap = False
        if kind == "jpeg" and (abs(rotation - 90.0) < 1e-6 or abs(rotation - 270.0) < 1e-6):
            try:
                orientation = jpeg_info(data)[5]
            except ImageError:
                orientation = 1
            swap = orientation in (5, 6, 7, 8) and abs(EXIF_ROTATION[orientation] - rotation) < 1e-6
        fw, fh = (h, w) if swap else (w, h)
        theta = math.radians(rotation)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        cx, cy = float(image.x) + w / 2.0, page_h - (float(image.y) + h / 2.0)
        frame = mat_mul(mat_mul((fw, 0.0, 0.0, fh, -fw / 2.0, -fh / 2.0), (cos_t, -sin_t, sin_t, cos_t, 0.0, 0.0)),
                        (1.0, 0.0, 0.0, 1.0, cx, cy))
        if kind == "pdf":
            src = self._source(data, "a PDF image")
            form = src.form(0) if src is not None else None
            if form is None:
                self.count("sticker_failed")
                return []
            ref, src_page = form
            unit = mat_mul(src_page.matrix, (1.0 / src_page.width, 0.0, 0.0, 1.0 / src_page.height, 0.0, 0.0))
            m = mat_mul(unit, frame)
            name = f"Fx{ref.num}"
        else:
            ref = self._image_xobject(data, kind)
            if ref is None:
                return []
            m = frame
            name = f"Im{ref.num}"
        res.xobject[name] = ref
        return ["q", " ".join(_n6(v) for v in m) + " cm", f"/{name} Do", "Q"]

    # -- ink --------------------------------------------------------------------------------

    @staticmethod
    def _colour(stroke: Stroke) -> Tuple[float, float, float, float]:
        try:
            vals = [float(v) if _finite(v) else 0.0 for v in list(stroke.color or ())[:4]]
        except TypeError:
            vals = []
        vals += [0.0] * (3 - len(vals)) + [1.0] * (4 - max(3, len(vals)))
        return vals[0], vals[1], vals[2], min(1.0, max(0.0, vals[3]))

    def _alpha_blend(self, stroke: Stroke) -> Tuple[float, Optional[str]]:
        alpha = self._colour(stroke)[3]
        if stroke.kind == "highlighter":
            return (alpha if alpha < 0.999 else HIGHLIGHTER_ALPHA), "Multiply"
        return alpha, None

    def _stroke_ops(self, geo: _Geometry, res: _Resources) -> List[str]:
        r, g, b, _a = self._colour(geo.stroke)
        alpha, blend = self._alpha_blend(geo.stroke)
        runs = geo.runs()
        colour = f"{_c(r)} {_c(g)} {_c(b)} RG 1 J 1 j"
        translucent = alpha < 0.999 or blend is not None
        if len(runs) == 1:
            width, path = runs[0]
            ops = ["q"]
            if translucent:
                ops.append(f"/{self.gs(res, alpha, alpha, blend)} gs")
            ops += [colour, f"{_n(width)} w"] + path + ["S", "Q"]
            return ops
        body: List[str] = [colour]
        for width, path in runs:
            body += [f"{_n(width)} w"] + path + ["S"]
        if not translucent:
            return ["q"] + body + ["Q"]
        x0, y0, x1, y1 = geo.bbox()
        group = self.w.add(make_stream({"Type": Name("XObject"), "Subtype": Name("Form"), "FormType": 1,
                                        "BBox": [x0, y0, x1, y1], "Resources": {},
                                        "Group": {"Type": Name("Group"), "S": Name("Transparency")}},
                                       ("\n".join(body) + "\n").encode("ascii")))
        self.form_count += 1
        name = f"Fm{group.num}"
        res.xobject[name] = group
        return ["q", f"/{self.gs(res, alpha, alpha, blend)} gs", f"/{name} Do", "Q"]

    def _fill(self, stroke: Stroke, page_h: float, res: _Resources) -> List[str]:
        rings = stroke.outline or ([stroke.points] if stroke.points else [])
        path: List[str] = []
        for ring in rings:
            pts = [(float(p.x), page_h - float(p.y)) for p in ring if _finite(p.x, p.y)]
            if len(pts) < 3:
                continue
            path.append(f"{_n(pts[0][0])} {_n(pts[0][1])} m")
            path += [f"{_n(x)} {_n(y)} l" for x, y in pts[1:]]
            path.append("h")
        if not path:
            self.count("bad_geometry")
            return []
        r, g, b, a = self._colour(stroke)
        ops = ["q"]
        if a < 0.999:
            ops.append(f"/{self.gs(res, a, a)} gs")
        ops += [f"{_c(r)} {_c(g)} {_c(b)} rg"] + path + ["f", "Q"]
        return ops

    def _annotation(self, geo: _Geometry, page_ref: Ref, page_index: int, k: int) -> Ref:
        stroke = geo.stroke
        r, g, b, _a = self._colour(stroke)
        alpha, _blend = self._alpha_blend(stroke)
        ap_res = _Resources()
        ops = self._stroke_ops(geo, ap_res)
        x0, y0, x1, y1 = geo.bbox()
        rect = [x0, y0, x1, y1]
        ap = self.w.add(make_stream({"Type": Name("XObject"), "Subtype": Name("Form"), "FormType": 1,
                                     "BBox": rect, "Resources": ap_res.to_dict()},
                                    ("\n".join(ops) + "\n").encode("ascii")))
        ink = Raw(("[" + " ".join(f"{_n(x)} {_n(y)}" for x, y in geo.polyline()) + "]").encode("ascii"))
        annot: Dict[str, Any] = {
            "Type": Name("Annot"), "Subtype": Name("Ink"), "Rect": rect, "InkList": [ink],
            "BS": {"Type": Name("Border"), "W": round(geo.median_width(), 3), "S": Name("S")},
            "C": [round(r, 4), round(g, 4), round(b, 4)], "F": 4, "P": page_ref,
            "NM": f"gnnote-{page_index + 1}-{k + 1}".encode("ascii"), "AP": {"N": ap},
        }
        if alpha < 0.999:
            annot["CA"] = round(alpha, 4)
        return self.w.add(annot)


def write_pdf(doc: Document, options: Any = None) -> bytes:
    """Write ``doc`` as a PDF 1.7 file (see the module docstring).

    ``options`` is a :class:`gnnote.convert.Options` (only ``pdf_ink`` is used here:
    ``"flatten"`` draws ink into the pages, ``"annotations"`` writes ``/Ink`` annotations);
    ``None`` means the defaults.  Lossy steps are recorded on ``doc.warnings``.
    """
    if not isinstance(doc, Document):
        raise TypeError("write_pdf expects a gnnote.model.Document")
    return _Writer(doc, options).write()
