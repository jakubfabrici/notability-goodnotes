"""Shared helpers of the PDF codec tests (no tests here).

* :func:`build_pdf` writes a classic PDF from numbered object bodies with exact xref offsets,
  :func:`stream` a stream object body;
* :func:`encode_png` is an independent PNG encoder (every colour type and bit depth,
  Adam7 interlacing, the five filter types chosen at random per row) used to feed the
  image path;
* PyMuPDF helpers (``pymupdf`` is a test oracle only, never imported by the package):
  :func:`render` and :func:`require_mupdf` skip the calling test without it,
  :func:`mupdf_warnings` then returns ``""`` (``pdf_info`` checks still run) and
  :func:`annot_types` falls back to gnnote's own parser, so CI without PyMuPDF still runs
  every pure-Python assertion.
"""
from __future__ import annotations

import random
import struct
import zlib
from typing import Dict, List, Optional, Sequence, Tuple

from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from gnnote.pdfutil import make_paper_pdf

# --------------------------------------------------------------------------- PDF building


def build_pdf(objects: Dict[int, bytes], root: int = 1, info: Optional[int] = None, version: str = "1.4",
              trailer_extra: bytes = b"") -> bytes:
    out = bytearray(f"%PDF-{version}\n%\xe2\xe3\xcf\xd3\n".encode("latin-1"))
    offsets: Dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for num in range(1, size):
        out += (b"%010d 00000 n \n" % offsets[num]) if num in offsets else b"0000000000 00001 f \n"
    trailer = b"<< /Size %d /Root %d 0 R" % (size, root)
    if info is not None:
        trailer += b" /Info %d 0 R" % info
    out += b"trailer\n" + trailer + b" " + trailer_extra + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def stream(data: bytes, extra: bytes = b"", compress: bool = False) -> bytes:
    if compress:
        data = zlib.compress(data)
        extra += b" /Filter /FlateDecode"
    return b"<< /Length %d %s >>\nstream\n" % (len(data), extra) + data + b"\nendstream"


def one_page_pdf(content: bytes, mediabox: Sequence[float] = (0, 0, 300, 400), rotate: int = 0,
                 annots: Sequence[bytes] = (), page_extra: bytes = b"", info: Optional[bytes] = None,
                 trailer_extra: bytes = b"") -> bytes:
    """A one-page PDF (Helvetica as /F1) whose annotation bodies become objects 10, 11, ..."""
    box = b" ".join(b"%g" % v for v in mediabox)
    refs = b" ".join(b"%d 0 R" % (10 + i) for i in range(len(annots)))
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [" + box + b"] /Rotate %d >>" % rotate,
        3: (b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> "
            + (b"/Annots [" + refs + b"] " if annots else b"") + page_extra + b" >>"),
        4: stream(content),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, body in enumerate(annots):
        objects[10 + i] = body
    if info is not None:
        objects[6] = info
    return build_pdf(objects, info=6 if info is not None else None, trailer_extra=trailer_extra)


# --------------------------------------------------------------------------- PNG / JPEG

_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _pack(samples: List[int], depth: int) -> bytes:
    if depth == 8:
        return bytes(samples)
    if depth == 16:
        return b"".join(struct.pack(">H", s) for s in samples)
    per = 8 // depth
    out = bytearray()
    for i in range(0, len(samples), per):
        b = 0
        for k, v in enumerate(samples[i:i + per]):
            b |= v << (8 - depth * (k + 1))
        out.append(b)
    return bytes(out)


def _filter(row: bytes, prev: bytes, bpp: int, ft: int) -> bytes:
    out = bytearray(len(row))
    for i in range(len(row)):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        if ft == 0:
            p = 0
        elif ft == 1:
            p = a
        elif ft == 2:
            p = b
        elif ft == 3:
            p = (a + b) >> 1
        else:
            pp = a + b - c
            pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
            p = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
        out[i] = (row[i] - p) & 255
    return bytes([ft]) + bytes(out)


