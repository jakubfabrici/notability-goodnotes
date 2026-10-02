"""GoodNotes ``.goodnotes`` -> :class:`gnnote.model.Document` (tolerant reader).

Byte layout implemented here (``docs/goodnotes-container.md``, ``goodnotes-stroke.md``,
``goodnotes-elements.md`` and their critic additions):

Container
    A plain ZIP.  ``index.notes.pb``, ``index.attachments.pb`` and ``index.events.pb`` are
    varint-length-prefixed protobuf record streams (``gnnote.protobuf.decode_records``).
    ``index.notes.pb`` lists one ``{#1 N, #2 "notes/" + N}`` record per page (``N`` = notes
    layer UUID), ``index.attachments.pb`` one ``{#1 A, #2 "attachments/" + A}`` per attachment
    (PDF paper templates / imported PDFs, PNG / JPEG rasters, M4A audio; no type stored).

Event log (``index.events.pb``)
    Every record is ``{#1 entity UUID, #E {body}}`` where the field number ``E`` is the event
    type.  Used here: ``#30`` document created (``#2.#1`` title), ``#31`` renamed, ``#2``
    template (``#2`` T, ``#4`` attachment A, ``#5`` 1-based PDF page, ``#7``/``#18`` ruled-paper
    hints, ``#8 {#1 f32 canvas W, #2 f32 canvas H}``, ``#9`` catalogue name), ``#54`` page created
    (``#2`` page UUID P, ``#3.#1`` template T, ``#4.#1`` ASCII order key), ``#55`` page reordered
    (``#2`` P, ``#3.#1`` new key), ``#56`` page deleted (``#2`` P), ``#6`` attachment added
    (``#5`` byte size).  Page -> paper binding: ``N`` equals ``P`` except for the last hex digit
    (``P`` + 1), so both are matched on their first 32 hex characters.

Page geometry
    Page size = the bound PDF page's MediaBox (``gnnote.pdfutil.pdf_info``, rotation applied);
    canvas size = ``#2.#8`` (= MediaBox x 132/72 in app-written files); all element geometry is
    in canvas units, origin top-left, y down, ``pt = canvas * page_width_pt / canvas_width``.

Page content (``notes/<N>``)
    A record stream of (metadata, content) pairs.  Metadata: ``{#1 element UUID, #2 clock,
    [#3 1 = tombstone], [#4 attachment UUID (images)], #8 device, #9 counter, #14 5381, #16
    schema}``.  The content record has exactly one top-level field whose number is the kind:

    * ``#7`` ink stroke ``{#1 UUID, #2 Apple-LZ4 frame -> TPL image (gnnote.applelz4 / gnnote.tpl),
      [#3 tool: absent flat/ball pen, 1 or 4 ribbon, 5 pencil], #4 {#1..#4 f32 RGBA, 0.0
      omitted}, [#5 1 highlighter], #6 "" | {#1 f32 dx, #2 f32 dy} lasso offset added to every
      point, [#9 auto-shape geometry: #1 line / #2 polyline / #3 rect / #4 ellipse, #15 f32 W],
      [#20 {#1 ""} marker], #21 schema}``.  Flat format: quadratic Bezier segments, turned into
      the exact cubic chain ``c1 = P0 + 2/3 (C - P0)``, ``c2 = P1 + 2/3 (C - P1)``; width ``W / 2``
      pt.  Ribbon: per-point radius ``r`` -> width ``2 r`` canvas units.  Pencil: width
      ``W / 2`` pt too (see ``PENCIL_WIDTH_FACTOR``).  Empty geometry (tombstones, shapes) is skipped.
    * ``#1`` image ``{#1 UUID, #2 rect(top-left, size), #3 rect(centre, size) crop [+ #3.#3
      rotation rad], #4 attachment UUID}``; raster bytes from ``attachments/<A>``.
    * ``#8`` text box ``{#1 UUID, #2 outer rect, #3 text frame (= #2 inset by #10), #6 RTF
      (gnnote.rtf.parse_rtf; ``\\fsN`` half-points in canvas units), #10 f32 padding}``.
    * anything else (``#20`` sticky note, ``#21``/``#22`` newer text and shape records, ...)
      -> one warning per kind, skipped.
"""
from __future__ import annotations

