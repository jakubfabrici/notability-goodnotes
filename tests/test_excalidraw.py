"""Excalidraw codec (``gnnote.excalidraw``): the freedraw width law, synthetic round trips of
every element type, the scene shape, inkterop's CC0 fixture, conversions of the GoodNotes and
Notability samples to ``.excalidraw`` and back, hardening and the registry, sniffing and CLI
plumbing."""
from __future__ import annotations

import base64
import json
import math
import random
from pathlib import Path
from typing import Any, Dict, List

import pytest

from gnnote import formats
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format
from gnnote.excalidraw import MAX_FACTOR, MIN_FACTOR, PT_PER_PX, pressure_for_factor, thickness_factor
from gnnote.excalidraw import reader as er
from gnnote.excalidraw.reader import read_excalidraw
from gnnote.excalidraw.writer import build_scene, write_excalidraw
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.goodnotes.writer import write_goodnotes
from gnnote.model import Document, Page, Point, Stroke, TextBox, TextRun
from gnnote.notability.reader import read_note
from gnnote.notability.writer import write_note
from tests.sample_docs import JPEG, full_document, png, stats

TOL = 0.01  # pt


def _scene(elements: List[Dict[str, Any]], **extra: Any) -> bytes:
    return json.dumps({"type": "excalidraw", "version": 2, "source": "test", "elements": elements,
                       "appState": {"viewBackgroundColor": "#ffffff"}, "files": {}, **extra}).encode()


def _el(kind: str, **fields: Any) -> Dict[str, Any]:
    base = {"id": kind + str(len(fields)), "type": kind, "x": 0, "y": 0, "width": 10, "height": 10, "angle": 0,
            "strokeColor": "#1e1e1e", "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 2,
            "strokeStyle": "solid", "roughness": 0, "opacity": 100, "isDeleted": False}
    base.update(fields)
    return base


# --------------------------------------------------------------------------- width law


def test_freedraw_width_law():
    assert thickness_factor(1.0) == pytest.approx(8.08, abs=0.01) == MAX_FACTOR
    assert thickness_factor(0.5) == pytest.approx(6.01, abs=0.01)
    assert MIN_FACTOR == pytest.approx(2.63, abs=0.01)
    for p in (0.0, 0.3, 0.5, 0.9, 1.0):
        assert pressure_for_factor(thickness_factor(p)) == pytest.approx(p, abs=1e-9)
    assert pressure_for_factor(100.0) == 1.0 and pressure_for_factor(0.0) == 0.0


# --------------------------------------------------------------------------- synthetic round trip


def test_every_element_type_round_trips():
    doc = full_document()
    back = read_excalidraw(write_excalidraw(doc))
    assert [c for p in back.pages for c in (p.width, p.height)] == pytest.approx(
        [c for p in doc.pages for c in (p.width, p.height)])
    p1, b1 = doc.pages[0], back.pages[0]
    assert [s.kind for s in b1.strokes] == [s.kind for s in p1.strokes]
    pressure = b1.strokes[0]
    src = p1.strokes[0].points
    assert [c for p in pressure.points for c in (p.x, p.y)] == pytest.approx([c for p in src for c in (p.x, p.y)],
                                                                            abs=TOL)
    assert [p.width for p in pressure.points] == pytest.approx([p.width for p in src], rel=1e-3)
    assert pressure.color == pytest.approx(p1.strokes[0].color, abs=1 / 255)
    highlighter = b1.strokes[2]
    assert highlighter.kind == "highlighter" and highlighter.color[3] == pytest.approx(0.5)
    assert b1.strokes[5].pen == "pencil"
    bezier = b1.strokes[3]
    assert bezier.controls is None
    for anchor in p1.strokes[3].points:
        assert min(math.dist((anchor.x, anchor.y), (p.x, p.y)) for p in bezier.points) < TOL
    dot = b1.strokes[4]
    assert len(dot.points) == 2 and dot.points[0].x == pytest.approx(120.0, abs=TOL)
    fill, lone = b1.strokes[7], b1.strokes[8]
    assert fill.kind == "fill" and fill.color == pytest.approx((0.2, 0.4, 0.9, 0.25), abs=0.01)
    assert [c for p in lone.outline[0] for c in (p.x, p.y)] == pytest.approx([60, 300, 140, 300, 100, 360], abs=TOL)
    # images: PNG and the rotated JPEG survive, the PDF sticker is dropped
    png_img, jpeg_img = b1.images
    assert png_img.data == p1.images[0].data and jpeg_img.data == JPEG
    assert (jpeg_img.x, jpeg_img.y, jpeg_img.w, jpeg_img.h) == pytest.approx((150, 400, 64, 48), abs=TOL)
    assert jpeg_img.rotation == pytest.approx(90.0)
    plain, turned = b1.texts
    assert plain.text == "Hello <ink> & text" and (plain.x, plain.y) == pytest.approx((40, 480), abs=TOL)
    assert plain.size == pytest.approx(14.0, abs=1e-3)
    assert turned.text == "Turned\nand bold" and turned.align == "center" and turned.rotation == pytest.approx(30.0)
    assert (turned.x, turned.y) == pytest.approx((260, 480), abs=TOL)
    assert turned.color == pytest.approx((0.8, 0.0, 0.0, 1.0), abs=1 / 255)
    assert back.pages[1].strokes and all(p.background is None for p in back.pages)
    joined = " | ".join(doc.warnings)
    for needle in ("PDF page backgrounds were dropped", "PDF images were dropped", "paper", "one plain style"):
        assert needle in joined


