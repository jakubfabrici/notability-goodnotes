# Ported from the Obsidian Importer's cabinet.ts, itself ported from OfficeIMO; both are under
# the MIT License, whose notice is reproduced here as it requires:
#
# Copyright (c) 2023 Obsidian
# Copyright (c) 2022 Evotec
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software
# and associated documentation files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
# BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""Microsoft Cabinet (CAB) archives, read only: the container of OneNote ``.onepkg`` packages.

OneNote for Windows exports a notebook (*File > Export > Notebook > OneNote Package*) as a
cabinet of its ``.one`` sections and ``.onetoc2`` tables of contents, LZX compressed.
Layout ([MS-CAB]; all integers little-endian):

* ``CFHEADER`` (36 bytes): ``MSCF``, the cabinet size (offset 8), the offset of the first
  ``CFFILE`` (16), version 1.3 (24, 25), folder and file counts (26, 28), flags (30: 1 a
  previous cabinet, 2 a next cabinet, 4 reserve fields present), set ID and index.  With
  flag 4, a 16-bit header reserve size and 8-bit folder and data reserve sizes follow, then
  the header reserve; with flags 1 and 2, NUL-terminated cabinet and disk names.
* ``CFFOLDER`` (8 bytes + folder reserve) per folder: offset of its first ``CFDATA``, number
  of ``CFDATA`` blocks, compression (low nibble 0 none, 1 MSZIP, 2 Quantum, 3 LZX; for LZX
  bits 8-12 give the window as a power of two).
* ``CFFILE`` (16 bytes + name) per file: size, offset in its folder's expanded data,
  folder index (0xFFFD-0xFFFF: continued from or into another cabinet), date, time,
  attributes (0x80: the name is UTF-8) and the NUL-terminated name (``\\`` separates
  folders).
* ``CFDATA`` (8 bytes + data reserve + data) per block: checksum (0: none), compressed and
  expanded sizes.  A folder's blocks expand, in order, to the folder's data, at most 32 KB
  each.  MSZIP blocks are ``CK`` plus raw deflate data whose dictionary is the 32 KB of
  output before them; LZX blocks are frames of one LZX stream (:mod:`.lzx`).

The checksum XORs the data as little-endian 32-bit words (the last 1-3 bytes packed most
significant first), then the 4 bytes of the two sizes.

Ported by hand from ``src/formats/onenote-file/cabinet/cabinet.ts`` of the Obsidian Importer
(https://github.com/obsidianmd/obsidian-importer, commit d2cb052c, MIT, Copyright (c) 2023
Obsidian), itself ported from OfficeIMO (https://github.com/EvotecIT/OfficeIMO, MIT,
Copyright (c) 2022 Evotec); the licence notice is at the top of this file and in
``NOTICE.md``.  Additions: MSZIP folders, expansion folder by folder on demand
(:meth:`Cabinet.folder_size` declares a folder's size before :meth:`Cabinet.expand` spends
anything on it, so the caller can apply its budget), :meth:`Cabinet.release`, and the
hardening below.

