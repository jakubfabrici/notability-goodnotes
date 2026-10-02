"""Unit tests for gnnote.tpl: the generic image codec, the hand-decoded strokes of
docs/goodnotes-stroke.md sections 2 and 3, byte-exact re-encoding of every stroke in the
sample notebooks, and the writer recipe of section 8."""
from __future__ import annotations

import math
import struct
import zipfile

import pytest

from gnnote import applelz4, protobuf as pb, tpl

PEN_PAGE = "notes/F51610D9-2DEC-4842-BD27-12371BC96206"
HL_PAGE = "notes/27B0C0FB-E6F5-4C9E-BDA0-98553E08EC7B"


def _bits(f: float) -> bytes:
    return struct.pack("<f", f)


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


# --------------------------------------------------------------------------- generic codec


def test_parse_format():
    assert tpl.parse_format("vu") == ["v", "u"]
    assert tpl.parse_format("A(v)") == [("A", ["v"])]
    assert tpl.parse_format("A(S(uu))") == [("A", [("S", ["u", "u"])])]
    for bad in ("x", "A(", "A()", "v)", "A(v", "Av"):
        with pytest.raises(ValueError):
            tpl.parse_format(bad)


def test_image_round_trip_all_scalars():
    img = tpl.TplImage("cjviuIUfA(j)A(S(uf))S(cv)", [-1, -2, 3, -4, 5, -6, 7, 1.5, [1, -1], [(1, 2.0), (3, 4.5)], (9, 10)])
    data = tpl.encode_image(img)
    assert data[:4] == b"tpl\x00" and struct.unpack_from("<I", data, 4)[0] == len(data)
    assert tpl.decode_image(data) == img


@pytest.mark.parametrize("mutate", [
    lambda d: b"xpl" + d[3:],                               # bad magic
    lambda d: d[:3] + b"\x01" + d[4:],                      # big-endian flag
    lambda d: d[:4] + _u32(len(d) + 1) + d[8:],             # size mismatch
    lambda d: d + b"\x00",                                  # trailing byte (size also wrong)
    lambda d: d[:4] + _u32(len(d) + 1) + d[8:] + b"\x00",   # trailing byte with matching size
    lambda d: d[:-1],                                       # truncated
    lambda d: d[:4] + _u32(len(d) - 1) + d[8:-1],           # truncated with matching size
    lambda d: d[:8] + d[8:].replace(b"A(v)", b"A(x)"),      # unknown format char
    lambda d: d[:8] + d[8:].replace(b"\x00", b"", 1),       # unterminated format
])
def test_decode_image_rejects_malformed(mutate):
    good = tpl.encode_flat(tpl.FlatStroke(24.0, (1.0, 2.0), [(3.0, 4.0, 5.0, 6.0)]))
    with pytest.raises(ValueError):
        tpl.decode_image(mutate(good))


def test_decode_unknown_format_rejected():
    data = tpl.encode_image(tpl.TplImage("vA(u)", [1, [1, 2]]))
    assert tpl.decode_image(data).fmt == "vA(u)"
    with pytest.raises(ValueError):
        tpl.decode(data)


def test_encode_image_value_errors():
    with pytest.raises(ValueError):
        tpl.encode_image(tpl.TplImage("vu", [1]))
    with pytest.raises(ValueError):
        tpl.encode_image(tpl.TplImage("v", [70000]))
    with pytest.raises(ValueError):
        tpl.encode_image(tpl.TplImage("A(S(uu))", [[(1, 2, 3)]]))


# --------------------------------------------------------------------------- flat recipe


