"""Robustness of the OneNote reader: truncated and mutated files of both packagings, crafted
structures (loops, deep nesting, oversized counts), decoded-size budgets, data element
fragments and performance on the large samples.  Nothing may escape ``read_onenote`` but a
``ValueError``; every input must finish quickly with bounded memory."""
from __future__ import annotations

import random
import struct
import time
import tracemalloc
from pathlib import Path
from typing import List

import pytest

from gnnote.model import Document
from gnnote.onenote import common, ink, reader
from gnnote.onenote import read_onenote
from gnnote.onenote.native import NativeStore
from gnnote.onenote.package import PackageStore

SMALL_SAMPLES = (
    "libmson:resources/sample-drawing/Section 1.one",  # native, one blue stroke
    "joplin:packages/onenote-converter/test-data/ink.one",  # native, pressure
    "onenote.rs:crates/parser/tests/samples/New Section Group/New Section 1.one",  # OneDrive packaging
    "onenote.rs:crates/parser/tests/samples/joplin/Simple notebook/Quick Notes.one",  # OneDrive packaging
)
SECONDS_PER_READ = 5.0


def _sample(samples, key: str) -> Path:
    name, _, rel = key.partition(":")
    path = samples.repo(name) / rel
    if not path.is_file():
        pytest.skip(f"sample {key} not available")
    return path


def _read_ok(data: bytes) -> None:
    """``read_onenote`` returns a Document or raises ValueError, within the time budget."""
    started = time.monotonic()
    try:
        doc = read_onenote(data)
    except ValueError:
        doc = None
    elapsed = time.monotonic() - started
    assert elapsed < SECONDS_PER_READ, f"read took {elapsed:.1f} s"
    if doc is not None:
        assert isinstance(doc, Document)


