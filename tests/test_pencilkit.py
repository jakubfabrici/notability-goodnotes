"""gnnote.pencilkit: synthetic PKDrawings, Apple's own fixtures (inkterop, CC0) and a real
iOS corpus (r987r/Flashcard, fetched at its pinned commit; skipped when absent)."""
from __future__ import annotations

import json
import math
import plistlib
import random
import struct
from pathlib import Path
from typing import Any, List

import pytest

from gnnote import pencilkit
from gnnote import protobuf as pb
from tests.cnote_builders import EMPTY_PKDRAWING, pk_blob, pk_ink, pk_path, pk_point, pk_stroke


def _blob(*points_lists, ink: str = "com.apple.ink.pen", **kw) -> bytes:
    return pk_blob([pk_ink(ink)], [pk_stroke(pk_path(points)) for points in points_lists], **kw)


# --------------------------------------------------------------------------- container


def test_collanote_empty_drawing_has_no_strokes() -> None:
    drawing = pencilkit.parse_pkdrawing(EMPTY_PKDRAWING)
    assert drawing.version == 1 and drawing.known_version
    assert drawing.strokes == [] and drawing.inks == []
    assert (drawing.tombstones, drawing.damaged, drawing.empty) == (0, 0, 0)
    assert pencilkit.decode_pkdrawing(EMPTY_PKDRAWING) == []


@pytest.mark.parametrize("data", [b"", b"wrd", b"wrd\xf0\x01", b"PK\x03\x04", b"bplist00", b"x" * 100])
def test_not_a_pkdrawing_is_a_value_error(data: bytes) -> None:
    assert not pencilkit.is_pkdrawing(data) or len(data) < 6
    with pytest.raises(ValueError):
        pencilkit.parse_pkdrawing(data)


def test_non_bytes_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        pencilkit.parse_pkdrawing("wrd")  # type: ignore[arg-type]


def test_damaged_top_level_message_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="damaged PKDrawing"):
        pencilkit.parse_pkdrawing(b"wrd\xf0\x01\x00" + b"\x2a\x50abc")  # length runs past the end


# --------------------------------------------------------------------------- channels


def test_every_channel_per_point_round_trips() -> None:
    pts = [pk_point(10.0, 20.0, t=0.0, w=3.0, aspect=1500, force=250, azimuth=0, altitude=65535, opacity=65535,
                    secondary=6.0),
           pk_point(30.5, 40.25, t=0.125, w=4.0, aspect=1000, force=1000, azimuth=65535, altitude=0, opacity=16384,
                    secondary=4.0)]
    blob = pk_blob([pk_ink("com.apple.ink.pen", (0.25, 0.5, 0.75, 1.0))], [pk_stroke(pk_path(pts))])
    (stroke,) = pencilkit.decode_pkdrawing(blob)
    assert stroke.ink == "com.apple.ink.pen" and stroke.color == (0.25, 0.5, 0.75, 1.0)
    a, b = stroke.points
    assert (a.x, a.y, a.time, a.width) == (10.0, 20.0, 0.0, 3.0)
    assert a.height == pytest.approx(4.5) and a.force == pytest.approx(0.25)
    assert a.azimuth == pytest.approx(-math.pi) and a.altitude == pytest.approx(0.0)
    assert a.opacity == pytest.approx(2.0) and a.secondary_scale == pytest.approx(2.0)
    assert (b.x, b.y, b.time, b.width) == (30.5, 40.25, 0.125, 4.0)
    assert b.height == pytest.approx(4.0) and b.force == pytest.approx(1.0)
    assert b.azimuth == pytest.approx(math.pi) and b.altitude == pytest.approx(math.pi / 2)
    assert b.opacity == pytest.approx(0.5, abs=1e-4) and b.secondary_scale == pytest.approx(1.0)
    assert stroke.created == pytest.approx(780000000.0)


