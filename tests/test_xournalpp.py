"""Xournal++ codec (``gnnote.xournalpp``): synthetic round trips of every element type,
Xournal++'s own test files (external test data, pinned in ``tests/conftest.py``), conversions
of the GoodNotes and Notability samples to ``.xopp`` and back, hardening and the registry,
sniffing and CLI plumbing."""
from __future__ import annotations

import base64
import gzip
import io
import math
import random
import re
import time
import zipfile
from pathlib import Path
from typing import Dict, Tuple

import pytest

from gnnote import __version__, formats
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format, to_document
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.goodnotes.writer import write_goodnotes
from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox
from gnnote.notability.reader import read_note
from gnnote.notability.writer import write_note
from gnnote.xournalpp import PACKAGE_MIMETYPE, PACKAGE_VERSION
from gnnote.xournalpp import reader as xr
from gnnote.xournalpp.reader import read_xopp
from gnnote.xournalpp.writer import write_xopp
from tests.sample_docs import BACKGROUND_PDF, JPEG, STICKER_PDF, counts, full_document, png, stats

TOL = 0.01  # pt


def _xy(points) -> list:
    """Flat [x0, y0, x1, y1, ...] (pytest.approx compares flat sequences only)."""
    return [c for p in points for c in ((p.x, p.y) if hasattr(p, "x") else p)]


def _xml(data: bytes) -> str:
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.read("content.xml").decode("utf-8")
    return gzip.decompress(data).decode("utf-8")


def _gz(xml: str) -> bytes:
    return gzip.compress(xml.encode("utf-8"), mtime=0)


def _doc(body: str, page: str = '<page width="600" height="800">', bg: str =
         '<background type="solid" color="#ffffffff" style="plain"/>') -> str:
    return (f'<?xml version="1.0" standalone="no"?>\n<xournal creator="test" fileversion="4">\n'
            f"<title>t</title>\n{page}\n{bg}\n<layer>\n{body}\n</layer>\n</page>\n</xournal>\n")


def _assert_bounded_and_writable(doc: Document) -> None:
    """Extreme input values end up inside the model's bounds, and every writer accepts them."""
    for page in doc.pages:
        for s in page.strokes:
            assert all(abs(p.x) <= 1e8 and abs(p.y) <= 1e8 and (p.width or 0) <= 1e4 for p in s.points)
        assert all(t.size <= 1e4 and abs(t.x) <= 1e8 and abs(t.y) <= 1e8 and math.isfinite(t.rotation) for t in page.texts)
        assert all(max(abs(i.x), abs(i.y), i.w, i.h) <= 1e8 and math.isfinite(i.rotation) for i in page.images)
    for fmt in formats.writable():
        assert fmt.write(doc, None)


# --------------------------------------------------------------------------- synthetic round trip


