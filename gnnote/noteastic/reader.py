"""Noteastic ``.ntcbak`` library backups -> :class:`gnnote.model.Document` (experimental, tolerant).

Noteastic (Windows, Microsoft Store 9PP6QXXZS9D1) keeps every notebook in one SQLite database
and exports nothing per notebook; its only native file is the full-library backup
(*Settings > Backup*).  The layout below comes from the one public description of the format,
the MIT-licensed reader ``Canxinner/noteastic-mcp-public`` (commit 0182e788; ``src/backup.js``,
``src/export.js``, ``test/backup.test.js``), and has **not** been checked against a backup made
by the app, so every document read here carries :data:`EXPERIMENTAL_WARNING`.  Details and open
questions are in ``docs/noteastic.md``; no code was copied::

    <name>.ntcbak                          a ZIP archive
      noteastic.db                         SQLite: the whole library (tables below)
      PDFs\\<name>.pdf, images ...           imported PDFs and inserted images, named by FilePath
                                           columns (Windows separators allowed)

    Folder(ID, Name), FolderFolderContent(FolderID, ChildFolderID)     the folder tree
    Book(ID, Name), FolderBookContent(FolderID, ChildBookID)           notebooks and their folders
    BookContent(BookID, PageIndex, PageID)                             page order (0-based index)
    Page(ID, GridID, Width, Height, BackgroundColor)
    Grid(ID, GridType), PDFGrid(GridID, FilePath, PageIndex, Password) paper, PDF page backgrounds
    PageContent(PageID, ElementID)
    Element(ID, ElementType, CreationDate, M11, M12, M21, M22, M31, M32, IsDeleted)
    StrokeElement(ElementID, StrokeWidth, StrokeHeight, Color, IsHighlighter, IgnorePressure, PointsData)
    LineElement(ElementID, LineType, X0, Y0, X1, Y1, StrokeColor, StrokeWidth, StrokeDashed, IsHighlighter)
    ShapeElement(ElementID, ShapeType, X0, Y0, X1, Y1, StrokeColor, StrokeWidth, StrokeDashed, FillColor)
    ImageElement(ElementID, ImageWidth, ImageHeight, FilePath)
    TextElement(ElementID, Text, Width, FontFamily, FontSize, Color, HorizontalTextAlignment, Spans)

Mapping:

* One :class:`Document` holds every notebook, in library order: folders depth-first by name
  (a folder's notebooks before its sub-folders), notebooks by name, then notebooks outside any
  folder; pages by ``BookContent.PageIndex``.  The title is the notebook's name when the library
  has one notebook, else "Noteastic library"; with several, a warning names each notebook's
  pages.  A bare ``noteastic.db`` is read too (without its PDFs and images).
* Units: page sizes and coordinates are taken to be Windows effective pixels (1/96 inch), so
  one unit is :data:`UNIT_PT` = 0.75 pt (unverified; the MIT reader's PDF export takes one
  unit as one pt).  A page whose ``PDFGrid`` names a PDF shows that PDF page's visible area
  (its CropBox) fitted into the Noteastic page (scaled to fit, centred, as pdf.js in the MIT
  reader and Windows' PDF renderer draw it); the converted page takes the PDF page's own size
  (its MediaBox) and the ink is mapped onto the visible area, which needs no unit assumption.
  Other grid types become the paper hint.
* Only elements with ``IsDeleted = 0`` (or no flag) are read, ordered by ``CreationDate`` then
  ``ID``; every point goes through the element's matrix (``x' = M11 x + M21 y + M31``,
  ``y' = M12 x + M22 y + M32``).  Line widths scale with the matrix (unverified).
* ``StrokeElement``: ``PointsData`` is ``"<n>;x,y,p;x,y,p;..."`` (the header is skipped unless
  it looks like the points, so a version number works too; points may lack the pressure;
  values after the pressure are ignored and damaged points left out, as the MIT reader does;
  numbers written with a decimal comma are decoded by guessing, with a warning), ``#AARRGGBB``
  colour, the larger of
  ``StrokeWidth`` / ``StrokeHeight`` as the pen width, highlighter from ``IsHighlighter`` or
  the element type.  Pressure ``p`` (0..1) scales the width by ``1.5 p + 0.25`` (the rule of
  WPF's ink renderer, which gnnote's OneNote reader uses too) unless ``IgnorePressure``.
* ``LineElement``: a straight stroke, plus a filled triangle per arrowhead (``SingleArrow``,
  ``DoubleArrow``).  ``ShapeElement``: a closed stroke (ellipse, triangle, pentagon, hexagon,
  octagon, rectangle; the quadrant shapes add their centre lines), preceded by a
  ``kind="fill"`` stroke when ``FillColor`` is not transparent.  ``ImageElement``: an image of
  ``ImageWidth`` x ``ImageHeight`` placed by the matrix.  ``TextElement``: a text box (one
  style; ``Spans`` are ignored with a warning).  Skew and mirroring are dropped: a mirrored
  picture or text box stays upright over the area Noteastic shows it in.  Other element types
  are skipped with a warning.

Hostile input: the database is opened read-only from memory by :mod:`gnnote.sqliteutil`
(size, work, data, row and value limits; real tables only, each checked before it is read);
ZIP members go through :class:`gnnote.readutil.ZipBundle`; a document holds at most
:data:`MAX_PAGES` pages, ``readutil.MAX_POINTS`` decoded ink points (checked before a stroke
is decoded, and charged whether or not its points could be read; :data:`MAX_COMMA_POINTS` of
them decoded by the slow decimal-comma guess, whatever the guess's outcome) and
:data:`MAX_IMAGE_BYTES` picture bytes (at most the backup's size plus
:data:`SHARED_IMAGE_BYTES`, counted per placement); each PDF member is parsed once.  Only "not
a Noteastic backup" (and a database that cannot be opened at all, or whose ``Page`` or
``BookContent`` table cannot be read) raises :class:`ValueError`; everything else is a warning.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from ..model import RGBA, Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from ..readutil import (MAX_MEMBER_BYTES, PageNotes, PointBudget, ZipBundle, ellipse_points, image_format,
                        image_pixel_size, straight_controls)
from ..sqliteutil import TOO_LARGE, SqliteDatabase, SqliteError, is_sqlite

__all__ = ["read_ntcbak", "parse_points_data", "ParsedPoints", "find_database", "EXPERIMENTAL_WARNING", "UNIT_PT",
           "DB_NAME"]

EXPERIMENTAL_WARNING = (
    "Noteastic support is experimental: it follows a public description of the backup format and has not "
    "yet been checked against a backup made by the app. Compare the result with your notebook.")
DB_NAME = "noteastic.db"
UNIT_PT = 0.75  # pt per Noteastic unit, assuming Windows effective pixels (1/96 in): unverified
DEFAULT_PAGE_UNITS = (793.7, 1122.5)  # A4 at 96 units per inch, for a page without a usable size
DEFAULT_PEN_UNITS = 2.0  # Windows Ink's default pen tip (InkDrawingAttributes.Size 2 x 2)
DEFAULT_LINE_UNITS = 1.0  # line / shape width when the row has none (the MIT reader's default)
MIN_LINE_UNITS = 0.5  # the MIT reader never draws thinner lines
DEFAULT_FONT_UNITS = 16.0  # font size when the row has none (the MIT reader's default)
PRESSURE_LAW = (1.5, 0.25)  # width factor a * p + b for pressure p in 0..1
ARROW_HALF_ANGLE = 0.45  # rad, half the opening of an arrowhead (the MIT reader's drawing)
ARROW_MIN_UNITS = 7.0  # arrowhead length: max(7, 5 x line width) units (the MIT reader's drawing)
ARROW_WIDTH_FACTOR = 5.0
ELLIPSE_SAMPLES = 64
MIN_WIDTH_PT = 0.1
MAX_WIDTH_PT = 1000.0
LINE_HEIGHT = 1.25  # line height / font size used to estimate a text box's height
CHAR_WIDTH = 0.6  # average character width / font size used when a text box has no width

MAX_PAGES = 10_000  # pages one document may hold
MAX_FOLDERS = 100_000  # rows of Folder and Book
MAX_LINKS = 200_000  # rows of FolderFolderContent, FolderBookContent and BookContent
MAX_PAGE_ROWS = 200_000  # rows of Page, Grid and PDFGrid
MAX_ELEMENTS = 1_000_000  # rows of PageContent, Element and each element table
MAX_FOLDER_DEPTH = 64
MAX_POINTS_CHARS = 64 * 1024 * 1024  # one PointsData value
MAX_POINTS_PER_STROKE = 1_000_000
MAX_COMMA_POINTS = 1_000_000  # points one document may decode by guessing decimal commas (slow: ~10 us each)
MAX_POINT_CHARS = 256  # one "x,y,p" part of PointsData (full-precision doubles need about 75)
MAX_TEXT_CHARS = 1_000_000
MAX_NAME_CHARS = 1_000
MAX_PATH_CHARS = 4_096
MAX_PATH_PARTS = 64
MAX_COORD_UNITS = 1.0e6  # coordinates (before and after the matrix) beyond this are damage
MAX_COORD_PT = 1.0e7  # converted coordinates beyond this are damage (gnnote's writers skip them too)
MAX_MATRIX_SCALE = 1.0e4  # |M11|, |M12|, |M21|, |M22| beyond this are damage
MAX_PAGE_UNITS = 100_000.0
MAX_IMAGE_BYTES = 512 * 1024 * 1024  # picture bytes one document may hand to the writers ...
SHARED_IMAGE_BYTES = 64 * 1024 * 1024  # ... and no more than the backup's size plus this
MAX_LISTED = 20  # notebooks named in the multi-notebook warning
MAX_NAMES = 5  # unknown type names quoted in a warning

BLACK: RGBA = (0.0, 0.0, 0.0, 1.0)
XY = Tuple[float, float]
XYP = Tuple[float, float, Optional[float]]

_INT_RE = re.compile(r"-?\d+\Z")
_FRAC_RE = re.compile(r"\d+(?:[eE][-+]?\d+)?\Z")
_HEX_RE = re.compile(r"#?([0-9a-fA-F]{8}|[0-9a-fA-F]{6})\Z")

# ElementType -> the table holding the element's data
_ELEMENT_TABLES = {
    "inkstroke": "stroke", "stroke": "stroke", "highlighter": "stroke",
    "line": "line", "linehighlighter": "line",
    "shape": "shape",
    "image": "image",
    "text": "text", "textbox": "text",
}
_HIGHLIGHTER_TYPES = ("highlighter", "linehighlighter")
_ARROW_TYPES = {"singlearrow": 1, "doublearrow": 2, "arrow": 1}
_PLAIN_LINE_TYPES = ("", "line", "straight", "straightline", "none", "plain", "simple")
_POLYGONS = {"triangle": 3, "pentagon": 5, "hexagon": 6, "octagon": 8}
_RECTANGLES = ("rectangle", "rect", "square", "")
_ELLIPSES = ("ellipse", "circle", "oval")
_QUADRANTS = ("onequadrant", "fourquadrant")
# Windows.UI.Xaml.TextAlignment, in case the alignment is stored as a number (inferred)
_ALIGN_NUMBERS = {0: "center", 1: "left", 2: "right", 3: "left", 4: "left"}
_ALIGN_NAMES = {"left": "left", "start": "left", "center": "center", "centre": "center", "right": "right",
                "end": "right", "justify": "left", "justified": "left", "detectfromcontent": "left"}


# --------------------------------------------------------------------------- value helpers


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    """A finite float from an SQLite value (numbers, or numeric text); ``default`` otherwise."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except OverflowError:
            return default
        return f if math.isfinite(f) else default
    if isinstance(value, str) and len(value) <= 64:
        try:
            f = float(value.strip())
        except ValueError:
            return default
        return f if math.isfinite(f) else default
    return default