def test_scene_shape():
    scene = build_scene(full_document())
    assert list(scene) == ["type", "version", "source", "elements", "appState", "files"]
    assert scene["type"] == "excalidraw" and scene["version"] == 2 and scene["source"].startswith("gnnote ")
    frames = [e for e in scene["elements"] if e["type"] == "frame"]
    assert [f["name"] for f in frames] == ["Page 1", "Page 2", "Page 3", "Page 4"]
    assert frames[1]["y"] == pytest.approx(frames[0]["height"] + 80)
    ids = {f["id"] for f in frames}
    assert all(e["frameId"] in ids for e in scene["elements"] if e["type"] != "frame")
    assert all(len(e["id"]) == 21 for e in scene["elements"])
    freedraw = next(e for e in scene["elements"] if e["type"] == "freedraw")
    for key in ("strokeColor", "backgroundColor", "fillStyle", "strokeWidth", "strokeStyle", "roughness", "opacity",
                "groupIds", "roundness", "seed", "version", "versionNonce", "isDeleted", "boundElements", "updated",
                "link", "locked", "points", "pressures", "simulatePressure", "lastCommittedPoint"):
        assert key in freedraw
    assert freedraw["points"][0] == [0.0, 0.0] and freedraw["simulatePressure"] is False
    assert max(freedraw["pressures"]) == 1.0 and len(freedraw["pressures"]) == len(freedraw["points"])
    image = next(e for e in scene["elements"] if e["type"] == "image")
    entry = scene["files"][image["fileId"]]
    assert entry["mimeType"] == "image/png" and base64.b64decode(entry["dataURL"].split(",", 1)[1]) == png()
    fill = next(e for e in scene["elements"] if e["type"] == "line")
    assert fill["polygon"] is True and fill["strokeColor"] == "transparent" and fill["points"][0] == fill["points"][-1]
    text = next(e for e in scene["elements"] if e["type"] == "text")
    assert text["fontFamily"] == 2 and text["originalText"] == "Hello <ink> & text"
    options = Options()
    options.random_seed = 11  # type: ignore[attr-defined]
    assert write_excalidraw(full_document(), options) == write_excalidraw(full_document(), options)
    assert write_excalidraw(full_document(), options) != write_excalidraw(full_document())


def test_wide_width_ranges_and_long_text():
    stroke = Stroke([Point(10 * i, 0, w) for i, w in enumerate([0.2, 2.0, 0.2])], width=1.0)
    long_box = TextBox(10, 50, 60, 20, "a long line that the source app wrapped inside its box", size=12.0)
    doc = Document(pages=[Page(300, 300, strokes=[stroke], texts=[long_box])])
    scene = build_scene(doc)
    back = read_excalidraw(write_excalidraw(doc))
    widths = [p.width for p in back.pages[0].strokes[0].points]
    assert widths[1] == pytest.approx(2.0, rel=1e-3) and widths[0] == pytest.approx(2.0 * MIN_FACTOR / MAX_FACTOR, rel=1e-3)
    assert any("3:1" in w for w in doc.warnings)
    text = next(e for e in scene["elements"] if e["type"] == "text")
    assert text["autoResize"] is False and "\n" in text["text"] and text["originalText"] == long_box.text
    assert back.pages[0].texts[0].text == long_box.text


