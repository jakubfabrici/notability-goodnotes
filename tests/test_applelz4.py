"""Unit tests for gnnote.applelz4: vectors from docs/ecosystem.md section 3, block grammar
corner cases, both compression levels, python-lz4 cross-checks (skipped when absent) and
every stroke frame of every sample notebook.

The sample walk asserts invariants on every frame of every ``.goodnotes`` file the fixture
finds and pins exact counts only for the files of :data:`FRAME_EXPECTED` (the goodparse and
parser-for-goodnotes samples at their pinned commits), so an unknown sample cannot break it.
"""
from __future__ import annotations

import random
import struct
import zipfile
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import pytest

from gnnote import applelz4, protobuf as pb

try:  # optional oracle, never imported by the package
    import lz4.block as lz4_block
except ImportError:  # pragma: no cover
    lz4_block = None


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


# --------------------------------------------------------------------------- vectors


def test_hello_world_vector_from_doc():
    payload = b"hello world!!"
    expected = bytes.fromhex("62763431 0d000000 0e000000 d0".replace(" ", "")) + payload + b"bv4$"
    assert applelz4.compress(payload) == expected
    assert applelz4.decompress(expected) == payload


def test_range20_vector_from_doc():
    payload = bytes(range(20))
    expected = bytes.fromhex("62763431 14000000 16000000 f005".replace(" ", "")) + payload + b"bv4$"
    assert applelz4.compress(payload) == expected
    assert applelz4.decompress(expected) == payload


def test_literal_extension_rule_658_and_270():
    # stroke doc section 4.3: 658 - 15 = 255 + 255 + 133 -> ff ff 85 ; 270 - 15 = 255 + 0 -> ff 00
    frame = applelz4.compress(b"t" * 658)
    assert frame[:16] == b"bv41" + _u32(658) + _u32(662) + b"\xf0\xff\xff\x85"
    assert len(frame) == 678
    frame = applelz4.compress(b"t" * 270)
    assert frame[:15] == b"bv41" + _u32(270) + _u32(273) + b"\xf0\xff\x00"
    assert len(frame) == 289


def test_empty_payload_is_just_terminator():
    assert applelz4.compress(b"") == b"bv4$"
    assert applelz4.decompress(b"bv4$") == b""
    assert applelz4.is_apple_lz4(b"bv4$")
    assert applelz4.is_apple_lz4(b"bv41....")
    assert applelz4.is_apple_lz4(b"bv4-....")
    assert not applelz4.is_apple_lz4(b"tpl\x00")
    assert not applelz4.is_apple_lz4(b"")