def test_every_element_type_round_trips():
    doc = full_document()
    data = write_xopp(doc)
    assert data[:2] == b"PK", "a document with a background PDF is written as the ZIP package"
    back = read_xopp(data)
    assert [(p.width, p.height) for p in back.pages] == [(p.width, p.height) for p in doc.pages]
    assert [p.paper for p in back.pages][0] == "lined" and back.pages[3].paper == "grid"
    p1, b1 = doc.pages[0], back.pages[0]
    # strokes: same order; the fill merged into its outline comes back right after it, the lone
    # fill (no matching outline) comes back as a fill alone
    assert [s.kind for s in b1.strokes] == [s.kind for s in p1.strokes]
    pressure = b1.strokes[0]
    src = p1.strokes[0].points
    assert _xy(pressure.points) == pytest.approx(_xy(src), abs=TOL)
    # every point keeps its width except the last, which repeats the last segment's width
    assert [p.width for p in pressure.points] == pytest.approx([p.width for p in src[:-1]] + [src[-2].width], abs=TOL)
    assert pressure.width == pytest.approx(1.5)
    assert pressure.color == pytest.approx(p1.strokes[0].color, abs=1 / 255)
    constant = b1.strokes[1]
    assert {round(p.width, 6) for p in constant.points} == {1.0}
    highlighter = b1.strokes[2]
    assert highlighter.kind == "highlighter" and highlighter.color[3] == pytest.approx(0x7F / 255)
    bezier = b1.strokes[3]
    assert bezier.controls is None and len(bezier.points) > 3
    for anchor in p1.strokes[3].points:  # the flattened chain passes through every anchor
        assert min(math.dist((anchor.x, anchor.y), (p.x, p.y)) for p in bezier.points) < TOL
    dot = b1.strokes[4]
    assert len(dot.points) == 2 and (dot.points[0].x, dot.points[0].y) == (dot.points[1].x, dot.points[1].y)
    outline, fill, lone = b1.strokes[6], b1.strokes[7], b1.strokes[8]
    assert outline.kind == "pen" and fill.kind == "fill" and lone.kind == "fill"
    assert fill.color == pytest.approx((0.2, 0.4, 0.9, 0.25), abs=1 / 255)
    assert lone.color == pytest.approx((0.9, 0.3, 0.1, 0.4), abs=1 / 255)
    assert _xy(lone.outline[0]) == pytest.approx([60, 300, 140, 300, 100, 360], abs=TOL)
    # images: PNG box, JPEG with its rotation, the PDF sticker as a teximage
    png_img, jpeg_img, sticker = b1.images
    assert png_img.fmt == "png" and (png_img.x, png_img.y, png_img.w, png_img.h) == pytest.approx((40, 400, 60, 40), abs=TOL)
    assert png_img.data == p1.images[0].data
    assert jpeg_img.fmt == "jpeg" and jpeg_img.data == JPEG and jpeg_img.rotation == pytest.approx(90.0, abs=1e-6)
    assert (jpeg_img.x, jpeg_img.y, jpeg_img.w, jpeg_img.h) == pytest.approx((150, 400, 64, 48), abs=TOL)
    assert sticker.fmt == "pdf" and sticker.data == STICKER_PDF
    assert (sticker.x, sticker.y, sticker.w, sticker.h) == pytest.approx((260, 400, 40, 30), abs=TOL)
    # text boxes
    plain, turned = b1.texts
    assert plain.text == "Hello <ink> & text" and (plain.x, plain.y) == pytest.approx((40, 480), abs=TOL)
    assert plain.size == pytest.approx(14.0)
    assert turned.text == "Turned\nand bold" and turned.rotation == pytest.approx(30.0, abs=1e-6)
    assert (turned.x, turned.y) == pytest.approx((260, 480), abs=TOL) and turned.align == "center"
    assert turned.runs[0].bold and turned.runs[0].font == "Helvetica"
    assert turned.color == pytest.approx((0.8, 0.0, 0.0, 1.0), abs=1 / 255)
    # PDF backgrounds: one attached PDF, page numbers kept
    assert [p.background.page_index if p.background else None for p in back.pages] == [None, 1, 0, None]
    assert back.pdfs[back.pages[1].background.pdf_id] == BACKGROUND_PDF
    assert not back.pages[1].template_is_builtin
    assert counts(back) == counts(doc)


def test_plain_document_is_gzip_xml_version_4():
    doc = Document(title="Plain", pages=[Page(300, 400, strokes=[Stroke([Point(1, 2, 1), Point(3, 4, 1)])])])
    data = write_xopp(doc)
    assert data[:2] == b"\x1f\x8b"
    xml = _xml(data)
    assert xml.startswith('<?xml version="1.0" standalone="no"?>\n')
    assert f'<xournal creator="gnnote {__version__}" fileversion="4">' in xml
    assert '<stroke tool="pen" color="#000000ff" width="1" capStyle="round">1 2 3 4</stroke>' in xml
    assert "<title>Plain</title>" in xml
    assert write_xopp(doc) == data, "gzip output is deterministic (mtime 0)"


def test_packaged_variant_mirrors_xournalpps_own_layout():
    data = write_xopp(full_document(), Options())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["mimetype", "META-INF/version", "content.xml", "attachments/bg.pdf"]
        assert zf.read("mimetype") == PACKAGE_MIMETYPE == b" application/xournal++\n"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert zf.read("META-INF/version") == PACKAGE_VERSION
        assert re.fullmatch(rb"current=(\d+?)(?:\n|\r\n)min=(\d+?)\n", zf.read("META-INF/version"))
        assert zf.read("attachments/bg.pdf") == BACKGROUND_PDF
        xml = zf.read("content.xml").decode()
    # domain / filename only on the first PDF page, as Xournal++ writes them
    assert '<background type="pdf" domain="attach" filename="attachments/bg.pdf" pageno="2"/>' in xml
    assert '<background type="pdf" pageno="1"/>' in xml
    options = Options()
    options.random_seed = 7  # type: ignore[attr-defined]
    assert write_xopp(full_document(), options) == write_xopp(full_document(), options)


def test_width_attribute_semantics():
    stroke = Stroke([Point(0, 0, 1.0), Point(10, 0, 2.0), Point(20, 0, 3.0)], width=2.5)
    xml = _xml(write_xopp(Document(pages=[Page(100, 100, strokes=[stroke])])))
    assert 'width="2.5 1 2"' in xml  # nominal, then one width per point except the last
    back = read_xopp(write_xopp(Document(pages=[Page(100, 100, strokes=[stroke])]))).pages[0].strokes[0]
    assert [p.width for p in back.points] == [1.0, 2.0, 2.0] and back.width == 2.5


