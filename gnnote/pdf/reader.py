"""PDF -> model (``read_pdf``): every page becomes a PDF-backed page, ink annotations become
editable strokes.

What is read (``docs/pdf.md`` has the details):

* Pages: one model page per PDF page in the order :func:`gnnote.pdfutil.pdf_info` reports,
  sized as displayed (MediaBox with ``/Rotate`` applied), ``background =
  PdfBackground(pdf_id, index)``, ``template_is_builtin = False``.  ``Document.pdfs[pdf_id]``
  holds the PDF minus the converted annotations; ``pdf_id`` is a UUID derived from the
  input bytes.  Title: ``/Info /Title``, else ``"PDF"``.
* Annotations (default user space -> the page's top-left model space: MediaBox origin to 0,
  ``/Rotate`` applied clockwise, y flipped).  Width ``/BS /W``, else ``/Border [h v w]``, else
  1; colour ``/C`` (1 = grey, 3 = RGB, 4 = CMYK, missing or empty -> black); alpha ``/CA``,
  else the ``/CA`` of an ExtGState in the normal appearance (GoodNotes' exports keep it
  there), else 1.  A stroke is a highlighter when its alpha is <= 0.6 or the appearance uses
  ``/BM /Multiply``.

  ======================  ==========================================================
  ``/Ink``                one stroke per ``/InkList`` path (polyline, repeated points
                          dropped)
  ``/Line``               ``/L`` as a two-anchor stroke (exact straight Bezier)
  ``/PolyLine``           ``/Vertices`` as a straight-sided stroke
  ``/Polygon``            the same, closed; ``/IC`` adds a fill (``kind = "fill"``)
  ``/Square``             ``/Rect`` minus ``/RD`` inset by half the border width,
                          closed; ``/IC`` adds a fill
  ``/Circle``             the ellipse inscribed in the same rectangle (64 samples)
  ``/FreeText``           a ``TextBox`` (``/Contents``, size and colour from ``/DA``
                          ``Tf`` / ``g`` / ``rg`` / ``k``, else ``/DS``; ``/Q``
                          alignment; rotated with the page)
  ``/Highlight``          one highlighter stroke per ``/QuadPoints`` quadrilateral:
                          its centre line, as wide as the quad is high, shortened by
                          half that width at both ends so the round caps end at the
                          quad (alpha ``/CA`` or 0.5; colour ``/C`` or yellow)
  ======================  ==========================================================

  Hidden (``/F`` bit 2) and no-view (bit 6) annotations are not converted.  Every converted
  annotation -- and every ``/Popup`` belonging to one -- is removed from the background by an
  incremental update that rewrites the affected page dictionaries with a filtered
  ``/Annots`` (or none); a file whose cross-reference data is damaged is rewritten instead
  (:func:`gnnote.pdf.objects.rewrite`).  When the result cannot be verified the annotations
  stay in the PDF and nothing is converted.  Other annotations (links, stamps, form fields,
  ...) stay part of the PDF pages.  Encrypted PDFs are carried unchanged with nothing
  converted.  Ink drawn into the page content itself stays part of the background.

Raises :class:`ValueError` only when the bytes are not a PDF (no ``%PDF`` header in the
first 1024 bytes) or no page can be found; anything else damaged becomes a warning.
"""
from __future__ import annotations

import hashlib
import math
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..model import Document, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from ..pdfutil import Keyword, _Lexer, _parse_value
from .objects import (PageObj, PdfError, PdfFile, Ref, Stream, decode_text, incremental_update, mat_apply,
                      mat_mul, rewrite)

__all__ = ["read_pdf", "CONVERTED_SUBTYPES"]

CONVERTED_SUBTYPES = ("Ink", "Line", "PolyLine", "Polygon", "Square", "Circle", "FreeText", "Highlight")
HIGHLIGHTER_MAX_ALPHA = 0.6
HIGHLIGHT_ALPHA = 0.5
ELLIPSE_SAMPLES = 64
MAX_ANNOTS_PER_PAGE = 100_000
MAX_POINTS = 5_000_000  # per document
MAX_AP_BYTES = 2_000_000  # appearance streams larger than this are not parsed for geometry
DEFAULT_TEXT_SIZE = 12.0
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_XY = Tuple[float, float]


def _uuid_for(data: bytes) -> str:
    return str(uuid.UUID(bytes=hashlib.sha1(data).digest()[:16])).upper()


def _straight(points: List[Point]) -> Optional[List[Tuple[Point, Point]]]:
    """Cubic handles at the thirds of every segment: an exact polyline as a Bezier chain."""
    if len(points) < 2:
        return None
    out = []
    for a, b in zip(points, points[1:]):
        c1 = Point(a.x + (b.x - a.x) / 3.0, a.y + (b.y - a.y) / 3.0, a.width)
        c2 = Point(a.x + 2.0 * (b.x - a.x) / 3.0, a.y + 2.0 * (b.y - a.y) / 3.0, b.width)
        out.append((c1, c2))
    return out