def _flag(value: Any) -> bool:
    """An SQLite boolean: non-zero numbers and "true" / "1" / "yes" are true."""
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "t")
    return False


def _text(value: Any, cap: int = MAX_NAME_CHARS) -> Optional[str]:
    if isinstance(value, bytes) and len(value) <= cap * 4:
        value = value.decode("utf-8", "replace")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    return value[:cap]


def _ident(value: Any) -> Any:
    """A row ID as a dictionary key: numbers and numeric text as ``int``, other text lower-case."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value == int(value) else value
    if isinstance(value, str):
        s = value.strip()
        if not s or len(s) > 256:
            return None
        if _INT_RE.match(s) and len(s) <= 19:
            return int(s)
        return s.lower()
    if isinstance(value, bytes) and 0 < len(value) <= 64:
        return value
    return None


def _order_key(value: Any) -> Tuple[int, Any]:
    """SQLite's order of mixed values: NULL, then numbers, then text, then blobs."""
    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (1, value)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, str):
        return (2, value)
    if isinstance(value, bytes):
        return (3, value)
    return (4, 0)


def _colour(value: Any, default: Optional[RGBA]) -> Tuple[Optional[RGBA], bool]:
    """``#AARRGGBB`` / ``#RRGGBB`` text (or an ARGB integer) -> (RGBA, readable)."""
    if value is None:
        return default, True
    if isinstance(value, int) and not isinstance(value, bool):
        v = value & 0xFFFFFFFF
        return ((v >> 16 & 255) / 255.0, (v >> 8 & 255) / 255.0, (v & 255) / 255.0, (v >> 24 & 255) / 255.0), True
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default, True
        m = _HEX_RE.match(s)
        if m:
            h = m.group(1)
            a = int(h[:2], 16) / 255.0 if len(h) == 8 else 1.0
            rgb = h[2:] if len(h) == 8 else h
            return (int(rgb[0:2], 16) / 255.0, int(rgb[2:4], 16) / 255.0, int(rgb[4:6], 16) / 255.0, a), True
        if s.lower() == "transparent":
            return (0.0, 0.0, 0.0, 0.0), True
    return default, False


def _has_spans(value: Any) -> bool:
    """True when a ``Spans`` value holds formatting runs (anything but empty, ``[]`` or ``null``)."""
    if value is TOO_LARGE:
        return True
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return isinstance(value, str) and value.strip() not in ("", "[]", "{}", "null")


def _clamp_width(width: float) -> float:
    return min(max(width, MIN_WIDTH_PT), MAX_WIDTH_PT)


# --------------------------------------------------------------------------- PointsData


_COMMA_INT_RE = re.compile(r"-?\d+(?:[eE][-+]?\d+)?\Z")


def _comma_value(group: Sequence[str]) -> Optional[float]:
    """One number written with a decimal comma, given as its one or two comma-separated tokens."""
    if len(group) == 1:
        return float(group[0]) if _COMMA_INT_RE.match(group[0]) else None
    if not _INT_RE.match(group[0]) or not _FRAC_RE.match(group[1]):
        return None
    return float(group[0] + "." + group[1])


# the ways to split k tokens into n numbers of one or two tokens each, for k <= 6
_GROUPINGS: Dict[Tuple[int, int], List[Tuple[int, ...]]] = {}


def _groupings(k: int, n: int) -> List[Tuple[int, ...]]:
    if (k, n) not in _GROUPINGS:
        if n == 0:
            found = [()] if k == 0 else []
        else:
            found = [(first,) + rest for first in (1, 2) if first <= k for rest in _groupings(k - first, n - 1)]
        _GROUPINGS[(k, n)] = found
    return _GROUPINGS[(k, n)]


def _decimal_comma_candidates(tokens: List[str]) -> List[XYP]:
    """Readings of one ``x,y,p`` triple whose numbers use a decimal comma (``12,5,30,25,0,5``)."""
    out: List[XYP] = []
    for n_values in (3, 2):
        for sizes in _groupings(len(tokens), n_values):
            pos = 0
            values: List[float] = []
            for size in sizes:
                v = _comma_value(tokens[pos:pos + size])
                if v is None or not math.isfinite(v):
                    break
                values.append(v)
                pos += size
            else:
                if n_values == 2:
                    out.append((values[0], values[1], None))
                elif 0.0 <= values[2] <= 1.0:
                    out.append((values[0], values[1], values[2]))
        if out:
            return out
    return out


class ParsedPoints(NamedTuple):
    """The result of :func:`parse_points_data`."""

    points: Optional[List[XYP]]  # None when the stroke is unreadable or has too many points
    how: str  # "ok", "comma", "too-many", "too-many-comma" or "unreadable"
    dropped: int = 0  # points that could not be read and were left out
    extra: bool = False  # points carried more than x, y and pressure (the rest was ignored)
    examined: int = 0  # point parts that were decoded (the work done, whatever the outcome)
    comma: bool = False  # the numbers looked like decimal-comma numbers (the slow path)


def _is_header(first: str, rest: Sequence[str]) -> bool:
    """Whether the first ``;``-part of ``PointsData`` is a header rather than a point.

    The reference reader always skips it (a point count in its fixture).  gnnote keeps it only
    when it looks exactly like the points after it: a part without a comma (a count or a
    version) or with a different number of comma-separated fields (``1,0`` before ``x,y,p``
    points) is a header.
    """
    if "," not in first:
        return True
    sample = [part.count(",") for part in rest[:64] if part and len(part) <= MAX_POINT_CHARS]
    if not sample:
        return False  # a lone part with commas: a point without a header
    typical = Counter(sample).most_common(1)[0][0]
    return first.count(",") != typical


