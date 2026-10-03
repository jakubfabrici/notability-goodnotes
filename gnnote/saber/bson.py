"""The subset of BSON (bsonspec.org) that Saber notes use, with a bounded decoder.

Adapted from inkterop's stdlib BSON walker (MIT, see ``NOTICE.md``) and hardened: every
length is checked against the enclosing document before it is used, nesting is limited to
:data:`MAX_DEPTH` and the number of decoded values to :data:`MAX_VALUES`, so a crafted file
costs bounded time and memory.  Types: double, string, document, array, binary, bool, null,
int32, int64 (decoding also accepts UTC datetime, timestamp and ObjectId, returned raw).
"""
from __future__ import annotations

import math
import re
import struct
from typing import Any, Dict, List, Tuple

__all__ = ["BsonError", "decode", "encode", "MAX_DEPTH", "MAX_VALUES"]

MAX_DEPTH = 32
MAX_VALUES = 5_000_000  # decoded values per file (a point is one value)
_I32 = struct.Struct("<i")
_I64 = struct.Struct("<q")
_F64 = struct.Struct("<d")
_SURROGATES = re.compile("[\ud800-\udfff]")


class BsonError(ValueError):
    """The data is not a well-formed BSON document (or exceeds the limits)."""


class _Decoder:
    def __init__(self, data: bytes):
        self.data = data
        self.values = 0

    def document(self, pos: int, end_limit: int, depth: int, as_list: bool) -> Tuple[Any, int]:
        data = self.data
        if depth > MAX_DEPTH:
            raise BsonError(f"documents nested deeper than {MAX_DEPTH} levels")
        if pos + 5 > end_limit:
            raise BsonError("truncated document")
        size = _I32.unpack_from(data, pos)[0]
        end = pos + size
        if size < 5 or end > end_limit or data[end - 1] != 0:
            raise BsonError("bad document length")
        pos += 4
        out_list: List[Any] = []
        out_dict: Dict[str, Any] = {}
        while pos < end - 1:
            kind = data[pos]
            name_end = data.find(b"\x00", pos + 1, end - 1)
            if name_end < 0:
                raise BsonError("unterminated element name")
            name = data[pos + 1:name_end].decode("utf-8", "replace")
            pos = name_end + 1
            self.values += 1
            if self.values > MAX_VALUES:
                raise BsonError(f"more than {MAX_VALUES} values")
            if kind == 0x01:
                if pos + 8 > end:
                    raise BsonError("truncated double")
                value: Any = _F64.unpack_from(data, pos)[0]
                pos += 8
            elif kind == 0x02:
                if pos + 4 > end:
                    raise BsonError("truncated string")
                n = _I32.unpack_from(data, pos)[0]
                if n < 1 or pos + 4 + n > end or data[pos + 3 + n] != 0:
                    raise BsonError("bad string length")
                value = data[pos + 4:pos + 3 + n].decode("utf-8", "replace")
                pos += 4 + n
            elif kind in (0x03, 0x04):
                value, pos = self.document(pos, end - 1, depth + 1, kind == 0x04)
            elif kind == 0x05:
                if pos + 5 > end:
                    raise BsonError("truncated binary")
                n = _I32.unpack_from(data, pos)[0]
                if n < 0 or pos + 5 + n > end:
                    raise BsonError("bad binary length")
                value = data[pos + 5:pos + 5 + n]
                pos += 5 + n
            elif kind == 0x07:  # ObjectId
                if pos + 12 > end:
                    raise BsonError("truncated ObjectId")
                value = data[pos:pos + 12]
                pos += 12
            elif kind == 0x08:
                if pos + 1 > end:
                    raise BsonError("truncated boolean")
                value = data[pos] != 0
                pos += 1
            elif kind == 0x0A:
                value = None
            elif kind == 0x10:
                if pos + 4 > end:
                    raise BsonError("truncated int32")
                value = _I32.unpack_from(data, pos)[0]
                pos += 4
            elif kind in (0x09, 0x11, 0x12):  # datetime, timestamp, int64
                if pos + 8 > end:
                    raise BsonError("truncated int64")
                value = _I64.unpack_from(data, pos)[0]
                pos += 8
            else:
                raise BsonError(f"unsupported BSON type 0x{kind:02x} for {name!r}")
            if as_list:
                out_list.append(value)
            else:
                out_dict[name] = value
        if pos != end - 1:
            raise BsonError("element overruns its document")
        return (out_list if as_list else out_dict), end


def decode(data: bytes) -> Dict[str, Any]:
    """Decode one top-level BSON document; :class:`BsonError` when it is malformed."""
    if len(data) < 5:
        raise BsonError("too short for a BSON document")
    size = _I32.unpack_from(data, 0)[0]
    if size < 5 or size > len(data):
        raise BsonError("bad document length")
    try:
        value, _end = _Decoder(data).document(0, size, 0, False)
    except (struct.error, IndexError) as exc:  # pragma: no cover - lengths are checked first
        raise BsonError(f"truncated document ({exc})") from None
    return value


def _cstring(name: str) -> bytes:
    raw = _SURROGATES.sub("�", name).encode("utf-8")
    if b"\x00" in raw:
        raise BsonError(f"element name {name!r} contains NUL")
    return raw + b"\x00"


def _element(name: str, value: Any) -> bytes:
    key = _cstring(name)
    if isinstance(value, bool):  # before int: bool is an int subclass
        return b"\x08" + key + (b"\x01" if value else b"\x00")
    if value is None:
        return b"\x0a" + key
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BsonError(f"non-finite double for {name!r}")
        return b"\x01" + key + _F64.pack(value)
    if isinstance(value, int):
        if -(2 ** 31) <= value < 2 ** 31:
            return b"\x10" + key + _I32.pack(value)
        return b"\x12" + key + _I64.pack(value)
    if isinstance(value, str):
        raw = _SURROGATES.sub("�", value).encode("utf-8")
        return b"\x02" + key + _I32.pack(len(raw) + 1) + raw + b"\x00"
    if isinstance(value, (bytes, bytearray)):
        return b"\x05" + key + _I32.pack(len(value)) + b"\x00" + bytes(value)
    if isinstance(value, dict):
        return b"\x03" + key + encode(value)
    if isinstance(value, (list, tuple)):
        return b"\x04" + key + encode({str(i): v for i, v in enumerate(value)})
    raise BsonError(f"cannot encode {type(value).__name__} for {name!r}")


def encode(document: Dict[str, Any]) -> bytes:
    """Encode a dict (insertion order kept) as one BSON document.  Python ``int`` becomes
    int32 when it fits and int64 otherwise (as Saber's own encoder stores ARGB colours),
    ``float`` a double, ``bytes`` binary subtype 0, lists arrays."""
    body = b"".join(_element(str(k), v) for k, v in document.items())
    return _I32.pack(len(body) + 5) + body + b"\x00"
