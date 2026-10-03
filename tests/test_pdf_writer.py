"""PDF writer (``gnnote.pdf.writer.write_pdf``): structure, ink in both modes, images, text,
imported backgrounds, every sample file.

PyMuPDF is the oracle (renders, text extraction, warnings); the tests skip without it.
"""
from __future__ import annotations

import re
import struct
import time
import zipfile
from pathlib import Path
from typing import List, Tuple

import pytest

from gnnote import __version__, formats
from gnnote.convert import Options, convert, detect_format, to_document
from gnnote.geometry import flatten_bezier
from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from gnnote.pdf.images import decode_png, png_image
from gnnote.pdf.objects import PdfFile
from gnnote.pdf.writer import write_pdf
from gnnote.pdfutil import make_paper_pdf, pdf_info

from tests.test_pdf_helpers import (build_pdf, encode_png, full_document, ink_bbox, jpeg_bytes, mupdf_warnings,
                                    one_page_pdf, random_pixels, render, require_mupdf, stream,
                                    with_exif_orientation)

FLAT = Options(pdf_ink="flatten")
ANNOT = Options(pdf_ink="annotations")


def _open(data: bytes):
    return require_mupdf().open(stream=data, filetype="pdf")


# --------------------------------------------------------------------------- registry / options


def test_pdf_is_a_registered_format() -> None:
    fmt = formats.get("pdf")
    assert (fmt.name, fmt.extension, fmt.input_extensions) == ("PDF", ".pdf", (".pdf",))
    assert fmt.readable and fmt.writable
    assert formats.default_target("pdf") == "notability"
    assert detect_format("x.bin", make_paper_pdf(100, 100)) == "pdf"
    assert detect_format("junk-first.dat", b"\0" * 500 + b"%PDF-1.4\n") == "pdf"
    assert not fmt.sniff(b"%PDF-1.4", ["stored.pdf"])  # a ZIP is never a PDF
    damaged_zip = b"PK\x03\x04" + bytes(26) + b"attachments/A" + make_paper_pdf(10, 10)
    assert detect_format("broken.goodnotes", damaged_zip) == "goodnotes"  # not taken for a PDF
    assert detect_format("x.pdf", b"not really") == "pdf"  # the extension decides when content does not
    with pytest.raises(ValueError, match="pdf_ink"):
        Options(pdf_ink="raster").validate()
    with pytest.raises(ValueError, match="pdf_ink"):
        write_pdf(Document(pages=[Page(100, 100)]), Options(pdf_ink="raster"))


# --------------------------------------------------------------------------- file structure


def test_structure_metadata_and_xref_offsets() -> None:
    doc = full_document("Skúška – тест")
    data = write_pdf(doc, FLAT)
    assert data.startswith(b"%PDF-1.7\n") and data.rstrip().endswith(b"%%EOF")
    # every xref entry points exactly at its object header
    xref_pos = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    assert data[xref_pos:xref_pos + 4] == b"xref"
    m = re.match(rb"xref\n0 (\d+)\n", data[xref_pos:])
    count = int(m.group(1))
    table = data[xref_pos + m.end():xref_pos + m.end() + 20 * count]
    for num in range(1, count):
        entry = table[20 * num:20 * num + 20]
        assert len(entry) == 20 and entry.endswith(b" n \n")
        offset = int(entry[:10])
        assert data[offset:].startswith(b"%d 0 obj\n" % num), num
    info = pdf_info(data)
    assert [(p.width, p.height) for p in info.pages] == [(455.04, 588.45), (595.28, 841.89), (300.0, 200.0)]
    assert info.producer == f"gnnote {__version__}" and info.warnings == []
    pdf = _open(data)
    assert pdf.metadata["title"] == "Skúška – тест" and pdf.metadata["format"] == "PDF 1.7"
    assert b"/Title <FEFF" in data  # UTF-16BE with BOM for a non-ASCII title
    assert mupdf_warnings(data) == ""
    assert write_pdf(full_document("Skúška – тест"), FLAT) == data  # deterministic
    ascii_doc = Document(title="Plain title", pages=[Page(100, 100)])
    assert b"/Title (Plain title)" in write_pdf(ascii_doc, FLAT)


def test_content_streams_are_flate_compressed() -> None:
    data = write_pdf(full_document(), FLAT)
    pdf = PdfFile(data)
    for page in pdf.pages():
        contents = pdf.resolve(page.dict["Contents"])
        assert contents.dict.get("Filter") == "FlateDecode"


