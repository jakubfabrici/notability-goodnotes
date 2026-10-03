"""Test helper: write small OneNote sections in the OneDrive "alternative packaging"
([MS-ONESTORE] 2.7 - 2.8, [MS-FSSHTTPB] 2.2.1) from an object description, so the reader can
be tested on exact, synthetic inputs without the sample repositories.

A section is a dict of object spaces; every space lists named objects and its root objects::

    build_section({
        "section": Space(roots={1: "sec"}, objects={
            "sec": (SECTION_NODE, {ELEMENT_CHILD_NODES: ["series"]}),
            "series": (PAGE_SERIES_NODE, {CHILD_GRAPH_SPACE_ELEMENT_NODES: [Cell("page")]}),
        }),
        "page": Space(roots={1: "manifest"}, objects={...}),
    }, root="section")

Property values are encoded by the type bits of their PropertyID: ints / floats / bytes for
fixed-size data (a float becomes an f32), bytes for variable data, a ``bool`` for type 2,
object names (``str``) for object references, :class:`Cell` for object-space references,
dicts and lists of dicts for nested property sets.  :class:`Blob` objects are file data
objects (pictures) with their bytes.
"""
from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

FILE_TYPE_ONE = uuid.UUID("7B5C52E4-D88C-4DA7-AEB1-5378D02996D3").bytes_le
FORMAT_PACKAGE = uuid.UUID("638DE92F-A6D4-4BC1-9A36-B3FC2511A5B7").bytes_le
SCHEMA_ONE = uuid.UUID("1F937CB4-B26F-445F-B9F8-17E20160E461").bytes_le
CONTEXT_GUID = uuid.UUID("84DEFAB9-AAA3-4A0D-A3A8-520C77AC7073").bytes_le
ROOT_ROLES = uuid.UUID("4A3717F8-1C14-49E7-9526-81D942DE1741").bytes_le
OBJECTS_GUID = uuid.UUID("11111111-2222-3333-4444-555555555555").bytes_le
SPACES_GUID = uuid.UUID("66666666-7777-8888-9999-AAAAAAAAAAAA").bytes_le
ELEMENTS_GUID = uuid.UUID("BBBBBBBB-CCCC-DDDD-EEEE-FFFFFFFFFFFF").bytes_le


@dataclass(frozen=True)
class Cell:
    """A reference to another object space of the section (by its name)."""

    space: str


@dataclass
class Blob:
    """A file data object (jcidPictureContainer14 by default) holding ``data``."""

    data: bytes
    jcid: int = 0x00080039
    length_prefix: bool = True  # OneDrive writes a compact-integer length before the bytes


@dataclass
class Space:
    roots: Dict[int, str]
    objects: Dict[str, Any] = field(default_factory=dict)  # name -> (jcid, props) | Blob


# ----------------------------------------------------------------------------------- primitives


def cu64(v: int) -> bytes:
    if v == 0:
        return b"\x00"
    for width in range(1, 8):
        if v < 1 << (7 * width):
            return ((v << width) | (1 << (width - 1))).to_bytes(width, "little")
    return b"\x80" + v.to_bytes(8, "little")


def ext_guid(guid: bytes, n: int) -> bytes:
    if guid == bytes(16) and n == 0:
        return b"\x00"
    if n < 32:
        return bytes([(n << 3) | 4]) + guid
    if n < 1024:
        return ((n << 6) | 0x20).to_bytes(2, "little") + guid
    if n < 1 << 17:
        return ((n << 7) | 0x40).to_bytes(3, "little") + guid
    return b"\x80" + n.to_bytes(4, "little") + guid


def stream_object(typ: int, payload: bytes, children: Optional[List[bytes]] = None) -> bytes:
    """A stream object: 16-bit header when it fits, else 32-bit; compound when it has children."""
    compound = children is not None
    if typ <= 0x3F and len(payload) <= 127:
        out = struct.pack("<H", (int(compound) << 2) | (typ << 3) | (len(payload) << 9))
    elif len(payload) < 0x7FFF:
        out = struct.pack("<I", 2 | (int(compound) << 2) | (typ << 3) | (len(payload) << 17))
    else:
        out = struct.pack("<I", 2 | (int(compound) << 2) | (typ << 3) | (0x7FFF << 17)) + cu64(len(payload))
    out += payload
    if compound:
        out += b"".join(children or [])
        out += bytes([(typ << 2) | 1]) if typ <= 0x3F else struct.pack("<H", (typ << 2) | 3)
    return out