def test_fill_merges_into_its_outline_or_stands_alone():
    doc = full_document()
    xml = _xml(write_xopp(doc))
    assert xml.count(" fill=") == 2
    assert re.search(r'<stroke tool="pen" color="#3366e6ff" width="1" fill="64" capStyle="round">', xml)
    assert re.search(r'<stroke tool="pen" color="#e64d1aff" width="0.1" fill="102" capStyle="round">', xml)
    assert any("hairline" in w for w in doc.warnings)


def test_second_background_pdf_gets_plain_paper():
    other = BACKGROUND_PDF.replace(b"612", b"600")
    doc = Document(pages=[Page(612, 792, background=PdfBackground("a.pdf", 0)),
                          Page(612, 792, background=PdfBackground("a.pdf", 1)),
                          Page(600, 792, background=PdfBackground("b.pdf", 0))],
                   pdfs={"a.pdf": BACKGROUND_PDF, "b.pdf": other})
    back = read_xopp(write_xopp(doc))
    assert [p.background is not None for p in back.pages] == [True, True, False]
    assert any("another background PDF" in w for w in doc.warnings)


def test_stock_paper_pdf_is_carried_only_in_pdf_mode():
    page = Page(612, 792, background=PdfBackground("paper.pdf", 0), paper="lined", template_is_builtin=True)
    doc = Document(pages=[page], pdfs={"paper.pdf": BACKGROUND_PDF})
    plain = write_xopp(doc, Options(paper="plain"))
    assert plain[:2] == b"\x1f\x8b" and 'style="ruled"' in _xml(plain)
    assert write_xopp(doc, Options(paper="pdf"))[:2] == b"PK"


def test_text_escaping_and_invalid_characters():
    box = TextBox(10, 10, 100, 20, 'a < b & "c"\x01\r\n', runs=[])
    back = read_xopp(write_xopp(Document(pages=[Page(200, 200, texts=[box])]))).pages[0].texts[0]
    assert back.text == 'a < b & "c"\r\n'


def test_long_text_gets_a_wrap_width():
    box = TextBox(10, 10, 80, 40, "a long line of text that a narrow box wrapped", size=12.0)
    xml = _xml(write_xopp(Document(pages=[Page(200, 200, texts=[box])])))
    assert 'wrap="80"' in xml
    back = read_xopp(write_xopp(Document(pages=[Page(200, 200, texts=[box])]))).pages[0].texts[0]
    assert back.w == pytest.approx(80.0)


def test_invalid_geometry_is_skipped_not_fatal():
    page = Page(float("nan"), 100, strokes=[Stroke([Point(float("inf"), 0, 1)]), Stroke([Point(0, 0, 1), Point(1, 1, 1)])],
                images=[Image(float("nan"), 0, 10, 10, png())], texts=[TextBox(float("nan"), 0, 10, 10, "x")])
    doc = Document(pages=[page])
    back = read_xopp(write_xopp(doc))
    assert len(back.pages[0].strokes) == 1 and not back.pages[0].images and not back.pages[0].texts
    assert any("invalid coordinates" in w for w in doc.warnings) and any("A4" in w for w in doc.warnings)
    empty = Document(pages=[])
    assert len(read_xopp(write_xopp(empty)).pages) == 1 and empty.warnings


# --------------------------------------------------------------------------- reader specifics


def test_reader_width_truncation_and_recovery():
    body = ('<stroke tool="pen" color="#000000ff" width="2 0.5 0.6">0 0 10 0 20 0 30 0</stroke>\n'
            '<stroke tool="pen" color="#000000ff" width="2 0.5 0 0.7 0.8 nan">0 0 1 0 2 0 3 0 4 0 5 0</stroke>\n'
            '<stroke tool="pen" color="#000000ff" width="2 -1 -1">0 0 1 0 2 0</stroke>\n'
            '<stroke tool="pen" color="#000000ff" width="2">5 5</stroke>')
    doc = read_xopp(_gz(_doc(body)))
    strokes = doc.pages[0].strokes
    # 4 points, 2 widths -> Xournal++ keeps 3 points
    assert [(p.x, p.width) for p in strokes[0].points] == [(0, 0.5), (10, 0.6), (20, 0.6)]
    # zero / NaN widths split the stroke: [0..2] and [2..5] minus the NaN tail
    assert [[p.x for p in s.points] for s in strokes[1:3]] == [[0, 1], [2, 3, 4]]
    assert len(strokes) == 3  # the all-negative stroke vanishes, the one-point stroke is skipped
    assert any("fewer widths" in w for w in doc.warnings) and any("fewer than two points" in w for w in doc.warnings)