def test_encode_flat_exact_bytes_minimal():
    data = tpl.encode_flat(tpl.FlatStroke(width=24.0, start=(1.0, 2.0), quads=[(3.0, 4.0, 5.0, 6.0)]))
    expected = (b"tpl\x00" + _u32(90) + b"vuA(v)A(S(uu))A(S(uuuu))vA(f)\x00"
                + b"\x02\x00" + b"\x00\x00\xc0\x41" + _u32(2) + b"\x00\x00\x01\x00"
                + _u32(1) + _bits(1.0) + _bits(2.0)
                + _u32(1) + _bits(3.0) + _bits(4.0) + _bits(5.0) + _bits(6.0)
                + b"\x01\x00" + _u32(0))
    assert data == expected
    assert len(data) == 8 + 30 + 2 + 4 + (4 + 2 * 2) + (4 + 8) + (4 + 16) + 2 + 4
    stroke = tpl.decode(data)
    assert isinstance(stroke, tpl.FlatStroke)
    assert stroke.width == 24.0 and stroke.start == (1.0, 2.0) and stroke.quads == [(3.0, 4.0, 5.0, 6.0)]
    assert stroke.flags == [0, 1] and stroke.version == 2 and stroke.has_trailer and stroke.dash == []
    assert tpl.encode_flat(stroke) == data


def test_encode_flat_short_form():
    stroke = tpl.FlatStroke(2.0, (0.0, 0.0), [(1.0, 1.0, 2.0, 2.0)], version=1, has_trailer=False)
    data = tpl.encode_flat(stroke)
    assert data[8:33] == b"vuA(v)A(S(uu))A(S(uuuu))\x00"
    assert len(data) == 8 + 25 + 2 + 4 + 8 + 12 + 20
    back = tpl.decode(data)
    assert back.version == 1 and not back.has_trailer and back.quads == stroke.quads


def test_empty_header_decodes_to_none():
    data = tpl.encode_empty_flat()
    assert len(data) == 62
    assert tpl.decode(data) is None
    assert tpl.decode(tpl.encode_empty_flat(5.0)) is None
    ribbon_empty = tpl.encode_image(tpl.TplImage(tpl.RIBBON_FORMAT, [2, [], [], [], [], [], [], [], [], [], []]))
    assert len(ribbon_empty) == 92
    assert tpl.decode(ribbon_empty) is None
    pencil_empty = tpl.encode_image(tpl.TplImage(tpl.PENCIL_FORMAT, [1, 0, [], [], [], [], [], [], [], []]))
    assert tpl.decode(pencil_empty) is None


def test_from_polyline_midpoint_controls():
    s = tpl.FlatStroke.from_polyline([(0, 0), (2, 2), (4, 0)], 4.0)
    assert s.start == (0.0, 0.0) and s.quads == [(1.0, 1.0, 2.0, 2.0), (3.0, 1.0, 4.0, 0.0)]
    assert s.anchors() == [(0.0, 0.0), (2.0, 2.0), (4.0, 0.0)]
    assert len(s.polyline()) == 5  # 2N - 1, odd by construction
    assert s.cubic_controls()[0] == (pytest.approx((2 / 3, 2 / 3)), pytest.approx((4 / 3, 4 / 3)))
    s = tpl.FlatStroke.from_polyline([(0, 0), (1, 1), (2, 0), (3, 1)], 4.0)
    assert len(s.quads) == 3 and len(s.polyline()) == 7
    s = tpl.FlatStroke.from_polyline([(5, 5)], 4.0)
    assert s.start == (5.0, 5.0) and s.quads == [pytest.approx((5.15, 5.0, 5.3, 5.0))]
    with pytest.raises(ValueError):
        tpl.FlatStroke.from_polyline([], 4.0)
    back = tpl.decode(tpl.encode_flat(s))
    assert back.anchors() == [(5.0, 5.0), pytest.approx((5.3, 5.0))]


def test_from_point_pairs_odd_rule():
    s = tpl.FlatStroke.from_point_pairs([(0, 0), (1, 1), (2, 0)], 4.0)
    assert s.start == (0.0, 0.0) and s.quads == [(1.0, 1.0, 2.0, 0.0)]
    s = tpl.FlatStroke.from_point_pairs([(0, 0), (1, 1), (2, 0), (3, 1)], 4.0)
    assert s.quads == [(1.0, 1.0, 2.0, 0.0), (3.0, 1.0, 3.0, 1.0)]  # last point duplicated
    assert s.polyline() == [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0), (3.0, 1.0), (3.0, 1.0)]
    s = tpl.FlatStroke.from_point_pairs([(5, 5)], 4.0)
    assert s.start == (5.0, 5.0) and s.quads == [(5.3, 5.0, 5.3, 5.0)]
    with pytest.raises(ValueError):
        tpl.FlatStroke.from_point_pairs([], 4.0)


