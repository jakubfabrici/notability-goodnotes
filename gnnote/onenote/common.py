"""Shared pieces of the OneNote container readers: errors, bounded byte access, extended
GUIDs, property sets ([MS-ONESTORE] 2.6) and the object-space interface both packagings
implement.

Everything here is written from the published [MS-ONESTORE] specification (Open
Specification Promise).  Every read is bounded by the bytes that are really there: a count
or length taken from the file is checked against the remaining input before anything is
allocated, so a crafted file cannot make the reader allocate more than a small multiple of
its own size.
"""
from __future__ import annotations

import struct
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "OneNoteError", "Damaged", "Encrypted", "ExtGuid", "NIL", "guid_bytes", "guid_str", "Buf",
    "Obj", "ObjectSpace", "Store", "decode_object_propset", "MAX_PROPSET_DEPTH",
    "FILE_TYPE_ONE", "FILE_TYPE_ONETOC2", "FORMAT_NATIVE", "FORMAT_PACKAGE",
]


class OneNoteError(ValueError):
    """The input is not a OneNote file, or it cannot be read at all."""


class Damaged(OneNoteError):
    """A structure inside a OneNote file points outside the file or contradicts itself."""


class Encrypted(OneNoteError):
    """The section is password protected; its content is never decoded."""


# An extended GUID ([MS-ONESTORE] 2.2.1): the 16 GUID bytes as stored (little-endian
# fields, i.e. ``uuid.UUID(...).bytes_le``) and the 32-bit ``n``.
ExtGuid = Tuple[bytes, int]
NIL: ExtGuid = (bytes(16), 0)


def guid_bytes(text: str) -> bytes:
    """``"{7B5C52E4-...}"`` -> the 16 bytes as stored in the file."""
    return uuid.UUID(text.strip("{}")).bytes_le


def guid_str(raw: bytes) -> str:
    try:
        return "{" + str(uuid.UUID(bytes_le=bytes(raw))).upper() + "}"
    except ValueError:
        return repr(raw)


# Header GUIDs ([MS-ONESTORE] 2.3.1 and 2.8.1).
FILE_TYPE_ONE = guid_bytes("{7B5C52E4-D88C-4DA7-AEB1-5378D02996D3}")
FILE_TYPE_ONETOC2 = guid_bytes("{43FF2FA1-EFD9-4C76-9EE2-10EA5722765F}")
FORMAT_NATIVE = guid_bytes("{109ADD3F-911B-49F5-A5D0-1791EDC8AED8}")
FORMAT_PACKAGE = guid_bytes("{638DE92F-A6D4-4BC1-9A36-B3FC2511A5B7}")

MAX_PROPSET_DEPTH = 16  # nested property sets (0x10 / 0x11); real files use 2-3 levels


class Buf:
    """A cursor over ``data[pos:end]`` whose every read is bounds-checked (:class:`Damaged`)."""

    __slots__ = ("data", "pos", "end")

    def __init__(self, data: Any, pos: int = 0, end: Optional[int] = None):
        size = len(data)
        end = size if end is None else end
        if not (0 <= pos <= end <= size):
            raise Damaged(f"reference {pos}..{end} lies outside the {size}-byte file")
        self.data = data
        self.pos = pos
        self.end = end

    def remaining(self) -> int:
        return self.end - self.pos

    def need(self, n: int) -> None:
        if n < 0 or self.pos + n > self.end:
            raise Damaged(f"structure truncated at byte {self.pos} (needs {n} more bytes)")

    def skip(self, n: int) -> None:
        self.need(n)
        self.pos += n

    def u8(self) -> int:
        self.need(1)
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        self.need(2)
        v = int.from_bytes(self.data[self.pos:self.pos + 2], "little")
        self.pos += 2
        return v

    def u32(self) -> int:
        self.need(4)
        v = int.from_bytes(self.data[self.pos:self.pos + 4], "little")
        self.pos += 4
        return v

    def u64(self) -> int:
        self.need(8)
        v = int.from_bytes(self.data[self.pos:self.pos + 8], "little")
        self.pos += 8
        return v

    def take(self, n: int) -> bytes:
        self.need(n)
        v = bytes(self.data[self.pos:self.pos + n])
        self.pos += n
        return v

    def guid(self) -> bytes:
        return self.take(16)

    def ext_guid20(self) -> ExtGuid:
        """The fixed 20-byte ExtendedGUID of the native format ([MS-ONESTORE] 2.2.1)."""
        g = self.guid()
        return (g, self.u32())


# ----------------------------------------------------------------------------------
# Objects and object spaces (the interface the MS-ONE layer uses)
# ----------------------------------------------------------------------------------


