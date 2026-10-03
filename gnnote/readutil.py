"""Shared helpers for the readers of the other apps' formats (MyScript Notes, Flexcil, reMarkable).

Everything here is defensive: a crafted container must not cost unbounded memory or time
(``docs/design.md`` section 1, "Bounded decompression") and must not raise anything but the
one :class:`ValueError` a reader documents for "not a <format> file".

* :class:`ZipBundle` -- ZIP member access with the per-member (256 MB declared) and
  per-container (1 GB, shared with nested archives) budgets; a member above them is skipped
  before a byte is inflated and reported once.
* :func:`load_json` -- JSON from bytes, ``None`` for anything unparsable (including the
  ``RecursionError`` of deeply nested input).
* :func:`num` -- a finite float or a default (JSON ``NaN``/``Infinity``/strings/booleans rejected).
* :class:`PointBudget` -- caps the number of ink points one document may produce.
* :class:`PageNotes` -- reports each distinct per-page problem once, with its pages.
* :func:`image_format` / :func:`image_pixel_size` -- PNG / JPEG / GIF sniffing.
* :func:`straight_controls`, :func:`ellipse_points`, :func:`quad_to_cubic` -- shape geometry
  shared by readers that turn vector shapes into ink strokes.
"""
from __future__ import annotations

import io
import json
import math
import struct
import zipfile
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .model import Point

__all__ = ["MAX_MEMBER_BYTES", "MAX_TOTAL_BYTES", "MAX_POINTS", "ByteBudget", "ZipBundle", "load_json",
           "num", "PageNotes", "PointBudget", "image_format", "image_pixel_size", "straight_controls",
           "ellipse_points", "quad_to_cubic", "bbox"]

MAX_MEMBER_BYTES = 256 * 1024 * 1024  # declared (inflated) size above which a ZIP member is skipped
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # inflated bytes one container (nested archives included) may yield
MAX_JSON_BYTES = 64 * 1024 * 1024  # a JSON part larger than this is not parsed
MAX_POINTS = 10_000_000  # ink points one document may produce (about 1 GB of Point objects)

XY = Tuple[float, float]


class ByteBudget:
    """Inflated bytes still allowed for one container (shared with archives nested in it)."""

    def __init__(self, limit: Optional[int] = None):
        self.left = MAX_TOTAL_BYTES if limit is None else limit

    def take(self, size: int) -> bool:
        if size > self.left:
            return False
        self.left -= size
        return True


class ZipBundle:
    """Bounded read access to the members of one ZIP archive.

    ``what`` names the format in the :class:`ValueError` raised when ``data`` is not a
    readable ZIP archive.  Members are looked up by exact name, then case-insensitively;
    :meth:`read` returns ``None`` for a missing, oversized, unsupported or corrupt member and
    records oversized ones in :attr:`skipped` (the caller turns that into a warning).
    """

    def __init__(self, data: bytes, what: str, budget: Optional[ByteBudget] = None):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(bytes(data)))
            infos = self.zip.infolist()
        except Exception as exc:  # noqa: BLE001 - BadZipFile, NotImplementedError, OSError, ...
            raise ValueError(f"not a {what} file: not a readable ZIP archive ({exc})") from exc
        self.budget = budget if budget is not None else ByteBudget()
        self.names: List[str] = []  # first-appearance order, duplicates removed
        seen = set()
        for info in infos:
            if info.filename not in seen:
                seen.add(info.filename)
                self.names.append(info.filename)
        self._exact = seen
        self._lower: dict = {}
        for name in self.names:
            self._lower.setdefault(name.lower(), name)
        self.skipped: List[str] = []  # refused by the size guard
        self.failed: List[str] = []  # present but unreadable (corrupt, encrypted, unsupported)

    def resolve(self, name: str) -> Optional[str]:
        """The stored spelling of ``name`` (exact match first, then case-insensitive)."""
        if name in self._exact:
            return name
        return self._lower.get(name.lower())

    def has(self, name: str) -> bool:
        return self.resolve(name) is not None

    def size(self, name: str) -> Optional[int]:
        stored = self.resolve(name)
        if stored is None:
            return None
        try:
            return int(self.zip.getinfo(stored).file_size)
        except (KeyError, ValueError):
            return None

    def read(self, name: str) -> Optional[bytes]:
        stored = self.resolve(name)
        if stored is None or stored.endswith("/"):
            return None
        try:
            declared = int(self.zip.getinfo(stored).file_size)
        except (KeyError, ValueError):
            return None
        # Decompression-bomb guard: the central directory declares the inflated size and
        # zipfile never yields more than that, so the member is refused before inflating.
        if declared > MAX_MEMBER_BYTES or not self.budget.take(declared):
            if stored not in self.skipped:
                self.skipped.append(stored)
            return None
        try:
            return self.zip.read(stored)
        except Exception:  # noqa: BLE001 - corrupt data, bad CRC, encryption, unsupported method
            if stored not in self.failed:
                self.failed.append(stored)
            return None

    def files(self, prefix: str = "") -> List[str]:
        """Member names (not directories) starting with ``prefix``, in archive order."""
        return [n for n in self.names if n.startswith(prefix) and not n.endswith("/")]

    def size_warning(self, what: str) -> Optional[str]:
        """One line describing the members the size guard refused, or ``None``."""
        if not self.skipped:
            return None
        shown = ", ".join(self.skipped[:3]) + (" ..." if len(self.skipped) > 3 else "")
        return (f"{what}: {len(self.skipped)} part(s) inflate above the {MAX_MEMBER_BYTES // (1024 * 1024)} MB "
                f"per-part / {MAX_TOTAL_BYTES // (1024 * 1024)} MB total limit and were skipped ({shown})")

    def failure_warning(self, what: str) -> Optional[str]:
        if not self.failed:
            return None
        shown = ", ".join(self.failed[:3]) + (" ..." if len(self.failed) > 3 else "")
        return f"{what}: {len(self.failed)} damaged or unsupported part(s) could not be read ({shown})"


