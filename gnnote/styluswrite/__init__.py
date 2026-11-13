"""Stylus Labs Write codec: :func:`gnnote.styluswrite.reader.read_write_svg` and
:func:`gnnote.styluswrite.writer.write_write_svg` (``docs/styluswrite.md``).

Write (Windows, macOS, Linux, iOS, Android) keeps a document as one SVG file, gzip-compressed
as ``.svgz`` by default: ``svg#write-document`` holds one ``svg.write-page`` per page, each with
a ``g.write-content`` group whose attributes are the Page Setup values and whose content after
the ``g.ruleline`` background is the editable ink.  Pen strokes are ``<path>`` elements: a
centre line with a constant ``stroke-width`` (class ``write-stroke-pen``) or a filled outline
with an even number of vertices (class ``write-flat-pen``).

The facts below come from Write's FAQ and the ``styluslabs/templates`` README (both used for
facts only) and from documents Write saved (``docs/styluswrite.md``).  Write's own source code
(AGPL-3.0) was not consulted.
"""
from __future__ import annotations

# Write's user unit: the FAQ gives the Page Setup units as 1/150 inch, and Page Setup values are
# the numbers in the file (templates README).  The only place this factor is defined.
PT_PER_UNIT = 72.0 / 150.0

DEFAULT_PAGE_UNITS = (768.0, 1050.0)  # the page size of Write's templates (units)
PAGE_POSITION = 10.0  # Write places the first page at x = y = 10 of its document canvas
PAGE_GAP = 20.0  # vertical gap between the stacked pages gnnote writes (units)

DEFAULT_RULE_COLOR = "#FF0000FF"  # alpha-first #AARRGGBB, Write's default ruling (opaque blue)
DEFAULT_PAPER_COLOR = "#FFFFFF"
LINED_PITCH = 40.0  # Write's default ruled pitch (units); grid and dot papers use GRID_PITCH
GRID_PITCH = 25.0

# Classes Write puts on elements (observed in Write documents and its templates)
CLASS_PAGE = "write-page"
CLASS_CONTENT = "write-content"
CLASS_RULELINE = "ruleline"
CLASS_STD_RULING = "write-std-ruling"
CLASS_STROKE_PEN = "write-stroke-pen"
CLASS_FLAT_PEN = "write-flat-pen"
CLASS_BOOKMARK = "bookmark"
CLASS_HYPERREF = "hyperref"

# gnnote's own mark of a stroke's kind ("pen" or "highlighter") on the paths it writes, so that a
# translucent pen is not read back as a highlighter (Write's highlighter encoding is unknown)
KIND_ATTRIBUTE = "data-gnnote-kind"
