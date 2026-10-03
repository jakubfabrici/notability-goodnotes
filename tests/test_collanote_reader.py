"""gnnote.collanote: synthetic notes built here (format 1, zipped format-2 packages, bare JSON),
the real notes of YTU-Archive and a 112-page notebook (fetched at pinned commits, skipped when
absent), sniffing, the convert API, the CLI, the server options and hardening."""
from __future__ import annotations

import base64
import io
import json
import math
import random
import struct
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from gnnote import formats
from gnnote import protobuf as pb
from gnnote.cli import main
from gnnote.collanote import reader as cn
from gnnote.collanote.reader import read_cnote
from gnnote.convert import Options, convert, detect_format, document_stats, to_document
from gnnote.goodnotes.writer import write_goodnotes
from gnnote.model import Document, Page, Point, Stroke
from gnnote.notability.writer import write_note
from gnnote.pdfutil import make_paper_pdf, pdf_info
from gnnote.server import build_options
from tests.cnote_builders import (
    attributed_runs, cnote_format1, cnote_package, cpage, dk_drawing, dk_stroke, image_attachment, note_json, pk_blob,
    pk_ink, pk_path, pk_point, pk_stroke, png_bytes, text_attachment, two_page_pdf,
)

S_A4 = cn.A4_PT_PER_UNIT  # pt per canvas unit of a blank notebook
SLIDE_CANVAS = (1485.0, 835.3125)  # a 16:9 PDF note


def _counts(doc: Document) -> Tuple[int, int, int, int]:
    return (len(doc.pages), sum(len(p.strokes) for p in doc.pages), sum(len(p.images) for p in doc.pages),
            sum(len(p.texts) for p in doc.pages))


def _simple_note(**note_extra: Any) -> bytes:
    stroke = dk_stroke([(100, 200, 2.0), (150, 250, 2.0)])
    return cnote_format1(note_json(**note_extra), [cpage([stroke]), cpage()])


# --------------------------------------------------------------------------- synthetic notes


def test_format1_blank_notebook_ink_kinds_and_scale() -> None:
    strokes = [
        dk_stroke([(100, 200, 2.0), (150, 250, 2.5)], width=2.0, rgba=(0.1, 0.2, 0.3, 1.0), ink_type=1),
        dk_stroke([(300, 400, 10.0), (500, 400, 10.0)], width=10.0, rgba=(1.0, 1.0, 0.0, 1.0), ink_type=27),
        dk_stroke([(10, 10, 13.0), (20, 30, 26.0)], width=13.0, rgba=(1.0, 0.15, 0.0, 0.4946), ink_type=5),
        dk_stroke([(0, 0, 2.0), (1050, 1485, 2.0)], width=2.0, rgba=(0.34, 0.62, 0.17, 1.0), ink_type=4, flag=1),
    ]
    data = cnote_format1(note_json(name="Physics", paperOrTemplate="Gray Notelined S"), [cpage(strokes)])
    doc = read_cnote(data)
    assert doc.title == "Physics" and doc.source_format == "collanote" and doc.pdfs == {}
    (page,) = doc.pages
    assert (page.width, page.height) == pytest.approx((595.2756, 841.8898), abs=1e-3)
    assert page.paper == "lined" and page.background is None
    pen, marker, wide, unknown = page.strokes
    assert pen.kind == "pen" and pen.controls is None and pen.color == pytest.approx((0.1, 0.2, 0.3, 1.0))
    assert [(p.x, p.y, p.width) for p in pen.points] == pytest.approx([(100 * S_A4, 200 * S_A4, 2 * S_A4),
                                                                        (150 * S_A4, 250 * S_A4, 2.5 * S_A4)])
    assert pen.width == pytest.approx(2 * S_A4)
    assert marker.kind == "highlighter" and marker.color == (1.0, 1.0, 0.0, 1.0)
    assert wide.kind == "pen" and wide.color[3] == pytest.approx(0.4946)  # the translucent pen keeps its alpha
    assert [p.width for p in wide.points] == pytest.approx([13 * S_A4, 26 * S_A4])
    assert unknown.kind == "pen" and unknown.points[1].x == pytest.approx(595.2756, abs=1e-3)
    assert any("inkType 4" in w and "1 stroke" in w for w in doc.warnings)
    assert len(doc.warnings) == 1


def test_format2_package_with_and_without_the_folder() -> None:
    pages = [cpage([dk_stroke([(10, 10, 2.0), (20, 20, 2.0)])]), cpage()]
    with_folder = read_cnote(cnote_package(note_json(name="Maths"), pages, folder="Maths.cnote"))
    without = read_cnote(cnote_package(note_json(name="Maths"), pages, folder=None))
    for doc in (with_folder, without):
        assert doc.title == "Maths" and _counts(doc) == (2, 1, 0, 0) and doc.warnings == []
    unnamed = read_cnote(cnote_package(note_json(name=None), pages, folder="Lecture 3.cnote"))
    assert unnamed.title == "Lecture 3"
    assert read_cnote(cnote_package(note_json(name="  "), pages, folder=None)).title == "Untitled"