def _dedupe(points: List[Point], eps: float = 1e-4) -> List[Point]:
    out: List[Point] = []
    for p in points:
        if not out or abs(p.x - out[-1].x) > eps or abs(p.y - out[-1].y) > eps:
            out.append(p)
    return out


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.doc = Document(source_format="pdf")
        self.pdf = PdfFile(data)
        self.pages = self.pdf.pages()
        if not self.pages:
            raise ValueError("no page found in the PDF")
        self.points = 0
        self.kept: Dict[str, int] = {}
        self.warned: Set[str] = set()

    def warn_once(self, key: str, message: str) -> None:
        if key not in self.warned:
            self.warned.add(key)
            self.doc.warn(message)

    # -- small value helpers ------------------------------------------------------------

    def _num(self, value: Any, default: Optional[float] = None) -> Optional[float]:
        n = self.pdf.number(value)
        return default if n is None else n

    def _colour(self, value: Any, default: Tuple[float, float, float]) -> Tuple[float, float, float]:
        comps = [min(1.0, max(0.0, v)) for v in self.pdf.numbers(value)]
        if len(comps) == 1:
            return comps[0], comps[0], comps[0]
        if len(comps) == 3:
            return comps[0], comps[1], comps[2]
        if len(comps) >= 4:
            c, m, y, k = comps[:4]
            return (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
        return default

    def _width(self, ad: Dict[str, Any]) -> float:
        bs = self.pdf.dict_of(ad.get("BS"))
        w = self._num(bs.get("W")) if bs else None
        if w is None or w <= 0:
            border = self.pdf.numbers(ad.get("Border"))
            w = border[2] if len(border) >= 3 else None
        if w is None or w <= 0 or not math.isfinite(w):
            w = 1.0
        return min(w, 1000.0)

    def _appearance(self, ad: Dict[str, Any]) -> Optional[Stream]:
        ap = self.pdf.dict_of(ad.get("AP"))
        normal = self.pdf.resolve(ap.get("N"))
        if isinstance(normal, dict):
            state = self.pdf.resolve(ad.get("AS"))
            normal = self.pdf.resolve(normal.get(str(state))) if isinstance(state, str) else None
        return normal if isinstance(normal, Stream) else None

    def _ap_alpha(self, ad: Dict[str, Any]) -> Tuple[Optional[float], bool]:
        """(stroke alpha found in the appearance's ExtGStates, uses /BM /Multiply)."""
        stream = self._appearance(ad)
        if stream is None:
            return None, False
        alpha: Optional[float] = None
        multiply = False
        todo = [(self.pdf.dict_of(stream.dict.get("Resources")), 0)]
        seen: Set[int] = set()
        while todo:
            res, depth = todo.pop()
            ext = self.pdf.dict_of(res.get("ExtGState"))
            for value in list(ext.values())[:64]:
                gs = self.pdf.dict_of(value)
                for key in ("CA", "ca"):
                    a = self._num(gs.get(key))
                    if a is not None and 0.0 <= a < 1.0:
                        alpha = a if alpha is None else min(alpha, a)
                bm = self.pdf.resolve(gs.get("BM"))
                names = bm if isinstance(bm, list) else [bm]
                if any(self.pdf.resolve(n) == "Multiply" for n in names):
                    multiply = True
            if depth < 2:
                for value in list(self.pdf.dict_of(res.get("XObject")).values())[:64]:
                    if isinstance(value, Ref):
                        if value.num in seen:
                            continue
                        seen.add(value.num)
                    xo = self.pdf.resolve(value)
                    if isinstance(xo, Stream):
                        todo.append((self.pdf.dict_of(xo.dict.get("Resources")), depth + 1))
        return alpha, multiply

    def _style(self, ad: Dict[str, Any]) -> Tuple[Tuple[float, float, float, float], str]:
        r, g, b = self._colour(ad.get("C"), (0.0, 0.0, 0.0))
        ca = self._num(ad.get("CA"))
        ap_alpha, multiply = self._ap_alpha(ad)
        alpha = ca if ca is not None else (ap_alpha if ap_alpha is not None else 1.0)
        alpha = min(1.0, max(0.0, alpha))
        kind = "highlighter" if alpha <= HIGHLIGHTER_MAX_ALPHA or multiply else "pen"
        return (r, g, b, alpha), kind

    # -- appearance streams -------------------------------------------------------------

    def _ap_placement(self, ad: Dict[str, Any], stream: Stream) -> Optional[Tuple[float, ...]]:
        """Appearance space -> default user space (PDF 32000-1 section 12.5.5)."""
        rect = self.pdf._box(ad.get("Rect"))
        bbox = self.pdf.numbers(stream.dict.get("BBox"))
        if rect is None or len(bbox) < 4:
            return None
        m = self.pdf.numbers(stream.dict.get("Matrix"))
        matrix = tuple(m[:6]) if len(m) >= 6 else _IDENTITY
        corners = [mat_apply(matrix, x, y) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
        tx0, ty0 = min(c[0] for c in corners), min(c[1] for c in corners)
        tx1, ty1 = max(c[0] for c in corners), max(c[1] for c in corners)
        if tx1 - tx0 <= 1e-9 or ty1 - ty0 <= 1e-9:
            return None
        sx = (rect[2] - rect[0]) / (tx1 - tx0)
        sy = (rect[3] - rect[1]) / (ty1 - ty0)
        return mat_mul(matrix, (sx, 0.0, 0.0, sy, rect[0] - tx0 * sx, rect[1] - ty0 * sy))

    def _ap_ops(self, ad: Dict[str, Any]):
        stream = self._appearance(ad)
        if stream is None or len(stream.raw) > MAX_AP_BYTES:
            return None, None
        placement = self._ap_placement(ad, stream)
        if placement is None:
            return None, None
        try:
            data = self.pdf.stream_data(stream)
        except PdfError:
            return None, None
        if len(data) > MAX_AP_BYTES:
            return None, None
        return _content_ops(data), placement

    def _ap_path(self, ad: Dict[str, Any], ink: List[List[_XY]]):
        """The single stroked path the normal appearance draws, in default user space, as
        ``(anchors, handles or None)`` -- when it agrees with ``/InkList`` (else ``None``).

        GoodNotes writes only the Bezier anchors of its shapes into ``/InkList`` (an ellipse
        becomes four points) but the exact curves into the appearance; our own constant-width
        strokes come back with their exact Bezier handles the same way.
        """
        ops, placement = self._ap_ops(ad)
        if ops is None:
            return None
        ctm = _IDENTITY
        stack: List[Tuple[float, ...]] = []
        subpaths: List[List[Tuple[str, Tuple[_XY, ...]]]] = []
        painted: List[List[List[Tuple[str, Tuple[_XY, ...]]]]] = []

        def pt(args: List[Any], i: int) -> Optional[_XY]:
            x, y = args[i], args[i + 1]
            if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) \
                    or not isinstance(y, (int, float)):
                return None
            return mat_apply(mat_mul(ctm, placement), float(x), float(y))

        try:
            for op, args in ops:
                if op == "q":
                    stack.append(ctm)
                    if len(stack) > 64:
                        return None
                elif op == "Q":
                    ctm = stack.pop() if stack else _IDENTITY
                elif op == "cm":
                    if len(args) != 6 or not all(isinstance(a, (int, float)) for a in args):
                        return None
                    ctm = mat_mul(tuple(float(a) for a in args), ctm)
                elif op == "m":
                    p = pt(args, 0) if len(args) == 2 else None
                    if p is None:
                        return None
                    subpaths.append([("m", (p,))])
                elif op in ("l", "c", "v", "y"):
                    if not subpaths:
                        return None
                    need = {"l": 2, "c": 6, "v": 4, "y": 4}[op]
                    if len(args) != need:
                        return None
                    pts = [pt(args, i) for i in range(0, need, 2)]
                    if any(p is None for p in pts):
                        return None
                    current = subpaths[-1][-1][1][-1]
                    if op == "l":
                        subpaths[-1].append(("l", (pts[0],)))
                    elif op == "c":
                        subpaths[-1].append(("c", tuple(pts)))
                    elif op == "v":
                        subpaths[-1].append(("c", (current, pts[0], pts[1])))
                    else:
                        subpaths[-1].append(("c", (pts[0], pts[1], pts[1])))
                elif op == "h":
                    if subpaths:
                        start = subpaths[-1][0][1][0]
                        if subpaths[-1][-1][1][-1] != start:
                            subpaths[-1].append(("l", (start,)))
                elif op == "re":
                    subpaths.append([("re", ())])
                elif op in ("n",):
                    subpaths = []
                elif op in ("S", "s"):
                    if op == "s" and subpaths:
                        start = subpaths[-1][0][1][0]
                        if subpaths[-1][-1][1][-1] != start:
                            subpaths[-1].append(("l", (start,)))
                    painted.append(subpaths)
                    subpaths = []
                elif op in ("f", "F", "f*", "B", "B*", "b", "b*", "BT", "Do", "BI", "sh", "d0", "d1"):
                    return None
        except (IndexError, TypeError, ValueError):
            return None
        if len(painted) != 1 or len(painted[0]) != 1 or painted[0][0][0][0] != "m":
            return None
        path = painted[0][0]
        anchors: List[_XY] = [path[0][1][0]]
        handles: List[Tuple[_XY, _XY]] = []
        curved = False
        for kind, pts in path[1:]:
            a = anchors[-1]
            if kind == "l":
                b = pts[0]
                handles.append(((a[0] + (b[0] - a[0]) / 3.0, a[1] + (b[1] - a[1]) / 3.0),
                                (a[0] + 2.0 * (b[0] - a[0]) / 3.0, a[1] + 2.0 * (b[1] - a[1]) / 3.0)))
                anchors.append(b)
            elif kind == "c":
                handles.append((pts[0], pts[1]))
                anchors.append(pts[2])
                curved = True
            else:
                return None
        # the appearance must draw the same stroke /InkList describes
        listed = [p for path_pts in ink for p in path_pts]
        if not listed:
            return None
        def box(points: List[_XY]) -> Tuple[float, float, float, float]:
            return (min(p[0] for p in points), min(p[1] for p in points),
                    max(p[0] for p in points), max(p[1] for p in points))

        drawn = box(anchors + [c for pair in handles for c in pair] if curved else anchors)
        on_curve = box(anchors)
        lx = box(listed)
        tol = max(2.0, 0.1 * max(lx[2] - lx[0], lx[3] - lx[1]))
        inside = all(lx[i] >= drawn[i] - tol for i in (0, 1)) and all(lx[i] <= drawn[i] + tol for i in (2, 3))
        within = all(on_curve[i] >= lx[i] - tol for i in (0, 1)) and all(on_curve[i] <= lx[i] + tol for i in (2, 3))
        if math.dist(anchors[0], listed[0]) > tol or not inside or not within:
            return None
        if not curved:
            return _dedupe_xy(anchors), None
        return anchors, handles

    def _ap_fill_width(self, ad: Dict[str, Any]) -> Optional[float]:
        """Typical width of a stroke whose appearance *fills* its outline (GoodNotes draws
        variable-width pens as one filled blob per segment): every filled subpath is taken
        as a stadium (a rectangle with round ends), whose width follows from its area A and
        perimeter P (``pi/4 w^2 - P/2 w + A = 0``); the median over the blobs is returned."""
        ops, placement = self._ap_ops(ad)
        if ops is None:
            return None
        ctm = _IDENTITY
        stack: List[Tuple[float, ...]] = []
        subpaths: List[List[_XY]] = []
        filled: List[List[_XY]] = []
        try:
            for op, args in ops:
                if op == "q":
                    stack.append(ctm)
                elif op == "Q":
                    ctm = stack.pop() if stack else _IDENTITY
                elif op == "cm" and len(args) == 6 and all(isinstance(a, (int, float)) for a in args):
                    ctm = mat_mul(tuple(float(a) for a in args), ctm)
                elif op in ("m", "l", "c", "v", "y"):
                    if not all(isinstance(a, (int, float)) and not isinstance(a, bool) for a in args) or len(args) % 2:
                        return None
                    m = mat_mul(ctm, placement)
                    pts = [mat_apply(m, float(args[i]), float(args[i + 1])) for i in range(0, len(args), 2)]
                    if op == "m":
                        subpaths.append(pts[:1])
                    elif not subpaths:
                        return None
                    elif op == "l":
                        subpaths[-1].append(pts[0])
                    else:
                        p0 = subpaths[-1][-1]
                        if op == "c":
                            c1, c2, p1 = pts
                        elif op == "v":
                            c1, c2, p1 = p0, pts[0], pts[1]
                        else:
                            c1, c2, p1 = pts[0], pts[1], pts[1]
                        for k in range(1, 9):
                            t = k / 8.0
                            u = 1.0 - t
                            subpaths[-1].append((u * u * u * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0]
                                                 + t * t * t * p1[0],
                                                 u * u * u * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1]
                                                 + t * t * t * p1[1]))
                elif op == "re":
                    subpaths = []  # GoodNotes' clip rectangle
                elif op == "n":
                    subpaths = []
                elif op in ("f", "F", "f*"):
                    filled += subpaths
                    subpaths = []
                elif op in ("S", "s", "B", "B*", "b", "b*", "BT", "Do", "BI", "sh"):
                    return None
        except (IndexError, TypeError, ValueError):
            return None
        widths: List[float] = []
        for poly in filled:
            if len(poly) < 3:
                continue
            area = 0.0
            perimeter = 0.0
            for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
                area += x0 * y1 - x1 * y0
                perimeter += math.hypot(x1 - x0, y1 - y0)
            area = abs(area) / 2.0
            disc = perimeter * perimeter / 4.0 - math.pi * area
            w = (perimeter / 2.0 - math.sqrt(max(0.0, disc))) / (math.pi / 2.0)
            if w > 0 and math.isfinite(w):
                widths.append(w)
        if not widths:
            return None
        widths.sort()
        return min(widths[len(widths) // 2], 1000.0)

    def _ap_text_size(self, ad: Dict[str, Any]) -> Optional[float]:
        """Font size (pt, default user space) of the first text the appearance shows."""
        ops, placement = self._ap_ops(ad)
        if ops is None:
            return None
        ctm = _IDENTITY
        stack: List[Tuple[float, ...]] = []
        tm = _IDENTITY
        size = None
        try:
            for op, args in ops:
                if op == "q":
                    stack.append(ctm)
                elif op == "Q":
                    ctm = stack.pop() if stack else _IDENTITY
                elif op == "cm" and len(args) == 6 and all(isinstance(a, (int, float)) for a in args):
                    ctm = mat_mul(tuple(float(a) for a in args), ctm)
                elif op == "BT":
                    tm = _IDENTITY
                elif op == "Tm" and len(args) == 6 and all(isinstance(a, (int, float)) for a in args):
                    tm = tuple(float(a) for a in args)
                elif op == "Tf" and len(args) == 2 and isinstance(args[1], (int, float)):
                    size = float(args[1])
                elif op in ("Tj", "TJ", "'", '"'):
                    if size is None:
                        return None
                    m = mat_mul(mat_mul(tm, ctm), placement)
                    shown = abs(size) * math.hypot(m[2], m[3])
                    return shown if 0.5 <= shown <= 1000.0 else None
        except (IndexError, TypeError, ValueError):
            return None
        return None

    # -- geometry ------------------------------------------------------------------------

    def _pairs(self, value: Any) -> List[_XY]:
        nums = self.pdf.numbers(value)
        return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]

    @staticmethod
    def _to_model(page: PageObj, x: float, y: float) -> _XY:
        dx, dy = mat_apply(page.matrix, x, y)
        return dx, page.height - dy

    def _points(self, page: PageObj, pairs: Sequence[_XY], width: float) -> List[Point]:
        out = []
        for x, y in pairs:
            mx, my = self._to_model(page, x, y)
            out.append(Point(mx, my, width))
        self.points += len(out)
        return out

    # -- conversions ---------------------------------------------------------------------

    def _convert(self, ad: Dict[str, Any], subtype: str, page: PageObj) -> Optional[Tuple[list, list]]:
        """``(strokes, texts)`` for one annotation, or ``None`` to leave it in the PDF."""
        if subtype == "FreeText":
            box = self._free_text(ad, page)
            return ([], [box]) if box is not None else None
        if subtype == "Highlight":
            marks = self._highlight(ad, page)
            return (marks, []) if marks else None
        colour, kind = self._style(ad)
        width = self._width(ad)
        strokes: List[Stroke] = []
        if subtype == "Ink":
            ink = self.pdf.resolve(ad.get("InkList"))
            if not isinstance(ink, list):
                return None
            paths = [self._pairs(path) for path in ink[:10000]]
            exact = self._ap_path(ad, paths) if len(paths) == 1 else None
            if exact is not None:
                anchors, handles = exact
                pts = [Point(*self._to_model(page, x, y), width) for x, y in anchors]
                controls = None
                if handles is not None:
                    controls = [(Point(*self._to_model(page, *c1), width), Point(*self._to_model(page, *c2), width))
                                for c1, c2 in handles]
                self.points += len(pts)
                strokes.append(Stroke(pts, color=colour, kind=kind, width=width, controls=controls))
            else:
                drawn = self._ap_fill_width(ad)
                if drawn is not None and width < drawn <= 4.0 * width + 10.0:
                    width = drawn  # variable-width pens: /BS /W understates what is drawn
                for pairs in paths:
                    pts = _dedupe(self._points(page, pairs, width))
                    if pts:
                        strokes.append(Stroke(pts, color=colour, kind=kind, width=width))
        elif subtype == "Line":
            pairs = self._pairs(ad.get("L"))[:2]
            if len(pairs) == 2:
                pts = self._points(page, pairs, width)
                strokes.append(Stroke(pts, color=colour, kind=kind, width=width, controls=_straight(pts)))
            if self.pdf.resolve(ad.get("LE")) is not None:
                ends = self.pdf.resolve(ad.get("LE"))
                if isinstance(ends, list) and any(self.pdf.resolve(e) not in (None, "None") for e in ends):
                    self.warn_once("line-ends", "line endings (arrow heads) of PDF line annotations were dropped")
        elif subtype in ("PolyLine", "Polygon"):
            pts = _dedupe(self._points(page, self._pairs(ad.get("Vertices")), width))
            if subtype == "Polygon" and len(pts) > 2 and (pts[0].x, pts[0].y) != (pts[-1].x, pts[-1].y):
                pts.append(Point(pts[0].x, pts[0].y, width))
            if pts:
                strokes.append(Stroke(pts, color=colour, kind=kind, width=width, controls=_straight(pts)))
                if subtype == "Polygon":
                    strokes += self._interior(ad, pts, colour[3])
        elif subtype in ("Square", "Circle"):
            pts = self._box_shape(ad, page, width, subtype == "Circle")
            if pts:
                controls = None if subtype == "Circle" else _straight(pts)
                strokes.append(Stroke(pts, color=colour, kind=kind, width=width, controls=controls))
                strokes += self._interior(ad, pts, colour[3])
        if self._dashed(ad):
            self.warn_once("dash", "dashed PDF annotation borders are drawn solid")
        return (strokes, []) if strokes else None

    def _dashed(self, ad: Dict[str, Any]) -> bool:
        bs = self.pdf.dict_of(ad.get("BS"))
        return bool(bs) and self.pdf.resolve(bs.get("S")) == "D"

    def _interior(self, ad: Dict[str, Any], pts: List[Point], alpha: float) -> List[Stroke]:
        if not self.pdf.numbers(ad.get("IC")) or len(pts) < 3:
            return []
        r, g, b = self._colour(ad.get("IC"), (1.0, 1.0, 1.0))
        ring = [Point(p.x, p.y, 0.0) for p in pts]
        return [Stroke(list(ring), color=(r, g, b, alpha), kind="fill", pen=None, width=0.0, outline=[ring])]

    def _box_shape(self, ad: Dict[str, Any], page: PageObj, width: float, ellipse: bool) -> List[Point]:
        rect = self.pdf._box(ad.get("Rect"))
        if rect is None:
            return []
        x0, y0, x1, y1 = rect
        rd = self.pdf.numbers(ad.get("RD"))
        if len(rd) >= 4 and all(v >= 0 for v in rd[:4]):
            x0, y0, x1, y1 = x0 + rd[0], y0 + rd[3], x1 - rd[2], y1 - rd[1]
        inset = width / 2.0
        if x1 - x0 > 2 * inset and y1 - y0 > 2 * inset:
            x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
        if x1 <= x0 or y1 <= y0:
            return []
        if ellipse:
            cx, cy, rx, ry = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0) / 2.0, (y1 - y0) / 2.0
            pairs = [(cx + rx * math.cos(2 * math.pi * i / ELLIPSE_SAMPLES),
                      cy + ry * math.sin(2 * math.pi * i / ELLIPSE_SAMPLES)) for i in range(ELLIPSE_SAMPLES + 1)]
        else:
            pairs = [(x0, y1), (x1, y1), (x1, y0), (x0, y0), (x0, y1)]
        return self._points(page, pairs, width)

    def _highlight(self, ad: Dict[str, Any], page: PageObj) -> List[Stroke]:
        r, g, b = self._colour(ad.get("C"), (1.0, 1.0, 0.0))
        ca = self._num(ad.get("CA"))
        alpha = ca if ca is not None and 0.0 < ca < 1.0 else HIGHLIGHT_ALPHA
        nums = self.pdf.numbers(ad.get("QuadPoints"))
        strokes: List[Stroke] = []
        for i in range(0, len(nums) - 7, 8):
            quad = [self._to_model(page, nums[i + 2 * k], nums[i + 2 * k + 1]) for k in range(4)]
            line = _quad_centre_line(quad)
            if line is None:
                continue
            (ax, ay), (bx, by), height = line
            pts = [Point(ax, ay, height), Point(bx, by, height)]
            self.points += 2
            strokes.append(Stroke(pts, color=(r, g, b, alpha), kind="highlighter", width=height,
                                  controls=_straight(pts)))
        return strokes

    def _free_text(self, ad: Dict[str, Any], page: PageObj) -> Optional[TextBox]:
        text = decode_text(self.pdf.resolve(ad.get("Contents"))).replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            return None
        rect = self.pdf._box(ad.get("Rect"))
        if rect is None:
            return None
        x0, y0, x1, y1 = rect
        rd = self.pdf.numbers(ad.get("RD"))
        if len(rd) >= 4 and all(v >= 0 for v in rd[:4]) and rd[0] + rd[2] < x1 - x0 and rd[1] + rd[3] < y1 - y0:
            x0, y0, x1, y1 = x0 + rd[0], y0 + rd[3], x1 - rd[2], y1 - rd[1]
        size, colour = _parse_da(decode_text(self.pdf.resolve(ad.get("DA"))))
        shown = self._ap_text_size(ad)
        if shown is not None:
            size = shown  # what viewers display (GoodNotes' /DA always says 13 pt)
        if size is None or colour is None:
            ds_size, ds_colour = _parse_ds(decode_text(self.pdf.resolve(ad.get("DS"))))
            size = size if size is not None else ds_size
            colour = colour if colour is not None else ds_colour
        size = size if size is not None and 0 < size <= 1000 else DEFAULT_TEXT_SIZE
        rgb = colour if colour is not None else (0.0, 0.0, 0.0)
        ca = self._num(ad.get("CA"))
        rgba = (rgb[0], rgb[1], rgb[2], min(1.0, max(0.0, ca)) if ca is not None else 1.0)
        q = int(self._num(ad.get("Q"), 0.0) or 0)
        align = {1: "center", 2: "right"}.get(q, "left")
        tx, ty = self._to_model(page, x0, y1)  # the text frame's top-left corner
        return TextBox(x=tx, y=ty, w=x1 - x0, h=y1 - y0, text=text, runs=[TextRun(text, size=size, color=rgba)],
                       color=rgba, size=size, rotation=float(page.rotate), align=align)

    # -- the whole file ---------------------------------------------------------------------

    def read(self) -> Document:
        pdf, doc = self.pdf, self.doc
        title = decode_text(pdf.resolve(pdf.info().get("Title"))).strip() if pdf.xref_ok else ""
        doc.title = title or "PDF"
        pdf_id = _uuid_for(self.data)
        pages = [Page(width=p.width, height=p.height, background=PdfBackground(pdf_id, i), paper="plain",
                      template_is_builtin=False) for i, p in enumerate(self.pages)]
        doc.pages = pages
        if pdf.encrypted:
            for message in pdf.warnings:
                doc.warn(f"PDF: {message}")
            doc.warn("the PDF is encrypted; its annotations stay part of the pages and were not converted")
            doc.pdfs[pdf_id] = self.data
            return doc
        new_annots: Dict[int, List[Any]] = {}
        converted: Dict[int, Tuple[List[Stroke], List[TextBox]]] = {}
        for index, page in enumerate(self.pages):
            try:
                result = self._page_annotations(page)
            except (PdfError, RecursionError, ValueError, TypeError) as exc:
                self.warn_once("annots", f"annotations of a PDF page could not be read ({exc}); they stay in the PDF")
                result = None
            if result is None:
                continue
            kept, strokes, texts = result
            if page.ref is None:
                self.warn_once("direct", "a PDF page is not an indirect object; its annotations stay in the PDF")
                continue
            new_annots[index] = kept
            converted[index] = (strokes, texts)
        data = self.data
        if new_annots:
            data = self._strip(new_annots)
            if data is None:
                doc.warn("the converted annotations could not be removed from the PDF; they stay part of the "
                         "pages and were not converted")
                data = self.data
                converted = {}
        for index, (strokes, texts) in converted.items():
            pages[index].strokes += strokes
            pages[index].texts += texts
        doc.pdfs[pdf_id] = data
        for message in pdf.warnings:  # defects the parser tolerated (damaged xref, ...)
            doc.warn(f"PDF: {message}")
        if self.kept:
            names = ", ".join(sorted(self.kept))
            total = sum(self.kept.values())
            doc.warn(f"{total} PDF annotations ({names}) stay part of the PDF pages")
        return doc

    def _page_annotations(self, page: PageObj) -> Optional[Tuple[List[Any], List[Stroke], List[TextBox]]]:
        """Converted strokes / texts of a page and the ``/Annots`` entries to keep, or
        ``None`` when nothing on the page is converted."""
        pdf = self.pdf
        annots = pdf.resolve(page.dict.get("Annots"))
        if not isinstance(annots, list) or not annots:
            return None
        strokes: List[Stroke] = []
        texts: List[TextBox] = []
        removed: Set[int] = set()  # indices into annots
        removed_nums: Set[int] = set()
        popups: Set[int] = set()
        for i, item in enumerate(annots[:MAX_ANNOTS_PER_PAGE]):
            ad = pdf.resolve(item)
            if not isinstance(ad, dict):
                continue
            subtype = pdf.resolve(ad.get("Subtype"))
            flags = int(self._num(ad.get("F"), 0.0) or 0)
            if subtype not in CONVERTED_SUBTYPES or flags & 2 or flags & 32 or self.points > MAX_POINTS:
                continue
            result = self._convert(ad, str(subtype), page)
            if result is None:
                continue
            strokes += result[0]
            texts += result[1]
            removed.add(i)
            if isinstance(item, Ref):
                removed_nums.add(item.num)
            popup = ad.get("Popup")
            if isinstance(popup, Ref):
                popups.add(popup.num)
        if not removed:
            for item in annots[:MAX_ANNOTS_PER_PAGE]:
                self._count_kept(item)
            return None
        kept: List[Any] = []
        for i, item in enumerate(annots):
            if i in removed:
                continue
            ad = pdf.resolve(item)
            if isinstance(ad, dict) and pdf.resolve(ad.get("Subtype")) == "Popup":
                parent = ad.get("Parent")
                if (isinstance(item, Ref) and item.num in popups) or \
                        (isinstance(parent, Ref) and parent.num in removed_nums):
                    continue
            kept.append(item)
            self._count_kept(item)
        return kept, strokes, texts

    def _count_kept(self, item: Any) -> None:
        ad = self.pdf.resolve(item)
        if not isinstance(ad, dict):
            return
        subtype = self.pdf.resolve(ad.get("Subtype"))
        if isinstance(subtype, str) and subtype not in ("Popup", "Link"):
            self.kept[subtype] = self.kept.get(subtype, 0) + 1

    def _strip(self, new_annots: Dict[int, List[Any]]) -> Optional[bytes]:
        """The PDF with the pages' ``/Annots`` replaced (``None`` when that fails)."""
        pdf = self.pdf
        dicts: Dict[int, Dict[str, Any]] = {}
        for index, kept in new_annots.items():
            d = dict(self.pages[index].dict)
            if kept:
                d["Annots"] = list(kept)
            else:
                d.pop("Annots", None)
            dicts[index] = d
        try:
            if pdf.healthy():
                changes = {self.pages[i].ref: d for i, d in dicts.items() if self.pages[i].ref is not None}
                out = incremental_update(pdf, changes)
            else:
                out = rewrite(pdf, dicts)
        except (PdfError, ValueError, TypeError, RecursionError):
            return None
        # verify: same pages, the rewritten pages carry exactly the kept annotations
        try:
            check = PdfFile(out)
            pages = check.pages()
        except Exception:  # noqa: BLE001 - any failure means "keep the original"
            return None
        if len(pages) != len(self.pages):
            return None
        for index, kept in new_annots.items():
            annots = check.resolve(pages[index].dict.get("Annots"))
            count = len(annots) if isinstance(annots, list) else 0
            if count != len(kept):
                return None
        return out


