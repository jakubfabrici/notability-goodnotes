"""Test-only builders for CollaNote notes and PencilKit drawings (synthetic data, no samples).

The byte shapes follow ``docs/collanote.md``: the ``_dkDrawing`` stroke protobuf, the page and
note JSON, the format-1 ZIP and the format-2 package, NSKeyedArchiver attributed strings, and
Apple's PKDrawing container with its channel table.  Nothing here is used by the package.
"""
from __future__ import annotations

import base64
import io
import json
import plistlib
import struct
import uuid
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gnnote import protobuf as pb

RGBA = Tuple[float, float, float, float]

# The empty PKDrawing CollaNote writes into every page's ``drawing`` field (64 bytes).
EMPTY_PKDRAWING = bytes.fromhex(
    "777264f0" "0100" "0800" "3214" "0d00000000" "1500000000" "1d00000000" "2500000000"
    "3a0e" "0800" "10ffffffffffffffff7f" "1800" "4210") + bytes(range(16))


# --------------------------------------------------------------------------- PencilKit

# bit -> struct code, ascending bit order (location, time, width, aspect, ?, force, azimuth,
# altitude, opacity, secondary width, ?)
PK_CODES = ((0, "ff"), (1, "f"), (2, "f"), (3, "H"), (4, "H"), (5, "H"), (6, "H"), (7, "H"),
            (8, "H"), (9, "f"), (10, "H"))


def pk_point(x: float, y: float, t: float = 0.0, w: float = 2.0, aspect: int = 1000, force: int = 500,
             azimuth: int = 32767, altitude: int = 0, opacity: int = 32767,
             secondary: Optional[float] = None) -> Dict[int, Any]:
    """Raw channel values of one point, keyed by channel bit."""
    return {0: (x, y), 1: t, 2: w, 3: aspect, 4: 0, 5: force, 6: azimuth, 7: altitude, 8: opacity,
            9: w if secondary is None else secondary, 10: 0}


def _pack(values: Dict[int, Any], mask: int) -> bytes:
    out = b""
    for bit, code in PK_CODES:
        if mask >> bit & 1:
            value = values[bit]
            out += struct.pack("<" + code, *(value if isinstance(value, tuple) else (value,)))
    return out


def pk_path(points: Sequence[Dict[int, Any]], per_point: Optional[int] = None, created: float = 780000000.0,
            count: Optional[int] = None, extra: bytes = b"") -> bytes:
    """A path message; channels that vary are per point unless ``per_point`` says otherwise."""
    if per_point is None:
        per_point = 1  # location is always per point
        for bit, _code in PK_CODES[1:]:
            if len({p[bit] for p in points}) > 1:
                per_point |= 1 << bit
    constant = 0x7FF & ~per_point
    const_block = _pack(points[0], constant) if points else b""
    records = b"".join(_pack(p, per_point) for p in points)
    return (pb.field_bytes(1, uuid.uuid4().bytes) + pb.field_fixed64(2, float(created))
            + pb.field_varint(3, len(points) if count is None else count) + pb.field_varint(4, per_point)
            + pb.field_varint(5, constant) + pb.field_bytes(6, const_block) + pb.field_bytes(7, records) + extra)


def pk_stroke(path: Optional[bytes], ink: int = 0, transform: Optional[Sequence[float]] = None,
              bounds: Optional[Sequence[float]] = None) -> bytes:
    """A stroke record; ``path=None`` is a deleted stroke (tombstone)."""
    body = (pb.field_bytes(1, uuid.uuid4().bytes)
            + pb.field_message(2, pb.field_varint(1, 1) + pb.field_varint(2, 1) + pb.field_varint(3, 1))
            + pb.field_message(3, pb.field_varint(2, 1))
            + pb.field_varint(4, ink))
    if path is not None:
        body += pb.field_message(5, path)
        rect = bounds if bounds is not None else (0.0, 0.0, 10.0, 10.0)
        body += pb.field_message(6, b"".join(pb.field_fixed32(i + 1, v) for i, v in enumerate(rect)))
        if transform is not None:
            body += pb.field_message(7, b"".join(pb.field_fixed32(i + 1, v) for i, v in enumerate(transform)))
    return body + pb.field_varint(8, 123456789)


