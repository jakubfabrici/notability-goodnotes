"""Cross-format tests for :mod:`gnnote.convert` over every reference sample.

Directions covered:

* GoodNotes -> Notability -> ``read_note`` (both paper modes): page and stroke counts, first
  anchors mapped back through the Notability writer's page transform, colours, highlighters,
  images and text boxes.
* Notability -> GoodNotes -> ``read_goodnotes``: page count and sizes, PDF backgrounds, stroke
  counts, first anchors, constant widths, images and text boxes; plus parser-for-goodnotes
  (MIT) run in a subprocess as an independent oracle.
* GoodNotes -> Notability -> GoodNotes: stroke counts and positions after the full chain.
* Parity with the proof-of-concept note that was imported on the author's iPad.

Stroke counting: our GoodNotes reader emits one model stroke per TPL sub-path (eraser-cut
elements and pencil elements carry several), and both writers write exactly one element /
curve per model stroke, so model stroke counts are preserved by every conversion.  Where an
oracle counts GoodNotes *elements* instead, the tests count elements independently.

Content only the newest GoodNotes files carry (schema 25/35, ``docs/goodnotes-v35-*.md``)
does not survive the Notability leg by design (``design.md`` 4.2): shape fills
(``Stroke.kind == "fill"``) and vector-sticker PDF images (``Image.fmt == "pdf"``) are
dropped with a warning, so the GoodNotes -> Notability comparisons use the surviving ink
and raster images (:func:`_ink`, :func:`_rasters`).  Every sample file the fixture finds is
checked against invariants; exact expectations exist only for the pinned files of
:data:`GOODNOTES_EXPECTED` (page sizes from GoodNotes' own PDF exports, counts from the
research documents).
"""
from __future__ import annotations

import io
import json
import math
import os
import plistlib
import statistics
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from gnnote import applelz4, protobuf, tpl
from gnnote.convert import (EXTENSIONS, GOODNOTES, NOTABILITY, ConvertResult, Options, convert,
                            detect_format, document_stats, other_format, output_filename, to_document)
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.goodnotes.writer import write_goodnotes
from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox
from gnnote.notability import x_inset
from gnnote.notability.reader import PLAIN_PAGE_WIDTH_PT, read_note
from gnnote.notability.writer import LEGACY_ASPECT, write_note

SCRATCH = Path(os.environ.get("GNNOTE_SCRATCH_WORK", "")).parent if os.environ.get("GNNOTE_SCRATCH_WORK") else None
XY = Tuple[float, float]


# --------------------------------------------------------------------------- helpers

def _read(path: Path) -> bytes:
    return path.read_bytes()


def _first(stroke: Stroke) -> XY:
    return stroke.points[0].x, stroke.points[0].y


def _ink(page: Page) -> List[Stroke]:
    """The strokes of a page that survive a conversion to Notability (no shape fills)."""
    return [s for s in page.strokes if s.kind != "fill"]


def _rasters(page: Page) -> List[Image]:
    """The images of a page that survive a conversion to Notability (PNG / JPEG only)."""
    return [im for im in page.images if im.fmt != "pdf" and not im.data.startswith(b"%PDF-")]


def _is_user_pdf_page(page: Page) -> bool:
    """A page that is PDF-backed in every Notability paper mode (design.md 4.2)."""
    return page.background is not None and not page.template_is_builtin


def _rgb8(color: Sequence[float]) -> Tuple[int, int, int]:
    return tuple(int(round(c * 255)) for c in color[:3])  # type: ignore[return-value]


def _rgba8(color: Sequence[float]) -> Tuple[int, int, int, int]:
    return tuple(int(round(c * 255)) for c in color[:4])  # type: ignore[return-value]


def _median_width(stroke: Stroke) -> float:
    return float(statistics.median(p.width for p in stroke.points))


def _plain_transform(page: Page, width: float) -> Tuple[float, float]:
    """(scale, x_offset) the Notability writer applies to a plain-paper page of ``page``'s size."""
    plain_h = LEGACY_ASPECT * width
    scale = width / page.width
    x_off = 0.0
    if page.height * scale > plain_h + 1e-6:
        scale = plain_h / page.height
        x_off = (width - page.width * scale) / 2.0
    return scale, x_off


