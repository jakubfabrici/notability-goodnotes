"""Microsoft OneNote reader (gnnote.onenote): every sample section of both packagings,
ink geometry / colours / highlighters / pressure, pictures, text, notebook ZIPs, refusals,
sniffing, conversions and the CLI.

Sample files come from the pinned repositories of tests/conftest.py (``SampleSet.ONENOTE_DIRS``);
the exact numbers below were cross-checked against one2html (a separate MIT-licensed
OneNote renderer, run as a subprocess oracle in ``test_one2html_oracle`` when its binary is
available) and against the research probe of docs/onenote.md.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import zipfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from gnnote import formats
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format, to_document
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.model import Document
from gnnote.notability.reader import read_note
from gnnote.onenote import read_onenote
from gnnote.onenote.ink import decode_path
from gnnote.onenote.reader import ONEPKG_MESSAGE
from gnnote.server import build_options

FORMAT_NATIVE = bytes.fromhex("3fdd9a101b91f549a5d01791edc8aed8")
FORMAT_PACKAGE = bytes.fromhex("2fe98d63d4a6c14b9a36b3fc2511a5b7")

# "repository:path inside it" -> (packaging, strokes per page, highlighter strokes, pictures);
# None = not readable (Git LFS pointer files, the encrypted section).  Stroke counts equal
# one2html's (its ink sub-paths); picture counts equal its <img> elements except for
# default-edited/a.one, whose pictures sit in a table (tables are dropped).
EXPECTED: Dict[str, Optional[Tuple[str, List[int], int, int]]] = {
    "onenote.rs:crates/parser/tests/samples/Large Desktop.one": None,
    "onenote.rs:crates/parser/tests/samples/Large OneDrive.one": None,
    "onenote.rs:crates/parser/tests/samples/New Section 1.one": ("fsshttpb", [1], 0, 1),
    "onenote.rs:crates/parser/tests/samples/New Section Group/New Section 1.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/New Section Group/New Section 2.one": ("fsshttpb", [0, 0], 0, 1),
    "onenote.rs:crates/parser/tests/samples/OneNote_RecycleBin/OneNote_DeletedPages.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/Schnelle Notizen.one": ("fsshttpb", [35], 8, 1),
    "onenote.rs:crates/parser/tests/samples/handwriting_recognition.one": ("native", [62, 0], 0, 1),
    "onenote.rs:crates/parser/tests/samples/joplin/Math.one": ("native", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/Notebook created on OneNote App/OneNote_RecycleBin/"
    "OneNote_DeletedPages.one": ("fsshttpb", [56, 1], 0, 2),
    "onenote.rs:crates/parser/tests/samples/joplin/Notebook created on OneNote App/Section A/Section A1.one":
        ("fsshttpb", [0, 0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/Notebook created on OneNote App/Section A/Section B/"
    "Section B1.one": ("fsshttpb", [0, 0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/Notebook created on OneNote App/Section D/Section D1.one":
        ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/Notebook created on OneNote App/Section.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/Notebook with subsections and subpages/Section 1.one":
        ("fsshttpb", [0] * 9, 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/Second/Group Section 1/Group Section 1-a/Subsection 3.one":
        ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/Second/Group Section 1/Subsection 1.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/Second/Group Section 1/Subsection 2.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/Simple notebook/Quick Notes.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/aaa/OneNote_RecycleBin/OneNote_DeletedPages.one":
        ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/aaa/Quick Notes.one": ("fsshttpb", [8, 178, 3, 5, 9, 7], 2, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/audio-test/Quick Notes.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/checkboxes_and_unicode.one": ("native", [0, 0, 0, 0], 0, 2),
    "onenote.rs:crates/parser/tests/samples/joplin/default-edited/a.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/desktop_missing_ink.one": ("native", [218, 34], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/hyperlink_is_broken/Quick Notes.one": ("fsshttpb", [0], 0, 1),
    "onenote.rs:crates/parser/tests/samples/joplin/new_section.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/notebook_with_chinese_char_on_link/Quick Notes.one":
        ("fsshttpb", [0, 0, 0, 0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/joplin/onenote_desktop.one": ("native", [11, 0, 0], 0, 1),
    "onenote.rs:crates/parser/tests/samples/joplin/scaled_ink.one": ("native", [2], 0, 0),
    "onenote.rs:crates/parser/tests/samples/non-legacy/New Section 1 2.one": ("fsshttpb", [1, 0], 0, 1),
    "onenote.rs:crates/parser/tests/samples/non-legacy/New Section 2.one": ("fsshttpb", [0, 0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/non-legacy/New Section 3.one": ("fsshttpb", [0], 0, 0),
    "onenote.rs:crates/parser/tests/samples/onenote-2016/OneWithFileData.one": ("native", [0], 0, 0),
    "joplin:packages/onenote-converter/test-data/Page versions.one": ("native", [0], 0, 0),
    "joplin:packages/onenote-converter/test-data/Printout.one": ("native", [0], 0, 1),
    "joplin:packages/onenote-converter/test-data/ink.one": ("native", [34], 0, 0),
    "joplin:packages/onenote-converter/test-data/onenote-2016/OneWithFileData.one": ("native", [0], 0, 0),
    "joplin:packages/onenote-converter/test-data/single-page/Untitled Section.one": ("fsshttpb", [0], 0, 0),
    "Interop-TestSuites:FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources/AlternativePackaging.one":
        ("fsshttpb", [0], 0, 0),
    "Interop-TestSuites:FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources/Encryption.one": None,
    "Interop-TestSuites:FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources/LargeData.one": ("native", [0], 0, 135),
    "Interop-TestSuites:FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources/OneWithFileData.one":
        ("native", [0], 0, 0),
    "Interop-TestSuites:FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources/OneWithoutFileData.one":
        ("native", [0], 0, 0),
    "libmson:resources/sample-drawing/Section 1.one": ("native", [1], 0, 0),
    "libmson:resources/sample-file-embedded/Neuer Abschnitt 1.one": ("native", [0], 0, 0),
    "libmson:resources/sample-picture/Section 1.one": ("native", [0], 0, 1),
    "libmson:resources/sample-single-text/Section 1.one": ("native", [0], 0, 0),
    "libmson:resources/sample-text/Neuer Abschnitt 1.one": ("native", [0], 0, 0),
    "libmson:resources/sample-text/Section 1.one": ("native", [0], 0, 0),
    "libmson:resources/sample-text/Section 2.one": ("native", [0, 0, 0, 0], 0, 0),
    "obsidian-importer:tests/onenote-file/fixtures/handwriting_recognition.one": ("native", [62, 0], 0, 1),
    "obsidian-importer:tests/onenote-file/fixtures/testOneNote.one": ("native", [0], 0, 3),
    "obsidian-importer:tests/onenote-file/fixtures/testOneNote2016.one": ("native", [0], 0, 0),
    "obsidian-importer:tests/onenote-file/fixtures/testOneNoteEmbeddedWordDoc.one": ("native", [0], 0, 0),
    "obsidian-importer:tests/onenote-file/fixtures/testOneNoteFromOffice365-2.one": ("fsshttpb", [0, 0], 0, 0),
    "obsidian-importer:tests/onenote-file/fixtures/testOneNoteFromOffice365.one": ("fsshttpb", [0, 0], 0, 0),
    "py-onenote-parser:Equipe Euro 2016.one": ("native", [13], 0, 1),
    "py-onenote-parser:Section sans titre.one": ("fsshttpb", [0], 0, 0),
}


def _key(samples, path: Path) -> Optional[str]:
    """``"repository:relative path"`` of a sample, ``None`` when its repository's commit
    is not the pinned one (exact expectations are then withheld)."""
    name = samples.repo_name_of(path)
    if name is None or samples.at_pinned_commit(name) is False:
        return None
    return f"{name}:{Path(path).resolve().relative_to(samples.repo(name).resolve()).as_posix()}"


def _sample(samples, key: str) -> Path:
    name, _, rel = key.partition(":")
    path = samples.repo(name) / rel
    if not path.is_file():
        pytest.skip(f"sample {key} not available")
    return path


def _packaging(data: bytes) -> str:
    return {FORMAT_NATIVE: "native", FORMAT_PACKAGE: "fsshttpb"}.get(data[48:64], "?")


def _check_invariants(doc: Document, label: str) -> None:
    assert doc.pages, label
    for page in doc.pages:
        assert 0 < page.width < 1e6 and 0 < page.height < 1e6, label
        for stroke in page.strokes:
            assert stroke.points and stroke.kind in ("pen", "highlighter"), label
            assert stroke.width > 0 and all(0.0 <= c <= 1.0 for c in stroke.color), label
            for p in stroke.points:
                # the page grows to hold its content (OneNote pages are unbounded canvases)
                assert 0 <= p.x <= page.width and 0 <= p.y <= page.height, (label, p)
                assert p.width > 0, label
        for image in page.images:
            assert image.fmt in ("png", "jpeg") and image.w > 0 and image.h > 0, label
            assert image.data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb") \
                or image.data[:3] == b"\xff\xd8\xff", label
            assert image.x >= 0 and image.y >= 0 and image.x + image.w <= page.width + 1e-6, label
        for box in page.texts:
            assert box.text.strip() and "".join(r.text for r in box.runs) == box.text, label
            assert "\x00" not in box.text and "\ufffc" not in box.text, label
            assert box.w > 0 and box.h > 0 and box.size > 0, label


# --------------------------------------------------------------------------- every sample


def test_every_sample_section_parses(samples) -> None:
    files = samples.onenote_files()
    packagings: Counter = Counter()
    checked = 0
    for path in files:
        data = path.read_bytes()
        key = _key(samples, path)
        label = key or str(path)
        if data.startswith(b"version https://git-lfs"):  # placeholder files in onenote.rs
            with pytest.raises(ValueError, match="not a OneNote file"):
                read_onenote(data)
            continue
        if path.name == "Encryption.one":
            with pytest.raises(ValueError, match="password protected"):
                read_onenote(data)
            continue
        doc = read_onenote(data)
        packagings[_packaging(data)] += 1
        _check_invariants(doc, label)
        expected = EXPECTED.get(key) if key else None
        if expected is not None:
            packaging, strokes, highlighters, pictures = expected
            assert _packaging(data) == packaging, label
            assert [len(p.strokes) for p in doc.pages] == strokes, label
            assert sum(s.kind == "highlighter" for p in doc.pages for s in p.strokes) == highlighters, label
            assert sum(len(p.images) for p in doc.pages) == pictures, label
            checked += 1
    assert packagings["native"] and packagings["fsshttpb"], packagings
    if any(samples.at_pinned_commit(n) for n, _ in samples.ONENOTE_DIRS if samples.repo_commit(n)):
        assert checked > 0


def test_expected_table_covers_the_pinned_corpus(samples) -> None:
    """Every sample of a pinned repository has an expectation (new files are noticed)."""
    missing = []
    for path in samples.onenote_files():
        key = _key(samples, path)
        if key is not None and samples.at_pinned_commit(samples.repo_name_of(path)) and key not in EXPECTED:
            missing.append(key)
    assert not missing, missing


# --------------------------------------------------------------------------- ink


def test_ink_positions_match_the_one2html_reference(samples) -> None:
    """ink.one's bounding box and stroke starts as one2html places them (96 px per inch)."""
    doc = read_onenote(_sample(samples, "joplin:packages/onenote-converter/test-data/ink.one").read_bytes())
    strokes = doc.pages[0].strokes
    px = 0.75  # pt per px
    assert min(p.x for s in strokes for p in s.points) == pytest.approx(36.06 * px, abs=0.01)
    assert min(p.y for s in strokes for p in s.points) == pytest.approx(23.21 * px, abs=0.01)
    assert (strokes[0].points[0].x / px, strokes[0].points[0].y / px) == pytest.approx((51, 151), abs=0.6)
    assert (strokes[1].points[0].x / px, strokes[1].points[0].y / px) == pytest.approx((36, 151), abs=0.6)
    colours = Counter(tuple(round(c * 255) for c in s.color[:3]) for s in strokes)
    assert colours == {(0, 0, 0): 19, (0, 140, 58): 9, (218, 12, 7): 6}
    assert all(s.width == pytest.approx(100 * 72 / 2540) for s in strokes)  # 1 mm pens


