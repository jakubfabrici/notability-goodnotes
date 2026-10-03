"""Synthetic documents and small binary assets shared by the open-format codec tests
(Xournal++, Saber, Excalidraw).  Everything here is generated; no sample file is involved."""
from __future__ import annotations

import math
import zlib
from typing import Dict, List, Sequence, Tuple

from gnnote.goodnotes.constants import THUMBNAIL_JPEG
from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from gnnote.notability.writer import white_png

JPEG = THUMBNAIL_JPEG  # a small valid baseline JPEG


def png(width: int = 6, height: int = 4) -> bytes:
    return white_png(width, height)


def pdf(sizes: Sequence[Tuple[float, float]]) -> bytes:
    """A minimal valid PDF with one empty page per ``(width, height)``."""
    n = len(sizes)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects: List[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>",
                            f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()]
    for i, (w, h) in enumerate(sizes):
        stream = zlib.compress(b"0 0 0 rg 10 10 20 20 re f\n")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:g} {h:g}] /Contents {4 + 2 * i} 0 R >>"
                       .encode())
        objects.append(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


STICKER_PDF = pdf([(40.0, 30.0)])
BACKGROUND_PDF = pdf([(612.0, 792.0), (612.0, 792.0)])


def _wave(x0: float, y0: float, n: int, widths: Sequence[float]) -> List[Point]:
    return [Point(x0 + 12.0 * i, y0 + 6.0 * math.sin(i), widths[i % len(widths)]) for i in range(n)]


def full_document() -> Document:
    """One document with every element type the model has."""
    p1 = Page(455.04, 588.45, paper="lined")
    pressure = Stroke(_wave(40, 60, 8, [0.8, 1.2, 1.6, 2.0, 1.4, 1.0, 0.9, 0.7]), color=(0.1, 0.2, 0.7, 1.0), width=1.5)
    constant = Stroke(_wave(40, 100, 6, [1.0]), color=(0.0, 0.0, 0.0, 1.0), width=1.0)
    highlighter = Stroke([Point(40, 140, 12.0), Point(200, 140, 12.0)], color=(1.0, 0.9, 0.0, 0.5),
                         kind="highlighter", width=12.0)
    anchors = [Point(250, 60, 1.2), Point(300, 90, 1.2), Point(350, 60, 1.2)]
    controls = [(Point(265, 40, 1.2), Point(285, 100, 1.2)), (Point(315, 80, 1.2), Point(335, 40, 1.2))]
    bezier = Stroke(anchors, color=(0.8, 0.1, 0.1, 1.0), width=1.2, controls=controls)
    dot = Stroke([Point(120, 200, 2.0)], color=(0.0, 0.5, 0.0, 1.0), width=2.0)
    pencil = Stroke(_wave(40, 230, 5, [0.6]), color=(0.3, 0.3, 0.3, 1.0), pen="pencil", width=0.6)
    rect = [Point(260, 160, 1.0), Point(380, 160, 1.0), Point(380, 240, 1.0), Point(260, 240, 1.0),
            Point(260, 160, 1.0)]
    outline = Stroke(rect, color=(0.2, 0.4, 0.9, 1.0), width=1.0)
    fill_ring = [Point(p.x, p.y, 0.0) for p in rect[:4]]
    fill = Stroke(list(fill_ring), color=(0.2, 0.4, 0.9, 0.25), kind="fill", width=0.0, outline=[fill_ring])
    lone_ring = [Point(60, 300, 0.0), Point(140, 300, 0.0), Point(100, 360, 0.0)]
    lone_fill = Stroke(list(lone_ring), color=(0.9, 0.3, 0.1, 0.4), kind="fill", width=0.0, outline=[lone_ring])
    p1.strokes = [pressure, constant, highlighter, bezier, dot, pencil, outline, fill, lone_fill]
    p1.images = [Image(40, 400, 60, 40, png(), fmt="png"),
                 Image(150, 400, 64, 48, JPEG, fmt="jpeg", rotation=90.0),
                 Image(260, 400, 40, 30, STICKER_PDF, fmt="pdf")]
    p1.texts = [TextBox(40, 480, 200, 20, "Hello <ink> & text", runs=[TextRun("Hello <ink> & text", size=14.0)],
                        size=14.0),
                TextBox(260, 480, 150, 40, "Turned\nand bold",
                        runs=[TextRun("Turned\nand bold", bold=True, font="Helvetica", size=12.0,
                                      color=(0.8, 0.0, 0.0, 1.0))],
                        color=(0.8, 0.0, 0.0, 1.0), size=12.0, rotation=30.0, align="center")]
    p2 = Page(612.0, 792.0, background=PdfBackground("background.pdf", 1))
    p2.strokes = [Stroke(_wave(100, 100, 4, [1.5]), width=1.5)]
    p3 = Page(612.0, 792.0, background=PdfBackground("background.pdf", 0))
    p4 = Page(455.04, 588.45, paper="grid")
    return Document(title="Every element", pages=[p1, p2, p3, p4], pdfs={"background.pdf": BACKGROUND_PDF})


def counts(doc: Document) -> List[Dict[str, int]]:
    return [{"ink": sum(1 for s in p.strokes if s.kind != "fill"),
             "fills": sum(1 for s in p.strokes if s.kind == "fill"),
             "images": len(p.images), "texts": len(p.texts)} for p in doc.pages]


def stats(doc: Document) -> Tuple[int, int, int, int]:
    """(pages, strokes, images, texts) as gnnote's ``document_stats`` counts them."""
    return (len(doc.pages), sum(len(p.strokes) for p in doc.pages), sum(len(p.images) for p in doc.pages),
            sum(len(p.texts) for p in doc.pages))