def load_json(raw: Optional[bytes], max_bytes: int = MAX_JSON_BYTES) -> Any:
    """Parse JSON bytes; ``None`` when absent, too large or not JSON."""
    if raw is None or len(raw) > max_bytes:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError, UnicodeDecodeError, TypeError, MemoryError):
        return None


def num(value: Any, default: Optional[float] = None) -> Optional[float]:
    """``value`` as a finite float; ``default`` for booleans, strings, ``None`` and non-finite numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        f = float(value)
    except (OverflowError, ValueError):
        return default
    return f if math.isfinite(f) else default


class PageNotes:
    """Per-page problems, reported once per distinct message with the pages it concerns
    ("Pages 3, 4, 9: ..."), so a damaged long document does not flood the warnings."""

    def __init__(self) -> None:
        self._pages: Dict[str, List[int]] = {}

    def add(self, page: int, message: str) -> None:
        pages = self._pages.setdefault(message, [])
        if not pages or pages[-1] != page:
            pages.append(page)

    def emit(self, warn: Callable[[str], None]) -> None:
        for message, pages in self._pages.items():
            if len(pages) == 1:
                warn(f"Page {pages[0]}: {message}")
            else:
                shown = ", ".join(str(p) for p in pages[:5])
                more = f" and {len(pages) - 5} more" if len(pages) > 5 else ""
                warn(f"Pages {shown}{more}: {message}")
        self._pages.clear()


class PointBudget:
    """Ink points one document may still produce; :attr:`exhausted` once a request was refused."""

    def __init__(self, limit: Optional[int] = None):
        self.left = MAX_POINTS if limit is None else limit
        self.exhausted = False

    def take(self, count: int) -> bool:
        if count > self.left:
            self.exhausted = True
            return False
        self.left -= count
        return True


def image_format(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:5] == b"%PDF-":
        return "pdf"
    return None


def image_pixel_size(data: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) in pixels of a PNG, GIF or JPEG; ``None`` when unknown."""
    fmt = image_format(data)
    try:
        if fmt == "png" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w and h else None
        if fmt == "gif" and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return (w, h) if w and h else None
        if fmt == "jpeg":
            pos = 2
            while pos + 9 < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                marker = data[pos + 1]
                if marker == 0xFF:
                    pos += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    pos += 2
                    continue
                length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
                    return (w, h) if w and h else None
                if length < 2:
                    return None
                pos += 2 + length
    except struct.error:
        return None
    return None


def straight_controls(points: Sequence[Point]) -> List[Tuple[Point, Point]]:
    """Cubic handles at the thirds of every segment: the exact Bezier form of a polyline, so
    writers that fit curves keep the sides straight and the corners sharp."""
    out: List[Tuple[Point, Point]] = []
    for p, q in zip(points, points[1:]):
        dx, dy = q.x - p.x, q.y - p.y
        out.append((Point(p.x + dx / 3.0, p.y + dy / 3.0, p.width),
                    Point(p.x + 2.0 * dx / 3.0, p.y + 2.0 * dy / 3.0, q.width)))
    return out


def ellipse_points(cx: float, cy: float, rx: float, ry: float, samples: int = 64,
                   start_angle: float = 0.0) -> List[XY]:
    """A closed polygon of ``samples`` points (first point repeated last) on the ellipse."""
    pts = [(cx + rx * math.cos(start_angle + 2.0 * math.pi * i / samples),
            cy + ry * math.sin(start_angle + 2.0 * math.pi * i / samples)) for i in range(samples)]
    pts.append(pts[0])
    return pts


def quad_to_cubic(p0: XY, c: XY, p1: XY) -> Tuple[XY, XY]:
    """The two cubic handles of the quadratic Bezier ``p0, c, p1`` (degree elevation)."""
    return ((p0[0] + 2.0 / 3.0 * (c[0] - p0[0]), p0[1] + 2.0 / 3.0 * (c[1] - p0[1])),
            (p1[0] + 2.0 / 3.0 * (c[0] - p1[0]), p1[1] + 2.0 / 3.0 * (c[1] - p1[1])))


def bbox(points: Iterable[XY]) -> Optional[Tuple[float, float, float, float]]:
    xs: List[float] = []
    ys: List[float] = []
    for x, y in points:
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)