def test_reader_colours_tools_layers_and_audio():
    body = ('<stroke tool="pen" color="blue" width="1">0 0 1 1</stroke>\n'
            '<stroke tool="highlighter" color="#ff000080" width="8" fn="rec.ogg" ts="5">0 0 9 9</stroke>\n'
            '<stroke tool="eraser" color="#ffffffff" width="8">0 0 9 9</stroke>\n'
            '<stroke tool="pen" color="#123456" width="1" style="dash">0 0 1 1</stroke>\n'
            '</layer><layer name="second"><text font="Times New Roman, Bold Italic" size="20" x="5" y="6" '
            'color="lightblue">hi</text>')
    doc = read_xopp(_gz(_doc(body)))
    s = doc.pages[0].strokes
    assert len(s) == 3
    assert s[0].color == pytest.approx((0x33 / 255, 0x33 / 255, 0xCC / 255, 1.0))
    assert s[1].kind == "highlighter" and s[1].color[3] == pytest.approx(0x80 / 255)
    assert s[2].color == pytest.approx((0x12 / 255, 0x34 / 255, 0x56 / 255, 1.0))
    t = doc.pages[0].texts[0]
    assert t.runs[0].font == "Times New Roman" and t.runs[0].bold and t.runs[0].italic and t.size == 20
    assert t.color == pytest.approx((0, 0xC0 / 255, 1, 1))
    joined = " | ".join(doc.warnings)
    for needle in ("eraser", "layers", "Audio", "drawn solid"):
        assert needle in joined


def test_reader_backgrounds():
    pages = ('<page width="200" height="300"><background type="solid" color="pink" style="isograph"/><layer/></page>'
             '<page width="200" height="300"><background type="pdf" domain="attach" filename="bg.pdf" pageno="1ll"/>'
             '<layer/></page>'
             '<page width="200" height="300"><background type="pixmap" domain="absolute" filename="/x.png"/><layer/>'
             '</page>')
    xml = f'<?xml version="1.0"?><xournal fileversion="4">{pages}</xournal>'
    doc = read_xopp(xml.encode())
    first = doc.pages[0]
    assert first.paper == "grid" and first.template_is_builtin and first.background is not None
    assert doc.pdfs[first.background.pdf_id].startswith(b"%PDF")  # pink paper as a generated PDF
    assert doc.pages[1].background is None and doc.pages[1].paper == "plain"
    joined = " | ".join(doc.warnings)
    assert "stored next to the Xournal++ file" in joined and "outside the file" in joined
    assert "isometric" in joined


def test_reader_file_version_5_matrices():
    body = ('<text font="Sans" size="10" matrix="0 2 -2 0 50 60" color="#000000ff">rot</text>\n'
            f'<image matrix="0 0.5 -0.5 0 100 100">{__import__("base64").b64encode(png(40, 20)).decode()}</image>')
    page = read_xopp(_gz(_doc(body))).pages[0]
    text, image = page.texts[0], page.images[0]
    assert (text.x, text.y, text.rotation, text.size) == pytest.approx((50, 60, 90, 20))
    # 40 x 20 px scaled by 0.5 and turned 90 degrees clockwise about the origin (100, 100)
    assert (image.w, image.h, image.rotation) == pytest.approx((20, 10, 90))
    assert (image.x + image.w / 2, image.y + image.h / 2) == pytest.approx((100 - 5, 100 + 10))


def test_reader_teximage_link_and_mrwriter_root():
    import base64
    payload = base64.b64encode(STICKER_PDF).decode()
    body = (f'<teximage text="x^2" left="10" top="20" right="50" bottom="50">{payload}</teximage>\n'
            '<link align="right" font="Sans" size="12" x="5" y="5" color="#ff00ffff" url="https://example.org">go</link>')
    doc = read_xopp(_gz(_doc(body).replace("<xournal", "<MrWriter").replace("</xournal>", "</MrWriter>")))
    image = doc.pages[0].images[0]
    assert image.fmt == "pdf" and (image.x, image.y, image.w, image.h) == pytest.approx((10, 20, 40, 30))
    assert doc.pages[0].texts[0].text == "go" and doc.pages[0].texts[0].align == "right"
    assert any("LaTeX" in w for w in doc.warnings) and any("links" in w for w in doc.warnings)


def test_reader_boilerplate_title_is_ignored():
    xml = _doc("").replace("<title>t</title>", "<title>Xournal++ document - see https://xournalpp.github.io/</title>")
    assert read_xopp(xml.encode()).title == "Untitled"
    assert read_xopp(_doc("").replace("<title>t</title>", "<title>My  notes</title>").encode()).title == "My notes"


# --------------------------------------------------------------------------- Xournal++'s own test files

