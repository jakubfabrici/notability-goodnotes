"""Tests for gnnote.geometry: the polyline -> Bezier fit and the flattener."""
from __future__ import annotations

import math
import random

import pytest

from gnnote import geometry
from gnnote.model import Point


def _h(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def test_uniform_spacing_is_the_classic_catmull_rom():
    pts = [Point(0, 0), Point(10, 5), Point(20, 0), Point(30, 5)]
    anchors, controls = geometry.polyline_to_bezier(pts)
    assert anchors == pts
    # interior handles: (p2 - p0) / 6 as before; end handles lie on the chord at a third
    c1, c2 = controls[1]
    assert (c1.x, c1.y) == pytest.approx((10 + 20 / 6, 5 + 0 / 6))
    assert (c2.x, c2.y) == pytest.approx((20 - 20 / 6, 0))
    first = controls[0][0]
    assert (first.x, first.y) == pytest.approx((10 / 3, 5 / 3))


def test_two_points_are_an_exact_straight_segment():
    anchors, controls = geometry.polyline_to_bezier([Point(0, 0, 1), Point(30, 60, 2)])
    (c1, c2), = controls
    assert (c1.x, c1.y) == pytest.approx((10, 20)) and (c2.x, c2.y) == pytest.approx((20, 40))
    assert c1.width == 1 and c2.width == 2
    for t in (0.25, 0.5, 0.75):
        x, y = geometry._cubic((0, 0), (c1.x, c1.y), (c2.x, c2.y), (30, 60), t)
        assert (x, y) == pytest.approx((30 * t, 60 * t))


def test_short_segment_after_a_long_one_does_not_loop():
    """Test5 p2 stroke 45: a 24 pt segment, then a 0.36 pt reversal; the uniform fit made a 1.8 pt loop."""
    pts = [Point(0, 0), Point(24.1, 0), Point(23.9, 0.3), Point(23.5, 0.6)]
    anchors, controls = geometry.polyline_to_bezier(pts)
    flat = geometry.flatten_bezier(anchors, controls, max_segment=0.05)
    assert max(p.x for p in flat) <= 24.1 + 0.15  # no hook past the turning point
    assert min(p.y for p in flat) >= -0.15
    for (p, q), (c1, c2) in zip(zip(anchors, anchors[1:]), controls):
        limit = _h(p, q) / 3 + 1e-9
        assert _h(c1, p) <= limit and _h(c2, q) <= limit


def test_handles_never_exceed_a_third_of_the_chord():
    rnd = random.Random(7)
    for _ in range(200):
        n = rnd.randint(2, 30)
        pts = [Point(rnd.uniform(0, 100), rnd.uniform(0, 100)) for _ in range(n)]
        if rnd.random() < 0.5:  # near-duplicate points, as pencil jitter produces
            pts.insert(rnd.randrange(n), Point(pts[0].x + 1e-3, pts[0].y))
        anchors, controls = geometry.polyline_to_bezier(pts)
        assert anchors == pts and len(controls) == len(pts) - 1
        for (p, q), (c1, c2) in zip(zip(anchors, anchors[1:]), controls):
            limit = _h(p, q) / 3 + 1e-9
            assert _h(c1, p) <= limit and _h(c2, q) <= limit


def test_single_point_becomes_a_drawable_dash():
    anchors, controls = geometry.polyline_to_bezier([Point(5, 5, 2)])
    assert len(anchors) == 2 and len(controls) == 1 and anchors[0].width == 2


def test_flatten_honours_the_spacing_on_long_segments():
    anchors, controls = geometry.polyline_to_bezier([Point(0, 0), Point(1000, 0)])
    flat = geometry.flatten_bezier(anchors, controls, max_segment=2.0)
    assert len(flat) >= 501  # 64-sample cap would have given 15.6 pt spacing
    assert max(_h(a, b) for a, b in zip(flat, flat[1:])) <= 2.0 + 1e-6
    assert (flat[-1].x, flat[-1].y) == (1000, 0)
