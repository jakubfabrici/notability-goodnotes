"""Small helpers shared by the open-format codecs (Xournal++, Saber, Excalidraw).

Everything here is format-neutral: image sniffing, polylines from model strokes, colour
clamping, rough text extents, counted warnings and the bounded ZIP / stream readers the
readers use against decompression bombs (``docs/design.md`` section 1).
"""
from __future__ import annotations

import io
import math
import zipfile
import zlib
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .geometry import flatten_bezier
from .model import RGBA, Document, Point, Stroke
from .notability.writer import exif_rotation, image_pixel_size, jpeg_exif_orientation

__all__ = ["MAX_MEMBER_BYTES", "MAX_TOTAL_BYTES", "FLATTEN_STEP_PT", "sniff_image", "image_pixel_size",
           "jpeg_exif_orientation", "exif_rotation", "stroke_polyline", "clamp_rgba", "to_byte", "is_finite",
           "estimate_text_extent", "bbox_matches", "Counter", "BoundedZip", "inflate_limited",
           "ensure_bytes"]

MAX_MEMBER_BYTES = 256 * 1024 * 1024  # declared (inflated) size above which a ZIP member is skipped
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # inflated bytes one archive may hand out in total
FLATTEN_STEP_PT = 1.0  # Bezier chains become polylines with about this spacing (pt)
TEXT_CHAR_WIDTH = 0.6  # em fraction used to guess a text box's width (as the GoodNotes writer does)
TEXT_LINE_HEIGHT = 1.25  # line height in ems used to guess a text box's height


def ensure_bytes(data: object, who: str) -> bytes:
    """``bytes(data)`` for bytes-like input; :class:`TypeError` for anything else."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"{who} expects bytes")
    return bytes(data)


def sniff_image(data: bytes) -> Optional[str]:
    """``"png"``, ``"jpeg"``, ``"gif"``, ``"webp"``, ``"bmp"``, ``"svg"`` or ``"pdf"`` from the
    leading bytes; ``None`` when unknown."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if data[:5] == b"%PDF-":
        return "pdf"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "svg"
    return None


def is_finite(*values: float) -> bool:
    try:
        return all(math.isfinite(float(v)) for v in values)
    except (TypeError, ValueError, OverflowError):
        return False


def stroke_polyline(stroke: Stroke, step: float = FLATTEN_STEP_PT) -> List[Point]:
    """The stroke's centre line as a polyline in pt: Bezier chains (``controls`` consistent
    with ``points``) are flattened with :func:`gnnote.geometry.flatten_bezier`, polylines are
    copied.  Points with a non-finite coordinate are dropped; a width that is not a positive
    finite number becomes the stroke's nominal width (or 1 pt)."""
    pts = [p for p in stroke.points if is_finite(p.x, p.y)]
    controls = stroke.controls
    if controls is not None and len(pts) == len(stroke.points) and len(pts) >= 2 \
            and len(controls) == len(pts) - 1 \
            and all(is_finite(c1.x, c1.y, c2.x, c2.y) for c1, c2 in controls):
        pts = flatten_bezier(pts, controls, max_segment=step)
    nominal = stroke.width if is_finite(stroke.width) and stroke.width > 0 else 1.0
    out: List[Point] = []
    for p in pts:
        w = p.width if p.width is not None and is_finite(p.width) and p.width > 0 else nominal
        out.append(Point(float(p.x), float(p.y), float(w)))
    return out


def to_byte(value: float) -> int:
    """A 0..1 component as 0..255, rounding halves up (``round`` would round 76.5 to 76)."""
    return int(math.floor(min(1.0, max(0.0, float(value))) * 255.0 + 0.5))


def clamp_rgba(color: Sequence[float], default_alpha: float = 1.0) -> RGBA:
    """``color`` as four floats in 0..1 (missing alpha = ``default_alpha``, NaN = 0)."""
    comps = list(color[:4]) + [default_alpha] * (4 - len(color[:4]))
    out = []
    for c in comps:
        try:
            v = float(c)
        except (TypeError, ValueError):
            v = 0.0
        out.append(min(1.0, max(0.0, v)) if v == v else 0.0)
    return out[0], out[1], out[2], out[3]


def estimate_text_extent(text: str, size: float) -> Tuple[float, float]:
    """A generous ``(width, height)`` in pt for unwrapped ``text`` at font ``size`` pt (used
    where a format stores no text box size)."""
    lines = text.split("\n") if text else [""]
    width = max(1.0, max(len(line) for line in lines) * size * TEXT_CHAR_WIDTH)
    height = max(size * TEXT_LINE_HEIGHT, len(lines) * size * TEXT_LINE_HEIGHT)
    return width, height