def test_manifest_page_count_and_newer_formats_warn() -> None:
    pages = [cpage()]
    doc = read_cnote(cnote_package(note_json(), pages, manifest={"format": 2, "minReader": 2, "pageCount": 3}))
    assert any("lists 3 pages but 1 page files" in w for w in doc.warnings)
    doc = read_cnote(cnote_package(note_json(), pages, manifest={"format": 3, "minReader": 3, "pageCount": 1}))
    assert any("package format 3" in w for w in doc.warnings)
    package = zipfile.ZipFile(io.BytesIO(cnote_package(note_json(name="Kept"), pages, folder=None)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in package.namelist():
            zf.writestr(name, b"{not json" if name == "manifest.cnm" else package.read(name))
    doc = read_cnote(buf.getvalue())
    assert doc.title == "Kept" and len(doc.pages) == 1
    assert any("manifest.cnm is not readable" in w for w in doc.warnings)


def test_pdf_backed_pages_and_the_scale_of_blank_pages() -> None:
    pdf = two_page_pdf([(960, 540), (720, 540)])
    on_slide = dk_stroke([(0, 0, 3.0), (1485, 835.3125, 3.0)])
    pages = [cpage(pdf=(0, 0), strokes=[on_slide]), cpage([on_slide]), cpage(pdf=(0, 1), strokes=[on_slide])]
    doc = read_cnote(cnote_format1(note_json(size=SLIDE_CANVAS), pages, pdfs={0: pdf}))
    first, blank, last = doc.pages
    assert (first.width, first.height) == (960.0, 540.0)
    assert (first.background.pdf_id, first.background.page_index) == ("0", 0)
    assert first.strokes[0].points[1].x == pytest.approx(960.0) and first.strokes[0].points[1].y == pytest.approx(540.0)
    assert first.strokes[0].points[0].width == pytest.approx(3 * 960 / 1485)
    # a blank page between slides gets the slides' scale, so it has their size
    assert blank.background is None and (blank.width, blank.height) == pytest.approx((960.0, 540.0))
    assert blank.strokes[0].points[1].x == pytest.approx(960.0)
    assert blank.strokes[0].points[0].width == pytest.approx(3 * 960 / 1485)
    assert (last.width, last.height, last.background.page_index) == (720.0, 540.0, 1)
    assert last.strokes[0].points[1].x == pytest.approx(720.0)
    assert doc.pdfs == {"0": pdf}
    assert document_stats(doc)["pdfs"] == 1
    assert [w for w in doc.warnings if "proportions" in w] == [
        "The PDF pages of page(s) 3 have other proportions than the note's canvas; the ink placement on them is unverified"]
    # a blank first page takes the scale of the next PDF page
    doc = read_cnote(cnote_format1(note_json(size=SLIDE_CANVAS), [cpage([on_slide]), cpage(pdf=(0, 0))], pdfs={0: pdf}))
    assert (doc.pages[0].width, doc.pages[0].height) == pytest.approx((960.0, 540.0))


def test_missing_and_out_of_range_pdf_references() -> None:
    pdf = make_paper_pdf(960, 540)
    pages = [cpage(pdf=(1, 0)), cpage(pdf=(0, 4)), cpage(pdf=(0, 0))]
    pages[2]["pdfPointer"] = "zero"
    doc = read_cnote(cnote_format1(note_json(size=SLIDE_CANVAS), pages, pdfs={0: pdf, 7: pdf}))
    assert all(p.background is None for p in doc.pages) and doc.pdfs == {}
    text = "\n".join(doc.warnings)
    assert "PDF 1.pdf is missing" in text and "page 5 of 0.pdf, which has only 1 pages" in text
    assert "unreadable PDF reference" in text and "7.pdf are not shown on any page" in text
    doc = read_cnote(cnote_format1(note_json(), [cpage(pdf=(0, 0))], pdfs={0: b"%PDF-1.4 nothing here"}))
    assert doc.pages[0].background is None and any("0.pdf has no readable page" in w for w in doc.warnings)


def test_images_are_placed_by_centre_and_size() -> None:
    png = png_bytes(4, 2)
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    attachments = [image_attachment(png, (0.5, 0.25), (0.2, 0.1)),
                   image_attachment(jpeg, (0.1, 0.1), (0.1, 0.1), rotation=30),
                   image_attachment(b"GIF89a....", (0.5, 0.5), (0.1, 0.1)),
                   {"type": "image", "imageInData": base64.b64encode(png).decode(), "bound": [[0, 0], [0.1, 0.1]]}]
    doc = read_cnote(cnote_format1(note_json(), [cpage(attachments=attachments)]))
    first, second = doc.pages[0].images
    w, h = 0.2 * 1050, 0.1 * 1485
    assert (first.x, first.y, first.w, first.h) == pytest.approx(((525 - w / 2) * S_A4, (371.25 - h / 2) * S_A4,
                                                                  w * S_A4, h * S_A4))
    assert first.fmt == "png" and first.data == png and first.rotation == 0.0
    assert second.fmt == "jpeg" and second.rotation == 30.0
    text = "\n".join(doc.warnings)
    assert "1 rotated image(s)" in text and "2 image(s) without readable PNG/JPEG data or position" in text


def test_text_boxes_carry_text_font_size_and_colour() -> None:
    box = text_attachment("https://example.org/notes.pdf\n", (0.8353785, 0.8052017), (0.2442953, 0.0555469))
    doc = read_cnote(cnote_format1(note_json(), [cpage(attachments=[box])]))
    (text,) = doc.pages[0].texts
    assert text.text == "https://example.org/notes.pdf"
    (run,) = text.runs
    assert run.text == text.text and run.font == "ChalkboardSE-Regular" and not run.bold
    assert run.size == pytest.approx(14 * S_A4) and text.size == pytest.approx(14 * S_A4)
    assert text.color == (0.0, 0.0, 1.0, 1.0) and run.color == (0.0, 0.0, 1.0, 1.0)
    w, h = 0.2442953 * 1050, 0.0555469 * 1485
    assert (text.x, text.y, text.w, text.h) == pytest.approx(((0.8353785 * 1050 - w / 2) * S_A4,
                                                              (0.8052017 * 1485 - h / 2) * S_A4, w * S_A4, h * S_A4))
    assert any("1 text box(es) were converted" in w for w in doc.warnings)
    # a box turned about its centre: the model turns about the top-left corner, which moves
    turned = text_attachment("Turned", (0.5, 0.5), (0.2, 0.1), rotation=90, font="Georgia-BoldItalic", rgba=None)
    (text,) = read_cnote(cnote_format1(note_json(), [cpage(attachments=[turned])])).pages[0].texts
    w, h = 0.2 * 1050, 0.1 * 1485
    assert text.rotation == 90.0 and text.runs[0].bold and text.runs[0].italic and text.color == (0, 0, 0, 1)
    assert (text.x, text.y) == pytest.approx(((525 + h / 2) * S_A4, (742.5 - w / 2) * S_A4))


def test_text_runs_follow_the_attribute_info() -> None:
    def box(data: str) -> Any:
        att = {"type": "text", "center": [0.5, 0.5], "bound": [[0, 0], [0.3, 0.1]], "rotatedDegree": 0,
               "attStringData": data}
        (text,) = read_cnote(cnote_format1(note_json(), [cpage(attachments=[att])])).pages[0].texts
        return text

    two = box(attributed_runs("Bold and plain", [(4, "Helvetica-Bold", 20.0), (10, "Helvetica", 12.0)]))
    assert two.text == "Bold and plain" and two.size == pytest.approx(20 * S_A4)
    assert [(r.text, r.bold, r.font) for r in two.runs] == [("Bold", True, "Helvetica-Bold"),
                                                            (" and plain", False, "Helvetica")]
    assert [r.size for r in two.runs] == pytest.approx([20 * S_A4, 12 * S_A4])
    emoji = box(attributed_runs("😀 ok", [(2, "A", 10.0), (3, "B", 10.0)]))  # lengths count UTF-16 units
    assert [r.text for r in emoji.runs] == ["😀", " ok"]
    short = box(attributed_runs("Bold and plain", [(4, "Helvetica-Bold", 20.0), (3, "Helvetica", 12.0)]))
    assert [(r.text, r.font) for r in short.runs] == [("Bold and plain", "Helvetica-Bold")]  # one run, first style
    broken = box(attributed_runs("Bold", [(4, "Helvetica-Bold", 20.0)], info=b"\x04\x09"))  # index out of range
    assert [(r.text, r.font) for r in broken.runs] == [("Bold", "Helvetica-Bold")]


def test_unreadable_text_and_unknown_attachments_are_reported() -> None:
    attachments = [text_attachment("This Sticker should be a image, not a TextView", (0.5, 0.5), (0.1, 0.1)),
                   {"type": "text", "attStringData": "not base64 plist", "center": [0.5, 0.5], "bound": [[0, 0], [1, 1]]},
                   {"type": "drawingBox", "center": [0.5, 0.5]}, {"type": 7}, "not an attachment"]
    doc = read_cnote(cnote_format1(note_json(), [cpage(attachments=attachments)]))
    assert doc.pages[0].texts == [] and doc.pages[0].images == []
    text = "\n".join(doc.warnings)
    assert "2 unreadable text box(es) were skipped" in text
    assert "1 attachment(s) of the unknown kind 'drawingBox' were skipped" in text
    assert "1 attachment(s) of the unknown kind '7' were skipped" in text


def test_audio_and_bookmarks_are_reported() -> None:
    note = note_json(audios=[{"audioData": "AAAA", "duration": 3.0}], bookmarks=[{"title": "a"}, {"title": "b"}])
    doc = read_cnote(cnote_format1(note, [cpage()]))
    assert "1 audio recording(s) are not converted" in doc.warnings
    assert "2 bookmark(s) are not converted" in doc.warnings
    manifest = {"format": 2, "minReader": 2, "pageCount": 1, "audioCount": 2}
    doc = read_cnote(cnote_package(note_json(), [cpage()], manifest=manifest, extra=[("0.m4a", b"x")]))
    assert "2 audio recording(s) are not converted" in doc.warnings


def test_stroke_count_mismatch_and_damaged_ink_are_reported() -> None:
    good = dk_stroke([(1, 1, 2.0), (2, 2, 2.0)])
    layer = base64.b64decode(dk_drawing([good, good]))
    pages = [
        cpage([good, good], count=5),  # the page says 5 strokes, its ink holds 2
        cpage(layers=[base64.b64encode(layer + b"\x0a\xff\x01").decode()]),  # truncated tail: keep the 2 strokes
        cpage(layers=["!!! not base64 !!!", 12]),
        cpage(layers=[dk_drawing([good, pb.field_varint(1, 0) + b"\x12\x05ab", good])]),  # one bad stroke record
    ]
    doc = read_cnote(cnote_format1(note_json(), pages))
    assert [len(p.strokes) for p in doc.pages] == [2, 2, 0, 2]
    text = "\n".join(doc.warnings)
    assert "Page(s) 1, 2, 4 record a different stroke count" in text
    assert "The ink data of page(s) 2, 3 is damaged" in text
    assert "1 damaged stroke(s) were skipped" in text


def test_unreadable_note_description_and_loose_base64() -> None:
    stroke = dk_stroke([(1, 1, 2.0), (2, 2, 2.0)])
    layer = dk_drawing([stroke])
    loose = layer.rstrip("=")[:20] + "\n" + layer.rstrip("=")[20:]  # wrapped and unpadded
    data = cnote_format1(None, [cpage(layers=[loose])], extra=[("note without pdf.cnote", b"\xff not json")])
    doc = read_cnote(data)
    assert len(doc.pages[0].strokes) == 1
    assert [w for w in doc.warnings if "note description" in w] == [
        "The note description (note without pdf.cnote) is not readable; defaults are used"]
    doc = read_cnote(cnote_format1(None, [cpage()]))
    assert "The note description is missing; defaults are used" in doc.warnings


def test_points_without_the_usual_layout_take_the_slow_path() -> None:
    # x == 0 is omitted on disk (proto3), so the point does not start with the x key
    stroke = dk_stroke([(0.0, 5.0, 2.0), (3.0, 0.0, 0.0), (7.0, 8.0, 2.0)], width=2.0)
    (page,) = read_cnote(cnote_format1(note_json(), [cpage([stroke])])).pages
    assert [(p.x / S_A4, p.y / S_A4, p.width / S_A4) for p in page.strokes[0].points] == pytest.approx(
        [(0.0, 5.0, 2.0), (3.0, 0.0, 2.0), (7.0, 8.0, 2.0)])  # a zero point width falls back to the style width


def _legacy(points_lists: List[List[Tuple[float, float]]], ink: str = "com.apple.ink.pen") -> bytes:
    return pk_blob([pk_ink(ink, (0.0, 0.0, 0.5, 1.0))],
                   [pk_stroke(pk_path([pk_point(x, y, w=3.0) for x, y in pts])) for pts in points_lists])


def test_legacy_pencilkit_pages() -> None:
    canvas_h = 1485.0
    pages = [
        cpage(drawing=_legacy([[(100, 100), (200, 300)]]), strokes=[dk_stroke([(5, 5, 2.0), (6, 6, 2.0)])]),
        cpage(),
        cpage(drawing=_legacy([[(100, 2 * canvas_h + 100), (300, 2 * canvas_h + 900)]], ink="com.apple.ink.marker")),
        cpage(drawing=b"wrd\xf0\x01\x00\x2a\xff"),
        cpage(drawing=b"not a drawing at all"),
    ]
    doc = read_cnote(cnote_format1(note_json(), pages))
    first, _, third, fourth, fifth = doc.pages
    legacy, modern = first.strokes  # legacy ink is older, so it lies below
    assert [(p.x, p.y) for p in legacy.points] == pytest.approx([(100 * S_A4, 100 * S_A4), (200 * S_A4, 300 * S_A4)])
    assert legacy.color == (0.0, 0.0, 0.5, 1.0) and modern.points[0].x == pytest.approx(5 * S_A4)
    (shifted,) = third.strokes  # all of its ink lies in the band of page 3 of one long canvas
    assert shifted.kind == "highlighter"
    assert [(p.x, p.y) for p in shifted.points] == pytest.approx([(100 * S_A4, 100 * S_A4), (300 * S_A4, 900 * S_A4)])
    assert fourth.strokes == [] and fifth.strokes == []
    text = "\n".join(doc.warnings)
    assert "Page(s) 1, 3 carry CollaNote 1.x PencilKit ink (moved up from one continuous canvas)" in text
    assert "Page 4: the legacy PencilKit ink layer is damaged" in text


def test_a_compressed_note_inside_a_zip_is_opened_once() -> None:
    inner = _simple_note(name="Inner")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Inner.cnote", inner)
        zf.writestr("__MACOSX/._Inner.cnote", b"resource fork")
    doc = read_cnote(buf.getvalue())
    assert doc.title == "Inner" and _counts(doc) == (2, 1, 0, 0)
    unnamed = io.BytesIO()
    with zipfile.ZipFile(unnamed, "w") as zf:
        zf.writestr("Week 3.cnote", _simple_note(name=None))
    assert read_cnote(unnamed.getvalue()).title == "Week 3"  # the file name stands in for the title
    twice = io.BytesIO()
    with zipfile.ZipFile(twice, "w") as zf:
        zf.writestr("Outer.cnote", buf.getvalue())
    with pytest.raises(ValueError, match="no note JSON"):
        read_cnote(twice.getvalue())


def test_an_archive_with_two_packages_reads_one_and_says_so() -> None:
    a = zipfile.ZipFile(io.BytesIO(cnote_package(note_json(name="A"), [cpage()], folder="A.cnote")))
    b = zipfile.ZipFile(io.BytesIO(cnote_package(note_json(name="B"), [cpage(), cpage()], folder="B.cnote")))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for src in (a, b):
            for name in src.namelist():
                zf.writestr(name, src.read(name))
    doc = read_cnote(buf.getvalue())
    assert doc.title == "B" and len(doc.pages) == 2  # the folder with more pages wins
    assert any("holds 2 notes; only B.cnote was read" in w for w in doc.warnings)


def test_bare_json_note_with_inline_pages_and_pdfs() -> None:
    pdf = make_paper_pdf(960, 540)
    note = note_json(size=SLIDE_CANVAS, pages=[cpage(pdf=(0, 0), strokes=[dk_stroke([(0, 0, 2), (1485, 0, 2)])]),
                                                cpage()],
                     importedPdfDatas=[base64.b64encode(pdf).decode()])
    data = json.dumps(note).encode()
    assert detect_format("old.cnote", data) == "collanote"
    doc = read_cnote(data)
    assert _counts(doc) == (2, 1, 0, 0) and doc.pages[0].background is not None and doc.pdfs == {"0": pdf}
    assert doc.pages[0].strokes[0].points[1].x == pytest.approx(960.0)
    assert any("early single-file CollaNote note" in w for w in doc.warnings)
    single_page = json.dumps(cpage([dk_stroke([(1, 1, 2), (2, 2, 2)])])).encode()
    assert _counts(read_cnote(single_page)) == (1, 1, 0, 0)


# --------------------------------------------------------------------------- registry, convert, CLI, server


def test_registry_entry_is_read_only() -> None:
    fmt = formats.get("collanote")
    assert (fmt.name, fmt.extension, fmt.input_extensions) == ("CollaNote", ".cnote", (".cnote",))
    assert fmt.readable and not fmt.writable
    assert formats.default_target("collanote") == "notability"
    assert formats.format_for_extension("Lecture.CNOTE") is fmt


def test_sniffing_every_container_variant() -> None:
    format1 = _simple_note()
    package = cnote_package(note_json(), [cpage()], folder="P.cnote")
    bare_package = cnote_package(note_json(), [cpage()], folder=None)
    only_pages = cnote_format1(None, [cpage()])
    for data in (format1, package, bare_package, only_pages):
        for name in ("x.cnote", "x.zip", "x.cnote.zip", "x", "x.bin"):
            assert detect_format(name, data) == "collanote", name
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as zf:
        zf.writestr("x.cnote", format1)
    assert detect_format("x.zip", nested.getvalue()) == "collanote"
    other = io.BytesIO()
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("a/b/0.cpage", b"{}")  # two folders deep: not a note
        zf.writestr("readme.txt", b"hi")
    with pytest.raises(ValueError, match="not a supported note file"):
        detect_format("x.zip", other.getvalue())
    page = Page(width=455.04, height=588.45)
    page.strokes.append(Stroke([Point(40, 40, 2.0), Point(120, 90, 2.0)], width=2.0))
    assert detect_format("x.zip", write_note(Document(pages=[page]), Options())) == "notability"
    assert detect_format("x.zip", write_goodnotes(Document(pages=[page]), Options())) == "goodnotes"
    assert detect_format("x.cnote", b"garbage") == "collanote"  # the extension decides; the reader refuses
    with pytest.raises(ValueError):
        to_document(b"garbage", "x.cnote")


def test_convert_accepts_a_zipped_package() -> None:
    stroke = dk_stroke([(100, 100, 2.0), (400, 300, 2.0)])
    data = cnote_package(note_json(name="Maths"), [cpage([stroke]), cpage()], folder="Maths.cnote")
    result = convert(data, "Maths.cnote.zip")
    assert (result.source_format, result.target_format, result.filename) == ("collanote", "notability", "Maths.note")
    assert result.stats == {"pages": 2, "strokes": 1, "images": 0, "texts": 0, "pdfs": 0}
    assert _counts(to_document(result.data, result.filename))[:2] == (2, 1)
    result = convert(data, "Maths.zip", Options(target="goodnotes"))
    assert result.filename == "Maths.goodnotes" and _counts(to_document(result.data, result.filename))[:2] == (2, 1)


def test_collanote_is_not_a_target() -> None:
    with pytest.raises(ValueError, match="CollaNote files cannot be written yet"):
        Options(target="collanote").validate()
    with pytest.raises(ValueError, match="CollaNote files cannot be written yet"):
        convert(_simple_note(), "x.cnote", Options(target="collanote"))
    with pytest.raises(ValueError, match="to must be one of goodnotes, notability"):
        build_options({"to": "collanote"})
    with pytest.raises(ValueError, match="CollaNote files cannot be written"):
        formats.get("collanote").write(Document(), Options())


def test_cli_lists_converts_and_describes_collanote(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["formats"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("collanote"))
    assert "CollaNote" in line and ".cnote" in line and line.rstrip().endswith("read only")
    src = tmp_path / "Lecture.cnote"
    src.write_bytes(_simple_note(name="Lecture"))
    assert main(["convert", str(src), "--to", "goodnotes"]) == 0
    assert (tmp_path / "Lecture.goodnotes").is_file()
    assert main(["convert", str(src)]) == 0  # default target: Notability
    assert (tmp_path / "Lecture.note").is_file()
    capsys.readouterr()
    assert main(["convert", str(src), "--to", "collanote"]) == 2
    assert "CollaNote files can be read but not written" in capsys.readouterr().err
    assert main(["info", str(src), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["format"] == "collanote" and info["title"] == "Lecture" and info["totals"]["strokes"] == 1
    assert main(["batch", str(tmp_path), "-o", str(tmp_path / "out"), "--to", "goodnotes"]) == 0
    assert (tmp_path / "out" / "Lecture.goodnotes").is_file()


def test_cli_takes_a_package_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """On a Mac a format-2 note is a folder ``X.cnote``; the CLI zips it in memory."""
    stroke = dk_stroke([(100, 100, 2.0), (400, 300, 2.0)])
    package = zipfile.ZipFile(io.BytesIO(cnote_package(note_json(name="Week 1"), [cpage([stroke]), cpage()],
                                                       folder="Week 1.cnote")))
    package.extractall(tmp_path / "library")
    folder = tmp_path / "library" / "Week 1.cnote"
    assert folder.is_dir()
    assert main(["convert", str(folder), "--to", "goodnotes"]) == 0
    assert (tmp_path / "library" / "Week 1.goodnotes").is_file()
    capsys.readouterr()
    assert main(["info", str(folder), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["format"] == "collanote" and info["title"] == "Week 1" and info["totals"]["strokes"] == 1
    assert main(["batch", str(tmp_path / "library"), "-o", str(tmp_path / "out")]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["Week 1.note"]
    empty = tmp_path / "Empty.cnote"
    empty.mkdir()
    assert main(["convert", str(empty)]) == 1  # an empty folder is no note


# --------------------------------------------------------------------------- hardening


@pytest.mark.parametrize("data", [
    b"", b"hello", b"PK\x03\x04 broken", b"[1, 2, 3]", b'{"name": "no pages"}', b"{" * 5,
    b"[" * 200_000 + b"]" * 200_000, b'{"pages": ' + b"[" * 200_000 + b"]" * 200_000 + b"}",
])
def test_not_a_note_is_a_value_error(data: bytes) -> None:
    with pytest.raises(ValueError):
        read_cnote(data)


def test_a_zip_without_note_members_is_a_value_error() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", b"hi")
    with pytest.raises(ValueError, match="no note JSON and no .cpage pages"):
        read_cnote(buf.getvalue())
    with pytest.raises(TypeError):
        read_cnote("not bytes")  # type: ignore[arg-type]


def test_member_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    data = cnote_format1(note_json(), [cpage([dk_stroke([(1, 1, 2), (2, 2, 2)])] * 40)])
    monkeypatch.setattr(cn, "MAX_MEMBER_BYTES", 2000)
    doc = read_cnote(data)
    assert len(doc.pages) == 1 and doc.pages[0].strokes == []
    assert any("0.cpage declares" in w and "above the 0 MB limit" in w for w in doc.warnings)
    assert any("Page 1 is unreadable" in w for w in doc.warnings)
    monkeypatch.setattr(cn, "MAX_MEMBER_BYTES", 1 << 30)
    monkeypatch.setattr(cn, "MAX_TOTAL_BYTES", 2000)
    doc = read_cnote(data)
    assert any("inflates to more than 0 MB in total" in w for w in doc.warnings)


def test_a_pdf_skipped_by_a_limit_is_not_called_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_paper_pdf(960, 540) + b"%" + b"x" * 50_000 + b"\n"
    data = cnote_format1(note_json(size=SLIDE_CANVAS), [cpage(pdf=(0, 0))], pdfs={0: pdf})
    assert read_cnote(data).pages[0].background is not None
    monkeypatch.setattr(cn, "MAX_MEMBER_BYTES", 20_000)
    doc = read_cnote(data)
    assert doc.pages[0].background is None and doc.pdfs == {}
    assert any("0.pdf could not be read" in w for w in doc.warnings)
    assert not any("missing" in w for w in doc.warnings)
    manifest = {"format": 2, "minReader": 3, "pageCount": 1}
    doc = read_cnote(cnote_package(note_json(), [cpage()], manifest=manifest))
    assert any("package format 3" in w for w in doc.warnings)


def test_payload_stroke_point_and_page_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    strokes = [dk_stroke([(i, i, 2), (i + 1, i + 1, 2), (i + 2, i, 2)]) for i in range(6)]
    pages = [cpage(strokes, attachments=[image_attachment(png_bytes(), (0.5, 0.5), (0.1, 0.1))])] + [cpage()] * 3
    data = cnote_format1(note_json(), pages)
    monkeypatch.setattr(cn, "MAX_STROKES_PER_PAGE", 4)
    doc = read_cnote(data)
    assert len(doc.pages[0].strokes) == 4 and any("beyond 4 strokes" in w for w in doc.warnings)
    monkeypatch.setattr(cn, "MAX_STROKES_PER_PAGE", 1000)
    monkeypatch.setattr(cn, "MAX_POINTS_PER_PAGE", 10)
    doc = read_cnote(data)
    assert len(doc.pages[0].strokes) == 3 and any("beyond 10 points" in w for w in doc.warnings)
    monkeypatch.setattr(cn, "MAX_POINTS_PER_PAGE", 1000)
    monkeypatch.setattr(cn, "MAX_PAGES", 2)
    doc = read_cnote(data)
    assert len(doc.pages) == 2 and any("only the first 2 were read" in w for w in doc.warnings)
    monkeypatch.setattr(cn, "MAX_PAGES", 100)
    monkeypatch.setattr(cn, "MAX_BLOB_BYTES", 30)
    doc = read_cnote(data)
    assert doc.pages[0].strokes == [] and doc.pages[0].images == []
    assert any("ink layer larger than" in w for w in doc.warnings)
    assert any("attachment larger than" in w for w in doc.warnings)


def test_hostile_values_degrade_to_warnings() -> None:
    nan = struct.unpack("<f", b"\x00\x00\xc0\x7f")[0]
    strokes = [dk_stroke([(nan, 1.0, 2.0), (2.0, 3.0, float("inf")), (4.0, 5.0, 2.0), (3e38, 1.0, 2.0)],
                         width=float("nan")),
               dk_stroke([(nan, nan, 1.0)]),
               dk_stroke([], width=2.0)]
    pages = [cpage(strokes), "not a page", {"_dkDrawing": "", "attachments": {"a": 1}, "pdfPointer": {"pdfIndex": -1}},
             {"_dkDrawing": {"x": 1}, "drawing": 7, "strokeCountBeforeSaving": True}]
    for size in (["a", "b"], [0, 0], [1e9, 5], None):
        doc = read_cnote(cnote_format1(note_json(size=size) if size is not None else {"name": "x"}, pages))
        assert len(doc.pages) == 4
        (stroke,) = doc.pages[0].strokes
        assert [(round(p.x / S_A4, 3), round(p.y / S_A4, 3)) for p in stroke.points] == [(2.0, 3.0), (4.0, 5.0)]
        assert all(math.isfinite(p.width) and p.width > 0 for p in stroke.points) and math.isfinite(stroke.width)
        assert (doc.pages[0].width, doc.pages[0].height) == pytest.approx((595.2756, 841.8898), abs=1e-3)
    assert any("page size is missing or unusable" in w for w in doc.warnings)
    assert any("Page 2 is unreadable" in w for w in doc.warnings)
    assert any("unreadable PDF reference" in w for w in doc.warnings)
    assert "3 ink point(s) with unusable coordinates (not a number, or far off the page) were dropped" in doc.warnings
    huge = two_page_pdf([(2e6, 1e6), (960, 540)])
    doc = read_cnote(cnote_format1(note_json(size=SLIDE_CANVAS), [cpage(pdf=(0, 0)), cpage(pdf=(0, 1))], pdfs={0: huge}))
    assert doc.pages[0].background is None and doc.pages[1].background is not None
    assert any("whose size is unusable" in w for w in doc.warnings)


def test_fuzzed_notes_raise_nothing_but_value_error() -> None:
    rng = random.Random(20261003)
    pdf = make_paper_pdf(960, 540)
    pages = [cpage([dk_stroke([(10 * i, 20 * i, 2.0) for i in range(5)], ink_type=27)], pdf=(0, 0),
                   attachments=[image_attachment(png_bytes(), (0.5, 0.5), (0.2, 0.2)),
                                text_attachment("Hi", (0.3, 0.3), (0.2, 0.05))]),
             cpage(drawing=_legacy([[(1, 1), (5, 5)]]))]
    note_bytes = json.dumps(note_json(size=SLIDE_CANVAS)).encode()
    page_bytes = [json.dumps(p).encode() for p in pages]

    def zipped(note: bytes, page_list: List[bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("note without pdf.cnote", note)
            for i, p in enumerate(page_list):
                zf.writestr(f"{i}.cpage", p)
            zf.writestr("0.pdf", pdf)
        return buf.getvalue()

    def mutate(data: bytes, count: int) -> bytes:
        out = bytearray(data)
        for _ in range(count):
            out[rng.randrange(len(out))] = rng.randrange(256)
        return bytes(out)

    whole = zipped(note_bytes, page_bytes)
    assert _counts(read_cnote(whole)) == (2, 2, 1, 1)
    for trial in range(300):
        if trial % 3 == 0:
            data = mutate(whole, rng.randint(1, 20))
        elif trial % 3 == 1:
            data = zipped(mutate(note_bytes, rng.randint(1, 5)), [mutate(p, rng.randint(1, 10)) for p in page_bytes])
        else:  # damage inside the base64 payloads while keeping the JSON valid
            broken = []
            for p in pages:
                q = json.loads(json.dumps(p))
                for key in ("_dkDrawing", "drawing"):
                    raw = base64.b64decode(q[key][0] if isinstance(q[key], list) else q[key] or "")
                    if raw:
                        damaged = base64.b64encode(mutate(raw, rng.randint(1, 6))).decode()
                        q[key] = [damaged] if isinstance(q[key], list) else damaged
                broken.append(json.dumps(q).encode())
            data = zipped(note_bytes, broken)
        try:
            doc = read_cnote(data)
        except ValueError:
            continue
        for page in doc.pages:
            assert page.width > 0 and page.height > 0
            for stroke in page.strokes:
                assert stroke.points and all(math.isfinite(p.x) and math.isfinite(p.y) for p in stroke.points)


# --------------------------------------------------------------------------- real notes (YTU-Archive)

# name -> (pages, strokes, points, images, title, page sizes in pt)
REAL_NOTES: Dict[str, Tuple[int, int, int, int, str, set]] = {
    "LED’ler.cnote": (42, 152, 3239, 7, "LED’ler", {(960.0, 540.0)}),
    "YİF 1-2.cnote": (64, 273, 5271, 0, "YİF 1-2", {(960.0, 540.0)}),
    "YİF 10.cnote": (22, 220, 4754, 0, "YİF 10", {(960.0, 540.0)}),
    "YİF 11.cnote": (31, 333, 8286, 0, "YİF 11", {(960.0, 540.0)}),
    "YİF 3-4.cnote": (37, 468, 7558, 0, "YİF 3-4", {(960.0, 540.0)}),
    "YİF 5.cnote": (23, 750, 13407, 0, "YİF 5", {(720.0, 540.0)}),
    "YİF 6.cnote": (23, 555, 9758, 0, "YIF-6", {(720.0, 540.0)}),
    "YİF 7.cnote": (22, 351, 5769, 0, "YİF 7", {(1180.0, 820.0)}),
    "YİF 8.cnote": (27, 772, 12468, 0, "YİF 8", {(960.0, 540.0)}),
    "YİF 9.cnote": (24, 375, 5680, 0, "YİF 9", {(960.0, 540.0)}),
}


def _note_bytes(path: Path, folder: bool = True) -> bytes:
    """A ``.cnote`` file as is; a format-2 package directory zipped (as it would be shared)."""
    if path.is_file():
        return path.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for member in sorted(path.iterdir()):
            zf.write(member, (path.name + "/" if folder else "") + member.name)
    return buf.getvalue()


def _members(path: Path) -> Dict[str, bytes]:
    if path.is_dir():
        return {m.name: m.read_bytes() for m in path.iterdir()}
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


@pytest.fixture(scope="module")
def real_notes(samples: Any) -> List[Tuple[Path, Document]]:
    return [(path, read_cnote(_note_bytes(path))) for path in samples.collanote_notes()]


def test_real_notes_read_completely(real_notes: List[Tuple[Path, Document]], samples: Any) -> None:
    assert len(real_notes) >= 1
    for path, doc in real_notes:
        assert doc.warnings == [], path.name
        assert all(p.background is not None for p in doc.pages if "1-2" not in path.name)
        expected = samples.expected_for(path, REAL_NOTES)
        if expected is None:
            continue
        pages, strokes, points, images, title, sizes = expected
        got_points = sum(len(s.points) for p in doc.pages for s in p.strokes)
        assert (len(doc.pages), _counts(doc)[1], got_points, _counts(doc)[2], doc.title) == \
            (pages, strokes, points, images, title), path.name
        assert {(round(p.width, 2), round(p.height, 2)) for p in doc.pages} == sizes, path.name
        if path.is_dir():  # the same package zipped without its folder reads identically
            assert _counts(read_cnote(_note_bytes(path, folder=False))) == _counts(doc)


def test_real_notes_match_a_generic_protobuf_decoding(real_notes: List[Tuple[Path, Document]]) -> None:
    """The reader's fast stroke path against gnnote.protobuf's generic decoder, point by point."""
    for path, doc in real_notes:
        members = _members(path)
        note = json.loads(members.get("basenote.cdat") or members["note without pdf.cnote"])
        canvas_w = float(note["size"][0])
        page_names = sorted((n for n in members if n.endswith(".cpage")), key=lambda n: int(n.split(".")[0]))
        for name, page in zip(page_names, doc.pages):
            s = page.width / canvas_w
            expected: List[Tuple[float, float, float]] = []
            for layer in json.loads(members[name])["_dkDrawing"]:
                for record in pb.get_all(pb.decode_message(base64.b64decode(layer)), 1):
                    fields = pb.message_value(record)
                    style = pb.message_value(pb.get(fields, 3))
                    style_w = pb.fixed32_float(pb.get(style, 1))
                    for point in pb.get_all(fields, 2):
                        pf = pb.message_value(point)
                        x, y, w = (pb.fixed32_float(pb.get(pf, i)) if pb.get(pf, i) else 0.0 for i in (1, 2, 3))
                        expected.append((x * s, y * s, (w or style_w) * s))
            got = [(p.x, p.y, p.width) for st in page.strokes for p in st.points]
            assert got == pytest.approx(expected, abs=1e-9), f"{path.name} {name}"


def test_real_notes_pdf_binding(real_notes: List[Tuple[Path, Document]]) -> None:
    for path, doc in real_notes:
        members = _members(path)
        sizes = [(p.width, p.height) for p in pdf_info(members["0.pdf"]).pages]
        assert doc.pdfs == {"0": members["0.pdf"]}
        page_names = sorted((n for n in members if n.endswith(".cpage")), key=lambda n: int(n.split(".")[0]))
        for name, page in zip(page_names, doc.pages):
            pointer = json.loads(members[name]).get("pdfPointer")
            if pointer is None:
                assert page.background is None  # a blank page inserted between slides
                assert (page.width, page.height) == pytest.approx(sizes[0])  # ... sized like them
                continue
            assert (page.background.pdf_id, page.background.page_index) == ("0", pointer["pageIndex"])
            assert (page.width, page.height) == sizes[pointer["pageIndex"]]


def test_real_notes_geometry(real_notes: List[Tuple[Path, Document]]) -> None:
    """Ink lands on its page: crosses drawn over whole slides may run past the edges."""
    points = inside = strokes = touching = 0
    for _path, doc in real_notes:
        for page in doc.pages:
            mx, my = 0.05 * page.width, 0.05 * page.height
            for stroke in page.strokes:
                strokes += 1
                x0, y0, x1, y1 = stroke.bbox()
                touching += x1 >= 0 and y1 >= 0 and x0 <= page.width and y0 <= page.height
                for p in stroke.points:
                    points += 1
                    inside += -mx <= p.x <= page.width + mx and -my <= p.y <= page.height + my
    assert inside / points > 0.95 and touching / strokes > 0.99


def test_real_notes_spot_checks(samples: Any) -> None:
    """Values of the pages whose renders over the slides were checked by eye (docs/collanote.md)."""
    folder = samples.repo("YTU-Archive") / "1-2" / "Semiconductor" / "slide"
    if samples.at_pinned_commit("YTU-Archive") is False:
        pytest.skip("YTU-Archive is not at its pinned commit")
    yif5 = folder / "YİF 5.cnote"
    led = folder / "LED’ler.cnote"
    if not (yif5.exists() and led.exists()):
        pytest.skip("spot-check notes not available")
    page = read_cnote(_note_bytes(yif5)).pages[1]
    first = page.strokes[0]
    assert (first.points[0].x, first.points[0].y, first.points[0].width) == pytest.approx((575.786, 213.514, 11.5308),
                                                                                          abs=1e-3)
    assert first.kind == "pen" and first.color == pytest.approx((1.0, 0.14913, 0.0, 0.49458), abs=1e-4)
    xs = [p.x for s in page.strokes for p in s.points]
    ys = [p.y for s in page.strokes for p in s.points]
    assert (min(xs), min(ys), max(xs), max(ys)) == pytest.approx((-59.41, 6.25, 719.72, 604.74), abs=0.01)
    page = read_cnote(_note_bytes(led)).pages[20]
    assert [v for i in page.images for v in (i.x, i.y, i.w, i.h)] == pytest.approx(
        [551.38, 256.63, 410.26, 285.09, 520.62, 248.16, 351.99, 244.6, 54.85, 321.49, 266.1, 184.92], abs=0.01)


@pytest.mark.parametrize("target", ["notability", "goodnotes"])
def test_real_notes_convert_to_both_apps(real_notes: List[Tuple[Path, Document]], target: str) -> None:
    for path, doc in real_notes:
        result = convert(_note_bytes(path), path.name + (".zip" if path.is_dir() else ""), Options(target=target))
        assert result.source_format == "collanote" and result.filename.endswith(formats.get(target).extension)
        back = to_document(result.data, result.filename)
        assert _counts(back) == _counts(doc), f"{path.name} -> {target}"
        assert document_stats(back)["pdfs"] == document_stats(doc)["pdfs"] == 1
        slides = [p.background.page_index for p in doc.pages if p.background is not None]
        assert [p.background.page_index for p in back.pages
                if p.background is not None and not p.template_is_builtin] == slides


# --------------------------------------------------------------------------- the 112-page notebook (opt-in)


def test_large_blank_paper_notebook(samples: Any) -> None:
    path = samples.large_file("collanote-notebook.cnote")
    data = path.read_bytes()
    doc = read_cnote(data)
    assert doc.title == "Full Notes" and doc.pdfs == {}
    assert _counts(doc) == (112, 116394, 59, 1)
    assert sum(len(s.points) for p in doc.pages for s in p.strokes) == 1821466
    assert {(round(p.width, 2), round(p.height, 2), p.paper) for p in doc.pages} == {(595.28, 841.89, "lined")}
    assert sorted(doc.warnings) == sorted([
        "1 bookmark(s) are not converted",
        "13 stroke(s) use CollaNote pen types this reader does not know (inkType 4); they were read as plain pen strokes",
        "1 text box(es) were converted; their font scale and box layout are inferred from a single sample",
    ])
    (text,) = [t for p in doc.pages for t in p.texts]
    assert text.text.startswith("https://www.gatsby.ucl.ac.uk/") and text.runs[0].font == "ChalkboardSE-Regular"
    assert text.size == pytest.approx(14 * S_A4) and text.color == (0.0, 0.0, 1.0, 1.0)
    kinds = {}
    for page in doc.pages:
        for stroke in page.strokes:
            kinds[stroke.kind] = kinds.get(stroke.kind, 0) + 1
    assert kinds == {"pen": 116132, "highlighter": 262}
    for target in ("notability", "goodnotes"):
        result = convert(data, path.name, Options(target=target))
        assert _counts(to_document(result.data, result.filename)) == (112, 116394, 59, 1), target