def test_scaled_container_with_negative_offset(samples) -> None:
    """InkScalingY 7.18 and OffsetFromParentVert -19.03 half-inches (one2html: 148,199 / 272,147 px)."""
    doc = read_onenote(_sample(samples, "onenote.rs:crates/parser/tests/samples/joplin/scaled_ink.one").read_bytes())
    first = [(s.points[0].x / 0.75, s.points[0].y / 0.75) for s in doc.pages[0].strokes]
    assert first[0] == pytest.approx((148, 199), abs=0.6)
    assert first[1] == pytest.approx((272, 147), abs=0.6)
    tall = max(s.bbox()[3] - s.bbox()[1] for s in doc.pages[0].strokes)
    assert tall > 300  # the scaled stroke spans most of the page


def test_highlighter_colour_width_and_transparency(samples) -> None:
    doc = read_onenote(_sample(samples, "onenote.rs:crates/parser/tests/samples/Schnelle Notizen.one").read_bytes())
    marker = [s for s in doc.pages[0].strokes if s.kind == "highlighter"]
    pens = [s for s in doc.pages[0].strokes if s.kind == "pen"]
    assert len(marker) == 8 and len(pens) == 27
    for s in marker:
        assert tuple(round(c * 255) for c in s.color[:3]) == (250, 243, 32)  # COLORREF 0x0020F3FA
        assert s.color[3] == pytest.approx(1 - 127 / 255)  # transparency 127
        assert s.width == pytest.approx(400 * 72 / 2540)  # 0.56 x 4 mm chisel: the larger side
    assert {tuple(round(c * 255) for c in s.color[:3]) for s in pens} == {(64, 64, 64)}
    assert all(p.width == s.width for s in pens for p in s.points)  # no pressure channel


