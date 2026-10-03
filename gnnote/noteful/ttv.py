"""Noteful's tag-type-value (TTV) records: a bounded, tolerant decoder and a strict encoder.

A record is a run of entries without a count or terminator; its end is given by the
enclosing length (a blob of the container, or the length prefix of a nested record)::

    u16 tag, u16 type, value [, u64 stamp]          all big-endian

``type`` = base type | ``LIST`` (0x0400: ``u32 count`` then that many values) | ``STAMPED``
(0x0800: a u64 timestamp in microseconds since 2001-01-01 follows the value).  Base types:

    0x01 bool (1 byte)   0x02 u64   0x03 f32   0x04 f64 date (s since 2001)
    0x05 UTF-8 string    0x06 bytes 0x07 nested record    (each: u32 length, then the bytes)
    0x11 u16   0x12 u32   0x13 u64   0x14 i32   0x20 f64   0x21 size (2 x f64)

Fixed-size types carry no length, so a value of an unknown type cannot be skipped: the
decoder stops the record it is in, keeps the entries decoded so far and notes the error.  A
nested record is length-prefixed, so its damage never spreads to the record around it.
Every count and length is checked against the bytes that remain, nesting is limited to
:data:`MAX_DEPTH` levels and one :class:`Decoder` hands out at most :data:`MAX_VALUES` values.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["BOOL", "U64", "F32", "DATE", "STRING", "BYTES", "RECORD", "U16", "U32", "U64_ALT",
           "I32", "F64", "SIZE", "LIST", "STAMPED", "MAX_DEPTH", "MAX_VALUES", "TTVError",
           "Entry", "Record", "Decoder", "encode"]

BOOL = 0x0001
U64 = 0x0002
F32 = 0x0003
DATE = 0x0004
STRING = 0x0005
BYTES = 0x0006
RECORD = 0x0007
U16 = 0x0011
U32 = 0x0012
U64_ALT = 0x0013
I32 = 0x0014
F64 = 0x0020
SIZE = 0x0021
LIST = 0x0400
STAMPED = 0x0800

MAX_DEPTH = 32  # app-written files nest at most 7 levels
MAX_VALUES = 10_000_000  # values one decoder produces; beyond this a record is treated as damaged

_FIXED: Dict[int, Tuple[str, int]] = {
    BOOL: ("B", 1), U64: ("Q", 8), F32: ("f", 4), DATE: ("d", 8), U16: ("H", 2), U32: ("I", 4),
    U64_ALT: ("Q", 8), I32: ("i", 4), F64: ("d", 8), SIZE: ("dd", 16),
}
_PREFIXED = (STRING, BYTES, RECORD)  # u32 length, then the bytes
_INTEGERS = (U64, U64_ALT, U32, U16, I32)
_FLOATS = (F64, F32, DATE)
_RANGES = {U16: (0, 0xFFFF), U32: (0, 0xFFFFFFFF), I32: (-0x80000000, 0x7FFFFFFF),
           U64: (0, 0xFFFFFFFFFFFFFFFF), U64_ALT: (0, 0xFFFFFFFFFFFFFFFF)}
_HH = struct.Struct(">HH")
_U32 = struct.Struct(">I")
_U64 = struct.Struct(">Q")


class TTVError(ValueError):
    """A record that cannot be decoded or encoded."""


@dataclass(frozen=True)
class Entry:
    type: int  # the full type word, LIST and STAMPED bits included
    value: Any  # bool / int / float / str / bytes / Record / (w, h), or a list of them
    stamp: Optional[int] = None

    @property
    def base(self) -> int:
        return self.type & ~(LIST | STAMPED)

    @property
    def is_list(self) -> bool:
        return bool(self.type & LIST)


class Record:
    """A decoded record: entries by tag (the first of duplicate tags wins) plus, when decoding
    stopped early, the reason (the entries before the damage are kept).

    The typed accessors return ``None`` (or ``default``) when the tag is absent *or* holds a
    value of another type, so a reader never sees a value it did not ask for.
    """

    __slots__ = ("entries", "error")

    def __init__(self, entries: Optional[Dict[int, Entry]] = None, error: Optional[str] = None):
        self.entries: Dict[int, Entry] = entries if entries is not None else {}
        self.error = error

    def __contains__(self, tag: int) -> bool:
        return tag in self.entries

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        inner = ", ".join(f"{t:#06x}: {e.value!r}" for t, e in self.entries.items())
        return f"Record({{{inner}}}{', error=' + repr(self.error) if self.error else ''})"

    def entry(self, tag: int) -> Optional[Entry]:
        return self.entries.get(tag)

    def stamp(self, tag: int) -> Optional[int]:
        e = self.entries.get(tag)
        return e.stamp if e is not None else None

    def _scalar(self, tag: int, bases: Sequence[int]) -> Any:
        e = self.entries.get(tag)
        if e is None or e.is_list or e.base not in bases:
            return None
        return e.value

    def _list(self, tag: int, bases: Sequence[int]) -> Optional[List[Any]]:
        e = self.entries.get(tag)
        if e is None or not e.is_list or e.base not in bases:
            return None
        return e.value

    def integer(self, tag: int, default: Optional[int] = None) -> Optional[int]:
        value = self._scalar(tag, _INTEGERS)
        return default if value is None else value

    def number(self, tag: int, default: Optional[float] = None) -> Optional[float]:
        """A float (f64, f32 or date) value; ``default`` when absent, mistyped or not finite."""
        value = self._scalar(tag, _FLOATS)
        return default if value is None or not math.isfinite(value) else value

    def string(self, tag: int, default: Optional[str] = None) -> Optional[str]:
        value = self._scalar(tag, (STRING,))
        return default if value is None else value

    def boolean(self, tag: int, default: Optional[bool] = None) -> Optional[bool]:
        value = self._scalar(tag, (BOOL,))
        return default if value is None else value

    def data(self, tag: int) -> Optional[bytes]:
        return self._scalar(tag, (BYTES,))

    def record(self, tag: int) -> Optional["Record"]:
        return self._scalar(tag, (RECORD,))

    def size(self, tag: int) -> Optional[Tuple[float, float]]:
        return self._scalar(tag, (SIZE,))

    def integers(self, tag: int) -> Optional[List[int]]:
        return self._list(tag, _INTEGERS)

    def numbers(self, tag: int) -> Optional[List[float]]:
        return self._list(tag, _FLOATS)

    def strings(self, tag: int) -> Optional[List[str]]:
        return self._list(tag, (STRING,))

    def booleans(self, tag: int) -> Optional[List[bool]]:
        return self._list(tag, (BOOL,))

    def records(self, tag: int) -> Optional[List["Record"]]:
        return self._list(tag, (RECORD,))


class Decoder:
    """Decodes records out of one buffer.

    ``errors`` collects the reason of every record that stopped early (nested ones included);
    the value budget (:data:`MAX_VALUES`) is shared by every :meth:`decode` call, so one
    decoder per file bounds the work a crafted file can cause.
    """

    def __init__(self, data: bytes, max_values: int = MAX_VALUES):
        self.data = data
        self.remaining = max_values
        self.errors: List[str] = []

    def decode(self, start: int, end: int) -> Record:
        """The record in ``data[start:end]`` (never raises; see :attr:`Record.error`)."""
        if not 0 <= start <= end <= len(self.data):
            rec = Record(error=f"record bounds {start}..{end} lie outside the {len(self.data)}-byte buffer")
            self.errors.append(rec.error or "")
            return rec
        return self._record(start, end, 0)

    # -- internals --------------------------------------------------------------------------

    def _spend(self, n: int) -> None:
        self.remaining -= n
        if self.remaining < 0:
            raise TTVError(f"more than {MAX_VALUES} values")

    def _record(self, start: int, end: int, depth: int) -> Record:
        rec = Record()
        if depth > MAX_DEPTH:
            rec.error = f"records nested deeper than {MAX_DEPTH} levels"
            self.errors.append(rec.error)
            return rec
        p = start
        while p < end:
            try:
                tag, typ, value, stamp, p = self._entry(p, end, depth)
            except TTVError as exc:
                rec.error = str(exc)
                self.errors.append(rec.error)
                break
            if tag not in rec.entries:
                rec.entries[tag] = Entry(typ, value, stamp)
        return rec

    def _entry(self, p: int, end: int, depth: int) -> Tuple[int, int, Any, Optional[int], int]:
        data = self.data
        if end - p < 4:
            raise TTVError(f"entry header cut off at offset {p}")
        tag, typ = _HH.unpack_from(data, p)
        p += 4
        base = typ & ~(LIST | STAMPED)
        if typ & LIST:
            if end - p < 4:
                raise TTVError(f"list count cut off at offset {p}")
            (count,) = _U32.unpack_from(data, p)
            value, p = self._list(base, typ, count, p + 4, end, depth)
        else:
            self._spend(1)
            value, p = self._value(base, typ, p, end, depth)
        stamp = None
        if typ & STAMPED:
            if end - p < 8:
                raise TTVError(f"timestamp cut off at offset {p}")
            (stamp,) = _U64.unpack_from(data, p)
            p += 8
        return tag, typ, value, stamp, p

    def _value(self, base: int, typ: int, p: int, end: int, depth: int) -> Tuple[Any, int]:
        data = self.data
        fixed = _FIXED.get(base)
        if fixed is not None:
            fmt, size = fixed
            if end - p < size:
                raise TTVError(f"value of type {typ:#06x} cut off at offset {p}")
            if base == BOOL:
                return data[p] != 0, p + 1
            if base == SIZE:
                return struct.unpack_from(">dd", data, p), p + 16
            return struct.unpack_from(">" + fmt, data, p)[0], p + size
        if base in _PREFIXED:
            if end - p < 4:
                raise TTVError(f"length cut off at offset {p}")
            (n,) = _U32.unpack_from(data, p)
            p += 4
            if n > end - p:
                raise TTVError(f"value of {n} bytes at offset {p} overruns its record")
            if base == STRING:
                return data[p:p + n].decode("utf-8", "replace"), p + n
            if base == BYTES:
                return bytes(data[p:p + n]), p + n
            return self._record(p, p + n, depth + 1), p + n
        raise TTVError(f"unknown value type {typ:#06x} at offset {p - 4}")

    def _list(self, base: int, typ: int, count: int, p: int, end: int, depth: int) -> Tuple[List[Any], int]:
        data = self.data
        fixed = _FIXED.get(base)
        if fixed is not None:
            fmt, size = fixed
            if count > (end - p) // size:
                raise TTVError(f"list of {count} values at offset {p} overruns its record")
            self._spend(count)
            if base == BOOL:
                return [b != 0 for b in data[p:p + count]], p + count
            if base == SIZE:
                flat = struct.unpack_from(f">{2 * count}d", data, p)
                return [(flat[i], flat[i + 1]) for i in range(0, 2 * count, 2)], p + 16 * count
            return list(struct.unpack_from(f">{count}{fmt}", data, p)), p + count * size
        if base in _PREFIXED:
            if count > (end - p) // 4:
                raise TTVError(f"list of {count} values at offset {p} overruns its record")
            self._spend(count)
            out: List[Any] = []
            for _ in range(count):
                value, p = self._value(base, typ, p, end, depth)
                out.append(value)
            return out, p
        raise TTVError(f"unknown value type {typ:#06x} at offset {p - 8}")


# ---------------------------------------------------------------------------------------
# encoding


def _encode_value(base: int, value: Any) -> bytes:
    if base == BOOL:
        return b"\x01" if value else b"\x00"
    if base in _RANGES:
        lo, hi = _RANGES[base]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise TTVError(f"{value!r} does not fit type {base:#06x}")
        fmt = _FIXED[base][0]
        return struct.pack(">" + fmt, value)
    if base in (F32, F64, DATE):
        v = float(value)
        if not math.isfinite(v):
            raise TTVError(f"non-finite number {value!r}")
        try:
            return struct.pack(">f" if base == F32 else ">d", v)
        except (OverflowError, struct.error) as exc:
            raise TTVError(f"{value!r} does not fit type {base:#06x}") from exc
    if base == SIZE:
        w, h = float(value[0]), float(value[1])
        if not (math.isfinite(w) and math.isfinite(h)):
            raise TTVError(f"non-finite size {value!r}")
        return struct.pack(">dd", w, h)
    if base == STRING:
        raw = str(value).encode("utf-8")
        return _U32.pack(len(raw)) + raw
    if base in (BYTES, RECORD):  # RECORD: the already encoded nested record
        raw = bytes(value)
        if len(raw) > 0xFFFFFFFF:
            raise TTVError("value longer than 4 GiB")
        return _U32.pack(len(raw)) + raw
    raise TTVError(f"cannot encode value type {base:#06x}")


def encode(entries: Iterable[Sequence[Any]]) -> bytes:
    """Encode ``(tag, type, value[, stamp])`` tuples, in the given order, as one record.

    A ``RECORD`` value is the nested record's encoded bytes; a ``LIST`` value is a sequence;
    ``STAMPED`` entries need the stamp (u64 microseconds since 2001).  Raises
    :class:`TTVError` (a ``ValueError``) for a value that does not fit its type.
    """
    out = bytearray()
    for item in entries:
        tag, typ, value = item[0], item[1], item[2]
        stamp = item[3] if len(item) > 3 else None
        base = typ & ~(LIST | STAMPED)
        out += _HH.pack(tag, typ)
        if typ & LIST:
            values = list(value)
            out += _U32.pack(len(values))
            for v in values:
                out += _encode_value(base, v)
        else:
            out += _encode_value(base, value)
        if typ & STAMPED:
            if stamp is None or not 0 <= int(stamp) <= 0xFFFFFFFFFFFFFFFF:
                raise TTVError(f"tag {tag:#06x} needs a u64 timestamp")
            out += _U64.pack(int(stamp))
    return bytes(out)