def pk_ink(identifier: str = "com.apple.ink.pen", rgba: RGBA = (0.0, 0.0, 0.0, 1.0),
           variant: Optional[str] = None) -> bytes:
    body = pb.field_message(1, b"".join(pb.field_fixed32(i + 1, c) for i, c in enumerate(rgba)))
    body += pb.field_bytes(2, identifier) + pb.field_varint(3, 3)
    if variant is not None:
        body += pb.field_bytes(4, variant)
    return body + pb.field_fixed64(8, 0.5)


def pk_blob(inks: Sequence[bytes], strokes: Sequence[bytes], version: int = 1) -> bytes:
    body = pb.field_varint(1, 0) + pb.field_bytes(2, bytes(16)) + pb.field_bytes(2, uuid.uuid4().bytes)
    body += b"".join(pb.field_message(4, i) for i in inks)
    body += b"".join(pb.field_message(5, s) for s in strokes)
    body += pb.field_message(7, pb.field_varint(1, 0)) + pb.field_bytes(8, uuid.uuid4().bytes)
    return b"wrd\xf0" + struct.pack("<H", version) + body


# --------------------------------------------------------------------------- CollaNote ink


def _f32_fields(values: Sequence[float], omit_zero: bool = True) -> bytes:
    return b"".join(pb.field_fixed32(i + 1, v) for i, v in enumerate(values) if v != 0.0 or not omit_zero)


def dk_point(x: float, y: float, w: float, t: float = 0.0, force: Optional[float] = None) -> bytes:
    """A Point message the way CollaNote writes it (zero values omitted)."""
    body = _f32_fields((x, y, w))
    if t:
        body += pb.field_fixed64(4, float(t))
    if force is not None:
        body += pb.field_fixed32(5, force) + pb.field_fixed32(6, 0.5)
    return body


def dk_stroke(points: Sequence[Tuple[float, float, float]], width: float = 2.0,
              rgba: RGBA = (0.0, 0.0, 0.0, 1.0), ink_type: int = 1, flag: int = 0) -> bytes:
    style = pb.field_fixed32(1, width) if width else b""
    style += pb.field_message(2, _f32_fields(rgba))
    if ink_type:
        style += pb.field_varint(3, ink_type)
    body = pb.field_bytes(1, str(uuid.uuid4()).upper())
    body += b"".join(pb.field_message(2, dk_point(x, y, w, t=0.01 * i)) for i, (x, y, w) in enumerate(points))
    body += pb.field_message(3, style) + pb.field_fixed64(4, 1740000000.0)
    return body + pb.field_varint(6, flag) + pb.field_varint(7, 0)


def dk_drawing(strokes: Sequence[bytes]) -> str:
    """The base64 text of one ``_dkDrawing`` entry."""
    return base64.b64encode(b"".join(pb.field_message(1, s) for s in strokes)).decode("ascii")


# --------------------------------------------------------------------------- attachments