def test_pressure_modulates_point_widths(samples) -> None:
    doc = read_onenote(_sample(samples, "onenote.rs:crates/parser/tests/samples/joplin/onenote_desktop.one")
                       .read_bytes())
    widths = [p.width / s.width for s in doc.pages[0].strokes for p in s.points]
    assert min(widths) >= 0.25 - 1e-9 and max(widths) <= 1.75 + 1e-9  # 1.5 p + 0.25
    assert max(widths) - min(widths) > 0.3
    assert any("pressure" in w.lower() for w in doc.warnings)


def test_ignore_pressure_keeps_constant_widths(samples) -> None:
    doc = read_onenote(_sample(samples, "onenote.rs:crates/parser/tests/samples/New Section 1.one").read_bytes())
    (stroke,) = doc.pages[0].strokes  # InkIgnorePressure, no pressure channel
    assert len({p.width for p in stroke.points}) == 1


def test_inline_ink_words_are_laid_out_in_their_outline(samples) -> None:
    doc = read_onenote(_sample(samples, "onenote.rs:crates/parser/tests/samples/joplin/desktop_missing_ink.one")
                       .read_bytes())
    assert [len(p.strokes) for p in doc.pages] == [218, 34]
    assert any("32 handwritten word(s)" in w and "approximately" in w for w in doc.warnings)
    page = doc.pages[0]
    # the first outline (offset 8.132 / 3.067 half-inches, 7.149 wide) holds four lines of
    # handwritten words: they now sit inside it, not 208 half-inches further down
    left, top, width = 8.132 * 36, 3.067 * 36, 7.149 * 36
    words = [s for s in page.strokes if left - 5 <= s.bbox()[0] and s.bbox()[2] <= left + width + 60
             and top - 5 <= s.bbox()[1] <= top + 4 * 40]
    assert len(words) >= 50
    assert page.height < 2000  # no stroke left at its original far-away position