import io
import math
import re
import statistics
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .. import applelz4, protobuf, tpl
from ..model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from ..pdfutil import pdf_info
from ..protobuf import Field
from ..rtf import parse_rtf

__all__ = ["read_goodnotes", "CANVAS_PER_POINT", "DEFAULT_PAGE_SIZE", "BUILTIN_TEMPLATE_RE"]

CANVAS_PER_POINT = 132.0 / 72.0  # GoodNotes canvas units per PDF point
DEFAULT_PAGE_SIZE = (455.04, 588.45)  # the GoodNotes "standard" paper, used when nothing else is known
# Pencil (tool 25) width in pt per TPL width word W.  goodnotes-stroke.md guessed 2.5 from a
# 3.8976-pt stroked path in Test5.pdf page 2, but that path is the W = 7.795 ball-pen stroke of
# the same page (7.795 / 2 = 3.8976); the two pencil strokes there are exported as rasters whose
# bounding boxes pad the centre line by 0.5-0.8 pt for W = 1.559, i.e. W / 2 like every other
# non-ribbon format.  Medium confidence.
PENCIL_WIDTH_FACTOR = 0.5
ELLIPSE_SAMPLES = 64
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
BUILTIN_TEMPLATE_RE = re.compile(
    r"^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}_[a-z0-9]+_\d+_\d+ - .+$"
)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.S)
_RECT_PATH_RE = re.compile(
    rb"([-\d.]+) ([-\d.]+) m\s+([-\d.]+) ([-\d.]+) l\s+([-\d.]+) ([-\d.]+) l\s+([-\d.]+) ([-\d.]+) l\s+h\s+f"
)

XY = Tuple[float, float]


# --------------------------------------------------------------------------- protobuf helpers


def _decode(data: bytes) -> Optional[List[Field]]:
    return protobuf.try_decode_message(data)


def _msg(fields: Sequence[Field], number: int) -> Optional[List[Field]]:
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_LEN:
        return None
    return _decode(bytes(f.value))


def _str(fields: Sequence[Field], number: int) -> Optional[str]:
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_LEN:
        return None
    try:
        return bytes(f.value).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _uuid(fields: Sequence[Field], number: int) -> Optional[str]:
    s = _str(fields, number)
    if s is not None and UUID_RE.match(s):
        return s.upper()
    return None


def _int(fields: Sequence[Field], number: int) -> Optional[int]:
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_VARINT:
        return None
    return int(f.value)


def _f32(fields: Sequence[Field], number: int, default: float = 0.0) -> float:
    f = protobuf.get(fields, number)
    if f is None or f.wire_type != protobuf.WIRE_FIXED32:
        return default
    v = protobuf.fixed32_float(f)
    return v if math.isfinite(v) else default


def _point(fields: Optional[Sequence[Field]]) -> Optional[XY]:
    if fields is None:
        return None
    if protobuf.get(fields, 1) is None and protobuf.get(fields, 2) is None:
        return None
    return (_f32(fields, 1), _f32(fields, 2))


def _rect(fields: Optional[Sequence[Field]]) -> Optional[Tuple[float, float, float, float]]:
    """``{#1 {x, y}, #2 {w, h}}`` -> (x, y, w, h)."""
    if fields is None:
        return None
    origin = _point(_msg(fields, 1))
    size = _point(_msg(fields, 2))
    if origin is None or size is None:
        return None
    return (origin[0], origin[1], size[0], size[1])


def _uuid_key(u: str) -> str:
    """Page UUID ``P`` and notes-layer UUID ``N`` differ only in the last hex digit."""
    return u.upper()[:35]


# --------------------------------------------------------------------------- event log model


@dataclass
class _Template:
    uuid: str
    attachment: Optional[str] = None
    pdf_page: int = 1  # 1-based
    canvas: Optional[XY] = None
    name: str = ""
    lined: bool = False


@dataclass
class _PageEvent:
    uuid: str
    template: Optional[str] = None
    order_key: Optional[str] = None


@dataclass
class _Events:
    title: Optional[str] = None
    templates: Dict[str, _Template] = field(default_factory=dict)
    pages: Dict[str, _PageEvent] = field(default_factory=dict)  # keyed by _uuid_key(P)
    deleted: set = field(default_factory=set)  # _uuid_key(P)
    attachment_sizes: Dict[str, int] = field(default_factory=dict)


