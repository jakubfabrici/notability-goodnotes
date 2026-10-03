"""PDF round trips and plumbing: write (annotations) -> read gives the strokes back, PDF <->
GoodNotes / Notability through ``convert``, the CLI and the HTTP server options."""
from __future__ import annotations

import http.client
import json
import math
import threading
from pathlib import Path
from typing import List, Sequence, Tuple

import pytest

from gnnote.cli import main
from gnnote.convert import Options, convert
from gnnote.geometry import flatten_bezier
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.model import Document, Page, Point, Stroke
from gnnote.notability.reader import read_note
from gnnote.pdf.reader import read_pdf
from gnnote.pdf.writer import write_pdf
from gnnote.pdfutil import pdf_info
from gnnote.server import build_options, make_server

from tests.test_pdf_helpers import annot_types, full_document, one_page_pdf, render


def _polyline(stroke: Stroke) -> List[Tuple[float, float]]:
    pts = flatten_bezier(stroke.points, stroke.controls, 1.0) if stroke.controls else stroke.points
    return [(p.x, p.y) for p in pts]


def _distance_to(point: Tuple[float, float], line: Sequence[Tuple[float, float]]) -> float:
    if len(line) == 1:
        return math.dist(point, line[0])
    best = float("inf")
    px, py = point
    for (ax, ay), (bx, by) in zip(line, line[1:]):
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
        best = min(best, math.hypot(px - ax - t * dx, py - ay - t * dy))
    return best


def _hausdorff(a: Stroke, b: Stroke) -> float:
    pa, pb = _polyline(a), _polyline(b)
    return max(max(_distance_to(p, pb) for p in pa), max(_distance_to(p, pa) for p in pb))


def _roundtrip_doc() -> Document:
    page1 = Page(455.04, 588.45)
    page1.strokes = [
        Stroke([Point(30, 30, 2), Point(90, 70, 2), Point(150, 30, 2)], width=2.0),
        Stroke([Point(40, 120, 1), Point(120, 140, 4), Point(200, 120, 1), Point(280, 150, 6)], width=2.0,
               color=(0.8, 0.1, 0.1, 1.0)),
        Stroke([Point(40, 220, 2), Point(140, 230, 2), Point(240, 220, 2)],
               controls=[(Point(70, 170, 2), Point(110, 270, 2)), (Point(170, 170, 2), Point(210, 270, 2))],
               width=2.0, color=(0.0, 0.0, 1.0, 1.0)),
        Stroke([Point(40, 320, 8), Point(260, 320, 8)], color=(1.0, 0.9, 0.0, 0.5), kind="highlighter", width=8.0),
        Stroke([Point(40, 380, 1), Point(240, 380, 5)], controls=[(Point(100, 340, 2), Point(180, 420, 4))],
               width=3.0, color=(0.0, 0.5, 0.0, 0.7)),
        Stroke([Point(320, 60, 6)], width=6.0),
    ]
    ring = [Point(300, 450), Point(400, 450), Point(400, 540), Point(300, 450)]
    page1.strokes.append(Stroke(list(ring), color=(1.0, 0.0, 0.0, 0.1), kind="fill", width=0.0, outline=[ring]))
    page2 = Page(842, 595)  # landscape
    page2.strokes = [Stroke([Point(10 + i * 7, 300 + 80 * math.sin(i / 7.0), 1.5) for i in range(100)], width=1.5,
                            color=(0.2, 0.3, 0.4, 1.0))]
    return Document(title="Round trip", pages=[page1, page2])