def test_ink_path_decoding() -> None:
    # count 3 (= 6 / 2), then +5, -3, +64 in sign-in-bit-0 varints
    assert decode_path(bytes([6, 10, 7, 0x80, 0x01])) == [5, -3, 64]
    assert decode_path(b"") == []
    from gnnote.onenote.common import Damaged
    with pytest.raises(Damaged):
        decode_path(bytes([200, 1, 2]))  # announces 100 values, holds 1
    with pytest.raises(Damaged):
        decode_path(bytes([6, 2, 4, 6]), max_values=2)
    with pytest.raises(Damaged):
        decode_path(bytes([2]) + b"\xff" * 12)  # varint longer than 64 bits


# --------------------------------------------------------------------------- pictures, text


def test_pictures_of_both_packagings(samples) -> None:
    doc = read_onenote(_sample(samples, "onenote.rs:crates/parser/tests/samples/Schnelle Notizen.one").read_bytes())
    (image,) = doc.pages[0].images
    assert image.fmt == "png" and image.data.startswith(b"\x89PNG")  # BLOB length prefix stripped
    assert (image.w, image.h) == pytest.approx((360.0, 360.0), abs=0.5)  # PictureWidth capped by LayoutMaxWidth
    doc = read_onenote(_sample(samples, "obsidian-importer:tests/onenote-file/fixtures/testOneNote.one").read_bytes())
    assert sorted(round(i.w) for i in doc.pages[0].images) == [237, 282, 352]
    assert all(i.data.startswith(b"\x89PNG") for i in doc.pages[0].images)


