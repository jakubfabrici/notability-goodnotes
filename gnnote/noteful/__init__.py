"""Noteful (``.noteful``) codec: constants shared by :mod:`.reader` and :mod:`.writer`.

A ``.noteful`` file is one big-endian binary container (``docs/noteful.md``)::

    AA BB CC DE                         magic
    blob, blob, ...                     raw blobs back to back
    root record                         TTV (:mod:`gnnote.noteful.ttv`): blob names, offsets, lengths
    AA BB CC DE 00 00 00 00 u32 u32     trailer: root offset and length

Lengths are Noteful units of 1/132 inch (``pt * 132 / 72``, the scale GoodNotes uses too);
the origin is the page's top-left corner, y grows downwards.
"""
from __future__ import annotations

MAGIC = b"\xaa\xbb\xcc\xde"
TRAILER_SIZE = 16

UNITS_PER_POINT = 132.0 / 72.0
POINTS_PER_UNIT = 72.0 / 132.0

APPLE_EPOCH = 978307200  # Unix time of 2001-01-01T00:00:00Z, the epoch of Noteful's timestamps
FORMAT_VERSION = 1.18  # root 0x0001 (float32) in files of Noteful 1.4.25 .. 1.4.33
OBJECT_VERSION = 280  # 0x0001 of an annotation record / 0x0016 of an object in those files
KNOWN_OBJECT_VERSIONS = (280, 288)  # 288: Noteful 1.4.33 and later (image-insert sample)

A4_UNITS = (1091.3385826771655, 1543.464566929134)  # 595.28 x 841.89 pt
TEXT_INSET = (5.0, 2.0)  # units between a text box's edge and its text (x, y)

# ink blob record tags
INK_STROKE = 0xF101
INK_STYLE = 0xF102
BLEND_NORMAL = 0  # pen
BLEND_MULTIPLY = 1  # highlighter; the app draws it at 50 % opacity
HIGHLIGHTER_OPACITY = 0.5

# object types (annotation 0x0005 -> object 0x0006 -> 0x0001)
OBJ_IMAGE = 1
OBJ_TEXT = 2
OBJ_RECTANGLE = 3
OBJ_ELLIPSE = 6
OBJ_POLYGON = 12
OBJ_LINE = 20
OBJ_BEZIER = 21
SHAPE_TYPES = (OBJ_RECTANGLE, OBJ_ELLIPSE, OBJ_POLYGON, OBJ_LINE, OBJ_BEZIER)

# point-list commands (CoreGraphics path element types)
CMD_MOVE = 0
CMD_LINE = 1
CMD_QUAD = 2
CMD_CUBIC = 3
CMD_CLOSE = 4

FLIP_HORIZONTAL = 1
FLIP_VERTICAL = 2

BACKGROUND_PDF = 1
BACKGROUND_TEMPLATE = 2

# arrow heads: a triangle 4.5 line widths long and as wide, its tip on the path's end point
ARROW_LENGTH = 4.5
ARROW_HALF_WIDTH = 2.25


def looks_like_noteful(data: bytes) -> bool:
    """True when ``data`` carries the magic at its start and in its 16-byte trailer."""
    return len(data) >= len(MAGIC) + TRAILER_SIZE and data[:4] == MAGIC and data[-16:-12] == MAGIC
