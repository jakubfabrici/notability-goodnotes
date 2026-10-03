"""PDF reader (``gnnote.pdf.reader.read_pdf``): pages, annotations -> model, the stripped
background PDF, GoodNotes' own exports, damaged and encrypted input.

PyMuPDF is the oracle (annotation lists of the stripped background, renders); the tests skip
without it.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import List

import pytest

from gnnote.convert import Options, convert, detect_format
from gnnote.model import Document, Page
from gnnote.pdf.objects import PdfFile
from gnnote.pdf.reader import read_pdf
from gnnote.pdf.writer import write_pdf
from gnnote.pdfutil import make_paper_pdf, pdf_info

from tests.test_pdf_helpers import (annot_types, build_pdf, ink_bbox, mupdf_warnings, one_page_pdf, render,
                                    require_mupdf, stream)

_annot_types = annot_types


def _background(doc: Document) -> bytes:
    return doc.pdfs[doc.pages[0].background.pdf_id]


# --------------------------------------------------------------------------- errors


def test_not_a_pdf_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="not a PDF"):
        read_pdf(b"hello world")
    with pytest.raises(ValueError, match="no page"):
        read_pdf(b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF")
    with pytest.raises(TypeError):
        read_pdf("not bytes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        convert(b"%PDF-1.4 garbage", "x.pdf")


def test_damaged_files_never_raise_beyond_value_error() -> None:
    ink = (b"<< /Type /Annot /Subtype /Ink /Rect [0 0 100 100] /InkList [[10 10 50 50 90 10]] "
           b"/BS << /W 2 >> /C [1 0 0] /Popup 11 0 R >>")
    popup = b"<< /Type /Annot /Subtype /Popup /Rect [100 100 200 200] /Parent 10 0 R >>"
    good = one_page_pdf(b"0 0 1 rg 10 10 50 50 re f", annots=[ink, popup])
    variants = {
        "no startxref": good.replace(b"startxref", b"startxxxx"),
        "offsets shifted": good[:9] + b"%junk junk junk\n" + good[9:],
        "truncated tail": good[: good.rfind(b"startxref")],
        "junk before header": b"GARBAGE\n" + good,
    }
    for label, data in variants.items():
        doc = read_pdf(data)
        assert len(doc.pages) == 1, label
        assert len(doc.pages[0].strokes) == 1, label
        stripped = _background(doc)
        assert pdf_info(stripped).pages and _annot_types(stripped) == [[]], label
        assert mupdf_warnings(stripped) == "", label
    for cut in range(10, len(good), 97):  # every truncation either reads or is a ValueError
        try:
            read_pdf(good[:cut])
        except ValueError:
            pass


# --------------------------------------------------------------------------- page geometry


def test_pages_sizes_title_and_background() -> None:
    info = b"<< /Title <FEFF0160006B00FA0161006B0061> /Producer (x) >>"
    data = one_page_pdf(b"", mediabox=(10, 20, 310, 420), rotate=90, info=info)
    doc = read_pdf(data)
    assert doc.title == "Škúška" and doc.source_format == "pdf"
    page = doc.pages[0]
    assert (page.width, page.height) == (400, 300)  # rotated MediaBox
    assert page.background is not None and page.background.page_index == 0
    assert not page.template_is_builtin and page.paper == "plain"
    assert doc.pdfs[page.background.pdf_id] == data  # nothing to strip: bytes untouched
    assert read_pdf(make_paper_pdf(100, 50)).title == "PDF"
    assert detect_format("x.bin", data) == "pdf"


# --------------------------------------------------------------------------- annotation kinds


def _ink(points: List[float], extra: bytes = b"") -> bytes:
    nums = b" ".join(b"%g" % v for v in points)
    return (b"<< /Type /Annot /Subtype /Ink /Rect [0 0 300 400] /InkList [[" + nums + b"]] /F 4 "
            + extra + b" >>")


def test_every_converted_annotation_kind() -> None:
    annots = [
        _ink([10, 390, 50, 350, 90, 390], b"/BS << /W 3 >> /C [0 0 1]"),                       # 10 ink
        b"<< /Type /Annot /Subtype /Line /Rect [0 0 300 400] /L [20 20 120 60] /BS << /W 2 >> "
        b"/C [1 0 0] /LE [/OpenArrow /None] >>",                                               # 11 line
        b"<< /Type /Annot /Subtype /PolyLine /Rect [0 0 300 400] /Vertices [10 100 60 150 110 100] "
        b"/Border [0 0 4] /C [0 1 0] >>",                                                      # 12 polyline
        b"<< /Type /Annot /Subtype /Polygon /Rect [0 0 300 400] /Vertices [150 100 250 100 200 180] "
        b"/C [0.5] /IC [1 1 0] /CA 0.8 >>",                                                     # 13 polygon + fill
        b"<< /Type /Annot /Subtype /Square /Rect [20 200 120 260] /BS << /W 4 >> /C [0 0 0 1] "
        b"/RD [2 2 2 2] >>",                                                                   # 14 square (CMYK black)
        b"<< /Type /Annot /Subtype /Circle /Rect [150 200 250 300] /BS << /W 2 /S /D >> /C [1 0 0] >>",  # 15 circle
        b"<< /Type /Annot /Subtype /FreeText /Rect [20 300 220 340] /Contents (Hello\\rworld) "
        b"/DA (/Helv 14 Tf 1 0 0 rg) /Q 1 >>",                                                 # 16 free text
        b"<< /Type /Annot /Subtype /Highlight /Rect [0 0 300 400] /C [1 1 0] "
        b"/QuadPoints [100 30 200 30 100 18 200 18] >>",                                        # 17 highlight
        b"<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] /A << /S /URI /URI (https://x.y) >> >>",  # 18 link
        b"<< /Type /Annot /Subtype /Text /Rect [280 380 300 400] /Contents (note) /Popup 20 0 R >>",  # 19 note
        b"<< /Type /Annot /Subtype /Popup /Rect [200 200 300 300] /Parent 19 0 R >>",           # 20 its popup
        b"<< /Type /Annot /Subtype /Popup /Rect [200 200 300 300] /Parent 10 0 R >>",           # 21 ink's popup
        _ink([1, 1, 2, 2], b"/F 2"),                                                           # 22 hidden ink
    ]
    data = one_page_pdf(b"", annots=annots)
    doc = read_pdf(data)
    page = doc.pages[0]
    kinds = [(s.kind, len(s.points)) for s in page.strokes]
    assert kinds == [("pen", 3), ("pen", 2), ("pen", 3), ("pen", 4), ("fill", 4), ("pen", 5), ("pen", 65),
                     ("highlighter", 2)]
    ink, line, poly, polygon, fill, square, circle, highlight = page.strokes
    assert [(p.x, p.y) for p in ink.points] == [(10, 10), (50, 50), (90, 10)]  # top-left origin
    assert ink.width == 3 and ink.color == (0, 0, 1, 1) and ink.controls is None
    assert [(p.x, p.y) for p in line.points] == [(20, 380), (120, 340)] and line.color == (1, 0, 0, 1)
    assert line.controls is not None  # exact straight Bezier, so writers keep it straight
    assert poly.width == 4 and poly.color == (0, 1, 0, 1)
    assert polygon.color == (0.5, 0.5, 0.5, 0.8) and (polygon.points[0].x, polygon.points[0].y) == \
        (polygon.points[-1].x, polygon.points[-1].y)
    assert fill.color == (1, 1, 0, 0.8) and fill.outline is not None and len(fill.outline[0]) == 4
    # square: Rect minus RD, inset by half the width
    xs, ys = [p.x for p in square.points], [p.y for p in square.points]
    assert (min(xs), max(xs), min(ys), max(ys)) == (24, 116, 144, 196) and square.color == (0, 0, 0, 1)
    cx = [p.x for p in circle.points]
    assert min(cx) == pytest.approx(151) and max(cx) == pytest.approx(249) and circle.controls is None
    assert highlight.width == pytest.approx(12) and highlight.color == (1, 1, 0, 0.5)
    assert [(round(p.x, 3), round(p.y, 3)) for p in highlight.points] == [(106, 376), (194, 376)]
    text = page.texts[0]
    assert (text.text, text.size, text.color, text.align) == ("Hello\nworld", 14, (1, 0, 0, 1), "center")
    assert (text.x, text.y, text.w, text.h) == (20, 60, 200, 40)
    warnings = " ".join(doc.warnings)
    assert "line endings" in warnings and "dashed" in warnings
    assert "2 PDF annotations (Ink, Text) stay part of the PDF pages" in doc.warnings
    # the converted annotations and the ink's popup are gone; link, note, its popup and the
    # hidden ink stay
    stripped = _background(doc)
    assert sorted(_annot_types(stripped)[0]) == ["Ink", "Link", "Popup", "Text"]
    assert mupdf_warnings(stripped) == ""
    assert stripped.startswith(data)  # an incremental update


def test_annotation_colours_alpha_and_highlighter_detection() -> None:
    ap_body = b"/G0 gs 1 0 0 RG 10 w 10 10 m 90 10 l S"
    ap = stream(ap_body, b"/Type /XObject /Subtype /Form /BBox [0 0 300 400] "
                         b"/Resources << /ExtGState << /G0 << /CA 0.5 /BM /Multiply >> >> >>")
    annots = [
        _ink([10, 10, 90, 10], b"/C [0.5] /CA 0.4"),                         # alpha 0.4 -> highlighter
        _ink([10, 20, 90, 20], b"/C [] /CA 0.9"),                            # empty colour -> black, pen
        _ink([10, 30, 90, 30], b"/C [0 1 1 0] /AP << /N 15 0 R >>"),          # CMYK, alpha from the appearance
        _ink([10, 40, 90, 40]),                                              # no colour: black
    ]
    objects_extra = {15: ap}
    data = one_page_pdf(b"", annots=annots)
    # splice object 15 in by rebuilding with the extra object
    doc_objects = _objects_of(data)
    doc_objects.update(objects_extra)
    data = build_pdf(doc_objects)
    page = read_pdf(data).pages[0]
    s0, s1, s2, s3 = page.strokes
    assert s0.kind == "highlighter" and s0.color == (0.5, 0.5, 0.5, 0.4)
    assert s1.kind == "pen" and s1.color == (0, 0, 0, 0.9)
    assert s2.kind == "highlighter" and s2.color == (1, 0, 0, 0.5)  # /CA .5 + /BM /Multiply in the AP
    assert s3.kind == "pen" and s3.color == (0, 0, 0, 1) and s3.width == 1


def _objects_of(data: bytes):
    out = {}
    for m in re.finditer(rb"(\d+) 0 obj\n(.*?)\nendobj\n", data, re.S):
        out[int(m.group(1))] = m.group(2)
    return out


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_rotated_pages_and_offset_mediabox(rotate: int) -> None:
    """The stroke lands where viewers show the annotation (MuPDF renders the original)."""
    box = (40, 60, 340, 460)
    ap = stream(b"0 0 1 RG 6 w 1 J 100 400 m 200 350 l 250 420 l S",
                b"/Type /XObject /Subtype /Form /BBox [40 60 340 460]")
    annot = (b"<< /Type /Annot /Subtype /Ink /Rect [40 60 340 460] /InkList [[100 400 200 350 250 420]] "
             b"/BS << /W 6 >> /C [0 0 1] /F 4 /AP << /N 11 0 R >> >>")
    data = one_page_pdf(b"", mediabox=box, rotate=rotate, annots=[annot, ap])
    doc = read_pdf(data)
    page = doc.pages[0]
    assert len(page.strokes) == 1
    expected = ink_bbox(render(data, zoom=2))
    redrawn = Document(pages=[Page(page.width, page.height, strokes=page.strokes)])
    got = ink_bbox(render(write_pdf(redrawn, Options()), zoom=2))
    assert expected is not None and got is not None
    assert all(abs(a - b) <= 2 for a, b in zip(expected, got)), (rotate, expected, got)
    assert _annot_types(_background(doc)) == [[]]


def test_free_text_on_a_rotated_page_rotates_with_it() -> None:
    annot = (b"<< /Type /Annot /Subtype /FreeText /Rect [100 100 200 130] /Contents (Turn) "
             b"/DA (0 0 1 rg /Helv 10 Tf) >>")
    doc = read_pdf(one_page_pdf(b"", mediabox=(0, 0, 300, 400), rotate=90, annots=[annot]))
    text = doc.pages[0].texts[0]
    assert text.rotation == 90.0 and text.size == 10 and text.color == (0, 0, 1, 1)
    assert (text.x, text.y) == pytest.approx((130, 100))  # the box's top-left corner as displayed


def test_encrypted_pdf_is_kept_untouched() -> None:
    data = one_page_pdf(b"", annots=[_ink([1, 1, 50, 50])], trailer_extra=b"/Encrypt << /Filter /Standard /V 1 >>")
    doc = read_pdf(data)
    assert len(doc.pages) == 1 and doc.pages[0].strokes == []
    assert _background(doc) == data
    assert any("encrypted" in w for w in doc.warnings)


def test_xref_stream_files_get_an_xref_stream_update(tmp_path: Path) -> None:
    pymupdf = require_mupdf()
    src = pymupdf.open()
    for i in range(3):
        page = src.new_page(width=300, height=400)
        page.insert_text((50, 50 + i * 10), f"page {i + 1}")
        annot = page.add_ink_annot([[(20, 300), (80, 250), (140, 320)]])
        annot.set_border(width=3)
        annot.set_colors(stroke=(1, 0, 0))
        annot.update()
        page.add_text_annot((200, 200), "keep me")
    data = src.tobytes(garbage=4, deflate=True, use_objstms=1)
    assert b"/ObjStm" in data and b"/XRef" in data
    doc = read_pdf(data)
    assert [len(p.strokes) for p in doc.pages] == [1, 1, 1]
    assert doc.pages[0].strokes[0].color[:3] == (1, 0, 0) and doc.pages[0].strokes[0].width == 3
    stripped = _background(doc)
    assert stripped.startswith(data)
    tail = stripped[len(data):]
    assert b"/Type /XRef" in tail and b"\nxref\n" not in tail  # same kind of section as before
    assert _annot_types(stripped) == [["Text", "Popup"]] * 3  # the note keeps its own popup
    assert mupdf_warnings(stripped) == ""
    assert len(pdf_info(stripped).pages) == 3


def test_damaged_xref_is_rewritten_instead() -> None:
    data = one_page_pdf(b"0 g 10 10 20 20 re f", annots=[_ink([10, 10, 100, 100]), _ink([5, 5, 6, 6], b"/F 2")])
    broken = data.replace(b"startxref", b"startxxxx")
    doc = read_pdf(broken)
    stripped = _background(doc)
    assert not stripped.startswith(broken)  # a fresh file, not an update
    assert _annot_types(stripped) == [["Ink"]]  # only the hidden one is left
    assert PdfFile(stripped).healthy() and mupdf_warnings(stripped) == ""
    assert render(stripped).pixel(20, 380) == (0, 0, 0)  # the page content survived


def test_inherited_annots_array_and_shared_annotations() -> None:
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
        3: b"<< /Type /Page /Parent 2 0 R /Annots 6 0 R >>",
        4: b"<< /Type /Page /Parent 2 0 R /Annots [7 0 R 8 0 R] >>",
        6: b"[7 0 R]",
        7: b"<< /Type /Annot /Subtype /Ink /Rect [0 0 200 200] /InkList [[1 1 100 100]] >>",
        8: b"<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] >>",
    }
    data = build_pdf(objects)
    doc = read_pdf(data)
    assert [len(p.strokes) for p in doc.pages] == [1, 1]
    assert _annot_types(_background(doc)) == [[], ["Link"]]


# --------------------------------------------------------------------------- GoodNotes exports

EXPORT_INK = {  # Ink annotations per page in GoodNotes' exports (PyMuPDF counts)
    "Test4": [0, 0], "Test5": [0, 14, 27], "Test6": [2, 1, 3, 0, 1], "Test7": [14, 4, 0, 5],
    "Test8": [2, 2, 0, 1], "Test9": [0, 102, 0, 11, 0, 22, 6],
}


def _export(samples, name: str) -> bytes:
    path = samples.repo("goodparse") / "samples" / f"{name}.pdf"
    if not path.is_file():
        pytest.skip(f"{name}.pdf export not available")
    return path.read_bytes()


@pytest.mark.parametrize("name", sorted(EXPORT_INK))
def test_goodnotes_exports_read_back(samples, name: str) -> None:
    data = _export(samples, name)
    doc = read_pdf(data)
    info = pdf_info(data)
    assert [(p.width, p.height) for p in doc.pages] == [(p.width, p.height) for p in info.pages]
    original = _annot_types(data)
    ink_counts = [t.count("Ink") for t in original]
    expected = samples.expected_for(Path(f"{name}.pdf"), EXPORT_INK)
    if expected is not None:
        assert ink_counts == expected
    # every ink annotation is one stroke; everything else stays in the PDF
    assert [len(p.strokes) for p in doc.pages] == ink_counts
    stripped = _background(doc)
    after = _annot_types(stripped)
    assert all("Ink" not in t and "FreeText" not in t for t in after)
    assert [sorted(t for t in a if t not in ("Ink", "FreeText")) for a in original] == [sorted(a) for a in after]
    assert mupdf_warnings(stripped, raster=False) == ""
    if ink_counts and any(ink_counts):
        assert stripped.startswith(data)
    for page in doc.pages:
        for s in page.strokes:
            assert s.width > 0 and len(s.points) >= 1
            assert all(-5 <= p.x <= page.width + 5 and -5 <= p.y <= page.height + 5 for p in s.points)


def test_goodnotes_export_details(samples) -> None:
    """Test9: highlighters (GoodNotes keeps /CA and /BM /Multiply in the appearance), shapes
    with their exact curves from the appearance, fountain-pen widths from the filled outline,
    the three text boxes with the size the appearance shows."""
    doc = read_pdf(_export(samples, "Test9"))
    page = doc.pages[1]
    kinds = [s.kind for s in page.strokes]
    assert kinds.count("highlighter") == 38 and kinds.count("pen") == 64  # as in Test9.goodnotes
    assert all(s.color[3] == 0.5 for s in page.strokes if s.kind == "highlighter")
    curved = [s for s in page.strokes if s.controls is not None]
    assert len(curved) >= 40  # ellipses and smoothed strokes keep their Bezier handles
    texts = doc.pages[2].texts
    assert [t.text for t in texts] == ["S U C H", "GOOD", "Hallo Franz…,!,,!,"]
    assert [round(t.size, 2) for t in texts] == [13.09, 30.0, 13.09]
    assert "PDF annotations (Stamp, Widget) stay part of the PDF pages" in doc.warnings[-1]


def test_goodnotes_export_widths_match_the_notebook(samples) -> None:
    """Strokes read from Test5.pdf have the widths GoodNotes' own file stores for them."""
    from gnnote.goodnotes.reader import read_goodnotes

    folder = samples.repo("goodparse") / "samples"
    if not (folder / "Test5.goodnotes").is_file():
        pytest.skip("Test5.goodnotes missing")
    pdf_doc = read_pdf(_export(samples, "Test5"))
    gn = read_goodnotes((folder / "Test5.goodnotes").read_bytes())
    ratios = []
    for pdf_page, gn_page in zip(pdf_doc.pages, gn.pages):
        for s in gn_page.strokes:
            if s.kind == "fill" or s.pen == "pencil" or not pdf_page.strokes:
                continue
            first = (s.points[0].x, s.points[0].y)
            best = min(pdf_page.strokes, key=lambda t: math.dist((t.points[0].x, t.points[0].y), first))
            if math.dist((best.points[0].x, best.points[0].y), first) > 2:
                continue
            widths = sorted(p.width for p in s.points)
            ratios.append(best.width / widths[len(widths) // 2])
    assert len(ratios) >= 30
    ratios.sort()
    assert 0.9 <= ratios[len(ratios) // 2] <= 1.1 and ratios[0] > 0.6 and ratios[-1] < 1.5


def test_loose_sample_pdfs_with_ink(samples) -> None:
    """Other Ink-annotated PDFs in the reference corpus (when present) convert and strip."""
    root = Path(samples.root)
    files = [p for p in sorted(root.rglob("*.pdf")) if p.is_file() and p.stat().st_size < 20_000_000]
    checked = 0
    for path in files:
        data = path.read_bytes()
        if b"/Ink" not in data:
            continue
        doc = read_pdf(data)
        types = _annot_types(_background(doc))
        assert all("Ink" not in t for t in types), path
        assert sum(len(p.strokes) for p in doc.pages) >= sum(t.count("Ink") for t in _annot_types(data)), path
        checked += 1
    if not checked:
        pytest.skip("no Ink-annotated sample PDFs")