# relative path under test/files -> (pages, ink strokes, fills, highlighters, images, texts, PDF pages),
# counted the way Xournal++ loads the file (eraser strokes dropped, invalid widths split, see
# docs/xournalpp.md section 2), checked by hand against the XML of the pinned commit.
XOPP_EXPECTED: Dict[str, Tuple[int, int, int, int, int, int, int]] = {
    "big-test.xoj": (200, 18517, 0, 0, 0, 0, 0),
    "load/.latex-fileversion-5.autosave.xopp": (1, 0, 0, 0, 2, 0, 0),
    "load/compatibility/stroke_line_return.xoj": (1, 1, 0, 0, 0, 0, 0),
    "load/image-fileversion-4.xopp": (1, 0, 0, 0, 1, 0, 0),
    "load/image-fileversion-5.xopp": (1, 0, 0, 0, 1, 0, 0),
    "load/latex-fileversion-4.xopp": (1, 0, 0, 0, 1, 0, 0),
    "load/latex-fileversion-5.xopp": (1, 0, 0, 0, 2, 0, 0),
    "load/layers.xopp": (1, 0, 0, 0, 0, 3, 0),
    "load/linebreaksLatex.xopp": (1, 0, 0, 0, 3, 0, 0),
    "load/links-fileversion-5.xopp": (1, 0, 0, 0, 0, 6, 0),
    "load/pages.xopp": (11, 0, 0, 0, 0, 11, 0),
    "load/relativePaths.xopp": (2, 0, 0, 0, 0, 4, 0),
    "load/strokes.xopp": (1, 11, 1, 4, 0, 0, 0),
    "load/text-fileversion-4.xopp": (1, 0, 0, 0, 0, 6, 0),
    "load/text-fileversion-5.xopp": (1, 0, 0, 0, 0, 11, 0),
    "packaged_xopp/audioAttachment/new.xopp": (1, 1, 0, 0, 0, 0, 0),
    "packaged_xopp/audioAttachment/old.xopp": (1, 1, 0, 0, 0, 0, 0),
    "packaged_xopp/big-test.xopp": (200, 18517, 0, 0, 0, 0, 0),
    "packaged_xopp/imgAttachment/doc_with_jpg.xopp": (1, 0, 0, 0, 1, 0, 0),
    "packaged_xopp/imgAttachment/new.xopp": (1, 0, 0, 0, 1, 0, 0),
    "packaged_xopp/imgAttachment/old.xopp": (1, 0, 0, 0, 1, 0, 0),
    "packaged_xopp/imgBackground/new.xopp": (1, 0, 0, 0, 1, 0, 0),
    "packaged_xopp/imgBackground/old.xopp": (1, 0, 0, 0, 0, 0, 0),
    "packaged_xopp/layer.xopp": (1, 0, 0, 0, 0, 3, 0),
    "packaged_xopp/pages.xopp": (6, 0, 0, 0, 1, 6, 0),
    "packaged_xopp/pdfBackground/new.xopp": (2, 0, 0, 0, 0, 0, 2),
    "packaged_xopp/pdfBackground/old.xopp": (2, 0, 0, 0, 0, 0, 0),
    "packaged_xopp/stroke/new.xopp": (1, 1, 0, 0, 0, 0, 0),
    "packaged_xopp/stroke/old.xopp": (1, 1, 0, 0, 0, 0, 0),
    "packaged_xopp/stroke/width_recovery.xopp": (1, 9, 0, 0, 0, 0, 0),
    "packaged_xopp/suite.xopp": (1, 7, 0, 2, 0, 1, 0),
    "packaged_xopp/suite_float_bw_compat.xopp": (1, 7, 0, 2, 0, 1, 0),
    "packaged_xopp/test.xopp": (1, 0, 0, 0, 0, 1, 0),
    "packaged_xopp/testPreview.xopp": (1, 1, 0, 0, 0, 0, 0),
    "packaged_xopp/testPreview2.xopp": (1, 1, 0, 0, 0, 0, 0),
    "packaged_xopp/text.xopp": (1, 0, 0, 0, 0, 3, 0),
    "pageTypeFormatCopy.xopp": (3, 0, 0, 0, 0, 3, 0),
    "preview-test-no-preview.unzipped.xoj": (1, 0, 0, 0, 0, 0, 0),
    "preview-test.unzipped.xoj": (1, 0, 0, 0, 0, 0, 0),
    "preview-test.xoj": (1, 0, 0, 0, 0, 0, 0),
    "preview-test2.xoj": (1, 1, 0, 0, 0, 0, 0),
    "test1.unzipped.xoj": (1, 0, 0, 0, 0, 1, 0),
    "test1.xoj": (1, 0, 0, 0, 0, 1, 0),
}
NOT_XOURNAL = {"preview-test-invalid.xoj"}  # an HTML page saved under a .xoj name


def _summary(doc: Document) -> Tuple[int, int, int, int, int, int, int]:
    strokes = [s for p in doc.pages for s in p.strokes]
    return (len(doc.pages), sum(1 for s in strokes if s.kind != "fill"), sum(1 for s in strokes if s.kind == "fill"),
            sum(1 for s in strokes if s.kind == "highlighter"), sum(len(p.images) for p in doc.pages),
            sum(len(p.texts) for p in doc.pages),
            sum(1 for p in doc.pages if p.background is not None and not p.template_is_builtin))