def attributed_string(text: str, font: Optional[str] = "ChalkboardSE-Regular", size: float = 14.0,
                      rgba: Optional[RGBA] = (0.0, 0.0, 1.0, 1.0)) -> str:
    """base64 of an NSKeyedArchiver ``NSAttributedString`` with one attribute run."""
    UID = plistlib.UID
    objects: List[Any] = ["$null"]

    def add(obj: Any) -> Any:
        objects.append(obj)
        return UID(len(objects) - 1)

    dict_class = add({"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]})
    keys: List[Any] = []
    values: List[Any] = []
    if font is not None:
        font_class = add({"$classname": "UIFont", "$classes": ["UIFont", "NSFont", "NSObject"]})
        name = add(font)
        keys.append(add("NSFont"))
        values.append(add({"NSName": name, "NSSize": size, "UIFontName": name, "UIFontPointSize": size,
                           "UIFontTraits": 0, "UISystemFont": False, "$class": font_class}))
    if rgba is not None:
        color_class = add({"$classname": "UIColor", "$classes": ["UIColor", "NSColor", "NSObject"]})
        keys.append(add("NSColor"))
        values.append(add({"UIColorComponentCount": 4, "UIRed": rgba[0], "UIGreen": rgba[1], "UIBlue": rgba[2],
                           "UIAlpha": rgba[3], "NSColorSpace": 2, "$class": color_class}))
    attributes = add({"NS.keys": keys, "NS.objects": values, "$class": dict_class})
    string_class = add({"$classname": "NSAttributedString", "$classes": ["NSAttributedString", "NSObject"]})
    root = add({"NSString": add(text), "NSAttributes": attributes, "$class": string_class})
    archive = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": root}, "$objects": objects}
    return base64.b64encode(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)).decode("ascii")


def attributed_runs(text: str, runs: Sequence[Tuple[int, str, float]], info: Optional[bytes] = None) -> str:
    """base64 of an ``NSAttributedString`` with several runs: ``(utf-16 length, font, size)``
    each, an ``NSAttributes`` array and ``NSAttributeInfo`` = varint pairs (length, index)."""
    UID = plistlib.UID
    objects: List[Any] = ["$null"]

    def add(obj: Any) -> Any:
        objects.append(obj)
        return UID(len(objects) - 1)

    dict_class = add({"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]})
    array_class = add({"$classname": "NSArray", "$classes": ["NSArray", "NSObject"]})
    font_class = add({"$classname": "UIFont", "$classes": ["UIFont", "NSFont", "NSObject"]})
    key = add("NSFont")
    table = []
    for _length, font, size in runs:
        value = add({"NSName": add(font), "NSSize": size, "$class": font_class})
        table.append(add({"NS.keys": [key], "NS.objects": [value], "$class": dict_class}))
    if info is None:
        info = b"".join(pb.varint(length) + pb.varint(i) for i, (length, _f, _s) in enumerate(runs))
    string_class = add({"$classname": "NSAttributedString", "$classes": ["NSAttributedString", "NSObject"]})
    root = add({"NSString": add(text), "NSAttributes": add({"NS.objects": table, "$class": array_class}),
                "NSAttributeInfo": add(info), "$class": string_class})
    archive = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": root}, "$objects": objects}
    return base64.b64encode(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)).decode("ascii")


IMAGE_PLACEHOLDER = "This Sticker should be a image, not a TextView"


def png_bytes(width: int = 4, height: int = 2) -> bytes:
    """A valid tiny PNG (pixel content irrelevant)."""
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def image_attachment(data: bytes, center: Tuple[float, float], size: Tuple[float, float],
                     rotation: float = 0.0, kind: str = "image") -> Dict[str, Any]:
    return {"type": kind, "id": "AbCdE", "imageInData": base64.b64encode(data).decode("ascii"),
            "center": list(center), "bound": [[0, 0], list(size)], "rotatedDegree": rotation,
            "attStringData": attributed_string(IMAGE_PLACEHOLDER, font=None, rgba=None)}


def text_attachment(text: str, center: Tuple[float, float], size: Tuple[float, float], rotation: float = 0.0,
                    **style: Any) -> Dict[str, Any]:
    return {"type": "text", "id": "14erY", "imageInData": "", "center": list(center), "bound": [[0, 0], list(size)],
            "rotatedDegree": rotation, "attStringData": attributed_string(text, **style)}


# --------------------------------------------------------------------------- notes


def note_json(name: Optional[str] = "Synthetic", size: Sequence[float] = (1050, 1485), **extra: Any) -> Dict[str, Any]:
    note: Dict[str, Any] = {
        "id": "abcdefghij0123456789", "size": list(size), "dateCreated": 780000000.0, "dateEdited": 780000100.0,
        "moddificationInfo": {"savedDate": 780000100.0, "moddifiedDevice": {"nameAndModel": "iPad", "id": "X"}},
        "thumbnail": "", "pages": [], "importedPdfDatas": [], "audios": [], "bookmarks": [], "favTools": [],
        "isCollaborationActivated": False, "isUsingCustomCover": False, "rememberedScrollOffset": 0,
    }
    if name is not None:
        note["name"] = name
    note.update(extra)
    return note


def cpage(strokes: Sequence[bytes] = (), pdf: Optional[Tuple[int, int]] = None,
          attachments: Sequence[Dict[str, Any]] = (), drawing: bytes = EMPTY_PKDRAWING,
          count: Optional[int] = None, layers: Optional[List[str]] = None) -> Dict[str, Any]:
    page: Dict[str, Any] = {
        "id": "pageid0001",
        "strokeCountBeforeSaving": len(strokes) if count is None else count,
        "_dkDrawing": layers if layers is not None else [dk_drawing(strokes) if strokes else ""],
        "drawing": base64.b64encode(drawing).decode("ascii"),
        "attachments": list(attachments),
    }
    if pdf is not None:
        page["pdfPointer"] = {"pdfIndex": pdf[0], "pageIndex": pdf[1]}
    return page


def _zip(members: Sequence[Tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def _json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def cnote_format1(note: Optional[Dict[str, Any]], pages: Sequence[Dict[str, Any]],
                  pdfs: Optional[Dict[int, bytes]] = None, extra: Sequence[Tuple[str, bytes]] = ()) -> bytes:
    """A format-1 ``.cnote`` ZIP (members in hash-like, not page, order)."""
    members: List[Tuple[str, bytes]] = [(f"{i}.cpage", _json(p)) for i, p in enumerate(pages)][::-1]
    if note is not None:
        members.insert(len(members) // 2, ("note without pdf.cnote", _json(note)))
    members += [(f"{i}.pdf", data) for i, data in (pdfs or {}).items()]
    return _zip(members + list(extra))


def cnote_package(note: Optional[Dict[str, Any]], pages: Sequence[Dict[str, Any]],
                  pdfs: Optional[Dict[int, bytes]] = None, folder: Optional[str] = "Synthetic.cnote",
                  manifest: Optional[Dict[str, Any]] = None, extra: Sequence[Tuple[str, bytes]] = ()) -> bytes:
    """A zipped format-2 package (``folder=None``: the files without the ``X.cnote/`` folder)."""
    prefix = f"{folder}/" if folder else ""
    if manifest is None:
        manifest = {"format": 2, "minReader": 2, "writer": "CollaNote 4.3.3 (2)", "writtenAt": 811715440.0,
                    "pageCount": len(pages), "pdfCount": len(pdfs or {}), "audioCount": 0}
    members: List[Tuple[str, bytes]] = [(prefix + "manifest.cnm", _json(manifest))]
    if note is not None:
        members.append((prefix + "basenote.cdat", _json(note)))
    members += [(f"{prefix}{i}.cpage", _json(p)) for i, p in enumerate(pages)]
    members += [(f"{prefix}{i}.pdf", data) for i, data in (pdfs or {}).items()]
    return _zip(members + [(prefix + name, data) for name, data in extra])


def two_page_pdf(sizes: Sequence[Tuple[float, float]] = ((960, 540), (720, 540))) -> bytes:
    """A minimal multi-page PDF with exact cross-reference offsets."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(len(sizes)))
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {len(sizes)} >>"]
    objects += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] >>" for w, h in sizes]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("ascii")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    out += b"".join(f"{o:010d} 00000 n \n".encode("ascii") for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out)