def bbox_matches(points: Sequence[Point], other: Sequence[Point], tolerance: float = 2.0) -> bool:
    """True when the two point sets have the same bounding box within ``tolerance`` pt plus
    2 % of the larger side (how a shape fill is matched to the outline stroke it fills)."""
    if not points or not other:
        return False
    ax0, ay0 = min(p.x for p in points), min(p.y for p in points)
    ax1, ay1 = max(p.x for p in points), max(p.y for p in points)
    bx0, by0 = min(p.x for p in other), min(p.y for p in other)
    bx1, by1 = max(p.x for p in other), max(p.y for p in other)
    tol = tolerance + 0.02 * max(bx1 - bx0, by1 - by0, ax1 - ax0, ay1 - ay0)
    return (abs(ax0 - bx0) <= tol and abs(ay0 - by0) <= tol
            and abs(ax1 - bx1) <= tol and abs(ay1 - by1) <= tol)


class Counter:
    """Counted warnings: ``add(key)`` while working, ``flush(doc)`` emits one line per key
    with the document-wide count, formatted from ``messages[key]`` (``{n}`` placeholder)."""

    def __init__(self, messages: Dict[str, str]):
        self.messages = messages
        self.counts: Dict[str, int] = {}

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def __getitem__(self, key: str) -> int:
        return self.counts.get(key, 0)

    def flush(self, doc: Document) -> None:
        for key, template in self.messages.items():
            n = self.counts.get(key, 0)
            if n:
                doc.warn(template.format(n=n))


class BoundedZip:
    """Read access to a ZIP archive with the decompression-bomb guard of the other readers:
    a member whose declared size exceeds ``MAX_MEMBER_BYTES``, or the remaining budget of
    ``MAX_TOTAL_BYTES``, is refused before a byte is inflated (``read`` returns ``None`` and
    the name is listed in ``skipped``).  Construction raises :class:`ValueError` when ``data``
    is not a readable ZIP archive."""

    def __init__(self, data: bytes, what: str, max_member: Optional[int] = None,
                 max_total: Optional[int] = None):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            self.names = self.zip.namelist()
        except (zipfile.BadZipFile, NotImplementedError, OSError, ValueError, RuntimeError,
                EOFError, IndexError, KeyError) as exc:
            raise ValueError(f"not a {what} file: not a readable ZIP archive ({exc})") from exc
        self.max_member = MAX_MEMBER_BYTES if max_member is None else max_member
        self.budget = MAX_TOTAL_BYTES if max_total is None else max_total
        self.skipped: List[str] = []
        self.broken: List[str] = []

    def size(self, name: str) -> Optional[int]:
        try:
            return self.zip.getinfo(name).file_size
        except KeyError:
            return None

    def read(self, name: str) -> Optional[bytes]:
        declared = self.size(name)
        if declared is None:
            return None
        if declared > self.max_member or declared > self.budget:
            self.skipped.append(name)
            return None
        self.budget -= declared
        try:
            with self.zip.open(name) as fh:
                data = fh.read(declared + 1)
        except Exception:  # noqa: BLE001 - corrupt member, unsupported method, bad CRC ...
            self.broken.append(name)
            return None
        if len(data) > declared:  # a member inflating past its declared size is damage
            self.broken.append(name)
            return None
        return data

    def report(self, doc: Document, max_member_label: Optional[int] = None) -> None:
        limit = (max_member_label or self.max_member) // (1024 * 1024)
        for name in self.skipped:
            doc.warn(f"Archive member {name} inflates above the {limit} MB per-member / "
                     f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB total limit and was skipped")
        for name in self.broken:
            doc.warn(f"Archive member {name} is damaged and was skipped")


def inflate_limited(data: bytes, wbits: int, limit: int) -> Tuple[bytes, Optional[str]]:
    """Inflate a zlib / gzip (``wbits`` 31 or 47) stream, concatenated gzip members included,
    producing at most ``limit`` bytes.

    Returns ``(output, problem)``; ``problem`` is ``None`` for a complete stream,
    ``"truncated"`` when the stream ends early or is damaged (the output then holds what could
    be inflated) and ``"too large"`` when the output would exceed ``limit``.
    """
    out = bytearray()
    rest = data
    while rest:
        d = zlib.decompressobj(wbits)
        try:
            chunk = d.decompress(rest, limit - len(out) + 1)
        except zlib.error:
            return bytes(out), "truncated"
        out += chunk
        if len(out) > limit:
            return bytes(out[:limit]), "too large"
        if d.unconsumed_tail:
            return bytes(out[:limit]), "too large"
        if not d.eof:
            try:
                out += d.flush()
            except zlib.error:
                pass
            return bytes(out[:limit]), "truncated"
        rest = d.unused_data
        # trailing zero padding after the last gzip member is common and harmless
        if not rest.strip(b"\x00"):
            break
    return bytes(out), None


def first_of(items: Iterable[Optional[float]], default: float) -> float:
    for item in items:
        if item is not None and is_finite(item):
            return float(item)
    return default