def test_xournalpp_sample_files(samples):
    files = samples.xournalpp_files()
    root = samples.repo("xournalpp") / "test" / "files"
    pinned = samples.at_pinned_commit("xournalpp") is not False
    checked = 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        if rel in NOT_XOURNAL:
            with pytest.raises(ValueError, match="not a Xournal"):
                read_xopp(path.read_bytes())
            continue
        doc = read_xopp(path.read_bytes())
        expected = XOPP_EXPECTED.get(rel) if pinned else None
        if expected is not None:
            assert _summary(doc) == expected, rel
            checked += 1
        for page in doc.pages:
            for stroke in page.strokes:
                assert len(stroke.points) >= 2 or stroke.kind == "fill", rel
                assert all(math.isfinite(p.x) and math.isfinite(p.y) and p.width > 0 for p in stroke.points
                           if stroke.kind != "fill"), rel
        # writing and reading back keeps every count (the two 6 MB stress files only read)
        if path.stat().st_size < 1_000_000:
            back = read_xopp(write_xopp(doc))
            assert _summary(back)[:6] == _summary(doc)[:6], rel
    if pinned:
        assert checked == len(XOPP_EXPECTED)


def test_sample_strokes_widths_and_text_details(samples):
    root = samples.repo("xournalpp") / "test" / "files"
    doc = read_xopp((root / "load" / "strokes.xopp").read_bytes())
    first = doc.pages[0].strokes[0]
    assert first.width == pytest.approx(2.26) and first.points[0].width == pytest.approx(0.41932136)
    assert first.color == pytest.approx((0x00 / 255, 0x2E / 255, 0x99 / 255, 1.0))
    assert doc.pages[0].paper == "grid"
    text = read_xopp((root / "load" / "text-fileversion-5.xopp").read_bytes()).pages[0].texts
    assert [t.text for t in text][:3] == ["red", "blue", "green"]
    assert (text[0].x, text[0].y) == pytest.approx((130.5, 96.75))
    aligned = [t.align for t in text]
    assert "center" in aligned and "right" in aligned
    pdf_doc = read_xopp((root / "packaged_xopp" / "pdfBackground" / "new.xopp").read_bytes())
    assert [p.background.page_index for p in pdf_doc.pages] == [0, 1]
    assert pdf_doc.pdfs[pdf_doc.pages[0].background.pdf_id] == \
        (root / "packaged_xopp" / "pdfBackground" / "old.xopp.bg.pdf").read_bytes()
    # the PDF survives a round trip through our own packaged output
    again = read_xopp(write_xopp(pdf_doc))
    assert [p.background.page_index for p in again.pages] == [0, 1]


# --------------------------------------------------------------------------- GoodNotes / Notability samples


def _cross_check(source: Document, back: Document) -> None:
    assert len(back.pages) == len(source.pages)
    for p_src, p_back in zip(source.pages, back.pages):
        assert len([s for s in p_back.strokes if s.kind != "fill"]) == len([s for s in p_src.strokes if s.kind != "fill"])
        assert len(p_back.images) == len(p_src.images)
        assert len(p_back.texts) == len([t for t in p_src.texts if (t.text or "").strip()])


def test_goodnotes_samples_to_xopp_and_back(samples):
    for path in samples.goodnotes_files():
        source = read_goodnotes(path.read_bytes())
        xopp = read_xopp(write_xopp(source))
        _cross_check(source, xopp)
        fills = sum(1 for p in source.pages for s in p.strokes if s.kind == "fill")
        assert sum(1 for p in xopp.pages for s in p.strokes if s.kind == "fill") == fills, path.name
        again = read_goodnotes(write_goodnotes(xopp))
        _cross_check(xopp, again)


def test_notability_samples_to_xopp_and_back(samples):
    for path in samples.note_files():
        source = read_note(path.read_bytes())
        xopp = read_xopp(write_xopp(source))
        _cross_check(source, xopp)
        first = next((s for p in source.pages for s in p.strokes), None)
        if first is not None:
            got = next(s for p in xopp.pages for s in p.strokes)
            assert (got.points[0].x, got.points[0].y) == pytest.approx((first.points[0].x, first.points[0].y), abs=TOL)
        again = read_note(write_note(xopp))
        assert stats(again)[:3] == stats(xopp)[:3], path.name


# --------------------------------------------------------------------------- hardening


def test_gzip_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(xr, "MAX_XML_BYTES", 100_000)
    bomb = gzip.compress(b"<xournal>" + b" " * 10_000_000, mtime=0)
    assert len(bomb) < 20_000
    t = time.monotonic()
    with pytest.raises(ValueError, match="inflates to more than"):
        read_xopp(bomb)
    assert time.monotonic() - t < 2.0