def _parse_events(data: bytes, doc: Document) -> _Events:
    ev = _Events()
    try:
        records = protobuf.decode_records(data)
    except ValueError:
        doc.warn("index.events.pb is truncated; page order and paper bindings may be incomplete")
        records = _salvage_records(data)
    for rec in records:
        fields = _decode(rec)
        if not fields:
            continue
        for f in fields:
            if f.number == 1 or f.wire_type != protobuf.WIRE_LEN:
                continue
            body = _decode(bytes(f.value))
            if body is None:
                continue
            kind = f.number
            if kind in (30, 31):
                name = _msg(body, 2)
                title = _str(name, 1) if name else None
                if title is not None:
                    ev.title = title
            elif kind == 2:
                t = _uuid(body, 2)
                if t is None:
                    continue
                tmpl = _Template(uuid=t, attachment=_uuid(body, 4), pdf_page=_int(body, 5) or 1,
                                 name=_str(body, 9) or "")
                size = _msg(body, 8)
                if size is not None:
                    w, h = _f32(size, 1), _f32(size, 2)
                    if w > 0 and h > 0:
                        tmpl.canvas = (w, h)
                tmpl.lined = protobuf.get(body, 7) is not None or protobuf.get(body, 18) is not None
                ev.templates[t] = tmpl
            elif kind == 54:
                p = _uuid(body, 2)
                if p is None:
                    continue
                page = ev.pages.setdefault(_uuid_key(p), _PageEvent(uuid=p))
                tref = _msg(body, 3)
                if tref is not None:
                    page.template = _uuid(tref, 1) or page.template
                key = _msg(body, 4)
                if key is not None and _str(key, 1) is not None:
                    page.order_key = _str(key, 1)
            elif kind == 55:
                p = _uuid(body, 2)
                key = _msg(body, 3)
                if p is not None and key is not None and _str(key, 1) is not None:
                    ev.pages.setdefault(_uuid_key(p), _PageEvent(uuid=p)).order_key = _str(key, 1)
            elif kind == 56:
                p = _uuid(body, 2)
                if p is not None:
                    ev.deleted.add(_uuid_key(p))
            elif kind == 6:
                a = _uuid(body, 1)
                size = _int(body, 5)
                if a is not None and size is not None:
                    ev.attachment_sizes[a] = size
            break
    return ev


def _salvage_records(data: bytes) -> List[bytes]:
    """Read as many leading records as possible from a damaged stream."""
    out: List[bytes] = []
    pos = 0
    try:
        while pos < len(data):
            length, pos = protobuf.read_varint(data, pos)
            if pos + length > len(data):
                break
            out.append(data[pos:pos + length])
            pos += length
    except ValueError:
        pass
    return out


def _index_pairs(data: bytes, prefix: str, doc: Document, what: str) -> List[Tuple[str, str]]:
    """``index.notes.pb`` / ``index.attachments.pb`` -> [(uuid, member name)] in file order."""
    try:
        records = protobuf.decode_records(data)
    except ValueError:
        doc.warn(f"{what} is truncated; reading what could be decoded")
        records = _salvage_records(data)
    out: List[Tuple[str, str]] = []
    for rec in records:
        fields = _decode(rec)
        if not fields:
            continue
        u = _uuid(fields, 1)
        member = _str(fields, 2)
        if member is None and u is not None:
            member = prefix + u
        if u is None and member is not None and member.startswith(prefix):
            u = member[len(prefix):].upper()
        if u is not None and member is not None:
            out.append((u, member))
    return out


# --------------------------------------------------------------------------- paper classification


