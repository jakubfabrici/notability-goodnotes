"""Microsoft Cabinet archives built from scratch for the tests: stored, MSZIP and LZX folders.

The LZX encoder is a small, slow test tool written from the bitstream description of
[MS-PATCH] (as [MS-CAB] folders use it), independent of ``gnnote/onenote/lzx.py``.  It
exists to reach what the makecab fixtures do not: uncompressed blocks (odd sizes, padding
at a frame boundary), several blocks per frame and blocks across frames, aligned-offset
blocks, every path-length run code (17, 18, 19), repeated offsets, long matches, all window
sizes, and E8 translation at frame boundaries.  During development its output was checked
against an independent LZX decoder (the Rust crate ``lzxd``, MIT OR Apache-2.0).

Conventions: a folder's output is cut into 32 KB frames, one per CFDATA block; the
bitstream is aligned to 16 bits at every frame end; no match crosses a frame or block
boundary.
"""
from __future__ import annotations

import bisect
import heapq
import struct
import zlib
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

FRAME = 32768
VERBATIM, ALIGNED, UNCOMPRESSED = 1, 2, 3
NONE, MSZIP, QUANTUM, LZX = 0, 1, 2, 3
MAX_MATCH = 257
E8_LIMIT = 0x40000000

Token = Union[int, Tuple[int, int, int]]  # a literal byte, or (length, position slot, footer)


# ------------------------------------------------------------------ bits and Huffman codes


class BitWriter:
    """16-bit little-endian words, most significant bit first (the LZX bit order)."""

    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def write(self, value: int, count: int) -> None:
        assert 0 <= value < (1 << count) or (count == 0 and value == 0), (value, count)
        self.acc = (self.acc << count) | value
        self.n += count
        while self.n >= 16:
            self.n -= 16
            self.out += ((self.acc >> self.n) & 0xFFFF).to_bytes(2, "little")
        self.acc &= (1 << self.n) - 1

    def align(self) -> None:
        """Zero bits up to the next 16-bit boundary (none when already there)."""
        if self.n:
            self.write(0, 16 - self.n)

    def align_raw(self) -> None:
        """1 to 16 zero bits: the padding in front of an uncompressed block's byte stream."""
        self.write(0, 16 - self.n)

    def raw(self, data: bytes) -> None:
        assert self.n == 0
        self.out += data


def position_slots(window: int) -> Tuple[List[int], List[int]]:
    bases: List[int] = []
    footers: List[int] = []
    base = slot = 0
    while base < window:
        bits = 0 if slot < 4 else (slot >> 1) - 1 if slot < 36 else 17
        bases.append(base)
        footers.append(bits)
        base += 1 << bits
        slot += 1
    return bases, footers


def huffman_lengths(freqs: Sequence[int], limit: int) -> List[int]:
    """Code lengths (at most ``limit``) of a Huffman code; every used symbol gets a code and
    at least two symbols are coded (a lone symbol is paired with a neighbour)."""
    lengths = [0] * len(freqs)
    used = [i for i, f in enumerate(freqs) if f]
    if not used:
        return lengths
    if len(used) == 1:
        lengths[used[0]] = 1
        lengths[1 if used[0] == 0 else 0] = 1
        return lengths
    weights = list(freqs)
    while True:
        heap = [(weights[i], i, [i]) for i in used]
        heapq.heapify(heap)
        depth = dict.fromkeys(used, 0)
        tie = len(freqs)
        while len(heap) > 1:
            w1, _, s1 = heapq.heappop(heap)
            w2, _, s2 = heapq.heappop(heap)
            for s in s1 + s2:
                depth[s] += 1
            tie += 1
            heapq.heappush(heap, (w1 + w2, tie, s1 + s2))
        if max(depth.values()) <= limit:
            for s, d in depth.items():
                lengths[s] = d
            return lengths
        weights = [max(1, w >> 1) if w else 0 for w in weights]


