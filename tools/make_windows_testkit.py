#!/usr/bin/env python3
"""Write gnnote's Windows test kit: one small test notebook in every format a Windows app might
open, plus a plain-language checklist (``docs/windows.md``, "How to test on Windows").

Usage::

    python3 tools/make_windows_testkit.py OUTDIR

The notebook is built in code (no third-party content, so the kit is as free as gnnote itself):

* page 1 (A4): black, red and blue pen strokes at three widths whose pressure rises and falls,
  one yellow highlighter stroke across the text box "gnnote p1", and one straight black line
  exactly 72 pt (1 inch) long for checking units;
* page 2 (A4, lined paper): a wavy pen stroke and a closed loop.

Files written into OUTDIR:

* ``gnnote-ink-annots.pdf``: the ink as PDF ink annotations (``--pdf-ink annotations``);
* ``gnnote-ink-flat.pdf``: the ink drawn into the pages (``--pdf-ink flatten``);
* ``gnnote-test.goodnotes``, ``gnnote-test.note``, ``gnnote-test.xopp``: the ``.note`` on
  Notability's plain paper (``paper="plain"``, the default), so its pages take Notability's own
  size and page 2 loses its lines.  gnnote's ink on that paper has been seen working on an iPad,
  but only in a one-page test without text (README.md, "Verification status"): the text box and
  the second page are untested there, and the checklist says so;
* ``gnnote-test-pdfpages.note``: the same with PDF-backed A4 pages (``paper="pdf"``), which keep
  page 2's lines and the 1 inch line but have never been opened on an iPad (README.md: untested
  on devices), so the checklist asks first whether it opens at all;
* ``gnnote-test.svgz`` when a ``styluswrite`` writer is registered (skipped otherwise);
* ``CHECKLIST.txt``: what to try in each Windows app and which files to send back.

Standard library plus gnnote only; the gnnote package of this checkout is used.
"""
from __future__ import annotations

import math
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gnnote import __version__  # noqa: E402
from gnnote import formats as _formats  # noqa: E402
from gnnote.convert import Options  # noqa: E402
from gnnote.model import RGBA, Document, Page, Point, Stroke, TextBox, TextRun  # noqa: E402

__all__ = ["build_document", "make_kit", "kit_files", "checklist", "main", "KitFile", "PAGE_SIZE",
           "INCH_LINE", "PENS", "TITLE", "LABEL", "PLAIN_NOTE_PROVEN", "PLAIN_NOTE_UNTESTED",
           "PDF_PAGES_UNTESTED"]

PAGE_SIZE = (595.276, 841.89)  # A4 in pt
TITLE = "gnnote Windows test"
LABEL = "gnnote p1"
LEFT = 72.0  # left margin of the drawing, 1 inch
# the three pen strokes of page 1: (colour name, RGBA, nominal width in pt)
PENS: Tuple[Tuple[str, RGBA, float], ...] = (
    ("black", (0.0, 0.0, 0.0, 1.0), 1.0),
    ("red", (0.85, 0.1, 0.1, 1.0), 2.5),
    ("blue", (0.1, 0.25, 0.85, 1.0), 5.0),
)
PEN_LENGTH = 360.0  # pt along x
PEN_ROWS = (170.0, 260.0, 350.0)  # y of the pen strokes' centre lines
MIN_PRESSURE = 0.3  # share of the nominal width at both ends of a pen stroke
HIGHLIGHTER: Tuple[RGBA, float] = ((1.0, 0.85, 0.0, 0.5), 16.0)
INCH_LINE = ((LEFT, 450.0), (LEFT + 72.0, 450.0))  # exactly 72 pt = 1 inch = 2.54 cm
WIDTH = 92  # CHECKLIST.txt columns
GLUE = "\u00a0"  # joins words textwrap must not split ("5 MB"); a plain space in CHECKLIST.txt
# menu commands that write a new file, kept on one line ("Save" alone writes into the open file)
SAVE_AS = f"Save{GLUE}as"
SAVE_COPY = f"Save{GLUE}a{GLUE}copy"
# what CHECKLIST.txt says about gnnote-test.note (Options.paper "plain"): what an iPad has shown
# working (README.md: ink, a one-page test) and what this file adds beyond it
# (docs/notability-format.md section 13: a hand-written TextBlockMediaObject is untested)
PLAIN_NOTE_PROVEN = ("ink on Notability's plain paper, the way gnnote's ink has already been seen "
                     "working on an iPad (in a one-page test)")