def test_annotations_round_trip_within_half_a_point() -> None:
    doc = _roundtrip_doc()
    back = read_pdf(write_pdf(doc, Options(pdf_ink="annotations")))
    assert back.title == "Round trip"
    for page, page_back in zip(doc.pages, back.pages):
        assert (page_back.width, page_back.height) == (page.width, page.height)
        strokes = [s for s in page.strokes if s.kind != "fill"]  # fills stay in the page content
        assert len(page_back.strokes) == len(strokes)
        for original, again in zip(strokes, page_back.strokes):
            assert _hausdorff(original, again) <= 0.5
            assert again.kind == original.kind
            expected_alpha = original.color[3] if original.kind != "highlighter" or original.color[3] < 1 else 0.5
            assert again.color == pytest.approx(tuple(original.color[:3]) + (expected_alpha,), abs=1e-3)
            widths = sorted(p.width for p in original.points)
            assert again.width == pytest.approx(widths[len(widths) // 2] if len(widths) % 2 else
                                                (widths[len(widths) // 2 - 1] + widths[len(widths) // 2]) / 2,
                                                abs=1e-3)
    # a constant-width Bezier comes back with its exact handles (from the appearance stream)
    bezier = back.pages[0].strokes[2]
    assert bezier.controls is not None and len(bezier.points) == 3
    assert [v for p in bezier.points for v in (p.x, p.y)] == pytest.approx([40, 220, 140, 230, 240, 220], abs=1e-6)
    assert [v for c1, c2 in bezier.controls for v in (c1.x, c1.y, c2.x, c2.y)] == \
        pytest.approx([70, 170, 110, 270, 170, 170, 210, 270], abs=1e-6)


def test_reading_our_annotations_leaves_exactly_the_rest_of_the_page() -> None:
    doc = full_document()
    data = write_pdf(doc, Options(pdf_ink="annotations"))
    back = read_pdf(data)
    assert [len(p.strokes) for p in back.pages] == [sum(1 for s in p.strokes if s.kind != "fill") for p in doc.pages]
    stripped = back.pdfs[back.pages[0].background.pdf_id]
    assert stripped.startswith(data)
    for page in doc.pages:  # what remains equals the flattened page without its ink
        page.strokes = [s for s in page.strokes if s.kind == "fill"]
    plain = write_pdf(doc, Options())
    for index in range(len(doc.pages)):
        a, b = render(stripped, index), render(plain, index)
        assert sum(1 for x, y in zip(a.samples, b.samples) if abs(x - y) > 8) <= len(a.samples) // 2000


def test_flattened_ink_stays_part_of_the_background() -> None:
    data = write_pdf(_roundtrip_doc(), Options(pdf_ink="flatten"))
    back = read_pdf(data)
    assert all(not p.strokes for p in back.pages)
    assert back.pdfs[back.pages[0].background.pdf_id] == data
    assert back.warnings == []


# --------------------------------------------------------------------------- other formats


def _annotated_pdf() -> bytes:
    ink = (b"<< /Type /Annot /Subtype /Ink /Rect [0 0 300 400] /InkList [[20 380 80 330 140 390]] "
           b"/BS << /W 3 >> /C [0 0 1] /F 4 >>")
    hl = (b"<< /Type /Annot /Subtype /Highlight /Rect [0 0 300 400] /C [1 1 0] "
          b"/QuadPoints [20 300 200 300 20 285 200 285] >>")
    return one_page_pdf(b"BT /F1 12 Tf 20 290 Td (Some highlighted text) Tj ET", annots=[ink, hl],
                        info=b"<< /Title (Annotated) >>")


def test_pdf_converts_to_notability_by_default_and_to_goodnotes() -> None:
    data = _annotated_pdf()
    result = convert(data, "Annotated.pdf")
    assert (result.source_format, result.target_format, result.filename) == ("pdf", "notability", "Annotated.note")
    assert result.stats == {"pages": 1, "strokes": 2, "images": 0, "texts": 0, "pdfs": 1}
    note = read_note(result.data)
    assert note.title == "Annotated" and len(note.pages) == 1
    assert note.pages[0].background is not None  # a PDF-backed Notability page
    assert sorted(s.kind for s in note.pages[0].strokes) == ["highlighter", "pen"]
    carried = note.pdfs[note.pages[0].background.pdf_id]
    assert b"/Subtype /Ink" in data and annot_types(carried) == [[]]

    gn = convert(data, "Annotated.pdf", Options(target="goodnotes"))
    back = read_goodnotes(gn.data)
    assert len(back.pages) == 1 and len(back.pages[0].strokes) == 2
    assert not back.pages[0].template_is_builtin  # the PDF is a user PDF


def test_goodnotes_export_to_notability(samples) -> None:
    path = samples.repo("goodparse") / "samples" / "Test9.pdf"
    if not path.is_file():
        pytest.skip("Test9.pdf not available")
    result = convert(path.read_bytes(), path.name)
    note = read_note(result.data)
    assert [len(p.strokes) for p in note.pages] == [0, 102, 0, 11, 0, 22, 6]
    assert result.stats["texts"] == 3


def test_goodnotes_to_pdf_to_notability_keeps_the_ink(samples) -> None:
    path = samples.repo("goodparse") / "samples" / "Test8.goodnotes"
    if not path.is_file():
        pytest.skip("Test8.goodnotes not available")
    original = read_goodnotes(path.read_bytes())
    pdf = convert(path.read_bytes(), path.name, Options(target="pdf", pdf_ink="annotations"))
    note = read_note(convert(pdf.data, "Test8.pdf").data)
    ink = [sum(1 for s in p.strokes if s.kind != "fill") for p in original.pages]
    assert [len(p.strokes) for p in note.pages] == ink


# --------------------------------------------------------------------------- CLI / server


def test_cli_pdf_options(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["formats"]) == 0
    assert "pdf" in capsys.readouterr().out
    src = tmp_path / "Mini.pdf"
    src.write_bytes(_annotated_pdf())
    assert main(["convert", str(src)]) == 0
    assert (tmp_path / "Mini.note").is_file()
    note_path = tmp_path / "Mini.note"
    assert main(["convert", str(note_path), "--to", "pdf", "--pdf-ink", "annotations", "-o",
                 str(tmp_path / "Again.pdf")]) == 0
    again = (tmp_path / "Again.pdf").read_bytes()
    assert pdf_info(again).pages and b"/Subtype /Ink" in again
    assert main(["convert", str(note_path), "--to", "pdf", "--pdf-ink", "raster"]) == 2
    capsys.readouterr()
    # batch: PDFs only with --include-pdf
    folder = tmp_path / "batch"
    folder.mkdir()
    (folder / "Doc.pdf").write_bytes(_annotated_pdf())
    assert main(["batch", str(folder)]) == 0
    listed = capsys.readouterr().out
    assert "no note files to convert" in listed and ".pdf" not in listed
    assert main(["batch", str(folder), "--include-pdf", "-o", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "Doc.note").is_file()
    assert main(["batch", str(tmp_path / "out"), "--to", "pdf", "--pdf-ink", "annotations"]) == 0
    assert (tmp_path / "out" / "Doc.pdf").is_file()


def test_server_pdf_ink_option() -> None:
    assert build_options({"pdf_ink": "Annotations", "to": "pdf"}) == {"pdf_ink": "annotations", "target": "pdf"}
    assert "pdf_ink" not in build_options({"pdf_ink": ""})
    with pytest.raises(ValueError, match="pdf_ink"):
        build_options({"pdf_ink": "raster"})


def test_server_converts_to_pdf_with_annotations(tmp_path: Path) -> None:
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    server = make_server("127.0.0.1", 0, root, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        boundary = "gnnoteBoundary"
        note = convert(_annotated_pdf(), "Doc.pdf").data
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"Doc.note\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + note + f"\r\n--{boundary}--\r\n".encode()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
        conn.request("POST", "/api/convert?to=pdf&pdf_ink=annotations", body=body,
                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        response = conn.getresponse()
        data = response.read()
        assert response.status == 200, data[:500]
        assert response.getheader("X-GnNote-Target-Format") == "pdf"
        assert "Doc.pdf" in response.getheader("Content-Disposition")
        assert data.startswith(b"%PDF-1.7") and b"/Subtype /Ink" in data
        conn.request("POST", "/api/convert?to=pdf&pdf_ink=raster", body=body,
                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        response = conn.getresponse()
        assert response.status == 400 and "pdf_ink" in json.loads(response.read())["error"]
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