def test_empty_and_damaged_models_still_give_a_valid_pdf() -> None:
    empty = Document(title="", pages=[])
    data = write_pdf(empty, FLAT)
    assert len(pdf_info(data).pages) == 1 and "no pages" in " ".join(empty.warnings)
    page = Page(float("nan"), -5)
    page.strokes = [Stroke([Point(float("nan"), 1, 1)]), Stroke([Point(1, 2, float("inf")), Point(3, 4, 0)]),
                    Stroke([], kind="fill")]
    page.images = [Image(0, 0, 10, 10, b"GIF89a....", "png"), Image(0, 0, 10, 10, b"", "png"),
                   Image(0, 0, 10, 10, b"\x89PNG\r\n\x1a\nbroken", "png"), Image(float("nan"), 0, 1, 1, b"x")]
    page.texts = [TextBox(float("nan"), 0, 10, 10, "x"), TextBox(0, 0, 0, 0, ""),
                  TextBox(5, 5, -3, 10, "neg\twidth\x00", size=float("nan"))]
    doc = Document(pages=[page])
    for options in (FLAT, ANNOT):
        data = write_pdf(doc, options)
        assert mupdf_warnings(data) == ""
        assert (pdf_info(data).pages[0].width, pdf_info(data).pages[0].height) == (612.0, 792.0)
    warnings = " ".join(doc.warnings)
    assert "no valid size" in warnings and "neither PNG, JPEG nor PDF" in warnings
    assert "could not be embedded" in warnings and "without data" in warnings
    with pytest.raises(TypeError):
        write_pdf("not a document")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- ink


def _curve_points(stroke: Stroke) -> List[Point]:
    if stroke.controls:
        return flatten_bezier(stroke.points, stroke.controls, 0.5)
    return list(stroke.points)


STROKES = {
    "polyline": Stroke([Point(60, 60, 3), Point(160, 140, 3), Point(260, 70, 3)], width=3.0),
    "bezier": Stroke([Point(50, 200, 2), Point(250, 210, 2)], controls=[(Point(100, 120, 2), Point(200, 300, 2))],
                     width=2.0, color=(0.0, 0.0, 1.0, 1.0)),
    "variable": Stroke([Point(40, 100, 1), Point(120, 160, 7), Point(200, 100, 2), Point(280, 170, 9)], width=3.0,
                       color=(0.7, 0.0, 0.0, 1.0)),
    "variable_bezier": Stroke([Point(40, 250, 1), Point(260, 230, 8)],
                              controls=[(Point(110, 120, 3), Point(190, 330, 6))], width=3.0),
    "highlighter": Stroke([Point(50, 150, 14), Point(250, 160, 14)], color=(1.0, 0.85, 0.0, 0.5),
                          kind="highlighter", width=14.0),
    "translucent_variable": Stroke([Point(40, 80, 2), Point(140, 200, 10), Point(260, 90, 4)], width=4.0,
                                   color=(0.0, 0.6, 0.0, 0.6)),
    "dot": Stroke([Point(150, 150, 12)], width=12.0),
}


@pytest.mark.parametrize("mode", ["flatten", "annotations"])
@pytest.mark.parametrize("name", sorted(STROKES))
def test_strokes_land_where_the_model_says(name: str, mode: str) -> None:
    stroke = STROKES[name]
    doc = Document(pages=[Page(320, 360, strokes=[stroke])])
    data = write_pdf(doc, Options(pdf_ink=mode))
    assert mupdf_warnings(data) == ""
    zoom = 2.0
    box = ink_bbox(render(data, zoom=zoom), threshold=40)
    assert box is not None
    pts = _curve_points(stroke)
    expect = (min(p.x - p.width / 2 for p in pts), min(p.y - p.width / 2 for p in pts),
              max(p.x + p.width / 2 for p in pts), max(p.y + p.width / 2 for p in pts))
    got = tuple(v / zoom for v in box)
    assert all(abs(a - b) <= 1.0 for a, b in zip(got, expect)), (name, got, expect)
    annots = list(_open(data)[0].annots())
    assert len(annots) == (1 if mode == "annotations" else 0)


def test_both_modes_render_the_same_page() -> None:
    doc = full_document()
    flat, annotated = write_pdf(doc, FLAT), write_pdf(doc, ANNOT)
    for index in range(3):
        a, b = render(flat, index), render(annotated, index)
        different = sum(1 for x, y in zip(a.samples, b.samples) if abs(x - y) > 8)
        assert different <= len(a.samples) // 2000, (index, different)
    # annotations mode: one /Ink annotation per non-fill stroke, fills stay in the content
    pdf = _open(annotated)
    ink_count = sum(1 for s in doc.pages[0].strokes if s.kind != "fill")
    assert [a.type[1] for a in pdf[0].annots()] == ["Ink"] * ink_count
    assert list(_open(flat)[0].annots()) == []