def test_surrogates_and_invalid_values_do_not_break_the_writer():
    page = Page(float("nan"), 100, strokes=[Stroke([Point(float("inf"), 0, 1)]), Stroke([Point(1, 1, 1)])],
                texts=[TextBox(5, 5, 50, 10, "a\ud800b", runs=[TextRun("a\ud800b")])])
    doc = Document(pages=[page])
    data = write_excalidraw(doc)
    back = read_excalidraw(data)
    assert back.pages[0].texts[0].text == "a�b" and len(back.pages[0].strokes) == 1
    assert any("invalid coordinates" in w for w in doc.warnings) and any("A4" in w for w in doc.warnings)


# --------------------------------------------------------------------------- reader specifics


def test_frameless_scene_is_one_page_around_the_content():
    scene = _scene([_el("freedraw", x=100, y=200, points=[[0, 0], [50, 10]], pressures=[0.5, 0.5],
                        simulatePressure=False, strokeWidth=1),
                    _el("freedraw", x=0, y=0, points=[[0, 0], [5, 5]], isDeleted=True)])
    doc = read_excalidraw(scene)
    (page,) = doc.pages
    assert (page.width, page.height) == pytest.approx(((50 + 40) * PT_PER_PX, (10 + 40) * PT_PER_PX))
    stroke = page.strokes[0]
    assert (stroke.points[0].x, stroke.points[0].y) == pytest.approx((20 * PT_PER_PX, 20 * PT_PER_PX))
    assert stroke.points[0].width == pytest.approx(thickness_factor(0.5) * PT_PER_PX)


def test_frames_become_pages_and_loose_content_another_page():
    frame_a = _el("frame", id="A", x=0, y=1000, width=400, height=300, name="second")
    frame_b = _el("frame", id="B", x=0, y=0, width=400, height=300, name="first")
    scene = _scene([frame_a, frame_b,
                    _el("freedraw", frameId="A", x=10, y=1010, points=[[0, 0], [10, 0]], simulatePressure=True),
                    _el("rectangle", x=50, y=50, width=20, height=10),  # no frameId: inside frame B
                    _el("text", x=2000, y=2000, width=50, height=25, text="far", originalText="far", fontSize=20)])
    doc = read_excalidraw(scene)
    assert len(doc.pages) == 3
    first, second, loose = doc.pages
    assert (first.width, first.height) == pytest.approx((300.0, 225.0))
    assert len(first.strokes) == 1 and len(second.strokes) == 1 and loose.texts[0].text == "far"
    assert second.strokes[0].points[0].x == pytest.approx(10 * PT_PER_PX)
    assert second.strokes[0].points[0].width == pytest.approx(2 * 6.9 * PT_PER_PX)


def test_shapes_lines_arrows_and_fills():
    scene = _scene([
        _el("ellipse", x=0, y=0, width=100, height=50, backgroundColor="#ff0000", fillStyle="hachure", roughness=1),
        _el("diamond", x=200, y=0, width=40, height=40, strokeColor="transparent", backgroundColor="#00ff0080"),
        _el("arrow", x=0, y=100, points=[[0, 0], [50, 0]], strokeStyle="dashed"),
        _el("line", x=0, y=200, points=[[0, 0], [30, 0], [30, 30], [0, 0]], backgroundColor="#0000ff"),
        _el("rectangle", x=300, y=0, width=10, height=10, angle=math.pi / 2),
        _el("embeddable", x=0, y=0),
    ])
    doc = read_excalidraw(scene)
    kinds = [(s.kind, len(s.points)) for s in doc.pages[0].strokes]
    assert kinds == [("pen", 5), ("fill", 64), ("fill", 4), ("pen", 2), ("pen", 4), ("fill", 3), ("pen", 5)]
    ellipse = doc.pages[0].strokes[0]
    assert ellipse.controls is not None and len(ellipse.controls) == 4
    assert doc.pages[0].strokes[2].color[3] == pytest.approx(0x80 / 255)
    joined = " | ".join(doc.warnings)
    for needle in ("unsupported kind", "arrowheads", "hand-drawn", "hatched", "dashed"):
        assert needle in joined