def _paper_style(pdf: bytes, lined_hint: bool) -> str:
    """Guess plain / lined / grid / dotted from a built-in template's content streams."""
    horizontal = vertical = curves = 0
    for m in _STREAM_RE.finditer(pdf):
        raw = m.group(1)
        try:
            text = zlib.decompressobj().decompress(raw.strip(b"\r\n"))
        except zlib.error:
            text = raw
        curves += len(re.findall(rb"\bc\b", text))
        for rm in _RECT_PATH_RE.finditer(text):
            try:
                xs = [float(rm.group(i)) for i in (1, 3, 5, 7)]
                ys = [float(rm.group(i)) for i in (2, 4, 6, 8)]
            except ValueError:
                continue
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if 0 < h <= 2.0 and w > 10 * h:
                horizontal += 1
            elif 0 < w <= 2.0 and h > 10 * w:
                vertical += 1
    if curves >= 40:
        return "dotted"
    if horizontal >= 3 and vertical >= 3:
        return "grid"
    if horizontal >= 3 or (lined_hint and horizontal == 0 and vertical == 0 and curves == 0):
        return "lined"
    return "plain"


# --------------------------------------------------------------------------- page assembly


class _Reader:
    def __init__(self, data: bytes):
        self.doc = Document(source_format="goodnotes")
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("not a .goodnotes file (not a ZIP archive)") from exc
        self.names = set(self.zip.namelist())
        self.attachments: Dict[str, str] = {}  # attachment UUID -> member name
        self._pdf_cache: Dict[str, Optional[object]] = {}
        self._att_cache: Dict[str, Optional[bytes]] = {}
        self._pencil_warned = False

    # -- members -----------------------------------------------------------------

    def member(self, name: str) -> Optional[bytes]:
        if name not in self.names:
            return None
        try:
            return self.zip.read(name)
        except (zipfile.BadZipFile, zlib.error, KeyError, RuntimeError, EOFError):
            self.doc.warn(f"ZIP member {name} is damaged and was skipped")
            return None

    def attachment(self, uuid: str) -> Optional[bytes]:
        if uuid not in self._att_cache:
            member = self.attachments.get(uuid, "attachments/" + uuid)
            self._att_cache[uuid] = self.member(member)
        return self._att_cache[uuid]

    def pdf(self, uuid: str):
        if uuid not in self._pdf_cache:
            info = None
            data = self.attachment(uuid)
            if data is not None and data.lstrip()[:5] == b"%PDF-":
                try:
                    info = pdf_info(data)
                except (ValueError, TypeError) as exc:
                    self.doc.warn(f"attachment {uuid} is not a readable PDF ({exc})")
                else:
                    for w in info.warnings:
                        self.doc.warn(f"attachment {uuid}: {w}")
            self._pdf_cache[uuid] = info
        return self._pdf_cache[uuid]

    # -- document ----------------------------------------------------------------

    def read(self) -> Document:
        doc = self.doc
        notes_index = self.member("index.notes.pb")
        attachments_index = self.member("index.attachments.pb")
        events_data = self.member("index.events.pb")
        if notes_index is None and not any(n.startswith("notes/") for n in self.names):
            raise ValueError("not a .goodnotes file (no index.notes.pb and no notes/ members)")

        if attachments_index is not None:
            for u, member in _index_pairs(attachments_index, "attachments/", doc, "index.attachments.pb"):
                self.attachments[u] = member
        for name in sorted(self.names):
            if name.startswith("attachments/"):
                self.attachments.setdefault(name[len("attachments/"):].upper(), name)

        if events_data is not None:
            events = _parse_events(events_data, doc)
        else:
            events = _Events()
            doc.warn("index.events.pb is missing; page order, paper and canvas size are guessed")
        doc.title = events.title or "Untitled"
        for a, size in events.attachment_sizes.items():
            member = self.attachments.get(a)
            if member is not None and member in self.names:
                try:
                    actual = self.zip.getinfo(member).file_size
                except KeyError:
                    continue
                if actual != size:
                    doc.warn(f"attachment {a} has {actual} bytes but its event says {size}")

        pages = self._page_list(notes_index, events)
        for n_uuid, member in pages:
            page = self._build_page(n_uuid, member, events)
            doc.pages.append(page)
        if not doc.pages:
            doc.warn("the notebook has no pages")
        return doc

    def _page_list(self, notes_index: Optional[bytes], events: _Events) -> List[Tuple[str, str]]:
        entries: List[Tuple[str, str]] = []
        if notes_index is not None:
            entries = _index_pairs(notes_index, "notes/", self.doc, "index.notes.pb")
        if not entries:
            entries = [(n[len("notes/"):].upper(), n) for n in sorted(self.names) if n.startswith("notes/")]
            if entries and notes_index is not None:
                self.doc.warn("index.notes.pb lists no pages; using the notes/ members in name order")
        kept: List[Tuple[str, str]] = []
        for u, member in entries:
            if _uuid_key(u) in events.deleted:
                continue
            kept.append((u, member))
        keyed = []
        for i, (u, member) in enumerate(kept):
            ev = events.pages.get(_uuid_key(u))
            key = ev.order_key if ev is not None else None
            keyed.append(((0, key.encode("utf-8", "replace"), i) if key is not None else (1, b"", i), (u, member)))
        keyed.sort(key=lambda item: item[0])
        return [entry for _k, entry in keyed]

    def _build_page(self, n_uuid: str, member: str, events: _Events) -> Page:
        doc = self.doc
        ev = events.pages.get(_uuid_key(n_uuid))
        tmpl = events.templates.get(ev.template) if ev is not None and ev.template else None
        width, height = DEFAULT_PAGE_SIZE
        background: Optional[PdfBackground] = None
        builtin = False
        paper = "plain"
        canvas: Optional[XY] = tmpl.canvas if tmpl is not None else None
        label = f"page {n_uuid[:8]}"

        if tmpl is None:
            doc.warn(f"{label}: no template event; page size and paper are guessed")
        elif tmpl.attachment is None:
            doc.warn(f"{label}: template {tmpl.uuid[:8]} names no attachment; page size guessed")
        else:
            info = self.pdf(tmpl.attachment)
            data = self.attachment(tmpl.attachment)
            if info is None:
                if data is None:
                    doc.warn(f"{label}: paper attachment {tmpl.attachment[:8]} is missing; page size guessed")
                else:
                    doc.warn(f"{label}: paper attachment {tmpl.attachment[:8]} is not a PDF; page size guessed")
            else:
                idx = tmpl.pdf_page - 1
                if idx < 0 or idx >= len(info.pages):
                    doc.warn(f"{label}: PDF page {tmpl.pdf_page} does not exist in attachment "
                             f"{tmpl.attachment[:8]} ({len(info.pages)} pages); using the last page")
                    idx = max(0, min(len(info.pages) - 1, idx))
                pg = info.pages[idx]
                width, height = pg.width, pg.height
                doc.pdfs.setdefault(tmpl.attachment, data)
                background = PdfBackground(tmpl.attachment, idx)
                builtin = (len(info.pages) == 1 and info.producer == "svg2pdf" and not info.creator
                           and bool(BUILTIN_TEMPLATE_RE.match(tmpl.name)))
                if builtin:
                    paper = _paper_style(data, tmpl.lined)
        if canvas is None and tmpl is not None and tmpl.attachment is not None and background is not None:
            doc.warn(f"{label}: template event carries no canvas size; assuming 132/72 units per point")
        if background is None and canvas is not None:
            width, height = canvas[0] / CANVAS_PER_POINT, canvas[1] / CANVAS_PER_POINT
        scale = 1.0 / CANVAS_PER_POINT
        if canvas is not None and canvas[0] > 0:
            scale = width / canvas[0]
            if canvas[1] > 0 and abs(height / canvas[1] - scale) > 0.01 * scale:
                doc.warn(f"{label}: canvas aspect differs from the paper PDF; using the horizontal scale")

        page = Page(width=width, height=height, background=background, paper=paper,
                    template_is_builtin=builtin)
        content = self.member(member)
        if content:
            _PageParser(self, page, content, scale, label).run()
        return page