def test_ink_annotation_dictionary() -> None:
    stroke = Stroke([Point(10, 20, 2), Point(30, 40, 4), Point(50, 20, 2)], color=(1.0, 0.5, 0.0, 0.5),
                    kind="highlighter", width=3.0)
    data = write_pdf(Document(pages=[Page(100, 100, strokes=[stroke])]), ANNOT)
    pdf = PdfFile(data)
    page = pdf.pages()[0]
    annot = pdf.resolve(pdf.resolve(page.dict["Annots"])[0])
    assert annot["Subtype"] == "Ink" and annot["F"] == 4 and annot["CA"] == 0.5
    assert pdf.resolve(annot["P"]) is page.dict
    assert pdf.numbers(annot["C"]) == [1.0, 0.5, 0.0]
    assert pdf.dict_of(annot["BS"])["W"] == 2.0  # the median width
    ink = pdf.numbers(pdf.resolve(annot["InkList"])[0])
    assert ink == [10, 80, 30, 60, 50, 80]  # default user space (y up)
    ap = pdf.resolve(pdf.dict_of(annot["AP"])["N"])
    gstates = pdf.dict_of(pdf.dict_of(ap.dict["Resources"])["ExtGState"])
    assert any(pdf.dict_of(g).get("BM") == "Multiply" and pdf.dict_of(g).get("CA") == 0.5 for g in gstates.values())


def test_highlighter_multiplies_and_translucent_strokes_do_not_darken_overlaps() -> None:
    doc = Document(pages=[Page(200, 100)])
    page = doc.pages[0]
    page.strokes.append(Stroke([Point(20, 50, 30), Point(180, 50, 30)], color=(0.0, 0.0, 0.0, 1.0), width=30))
    page.strokes.append(Stroke([Point(100, 10, 20), Point(100, 90, 20)], color=(1.0, 1.0, 0.0, 1.0),
                               kind="highlighter", width=20))
    # variable width, translucent, folding back over itself
    page.strokes.append(Stroke([Point(20, 85, 4), Point(60, 85, 8), Point(30, 85, 4)], width=6,
                               color=(0.0, 0.0, 1.0, 0.5)))
    for options in (FLAT, ANNOT):
        pix = render(write_pdf(doc, options))
        assert pix.pixel(100, 50) == (0, 0, 0)  # multiply keeps black text black under the highlighter
        r, g, b = pix.pixel(100, 20)
        assert r > 240 and g > 240 and 100 < b < 160  # yellow at half strength over white
        # the overlapping pieces of the translucent stroke have one uniform tone
        tones = {pix.pixel(x, 85) for x in range(32, 50)}
        assert len(tones) == 1 and list(tones)[0][2] > 200, tones


def test_fill_uses_its_alpha() -> None:
    ring = [Point(10, 10), Point(90, 10), Point(90, 90), Point(10, 90), Point(10, 10)]
    doc = Document(pages=[Page(100, 100, strokes=[Stroke(list(ring), color=(1.0, 0.0, 0.0, 0.2), kind="fill",
                                                          width=0.0, outline=[ring])])])
    for options in (FLAT, ANNOT):
        data = write_pdf(doc, options)
        r, g, b = render(data).pixel(50, 50)
        assert r == 255 and 195 < g < 210 and g == b
        assert list(_open(data)[0].annots()) == []


# --------------------------------------------------------------------------- text


def _spans(data: bytes, page: int = 0) -> List[dict]:
    out = []
    for block in _open(data)[page].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                span["dir"] = line["dir"]
                out.append(span)
    return out


def test_windows_1252_text_uses_helvetica_and_extracts_exactly() -> None:
    text = "Grüße aus Köln – « café », 5 € … naïve Œuvre"
    doc = Document(pages=[Page(595, 842, texts=[TextBox(40, 60, 520, 40, text, size=12)])])
    data = write_pdf(doc, FLAT)
    page = _open(data)[0]
    assert page.get_text().strip() == text
    assert [(f[2], f[3]) for f in page.get_fonts()] == [("Type1", "Helvetica")]  # nothing embedded
    assert mupdf_warnings(data) == "" and doc.warnings == []


def test_slovak_text_extracts_exactly() -> None:
    # c, d, l, n and t with caron are not in Windows-1252: the Unicode font is embedded
    text = "Žluťoučký kôň úpěl ďábelské ódy – ľúbezné šťavnaté „úvodzovky“ ÄÔŔĹ"
    doc = Document(pages=[Page(595, 842, texts=[TextBox(40, 60, 520, 40, text, size=12)])])
    data = write_pdf(doc, FLAT)
    page = _open(data)[0]
    assert page.get_text().strip() == text
    assert [f[2] for f in page.get_fonts()] == ["Type0"]
    assert mupdf_warnings(data) == "" and doc.warnings == []