@pytest.mark.parametrize("n", [0, 1, 14, 15, 16, 269, 270, 271, 1000, 32767, 32768, 32769, 65536, 200000])
def test_level0_round_trip_sizes(n):
    rng = random.Random(n)
    payload = bytes(rng.getrandbits(8) for _ in range(n))
    frame = applelz4.compress(payload)
    blocks = applelz4.split_blocks(frame)
    assert blocks[-1][0] == b"bv4$"
    assert len(blocks) - 1 == -(-n // applelz4.BLOCK_SIZE)
    assert all(b[1] <= applelz4.BLOCK_SIZE for b in blocks[:-1])
    assert applelz4.decompress(frame) == payload


def test_stored_block_single_size_word():
    frame = b"bv4-" + _u32(5) + b"hello" + b"bv41" + _u32(2) + _u32(3) + b"\x20!?" + b"bv4$"
    assert applelz4.decompress(frame) == b"hello!?"


def test_trailing_bytes_after_terminator_are_ignored():
    assert applelz4.decompress(b"bv4$garbage") == b""


def test_match_across_block_boundary():
    block1 = b"\x40abcd"                       # 4 literals
    block2 = b"\x10x\x04\x00" + b"\x10y"       # 1 literal, match offset 4 len 4, then 1 literal
    frame = (b"bv41" + _u32(4) + _u32(len(block1)) + block1
             + b"bv41" + _u32(6) + _u32(len(block2)) + block2 + b"bv4$")
    assert applelz4.decompress(frame) == b"abcdxbcdxy"


def test_overlapping_match_copy():
    block = b"\x15a\x01\x00" + b"\x10b"        # 'a', then 9 bytes at offset 1, then 'b'
    assert bytes(applelz4.lz4_block_decompress(block)) == b"a" * 10 + b"b"
    block = b"\x22ab\x02\x00" + b"\x00"        # 'ab', 6 bytes at offset 2, empty final literals
    assert bytes(applelz4.lz4_block_decompress(block)) == b"abababab"


def test_long_match_length_extension():
    block = b"\x1fa\x01\x00\xff\x05" + b"\x10z"   # 1 literal, match 15 + 255 + 5 + 4 = 279
    out = bytes(applelz4.lz4_block_decompress(block))
    assert out == b"a" * 280 + b"z"


@pytest.mark.parametrize("frame", [
    b"",
    b"bv4",
    b"xxxx",
    b"bv41" + _u32(1) + _u32(1) + b"\x10a",                    # no terminator
    b"bv41" + _u32(1),                                          # truncated header
    b"bv41" + _u32(1) + _u32(5) + b"\x10a" + b"bv4$",           # payload longer than the frame
    b"bv41" + _u32(2) + _u32(2) + b"\x10a" + b"bv4$",           # decoded size mismatch
    b"bv41" + _u32(5) + _u32(6) + b"\x10a\x00\x00\x10b" + b"bv4$",  # match offset 0
    b"bv41" + _u32(6) + _u32(6) + b"\x10a\x02\x00\x10b" + b"bv4$",  # offset before the start
    b"bv41" + _u32(3) + _u32(2) + b"\x30ab" + b"bv4$",          # literals truncated
    b"bv41" + _u32(3) + _u32(3) + b"\x10a\x01" + b"bv4$",       # offset truncated
    b"bv41" + _u32(20) + _u32(2) + b"\xf0\xff" + b"bv4$",       # length extension truncated
    b"bv4-" + _u32(9) + b"abc" + b"bv4$",                       # stored block truncated
    b"bv4-" + _u32(1),                                          # stored header truncated
])
def test_decompress_rejects_malformed(frame):
    with pytest.raises(ValueError):
        applelz4.decompress(frame)


# --------------------------------------------------------------------------- level 1


def _corpus():
    rng = random.Random(7)
    yield b"abc" * 1000
    yield bytes(rng.getrandbits(8) for _ in range(5000))
    yield b"x" * 70000
    yield (b"the quick brown fox " * 50 + bytes(rng.getrandbits(8) for _ in range(300))) * 40
    yield bytes(range(256)) * 300
    yield b"ab" * 7


def test_level1_round_trip_and_smaller():
    for payload in _corpus():
        frame = applelz4.compress(payload, level=1)
        assert applelz4.decompress(frame) == payload
        if payload.count(b"abc") > 10 or payload == b"x" * 70000:
            assert len(frame) < len(payload) // 10
    assert applelz4.compress(b"", level=1) == b"bv4$"


def test_level1_block_rules():
    data = b"abcd" * 10
    block = applelz4.lz4_block_compress(data, level=1)
    # ends with a literal-only sequence of at least 5 bytes
    assert bytes(applelz4.lz4_block_decompress(block, expected_size=len(data))) == data
    assert block.endswith(data[-5:])
    # inputs shorter than 13 bytes are all literals
    assert applelz4.lz4_block_compress(b"abababab", level=1) == b"\x80abababab"


@pytest.mark.skipif(lz4_block is None, reason="python-lz4 not installed")
def test_level1_blocks_decode_with_python_lz4():
    for payload in _corpus():
        for chunk_start in range(0, len(payload), applelz4.BLOCK_SIZE):
            chunk = payload[chunk_start:chunk_start + applelz4.BLOCK_SIZE]
            for level in (0, 1):
                block = applelz4.lz4_block_compress(chunk, level)
                assert lz4_block.decompress(block, uncompressed_size=len(chunk)) == chunk


@pytest.mark.skipif(lz4_block is None, reason="python-lz4 not installed")
def test_python_lz4_blocks_decode_with_our_decoder():
    for payload in _corpus():
        chunk = payload[:applelz4.BLOCK_SIZE]
        for kwargs in ({}, {"mode": "high_compression"}, {"mode": "fast", "acceleration": 8}):
            block = lz4_block.compress(chunk, store_size=False, **kwargs)
            assert bytes(applelz4.lz4_block_decompress(block, expected_size=len(chunk))) == chunk


# --------------------------------------------------------------------------- samples

# file -> (frames, frames with several bv41 blocks, frames with a bv4- stored block,
#          largest decoded TPL image in bytes).  GoodNotes splits at 32 KiB: the 100 212-byte
#          ribbon stroke of Test6 and the 133 538-byte one of Test7 are the multi-block
#          frames (docs/goodnotes-v35-strokes.md section 2), Test9 holds the one stored block.
FRAME_EXPECTED: Dict[str, Tuple[int, int, int, int]] = {
    "Test4.goodnotes": (5, 0, 0, 3798),
    "Test5.goodnotes": (46, 0, 0, 9745),
    "Test6.goodnotes": (22, 1, 0, 100212),
    "Test7.goodnotes": (36, 1, 0, 133538),
    "Test8.goodnotes": (10, 0, 0, 1793),
    "Test9.goodnotes": (186, 0, 1, 12823),
    "test.goodnotes": (2, 0, 0, 986),
    "test2.goodnotes": (1, 0, 0, 192),
    "test3.goodnotes": (2, 0, 0, 192),
    "ex1.goodnotes": (2807, 0, 0, 756),
    "ex2.goodnotes": (25, 0, 0, 30520),
    "ex3.goodnotes": (2740, 0, 0, 1008),
    "record.goodnotes": (49, 0, 0, 666),
}


def _stroke_frames(path: Path) -> Iterator[Tuple[str, bytes]]:
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.startswith("notes/"):
                continue
            for rec in pb.decode_records(z.read(name)):
                for content in pb.get_all(pb.decode_message(rec), 7):
                    geo = pb.get(pb.decode_message(content.value), 2)
                    if geo is not None and applelz4.is_apple_lz4(geo.value):
                        yield name, geo.value


def _walk_frame(frame: bytes) -> List[Tuple[bytes, int, bytes]]:
    """Independent parse of a frame into ``(magic, decoded_size, payload)`` blocks; the
    terminator must be the last four bytes."""
    blocks: List[Tuple[bytes, int, bytes]] = []
    pos = 0
    while True:
        magic = frame[pos:pos + 4]
        if magic == b"bv4$":
            assert pos + 4 == len(frame), "bytes after bv4$"
            return blocks
        if magic == b"bv41":
            dsize, csize = struct.unpack_from("<II", frame, pos + 4)
            blocks.append((magic, dsize, frame[pos + 12:pos + 12 + csize]))
            pos += 12 + csize
        elif magic == b"bv4-":
            (size,) = struct.unpack_from("<I", frame, pos + 4)
            blocks.append((magic, size, frame[pos + 8:pos + 8 + size]))
            pos += 8 + size
        else:
            raise AssertionError(f"unexpected block magic {magic!r}")


def _check_frame(frame: bytes, index: int) -> bytes:
    """Invariants of one GoodNotes-written frame; returns the decoded image."""
    blocks = _walk_frame(frame)
    assert blocks, "frame without a data block"
    assert [b[:2] for b in applelz4.split_blocks(frame)] == [(m, d) for m, d, _p in blocks] + [(b"bv4$", 0)]
    raw = applelz4.decompress(frame)
    assert len(raw) == sum(d for _m, d, _p in blocks)
    assert raw[:4] == b"tpl\x00"
    assert struct.unpack_from("<I", raw, 4)[0] == len(raw)
    # every block decodes on its own as well as with the previous output as history
    offset = 0
    for magic, dsize, payload in blocks:
        if magic == b"bv4-":
            assert payload == raw[offset:offset + dsize]
        else:
            assert bytes(applelz4.lz4_block_decompress(payload, expected_size=dsize)) == raw[offset:offset + dsize]
            if lz4_block is not None:
                assert lz4_block.decompress(payload, uncompressed_size=dsize) == raw[offset:offset + dsize]
                assert lz4_block.decompress(payload, uncompressed_size=dsize, dict=raw[:offset]) == raw[offset:offset + dsize]
        offset += dsize
    # re-framing with our writer round-trips and uses GoodNotes' block size
    packed = applelz4.compress(raw)
    assert applelz4.decompress(packed) == raw
    assert len(applelz4.split_blocks(packed)) - 1 == max(1, -(-len(raw) // applelz4.BLOCK_SIZE))
    if index % 25 == 0 or len(blocks) > 1 or blocks[0][0] == b"bv4-":
        packed = applelz4.compress(raw, level=1)
        assert applelz4.decompress(packed) == raw
        if lz4_block is not None:
            offset = 0
            for _m, dsize, payload in _walk_frame(packed):
                assert lz4_block.decompress(payload, uncompressed_size=dsize) == raw[offset:offset + dsize]
                offset += dsize
    return raw


def test_every_sample_frame_decodes_and_reframes(samples):
    for path in samples.goodnotes_files():
        count = multi = stored = max_block = 0
        largest = (0, b"")
        for index, (_name, frame) in enumerate(_stroke_frames(path)):
            raw = _check_frame(frame, index)
            count += 1
            blocks = _walk_frame(frame)
            multi += sum(1 for b in blocks if b[0] == b"bv41") > 1
            stored += any(b[0] == b"bv4-" for b in blocks)
            max_block = max(max_block, *(d for _m, d, _p in blocks))
            if len(raw) > largest[0]:
                largest = (len(raw), frame)
        assert count >= 1, path.name
        expected = samples.expected_for(path, FRAME_EXPECTED)
        if expected is not None:
            assert (count, multi, stored, largest[0]) == expected, path.name
            assert max_block <= applelz4.BLOCK_SIZE, path.name  # GoodNotes 2026 splits at 32 KiB
        # the largest image of every file also survives real compression
        raw = applelz4.decompress(largest[1])
        packed = applelz4.compress(raw, level=1)
        assert applelz4.decompress(packed) == raw
        if len(raw) > 512:
            assert len(packed) < len(raw), path.name