def encode_png(width: int, height: int, ctype: int, depth: int, pixels: List[List[Tuple[int, ...]]],
               palette: Optional[bytes] = None, trns: Optional[bytes] = None, interlace: bool = False,
               seed: int = 1) -> bytes:
    """A PNG of ``pixels`` (rows of per-pixel sample tuples), filters chosen at random."""
    rnd = random.Random(seed)
    bpp = max(1, CHANNELS[ctype] * depth // 8)

    def rows_data(rows: List[List[Tuple[int, ...]]]) -> bytes:
        out = b""
        prev: Optional[bytes] = None
        for r in rows:
            packed = _pack([s for px in r for s in px], depth)
            if prev is None:
                prev = bytes(len(packed))
            out += _filter(packed, prev, bpp, rnd.randrange(5))
            prev = packed
        return out

    if not interlace:
        raw = rows_data(pixels)
    else:
        raw = b""
        for x0, y0, dx, dy in _ADAM7:
            rows = [[pixels[y][x] for x in range(x0, width, dx)] for y in range(y0, height, dy)]
            rows = [r for r in rows if r]
            if rows:
                raw += rows_data(rows)
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, ctype, 0, 0,
                                                             1 if interlace else 0))
    if palette is not None:
        out += _chunk(b"PLTE", bytes(palette))
    if trns is not None:
        out += _chunk(b"tRNS", bytes(trns))
    comp = zlib.compress(raw, 9)
    half = len(comp) // 2
    return out + _chunk(b"IDAT", comp[:half]) + _chunk(b"IDAT", comp[half:]) + _chunk(b"IEND", b"")


def random_pixels(width: int, height: int, ctype: int, depth: int, seed: int = 2) -> List[List[Tuple[int, ...]]]:
    rnd = random.Random(seed)
    top = (1 << depth) - 1
    return [[tuple(rnd.randint(0, top) for _ in range(CHANNELS[ctype])) for _ in range(width)]
            for _ in range(height)]


def with_exif_orientation(jpeg: bytes, orientation: int) -> bytes:
    """``jpeg`` with an APP1 Exif segment carrying only the orientation tag."""
    tiff = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1) + \
        struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack("<I", 0)
    app1 = b"Exif\x00\x00" + tiff
    return jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + jpeg[2:]


def jpeg_bytes(width: int, height: int, colorspace: str = "rgb", marker: bool = False,
               fill: Optional[int] = None) -> bytes:
    """A JPEG from PyMuPDF; ``marker`` paints the top-left quarter red (to see orientation),
    ``fill`` is the background sample value (default white)."""
    pymupdf = require_mupdf()

    cs = {"rgb": pymupdf.csRGB, "gray": pymupdf.csGRAY, "cmyk": pymupdf.csCMYK}[colorspace]
    pix = pymupdf.Pixmap(cs, pymupdf.IRect(0, 0, width, height), False)
    pix.clear_with(fill if fill is not None else (255 if colorspace != "cmyk" else 0))
    if marker:
        pix.set_rect(pymupdf.IRect(0, 0, width // 2, height // 2),
                     (255, 0, 0) if colorspace == "rgb" else ((60,) if colorspace == "gray" else (0, 255, 255, 0)))
    return pix.tobytes("jpg", jpg_quality=95)


# --------------------------------------------------------------------------- documents


def full_document(title: str = "Every element") -> Document:
    """One page per element family plus a mixed page: strokes (polyline, Bezier, variable
    width, highlighter, translucent, dot, fill), images (PNG, PNG with alpha, JPEG, PDF
    sticker), text boxes (runs, styles, alignment, rotation) on lined and PDF paper."""
    png = encode_png(8, 6, 2, 8, random_pixels(8, 6, 2, 8))
    rgba = encode_png(8, 6, 6, 8, random_pixels(8, 6, 6, 8, seed=5))
    sticker = make_paper_pdf(50, 40, "grid")
    pages = []
    ink = Page(455.04, 588.45, paper="lined")
    ink.strokes = [
        Stroke([Point(30, 30, 2), Point(90, 70, 2), Point(150, 30, 2)], width=2.0),
        Stroke([Point(40, 120, 1), Point(120, 140, 4), Point(200, 120, 1), Point(280, 150, 6)], width=2.0,
               color=(0.8, 0.1, 0.1, 1.0)),
        Stroke([Point(40, 220, 2), Point(240, 220, 2)], controls=[(Point(100, 170, 2), Point(180, 270, 2))],
               width=2.0, color=(0.0, 0.0, 1.0, 1.0)),
        Stroke([Point(40, 320, 8), Point(260, 320, 8)], color=(1.0, 0.9, 0.0, 0.5), kind="highlighter", width=8.0),
        Stroke([Point(40, 380, 1), Point(240, 380, 5)], controls=[(Point(100, 340, 2), Point(180, 420, 4))],
               width=3.0, color=(0.0, 0.5, 0.0, 0.7)),
        Stroke([Point(320, 60, 6)], width=6.0),
    ]
    poly = [Point(300, 450), Point(400, 450), Point(400, 540), Point(300, 540), Point(300, 450)]
    ink.strokes.append(Stroke(list(poly), color=(1.0, 0.0, 0.0, 0.1), kind="fill", width=0.0, outline=[poly]))
    pages.append(ink)
    media = Page(595.28, 841.89, paper="grid")
    media.images = [Image(40, 40, 160, 120, png, "png"), Image(240, 40, 160, 120, rgba, "png", rotation=30.0),
                    Image(40, 220, 100, 80, sticker, "pdf")]
    media.texts = [
        TextBox(40, 400, 300, 60, "Left aligned text that is long enough to wrap onto a second line.",
                runs=[TextRun("Left aligned ", bold=True, size=14), TextRun("text that is long enough ", italic=True, size=14,
                                                                           color=(0.0, 0.0, 0.8, 1.0)),
                      TextRun("to wrap onto a second line.", underline=True, size=14)], size=14),
        TextBox(40, 500, 300, 30, "Centred", size=16, align="center"),
        TextBox(40, 560, 300, 30, "Right", size=16, align="right", color=(0.5, 0.5, 0.5, 1.0)),
        TextBox(400, 600, 120, 30, "Rotated", size=12, rotation=90.0),
    ]
    pages.append(media)
    doc = Document(title=title, pages=pages)
    doc.pdfs["paper"] = make_paper_pdf(300, 200, "dotted")
    pages.append(Page(300, 200, background=PdfBackground("paper", 0), template_is_builtin=True,
                      strokes=[Stroke([Point(20, 20, 1.5), Point(280, 180, 1.5)], width=1.5)]))
    return doc


# --------------------------------------------------------------------------- PyMuPDF oracle


def mupdf():
    """The ``pymupdf`` module, or ``None`` when it is not installed."""
    try:
        import pymupdf  # type: ignore
    except ImportError:
        return None
    return pymupdf


def require_mupdf():
    """``pymupdf``, skipping the calling test when it is not installed."""
    import pytest

    return pytest.importorskip("pymupdf")


def annot_types(data: bytes) -> List[List[str]]:
    """Subtypes of every page's ``/Annots`` entries (links and widgets too), as MuPDF lists
    them -- or, without PyMuPDF, as gnnote's own parser does."""
    module = mupdf()
    if module is None:
        from gnnote.pdf.objects import PdfFile

        pdf = PdfFile(data)
        out = []
        for page in pdf.pages():
            annots = pdf.resolve(page.dict.get("Annots"))
            out.append([str(pdf.resolve(pdf.dict_of(a).get("Subtype"))) for a in annots]
                       if isinstance(annots, list) else [])
        return out
    doc = module.open(stream=data, filetype="pdf")
    out = []
    for page in doc:
        out.append([doc.xref_get_key(xref, "Subtype")[1].lstrip("/") for xref, _k, _i in page.annot_xrefs()])
    return out


def render(data: bytes, page: int = 0, zoom: float = 1.0, annots: bool = True):
    pymupdf = require_mupdf()

    doc = pymupdf.open(stream=data, filetype="pdf")
    return doc[page].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=annots, alpha=False)


