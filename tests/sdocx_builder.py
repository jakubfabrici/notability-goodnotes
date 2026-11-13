"""Builds synthetic Samsung Notes ``.sdocx`` notes in code for ``tests/test_samsungnotes.py``.

The byte layouts mirror what :mod:`gnnote.samsungnotes.records` reads (and what the public
sample notes contain); every builder returns ``bytes``.  Nothing here was ever opened in
Samsung Notes: the notes only exercise gnnote's reader.
"""
from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from typing import Dict, List, Optional, Sequence, Tuple

PAGE_END = b"Page for SAMSUNG S-Pen SDK"
END_TAG_IDENT = b"Document for S-Pen SDK"
PEN = "com.samsung.android.sdk.pen.pen.preload."
MTIME = 1_700_000_000_000_000
XY = Tuple[float, float]
Rect = Tuple[float, float, float, float]


def bitfield(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    n = (value.bit_length() + 7) // 8
    return bytes([n]) + value.to_bytes(n, "little")


def short_str(s: str) -> bytes:
    raw = s.encode("utf-16-le")
    return struct.pack("<H", len(raw) // 2) + raw


def short_utf8(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def frame(data_type: int, props: int, fields: int, fixed: bytes, flex: bytes = b"") -> bytes:
    """``u32 size (inclusive), u16 data type, u32 flex offset, props, fields, fixed, flex``."""
    head = struct.pack("<H", data_type)
    bits = bitfield(props) + bitfield(fields)
    flex_off = 4 + 2 + 4 + len(bits) + len(fixed) if fields else 0
    body = head + struct.pack("<I", flex_off) + bits + fixed + flex
    return struct.pack("<I", len(body) + 4) + body


def point_delta(v: float) -> int:
    q = round(abs(v) * 32)
    assert q == abs(v) * 32 and q < 1 << 15, v
    return q | (0x8000 if v < 0 else 0)


def small_delta(v: float) -> int:
    q = round(abs(v) * 4096)
    assert q == abs(v) * 4096 and q < 1 << 15, v
    return q | (0x8000 if v < 0 else 0)


# --------------------------------------------------------------------------- objects


def object_base(uuid: str, rect: Rect, angle: Optional[float] = None, page_index: Optional[int] = None,
                fmt: int = 4000, visible: bool = True) -> bytes:
    fixed = struct.pack("<I", fmt) + short_utf8(uuid) + struct.pack("<q", MTIME) + struct.pack("<4d", *rect) \
        + struct.pack("<I", 0) + b"\x00"
    fields = 0
    flex = b""
    if angle is not None:
        fields |= 1
        flex += struct.pack("<f", angle)
    if page_index is not None:
        fields |= 1 << 20
        flex += struct.pack("<I", page_index)
    return frame(0, 0b0110 | (0b1000 if visible else 0), fields, fixed, flex)  # selectable, movable, visible


def stroke(points: Sequence[XY], pressures: Optional[Sequence[float]] = None, *, pen_id: int = 0,
           colour: bytes = b"\x00\x00\x00\xff", size: float = 9.0, compressed: bool = False, tilt: bool = False,
           eraser: bool = False, fixed_width: bool = False, tool_type: int = 2, line_type: Optional[int] = None,
           unknown_field: bool = False, uuid: str = "stroke", visible: bool = True, rainbow: bool = False,
           gradient: Sequence[bytes] = (), colour_type: Optional[int] = None) -> bytes:
    """A type-1 object payload (base frame + stroke frame), without the trailing hash."""
    n = len(points)
    pressures = list(pressures) if pressures is not None else [0.5] * n
    times = [i * 8 for i in range(n)]
    events = b""
    if n and not compressed:
        events += b"".join(struct.pack("<2d", x, y) for x, y in points)
        events += struct.pack(f"<{n}f", *pressures) + struct.pack(f"<{n}I", *times)
        if tilt:
            events += struct.pack(f"<{n}f", *[0.5] * n) + struct.pack(f"<{n}f", *[1.0] * n)
    elif n:
        m = n - 1
        events += struct.pack("<2d", *points[0])
        for i in range(m):
            events += struct.pack("<2H", point_delta(points[i + 1][0] - points[i][0]),
                                  point_delta(points[i + 1][1] - points[i][1]))
        events += struct.pack("<f", pressures[0])
        events += struct.pack(f"<{m}H", *[small_delta(pressures[i + 1] - pressures[i]) for i in range(m)])
        events += struct.pack("<I", 0) + struct.pack(f"<{m}H", *[8] * m)
        if tilt:
            events += struct.pack("<f", 0.5) + struct.pack(f"<{m}H", *[0] * m)
            events += struct.pack("<f", 1.0) + struct.pack(f"<{m}H", *[0] * m)
    props = (1 if compressed else 0) | (4 if tilt else 0) | (8 if eraser else 0) | (16 if fixed_width else 0) \
        | (1 << 12 if rainbow else 0)
    fields = (1 << 2) | (1 << 3) | (1 << 7)
    flex = b""
    if unknown_field:
        fields |= 1 << 5  # a list sdocx2pdf does not decode either
        flex += b"\x00\x00\x00\x00"
    flex += colour + struct.pack("<f", size) + struct.pack("<I", pen_id)
    if line_type is not None:
        fields |= 1 << 14
        flex += struct.pack("<H", line_type)
    if gradient:
        fields |= 1 << 24
        flex += struct.pack("<H", len(gradient)) + b"".join(gradient)
    if colour_type is not None:
        fields |= 1 << 25
        flex += struct.pack("<H", colour_type)
    fixed = struct.pack("<H", n) + events + struct.pack("<H", tool_type)
    xs = [p[0] for p in points] or [0.0]
    ys = [p[1] for p in points] or [0.0]
    return object_base(uuid, (min(xs), min(ys), max(xs), max(ys)), visible=visible) + frame(1, props, fields, fixed, flex)


def path(segments: Sequence[Tuple[int, Sequence[float]]]) -> bytes:
    """``segments``: (kind, numbers) with 1 move, 2 line (x, y), 3 quad (cx, cy, x, y), 4 cubic
    (6 numbers), 5 arc (x0, y0, x1, y1, start, sweep), 6 close, 7 oval (x0, y0, x1, y1)."""
    out = struct.pack("<I", len(segments))
    for kind, nums in segments:
        out += bytes([kind]) + struct.pack(f"<{len(nums)}d", *nums)
    return out


def shape_base(line_colour: Optional[bytes] = None, line_width: Optional[float] = None, dash: int = 0,
               begin_arrow: int = 0, end_arrow: int = 0, colour_type: int = 0) -> bytes:
    fixed = struct.pack("<I", 0) + struct.pack("<I", 4) + struct.pack("<I", 0) + b"\x00"
    fields = 0
    flex = b""
    if line_colour is not None:
        fields |= 1 << 2
        effect = bitfield(0) + bytes([colour_type]) + line_colour + b"\x00" + struct.pack("<H", 0) \
            + struct.pack("<2f", 0, 0) + b"\x00"
        flex += struct.pack("<I", len(effect)) + effect
    if line_width is not None:
        fields |= 1 << 3
        flex += struct.pack("<I", 12) + struct.pack("<f", line_width) + bytes([0, dash, 1, 1, begin_arrow, 0,
                                                                                end_arrow, 0])
    return frame(6, 0, fields, fixed, flex)


def fill_colour(colour: bytes, gradient: bool = False) -> bytes:
    body = b"\x01" + bitfield(1 if gradient else 0) + colour + b"\x00" + struct.pack("<h", 0) \
        + struct.pack("<2f", 0, 0) + b"\x00"
    return struct.pack("<I", len(body) + 4) + body


def fill_image(bind_id: int) -> bytes:
    body = b"\x02" + b"\x00" + struct.pack("<i", bind_id) + bytes(16 + 8 + 12 + 1 + 16 + 4)
    return struct.pack("<I", len(body) + 4) + body


def text_common(text: str, spans: Sequence[Tuple[int, int, int, bytes]] = (),
                paragraphs: Sequence[Tuple[int, int, int, bytes]] = (),
                margins: Tuple[float, float, float, float] = (16.0, 10.0, 16.0, 10.0),
                inline: Sequence[Tuple[int, int, bytes, int, int]] = (), fmt: int = 4000) -> bytes:
    """``spans``/``paragraphs``: (kind, start, end, data); ``inline``: (index, object type,
    object bytes without hash, layout, constraint)."""
    units = text.encode("utf-16-le")
    body = struct.pack("<I", len(units) // 2) + units
    body += struct.pack("<I", len(spans))
    for kind, start, end, data in spans:
        body += struct.pack("<H", 16 + len(data)) + struct.pack("<4I", kind, start, end, 0) + data
    body += struct.pack("<I", len(paragraphs))
    for kind, start, end, data in paragraphs:
        body += struct.pack("<H", 12 + len(data)) + struct.pack("<3I", kind, start, end) + data
    body += struct.pack("<4f", *margins) + b"\x00" + struct.pack("<H", 0)
    if fmt >= 2035:
        body += struct.pack("<II", 1 if inline else 0, 0)
        if inline:
            body += struct.pack("<I", len(inline))
            for index, obj_type, obj, layout, constraint in inline:
                item = struct.pack("<II", len(obj), obj_type) + obj + struct.pack("<III", index, layout, constraint)
                body += struct.pack("<I", len(item)) + item
    return struct.pack("<I", len(body)) + body


def size_span(start: int, end: int, size_dp: float) -> Tuple[int, int, int, bytes]:
    return 3, start, end, struct.pack("<f", size_dp) + bytes(4)


def flag_span(kind: int, start: int, end: int, on: bool = True) -> Tuple[int, int, int, bytes]:
    return kind, start, end, struct.pack("<I", 1 if on else 0) + bytes(4)


def shape(obj_type: int, rect: Rect, segments: Sequence[Tuple[int, Sequence[float]]], *, angle: float = 0.0,
          shape_type: int = 4, line_colour: Optional[bytes] = None, line_width: Optional[float] = None,
          fill: Optional[bytes] = None, text: Optional[bytes] = None, flip: int = 0, crop: Optional[Rect] = None,
          original: Optional[Rect] = None, page_index: Optional[int] = None, uuid: str = "shape", dash: int = 0,
          fmt: int = 4000, border: Optional[int] = None, border_width: Optional[float] = None,
          border_picture: Optional[int] = None) -> bytes:
    """A shape (7), text box (2) or picture (3): base, shape base, shape and last frame."""
    out = object_base(uuid, rect, angle=angle or None, page_index=page_index, fmt=fmt)
    out += shape_base(line_colour, line_width, dash=dash)
    p = path(segments) if segments else b""
    fixed = struct.pack("<I", shape_type) + struct.pack("<4d", *rect) + struct.pack("<f", angle) \
        + struct.pack("<I", len(p)) + p + b"\x00"
    if obj_type == 7:
        fixed += struct.pack("<4d", *rect)
    fields = 0
    flex = b""
    if text is not None:
        fields |= 1
        flex += text
    if fill is not None:
        fields |= 1 << 5
        flex += fill
    out += frame(7, flip, fields, fixed, flex)
    if obj_type == 3:
        fields = 0
        flex = b""
        if crop is not None:
            fields |= 1 << 1
            flex += struct.pack("<4i", *[int(v) for v in crop])
        if border_width is not None:
            fields |= 1 << 4
            flex += struct.pack("<f", border_width)
        if border is not None:
            fields |= 1 << 5
            flex += struct.pack("<H", border)
        if border_picture is not None:
            fields |= 1 << 9
            flex += struct.pack("<I", border_picture)
        if original is not None:
            fields |= 1 << 17
            flex += struct.pack("<4d", *original)
        out += frame(3, 0, fields, b"", flex)
    elif obj_type == 2:
        out += frame(2, 0, 0, b"")
    return out


def rect_path(rect: Rect) -> List[Tuple[int, Sequence[float]]]:
    x0, y0, x1, y1 = rect
    return [(1, (x0, y0)), (2, (x1, y0)), (2, (x1, y1)), (2, (x0, y1)), (6, ())]


def line(start: XY, end: XY, *, line_colour: Optional[bytes] = b"\x00\x00\xff\xff", line_width: Optional[float] = 6.0,
         begin_arrow: int = 0, end_arrow: int = 0, uuid: str = "line", controls: Sequence[XY] = (),
         connector: int = 0) -> bytes:
    rect = (min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1]))
    out = object_base(uuid, rect) + shape_base(line_colour, line_width, begin_arrow=begin_arrow, end_arrow=end_arrow)
    fixed = bytes([connector, 0, len(controls)]) + b"".join(struct.pack("<2d", *c) for c in controls) \
        + struct.pack("<2d", *start) + struct.pack("<2d", *end) + struct.pack("<4d", *rect) \
        + struct.pack("<4d", *rect) + struct.pack("<f", 0.0)
    return out + frame(8, 0, 0, fixed)


def other(obj_type: int, rect: Rect = (0, 0, 10, 10), page_index: Optional[int] = None) -> bytes:
    return object_base(f"other-{obj_type}", rect, page_index=page_index) + struct.pack("<I", 8) + bytes(4)


# --------------------------------------------------------------------------- pages and members


def layer(objects: Sequence[Tuple[int, bytes]], name: str = "", hidden: bool = False, children: int = 0) -> bytes:
    """``objects``: (object type, payload without hash).  The header's flex offset is stored
    relative to the layer here; :func:`page` makes it absolute."""
    fields = 4 if name else 0
    head = bitfield(1 if hidden else 0) + bitfield(fields) + struct.pack("<I", 1)
    rel_flex = 4 + 4 + len(head)
    body = struct.pack("<I", rel_flex if name else 0) + head + (short_str(name) if name else b"")
    out = struct.pack("<I", len(body) + 4) + body
    out += struct.pack("<I", len(objects))
    for obj_type, payload in objects:
        digest = hashlib.sha256(f"x{MTIME}".encode()).digest()
        out += bytes([obj_type]) + struct.pack("<H", children) + struct.pack("<I", len(payload) + 32) + payload + digest
    return out + bytes(32)


def page(uuid: str, width: int, height: int, layers: Sequence[bytes], *, template: Optional[int] = None,
         pdfs: Sequence[Tuple[int, int, Rect]] = (), background: Optional[bytes] = None, fmt: int = 4000,
         recognition: Sequence[bytes] = (), custom_objects: Sequence[bytes] = (), orientation: int = 0) -> bytes:
    """``recognition``: handwriting recognition blobs (field bit 16); ``custom_objects``: the
    bodies of sticky notes (field bit 18, each stored as u32 type 1 and a u32-sized body)."""
    fields = 0
    flex = b""
    if background is not None:
        fields |= 1 << 5
        flex += background
    if pdfs:
        fields |= 1 << 8
        flex += struct.pack("<H", len(pdfs))
        for bind, index, rect in pdfs:
            flex += struct.pack("<II", bind, index)
            flex += struct.pack("<4i", *[int(v) for v in rect]) if fmt >= 2034 else struct.pack("<4d", *rect)
    if template is not None:
        fields |= 1 << 9
        flex += struct.pack("<I", template)
    if recognition:
        fields |= 1 << 15 | 1 << 16
        flex += struct.pack("<q", MTIME) + struct.pack("<I", len(recognition))
        flex += b"".join(struct.pack("<I", len(blob)) + blob for blob in recognition)
    if custom_objects:
        fields |= 1 << 18
        flex += struct.pack("<I", len(custom_objects))
        flex += b"".join(struct.pack("<II", 1, len(body)) + body for body in custom_objects)
    bits = bitfield(0) + bitfield(fields)
    fixed = struct.pack("<5I", orientation, width, height, 0, 0) + short_str(uuid) + struct.pack("<q", MTIME) \
        + struct.pack("<II", fmt, fmt)
    head_len = 8 + len(bits) + len(fixed)
    page_end = head_len + len(flex)
    out = bytearray(struct.pack("<II", page_end, head_len if fields else 0) + bits + fixed + flex)
    out += struct.pack("<HH", len(layers), 0)
    for lay in layers:
        start = len(out)
        out += lay
        rel = struct.unpack_from("<I", out, start + 4)[0]
        if rel:
            struct.pack_into("<I", out, start + 4, start + rel)
    out += bytes(32) + PAGE_END
    return bytes(out)


def text_object(text: bytes, rect: Rect = (0, 0, 0, 0), fmt: int = 4000) -> bytes:
    """The note title / body record: a text-box object (base, shape base, shape, text frame)."""
    return shape(2, rect, [], text=text, uuid="body", fmt=fmt)


def note(width: int, height: int, strings: Optional[Dict[int, str]] = None, title: Optional[bytes] = None,
         body: Optional[bytes] = None, font_delta: Optional[int] = None) -> bytes:
    strings = strings or {}
    fixed = struct.pack("<I", 4000) + short_str("doc") + struct.pack("<I", 1) + struct.pack("<qq", MTIME, MTIME) \
        + struct.pack("<5I", width, height, 0, 0, 1)
    title = title or b""
    body = body or b""
    fixed += struct.pack("<I", len(title)) + title + struct.pack("<I", len(body)) + body
    bits = bitfield(0) + bitfield(1 << 10 | (1 << 11 if font_delta is not None else 0))
    flex_off = 4 + len(bits) + len(fixed)
    reg = struct.pack("<H", len(strings)) + b"".join(struct.pack("<I", k) + short_str(v) for k, v in strings.items())
    data = struct.pack("<I", flex_off) + bits + fixed + struct.pack("<I", len(reg)) + reg
    if font_delta is not None:
        data += struct.pack("<i", font_delta)
    return data + hashlib.sha256(data).digest()


def media_info(entries: Dict[int, str]) -> bytes:
    out = struct.pack("<I", 4000) + struct.pack("<H", len(entries))
    for bind, name in entries.items():
        entry = struct.pack("<I", bind) + short_str(name) + b"0" * 64 + struct.pack("<H", 1) \
            + struct.pack("<q", MTIME) + b"\x01"
        out += struct.pack("<I", len(entry)) + entry
    return out + b"EOFX"


def end_tag(width: int, height: int, pageless: bool = False, locked: bool = False, landscape: bool = False) -> bytes:
    body = struct.pack("<I", 4000) + short_str("doc") + struct.pack("<q", MTIME) + struct.pack("<I", 2 if landscape else 0) \
        + short_str("") + struct.pack("<If", width, float(height)) + short_str("") \
        + struct.pack("<II", 0xFFFFFFFF, 0xFFFFFFFF) + short_str("") + struct.pack("<I", 1) \
        + struct.pack("<q", MTIME) + struct.pack("<I", 0) + struct.pack("<HH", 1 if pageless else 0,
                                                                         1 if locked else 0)
    body += END_TAG_IDENT
    return struct.pack("<H", len(body)) + body


def page_list(uuids: Sequence[str]) -> bytes:
    return bytes(32) + struct.pack("<H", len(uuids)) + b"".join(short_str(u) + bytes(32) for u in uuids)


def sdocx(pages: Sequence[bytes], *, width: int = 1080, height: int = 1527, strings: Optional[Dict[int, str]] = None,
          title: Optional[bytes] = None, body: Optional[bytes] = None, media: Optional[Dict[int, Tuple[str, bytes]]] = None,
          pageless: bool = False, locked: bool = False, folder: str = "", uuids: Optional[Sequence[str]] = None,
          extra: Optional[Dict[str, bytes]] = None, page_list_member: bool = True,
          font_delta: Optional[int] = None, landscape: bool = False) -> bytes:
    """A complete note: ``pages`` are :func:`page` results, named by ``uuids`` (default
    ``page-0``, ``page-1`` ...)."""
    uuids = list(uuids) if uuids is not None else [f"page-{i}" for i in range(len(pages))]
    media = media or {}
    members: List[Tuple[str, bytes]] = []
    if page_list_member:
        members.append(("pageIdInfo.dat", page_list(uuids)))
    members += [(f"{u}.page", p) for u, p in zip(uuids, pages)]
    members.append(("media/mediaInfo.dat", media_info({k: v[0] for k, v in media.items()})))
    members += [("media/" + name, data) for name, data in media.values()]
    members.append(("note.note", note(width, height, strings, title, body, font_delta)))
    members.append(("end_tag.bin", end_tag(width, height, pageless, locked, landscape)))
    members += list((extra or {}).items())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(folder + name, data)
    return buf.getvalue()


def tiny_png(width: int = 4, height: int = 3, colour: Tuple[int, int, int] = (200, 30, 30)) -> bytes:
    import zlib

    rows = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