# --------------------------------------------------------------------------- page content


@dataclass
class _Meta:
    uuid: str
    tombstone: bool = False
    attachment: Optional[str] = None


class _PageParser:
    def __init__(self, reader: _Reader, page: Page, data: bytes, scale: float, label: str):
        self.reader = reader
        self.doc = reader.doc
        self.page = page
        self.data = data
        self.scale = scale
        self.label = label
        self.pending: Optional[_Meta] = None
        self.unknown_kinds: set = set()

    def run(self) -> None:
        try:
            records = protobuf.decode_records(self.data)
        except ValueError:
            self.doc.warn(f"{self.label}: ink layer is truncated; reading what could be decoded")
            records = _salvage_records(self.data)
        for rec in records:
            fields = _decode(rec)
            if not fields:
                continue
            meta = self._as_metadata(fields)
            if meta is not None:
                self.pending = meta
                continue
            if len(fields) != 1 or fields[0].wire_type != protobuf.WIRE_LEN:
                self.doc.warn(f"{self.label}: unrecognised record skipped")
                continue
            kind = fields[0].number
            body = _decode(bytes(fields[0].value))
            meta = self.pending
            self.pending = None
            if body is None:
                self.doc.warn(f"{self.label}: element record #{kind} could not be decoded")
                continue
            if meta is not None and meta.tombstone:
                continue
            try:
                if kind == 7:
                    self._stroke(body)
                elif kind == 1:
                    self._image(body, meta)
                elif kind == 8:
                    self._text(body)
                else:
                    if kind not in self.unknown_kinds:
                        self.unknown_kinds.add(kind)
                        self.doc.warn(f"{self.label}: unsupported element kind #{kind} skipped")
            except Exception as exc:  # noqa: BLE001 - tolerant reader: never fail on one element
                self.doc.warn(f"{self.label}: element #{kind} skipped ({exc.__class__.__name__}: {exc})")

    @staticmethod
    def _as_metadata(fields: Sequence[Field]) -> Optional[_Meta]:
        u = _uuid(fields, 1)
        if u is None:
            return None
        if not any(f.number in (8, 9, 16) and f.wire_type == protobuf.WIRE_VARINT for f in fields):
            return None
        return _Meta(uuid=u, tombstone=_int(fields, 3) == 1, attachment=_uuid(fields, 4))

    # -- geometry helpers --------------------------------------------------------

    def _pt(self, x: float, y: float, width_pt: float, offset: XY) -> Point:
        s = self.scale
        return Point((x + offset[0]) * s, (y + offset[1]) * s, width_pt)

    def _add_stroke(self, points: List[Point], controls: Optional[List[Tuple[Point, Point]]],
                    color: Tuple[float, float, float, float], kind: str, pen: Optional[str],
                    width: float, outline: Optional[List[List[Point]]] = None) -> None:
        if not points:
            return
        if controls is not None and len(controls) != len(points) - 1:
            controls = None
        coords = [v for p in points for v in (p.x, p.y, p.width)]
        if controls:
            coords += [v for c1, c2 in controls for v in (c1.x, c1.y, c2.x, c2.y)]
        if not all(math.isfinite(v) for v in coords):
            self.doc.warn(f"{self.label}: stroke with non-finite coordinates skipped")
            return
        if width <= 0:
            width = 0.5  # a visible hairline instead of an invisible stroke
            for p in points:
                p.width = width
        self.page.strokes.append(Stroke(points=points, color=color, kind=kind, pen=pen,
                                        width=width, controls=controls, outline=outline))

    # -- strokes -----------------------------------------------------------------

    def _stroke(self, body: Sequence[Field]) -> None:
        color = self._color(_msg(body, 4))
        kind = "highlighter" if _int(body, 5) == 1 else "pen"
        tool = _int(body, 3)
        pen = "ballpoint" if tool is None else ("fountain" if tool in (1, 4) else ("pencil" if tool == 5 else None))
        marker = _msg(body, 20)
        if marker and protobuf.get(marker, 1) is not None:
            pen = "marker"
        offset = _point(_msg(body, 6)) or (0.0, 0.0)

        shape = _msg(body, 9)
        if shape and self._shape(shape, color, kind, pen, offset):
            return

        frame_field = protobuf.get(body, 2)
        if frame_field is None or frame_field.wire_type != protobuf.WIRE_LEN:
            self.doc.warn(f"{self.label}: stroke without geometry skipped")
            return
        frame = bytes(frame_field.value)
        if not frame:
            return
        if not applelz4.is_apple_lz4(frame):
            self.doc.warn(f"{self.label}: stroke geometry is not an Apple LZ4 frame; stroke skipped")
            return
        try:
            geo = tpl.decode(applelz4.decompress(frame))
        except ValueError as exc:
            self.doc.warn(f"{self.label}: stroke geometry could not be decoded ({exc}); stroke skipped")
            return
        if geo is None:
            return  # erased element: no points
        if isinstance(geo, tpl.FlatStroke):
            self._flat(geo, color, kind, pen, offset)
        elif isinstance(geo, tpl.RibbonStroke):
            self._ribbon(geo, color, kind, pen or "fountain", offset)
        elif isinstance(geo, tpl.PencilStroke):
            if not self.reader._pencil_warned:
                self.reader._pencil_warned = True
                self.doc.warn("pencil strokes are approximated: constant width, tilt data dropped")
            self._pencil(geo, color, kind, "pencil", offset)

    def _flat(self, geo: tpl.FlatStroke, color, kind: str, pen: Optional[str], offset: XY) -> None:
        width_pt = geo.width / 2.0
        try:
            subpaths = geo.subpaths()
        except ValueError as exc:
            self.doc.warn(f"{self.label}: flat stroke flags are inconsistent ({exc}); stroke skipped")
            return
        for start, quads in subpaths:
            points = [self._pt(start[0], start[1], width_pt, offset)]
            if not quads:
                self._add_stroke(points, None, color, kind, pen, width_pt)
                continue
            controls: List[Tuple[Point, Point]] = []
            px, py = start
            for cx, cy, ex, ey in quads:
                c1 = (px + 2.0 / 3.0 * (cx - px), py + 2.0 / 3.0 * (cy - py))
                c2 = (ex + 2.0 / 3.0 * (cx - ex), ey + 2.0 / 3.0 * (cy - ey))
                controls.append((self._pt(c1[0], c1[1], width_pt, offset), self._pt(c2[0], c2[1], width_pt, offset)))
                points.append(self._pt(ex, ey, width_pt, offset))
                px, py = ex, ey
            self._add_stroke(points, controls, color, kind, pen, width_pt)

    def _ribbon(self, geo: tpl.RibbonStroke, color, kind: str, pen: Optional[str], offset: XY) -> None:
        subpaths = geo.subpaths or ([geo.points] if geo.points else [])
        for sub in subpaths:
            points = [self._pt(x, y, 2.0 * r * self.scale, offset) for x, y, r in sub]
            if not points:
                continue
            width = statistics.median(p.width for p in points)
            self._add_stroke(points, None, color, kind, pen, width)

    def _pencil(self, geo: tpl.PencilStroke, color, kind: str, pen: str, offset: XY) -> None:
        width_pt = PENCIL_WIDTH_FACTOR * geo.width
        subpaths = geo.subpaths or ([geo.points] if geo.points else [])
        for sub in subpaths:
            points = [self._pt(x, y, width_pt, offset) for x, y in sub]
            self._add_stroke(points, None, color, kind, pen, width_pt)

    @staticmethod
    def _color(fields: Optional[Sequence[Field]]) -> Tuple[float, float, float, float]:
        if fields is None:
            return (0.0, 0.0, 0.0, 1.0)
        comps = [max(0.0, min(1.0, _f32(fields, i))) for i in (1, 2, 3, 4)]
        return (comps[0], comps[1], comps[2], comps[3])

    # -- auto-shapes (#9) --------------------------------------------------------

    def _shape(self, shape: Sequence[Field], color, kind: str, pen: Optional[str], offset: XY) -> bool:
        """Emit the recognised shape as a polyline stroke; False when #9 holds no geometry."""
        width_pt = _f32(shape, 15) / 2.0
        pts: List[XY] = []
        line = _msg(shape, 1)
        poly = _msg(shape, 2)
        rect = _msg(shape, 3)
        ellipse = _msg(shape, 4)
        if ellipse is not None:
            centre = _point(_msg(ellipse, 1))
            radii = _point(_msg(ellipse, 2))
            if centre is None or radii is None:
                return False
            theta = _f32(ellipse, 3)
            ct, st = math.cos(theta), math.sin(theta)
            for i in range(ELLIPSE_SAMPLES + 1):
                t = 2.0 * math.pi * i / ELLIPSE_SAMPLES
                ex, ey = radii[0] * math.cos(t), radii[1] * math.sin(t)
                pts.append((centre[0] + ex * ct - ey * st, centre[1] + ex * st + ey * ct))
        elif rect is not None:
            centre = _point(_msg(rect, 1))
            size = _point(_msg(rect, 2))
            if centre is None or size is None:
                return False
            hw, hh = size[0] / 2.0, size[1] / 2.0
            cx, cy = centre
            pts = [(cx - hw, cy - hh), (cx + hw, cy - hh), (cx + hw, cy + hh), (cx - hw, cy + hh), (cx - hw, cy - hh)]
        else:
            container = line if line is not None else poly
            if container is None:
                return False
            for f in container:
                if f.wire_type != protobuf.WIRE_LEN:
                    continue
                p = _point(_decode(bytes(f.value)))
                if p is not None:
                    pts.append(p)
            if len(pts) < 1:
                return False
        if width_pt <= 0:
            width_pt = 1.0
        points = [self._pt(x, y, width_pt, offset) for x, y in pts]
        self._add_stroke(points, None, color, kind, pen, width_pt)
        return True

    # -- images ------------------------------------------------------------------

    def _image(self, body: Sequence[Field], meta: Optional[_Meta]) -> None:
        rect = _rect(_msg(body, 2))
        if rect is None:
            self.doc.warn(f"{self.label}: image without a placement rectangle skipped")
            return
        attachment = _uuid(body, 4) or (meta.attachment if meta is not None else None)
        data = self.reader.attachment(attachment) if attachment else None
        if data is None:
            self.doc.warn(f"{self.label}: image attachment {attachment[:8] if attachment else '?'} is missing; image skipped")
            return
        fmt = _sniff_image(data)
        if fmt is None:
            self.doc.warn(f"{self.label}: image attachment {attachment[:8]} is neither PNG nor JPEG; image skipped")
            return
        rotation = 0.0
        crop_fields = _msg(body, 3)
        crop = _rect(crop_fields)
        if crop is not None:
            if abs(crop[2] - rect[2]) > 0.01 or abs(crop[3] - rect[3]) > 0.01:
                self.doc.warn(f"{self.label}: image crop rectangle ignored (whole image shown)")
            if crop_fields is not None and protobuf.get(crop_fields, 3) is not None:
                rad = _f32(crop_fields, 3)
                if abs(rad) > 1e-6:
                    rotation = math.degrees(rad)
                    self.doc.warn(f"{self.label}: image rotation of {rotation:.1f} degrees applied about the box centre (unverified)")
        s = self.scale
        self.page.images.append(Image(x=rect[0] * s, y=rect[1] * s, w=rect[2] * s, h=rect[3] * s,
                                      data=data, fmt=fmt, rotation=rotation))

    # -- text boxes --------------------------------------------------------------

    def _text(self, body: Sequence[Field]) -> None:
        outer = _rect(_msg(body, 2))
        inner = _rect(_msg(body, 3))
        padding = _f32(body, 10, 10.0)
        if inner is None and outer is not None:
            inner = (outer[0] + padding, outer[1] + padding, max(0.0, outer[2] - 2 * padding),
                     max(0.0, outer[3] - 2 * padding))
        if inner is None:
            self.doc.warn(f"{self.label}: text box without a frame skipped")
            return
        rtf_field = protobuf.get(body, 6)
        rtf = bytes(rtf_field.value) if rtf_field is not None and rtf_field.wire_type == protobuf.WIRE_LEN else b""
        text, runs = parse_rtf(rtf)
        s = self.scale
        for run in runs:
            if run.size is not None:
                run.size = run.size * s
        size = next((r.size for r in runs if r.size is not None), None)
        if size is None:
            size = 24.0 * s  # GoodNotes' default \fs48
        color = next((r.color for r in runs if r.color is not None), None) or (0.0, 0.0, 0.0, 1.0)
        if not runs and text:
            runs = [TextRun(text, size=size)]
        self.page.texts.append(TextBox(x=inner[0] * s, y=inner[1] * s, w=inner[2] * s, h=inner[3] * s,
                                       text=text, runs=runs, color=color, size=size))


def _sniff_image(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return None


# --------------------------------------------------------------------------- public API


def read_goodnotes(data: bytes) -> Document:
    """Parse a ``.goodnotes`` file into a :class:`Document`.

    Raises ``ValueError`` only when ``data`` is not a GoodNotes container at all (not a ZIP,
    or a ZIP without a page index and without ``notes/`` members).  Everything else that is
    damaged or unknown is skipped with a line on ``Document.warnings``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_goodnotes expects bytes")
    return _Reader(bytes(data)).read()