def canonical_codes(lengths: Sequence[int]) -> List[int]:
    count = [0] * 18
    for n in lengths:
        if n:
            count[n] += 1
    first = [0] * 18
    code = 0
    for n in range(1, 17):
        code = (code + count[n - 1]) << 1
        first[n] = code
    codes = [0] * len(lengths)
    for symbol, n in enumerate(lengths):
        if n:
            codes[symbol] = first[n]
            first[n] += 1
    return codes


def write_lengths(bw: BitWriter, prev: List[int], new: Sequence[int], start: int, stop: int,
                  runs: bool = True) -> None:
    """``new[start:stop]`` as changes from ``prev`` through a pretree (``prev`` is updated)."""
    ops: List[Tuple[int, int, int, Optional[int]]] = []  # (code, extra bits, extra value, delta)
    x = start
    while x < stop:
        v = new[x]
        run = 1
        while x + run < stop and new[x + run] == v:
            run += 1
        if runs and v == 0 and run >= 20:
            r = min(run, 51)
            ops.append((18, 5, r - 20, None))
        elif runs and v == 0 and run >= 4:
            r = min(run, 19)
            ops.append((17, 4, r - 4, None))
        elif runs and run >= 4:
            r = min(run, 5)
            ops.append((19, 1, r - 4, (prev[x] - v) % 17))
        else:
            r = 1
            ops.append(((prev[x] - v) % 17, 0, 0, None))
        x += r
    freqs = [0] * 20
    for code, _, _, delta in ops:
        freqs[code] += 1
        if delta is not None:
            freqs[delta] += 1
    pre = huffman_lengths(freqs, 15)
    for n in pre:
        bw.write(n, 4)
    codes = canonical_codes(pre)
    for code, nbits, extra, delta in ops:
        bw.write(codes[code], pre[code])
        if nbits:
            bw.write(extra, nbits)
        if delta is not None:
            bw.write(codes[delta], pre[delta])
    prev[start:stop] = new[start:stop]


# ------------------------------------------------------------------ LZX


def e8_translate(data: bytes, size: int) -> bytes:
    """The encoder side of x86 call translation, frame by frame (what the decoder undoes)."""
    out = bytearray(data)
    for start in range(0, len(out), FRAME):
        length = min(FRAME, len(out) - start)
        if start >= E8_LIMIT or length <= 10:
            continue
        p = start
        stop = start + length - 10
        while p < stop:
            if out[p] != 0xE8:
                p += 1
                continue
            rel = struct.unpack_from("<i", out, p + 1)[0]
            if -p <= rel < size - p:
                struct.pack_into("<i", out, p + 1, rel + p)
            elif size - p <= rel < size:
                struct.pack_into("<i", out, p + 1, rel - size)
            p += 5
    return bytes(out)


class _Matcher:
    """Greedy LZ77 over the whole folder: hash chains of 3-byte prefixes plus the repeated offsets."""

    def __init__(self, data: bytes, window: int, matches: bool, chain: int = 16):
        self.data = data
        self.max_offset = window - 3
        self.matches = matches
        self.chain = chain
        self.heads: Dict[bytes, List[int]] = {}
        self.indexed = 0

    def _index_to(self, pos: int) -> None:
        data = self.data
        for i in range(self.indexed, pos):
            key = data[i:i + 3]
            if len(key) == 3:
                chain = self.heads.setdefault(key, [])
                chain.append(i)
                if len(chain) > self.chain:
                    del chain[0]
        self.indexed = max(self.indexed, pos)

    def _length(self, pos: int, src: int, limit: int) -> int:
        data = self.data
        n = 0
        top = min(limit - pos, MAX_MATCH)
        while n < top and data[src + n] == data[pos + n]:
            n += 1
        return n

    def best(self, pos: int, limit: int, recent: Sequence[int]) -> Tuple[int, int]:
        """(length, offset) of the best match at ``pos`` ending by ``limit``; (0, 0) when none."""
        if not self.matches or limit - pos < 2:
            return 0, 0
        self._index_to(pos)
        best_len, best_off = 0, 0
        for r in recent:
            if r <= pos and r <= self.max_offset:
                n = self._length(pos, pos - r, limit)
                if n >= 2 and n > best_len:
                    best_len, best_off = n, r
        for src in reversed(self.heads.get(self.data[pos:pos + 3], ())):
            off = pos - src
            if off > self.max_offset:
                break
            n = self._length(pos, src, limit)
            if n >= 3 and n > best_len + 1:
                best_len, best_off = n, off
        return best_len, best_off