def parse_points_data(text: str, max_points: int = MAX_POINTS_PER_STROKE,
                      max_comma_points: Optional[int] = None) -> ParsedPoints:
    """Decode a ``PointsData`` value into ``(x, y, pressure)`` triples.

    Returns :class:`ParsedPoints`: ``how`` is ``"ok"``, ``"comma"`` (numbers written with a
    decimal comma, decoded by guessing where each number ends), ``"too-many"`` (more than
    ``max_points``), ``"too-many-comma"`` (more than ``max_comma_points`` points written with a
    decimal comma) or ``"unreadable"``; ``points`` is ``None`` in the last three.  The
    first ``;``-separated part is a header unless it looks like the points after it
    (:func:`_is_header`).  A point may lack its pressure (``None``); fields after the pressure
    are ignored (``extra``), as the reference reader does.  Single points that cannot be read
    (or are not finite) are left out and counted in ``dropped``; the stroke is unreadable when
    no point, or fewer than half of them, can be read.  ``examined`` counts the parts that were
    decoded and ``comma`` says the decimal-comma guess was used (or would have been, for
    ``"too-many-comma"``), so the caller can charge the work whatever the outcome.
    """
    if text.count(";") > max_points + 1:  # counted before splitting: a crafted value stays cheap
        return ParsedPoints(None, "too-many")
    parts = [s.strip() for s in text.split(";")]
    if parts and _is_header(parts[0], parts[1:]):
        parts = parts[1:]
    body = [s for s in parts if s]
    if len(body) > max_points:
        return ParsedPoints(None, "too-many")
    if not body:
        return ParsedPoints([], "ok")
    # A decimal comma: no '.' anywhere, and points of more than three comma-separated tokens.
    comma = not any("." in s for s in body) and any(s.count(",") > 2 for s in body if len(s) <= MAX_POINT_CHARS)
    if comma and max_comma_points is not None and len(body) > max_comma_points:
        return ParsedPoints(None, "too-many-comma", comma=True)  # decided before the costly splitting
    # Parts longer than any real point are damage: dropped before they are split at commas.
    split = [s.split(",") if len(s) <= MAX_POINT_CHARS else [] for s in body]
    points: List[XYP] = []
    extra = False
    if not comma:
        for fields in split:
            if len(fields) < 2:
                continue
            try:
                x, y = float(fields[0]), float(fields[1])
            except ValueError:
                continue
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            p: Optional[float] = None
            if len(fields) > 2:
                extra = extra or len(fields) > 3
                try:
                    p = float(fields[2])
                except ValueError:
                    p = None
                if p is not None and not math.isfinite(p):
                    p = None
            points.append((x, y, p))
        how = "ok"
    else:
        readings = [_decimal_comma_candidates([t.strip() for t in fields]) if 2 <= len(fields) <= 6 else []
                    for fields in split]
        chosen: List[Optional[XYP]] = [r[0] if len(r) == 1 else None for r in readings]
        for i, options in enumerate(readings):  # ambiguous points: the reading nearest a neighbour
            if chosen[i] is not None or not options:
                continue
            neighbour = next((chosen[j] for j in (i - 1, i + 1) if 0 <= j < len(chosen) and chosen[j] is not None),
                             None)
            if neighbour is None:
                chosen[i] = options[0]
            else:
                chosen[i] = min(options, key=lambda o: (o[0] - neighbour[0]) ** 2 + (o[1] - neighbour[1]) ** 2)
        points = [c for c in chosen if c is not None]
        how = "comma"
    dropped = len(body) - len(points)
    if not points or 2 * dropped > len(body):
        return ParsedPoints(None, "unreadable", examined=len(body), comma=comma)
    return ParsedPoints(points, how, dropped, extra, len(body), comma)


# --------------------------------------------------------------------------- geometry


class _Matrix:
    """An element's affine matrix in row-vector form (``System.Numerics.Matrix3x2``)."""

    __slots__ = ("m11", "m12", "m21", "m22", "m31", "m32")

    def __init__(self, m11: float, m12: float, m21: float, m22: float, m31: float, m32: float):
        self.m11, self.m12, self.m21, self.m22, self.m31, self.m32 = m11, m12, m21, m22, m31, m32

    def apply(self, x: float, y: float) -> XY:
        return self.m11 * x + self.m21 * y + self.m31, self.m12 * x + self.m22 * y + self.m32

    @property
    def scale_x(self) -> float:
        return math.hypot(self.m11, self.m12)

    @property
    def scale_y(self) -> float:
        return math.hypot(self.m21, self.m22)

    @property
    def det(self) -> float:
        return self.m11 * self.m22 - self.m12 * self.m21

    @property
    def line_scale(self) -> float:
        """How the matrix scales a line width: the square root of the area scale."""
        det = abs(self.det)
        return math.sqrt(det) if det > 0 else max(self.scale_x, self.scale_y)

    @property
    def rotation(self) -> float:
        """Clockwise degrees (y grows downwards) of the x axis.  A mirrored matrix is a turn
        plus a flip of either axis; gnnote drops the flip and keeps the turn nearer upright
        (that of the x axis or of the y axis), so a flipped box is not turned upside down."""
        x_axis = math.degrees(math.atan2(self.m12, self.m11)) if (self.m11 or self.m12) else 0.0
        if self.det >= 0:
            return x_axis
        y_axis = math.degrees(math.atan2(-self.m21, self.m22))
        return min((x_axis, y_axis), key=lambda d: abs((d + 180.0) % 360.0 - 180.0))

    @property
    def skewed(self) -> bool:
        """True when the axes are not perpendicular or one of them is mirrored."""
        sx, sy = self.scale_x, self.scale_y
        if sx == 0 or sy == 0:
            return True
        dot = self.m11 * self.m21 + self.m12 * self.m22
        return abs(dot) > 1e-6 * sx * sy or self.det < 0


class _Frame:
    """Noteastic page units -> model points: ``((x - ox) * k, (y - oy) * k)``."""

    __slots__ = ("k", "ox", "oy")

    def __init__(self, k: float, ox: float = 0.0, oy: float = 0.0):
        self.k, self.ox, self.oy = k, ox, oy

    def pt(self, x: float, y: float) -> XY:
        return (x - self.ox) * self.k, (y - self.oy) * self.k


# --------------------------------------------------------------------------- the archive


def find_database(names: Sequence[str]) -> Optional[str]:
    """The member holding ``noteastic.db`` (any case, at the top or one folder down)."""
    found: Optional[str] = None
    for name in names:
        parts = name.replace("\\", "/").split("/")
        if len(parts) > 2 or parts[-1].lower() != DB_NAME or parts[0] == "__MACOSX":
            continue
        if len(parts) == 1:
            return name
        if found is None:
            found = name
    return found


class _Assets:
    """The backup's PDFs and images, found by a ``FilePath`` column's value."""

    def __init__(self, bundle: ZipBundle, prefix: str):
        self.bundle = bundle
        self.prefix = prefix.lower()
        self.index: Dict[str, str] = {}
        for name in bundle.names:
            if not name.endswith("/"):
                self.index.setdefault(name.replace("\\", "/").lower(), name)
        self.cache: Dict[str, Optional[bytes]] = {}

    def resolve(self, path: str) -> Optional[str]:
        """The member a stored path names: Windows separators allowed, relative to the database's
        folder or to the archive root; an absolute path matches by its longest suffix that is a
        member."""
        parts = [p for p in path.replace("\\", "/").strip().split("/") if p not in ("", ".")]
        if not parts or len(parts) > MAX_PATH_PARTS:
            return None
        for i in range(len(parts)):
            tail = "/".join(parts[i:]).lower()
            for candidate in ((self.prefix + tail) if self.prefix else "", tail):
                if candidate and candidate in self.index:
                    return self.index[candidate]
        return None

    def read(self, path: str) -> Tuple[Optional[str], Optional[bytes]]:
        member = self.resolve(path)
        if member is None:
            return None, None
        if member not in self.cache:
            self.cache[member] = self.bundle.read(member)
        return member, self.cache[member]


def _pdf_boxes(data: bytes) -> List[Tuple[float, float, Tuple[float, float, float, float]]]:
    """Every page of a PDF: its displayed size (the MediaBox turned by /Rotate, as gnnote's
    model pages use it) and its visible area, the CropBox clipped to the MediaBox, as
    ``(left, top, right, bottom)`` in pt from the displayed top-left corner.  [] when the PDF
    cannot be read."""
    try:
        from ..pdf.objects import PdfFile  # imported lazily: the PDF parser is large

        pages = PdfFile(data).pages()
    except Exception:  # noqa: BLE001 - an unreadable PDF must not stop the reader
        return []
    out: List[Tuple[float, float, Tuple[float, float, float, float]]] = []
    for page in pages:
        x0, y0, x1, y1 = page.box
        if not (x1 - x0 >= 1.0 and y1 - y0 >= 1.0):
            continue
        crop = page.crop or page.box
        cx0, cy0, cx1, cy1 = max(crop[0], x0), max(crop[1], y0), min(crop[2], x1), min(crop[3], y1)
        if not (cx1 - cx0 >= 1.0 and cy1 - cy0 >= 1.0):
            cx0, cy0, cx1, cy1 = x0, y0, x1, y1  # an empty or absurd CropBox: the whole page shows
        corners = [_shown(cx0, cy0, page.box, page.rotate), _shown(cx1, cy1, page.box, page.rotate)]
        left, right = sorted(c[0] for c in corners)
        top, bottom = sorted(c[1] for c in corners)
        out.append((page.width, page.height, (left, top, right, bottom)))
    return out