@pytest.mark.parametrize("prolog", [
    '<!DOCTYPE xournal [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>',
    '<!DOCTYPE xournal SYSTEM "file:///etc/passwd">',
    '<!DOCTYPE xournal [<!ENTITY % p SYSTEM "http://example.invalid/x"> %p;]>',
])
def test_dtds_and_entities_are_refused(prolog: str):
    xml = '<?xml version="1.0"?>' + prolog + _doc('<text x="1" y="1" size="12">&b;</text>').split("?>", 1)[1]
    t = time.monotonic()
    with pytest.raises(ValueError, match="document type or entity"):
        read_xopp(_gz(xml))
    assert time.monotonic() - t < 1.0


def test_nesting_and_element_limits(monkeypatch):
    deep = _doc("<a>" * 200 + "</a>" * 200)
    doc = read_xopp(deep.encode())
    assert any("nested deeper" in w for w in doc.warnings) and len(doc.pages) == 1
    monkeypatch.setattr(xr, "MAX_ELEMENTS", 50)
    many = _doc("\n".join('<stroke tool="pen" color="#000000ff" width="1">0 0 1 1</stroke>' for _ in range(200)))
    doc = read_xopp(many.encode())
    assert any("more than 50 XML elements" in w for w in doc.warnings)
    assert 0 < len(doc.pages[0].strokes) < 50


def test_point_limits(monkeypatch):
    monkeypatch.setattr(xr, "MAX_POINTS_PER_STROKE", 10)
    monkeypatch.setattr(xr, "MAX_TOTAL_POINTS", 25)
    stroke = '<stroke tool="pen" color="#000000ff" width="1">' + " ".join(f"{i} {i}" for i in range(20)) + "</stroke>"
    doc = read_xopp(_doc("\n".join([stroke] * 4)).encode())
    assert [len(s.points) for s in doc.pages[0].strokes] == [10, 10]
    assert any("per stroke" in w for w in doc.warnings) and any("per file" in w for w in doc.warnings)