def mupdf_warnings(data: bytes, dpi: int = 20, raster: bool = True) -> str:
    """Open ``data`` with MuPDF and interpret every page with its annotations -- rendered
    at ``dpi``, or (``raster=False``, much faster: images are not decoded) through MuPDF's
    bbox and text devices; the warnings MuPDF printed (``""`` without PyMuPDF)."""
    pymupdf = mupdf()
    if pymupdf is None:
        return ""
    pymupdf.TOOLS.mupdf_warnings(True)
    doc = pymupdf.open(stream=data, filetype="pdf")
    for page in doc:  # loading a page loads its annotations (page.annots() is slow for thousands)
        if raster:
            page.get_pixmap(dpi=dpi)
        else:
            page.get_bboxlog()
            page.get_text()
    return pymupdf.TOOLS.mupdf_warnings(True)


def ink_bbox(pix, threshold: int = 60) -> Optional[Tuple[int, int, int, int]]:
    """Pixel bbox ``(x0, y0, x1, y1)`` of the pixels with a channel darker than
    ``255 - threshold`` (ink on white); ``None`` when there are none."""
    n, w, h, stride = pix.n, pix.width, pix.height, pix.stride
    samples = pix.samples
    table = bytes(1 if v < 255 - threshold else 0 for v in range(256))
    x0, x1, y0, y1 = w, -1, None, None
    for y in range(h):
        row = samples[y * stride:y * stride + w * n].translate(table)
        i = row.find(1)
        if i < 0:
            continue
        j = row.rfind(1)
        x0, x1 = min(x0, i // n), max(x1, j // n)
        y0 = y if y0 is None else y0
        y1 = y
    if y0 is None:
        return None
    return x0, y0, x1 + 1, y1 + 1
