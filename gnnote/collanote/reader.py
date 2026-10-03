"""CollaNote ``.cnote`` -> :class:`gnnote.model.Document` (tolerant reader).

Byte layout (``docs/collanote.md``; checked against ten notes of CollaNote 2025-2026 and one
112-page notebook):

Containers
    * Format 1: a ZIP (stored members, ZIP64 extras) holding ``note without pdf.cnote`` (the
      note JSON), ``0.cpage`` .. ``N.cpage`` (one JSON object per page; the number is the page
      order) and ``0.pdf`` .. (the imported PDFs, by ``pdfIndex``).
    * Format 2 (CollaNote 4.3+): a directory package ``X.cnote/`` with ``manifest.cnm`` (JSON
      ``{format: 2, minReader, writer, pageCount, pdfCount, audioCount, writtenAt}``),
      ``basenote.cdat`` (the note JSON) and the same ``N.cpage`` / ``N.pdf`` files.  It reaches
      this reader zipped, with or without the ``X.cnote/`` folder in the member names.
    * A ZIP whose only member is a ``.cnote`` file (a compressed note) is opened once.
    * Bare JSON (early notes, unverified): the note JSON itself with the pages inline in
      ``pages`` and the PDFs inline in ``importedPdfDatas`` (base64).

Note JSON
    ``name``, ``size`` = ``[W, H]`` (the page canvas in CollaNote units, shared by every page),
    ``paperOrTemplate`` (e.g. ``"Gray Notelined S"``), ``audios``, ``bookmarks`` (both reported,
    not converted), ``pages`` / ``importedPdfDatas`` (empty in containers), ``thumbnail``,
    ``favTools`` / ``_dkFavTools`` (ignored).

Page JSON
    ``_dkDrawing``: a list of base64 strings, each a protobuf ``Drawing {#1 Stroke ...}`` with
    ``Stroke {#1 uuid, #2 Point ..., #3 Style, #4 f64 time, #6 flag, #7 0}``, ``Point {#1 x, #2 y,
    #3 width, #4 f64 time, #5 / #6 / #7 force and angles}`` (f32, canvas units, origin top-left,
    y down; zero values omitted) and ``Style {#1 f32 width, #2 colour {#1 r, #2 g, #3 b, #4 a}
    f32, #3 inkType}``; inkType 1 = pen, 5 = translucent wide pen (alpha in the colour),
    27 = highlighter (stored opaque); other codes are read as pens with a warning.
    ``drawing``: a base64 Apple PKDrawing (:mod:`gnnote.pencilkit`): empty in current notes,
    the ink itself on pages last saved by CollaNote 1.x.  ``pdfPointer {pdfIndex, pageIndex}``
    (0-based) puts page ``pageIndex`` of ``<pdfIndex>.pdf`` behind the page; absent on blank
    pages.  ``attachments``: ``{type, id, center [cx/W, cy/H], bound [[0, 0], [w/W, h/H]],
    rotatedDegree, imageInData (base64 PNG/JPEG), attStringData (base64 NSKeyedArchiver
    NSAttributedString)}`` with ``type`` ``"image"`` or ``"text"``, in z-order.
    ``strokeCountBeforeSaving`` equals the stroke count of ``_dkDrawing``.

Geometry
    A PDF-backed page has the PDF page's size in pt and ``s = pdf_width / W`` pt per canvas
    unit.  A blank page is ``W x H`` units, with the scale of the nearest PDF page of the same
    note (so it matches its neighbours) or, in a note without PDFs, 0.2 mm per unit (a
    ``[1050, 1485]`` notebook is A4).  Coordinates are scaled, never flipped; ink outside the
    page is kept.

Legacy pages (CollaNote 1.x, no sample available; unverified)
    PKDrawing ink in ``drawing`` is read with the same scale; when all of a page's legacy ink
    lies in the band of page ``i`` of one continuous canvas (``y`` around ``i * H``), it is moved
    up by ``i * H``.

Hardening: ZIP members above :data:`MAX_MEMBER_BYTES` (declared size) or beyond
:data:`MAX_TOTAL_BYTES` per note are skipped before inflating; a base64 payload may decode to
at most :data:`MAX_BLOB_BYTES`; a page holds at most :data:`MAX_STROKES_PER_PAGE` strokes and
:data:`MAX_POINTS_PER_PAGE` points; at most :data:`MAX_PAGES` pages are read.  Only
:class:`ValueError` escapes :func:`read_cnote` (for data that is not a CollaNote note at all).
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import math
import re
import statistics
import struct
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import pencilkit, protobuf
from ..model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun

__all__ = ["read_cnote", "NOTE_MEMBER", "PACKAGE_NOTE_MEMBER", "MANIFEST_MEMBER", "A4_PT_PER_UNIT",
           "DEFAULT_CANVAS", "INK_PEN", "INK_TRANSLUCENT_PEN", "INK_HIGHLIGHTER"]

NOTE_MEMBER = "note without pdf.cnote"  # format 1 note JSON
PACKAGE_NOTE_MEMBER = "basenote.cdat"  # format 2 note JSON
MANIFEST_MEMBER = "manifest.cnm"  # format 2 manifest
MARKER_MEMBERS = (NOTE_MEMBER, PACKAGE_NOTE_MEMBER, MANIFEST_MEMBER)
KNOWN_FORMAT = 2  # the newest manifest "format" this reader was written against

A4_PT_PER_UNIT = 72.0 / 25.4 * 0.2  # 0.2 mm per canvas unit: a [1050, 1485] notebook is A4
DEFAULT_CANVAS = (1050.0, 1485.0)

INK_PEN = 1
INK_TRANSLUCENT_PEN = 5
INK_HIGHLIGHTER = 27
_KNOWN_INKS = (INK_PEN, INK_TRANSLUCENT_PEN, INK_HIGHLIGHTER)

MAX_MEMBER_BYTES = 256 * 1024 * 1024  # declared (decompressed) size above which a ZIP member is skipped
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # decompressed bytes one note may hand out in total
MAX_BLOB_BYTES = 64 * 1024 * 1024  # one decoded base64 payload (ink layer, image, PDF)
MAX_PAGES = 10_000
MAX_STROKES_PER_PAGE = 200_000
MAX_POINTS_PER_PAGE = 2_000_000
DEFAULT_TEXT_SIZE = 14.0  # canvas units, for a text box whose font carries no size (the sample's size)
MAX_CANVAS = 1_000_000.0  # a note size beyond this many units is damage
MAX_WIDTH = 1_000_000.0  # a stroke width beyond this many units is damage
_PAGE_NAME_RE = re.compile(r"^(\d{1,9})\.cpage$", re.IGNORECASE)
_PDF_NAME_RE = re.compile(r"^(\d{1,9})\.pdf$", re.IGNORECASE)
_AUDIO_EXTENSIONS = (".m4a", ".caf", ".aac", ".wav", ".mp3", ".aiff")
_XYW = struct.Struct("<xfxfxf")  # Point {#1 x, #2 y, #3 width} with one-byte keys, the usual layout
_PLACEHOLDER_TEXT = "This Sticker should be a image, not a TextView"  # attStringData of every image

JsonLoader = Callable[[], Optional[Any]]
BytesLoader = Callable[[], Optional[bytes]]


# --------------------------------------------------------------------------- containers


@dataclass
class _Source:
    """What a container holds, with lazy loaders so pages are parsed one at a time."""

    note: Dict[str, Any]
    pages: List[Tuple[int, JsonLoader]]
    pdfs: Dict[int, BytesLoader]
    manifest: Optional[Dict[str, Any]] = None
    folder: str = ""  # "X" of an "X.cnote/" prefix (title fallback)
    audio_members: int = 0


def _is_junk(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return name.startswith("__MACOSX/") or base.startswith("._") or base == ".DS_Store" or name.endswith("/")


def _load_json(data: Optional[bytes]) -> Optional[Any]:
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None


class _Zip:
    """Bounded ZIP member access (the decompression-bomb guard of the other readers)."""

    def __init__(self, data: bytes, doc: Document):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            self.names = [n for n in self.zip.namelist() if not _is_junk(n)]
        except (zipfile.BadZipFile, NotImplementedError, OSError, ValueError, RuntimeError, EOFError) as exc:
            raise ValueError(f"not a CollaNote note: not a readable ZIP archive ({exc})") from None
        self.doc = doc
        self.budget = MAX_TOTAL_BYTES

    def read(self, name: str) -> Optional[bytes]:
        try:
            declared = self.zip.getinfo(name).file_size
        except KeyError:
            return None
        if declared > MAX_MEMBER_BYTES:
            self.doc.warn(f"ZIP member {name} declares {declared} bytes, above the "
                          f"{MAX_MEMBER_BYTES // (1024 * 1024)} MB limit, and was skipped")
            return None
        if declared > self.budget:
            self.doc.warn(f"ZIP member {name} was skipped: the note inflates to more than "
                          f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB in total")
            return None
        self.budget -= declared
        try:
            return self.zip.read(name)
        except Exception:  # noqa: BLE001 - bad CRC, truncated or unsupported member, MemoryError
            self.doc.warn(f"ZIP member {name} is damaged and was skipped")
            return None


def _locate_root(names: Sequence[str]) -> Tuple[Optional[str], List[str]]:
    """The folder ("" or "X/") holding the note, plus any other folder that holds one too."""
    roots: Dict[str, List[str]] = {}
    for name in names:
        parts = name.split("/")
        if len(parts) > 2:
            continue
        base = parts[-1]
        if base in MARKER_MEMBERS or _PAGE_NAME_RE.match(base):
            roots.setdefault(parts[0] + "/" if len(parts) == 2 else "", []).append(base)
    if not roots:
        return None, []

    def rank(prefix: str) -> Tuple[int, int, int, str]:
        bases = roots[prefix]
        has_note = any(b in MARKER_MEMBERS for b in bases)
        return (0 if prefix == "" else 1, 0 if has_note else 1, -len(bases), prefix)

    ordered = sorted(roots, key=rank)
    return ordered[0], ordered[1:]


def _zip_source(data: bytes, doc: Document, depth: int) -> _Source:
    archive = _Zip(data, doc)
    prefix, others = _locate_root(archive.names)
    if prefix is None:
        inner = [n for n in archive.names if n.lower().endswith(".cnote")]
        if depth == 0 and len(inner) == 1 and len(archive.names) == 1:
            payload = archive.read(inner[0])
            if payload is None:
                raise ValueError("not a readable CollaNote note: the compressed note could not be extracted")
            source = _open(payload, doc, depth + 1)
            if not source.folder:
                source.folder = inner[0].rsplit("/", 1)[-1][: -len(".cnote")]
            return source
        raise ValueError("not a CollaNote note: no note JSON and no .cpage pages in the archive")
    if others:
        doc.warn(f"The archive holds {len(others) + 1} notes; only {prefix.rstrip('/') or 'the top-level one'} was read")
    members = {n[len(prefix):]: n for n in archive.names if n.startswith(prefix) and "/" not in n[len(prefix):]}
    note: Dict[str, Any] = {}
    manifest = None
    if MANIFEST_MEMBER in members:
        loaded = _load_json(archive.read(members[MANIFEST_MEMBER]))
        if isinstance(loaded, dict):
            manifest = loaded
        else:
            doc.warn(f"{MANIFEST_MEMBER} is not readable and was ignored")
    order = (PACKAGE_NOTE_MEMBER, NOTE_MEMBER) if manifest is not None else (NOTE_MEMBER, PACKAGE_NOTE_MEMBER)
    present = [candidate for candidate in order if candidate in members]
    for candidate in present:
        loaded = _load_json(archive.read(members[candidate]))
        if isinstance(loaded, dict):
            note = loaded
            break
    else:
        if present:
            doc.warn(f"The note description ({present[0]}) is not readable; defaults are used")
        else:
            doc.warn("The note description is missing; defaults are used")

    pages: List[Tuple[int, JsonLoader]] = []
    pdfs: Dict[int, BytesLoader] = {}
    audio = 0
    for base, full in members.items():
        m = _PAGE_NAME_RE.match(base)
        if m:
            pages.append((int(m.group(1)), (lambda n=full: _load_json(archive.read(n)))))
            continue
        m = _PDF_NAME_RE.match(base)
        if m:
            pdfs[int(m.group(1))] = (lambda n=full: archive.read(n))
            continue
        if base.lower().endswith(_AUDIO_EXTENSIONS):
            audio += 1
        elif base.lower().endswith(".cpage"):
            doc.warn(f"Page file {base} has no page number and was skipped")
    pages.sort(key=lambda item: item[0])
    folder = prefix.rstrip("/")
    if folder.lower().endswith(".cnote"):
        folder = folder[: -len(".cnote")]
    return _Source(note=note, pages=pages, pdfs=pdfs, manifest=manifest, folder=folder, audio_members=audio)


def _json_source(data: bytes, doc: Document) -> _Source:
    note = _load_json(data)
    if not isinstance(note, dict):
        raise ValueError("not a CollaNote note: not a JSON object")
    pages = note.get("pages")
    if not isinstance(pages, list) or not any(isinstance(p, dict) for p in pages):
        if isinstance(note.get("_dkDrawing"), list) or isinstance(note.get("drawing"), str):
            pages = [note]  # a single page JSON on its own
            note = {}
        else:
            raise ValueError("not a CollaNote note: the JSON holds no pages")
    doc.warn("This is an early single-file CollaNote note; that layout is read on a best-effort basis (no sample was verified)")
    return _Source(note=note, pages=[(i, (lambda p=p: p)) for i, p in enumerate(pages)], pdfs={})


def _open(data: bytes, doc: Document, depth: int = 0) -> _Source:
    if data[:2] == b"PK":
        return _zip_source(data, doc, depth)
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head[:1] == b"{":
        return _json_source(data, doc)
    raise ValueError("not a CollaNote note: neither a ZIP archive nor a JSON note")


# --------------------------------------------------------------------------- small parsers


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except (OverflowError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _index(value: Any) -> Optional[int]:
    """A non-negative integer index (JSON numbers like ``2.0`` included)."""
    number = _number(value)
    if number is None or number < 0 or number != int(number) or number > 1e9:
        return None
    return int(number)


def _pair(value: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    a, b = _number(value[0]), _number(value[1])
    if a is None or b is None:
        return None
    return a, b


def _b64(value: Any) -> Optional[bytes]:
    """base64 text -> bytes (``""`` -> ``b""``); ``None`` for anything else.

    Raises :class:`_TooLarge` above :data:`MAX_BLOB_BYTES`.
    """
    if isinstance(value, list) and len(value) == 1:  # some fields wrap the string in a list
        value = value[0]
    if not isinstance(value, str):
        return None
    if not value:
        return b""
    if len(value) // 4 * 3 > MAX_BLOB_BYTES:
        raise _TooLarge()
    try:
        return base64.b64decode(value)
    except (binascii.Error, ValueError):
        pass
    compact = "".join(value.split())  # wrapped and / or unpadded base64
    try:
        return base64.b64decode(compact + "=" * (-len(compact) % 4))
    except (binascii.Error, ValueError):
        return None


class _TooLarge(ValueError):
    pass


def _color(raw: Optional[bytes]) -> Tuple[float, float, float, float]:
    if raw is None:
        return (0.0, 0.0, 0.0, 1.0)  # no colour message at all: default black
    fields = protobuf.decode_message(raw, max_fields=64)
    comps = []
    for number in (1, 2, 3, 4):
        f = protobuf.get(fields, number)
        value = protobuf.fixed32_float(f) if f is not None and f.wire_type == protobuf.WIRE_FIXED32 else 0.0
        comps.append(min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0)
    return comps[0], comps[1], comps[2], comps[3]


def _style(raw: Optional[bytes]) -> Tuple[float, Tuple[float, float, float, float], int]:
    """``(width, rgba, inkType)`` of a Style message (zero values are omitted on disk)."""
    if raw is None:
        return 0.0, (0.0, 0.0, 0.0, 1.0), 0
    fields = protobuf.decode_message(raw, max_fields=64)
    width = 0.0
    f = protobuf.get(fields, 1)
    if f is not None and f.wire_type == protobuf.WIRE_FIXED32:
        width = protobuf.fixed32_float(f)
    color_field = protobuf.get(fields, 2)
    color = _color(color_field.value if color_field is not None and color_field.wire_type == protobuf.WIRE_LEN
                   else None)  # type: ignore[arg-type]
    ink = protobuf.get(fields, 3)
    ink_type = int(ink.value) if ink is not None and ink.wire_type == protobuf.WIRE_VARINT else 0  # type: ignore[arg-type]
    return width, color, ink_type


def _slow_point(raw: bytes) -> Tuple[float, float, float]:
    fields = protobuf.decode_message(raw, max_fields=64)
    out = []
    for number in (1, 2, 3):
        f = protobuf.get(fields, number)
        if f is None:
            out.append(0.0)
        elif f.wire_type != protobuf.WIRE_FIXED32:
            raise ValueError("point coordinate is not a float")
        else:
            out.append(protobuf.fixed32_float(f))
    return out[0], out[1], out[2]


class _PointBudget(ValueError):
    pass


def _stroke_record(buf: bytes, budget: List[int]) -> Tuple[List[Tuple[float, float, float]], Optional[bytes]]:
    """Points ``(x, y, width)`` and the Style payload of one Stroke message (fast path)."""
    points: List[Tuple[float, float, float]] = []
    style: Optional[bytes] = None
    read_varint = protobuf.read_varint
    unpack = _XYW.unpack_from
    pos = 0
    n = len(buf)
    while pos < n:
        key = buf[pos]
        if key < 0x80:
            pos += 1
        else:
            key, pos = read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError("field number 0")
        if wire == 2:
            if pos >= n:
                raise ValueError("truncated length")
            length = buf[pos]
            if length < 0x80:
                pos += 1
            else:
                length, pos = read_varint(buf, pos)
            end = pos + length
            if end > n:
                raise ValueError("field runs past the end of the stroke")
            if number == 2:
                if length >= 15 and buf[pos] == 0x0D and buf[pos + 5] == 0x15 and buf[pos + 10] == 0x1D:
                    points.append(unpack(buf, pos))
                else:
                    points.append(_slow_point(buf[pos:end]))
                if len(points) > budget[0]:
                    raise _PointBudget()
            elif number == 3:
                style = buf[pos:end]
            pos = end
        elif wire == 0:
            _, pos = read_varint(buf, pos)
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
    if pos != n:
        raise ValueError("truncated stroke")
    budget[0] -= len(points)
    return points, style


def _drawing_records(buf: bytes) -> Tuple[List[bytes], Optional[str]]:
    """The Stroke payloads (field 1) of a Drawing message, and why reading stopped early."""
    records: List[bytes] = []
    read_varint = protobuf.read_varint
    pos = 0
    n = len(buf)
    try:
        while pos < n:
            key, pos = read_varint(buf, pos)
            number, wire = key >> 3, key & 7
            if number == 0:
                return records, "damaged"
            if wire == 2:
                length, pos = read_varint(buf, pos)
                end = pos + length
                if end > n:
                    return records, "damaged"
                if number == 1:
                    if len(records) >= MAX_STROKES_PER_PAGE:
                        return records, "limit"
                    records.append(buf[pos:end])
                pos = end
            elif wire == 0:
                _, pos = read_varint(buf, pos)
            elif wire == 5:
                pos += 4
            elif wire == 1:
                pos += 8
            else:
                return records, "damaged"
    except ValueError:
        return records, "damaged"
    return records, ("damaged" if pos > n else None)


# --------------------------------------------------------------------------- attributed text


def _utf16_slice(text: str, start: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[2 * start: 2 * (start + length)].decode("utf-16-le", "replace")


def _read_varints(data: bytes) -> List[int]:
    out: List[int] = []
    pos = 0
    while pos < len(data) and len(out) < 100_000:
        value, pos = protobuf.read_varint(data, pos)
        out.append(value)
    return out


def _font_style(name: Optional[str], traits: int) -> Tuple[bool, bool]:
    lowered = (name or "").lower()
    bold = bool(traits & 2) or any(w in lowered for w in ("bold", "heavy", "black", "semibold"))
    italic = bool(traits & 1) or "italic" in lowered or "oblique" in lowered
    return bold, italic


def _attributed_text(data: bytes) -> Optional[Tuple[str, List[TextRun]]]:
    """Plain text and runs of an NSKeyedArchiver ``NSAttributedString`` (sizes in canvas units)."""
    from ..notability.keyedarchive import color_from_uicolor, load_archive

    archive = load_archive(data)
    root = archive.root
    text = archive.string(archive.get(root, "NSString"))
    if text is None:
        return None
    attributes = archive.get(root, "NSAttributes")
    info = archive.data(archive.get(root, "NSAttributeInfo"))
    spans: List[Tuple[int, int, Any]] = []  # (utf-16 start, length, attribute dictionary)
    total = len(text.encode("utf-16-le")) // 2
    is_array = isinstance(attributes, dict) and "NS.objects" in attributes and "NS.keys" not in attributes
    table = archive.array(attributes) if info and is_array else []
    if table:
        try:
            values = _read_varints(info)
        except ValueError:
            values = []
        start = 0
        for length, index in zip(values[0::2], values[1::2]):
            if not 0 <= index < len(table):
                spans = []
                break
            spans.append((start, length, table[index]))
            start += length
        if start != total:  # the runs must cover the text exactly; else one run with the first style
            spans = []
        if not spans:
            spans = [(0, total, table[0])]
    if not spans:
        spans = [(0, total, attributes)]

    runs: List[TextRun] = []
    for start, length, attrs in spans:
        entries = archive.dictionary(attrs)
        font = archive.deref(entries.get("NSFont"))
        name = archive.string(archive.get(font, "NSName")) or archive.string(archive.get(font, "UIFontName"))
        size = archive.number(archive.get(font, "NSSize"), 0.0) or archive.number(archive.get(font, "UIFontPointSize"), 0.0)
        bold, italic = _font_style(name, archive.integer(archive.get(font, "UIFontTraits"), 0))
        color = color_from_uicolor(archive, entries.get("NSColor"))
        underline = archive.integer(entries.get("NSUnderline"), 0) != 0
        piece = _utf16_slice(text, start, length) if len(spans) > 1 else text
        runs.append(TextRun(piece, bold=bold, italic=italic, underline=underline, font=name,
                            size=size if size > 0 and math.isfinite(size) else None,
                            color=tuple(min(1.0, max(0.0, c)) for c in color) if color else None))  # type: ignore[arg-type]
    return text, runs


# --------------------------------------------------------------------------- the reader


@dataclass
class _PageInfo:
    page: Page
    scale: Optional[float]  # pt per canvas unit of a PDF-backed page; None for a blank page


@dataclass
class _Counts:
    unknown_inks: Dict[int, int] = field(default_factory=dict)
    damaged_strokes: int = 0
    damaged_layers: List[int] = field(default_factory=list)
    count_mismatch: List[int] = field(default_factory=list)
    legacy_pages: List[int] = field(default_factory=list)
    legacy_shifted: int = 0
    rotated_images: int = 0
    texts: int = 0
    skipped_images: int = 0
    unknown_attachments: Dict[str, int] = field(default_factory=dict)
    ratio_pages: List[int] = field(default_factory=list)


def _pages_list(numbers: Sequence[int]) -> str:
    shown = ", ".join(str(n) for n in numbers[:8])
    return shown + (f" and {len(numbers) - 8} more" if len(numbers) > 8 else "")


class _Reader:
    def __init__(self, data: bytes):
        self.doc = Document(source_format="collanote")
        self.source = _open(data, self.doc)
        self.counts = _Counts()
        self._pdf_info: Dict[int, Optional[List[Tuple[float, float]]]] = {}
        self._pdf_bytes: Dict[int, bytes] = {}

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    # -- note level ---------------------------------------------------------------------

    def canvas(self) -> Tuple[float, float]:
        size = _pair(self.source.note.get("size"))
        if size is not None and 1.0 <= size[0] <= MAX_CANVAS and 1.0 <= size[1] <= MAX_CANVAS:
            return size
        if self.source.note:
            self.warn("The note's page size is missing or unusable; A4 pages were assumed")
        return DEFAULT_CANVAS

    def paper(self) -> str:
        name = self.source.note.get("paperOrTemplate")
        lowered = name.lower() if isinstance(name, str) else ""
        if "line" in lowered:
            return "lined"
        if "grid" in lowered or "square" in lowered:
            return "grid"
        if "dot" in lowered:
            return "dotted"
        return "plain"

    def check_manifest(self, pages_found: int) -> None:
        manifest = self.source.manifest
        if manifest is None:
            return
        fmt = _index(manifest.get("format"))
        min_reader = _index(manifest.get("minReader"))
        if (fmt is not None and fmt > KNOWN_FORMAT) or (min_reader is not None and min_reader > KNOWN_FORMAT):
            self.warn(f"The note was saved in CollaNote package format {fmt}, newer than the format "
                      f"{KNOWN_FORMAT} this reader knows; some content may be missing")
        declared = _index(manifest.get("pageCount"))
        if declared is not None and declared != pages_found:
            self.warn(f"The note's manifest lists {declared} pages but {pages_found} page files were found")

    def report_note_extras(self) -> None:
        note = self.source.note
        audios = note.get("audios")
        audio = len(audios) if isinstance(audios, list) else 0
        if self.source.manifest is not None:
            audio = max(audio, _index(self.source.manifest.get("audioCount")) or 0)
        audio = max(audio, self.source.audio_members)
        if audio:
            self.warn(f"{audio} audio recording(s) are not converted")
        bookmarks = note.get("bookmarks")
        if isinstance(bookmarks, list) and bookmarks:
            self.warn(f"{len(bookmarks)} bookmark(s) are not converted")

    def title(self) -> str:
        """The note's name (NFC: CollaNote stores some names decomposed), else the package folder."""
        name = self.source.note.get("name")
        if not (isinstance(name, str) and name.strip()):
            name = self.source.folder or "Untitled"
        return unicodedata.normalize("NFC", name.strip())

    # -- PDFs ---------------------------------------------------------------------------

    def pdf_pages(self, index: int) -> Optional[List[Tuple[float, float]]]:
        """Page sizes (pt, display orientation) of ``<index>.pdf``; ``None`` (warned once) when
        the PDF is missing or unreadable."""
        if index in self._pdf_info:
            return self._pdf_info[index]
        sizes: Optional[List[Tuple[float, float]]] = None
        data = self.pdf_data(index)
        if data is None:
            self.warn(f"PDF {index}.pdf is missing from the note; the pages showing it were read as blank pages")
        else:
            try:
                from ..pdfutil import pdf_info

                sizes = [(float(p.width), float(p.height)) for p in pdf_info(data).pages]
            except Exception:  # noqa: BLE001 - an unreadable PDF only loses its pages' backgrounds
                sizes = None
            if not sizes:
                self.warn(f"PDF {index}.pdf has no readable page; the pages showing it were read as blank pages")
                sizes = None
            else:
                self._pdf_bytes[index] = data
        self._pdf_info[index] = sizes
        return sizes

    def pdf_data(self, index: int) -> Optional[bytes]:
        loader = self.source.pdfs.get(index)
        if loader is not None:
            return loader()
        inline = self.source.note.get("importedPdfDatas")
        if isinstance(inline, list) and index < len(inline):
            entry = inline[index]
            candidates = [entry] if isinstance(entry, str) else (
                [v for v in entry.values() if isinstance(v, str)] if isinstance(entry, dict) else [])
            for candidate in candidates:
                try:
                    data = _b64(candidate)
                except _TooLarge:
                    self.warn(f"Embedded PDF {index} is larger than {MAX_BLOB_BYTES // (1024 * 1024)} MB and was skipped")
                    return None
                if data is not None and data.lstrip()[:5] == b"%PDF-":
                    return data
        return None

    # -- pages --------------------------------------------------------------------------

    def read(self) -> Document:
        doc = self.doc
        doc.title = self.title()
        width, height = self.canvas()
        paper = self.paper()
        entries = self.source.pages
        self.check_manifest(len(entries))
        self.report_note_extras()
        if len(entries) > MAX_PAGES:
            self.warn(f"The note has {len(entries)} pages; only the first {MAX_PAGES} were read")
            entries = entries[:MAX_PAGES]
        infos: List[_PageInfo] = []
        for number, (_index_on_disk, loader) in enumerate(entries, start=1):
            data = loader()
            if not isinstance(data, dict):
                self.warn(f"Page {number} is unreadable and was replaced by a blank page")
                data = {}
            try:
                info = self.page(number, data, width, height, paper)
            except Exception as exc:  # noqa: BLE001 - one damaged page must not lose the note
                self.warn(f"Page {number} could not be read ({exc.__class__.__name__}) and was replaced by a blank page")
                info = _PageInfo(Page(width=width, height=height, paper=paper), None)
            infos.append(info)
        self.finish_blank_pages(infos)
        doc.pages = [info.page for info in infos]
        used = {p.background.pdf_id for p in doc.pages if p.background is not None}
        for index, data in sorted(self._pdf_bytes.items()):
            if str(index) in used:
                doc.pdfs[str(index)] = data
        unused = sorted(i for i in self.source.pdfs if i not in self._pdf_info)
        if unused:
            self.warn(f"Imported PDF(s) {', '.join(f'{i}.pdf' for i in unused)} are not shown on any page and were dropped")
        self.report()
        if not doc.pages:
            self.warn("The note has no pages")
        return doc

    def page(self, number: int, data: Dict[str, Any], width: float, height: float, paper: str) -> _PageInfo:
        page: Optional[Page] = None
        scale: Optional[float] = None
        pointer = data.get("pdfPointer")
        if pointer is not None:
            resolved = self.pdf_page(number, pointer)
            if resolved is not None:
                pdf_index, page_index, pw, ph = resolved
                scale = pw / width
                page = Page(width=pw, height=ph, background=PdfBackground(str(pdf_index), page_index))
                if abs(ph / pw - height / width) > 0.01 * (height / width):
                    self.counts.ratio_pages.append(number)
        if page is None:
            page = Page(width=width, height=height, paper=paper)  # canvas units; scaled in finish_blank_pages
        s = scale if scale is not None else 1.0
        self.legacy_ink(number, data, page, s, height)
        self.ink(number, data, page, s)
        self.attachments(number, data, page, s, width, height)
        return _PageInfo(page, scale)

    def pdf_page(self, number: int, pointer: Any) -> Optional[Tuple[int, int, float, float]]:
        if not isinstance(pointer, dict):
            self.warn(f"Page {number} has an unreadable PDF reference; it was read as a blank page")
            return None
        pdf_index = _index(pointer.get("pdfIndex"))
        page_index = _index(pointer.get("pageIndex"))
        if pdf_index is None or page_index is None:
            self.warn(f"Page {number} has an unreadable PDF reference; it was read as a blank page")
            return None
        sizes = self.pdf_pages(pdf_index)
        if sizes is None:
            return None
        if page_index >= len(sizes):
            self.warn(f"Page {number} shows page {page_index + 1} of {pdf_index}.pdf, which has only "
                      f"{len(sizes)} pages; it was read as a blank page")
            return None
        pw, ph = sizes[page_index]
        if not (pw > 0 and ph > 0):
            return None
        return pdf_index, page_index, pw, ph

    def finish_blank_pages(self, infos: List[_PageInfo]) -> None:
        """Scale blank pages (built in canvas units) like their nearest PDF-backed neighbour."""
        scales = [info.scale for info in infos]
        for i, info in enumerate(infos):
            if info.scale is not None:
                continue
            before = next((scales[j] for j in range(i - 1, -1, -1) if scales[j] is not None), None)
            after = next((scales[j] for j in range(i + 1, len(scales)) if scales[j] is not None), None)
            s = before if before is not None else (after if after is not None else A4_PT_PER_UNIT)
            _scale_page(info.page, s)

    # -- ink ----------------------------------------------------------------------------

    def ink(self, number: int, data: Dict[str, Any], page: Page, s: float) -> None:
        layers = data.get("_dkDrawing")
        if layers is None:
            return
        if isinstance(layers, str):
            layers = [layers]
        if not isinstance(layers, list):
            self.counts.damaged_layers.append(number)
            return
        budget = [MAX_POINTS_PER_PAGE]
        records_total = 0
        for layer in layers:
            try:
                blob = _b64(layer)
            except _TooLarge:
                self.warn(f"Page {number}: an ink layer larger than {MAX_BLOB_BYTES // (1024 * 1024)} MB was skipped")
                continue
            if blob is None:
                self.counts.damaged_layers.append(number)
                continue
            if not blob:
                continue  # "" is an empty drawing (pages without ink)
            records, stopped = _drawing_records(blob)
            records_total += len(records)
            if stopped == "damaged":
                self.counts.damaged_layers.append(number)
            elif stopped == "limit":
                self.warn(f"Page {number}: ink beyond {MAX_STROKES_PER_PAGE} strokes was skipped")
            for record in records:
                try:
                    stroke = self.stroke(record, s, budget)
                except _PointBudget:
                    self.warn(f"Page {number}: ink beyond {MAX_POINTS_PER_PAGE} points was skipped")
                    return
                except Exception:  # noqa: BLE001 - one damaged stroke must not lose the page
                    self.counts.damaged_strokes += 1
                    continue
                if stroke is not None:
                    page.strokes.append(stroke)
        declared = data.get("strokeCountBeforeSaving")
        if isinstance(declared, int) and not isinstance(declared, bool) and declared != records_total:
            self.counts.count_mismatch.append(number)

    def stroke(self, record: bytes, s: float, budget: List[int]) -> Optional[Stroke]:
        raw_points, style_raw = _stroke_record(record, budget)
        width, color, ink_type = _style(style_raw)
        if not raw_points:
            return None
        if not (0.0 < width < MAX_WIDTH):
            positive = [w for _, _, w in raw_points if 0.0 < w < MAX_WIDTH]
            width = float(statistics.median(positive)) if positive else 1.0
        points: List[Point] = []
        for x, y, w in raw_points:
            if x - x == 0.0 and y - y == 0.0:  # finite
                points.append(Point(x * s, y * s, (w if 0.0 < w < MAX_WIDTH else width) * s))
        if not points:
            return None
        if ink_type == INK_HIGHLIGHTER:
            kind = "highlighter"
        else:
            kind = "pen"
            if ink_type not in _KNOWN_INKS:
                self.counts.unknown_inks[ink_type] = self.counts.unknown_inks.get(ink_type, 0) + 1
        return Stroke(points=points, color=color, kind=kind, width=width * s)

    def legacy_ink(self, number: int, data: Dict[str, Any], page: Page, s: float, height: float) -> None:
        encoded = data.get("drawing")
        if not isinstance(encoded, str) or not encoded:
            return
        try:
            blob = _b64(encoded)
        except _TooLarge:
            self.warn(f"Page {number}: a legacy ink layer larger than {MAX_BLOB_BYTES // (1024 * 1024)} MB was skipped")
            return
        if blob is None or not pencilkit.is_pkdrawing(blob):
            return  # PencilKit itself reads such data as an empty drawing
        try:
            drawing = pencilkit.parse_pkdrawing(blob)
        except ValueError:
            self.warn(f"Page {number}: the legacy PencilKit ink layer is damaged and was skipped")
            return
        self.counts.damaged_strokes += drawing.damaged
        if not drawing.strokes:
            return
        dy = 0.0
        index = number - 1
        if index > 0 and height > 0:
            ys = [p.y for st in drawing.strokes for p in st.points if math.isfinite(p.y)]
            offset = index * height
            if ys and max(ys) > 1.25 * height and min(ys) >= offset - 0.25 * height:
                dy = -offset
                self.counts.legacy_shifted += 1
        strokes = pencilkit.to_model_strokes(drawing.strokes, scale=s, dy=dy)
        if strokes:
            page.strokes.extend(strokes)
            self.counts.legacy_pages.append(number)

    # -- attachments ----------------------------------------------------------------------

    def attachments(self, number: int, data: Dict[str, Any], page: Page, s: float, width: float,
                    height: float) -> None:
        items = data.get("attachments")
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            try:
                if kind == "image" or (kind != "text" and item.get("imageInData")):
                    self.image(number, item, page, s, width, height)
                elif kind == "text":
                    self.text(number, item, page, s, width, height)
                else:
                    label = str(kind) if isinstance(kind, (str, int, float)) else "unknown"
                    self.counts.unknown_attachments[label] = self.counts.unknown_attachments.get(label, 0) + 1
            except _TooLarge:
                self.warn(f"Page {number}: an attachment larger than {MAX_BLOB_BYTES // (1024 * 1024)} MB was skipped")
            except Exception as exc:  # noqa: BLE001 - one damaged attachment must not lose the page
                self.warn(f"Page {number}: an unreadable attachment was skipped ({exc.__class__.__name__})")

    def frame(self, item: Dict[str, Any], width: float, height: float) -> Optional[Tuple[float, float, float, float, float]]:
        """``(x, y, w, h, rotation)`` in canvas units (top-left corner of the unrotated box)."""
        center = _pair(item.get("center"))
        bound = item.get("bound")
        size = None
        if isinstance(bound, list) and len(bound) == 2 and isinstance(bound[1], list):
            size = _pair(bound[1])
        elif isinstance(bound, list):
            size = _pair(bound)
        if center is None or size is None:
            return None
        w, h = size[0] * width, size[1] * height
        if not (w > 0 and h > 0):
            return None
        rotation = _number(item.get("rotatedDegree")) or 0.0
        return center[0] * width - w / 2, center[1] * height - h / 2, w, h, rotation

    def image(self, number: int, item: Dict[str, Any], page: Page, s: float, width: float, height: float) -> None:
        data = _b64(item.get("imageInData"))
        if not data:
            self.counts.skipped_images += 1
            return
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            fmt = "png"
        elif data[:3] == b"\xff\xd8\xff":
            fmt = "jpeg"
        elif data.lstrip()[:5] == b"%PDF-":
            fmt = "pdf"
        else:
            self.counts.skipped_images += 1
            return
        frame = self.frame(item, width, height)
        if frame is None:
            self.counts.skipped_images += 1
            return
        x, y, w, h, rotation = frame
        if rotation:
            self.counts.rotated_images += 1
        page.images.append(Image(x=x * s, y=y * s, w=w * s, h=h * s, data=data, fmt=fmt, rotation=rotation))

    def text(self, number: int, item: Dict[str, Any], page: Page, s: float, width: float, height: float) -> None:
        frame = self.frame(item, width, height)
        raw = _b64(item.get("attStringData"))
        parsed = None
        if raw is not None:
            try:
                parsed = _attributed_text(raw)
            except Exception:  # noqa: BLE001 - plistlib raises several types on damaged archives
                parsed = None
        if frame is None or parsed is None or not parsed[0].strip() or parsed[0] == _PLACEHOLDER_TEXT:
            self.counts.unknown_attachments["text"] = self.counts.unknown_attachments.get("text", 0) + 1
            return
        text, runs = parsed
        x, y, w, h, rotation = frame
        for run in runs:
            if run.size is not None:
                run.size *= s
        trimmed = text.rstrip("\n")
        if runs and trimmed != text:  # the trailing newline of the text view is not content
            last = runs[-1]
            last.text = last.text.rstrip("\n")
            if not last.text and len(runs) > 1:
                runs.pop()
        first = runs[0] if runs else None
        size = first.size if first is not None and first.size else DEFAULT_TEXT_SIZE * s
        color = first.color if first is not None and first.color else (0.0, 0.0, 0.0, 1.0)
        # CollaNote turns the box about its centre; the model turns it about its top-left corner.
        ox, oy = x, y
        if rotation:
            theta = math.radians(rotation)
            cx, cy = x + w / 2, y + h / 2
            ox = cx - (w / 2) * math.cos(theta) + (h / 2) * math.sin(theta)
            oy = cy - (w / 2) * math.sin(theta) - (h / 2) * math.cos(theta)
        page.texts.append(TextBox(x=ox * s, y=oy * s, w=w * s, h=h * s, text=trimmed, runs=runs,
                                  color=color, size=size, rotation=rotation))
        self.counts.texts += 1

    # -- warnings -------------------------------------------------------------------------

    def report(self) -> None:
        c = self.counts
        if c.unknown_inks:
            codes = ", ".join(str(k) for k in sorted(c.unknown_inks))
            total = sum(c.unknown_inks.values())
            self.warn(f"{total} stroke(s) use CollaNote pen types this reader does not know (inkType {codes}); "
                      "they were read as plain pen strokes")
        if c.damaged_strokes:
            self.warn(f"{c.damaged_strokes} damaged stroke(s) were skipped")
        if c.damaged_layers:
            self.warn(f"The ink data of page(s) {_pages_list(sorted(set(c.damaged_layers)))} is damaged; "
                      "what could be read was kept")
        if c.count_mismatch:
            self.warn(f"Page(s) {_pages_list(c.count_mismatch)} record a different stroke count than their "
                      "ink data holds; the page may be incomplete")
        if c.legacy_pages:
            note = " (moved up from one continuous canvas)" if c.legacy_shifted else ""
            self.warn(f"Page(s) {_pages_list(c.legacy_pages)} carry CollaNote 1.x PencilKit ink{note}; "
                      "its placement is unverified")
        if c.ratio_pages:
            self.warn(f"The PDF pages of page(s) {_pages_list(c.ratio_pages)} have other proportions than the "
                      "note's canvas; the ink placement on them is unverified")
        if c.rotated_images:
            self.warn(f"{c.rotated_images} rotated image(s): the direction of CollaNote's image rotation is "
                      "unverified")
        if c.skipped_images:
            self.warn(f"{c.skipped_images} image(s) without readable PNG/JPEG data or position were skipped")
        if c.texts:
            self.warn(f"{c.texts} text box(es) were converted; their font scale and box layout are inferred "
                      "from a single sample")
        for kind, count in sorted(c.unknown_attachments.items()):
            what = "unreadable text box(es)" if kind == "text" else f"attachment(s) of the unknown kind {kind!r}"
            self.warn(f"{count} {what} were skipped")