PLAIN_NOTE_UNTESTED = "its text box and second page have not been tested on an iPad yet"
# what CHECKLIST.txt says about gnnote-test-pdfpages.note (Options.paper "pdf")
PDF_PAGES_UNTESTED = "no gnnote .note file with PDF pages has been opened on an iPad yet"


def _pressure_wave(x0: float, y0: float, width: float, amplitude: float = 14.0,
                   period: float = 120.0, step: float = 3.0) -> List[Point]:
    """A sine wave along x whose rendered width follows a pen pressed lightly, then hard, then
    lightly again: ``MIN_PRESSURE`` x ``width`` at both ends, ``width`` in the middle."""
    n = int(round(PEN_LENGTH / step))
    points = []
    for i in range(n + 1):
        t = i / n
        pressure = MIN_PRESSURE + (1.0 - MIN_PRESSURE) * math.sin(math.pi * t)
        x = x0 + PEN_LENGTH * t
        points.append(Point(round(x, 3), round(y0 + amplitude * math.sin(2 * math.pi * (x - x0) / period), 3),
                            round(width * pressure, 4)))
    return points


def _loop(cx: float, cy: float, r: float, width: float, n: int = 72) -> List[Point]:
    """A closed circle, its first point repeated at the end."""
    return [Point(round(cx + r * math.cos(2 * math.pi * i / n), 3), round(cy + r * math.sin(2 * math.pi * i / n), 3),
                  width) for i in range(n + 1)]


def build_document() -> Document:
    """The two-page test notebook (a fresh object on every call: writers may add warnings)."""
    width, height = PAGE_SIZE
    page1 = Page(width, height)
    page1.texts.append(TextBox(LEFT, 60.0, 220.0, 30.0, LABEL, runs=[TextRun(LABEL, size=20.0)], size=20.0))
    for (_, color, pen_width), y in zip(PENS, PEN_ROWS):
        page1.strokes.append(Stroke(_pressure_wave(LEFT, y, pen_width), color=color, width=pen_width))
    hl_color, hl_width = HIGHLIGHTER
    page1.strokes.append(Stroke([Point(LEFT - 4.0, 78.0, hl_width), Point(LEFT + 112.0, 78.0, hl_width)],
                                color=hl_color, kind="highlighter", width=hl_width))
    (x0, y0), (x1, y1) = INCH_LINE
    page1.strokes.append(Stroke([Point(x0, y0, 1.0), Point(x1, y1, 1.0)], width=1.0))

    page2 = Page(width, height, paper="lined")
    page2.strokes.append(Stroke(_pressure_wave(LEFT, 200.0, 2.0, amplitude=10.0, period=60.0), width=2.0))
    page2.strokes.append(Stroke(_loop(300.0, 400.0, 60.0, 2.0), color=(0.85, 0.1, 0.1, 1.0), width=2.0))
    return Document(title=TITLE, pages=[page1, page2], source_format="gnnote")


@dataclass(frozen=True)
class KitFile:
    """One file of the kit: its name, the format id that writes it and the writer options."""

    name: str
    format_id: str
    pdf_ink: str = "flatten"
    purpose: str = ""
    paper: str = "plain"