def data_element(n: int, dtype: int, children: List[bytes]) -> bytes:
    header = ext_guid(ELEMENTS_GUID, n) + b"\x00" + cu64(dtype)  # id, null serial, type
    return stream_object(0x01, header, children)


# ----------------------------------------------------------------------------------- property sets


def _propset(props: Dict[int, Any], oids: List[bytes], cells: List[bytes]) -> bytes:
    prids = b""
    data = b""
    for pid, value in props.items():
        kind = (pid >> 26) & 0x1F
        prid = pid
        if kind == 0x2:
            prid = pid | (0x80000000 if value else 0)
        elif kind == 0x3:
            data += struct.pack("<B", value)
        elif kind == 0x4:
            data += struct.pack("<H", value)
        elif kind == 0x5:
            data += value if isinstance(value, bytes) else (
                struct.pack("<f", value) if isinstance(value, float) else struct.pack("<I", value))
        elif kind == 0x6:
            data += value if isinstance(value, bytes) else struct.pack("<Q", value)
        elif kind == 0x7:
            data += struct.pack("<I", len(value)) + value
        elif kind == 0x8:
            oids.append(value)
        elif kind == 0x9:
            data += struct.pack("<I", len(value))
            oids.extend(value)
        elif kind == 0xA:
            cells.append(value)
        elif kind == 0xB:
            data += struct.pack("<I", len(value))
            cells.extend(value)
        elif kind == 0x10:
            data += struct.pack("<I", len(value))
            if value:
                data += struct.pack("<I", 0x11 << 26)
                for nested in value:
                    data += _propset(nested, oids, cells)
        elif kind == 0x11:
            data += _propset(value, oids, cells)
        else:
            raise ValueError(f"cannot encode property type {kind:#x}")
        prids += struct.pack("<I", prid)
    return struct.pack("<H", len(props)) + prids + data


def object_propset(props: Dict[int, Any]) -> Tuple[bytes, List[Any], List[Any]]:
    """ObjectSpaceObjectPropSet bytes plus the referenced objects and spaces, in order."""
    oids: List[Any] = []
    cells: List[Any] = []
    body = _propset(props, oids, cells)
    streams = struct.pack("<I", len(oids) | (0 if cells else 0x80000000))
    streams += b"".join(struct.pack("<I", 0 if r is None else ((i + 1) << 8) | 1) for i, r in enumerate(oids))
    if cells:
        streams += struct.pack("<I", len(cells)) + b"".join(struct.pack("<I", ((i + 1) << 8) | 2)
                                                            for i in range(len(cells)))
    return streams + body, oids, cells


# ----------------------------------------------------------------------------------- the file