def _dedupe_xy(points: List[_XY], eps: float = 1e-4) -> List[_XY]:
    out: List[_XY] = []
    for p in points:
        if not out or abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps:
            out.append(p)
    return out


def _content_ops(data: bytes, limit: int = 200_000):
    """``(operator, operands)`` pairs of a content stream (inline images end the walk)."""
    lexer = _Lexer(data)
    operands: List[Any] = []
    count = 0
    while count < limit:
        tok = lexer.next()
        if tok is None:
            return
        count += 1
        if isinstance(tok, Keyword):
            if tok in ("<<", "["):
                operands.append(_parse_value(lexer, tok))
                continue
            if tok == "null":
                operands.append(None)
                continue
            yield str(tok), operands
            if tok == "BI":
                return
            operands = []
        else:
            operands.append(tok)


def _quad_centre_line(quad: List[_XY]) -> Optional[Tuple[_XY, _XY, float]]:
    """Centre line ``(start, end, height)`` of a text-markup quadrilateral.

    The text runs from the first to the second corner in both orders seen in files (the
    specification's counter-clockwise BL BR TR TL and Acrobat's TL TR BL BR); the line spans
    the quad's extent along that direction, at the middle of its extent across it.
    """
    (x1, y1), (x2, y2) = quad[0], quad[1]
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 1e-9:  # degenerate first edge: use the longest edge instead
        edges = [(quad[i], quad[(i + 1) % 4]) for i in range(4)]
        (x1, y1), (x2, y2) = max(edges, key=lambda e: math.hypot(e[1][0] - e[0][0], e[1][1] - e[0][1]))
        length = math.hypot(x2 - x1, y2 - y1)
        if length <= 1e-9:
            return None
    dx, dy = (x2 - x1) / length, (y2 - y1) / length
    nx, ny = -dy, dx
    along = [px * dx + py * dy for px, py in quad]
    across = [px * nx + py * ny for px, py in quad]
    t0, t1 = min(along), max(along)
    n0, n1 = min(across), max(across)
    height = n1 - n0
    if height <= 1e-6:
        return None
    mid = (n0 + n1) / 2.0
    if t1 - t0 > height:  # the round caps of the stroke end at the quad's edges
        t0, t1 = t0 + height / 2.0, t1 - height / 2.0
    else:
        t0 = t1 = (t0 + t1) / 2.0
    start = (dx * t0 + nx * mid, dy * t0 + ny * mid)
    end = (dx * t1 + nx * mid, dy * t1 + ny * mid)
    return start, end, height