def kit_files() -> List[KitFile]:
    """The files this gnnote can write, in checklist order (Write only when it is registered)."""
    files = [
        KitFile("gnnote-ink-annots.pdf", "pdf", "annotations",
                "the notebook as a PDF whose handwriting is PDF ink annotations: PDF apps can select, "
                "move, recolour and delete each stroke"),
        KitFile("gnnote-ink-flat.pdf", "pdf", "flatten",
                "the same PDF with the handwriting drawn into the pages: it looks the same, but the "
                "strokes cannot be edited (for comparison)"),
    ]
    # (format id, file name suffix, paper mode, purpose)
    native = (
        ("goodnotes", "", "plain", "for Goodnotes for Windows (Import)"),
        # Notability's plain paper is the default and the paper gnnote's ink has been seen working
        # on (one page, ink only); it has its own page size (A4 is scaled down) and no lined paper
        # of gnnote's.  The text box and page 2 go beyond that test, and the purpose says so.
        ("notability", "", "plain",
         f"for Notability on the iPad: {PLAIN_NOTE_PROVEN}; {PLAIN_NOTE_UNTESTED} (round-trip check "
         "only; there is no Notability pen app for Windows)"),
        # PDF-backed pages keep the A4 size and page 2's lines, but no such .note has been opened
        # on an iPad yet (README.md: "--paper pdf ... untested on devices")
        ("notability", "-pdfpages", "pdf",
         f"the same for Notability with A4 PDF pages that keep page 2's lines; {PDF_PAGES_UNTESTED}"),
        ("xournalpp", "", "plain", "for Xournal++ and Rnote"),
        ("styluswrite", "", "plain", "for Stylus Labs Write"),
    )
    for format_id, suffix, paper, purpose in native:
        fmt = _formats.FORMATS.get(format_id)
        if fmt is None or not fmt.writable:
            continue
        files.append(KitFile("gnnote-test" + suffix + fmt.extension, format_id, purpose=purpose, paper=paper))
    return files


