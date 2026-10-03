"""Protocol-buffer wire format and length-prefixed record streams (schema-free).

GoodNotes stores every ``.pb`` member and every ``notes/<UUID>`` page as plain protobuf
wire data without a published schema, so this module works on the *wire* level only:
a message is a flat list of :class:`Field` triples, nested messages are just ``bytes``
values that the caller decodes again when it knows they are messages.

Wire layout implemented (``docs/goodnotes-container.md`` section 2):

* ``key = varint((field_number << 3) | wire_type)``.
* varint: base-128, little-endian groups of 7 bits, bit 7 = continuation, at most 10 bytes.
* wire type 0 (varint): ``Field.value`` is the unsigned ``int`` as stored.
* wire type 1 (fixed64): 8 little-endian bytes, kept raw as ``bytes`` (``fixed64_float`` /
  ``fixed64_uint`` interpret them).
* wire type 2 (length-delimited): ``varint length`` + payload, kept raw as ``bytes``
  (a string, a nested message or opaque data).
* wire type 5 (fixed32): 4 little-endian bytes, kept raw as ``bytes`` (``fixed32_float``
  interprets them as IEEE-754 float32, which is how GoodNotes stores colours, sizes and
  coordinates).
* wire types 3/4 (groups) and 6/7 never occur in the files and are rejected.

Record streams: ``index.notes.pb``, ``index.attachments.pb``, ``index.events.pb``,
``index.search.pb``, ``search/<UUID>`` and ``notes/<UUID>`` are a concatenation of
``<varint L><L bytes>`` records, not one message; ``decode_records`` / ``encode_records``
handle that framing.  A 0-byte stream is valid and empty.

Every malformed input (truncated varint or payload, bad wire type, field number 0, an
over-long varint, a record that runs past the end) raises ``ValueError`` -- nothing is
accepted silently.  ``try_decode_message`` is the tolerant wrapper for "is this payload a
message?" probing.
"""
from __future__ import annotations

import struct
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple, Union

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5

_MAX_VARINT_BYTES = 10
_U64_MASK = (1 << 64) - 1


class Field(NamedTuple):
    """One decoded wire field.  ``value`` is ``int`` for varints and ``bytes`` otherwise."""

    number: int
    wire_type: int
    value: Union[int, bytes]


# --------------------------------------------------------------------------- varints


def read_varint(data: bytes, pos: int = 0) -> Tuple[int, int]:
    """Decode one varint at ``pos``; return ``(value, position after it)``."""
    value = 0
    shift = 0
    n = len(data)
    start = pos
    while True:
        if pos >= n:
            raise ValueError(f"truncated varint at offset {start}")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if pos - start >= _MAX_VARINT_BYTES:
            raise ValueError(f"varint longer than {_MAX_VARINT_BYTES} bytes at offset {start}")


def varint(n: int) -> bytes:
    """Encode an integer as a varint.  Negative values use 64-bit two's complement."""
    if n < 0:
        n &= _U64_MASK
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


# --------------------------------------------------------------------------- decoding


def decode_message(data: bytes) -> List[Field]:
    """Decode a whole message into its fields, in file order.  Raises ``ValueError``."""
    fields: List[Field] = []
    pos = 0
    n = len(data)
    while pos < n:
        key, pos = read_varint(data, pos)
        number = key >> 3
        wire_type = key & 7
        if number == 0:
            raise ValueError(f"field number 0 at offset {pos}")
        if wire_type == WIRE_VARINT:
            value, pos = read_varint(data, pos)
            fields.append(Field(number, wire_type, value))
        elif wire_type == WIRE_LEN:
            length, pos = read_varint(data, pos)
            end = pos + length
            if end > n:
                raise ValueError(f"field {number}: length {length} runs past the end at offset {pos}")
            fields.append(Field(number, wire_type, bytes(data[pos:end])))
            pos = end
        elif wire_type == WIRE_FIXED32:
            end = pos + 4
            if end > n:
                raise ValueError(f"field {number}: truncated fixed32 at offset {pos}")
            fields.append(Field(number, wire_type, bytes(data[pos:end])))
            pos = end
        elif wire_type == WIRE_FIXED64:
            end = pos + 8
            if end > n:
                raise ValueError(f"field {number}: truncated fixed64 at offset {pos}")
            fields.append(Field(number, wire_type, bytes(data[pos:end])))
            pos = end
        else:
            raise ValueError(f"field {number}: unsupported wire type {wire_type} at offset {pos}")
    return fields


def try_decode_message(data: bytes) -> Optional[List[Field]]:
    """``decode_message`` that returns ``None`` instead of raising (for probing nested payloads)."""
    try:
        return decode_message(data)
    except ValueError:
        return None


MAX_RECORDS = 200_000  # a record stream with more entries is damaged (or a decompression bomb); the
# largest real member in the corpus (ex1.goodnotes, 5620 strokes on one page) holds 5620 records