_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"
_TF_RE = re.compile(r"/\S+\s+(" + _NUM + r")\s+Tf")
_RG_RE = re.compile(r"(" + _NUM + r")\s+(" + _NUM + r")\s+(" + _NUM + r")\s+rg\b")
_K_RE = re.compile(r"(" + _NUM + r")\s+(" + _NUM + r")\s+(" + _NUM + r")\s+(" + _NUM + r")\s+k\b")
_G_RE = re.compile(r"(?<![\d.])(" + _NUM + r")\s+g\b")


def _parse_da(da: str) -> Tuple[Optional[float], Optional[Tuple[float, float, float]]]:
    """Font size and fill colour from a default-appearance string (``/Helv 12 Tf 0 g``)."""
    size = None
    m = _TF_RE.search(da)
    if m:
        size = float(m.group(1)) or None
    colour: Optional[Tuple[float, float, float]] = None
    best = -1
    for regex, n in ((_RG_RE, 3), (_K_RE, 4), (_G_RE, 1)):
        for m in regex.finditer(da):
            if m.start() < best:
                continue
            vals = [min(1.0, max(0.0, float(v))) for v in m.groups()]
            if n == 3:
                colour = (vals[0], vals[1], vals[2])
            elif n == 4:
                c, mm, y, k = vals
                colour = ((1 - c) * (1 - k), (1 - mm) * (1 - k), (1 - y) * (1 - k))
            else:
                colour = (vals[0], vals[0], vals[0])
            best = m.start()
    return size, colour