def test_cubic_controls_degree_elevation():
    s = tpl.FlatStroke.from_quadratics((0, 0), [(3, 0, 6, 0), (6, 3, 6, 6)], 1.0)
    assert s.anchors() == [(0.0, 0.0), (6.0, 0.0), (6.0, 6.0)]
    c = s.cubic_controls()
    assert c[0] == ((2.0, 0.0), (4.0, 0.0))
    assert c[1][0] == pytest.approx((6.0, 2.0)) and c[1][1] == pytest.approx((6.0, 4.0))


def test_multi_subpath_flat():
    s = tpl.FlatStroke(1.0, (0.0, 0.0), [(1.0, 1.0, 2.0, 2.0), (9.0, 9.0, 8.0, 8.0)],
                       flags=[0, 1, 0, 1], extra_starts=[(7.0, 7.0)])
    data = tpl.encode_flat(s)
    back = tpl.decode(data)
    assert back.subpaths() == [((0.0, 0.0), [(1.0, 1.0, 2.0, 2.0)]), ((7.0, 7.0), [(9.0, 9.0, 8.0, 8.0)])]
    assert back.anchors() == [(0.0, 0.0), (2.0, 2.0)]
    assert tpl.encode_flat(back) == data


def test_encode_flat_rejects_inconsistent():
    with pytest.raises(ValueError):
        tpl.encode_flat(tpl.FlatStroke(1.0, (0.0, 0.0), [(1.0, 1.0, 2.0, 2.0)], flags=[0, 1, 1]))
    with pytest.raises(ValueError):
        tpl.encode_flat(tpl.FlatStroke(1.0, (0.0, 0.0), [(1.0, 1.0, 2.0, 2.0)], flags=[0, 2]))
    with pytest.raises(ValueError):
        tpl.encode_flat(tpl.FlatStroke(1.0, (0.0, 0.0), []))
    with pytest.raises(ValueError):
        tpl.encode_flat(tpl.FlatStroke(float("nan"), (0.0, 0.0), [(1.0, 1.0, 2.0, 2.0)]))
    with pytest.raises(ValueError):
        tpl.encode_flat(tpl.FlatStroke(1.0, (0.0, 0.0), [(1.0, 1.0, 2.0)]))  # type: ignore[list-item]
    with pytest.raises(ValueError):
        tpl.decode(tpl.encode_image(tpl.TplImage(tpl.FLAT_FORMAT, [2, 0, [1], [], [(1, 2, 3, 4)], 1, []])))


# --------------------------------------------------------------------------- sample strokes


def _stroke_records(path, member):
    with zipfile.ZipFile(path) as z:
        records = pb.decode_records(z.read(member))
    out = {}
    for index, rec in enumerate(records):
        content = pb.get(pb.decode_message(rec), 7)
        if content is None:
            continue
        fields = pb.decode_message(content.value)
        geo = pb.get(fields, 2)
        if geo is not None and applelz4.is_apple_lz4(geo.value):
            out[index] = (fields, applelz4.decompress(geo.value))
    return out


@pytest.fixture(scope="module")
def test5(samples):
    path = samples.repo("goodparse") / "samples" / "Test5.goodnotes"
    if not path.is_file():
        pytest.skip("Test5.goodnotes not available")
    return path