def decode_records(data: bytes, max_records: int = MAX_RECORDS) -> List[bytes]:
    """Split a ``<varint L><L bytes>...`` record stream into its records.

    Raises ``ValueError`` when a record runs past the end or the stream holds more than
    ``max_records`` entries (a run of zero bytes would otherwise become one empty record per
    byte, so a highly compressible member could cost unbounded time and memory).
    """
    records: List[bytes] = []
    pos = 0
    n = len(data)
    while pos < n:
        length, pos = read_varint(data, pos)
        end = pos + length
        if end > n:
            raise ValueError(f"record of length {length} at offset {pos} runs past the end")
        if len(records) >= max_records:
            raise ValueError(f"record stream holds more than {max_records} records")
        records.append(bytes(data[pos:end]))
        pos = end
    return records


# --------------------------------------------------------------------------- encoding


def encode_records(records: Iterable[bytes]) -> bytes:
    """Concatenate records with their varint length prefixes."""
    return b"".join(varint(len(r)) + bytes(r) for r in records)


def _key(number: int, wire_type: int) -> bytes:
    if number < 1:
        raise ValueError(f"field number must be >= 1, got {number}")
    return varint((number << 3) | wire_type)


def field_varint(number: int, n: int) -> bytes:
    return _key(number, WIRE_VARINT) + varint(n)


def field_bytes(number: int, b: Union[bytes, bytearray, memoryview, str]) -> bytes:
    """Length-delimited field; a ``str`` is UTF-8 encoded."""
    payload = b.encode("utf-8") if isinstance(b, str) else bytes(b)
    return _key(number, WIRE_LEN) + varint(len(payload)) + payload


def field_message(number: int, body: bytes) -> bytes:
    """Nested message field (identical to ``field_bytes``; named for readability)."""
    return field_bytes(number, body)


def field_fixed32(number: int, f: float) -> bytes:
    """fixed32 field holding an IEEE-754 float32."""
    return _key(number, WIRE_FIXED32) + struct.pack("<f", f)


def field_fixed32_bits(number: int, u: int) -> bytes:
    """fixed32 field holding a raw uint32 bit pattern."""
    return _key(number, WIRE_FIXED32) + struct.pack("<I", u)


def field_fixed64(number: int, x: Union[int, float]) -> bytes:
    """fixed64 field: a ``float`` is stored as float64, an ``int`` as uint64."""
    if isinstance(x, float):
        return _key(number, WIRE_FIXED64) + struct.pack("<d", x)
    return _key(number, WIRE_FIXED64) + struct.pack("<Q", x & _U64_MASK)


def encode_field(field: Field) -> bytes:
    """Re-encode one decoded field byte-for-byte (fixed values are kept raw)."""
    number, wire_type, value = field
    if wire_type == WIRE_VARINT:
        if not isinstance(value, int):
            raise ValueError(f"field {number}: varint value must be int")
        return field_varint(number, value)
    if wire_type == WIRE_LEN:
        if isinstance(value, int):
            raise ValueError(f"field {number}: length-delimited value must be bytes")
        return field_bytes(number, value)
    if wire_type == WIRE_FIXED32:
        if isinstance(value, int) or len(value) != 4:
            raise ValueError(f"field {number}: fixed32 value must be 4 bytes")
        return _key(number, WIRE_FIXED32) + bytes(value)
    if wire_type == WIRE_FIXED64:
        if isinstance(value, int) or len(value) != 8:
            raise ValueError(f"field {number}: fixed64 value must be 8 bytes")
        return _key(number, WIRE_FIXED64) + bytes(value)
    raise ValueError(f"field {number}: unsupported wire type {wire_type}")


def encode_message(fields: Iterable[Field]) -> bytes:
    """Re-encode a field list; ``encode_message(decode_message(x)) == x`` for valid ``x``."""
    return b"".join(encode_field(f) for f in fields)


# --------------------------------------------------------------------------- helpers


def get(fields: Sequence[Field], number: int) -> Optional[Field]:
    """First field with that number, or ``None``."""
    for f in fields:
        if f.number == number:
            return f
    return None


def get_all(fields: Sequence[Field], number: int) -> List[Field]:
    return [f for f in fields if f.number == number]


def _raw(field: Field, size: int, kind: str) -> bytes:
    if isinstance(field.value, int) or len(field.value) != size:
        raise ValueError(f"field {field.number} is not a {kind}")
    return field.value


def fixed32_float(field: Field) -> float:
    return struct.unpack("<f", _raw(field, 4, "fixed32"))[0]


def fixed32_uint(field: Field) -> int:
    return struct.unpack("<I", _raw(field, 4, "fixed32"))[0]


def fixed64_float(field: Field) -> float:
    return struct.unpack("<d", _raw(field, 8, "fixed64"))[0]


def fixed64_uint(field: Field) -> int:
    return struct.unpack("<Q", _raw(field, 8, "fixed64"))[0]


def varint_value(field: Field) -> int:
    if field.wire_type != WIRE_VARINT or not isinstance(field.value, int):
        raise ValueError(f"field {field.number} is not a varint")
    return field.value


def bytes_value(field: Field) -> bytes:
    if field.wire_type != WIRE_LEN or isinstance(field.value, int):
        raise ValueError(f"field {field.number} is not length-delimited")
    return field.value


def string_value(field: Field) -> str:
    return bytes_value(field).decode("utf-8")


def message_value(field: Field) -> List[Field]:
    """Decode a length-delimited field as a nested message (raises ``ValueError``)."""
    return decode_message(bytes_value(field))