def test_constant_block_feeds_every_point() -> None:
    # a dot: only the location is per point (masks 0x001 / 0x7FE), like Apple's case01-dot
    blob = pk_blob([pk_ink()], [pk_stroke(pk_path([pk_point(100.0, 100.0, w=5.0, force=500)], per_point=1))])
    (stroke,) = pencilkit.decode_pkdrawing(blob)
    (p,) = stroke.points
    assert (p.x, p.y, p.width, p.force) == (100.0, 100.0, 5.0, 0.5)
    assert p.opacity == pytest.approx(0.99998, abs=1e-4)


def test_version_2_ink_variant_and_path_field_9() -> None:
    path = pk_path([pk_point(1, 2), pk_point(3, 4)], extra=pb.field_varint(9, 0))
    blob = pk_blob([pk_ink("com.apple.ink.pen", variant="fixed-width")], [pk_stroke(path)], version=2)
    drawing = pencilkit.parse_pkdrawing(blob)
    assert drawing.version == 2 and drawing.known_version
    assert drawing.inks[0].variant == "fixed-width"
    assert drawing.strokes[0].ink_variant == "fixed-width" and len(drawing.strokes[0].points) == 2
    unknown = pencilkit.parse_pkdrawing(blob[:4] + struct.pack("<H", 7) + blob[6:])
    assert unknown.version == 7 and not unknown.known_version and len(unknown.strokes) == 1


def test_tombstones_are_skipped_and_counted() -> None:
    live = pk_stroke(pk_path([pk_point(1, 1), pk_point(2, 2)]))
    blob = pk_blob([pk_ink()], [pk_stroke(None), live, pk_stroke(None)])
    drawing = pencilkit.parse_pkdrawing(blob)
    assert len(drawing.strokes) == 1 and drawing.tombstones == 2 and drawing.damaged == 0


def test_transform_moves_the_points_and_scales_widths() -> None:
    pts = [pk_point(10.0, 10.0, w=2.0), pk_point(20.0, 10.0, w=2.0)]
    moved = pk_blob([pk_ink()], [pk_stroke(pk_path(pts), transform=(1, 0, 0, 1, -62.5, 59.5))])
    (stroke,) = pencilkit.decode_pkdrawing(moved)
    assert [(p.x, p.y) for p in stroke.points] == [(-52.5, 69.5), (-42.5, 69.5)]
    assert stroke.transform == (1.0, 0.0, 0.0, 1.0, -62.5, 59.5) and stroke.points[0].width == 2.0
    scaled = pk_blob([pk_ink()], [pk_stroke(pk_path(pts), transform=(2, 0, 0, 2, 1, 1))])
    (stroke,) = pencilkit.decode_pkdrawing(scaled)
    assert [(p.x, p.y) for p in stroke.points] == [(21.0, 21.0), (41.0, 21.0)]
    assert stroke.points[0].width == pytest.approx(4.0)
    rotated = pk_blob([pk_ink()], [pk_stroke(pk_path(pts), transform=(0, 1, -1, 0, 0, 0))])
    (stroke,) = pencilkit.decode_pkdrawing(rotated)  # (x, y) -> (a x + c y + tx, b x + d y + ty)
    assert [(p.x, p.y) for p in stroke.points] == [(-10.0, 10.0), (-10.0, 20.0)]


def test_damaged_strokes_are_skipped_and_counted() -> None:
    good = pk_stroke(pk_path([pk_point(1, 1), pk_point(2, 2)]))
    bad_count = pk_stroke(pk_path([pk_point(1, 1), pk_point(2, 2)], count=3))  # records do not match
    bad_masks = pk_stroke(pk_path([pk_point(1, 1)]).replace(pb.field_varint(5, 0x7FE), pb.field_varint(5, 0x7FF)))
    bad_ink = pk_stroke(pk_path([pk_point(1, 1)]), ink=5)
    not_a_message = pb.field_bytes(5, b"\xff\xff")
    blob = pk_blob([pk_ink()], [good, bad_count, bad_masks, bad_ink, not_a_message])
    drawing = pencilkit.parse_pkdrawing(blob)
    assert len(drawing.strokes) == 1 and drawing.damaged == 4


