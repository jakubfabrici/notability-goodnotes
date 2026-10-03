"""MyScript Notes / Nebo (``.nebo``) reader: the CC0 sample, an oracle, synthetic packages, hardening."""
from __future__ import annotations

import io
import json
import os
import random
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import pytest

from gnnote import readutil
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format, to_document
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.nebo import reader as nebo_reader
from gnnote.nebo.bink import BinkError, parse_bink
from gnnote.nebo.reader import MM_TO_PT, parse_css, read_nebo
from gnnote.notability.reader import read_note

# --------------------------------------------------------------------------- synthetic packages

CHANNELS = ((b"X", b"\x20\x04\x01\x00", b"mm"), (b"Y", b"\x20\x04\x01\x00", b"mm"),
            (b"F", b"\x20\x04\x01\x00", None), (b"T", b"\x20\x02\x01\x00", b"ms"))
STYLE_CSS = """
ink { color: #000000ff; -myscript-pen-brush:FeltPen; -myscript-pen-width: 0.625 }
stroke { -myscript-pen-brush:FeltPen; }
.component-brush { -myscript-pen-brush:Highlighter; -myscript-pen-width:5.0; }
.pen-025 { -myscript-pen-width:0.35; }
.pen-050 { -myscript-pen-width:0.9; } /* a comment */
.brush-0250 { -myscript-pen-width:2.5; }
"""

Stroke = Tuple[Sequence[float], Sequence[float], Optional[Sequence[int]]]


def _s(text: bytes) -> bytes:
    return struct.pack("<I", len(text)) + text


def bink(strokes: Sequence[Optional[Stroke]], tags: Sequence[Tuple[str, int, int, str]] = (),
         precision: int = 1000, plain: Sequence[int] = ()) -> bytes:
    """A BINK v5 blob: ``None`` = an erased record; indices in ``plain`` use the plain layout."""
    out = bytearray(b"BINK\x00" + struct.pack("<IBI", 5, 0, 1) + struct.pack("<I", len(CHANNELS)))
    for name, tag, unit in CHANNELS:
        out += _s(name) + tag + (struct.pack("<I", 1) + _s(unit) if unit else struct.pack("<I", 0))
    layout = struct.pack("<I", 4) + b"".join(struct.pack("<III", *e) + CHANNELS[i][1]
                                             for i, e in enumerate([(0, 0, 8), (4, 0, 8), (0, 8, 4), (0, 12, 4)]))
    out += struct.pack("<I", len(layout)) + layout
    out += struct.pack("<IIIB", precision, precision, 3, 0) + struct.pack("<I", len(strokes))
    units = precision / 2.0
    for index, stroke in enumerate(strokes):
        if stroke is None:
            out += b"\xff\xff\xff\xff"
            continue
        xs, ys, force = stroke
        n = len(xs)
        if index in plain:
            out += struct.pack("<IQII", 0, 1_783_614_500_000_000, 0x0C49, n)
            out += b"".join(struct.pack("<ff", x, y) for x, y in zip(xs, ys)) + b"\x00" * 8 * n
            continue
        steps_x = [round((x - xs[0]) * units) for x in xs]
        steps_y = [round((y - ys[0]) * units) for y in ys]
        dx = [steps_x[0]] + [b - a for a, b in zip(steps_x, steps_x[1:])]
        dy = [steps_y[0]] + [b - a for a, b in zip(steps_y, steps_y[1:])]
        out += struct.pack("<IQffHHHI", 0x80000000, 1_783_614_500_000_000, xs[0], ys[0], 4281, 0x0C49, 0, n)
        out += struct.pack(f"<{n}h", *dx) + struct.pack(f"<{n}h", *dy) + bytes(force or [255] * n)
    out += struct.pack("<II", 0, len(tags)) + b"\x00"
    for i, (name, first, last, text) in enumerate(tags):
        if i:
            out += struct.pack("<III", 12, i, 0)
        out += _s(name.encode()) + struct.pack("<I", 1) + struct.pack("<HHIHHI", 3, 0, first, 0xFF05, 0, last)
        out += _s(text.encode())
    out += struct.pack("<IIIII", 11, 31, 0, 32, 0) + struct.pack("<I", 1) + struct.pack("<HHIHHI", 3, 0, 0, 0xFF05, 0, 0)
    return bytes(out)


