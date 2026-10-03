"""Curve helpers: polyline <-> cubic Bezier chains, simplification, flattening."""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from .model import Point

XY = Tuple[float, float]
MAX_FLATTEN_STEPS = 4096  # per Bezier segment; a 1000 pt segment at 2 pt spacing needs 500


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def dedupe(points: Sequence[Point], eps: float = 1e-3) -> List[Point]:
    """Drop consecutive points closer than ``eps`` (keeps the first of a run)."""
    out: List[Point] = []
    for p in points:
        if not out or abs(p.x - out[-1].x) > eps or abs(p.y - out[-1].y) > eps:
            out.append(p)
    return out


def simplify(points: Sequence[Point], tolerance: float) -> List[Point]:
    """Ramer-Douglas-Peucker on (x, y); widths of kept points are preserved."""
    if len(points) <= 2 or tolerance <= 0:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a].x, points[a].y
        bx, by = points[b].x, points[b].y
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        best, best_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i].x - ax, points[i].y - ay
            if seg_len2 == 0:
                d = math.hypot(px, py)
            else:
                t = max(0.0, min(1.0, (px * dx + py * dy) / seg_len2))
                d = math.hypot(px - t * dx, py - t * dy)
            if d > best:
                best, best_i = d, i
        if best > tolerance and best_i > 0:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [p for p, k in zip(points, keep) if k]


def polyline_to_bezier(points: Sequence[Point]) -> Tuple[List[Point], List[Tuple[Point, Point]]]:
    """Chord-length Catmull-Rom fit: anchors pass through every input point.

    The tangent at an anchor is the chord ``p2 - p0`` of its neighbours, scaled by the length
    of the segment the handle belongs to relative to the two adjacent segments (so a short
    segment after a long one gets a short handle instead of the hook or loop a uniform
    Catmull-Rom produces), and every handle is clamped to a third of its segment's chord.
    For evenly spaced points this is the classic ``(p2 - p0) / 6`` handle; the first and last
    handles lie on the chord, so a two-point stroke is an exact straight line.

    Returns ``(anchors, controls)`` with ``len(controls) == len(anchors) - 1``.
    A single point becomes a tiny two-anchor segment so the result is drawable.
    """
    pts = list(points)
    if len(pts) == 1:
        p = pts[0]
        pts = [p, Point(p.x + 0.5, p.y + 0.5, p.width)]
    controls: List[Tuple[Point, Point]] = []
    n = len(pts)
    seg = [math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y) for i in range(n - 1)]
    for i in range(n - 1):
        p1, p2 = pts[i], pts[i + 1]
        d1 = seg[i]
        limit = d1 / 3.0
        # handle at p1 (tangent from p0 to p2)
        if i > 0 and seg[i - 1] + d1 > 0:
            p0 = pts[i - 1]
            k = d1 / (3.0 * (seg[i - 1] + d1))
            hx, hy = (p2.x - p0.x) * k, (p2.y - p0.y) * k
        else:
            hx, hy = (p2.x - p1.x) / 3.0, (p2.y - p1.y) / 3.0
        hx, hy = _clamp_handle(hx, hy, limit)
        c1 = Point(p1.x + hx, p1.y + hy, p1.width)
        # handle at p2 (tangent from p1 to p3)
        if i + 2 < n and d1 + seg[i + 1] > 0:
            p3 = pts[i + 2]
            k = d1 / (3.0 * (d1 + seg[i + 1]))
            hx, hy = (p3.x - p1.x) * k, (p3.y - p1.y) * k
        else:
            hx, hy = (p2.x - p1.x) / 3.0, (p2.y - p1.y) / 3.0
        hx, hy = _clamp_handle(hx, hy, limit)
        c2 = Point(p2.x - hx, p2.y - hy, p2.width)
        controls.append((c1, c2))
    return pts, controls


def _clamp_handle(hx: float, hy: float, limit: float) -> XY:
    length = math.hypot(hx, hy)
    if length > limit and length > 0:
        f = limit / length
        return hx * f, hy * f
    return hx, hy


def _cubic(p0: XY, c1: XY, c2: XY, p1: XY, t: float) -> XY:
    mt = 1.0 - t
    a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
    return (a * p0[0] + b * c1[0] + c * c2[0] + d * p1[0],
            a * p0[1] + b * c1[1] + c * c2[1] + d * p1[1])


def flatten_bezier(anchors: Sequence[Point], controls: Sequence[Tuple[Point, Point]],
                   max_segment: float = 2.0) -> List[Point]:
    """Sample a Bezier chain into a polyline with roughly ``max_segment`` pt spacing.

    Widths are interpolated linearly between anchors.  A segment is never split into more
    than ``MAX_FLATTEN_STEPS`` samples.
    """
    if len(anchors) == 1 or not controls:
        return [Point(p.x, p.y, p.width) for p in anchors]
    out: List[Point] = [Point(anchors[0].x, anchors[0].y, anchors[0].width)]
    for i, (c1, c2) in enumerate(controls):
        p0, p1 = anchors[i], anchors[i + 1]
        chord = (math.hypot(c1.x - p0.x, c1.y - p0.y) + math.hypot(c2.x - c1.x, c2.y - c1.y)
                 + math.hypot(p1.x - c2.x, p1.y - c2.y))
        steps = max(1, min(MAX_FLATTEN_STEPS, int(math.ceil(chord / max_segment))))
        for s in range(1, steps + 1):
            t = s / steps
            x, y = _cubic((p0.x, p0.y), (c1.x, c1.y), (c2.x, c2.y), (p1.x, p1.y), t)
            out.append(Point(x, y, _lerp(p0.width, p1.width, t)))
    return out


def resample(points: Sequence[Point], spacing: float) -> List[Point]:
    """Resample a polyline to (approximately) uniform ``spacing`` along its length."""
    pts = dedupe(points)
    if len(pts) < 2 or spacing <= 0:
        return list(pts)
    out = [pts[0]]
    carry = 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b.x - a.x, b.y - a.y)
        if seg == 0:
            continue
        pos = spacing - carry
        while pos <= seg:
            t = pos / seg
            out.append(Point(_lerp(a.x, b.x, t), _lerp(a.y, b.y, t), _lerp(a.width, b.width, t)))
            pos += spacing
        carry = seg - (pos - spacing)
    if out[-1] is not pts[-1]:
        out.append(pts[-1])
    return out


def polyline_length(points: Sequence[Point]) -> float:
    return sum(math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(points, points[1:]))