def build_section(spaces: Dict[str, Space], root: str, *, extra_root_roles: Optional[Dict[str, Dict[int, str]]] = None,
                  fragment: Optional[int] = None) -> bytes:
    """A complete ``.one`` in the alternative packaging.

    ``extra_root_roles`` adds root declares (e.g. role 3, the encryption key root);
    ``fragment`` splits every data element larger than that many bytes into fragments.
    """
    names: Dict[str, int] = {}

    def oid(name: Optional[str]) -> bytes:
        if name is None:  # a null reference
            return b"\x00"
        names.setdefault(name, len(names) + 1)
        return ext_guid(OBJECTS_GUID, names[name])

    space_ids = {name: i + 1 for i, name in enumerate(spaces)}

    def cell(space_name: str) -> bytes:
        return ext_guid(CONTEXT_GUID, 1) + ext_guid(SPACES_GUID, space_ids[space_name])

    elements: List[bytes] = []
    serial = iter(range(1, 1_000_000))
    index_children: List[bytes] = []
    storage_children = [stream_object(0x0C, SCHEMA_ONE),
                        stream_object(0x07, ext_guid(CONTEXT_GUID, 2) + cell(root))]
    for space_name, space in spaces.items():
        manifest_n, revision_n, mapping_n, group_n = (next(serial) for _ in range(4))
        rid = ext_guid(ELEMENTS_GUID, 5000 + revision_n)
        declarations: List[bytes] = []
        datas: List[bytes] = []
        for name, spec in space.objects.items():
            if isinstance(spec, Blob):
                blob_n = next(serial)
                payload = (cu64(len(spec.data)) if spec.length_prefix else b"") + spec.data
                elements.append(data_element(blob_n, 0x0A, [stream_object(0x02, payload)]))
                for partition, data in ((4, struct.pack("<I", spec.jcid)), (1, object_propset({})[0])):
                    declarations.append(stream_object(0x18, oid(name) + cu64(partition) + cu64(len(data)) + b"\x00\x00"))
                    datas.append(stream_object(0x16, b"\x00\x00" + cu64(len(data)) + data))
                declarations.append(stream_object(0x05, oid(name) + ext_guid(ELEMENTS_GUID, blob_n) + cu64(2)
                                                  + b"\x00\x00"))
                datas.append(stream_object(0x1C, b"\x00\x00" + ext_guid(ELEMENTS_GUID, blob_n)))
                continue
            jcid, props = spec
            body, refs, spaces_used = object_propset(props)
            ref_array = cu64(len(refs)) + b"".join(oid(r) for r in refs)
            cell_array = cu64(len(spaces_used)) + b"".join(cell(c.space) for c in spaces_used)
            for partition, data, arrays in ((4, struct.pack("<I", jcid), b"\x00\x00"), (1, body, ref_array + cell_array)):
                declarations.append(stream_object(0x18, oid(name) + cu64(partition) + cu64(len(data))
                                                  + cu64(len(refs) if partition == 1 else 0)
                                                  + cu64(len(spaces_used) if partition == 1 else 0)))
                datas.append(stream_object(0x16, arrays + cu64(len(data)) + data))
        group = data_element(group_n, 0x05, [stream_object(0x1D, b"", declarations), stream_object(0x1E, b"", datas)])
        roots = dict(space.roots)
        roots.update((extra_root_roles or {}).get(space_name, {}))
        revision_children = [stream_object(0x1A, rid + b"\x00")]
        revision_children += [stream_object(0x0A, ext_guid(ROOT_ROLES, role) + oid(obj)) for role, obj in roots.items()]
        revision_children.append(stream_object(0x19, ext_guid(ELEMENTS_GUID, group_n)))
        elements += [group, data_element(revision_n, 0x04, revision_children),
                     data_element(manifest_n, 0x03, [stream_object(0x0B, rid)])]
        index_children.append(stream_object(0x0E, cell(space_name) + ext_guid(ELEMENTS_GUID, manifest_n) + b"\x00"))
        index_children.append(stream_object(0x0D, rid + ext_guid(ELEMENTS_GUID, revision_n) + b"\x00"))
        del mapping_n
    elements.insert(0, data_element(next(serial), 0x01, index_children))
    elements.insert(1, data_element(next(serial), 0x02, storage_children))
    if fragment:
        split: List[bytes] = []
        for element in elements:
            if len(element) <= fragment:
                split.append(element)
                continue
            target = ext_guid(ELEMENTS_GUID, 90000 + next(serial))
            for start in range(0, len(element), fragment):
                chunk = element[start:start + fragment]
                body = target + cu64(len(element)) + cu64(start) + cu64(len(chunk)) + chunk
                split.append(data_element(next(serial), 0x06, [stream_object(0x6A, body)]))
        elements = split
    package = stream_object(0x15, b"\x00", elements)
    packaging = stream_object(0x7A, ext_guid(ELEMENTS_GUID, 1) + SCHEMA_ONE, [package])
    header = FILE_TYPE_ONE + uuid.UUID(int=1).bytes_le + uuid.UUID(int=2).bytes_le + FORMAT_PACKAGE + bytes(4)
    return header + packaging


# ----------------------------------------------------------------------------------- content helpers


def ink_path(*channels: List[int]) -> bytes:
    """InkPath bytes: every channel delta-coded, dimension-major, signed varints."""
    values: List[int] = []
    for channel in channels:
        previous = 0
        for i, v in enumerate(channel):
            values.append(v if i == 0 else v - previous)
            previous = v

    def varint(u: int) -> bytes:
        out = b""
        while True:
            byte = u & 0x7F
            u >>= 7
            if u:
                out += bytes([byte | 0x80])
            else:
                return out + bytes([byte])

    return varint(2 * len(values)) + b"".join(varint((-v << 1) | 1 if v < 0 else v << 1) for v in values)


def ink_dimensions(pressure: bool = False) -> bytes:
    def entry(guid: str, lo: int, hi: int, unit: int, resolution: float) -> bytes:
        return uuid.UUID(guid).bytes_le + struct.pack("<iiIf", lo, hi, unit, resolution)

    out = entry("598A6A8F-52C0-4BA0-93AF-AF357411A561", -(1 << 31), (1 << 31) - 1, 2, 1000.0)
    out += entry("B53F9F75-04E0-4498-A7EE-C30DBB5A9011", -(1 << 31), (1 << 31) - 1, 2, 1000.0)
    if pressure:
        out += entry("7307502D-F9F4-4E18-B3F2-2CE1B1A3610C", 0, 32767, 0, 1.0)
    return out


def utf16z(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"