def make_kit(outdir: Path) -> Dict[str, List[str]]:
    """Write every kit file and ``CHECKLIST.txt`` into ``outdir`` (created when missing).

    Returns ``{file name: writer warnings}`` for the notebook files, in writing order.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, List[str]] = {}
    files = kit_files()
    for item in files:
        doc = build_document()
        options = Options(target=item.format_id, pdf_ink=item.pdf_ink, paper=item.paper)
        options.validate()
        data = _formats.get(item.format_id).write(doc, options)
        (outdir / item.name).write_bytes(data)
        written[item.name] = list(doc.warnings)
    # plain ASCII with CRLF line ends: every Windows editor shows it as it is
    text = checklist(files)
    (outdir / "CHECKLIST.txt").write_bytes(text.replace("\n", "\r\n").encode("ascii"))
    return written


def _hang(text: str, first: str, rest: str) -> List[str]:
    """``text`` wrapped at ``WIDTH`` columns: the first line starts with ``first``, the others
    with ``rest``.  Words joined with ``GLUE`` stay on one line (textwrap does not break at
    U+00A0); the glue becomes a plain space afterwards, so the result is ASCII."""
    lines = textwrap.wrap(text, WIDTH, initial_indent=first, subsequent_indent=rest,
                          break_long_words=False, break_on_hyphens=False)
    return [line.replace(GLUE, " ") for line in lines]


def _section(title: str, steps: Sequence[str], send: Optional[str] = None) -> List[str]:
    out = ["", title, "-" * len(title)]
    for number, step in enumerate(steps, 1):
        out.extend(_hang(step, f"  {number}. ", "     "))
    if send:
        out.extend(_hang("Send back: " + send, "  -> ", "     "))
    return out


def checklist(files: Sequence[KitFile]) -> str:
    """The plain-language ``CHECKLIST.txt`` for the given kit files."""
    names = {f.format_id + ":" + f.pdf_ink for f in files}
    has_write = any(f.format_id == "styluswrite" for f in files)
    notes = {f.paper: f.name for f in files if f.format_id == "notability"}
    out: List[str] = [
        f"gnnote Windows test kit (made with gnnote {__version__})",
        "=" * 60,
        "",
        "Thank you for testing! This folder holds one small test notebook in several formats.",
        "Please open them in the Windows apps you have, try the steps below and send back the",
        "files and answers marked 'Send back'. Skip any app you do not use. More background:",
        "docs/windows.md in the gnnote project.",
        "",
        "What the test notebook looks like",
        "---------------------------------",
        "  Page 1 (A4):",
        f"   - the text \"{LABEL}\" at the top left, with a yellow highlighter stroke across it;",
        "   - three wavy pen strokes: black (thin), red (medium) and blue (thick). Each one is",
        "     thin at both ends and thickest in the middle, as if the pen was pressed harder there;",
        "   - a short straight black line that is exactly 1 inch (2.54 cm) long when the page is",
        "     printed at actual size.",
        "  Page 2 (A4, lined paper): a black wavy stroke and a red circle.",
    ]
    if "goodnotes:flatten" in names:
        out.extend(_hang("Exception: in gnnote-test.goodnotes every stroke has one even width (gnnote "
                         "writes GoodNotes strokes that way), so there the wavy strokes are not thinner "
                         "at the ends.", "  ", "  "))
    if "plain" in notes:
        out.extend(_hang(f"Exception: {notes['plain']} uses Notability's own plain paper, which is a "
                         f"little smaller than A4: everything on it is drawn about 5{GLUE}% smaller (the "
                         f"1{GLUE}inch line too) and page{GLUE}2 has no lines.", "  ", "  "))
    out.extend([
        "",
        "Files in this folder",
        "--------------------",
    ])
    for f in files:
        out.extend(_hang(f"{f.name}: {f.purpose}.", "  ", "      "))
    out.extend([
        "  CHECKLIST.txt: this file.",
        "",
        "Before you start (privacy)",
        "--------------------------",
        "  - Use test content only, or a separate Windows user account. Some apps back up their",
        "    whole library into one file (Noteastic), so a backup can contain all your notes.",
        "  - Install apps only from the Microsoft Store or from the developer's own website.",
        "  - Never send files that contain private notes.",
        "",
        "For every app you test",
        "----------------------",
        "  - Note the app's name and version (usually Settings > About) and your Windows version.",
        "  - Take a screenshot of each page as the app shows it.",
        "  - Answer each question with yes or no; a short comment helps when something looks odd",
        "    (missing strokes, wrong colours, wrong thickness, things in the wrong place).",
        "  - Name the files you send back after the app, as suggested below.",
    ])
    # several apps open the same kit file one after the other (gnnote-ink-annots.pdf), and each
    # is judged against the description above: none may write its edits into it
    out.extend(_hang(f"Leave the kit's files unchanged: use {SAVE_AS} (or {SAVE_COPY}) and a new name. "
                     "Some apps write into the open file, even on their own: if one did, copy that "
                     "file again from the kit before you test the next app.", "  - ", "    "))
    if "goodnotes:flatten" in names:
        out += _section("Goodnotes for Windows", [
            "Import gnnote-test.goodnotes (Import in the library). Does it open without an error? Are "
            "both pages there, with all the strokes, the highlighter and the text? gnnote writes "
            "GoodNotes strokes with one even width each, so in this file the wavy strokes are not "
            "thinner at the ends: that is expected, not a fault of the Windows app.",
            "Select one stroke with the lasso. Can you move it? Can you change its colour?",
            "Export the notebook: Export > PDF, PDF Data Format 'Editable'. Save it as "
            "goodnotes-editable.pdf. Export it once more with 'Flattened' as goodnotes-flat.pdf.",
            f"If the import fails, write down the exact message. (Free accounts import files up to 5{GLUE}MB "
            "only; this file is far smaller.)",
        ], "goodnotes-editable.pdf, goodnotes-flat.pdf, screenshots, the answers.")
    if "pdf:annotations" in names:
        out += _section("Drawboard PDF, Xodo, Adobe Acrobat Reader, Foxit, PDF-XChange", [
            "Open gnnote-ink-annots.pdf. Do both pages look like the description above?",
            "Click or tap one of the wavy strokes with the selection tool. Can you select it? Move it? "
            "Change its colour? Delete it? Erase only part of it with the eraser?",
            "After you moved or recoloured a stroke, is it still thin at the ends and thick in the "
            "middle, or has it become one even thickness?",
            f"Draw one pen stroke and one highlighter stroke on page{GLUE}1, then use {SAVE_AS} (or "
            f"{SAVE_COPY}) and name the new file after the app, for example drawboard.pdf or xodo.pdf. "
            "Do not flatten.",
        ], "the saved PDF of each app, screenshots, the answers.")
        out += _section("Microsoft Edge", [
            "Open gnnote-ink-annots.pdf in Edge (right-click the file > Open with > Microsoft Edge). "
            "Do both pages look right?",
            "Can Edge's eraser remove one of gnnote's strokes?",
            "Draw two strokes in two colours with Draw and one with Highlight, then Save as edge.pdf.",
        ], "edge.pdf, the answers.")
        out += _section("Noteastic", [
            "Drag gnnote-ink-annots.pdf into a library folder and import all pages.",
            "Is gnnote's handwriting visible? Can the lasso select it or change its colour?",
            "Write one stroke on each page. In the library, right-click the notebook > Export > the "
            "whole notebook, and save it as noteastic-export.pdf.",
            "A Noteastic backup (Settings > Backup) contains your whole library. Make one only on a "
            "separate Windows account with test notes; docs/windows.md, 'Sample files we would like', "
            "lists what such a test library should contain.",
        ], "noteastic-export.pdf, the Settings > About screenshot, the answers.")
        out += _section("Microsoft Journal", [
            "Insert gnnote-ink-annots.pdf into a new journal (Journal copies the PDF pages in).",
            "Is gnnote's handwriting visible? Can the lasso select it as ink, or is it part of the page?",
            "Write one stroke on each page. Export as PDF: journal.pdf.",
            "If you have the desktop OneNote (not 'OneNote for Windows 10'): Export to OneNote, then in "
            "OneNote use File > Export > Section > OneNote Section (.one): journal.one.",
        ], "journal.pdf, journal.one, your .ink file from Documents\\Journals (test content only), "
           "the answers.")
        out += _section("LiquidText (optional)", [
            "Import gnnote-ink-annots.pdf. Is gnnote's handwriting visible? Can you select one stroke?",
            "Draw one stroke and export the document as PDF: liquidtext.pdf.",
        ], "liquidtext.pdf, the answers.")
    if "xournalpp:flatten" in names:
        out += _section("Xournal++ and Rnote", [
            "Xournal++: open gnnote-test.xopp. Do the strokes get thinner and thicker as described? Is "
            "page 2 lined? Save a copy as xournalpp.xopp.",
            "Rnote: import gnnote-test.xopp. Are both pages and all strokes there? Export the document "
            "as .xopp: rnote.xopp.",
        ], "xournalpp.xopp, rnote.xopp, the answers.")
    if has_write:
        out += _section("Stylus Labs Write", [
            "Open gnnote-test.svgz.",
            "Can you select a stroke, change its colour and erase part of it?",
            "Draw one stroke with each pen type, then Save as write-resaved.svgz.",
        ], "write-resaved.svgz, the answers.")
    if notes:
        steps = []
        if "plain" in notes:
            steps.append(
                f"Open {notes['plain']} in Notability (share it from the Files app). It holds "
                f"{PLAIN_NOTE_PROVEN}, but {PLAIN_NOTE_UNTESTED}, so please say whether it opens at all "
                "and write down any error message word for word. Its pages are Notability's own size, so "
                f"everything is a little smaller and page{GLUE}2 has no lines: that is expected. Do both "
                "pages, all strokes, the highlighter and the text appear? Can you select and move a stroke?")
        if "pdf" in notes:
            steps.append(
                f"Open {notes['pdf']} the same way. This is a first test: {PDF_PAGES_UNTESTED}, so please "
                "say first whether it opens at all, and write down any error message word for word. If it "
                f"opens: are the pages A4 with lines on page{GLUE}2 (the lines are part of the PDF "
                "page, not Notability paper)? Do all strokes appear, and can you select and move one?")
        out += _section("Notability on the iPad (optional)", steps,
                        "a screenshot of each page of each file, the answers.")
    out.extend([
        "",
        "Where to send everything",
        "------------------------",
        "  Attach the files and answers to an issue on the gnnote project page, or send them to",
        "  the person who gave you this kit. Thank you!",
        "",
    ])
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print("usage: python3 tools/make_windows_testkit.py OUTDIR", file=sys.stderr)
        return 0 if args and args[0] in ("-h", "--help") else 2
    outdir = Path(args[0])
    try:
        written = make_kit(outdir)
    except (OSError, ValueError) as exc:
        print(f"make_windows_testkit: {exc}", file=sys.stderr)
        return 1
    for name, warnings in written.items():
        print(outdir / name)
        for warning in warnings:
            print(f"  warning: {warning}", file=sys.stderr)
    print(outdir / "CHECKLIST.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