def test_point_and_stroke_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    blob = _blob([pk_point(i, i) for i in range(10)], [pk_point(i, i) for i in range(10)])
    monkeypatch.setattr(pencilkit, "MAX_POINTS", 15)
    with pytest.raises(ValueError, match="more than 15 points"):
        pencilkit.parse_pkdrawing(blob)
    monkeypatch.setattr(pencilkit, "MAX_POINTS", 1000)
    monkeypatch.setattr(pencilkit, "MAX_STROKES", 1)
    with pytest.raises(ValueError, match="more than 1 strokes"):
        pencilkit.parse_pkdrawing(blob)


def test_fuzzed_drawings_raise_nothing_but_value_error() -> None:
    rng = random.Random(1234)
    base = pk_blob([pk_ink(), pk_ink("com.apple.ink.marker", (1, 1, 0, 1))],
                   [pk_stroke(pk_path([pk_point(i, 2 * i, t=i / 10, force=100 * i) for i in range(6)]), ink=i % 2,
                              transform=(1, 0, 0, 1, 3, 4) if i else None) for i in range(4)] + [pk_stroke(None)])
    assert len(pencilkit.decode_pkdrawing(base)) == 4
    for trial in range(1500):
        data = bytearray(base)
        for _ in range(rng.randint(1, 8)):
            data[rng.randrange(6, len(data))] = rng.randrange(256)
        if trial % 5 == 0:
            data = data[: rng.randrange(6, len(data))]
        try:
            pencilkit.parse_pkdrawing(bytes(data))
        except ValueError:
            pass


# --------------------------------------------------------------------------- model conversion


def test_ink_kinds() -> None:
    assert pencilkit.ink_kind("com.apple.ink.pen") == ("pen", None, True)
    assert pencilkit.ink_kind("com.apple.ink.marker") == ("highlighter", "marker", True)
    assert pencilkit.ink_kind("com.apple.ink.pencil") == ("pen", "pencil", True)
    assert pencilkit.ink_kind("COM.APPLE.INK.FOUNTAINPEN") == ("pen", "fountain", True)
    assert pencilkit.ink_kind("com.example.sparkle") == ("pen", None, False)


def test_to_model_stroke_scales_offsets_and_folds_opacity() -> None:
    pts = [pk_point(10.0, 20.0, w=4.0, opacity=16384), pk_point(30.0, 40.0, w=6.0, opacity=16384)]
    blob = pk_blob([pk_ink("com.apple.ink.pencil", (0.2, 0.4, 0.6, 1.0))], [pk_stroke(pk_path(pts))])
    (pk,) = pencilkit.decode_pkdrawing(blob)
    stroke = pencilkit.to_model_stroke(pk, scale=0.5, dx=-10.0, dy=0.0)
    assert stroke is not None and stroke.kind == "pen" and stroke.pen == "pencil" and stroke.controls is None
    assert [(p.x, p.y, p.width) for p in stroke.points] == [(0.0, 10.0, 2.0), (10.0, 20.0, 3.0)]
    assert stroke.width == pytest.approx(2.5)
    assert stroke.color[:3] == pytest.approx((0.2, 0.4, 0.6)) and stroke.color[3] == pytest.approx(0.5, abs=1e-3)
    marker = pk_blob([pk_ink("com.apple.ink.marker", (1.0, 0.9, 0.0, 1.0))], [pk_stroke(pk_path(pts[:1]))])
    (pk,) = pencilkit.decode_pkdrawing(marker)
    converted = pencilkit.to_model_strokes([pk])
    assert len(converted) == 1 and converted[0].kind == "highlighter"


def test_to_model_stroke_drops_non_finite_points() -> None:
    pts = [pk_point(float("nan"), 1.0), pk_point(1.0, float("inf")), pk_point(5.0, 6.0, w=0.0)]
    (pk,) = pencilkit.decode_pkdrawing(pk_blob([pk_ink()], [pk_stroke(pk_path(pts, per_point=0x7))]))
    stroke = pencilkit.to_model_stroke(pk, scale=2.0)
    assert stroke is not None and [(p.x, p.y) for p in stroke.points] == [(10.0, 12.0)]
    assert stroke.points[0].width > 0  # a zero width falls back to a nominal one
    only_bad = pencilkit.PKStroke("com.apple.ink.pen", (0, 0, 0, 1), [pencilkit.PKPoint(float("nan"), 0.0)])
    assert pencilkit.to_model_stroke(only_bad) is None