def _match_pairs(expected: List[XY], actual: List[XY], tol: float) -> Tuple[List[Tuple[int, int]], List[int]]:
    """Pair every expected point with an actual point within ``tol``.

    Tries the in-order pairing first (cheap, and the usual case); points that do not match in
    order are paired with the nearest unused actual point.  Returns ``(pairs, unmatched)``.
    """
    pairs: List[Tuple[int, int]] = []
    unmatched: List[int] = []
    used = [False] * len(actual)
    pending: List[int] = []
    for i, e in enumerate(expected):
        if i < len(actual) and not used[i] and math.dist(e, actual[i]) <= tol:
            used[i] = True
            pairs.append((i, i))
        else:
            pending.append(i)
    for i in pending:
        e = expected[i]
        best, best_d = -1, tol
        for j, a in enumerate(actual):
            if used[j]:
                continue
            d = math.dist(e, a)
            if d <= best_d:
                best, best_d = j, d
        if best < 0:
            unmatched.append(i)
        else:
            used[best] = True
            pairs.append((i, best))
    return pairs, unmatched


# --------------------------------------------------------------------------- detect_format / API

def _zip_with(names: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in names.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_detect_format_by_content_and_extension() -> None:
    gn = _zip_with({"schema.pb": b"\x08\x18", "index.notes.pb": b""})
    nb = _zip_with({"My note/Session.plist": b"bplist00", "My note/metadata.plist": b""})
    assert detect_format("a.goodnotes", gn) == GOODNOTES
    assert detect_format("a.note", nb) == NOTABILITY
    # content wins over the extension, and .zip / no extension are fine
    assert detect_format("a.zip", gn) == GOODNOTES
    assert detect_format("a.zip", nb) == NOTABILITY
    assert detect_format("renamed.note", gn) == GOODNOTES
    assert detect_format("noext", nb) == NOTABILITY
    # unrecognisable content falls back to the extension (the reader then reports the problem)
    assert detect_format("x.goodnotes", b"junk") == GOODNOTES
    assert detect_format("x.NOTE", b"junk") == NOTABILITY
    with pytest.raises(ValueError):
        detect_format("x.zip", _zip_with({"readme.txt": b"hi"}))
    with pytest.raises(ValueError):
        detect_format("x.txt", b"junk")
    with pytest.raises(ValueError):
        detect_format("", b"")
    with pytest.raises(TypeError):
        detect_format("x.note", "not bytes")  # type: ignore[arg-type]


def test_other_format_and_output_filename() -> None:
    assert other_format(GOODNOTES) == NOTABILITY and other_format(NOTABILITY) == GOODNOTES
    with pytest.raises(ValueError):
        other_format("pdf")
    assert output_filename("dir/Mathe 1.goodnotes", NOTABILITY) == "Mathe 1.note"
    assert output_filename("x.note", GOODNOTES) == "x.goodnotes"
    assert output_filename("archive.zip", NOTABILITY) == "archive.note"
    assert output_filename(".note", GOODNOTES) == ".note.goodnotes"
    assert output_filename("", NOTABILITY) == "converted.note"
    assert EXTENSIONS == {GOODNOTES: ".goodnotes", NOTABILITY: ".note", "noteful": ".noteful"}


def test_options_validation() -> None:
    Options().validate()
    Options(paper="pdf", pressure=False, simplify=1.5).validate()
    with pytest.raises(ValueError):
        Options(paper="lined").validate()
    with pytest.raises(ValueError):
        Options(simplify=-1).validate()
    with pytest.raises(ValueError):
        Options(notability_page_width=0).validate()
    with pytest.raises(ValueError):
        convert(b"", "x.note", Options(paper="grid"))


def _synthetic_doc() -> Document:
    page = Page(455.04, 588.45)
    page.strokes.append(Stroke([Point(10, 10, 2.0), Point(50, 40, 2.0), Point(90, 10, 2.0)], width=2.0))
    page.strokes.append(Stroke([Point(100, 100, 6.0), Point(200, 100, 6.0)], color=(1.0, 1.0, 0.0, 0.5),
                               kind="highlighter", width=6.0))
    page.texts.append(TextBox(20, 200, 150, 30, "hello"))
    return Document(title="Synthetic", pages=[page], source_format=GOODNOTES)


def test_convert_synthetic_both_ways_and_result_shape() -> None:
    doc = _synthetic_doc()
    note = write_note(doc, Options())
    result = convert(note, "Synthetic.note", Options(title="Renamed"))
    assert isinstance(result, ConvertResult)
    assert (result.source_format, result.target_format) == (NOTABILITY, GOODNOTES)
    assert result.filename == "Synthetic.goodnotes"  # name follows the input file, not the title
    assert result.stats == {"pages": 1, "strokes": 2, "images": 0, "texts": 1, "pdfs": 0}
    assert all(isinstance(w, str) for w in result.warnings)
    assert len(result.warnings) == len(set(result.warnings))
    back = read_goodnotes(result.data)
    assert back.title == "Renamed"  # the title override reaches the writer through Document.title
    assert [len(p.strokes) for p in back.pages] == [2]
    assert back.pages[0].strokes[1].kind == "highlighter"

    result2 = convert(result.data, result.filename)
    assert (result2.source_format, result2.target_format) == (GOODNOTES, NOTABILITY)
    assert result2.filename == "Synthetic.note"
    assert result2.stats["strokes"] == 2 and result2.stats["texts"] == 1
    again = read_note(result2.data)
    assert again.title == "Renamed"
    # gnnote-generated paper reads back as stock paper, so the note stays on plain pages
    assert back.pages[0].template_is_builtin
    assert again.pages[0].background is None


def test_to_document_and_stats_count_user_pdfs_only() -> None:
    doc = _synthetic_doc()
    doc.pages[0].background = PdfBackground("paper", 0)
    doc.pages[0].template_is_builtin = True
    doc.pdfs["paper"] = b"%PDF-1.4 not really"
    assert document_stats(doc)["pdfs"] == 0
    doc.pages[0].template_is_builtin = False
    assert document_stats(doc)["pdfs"] == 1
    data = write_goodnotes(_synthetic_doc(), Options())
    read = to_document(data, "anything.zip")
    assert read.source_format == GOODNOTES and len(read.pages) == 1


def test_convert_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        convert(b"not a file", "x.txt")
    with pytest.raises(ValueError):
        convert(b"not a zip", "x.note")
    with pytest.raises(ValueError):
        convert(b"not a zip", "x.goodnotes")


# --------------------------------------------------------------------------- GoodNotes -> Notability

STD = (455.04, 588.45)  # GoodNotes "standard" paper
A4 = (595.28, 841.89)

# file -> what ``convert`` reports for it (``stats``: pages / strokes / images / texts / user
# PDFs) plus the page sizes in display order and the counts of content that cannot reach
# Notability (shape fills among the strokes, PDF stickers among the images).  Test6 .. Test9:
# docs/goodnotes-v35-elements.md section 0 (fills are counted as strokes by the reader);
# Test9's seven sizes are those of GoodNotes' export Test9.pdf (binding doc section 9.1).
GOODNOTES_EXPECTED: Dict[str, Dict[str, Any]] = {
    "Test4.goodnotes": {"stats": {"pages": 2, "strokes": 5, "images": 0, "texts": 0, "pdfs": 0},
                        "sizes": [STD] * 2, "fills": 0, "pdf_images": 0},
    "Test5.goodnotes": {"stats": {"pages": 3, "strokes": 65, "images": 1, "texts": 2, "pdfs": 0},
                        "sizes": [STD] * 3, "fills": 0, "pdf_images": 0},
    "Test6.goodnotes": {"stats": {"pages": 5, "strokes": 21, "images": 0, "texts": 5, "pdfs": 0},
                        "sizes": [STD] * 5, "fills": 3, "pdf_images": 0},
    "Test7.goodnotes": {"stats": {"pages": 4, "strokes": 27, "images": 0, "texts": 14, "pdfs": 0},
                        "sizes": [STD] * 4, "fills": 3, "pdf_images": 0},
    "Test8.goodnotes": {"stats": {"pages": 4, "strokes": 10, "images": 0, "texts": 0, "pdfs": 0},
                        "sizes": [STD] * 4, "fills": 4, "pdf_images": 0},
    "Test9.goodnotes": {"stats": {"pages": 7, "strokes": 162, "images": 2, "texts": 4, "pdfs": 3},
                        "sizes": [A4, A4, A4, A4, (1280.0, 905.0), (595.2, 841.68), (454.91, 143.28)],
                        "fills": 0, "pdf_images": 1},
    "test.goodnotes": {"stats": {"pages": 1, "strokes": 1, "images": 0, "texts": 0, "pdfs": 0},
                       "sizes": [A4], "fills": 0, "pdf_images": 0},
    "test2.goodnotes": {"stats": {"pages": 1, "strokes": 1, "images": 0, "texts": 0, "pdfs": 0},
                        "sizes": [A4], "fills": 0, "pdf_images": 0},
    "test3.goodnotes": {"stats": {"pages": 1, "strokes": 2, "images": 0, "texts": 0, "pdfs": 0},
                        "sizes": [A4], "fills": 0, "pdf_images": 0},
    "ex1.goodnotes": {"stats": {"pages": 1, "strokes": 1494, "images": 3, "texts": 0, "pdfs": 0},
                      "sizes": [STD], "fills": 0, "pdf_images": 0},
    "ex2.goodnotes": {"stats": {"pages": 1, "strokes": 28, "images": 0, "texts": 0, "pdfs": 0},
                      "sizes": [STD], "fills": 0, "pdf_images": 0},
    "ex3.goodnotes": {"stats": {"pages": 1, "strokes": 2459, "images": 2, "texts": 0, "pdfs": 0},
                      "sizes": [STD], "fills": 0, "pdf_images": 0},
    "record.goodnotes": {"stats": {"pages": 2, "strokes": 12, "images": 0, "texts": 0, "pdfs": 0},
                         "sizes": [STD] * 2, "fills": 0, "pdf_images": 0},
}


def _check_goodnotes_expectations(samples, path: Path, src: Document, result: ConvertResult) -> None:
    """Invariants for any GoodNotes sample, exact values for the pinned ones."""
    assert result.stats == document_stats(src)
    assert result.stats["pages"] == len(src.pages) >= 1, path.name
    assert all(p.width > 0 and p.height > 0 for p in src.pages), path.name
    expected = samples.expected_for(path, GOODNOTES_EXPECTED)
    if expected is None:
        return
    assert result.stats == expected["stats"], path.name
    assert [(round(p.width, 2), round(p.height, 2)) for p in src.pages] == expected["sizes"], path.name
    assert sum(len(p.strokes) - len(_ink(p)) for p in src.pages) == expected["fills"], path.name
    assert sum(len(p.images) - len(_rasters(p)) for p in src.pages) == expected["pdf_images"], path.name
    if expected["fills"]:
        assert any("shape fills dropped" in w for w in result.warnings), result.warnings
    if expected["pdf_images"]:
        assert any("PDF images dropped" in w for w in result.warnings), result.warnings


@pytest.mark.parametrize("paper", ["plain", "pdf"])
def test_goodnotes_to_notability_roundtrip(samples, paper: str) -> None:
    width = 574.0
    plain_h = LEGACY_ASPECT * width
    for path in samples.goodnotes_files():
        data = _read(path)
        src = read_goodnotes(data)
        result = convert(data, path.name, Options(paper=paper))
        assert result.filename == path.stem + ".note"
        _check_goodnotes_expectations(samples, path, src, result)
        back = read_note(result.data)
        assert len(back.pages) == len(src.pages), path.name
        assert sum(len(p.strokes) for p in back.pages) == sum(len(_ink(p)) for p in src.pages), path.name
        assert sum(len(p.images) for p in back.pages) == sum(len(_rasters(p)) for p in src.pages), path.name
        assert sum(len(p.texts) for p in back.pages) == sum(len(p.texts) for p in src.pages), path.name

        # Expected document-space coordinates of every source first anchor and the matching
        # source stroke, in page order; actual ones from the read-back note (its pages carry
        # the same slot layout, so document space is comparable even if a stroke that starts
        # outside its page was assigned to the neighbouring slot by the reader).
        expected: List[XY] = []
        exp_strokes: List[Stroke] = []
        exp_scale: List[float] = []
        y = 0.0
        for page in src.pages:
            if paper == "plain" and not _is_user_pdf_page(page):
                scale, x_off = _plain_transform(page, width)
                y_off, slot_h = y, plain_h
            else:
                scale, x_off = width / page.width, 0.0
                slot_h = float(math.ceil(page.height * scale))
                y_off = y + (slot_h - page.height * scale)
            for s in _ink(page):
                expected.append((x_off + s.points[0].x * scale, y_off + s.points[0].y * scale))
                exp_strokes.append(s)
                exp_scale.append(scale)
            y += slot_h
        actual: List[XY] = []
        act_strokes: List[Stroke] = []
        y = 0.0
        for page in back.pages:
            if page.background is None:
                scale, slot_h, y_off = width / PLAIN_PAGE_WIDTH_PT, plain_h, y
            else:
                scale = width / page.width
                slot_h = float(math.ceil(page.height * scale))
                y_off = y + (slot_h - page.height * scale)
            for s in page.strokes:
                actual.append((s.points[0].x * scale, y_off + s.points[0].y * scale))
                act_strokes.append(s)
            y += slot_h
        assert len(expected) == len(actual)
        tol_doc = 0.6 * max(exp_scale) if exp_scale else 1.0
        pairs, unmatched = _match_pairs(expected, actual, tol_doc)
        assert not unmatched, f"{path.name}: {len(unmatched)} first anchors moved by more than 0.6 pt"
        for i, j in pairs:
            s, t = exp_strokes[i], act_strokes[j]
            assert _rgb8(s.color) == _rgb8(t.color), path.name
            assert (s.kind == "highlighter") == (t.kind == "highlighter"), path.name
            if s.kind != "highlighter":
                assert _rgba8(s.color) == _rgba8(t.color), path.name
            assert len(s.points) == len(t.points) or s.controls is None, path.name  # exact Bezier pass-through
        if paper == "pdf":
            assert all(p.background is not None for p in back.pages), path.name
            assert [(round(p.width, 2), round(p.height, 2)) for p in back.pages] == \
                   [(round(p.width, 2), round(p.height, 2)) for p in src.pages], path.name


def test_goodnotes_to_notability_widths_and_pressure_flag(samples) -> None:
    for path in samples.goodnotes_files():
        data = _read(path)
        src = read_goodnotes(data)
        for pressure in (True, False):
            back = read_note(convert(data, path.name, Options(paper="pdf", pressure=pressure)).data)
            exp = [_median_width(s) for p in src.pages for s in _ink(p)]
            got = [_median_width(s) for p in back.pages for s in p.strokes]
            assert len(exp) == len(got)
            for e, g in zip(exp, got):
                assert abs(e - g) <= 0.1 * max(e, 0.3) + 0.05, (path.name, e, g)
            if not pressure:
                for p in back.pages:
                    for s in p.strokes:
                        ws = [pt.width for pt in s.points]
                        assert max(ws) - min(ws) <= 1e-3 * max(ws) + 1e-6


# --------------------------------------------------------------------------- Notability -> GoodNotes

PFG_SCRIPT = r"""
import json, sys
import oracle_shims
oracle_shims.frame_parser_for_goodnotes()
from goodnotes_re import GoodNotesDocument
out = []
with GoodNotesDocument.open(sys.argv[1]) as doc:
    for p in doc.pages():
        # parser-for-goodnotes splits one element into several pieces where consecutive points
        # are more than 300 canvas units apart (a pen-lift heuristic), so count elements by uuid
        out.append({"strokes": len({s.uuid for s in p.strokes}), "pieces": len(p.strokes),
                    "width": p.dimensions.width, "height": p.dimensions.height,
                    "images": len(p.image_elements), "texts": len(p.text_fragments)})
print(json.dumps(out))
"""


def run_parser_for_goodnotes(samples, path: Path) -> List[Dict[str, Any]]:
    """parser-for-goodnotes' view of ``path`` from a subprocess with its own PYTHONPATH
    (the script calls tests/oracle_shims.py first)."""
    root = samples.repo("parser-for-goodnotes") / "src"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), str(Path(__file__).resolve().parent)]))
    proc = subprocess.run([sys.executable, "-c", PFG_SCRIPT, str(path)], env=env,
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        if "ModuleNotFoundError" in proc.stderr and "goodnotes_re" not in proc.stderr.split("ModuleNotFoundError")[-1]:
            pytest.skip(f"parser-for-goodnotes dependency missing: {proc.stderr.strip().splitlines()[-1]}")
        pytest.fail(f"parser-for-goodnotes failed on {path.name}:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def test_notability_to_goodnotes_roundtrip(samples, tmp_path: Path) -> None:
    for path in samples.note_files():
        data = _read(path)
        src = read_note(data)
        result = convert(data, path.name)
        assert result.filename == path.stem + ".goodnotes"
        back = read_goodnotes(result.data)
        assert len(back.pages) == len(src.pages), path.name
        for i, (a, b) in enumerate(zip(src.pages, back.pages)):
            label = f"{path.name} page {i + 1}"
            assert abs(a.width - b.width) < 0.05 and abs(a.height - b.height) < 0.05, label
            assert b.background is not None, label  # every GoodNotes page has a paper PDF
            if a.background is not None:
                # A PDF behind a Notability page (a user PDF, or Notability's own 11.7+
                # template paper) is carried verbatim with the same page index.  It is not
                # GoodNotes stock paper, so it reads back as a user PDF (design.md 4.4: only
                # generated paper gets the catalogue name).
                assert not b.template_is_builtin, label
                assert b.background.page_index == a.background.page_index, label
                assert back.pdfs[b.background.pdf_id] == src.pdfs[a.background.pdf_id], label
            else:
                # plain Notability paper becomes gnnote-generated paper = stock paper
                assert b.template_is_builtin and b.paper == "plain", label
            assert len(b.strokes) == len(a.strokes), label
            assert len(b.images) == len(a.images), label
            non_empty = [t for t in a.texts if (t.text or "").strip() or t.runs]
            assert len(b.texts) == len(non_empty), label
            exp = [_first(s) for s in a.strokes]
            got = [_first(s) for s in b.strokes]
            pairs, unmatched = _match_pairs(exp, got, 0.6)
            assert not unmatched, f"{label}: {len(unmatched)} first anchors moved by more than 0.6 pt"
            for x, yv in pairs:
                s, t = a.strokes[x], b.strokes[yv]
                median = _median_width(s)
                assert abs(t.width - median) <= 0.10 * median + 1e-6, (label, median, t.width)
                ws = [p.width for p in t.points]
                assert max(ws) - min(ws) < 1e-6, label  # flat strokes: constant width
                assert _rgb8(s.color) == _rgb8(t.color), label
                assert (s.kind == "highlighter") == (t.kind == "highlighter"), label
        if src.warnings:
            # lossy steps of the source are reported, e.g. Notability vector shapes
            assert all(w in result.warnings for w in src.warnings)

        # independent oracle: page and stroke counts as parser-for-goodnotes sees them
        out = tmp_path / result.filename
        out.write_bytes(result.data)
        oracle = run_parser_for_goodnotes(samples, out)
        assert len(oracle) == len(src.pages), path.name
        assert [o["strokes"] for o in oracle] == [len(p.strokes) for p in src.pages], path.name
        assert sum(o["images"] for o in oracle) == sum(len(p.images) for p in src.pages), path.name


def test_notability_shapes_sample_reports_dropped_shapes(samples) -> None:
    """Vector shapes cannot round-trip; the warning must say so."""
    shape_notes = [p for p in samples.note_files() if p.name == "shapes.note"]
    if not shape_notes:
        pytest.skip("shapes.note not available")
    result = convert(_read(shape_notes[0]), "shapes.note")
    assert any("vector shape" in w for w in result.warnings), result.warnings


# --------------------------------------------------------------------------- full chain

@pytest.mark.parametrize("paper", ["pdf", "plain"])
def test_goodnotes_note_goodnotes_chain(samples, paper: str) -> None:
    for path in samples.goodnotes_files():
        data = _read(path)
        src = read_goodnotes(data)
        step1 = convert(data, path.name, Options(paper=paper))
        step2 = convert(step1.data, step1.filename)
        assert step2.filename == path.stem + ".goodnotes"
        back = read_goodnotes(step2.data)
        assert len(back.pages) == len(src.pages), path.name
        assert sum(len(p.strokes) for p in back.pages) == sum(len(_ink(p)) for p in src.pages), path.name
        assert sum(len(p.images) for p in back.pages) == sum(len(_rasters(p)) for p in src.pages), path.name
        assert sum(len(p.texts) for p in back.pages) == sum(len(p.texts) for p in src.pages), path.name
        assert not any(s.kind == "fill" for p in back.pages for s in p.strokes), path.name
        # PDF-backed pages (every page in "pdf" mode, user-PDF pages in both modes) keep the
        # original paper, size and coordinates 1:1; plain pages come back as 612 x 803.25 pt
        # with coordinates scaled by the plain transform and then by 612 / W, horizontally
        # centred when the page was taller than Notability's plain page
        factor, offset = [], []
        for i, (a, b) in enumerate(zip(src.pages, back.pages)):
            if paper == "pdf" or _is_user_pdf_page(a):
                assert abs(a.width - b.width) < 0.05 and abs(a.height - b.height) < 0.05, (path.name, i)
                assert b.background is not None, (path.name, i)
                factor.append(1.0)
                offset.append(0.0)
            else:
                scale, x_off = _plain_transform(a, 574.0)
                factor.append(scale * PLAIN_PAGE_WIDTH_PT / 574.0)
                offset.append(x_off * PLAIN_PAGE_WIDTH_PT / 574.0)
        for i, (a, b) in enumerate(zip(src.pages, back.pages)):
            ink = _ink(a)
            exp = [(offset[i] + s.points[0].x * factor[i], s.points[0].y * factor[i]) for s in ink]
            got = [_first(s) for s in b.strokes]
            if len(exp) != len(got):
                continue  # strokes starting outside the page may change pages on the way (checked globally)
            pairs, unmatched = _match_pairs(exp, got, 1.0 * factor[i])
            assert not unmatched, f"{path.name} page {i + 1}: {len(unmatched)} strokes moved by more than 1 pt"
            for x, yv in pairs:
                assert _rgb8(ink[x].color) == _rgb8(b.strokes[yv].color)
                assert (ink[x].kind == "highlighter") == (b.strokes[yv].kind == "highlighter")


# --------------------------------------------------------------------------- PoC parity

def _ink_elements(data: bytes, member: str) -> List[Dict[str, Any]]:
    """Independent walk of one ``notes/`` member: one entry per non-empty ink element with
    its sub-path count ``k``, TPL format and first stored point (canvas units)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    out: List[Dict[str, Any]] = []
    tombstone = False
    for rec in protobuf.decode_records(z.read(member)):
        fields = protobuf.decode_message(rec)
        f1 = protobuf.get(fields, 1)
        if f1 is not None and f1.wire_type == protobuf.WIRE_LEN and len(f1.value) == 36 and b"-" in f1.value:
            f3 = protobuf.get(fields, 3)
            tombstone = f3 is not None and f3.value == 1
            continue
        if len(fields) != 1 or fields[0].number != 7:
            tombstone = False
            continue
        body = protobuf.decode_message(fields[0].value)
        if tombstone:
            tombstone = False
            continue
        f9 = protobuf.get(body, 9)
        if f9 is not None and f9.value:
            shape = protobuf.decode_message(f9.value)
            if any(f.number in (1, 2, 3, 4) for f in shape):
                out.append({"k": 1, "fmt": "shape", "first": (0.0, 0.0)})  # one sampled stroke
                continue
        frame = protobuf.get(body, 2)
        if frame is None or not applelz4.is_apple_lz4(frame.value):
            continue
        geo = tpl.decode(applelz4.decompress(frame.value))
        if geo is None:
            continue
        off = (0.0, 0.0)
        f6 = protobuf.get(body, 6)
        if f6 is not None and f6.value:
            sub = protobuf.decode_message(f6.value)
            off = (protobuf.fixed32_float(protobuf.get(sub, 1)) if protobuf.get(sub, 1) else 0.0,
                   protobuf.fixed32_float(protobuf.get(sub, 2)) if protobuf.get(sub, 2) else 0.0)
        if isinstance(geo, tpl.FlatStroke):
            out.append({"k": geo.effective_flags().count(0), "fmt": "flat",
                        "first": (geo.start[0] + off[0], geo.start[1] + off[1])})
        elif isinstance(geo, tpl.RibbonStroke):
            out.append({"k": len(geo.subpaths), "fmt": "ribbon",
                        "first": (geo.points[0][0] + off[0], geo.points[0][1] + off[1])})
        else:
            out.append({"k": len(geo.subpaths), "fmt": "pencil",
                        "first": (geo.points[0][0] + off[0], geo.points[0][1] + off[1])})
    return out


def _session_curves(note: bytes) -> Tuple[List[List[XY]], List[bytes]]:
    """All curves of a note's top-level InkedSpatialHash as anchor+control point lists, plus colours."""
    z = zipfile.ZipFile(io.BytesIO(note))
    name = next(n for n in z.namelist() if n.endswith("Session.plist"))
    pl = plistlib.loads(z.read(name))
    objs = pl["$objects"]
    h = next(o for o in objs if isinstance(o, dict) and "curvespoints" in o)

    def value(v: Any) -> Any:
        return objs[v.data] if isinstance(v, plistlib.UID) else v

    nc = int(value(h["numcurves"]))
    npts = struct.unpack(f"<{nc}i", value(h["curvesnumpoints"]))
    raw = value(h["curvespoints"])
    pts = struct.unpack(f"<{len(raw) // 4}f", raw)
    colors = value(h["curvescolors"])
    curves: List[List[XY]] = []
    off = 0
    for n in npts:
        curves.append([(pts[2 * i], pts[2 * i + 1]) for i in range(off, off + n)])
        off += n
    return curves, [colors[4 * i: 4 * i + 4] for i in range(nc)]


def _poc_note() -> Optional[Path]:
    env = os.environ.get("GNNOTE_POC_NOTE")
    candidates = [Path(env)] if env else []
    if SCRATCH is not None:
        candidates.append(SCRATCH / "poc" / "GoodNotes test import.note")
    samples_root = os.environ.get("GNNOTE_SAMPLES")
    if samples_root:
        candidates.append(Path(samples_root).parent / "poc" / "GoodNotes test import.note")
    for c in candidates:
        if c.is_file():
            return c
    return None


def test_poc_parity_test5_page_3(samples) -> None:
    """Our Test5 page 3 ink matches the proof-of-concept note that imported on an iPad.

    The PoC was made with goodparse, which drops the first stored point of every stroke,
    keeps only the first half of the stored points of flat strokes (controls and ends
    interleaved), skips auto-shape elements and decodes the pencil elements it recognises as
    one identical partial curve; its stride heuristic also truncates one ribbon stroke.
    Hence: every PoC curve pairs in order with a non-pencil ink element (its first point
    within 2 document units of our first anchor, first handles or second anchor; ribbon
    strokes also agree on their last point unless goodparse truncated them; RGB identical,
    highlighter alpha is Notability's own), the only unpaired PoC curves are the pencil ones
    (lying on our pencil ink), and our curve count equals the number of GoodNotes sub-paths
    on the page.
    """
    poc = _poc_note()
    if poc is None:
        pytest.skip("PoC note not available (set GNNOTE_POC_NOTE)")
    path = samples.repo("goodparse") / "samples" / "Test5.goodnotes"
    data = _read(path)
    src = read_goodnotes(data)
    page = src.pages[2]
    width = 574.0
    y_offset = 2 * LEGACY_ASPECT * width  # page 3 sits below two plain slots

    # Locate the notes/ member of page 3 independently: the one whose sub-path total and
    # first stored point agree with the reader's page.
    members = [n for n in zipfile.ZipFile(io.BytesIO(data)).namelist() if n.startswith("notes/")]
    elements: Optional[List[Dict[str, Any]]] = None
    for member in members:
        walk = _ink_elements(data, member)
        if walk and sum(e["k"] for e in walk) == len(page.strokes):
            fx, fy = walk[0]["first"]  # canvas units: 132 per inch
            p0 = page.strokes[0].points[0]
            if abs(fx * 72 / 132 - p0.x) < 0.5 and abs(fy * 72 / 132 - p0.y) < 0.5:
                elements = walk
                break
    assert elements is not None, "page 3 member not identified"

    poc_curves, poc_colors = _session_curves(_read(poc))
    out = convert(data, path.name, Options(paper="plain"))
    all_curves, all_colors = _session_curves(out.data)
    ours = [(c, col) for c, col in zip(all_curves, all_colors)
            if y_offset <= c[0][1] < y_offset + LEGACY_ASPECT * width]
    # the PoC mapped the page's left edge to document x = 0; gnnote applies Notability's
    # paper inset (design.md 4.2), so compare in the PoC's frame
    ours = [([(x - x_inset(width), y - y_offset) for x, y in c], col) for c, col in ours]
    assert len(ours) == sum(e["k"] for e in elements) == len(page.strokes) == 48
    assert len(poc_curves) == 28

    # our strokes per element, in order
    ranges: List[Tuple[Dict[str, Any], List[Tuple[List[XY], bytes]]]] = []
    start = 0
    for element in elements:
        ranges.append((element, ours[start:start + element["k"]]))
        start += element["k"]
    comparable = [(e, mine) for e, mine in ranges if e["fmt"] in ("flat", "ribbon")]
    pencil_points = [p for e, mine in ranges if e["fmt"] == "pencil" for c, _ in mine for p in c[::3]]

    paired = 0
    pencil_curves = 0
    truncated = 0
    ei = 0
    for ci, curve in enumerate(poc_curves):
        poc_first, poc_last = curve[0], curve[-1]
        if ei < len(comparable):
            element, mine = comparable[ei]
            head = mine[0][0][:4]  # first anchor, two handles, second anchor
            if min(math.dist(poc_first, p) for p in head) < 2.0:
                if element["fmt"] == "ribbon":
                    ours_pts = mine[-1][0]
                    if len(curve) in (len(ours_pts) - 3, len(ours_pts)):
                        # goodparse drops the first stored point; a dot is a 4-point dash on
                        # both sides
                        d_last = math.dist(poc_last, ours_pts[-1])
                        assert d_last < 2.0, f"PoC curve {ci}: last point off by {d_last:.2f}"
                    else:
                        # goodparse's stride heuristic truncated this element; what it kept
                        # must still lie on our curve
                        assert len(curve) < len(ours_pts), f"PoC curve {ci}: point count"
                        anchors = ours_pts[::3]
                        for p in curve[::3]:
                            assert min(math.dist(p, a) for a in anchors) < 2.0, f"PoC curve {ci}: off our curve"
                        truncated += 1
                assert poc_colors[ci][:3] == mine[0][1][:3], f"PoC curve {ci}: colour"
                # The PoC kept GoodNotes' highlighter alpha (0.5 = 0x80); the writer uses the
                # alpha Notability itself stores for highlighters (0x6b, design.md 4.2).
                expected_alpha = 0x6B if mine[0][1][3] == 0x6B else poc_colors[ci][3]
                assert mine[0][1][3] == expected_alpha, f"PoC curve {ci}: alpha"
                paired += 1
                ei += 1
                continue
        # not an ink element goodparse decoded faithfully: must be its pencil rendering
        assert min(math.dist(poc_first, p) for p in pencil_points) < 2.0, f"PoC curve {ci} unmatched"
        pencil_curves += 1
    assert paired == len(comparable) == 26, (paired, len(comparable))
    assert pencil_curves == 2
    assert truncated == 1  # the red ribbon stroke goodparse cut after two anchors
