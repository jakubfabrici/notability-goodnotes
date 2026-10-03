"""Saber codec: :func:`gnnote.saber.reader.read_saber` and :func:`gnnote.saber.writer.write_saber`
(``docs/saber.md``).

A Saber note is one BSON document (``.sbn2``, format version 19), or the same as JSON in
legacy ``.sbn`` files (versions up to 12); a ``.sba`` archive is a ZIP of ``main.sbn2`` plus
its assets ``main.sbn2.0``, ``main.sbn2.1`` ... (images, PDFs).

The values below are documented facts of the format.  The Saber app is GPL-3.0 and was read
for facts only; nothing was copied from it.  The pressure-to-width law is the documented
radius formula of perfect-freehand (MIT), which Saber draws its ink with.
"""
from __future__ import annotations

from typing import Dict

FORMAT_VERSION = 19  # what Saber 1.2x .. 1.36 writes ("assets in separate files, .sba")
PAGE_WIDTH = 1000.0  # Saber pages are 1000 units wide (imported PDFs are scaled to it)
DEFAULT_PAGE_SIZE = (1000.0, 1400.0)  # units
# Saber unit -> pt for pages without a PDF background.  Saber scales an imported A4 PDF page
# (which it measures as 595 x 841 pt) to 1000 units, so a 1000-unit page is read as A4 width.
PT_PER_UNIT = 595.0 / 1000.0
MAIN_MEMBER = "main.sbn2"

# StrokeOptions defaults Saber sets at start-up (keys absent from a stroke take these values)
DEFAULT_SIZE = 10.0
DEFAULT_THINNING = 0.5
DEFAULT_SIMULATE_PRESSURE = True
DEFAULT_PRESSURE_ENABLED = True
HIGHLIGHTER_ALPHA = 100  # alpha byte of Saber's highlighter colours
DEFAULT_LINE_HEIGHT = 40
DEFAULT_LINE_THICKNESS = 3

# Tool ids ("Pen" is the pre-ToolId name of the fountain pen)
FOUNTAIN_PEN = "fountainPen"
BALLPOINT_PEN = "ballpointPen"
PENCIL = "Pencil"
HIGHLIGHTER = "Highlighter"
SHAPE_PEN = "ShapePen"
PEN_NAMES: Dict[str, str] = {FOUNTAIN_PEN: "fountain", "Pen": "fountain", BALLPOINT_PEN: "ballpoint",
                             PENCIL: "pencil"}

# Background patterns ("p", one per note) -> model paper hint, and back
PAPER_FOR_PATTERN: Dict[str, str] = {
    "": "plain", "college": "lined", "college-rtl": "lined", "lined": "lined", "cornell": "lined",
    "staffs": "lined", "tablature": "lined", "grid": "grid", "dots": "dotted",
}
APPROXIMATED_PATTERNS = ("cornell", "staffs", "tablature")
PATTERN_FOR_PAPER: Dict[str, str] = {"plain": "", "lined": "lined", "grid": "grid", "dotted": "dots"}

# Flutter BoxFit indices used for page background images ("f")
BOX_FIT = ("fill", "contain", "cover", "fitWidth", "fitHeight", "none", "scaleDown")

# Saber's page text (one Quill document per page) is laid out from the top of the page: font
# size = line height "l", padding top 1.2 l, left / right 0.5 l (units).
TEXT_TOP = 1.2
TEXT_SIDE = 0.5