def package(pages: Dict[str, Optional[bytes]], css: Optional[str] = STYLE_CSS, meta: Optional[dict] = None,
            page_meta: Optional[dict] = None, rel: Optional[dict] = None, extra: Optional[Dict[str, bytes]] = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("rel.json", json.dumps(rel if rel is not None else {"pages": {p: {"version": 5} for p in pages}}))
        zf.writestr("index.bdom", b"BDOM\x00\x02")
        zf.writestr("meta.json", json.dumps(meta if meta is not None else {"pageTitle": "Synthetic",
                                                                          "pageExtent": [0, 0, 210, 297]}))
        for pid, ink in pages.items():
            zf.writestr(f"pages/{pid}/meta.json", json.dumps(page_meta if page_meta is not None
                                                             else {"pageExtent": [0, 0, 210, 297]}))
            zf.writestr(f"pages/{pid}/page.bdom", b"BDOM\x00\x02")
            if ink is not None:
                zf.writestr(f"pages/{pid}/ink.bink", ink)
            if css is not None:
                zf.writestr(f"pages/{pid}/style.css", css)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def line(x0: float, y0: float, x1: float, y1: float, n: int = 5, force: Optional[Sequence[int]] = None) -> Stroke:
    xs = [x0 + (x1 - x0) * i / (n - 1) for i in range(n)]
    ys = [y0 + (y1 - y0) * i / (n - 1) for i in range(n)]
    return xs, ys, force


# --------------------------------------------------------------------------- the CC0 sample

# Per-file facts, read from the files themselves (``docs/nebo.md`` section 6): stroke kind,
# point count, colour (8-bit RGBA from the .STYLE tag), stylesheet width in mm and the bbox
# of the decoded points in mm.
SAMPLE_EXPECTED = {
    "nebo-ipad-pen-highlighter.nebo": {
        "title": "My folder",
        "pages": [(210.0, 297.0)],
        "strokes": [
            ("pen", 280, (0, 0, 0, 255), 0.35, (14.7825, 11.4706, 48.0945, 50.4426)),
            ("highlighter", 178, (255, 221, 51, 102), 5.0, (77.8485, 5.8589, 165.2485, 61.1649)),
        ],
    },
}


def _rgba8(color) -> Tuple[int, ...]:
    return tuple(round(c * 255) for c in color)


def test_samples_read_with_their_counts_and_geometry(samples):
    for path in samples.nebo_files():
        doc = read_nebo(path.read_bytes())
        assert doc.source_format == "nebo" and doc.pages
        for page in doc.pages:
            for stroke in page.strokes:
                x0, y0, x1, y1 = stroke.bbox()
                assert -1 <= x0 and x1 <= page.width + 1 and -1 <= y0 and y1 <= page.height + 1, path.name
                assert all(p.width > 0 for p in stroke.points)
        expected = samples.expected_for(path, SAMPLE_EXPECTED)
        if expected is None:
            continue
        assert doc.title == expected["title"]
        assert [(round(p.width / MM_TO_PT, 3), round(p.height / MM_TO_PT, 3)) for p in doc.pages] == expected["pages"]
        got = []
        for stroke in doc.pages[0].strokes:
            bbox = tuple(round(v / MM_TO_PT, 4) for v in stroke.bbox())
            got.append((stroke.kind, len(stroke.points), _rgba8(stroke.color), round(stroke.width / MM_TO_PT, 4), bbox))
        assert got == expected["strokes"]
        # every point of the capacitive-pen sample has force 255: constant width = stylesheet width
        assert {round(p.width, 6) for p in doc.pages[0].strokes[0].points} == {round(0.35 * MM_TO_PT, 6)}
        assert doc.warnings == ["Typed and converted text, typeset shapes and math are stored in MyScript's "
                                "undecoded layout data and are not converted; handwriting is"]


ORACLE = r"""
import json, sys
from pathlib import Path
from inkterop.formats.nebo.reader import NeboReader
doc = NeboReader().read(Path(sys.argv[1]))
out = []
for page in doc.pages:
    strokes = []
    for layer in page.layers:
        for s in layer.strokes:
            strokes.append({"x": list(s.x), "y": list(s.y), "rgb": [s.color.r, s.color.g, s.color.b],
                            "highlighter": s.tool.family.name == "HIGHLIGHTER"})
    out.append({"extent": [page.bounds.x_min, page.bounds.y_min, page.bounds.x_max, page.bounds.y_max],
                "strokes": strokes})
print(json.dumps(out))
"""


def _oracle(samples, path: Path) -> list:
    src = samples.repo("inkterop") / "core" / "src"
    env = dict(os.environ, PYTHONPATH=str(src))
    proc = subprocess.run([sys.executable, "-c", ORACLE, str(path)], env=env, capture_output=True, text=True,
                          timeout=600)
    if proc.returncode != 0:
        pytest.skip(f"inkterop's Nebo reader could not run: {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


def test_samples_match_inkterops_decoder(samples):
    """inkterop (MIT) decodes BINK independently; it runs in its own process."""
    for path in samples.nebo_files():
        oracle = _oracle(samples, path)
        doc = read_nebo(path.read_bytes())
        assert len(doc.pages) == len(oracle)
        for page, ref in zip(doc.pages, oracle):
            assert len(page.strokes) == len(ref["strokes"])
            assert page.width == pytest.approx((ref["extent"][2] - ref["extent"][0]) * MM_TO_PT)
            for stroke, want in zip(page.strokes, ref["strokes"]):
                assert [round(p.x / MM_TO_PT, 4) for p in stroke.points] == [round(v, 4) for v in want["x"]]
                assert [round(p.y / MM_TO_PT, 4) for p in stroke.points] == [round(v, 4) for v in want["y"]]
                assert stroke.color[:3] == pytest.approx(tuple(want["rgb"]))
                assert (stroke.kind == "highlighter") == want["highlighter"]


@pytest.mark.parametrize("target", ["goodnotes", "notability"])
def test_samples_convert_and_read_back(samples, target):
    for path in samples.nebo_files():
        doc = read_nebo(path.read_bytes())
        result = convert(path.read_bytes(), path.name, Options(target=target))
        assert result.source_format == "nebo" and result.filename == path.stem + (".goodnotes" if target == "goodnotes"
                                                                                   else ".note")
        back = read_goodnotes(result.data) if target == "goodnotes" else read_note(result.data)
        assert len(back.pages) == len(doc.pages)
        assert [len(p.strokes) for p in back.pages] == [len(p.strokes) for p in doc.pages]
        assert sum(s.kind == "highlighter" for p in back.pages for s in p.strokes) == \
            sum(s.kind == "highlighter" for p in doc.pages for s in p.strokes)
        assert result.stats["strokes"] == sum(len(p.strokes) for p in doc.pages)


# --------------------------------------------------------------------------- synthetic packages


def test_pages_follow_rel_json_and_carry_the_style_cascade():
    ink1 = bink([line(10, 10, 60, 10), None, line(10, 30, 60, 30), line(10, 50, 60, 50)],
                tags=[(".STYLE", 0, 0, '"color:#ff000080;"'), ("pen-050", 0, 3, ""),
                      (".STYLE", 2, 3, '"color:#0000ffff;-myscript-pen-width:1.2"'),
                      ("component-brush", 3, 3, ""), ("brush-0250", 3, 3, "")])
    ink2 = bink([line(20, 20, 40, 80)], tags=[("pen-025", 0, 0, "")])
    data = package({"bbbb": ink2, "aaaa": ink1}, rel={"pages": {"aaaa": {}, "bbbb": {}}})
    doc = read_nebo(data)
    assert doc.title == "Synthetic" and len(doc.pages) == 2
    first, second = doc.pages
    assert len(first.strokes) == 3  # the erased record is skipped
    red, blue, highlighter = first.strokes
    assert _rgba8(red.color) == (255, 0, 0, 128) and red.kind == "pen"
    assert red.width == pytest.approx(0.9 * MM_TO_PT)  # .pen-050 from the stylesheet
    assert _rgba8(blue.color) == (0, 0, 255, 255) and blue.width == pytest.approx(1.2 * MM_TO_PT)  # .STYLE wins
    # the erased record counts: tag index 3 is the third live stroke
    assert highlighter.kind == "highlighter" and highlighter.width == pytest.approx(1.2 * MM_TO_PT)
    assert [round(v / MM_TO_PT, 3) for v in red.bbox()] == [10.0, 10.0, 60.0, 10.0]
    assert second.strokes[0].width == pytest.approx(0.35 * MM_TO_PT)
    assert [round(v / MM_TO_PT, 3) for v in second.strokes[0].bbox()] == [20.0, 20.0, 40.0, 80.0]


def test_highlighter_markers():
    ink = bink([line(10, 10, 60, 10), line(10, 20, 60, 20), line(10, 30, 60, 30)],
               tags=[("HIGHLIGHT_STROKES", 0, 0, ""), ("highlighter-4-2", 1, 1, ""),
                     (".STYLE", 2, 2, '"-myscript-pen-brush: Highlighter oriented"')])
    doc = read_nebo(package({"p": ink}))
    assert [s.kind for s in doc.pages[0].strokes] == ["highlighter"] * 3
    assert doc.pages[0].strokes[0].width == pytest.approx(5.0 * MM_TO_PT)  # default highlighter width


def test_pressure_follows_the_fitted_width_law():
    forces = [0, 64, 128, 192, 255]
    ink = bink([line(10, 10, 60, 10, force=forces), line(10, 20, 60, 20, force=forces),
                line(10, 30, 60, 30, force=[255] * 5)],
               tags=[("pen-025", 0, 2, ""), (".STYLE", 0, 0, '"-myscript-pen-pressure-sensitivity: 0.8"'),
                     (".STYLE", 1, 1, '"-myscript-pen-pressure-sensitivity: 0"')])
    pressure, no_sensitivity, capacitive = read_nebo(package({"p": ink})).pages[0].strokes
    expected = [0.25 * MM_TO_PT * min(max(1 + 0.8 * 2.43 * (f / 255 - 0.29), 0.2), 3.0) for f in forces]
    assert [p.width for p in pressure.points] == pytest.approx(expected)
    assert pressure.width == pytest.approx(0.35 * MM_TO_PT)  # nominal = stylesheet width
    assert {round(p.width, 6) for p in no_sensitivity.points} == {round(0.35 * MM_TO_PT, 6)}
    assert {round(p.width, 6) for p in capacitive.points} == {round(0.35 * MM_TO_PT, 6)}


def test_missing_stylesheet_uses_the_class_name_and_defaults():
    ink = bink([line(10, 10, 60, 10), line(10, 20, 60, 20)], tags=[("pen-050", 0, 0, "")])
    first, second = read_nebo(package({"p": ink}, css=None)).pages[0].strokes
    assert first.width == pytest.approx(0.5 * MM_TO_PT)
    assert second.width == pytest.approx(0.625 * MM_TO_PT) and second.color == (0.0, 0.0, 0.0, 1.0)


def test_plain_records_and_precision():
    ink = bink([line(10, 10, 60, 10), line(10, 20, 30, 40)], plain=(1,), precision=1024)
    doc = read_nebo(package({"p": ink}))
    packed, plain = doc.pages[0].strokes
    assert [round(v / MM_TO_PT, 2) for v in packed.bbox()] == [10.0, 10.0, 60.0, 10.0]  # 512 units per mm
    assert [round(v / MM_TO_PT, 3) for v in plain.bbox()] == [10.0, 20.0, 30.0, 40.0]
    assert any("precision 1024" in w for w in doc.warnings)


def test_page_grows_for_ink_outside_it():
    ink = bink([line(10, 10, 60, 400, n=50), line(-20, 5, 0, 5)])
    doc = read_nebo(package({"p": ink}))
    page = doc.pages[0]
    assert page.height == pytest.approx((400 + 5) * MM_TO_PT)
    assert page.width == pytest.approx((210 + 25) * MM_TO_PT)
    assert min(p.x for s in page.strokes for p in s.points) == pytest.approx(5 * MM_TO_PT)
    assert any("enlarged" in w for w in doc.warnings)


def test_page_size_fallbacks():
    ink = bink([line(10, 10, 20, 20)])
    letter = read_nebo(package({"p": ink}, page_meta={"pageExtent": [0, 0, 215.9, 279.4]}))
    assert (round(letter.pages[0].width, 2), round(letter.pages[0].height, 2)) == (612.0, 792.0)
    kobo_meta = {"iink-user-metadata": {"kobo": {"geometry": "1404x1872", "dpi": 228}}}
    kobo = read_nebo(package({"p": ink}, meta=kobo_meta, page_meta={}))
    assert kobo.pages[0].width == pytest.approx(1404 / 228 * 72)
    unknown = read_nebo(package({"p": ink}, meta={}, page_meta={}))
    assert unknown.pages[0].width == pytest.approx(210 * MM_TO_PT)
    assert any("A4 assumed" in w for w in unknown.warnings)


def test_pages_without_rel_json_come_from_the_members_and_blank_pages_survive():
    data = package({"one": bink([line(1, 1, 5, 5)]), "two": None}, rel={"nothing": True})
    doc = read_nebo(data)
    assert [len(p.strokes) for p in doc.pages] == [1, 0]


def test_embedded_objects_and_unknown_header_are_reported():
    ink = bytearray(bink([line(10, 10, 60, 10)]))
    ink[14:18] = struct.pack("<I", 1000)  # a channel table the decoder cannot follow
    doc = read_nebo(package({"p": bytes(ink)}, extra={"objects/0b3c.png": b"\x89PNG\r\n\x1a\n"}))
    assert len(doc.pages[0].strokes) == 1  # found by scanning for the record
    assert any("embedded object" in w for w in doc.warnings)


def test_css_parser():
    rules = parse_css("a, .b { color: red; width: 1 } /* x { y } */ .b { width: 2 }")
    assert rules == {"a": {"color": "red", "width": "1"}, ".b": {"color": "red", "width": "2"}}


# --------------------------------------------------------------------------- detection and CLI


def test_detection_by_content_and_extension(samples, tmp_path: Path):
    data = package({"p": bink([line(1, 1, 5, 5)])})
    assert detect_format("x.nebo", data) == "nebo"
    assert detect_format("renamed.zip", data) == "nebo"
    assert to_document(data, "renamed.zip").source_format == "nebo"
    with pytest.raises(ValueError, match="not a MyScript Notes file"):
        to_document(b"not a zip", "x.nebo")
    other = io.BytesIO()
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("hello.txt", "x")
    with pytest.raises(ValueError, match="not a MyScript Notes file"):
        read_nebo(other.getvalue())


def test_cli_lists_converts_and_refuses_nebo_as_a_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["formats"]) == 0
    listed = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("nebo")]
    assert listed and "MyScript Notes (Nebo)" in listed[0] and listed[0].endswith("read only")
    src = tmp_path / "Lecture.nebo"
    src.write_bytes(package({"p": bink([line(10, 10, 60, 10), line(10, 20, 60, 20)])}))
    assert main(["convert", str(src), "--to", "goodnotes"]) == 0
    assert len(read_goodnotes((tmp_path / "Lecture.goodnotes").read_bytes()).pages[0].strokes) == 2
    assert main(["convert", str(src)]) == 0  # default target: Notability
    assert (tmp_path / "Lecture.note").is_file()
    capsys.readouterr()
    assert main(["convert", str(tmp_path / "Lecture.goodnotes"), "--to", "nebo"]) == 2
    assert "MyScript Notes (Nebo) files can be read but not written" in capsys.readouterr().err
    with pytest.raises(ValueError, match="cannot be written"):
        convert(src.read_bytes(), src.name, Options(target="nebo"))