def _parse_ds(ds: str) -> Tuple[Optional[float], Optional[Tuple[float, float, float]]]:
    """Font size and colour from a rich-text default style (``font: Helvetica 12pt; color:#FF0000``)."""
    size = None
    m = re.search(r"(" + _NUM + r")\s*pt", ds)
    if m:
        size = float(m.group(1)) or None
    colour = None
    m = re.search(r"color\s*:\s*#([0-9a-fA-F]{6})", ds)
    if m:
        h = m.group(1)
        colour = (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)
    return size, colour


def read_pdf(data: bytes) -> Document:
    """Read a PDF into a :class:`Document` (see the module docstring)."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_pdf expects bytes")
    data = bytes(data)
    if b"%PDF" not in data[:1024]:
        raise ValueError("not a PDF file (no %PDF header)")
    try:
        reader = _Reader(data)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - a damaged file is a ValueError, never a crash
        raise ValueError(f"the PDF could not be read ({exc.__class__.__name__}: {exc})") from None
    try:
        return reader.read()
    except Exception as exc:  # noqa: BLE001 - keep the pages, report the problem
        doc = reader.doc
        if not doc.pages:
            raise ValueError(f"the PDF could not be read ({exc.__class__.__name__}: {exc})") from None
        doc.warn(f"the PDF's annotations could not be converted ({exc.__class__.__name__}: {exc})")
        for page in doc.pages:
            page.strokes.clear()
            page.texts.clear()
        if doc.pages[0].background is not None:
            doc.pdfs[doc.pages[0].background.pdf_id] = data
        return doc