def test_ukrainian_text_embeds_the_unicode_font() -> None:
    uk = "Привіт, ґанок! Їжак і єнот — «Україна» № 5"
    sk = "Ďakujem, ľúbim ťa"
    doc = Document(pages=[Page(595, 842, texts=[TextBox(40, 60, 520, 30, uk, size=14),
                                                TextBox(40, 120, 520, 30, sk, size=14, align="right")])])
    data = write_pdf(doc, FLAT)
    page = _open(data)[0]
    lines = [line.strip() for line in page.get_text().splitlines() if line.strip()]
    assert lines == [uk, sk]
    fonts = page.get_fonts()
    assert len(fonts) == 1 and fonts[0][2] == "Type0" and fonts[0][3].endswith("+DejaVuSans")
    assert fonts[0][1] == "ttf"  # embedded font program
    spans = _spans(data)
    assert {s["font"].split("+")[-1] for s in spans} == {"DejaVuSans"}
    assert mupdf_warnings(data) == "" and doc.warnings == []
    # the embedded program only carries the glyphs the document uses
    assert len(data) < 60_000


def test_characters_outside_the_font_become_question_marks_with_one_warning() -> None:
    doc = Document(pages=[Page(300, 100, texts=[TextBox(10, 10, 280, 30, "Ahoj 世界 😀", size=12)])])
    data = write_pdf(doc, FLAT)
    assert _open(data)[0].get_text().strip() == "Ahoj ?? ?"
    assert doc.warnings == ["3 characters are missing from the PDF font and were printed as '?'"]


def test_text_layout_alignment_wrapping_styles_and_rotation() -> None:
    boxes = [TextBox(50, 50, 200, 40, "left", size=20),
             TextBox(50, 150, 200, 40, "centre", size=20, align="center"),
             TextBox(50, 250, 200, 40, "right", size=20, align="right"),
             TextBox(300, 50, 60, 200, "one two three four five six seven", size=12),
             TextBox(450, 300, 200, 40, "down", size=20, rotation=90.0)]
    boxes.append(TextBox(50, 400, 300, 40, "bold italic plain", runs=[TextRun("bold ", bold=True, size=16),
                                                                      TextRun("italic ", italic=True, size=16),
                                                                      TextRun("plain", size=24, underline=True)],
                         size=16))
    data = write_pdf(Document(pages=[Page(700, 600, texts=boxes)]), FLAT)
    spans = {s["text"].strip(): s for s in _spans(data)}
    # first baseline 0.952 em below the box top
    left = spans["left"]
    assert abs(left["origin"][0] - 50) < 0.5 and abs(left["origin"][1] - (50 + 0.952 * 20)) < 0.5
    centre = spans["centre"]
    assert abs((centre["bbox"][0] + centre["bbox"][2]) / 2 - 150) < 1.0
    right = spans["right"]
    assert abs(right["bbox"][2] - 250) < 1.0
    wrapped = [s for s in _spans(data) if s["origin"][0] < 310 and s["origin"][0] > 290]
    assert [s["text"].strip() for s in wrapped] == ["one two", "three four", "five six", "seven"]
    assert all(s["bbox"][2] <= 360 + 1 for s in wrapped)
    down = spans["down"]
    assert down["dir"] == pytest.approx((0.0, 1.0), abs=1e-6)  # rotated 90 degrees clockwise
    assert abs(down["origin"][0] - (450 - 0.952 * 20)) < 0.5 and abs(down["origin"][1] - 300) < 0.5
    assert spans["plain"]["size"] == pytest.approx(24, abs=0.1)
    styled = render(data, zoom=2)
    assert ink_bbox(styled) is not None


# --------------------------------------------------------------------------- images


def _reference_image_pdf(image: bytes, rect: Tuple[float, float, float, float], size=(200, 200)) -> bytes:
    pymupdf = require_mupdf()
    ref = pymupdf.open()
    page = ref.new_page(width=size[0], height=size[1])
    page.insert_image(pymupdf.Rect(*rect), stream=image, keep_proportion=False)
    return ref.tobytes()


PNG_CASES = [(0, 1, False), (0, 4, True), (0, 8, False), (0, 16, True), (2, 8, False), (2, 16, False),
             (2, 8, True), (3, 2, False), (3, 8, False), (3, 4, True), (4, 8, False), (4, 16, True),
             (6, 8, False), (6, 16, False), (6, 8, True)]