# --------------------------------------------------------------------------- hardening


def _sample_bink(samples) -> bytes:
    path = samples.nebo_files()[0]
    with zipfile.ZipFile(path) as zf:
        member = next(n for n in zf.namelist() if n.endswith("ink.bink"))
        return zf.read(member)


def test_truncated_ink_never_raises(samples):
    blob = _sample_bink(samples)
    for cut in list(range(0, 200)) + list(range(200, len(blob), 7)):
        ink = parse_bink(blob[:cut]) if blob[:cut].startswith(b"BINK\x00") else None
        if ink is not None:
            assert len(ink.strokes) <= 2
        doc = read_nebo(package({"p": blob[:cut]}))
        assert len(doc.pages) == 1
    with pytest.raises(BinkError):
        parse_bink(b"BDOM")


def test_mutated_ink_never_raises(samples):
    blob = _sample_bink(samples)
    rng = random.Random(1234)
    for _ in range(300):
        data = bytearray(blob)
        for _ in range(rng.randint(1, 8)):
            data[rng.randrange(5, len(data))] = rng.randrange(256)
        doc = read_nebo(package({"p": bytes(data)}))
        for stroke in doc.pages[0].strokes:
            assert all(abs(p.x) < 1e6 and abs(p.y) < 1e6 for p in stroke.points)