def test_printout_pages_become_pictures(samples) -> None:
    doc = read_onenote(_sample(samples, "joplin:packages/onenote-converter/test-data/Printout.one").read_bytes())
    (image,) = doc.pages[0].images
    assert image.fmt == "png" and image.w == pytest.approx(17.002 * 36, abs=0.1)
    assert any("printout" in w for w in doc.warnings)
    assert any("attached file" in w for w in doc.warnings)


def test_typed_text_becomes_text_boxes_with_runs(samples) -> None:
    key = ("onenote.rs:crates/parser/tests/samples/joplin/Notebook created on OneNote App/OneNote_RecycleBin/"
           "OneNote_DeletedPages.one")
    doc = read_onenote(_sample(samples, key).read_bytes())
    texts = {box.text.split("\n")[0]: box for box in doc.pages[0].texts}
    assert "Created on OneNote App" in texts  # the page title
    body = next(box for box in doc.pages[0].texts if box.text.startswith("Bold text"))
    runs = {r.text.strip(): r for r in body.runs if r.text.strip()}
    assert runs["Bold text"].bold and not runs["Bold text"].italic
    assert runs["Italic text"].italic and not runs["Italic text"].bold
    assert "\u2022 A" in body.text  # bulleted list
    assert any("text box" in w and "approximate" in w for w in doc.warnings)


def test_tables_and_note_tags_are_reported(samples) -> None:
    doc = read_onenote(_sample(samples, "onenote.rs:crates/parser/tests/samples/joplin/default-edited/a.one")
                       .read_bytes())
    assert any("table" in w for w in doc.warnings)
    doc = read_onenote(_sample(samples, "onenote.rs:crates/parser/tests/samples/joplin/checkboxes_and_unicode.one")
                       .read_bytes())
    assert any("note tag" in w for w in doc.warnings)


def test_section_title(samples) -> None:
    doc = read_onenote(_sample(samples, "onenote.rs:crates/parser/tests/samples/Schnelle Notizen.one").read_bytes())
    assert doc.title == "Scribbles"  # SectionDisplayName "Scribbles.one"
    path = _sample(samples, "joplin:packages/onenote-converter/test-data/ink.one")
    assert read_onenote(path.read_bytes()).title == ""  # a desktop section is named by its file
    assert to_document(path.read_bytes(), "Physics.one").title == "Physics"


# --------------------------------------------------------------------------- refusals


def test_encrypted_section_is_refused(samples) -> None:
    path = _sample(samples, "Interop-TestSuites:FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources/Encryption.one")
    with pytest.raises(ValueError, match="password protected"):
        read_onenote(path.read_bytes())
    with pytest.raises(ValueError, match="password protected"):
        convert(path.read_bytes(), "Encryption.one", Options(target="notability"))


def test_table_of_contents_alone_is_refused(samples) -> None:
    (toc,) = [p for p in samples.onenote_files("*.onetoc2") if p.parent.name == "Resources"
              and p.name == "Open Notebook.onetoc2"] or [None]
    if toc is None:
        pytest.skip("Interop-TestSuites table of contents not available")
    with pytest.raises(ValueError, match="table of contents"):
        read_onenote(toc.read_bytes())