Hostile input: every offset and size is checked against the bytes present before use, the
cabinet's tables are bounded (:data:`MAX_FILES`, names of at most :data:`MAX_NAME_BYTES`),
the ``CFDATA`` blocks walked across all folders, headers and payloads, cannot add up to more
bytes than the file holds and folders may not share data blocks (so all folders together
decode at most the file's bytes), a compressed block expands to at most 32 KB, a
folder must not declare more output than :data:`MAX_RATIO` times its compressed bytes,
checksums are verified when present, and the LZX E8 passes of all folders together may
spend :data:`E8_MIN` plus :data:`E8_PER_BYTE` work units (about 0.3 microseconds each: one
per call marker rewritten, one per three stepped over) per byte of the file.
Multi-cabinet sets and Quantum compression are refused with a :class:`CabError`, the only
exception this module raises.
"""
from __future__ import annotations

import bisect
import struct
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .lzx import FRAME_SIZE, E8Budget, LzxBudgetError, LzxError, lzx_decompress

__all__ = ["CabError", "CabMember", "Cabinet", "is_cabinet", "checksum", "MAX_FILES", "MAX_RATIO"]

SIGNATURE = b"MSCF"
HEADER_SIZE = 36
FLAG_PREV_CABINET = 0x0001
FLAG_NEXT_CABINET = 0x0002
FLAG_RESERVE_PRESENT = 0x0004
ATTR_NAME_IS_UTF = 0x0080
CONTINUED_FOLDER = 0xFFFD  # this and above: the file continues from or into another cabinet
COMPRESS_NONE, COMPRESS_MSZIP, COMPRESS_QUANTUM, COMPRESS_LZX = 0, 1, 2, 3
MAX_HEADER_RESERVE = 60_000  # [MS-CAB]
MAX_FILES = 16_384  # files one cabinet may list (a notebook has a few per section)
MAX_NAME_BYTES = 1024  # longest file name read (Windows paths stay well below)
MAX_RATIO = 1100  # expanded / compressed bytes of a folder: LZX and deflate stay near 1030
RATIO_SLACK = 2 * FRAME_SIZE  # tiny folders may expand a little more than the ratio
# The LZX E8 passes of one file may spend E8_MIN work units plus E8_PER_BYTE per byte of the file,
# about 0.3 microseconds each: one unit per x86 call marker rewritten (0.2 to 0.4 microseconds), a
# thousand per regular expression a folder uses, and one per three markers whose target lies
# outside the translated range (most E8 bytes of data that is not x86 code), which are skipped in
# C at up to about 95 ns each.  Real data needs well under one unit per byte; E8-dense data that
# compresses about 1000:1 (a uniform bitmap area) up to about 50 MB fits E8_MIN alone.
E8_PER_BYTE = 4
E8_MIN = 4_000_000  # about a second

_COMPRESSION_NAMES = {COMPRESS_NONE: "stored", COMPRESS_MSZIP: "MSZIP", COMPRESS_QUANTUM: "Quantum",
                      COMPRESS_LZX: "LZX"}


class CabError(ValueError):
    """The cabinet is damaged, truncated, part of a multi-cabinet set or uses Quantum."""


@dataclass(frozen=True)
class CabMember:
    """One file of a cabinet: its name (``/``-separated), size and place in its folder."""

    name: str
    size: int
    folder: int
    offset: int


@dataclass
class _Folder:
    data_offset: int
    blocks: int
    compression: int
    blocks_read: Optional[List[Tuple[int, int, int]]] = None  # (payload offset, compressed, expanded)
    size: int = 0  # declared expanded bytes, once the blocks were walked


def is_cabinet(data: bytes) -> bool:
    return bytes(data[:4]) == SIGNATURE


def _u16(data: bytes, offset: int, what: str) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise CabError(f"truncated cabinet: {what} lies past its end")
    return data[offset] | (data[offset + 1] << 8)


def _u32(data: bytes, offset: int, what: str) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise CabError(f"truncated cabinet: {what} lies past its end")
    return int.from_bytes(data[offset:offset + 4], "little")


def _cstring(data: bytes, offset: int, limit: int, what: str) -> Tuple[bytes, int]:
    """The NUL-terminated string at ``offset`` (at most ``limit`` bytes) and the offset after it."""
    end = data.find(b"\x00", offset, offset + limit + 1)
    if end < 0:
        if offset + limit + 1 > len(data):
            raise CabError(f"truncated cabinet: {what} lies past its end")
        raise CabError(f"damaged cabinet: {what} is longer than {limit} bytes")
    return bytes(data[offset:end]), end + 1


def checksum(data: bytes, seed: int = 0) -> int:
    """The [MS-CAB] checksum of ``data`` (XOR of little-endian 32-bit words, tail packed high first)."""
    whole = len(data) & ~3
    x = int.from_bytes(data[:whole], "little")
    words = whole // 4
    while words > 1:  # XOR the halves together: linear time, no Python loop over the words
        half = (words + 1) // 2
        x = (x >> (32 * half)) ^ (x & ((1 << (32 * half)) - 1))
        words = half
    tail = 0
    for b in data[whole:]:
        tail = (tail << 8) | b
    return (x ^ tail ^ seed) & 0xFFFFFFFF


def _decode_name(raw: bytes, attributes: int) -> str:
    if attributes & ATTR_NAME_IS_UTF:
        return raw.decode("utf-8", "replace")
    try:
        return raw.decode("utf-8")  # pure ASCII, or UTF-8 without the flag
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")  # the "current locale" of most such files


class Cabinet:
    """The directory of one cabinet; :meth:`read` expands a member's folder on demand.

    Expanded folders are kept (a cabinet usually has one), so reading every member costs
    one expansion per folder.  Errors are :class:`CabError`; :meth:`read` raises it for a
    member whose folder is damaged, the constructor for a damaged directory.
    """

    def __init__(self, data: bytes):
        data = bytes(data)
        if len(data) < HEADER_SIZE or data[:4] != SIGNATURE:
            raise CabError("not a cabinet archive (no MSCF header)")
        self.data = data
        declared = _u32(data, 8, "the header")
        self.truncated = declared > len(data)
        files_offset = _u32(data, 16, "the header")
        folder_count = _u16(data, 26, "the header")
        file_count = _u16(data, 28, "the header")
        flags = _u16(data, 30, "the header")
        if flags & (FLAG_PREV_CABINET | FLAG_NEXT_CABINET):
            raise CabError("the package is one part of a multi-file cabinet set, which gnnote cannot read; "
                           "export the notebook again as a single .onepkg file")
        if file_count > MAX_FILES:
            raise CabError(f"the cabinet lists {file_count} files, more than gnnote reads ({MAX_FILES})")
        offset = HEADER_SIZE
        folder_reserve = self.data_reserve = 0
        if flags & FLAG_RESERVE_PRESENT:
            header_reserve = _u16(data, offset, "the reserve sizes")
            if header_reserve > MAX_HEADER_RESERVE:
                raise CabError("damaged cabinet: its header reserve is too large")
            folder_reserve = _u16(data, offset + 2, "the reserve sizes") & 0xFF
            self.data_reserve = data[offset + 3]
            offset += 4 + header_reserve
        self.folders: List[_Folder] = []
        for k in range(folder_count):
            if offset + 8 > len(data):
                raise CabError(f"truncated cabinet: folder {k + 1} of {folder_count} is missing")
            start, blocks, compression = struct.unpack_from("<IHH", data, offset)
            self.folders.append(_Folder(start, blocks, compression))
            offset += 8 + folder_reserve
        self.members: List[CabMember] = []
        self.continued = 0  # files that continue from or into another cabinet
        self.misplaced = 0  # files naming a folder the cabinet does not have
        offset = files_offset
        for k in range(file_count):
            if offset + 16 > len(data):
                raise CabError(f"truncated cabinet: file {k + 1} of {file_count} is missing")
            size, folder_offset, folder, _date, _time, attributes = struct.unpack_from("<IIHHHH", data, offset)
            raw, offset = _cstring(data, offset + 16, MAX_NAME_BYTES, "a file name")
            if folder >= CONTINUED_FOLDER:
                self.continued += 1
                continue
            if folder >= folder_count:
                self.misplaced += 1
                continue
            name = _decode_name(raw, attributes).replace("\\", "/")
            self.members.append(CabMember(name, size, folder, folder_offset))
        if self.continued:
            raise CabError("the package is one part of a multi-file cabinet set (its files continue in another "
                           "cabinet), which gnnote cannot read; export the notebook again as a single .onepkg file")
        self._walked = 0  # bytes of CFDATA blocks walked so far (headers and payloads), over all folders
        self._expanded: Dict[int, bytearray] = {}
        self._released: Set[int] = set()  # folders whose expanded data was dropped
        self._ranges: List[Tuple[int, int]] = []  # sorted, disjoint (start, end) of the folders walked
        self._e8 = E8Budget(E8_MIN + E8_PER_BYTE * len(data))  # shared by every LZX folder

    # -- folders -------------------------------------------------------------------------

    def folder_size(self, index: int) -> int:
        """The expanded size folder ``index`` declares (its ``CFDATA`` blocks are walked once)."""
        folder = self.folders[index]
        if folder.blocks_read is not None:
            return folder.size
        kind = folder.compression & 0x000F
        if kind == COMPRESS_QUANTUM:
            raise CabError("the package uses Quantum compression, which gnnote cannot unpack")
        if kind not in (COMPRESS_NONE, COMPRESS_MSZIP, COMPRESS_LZX):
            raise CabError(f"the package uses an unknown compression method ({kind})")
        data = self.data
        self._walked += folder.blocks * (8 + self.data_reserve)  # counted before the walk, every walk
        if self._walked > len(data):
            raise CabError("damaged cabinet: its folders declare more data blocks than the file holds")
        offset = folder.data_offset
        blocks: List[Tuple[int, int, int]] = []
        total = packed = 0
        for k in range(folder.blocks):
            if offset < HEADER_SIZE or offset + 8 + self.data_reserve > len(data):
                raise CabError(self._cut(f"data block {k + 1} of {folder.blocks}"))
            csum, size, expanded = struct.unpack_from("<IHH", data, offset)
            payload = offset + 8 + self.data_reserve
            if payload + size > len(data):
                raise CabError(self._cut(f"data block {k + 1} of {folder.blocks}"))
            self._walked += size
            if self._walked > len(data):  # real folders' blocks never overlap: together they fit the file
                raise CabError("damaged cabinet: its folders' data blocks overlap or add up to more than the "
                               "file holds")
            if kind == COMPRESS_NONE and size != expanded:
                raise CabError("damaged cabinet: a stored data block changes size")
            if kind != COMPRESS_NONE and expanded > FRAME_SIZE:  # stored blocks may hold up to 64 KB
                raise CabError(f"damaged cabinet: a data block expands to {expanded} bytes, "
                               f"more than {FRAME_SIZE}")
            if csum and not self._checksum_ok(offset, payload, size, csum):
                raise CabError(f"damaged cabinet: data block {k + 1} of {folder.blocks} fails its checksum")
            blocks.append((payload, size, expanded))
            total += expanded
            packed += size
            offset = payload + size
        if total > MAX_RATIO * packed + RATIO_SLACK:
            raise CabError(f"damaged cabinet: a folder declares {total} bytes unpacked from {packed} "
                           "bytes, more than compression can achieve")
        if offset > folder.data_offset:
            self._claim(folder.data_offset, offset)
        folder.blocks_read = blocks
        folder.size = total
        return total

    def _claim(self, start: int, end: int) -> None:
        """Record that a folder's data blocks span ``[start, end)``; real folders never share."""
        ranges = self._ranges
        k = bisect.bisect_left(ranges, (start, end))
        if (k and ranges[k - 1][1] > start) or (k < len(ranges) and ranges[k][0] < end):
            raise CabError("damaged cabinet: two folders share data blocks")
        ranges.insert(k, (start, end))

    def _checksum_ok(self, offset: int, payload: int, size: int, csum: int) -> bool:
        data = self.data
        partial = checksum(data[payload:payload + size])
        if checksum(data[offset + 4:offset + 8], partial) == csum:
            return True
        # [MS-CAB] says the checksum covers the data reserve too; its sample code does not
        return bool(self.data_reserve) and checksum(data[offset + 4:payload], partial) == csum

    def _cut(self, what: str) -> str:
        if self.truncated:
            return f"truncated cabinet: {what} lies past its end"
        return f"damaged cabinet: {what} lies outside the file"

    def is_expanded(self, index: int) -> bool:
        return index in self._expanded

    def release(self, index: int) -> None:
        """Drop folder ``index``'s expanded data once no more of its files will be read; it is
        not expanded a second time (:meth:`expand` then raises)."""
        if self._expanded.pop(index, None) is not None:
            self._released.add(index)

    def expand(self, index: int) -> bytearray:
        """Folder ``index``'s expanded data (kept for later reads; callers must not change it)."""
        found = self._expanded.get(index)
        if found is not None:
            return found
        if index in self._released:
            raise CabError("a folder of the package was read already and is not expanded again")
        total = self.folder_size(index)
        folder = self.folders[index]
        assert folder.blocks_read is not None
        data = self.data
        kind = folder.compression & 0x000F
        if kind == COMPRESS_NONE:
            out = bytearray().join(data[p:p + n] for p, n, _ in folder.blocks_read)
        elif kind == COMPRESS_MSZIP:
            out = self._mszip(folder.blocks_read)
        else:
            frames = [data[p:p + n] for p, n, _ in folder.blocks_read]
            sizes = [m for _, _, m in folder.blocks_read]
            try:
                out = lzx_decompress(frames, sizes, (folder.compression >> 8) & 0x1F, e8_budget=self._e8)
            except LzxBudgetError as exc:  # possibly valid data: not called damaged
                raise CabError(str(exc)) from None
            except LzxError as exc:
                raise CabError(f"damaged cabinet: {exc}") from None
        if len(out) != total:
            raise CabError("damaged cabinet: a folder expands to another size than it declares")
        self._expanded[index] = out
        return out

    def _mszip(self, blocks: List[Tuple[int, int, int]]) -> bytearray:
        data = self.data
        parts: List[bytes] = []
        history = b""
        for k, (payload, size, expanded) in enumerate(blocks):
            if data[payload:payload + 2] != b"CK":
                raise CabError(f"damaged cabinet: MSZIP block {k + 1} lacks its CK signature")
            try:
                inflater = zlib.decompressobj(-15, zdict=history) if history else zlib.decompressobj(-15)
                out = inflater.decompress(data[payload + 2:payload + size], expanded + 1)
            except zlib.error as exc:
                raise CabError(f"damaged cabinet: MSZIP block {k + 1}: {exc}") from None
            if len(out) != expanded:
                raise CabError(f"damaged cabinet: MSZIP block {k + 1} expands to {len(out)} bytes, "
                               f"not {expanded}")
            parts.append(out)
            history = (history + out)[-FRAME_SIZE:]
        return bytearray().join(parts)

    def read(self, member: CabMember) -> bytes:
        """The bytes of ``member`` (its folder is expanded on first use)."""
        out = self.expand(member.folder)
        if member.offset + member.size > len(out):
            raise CabError(f"damaged cabinet: {member.name} extends past the data of its folder")
        with memoryview(out) as view:
            return bytes(view[member.offset:member.offset + member.size])

    def description(self, index: int) -> str:
        """``LZX (2^21 window)`` and the like, for messages and tests."""
        compression = self.folders[index].compression
        kind = compression & 0x000F
        name = _COMPRESSION_NAMES.get(kind, f"type {kind}")
        return f"{name} (2^{(compression >> 8) & 0x1F} window)" if kind == COMPRESS_LZX else name
