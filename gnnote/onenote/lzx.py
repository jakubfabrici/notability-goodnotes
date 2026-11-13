# Ported from the Obsidian Importer's lzx.ts, itself ported from OfficeIMO; both are under the
# MIT License, whose notice is reproduced here as it requires:
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
"""LZX decompression for Microsoft Cabinet folders (the compression of OneNote ``.onepkg`` packages).

A CAB folder compressed with LZX ([MS-CAB]) is a sequence of CFDATA blocks, here called
*frames*: every frame expands to 32 KB (the last one may be shorter) and its compressed
bits start on a fresh 16-bit boundary, but the decoder state (window, Huffman path lengths,
repeated offsets, the current block) carries on from one frame to the next.  The bitstream
is the one [MS-PATCH] describes for LZX DELTA, minus the delta features: no reference
data, no extra match lengths, and windows of 2^15 to 2^21 bytes (the CAB folder's
``typeCompress`` names the window).

* The bitstream is read as little-endian 16-bit words, most significant bit first.
* The stream opens with one bit: when set, x86 call translation ("E8 translation") is on and
  32 bits give its translation size.  After decoding, every frame that starts below 1 GB
  and is longer than 10 bytes gets the translation undone (:func:`_undo_e8`).
* Blocks: 3 bits type (1 verbatim, 2 aligned offset, 3 uncompressed), 24 bits size (output
  bytes).  Blocks may span frames and frames may hold several blocks; no match crosses a
  frame boundary.
* Verbatim and aligned blocks carry their trees as path lengths: each tree part is coded
  with a 20-symbol pretree (4 bits per length) whose codes give the change of every length
  modulo 17 or runs (17: 4-19 zeros, 18: 20-51 zeros, 19: 4-5 copies of one changed
  length).  Path lengths are kept from block to block.  An aligned block first stores its
  8-symbol aligned-offset tree as plain 3-bit lengths.
* Main tree symbols below 256 are literals; above, ``(position slot << 3) | length
  header``.  Slots 0-2 reuse the three most recent offsets; others read ``footer_bits``
  extra bits (in aligned blocks the low three of them come from the aligned-offset tree).
* An uncompressed block skips 1 to 16 bits to the next 16-bit boundary, stores the three
  recent offsets as little-endian 32-bit values and then its bytes, plus one padding byte
  when its size is odd.

Ported by hand from ``src/formats/onenote-file/cabinet/lzx.ts`` of the Obsidian Importer
(https://github.com/obsidianmd/obsidian-importer, commit d2cb052c, MIT, Copyright (c) 2023
Obsidian), which says it was ported from OfficeIMO (https://github.com/EvotecIT/OfficeIMO,
MIT, Copyright (c) 2022 Evotec); the licence notice is at the top of this file and in
``NOTICE.md``.  The facts were checked against Microsoft's [MS-PATCH] and [MS-CAB].
Differences from the port's source: table-driven Huffman decoding (one table lookup per
symbol), a bit buffer refilled 32 bits at a time, slice copies for matches, the 1 to 16
bits of padding [MS-PATCH] gives an uncompressed block, trees with a single code are
accepted, the output grows frame by frame, and the E8 pass skips markers it leaves alone in
C (a regular expression) and has a budget.

Hostile input: every length, offset and count is checked before use; output never exceeds
the sizes the caller declares (at most 32 KB per frame) and is only allocated as frames are
decoded; reading past a frame's bytes, a code missing from its tree, an oversubscribed tree
or a match outside the window or across a block or frame raise :class:`LzxError` (a
:class:`ValueError`).  The work is bounded: at most 32 KB of output per frame (every token
yields at least a byte), at most :data:`MAX_BLOCKS_PER_FRAME` blocks (and so tree rebuilds)
starting in one frame, and the E8 pass stops with an :class:`LzxBudgetError` once its work
(call markers it rewrites or considers rewriting, and the markers it steps over in C, three
per unit) exceeds what its :class:`E8Budget` allows.
"""
from __future__ import annotations

import math
import re
import struct
from typing import Dict, List, Optional, Pattern, Sequence, Tuple, Union

__all__ = ["LzxError", "LzxBudgetError", "E8Budget", "lzx_decompress", "MIN_WINDOW_BITS", "MAX_WINDOW_BITS",
           "FRAME_SIZE"]

MIN_WINDOW_BITS = 15
MAX_WINDOW_BITS = 21
FRAME_SIZE = 32768  # output bytes of every frame (CFDATA block) but the last
VERBATIM, ALIGNED, UNCOMPRESSED = 1, 2, 3
LITERALS = 256
LENGTH_SYMBOLS = 249
ALIGNED_SYMBOLS = 8
PRETREE_SYMBOLS = 20
MAX_CODE_BITS = 16
TABLE_BITS = 12  # codes up to this long decode with one lookup; longer ones walk the code lengths
MIN_MATCH = 2
E8_LIMIT = 0x40000000  # E8 translation stops after the first 1 GB of output
E8_MIN_FRAME = 10  # frames of at most this many bytes are not translated (nor their last 10 bytes)
PAD_WORDS = 4  # zero words appended to a frame so that refills never test the end
MAX_BLOCKS_PER_FRAME = 32  # blocks starting in one frame (encoders write few, large blocks); bounds
# the cost of rebuilding trees
_I32 = struct.Struct("<i")
_INVALID = -1  # table entry: no code of the tree starts with these bits
_LONG = -2  # table entry: a code longer than the table's bits starts here
_MASKS = tuple((1 << n) - 1 for n in range(64))


class LzxError(ValueError):
    """The LZX data is damaged or not LZX at all."""


class LzxBudgetError(LzxError):
    """The LZX data needs more x86 call translation work than its :class:`E8Budget` allows."""


class E8Budget:
    """How much work the E8 pass may still do (about 0.3 microseconds per unit: one per call
    marker it may rewrite, one per three markers it steps over, see :func:`_undo_e8`); one
    budget can be shared by every folder of a cabinet, so that the work is bounded for the
    whole file."""

    __slots__ = ("left",)

    def __init__(self, left: int):
        self.left = left


class _Tree:
    """A canonical Huffman code: a lookup table indexed by the next ``bits`` bits.

    Entries are ``symbol << 5 | code length``; codes longer than ``bits`` (rare: they are the
    least frequent symbols) are decoded by :meth:`slow` from the per-length first codes.
    """

    __slots__ = ("table", "bits", "mask", "first", "count", "offset", "symbols")

    def __init__(self, lengths: Sequence[int], name: str):
        count = [0] * (MAX_CODE_BITS + 1)
        for n in lengths:
            count[n] += 1
        count[0] = 0
        left = 1  # Kraft: codes still free at the current length
        for n in range(1, MAX_CODE_BITS + 1):
            left = (left << 1) - count[n]
            if left < 0:
                raise LzxError(f"invalid LZX data: the {name} is oversubscribed")
        longest = max((n for n in range(1, MAX_CODE_BITS + 1) if count[n]), default=0)
        bits = min(longest, TABLE_BITS)
        first = [0] * (MAX_CODE_BITS + 2)
        offset = [0] * (MAX_CODE_BITS + 2)
        code = 0
        index = 0
        for n in range(1, MAX_CODE_BITS + 1):
            code = (code + count[n - 1]) << 1
            first[n] = code
            offset[n] = index
            index += count[n]
        symbols = [0] * index
        table = [_INVALID] * (1 << bits)
        nxt = first[:]
        slot = offset[:]
        for symbol, n in enumerate(lengths):
            if not n:
                continue
            c = nxt[n]
            nxt[n] = c + 1
            symbols[slot[n]] = symbol
            slot[n] += 1
            if n <= bits:
                shift = bits - n
                start = c << shift
                table[start:start + (1 << shift)] = [(symbol << 5) | n] * (1 << shift)
            else:
                table[c >> (n - bits)] = _LONG
        self.table = table
        self.bits = bits
        self.mask = (1 << bits) - 1
        self.first = first
        self.count = count
        self.offset = offset
        self.symbols = symbols

    def slow(self, buf: int, nb: int) -> int:
        """The entry of a code longer than the table (``nb >= 16`` bits are buffered)."""
        for n in range(1, MAX_CODE_BITS + 1):
            k = ((buf >> (nb - n)) & _MASKS[n]) - self.first[n]
            if 0 <= k < self.count[n]:
                return (self.symbols[self.offset[n] + k] << 5) | n
        raise LzxError("invalid LZX data: a Huffman code that is not in its tree")


class _Bits:
    """The bit reader of one frame: 16-bit little-endian words, most significant bit first.

    ``buf`` holds ``nb`` valid low bits (higher bits are stale and masked off on refill).
    The frame's bytes are unpacked into words once for each byte parity (an odd-sized
    uncompressed block leaves the words on odd offsets), so restarting after raw bytes
    costs nothing.  Reading past the frame's bytes reads zeros from a few padding words and
    then fails with an :class:`IndexError`; :meth:`check` tells an over-read from a complete
    frame.
    """

    __slots__ = ("data", "base", "words", "i", "nb", "buf", "_parity")

    def __init__(self, data: bytes, start: int = 0):
        self.data = data
        self._parity: List[Optional[Tuple[int, ...]]] = [None, None]
        self.restart(start)

    def restart(self, start: int) -> None:
        """Continue reading words at byte ``start`` (after bytes read raw)."""
        odd = start & 1
        words = self._parity[odd]
        if words is None:
            tail = self.data[odd:]
            padded = tail + bytes((len(tail) & 1) + 2 * PAD_WORDS)
            words = self._parity[odd] = struct.unpack(f"<{len(padded) // 2}H", padded)
        self.words = words
        self.base = odd  # byte offset of words[0]
        self.i = (start - odd) >> 1
        self.nb = 0
        self.buf = 0

    def byte(self) -> int:
        """The offset of the next unread byte (at a 16-bit boundary only after raw bytes)."""
        return self.base + ((self.i * 16 - self.nb) >> 3)

    def bits(self, n: int) -> int:
        """The next ``n`` bits (``n <= 32``)."""
        if self.nb < n:
            w = self.words
            self.buf = ((self.buf & _MASKS[self.nb]) << 32) | (w[self.i] << 16) | w[self.i + 1]
            self.i += 2
            self.nb += 32
        self.nb -= n
        return (self.buf >> self.nb) & _MASKS[n]

    def symbol(self, tree: _Tree) -> int:
        if self.nb < MAX_CODE_BITS:
            w = self.words
            self.buf = ((self.buf & _MASKS[self.nb]) << 32) | (w[self.i] << 16) | w[self.i + 1]
            self.i += 2
            self.nb += 32
        e = tree.table[(self.buf >> (self.nb - tree.bits)) & tree.mask]
        if e < 0:
            e = tree.slow(self.buf, self.nb)
        self.nb -= e & 31
        return e >> 5

    def position(self) -> int:
        """Bits consumed since the start of the frame."""
        return self.base * 8 + self.i * 16 - self.nb

    def check(self) -> None:
        if self.position() > len(self.data) * 8:
            raise LzxError("invalid LZX data: a frame ends in the middle of its bitstream")


def _position_slots(window: int) -> Tuple[List[int], List[int]]:
    """Base offsets and footer bit counts of the position slots of a ``window``-byte window."""
    bases: List[int] = []
    footers: List[int] = []
    base = 0
    slot = 0
    while base < window:
        bits = 0 if slot < 4 else (slot >> 1) - 1 if slot < 36 else 17
        bases.append(base)
        footers.append(bits)
        base += 1 << bits
        slot += 1
    return bases, footers


class _Decoder:
    """One folder's LZX stream, decoded frame by frame into a buffer that grows by a frame at a
    time (declared sizes alone never allocate anything)."""

    def __init__(self, total: int, window_bits: int):
        if not MIN_WINDOW_BITS <= window_bits <= MAX_WINDOW_BITS:
            raise LzxError(f"unsupported LZX window of 2^{window_bits} bytes (CAB allows 2^15 to 2^21)")
        self.window = 1 << window_bits
        self.bases, self.footers = _position_slots(self.window)
        self.total = total
        self.out = bytearray()
        self.pos = 0
        self.main_lengths = [0] * (LITERALS + 8 * len(self.bases))
        self.length_lengths = [0] * LENGTH_SYMBOLS
        self.main: Optional[_Tree] = None
        self.length: Optional[_Tree] = None
        self.aligned: Optional[_Tree] = None
        self.r0 = self.r1 = self.r2 = 1
        self.block_type = 0
        self.block_size = 0
        self.block_left = 0
        self.header_read = False
        self.e8_size = 0
        self.pad_pending = False  # an odd uncompressed block ended a frame; its padding opens the next
        self.frame_blocks = 0  # blocks started in the current frame
        self.frames: List[Tuple[int, int]] = []

    # -- frames ------------------------------------------------------------------------

    def frame(self, chunk: bytes, size: int) -> None:
        start = self.pos
        end = start + size
        if end > self.total:
            raise LzxError("invalid LZX data: more output than the folder declares")
        self.out += bytes(end - len(self.out))  # this frame's room (``pos`` only ever reaches ``end``)
        self.frames.append((start, size))
        self.frame_blocks = 0
        br = _Bits(chunk, 1 if self.pad_pending and chunk else 0)
        self.pad_pending = False
        try:
            while self.pos < end:
                if self.block_left == 0:
                    self._block_header(br)
                before = self.pos
                limit = min(end, before + self.block_left)
                if self.block_type == UNCOMPRESSED:
                    self._raw(br, limit)
                else:
                    self._tokens(br, limit)
                self.block_left -= self.pos - before
                if self.block_left == 0 and self.block_type == UNCOMPRESSED:
                    self._after_raw(br)
        except IndexError:
            raise LzxError("invalid LZX data: a frame ends in the middle of its bitstream") from None
        br.check()

    def _block_header(self, br: _Bits) -> None:
        self.frame_blocks += 1
        if self.frame_blocks > MAX_BLOCKS_PER_FRAME:
            raise LzxError(f"invalid LZX data: more than {MAX_BLOCKS_PER_FRAME} blocks start in one frame")
        if not self.header_read:
            self.header_read = True
            if br.bits(1):
                self.e8_size = (br.bits(16) << 16) | br.bits(16)
                if self.e8_size > 0x7FFFFFFF:
                    raise LzxError("invalid LZX data: E8 translation size out of range")
        kind = br.bits(3)
        size = (br.bits(16) << 8) | br.bits(8)
        if kind not in (VERBATIM, ALIGNED, UNCOMPRESSED) or size == 0:
            raise LzxError(f"invalid LZX data: invalid block header (type {kind}, {size} bytes)")
        self.block_type = kind
        self.block_size = size
        self.block_left = size
        if kind == UNCOMPRESSED:
            pos = br.i * 16 - br.nb  # bits read since words[0]
            pos = pos + 16 if pos % 16 == 0 else (pos | 15) + 1  # 1 to 16 bits of padding
            byte = br.base + (pos >> 3)
            raw = br.data[byte:byte + 12]
            if len(raw) < 12:
                raise LzxError("invalid LZX data: an uncompressed block header is cut off")
            r0, r1, r2 = struct.unpack("<3I", raw)
            for r in (r0, r1, r2):
                if r == 0 or r > self.window:
                    raise LzxError("invalid LZX data: repeated offset outside the window")
            self.r0, self.r1, self.r2 = r0, r1, r2
            br.restart(byte + 12)
            return
        if kind == ALIGNED:
            self.aligned = _Tree([br.bits(3) for _ in range(ALIGNED_SYMBOLS)], "aligned-offset tree")
        self._lengths(br, self.main_lengths, 0, LITERALS)
        self._lengths(br, self.main_lengths, LITERALS, len(self.main_lengths))
        self.main = _Tree(self.main_lengths, "main tree")
        self._lengths(br, self.length_lengths, 0, LENGTH_SYMBOLS)
        self.length = _Tree(self.length_lengths, "length tree")

    @staticmethod
    def _lengths(br: _Bits, lengths: List[int], start: int, stop: int) -> None:
        """Path lengths ``lengths[start:stop]``, coded as changes through a pretree."""
        pretree = _Tree([br.bits(4) for _ in range(PRETREE_SYMBOLS)], "pretree")
        x = start
        while x < stop:
            code = br.symbol(pretree)
            if code <= MAX_CODE_BITS:
                lengths[x] = (lengths[x] - code + 17) % 17
                x += 1
                continue
            if code == 17:
                run, value = br.bits(4) + 4, 0
            elif code == 18:
                run, value = br.bits(5) + 20, 0
            else:  # 19
                run = br.bits(1) + 4
                delta = br.symbol(pretree)
                if delta > MAX_CODE_BITS:
                    raise LzxError("invalid LZX data: invalid path-length run")
                value = (lengths[x] - delta + 17) % 17
            if run > stop - x:
                raise LzxError("invalid LZX data: a path-length run overruns its tree")
            lengths[x:x + run] = [value] * run
            x += run

    # -- uncompressed blocks ---------------------------------------------------------------

    def _raw(self, br: _Bits, limit: int) -> None:
        byte = br.byte()
        n = limit - self.pos
        data = br.data[byte:byte + n]
        if len(data) < n:
            raise LzxError("invalid LZX data: an uncompressed block is cut off")
        self.out[self.pos:limit] = data
        self.pos = limit
        br.restart(byte + n)

    def _after_raw(self, br: _Bits) -> None:
        if not self.block_size & 1:
            return
        byte = br.byte()
        if byte < len(br.data):
            br.restart(byte + 1)  # the padding byte after an odd-sized block
        else:
            self.pad_pending = True

    # -- verbatim and aligned blocks: the hot loop -------------------------------------------

    def _tokens(self, br: _Bits, limit: int) -> None:
        """Literals and matches up to output position ``limit``."""
        out = self.out
        pos = self.pos
        words = br.words
        i = br.i
        nb = br.nb
        buf = br.buf
        main = self.main
        length = self.length
        assert main is not None and length is not None
        mt, mb, mm = main.table, main.bits, main.mask
        lt, lb, lm = length.table, length.bits, length.mask
        aligned = self.aligned if self.block_type == ALIGNED else None
        if aligned is not None:
            at, ab, am = aligned.table, aligned.bits, aligned.mask
        bases = self.bases
        footers = self.footers
        window = self.window
        masks = _MASKS
        r0, r1, r2 = self.r0, self.r1, self.r2
        try:
            while pos < limit:
                if nb < 16:
                    buf = ((buf & masks[nb]) << 32) | (words[i] << 16) | words[i + 1]
                    i += 2
                    nb += 32
                e = mt[(buf >> (nb - mb)) & mm]
                if e < 0:
                    e = main.slow(buf, nb)
                nb -= e & 31
                s = e >> 5
                if s < LITERALS:
                    out[pos] = s
                    pos += 1
                    continue
                s -= LITERALS
                n = s & 7
                if n == 7:
                    if nb < 16:
                        buf = ((buf & masks[nb]) << 32) | (words[i] << 16) | words[i + 1]
                        i += 2
                        nb += 32
                    e = lt[(buf >> (nb - lb)) & lm]
                    if e < 0:
                        e = length.slow(buf, nb)
                    nb -= e & 31
                    n += e >> 5
                n += MIN_MATCH
                s >>= 3  # position slot
                if s > 2:
                    bits = footers[s]
                    if aligned is not None and bits >= 3:
                        if nb < 16:
                            buf = ((buf & masks[nb]) << 32) | (words[i] << 16) | words[i + 1]
                            i += 2
                            nb += 32
                        nb -= bits - 3
                        off = bases[s] + (((buf >> nb) & masks[bits - 3]) << 3) - 2
                        if nb < 16:
                            buf = ((buf & masks[nb]) << 32) | (words[i] << 16) | words[i + 1]
                            i += 2
                            nb += 32
                        e = at[(buf >> (nb - ab)) & am]
                        if e < 0:
                            e = aligned.slow(buf, nb)
                        nb -= e & 31
                        off += e >> 5
                    else:
                        if nb < 17:
                            buf = ((buf & masks[nb]) << 32) | (words[i] << 16) | words[i + 1]
                            i += 2
                            nb += 32
                        nb -= bits
                        off = bases[s] + ((buf >> nb) & masks[bits]) - 2
                    r2 = r1
                    r1 = r0
                    r0 = off
                elif s == 0:
                    off = r0
                elif s == 1:
                    off = r1
                    r1 = r0
                    r0 = off
                else:
                    off = r2
                    r2 = r0
                    r0 = off
                end = pos + n
                if end > limit:
                    raise LzxError("invalid LZX data: a match crosses a block or frame boundary")
                if off > pos or off > window:
                    raise LzxError("invalid LZX data: a match reaches outside the window")
                src = pos - off
                if off >= n:
                    out[pos:end] = out[src:src + n]
                else:  # overlapping: the last ``off`` bytes repeat
                    out[pos:end] = (out[src:pos] * (n // off + 1))[:n]
                pos = end
        finally:
            self.pos = pos
            br.i = i
            br.nb = nb
            br.buf = buf
            self.r0, self.r1, self.r2 = r0, r1, r2

    # -- the end ---------------------------------------------------------------------------

    def finish(self, e8_budget: Optional[E8Budget]) -> bytearray:
        if self.pos != self.total:
            raise LzxError("invalid LZX data: the folder expands to fewer bytes than it declares")
        if self.e8_size:
            _undo_e8(self.out, self.frames, self.e8_size, e8_budget)
        return self.out


# -- x86 call translation -----------------------------------------------------------------
#
# A marker is an ``E8`` byte; the four bytes after it are a little-endian target ``v`` that is
# rewritten when ``-p <= v < size`` (``p``: the marker's place in the output).  Most ``E8``
# bytes of data that is not x86 code have targets far outside that range, and visiting each of
# them in Python would cost far more than decoding the data did, so a regular expression steps
# over them in C and hands Python only the markers that may need rewriting.  Stepping over is
# not free (35 to 60 ns per marker of a uniform bitmap area, up to about 95 ns for crafted
# targets, and LZX can expand 1.3 MB to 1 GB of them), so the budget pays for it too, frame by
# frame before the work.  The expression uses no possessive quantifier or atomic group, which
# CPython 3.11.0 to 3.11.4 match wrongly; it never backtracks into an earlier pass of its
# greedy loop either, since what follows the loop cannot fail.  A check at import time runs
# it on a small buffer and, should it ever disagree with the plain loop, the plain loop
# (:func:`_undo_e8_plain`) does all the work.

E8_REACH_STEP = 1 << 20  # negative targets are classified to this many output bytes (one expression
# per MB of output; at most this much further back than the frame's markers count as "may need")
E8_PATTERN_UNITS = 1000  # budget units charged for each expression a folder uses (compiling one
# takes about 0.25 ms, as long as about 1000 rewritten markers; folders with distinct translation
# sizes each need their own)
E8_SKIPS_PER_UNIT = 3  # markers one budget unit pays for stepping over in C, so that a unit costs
# at most about 0.3 microseconds, like a rewritten marker (counted from the frame's E8 bytes, at
# most one per five bytes)
_E8_PATTERN_CACHE = 64  # compiled expressions kept (one folder uses one at a time)
_E8_PATTERNS: Dict[Tuple[int, int], Pattern[bytes]] = {}


def _ranges(lo: int, hi: int, width: int = 4) -> List[List[Tuple[int, int]]]:
    """``lo <= v <= hi`` for a ``width``-byte unsigned ``v``, as alternatives: each is one byte
    range per byte of ``v``, most significant first (at most ``2 * width - 1`` of them)."""
    if lo > hi:
        return []
    if width == 1:
        return [[(lo, hi)]]
    unit = 1 << (8 * (width - 1))
    lo_top, lo_rest = divmod(lo, unit)
    hi_top, hi_rest = divmod(hi, unit)
    if lo_top == hi_top:
        return [[(lo_top, lo_top)] + rest for rest in _ranges(lo_rest, hi_rest, width - 1)]
    head: List[List[Tuple[int, int]]] = []
    tail: List[List[Tuple[int, int]]] = []
    if lo_rest:
        head = [[(lo_top, lo_top)] + rest for rest in _ranges(lo_rest, unit - 1, width - 1)]
        lo_top += 1
    if hi_rest != unit - 1:
        tail = [[(hi_top, hi_top)] + rest for rest in _ranges(0, hi_rest, width - 1)]
        hi_top -= 1
    middle = [[(lo_top, hi_top)] + [(0, 255)] * (width - 1)] if lo_top <= hi_top else []
    return head + middle + tail


def _targets(*bounds: Tuple[int, int]) -> bytes:
    """An expression (for ``re.DOTALL``) for the four bytes of a target (little-endian, so the
    least significant byte comes first) whose unsigned value lies in one of the inclusive
    ``bounds``; the alternatives covering most values come first."""
    alternatives = [alternative for lo, hi in bounds for alternative in _ranges(lo, hi)]
    alternatives.sort(key=lambda ranges: -math.prod(b - a + 1 for a, b in ranges))
    parts = [b"".join(b"." if (a, b) == (0, 255) else b"\\x%02x" % a if a == b else b"[\\x%02x-\\x%02x]" % (a, b)
                      for a, b in reversed(alternative)) for alternative in alternatives]
    return b"(?:" + b"|".join(parts) + b")" if parts else b"(?!)"


def _e8_pattern(size: int, reach: int) -> Pattern[bytes]:
    """An expression that, matched at the start of a frame or just after an earlier match,
    steps over bytes that are not ``E8`` and over markers whose target cannot need rewriting
    (all in C; each pass of its loop takes the bytes before one such marker and the marker),
    then captures (group 1) the run of consecutive markers that may, if any: the group is
    empty when no such marker is left before the end of the frame.

    A target that may need rewriting is, as an unsigned value, below ``size`` (it is then
    rewritten wherever it is) or at least ``2^32 - reach``, where ``reach`` is at least the
    highest place of a marker in the frame (a negative target ``v`` is rewritten when
    ``-p <= v``).  Markers are matched left to right as the translation visits them: one
    ``E8`` and its four bytes at a time.
    """
    key = (size, reach)
    found = _E8_PATTERNS.get(key)
    if found is None:
        if len(_E8_PATTERNS) >= _E8_PATTERN_CACHE:
            _E8_PATTERNS.clear()
        low = (1 << 32) - reach  # the lowest unsigned value of a negative target that may need rewriting
        maybe = _targets((0, size - 1), (low, (1 << 32) - 1))
        never = _targets((size, low - 1))
        found = _E8_PATTERNS[key] = re.compile(
            b"(?:[^\\xe8]*\\xe8" + never + b")*[^\\xe8]*((?:\\xe8" + maybe + b")+)?", re.DOTALL)
    return found


def _over_budget() -> LzxBudgetError:
    return LzxBudgetError("the package holds more x86 call markers than gnnote restores for a file of its size")


def _undo_e8(out: bytearray, frames: Sequence[Tuple[int, int]], size: int, budget: Optional[E8Budget]) -> None:
    """Undo x86 call translation: a call marker ``E8`` is followed by an absolute target
    (relative to the translation size) that becomes the relative displacement again.

    Markers are visited left to right within each frame, skipping the four bytes after each.
    The markers whose target cannot need rewriting (most markers in data that is not x86 code,
    such as runs of ``E8`` bytes and most uniform bitmap areas) are stepped over by
    :func:`_e8_pattern` without Python work.  ``budget`` caps the work, about 0.3 microseconds
    per unit in CPython: one unit per marker that may need rewriting and one per run of such
    markers (0.2 to 0.4 microseconds each), one per :data:`E8_SKIPS_PER_UNIT` markers the
    expression may step over, and :data:`E8_PATTERN_UNITS` for each expression used.
    """
    if _E8_REGEX_OK:
        _undo_e8_regex(out, frames, size, budget)
    else:
        _undo_e8_plain(out, frames, size, budget)


def _undo_e8_regex(out: bytearray, frames: Sequence[Tuple[int, int]], size: int,
                   budget: Optional[E8Budget]) -> None:
    """:func:`_undo_e8` with :func:`_e8_pattern`: one expression per megabyte of output (the
    expressions are cached, but each one a call uses is charged, so that the cost does not
    depend on what was read before)."""
    get = _I32.unpack_from
    put = _I32.pack_into
    charged = -1  # the reach whose expression this call last paid for
    for start, length in frames:
        if start >= E8_LIMIT:
            break
        if length <= E8_MIN_FRAME:
            continue
        stop = start + length - E8_MIN_FRAME  # markers at or after it are not translated
        markers = out.count(0xE8, start, stop)
        if not markers:
            continue
        reach = -(-(stop - 1) // E8_REACH_STEP) * E8_REACH_STEP  # >= the place of every marker of the frame
        if budget is not None:  # paid before the work: the markers the expression may step over in C
            # (each is an E8 byte and takes five bytes) and, once per megabyte, the expression
            budget.left -= -(-min(markers, (stop - start + 4) // 5) // E8_SKIPS_PER_UNIT)
            if reach != charged:
                charged = reach
                budget.left -= E8_PATTERN_UNITS
            if budget.left < 0:
                raise _over_budget()
        match = _e8_pattern(size, reach).match
        end = stop + 4  # a marker before ``stop`` has its four bytes before ``end``
        p = start
        while True:
            first, p = match(out, p, end).span(1)  # always matches; (-1, -1) once no marker is left
            if first < 0:
                break
            if budget is not None:
                budget.left -= (p - first) // 5 + 1  # the markers and the match that found them
                if budget.left < 0:
                    raise _over_budget()
            for q in range(first, p, 5):
                value = get(out, q + 1)[0]
                if -q <= value < size:
                    put(out, q + 1, value - q if value >= 0 else value + size)


def _undo_e8_plain(out: bytearray, frames: Sequence[Tuple[int, int]], size: int,
                   budget: Optional[E8Budget]) -> None:
    """:func:`_undo_e8` without the regular expression: every marker is visited in Python and
    costs one unit of ``budget`` (only used when the import-time check finds the expression
    misbehaving)."""
    get = _I32.unpack_from
    put = _I32.pack_into
    for start, length in frames:
        if start >= E8_LIMIT:
            break
        if length <= E8_MIN_FRAME:
            continue
        stop = start + length - E8_MIN_FRAME
        q = out.find(0xE8, start, stop)
        while q >= 0:
            if budget is not None:
                budget.left -= 1
                if budget.left < 0:
                    raise _over_budget()
            value = get(out, q + 1)[0]
            if -q <= value < size:
                put(out, q + 1, value - q if value >= 0 else value + size)
            q = out.find(0xE8, q + 5, stop)


def _e8_check_case() -> Tuple[bytearray, List[Tuple[int, int]], int]:
    """A small output buffer, its frames and a translation size that hold every kind of marker:
    rewritten ones, near misses on both sides of the range, runs of ``E8``, markers in the last
    bytes of a frame and frames too short to translate."""
    size = 0x0123_4567
    data = bytearray(b"ab\xe8\x01\x02\x03\x05cd\xe8\x01\x02\x03\x00\xe8\x00\x00\x00\xffzz")
    targets = (0, 1, size - 1, size, size + 1, 0x0123_4500, 0x0123_4600, 0x0124_0000, 0x7FFF_FFFF,
               -1, -2, -3000, -5000, -E8_REACH_STEP, -E8_REACH_STEP - 1, -(1 << 31), 0x00E8_E8E8, -0x1717_1718)
    seed = 12345
    while len(data) < 6000:
        seed = (seed * 1103515245 + 12345) & 0x7FFF_FFFF  # a fixed pseudo-random sequence
        kind = seed >> 28
        if kind < 3:
            data += b"\xe8" * (1 + (seed >> 8) % 12)
        elif kind < 6:
            data += b"\xe8" + struct.pack("<i", targets[(seed >> 4) % len(targets)])
        elif kind < 7:
            data += b"\xe8" + struct.pack("<i", -len(data) + (seed >> 4) % 3 - 1)  # about -p
        else:
            data += bytes((seed >> k) & 0xFF for k in range(1 + seed % 7))
    return data, [(0, 2000), (2000, 15), (2015, 9), (2024, len(data) - 2024)], size


def _e8_regex_works() -> bool:
    """Whether :func:`_undo_e8_regex` gives the plain loop's result on :func:`_e8_check_case`."""
    data, frames, size = _e8_check_case()
    try:
        fast, plain = bytearray(data), bytearray(data)
        _undo_e8_regex(fast, frames, size, None)
        _undo_e8_plain(plain, frames, size, None)
        return fast == plain and fast != data
    except Exception:  # any failure means: do not trust the expression
        return False


_E8_REGEX_OK = _e8_regex_works()  # False only on an interpreter whose ``re`` misbehaves


def lzx_decompress(frames: Sequence[bytes], sizes: Sequence[int], window_bits: int,
                   e8_budget: Union[None, int, E8Budget] = None) -> bytearray:
    """The output of one CAB LZX folder: ``frames[k]`` (a CFDATA block's bytes) expands to
    ``sizes[k]`` bytes (at most 32 KB each).

    ``e8_budget`` caps the work of the E8 pass, about 0.3 microseconds per unit (one per x86
    call marker it may rewrite, one per three it steps over, see :func:`_undo_e8`; ``None``:
    no cap; an :class:`E8Budget` is drawn down, so several calls can share it).
    Raises :class:`LzxError` for damaged data and :class:`LzxBudgetError` past the budget.
    """
    if len(frames) != len(sizes):
        raise LzxError("LZX frames and sizes do not match")
    for size in sizes:
        if not 0 <= size <= FRAME_SIZE:
            raise LzxError(f"invalid LZX data: a frame declares {size} bytes (at most {FRAME_SIZE})")
    budget = E8Budget(e8_budget) if isinstance(e8_budget, int) else e8_budget
    decoder = _Decoder(sum(sizes), window_bits)
    for chunk, size in zip(frames, sizes):
        if size:
            decoder.frame(bytes(chunk), size)
    return decoder.finish(budget)