def _scale_page(page: Page, s: float) -> None:
    """Scale a page built in canvas units to pt."""
    page.width *= s
    page.height *= s
    for stroke in page.strokes:
        stroke.width *= s
        for p in stroke.points:
            p.x *= s
            p.y *= s
            p.width *= s
    for image in page.images:
        image.x *= s
        image.y *= s
        image.w *= s
        image.h *= s
    for box in page.texts:
        box.x *= s
        box.y *= s
        box.w *= s
        box.h *= s
        box.size *= s
        for run in box.runs:
            if run.size is not None:
                run.size *= s


def read_cnote(data: bytes) -> Document:
    """Parse a CollaNote ``.cnote`` note (format 1 ZIP, zipped format 2 package or bare JSON).

    Raises :class:`ValueError` only when ``data`` is not a CollaNote note at all; damaged or
    unknown content is skipped with a line on ``Document.warnings``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_cnote expects bytes")
    try:
        return _Reader(bytes(data)).read()
    except ValueError:
        raise
    except MemoryError:
        raise ValueError("not a readable CollaNote note: it does not fit in memory") from None
    except Exception as exc:  # noqa: BLE001 - the reader contract: nothing but ValueError escapes
        raise ValueError(f"not a readable CollaNote note ({exc.__class__.__name__}: {exc})") from None