def test_onepkg_is_refused_with_onedrive_instructions(samples) -> None:
    synthetic = b"MSCF" + bytes(40) + b"Notebook/Open Notebook.onetoc2\x00Notebook/Math.one\x00"
    with pytest.raises(ValueError, match="onedrive.com"):
        read_onenote(synthetic)
    assert detect_format("Notebook.onepkg", synthetic) == "onenote"
    with pytest.raises(ValueError, match=r"\.onepkg"):
        convert(synthetic, "Notebook.onepkg", Options(target="notability"))
    real = [p for p in samples.onenote_files("*.onepkg")] if _has_onepkg(samples) else []
    for path in real:  # makecab-made fixtures of the importer projects
        with pytest.raises(ValueError, match="onedrive.com"):
            read_onenote(path.read_bytes())
    assert "OneDrive" in ONEPKG_MESSAGE


def _has_onepkg(samples) -> bool:
    try:
        return bool(samples.onenote_files("*.onepkg"))
    except pytest.skip.Exception:
        return False


@pytest.mark.parametrize("data, message", [
    (b"", "not a OneNote file"),
    (b"hello world" * 100, "not a OneNote file"),
    (b"version https://git-lfs.github.com/spec/v1\noid sha256:00\nsize 1\n", "not a OneNote file"),
    (b"MSCF" + bytes(100), "not a OneNote file"),  # a CAB that names no OneNote file
])
def test_not_onenote_is_a_value_error(data: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        read_onenote(data)


def test_header_only_files_are_value_errors() -> None:
    one = bytes.fromhex("e4525c7b8cd8a74daeb15378d02996d3")
    native = one + bytes(32) + FORMAT_NATIVE + bytes(1024 - 64)
    with pytest.raises(ValueError, match="damaged OneNote file"):
        read_onenote(native)
    package = one + bytes(32) + FORMAT_PACKAGE + bytes(8)
    with pytest.raises(ValueError, match="damaged OneNote file"):
        read_onenote(package)
    with pytest.raises(ValueError):
        read_onenote(native[:100])  # truncated header


def test_read_onenote_expects_bytes() -> None:
    with pytest.raises(TypeError):
        read_onenote("not bytes")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- notebook ZIPs


def _zip(entries: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def test_onedrive_notebook_zip_merges_sections_in_toc_order(samples) -> None:
    """The notebook folder OneNote for the web created, zipped the way onedrive.com
    downloads a folder: sections, section groups, one .onetoc2 per folder, recycle bin."""
    base = _sample(samples, "onenote.rs:crates/parser/tests/samples/joplin/Notebook created on OneNote App/"
                            "Section.one").parent
    entries = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            entries["Notebook created on OneNote App/" + path.relative_to(base).as_posix()] = path.read_bytes()
    data = _zip(dict(reversed(list(entries.items()))))  # member order must not matter
    assert detect_format("Notebook created on OneNote App.zip", data) == "onenote"
    doc = read_onenote(data)
    assert doc.title == "Notebook created on OneNote App"
    assert len(doc.pages) == 1 + 2 + 2 + 1  # Section, Section A1, Section B1, Section D1; no recycle bin
    assert any("4 sections were merged" in w and w.endswith("Section, Section A1, Section B1, Section D1")
               for w in doc.warnings)
    assert any("recycle bin" in w for w in doc.warnings)
    result = convert(data, "Notebook created on OneNote App.zip", Options(target="notability"))
    assert result.source_format == "onenote" and result.filename == "Notebook created on OneNote App.note"
    assert result.stats["pages"] == 6


def test_zip_order_follows_the_table_of_contents_not_file_names(samples) -> None:
    folder = _sample(samples, "Interop-TestSuites:FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources/"
                              "OneWithFileData.one").parent
    names = ["OneWithoutFileData.one", "OneWithFileData.one", "AlternativePackaging.one", "Encryption.one",
             "Open Notebook.onetoc2"]
    data = _zip({name: (folder / name).read_bytes() for name in names})
    doc = read_onenote(data)
    # TOC: OneWithFileData (1), OneWithoutFileData (2), Encryption (3, password protected);
    # AlternativePackaging is not in the TOC and comes after the listed sections
    merged = next(w for w in doc.warnings if "merged" in w)
    assert merged.endswith("OneWithFileData, OneWithoutFileData, AlternativePackaging"), merged
    assert any("Password-protected" in w and "Encryption" in w for w in doc.warnings)
    single = [read_onenote((folder / n).read_bytes()) for n in ("OneWithFileData.one", "OneWithoutFileData.one",
                                                              "AlternativePackaging.one")]
    assert [p.texts[0].text for p in doc.pages] == [d.pages[0].texts[0].text for d in single]


def test_zip_without_toc_uses_file_names_and_warns(samples) -> None:
    sections = {
        "b.one": _sample(samples, "joplin:packages/onenote-converter/test-data/ink.one").read_bytes(),
        "a.one": _sample(samples, "libmson:resources/sample-drawing/Section 1.one").read_bytes(),
    }
    doc = read_onenote(_zip(sections))
    assert [len(p.strokes) for p in doc.pages] == [1, 34]
    assert any("file-name order" in w for w in doc.warnings)
    assert doc.title == ""  # several sections at the root: the ZIP's file name names it
    assert convert(_zip(sections), "Lectures.zip", Options(target="notability")).filename == "Lectures.note"


def test_zip_error_paths(samples) -> None:
    ink = _sample(samples, "joplin:packages/onenote-converter/test-data/ink.one").read_bytes()
    with pytest.raises(ValueError, match="no .one section files"):
        read_onenote(_zip({"readme.txt": b"x"}))
    with pytest.raises(ValueError, match="only OneNote's recycle bin"):
        read_onenote(_zip({"N/OneNote_RecycleBin/OneNote_DeletedPages.one": ink}))
    with pytest.raises(ValueError, match=r"none of the notebook's sections could be read \(not a OneNote file"):
        read_onenote(_zip({"N/broken.one": b"garbage"}))
    damaged = read_onenote(_zip({"N/broken.one": b"garbage", "N/ink.one": ink}))
    assert len(damaged.pages) == 1
    assert "Damaged section(s) could not be read and were skipped: broken" in damaged.warnings
    many = read_onenote(_zip({**{f"N/s{i:02}.one": b"garbage" for i in range(25)}, "N/ink.one": ink}))
    assert len(many.pages) == 1
    assert [w for w in many.warnings if w.startswith("Damaged")] == [
        "Damaged section(s) could not be read and were skipped: "
        "s00, s01, s02, s03, s04, s05, s06, s07, s08, s09, ... (25 in all)"]
    with pytest.raises(ValueError, match="not a readable ZIP"):
        read_onenote(b"PK\x03\x04" + bytes(30))


def test_zip_member_size_guard(monkeypatch: pytest.MonkeyPatch, samples) -> None:
    from gnnote.onenote import reader
    ink = _sample(samples, "joplin:packages/onenote-converter/test-data/ink.one").read_bytes()
    small = _sample(samples, "libmson:resources/sample-drawing/Section 1.one").read_bytes()
    monkeypatch.setattr(reader, "MAX_MEMBER_BYTES", len(small) + 1)
    doc = read_onenote(_zip({"ink.one": ink, "small.one": small}))
    assert len(doc.pages) == 1 and any(w.startswith("File(s) too large once unpacked") and w.endswith(": ink.one")
                                       for w in doc.warnings)


# --------------------------------------------------------------------------- sniffing, registry


def test_registry_entry_and_sniffing(samples) -> None:
    fmt = formats.get("onenote")
    assert fmt.readable and not fmt.writable and fmt.extension == ".one"
    native = _sample(samples, "joplin:packages/onenote-converter/test-data/ink.one").read_bytes()
    package = _sample(samples, "onenote.rs:crates/parser/tests/samples/Schnelle Notizen.one").read_bytes()
    for data in (native, package):
        assert detect_format("x.one", data) == "onenote"
        assert detect_format("renamed.bin", data) == "onenote"  # by content
    zipped = _zip({"Notebook/Section.one": package, "Notebook/Open Notebook.onetoc2": b"x"})
    assert detect_format("Notebook.zip", zipped) == "onenote"  # the web page passes the .zip name
    assert formats.default_target("onenote") == "notability"
    assert not fmt.sniff(b"PK\x03\x04", ["a/Session.plist"]) and not fmt.sniff(b"%PDF-1.4", None)


def test_onenote_is_not_a_target() -> None:
    with pytest.raises(ValueError, match="cannot be written"):
        Options(target="onenote").validate()
    with pytest.raises(ValueError, match="OneNote files can be read but not written"):
        build_options({"to": "onenote"})


def test_cli_formats_and_convert(tmp_path: Path, samples, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["formats"]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("onenote"))
    assert "OneNote" in line and ".one" in line and "read only" in line
    src = tmp_path / "Lecture.one"
    src.write_bytes(_sample(samples, "joplin:packages/onenote-converter/test-data/ink.one").read_bytes())
    assert main(["convert", str(src), "--to", "notability"]) == 0
    out = capsys.readouterr().out
    assert "onenote -> notability" in out and "34 strokes" in out
    back = read_note((tmp_path / "Lecture.note").read_bytes())
    assert back.title == "Lecture" and sum(len(p.strokes) for p in back.pages) == 34
    assert main(["convert", str(src), "--to", "onenote"]) == 2
    assert "OneNote files can be read but not written" in capsys.readouterr().err
    assert main(["info", str(src)]) == 0
    assert "Format:  onenote" in capsys.readouterr().out


# --------------------------------------------------------------------------- conversions


def test_inked_samples_convert_to_notability_and_goodnotes_and_back(samples) -> None:
    converted = 0
    for path in samples.onenote_files():
        data = path.read_bytes()
        if path.name == "Encryption.one" or data.startswith(b"version https://git-lfs"):
            continue
        doc = read_onenote(data)
        strokes = [len(p.strokes) for p in doc.pages]
        if not sum(strokes):
            continue
        note = convert(data, path.name, Options(target="notability"))
        back = read_note(note.data)
        assert len(back.pages) == len(doc.pages), path
        assert sum(len(p.strokes) for p in back.pages) == sum(strokes), path
        gn = convert(data, path.name, Options(target="goodnotes"))
        back = read_goodnotes(gn.data)
        assert [len(p.strokes) for p in back.pages] == strokes, path
        highlighters = sum(s.kind == "highlighter" for p in doc.pages for s in p.strokes)
        assert sum(s.kind == "highlighter" for p in back.pages for s in p.strokes) == highlighters, path
        converted += 1
    assert converted >= 1


# --------------------------------------------------------------------------- one2html oracle


def _one2html_binary(samples) -> Optional[str]:
    candidates = [os.environ.get("GNNOTE_ONE2HTML"), shutil.which("one2html"),
                  str(samples.root / "one2html" / "target" / "release" / "one2html")]
    return next((c for c in candidates if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


_INK_PATH = re.compile(r'<path d="(M [^"]*)" fill="none" opacity="([0-9.]+)" stroke="([^"]+)"')


def _one2html_strokes(binary: str, path: Path, out: Path) -> Optional[Tuple[List[int], Counter]]:
    """Ink strokes per page (sorted) and their (r, g, b, opacity) as one2html renders them."""
    try:
        proc = subprocess.run([binary, "-i", str(path), "-o", str(out)], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    per_page: List[int] = []
    colours: Counter = Counter()
    for html in out.rglob("*.html"):
        if html.parent == out:
            continue  # the section index pages
        count = 0
        for d, opacity, stroke in _INK_PATH.findall(html.read_text(encoding="utf-8", errors="replace")):
            n = d.count("M ")
            count += n
            m = re.match(r"rgb\((\d+), (\d+), (\d+)\)", stroke)
            rgb = tuple(int(v) for v in m.groups()) if m else (0, 0, 0)  # WindowText = automatic = black
            colours[rgb + (round(float(opacity), 2),)] += n
        per_page.append(count)
    return sorted(per_page), colours


def test_one2html_oracle(samples, tmp_path: Path) -> None:
    binary = _one2html_binary(samples)
    if binary is None:
        pytest.skip("one2html not available (set GNNOTE_ONE2HTML or put it on PATH)")
    compared = 0
    for i, path in enumerate(samples.onenote_files()):
        data = path.read_bytes()
        if path.name == "Encryption.one" or data.startswith(b"version https://git-lfs"):
            continue
        oracle = _one2html_strokes(binary, path, tmp_path / str(i))
        if oracle is None:
            continue  # one2html cannot read every sample
        doc = read_onenote(data)
        per_page, colours = oracle
        assert sorted(len(p.strokes) for p in doc.pages) == per_page, path
        mine = Counter(tuple(round(c * 255) for c in s.color[:3]) + (round(s.color[3], 2),)
                       for p in doc.pages for s in p.strokes)
        assert mine == colours, path
        compared += 1
    assert compared >= 20