class PeriodicMatcher:
    """For data that repeats with ``period`` bytes from ``start`` on: literals before, then
    matches at that offset of up to ``longest`` bytes (tens of megabytes compress in a moment;
    ``longest=2`` makes the slowest stream to decode)."""

    def __init__(self, period: int, start: int = 0, longest: int = MAX_MATCH):
        self.period = period
        self.start = start
        self.longest = longest

    def best(self, pos: int, limit: int, recent: Sequence[int]) -> Tuple[int, int]:
        if pos < self.start + self.period or limit - pos < 2:
            return 0, 0
        return min(self.longest, limit - pos), self.period


def _write_compressed_block(bw: BitWriter, kind: int, tokens: Sequence[Token], start: int, main_prev: List[int],
                            length_prev: List[int], footers: Sequence[int], runs: bool,
                            at_output: Callable[[int], None]) -> None:
    main_size = len(main_prev)
    main_freq = [0] * main_size
    length_freq = [0] * 249
    aligned_freq = [0] * 8
    for t in tokens:
        if isinstance(t, int):
            main_freq[t] += 1
            continue
        n, slot, footer = t
        header = min(n - 2, 7)
        main_freq[256 + slot * 8 + header] += 1
        if header == 7:
            length_freq[n - 9] += 1
        if slot > 2 and footers[slot] >= 3:
            aligned_freq[footer & 7] += 1
    aligned: List[int] = []
    aligned_codes: List[int] = []
    if kind == ALIGNED:
        aligned = huffman_lengths(aligned_freq, 7)
        if not any(aligned):
            aligned = [3] * 8
        for n in aligned:
            bw.write(n, 3)
        aligned_codes = canonical_codes(aligned)
    main_lengths = huffman_lengths(main_freq, 16)
    length_lengths = huffman_lengths(length_freq, 16)
    write_lengths(bw, main_prev, main_lengths, 0, 256, runs)
    write_lengths(bw, main_prev, main_lengths, 256, main_size, runs)
    write_lengths(bw, length_prev, length_lengths, 0, 249, runs)
    main_codes = canonical_codes(main_lengths)
    length_codes = canonical_codes(length_lengths)
    pos = start
    for t in tokens:
        if isinstance(t, int):
            bw.write(main_codes[t], main_lengths[t])
            pos += 1
        else:
            n, slot, footer = t
            header = min(n - 2, 7)
            symbol = 256 + slot * 8 + header
            bw.write(main_codes[symbol], main_lengths[symbol])
            if header == 7:
                bw.write(length_codes[n - 9], length_lengths[n - 9])
            if slot > 2:
                bits = footers[slot]
                if kind == ALIGNED and bits >= 3:
                    bw.write(footer >> 3, bits - 3)
                    bw.write(aligned_codes[footer & 7], aligned[footer & 7])
                else:
                    bw.write(footer, bits)
            pos += n
        at_output(pos)