def test_hand_decode_pen_ribbon(test5):
    fields, raw = _stroke_records(test5, PEN_PAGE)[19]
    assert len(raw) == 658
    assert pb.string_value(pb.get(fields, 1)) == "6C008FF2-04F1-4455-B2F5-5AE0C232CB8A"
    assert pb.varint_value(pb.get(fields, 3)) == 1
    s = tpl.decode(raw)
    assert isinstance(s, tpl.RibbonStroke)
    assert s.version == 2 and s.width is None
    assert s.flags == [0, 1, 1, 1, 1]
    assert len(s.points) == 9 and len(s.subpaths) == 1
    assert s.points[0] == pytest.approx((253.2926, 346.6660, 2.9469), abs=1e-4)
    assert s.points[1] == pytest.approx((244.8517, 339.4684, 2.9397), abs=1e-4)
    assert s.points[2] == pytest.approx((240.4677, 332.4016, 3.1278), abs=1e-4)
    assert s.points[-1] == pytest.approx((244.7863, 292.2255, 3.8103), abs=1e-4)
    assert s.start_extra == [[]] and s.panel_extra == [[]] * 4
    assert s.panel_counts == [5, 5, 5, 5]
    assert s.commands == [0, 2, 3, 2, 3, 0, 2, 3, 2, 3, 0, 3, 2, 3, 2, 0, 2, 3, 2, 3]
    assert sorted(s.commands) == [0] * 4 + [2] * 8 + [3] * 8
    assert len(s.moves) == 8 and s.moves[:2] == pytest.approx((237.8478, 334.1101), abs=1e-4)
    assert s.pool7 == []
    assert len(s.cubics) == 48 and s.cubics[:2] == pytest.approx((241.512, 339.729), abs=1e-3)
    assert len(s.arcs) == 40
    assert s.arcs[:5] == pytest.approx((253.2926, 346.6660, 2.9469, 2.2775, -0.8654), abs=1e-4)
    assert s.arc_flags == [1] * 8
    assert tpl.encode_image(s.image) == raw
    # every arc is centred on a centre-line point with that point's radius
    pts = {(round(x, 3), round(y, 3)): r for x, y, r in s.points}
    for k in range(0, 40, 5):
        cx, cy, r = s.arcs[k:k + 3]
        assert pts[(round(cx, 3), round(cy, 3))] == pytest.approx(r, abs=1e-5)


def test_hand_decode_highlighter_flat(test5):
    fields, raw = _stroke_records(test5, HL_PAGE)[57]
    assert len(raw) == 270
    assert pb.get(fields, 3) is None and pb.varint_value(pb.get(fields, 5)) == 1
    assert raw[38:44] == b"\x02\x00\x00\x00\xc0\x41"
    s = tpl.decode(raw)
    assert isinstance(s, tpl.FlatStroke)
    assert s.width == 24.0 and s.version == 2 and s.has_trailer
    assert s.flags == [0] + [1] * 11 and s.extra_starts == []
    assert s.start == pytest.approx((271.6290, 35.4287), abs=1e-4)
    assert len(s.quads) == 11
    assert s.quads[0] == pytest.approx((273.7178, 37.0463, 276.8644, 40.6287), abs=1e-4)
    assert s.quads[1] == pytest.approx((278.8803, 42.9238, 283.0983, 48.3202), abs=1e-4)
    assert s.quads[-1] == pytest.approx((346.9672, 190.8571, 348.4650, 192.9523), abs=1e-4)
    assert len(s.polyline()) == 23 and len(s.anchors()) == 12
    assert s.trailer_word == 1 and s.dash == []
    assert tpl.encode_flat(s) == raw
    # the writer recipe reproduces the app's bytes from the typed values alone
    rebuilt = tpl.FlatStroke(width=s.width, start=s.start, quads=s.quads)
    assert tpl.encode_flat(rebuilt) == raw