class Obj:
    """One object of an object space: its JCID and decoded properties.

    ``props`` maps a PropertyID (with the Boolean value bit cleared, i.e. the 31-bit values
    the [MS-ONE] property table lists) to its value: ``None`` (no data), ``bool``, ``bytes``
    (fixed-size and variable-size data), an object reference (:data:`ExtGuid` or ``None``), an
    object-space or context reference (opaque, resolved by the store), a ``list`` of
    references, a nested property-set ``dict`` or a ``list`` of such dicts.
    """

    __slots__ = ("oid", "jcid", "props")

    def __init__(self, oid: ExtGuid, jcid: int, props: Dict[int, Any]):
        self.oid = oid
        self.jcid = jcid
        self.props = props

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Obj({guid_str(self.oid[0])},{self.oid[1]} jcid={self.jcid:#010x} props={len(self.props)})"


class ObjectSpace:
    """The current revision (default context, revision role 1) of one object space."""

    def __init__(self, key: Any):
        self.key = key
        self.roots: Dict[int, ExtGuid] = {}  # root role -> object id
        self.warnings: List[str] = []

    def get(self, oid: Optional[ExtGuid]) -> Optional[Obj]:  # pragma: no cover - interface
        raise NotImplementedError

    def file_data(self, oid: Optional[ExtGuid]) -> Optional[bytes]:  # pragma: no cover - interface
        """The bytes of a file data object (a picture or embedded file), ``None`` if absent."""
        raise NotImplementedError

    def root(self, role: int = 1) -> Optional[Obj]:
        return self.get(self.roots.get(role))


class Store:
    """A whole revision-store file, whatever its packaging."""

    file_type: bytes = FILE_TYPE_ONE
    encrypted: bool = False

    def __init__(self) -> None:
        self.warnings: List[str] = []

    @property
    def is_toc(self) -> bool:
        return self.file_type == FILE_TYPE_ONETOC2

    def root_space(self) -> Optional[ObjectSpace]:  # pragma: no cover - interface
        raise NotImplementedError

    def space(self, ref: Any) -> Optional[ObjectSpace]:  # pragma: no cover - interface
        """The object space an ObjectSpaceID property refers to."""
        raise NotImplementedError

    def file_identity(self) -> Optional[bytes]:  # pragma: no cover - interface
        """``Header.guidFile`` of the revision store (what a ``.onetoc2`` uses to name it)."""
        raise NotImplementedError


# ----------------------------------------------------------------------------------
# ObjectSpaceObjectPropSet / PropertySet ([MS-ONESTORE] 2.6.1 - 2.6.9)
# ----------------------------------------------------------------------------------

_FIXED_SIZES = {0x3: 1, 0x4: 2, 0x5: 4, 0x6: 8}


def _stream_header(buf: Buf) -> Tuple[List[int], bool, bool]:
    """One ObjectSpaceObjectStream (header + CompactIDs): (ids, extended, osid_absent)."""
    header = buf.u32()
    count = header & 0xFFFFFF
    buf.need(4 * count)
    data, pos = buf.data, buf.pos
    ids = [int.from_bytes(data[pos + 4 * i:pos + 4 * i + 4], "little") for i in range(count)]
    buf.pos = pos + 4 * count
    return ids, bool(header & 0x40000000), bool(header & 0x80000000)


Resolver = Callable[[List[int], List[int], List[int]], Tuple[List[Any], List[Any], List[Any]]]


def decode_object_propset(data: Any, start: int, end: int, resolve: Resolver) -> Dict[int, Any]:
    """Decode an ObjectSpaceObjectPropSet held in ``data[start:end]``.

    ``resolve(oid_cids, osid_cids, context_cids)`` turns the CompactIDs of the OIDs, OSIDs
    and ContextIDs streams into references: the native packaging looks them up in a global
    identification table, the FSSHTTPB packaging pairs them with the Object Data arrays
    ([MS-ONESTORE] 2.7.8).
    """
    buf = Buf(data, start, end)
    oid_ids, _ext, osid_absent = _stream_header(buf)
    osid_ids: List[int] = []
    ctx_ids: List[int] = []
    if not osid_absent:
        osid_ids, ext2, _ = _stream_header(buf)
        if ext2:
            ctx_ids, _, _ = _stream_header(buf)
    oids, osids, ctxs = resolve(oid_ids, osid_ids, ctx_ids)
    return _decode_propset(buf, (_Refs(oids), _Refs(osids), _Refs(ctxs)), 0)


class _Refs:
    """Sequential consumer of one reference stream."""

    __slots__ = ("items", "pos")

    def __init__(self, items: Sequence[Any]):
        self.items = items
        self.pos = 0

    def one(self) -> Any:
        if self.pos >= len(self.items):
            raise Damaged("property set references more objects than its stream holds")
        v = self.items[self.pos]
        self.pos += 1
        return v

    def many(self, n: int) -> List[Any]:
        if n < 0 or self.pos + n > len(self.items):
            raise Damaged("property set references more objects than its stream holds")
        v = list(self.items[self.pos:self.pos + n])
        self.pos += n
        return v


def _decode_propset(buf: Buf, streams: Tuple[_Refs, _Refs, _Refs], depth: int) -> Dict[int, Any]:
    if depth > MAX_PROPSET_DEPTH:
        raise Damaged("property sets nested too deeply")
    count = buf.u16()
    buf.need(4 * count)
    data, pos = buf.data, buf.pos
    prids = [int.from_bytes(data[pos + 4 * i:pos + 4 * i + 4], "little") for i in range(count)]
    buf.pos = pos + 4 * count
    out: Dict[int, Any] = {}
    oids, osids, ctxs = streams
    for prid in prids:
        kind = (prid >> 26) & 0x1F
        key = prid & 0x7FFFFFFF
        value: Any
        if kind == 0x1:
            value = None
        elif kind == 0x2:
            value = bool(prid & 0x80000000)
        elif kind in _FIXED_SIZES:
            value = buf.take(_FIXED_SIZES[kind])
        elif kind == 0x7:
            value = buf.take(buf.u32())
        elif kind == 0x8:
            value = oids.one()
        elif kind == 0x9:
            value = oids.many(buf.u32())
        elif kind == 0xA:
            value = osids.one()
        elif kind == 0xB:
            value = osids.many(buf.u32())
        elif kind == 0xC:
            value = ctxs.one()
        elif kind == 0xD:
            value = ctxs.many(buf.u32())
        elif kind == 0x10:
            n = buf.u32()
            value = []
            if n:
                buf.u32()  # the element PropertyID (type 0x11)
                if n > buf.remaining() // 2:  # every nested set is at least 2 bytes
                    raise Damaged("property value array longer than its data")
                for _ in range(n):
                    value.append(_decode_propset(buf, streams, depth + 1))
        elif kind == 0x11:
            value = _decode_propset(buf, streams, depth + 1)
        else:
            raise Damaged(f"unknown property type {kind:#x} in property {prid:#010x}")
        out[key] = value
    return out


# ----------------------------------------------------------------------------------
# Typed property access (tolerant: a value of the wrong shape reads as absent)
# ----------------------------------------------------------------------------------


def p_bytes(props: Dict[int, Any], pid: int) -> Optional[bytes]:
    v = props.get(pid)
    return v if isinstance(v, (bytes, bytearray)) else None


def p_u8(props: Dict[int, Any], pid: int) -> Optional[int]:
    v = p_bytes(props, pid)
    return v[0] if v else None


def p_u16(props: Dict[int, Any], pid: int) -> Optional[int]:
    v = p_bytes(props, pid)
    return int.from_bytes(v[:2], "little") if v is not None and len(v) >= 2 else None


def p_u32(props: Dict[int, Any], pid: int) -> Optional[int]:
    v = p_bytes(props, pid)
    return int.from_bytes(v[:4], "little") if v is not None and len(v) >= 4 else None


def p_f32(props: Dict[int, Any], pid: int) -> Optional[float]:
    v = p_bytes(props, pid)
    if v is None or len(v) != 4:
        return None
    f = struct.unpack("<f", v)[0]
    return f if f == f and abs(f) != float("inf") else None


def p_bool(props: Dict[int, Any], pid: int) -> bool:
    v = props.get(pid)
    return v is True


def p_ref(props: Dict[int, Any], pid: int) -> Any:
    v = props.get(pid)
    return None if isinstance(v, (list, dict, bytes, bool)) else v


def p_refs(props: Dict[int, Any], pid: int) -> List[Any]:
    v = props.get(pid)
    if isinstance(v, list):
        return [x for x in v if x is not None and not isinstance(x, (dict, list))]
    if v is None or isinstance(v, (dict, bytes, bool)):
        return []
    return [v]


def p_text(props: Dict[int, Any], pid: int) -> Optional[str]:
    """A UTF-16LE string property (WzInAtom and friends), without trailing NULs."""
    v = p_bytes(props, pid)
    if v is None:
        return None
    if len(v) % 2:
        v = v[:-1]
    return v.decode("utf-16-le", "replace").rstrip("\x00")


def p_u32_array(props: Dict[int, Any], pid: int) -> List[int]:
    v = p_bytes(props, pid)
    if not v:
        return []
    return [int.from_bytes(v[i:i + 4], "little") for i in range(0, len(v) - len(v) % 4, 4)]


def p_sets(props: Dict[int, Any], pid: int) -> List[Dict[int, Any]]:
    v = props.get(pid)
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []
