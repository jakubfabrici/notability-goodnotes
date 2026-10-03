"""Apple framed LZ4 (``libcompression`` COMPRESSION_LZ4 stream) as used by GoodNotes strokes.

Framing (``docs/ecosystem.md`` section 3, ``docs/goodnotes-stroke.md`` section 4), all integers
little-endian, no checksums, no alignment::

    stream := block* 'bv4$'
    block  := 'bv41' u32 decoded_size u32 encoded_size <encoded_size bytes of LZ4 block>
            | 'bv4-' u32 size <size bytes stored verbatim>          (ONE size word)

Blocks may refer back into the previous blocks' output, so one output buffer is shared by
the whole stream.  GoodNotes writes exactly one ``bv41`` block per stroke (5677/5677 observed,
largest 30 520 bytes); the decoder nevertheless accepts any mix of blocks.  Bytes after
``bv4$`` are ignored, as Apple's documentation requires.

LZ4 block format (``lz4_Block_format.md``)::

    sequence := token [lit_ext...] literals [offset_u16le [match_ext...]]
    token    := (literal_length_nibble << 4) | match_length_nibble
    nibble 15 means "add the following bytes, each 0..255, until one is not 255"
    match length = nibble (+ extension) + 4; offset 1..65535 (0 is invalid)
    the last sequence holds only literals (no offset), and may be the only sequence

``compress(level=0)`` emits one all-literal sequence per 32 KiB chunk (``token 0xF0`` +
extension bytes + the chunk, or ``n << 4`` + chunk for n < 15), which is what the writer uses:
it is standard LZ4, decodable by every known reader, and the ZIP deflate layer removes the
redundancy.  ``compress(level=1)`` is a small greedy hash-chain-free LZ4 compressor honouring
the reference encoder's end-of-block rules (last 5 bytes literal, last match starts at least
12 bytes before the end); its output is only for size, both levels decode identically.
"""
from __future__ import annotations

import struct
from typing import List

MAGIC_COMPRESSED = b"bv41"
MAGIC_STORED = b"bv4-"
MAGIC_END = b"bv4$"
BLOCK_SIZE = 32768  # GoodNotes (2026 builds) frames its own blobs in 32 KiB blocks; mirror that

_MIN_MATCH = 4
_MF_LIMIT = 12       # a match may not start closer than this to the end of the block
_LAST_LITERALS = 5   # the last bytes of a block are always literals
_MAX_OFFSET = 65535
MAX_BLOCK_OUTPUT = 64 * 1024 * 1024  # cap for a block whose size is not declared
MAX_STREAM_OUTPUT = 256 * 1024 * 1024  # summed declared sizes of a stream (largest real frame: 30 520 bytes)


def is_apple_lz4(data: bytes) -> bool:
    """True when ``data`` starts with one of the three Apple LZ4 block magics."""
    head = bytes(data[:4])
    return head in (MAGIC_COMPRESSED, MAGIC_STORED, MAGIC_END)


# --------------------------------------------------------------------------- LZ4 block


def _read_length(src: bytes, pos: int, base: int) -> "tuple[int, int]":
    """Extend a nibble of 15 with the 0..255 continuation bytes."""
    n = len(src)
    while True:
        if pos >= n:
            raise ValueError("LZ4 block truncated inside a length extension")
        byte = src[pos]
        pos += 1
        base += byte
        if byte != 255:
            return base, pos


def lz4_block_decompress(src: bytes, out: "bytearray | None" = None,
                         expected_size: "int | None" = None) -> bytearray:
    """Decode one raw LZ4 block, appending to ``out`` (the history window) and returning it.

    When ``expected_size`` is given the block must produce exactly that many new bytes, and
    the budget is enforced *before* every literal and match copy, so a block that lies about
    its size cannot make the decoder materialise more than ``expected_size`` bytes (a match
    of a few MB of length-extension bytes would otherwise expand ~255:1 into gigabytes).
    Without ``expected_size`` the output is capped at :data:`MAX_BLOCK_OUTPUT`.
    """
    if out is None:
        out = bytearray()
    start_len = len(out)
    budget = expected_size if expected_size is not None else MAX_BLOCK_OUTPUT
    pos = 0
    n = len(src)
    while pos < n:
        token = src[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == 15:
            lit_len, pos = _read_length(src, pos, lit_len)
        if pos + lit_len > n:
            raise ValueError("LZ4 block truncated inside literals")
        if len(out) - start_len + lit_len > budget:
            raise ValueError(f"LZ4 block decodes to more than {budget} bytes")
        out += src[pos:pos + lit_len]
        pos += lit_len
        if pos == n:
            break  # last sequence: literals only
        if pos + 2 > n:
            raise ValueError("LZ4 block truncated inside a match offset")
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0:
            raise ValueError("LZ4 match offset 0")
        if offset > len(out):
            raise ValueError(f"LZ4 match offset {offset} reaches before the start of the output")
        match_len = token & 15
        if match_len == 15:
            match_len, pos = _read_length(src, pos, match_len)
        match_len += _MIN_MATCH
        if len(out) - start_len + match_len > budget:
            raise ValueError(f"LZ4 block decodes to more than {budget} bytes")
        begin = len(out) - offset
        if offset >= match_len:
            out += out[begin:begin + match_len]
        else:
            # overlapping copy: the pattern of ``offset`` bytes repeats
            pattern = bytes(out[begin:])
            reps = match_len // offset + 1
            out += (pattern * reps)[:match_len]
    if expected_size is not None and len(out) - start_len != expected_size:
        raise ValueError(f"LZ4 block decoded to {len(out) - start_len} bytes, expected {expected_size}")
    return out


def _emit_sequence(out: bytearray, literals: bytes, offset: int, match_len: int) -> None:
    """Append one sequence; ``offset == 0`` means the final literals-only sequence."""
    lit_len = len(literals)
    extra = match_len - _MIN_MATCH if offset else 0
    token = (min(lit_len, 15) << 4) | (min(extra, 15) if offset else 0)
    out.append(token)
    if lit_len >= 15:
        rest = lit_len - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out += literals
    if offset:
        out += struct.pack("<H", offset)
        if extra >= 15:
            rest = extra - 15
            while rest >= 255:
                out.append(255)
                rest -= 255
            out.append(rest)


def lz4_block_compress(data: bytes, level: int = 0) -> bytes:
    """Encode one raw LZ4 block (``len(data) <= BLOCK_SIZE`` is the caller's job).

    level 0: a single all-literal sequence.  level 1: greedy hash-table matcher.
    """
    data = bytes(data)
    out = bytearray()
    n = len(data)
    if level <= 0 or n < _MF_LIMIT + 1:
        _emit_sequence(out, data, 0, 0)
        return bytes(out)
    table: dict = {}
    anchor = 0
    pos = 0
    last_start = n - _MF_LIMIT       # last position where a match may start
    match_limit = n - _LAST_LITERALS  # matches may not extend past here
    while pos <= last_start:
        key = data[pos:pos + _MIN_MATCH]
        cand = table.get(key)
        table[key] = pos
        if cand is None or pos - cand > _MAX_OFFSET:
            pos += 1
            continue
        length = _MIN_MATCH
        while pos + length < match_limit and data[cand + length] == data[pos + length]:
            length += 1
        _emit_sequence(out, data[anchor:pos], pos - cand, length)
        pos += length
        anchor = pos
    _emit_sequence(out, data[anchor:], 0, 0)
    return bytes(out)


# --------------------------------------------------------------------------- Apple frame


def decompress(data: bytes) -> bytes:
    """Decode a ``bv41``/``bv4-`` block stream terminated by ``bv4$``.  Raises ``ValueError``."""
    data = bytes(data)
    out = bytearray()
    pos = 0
    n = len(data)
    while True:
        if pos + 4 > n:
            raise ValueError("Apple LZ4 stream ends without a bv4$ terminator")
        magic = data[pos:pos + 4]
        pos += 4
        if magic == MAGIC_END:
            return bytes(out)
        if magic == MAGIC_COMPRESSED:
            if pos + 8 > n:
                raise ValueError("truncated bv41 block header")
            decoded_size, encoded_size = struct.unpack_from("<II", data, pos)
            pos += 8
            if pos + encoded_size > n:
                raise ValueError("truncated bv41 block payload")
            if len(out) + decoded_size > MAX_STREAM_OUTPUT:
                raise ValueError(f"Apple LZ4 stream declares more than {MAX_STREAM_OUTPUT} bytes")
            lz4_block_decompress(data[pos:pos + encoded_size], out, decoded_size)
            pos += encoded_size
        elif magic == MAGIC_STORED:
            if pos + 4 > n:
                raise ValueError("truncated bv4- block header")
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
            if pos + size > n:
                raise ValueError("truncated bv4- block payload")
            if len(out) + size > MAX_STREAM_OUTPUT:
                raise ValueError(f"Apple LZ4 stream declares more than {MAX_STREAM_OUTPUT} bytes")
            out += data[pos:pos + size]
            pos += size
        else:
            raise ValueError(f"bad Apple LZ4 block magic {magic!r} at offset {pos - 4}")


def compress(data: bytes, level: int = 0) -> bytes:
    """Encode ``data`` as ``bv41`` blocks of at most BLOCK_SIZE (32 KiB) bytes plus ``bv4$``.

    Empty input encodes as just ``bv4$``.  Matches never cross block boundaries, so each block
    is independently decodable as well.
    """
    data = bytes(data)
    out = bytearray()
    for start in range(0, len(data), BLOCK_SIZE):
        chunk = data[start:start + BLOCK_SIZE]
        block = lz4_block_compress(chunk, level)
        out += MAGIC_COMPRESSED + struct.pack("<II", len(chunk), len(block)) + block
    out += MAGIC_END
    return bytes(out)


def split_blocks(data: bytes) -> List[tuple]:
    """List the blocks of a stream as ``(magic, decoded_size, encoded_size)`` tuples (debugging aid)."""
    data = bytes(data)
    blocks: List[tuple] = []
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        magic = data[pos:pos + 4]
        if magic == MAGIC_END:
            blocks.append((magic, 0, 0))
            return blocks
        if magic == MAGIC_COMPRESSED:
            if pos + 12 > n:
                break
            d, e = struct.unpack_from("<II", data, pos + 4)
            blocks.append((magic, d, e))
            pos += 12 + e
        elif magic == MAGIC_STORED:
            if pos + 8 > n:
                break
            (d,) = struct.unpack_from("<I", data, pos + 4)
            blocks.append((magic, d, d))
            pos += 8 + d
        else:
            break
    raise ValueError("Apple LZ4 stream is malformed or lacks a bv4$ terminator")
