"""Paper PDFs for the page backgrounds of Stylus Labs Write documents (``docs/styluswrite.md``
section 4).

Write draws a page's standard ruling from its Page Setup values: the paper colour, horizontal
rules every ``yruling`` units starting one pitch below the top, vertical rules every
``xruling`` units, a red margin line at ``marginLeft``, all in the alpha-first ``rulecolor``
(the margin keeps its red and takes the rules' opacity).  Dot papers are custom rulings: a
``<pattern>`` of circles filling the page, or circles drawn one by one.  :class:`Paper`
describes such a background in pt and :func:`paper_pdf` draws it as a one-page PDF in the shape
of :func:`gnnote.pdfutil.make_paper_pdf`'s papers (rules as thin ``re`` rectangles, dots as
Bezier circles, producer ``gnnote``), so readers that recognise gnnote's stock papers recognise
these too.  Colours with an alpha are blended over the paper colour, so the PDF needs no
transparency.

The work is bounded: :meth:`Paper.primitives` counts the rules and dots before anything is
drawn, and the reader refuses papers above :data:`MAX_PRIMITIVES`.
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass
from typing import List, Sequence, Tuple

__all__ = ["Paper", "DotGrid", "Dot", "paper_pdf", "blend", "MAX_PRIMITIVES"]

RGB = Tuple[float, float, float]
MAX_PRIMITIVES = 50_000  # rules plus dots one generated paper may draw
_HUGE = 1 << 40  # stands for "more than any budget" in counts
_KAPPA = 0.5523  # Bezier handle length of a quarter circle (as pdfutil's papers)


def blend(color: Sequence[float], under: RGB) -> RGB:
    """``color`` (r, g, b, a) composited over the opaque ``under``."""
    a = min(1.0, max(0.0, float(color[3]) if len(color) > 3 else 1.0))
    return (color[0] * a + under[0] * (1.0 - a), color[1] * a + under[1] * (1.0 - a),
            color[2] * a + under[2] * (1.0 - a))


def _steps(start: float, end: float, first: float, pitch: float) -> int:
    """How many values ``first + k * pitch`` (``k`` any integer) lie in ``[start, end]``."""
    if not (pitch > 0 and end >= start):
        return 0
    lo, hi = (start - first) / pitch, (end - first) / pitch
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi - lo > _HUGE:
        return _HUGE
    return max(0, math.floor(hi) - math.ceil(lo) + 1)


@dataclass(frozen=True)
class DotGrid:
    """Dots of radius ``r`` centred on ``(x0 + i dx, y0 + j dy)`` for every integer ``i``, ``j``
    whose centre lies in the box ``left``..``right`` x ``top``..``bottom`` (pt, y down)."""

    x0: float
    y0: float
    dx: float
    dy: float
    r: float
    left: float
    top: float
    right: float
    bottom: float
    color: RGB

    def count(self) -> int:
        nx = _steps(self.left, self.right, self.x0, self.dx)
        ny = _steps(self.top, self.bottom, self.y0, self.dy)
        return min(_HUGE, nx * ny)


@dataclass(frozen=True)
class Dot:
    x: float
    y: float
    r: float
    color: RGB


@dataclass(frozen=True)
class Paper:
    """A page background in pt (y down).  Pitches of 0 draw no rules, a margin of 0 none."""

    width: float
    height: float
    color: RGB = (1.0, 1.0, 1.0)
    rule_color: RGB = (0.0, 0.0, 1.0)
    rule_width: float = 0.48
    xpitch: float = 0.0
    ypitch: float = 0.0
    margin: float = 0.0
    margin_color: RGB = (1.0, 0.0, 0.0)
    grids: Tuple[DotGrid, ...] = ()
    dots: Tuple[Dot, ...] = ()

    def rule_counts(self) -> Tuple[int, int]:
        """(horizontal, vertical) rules: every pitch from one pitch in, short of the far edge."""
        h = _steps(self.ypitch, self.height - 1e-6, self.ypitch, self.ypitch) if self.ypitch > 0 else 0
        v = _steps(self.xpitch, self.width - 1e-6, self.xpitch, self.xpitch) if self.xpitch > 0 else 0
        return h, v

    def has_margin(self) -> bool:
        return 0 < self.margin < self.width

    def primitives(self) -> int:
        """Rules, margin and dots this paper draws (counted without drawing them)."""
        h, v = self.rule_counts()
        total = h + v + int(self.has_margin()) + len(self.dots)
        for grid in self.grids:
            total += grid.count()
        return min(_HUGE, total)

    def is_blank(self) -> bool:
        """White paper without anything drawn on it."""
        return self.primitives() == 0 and all(c >= 1.0 for c in self.color)


def _f(x: float) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _rgb(color: RGB) -> str:
    return " ".join(_f(min(1.0, max(0.0, c))) for c in color)


def _circle(cx: float, cy: float, r: float) -> str:
    k = _KAPPA * r
    return (f"{_f(cx + r)} {_f(cy)} m {_f(cx + r)} {_f(cy + k)} {_f(cx + k)} {_f(cy + r)} {_f(cx)} {_f(cy + r)} c "
            f"{_f(cx - k)} {_f(cy + r)} {_f(cx - r)} {_f(cy + k)} {_f(cx - r)} {_f(cy)} c "
            f"{_f(cx - r)} {_f(cy - k)} {_f(cx - k)} {_f(cy - r)} {_f(cx)} {_f(cy - r)} c "
            f"{_f(cx + k)} {_f(cy - r)} {_f(cx + r)} {_f(cy - k)} {_f(cx + r)} {_f(cy)} c h f\n")


def paper_pdf(paper: Paper) -> bytes:
    """A one-page PDF 1.4 of the paper.  The caller checks :meth:`Paper.primitives` first."""
    w, h = paper.width, paper.height
    if not (w > 0 and h > 0 and math.isfinite(w) and math.isfinite(h)):
        raise ValueError("page size must be positive")
    if paper.primitives() > MAX_PRIMITIVES:
        raise ValueError("the paper draws too many rules or dots")
    half = paper.rule_width / 2.0
    content: List[str] = [f"q {_rgb(paper.color)} rg 0 0 {_f(w)} {_f(h)} re f Q\n"]
    rows, cols = paper.rule_counts()
    if rows or cols:
        content.append(f"q {_rgb(paper.rule_color)} rg\n")
        for k in range(1, rows + 1):
            content.append(f"0 {_f(h - k * paper.ypitch - half)} {_f(w)} {_f(paper.rule_width)} re f\n")
        for k in range(1, cols + 1):
            content.append(f"{_f(k * paper.xpitch - half)} 0 {_f(paper.rule_width)} {_f(h)} re f\n")
        content.append("Q\n")
    if paper.has_margin():
        content.append(f"q {_rgb(paper.margin_color)} rg {_f(paper.margin - half)} 0 {_f(paper.rule_width)} "
                       f"{_f(h)} re f Q\n")
    for grid in paper.grids:
        if not grid.count():
            continue
        content.append(f"q {_rgb(grid.color)} rg\n")
        i0, i1 = math.ceil((grid.left - grid.x0) / grid.dx), math.floor((grid.right - grid.x0) / grid.dx)
        j0, j1 = math.ceil((grid.top - grid.y0) / grid.dy), math.floor((grid.bottom - grid.y0) / grid.dy)
        for j in range(j0, j1 + 1):
            y = h - (grid.y0 + j * grid.dy)
            for i in range(i0, i1 + 1):
                content.append(_circle(grid.x0 + i * grid.dx, y, grid.r))
        content.append("Q\n")
    for dot in paper.dots:
        content.append(f"q {_rgb(dot.color)} rg {_circle(dot.x, h - dot.y, dot.r)}Q\n")
    stream = zlib.compress("".join(content).encode("ascii"), 6)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_f(w)} {_f(h)}] "
         f"/Resources << /ProcSet [/PDF] >> /Contents 4 0 R >>").encode("ascii"),
        b"<< /Length " + str(len(stream)).encode("ascii") + b" /Filter /FlateDecode >>\nstream\n" + stream
        + b"\nendstream",
        b"<< /Producer (gnnote) /Creator (gnnote) >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: List[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 5 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode("ascii")
    return bytes(out)