def test_truncated_files_keep_what_was_read():
    xml = _doc('<stroke tool="pen" color="#000000ff" width="1">0 0 5 5</stroke>\n'
               '<stroke tool="pen" color="#000000ff" width="1">1 1 6 6</stroke>')
    cut = xml[: xml.index("1 1 6")]
    doc = read_xopp(cut.encode())
    assert len(doc.pages) == 1 and len(doc.pages[0].strokes) == 1
    assert any("damaged" in w for w in doc.warnings)
    gz = _gz(xml * 1)
    doc = read_xopp(gz[: len(gz) * 2 // 3])
    assert any("truncated" in w or "damaged" in w for w in doc.warnings)


def test_bytes_after_the_gzip_stream_are_ignored():
    xml = _doc('<stroke tool="pen" color="#000000ff" width="1">0 0 5 5</stroke>')
    for tail in (b"\x00" * 8, b"trailing garbage"):  # zlib's gzread ignores both
        doc = read_xopp(_gz(xml) + tail)
        assert len(doc.pages[0].strokes) == 1 and not doc.warnings
    doc = read_xopp(_gz(xml) + _gz(xml)[:12])  # a second gzip member that is cut off
    assert len(doc.pages[0].strokes) == 1 and any("truncated" in w for w in doc.warnings)


def test_extreme_values_stay_in_range():
    big = "1e300"
    image = base64.b64encode(png()).decode()
    body = (f'<stroke tool="pen" color="#000000ff" width="{big} 1 {big}">0 0 {big} 5 10 10 20 20</stroke>\n'
            f'<text font="Sans" size="{big}" x="5" y="5" color="#000000ff">big</text>\n'
            f'<text font="Sans" size="12" x="{big}" y="5" color="#000000ff">far</text>\n'
            f'<text font="Sans" size="12" x="5" y="5" matrix="{big} 0 0 {big} 5 5" color="#000000ff">scaled</text>\n'
            f'<image left="0" top="0" right="{big}" bottom="10">{image}</image>\n'
            f'<image left="0" top="0" right="10" bottom="10" matrix="{big} 0 0 1 0 0">{image}</image>')
    doc = read_xopp(_doc(body).encode())
    assert [t.text for t in doc.pages[0].texts] == ["big", "scaled"] and not doc.pages[0].images
    assert any("out of range" in w for w in doc.warnings)
    _assert_bounded_and_writable(doc)


def test_non_finite_rotations_are_written_unrotated():
    page = Page(200, 200, images=[Image(0, 0, 10, 10, png(), rotation=float("nan"))],
                texts=[TextBox(5, 5, 50, 10, "x", rotation=float("inf"))])
    xml = _xml(write_xopp(Document(pages=[page])))
    assert "nan" not in xml and "inf" not in xml and "matrix" not in xml


def test_zip_member_limit(monkeypatch):
    data = write_xopp(full_document())
    from gnnote import codecutil
    monkeypatch.setattr(codecutil, "MAX_TOTAL_BYTES", len(_xml(data)) + 10)
    doc = read_xopp(data)  # content.xml fits, the attached PDF does not
    assert all(p.background is None for p in doc.pages)
    assert any("missing from the package" in w for w in doc.warnings)
    assert any("inflates above" in w for w in doc.warnings)


@pytest.mark.parametrize("data", [b"", b"\x1f\x8b\x08", b"PK\x03\x04garbage", b"<html></html>", b"not xml",
                                  b"<xournal", "<?xml version='1.0'?><other/>".encode()])
def test_not_a_xournal_file(data: bytes):
    with pytest.raises(ValueError):
        read_xopp(data)


def test_garbage_attributes_are_tolerated():
    body = ('<stroke tool="laser" color="#zz" width="abc">0 0 1 1 2</stroke>\n'
            '<stroke tool="pen" color="#000000ff" width="1e309 1 1">0 0 1 1 nan 3 4 4</stroke>\n'
            '<text size="-3" x="x" y="1e999" color="nope">t</text>\n'
            '<image left="a" top="0" right="10" bottom="10">!!notbase64!!</image>\n'
            '<image left="0" top="0" right="10" bottom="10">R0lGODlhAQABAAAAACw=</image>')
    doc = read_xopp(_doc(body, page='<page width="-5" height="nan">').encode())
    page = doc.pages[0]
    assert (page.width, page.height) == pytest.approx((595.27559, 841.88976))
    assert len(page.strokes) == 2 and len(page.texts) == 1 and not page.images
    assert all(math.isfinite(p.x) for s in page.strokes for p in s.points)


def test_fuzzed_files_raise_only_value_error():
    seeds = [write_xopp(full_document()), write_xopp(Document(pages=full_document().pages[:1])),
             _doc('<stroke tool="pen" color="#000000ff" width="1 2 3">0 0 1 1 2 2</stroke>').encode()]
    rng = random.Random(1234)
    for seed in seeds:
        for _ in range(60):
            data = bytearray(seed)
            for _ in range(rng.randint(1, 12)):
                pos = rng.randrange(len(data))
                data[pos] = rng.randrange(256)
            if rng.random() < 0.3:
                data = data[: rng.randrange(1, len(data))]
            try:
                doc = read_xopp(bytes(data))
            except ValueError:
                continue
            assert isinstance(doc, Document)


# --------------------------------------------------------------------------- registry, sniffing, CLI


def test_registry_entry_and_sniffing():
    fmt = formats.get("xournalpp")
    assert (fmt.name, fmt.extension, fmt.input_extensions) == ("Xournal++", ".xopp", (".xopp", ".xoj"))
    gz = write_xopp(Document(pages=[Page(100, 100)]))
    packaged = write_xopp(full_document())
    plain = _doc("").encode()
    for data in (gz, packaged, plain):
        assert detect_format("renamed.bin", data) == "xournalpp"
    assert detect_format("x.xoj", b"garbage") == "xournalpp"  # by extension
    assert not fmt.sniff(b"<?xml version='1.0'?><svg/>", None)
    assert not fmt.sniff(b"\x1f\x8b\x08garbage", None)
    note = write_note(Document(pages=[Page(100, 100)]))
    goodnotes = write_goodnotes(Document(pages=[Page(100, 100)]))
    assert detect_format("x.note", note) == "notability" and detect_format("x.goodnotes", goodnotes) == "goodnotes"
    assert not fmt.sniff(note, formats.sniff_zip_names(note))
    assert not fmt.sniff(goodnotes, formats.sniff_zip_names(goodnotes))


def test_convert_api_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    doc = full_document()
    note = write_note(doc)
    result = convert(note, "Every.note", Options(target="xournalpp"))
    assert result.filename == "Every.xopp" and result.target_format == "xournalpp"
    assert to_document(result.data, result.filename).pages
    assert main(["formats"]) == 0
    listed = capsys.readouterr().out
    assert "xournalpp" in listed and "Xournal++" in listed and ".xopp, .xoj" in listed
    src = tmp_path / "Every.xopp"
    src.write_bytes(write_xopp(doc))
    assert main(["convert", str(src), "--to", "notability"]) == 0
    out = capsys.readouterr()
    assert "xournalpp -> notability" in out.out
    back = read_note((tmp_path / "Every.note").read_bytes())
    assert len(back.pages) == 4 and sum(len(p.strokes) for p in back.pages) >= 7
    assert main(["convert", str(src)]) == 0  # default target for other apps: Notability
    assert main(["info", str(src), "--json"]) == 0
    assert '"format": "xournalpp"' in capsys.readouterr().out