def lzx_compress(data: bytes, window_bits: int = 16, blocks: Optional[Sequence[Tuple[int, int]]] = None,
                 e8: Optional[int] = None, matches: bool = True, runs: bool = True,
                 pad_in_next_frame: bool = True, translated: Optional[bytes] = None,
                 matcher: Optional[PeriodicMatcher] = None) -> Tuple[List[bytes], List[int]]:
    """``data`` as LZX frames (CFDATA payloads) and their expanded sizes.

    ``blocks`` is a list of (block type, output bytes) covering ``data`` (default: one
    verbatim block); ``e8`` turns x86 call translation on with that translation size;
    ``matches=False`` writes literals only; ``runs=False`` codes every path length singly;
    ``pad_in_next_frame`` puts the padding byte of an odd uncompressed block that ends a
    frame at the start of the next frame (else at the end of its own); ``translated`` is the
    data after x86 call translation when the caller has it (large tests), and ``matcher`` a
    :class:`PeriodicMatcher` for large periodic data.
    """
    window = 1 << window_bits
    bases, footers = position_slots(window)
    if blocks is None:
        blocks = [(VERBATIM, len(data))] if data else []
    assert sum(size for _, size in blocks) == len(data)
    if not data:
        return [], []
    src = translated if translated is not None else e8_translate(data, e8) if e8 else data
    assert len(src) == len(data)
    total = len(src)
    bw = BitWriter()
    cuts = [0]
    if e8:
        bw.write(1, 1)
        bw.write(e8 >> 16, 16)
        bw.write(e8 & 0xFFFF, 16)
    else:
        bw.write(0, 1)
    if matcher is None:
        matcher = _Matcher(src, window, matches)  # type: ignore[assignment]
    recent = [1, 1, 1]
    main_prev = [0] * (256 + 8 * len(bases))
    length_prev = [0] * 249
    pos = 0
    pad_pending = False

    def frame_end(p: int) -> int:
        return min(total, (p // FRAME + 1) * FRAME)

    def at_output(p: int) -> None:
        """Cut a frame when the output reaches a frame boundary (the bitstream is aligned)."""
        if p % FRAME == 0 and p < total:
            bw.align()
            cuts.append(len(bw.out))

    for kind, size in blocks:
        assert size > 0 and kind in (VERBATIM, ALIGNED, UNCOMPRESSED)
        block_end = pos + size
        if pad_pending:  # the padding byte of the previous block opens this frame
            bw.raw(b"\x00")
            pad_pending = False
        bw.write(kind, 3)
        bw.write(size, 24)
        if kind == UNCOMPRESSED:
            bw.align_raw()
            bw.raw(struct.pack("<3I", *recent))
            while pos < block_end:
                stop = min(block_end, frame_end(pos))
                bw.raw(src[pos:stop])
                pos = stop
                if pos % FRAME == 0 and pos < total:  # a frame ends inside or at the end of the block
                    if pos == block_end and size & 1:
                        if pad_in_next_frame:
                            pad_pending = True
                        else:
                            bw.raw(b"\x00")
                    cuts.append(len(bw.out))
                elif pos == block_end and size & 1:
                    bw.raw(b"\x00")
            continue
        tokens: List[Token] = []
        while pos < block_end:
            limit = min(block_end, frame_end(pos))
            n, off = matcher.best(pos, limit, recent)
            if n < 2:
                tokens.append(src[pos])
                pos += 1
                continue
            if off == recent[0]:
                slot, footer = 0, 0
            elif off == recent[1]:
                slot, footer = 1, 0
                recent[0], recent[1] = recent[1], recent[0]
            elif off == recent[2]:
                slot, footer = 2, 0
                recent[0], recent[2] = recent[2], recent[0]
            else:
                formatted = off + 2
                slot = bisect.bisect_right(bases, formatted) - 1
                footer = formatted - bases[slot]
                recent[0], recent[1], recent[2] = off, recent[0], recent[1]
            tokens.append((n, slot, footer))
            pos += n
        _write_compressed_block(bw, kind, tokens, block_end - size, main_prev, length_prev, footers, runs,
                                at_output)
    bw.align()
    cuts.append(len(bw.out))
    frames = [bytes(bw.out[a:b]) for a, b in zip(cuts, cuts[1:])]
    sizes = [min(FRAME, total - k) for k in range(0, total, FRAME)]
    assert len(frames) == len(sizes), (len(frames), len(sizes))
    return frames, sizes


# ------------------------------------------------------------------ MSZIP and stored


def mszip_compress(data: bytes, level: int = 6) -> Tuple[List[bytes], List[int]]:
    """MSZIP frames: ``CK`` and a raw deflate stream per 32 KB, primed with the output before it."""
    frames: List[bytes] = []
    sizes: List[int] = []
    for start in range(0, len(data), FRAME):
        chunk = data[start:start + FRAME]
        history = data[max(0, start - FRAME):start]
        comp = zlib.compressobj(level, zlib.DEFLATED, -15, zdict=history) if history else \
            zlib.compressobj(level, zlib.DEFLATED, -15)
        frames.append(b"CK" + comp.compress(chunk) + comp.flush())
        sizes.append(len(chunk))
    return frames, sizes


def stored_frames(data: bytes) -> Tuple[List[bytes], List[int]]:
    frames = [data[k:k + FRAME] for k in range(0, len(data), FRAME)]
    return frames, [len(f) for f in frames]


# ------------------------------------------------------------------ the cabinet


def cab_checksum(data: bytes, seed: int = 0) -> int:
    """[MS-CAB] checksum: XOR of little-endian 32-bit words; 1-3 trailing bytes packed high first."""
    csum = seed
    whole = len(data) // 4 * 4
    for k in range(0, whole, 4):
        csum ^= int.from_bytes(data[k:k + 4], "little")
    tail = 0
    for b in data[whole:]:
        tail = (tail << 8) | b
    return (csum ^ tail) & 0xFFFFFFFF


Frames = Tuple[List[bytes], List[int]]


def compress(data: bytes, method: int = LZX, window_bits: int = 21, **lzx: object) -> Frames:
    if method == LZX:
        return lzx_compress(data, window_bits, **lzx)  # type: ignore[arg-type]
    if method == MSZIP:
        return mszip_compress(data)
    return stored_frames(data)


def build_cab(folders: Sequence[Sequence[Tuple[str, bytes]]], method: int = LZX, window_bits: int = 21,
              frames: Optional[Sequence[Frames]] = None, header_reserve: int = 0, folder_reserve: int = 0,
              data_reserve: int = 0, checksums: bool = True, reserve_flag: bool = False, flags: int = 0,
              utf8: bool = False, type_compress: Optional[int] = None, **lzx: object) -> bytes:
    """A cabinet of ``folders`` (each a list of (name, bytes); names use ``\\`` or ``/``).

    ``frames`` replaces the compressed frames of each folder (for crafted data);
    ``type_compress`` replaces the folders' compression field.
    """
    reserve = reserve_flag or header_reserve or folder_reserve or data_reserve
    flags |= 0x0004 if reserve else 0
    head_size = 36 + (4 + header_reserve if reserve else 0)
    folder_table = len(folders) * (8 + folder_reserve)
    file_entries = []
    for index, files in enumerate(folders):
        offset = 0
        for name, data in files:
            raw_name = name.replace("/", "\\").encode("utf-8")
            attribs = 0x20 | (0x80 if utf8 or any(b >= 0x80 for b in raw_name) else 0)
            file_entries.append(struct.pack("<IIHHHH", len(data), offset, index, 0x5A21, 0x6000, attribs)
                                + raw_name + b"\x00")
            offset += len(data)
    files_offset = head_size + folder_table
    data_offset = files_offset + sum(len(e) for e in file_entries)
    folder_records = []
    data_blocks = bytearray()
    for index, files in enumerate(folders):
        content = b"".join(d for _, d in files)
        chunks, sizes = frames[index] if frames is not None else compress(content, method, window_bits, **lzx)
        start = data_offset + len(data_blocks)
        for chunk, size in zip(chunks, sizes):
            sizes_field = struct.pack("<HH", len(chunk), size)
            csum = cab_checksum(sizes_field, cab_checksum(chunk)) if checksums else 0
            data_blocks += struct.pack("<I", csum) + sizes_field + bytes(data_reserve) + chunk
        kind = type_compress if type_compress is not None else \
            (method | (window_bits << 8) if method == LZX else method)
        folder_records.append(struct.pack("<IHH", start, len(chunks), kind) + bytes(folder_reserve))
    total = data_offset + len(data_blocks)
    count = sum(len(f) for f in folders)
    head = b"MSCF" + struct.pack("<IIIII", 0, total, 0, files_offset, 0) + bytes([3, 1]) + \
        struct.pack("<HHHHH", len(folders), count, flags, 0x1234, 0)
    if reserve:
        head += struct.pack("<HBB", header_reserve, folder_reserve, data_reserve) + bytes(header_reserve)
    out = head + b"".join(folder_records) + b"".join(file_entries) + bytes(data_blocks)
    assert len(out) == total
    return out