# --------------------------------------------------------------------------- Apple's fixtures (inkterop, CC0)


def test_inkterop_fixtures_match_apples_truth(samples: Any) -> None:
    """Every channel of every point of the four PencilKit-written fixtures."""
    for blob_path in samples.pkdrawing_fixtures():
        truth = json.loads(blob_path.with_name(blob_path.stem + ".truth.json").read_text(encoding="utf-8"))
        drawing = pencilkit.parse_pkdrawing(blob_path.read_bytes())
        assert (drawing.damaged, drawing.empty, drawing.tombstones) == (0, 0, 0)
        assert len(drawing.strokes) == truth["strokeCount"], blob_path.name
        for stroke, want in zip(drawing.strokes, truth["strokes"]):
            assert stroke.ink == want["ink"]
            assert len(stroke.points) == want["controlPointCount"]
            c = want["color"]
            assert stroke.color == pytest.approx((c["r"], c["g"], c["b"], c["a"]), abs=1e-6)
            rb = want["renderBounds"]
            assert stroke.render_bounds == pytest.approx((rb["x"], rb["y"], rb["w"], rb["h"]), abs=1e-4)
            for p, q in zip(stroke.points, want["controlPoints"]):
                got = (p.x, p.y, p.time, p.width, p.height, p.force, p.azimuth, p.altitude, p.opacity,
                       p.secondary_scale)
                expected = (q["x"], q["y"], q["t"], q["w"], q["h"], q["force"], q["azimuth"], q["altitude"],
                            q["opacity"], q["secondaryScale"])
                assert got == pytest.approx(expected, abs=1e-9), blob_path.name


# --------------------------------------------------------------------------- real iOS corpus (r987r/Flashcard)


def _pk_blobs(path: Path) -> List[bytes]:
    found: List[bytes] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
        elif isinstance(obj, (bytes, bytearray)) and pencilkit.is_pkdrawing(obj):
            found.append(bytes(obj))

    walk(plistlib.loads(path.read_bytes()))
    return found


def test_real_ios_corpus_decodes_completely(samples: Any) -> None:
    cards = samples.repo("Flashcard") / "Cards.cards"
    if not cards.is_file():
        pytest.skip("Cards.cards not available")
    blobs = _pk_blobs(cards)
    totals = {"strokes": 0, "tombstones": 0, "damaged": 0, "empty": 0, "points": 0, "transformed": 0}
    versions: dict = {}
    for blob in blobs:
        drawing = pencilkit.parse_pkdrawing(blob)
        versions[drawing.version] = versions.get(drawing.version, 0) + 1
        totals["strokes"] += len(drawing.strokes)
        totals["tombstones"] += drawing.tombstones
        totals["damaged"] += drawing.damaged
        totals["empty"] += drawing.empty
        totals["points"] += sum(len(s.points) for s in drawing.strokes)
        totals["transformed"] += sum(1 for s in drawing.strokes if s.transform is not None)
        for stroke in drawing.strokes:
            # renderBounds are stored in page space: a lasso-moved stroke's points only fall inside
            # them once its transform is applied (checked: 2,013 / 2,013, and 0 without it)
            x, y, w, h = stroke.render_bounds
            assert all(x - 2 <= p.x <= x + w + 2 and y - 2 <= p.y <= y + h + 2 for p in stroke.points)
    # every record decodes with no leftover byte (record sizes must match exactly)
    assert totals["damaged"] == 0 and totals["empty"] == 0
    if samples.at_pinned_commit("Flashcard") is not False:
        assert len(blobs) == 854 and versions == {1: 853, 2: 1}
        assert totals == {"strokes": 16401, "tombstones": 543, "damaged": 0, "empty": 0, "points": 444971,
                          "transformed": 2013}