def test_hostile_counts_are_bounded():
    huge = bytearray(bink([line(1, 1, 5, 5)]))
    count_at = bytes(huge).index(struct.pack("<IIIB", 1000, 1000, 3, 0)) + 13
    huge[count_at:count_at + 4] = struct.pack("<I", 0xFFFFFFF0)  # absurd record count -> scan
    assert len(read_nebo(package({"p": bytes(huge)})).pages[0].strokes) == 1
    points = bytearray(bink([line(1, 1, 5, 5)]))
    n_at = points.index(struct.pack("<HHH", 4281, 0x0C49, 0)) + 6
    points[n_at:n_at + 4] = struct.pack("<I", 0x7FFFFFFF)
    doc = read_nebo(package({"p": bytes(points)}))
    assert doc.pages[0].strokes == []
    assert any("point count" in w or "truncated" in w for w in doc.warnings)


def test_member_size_guard_and_point_budget(monkeypatch: pytest.MonkeyPatch):
    data = package({"p": bink([line(1, 1, 5, 5), line(5, 5, 9, 9)])})
    monkeypatch.setattr(readutil, "MAX_MEMBER_BYTES", 100)
    doc = read_nebo(data)
    assert doc.pages[0].strokes == [] and any("inflate above" in w for w in doc.warnings)
    monkeypatch.setattr(readutil, "MAX_MEMBER_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(readutil, "MAX_POINTS", 6)
    doc = read_nebo(data)
    assert len(doc.pages[0].strokes) == 1 and any("more ink points" in w for w in doc.warnings)


def test_tag_work_cap(monkeypatch: pytest.MonkeyPatch):
    ink = bink([line(1, 1, 5, 5)] * 4, tags=[("pen-050", 0, 3, "")] * 3)
    monkeypatch.setattr(nebo_reader, "MAX_TAG_WORK", 5)
    doc = read_nebo(package({"p": ink}))
    assert len(doc.pages[0].strokes) == 4 and any("style tables are too large" in w for w in doc.warnings)


def test_damaged_json_parts_are_tolerated():
    deep = b"[" * 100_000 + b"]" * 100_000
    data = package({"p": bink([line(1, 1, 5, 5)])}, extra={})
    with zipfile.ZipFile(io.BytesIO(data)) as src:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as dst:
            for name in src.namelist():
                payload = src.read(name)
                if name.endswith("meta.json"):
                    payload = deep
                if name == "rel.json":
                    payload = b'{"pages": {"p": NaN'
                dst.writestr(name, payload)
    doc = read_nebo(out.getvalue())
    assert len(doc.pages) == 1 and len(doc.pages[0].strokes) == 1


def test_damaged_zip_member_is_reported():
    data = bytearray(package({"p": bink([line(1, 1, 5, 5)] * 50)}))
    pos = data.index(b"pages/p/ink.bink") + len("pages/p/ink.bink")
    data[pos + 10:pos + 30] = b"\x00" * 20  # corrupt the deflated ink
    doc = read_nebo(bytes(data))
    assert len(doc.pages) == 1
    assert any("could not be read" in w for w in doc.warnings) or doc.pages[0].strokes == []
