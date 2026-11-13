"""tools/make_windows_testkit.py: the kit is written, every file reads back with gnnote as the
format it claims to be, and the test notebook keeps what the checklist tells testers to look for."""
from __future__ import annotations

import importlib.util
import math
import plistlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from gnnote import formats as _formats
from gnnote.convert import detect_format, to_document
from gnnote.formats import NoteFormat
from gnnote.model import Document, Stroke

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "make_windows_testkit.py"


def _load_tool():
    name = "make_windows_testkit"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while the class is created
    spec.loader.exec_module(module)
    return module


kit = _load_tool()


@pytest.fixture(scope="module")
def kit_dir(tmp_path_factory) -> Path:
    """The kit written once by running the script the way the docs say (another working dir)."""
    out = tmp_path_factory.mktemp("kit") / "out"
    proc = subprocess.run([sys.executable, str(TOOL), str(out)], cwd=str(out.parent),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    listed = [Path(line).name for line in proc.stdout.splitlines()]
    assert listed == [f.name for f in kit.kit_files()] + ["CHECKLIST.txt"]
    return out


def _read(path: Path) -> Document:
    return to_document(path.read_bytes(), path.name)


def _length(stroke: Stroke) -> float:
    return sum(math.dist((a.x, a.y), (b.x, b.y)) for a, b in zip(stroke.points, stroke.points[1:]))


def _widths(stroke: Stroke) -> List[float]:
    return [p.width for p in stroke.points]


def _pens(page) -> List[Stroke]:
    """The three pressure strokes of page 1: the long ones (the 72 pt line and the 116 pt
    highlighter are shorter)."""
    return [s for s in page.strokes if s.kind == "pen" and _length(s) > 300]


def _inch_line(page) -> Stroke:
    lines = [s for s in page.strokes if s.kind == "pen" and 60 < _length(s) < 80]
    assert len(lines) == 1, [(s.kind, _length(s)) for s in page.strokes]
    return lines[0]


# ---------------------------------------------------------------------------------------------
# the notebook built in code


def test_document_matches_the_checklist_description() -> None:
    doc = kit.build_document()
    assert doc.title == kit.TITLE
    assert [(p.width, p.height) for p in doc.pages] == [kit.PAGE_SIZE] * 2
    page1, page2 = doc.pages
    assert [t.text for t in page1.texts] == [kit.LABEL]
    assert [s.kind for s in page1.strokes] == ["pen", "pen", "pen", "highlighter", "pen"]
    pens = page1.strokes[:3]
    assert [s.color for s in pens] == [color for _, color, _ in kit.PENS]
    assert [s.width for s in pens] == [1.0, 2.5, 5.0]
    for stroke in pens:
        widths = _widths(stroke)
        # pressed lightly at both ends, hardest in the middle
        assert widths[0] == widths[-1] == pytest.approx(kit.MIN_PRESSURE * stroke.width, abs=1e-3)
        assert max(widths) == pytest.approx(stroke.width, rel=1e-3)
        assert widths.index(max(widths)) == pytest.approx(len(widths) / 2, abs=1)
        assert _length(stroke) > kit.PEN_LENGTH
    highlighter = page1.strokes[3]
    assert highlighter.color == (1.0, 0.85, 0.0, 0.5) and highlighter.width == 16.0
    box = page1.texts[0]
    assert box.y < highlighter.points[0].y < box.y + box.h  # across the label
    line = page1.strokes[4]
    assert len(line.points) == 2 and line.points[0].y == line.points[1].y
    assert _length(line) == 72.0
    assert page2.paper == "lined" and len(page2.strokes) == 2 and not page2.texts
    loop = page2.strokes[1]
    assert (loop.points[0].x, loop.points[0].y) == (loop.points[-1].x, loop.points[-1].y)
    # every call builds a new notebook (writers append warnings to it)
    assert kit.build_document() is not doc and kit.build_document().pages[0].strokes[0] is not pens[0]


def test_every_point_lies_on_the_page() -> None:
    for page in kit.build_document().pages:
        for stroke in page.strokes:
            x0, y0, x1, y1 = stroke.bbox()
            assert 0 < x0 and x1 < page.width and 0 < y0 and y1 < page.height


# ---------------------------------------------------------------------------------------------
# the files, read back with gnnote


def test_kit_files_and_formats(kit_dir: Path) -> None:
    names = sorted(p.name for p in kit_dir.iterdir())
    expected = {"CHECKLIST.txt", "gnnote-ink-annots.pdf", "gnnote-ink-flat.pdf", "gnnote-test.goodnotes",
                "gnnote-test.note", "gnnote-test-pdfpages.note", "gnnote-test.xopp"}
    write = _formats.FORMATS.get("styluswrite")
    if write is not None and write.writable:
        expected.add("gnnote-test" + write.extension)
    assert set(names) == expected
    for item in kit.kit_files():
        data = (kit_dir / item.name).read_bytes()
        assert detect_format(item.name, data) == item.format_id
        # the content alone decides, whatever the file is called
        assert detect_format("renamed.bin", data) == item.format_id


def test_annotated_pdf_keeps_the_ink_editable(kit_dir: Path) -> None:
    doc = _read(kit_dir / "gnnote-ink-annots.pdf")
    assert doc.title == kit.TITLE
    assert [(round(p.width, 2), round(p.height, 2)) for p in doc.pages] == [(595.28, 841.89)] * 2
    assert [len(p.strokes) for p in doc.pages] == [5, 2]
    page1 = doc.pages[0]
    assert [s.kind for s in page1.strokes].count("highlighter") == 1
    highlighter = next(s for s in page1.strokes if s.kind == "highlighter")
    assert highlighter.color[:3] == pytest.approx((1.0, 0.85, 0.0), abs=0.01)
    assert highlighter.width == pytest.approx(16.0, abs=0.01)
    pens = _pens(page1)
    assert [tuple(round(c, 2) for c in s.color[:3]) for s in pens] == [(0.0, 0.0, 0.0), (0.85, 0.1, 0.1),
                                                                         (0.1, 0.25, 0.85)]
    # the caveat docs/windows.md states: a PDF ink annotation has one width per stroke, so the
    # pressure profile is lost on the way back (the median width remains)
    for stroke, (_, _, nominal) in zip(pens, kit.PENS):
        assert len(set(_widths(stroke))) == 1
        assert kit.MIN_PRESSURE * nominal < stroke.width < nominal
    assert pens[0].width < pens[1].width < pens[2].width
    line = _inch_line(page1)
    assert _length(line) == pytest.approx(72.0, abs=0.01)
    assert line.points[0].x == pytest.approx(72.0, abs=0.01)
    assert line.points[0].y == pytest.approx(450.0, abs=0.01)
    # the text is real PDF text in the page, not an annotation: it stays background
    assert not page1.texts
    assert all(p.background is not None for p in doc.pages)


def test_flattened_pdf_has_no_editable_ink(kit_dir: Path) -> None:
    doc = _read(kit_dir / "gnnote-ink-flat.pdf")
    assert len(doc.pages) == 2
    # the ink is part of the page content: shown as the pages' background, nothing to edit
    assert sum(len(p.strokes) for p in doc.pages) == 0
    assert all(p.background is not None for p in doc.pages)


def test_pdfs_with_mupdf(kit_dir: Path) -> None:
    fitz = pytest.importorskip("pymupdf")
    annots = fitz.open(str(kit_dir / "gnnote-ink-annots.pdf"))
    flat = fitz.open(str(kit_dir / "gnnote-ink-flat.pdf"))
    assert annots.page_count == flat.page_count == 2
    kinds = [[a.type[1] for a in page.annots()] for page in annots]
    assert kinds == [["Ink"] * 5, ["Ink"] * 2]
    assert [list(page.annots()) for page in flat] == [[], []]
    for document in (annots, flat):
        assert document[0].get_text().strip() == kit.LABEL
        for page in document:
            assert page.rect.width == pytest.approx(kit.PAGE_SIZE[0], abs=0.01)
    # both look the same: the annotations carry a drawing of the flattened ink
    for a, f in zip(annots, flat):
        pa, pf = a.get_pixmap(dpi=36), f.get_pixmap(dpi=36)
        assert len(pa.samples) == len(pf.samples)
        assert not [i for i, (x, y) in enumerate(zip(pa.samples, pf.samples)) if abs(x - y) > 40]
        assert sum(1 for b in pa.samples if b < 128) > 100  # there is ink on the page


def test_goodnotes_file(kit_dir: Path) -> None:
    doc = _read(kit_dir / "gnnote-test.goodnotes")
    assert doc.title == kit.TITLE
    assert [len(p.strokes) for p in doc.pages] == [5, 2]
    assert [t.text for t in doc.pages[0].texts] == [kit.LABEL]
    assert [s.kind for s in doc.pages[0].strokes].count("highlighter") == 1
    assert doc.pages[1].paper == "lined"
    # GoodNotes' flat pen has one width per stroke (the writer says so in a warning)
    for stroke in _pens(doc.pages[0]):
        assert len({round(w, 3) for w in _widths(stroke)}) == 1
    assert _length(_inch_line(doc.pages[0])) == pytest.approx(72.0, abs=0.05)


@pytest.mark.parametrize("name", ["gnnote-test.note", "gnnote-test-pdfpages.note"])
def test_notability_files_keep_pressure(kit_dir: Path, name: str) -> None:
    doc = _read(kit_dir / name)
    assert doc.title == kit.TITLE
    assert [len(p.strokes) for p in doc.pages] == [5, 2]
    assert [t.text for t in doc.pages[0].texts] == [kit.LABEL]
    assert [s.kind for s in doc.pages[0].strokes].count("highlighter") == 1
    pens = _pens(doc.pages[0])
    assert len(pens) == 3
    for stroke in pens:
        widths = _widths(stroke)
        assert max(widths) / min(widths) > 2.0  # thin ends, thick middle


def test_notability_kit_files_use_both_paper_modes() -> None:
    """gnnote-test.note is the default plain paper, the paper gnnote's ink has been seen working
    on; the PDF-page mode, untested on devices, gets a file of its own instead of replacing it.
    Each purpose names what is untested."""
    notes = [f for f in kit.kit_files() if f.format_id == "notability"]
    assert [(f.name, f.paper) for f in notes] == [("gnnote-test.note", "plain"),
                                                  ("gnnote-test-pdfpages.note", "pdf")]
    assert kit.PDF_PAGES_UNTESTED in notes[1].purpose
    assert kit.PLAIN_NOTE_PROVEN in notes[0].purpose and kit.PLAIN_NOTE_UNTESTED in notes[0].purpose


def test_plain_notability_claim_stays_within_what_an_ipad_has_shown(kit_dir: Path) -> None:
    """README.md confirms only gnnote's ink in a one-page test, and docs/notability-format.md
    section 13 calls a hand-written TextBlockMediaObject untested.  gnnote-test.note has a text
    box and two pages, so nothing may present it as a kind of file already seen working, and
    what it adds beyond the test must be named wherever the file is described."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "confirmed on the author's iPad with a one-page test" in readme
    spec = " ".join((ROOT / "docs" / "notability-format.md").read_text(encoding="utf-8").split())
    assert "`TextBlockMediaObject` objects in a 10.4-format file is untested (only ink has been proven)" in spec
    # the file really goes beyond that test: a Notability text block and a second page
    with zipfile.ZipFile(kit_dir / "gnnote-test.note") as archive:
        session = next(n for n in archive.namelist() if n.endswith("/Session.plist"))
        objects = plistlib.loads(archive.read(session))["$objects"]
    classes = {o.get("$classname") for o in objects if isinstance(o, dict)}
    assert "TextBlockMediaObject" in classes
    assert len(_read(kit_dir / "gnnote-test.note").pages) == 2
    assert "one-page test" in kit.PLAIN_NOTE_PROVEN
    assert "text box" in kit.PLAIN_NOTE_UNTESTED and "second page" in kit.PLAIN_NOTE_UNTESTED
    text = _checklist(kit_dir)
    docs = (ROOT / "docs" / "windows.md").read_text(encoding="utf-8")
    for where in (text, docs):
        flat = " ".join(where.split())
        assert "already seen working" not in flat and "the kind of .note" not in flat
        assert "the kind of `.note`" not in flat
    notability = " ".join(text.split("\nNotability on the iPad (optional)\n")[1].split("\n\n")[0].split())
    plain = notability.split(" 2. Open ")[0]
    assert kit.PLAIN_NOTE_PROVEN in plain and kit.PLAIN_NOTE_UNTESTED in plain
    assert "write down any error message word for word" in plain
    files = " ".join(text.split("Files in this folder\n")[1].split("\n\n")[0].split())
    assert f"gnnote-test.note: for Notability on the iPad: {kit.PLAIN_NOTE_PROVEN}; {kit.PLAIN_NOTE_UNTESTED}" in files


def test_plain_notability_file_uses_notability_plain_paper(kit_dir: Path) -> None:
    """Ink on Notability's plain paper: no PDF pages, Notability's own page size (612 x 803.25
    pt), the A4 drawing scaled to its height (about 95 %, as CHECKLIST.txt says) and page 2
    without lines."""
    doc = _read(kit_dir / "gnnote-test.note")
    assert [(round(p.width, 2), round(p.height, 2)) for p in doc.pages] == [(612.0, 803.25)] * 2
    assert not doc.pdfs and all(p.background is None for p in doc.pages)
    assert [p.paper for p in doc.pages] == ["plain", "plain"]
    scale = 803.25 / kit.PAGE_SIZE[1]
    assert round(1 - scale, 2) == 0.05
    assert _length(_inch_line(doc.pages[0])) == pytest.approx(72.0 * scale, abs=0.05)
    assert [len(p.strokes) for p in doc.pages] == [5, 2]


def test_pdf_page_notability_file_is_the_notebook_the_checklist_describes(kit_dir: Path) -> None:
    """A4 pages, lined paper on page 2 and a line of exactly 1 inch: PDF-backed pages
    (``paper="pdf"``) keep the notebook as it was built."""
    doc = _read(kit_dir / "gnnote-test-pdfpages.note")
    assert [(round(p.width, 2), round(p.height, 2)) for p in doc.pages] == [(595.28, 841.89)] * 2
    assert all(p.background is not None for p in doc.pages)
    line = _inch_line(doc.pages[0])
    assert _length(line) == pytest.approx(72.0, abs=0.01)
    assert (line.points[0].x, line.points[0].y) == pytest.approx(kit.INCH_LINE[0], abs=0.01)
    pymupdf = pytest.importorskip("pymupdf")
    plain, lined = (doc.pdfs[p.background.pdf_id] for p in doc.pages)
    rules = []
    for data in (plain, lined):
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            page = pdf[doc.pages[0].background.page_index]
            rules.append([d for d in page.get_drawings()
                          if d["rect"].width > 300 and d["rect"].height < 1])  # long horizontal rules
    assert not rules[0] and len(rules[1]) > 10


def test_xournalpp_file_keeps_pressure_and_units(kit_dir: Path) -> None:
    doc = _read(kit_dir / "gnnote-test.xopp")
    assert [(round(p.width, 2), round(p.height, 2)) for p in doc.pages] == [(595.28, 841.89)] * 2
    assert [len(p.strokes) for p in doc.pages] == [5, 2]
    assert [t.text for t in doc.pages[0].texts] == [kit.LABEL]
    assert doc.pages[1].paper == "lined"
    for stroke, (_, _, nominal) in zip(_pens(doc.pages[0]), kit.PENS):
        widths = _widths(stroke)
        assert min(widths) == pytest.approx(kit.MIN_PRESSURE * nominal, rel=0.05)
        assert max(widths) == pytest.approx(nominal, rel=0.05)
    line = _inch_line(doc.pages[0])
    assert _length(line) == pytest.approx(72.0, abs=0.01)


def test_stylus_labs_write_file_when_registered(kit_dir: Path) -> None:
    write = _formats.FORMATS.get("styluswrite")
    if write is None or not write.writable:
        assert not list(kit_dir.glob("*.svgz"))
        pytest.skip("no styluswrite writer registered")
    doc = _read(kit_dir / ("gnnote-test" + write.extension))
    assert [len(p.strokes) for p in doc.pages] == [5, 2]


# ---------------------------------------------------------------------------------------------
# the checklist


def _checklist(kit_dir: Path) -> str:
    raw = (kit_dir / "CHECKLIST.txt").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")  # Windows line ends throughout
    return raw.decode("ascii").replace("\r\n", "\n")


def test_checklist_names_exactly_the_files_of_the_kit(kit_dir: Path) -> None:
    text = _checklist(kit_dir)
    files = text.split("Files in this folder\n")[1].split("\n\n")[0]
    listed = [line.strip().split(":")[0] for line in files.splitlines()[1:] if line.startswith("  ") and
              not line.startswith("      ")]
    assert sorted(listed) == sorted(p.name for p in kit_dir.iterdir())
    mentioned = {word.strip(".,:;()'\"") for word in text.split() if word.startswith("gnnote-")}
    assert mentioned <= {p.name for p in kit_dir.iterdir()}, mentioned
    for section in ("Goodnotes for Windows", "Microsoft Edge", "Noteastic", "Microsoft Journal",
                    "LiquidText", "Xournal++ and Rnote", "Before you start (privacy)"):
        assert "\n" + section in text
    assert max(len(line) for line in text.splitlines()) <= kit.WIDTH
    assert kit.LABEL in text and "1 inch (2.54 cm)" in text


def test_checklist_keeps_numbers_with_their_units(kit_dir: Path) -> None:
    text = _checklist(kit_dir)
    assert "up to 5 MB only" in " ".join(text.split())
    assert "5 MB" in text  # on one line
    assert not [line for line in text.splitlines() if line.lstrip().startswith("MB")]
    # the glue itself: a forced break right at the glued pair moves both words to the next line
    head = "x" * (kit.WIDTH - 4)
    assert kit._hang(head + " 5 MB", "", "") == [head + " 5", "MB"]
    assert kit._hang(head + " 5" + kit.GLUE + "MB", "", "") == [head, "5 MB"]
    assert text.isascii()


def test_checklist_says_goodnotes_strokes_have_one_width(kit_dir: Path) -> None:
    """gnnote's GoodNotes writer drops pressure; testers must not report it as an import fault."""
    text = _checklist(kit_dir)
    goodnotes = text.split("\nGoodnotes for Windows\n")[1].split("\n\n")[0]
    assert "one even width" in " ".join(goodnotes.split())
    description = text.split("What the test notebook looks like\n")[1].split("\n\n")[0]
    assert "gnnote-test.goodnotes every stroke has one even width" in " ".join(description.split())



def test_checklist_says_the_pdf_page_note_is_untested(kit_dir: Path) -> None:
    """README.md and docs/design.md call ``--paper pdf`` untested on devices: the checklist must
    say so, so that a tester neither reads a failure as a broken baseline nor expects the
    plain-paper file to look like A4."""
    text = _checklist(kit_dir)
    notability = " ".join(text.split("\nNotability on the iPad (optional)\n")[1].split("\n\n")[0].split())
    plain, pdf_pages = notability.split(" 2. Open ")
    assert "gnnote-test.note" in plain and kit.PLAIN_NOTE_PROVEN in plain
    assert kit.PLAIN_NOTE_UNTESTED in plain and "whether it opens at all" in plain
    assert "page 2 has no lines: that is expected" in plain
    assert pdf_pages.startswith("gnnote-test-pdfpages.note")
    assert kit.PDF_PAGES_UNTESTED in pdf_pages and "whether it opens at all" in pdf_pages
    assert "lines are part of the PDF page" in pdf_pages
    files = " ".join(text.split("Files in this folder\n")[1].split("\n\n")[0].split())
    assert "gnnote-test-pdfpages.note: the same for Notability with A4 PDF pages" in files
    assert kit.PDF_PAGES_UNTESTED in files
    description = " ".join(text.split("What the test notebook looks like\n")[1].split("\n\n")[0].split())
    assert ("gnnote-test.note uses Notability's own plain paper, which is a little smaller than A4: "
            "everything on it is drawn about 5 % smaller (the 1 inch line too) and page 2 has no lines"
            ) in description
    # numbers stay on the line of their unit ("5 %", "1 inch", "page 2")
    lines = text.splitlines()
    split = [(a, b) for a, b in zip(lines, lines[1:])
             for first, second in (("5", "%"), ("1", "inch"), ("page", "2"))
             if a.rstrip().endswith(" " + first) and b.split()[:1] == [second]]
    assert not split


def test_checklist_has_a_write_section_only_with_a_write_file(kit_dir: Path) -> None:
    text = _checklist(kit_dir)
    has_svgz = any(kit_dir.glob("*.svgz"))
    assert ("\nStylus Labs Write\n" in text) == has_svgz


def _section_text(text: str, title: str) -> str:
    """One checklist section without its underline, the lines joined with single spaces."""
    lines = text.split("\n" + title + "\n")[1].split("\n\n")[0].splitlines()
    assert lines[0] == "-" * len(title), lines[0]
    return " ".join(" ".join(lines[1:]).split())


def test_pdf_apps_save_a_new_file_not_the_kit_pdf(kit_dir: Path) -> None:
    """Acrobat, Xodo and the others write a plain Save into the open file: the PDF-apps section
    must ask for Save as, because Edge, Noteastic, Journal and LiquidText open the same
    gnnote-ink-annots.pdf afterwards and are judged against the untouched notebook."""
    text = _checklist(kit_dir)
    pdf_apps = _section_text(text, "Drawboard PDF, Xodo, Adobe Acrobat Reader, Foxit, PDF-XChange")
    assert pdf_apps.startswith("1. Open gnnote-ink-annots.pdf.")
    assert ("then use Save as (or Save a copy) and name the new file after the app, for example "
            "drawboard.pdf or xodo.pdf. Do not flatten.") in pdf_apps
    assert "then Save." not in pdf_apps
    later = text.split("\nDrawboard PDF, Xodo, Adobe Acrobat Reader, Foxit, PDF-XChange\n")[1]
    for section in ("Microsoft Edge", "Noteastic", "Microsoft Journal", "LiquidText (optional)"):
        assert "gnnote-ink-annots.pdf" in _section_text(later, section), section
    # the general rule comes before every app section, with what to do when an app saved anyway
    every = _section_text(text, "For every app you test")
    assert "Leave the kit's files unchanged: use Save as (or Save a copy) and a new name." in every
    assert "copy that file again from the kit before you test the next app" in every
    assert text.index("\nFor every app you test\n") < text.index("\nGoodnotes for Windows\n")
    # the menu commands are never split over two lines
    lines = text.splitlines()
    assert not [(a, b) for a, b in zip(lines, lines[1:])
                if a.rstrip().endswith(" Save") or (a.rstrip().endswith(" Save a") and b.split()[:1] == ["copy)"])]


def test_every_save_in_the_checklist_writes_a_new_file() -> None:
    """Every Save the checklist asks for, in every section (Write included), names a new file:
    Save as, Save a copy or Save it as, never a bare Save into a kit file."""
    files = kit.kit_files()
    if not any(f.format_id == "styluswrite" for f in files):
        files.append(kit.KitFile("gnnote-test.svgz", "styluswrite", purpose="for Stylus Labs Write"))
    text = kit.checklist(files)
    assert "\nStylus Labs Write\n" in text
    flat = " ".join(text.split())
    saves = list(re.finditer(r"\bsave\b", flat, re.IGNORECASE))
    assert len(saves) >= 6
    for match in saves:
        context = flat[max(0, match.start() - 50):match.end() + 40]
        assert not re.match(r"[.,;)]", flat[match.end():match.end() + 1]), context
        if match.group() == "Save":  # the menu command
            assert re.match(r" (as|a copy|it as)\b", flat[match.end():]), context
    write = _section_text(text, "Stylus Labs Write")
    assert "then Save as write-resaved.svgz" in write


# ---------------------------------------------------------------------------------------------
# optional Write output and the command line


def test_write_output_follows_the_registry(tmp_path: Path, monkeypatch) -> None:
    """A registered ``styluswrite`` writer adds gnnote-test.svgz and its checklist section;
    without one both are left out.  The test works on a copy of the registry: the real one
    keeps its entries in their order (the web page's format list and the sniff order follow
    it, and a monkeypatched key would come back at the end)."""
    registry = _formats.FORMATS
    before = list(registry.items())
    with monkeypatch.context() as patch:
        patch.setattr(_formats, "FORMATS", dict(registry))
        patch.delitem(_formats.FORMATS, "styluswrite", raising=False)
        assert "styluswrite" not in [f.format_id for f in kit.kit_files()]
        read_only = NoteFormat(id="styluswrite", name="Stylus Labs Write", extension=".svgz",
                               input_extensions=(".svgz",), sniff=lambda data, names: False,
                               reader="gnnote.xournalpp.reader:read_xopp")
        patch.setitem(_formats.FORMATS, "styluswrite", read_only)
        assert "styluswrite" not in [f.format_id for f in kit.kit_files()]
        stand_in = NoteFormat(id="styluswrite", name="Stylus Labs Write", extension=".svgz",
                              input_extensions=(".svgz",), sniff=lambda data, names: False,
                              reader="gnnote.xournalpp.reader:read_xopp",
                              writer="gnnote.xournalpp.writer:write_xopp")
        patch.setitem(_formats.FORMATS, "styluswrite", stand_in)
        assert registry.get("styluswrite") is not stand_in  # the real registry is untouched
        written: Dict[str, List[str]] = kit.make_kit(tmp_path)
        assert list(written)[-1] == "gnnote-test.svgz"
        # what the stand-in writes: the test notebook as a Xournal++ file
        stand_in_doc = _formats.get("xournalpp").read((tmp_path / "gnnote-test.svgz").read_bytes())
        assert [len(p.strokes) for p in stand_in_doc.pages] == [5, 2]
    assert _formats.FORMATS is registry
    assert list(registry.items()) == before  # same entries, same order
    text = _checklist(tmp_path)
    assert "gnnote-test.svgz: for Stylus Labs Write." in text and "\nStylus Labs Write\n" in text


def test_registry_order_survives_a_registered_write_format(tmp_path: Path, monkeypatch) -> None:
    """The case the copy guards against: with a real ``styluswrite`` entry in the middle of the
    registry, the test above must leave it exactly where it was."""
    registry = _formats.FORMATS
    original = list(registry.items())
    real = NoteFormat(id="styluswrite", name="Stylus Labs Write", extension=".svgz",
                      input_extensions=(".svgz",), sniff=lambda data, names: False,
                      reader="gnnote.xournalpp.reader:read_xopp")
    middle = list(registry.items())
    middle = [(k, v) for k, v in middle if k != "styluswrite"]
    middle.insert(len(middle) // 2, ("styluswrite", real))
    try:
        registry.clear()
        registry.update(middle)
        test_write_output_follows_the_registry(tmp_path, monkeypatch)
        monkeypatch.undo()
        assert list(registry.items()) == middle
    finally:
        registry.clear()
        registry.update(original)


def test_writer_warnings_are_returned(tmp_path: Path) -> None:
    written = kit.make_kit(tmp_path / "new" / "folder")  # created on demand
    assert list(written) == [f.name for f in kit.kit_files()]
    assert any("constant" in w for w in written["gnnote-test.goodnotes"])
    # the plain-paper Notability file is scaled to Notability's page (the writer says so); the
    # PDF-page one keeps A4 and is written without warnings
    assert any("scaled down to fit" in w for w in written["gnnote-test.note"])
    assert written["gnnote-test-pdfpages.note"] == []


def test_command_line_usage(tmp_path: Path) -> None:
    for args, code in (([], 2), (["a", "b"], 2), (["--help"], 0)):
        proc = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=60,
                              cwd=str(tmp_path))
        assert proc.returncode == code and "usage:" in proc.stderr
    blocker = tmp_path / "file"
    blocker.write_text("x")
    proc = subprocess.run([sys.executable, str(TOOL), str(blocker / "kit")], capture_output=True, text=True,
                          timeout=60, cwd=str(tmp_path))
    assert proc.returncode == 1 and "make_windows_testkit:" in proc.stderr