def test_text_images_colours_and_background():
    data = base64.b64encode(png(8, 4)).decode()
    files = {"f1": {"mimeType": "image/png", "id": "f1", "dataURL": "data:image/png;base64," + data},
             "f2": {"mimeType": "image/svg+xml", "id": "f2", "dataURL": "data:image/svg+xml;base64,PHN2Zy8+"}}
    scene = _scene([
        _el("text", x=10, y=20, width=100, height=50, text="wrapped\ntext", originalText="wrapped text",
            fontSize=16, fontFamily=1, strokeColor="#f00", textAlign="right", angle=math.pi),
        _el("image", x=0, y=100, width=80, height=40, fileId="f1", scale=[-1, 1], angle=0.5),
        _el("image", x=0, y=200, width=80, height=40, fileId="f2"),
        _el("image", x=0, y=300, width=80, height=40, fileId="missing"),
    ], files=files, appState={"viewBackgroundColor": "#ffec99"})
    doc = read_excalidraw(scene)
    page = doc.pages[0]
    text = page.texts[0]
    assert text.text == "wrapped text" and text.runs[0].font == "Virgil" and text.size == pytest.approx(12.0)
    assert text.color == pytest.approx((1, 0, 0, 1)) and text.align == "right" and text.rotation == pytest.approx(180)
    image = page.images[0]
    assert image.data == png(8, 4) and image.rotation == pytest.approx(math.degrees(0.5)) and len(page.images) == 1
    assert page.background is not None and page.template_is_builtin
    joined = " | ".join(doc.warnings)
    assert "crops or flips" in joined and "neither PNG nor JPEG" in joined


def test_inkterop_fixture(samples):
    doc = read_excalidraw(samples.inkterop_fixture("excalidraw", "scribble.excalidraw").read_bytes())
    (page,) = doc.pages
    assert [(s.kind, len(s.points)) for s in page.strokes] == [("pen", 5), ("pen", 3), ("pen", 5)]
    pressured = page.strokes[0]
    assert [p.width for p in pressured.points] == pytest.approx(
        [2 * thickness_factor(p) * PT_PER_PX for p in (0.2, 0.4, 0.6, 0.8, 1.0)])
    assert page.strokes[1].color[3] == pytest.approx(0.6) and page.strokes[1].points[0].width == pytest.approx(
        4 * 6.9 * PT_PER_PX)
    assert page.texts[0].text == "hello ink" and page.texts[0].size == pytest.approx(15.0)
    back = read_excalidraw(write_excalidraw(doc))
    assert [(s.kind, len(s.points)) for s in back.pages[0].strokes][:2] == [("pen", 5), ("pen", 3)]
    assert [p.width for p in back.pages[0].strokes[0].points] == pytest.approx([p.width for p in pressured.points],
                                                                              rel=1e-3)


# --------------------------------------------------------------------------- GoodNotes / Notability samples


def _ink(doc: Document) -> List[int]:
    return [sum(1 for s in p.strokes if s.kind != "fill") for p in doc.pages]


def _rasters(doc: Document) -> List[int]:
    return [sum(1 for im in p.images if not im.data.startswith(b"%PDF")) for p in doc.pages]


def test_goodnotes_samples_to_excalidraw_and_back(samples):
    for path in samples.goodnotes_files():
        source = read_goodnotes(path.read_bytes())
        scene = read_excalidraw(write_excalidraw(source))
        assert len(scene.pages) == len(source.pages), path.name
        assert _ink(scene) == _ink(source), path.name
        assert _rasters(scene) == _rasters(source), path.name
        assert [len(p.texts) for p in scene.pages] == [len([t for t in p.texts if t.text.strip()])
                                                       for p in source.pages], path.name
        fills = sum(1 for p in source.pages for s in p.strokes if s.kind == "fill")
        assert sum(1 for p in scene.pages for s in p.strokes if s.kind == "fill") == fills, path.name
        again = read_goodnotes(write_goodnotes(scene))
        assert _ink(again) == _ink(scene), path.name


def test_notability_samples_to_excalidraw_and_back(samples):
    for path in samples.note_files():
        source = read_note(path.read_bytes())
        scene = read_excalidraw(write_excalidraw(source))
        assert len(scene.pages) == len(source.pages), path.name
        assert _ink(scene) == _ink(source) and _rasters(scene) == _rasters(source), path.name
        again = read_note(write_note(scene))
        assert stats(again)[1:3] == stats(scene)[1:3], path.name


# --------------------------------------------------------------------------- hardening