def test_hand_decode_pencil(test5):
    fields, raw = _stroke_records(test5, PEN_PAGE)[23]
    assert pb.varint_value(pb.get(fields, 3)) == 5 and pb.varint_value(pb.get(fields, 21)) == 25
    s = tpl.decode(raw)
    assert isinstance(s, tpl.PencilStroke)
    assert s.width == pytest.approx(1.559055, abs=1e-6) and s.version == 1
    assert s.flags == [0] + [1] * 13
    assert len(s.points) == 27 and len(s.attrs) == 27 and len(s.seeds) == 13 and len(s.subpaths) == 1
    assert s.points[0] == pytest.approx((374.5799, 445.0452), abs=1e-3)
    assert s.points[1] == pytest.approx((374.7918, 439.1896), abs=1e-3)
    for a, b, c in s.attrs:
        assert a == pytest.approx(math.pi / 6, abs=1e-5) and b == pytest.approx(math.pi / 3, abs=1e-5) and c == 0.0
    assert s.trailing == [[], [], [], [], []]
    assert tpl.encode_image(s.image) == raw


def test_multi_subpath_ribbon_and_variants(test5):
    _fields, raw = _stroke_records(test5, PEN_PAGE)[1]
    s = tpl.decode(raw)
    assert isinstance(s, tpl.RibbonStroke)
    assert s.flags == [0] + [1] * 9 + [0] + [1] * 9
    assert len(s.subpaths) == 2 and [len(p) for p in s.subpaths] == [19, 19] and len(s.points) == 38
    assert len(s.cubics) == 37 * 6
    _fields, raw = _stroke_records(test5, HL_PAGE)[7]
    s = tpl.decode(raw)
    assert s.flags[:2] == [4, 5] and set(s.flags) == {4, 5}
    assert s.start_extra == [[0.0]]
    assert all(e[:2] == [0.0, 0.0] and e[2] == pytest.approx(0.1) for e in s.panel_extra)
    assert all(r == 18.0 for _x, _y, r in s.points)
    assert s.commands[:8] == [0, 2, 2, 6, 2, 2, 2, 6]
    assert len(s.arcs) % 7 == 0


def _all_images(samples):
    for path in samples.goodnotes_files():
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not name.startswith("notes/"):
                    continue
                for rec in pb.decode_records(z.read(name)):
                    for content in pb.get_all(pb.decode_message(rec), 7):
                        fields = pb.decode_message(content.value)
                        geo = pb.get(fields, 2)
                        if geo is not None and applelz4.is_apple_lz4(geo.value):
                            yield path.name, name, fields, applelz4.decompress(geo.value)


def test_every_sample_stroke_round_trips(samples):
    counts = {"flat": 0, "ribbon": 0, "pencil": 0, "empty": 0}
    for _file, _page, fields, raw in _all_images(samples):
        image = tpl.decode_image(raw)
        assert image.fmt in tpl.KNOWN_FORMATS
        assert tpl.encode_image(image) == raw
        s = tpl.decode(raw)
        f3 = pb.get(fields, 3)
        if s is None:
            counts["empty"] += 1
            assert not any(isinstance(v, list) and v for v in image.values)
        elif isinstance(s, tpl.FlatStroke):
            counts["flat"] += 1
            assert f3 is None
            assert tpl.encode_flat(s) == raw
            assert len(s.polyline()) == 1 + 2 * len(s.quads) and len(s.polyline()) % 2 == 1
            assert s.flags == [0] + [1] * len(s.quads)
            assert s.trailer_word == 1 and s.dash == [] and s.version in (1, 2)
            assert s.width > 0
            s.cubic_controls()
        elif isinstance(s, tpl.RibbonStroke):
            counts["ribbon"] += 1
            assert pb.varint_value(f3) in (1, 4)
            assert len(s.points) == sum(len(p) for p in s.subpaths)
            assert all(r > 0 for _x, _y, r in s.points)
            assert len(s.arc_flags) * 5 <= len(s.arcs) <= len(s.arc_flags) * 7
        else:
            counts["pencil"] += 1
            assert pb.varint_value(f3) == 5 and pb.varint_value(pb.get(fields, 21)) == 25
            assert len(s.points) == len(s.attrs) == 1 + 2 * len(s.seeds) + len(s.subpaths) - 1
    assert counts == {"flat": 3972, "ribbon": 42, "pencil": 8, "empty": 1655}
