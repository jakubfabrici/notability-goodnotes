"""OneNote ``.onepkg`` notebook packages: the Microsoft Cabinet reader (gnnote.onenote.cab),
the LZX decoder (gnnote.onenote.lzx) and the package path of the OneNote reader.

Ground truth comes from two independent sides:

* the makecab fixtures of the Obsidian Importer (``obsidian-importer`` in tests/conftest.py,
  ``tests/onenote-file/fixtures``, made by Windows ``makecab`` and published by OfficeIMO
  under MIT): their expected bytes are rebuilt here or pinned by SHA-256;
* cabinets built in the tests (tests/cab_builder.py): stored and MSZIP folders with the
  standard library, LZX folders with a small encoder written from [MS-PATCH], whose output
  was checked against the MIT-licensed ``lzxd`` decoder during development.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
import struct
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from gnnote import formats
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.notability.reader import read_note
from gnnote.onenote import cab as cabmod
from gnnote.onenote import lzx as lzxmod
from gnnote.onenote import read_onenote
from gnnote.onenote.cab import Cabinet, CabError
from gnnote.onenote.lzx import LzxError, lzx_decompress
from gnnote.pdf.reader import read_pdf
from tests import cab_builder as cb
from tests.test_onenote_synthetic import _ink_page, _section

FIXTURES = "tests/onenote-file/fixtures"
E8_NAMES = ["makecab-lzx-e8.cab"] + [f"makecab-lzx{bits}-e8.cab" for bits in range(15, 21)]
# (cabinet, expanded section, its size, its SHA-256) as SOURCE.md of the fixtures records them
SECTION_CABS = [
    ("makecab-lzx-testOneNote2016.cab", "testOneNote2016.one", 14744,
     "fcfc3c2e65482dc6f70f6a613b058e908f67db2ebb16a343bc2367e02bbb471c"),
    ("makecab-lzx-testOneNoteFromOffice365-2.cab", "testOneNoteFromOffice365-2.one", 69986,
     "8cd245ed549043534118a00ce29715147c880c38ca88c3481acc19ae28e980c2"),
]


def _fixture(samples, name: str) -> Path:
    path = samples.repo("obsidian-importer") / FIXTURES / name
    if not path.is_file():
        pytest.skip(f"fixture {name} not available")
    return path


def _e8_payload() -> bytes:
    """The 4,096 bytes every makecab-lzx*-e8.cab holds: a text pattern with three x86 calls."""
    pattern = b"OfficeIMO-LZX-E8-independent-oracle-"
    payload = bytearray((pattern * (4096 // len(pattern) + 1))[:4096])
    for offset, displacement in ((64, 500), (1024, -50), (2048, 1_000_000)):
        payload[offset] = 0xE8
        struct.pack_into("<i", payload, offset + 1, displacement)
    return bytes(payload)


def _section_bytes(title: str, xs: Sequence[int]) -> bytes:
    """A synthetic one-page section with one stroke (tests/onenote_builder.py)."""
    page = _ink_page({}, xs=list(xs), ys=[0, 500, 1000][:len(xs)])
    return _section([page], title=title)


def _notebook(method: int = cb.LZX, **kw: object) -> bytes:
    """A package of two synthetic sections and no table of contents."""
    return cb.build_cab([[("Beta.one", _section_bytes("Beta", [0, 1000, 2000])),
                          ("Alpha.one", _section_bytes("Alpha", [0, 500, 900]))]], method, **kw)


def _corpus(seed: int, size: int) -> bytes:
    """Compressible bytes with some x86 call markers: text, binary records and repeats."""
    rng = random.Random(seed)
    words = [bytes(rng.randrange(97, 123) for _ in range(rng.randrange(2, 9))) for _ in range(40)]
    out = bytearray()
    while len(out) < size:
        r = rng.random()
        if r < 0.4:
            out += rng.choice(words) + b" "
        elif r < 0.6:
            out += struct.pack("<IHB", rng.randrange(1 << 20), rng.randrange(300), rng.randrange(3))
        elif r < 0.7:
            out += b"\xe8" + struct.pack("<i", rng.randrange(-5000, 50000))
        elif r < 0.9 and len(out) > 600:
            start = rng.randrange(len(out) - 500)
            out += out[start:start + rng.randrange(3, 400)]
        else:
            out += bytes(rng.randrange(256) for _ in range(rng.randrange(1, 30)))
    return bytes(out[:size])


# =========================================================================== makecab fixtures


@pytest.mark.parametrize("name", E8_NAMES)
def test_makecab_e8_fixtures_expand_to_the_rebuilt_pattern(samples, name: str) -> None:
    cab = Cabinet(_fixture(samples, name).read_bytes())
    assert [(m.name, m.size, m.folder, m.offset) for m in cab.members] == [("officeimo-lzx-e8.bin", 4096, 0, 0)]
    bits = 21 if name == "makecab-lzx-e8.cab" else int(name[11:13])
    assert cab.description(0) == f"LZX (2^{bits} window)"
    assert cab.read(cab.members[0]) == _e8_payload()


@pytest.mark.parametrize("cab_name, one_name, size, sha256", SECTION_CABS)
def test_makecab_section_cabinets_expand_byte_for_byte(samples, cab_name: str, one_name: str, size: int,
                                                       sha256: str) -> None:
    cab = Cabinet(_fixture(samples, cab_name).read_bytes())
    (member,) = cab.members
    assert (member.name, member.size) == (one_name, size)
    data = cab.read(member)
    assert hashlib.sha256(data).hexdigest() == sha256
    loose = [p for p in samples.onenote_files() if p.name == one_name]
    assert loose and all(p.read_bytes() == data for p in loose)  # the same section in the sample repositories
    doc = read_onenote(_fixture(samples, cab_name).read_bytes())
    alone = read_onenote(data)
    assert [len(p.texts) for p in doc.pages] == [len(p.texts) for p in alone.pages]
    assert doc.title == one_name[:-4]  # a one-section package is named after the section's file


def test_makecab_notebook_package(samples) -> None:
    data = _fixture(samples, "makecab-lzx-notebook.onepkg").read_bytes()
    cab = Cabinet(data)
    assert [(m.name, m.size, m.offset) for m in cab.members] == [
        ("Open Notebook.onetoc2", 1680, 0), ("Compressed section.one", 3880, 1680)]
    assert len(cab.folders) == 1 and cab.description(0) == "LZX (2^21 window)"
    assert detect_format("Notebook.onepkg", data) == "onenote"
    assert detect_format("renamed.bin", data) == "onenote"  # by content
    doc = read_onenote(data)
    assert doc.title == "Compressed section" and len(doc.pages) == 1
    assert sorted(t.text for t in doc.pages[0].texts) == ["Compressed page", "LZX package content"]
    result = convert(data, "Notebook.onepkg", Options(target="notability"))
    assert result.filename == "Notebook.note" and result.source_format == "onenote"
    back = read_note(result.data)
    assert len(back.pages) == 1 and back.pages[0].texts


def test_flipped_bit_fails_the_checksum(samples) -> None:
    data = bytearray(_fixture(samples, "makecab-lzx-testOneNote2016.cab").read_bytes())
    start = struct.unpack_from("<I", data, 36)[0]  # the first folder's first CFDATA
    data[start + 8] ^= 0x01
    cab = Cabinet(bytes(data))  # the directory is intact
    with pytest.raises(CabError, match="data block 1 of 1 fails its checksum"):
        cab.read(cab.members[0])
    with pytest.raises(ValueError, match=r"sections could be unpacked \(damaged cabinet: data block 1 of 1 fails"):
        read_onenote(bytes(data))


def test_e8_fixtures_are_not_claimed_by_any_format(samples) -> None:
    for name in E8_NAMES:
        data = _fixture(samples, name).read_bytes()
        assert not any(f.sniff(data, None) for f in formats.FORMATS.values()), name
        with pytest.raises(ValueError, match="no .one section files"):
            read_onenote(data)  # a cabinet, but not a notebook
        with pytest.raises(ValueError, match="not a supported note file"):
            detect_format(name, data)


# =========================================================================== LZX round trips


@pytest.mark.parametrize("bits", range(15, 22))
def test_lzx_every_window_size(bits: int) -> None:
    data = _corpus(bits, 3 * cb.FRAME + 1234)
    frames, sizes = cb.lzx_compress(data, bits, e8=12_000_000)
    assert len(frames) == 4
    assert lzx_decompress(frames, sizes, bits) == data


@pytest.mark.parametrize("blocks", [
    [(cb.UNCOMPRESSED, 70001)],  # one odd uncompressed block across three frames
    [(cb.ALIGNED, 30000), (cb.UNCOMPRESSED, 2767), (cb.VERBATIM, 37234)],  # the odd block ends a frame
    [(cb.VERBATIM, 1), (cb.UNCOMPRESSED, 1), (cb.ALIGNED, 2), (cb.UNCOMPRESSED, 3), (cb.VERBATIM, 69994)],
    [(cb.ALIGNED, 70001)],
    [(cb.VERBATIM, 40000), (cb.VERBATIM, 30001)],  # a block boundary inside a frame, trees delta-coded
    [(cb.UNCOMPRESSED, 32767), (cb.UNCOMPRESSED, 1), (cb.VERBATIM, 37233)],  # blocks ending on the frame end
    [((cb.UNCOMPRESSED, cb.VERBATIM, cb.ALIGNED)[k % 3], 2187) for k in range(32)] + [(cb.ALIGNED, 1)],
])
@pytest.mark.parametrize("pad_in_next_frame", [True, False])
def test_lzx_block_layouts(blocks: List[Tuple[int, int]], pad_in_next_frame: bool) -> None:
    data = _corpus(len(blocks), sum(size for _, size in blocks))
    frames, sizes = cb.lzx_compress(data, 17, blocks=blocks, e8=1_000_000, pad_in_next_frame=pad_in_next_frame)
    assert lzx_decompress(frames, sizes, 17) == data


def test_lzx_path_length_runs_literals_only_and_long_matches() -> None:
    data = _corpus(5, 50_000)
    for kwargs in ({"runs": False}, {"matches": False}, {}):
        frames, sizes = cb.lzx_compress(data, 16, **kwargs)
        assert lzx_decompress(frames, sizes, 16) == data
    zeros = bytes(100_000)  # matches of the longest length (257) on repeated offset 1
    frames, sizes = cb.lzx_compress(zeros, 15)
    assert sum(map(len, frames)) < 1000
    assert lzx_decompress(frames, sizes, 15) == zeros
    tiny = b"x"
    assert lzx_decompress(*cb.lzx_compress(tiny, 15), 15) == tiny


def test_lzx_uncompressed_block_after_a_header_on_a_word_boundary() -> None:
    """An uncompressed block whose header ends exactly on a 16-bit boundary is preceded by a
    whole padding word (1 to 16 bits of padding, never 0)."""
    seen = 0
    original = cb.BitWriter.align_raw

    def counting(self: cb.BitWriter) -> None:
        nonlocal seen
        seen += self.n == 0
        original(self)

    for first in range(1, 120):
        data = _corpus(first, first + 500)
        cb.BitWriter.align_raw = counting  # type: ignore[method-assign]
        try:
            before = seen
            frames, sizes = cb.lzx_compress(data, 15, blocks=[(cb.VERBATIM, first), (cb.UNCOMPRESSED, 500)])
        finally:
            cb.BitWriter.align_raw = original  # type: ignore[method-assign]
        assert lzx_decompress(frames, sizes, 15) == data
        if seen > before:
            return
    pytest.fail("no layout put an uncompressed block header on a word boundary")


def test_e8_translation_at_frame_ends() -> None:
    """Markers in the last 10 bytes of a frame stay as they are; translation starts afresh in
    every frame; negative targets and the translation size's edges are handled."""
    data = bytearray(_corpus(9, 2 * cb.FRAME + 100))
    for p, target in ((cb.FRAME - 11, 5), (cb.FRAME - 10, 5), (cb.FRAME - 3, 7), (cb.FRAME + 1, -3),
                      (cb.FRAME + 6, 999_999), (2 * cb.FRAME + 80, 1)):
        data[p] = 0xE8
        struct.pack_into("<i", data, p + 1, target)
    data[2 * cb.FRAME - 40:2 * cb.FRAME] = b"\xe8" * 40  # a run of markers
    frames, sizes = cb.lzx_compress(bytes(data), 21, e8=1_000_000)
    assert lzx_decompress(frames, sizes, 21) == data