@pytest.mark.parametrize("key", SMALL_SAMPLES)
def test_truncated_files_never_crash(samples, key: str) -> None:
    data = _sample(samples, key).read_bytes()
    cuts = sorted({0, 1, 16, 63, 64, 67, 68, 70, 100, 1023, 1024, len(data) - 1}
                  | {len(data) * i // 37 for i in range(1, 37)})
    for cut in cuts:
        if 0 <= cut < len(data):
            _read_ok(data[:cut])


@pytest.mark.parametrize("key", SMALL_SAMPLES)
def test_mutated_files_never_crash(samples, key: str) -> None:
    data = _sample(samples, key).read_bytes()
    rng = random.Random(f"gnnote-onenote-{key}")
    specials = (b"\xff\xff\xff\xff", b"\xff\xff\xff\x7f", b"\x00\x00\x00\x00", b"\x00\x00\x00\x80", b"\xfe\xff")
    for _ in range(120):
        mutated = bytearray(data)
        for _ in range(rng.randint(1, 4)):
            pos = rng.randrange(64, len(data))  # keep the header GUIDs: the input stays "OneNote"
            if rng.random() < 0.5:
                mutated[pos] ^= 1 << rng.randrange(8)
            else:
                patch = rng.choice(specials)
                mutated[pos:pos + len(patch)] = patch
        _read_ok(bytes(mutated[:len(data)]))


def test_random_bytes_behind_a_valid_header() -> None:
    rng = random.Random(7)
    one = bytes.fromhex("e4525c7b8cd8a74daeb15378d02996d3")
    for fmt in (bytes.fromhex("3fdd9a101b91f549a5d01791edc8aed8"), bytes.fromhex("2fe98d63d4a6c14b9a36b3fc2511a5b7")):
        for size in (64, 68, 100, 1024, 1100, 5000):
            for _ in range(10):
                body = bytes(rng.randrange(256) for _ in range(size - 64))
                _read_ok(one + bytes(32) + fmt + body)


# --------------------------------------------------------------------------- crafted structures


def test_property_set_counts_are_bounded_by_the_data() -> None:
    # a PropertySet announcing 0xFFFF properties in 6 bytes; then an ArrayOfObjectIDs whose
    # count (0xFFFFFFFF) exceeds the OIDs stream
    oids = struct.pack("<I", 0)  # empty OIDs stream, OSIDs present? no: bit 31 set below
    with pytest.raises(common.Damaged):
        common.decode_object_propset(struct.pack("<I", 0x80000000) + struct.pack("<H", 0xFFFF) + b"\x00" * 4,
                                     0, 10, lambda a, b, c: (a, b, c))
    prop = struct.pack("<I", (0x9 << 26) | 0x1234)  # type 0x9: ArrayOfObjectIDs
    blob = struct.pack("<I", 0x80000000) + struct.pack("<H", 1) + prop + struct.pack("<I", 0xFFFFFFFF)
    with pytest.raises(common.Damaged):
        common.decode_object_propset(blob, 0, len(blob), lambda a, b, c: (a, b, c))
    nested = struct.pack("<I", 0x80000000) + (struct.pack("<H", 1) + struct.pack("<I", 0x11 << 26)) * 40 + b"\x00\x00"
    with pytest.raises(common.Damaged, match="nested too deeply"):
        common.decode_object_propset(nested, 0, len(nested), lambda a, b, c: (a, b, c))
    assert len(oids) == 4


def test_deeply_nested_stream_objects_are_refused() -> None:
    one = bytes.fromhex("e4525c7b8cd8a74daeb15378d02996d3")
    package = bytes.fromhex("2fe98d63d4a6c14b9a36b3fc2511a5b7")
    # 0x7A packaging start (32-bit, compound) with a 17-byte payload, then 10 000 nested
    # compound 16-bit starts of type 0x15 and no ends
    head = struct.pack("<I", 2 | 4 | (0x7A << 3) | (17 << 17)) + b"\x00" + bytes(16)
    nested = struct.pack("<H", 4 | (0x15 << 3)) * 10_000
    data = one + bytes(32) + package + bytes(4) + head + nested
    with pytest.raises(ValueError, match="damaged"):
        read_onenote(data)


def test_file_node_list_loop_is_detected(samples) -> None:
    data = bytearray(_sample(samples, SMALL_SAMPLES[0]).read_bytes())
    store = NativeStore(bytes(data))
    stp, cb = store.fcr_root
    data[stp + cb - 20:stp + cb - 8] = struct.pack("<QI", stp, cb)  # nextFragment -> itself
    struct.pack_into("<I", data, 96, 0)  # no committed transactions: no node limit either
    _read_ok(bytes(data))


def test_ink_point_budget(monkeypatch: pytest.MonkeyPatch, samples) -> None:
    data = _sample(samples, SMALL_SAMPLES[1]).read_bytes()
    monkeypatch.setattr(ink, "MAX_POINTS", 50)
    doc = read_onenote(data)
    assert sum(len(s.points) for p in doc.pages for s in p.strokes) <= 50
    assert any("more ink than gnnote reads" in w for w in doc.warnings)


def test_picture_budget(monkeypatch: pytest.MonkeyPatch, samples) -> None:
    data = _sample(samples, "onenote.rs:crates/parser/tests/samples/Schnelle Notizen.one").read_bytes()
    monkeypatch.setattr(reader, "MAX_IMAGE_BYTES", 1000)
    doc = read_onenote(data)
    assert doc.pages[0].images == [] and any("pictures exceed" in w for w in doc.warnings)


def test_ink_path_announcing_too_many_values_is_dropped() -> None:
    raw = bytes([0xFE, 0xFF, 0xFF, 0xFF, 0x0F]) + b"\x02" * 10  # announces ~2^31 values
    with pytest.raises(common.Damaged):
        ink.decode_path(raw)


# --------------------------------------------------------------------------- FSSHTTPB fragments


def _cu64(v: int) -> bytes:
    if v == 0:
        return b"\x00"
    for width in range(1, 8):
        if v < 1 << (7 * width):
            return ((v << width) | (1 << (width - 1))).to_bytes(width, "little")
    return b"\x80" + v.to_bytes(8, "little")


def _fragments(element: bytes, size: int, group: bytes) -> bytes:
    """``element`` (a whole serialized data element) as Data Element Fragment elements."""
    out = b""
    for n, start in enumerate(range(0, len(element), size)):
        chunk = element[start:start + size]
        body = bytes([4 | (1 << 3)]) + group  # fragment extended GUID: the same for every piece
        body += _cu64(len(element)) + _cu64(start) + _cu64(len(chunk)) + chunk
        payload = bytes([(n % 31) << 3 | 4]) + bytes(range(16)) + b"\x00" + _cu64(6)  # element id, serial, type 6
        out += struct.pack("<H", 4 | (0x01 << 3) | (len(payload) << 9)) + payload
        if len(body) < 0x7FFF:
            out += struct.pack("<I", 2 | (0x6A << 3) | (len(body) << 17)) + body
        else:
            out += struct.pack("<I", 2 | (0x6A << 3) | (0x7FFF << 17)) + _cu64(len(body)) + body
        out += bytes([(0x01 << 2) | 1])  # data element end
    return out


def test_fragmented_data_elements_are_reassembled(samples) -> None:
    data = _sample(samples, "onenote.rs:crates/parser/tests/samples/Schnelle Notizen.one").read_bytes()
    store = PackageStore(data)
    blob = max((e for e in store._elements.values() if e.type == 10), key=lambda e: e.end - e.pos)
    element = data[blob.pos:blob.end]
    group = bytes.fromhex("00112233445566778899aabbccddeeff")
    pieces: List[bytes] = [_fragments(element, size, group) for size in (100_000, 70_000)]
    original = read_onenote(data)
    for fragmented in pieces:
        rebuilt = data[:blob.pos] + fragmented + data[blob.end:]
        doc = read_onenote(rebuilt)
        assert [len(p.strokes) for p in doc.pages] == [len(p.strokes) for p in original.pages]
        assert [i.data for i in doc.pages[0].images] == [i.data for i in original.pages[0].images]
    # a missing piece: the element is incomplete, the picture is lost, nothing crashes
    first = _fragments(element, 100_000, group)
    end_of_first = first.index(bytes([(0x01 << 2) | 1]) + struct.pack("<H", 4 | (0x01 << 3) | (19 << 9))) + 1
    doc = read_onenote(data[:blob.pos] + first[end_of_first:] + data[blob.end:])
    assert any("incomplete" in w or "picture" in w for w in doc.warnings)


# --------------------------------------------------------------------------- performance


def _timed_read(data: bytes):
    tracemalloc.start()
    started = time.monotonic()
    try:
        doc = read_onenote(data)
    finally:
        elapsed = time.monotonic() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    return doc, elapsed, peak


def test_large_picture_section_is_fast_and_bounded(samples) -> None:
    data = _sample(samples, "Interop-TestSuites:FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources/"
                            "LargeData.one").read_bytes()  # 11 MB, 135 pictures
    doc, elapsed, peak = _timed_read(data)
    assert len(doc.pages[0].images) == 135
    assert elapsed < 10.0, elapsed
    assert peak < 4 * len(data) + 64 * 1024 * 1024, peak  # the pictures themselves plus overhead
    assert any("much larger than a OneNote page" in w for w in doc.warnings)  # 135 pictures in one outline


def test_46_mb_section_is_fast_and_bounded(samples) -> None:
    """Kev744/py-onenote-parser's 46 MB section (LARGE in conftest: only when present)."""
    data = _sample(samples, "py-onenote-parser:Equipe Euro 2016.one").read_bytes()
    doc, elapsed, peak = _timed_read(data)
    assert [len(p.strokes) for p in doc.pages] == [13] and len(doc.pages[0].images) == 1
    assert elapsed < 10.0, elapsed
    assert peak < 2 * len(data), peak