@pytest.mark.parametrize("ctype,depth,interlace", PNG_CASES)
def test_png_images_render_like_mupdfs_own_import(ctype: int, depth: int, interlace: bool) -> None:
    w, h = 11, 7
    palette = trns = None
    if ctype == 3:
        n = 1 << depth
        palette = bytes((i * 53 + c * 91) % 256 for i in range(n) for c in range(3))
        trns = bytes((i * 77) % 256 for i in range(max(1, n // 2)))
    png = encode_png(w, h, ctype, depth, random_pixels(w, h, ctype, depth, seed=depth + ctype),
                     palette=palette, trns=trns, interlace=interlace)
    rect = (20, 30, 20 + w * 10, 30 + h * 10)
    doc = Document(pages=[Page(200, 200, images=[Image(20, 30, w * 10, h * 10, png, "png")])])
    ours = render(write_pdf(doc, FLAT))
    theirs = render(_reference_image_pdf(png, rect))
    for y in range(h):
        for x in range(w):
            px, py = 25 + 10 * x, 35 + 10 * y
            a, b = ours.pixel(px, py), theirs.pixel(px, py)
            assert max(abs(i - j) for i, j in zip(a, b)) <= 3, (x, y, a, b)


def test_png_colour_key_transparency() -> None:
    pixels = [[(0, 0, 255) if (x + y) % 2 else (255, 0, 0) for x in range(6)] for y in range(4)]
    png = encode_png(6, 4, 2, 8, pixels, trns=struct.pack(">HHH", 255, 0, 0))
    image = png_image(png)
    assert image.dict["Mask"] == [255, 255, 0, 0, 0, 0] and image.smask is None
    page = Page(100, 100, images=[Image(0, 0, 60, 40, png, "png")])
    pix = render(write_pdf(Document(pages=[page]), FLAT))
    assert pix.pixel(5, 5) == (255, 255, 255)  # red pixels are transparent
    assert pix.pixel(15, 5) == (0, 0, 255)


def test_damaged_png_is_salvaged_instead_of_passed_through() -> None:
    png = encode_png(40, 30, 2, 8, random_pixels(40, 30, 2, 8))
    start = png.index(b"IDAT") - 4
    length = struct.unpack(">I", png[start:start + 4])[0]
    damaged = (png[:start] + struct.pack(">I", length // 2) + b"IDAT" + png[start + 8:start + 8 + length // 2]
               + b"\0\0\0\0" + png[png.index(b"IEND") - 4:])
    assert "DecodeParms" not in png_image(damaged).dict  # decoded, not copied
    doc = Document(pages=[Page(100, 100, images=[Image(0, 0, 80, 60, damaged)])])
    assert mupdf_warnings(write_pdf(doc, FLAT)) == "" and doc.warnings == []


def test_png_paths_avoid_unfiltering_where_possible() -> None:
    rgba = encode_png(9, 5, 6, 8, random_pixels(9, 5, 6, 8))
    img = png_image(rgba)
    assert img.dict["DecodeParms"]["Colors"] == 3 and img.smask[0]["DecodeParms"]["Colors"] == 1
    rgb = encode_png(9, 5, 2, 8, random_pixels(9, 5, 2, 8))
    passthrough = png_image(rgb)
    assert passthrough.dict["DecodeParms"] == {"Predictor": 15, "Colors": 3, "BitsPerComponent": 8, "Columns": 9}
    pal = encode_png(9, 5, 3, 4, random_pixels(9, 5, 3, 4), palette=bytes(range(48)))
    indexed = png_image(pal)
    assert indexed.dict["ColorSpace"][0] == "Indexed" and indexed.smask is None


def test_png_decoder_matches_mupdf() -> None:
    pymupdf = require_mupdf()
    for ctype, depth, interlace in PNG_CASES:
        w, h = 13, 9
        palette = trns = None
        if ctype == 3:
            palette = bytes((i * 7) % 256 for i in range(3 << depth))
            trns = bytes([0, 128, 255])
        png = encode_png(w, h, ctype, depth, random_pixels(w, h, ctype, depth), palette=palette, trns=trns,
                         interlace=interlace)
        width, height, channels, bits, samples = decode_png(png)
        if bits == 16:
            samples = samples[0::2]
        mine = bytearray(samples)
        if channels in (2, 4):  # MuPDF keeps pixmaps premultiplied
            for i in range(0, len(mine), channels):
                a = mine[i + channels - 1]
                for k in range(channels - 1):
                    mine[i + k] = (mine[i + k] * a + 127) // 255
        ref = pymupdf.Pixmap(png)
        assert (width, height, channels) == (ref.width, ref.height, ref.n)
        assert max(abs(a - b) for a, b in zip(mine, ref.samples)) <= 1, (ctype, depth, interlace)


def test_large_rgba_png_is_fast() -> None:
    pymupdf = require_mupdf()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 2000, 1500), True)
    pix.clear_with(0)
    for i in range(0, 2000, 50):
        pix.set_rect(pymupdf.IRect(i, (i * 7) % 1500, i + 40, (i * 7) % 1500 + 300), (i % 256, 90, 200, 180))
    png = pix.tobytes("png")
    doc = Document(pages=[Page(800, 600, images=[Image(0, 0, 800, 600, png, "png")])])
    start = time.perf_counter()
    data = write_pdf(doc, FLAT)
    assert time.perf_counter() - start < 5.0
    assert mupdf_warnings(data) == ""


@pytest.mark.parametrize("colorspace", ["rgb", "gray", "cmyk"])
def test_jpeg_passthrough(colorspace: str) -> None:
    jpeg = jpeg_bytes(40, 20, colorspace, marker=True)
    doc = Document(pages=[Page(200, 200, images=[Image(20, 20, 160, 80, jpeg, "jpeg")])])
    data = write_pdf(doc, FLAT)
    assert jpeg in data  # passed through untouched
    ours, theirs = render(data), render(_reference_image_pdf(jpeg, (20, 20, 180, 100)))
    for px, py in ((40, 40), (140, 40), (40, 90), (140, 90)):
        a, b = ours.pixel(px, py), theirs.pixel(px, py)
        assert max(abs(i - j) for i, j in zip(a, b)) <= 8, (colorspace, px, py, a, b)
    if colorspace == "cmyk":
        assert b"/Decode [1 0 1 0 1 0 1 0]" in data


def _red_quadrant(pix, box: Tuple[float, float, float, float]) -> str:
    x, y, w, h = box
    quads = {"top-left": (x + w / 4, y + h / 4), "top-right": (x + 3 * w / 4, y + h / 4),
             "bottom-left": (x + w / 4, y + 3 * h / 4), "bottom-right": (x + 3 * w / 4, y + 3 * h / 4)}
    red = [name for name, (px, py) in quads.items()
           if pix.pixel(int(px), int(py))[0] > 180 and max(pix.pixel(int(px), int(py))[1:]) < 100]
    assert len(red) == 1, red
    return red[0]


@pytest.mark.parametrize("orientation,rotation,corner", [(6, 90.0, "top-right"), (8, 270.0, "bottom-left"),
                                                         (3, 180.0, "bottom-right"), (1, 0.0, "top-left")])
def test_exif_photos_are_upright(orientation: int, rotation: float, corner: str) -> None:
    """GoodNotes' convention: the box is the displayed box and ``rotation`` turns the raw
    pixels into it -- a raw landscape photo with EXIF 6 fills a portrait box upright."""
    raw = with_exif_orientation(jpeg_bytes(64, 32, "rgb", marker=True, fill=96), orientation)
    portrait = orientation in (6, 8)
    box = (50.0, 40.0, 64.0 if portrait else 128.0, 128.0 if portrait else 64.0)
    doc = Document(pages=[Page(250, 250, images=[Image(*box, raw, "jpeg", rotation=rotation)])])
    pix = render(write_pdf(doc, FLAT))
    assert _red_quadrant(pix, box) == corner
    bbox = ink_bbox(pix, threshold=10)
    assert bbox is not None and all(abs(a - b) <= 1.5 for a, b in
                                    zip(bbox, (box[0], box[1], box[0] + box[2], box[1] + box[3])))


def test_non_exif_rotation_turns_the_box_about_its_centre() -> None:
    png = encode_png(4, 2, 2, 8, [[(0, 0, 0)] * 4] * 2)
    doc = Document(pages=[Page(300, 300, images=[Image(100, 125, 100, 50, png, "png", rotation=90.0)])])
    bbox = ink_bbox(render(write_pdf(doc, FLAT)))
    assert bbox is not None and all(abs(a - b) <= 1.5 for a, b in zip(bbox, (125, 100, 175, 200)))


def test_pdf_sticker_image_fills_its_box() -> None:
    sticker = make_paper_pdf(40, 20, "plain", color=(0.0, 0.0, 0.0))
    doc = Document(pages=[Page(200, 200, images=[Image(30, 40, 100, 60, sticker, "pdf")])])
    data = write_pdf(doc, FLAT)
    bbox = ink_bbox(render(data))
    assert bbox is not None and all(abs(a - b) <= 1 for a, b in zip(bbox, (30, 40, 130, 100)))


def test_identical_images_are_embedded_once() -> None:
    png = encode_png(5, 5, 2, 8, random_pixels(5, 5, 2, 8))
    page = Page(200, 200, images=[Image(0, 0, 50, 50, png), Image(100, 100, 50, 50, png)])
    data = write_pdf(Document(pages=[page, Page(200, 200, images=[Image(0, 0, 20, 20, png)])]), FLAT)
    assert data.count(b"/Subtype /Image") == 1


# --------------------------------------------------------------------------- backgrounds


def _rotated_source(rotate: int, box=(50, 100, 350, 500), with_annot: bool = False) -> bytes:
    x0, y0, x1, y1 = box
    content = (b"1 0 0 rg %g %g 40 40 re f 0 0 1 rg %g %g %g 15 re f 0 g BT /F1 24 Tf %g %g Td (Hi) Tj ET"
               % (x0 + 10, y1 - 50, x0, y0, x1 - x0, x0 + 60, y0 + 60))
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [%g %g %g %g] /Rotate %d "
            b"/Resources << /Font << /F1 5 0 R >> >> >>" % (x0, y0, x1, y1, rotate)),
        3: b"<< /Type /Page /Parent 2 0 R /Contents [4 0 R 6 0 R]" + (b" /Annots [7 0 R 9 0 R]" if with_annot else b"")
           + b" >>",
        # two content streams, split between tokens (PDF 32000-1 section 7.8.2)
        4: stream(content[:content.index(b" 0 0 1 rg")]),
        6: stream(content[content.index(b" 0 0 1 rg"):], compress=True),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    if with_annot:
        ap = b"0 1 0 rg 0 0 30 20 re f"
        objects[7] = (b"<< /Type /Annot /Subtype /Square /Rect [%g %g %g %g] /F 4 /AP << /N 8 0 R >> >>"
                      % (x0 + 150, y0 + 150, x0 + 210, y0 + 190))
        objects[8] = stream(ap, b"/Type /XObject /Subtype /Form /BBox [0 0 30 20]")
        objects[9] = (b"<< /Type /Annot /Subtype /Square /Rect [%g %g %g %g] /F 2 /AP << /N 8 0 R >> >>"
                      % (x0 + 20, y0 + 150, x0 + 80, y0 + 190))  # hidden: not drawn
    return build_pdf(objects)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_background_honours_rotate_and_mediabox_origin(rotate: int) -> None:
    src = _rotated_source(rotate, with_annot=True)
    info = pdf_info(src).pages[0]
    doc = Document(pages=[Page(info.width, info.height, background=PdfBackground("src", 0))], pdfs={"src": src})
    out = write_pdf(doc, FLAT)
    assert mupdf_warnings(out) == "" and doc.warnings == []
    a, b = render(src), render(out)
    assert (a.width, a.height) == (b.width, b.height)
    assert sum(1 for x, y in zip(a.samples, b.samples) if abs(x - y) > 40) == 0


def test_background_scales_to_the_model_page_and_shares_resources() -> None:
    src = _rotated_source(0)
    doc = Document(pages=[Page(600, 800, background=PdfBackground("src", 0)),
                          Page(600, 800, background=PdfBackground("same bytes", 0))],
                   pdfs={"src": src, "same bytes": bytes(src)})
    out = write_pdf(doc, FLAT)
    assert out.count(b"/BaseFont /Helvetica") == 1  # the source font is copied once
    assert out.count(b"/Subtype /Form") == 1  # and the page form is shared, even across pdf ids
    big, small = render(out), render(src)
    # the red square near the source's top-left corner scales with the page (factor 2)
    assert big.pixel(2 * 20 + 20, 2 * 10 + 20)[0] > 200 and big.pixel(2 * 20 + 20, 2 * 10 + 20)[1] < 50
    assert small.pixel(30, 30)[1] < 50


@pytest.mark.parametrize("label,data", [
    ("encrypted", one_page_pdf(b"0 0 1 rg 0 0 300 400 re f", trailer_extra=b"/Encrypt << /Filter /Standard >>")),
    ("garbage", b"%PDF-1.4\nthis is not a pdf at all"),
    ("not a pdf", b"hello"),
])
def test_unreadable_backgrounds_are_left_blank_with_a_warning(label: str, data: bytes) -> None:
    doc = Document(pages=[Page(300, 400, background=PdfBackground("bad", 0),
                               strokes=[Stroke([Point(10, 10, 2), Point(50, 50, 2)], width=2)])], pdfs={"bad": data})
    out = write_pdf(doc, FLAT)
    assert mupdf_warnings(out) == ""
    assert any("left out" in w or "blank" in w for w in doc.warnings), doc.warnings
    pix = render(out)
    assert pix.pixel(150, 300) == (255, 255, 255)  # no background drawn
    assert pix.pixel(30, 30) == (0, 0, 0)  # the ink is still there


def test_missing_background_and_page_index_warn() -> None:
    doc = Document(pages=[Page(100, 100, background=PdfBackground("gone", 0)),
                          Page(100, 100, background=PdfBackground("paper", 3))],
                   pdfs={"paper": make_paper_pdf(100, 100)})
    write_pdf(doc, FLAT)
    joined = " ".join(doc.warnings)
    assert "'gone' is missing" in joined and "PDF page 4 does not exist" in joined


def test_paper_styles_without_background_are_drawn() -> None:
    lined = write_pdf(Document(pages=[Page(200, 200, paper="lined")]), FLAT)
    plain = write_pdf(Document(pages=[Page(200, 200, paper="plain")]), FLAT)
    assert ink_bbox(render(lined, zoom=2), threshold=5) is not None
    assert ink_bbox(render(plain, zoom=2), threshold=5) is None


def _sample_attachment_pdfs(samples) -> List[Tuple[str, bytes]]:
    items: List[Tuple[str, bytes]] = []
    try:
        files = list(samples.goodnotes_files())
    except pytest.skip.Exception:
        files = []
    try:
        files += list(samples.note_files())
    except pytest.skip.Exception:
        pass
    for path in files:
        try:
            z = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            continue
        with z:
            for name in z.namelist():
                if name.endswith("/"):
                    continue
                data = z.read(name)
                if data.startswith(b"%PDF"):
                    items.append((f"{path.name}:{name.rsplit('/', 1)[-1]}", data))
    if not items:
        pytest.skip("no sample PDF attachments available")
    return items


def test_every_sample_pdf_attachment_imports_like_mupdf_shows_it(samples) -> None:
    """Every PDF inside the GoodNotes samples (catalogue papers, Test9's Figma export, Excel
    form, photo strip, die-cut sticker) and the .note samples, imported as a background, renders
    like the source page (first three pages of each)."""
    seen = set()
    for name, data in _sample_attachment_pdfs(samples):
        key = (len(data), hash(data))
        if key in seen:
            continue
        seen.add(key)
        info = pdf_info(data)
        indices = list(range(min(3, len(info.pages))))
        doc = Document(pages=[Page(info.pages[i].width, info.pages[i].height, background=PdfBackground("a", i))
                              for i in indices], pdfs={"a": data})
        out = write_pdf(doc, FLAT)
        assert doc.warnings == [], name
        assert mupdf_warnings(out, dpi=10) == "", name
        for i in indices:
            a, b = render(data, i, zoom=0.5), render(out, i, zoom=0.5)
            assert (a.width, a.height) == (b.width, b.height), name
            off = sum(1 for x, y in zip(a.samples, b.samples) if abs(x - y) > 32)
            assert off <= len(a.samples) // 500, (name, i, off)


# --------------------------------------------------------------------------- samples


def _sample_notes(samples) -> List[Path]:
    files: List[Path] = []
    for getter in (samples.goodnotes_files, samples.note_files):
        try:
            files += list(getter())
        except pytest.skip.Exception:
            pass
    if not files:
        pytest.skip("no sample files available")
    return files


def test_every_sample_converts_to_pdf_without_mupdf_warnings(samples) -> None:
    for path in _sample_notes(samples):
        data = path.read_bytes()
        try:
            to_document(data, path.name)
        except ValueError:
            continue  # a fixture the readers refuse (tested elsewhere)
        for mode in ("flatten", "annotations"):
            result = convert(data, path.name, Options(target="pdf", pdf_ink=mode))
            assert result.target_format == "pdf" and result.filename.endswith(".pdf"), path.name
            info = pdf_info(result.data)
            assert len(info.pages) == result.stats["pages"] and info.warnings == [], path.name
            # every page and annotation interpreted (rasterising every photo again is slow)
            assert mupdf_warnings(result.data, raster=False) == "", (path.name, mode)


def test_test5_converts_quickly(samples) -> None:
    path = samples.repo("goodparse") / "samples" / "Test5.goodnotes"
    if not path.is_file():
        pytest.skip("Test5.goodnotes not available")
    data = path.read_bytes()
    start = time.perf_counter()
    result = convert(data, path.name, Options(target="pdf"))
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, elapsed
    assert len(pdf_info(result.data).pages) == 3


def test_goodnotes_sample_matches_goodnotes_own_export(samples) -> None:
    """Test9 rendered from the .goodnotes file looks like GoodNotes' own Test9.pdf (ink,
    highlighters, the EXIF photo, the sticker, user PDFs) -- pixel differences stay small."""
    folder = samples.repo("goodparse") / "samples"
    if not (folder / "Test9.goodnotes").is_file() or not (folder / "Test9.pdf").is_file():
        pytest.skip("Test9 sample or export missing")
    ours = convert((folder / "Test9.goodnotes").read_bytes(), "Test9.goodnotes", Options(target="pdf")).data
    theirs = (folder / "Test9.pdf").read_bytes()
    for index in range(7):
        a, b = render(ours, index, zoom=0.25), render(theirs, index, zoom=0.25)
        assert (a.width, a.height) == (b.width, b.height)
        off = sum(1 for x, y in zip(a.samples, b.samples) if abs(x - y) > 64)
        assert off <= len(a.samples) // 25, (index, off)
