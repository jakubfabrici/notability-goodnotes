"""Format-neutral document model shared by the GoodNotes and Notability codecs.

Units and conventions (everything is normalised to these at the codec boundary):

* Lengths are PDF points (1/72 inch).  Page origin is the top-left corner, y grows
  downwards.  Both apps use this orientation internally once their own scale factor
  is applied, so the codecs only scale, never flip.
* Colours are ``(r, g, b, a)`` floats in 0..1.
* A :class:`Stroke` is either a polyline (``controls is None``; GoodNotes native) or
  a chain of cubic Bezier segments (``controls`` holds one ``(c1, c2)`` pair per
  segment; Notability native).  ``points`` are always the on-curve anchors and each
  anchor carries the *rendered* width at that position, i.e. pen width with the
  pressure already applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

RGBA = Tuple[float, float, float, float]


@dataclass
class Point:
    x: float
    y: float
    width: float = 1.0  # rendered width in pt at this point


@dataclass
class Stroke:
    points: List[Point]
    color: RGBA = (0.0, 0.0, 0.0, 1.0)
    kind: str = "pen"  # "pen" | "highlighter" | "fill" (a filled closed shape; see ``outline``)
    pen: Optional[str] = None  # best-effort tool name: ballpoint / fountain / brush / pencil
    width: float = 1.0  # nominal pen width in pt (before pressure)
    controls: Optional[List[Tuple[Point, Point]]] = None  # cubic Bezier handles, len == len(points) - 1
    outline: Optional[List[List[Point]]] = None  # closed polygons (pt) for strokes GoodNotes stores as filled shapes
    # ``kind == "fill"``: ``outline`` holds the filled region (GoodNotes' translucent shape fill),
    # ``color`` its fill colour (alpha included) and ``points`` the first polygon's vertices so
    # bbox()/page assignment keep working; such strokes have no centre line to draw.

    @property
    def is_bezier(self) -> bool:
        return self.controls is not None

    def bbox(self) -> Tuple[float, float, float, float]:
        xs = [p.x for p in self.points]
        ys = [p.y for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Image:
    x: float
    y: float
    w: float
    h: float
    data: bytes
    fmt: str = "png"  # "png" | "jpeg" | "pdf" (a vector sticker: page 1 of ``data`` fills the box)
    rotation: float = 0.0  # degrees, clockwise, about the box centre


@dataclass
class TextRun:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font: Optional[str] = None
    size: Optional[float] = None
    color: Optional[RGBA] = None


@dataclass
class TextBox:
    x: float
    y: float
    w: float
    h: float
    text: str  # plain text, lines separated by "\n"
    runs: List[TextRun] = field(default_factory=list)
    color: RGBA = (0.0, 0.0, 0.0, 1.0)
    size: float = 12.0
    rotation: float = 0.0  # degrees, clockwise, about the box's top-left corner
    align: str = "left"  # "left" | "center" | "right"


@dataclass
class PdfBackground:
    """One page of a PDF stored in :attr:`Document.pdfs` shown behind a page."""

    pdf_id: str
    page_index: int  # 0-based page inside the PDF


@dataclass
class Page:
    width: float
    height: float
    strokes: List[Stroke] = field(default_factory=list)
    images: List[Image] = field(default_factory=list)
    texts: List[TextBox] = field(default_factory=list)
    background: Optional[PdfBackground] = None
    paper: str = "plain"  # hint when there is no PDF background: plain / lined / grid / dotted
    template_is_builtin: bool = False  # True when ``background`` is a stock paper template of the source app


@dataclass
class Document:
    title: str = "Untitled"
    pages: List[Page] = field(default_factory=list)
    pdfs: Dict[str, bytes] = field(default_factory=dict)  # shared PDF blobs referenced by PdfBackground.pdf_id
    source_format: str = ""  # "goodnotes" | "notability"
    warnings: List[str] = field(default_factory=list)  # human-readable notes about lossy steps

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