def _shown(x: float, y: float, box: Tuple[float, float, float, float], rotate: int) -> XY:
    """A point of PDF user space on the displayed page of MediaBox ``box`` turned clockwise by
    ``rotate`` degrees (top-left origin, y down; see docs/pdf.md section 1)."""
    x0, y0, x1, y1 = box
    if rotate == 90:
        return y - y0, x - x0
    if rotate == 180:
        return x1 - x, y - y0
    if rotate == 270:
        return y1 - y, x1 - x
    return x - x0, y1 - y


# --------------------------------------------------------------------------- the reader


class _Reader:
    def __init__(self, db: SqliteDatabase, assets: Optional[_Assets], size: int = 0):
        self.db = db
        self.assets = assets
        # Picture bytes handed to the writers: every placement of a picture is written out
        # again, so a backup naming one picture many times must not multiply it without bound.
        self.image_bytes_left = min(MAX_IMAGE_BYTES, size + SHARED_IMAGE_BYTES)
        self.doc = Document(title="Noteastic library", source_format="noteastic")
        self.doc.warn(EXPERIMENTAL_WARNING)
        self.counts: Counter = Counter()
        self.type_names: Dict[str, Counter] = {"element": Counter(), "shape": Counter(), "line": Counter(),
                                               "grid": Counter()}
        self.notes = PageNotes()
        self.budget = PointBudget()
        self.comma_points_left = MAX_COMMA_POINTS
        self.missing_tables: List[str] = []
        self.missing_columns: List[str] = []
        self.failed_tables: set = set()  # tables that are absent, unreadable or lack a required column
        self.has_books = False  # a usable Book table names the notebooks
        self.deleted_books: set = set()
        self.pdfs: Dict[str, Any] = {}  # normalised FilePath -> load_pdf()'s result
        self.pdf_members: Dict[str, Any] = {}  # archive member -> load_pdf()'s result
        self.no_archive = False

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    # -- database access ---------------------------------------------------------------

    def rows(self, table: str, columns: Sequence[str], required: Sequence[str], max_rows: int,
             caps: Optional[Dict[str, int]] = None, accept: Optional[Callable[[Tuple[Any, ...]], bool]] = None,
             essential: bool = False, extras: Sequence[str] = ()) -> List[Tuple[Any, ...]]:
        """Rows of ``columns`` + ``extras`` of a table, problems reported as warnings (an
        ``essential`` table raises instead).  ``columns`` are the documented ones: a missing one
        is reported; ``extras`` are columns a real database may have (read as ``None`` when absent)."""
        try:
            sel = self.db.select(table, tuple(columns) + tuple(extras), required, max_rows=max_rows, caps=caps,
                                 accept=accept)
        except SqliteError as exc:
            if essential:
                raise
            self.failed_tables.add(table)
            self.warn(f"The {table} table of noteastic.db could not be read ({exc}); its content is missing")
            return []
        if not sel.present:
            if essential:
                lacking = ", ".join(c for c in required if c in sel.missing)
                raise ValueError(f"not a readable Noteastic database: its {table} table lacks the column(s) {lacking}")
            self.failed_tables.add(table)
            if self.db.has_table(table):
                self.missing_columns += [f"{table}.{c}" for c in required if c in sel.missing]
            else:
                self.missing_tables.append(table)
            return []
        self.missing_columns += [f"{table}.{c}" for c in sel.missing if c not in extras]
        if sel.truncated:
            self.warn(f"The {table} table holds more than {max_rows} rows; only the first {max_rows} were read")
        for column, n in sel.oversized.items():
            self.warn(f"{n} value(s) of {table}.{column} were too large to read and were ignored")
        return sel.rows

    # -- library -------------------------------------------------------------------------

    def read(self) -> Document:
        db = self.db
        if not (db.has_table("Page") and db.has_table("BookContent")):
            held = sorted(db.tables.values(), key=str.lower)
            shown = ", ".join(held[:12]) + (", ..." if len(held) > 12 else "")
            raise ValueError("not a Noteastic database: noteastic.db has no Page and BookContent tables"
                             + (f" (it holds {shown})" if held else " (it holds no tables)"))
        notebooks = self.notebooks()
        page_order = self.page_order(notebooks)
        if not page_order:
            raise ValueError("this Noteastic backup holds no notebook pages to convert")
        built = self.build(page_order)
        self.listing(notebooks, built)
        self.report()
        return self.doc

    def notebooks(self) -> List[Tuple[Any, str, str]]:
        """``(book id, name, folder path)`` of every live notebook, in library order."""
        folder_rows = self.rows("Folder", ("ID", "Name"), ("ID",), MAX_FOLDERS, extras=("IsDeleted",))
        folders: Dict[Any, str] = {}
        for fid, name, deleted in folder_rows:
            key = _ident(fid)
            if key is None or key in folders:
                continue
            if _flag(deleted):
                self.counts["deleted_folders"] += 1
                continue
            folders[key] = (_text(name) or "").strip() or "Untitled folder"
        parent: Dict[Any, Any] = {}
        for pid, cid in self.rows("FolderFolderContent", ("FolderID", "ChildFolderID"),
                                  ("FolderID", "ChildFolderID"), MAX_LINKS):
            p, c = _ident(pid), _ident(cid)
            if p is not None and c is not None and c != p and c not in parent:
                parent[c] = p
        book_rows = self.rows("Book", ("ID", "Name"), ("ID",), MAX_FOLDERS, extras=("IsDeleted",))
        self.has_books = "Book" not in self.failed_tables
        books: Dict[Any, str] = {}
        for bid, name, deleted in book_rows:
            key = _ident(bid)
            if key is None or key in books or key in self.deleted_books:
                continue
            if _flag(deleted):
                self.counts["deleted_books"] += 1
                self.deleted_books.add(key)
                continue
            books[key] = (_text(name) or "").strip() or "Untitled notebook"
        book_folder: Dict[Any, Any] = {}
        for fid, bid in self.rows("FolderBookContent", ("FolderID", "ChildBookID"), ("FolderID", "ChildBookID"),
                                  MAX_LINKS):
            f, b = _ident(fid), _ident(bid)
            if f is not None and b is not None and b not in book_folder:
                book_folder[b] = f

        def by_name(names: Dict[Any, str]) -> Callable[[Any], Tuple[str, Tuple[int, Any]]]:
            return lambda ident: (names[ident].casefold(), _order_key(ident))

        children: Dict[Any, List[Any]] = {}
        roots: List[Any] = []
        for fid in folders:
            p = parent.get(fid)
            if p is not None and p in folders:
                children.setdefault(p, []).append(fid)
            else:
                roots.append(fid)
        in_folder: Dict[Any, List[Any]] = {}
        for bid in books:
            f = book_folder.get(bid)
            if f is not None and f in folders:
                in_folder.setdefault(f, []).append(bid)
        order: List[Tuple[Any, str, str]] = []
        placed: set = set()
        visited: set = set()
        stack: List[Tuple[Any, Tuple[str, ...]]] = [(f, ()) for f in sorted(roots, key=by_name(folders), reverse=True)]
        while stack:
            fid, path = stack.pop()
            if fid in visited:
                continue
            visited.add(fid)
            path = path + (folders[fid],)
            for bid in sorted(in_folder.get(fid, []), key=by_name(books)):
                if bid not in placed:
                    placed.add(bid)
                    order.append((bid, books[bid], " / ".join(path)))
            if len(path) < MAX_FOLDER_DEPTH:
                stack += [(c, path) for c in sorted(children.get(fid, []), key=by_name(folders), reverse=True)
                          if c not in visited]
        for bid in sorted((b for b in books if b not in placed), key=by_name(books)):
            order.append((bid, books[bid], ""))
        return order

    def page_order(self, notebooks: List[Tuple[Any, str, str]]) -> List[Tuple[Any, int]]:
        """``(page ID, index of its notebook in notebooks)`` of the pages to convert, in order."""
        rows = self.rows("BookContent", ("BookID", "PageIndex", "PageID"), ("BookID", "PageID"), MAX_LINKS,
                         essential=True)
        by_book: Dict[Any, List[Tuple[Tuple[int, Any], int, Any]]] = {}
        for seq, (bid, index, pid) in enumerate(rows):
            b, p = _ident(bid), _ident(pid)
            if b is None or p is None:
                self.counts["bad_links"] += 1
                continue
            by_book.setdefault(b, []).append((_order_key(_num(index)), seq, p))
        if not self.has_books:  # no usable Book table: every BookID of BookContent is a notebook
            notebooks[:] = [(b, f"Notebook {b}", "") for b in sorted(by_book, key=_order_key)]
        known = {n[0] for n in notebooks} | self.deleted_books
        orphans = [b for b in by_book if b not in known]
        if orphans:
            self.counts["orphan_books"] += len(orphans)
        order: List[Tuple[Any, int]] = []
        for number, (bid, _name, _path) in enumerate(notebooks):
            entries = sorted(by_book.get(bid, []))
            room = max(0, MAX_PAGES - len(order))
            order += [(pid, number) for _index, _seq, pid in entries[:room]]
            self.counts["over_pages"] += max(0, len(entries) - room)
        return order

    # -- pages -----------------------------------------------------------------------------

    def build(self, page_order: List[Tuple[Any, int]]) -> Dict[int, List[int]]:
        """Convert the pages; returns the page numbers each notebook (by its index) received."""
        wanted = {pid for pid, _book in page_order}
        pages: Dict[Any, Tuple[Any, ...]] = {}
        for row in self.rows("Page", ("ID", "GridID", "Width", "Height", "BackgroundColor"), ("ID",),
                             MAX_PAGE_ROWS, essential=True, accept=lambda r: _ident(r[0]) in wanted,
                             extras=("IsDeleted",)):
            pages.setdefault(_ident(row[0]), row)
        grid_ids = {_ident(r[1]) for r in pages.values()} - {None}
        grids: Dict[Any, str] = {}
        for gid, gtype in self.rows("Grid", ("ID", "GridType"), ("ID",), MAX_PAGE_ROWS,
                                    accept=lambda r: _ident(r[0]) in grid_ids):
            grids.setdefault(_ident(gid), (_text(gtype, 200) or "").strip())
        pdf_grids: Dict[Any, Tuple[Any, ...]] = {}
        for row in self.rows("PDFGrid", ("GridID", "FilePath", "PageIndex", "Password"), ("GridID", "FilePath"),
                             MAX_PAGE_ROWS, caps={"FilePath": MAX_PATH_CHARS},
                             accept=lambda r: _ident(r[0]) in grid_ids):
            pdf_grids.setdefault(_ident(row[0]), row)
        contents: Dict[Any, List[Any]] = {}
        element_ids: set = set()
        for pid, eid in self.rows("PageContent", ("PageID", "ElementID"), ("PageID", "ElementID"), MAX_ELEMENTS,
                                  accept=lambda r: _ident(r[0]) in wanted):
            p, e = _ident(pid), _ident(eid)
            if e is None:
                continue
            listed = contents.setdefault(p, [])
            listed.append(e)
            element_ids.add(e)
        elements = self.elements(element_ids)
        live = {e for e, row in elements.items() if not row[3]}
        tables = self.element_tables(live, elements)
        built: Dict[int, List[int]] = {}
        for pid, book in page_order:
            row = pages.get(pid)
            if row is None:
                self.counts["missing_pages"] += 1
                continue
            if _flag(row[5]):
                self.counts["deleted_pages"] += 1
                continue
            page_number = len(self.doc.pages) + 1
            page, frame = self.page(page_number, row, grids, pdf_grids)
            self.fill(page, frame, contents.get(pid, []), elements, tables)
            self.doc.pages.append(page)
            built.setdefault(book, []).append(page_number)
        return built

    def page(self, number: int, row: Tuple[Any, ...], grids: Dict[Any, str],
             pdf_grids: Dict[Any, Tuple[Any, ...]]) -> Tuple[Page, _Frame]:
        _pid, gid, width, height, background_colour, _deleted = row
        w, h = _num(width), _num(height)
        if w is None or h is None or not (1.0 <= w <= MAX_PAGE_UNITS and 1.0 <= h <= MAX_PAGE_UNITS):
            w, h = DEFAULT_PAGE_UNITS
            self.notes.add(number, "no usable page size; A4 was used")
        colour, _readable = _colour(background_colour, None)
        if colour is not None and colour[3] > 0.05 and min(colour[:3]) < 0.97:
            self.counts["page_colours"] += 1
        key = _ident(gid)
        grid_type = grids.get(key, "")
        pdf_row = pdf_grids.get(key)
        if pdf_row is not None or grid_type.lower() == "pdf":
            placed = self.pdf_page(number, pdf_row, w, h)
            if placed is not None:
                return placed
        paper = self.paper(grid_type)
        if paper != "plain":
            self.counts["paper"] += 1
        return Page(width=w * UNIT_PT, height=h * UNIT_PT, paper=paper), _Frame(UNIT_PT)

    def paper(self, grid_type: str) -> str:
        g = grid_type.lower()
        if g in ("", "pdf", "blank", "none", "plain", "empty", "default"):
            return "plain"
        if "check" in g or "grid" in g or "square" in g or "karo" in g:
            return "grid"
        if "bullet" in g or "dot" in g:
            return "dotted"
        if "line" in g or "ruled" in g or "cornell" in g:
            return "lined"
        self.type_names["grid"][grid_type[:40]] += 1
        return "plain"

    def pdf_page(self, number: int, pdf_row: Optional[Tuple[Any, ...]], w: float,
                 h: float) -> Optional[Tuple[Page, _Frame]]:
        """A PDF-backed page: the PDF page's visible area (its CropBox) fitted and centred in the
        ``w`` x ``h`` Noteastic page, as pdf.js and Windows' PDF renderer show it.  The converted
        page is the PDF page's MediaBox, as gnnote's writers draw PDF pages, so the ink is mapped
        onto the visible area inside it."""
        if pdf_row is None:
            self.notes.add(number, "it is a PDF page without a PDF reference; it is plain paper")
            return None
        _gid, file_path, page_index, password = pdf_row
        path = _text(file_path, MAX_PATH_CHARS)
        if not path or not path.strip():
            self.notes.add(number, "its PDF background has no file name; the page is plain paper")
            return None
        loaded = self.load_pdf(path)
        if loaded is None:
            if self.assets is not None:
                self.notes.add(number, f"its PDF background {path.strip()} is missing from the backup or unreadable; "
                                       "the page is plain paper")
            return None
        member, boxes = loaded
        index = _num(page_index, 0.0)
        if index is None or index != int(index) or not 0 <= index < len(boxes):
            self.notes.add(number, f"the PDF page it shows does not exist in {member}; the page is plain paper")
            return None
        index = int(index)
        if _text(password, 256):
            self.counts["passwords"] += 1
        page_w, page_h, (left, top, right, bottom) = boxes[index]
        pw, ph = right - left, bottom - top
        s = min(w / pw, h / ph)
        if abs(w / h - pw / ph) > 0.01 * (pw / ph):
            self.counts["letterboxed"] += 1
        if abs(pw - page_w) > 0.5 or abs(ph - page_h) > 0.5:
            self.counts["cropped"] += 1
        # Noteastic units -> the visible area (pt) -> the MediaBox page: (u - ox) / s + left
        frame = _Frame(1.0 / s, (w - pw * s) / 2.0 - left * s, (h - ph * s) / 2.0 - top * s)
        return Page(width=page_w, height=page_h, background=PdfBackground(member, index)), frame

    def load_pdf(self, path: str) -> Optional[Tuple[str, List[Tuple[float, float, Tuple[float, float, float, float]]]]]:
        """The PDF a ``FilePath`` names: ``(pdf id, pages)`` with each page's displayed size and
        visible area (left, top, right, bottom in pt from the displayed page's top-left corner);
        ``None`` when it is missing or unreadable.  Each archive member is parsed once, however
        its path is spelled."""
        key = path.replace("\\", "/").strip().lower()
        if key in self.pdfs:
            return self.pdfs[key]
        result = None
        if self.assets is None:
            self.no_archive = True
        else:
            member = self.assets.resolve(path)
            if member is not None and member in self.pdf_members:
                result = self.pdf_members[member]
            elif member is not None:
                _member, data = self.assets.read(path)
                if data is not None and b"%PDF" in data[:1024]:
                    boxes = _pdf_boxes(data)
                    if boxes:
                        pdf_id = member.replace("\\", "/")  # a ZIP written on Windows may use backslashes
                        self.doc.pdfs[pdf_id] = data
                        result = (pdf_id, boxes)
                self.pdf_members[member] = result
        self.pdfs[key] = result
        return result

    # -- elements --------------------------------------------------------------------------

    def elements(self, wanted: set) -> Dict[Any, Tuple[Any, ...]]:
        """``Element`` rows of the wanted IDs: ``(type, order key, matrix or None, deleted)``."""
        rows = self.rows("Element", ("ID", "ElementType", "CreationDate", "M11", "M12", "M21", "M22", "M31", "M32",
                                     "IsDeleted"), ("ID",), MAX_ELEMENTS, caps={"CreationDate": 256},
                         accept=lambda r: _ident(r[0]) in wanted)
        out: Dict[Any, Tuple[Any, ...]] = {}
        for row in rows:
            key = _ident(row[0])
            if key in out:
                continue
            values = [_num(v) for v in row[3:9]]
            defaults = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            if any(v is None and raw is not None for v, raw in zip(values, row[3:9])):
                matrix = None  # present but unreadable (text, too large, non-finite)
            else:
                m = [d if v is None else v for v, d in zip(values, defaults)]
                if max(abs(v) for v in m[:4]) > MAX_MATRIX_SCALE or max(abs(m[4]), abs(m[5])) > MAX_COORD_UNITS:
                    matrix = None
                else:
                    matrix = _Matrix(*m)
            etype = (_text(row[1], 64) or "").strip()
            created = row[2] if row[2] is not TOO_LARGE else None
            out[key] = (etype, (_order_key(created), _order_key(row[0])), matrix, _flag(row[9]))
        return out

    def element_tables(self, live: set, elements: Dict[Any, Tuple[Any, ...]]) -> Dict[str, Dict[Any, Tuple[Any, ...]]]:
        def accept(r: Tuple[Any, ...]) -> bool:
            return _ident(r[0]) in live

        specs = {
            "stroke": ("StrokeElement", ("ElementID", "StrokeWidth", "StrokeHeight", "Color", "IsHighlighter",
                                         "IgnorePressure", "PointsData"), {"PointsData": MAX_POINTS_CHARS}),
            "line": ("LineElement", ("ElementID", "LineType", "X0", "Y0", "X1", "Y1", "StrokeColor", "StrokeWidth",
                                     "StrokeDashed", "IsHighlighter"), None),
            "shape": ("ShapeElement", ("ElementID", "ShapeType", "X0", "Y0", "X1", "Y1", "StrokeColor",
                                       "StrokeWidth", "StrokeDashed", "FillColor"), None),
            "image": ("ImageElement", ("ElementID", "ImageWidth", "ImageHeight", "FilePath"),
                      {"FilePath": MAX_PATH_CHARS}),
            "text": ("TextElement", ("ElementID", "Text", "Width", "FontFamily", "FontSize", "Color",
                                     "HorizontalTextAlignment", "Spans"), {"Text": MAX_TEXT_CHARS}),
        }
        needed = {kind for e in live for kind in ([_ELEMENT_TABLES.get(elements[e][0].lower())]
                                                   if elements[e][0] else list(specs))}
        out: Dict[str, Dict[Any, Tuple[Any, ...]]] = {}
        for kind, (table, columns, caps) in specs.items():
            out[kind] = {}
            if kind not in needed:
                continue
            for row in self.rows(table, columns, ("ElementID",), MAX_ELEMENTS, caps=caps, accept=accept):
                out[kind].setdefault(_ident(row[0]), row)
        return out

    def fill(self, page: Page, frame: _Frame, ids: List[Any], elements: Dict[Any, Tuple[Any, ...]],
             tables: Dict[str, Dict[Any, Tuple[Any, ...]]]) -> None:
        seen: set = set()
        live: List[Tuple[Tuple[Any, Any], Any]] = []
        for eid in ids:
            if eid in seen:
                continue
            seen.add(eid)
            row = elements.get(eid)
            if row is None:
                self.counts["missing_elements"] += 1
                continue
            if row[3]:
                continue  # soft-deleted (erased) element
            live.append((row[1], eid))
        live.sort(key=lambda item: item[0])
        for _key, eid in live:
            etype, _order, matrix = elements[eid][:3]
            kind = _ELEMENT_TABLES.get(etype.lower()) if etype else None
            if etype and kind is None:
                self.type_names["element"][etype[:40]] += 1
                continue
            if kind is None:  # no type recorded: the table that holds the element decides
                kind = next((k for k in ("stroke", "line", "shape", "image", "text") if eid in tables[k]), None)
            data = tables[kind].get(eid) if kind is not None else None
            if data is None:
                self.counts["no_data"] += 1
                continue
            if matrix is None:
                self.counts["bad_geometry"] += 1
                continue
            highlighter = etype.lower() in _HIGHLIGHTER_TYPES
            if kind == "stroke":
                stroke = self.stroke(data, matrix, frame, highlighter)
                if stroke is not None:
                    page.strokes.append(stroke)
            elif kind == "line":
                page.strokes.extend(self.line(data, matrix, frame, highlighter))
            elif kind == "shape":
                page.strokes.extend(self.shape(data, matrix, frame))
            elif kind == "image":
                image = self.image(data, matrix, frame)
                if image is not None:
                    page.images.append(image)
            else:
                box = self.text(data, matrix, frame)
                if box is not None:
                    page.texts.append(box)

    # -- element kinds -------------------------------------------------------------------------

    def place(self, matrix: _Matrix, frame: _Frame, pts: Sequence[XY]) -> Optional[List[XY]]:
        """Element-space points -> page points; ``None`` (counted) when any lies absurdly far."""
        out: List[XY] = []
        limit = MAX_COORD_UNITS
        for x, y in pts:
            if abs(x) > limit or abs(y) > limit:
                self.counts["bad_geometry"] += 1
                return None
            u, v = matrix.apply(x, y)
            if abs(u) > limit or abs(v) > limit:
                self.counts["bad_geometry"] += 1
                return None
            px, py = frame.pt(u, v)
            if abs(px) > MAX_COORD_PT or abs(py) > MAX_COORD_PT:
                self.counts["bad_geometry"] += 1
                return None
            out.append((px, py))
        return out

    def colour(self, value: Any, default: RGBA) -> RGBA:
        rgba, ok = _colour(value, default)
        if not ok:
            self.counts["bad_colours"] += 1
        return rgba if rgba is not None else default

    def stroke(self, row: Tuple[Any, ...], matrix: _Matrix, frame: _Frame, highlighter: bool) -> Optional[Stroke]:
        _eid, width, height, colour, is_highlighter, ignore_pressure, points_data = row
        if points_data is TOO_LARGE:
            return None
        text = points_data
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8", "strict")
            except UnicodeDecodeError:
                text = None
        if not isinstance(text, str):
            self.counts["unreadable"] += 1
            return None
        if text.count(";") - 1 > self.budget.left:  # the budget cannot cover it: not even parsed
            self.budget.exhausted = True
            return None
        parsed = parse_points_data(text, MAX_POINTS_PER_STROKE, self.comma_points_left)
        # Decoding cost the same work whether or not the points could be read: every part
        # decoded counts, against the decimal-comma allowance on that slow path and against the
        # point budget, so damaged strokes cannot be decoded without bound either.
        if parsed.comma:
            self.comma_points_left -= parsed.examined
        if parsed.examined and not self.budget.take(parsed.examined):
            return None
        triples, how = parsed.points, parsed.how
        if triples is None:
            key = {"too-many": "too_many_points", "too-many-comma": "comma_budget"}.get(how, "unreadable")
            self.counts[key] += 1
            return None
        if not triples:
            self.counts["empty"] += 1
            return None
        if how == "comma":
            self.counts["comma"] += 1
        if parsed.dropped:
            self.counts["dropped_points"] += parsed.dropped
            self.counts["damaged_strokes"] += 1
        if parsed.extra:
            self.counts["extra_fields"] += 1
        placed = self.place(matrix, frame, [(x, y) for x, y, _p in triples])
        if placed is None:
            return None
        w = _num(width, 0.0) or 0.0
        h = _num(height, 0.0) or 0.0
        size = max(abs(w), abs(h))
        if not 0.0 < size <= MAX_COORD_UNITS:
            size = DEFAULT_PEN_UNITS
        base = _clamp_width(size * matrix.line_scale * frame.k)
        use_pressure = not _flag(ignore_pressure)
        a, b = PRESSURE_LAW
        varied = False
        points: List[Point] = []
        for (x, y), (_x, _y, p) in zip(placed, triples):
            if use_pressure and p is not None:
                q = min(1.0, max(0.0, p))
                if abs(q - 0.5) > 1e-6:
                    varied = True
                points.append(Point(x, y, _clamp_width(base * (a * q + b))))
            else:
                points.append(Point(x, y, base))
        if varied:
            self.counts["pressure"] += 1
        kind = "highlighter" if highlighter or _flag(is_highlighter) else "pen"
        return Stroke(points=points, color=self.colour(colour, BLACK), kind=kind, width=base)

    def line(self, row: Tuple[Any, ...], matrix: _Matrix, frame: _Frame, highlighter: bool) -> List[Stroke]:
        _eid, line_type, x0, y0, x1, y1, colour, width, dashed, is_highlighter = row
        ends = [_num(v) for v in (x0, y0, x1, y1)]
        if any(v is None for v in ends):
            self.counts["unreadable_shapes"] += 1
            return []
        ax, ay, bx, by = ends  # type: ignore[misc]
        lw = _num(width, 0.0) or 0.0
        lw = max(lw, MIN_LINE_UNITS) if 0.0 < lw <= MAX_COORD_UNITS else DEFAULT_LINE_UNITS
        rgba = self.colour(colour, BLACK)
        kind = "highlighter" if highlighter or _flag(is_highlighter) else "pen"
        pw = _clamp_width(lw * matrix.line_scale * frame.k)
        shaft = self.place(matrix, frame, [(ax, ay), (bx, by)])
        if shaft is None:
            return []
        if not self.budget.take(2):
            return []
        out = [self.polyline(shaft, pw, rgba, kind)]
        lt = (_text(line_type, 64) or "").strip()
        heads = _ARROW_TYPES.get(lt.lower(), 0)
        if not heads and lt.lower() not in _PLAIN_LINE_TYPES:
            self.type_names["line"][lt[:40]] += 1
        size = max(ARROW_MIN_UNITS, lw * ARROW_WIDTH_FACTOR)
        angle = math.atan2(by - ay, bx - ax)
        tips = [((bx, by), angle)] + ([((ax, ay), angle + math.pi)] if heads == 2 else [])
        for (tx, ty), direction in tips[:heads]:
            triangle = [(tx, ty),
                        (tx - math.cos(direction - ARROW_HALF_ANGLE) * size,
                         ty - math.sin(direction - ARROW_HALF_ANGLE) * size),
                        (tx - math.cos(direction + ARROW_HALF_ANGLE) * size,
                         ty - math.sin(direction + ARROW_HALF_ANGLE) * size),
                        (tx, ty)]
            placed = self.place(matrix, frame, triangle)
            if placed is None or not self.budget.take(8):
                continue
            # a highlighter's head is filled as translucently as gnnote's writers draw highlighters
            out.append(self.fill_stroke(placed, rgba if kind == "pen" else rgba[:3] + (rgba[3] * 0.5,)))
            out.append(self.polyline(placed, pw, rgba, kind))
        if _flag(dashed):
            self.counts["dashed"] += 1
        self.counts["shapes"] += 1
        return out

    def shape(self, row: Tuple[Any, ...], matrix: _Matrix, frame: _Frame) -> List[Stroke]:
        _eid, shape_type, x0, y0, x1, y1, colour, width, dashed, fill_colour = row
        corners = [_num(v) for v in (x0, y0, x1, y1)]
        if any(v is None for v in corners):
            self.counts["unreadable_shapes"] += 1
            return []
        ax, ay, bx, by = corners  # type: ignore[misc]
        left, right, top, bottom = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
        cx, cy, rx, ry = (left + right) / 2.0, (top + bottom) / 2.0, (right - left) / 2.0, (bottom - top) / 2.0
        st = (_text(shape_type, 64) or "").strip()
        key = st.lower()
        lines: List[List[XY]] = []
        straight = True
        if key in _ELLIPSES:
            outline = ellipse_points(cx, cy, rx, ry, ELLIPSE_SAMPLES)
            straight = False
        elif key == "triangle":
            outline = [(cx, top), (right, bottom), (left, bottom), (cx, top)]
        elif key in _POLYGONS:
            outline = ellipse_points(cx, cy, rx, ry, _POLYGONS[key], start_angle=-math.pi / 2.0)
        else:
            outline = [(left, top), (right, top), (right, bottom), (left, bottom), (left, top)]
            if key in _QUADRANTS:
                lines.append([(cx, top), (cx, bottom)])
                if key == "fourquadrant":
                    lines.append([(left, cy), (right, cy)])
            elif key not in _RECTANGLES:
                self.type_names["shape"][st[:40]] += 1
        lw = _num(width, 0.0) or 0.0
        lw = max(lw, MIN_LINE_UNITS) if 0.0 < lw <= MAX_COORD_UNITS else DEFAULT_LINE_UNITS
        pw = _clamp_width(lw * matrix.line_scale * frame.k)
        rgba = self.colour(colour, BLACK)
        placed = self.place(matrix, frame, outline)
        if placed is None:
            return []
        extra = [self.place(matrix, frame, seg) for seg in lines]
        if any(seg is None for seg in extra):
            return []
        if not self.budget.take(2 * len(placed) + sum(len(seg) for seg in extra if seg)):
            return []
        out: List[Stroke] = []
        fill, ok = _colour(fill_colour, None)
        if not ok:
            self.counts["bad_colours"] += 1
        if fill is not None and fill[3] > 0.0:
            out.append(self.fill_stroke(placed, fill))
        out.append(self.polyline(placed, pw, rgba, "pen", straight=straight))
        out += [self.polyline(seg, pw, rgba, "pen") for seg in extra if seg]
        if _flag(dashed):
            self.counts["dashed"] += 1
        self.counts["shapes"] += 1
        return out

    @staticmethod
    def polyline(pts: Sequence[XY], width: float, colour: RGBA, kind: str, straight: bool = True) -> Stroke:
        points = [Point(x, y, width) for x, y in pts]
        return Stroke(points, color=colour, kind=kind, width=width,
                      controls=straight_controls(points) if straight and len(points) >= 2 else None)

    @staticmethod
    def fill_stroke(pts: Sequence[XY], colour: RGBA) -> Stroke:
        ring = [Point(x, y, 0.0) for x, y in pts]
        return Stroke(points=ring, color=colour, kind="fill", width=0.0, outline=[list(ring)])

    def image(self, row: Tuple[Any, ...], matrix: _Matrix, frame: _Frame) -> Optional[Image]:
        _eid, image_width, image_height, file_path = row
        path = _text(file_path, MAX_PATH_CHARS)
        if not path or not path.strip():
            self.counts["missing_images"] += 1
            return None
        if self.assets is None:
            self.no_archive = True
            return None
        _member, data = self.assets.read(path)
        if data is None:
            self.counts["missing_images"] += 1
            return None
        fmt = image_format(data)
        if fmt not in ("png", "jpeg", "pdf"):
            self.counts["unsupported_images"] += 1
            return None
        iw, ih = _num(image_width, 0.0) or 0.0, _num(image_height, 0.0) or 0.0
        if not (0 < iw <= MAX_COORD_UNITS and 0 < ih <= MAX_COORD_UNITS):
            pixels = image_pixel_size(data) if fmt != "pdf" else None
            if pixels is None:
                self.counts["bad_geometry"] += 1
                return None
            iw, ih = float(pixels[0]), float(pixels[1])
        centre = self.place(matrix, frame, [(iw / 2.0, ih / 2.0)])
        if centre is None:
            return None
        (cx, cy), = centre
        w, h = iw * matrix.scale_x * frame.k, ih * matrix.scale_y * frame.k
        if not (w > 0 and h > 0):
            self.counts["bad_geometry"] += 1
            return None
        if len(data) > self.image_bytes_left:
            self.counts["image_budget"] += 1
            return None
        self.image_bytes_left -= len(data)
        if matrix.skewed:
            self.counts["skewed"] += 1
        self.counts["images"] += 1
        return Image(cx - w / 2.0, cy - h / 2.0, w, h, data, fmt=fmt, rotation=round(matrix.rotation, 6) % 360.0)

    def text(self, row: Tuple[Any, ...], matrix: _Matrix, frame: _Frame) -> Optional[TextBox]:
        _eid, value, width, font_family, font_size, colour, alignment, spans = row
        if value is TOO_LARGE:
            return None
        text = _text(value, MAX_TEXT_CHARS)
        if text is None:
            return None
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            return None
        if _has_spans(spans):
            self.counts["spans"] += 1
        size_u = _num(font_size, 0.0) or 0.0
        if not 0.0 < size_u <= 10_000.0:
            size_u = DEFAULT_FONT_UNITS
        sx, sy = matrix.scale_x * frame.k, matrix.scale_y * frame.k
        size = min(max(size_u * sy, 0.5), MAX_WIDTH_PT)
        lines = text.split("\n")
        w_u = _num(width, 0.0) or 0.0
        if 0.0 < w_u <= MAX_COORD_UNITS:
            w = w_u * sx
        else:
            w = max(1.0, max(len(line) for line in lines) * size * CHAR_WIDTH)
        h = max(1, len(lines)) * size * LINE_HEIGHT
        rotation = round(matrix.rotation, 6) % 360.0
        if matrix.det < 0 and sx > 0 and sy > 0:
            # Mirrored: the box Noteastic shows lies on the other side of the element's origin.
            # Keep the box upright (rotation from the y axis) around the centre of that area.
            centre = self.place(matrix, frame, [(w / sx / 2.0, h / sy / 2.0)])
            if centre is None:
                return None
            (cx, cy), = centre
            t = math.radians(rotation)
            hw, hh = max(w, 1.0) / 2.0, h / 2.0
            x, y = cx - (hw * math.cos(t) - hh * math.sin(t)), cy - (hw * math.sin(t) + hh * math.cos(t))
        else:
            origin = self.place(matrix, frame, [(0.0, 0.0)])
            if origin is None:
                return None
            (x, y), = origin
        rgba = self.colour(colour, BLACK)
        font = (_text(font_family, 200) or "").strip() or None
        align = "left"
        if isinstance(alignment, str):
            alignment = alignment.strip().lower()
            align = _ALIGN_NAMES.get(alignment, "left")
        number = _num(alignment) if alignment is not None and not isinstance(alignment, bytes) else None
        if number is not None and number.is_integer() and 0 <= number < 10:
            align = _ALIGN_NUMBERS.get(int(number), "left")
        if matrix.skewed:
            self.counts["skewed"] += 1
        self.counts["texts"] += 1
        run = TextRun(text, font=font, size=size, color=rgba)
        return TextBox(x, y, max(w, 1.0), h, text, runs=[run], color=rgba, size=size, rotation=rotation, align=align)

    # -- warnings --------------------------------------------------------------------------------

    def listing(self, notebooks: List[Tuple[Any, str, str]], built: Dict[int, List[int]]) -> None:
        """The title, and with several notebooks a warning naming each one's pages."""
        if len(notebooks) == 1:
            if self.has_books:
                self.doc.title = notebooks[0][1]
            return
        if not notebooks:
            return
        parts = []
        for number, (_bid, name, path) in enumerate(notebooks[:MAX_LISTED]):
            label = f"{path} / {name}" if path else name
            pages = built.get(number, [])
            if not pages:
                parts.append(f"{label!r} (no pages)")
            elif len(pages) == 1:
                parts.append(f"{label!r} (page {pages[0]})")
            else:
                parts.append(f"{label!r} (pages {pages[0]}-{pages[-1]})")
        more = f", and {len(notebooks) - MAX_LISTED} more" if len(notebooks) > MAX_LISTED else ""
        self.warn(f"This Noteastic backup holds {len(notebooks)} notebooks; they were converted into one document, "
                  f"in library order: {', '.join(parts)}{more}")

    def report(self) -> None:
        c = self.counts
        self.notes.emit(self.warn)

        def names(kind: str) -> str:
            found = self.type_names[kind]
            shown = ", ".join(n or "(empty)" for n, _ in found.most_common(MAX_NAMES))
            return shown + (", ..." if len(found) > MAX_NAMES else "")

        if self.missing_tables or self.missing_columns:
            parts = []
            if self.missing_tables:
                parts.append("missing table(s) " + ", ".join(dict.fromkeys(self.missing_tables)))
            if self.missing_columns:
                cols = list(dict.fromkeys(self.missing_columns))
                parts.append("missing column(s) " + ", ".join(cols[:30]) + (", ..." if len(cols) > 30 else ""))
            self.warn("noteastic.db differs from the documented layout (" + "; ".join(parts)
                      + "); defaults were used where possible")
        messages = [
            ("over_pages", f"The library holds more than {MAX_PAGES} pages; the rest ({{n}}) were not converted"),
            ("deleted_books", "{n} notebook(s) marked as deleted were skipped"),
            ("deleted_folders", "{n} folder(s) marked as deleted were skipped; their notebooks are listed outside "
                                "any folder"),
            ("deleted_pages", "{n} page(s) marked as deleted were skipped"),
            ("orphan_books", "The pages of {n} notebook(s) that are not in the library's notebook list were skipped"),
            ("missing_pages", "{n} page reference(s) point to pages missing from the database and were skipped"),
            ("bad_links", "{n} unusable page reference(s) were skipped"),
            ("missing_elements", "{n} page item(s) refer to elements missing from the database and were skipped"),
            ("no_data", "{n} element(s) without their data row were skipped"),
            ("bad_geometry", "{n} element(s) with unusable positions or sizes were skipped"),
            ("unreadable", "{n} stroke(s) with unreadable point data were skipped"),
            ("dropped_points", f"{{n}} unreadable point(s) were left out of {c['damaged_strokes']} stroke(s)"),
            ("extra_fields", "The points of {n} stroke(s) carry more values than position and pressure; the extra "
                             "values were ignored"),
            ("too_many_points", f"{{n}} stroke(s) with more than {MAX_POINTS_PER_STROKE} points were skipped"),
            ("empty", "{n} stroke(s) without points were skipped"),
            ("comma", "{n} stroke(s) store their numbers with a decimal comma; gnnote guessed where each number "
                      "ends, so check their shapes"),
            ("comma_budget", f"{{n}} stroke(s) written with a decimal comma were skipped: gnnote decodes at most "
                             f"{MAX_COMMA_POINTS} such points per document"),
            ("pressure", "Pen pressure of {n} stroke(s) was turned into widths with an approximate curve"),
            ("shapes", "{n} line(s) and shape(s) were converted to ink strokes"),
            ("unreadable_shapes", "{n} line(s) or shape(s) without usable coordinates were skipped"),
            ("dashed", "{n} dashed line(s) or shape(s) are drawn solid"),
            ("bad_colours", "{n} colour value(s) could not be read; black (or no fill) was used"),
            ("texts", "{n} text box(es) were converted; Noteastic lays text out itself, so their sizes and line "
                      "breaks are approximate"),
            ("spans", "Rich-text formatting of {n} text box(es) was not converted; each keeps one font, size and "
                      "colour"),
            ("missing_images", "{n} image(s) are missing from the backup and were skipped"),
            ("unsupported_images", "{n} image(s) in formats other than PNG or JPEG were skipped"),
            ("skewed", "{n} skewed or mirrored image(s) or text box(es) were placed without the skew or mirror "
                       "(upright, over the area Noteastic shows them in)"),
            ("image_budget", f"{{n}} picture placement(s) were skipped: the backup places its pictures more often "
                             f"than gnnote copies them (at most the backup's size plus "
                             f"{SHARED_IMAGE_BYTES // (1024 * 1024)} MB of pictures)"),
            ("passwords", "{n} page(s) show a password-protected PDF; the target app may ask for the password or "
                          "show those pages blank"),
            ("letterboxed", "{n} PDF page(s) have a different shape in Noteastic than in the PDF; the converted pages "
                            "take the PDF's shape, so ink beside the PDF page may lie outside them"),
            ("cropped", "{n} PDF page(s) are cropped (their CropBox is smaller than the page); the converted pages "
                        "show the whole PDF page, with the ink on the cropped area"),
            ("page_colours", "The background colour of {n} page(s) was not converted (the pages are white)"),
            ("paper", "{n} page(s) use lined, squared or dotted paper; it is passed on as a paper style, which "
                      "the target app draws with its own spacing (or not at all)"),
        ]
        for key, template in messages:
            if c[key]:
                self.warn(template.format(n=c[key]))
        if self.type_names["element"]:
            n = sum(self.type_names["element"].values())
            self.warn(f"{n} element(s) of unknown types were skipped ({names('element')})")
        if self.type_names["shape"]:
            n = sum(self.type_names["shape"].values())
            self.warn(f"{n} shape(s) of unknown types were drawn as rectangles ({names('shape')})")
        if self.type_names["line"]:
            n = sum(self.type_names["line"].values())
            self.warn(f"{n} line(s) of unknown types were drawn without arrowheads ({names('line')})")
        if self.type_names["grid"]:
            n = sum(self.type_names["grid"].values())
            self.warn(f"{n} page(s) use paper types gnnote does not know and are plain ({names('grid')})")
        if self.budget.exhausted:
            self.warn("The library holds more ink points than gnnote reads in one go; the remaining strokes were "
                      "skipped")
        if self.no_archive:
            self.warn("The database was converted without its backup archive, so its PDFs and images are missing; "
                      "convert the .ntcbak file to keep them")
        if self.assets is not None:
            bundle = self.assets.bundle
            for message in (bundle.size_warning("Noteastic backup"), bundle.failure_warning("Noteastic backup")):
                if message:
                    self.warn(message)