def test_size_and_count_limits(monkeypatch):
    monkeypatch.setattr(er, "MAX_SCENE_BYTES", 50)
    with pytest.raises(ValueError, match="larger than"):
        read_excalidraw(_scene([]))
    monkeypatch.setattr(er, "MAX_SCENE_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(er, "MAX_ELEMENTS", 2)
    monkeypatch.setattr(er, "MAX_POINTS_PER_ELEMENT", 3)
    many = [_el("freedraw", x=i, y=0, points=[[0, 0], [1, 1], [2, 2], [3, 3]]) for i in range(5)]
    doc = read_excalidraw(_scene(many))
    assert len(doc.pages[0].strokes) == 2 and all(len(s.points) == 3 for s in doc.pages[0].strokes)
    assert any("first 2 elements" in w for w in doc.warnings) and any("per element" in w for w in doc.warnings)


def test_image_data_limits(monkeypatch):
    monkeypatch.setattr(er, "MAX_FILE_BYTES", 10)
    data = base64.b64encode(png(8, 8)).decode()
    files = {"f": {"mimeType": "image/png", "dataURL": "data:image/png;base64," + data}}
    doc = read_excalidraw(_scene([_el("image", fileId="f")], files=files))
    assert not doc.pages[0].images and any("neither PNG" in w for w in doc.warnings)


def test_hostile_values_are_tolerated():
    raw = ('{"type": "excalidraw", "elements": [{"type": "freedraw", "x": NaN, "y": 0, "points": [[0, 0]]},'
           ' {"type": "freedraw", "x": 1e300, "y": 0, "points": [[0, 0], [1, 1]]},'
           ' {"type": "freedraw", "x": 0, "y": 0, "points": [[0, 0], ["a", 1], [Infinity, 2], [3, 3]],'
           '  "pressures": "x", "strokeWidth": -5, "strokeColor": "rgb(1,2,3)", "opacity": "x"},'
           ' {"type": "text", "x": 0, "y": 0, "text": 5}, {"type": "image", "x": 0, "y": 0, "width": 5,'
           '  "height": 5, "fileId": {}}, "junk", {"type": "rectangle", "x": "a"}], "files": [], "appState": 3}')
    doc = read_excalidraw(raw.encode())
    page = doc.pages[0]
    assert len(page.strokes) == 1 and [(p.x, p.y) for p in page.strokes[0].points][1] != (math.inf, 2)
    assert all(math.isfinite(p.x) for p in page.strokes[0].points)
    assert any("invalid coordinates" in w for w in doc.warnings) and any("colours" in w for w in doc.warnings)


@pytest.mark.parametrize("data", [b"", b"[]", b"{}", b'{"type": "other"}', b"\xff\xfe", b"not json",
                                  b'{"type": "excalidraw", "elements": 5}', b"[" * 50000])
def test_not_an_excalidraw_file(data: bytes):
    with pytest.raises(ValueError):
        read_excalidraw(data)


def test_fuzzed_scenes_raise_only_value_error():
    seed = write_excalidraw(full_document())
    rng = random.Random(99)
    for _ in range(80):
        data = bytearray(seed)
        for _ in range(rng.randint(1, 10)):
            data[rng.randrange(len(data))] = rng.choice(b'0123456789-.,:{}[]"eE \nnulltruefalse')
        try:
            doc = read_excalidraw(bytes(data))
        except ValueError:
            continue
        assert isinstance(doc, Document)
    scene = json.loads(seed)
    for _ in range(80):  # structurally valid JSON with values of the wrong type
        mutated = json.loads(json.dumps(scene))
        element = rng.choice(mutated["elements"])
        key = rng.choice(sorted(element))
        element[key] = rng.choice([None, "x", -1, 1e308, [], {}, [[None]], True])
        doc = read_excalidraw(json.dumps(mutated).encode())
        assert isinstance(doc, Document)


# --------------------------------------------------------------------------- registry, sniffing, CLI


def test_registry_entry_and_sniffing():
    fmt = formats.get("excalidraw")
    assert (fmt.name, fmt.extension, fmt.input_extensions) == ("Excalidraw", ".excalidraw", (".excalidraw",))
    assert detect_format("renamed.json", write_excalidraw(full_document())) == "excalidraw"
    assert detect_format("x.excalidraw", b"garbage") == "excalidraw"
    assert not fmt.sniff(b'{"v": 19, "z": []}', None) and not fmt.sniff(b"PK..", ["main.sbn2"])
    assert formats.get("saber").sniff(write_excalidraw(full_document()), None) is False


def test_convert_api_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    result = convert(write_note(full_document()), "Every.note", Options(target="excalidraw"))
    assert result.filename == "Every.excalidraw" and json.loads(result.data)["type"] == "excalidraw"
    assert main(["formats"]) == 0
    assert "excalidraw" in capsys.readouterr().out
    src = tmp_path / "Board.excalidraw"
    src.write_bytes(write_excalidraw(full_document()))
    assert main(["convert", str(src), "--to", "saber"]) == 0
    assert "excalidraw -> saber" in capsys.readouterr().out
    assert (tmp_path / "Board.sba").is_file()
    assert main(["convert", str(src)]) == 0  # default target: Notability
    assert len(read_note((tmp_path / "Board.note").read_bytes()).pages) == 4
