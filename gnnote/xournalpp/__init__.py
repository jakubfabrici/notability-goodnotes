"""Xournal++ codec: :func:`gnnote.xournalpp.reader.read_xopp` and
:func:`gnnote.xournalpp.writer.write_xopp` (``docs/xournalpp.md``).

A ``.xopp`` (and the original Xournal ``.xoj``) is gzip-compressed XML in PDF points with a
top-left origin, so geometry maps 1:1 onto :mod:`gnnote.model`.  Xournal++ 1.2 and later also
read a ZIP-packaged variant (``mimetype``, ``META-INF/version``, ``content.xml`` and
``attachments/``), which is the only single-file form that can carry a background PDF.

The values below are documented facts of the format (the Xournal++ sources are GPL and were
consulted for facts only, no code was taken from them).
"""
from __future__ import annotations

from typing import Dict, Tuple

RGB8 = Tuple[int, int, int]

# Xournal's predefined stroke / text colour names (still accepted by Xournal++).
NAMED_COLORS: Dict[str, RGB8] = {
    "black": (0x00, 0x00, 0x00), "blue": (0x33, 0x33, 0xCC), "red": (0xFF, 0x00, 0x00),
    "green": (0x00, 0x80, 0x00), "gray": (0x80, 0x80, 0x80), "lightblue": (0x00, 0xC0, 0xFF),
    "lightgreen": (0x00, 0xFF, 0x00), "magenta": (0xFF, 0x00, 0xFF), "orange": (0xFF, 0x80, 0x00),
    "yellow": (0xFF, 0xFF, 0x00), "white": (0xFF, 0xFF, 0xFF),
}
# Background colour names of the original Xournal (checked before NAMED_COLORS on <background>).
BACKGROUND_COLORS: Dict[str, RGB8] = {
    "blue": (0xA0, 0xE8, 0xFF), "pink": (0xFF, 0xC0, 0xD4), "green": (0x80, 0xFF, 0xC0),
    "orange": (0xFF, 0xC0, 0x80), "yellow": (0xFF, 0xFF, 0x80),
}

# <background type="solid" style=...> -> model paper hint (and back).  "ruled" is horizontal
# rules only, "lined" adds a vertical margin line; the iso and stave styles have no model
# counterpart and are approximated.
PAPER_FOR_STYLE: Dict[str, str] = {
    "plain": "plain", "ruled": "lined", "lined": "lined", "staves": "lined",
    "graph": "grid", "isograph": "grid", "dotted": "dotted", "isodotted": "dotted",
}
APPROXIMATED_STYLES = ("staves", "isograph", "isodotted")
STYLE_FOR_PAPER: Dict[str, str] = {"plain": "plain", "lined": "ruled", "ruled": "ruled",
                                   "grid": "graph", "dotted": "dotted"}

FILE_VERSION = 4  # what every released Xournal++ (up to 1.3.8) writes and reads
# A shape fill without a matching outline stroke is written as a filled stroke with this
# hairline outline width (pt); the reader reads such a stroke back as the fill alone.
FILL_OUTLINE_WIDTH = 0.1
DEFAULT_PAGE_SIZE = (595.27559, 841.88976)  # A4, Xournal++'s default page
HIGHLIGHTER_ALPHA = 0x7F  # Xournal++ writes highlighter colours with this alpha byte

# ZIP-packaged variant: member names and contents exactly as in Xournal++'s own packaged test
# files (test/files/packaged_xopp).  The released loaders (1.2.0 .. 1.3.8) reject a mimetype
# member that is *exactly* "application/xournal++", so the test files' spelling with a leading
# space and a trailing newline is used verbatim.
PACKAGE_MIMETYPE = b" application/xournal++\n"
PACKAGE_VERSION = b"current=4\nmin=4\n"
PACKAGE_CONTENT = "content.xml"
PACKAGE_PDF = "attachments/bg.pdf"