# --------------------------------------------------------------------------- public API


def read_ntcbak(data: bytes) -> Document:
    """Read a Noteastic library backup (``.ntcbak``) or a bare ``noteastic.db`` given as bytes.

    Every notebook of the library becomes part of one :class:`Document` (see the module
    docstring); ``Document.warnings`` always starts with :data:`EXPERIMENTAL_WARNING`.
    Raises :class:`ValueError` when the data is not a Noteastic backup or its database cannot be
    opened, and when the library holds no pages.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_ntcbak expects bytes")
    data = bytes(data)
    assets: Optional[_Assets] = None
    wal = False
    if is_sqlite(data):
        db_bytes = data
    elif data[:2] == b"PK":
        bundle = ZipBundle(data, "Noteastic backup")
        member = find_database(bundle.names)
        if member is None:
            raise ValueError("not a Noteastic backup: the archive holds no noteastic.db database")
        raw = bundle.read(member)
        if raw is None:
            if member in bundle.skipped:
                raise ValueError("the Noteastic database (noteastic.db) in this backup is too large to read "
                                 f"(more than {MAX_MEMBER_BYTES // (1024 * 1024)} MB unpacked)")
            raise ValueError("the Noteastic database (noteastic.db) in this backup cannot be unpacked (the archive is "
                             "damaged, encrypted or uses a compression method gnnote does not support)")
        if not is_sqlite(raw):
            raise ValueError("not a Noteastic backup: its noteastic.db is not an SQLite database")
        db_bytes = raw
        prefix = member.replace("\\", "/")[: -len(DB_NAME)]
        assets = _Assets(bundle, prefix)
        wal_member = assets.resolve(prefix + DB_NAME + "-wal")
        wal = wal_member is not None and (bundle.size(wal_member) or 0) > 0
    else:
        raise ValueError("not a Noteastic backup (expected a .ntcbak ZIP archive or a noteastic.db SQLite database)")
    try:
        db = SqliteDatabase(db_bytes, "the Noteastic database (noteastic.db)")
    except SqliteError as exc:
        raise ValueError(f"not a readable Noteastic backup: {exc}") from exc
    try:
        doc = _Reader(db, assets, len(data)).read()
    finally:
        db.close()
    if wal:
        doc.warn("The backup also holds a write-ahead log (noteastic.db-wal); changes still in it were not read")
    return doc