def _undo_e8_reference(out: bytearray, frames: Sequence[Tuple[int, int]], size: int) -> int:
    """The translation undone marker by marker; returns the number of markers visited."""
    visited = 0
    for start, length in frames:
        if length <= 10:
            continue
        p = start
        while p < start + length - 10:
            if out[p] != 0xE8:
                p += 1
                continue
            visited += 1
            value = struct.unpack_from("<i", out, p + 1)[0]
            if -p <= value < size:
                struct.pack_into("<I", out, p + 1, (value - p if value >= 0 else value + size) & 0xFFFFFFFF)
            p += 5
    return visited


def _e8_fixed_units(out: bytes, frames: Sequence[Tuple[int, int]]) -> int:
    """The units the E8 pass charges whatever the markers' targets: for every frame with an
    ``E8`` byte before its last 10 bytes, one unit per E8_SKIPS_PER_UNIT markers it may step
    over in C (no more than its ``E8`` bytes, nor than one per five bytes) and the expression
    of each megabyte of output it moves into."""
    units, reach = 0, -1
    for start, length in frames:
        stop = start + length - 10
        if length <= 10 or not out.count(0xE8, start, stop):
            continue
        markers = min(out.count(0xE8, start, stop), math.ceil((stop - start) / 5))
        units += math.ceil(markers / lzxmod.E8_SKIPS_PER_UNIT)
        if math.ceil((stop - 1) / lzxmod.E8_REACH_STEP) != reach:
            reach = math.ceil((stop - 1) / lzxmod.E8_REACH_STEP)
            units += lzxmod.E8_PATTERN_UNITS
    return units


def _frames_of(size: int) -> List[Tuple[int, int]]:
    return [(k, min(cb.FRAME, size - k)) for k in range(0, size, cb.FRAME)]


def _e8_soup(rng: random.Random, size: int) -> bytearray:
    """Markers whose targets fall inside, near and far outside the translated range, runs of
    ``E8`` bytes and other bytes."""
    out = bytearray()
    while len(out) < size:
        r = rng.random()
        if r < 0.25:
            out += b"\xe8" * rng.randrange(1, 40)
        elif r < 0.6:
            top = rng.choice([0, 0, 0xFF, 0xFF, 0xFE, 0xC0, 0xBF, 0x80, 0x7F, 0x01, 0xB7, 0xE8, rng.randrange(256)])
            out += bytes([0xE8, rng.randrange(256), rng.randrange(256), rng.randrange(256), top])
        else:
            out += bytes(rng.randrange(256) for _ in range(rng.randrange(1, 10)))
    return out[:size]


def test_e8_pass_matches_a_marker_by_marker_reference() -> None:
    rng = random.Random(3)
    for _ in range(200):
        out = _e8_soup(rng, 3000)
        frames, start = [], 0
        while start < len(out):
            frames.append((start, min(rng.choice([1000, 7, 11, 2000]), len(out) - start)))
            start += frames[-1][1]
        size = rng.choice([1000, 12_000_000, 0x7FFFFFFF, 1, 1 << 24, (1 << 24) + 1])
        expected = bytearray(out)
        _undo_e8_reference(expected, frames, size)
        lzxmod._undo_e8(out, frames, size, None)
        assert out == expected
    # far into the output, where markers with negative targets are translated too: frames
    # are given by their place in the output, so only these few are filled in
    big = bytearray(0x40000000 // 16)
    frames = [(start, cb.FRAME) for start in (0x00FF0000, 0x01000000 - 100, 0x02345678, 0x03FF8000)]
    for start, length in frames:
        big[start:start + length] = _e8_soup(rng, length)
    for size in (12_000_000, 0x7FFFFFFF, 1):
        expected = bytearray(big)
        for frame in frames:
            _undo_e8_reference(expected, [frame], size)
        out = bytearray(big)
        lzxmod._undo_e8(out, frames, size, None)
        assert out == expected
    # markers whose targets lie just inside or outside -p (their own place) and -reach (how far
    # back the expression looks for a frame), around the steps of reach
    step = lzxmod.E8_REACH_STEP
    frames = [(start, cb.FRAME) for start in (step - 20_000, 3 * step - cb.FRAME, 3 * step + 5, 7 * step - 11)]
    for start, length in frames:
        body = _e8_soup(rng, length)
        for k in range(0, length - 5, 13):
            q = start + k
            reach = -(-(start + length - 11) // step) * step
            body[k:k + 5] = b"\xe8" + struct.pack("<i", rng.choice([-q, -q - 1, -q + 1, -q - 5, -reach, -reach + 1,
                                                                    -reach - 1, -step, -step - 1]))
        big[start:start + length] = body
    for size in (12_000_000, 0x7FFFFFFF, 1):
        expected = bytearray(big)
        for frame in frames:
            _undo_e8_reference(expected, [frame], size)
        out = bytearray(big)
        lzxmod._undo_e8(out, frames, size, None)
        assert out == expected
    # runs of E8 bytes are stepped over in C: they cost the expressions (one per megabyte of
    # output) and a unit per three markers (here one per five bytes), no Python work
    solid = bytearray(b"\xe8" * 3_000_000)
    frames = _frames_of(len(solid))
    units = _e8_fixed_units(solid, frames)
    assert units == 3 * lzxmod.E8_PATTERN_UNITS + 91 * math.ceil(32758 / 5 / 3) + math.ceil(18102 / 5 / 3)
    budget = lzxmod.E8Budget(units)
    lzxmod._undo_e8(solid, frames, 12_000_000, budget)
    assert solid == b"\xe8" * 3_000_000 and budget.left == 0


def test_e8_expression_classifies_targets_exactly() -> None:
    """The run the expression hands to Python holds exactly the markers whose target, read as
    unsigned, is below the translation size or at least 2^32 - reach (reach: at least the
    place of the frame's last marker, rounded up to E8_REACH_STEP)."""
    rng = random.Random(8)
    step = lzxmod.E8_REACH_STEP
    for size in (1, 2, 0xFF, 0x100, 0x1_0000, 12_000_000, 0x0123_4567, 0x7FFF_FFFF, rng.randrange(1, 1 << 31)):
        for reach in (0, step, 5 * step, 1025 * step):
            low = (1 << 32) - reach
            values = {0, 1, size - 1, size, size + 1, low - 1, low, low + 1, (1 << 32) - 1, 0x00E8_0000,
                      0xE8E8_E8E8, 0xFF9F_C5E8}
            for bound in (size, low):
                for shift in (0, 8, 16, 24):  # the bound with one byte one higher or lower
                    values |= {(bound + (1 << shift)) % (1 << 32), (bound - (1 << shift)) % (1 << 32)}
            values |= {rng.randrange(1 << 32) for _ in range(200)}
            match = lzxmod._e8_pattern(size, reach).match
            never = b"a\xe8" + size.to_bytes(4, "little")  # a byte that is not E8, a marker never rewritten
            for value in (v for v in values if 0 <= v < 1 << 32):
                maybe = value < size or value >= low
                marker = b"\xe8" + value.to_bytes(4, "little")
                assert match(marker).span(1) == ((0, 5) if maybe else (-1, -1)), (size, reach, hex(value))
                assert match(never + marker).span(1) == ((6, 11) if maybe else (-1, -1))
    for lo, hi in ((0, 0), (5, 5), (0, (1 << 32) - 1), (0x0102_0304, 0x0102_0305), (0x00FF_FFFF, 0x0100_0000),
                   (12_000_000, (1 << 32) - step - 1)):
        alternatives = lzxmod._ranges(lo, hi)
        assert len(alternatives) <= 7
        for value in {lo, hi, lo - 1, hi + 1, (lo + hi) // 2} | {rng.randrange(1 << 32) for _ in range(100)}:
            if 0 <= value < 1 << 32:
                inside = any(all(a <= byte <= b for (a, b), byte in zip(alt, value.to_bytes(4, "big")))
                             for alt in alternatives)
                assert inside == (lo <= value <= hi)


def test_e8_expression_avoids_what_old_interpreters_match_wrongly() -> None:
    """CPython 3.11.0 to 3.11.4 match possessive quantifiers and atomic groups wrongly (with
    them, the makecab notebook decoded to garbage and 0 pages there); the expression uses
    neither, never needs to backtrack, and the import-time check confirms it on this
    interpreter with a case that holds the minimal pattern those versions got wrong."""
    for size, reach in ((12_000_000, lzxmod.E8_REACH_STEP), (1, 0), (0x7FFF_FFFF, 1 << 30)):
        source = lzxmod._e8_pattern(size, reach).pattern
        for construct in (b"++", b"*+", b"?+", b"}+", b"(?>", b"(?=", b"(?<", b"\\1"):
            assert construct not in source
    assert lzxmod._E8_REGEX_OK and lzxmod._e8_regex_works()
    data, frames, size = lzxmod._e8_check_case()
    assert data.startswith(b"ab\xe8\x01\x02\x03\x05cd\xe8\x01\x02\x03\x00\xe8\x00\x00\x00\xffzz")
    expected = bytearray(data)
    _undo_e8_reference(expected, frames, size)
    out = bytearray(data)
    lzxmod._undo_e8(out, frames, size, None)
    assert out == expected != data


def test_e8_expressions_are_charged_per_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compiling an expression costs about as much as a thousand rewritten markers, and folders
    with distinct translation sizes each need one: every folder pays for the expressions it
    uses, whether they were cached or not, so many small folders stay within the budget."""
    data = b"\xe8\x01\x00\x00\x00" + bytes(range(1, 40))
    folders, streams = [], []
    for k in range(40):
        streams.append(cb.lzx_compress(data, 15, e8=1000 + k))
        folders.append([(f"S{k}.one", data)])
    package = cb.build_cab(folders, frames=streams, window_bits=15)
    # ten folders' worth: the expression, the frame's one E8 byte, the marker and its match
    monkeypatch.setattr(cabmod, "E8_MIN", 10 * (lzxmod.E8_PATTERN_UNITS + 3))
    monkeypatch.setattr(cabmod, "E8_PER_BYTE", 0)
    for _ in range(2):  # the same with the expressions cached
        cab = Cabinet(package)
        results = []
        for member in cab.members:
            try:
                results.append(cab.read(member) is not None)
            except CabError as exc:
                assert "x86 call markers" in str(exc)
                results.append(False)
        assert results == [True] * 10 + [False] * 30


def test_e8_plain_loop_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should the import-time check fail (an interpreter whose ``re`` misbehaves), every marker
    is visited in Python and costs one unit: the same output, still bounded work."""
    monkeypatch.setattr(lzxmod, "_e8_pattern", lambda size, reach: re.compile(  # misses every marker
        b"(?:[^\\xe8]+|\\xe8....)*((?:\\xe8....)+)?", re.DOTALL))
    assert not lzxmod._e8_regex_works()
    monkeypatch.setattr(lzxmod, "_e8_pattern", lambda size, reach: re.compile(b"(?!)"))  # never matches
    assert not lzxmod._e8_regex_works()
    monkeypatch.undo()
    monkeypatch.setattr(lzxmod, "_E8_REGEX_OK", False)
    rng = random.Random(4)
    for _ in range(50):
        out = _e8_soup(rng, 3000)
        frames = [(k, min(1000, len(out) - k)) for k in range(0, len(out), 1000)]
        size = rng.choice([1000, 12_000_000, 0x7FFFFFFF, 1])
        expected = bytearray(out)
        visited = _undo_e8_reference(expected, frames, size)
        budget = lzxmod.E8Budget(visited)
        lzxmod._undo_e8(out, frames, size, budget)
        assert out == expected and budget.left == 0
        with pytest.raises(lzxmod.LzxBudgetError):
            lzxmod._undo_e8(bytearray(out), frames, size, lzxmod.E8Budget(visited - 1))
    data = bytearray(_corpus(2, 3 * cb.FRAME))
    data[100:105] = b"\xe8" + struct.pack("<i", 77)
    frames, sizes = cb.lzx_compress(bytes(data), 17, e8=1_000_000)
    assert lzx_decompress(frames, sizes, 17) == data


def _periodic_lzx(pattern: bytes, size: int, translated: Optional[bytes] = None,
                  head: bytes = b"", e8: int = 12_000_000) -> Tuple[List[bytes], List[int]]:
    """``head`` and then ``pattern`` repeated up to ``size`` bytes as one LZX folder with x86
    call translation on (translation size ``e8``, 2^21 window); ``translated`` is what the
    encoder compresses."""
    data = head + (pattern * (size // len(pattern) + 1))[:size - len(head)]
    blocks = []
    left = len(data)
    while left:
        blocks.append((cb.VERBATIM, min(left, 1 << 23)))
        left -= blocks[-1][1]
    return cb.lzx_compress(data, 21, e8=e8, blocks=blocks, translated=translated if translated else data,
                           matcher=cb.PeriodicMatcher(len(pattern), len(head)))


# uniform bitmap areas whose pixels hold an E8 byte: (pixel bytes as stored, megabytes)
E8_BITMAPS = [
    (b"\xe8\xc5\x9f\xff", 20),  # #9FC5E8, BGRA: targets FF9FC5E8-like, far below -p
    (b"\xe8\xf0\xff", 20),  # #FFF0E8, BGR
    (b"\x00\x00\xe8", 20),  # #E80000, BGR: every target is 00E80000, positive, top byte 00
    (b"\x00\xe8\x00", 8),  # #00E800
    (b"\xe8\x00\x00", 8),  # #0000E8
    (b"\x00\xe8\xe8", 8),  # #E8E800
    (b"\xe8\x00\xff", 8),  # #FF00E8: targets 00E8FF00
    (b"\xe8\xe8\xe8", 8),  # grey 232: E8E8E8E8
    (b"\xe8\x00", 8),  # 16-bit pixels
]


@pytest.mark.parametrize("pattern, megabytes", E8_BITMAPS, ids=[p.hex() for p, _ in E8_BITMAPS])
def test_valid_e8_dense_data_fits_the_budget(pattern: bytes, megabytes: int) -> None:
    """A uniform bitmap area whose colour holds an E8 byte puts a marker every few bytes and
    compresses about 1000:1.  The encoder left its markers alone (their targets are outside
    the translated range, lzxd agrees), and the decoder steps over them in C: they cost the
    expressions (one per megabyte) and a unit per three markers stepped over, no Python work.
    The classification is exact for positive targets, so a marker such as ``E8 00 00 E8 00``
    (target 00E80000, beyond the translation size of 12,000,000 although its top byte is 0)
    is not handed to Python.  Such areas fit a small package's budget up to about 50 MB."""
    size = megabytes << 20
    frames, sizes = _periodic_lzx(pattern, size)
    assert sum(map(len, frames)) < size // 500
    expected = (pattern * (size // len(pattern) + 1))[:size]
    units = _e8_fixed_units(expected, _frames_of(size))
    assert units <= megabytes * (lzxmod.E8_PATTERN_UNITS + 70_000)  # one unit per 15 bytes at most
    start = time.perf_counter()
    budget = lzxmod.E8Budget(units)
    out = lzx_decompress(frames, sizes, 21, e8_budget=budget)
    assert time.perf_counter() - start < 10 and budget.left == 0
    assert out == expected
    data = cb.build_cab([[("Notebook\\Scans.one", bytes(size))]], frames=[(frames, sizes)])
    assert units < cabmod.E8_MIN + cabmod.E8_PER_BYTE * len(data) - 2_500_000  # far within the package's
    cab = Cabinet(data)
    assert cab.read(cab.members[0]) == out


@pytest.mark.parametrize("pattern", [b"\xe8\xc5\x9f\xff", b"\x00\x00\xe8"], ids=["9FC5E8", "E80000"])
def test_package_with_a_large_e8_dense_attachment_reads_every_section(pattern: bytes) -> None:
    """A package is one folder: a refused folder would make every section unreadable."""
    diary = _section_bytes("Diary", [0, 1000, 2000])
    scans = _section_bytes("Scans", [0, 500, 900])
    scans_padded = scans + pattern * ((20 << 20) // len(pattern))  # a section followed by 20 MB of the bitmap area
    assert len(read_onenote(scans_padded).pages) == 1
    head = diary + scans
    cut = (len(head) // cb.FRAME + 1) * cb.FRAME  # the sections' own bytes are translated, frame by frame
    data = diary + scans_padded
    frames, sizes = _periodic_lzx(pattern, len(data), head=head,
                                  translated=cb.e8_translate(data[:cut], 12_000_000) + data[cut:])
    package = cb.build_cab([[("Notebook\\Diary.one", diary), ("Notebook\\Scans.one", scans_padded)]],
                           frames=[(frames, sizes)])
    doc = read_onenote(package)
    assert len(doc.pages) == 2 and not any("could not be unpacked" in w for w in doc.warnings)


# crafted markers the expression steps over at its slowest (every target in the last of its
# alternatives, back to back or one byte apart) and runs of E8: (pattern, translation size)
SLOW_E8 = [
    (b"\x01\xe8\x67\x45\x23\x01", 0x0123_4567),
    (b"\xe8\x00\x20\xb7\x00", 12_000_000),
    (b"\xe8\xff\x1c\xb7\x00", 0x00B7_1C23),
    (b"\xe8", 12_000_000),
]


@pytest.mark.parametrize("pattern, size", SLOW_E8, ids=[p.hex() for p, _ in SLOW_E8])
def test_e8_markers_stepped_over_in_c_are_charged(pattern: bytes, size: int) -> None:
    """Stepping over markers in C is not free: crafted targets cost up to about 95 ns each, and
    LZX reaches about 1000:1, so a package of 1.3 MB can expand to 1 GB of them (21 seconds
    when only the expressions were charged).  A unit per three markers keeps the pass within
    about 0.3 microseconds per unit, the cost of a rewritten marker."""
    out = bytearray((pattern * ((2 << 20) // len(pattern) + 1))[:2 << 20])
    frames = _frames_of(len(out))
    expected = bytearray(out)
    visits = lzxmod.E8Budget(1 << 40)
    lzxmod._undo_e8_plain(expected, frames, size, visits)  # one unit per marker
    markers = (1 << 40) - visits.left
    units = _e8_fixed_units(out, frames)
    assert expected == out and markers > len(out) // 7
    assert markers / 3 <= units - 2 * lzxmod.E8_PATTERN_UNITS <= markers / 3 + len(frames)
    budget = lzxmod.E8Budget(units)
    start = time.perf_counter()
    lzxmod._undo_e8(out, frames, size, budget)
    elapsed = time.perf_counter() - start
    assert out == expected and budget.left == 0
    assert elapsed < units * 2e-6  # measured: 0.1 to 0.3 microseconds per unit
    with pytest.raises(lzxmod.LzxBudgetError):
        lzxmod._undo_e8(bytearray(expected), frames, size, lzxmod.E8Budget(units - 1))


def test_crafted_package_of_e8_markers_stops_at_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A package of about 1.3 MB that expands to 1 GB of the slowest markers once spent 21
    seconds in the E8 pass; scaled down here (12 MB from 13 KB, a budget of 200,000 units plus
    4 per byte), it is refused once the budget is spent, with the message that does not call
    the data damaged."""
    pattern, size = SLOW_E8[0]
    total = 12 << 20
    data = (pattern * (total // len(pattern) + 1))[:total]
    frames, sizes = _periodic_lzx(pattern, total, e8=size)
    quarter = total // 4
    package = cb.build_cab([[(f"Notebook\\S{k}.one", data[k * quarter:(k + 1) * quarter]) for k in range(4)]],
                           frames=[(frames, sizes)])
    assert len(package) < 20_000
    monkeypatch.setattr(cabmod, "E8_MIN", 200_000)
    needed = _e8_fixed_units(data, _frames_of(total))
    assert needed > 2 * (cabmod.E8_MIN + cabmod.E8_PER_BYTE * len(package))
    start = time.perf_counter()
    with pytest.raises(ValueError, match=r"sections could be unpacked \(the package holds more x86 call markers"):
        read_onenote(package)
    assert time.perf_counter() - start < 10
    monkeypatch.setattr(cabmod, "E8_MIN", needed)  # with enough budget the same folder expands
    cab = Cabinet(package)
    assert cab.read(cab.members[1]) == data[quarter:2 * quarter]


def _dense_call_folder(markers: int) -> Tuple[List[bytes], List[int]]:
    """An LZX folder whose compressed stream holds ``E8 00 00 00 00`` over and over: every
    marker's target is 0, inside the translated range, so each one is rewritten."""
    stream = b"\xe8\x00\x00\x00\x00" * markers
    return cb.lzx_compress(stream, 16, e8=12_000_000, translated=stream, matcher=cb.PeriodicMatcher(5))


def test_e8_budget_is_shared_by_every_folder_of_a_package(monkeypatch: pytest.MonkeyPatch) -> None:
    frames, sizes = _dense_call_folder(20_000)  # 100,000 bytes, about 19,990 markers rewritten
    out = lzx_decompress(frames, sizes, 16)
    expected = bytearray(b"\xe8\x00\x00\x00\x00" * 20_000)
    _undo_e8_reference(expected, [(k, min(cb.FRAME, len(expected) - k)) for k in range(0, len(expected), cb.FRAME)],
                       12_000_000)
    assert out == expected
    folders = [[(f"S{k}.one", bytes(sum(sizes)))] for k in range(3)]
    data = cb.build_cab(folders, frames=[(frames, sizes)] * 3, window_bits=16)
    monkeypatch.setattr(cabmod, "E8_MIN", 30_000)  # one folder's markers fit, two do not
    monkeypatch.setattr(cabmod, "E8_PER_BYTE", 0)
    cab = Cabinet(data)
    assert cab.read(cab.members[0]) == out
    for member in cab.members[1:]:
        with pytest.raises(CabError) as caught:
            cab.read(member)
        assert str(caught.value) == ("the package holds more x86 call markers than gnnote restores for a file of "
                                     "its size")  # not called damaged: such data may be valid
    budget = lzxmod.E8Budget(30_000)  # the same budget, shared by hand
    lzx_decompress(frames, sizes, 16, e8_budget=budget)
    with pytest.raises(lzxmod.LzxBudgetError):
        lzx_decompress(frames, sizes, 16, e8_budget=budget)


def test_e8_budget_bounds_the_whole_package_in_time() -> None:
    """Twelve folders of 1,000,000 rewritten markers each (5 MB from 7 KB) need far more than
    the package's budget: after it, every further folder fails at once (a budget per folder
    would have let each through)."""
    frames, sizes = _dense_call_folder(1_000_000)
    folders = [[(f"S{k}.one", bytes(sum(sizes)))] for k in range(12)]
    data = cb.build_cab(folders, frames=[(frames, sizes)] * 12, window_bits=16)
    assert len(data) < 100_000
    cab = Cabinet(data)
    start = time.perf_counter()
    results = []
    for member in cab.members:
        try:
            cab.read(member)
            results.append(True)
        except CabError as exc:
            assert "x86 call markers" in str(exc)
            results.append(False)
    assert results[:3] == [True] * 3 and not any(results[5:])
    assert time.perf_counter() - start < 30
    assert _fails_fast(data, 30.0) is not None


# =========================================================================== cabinets


def test_stored_mszip_and_lzx_folders_reserves_and_names() -> None:
    files_a = [("Notebook\\Section A.one", _corpus(1, 50_000)), ("Notebook/Empty.bin", b"")]
    files_b = [("Notebook\\Grüppe\\B.one", _corpus(2, 70_000) * 2), ("Tiny.one", b"xyz")]
    for method in (cb.NONE, cb.MSZIP, cb.LZX):
        for reserves in ({}, {"header_reserve": 20, "folder_reserve": 3, "data_reserve": 5}, {"reserve_flag": True}):
            data = cb.build_cab([files_a, files_b], method, window_bits=18, **reserves)
            cab = Cabinet(data)
            names = [m.name for m in cab.members]
            assert names == ["Notebook/Section A.one", "Notebook/Empty.bin", "Notebook/Grüppe/B.one", "Tiny.one"]
            assert [cab.read(m) for m in cab.members] == [d for _, d in files_a + files_b]
            assert cab.folder_size(1) == 140_003
    half = _corpus(3, cb.FRAME)[cb.FRAME // 2:]
    twice = _corpus(3, cb.FRAME) + half * 2  # the second block starts with bytes of the first
    frames, sizes = cb.mszip_compress(twice)
    assert len(frames[1]) < 1000  # the second block refers to the first through the dictionary
    cab = Cabinet(cb.build_cab([[("twice.bin", twice)]], cb.MSZIP, frames=[(frames, sizes)]))
    assert cab.read(cab.members[0]) == twice


def test_name_encodings_and_checksums() -> None:
    flagged = cb.build_cab([[("Caf\u00e9\\\u65e5\u8a18.one", b"1")]], cb.NONE)  # UTF-8 with the 0x80 attribute
    assert [m.name for m in Cabinet(flagged).members] == ["Caf\u00e9/\u65e5\u8a18.one"]
    legacy = cb.build_cab([[("CafX.one", b"1")]], cb.NONE).replace(b"CafX", b"Caf\xe9")  # code page 1252, no flag
    assert [m.name for m in Cabinet(legacy).members] == ["Caf\u00e9.one"]
    unflagged = cb.build_cab([[("CafXY.one", b"1")]], cb.NONE).replace(b"CafXY", "Caf\u00e9".encode())
    assert [m.name for m in Cabinet(unflagged).members] == ["Caf\u00e9.one"]  # UTF-8 without the flag
    no_sums = cb.build_cab([[("a.one", b"abc" * 1000)]], cb.LZX, checksums=False)
    cab = Cabinet(no_sums)
    assert cab.read(cab.members[0]) == b"abc" * 1000
    assert cabmod.checksum(b"\x01\x02\x03\x04\x05\x06\x07") == cb.cab_checksum(b"\x01\x02\x03\x04\x05\x06\x07")
    rng = random.Random(4)
    for n in range(0, 40):
        blob = bytes(rng.randrange(256) for _ in range(n))
        assert cabmod.checksum(blob, 0x1234) == cb.cab_checksum(blob, 0x1234)


def test_multi_cabinet_sets_and_quantum_are_refused() -> None:
    plain = cb.build_cab([[("a.one", b"x" * 10)]], cb.NONE)
    for flag in (1, 2, 3):
        data = plain[:30] + struct.pack("<H", flag) + plain[32:]
        with pytest.raises(CabError, match="multi-file cabinet set"):
            Cabinet(data)
        with pytest.raises(ValueError, match="not a readable OneNote package: the package is one part"):
            read_onenote(data)
    continued = bytearray(plain)
    files_offset = struct.unpack_from("<I", continued, 16)[0]
    struct.pack_into("<H", continued, files_offset + 8, 0xFFFD)  # continued from the previous cabinet
    with pytest.raises(CabError, match="continue in another cabinet"):
        Cabinet(bytes(continued))
    quantum = cb.build_cab([[("a.one", b"x" * 10)]], cb.NONE, type_compress=0x0F02)
    with pytest.raises(CabError, match="Quantum"):
        Cabinet(quantum).read(Cabinet(quantum).members[0])
    with pytest.raises(ValueError, match=r"sections could be unpacked \(the package uses Quantum compression"):
        read_onenote(quantum)
    unknown = cb.build_cab([[("a.one", b"x" * 10)]], cb.NONE, type_compress=7)
    with pytest.raises(ValueError, match="unknown compression method"):
        read_onenote(unknown)
    for bits in (14, 22):
        wrong_window = cb.build_cab([[("a.one", b"x" * 10)]], cb.LZX, type_compress=3 | bits << 8,
                                    frames=[cb.lzx_compress(b"x" * 10, 15)])
        with pytest.raises(ValueError, match="unsupported LZX window"):
            read_onenote(wrong_window)


# =========================================================================== the package path


def test_package_without_toc_reads_sections_in_file_name_order() -> None:
    for method in (cb.NONE, cb.MSZIP, cb.LZX):
        doc = read_onenote(_notebook(method))
        assert [len(p.strokes) for p in doc.pages] == [1, 1] and doc.title == ""
        assert any(w.endswith("sections were merged into one document, in this order: Alpha, Beta")
                   for w in doc.warnings)
        assert any("file-name order" in w for w in doc.warnings)
    result = convert(_notebook(), "My Notebook.onepkg", Options(target="goodnotes"))
    assert result.filename == "My Notebook.goodnotes" and result.stats["pages"] == 2
    assert len(read_goodnotes(result.data).pages) == 2


def test_package_error_paths_and_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.onenote import reader
    ink = _section_bytes("Ink", [0, 1000, 2000])
    with pytest.raises(ValueError, match="the package holds no .one section files"):
        read_onenote(cb.build_cab([[("readme.txt", b"x")]], cb.NONE))
    with pytest.raises(ValueError, match="only OneNote's recycle bin"):
        read_onenote(cb.build_cab([[("OneNote_RecycleBin\\OneNote_DeletedPages.one", ink)]], cb.LZX))
    with pytest.raises(ValueError, match=r"table of contents \(.onetoc2\) but no sections"):
        read_onenote(cb.build_cab([[("Open Notebook.onetoc2", b"x")]], cb.LZX))
    with pytest.raises(ValueError, match="none of the notebook's sections could be read"):
        read_onenote(cb.build_cab([[("broken.one", b"garbage")]], cb.MSZIP))
    # one damaged folder among two: the other folder's sections are read
    good = cb.build_cab([[("Ink.one", ink)], [("Lost.one", ink)]], cb.LZX)
    bad = bytearray(good)
    second = struct.unpack_from("<I", bad, 36 + 8)[0]
    bad[second + 20] ^= 0xFF
    doc = read_onenote(bytes(bad))
    assert len(doc.pages) == 1
    assert any(w.startswith("File(s) that could not be unpacked were skipped (damaged cabinet: data block 1 of 1 "
                            "fails its checksum): Lost.one") for w in doc.warnings)
    # a file pointing at a folder the cabinet does not have
    misplaced = bytearray(cb.build_cab([[("Ink.one", ink), ("Gone.one", ink)]], cb.NONE))
    files_offset = struct.unpack_from("<I", misplaced, 16)[0]
    second_file = misplaced.index(b"Ink.one\x00") + 8
    assert struct.unpack_from("<H", misplaced, files_offset + 8)[0] == 0
    struct.pack_into("<H", misplaced, second_file + 8, 5)
    doc = read_onenote(bytes(misplaced))
    assert len(doc.pages) == 1
    assert "1 file(s) of the package name a folder the package does not have and were skipped" in doc.warnings
    # size guards: per member (checked before the folder is expanded), per folder and in total
    monkeypatch.setattr(reader, "MAX_MEMBER_BYTES", len(ink) - 1)

    def no_expansion(self: Cabinet, index: int) -> bytearray:
        raise AssertionError("a folder was expanded for an oversized member")

    with monkeypatch.context() as m:
        m.setattr(Cabinet, "expand", no_expansion)
        with pytest.raises(ValueError, match=r"none of the notebook's sections could be read \(too large"):
            read_onenote(cb.build_cab([[("Ink.one", ink)]], cb.LZX))
    monkeypatch.setattr(reader, "MAX_MEMBER_BYTES", 1 << 30)
    monkeypatch.setattr(reader, "MAX_TOTAL_BYTES", len(ink) + 10)
    doc = read_onenote(cb.build_cab([[("A.one", ink)], [("B.one", ink)]], cb.MSZIP))
    assert len(doc.pages) == 1
    assert any(w.startswith("File(s) too large once unpacked") and w.endswith(": B.one") for w in doc.warnings)


def test_package_folder_larger_than_the_budget_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.onenote import reader
    ink = _section_bytes("Ink", [0, 1000, 2000])
    monkeypatch.setattr(reader, "MAX_TOTAL_BYTES", len(ink) + 10)  # the folder expands to twice that
    with pytest.raises(ValueError, match=r"none of the notebook's sections could be read \(too large once unpacked\)"):
        read_onenote(cb.build_cab([[("A.one", ink), ("B.one", ink)]], cb.MSZIP))


def test_onedrive_notebook_folder_as_a_package(samples) -> None:
    """The notebook folder of onenote.rs, packed the way OneNote for Windows exports a
    notebook (one LZX folder, backslash paths, the recycle bin included)."""
    from tests.test_onenote import _sample
    base = _sample(samples, "onenote.rs:crates/parser/tests/samples/joplin/Notebook created on OneNote App/"
                            "Section.one").parent
    files = [(path.relative_to(base).as_posix().replace("/", "\\"), path.read_bytes())
             for path in sorted(base.rglob("*")) if path.is_file()]
    data = cb.build_cab([files], cb.LZX, window_bits=21, e8=12_000_000)
    assert detect_format("Notebook created on OneNote App.onepkg", data) == "onenote"
    doc = read_onenote(data)
    assert len(doc.pages) == 1 + 2 + 2 + 1  # Section, Section A1, Section B1, Section D1; no recycle bin
    assert any("4 sections were merged" in w and w.endswith("Section, Section A1, Section B1, Section D1")
               for w in doc.warnings)
    assert any("recycle bin" in w for w in doc.warnings)
    result = convert(data, "Notebook created on OneNote App.onepkg", Options(target="notability"))
    assert result.filename == "Notebook created on OneNote App.note" and result.stats["pages"] == 6


def test_desktop_sections_follow_the_package_table_of_contents(samples) -> None:
    from tests.test_onenote import _sample
    folder = _sample(samples, "Interop-TestSuites:FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources/"
                              "OneWithFileData.one").parent
    names = ["OneWithoutFileData.one", "OneWithFileData.one", "AlternativePackaging.one", "Encryption.one",
             "Open Notebook.onetoc2"]
    doc = read_onenote(cb.build_cab([[(n, (folder / n).read_bytes()) for n in names]], cb.LZX))
    merged = next(w for w in doc.warnings if "merged" in w)
    assert merged.endswith("OneWithFileData, OneWithoutFileData, AlternativePackaging"), merged
    assert any("Password-protected" in w and "Encryption" in w for w in doc.warnings)


def test_makecab_package_toc_orders_an_added_section(samples) -> None:
    """The fixture's .onetoc2 (written by OfficeIMO) lists only its section, which then comes
    before a section the table of contents does not know, whatever their names."""
    cab = Cabinet(_fixture(samples, "makecab-lzx-notebook.onepkg").read_bytes())
    files = [(m.name, cab.read(m)) for m in cab.members] + [("A first by name.one", _section_bytes("A", [0, 9]))]
    doc = read_onenote(cb.build_cab([files], cb.MSZIP))
    merged = next(w for w in doc.warnings if "merged" in w)
    assert merged.endswith("in this order: Compressed section, A first by name"), merged
    assert not any("file-name order" in w for w in doc.warnings)


def test_inked_package_converts_to_every_target(samples, tmp_path: Path) -> None:
    from tests.test_onenote import _sample
    handwriting = _fixture(samples, "handwriting_recognition.one").read_bytes()
    ink = _sample(samples, "joplin:packages/onenote-converter/test-data/ink.one").read_bytes()
    data = cb.build_cab([[("Lecture\\Handwriting.one", handwriting), ("Lecture\\Ink.one", ink)]], cb.LZX,
                        window_bits=21, e8=12_000_000)
    doc = read_onenote(data)
    strokes = [len(p.strokes) for p in doc.pages]
    assert strokes == [62, 0, 34] and doc.title == "Lecture"
    src = tmp_path / "Lecture.onepkg"
    src.write_bytes(data)
    for target, ext in (("notability", ".note"), ("goodnotes", ".goodnotes"), ("pdf", ".pdf")):
        assert main(["convert", str(src), "--to", target]) == 0
        out = (tmp_path / ("Lecture" + ext)).read_bytes()
        back = {"notability": read_note, "goodnotes": read_goodnotes, "pdf": read_pdf}[target](out)
        assert len(back.pages) == 3, target
        if target != "pdf":
            assert [len(p.strokes) for p in back.pages] == strokes, target


# =========================================================================== hostile input


def _fails_fast(data: bytes, budget: float = 5.0) -> Optional[str]:
    """``None`` when ``read_onenote`` reads ``data``, else its ValueError message; never slow."""
    start = time.perf_counter()
    try:
        read_onenote(data)
        message = None
    except ValueError as exc:
        message = str(exc)
    assert time.perf_counter() - start < budget
    return message


def test_truncated_cabinets_are_value_errors(samples) -> None:
    for data in (_notebook(cb.LZX), _notebook(cb.MSZIP), _fixture(samples, "makecab-lzx-notebook.onepkg").read_bytes()):
        for cut in sorted({*range(0, 200, 7), *range(200, len(data), max(1, len(data) // 60))}):
            message = _fails_fast(data[:cut])
            assert message is not None, cut


def test_bad_offsets_and_counts() -> None:
    data = bytearray(_notebook(cb.LZX))
    cases: Dict[str, Tuple[int, str, int]] = {
        "files offset past the end": (16, "<I", 0x7FFFFFF0),
        "file count beyond the table": (28, "<H", 500),
        "folder count beyond the file": (26, "<H", 60000),
        "folder data past the end": (36, "<I", len(data) + 100),
        "CFDATA count beyond the file": (40, "<H", 65535),
    }
    expected = {
        "files offset past the end": "truncated cabinet: file 1 of 2 is missing",
        "file count beyond the table": "truncated cabinet",
        "folder count beyond the file": "truncated cabinet: folder",
        "folder data past the end": "damaged cabinet: data block 1 of",
        "CFDATA count beyond the file": "folders declare more data blocks than the file holds",
    }
    for label, (offset, fmt, value) in cases.items():
        bad = bytearray(data)
        struct.pack_into(fmt, bad, offset, value)
        message = _fails_fast(bytes(bad))
        assert message is not None and expected[label] in message, (label, message)
    first = struct.unpack_from("<I", data, 36)[0]
    oversized = bytearray(data)
    struct.pack_into("<H", oversized, first + 6, 40000)  # a CFDATA that expands past 32 KB
    assert "expands to 40000 bytes" in (_fails_fast(bytes(oversized)) or "")
    member = bytearray(data)
    files_offset = struct.unpack_from("<I", data, 16)[0]
    struct.pack_into("<I", member, files_offset + 4, 10_000_000)  # a file beyond its folder's data
    doc = read_onenote(bytes(member))
    assert len(doc.pages) == 1 and "File(s) that could not be unpacked were skipped (damaged cabinet: Beta.one " \
        "extends past the data of its folder): Beta.one" in doc.warnings
    cab = Cabinet(bytes(member))
    with pytest.raises(CabError, match="extends past the data of its folder"):
        cab.read(cab.members[0])
    with pytest.raises(CabError, match="more than gnnote reads"):
        Cabinet(data[:28] + struct.pack("<H", cabmod.MAX_FILES + 1) + data[30:])
    long_name = cb.build_cab([[("x" * 2000 + ".one", b"1")]], cb.NONE)
    with pytest.raises(CabError, match="longer than 1024 bytes"):
        Cabinet(long_name)
    with pytest.raises(CabError, match="no MSCF header"):
        Cabinet(b"MSCX" + bytes(60))


def test_expansion_ratio_is_bounded() -> None:
    frames = [b"\x00"] * 200, [cb.FRAME] * 200  # 6.5 MB declared from 200 bytes
    data = cb.build_cab([[("a.one", b"")]], cb.LZX, frames=[frames], window_bits=15)
    with pytest.raises(CabError, match="more than compression can achieve"):
        Cabinet(data).folder_size(0)


def _share_first_folder(data: bytes, folders: int) -> bytes:
    """``data`` (a cabinet of ``folders`` folders) with every folder pointing at the first one's
    data blocks."""
    bad = bytearray(data)
    first = bad[36:44]
    for k in range(1, folders):
        bad[36 + 8 * k:44 + 8 * k] = first
    return bytes(bad)


def test_folders_sharing_data_blocks_are_refused() -> None:
    ink = _section_bytes("Ink", [0, 1000, 2000])
    data = _share_first_folder(cb.build_cab([[("A.one", ink)], [("B.one", ink)]], cb.LZX), 2)
    cab = Cabinet(data)
    assert cab.read(cab.members[0]) == ink
    with pytest.raises(CabError, match="two folders share data blocks"):
        cab.read(cab.members[1])
    doc = read_onenote(data)
    assert len(doc.pages) == 1
    assert "File(s) that could not be unpacked were skipped (damaged cabinet: two folders share data blocks): " \
        "B.one" in doc.warnings
    # partly shared: the second folder starts inside the first one's blocks
    two = cb.build_cab([[("A.one", _corpus(5, 40_000))], [("B.one", _corpus(6, 40_000))]], cb.NONE)
    tail = bytearray(two)
    first = struct.unpack_from("<I", tail, 36)[0]
    struct.pack_into("<IH", tail, 44, first + 8 + cb.FRAME, 1)  # the first folder's second block
    cab = Cabinet(bytes(tail))
    cab.folder_size(0)
    with pytest.raises(CabError, match="two folders share data blocks"):
        cab.folder_size(1)
    cab = Cabinet(bytes(tail))  # in the other order
    cab.folder_size(1)
    with pytest.raises(CabError, match="two folders share data blocks"):
        cab.folder_size(0)
    # the data reserve counts too: blocks of 8 + 200 bytes, more than the file holds
    reserved = bytearray(cb.build_cab([[("A.one", ink)]], cb.NONE, data_reserve=200))
    struct.pack_into("<H", reserved, 40 + 4, len(reserved) // 208 + 1)  # the folder record follows the sizes
    with pytest.raises(CabError, match="more data blocks than the file holds"):
        Cabinet(bytes(reserved)).folder_size(0)
    # a walk that failed counts again: a damaged folder cannot be walked over and over for free
    walks = Cabinet(_share_first_folder(cb.build_cab([[("A.one", ink)], [("B.one", ink)]], cb.LZX), 2))
    walks.folder_size(0)
    for _ in range(3):
        with pytest.raises(CabError):
            walks.folder_size(1)
    assert walks._walked > len(walks.data)


def test_many_folders_sharing_one_costly_stream_stay_cheap() -> None:
    """Two hundred folders reuse the data blocks of one: 256 KB of two-byte matches, the
    slowest LZX stream there is.  Only the first folder is decoded (before, every folder was,
    from a file of a few kilobytes)."""
    stream = b"AB" * (1 << 17)
    frames, sizes = cb.lzx_compress(stream, 16, translated=stream, matcher=cb.PeriodicMatcher(2, longest=2))
    count = 200
    folders = [[(f"S{k}.one", bytes(len(stream)))] for k in range(count)]
    data = _share_first_folder(cb.build_cab(folders, frames=[(frames, sizes)] * count, window_bits=16), count)
    cut = struct.unpack_from("<I", data, 36)[0] + sum(8 + len(f) for f in frames)
    data = data[:cut]  # the copies of the data blocks are gone: only the shared ones are left
    data = data[:8] + struct.pack("<I", len(data)) + data[12:]
    cab = Cabinet(data)
    start = time.perf_counter()
    assert cab.read(cab.members[0]) == stream
    single = time.perf_counter() - start
    for member in cab.members[1:]:
        with pytest.raises(CabError, match="overlap or add up|share data blocks|more data blocks than"):
            cab.read(member)
    assert time.perf_counter() - start < 5 + 3 * single
    _fails_fast(data, 5 + 3 * single)


def test_declared_sizes_alone_allocate_nothing() -> None:
    """A folder of 32,000 empty data blocks that each declare 32 KB (1 GB in all): the output
    grows frame by frame, so the first bad frame fails before any large allocation."""
    import tracemalloc
    frames, sizes = [bytes(32)] * 32000, [cb.FRAME] * 32000
    tracemalloc.start()
    try:
        with pytest.raises(LzxError, match="invalid block header"):
            lzx_decompress(frames, sizes, 21)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 << 20
    files = [(f"s{k}.one", bytes(0)) for k in range(5)]
    data = bytearray(cb.build_cab([files], frames=[(frames, sizes)], checksums=False))
    files_offset = struct.unpack_from("<I", data, 16)[0]
    part = 32000 * cb.FRAME // 5
    offset = files_offset
    for k in range(5):  # five files of about 200 MB each
        struct.pack_into("<II", data, offset, part, k * part)
        offset = data.index(b"\x00", offset + 16) + 1
    tracemalloc.start()
    try:
        message = _fails_fast(bytes(data))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert message is not None and "invalid block header" in message
    assert peak < 64 << 20


def test_memory_errors_while_unpacking_are_value_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime with capped memory (a browser worker) may fail to hold a folder: that folder
    counts as too large, and ``read_onenote`` still raises nothing but ValueError."""
    real = cabmod.lzx_decompress
    calls: List[int] = []

    def second_fails(*args: object, **kwargs: object) -> bytearray:
        calls.append(1)
        if len(calls) == 2:
            raise MemoryError
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cabmod, "lzx_decompress", second_fails)
    ink = _section_bytes("Ink", [0, 1000, 2000])
    doc = read_onenote(cb.build_cab([[("A.one", ink)], [("B.one", ink)]], cb.LZX, window_bits=16))
    assert len(doc.pages) == 1 and len(calls) == 2
    assert any(w.startswith("File(s) too large once unpacked") and w.endswith(": B.one") for w in doc.warnings)

    def no_memory(*args: object, **kwargs: object) -> bytearray:
        raise MemoryError

    monkeypatch.setattr(cabmod, "lzx_decompress", no_memory)
    with pytest.raises(ValueError, match=r"none of the notebook's sections could be read \(too large once unpacked\)"):
        read_onenote(_notebook(cb.LZX))


def test_folders_are_released_after_their_last_member() -> None:
    from gnnote.onenote import reader
    ink = _section_bytes("Ink", [0, 1000, 2000])
    package = reader._Package(cb.build_cab([[("A.one", ink), ("B.one", ink)], [("C.one", ink)]], cb.LZX))
    assert package.read("A.one") == ink and package.cab.is_expanded(0)
    assert package.read("C.one") == ink and not package.cab.is_expanded(1)  # its only member was read
    assert package.read("B.one") == ink and not package.cab.is_expanded(0)
    with pytest.raises(CabError, match="not expanded again"):  # never decoded twice
        package.cab.read(package.files["A.one"])
    assert package.read("A.one") is None and package.unreadable == ["A.one"]


def _lzx_frames(bits: Sequence[Tuple[int, int]], size: int) -> Tuple[List[bytes], List[int]]:
    """One frame of hand-written bits (value, count) expanding to ``size`` bytes."""
    bw = cb.BitWriter()
    for value, count in bits:
        bw.write(value, count)
    bw.align()
    return [bytes(bw.out) + bytes(4)], [size]


def _verbatim_header(size: int) -> List[Tuple[int, int]]:
    return [(0, 1), (cb.VERBATIM, 3), (size, 24)]


def _pretree(lengths: Sequence[int]) -> List[Tuple[int, int]]:
    return [(n, 4) for n in lengths]


@pytest.mark.parametrize("bits, size, message", [
    ([(0, 1), (0, 3), (10, 24)], 10, "invalid block header"),  # block type 0
    ([(0, 1), (5, 3), (10, 24)], 10, "invalid block header"),  # block type 5
    ([(0, 1), (cb.VERBATIM, 3), (0, 24)], 10, "invalid block header"),  # an empty block
    (_verbatim_header(10) + _pretree([1] * 20), 10, "the pretree is oversubscribed"),
    (_verbatim_header(10) + _pretree([0] * 20), 10, "not in its tree"),  # an empty pretree
    ([(0, 1), (cb.UNCOMPRESSED, 3), (10, 24), (0, 4)] + [(0, 16)] * 6, 10, "repeated offset outside the window"),
    ([(0, 1), (cb.ALIGNED, 3), (10, 24)] + [(1, 3)] * 8, 10, "the aligned-offset tree is oversubscribed"),
])
def test_invalid_lzx_streams(bits: List[Tuple[int, int]], size: int, message: str) -> None:
    frames, sizes = _lzx_frames(bits, size)
    with pytest.raises(LzxError, match=message):
        lzx_decompress(frames, sizes, 15)


def _verbatim_stream(symbols: Sequence[int], size: int, bits: int = 15) -> Tuple[List[bytes], List[int]]:
    """One verbatim block of main-tree ``symbols`` (no footers, no length tree) expanding to ``size``."""
    bw = cb.BitWriter()
    bw.write(0, 1)
    bw.write(cb.VERBATIM, 3)
    bw.write(size, 24)
    main_size = 256 + 8 * len(cb.position_slots(1 << bits)[0])
    freqs = [0] * main_size
    for symbol in symbols:
        freqs[symbol] += 1
    lengths = cb.huffman_lengths(freqs, 16)
    prev = [0] * main_size
    cb.write_lengths(bw, prev, lengths, 0, 256)
    cb.write_lengths(bw, prev, lengths, 256, main_size)
    cb.write_lengths(bw, [0] * 249, [0] * 249, 0, 249)
    codes = cb.canonical_codes(lengths)
    for symbol in symbols:
        bw.write(codes[symbol], lengths[symbol])
    bw.align()
    return [bytes(bw.out)], [size]


def test_lzx_matches_outside_the_window_or_across_frames() -> None:
    a = ord("a")
    assert lzx_decompress(*_verbatim_stream([a, 256 + 1], 4), 15) == b"aaaa"  # 'a', then 3 more from offset 1
    with pytest.raises(LzxError, match="outside the window"):  # a repeated offset before any output
        lzx_decompress(*_verbatim_stream([256, a], 3), 15)
    with pytest.raises(LzxError, match="crosses a block or frame boundary"):  # 'a' + 8 bytes in a 5-byte block
        lzx_decompress(*_verbatim_stream([a, 256 + 6], 5), 15)
    data = _corpus(11, 2 * cb.FRAME)
    frames, sizes = cb.lzx_compress(data, 16)
    with pytest.raises(LzxError, match="frame ends in the middle|crosses"):
        lzx_decompress([frames[0][: len(frames[0]) // 2]], [sizes[0]], 16)
    with pytest.raises(LzxError, match="fewer bytes|frame ends in the middle|crosses"):
        lzx_decompress(frames, [sizes[0], sizes[1] - 1], 16)
    with pytest.raises(LzxError, match="at most 32768"):
        lzx_decompress(frames, [40000, 1], 16)
    with pytest.raises(LzxError, match="do not match"):
        lzx_decompress(frames, sizes[:1], 16)
    with pytest.raises(LzxError, match="unsupported LZX window"):
        lzx_decompress(frames, sizes, 22)
    shifted = [frames[0][:-2], frames[1]]  # a frame that stops early
    with pytest.raises(LzxError):
        lzx_decompress(shifted, sizes, 16)


def test_lzx_block_and_e8_caps() -> None:
    data = _corpus(12, 40)
    blocks = [(cb.VERBATIM, 1)] * 40
    frames, sizes = cb.lzx_compress(data, 15, blocks=blocks)
    with pytest.raises(LzxError, match="more than 32 blocks start in one frame"):
        lzx_decompress(frames, sizes, 15)
    ok = cb.lzx_compress(data, 15, blocks=[(cb.VERBATIM, 1)] * 31 + [(cb.VERBATIM, 9)])
    assert lzx_decompress(*ok, 15) == data
    calls = (b"\xe8" + struct.pack("<i", 1000) + b"\x90" * 3) * 8000  # x86 calls the encoder translated
    frames, sizes = cb.lzx_compress(calls, 16, e8=1_000_000)
    # a marker and its match each (all but the markers in the last 10 bytes of a frame), plus
    # stepping over the frames and the expression
    stream = bytearray(cb.e8_translate(calls, 1_000_000))
    units = _e8_fixed_units(stream, _frames_of(len(calls)))
    units += 2 * _undo_e8_reference(stream, _frames_of(len(calls)), 1_000_000)
    assert stream == calls and units < 20_000
    assert lzx_decompress(frames, sizes, 16, e8_budget=units) == calls
    with pytest.raises(lzxmod.LzxBudgetError, match="x86 call markers"):
        lzx_decompress(frames, sizes, 16, e8_budget=units - 1)
    others = bytes(range(0xE0, 0xF0)) * 5000  # markers whose targets lie far outside the range: stepped over
    frames, sizes = cb.lzx_compress(others, 16, e8=1_000_000)
    units = _e8_fixed_units(others, _frames_of(len(others)))
    assert units == lzxmod.E8_PATTERN_UNITS + 2 * math.ceil(2048 / 3) + math.ceil(903 / 3)
    assert lzx_decompress(frames, sizes, 16, e8_budget=units) == others
    with pytest.raises(lzxmod.LzxBudgetError):
        lzx_decompress(frames, sizes, 16, e8_budget=units - 1)


def test_random_damage_is_a_value_error_in_bounded_time(samples) -> None:
    rng = random.Random(1)
    bases = [_notebook(cb.LZX, window_bits=16), _notebook(cb.MSZIP),
             _fixture(samples, "makecab-lzx-notebook.onepkg").read_bytes()]
    start = time.perf_counter()
    for base in bases:
        for _ in range(60):
            bad = bytearray(base)
            for _ in range(rng.randrange(1, 6)):
                bad[rng.randrange(len(bad))] = rng.randrange(256)
            _fails_fast(bytes(bad))
            # with checksums gone, damaged LZX data reaches the decoder
            nosum = bytearray(bad)
            cab = Cabinet(bytes(base))
            for folder in cab.folders:
                offset = folder.data_offset
                for _k in range(folder.blocks):
                    struct.pack_into("<I", nosum, offset, 0)
                    size = struct.unpack_from("<H", base, offset + 4)[0]
                    offset += 8 + cab.data_reserve + size
            _fails_fast(bytes(nosum))
    assert time.perf_counter() - start < 60


def test_ported_modules_carry_their_mit_notices() -> None:
    """The web bundle and the wheel ship the .py files without NOTICE.md: the MIT notices of
    the ported code travel inside the files themselves."""
    for module in (cabmod, lzxmod):
        text = Path(module.__file__).read_text(encoding="utf-8")
        head = text[:text.index('"""')]
        assert "Copyright (c) 2023 Obsidian" in head and "Copyright (c) 2022 Evotec" in head
        assert "Permission is hereby granted, free of charge" in head
        assert "The above copyright notice and this permission notice shall be included" in head
        assert 'THE SOFTWARE IS PROVIDED "AS IS"' in head


# =========================================================================== registry and sniffing


def test_registry_entry() -> None:
    fmt = formats.get("onenote")
    assert fmt.input_extensions == (".one", ".onepkg") and fmt.readable and not fmt.writable
    assert formats.format_for_extension("Notebook.onepkg") is fmt
    assert detect_format("Notebook.onepkg", b"MSCF" + bytes(60)) == "onenote"  # by extension: the reader explains
    assert '".onepkg"' in formats.web_formats_js()


def test_sniffer_reads_the_cabinet_file_table() -> None:
    sniff = formats.get("onenote").sniff
    assert sniff(_notebook(), None)
    assert sniff(cb.build_cab([[("Notebook\\Open Notebook.onetoc2", b"x")]], cb.NONE), None)
    assert not sniff(cb.build_cab([[("a.txt", b"x.one\x00")]], cb.NONE), None)  # names, not content
    assert not sniff(cb.build_cab([[("section.one.bak", b"x")]], cb.NONE), None)
    assert not sniff(b"MSCF" + bytes(100), None)
    assert not sniff(b"MSCF", None)
    truncated = _notebook()[:60]
    assert not sniff(truncated, None)
    for name in ("x.onepkg", "x.cab"):
        assert detect_format(name, _notebook()) == "onenote"


def _sample_files(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts
                  and p.stat().st_size < 64 * 1024 * 1024)


def test_no_other_sample_is_claimed_and_no_other_format_claims_packages(samples) -> None:
    """The OneNote sniffer claims only OneNote files among all sample files, and no other
    format's sniffer claims a cabinet."""
    root = samples.repo("obsidian-importer").parent
    onenote = formats.get("onenote")
    others = [f for f in formats.FORMATS.values() if f.id != "onenote"]
    claimed: List[str] = []
    cabinets = 0
    for path in _sample_files(root):
        data = path.read_bytes()
        names = formats.sniff_zip_names(data)
        if onenote.sniff(data, names):
            claimed.append(path.name)
            assert path.name.lower().endswith((".one", ".onetoc2", ".onepkg", ".cab")), path
        if data.startswith(b"MSCF"):
            cabinets += 1
            assert not [f.id for f in others if f.sniff(data, names)], path
    assert cabinets >= 10
    assert "makecab-lzx-notebook.onepkg" in claimed and "makecab-lzx-testOneNote2016.cab" in claimed
    assert not any(n.startswith("makecab-lzx") and n.endswith("-e8.cab") for n in claimed)
    built = [_notebook(m) for m in (cb.NONE, cb.MSZIP, cb.LZX)]
    for data in built:
        assert onenote.sniff(data, None)
        assert not [f.id for f in others if f.sniff(data, None)]


def test_lzx_decodes_at_a_usable_speed() -> None:
    """A loose bound (machines differ; about 6 to 20 MB/s were measured on a shared 4-CPU machine)."""
    data = _corpus(21, 1 << 20)
    frames, sizes = cb.lzx_compress(data, 21, e8=12_000_000)
    start = time.perf_counter()
    assert lzx_decompress(frames, sizes, 21) == data
    assert len(data) / (time.perf_counter() - start) > 200_000
