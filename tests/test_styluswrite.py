"""Stylus Labs Write codec (``gnnote.styluswrite``): the SVG path / transform parser, the reader
on synthetic documents and on pages Write saved (pinned, never committed), the writer's
skeleton and round trips, conversions to the other apps, hardening, and the registry, sniffing
and CLI plumbing."""
from __future__ import annotations

import base64
import gzip
import math
import random
import time
import tracemalloc
import zlib
from pathlib import Path
from typing import Any, Callable, List, Tuple
from xml.etree import ElementTree as ET

import pytest

from gnnote import formats
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from gnnote.notability.reader import read_note
from gnnote.pdf.reader import read_pdf
from gnnote.styluswrite import PT_PER_UNIT
from gnnote.styluswrite import reader as wr
from gnnote.styluswrite import svgpath
from gnnote.styluswrite.reader import parse_color, parse_write_color, read_write_svg
from gnnote.styluswrite.writer import build_svg, outline_vertices, write_write_svg
from tests.sample_docs import JPEG, full_document, png

U = PT_PER_UNIT  # pt per Write unit
TOL = 1e-6
SVG_NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'


def _doc(content: str, *, attrs: str = 'width="800" height="1000" xruling="0" yruling="40" marginLeft="0" '
         'papercolor="#FFFFFF" rulecolor="#FF0000FF"', ruleline: str = "", pages: int = 1) -> bytes:
    """A Write 3 document with ``pages`` copies of a page holding ``content``."""
    page = (f'<svg class="write-page" x="10" y="10" width="800px" height="1000px">'
            f'<g class="write-content write-v3" {attrs}>'
            f'<g class="ruleline write-std-ruling write-scale-down" fill="none" stroke="#0000FF">'
            f'<rect class="pagerect" fill="#FFFFFF" stroke="none" x="0" y="0" width="800" height="1000" />'
            f'{ruleline}</g>{content}</g></svg>')
    return (f'<svg id="write-document" {SVG_NS}><rect id="write-doc-background" width="100%" height="100%" '
            f'fill="#808080"/>' + page * pages + "</svg>").encode()


def _pen(d: str, width: float = 2.0, color: str = "#000000", extra: str = "") -> str:
    return (f'<path class="write-stroke-pen" fill="none" stroke="{color}" stroke-width="{width}" '
            f'stroke-linecap="round" stroke-linejoin="round"{extra} d="{d}" />')


def _xy(points: List[Point]) -> List[Tuple[float, float]]:
    return [(round(p.x, 6), round(p.y, 6)) for p in points]


def _close(a: float, b: float, tol: float = TOL) -> bool:
    return abs(a - b) <= tol


CUSTOM_BACKGROUND = ("1 pages have a background drawn by a custom template or an imported PDF page; it was not "
                     "converted (only the paper colour, and Write's standard ruling if the page uses it, are kept)")


CUSTOM_BACKGROUND_IMAGES = ("1 pages have a background drawn by a custom template or an imported PDF page; only "
                            "its images were converted (with the paper colour, and Write's standard ruling if the "
                            "page uses it)")
USE_SKIPPED = ("reused shapes (<use> elements) that refer to a missing, external, non-drawing or circular target were "
               "skipped")


def _png_uri(width: int = 6, height: int = 4) -> str:
    return "data:image/png;base64," + base64.b64encode(png(width, height)).decode()


def _peak(call: Callable[[], Any]) -> Tuple[Any, int]:
    """``call()``'s result and the peak of the Python memory it allocated."""
    tracemalloc.start()
    try:
        result = call()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# --------------------------------------------------------------------------- path data


def test_number_grammar_and_absolute_relative_commands() -> None:
    path = svgpath.parse_path("M10,20L30 40l1e1-5h-.5V0.5.5zm1 1l0 0")
    assert path.error is None and len(path.subpaths) == 2
    first, second = path.subpaths
    assert first.points == [(10, 20), (30, 40), (40, 35), (39.5, 35), (39.5, 0.5), (39.5, 0.5)]
    assert first.closed and not first.curved
    assert second.points == [(11, 21), (11, 21)]  # a relative moveto after Z starts at the subpath start
    # extra pairs after a moveto are linetos (relative after m), exponents with signs
    assert svgpath.parse_path("m1 1 2 2 3e+0 3E-0").subpaths[0].points == [(1, 1), (3, 3), (6, 6)]
    assert svgpath.parse_path("M-1-2 -.5-.5").subpaths[0].points == [(-1, -2), (-0.5, -0.5)]


def test_drawing_after_closepath_continues_from_the_subpath_start() -> None:
    path = svgpath.parse_path("M5 5 L10 5 Z l0 10")
    assert [s.points for s in path.subpaths] == [[(5, 5), (10, 5)], [(5, 5), (5, 15)]]
    assert [s.closed for s in path.subpaths] == [True, False]


def test_curves_are_flattened_within_the_tolerance() -> None:
    p0, p1, p2, p3 = (0, 0), (0, 100), (100, 100), (100, 0)
    path = svgpath.parse_path("M0 0 C0 100 100 100 100 0", tolerance=0.05)
    pts = path.subpaths[0].points
    assert path.subpaths[0].curved and pts[0] == (0, 0) and pts[-1] == (100, 0) and len(pts) > 20

    def bezier(t: float) -> Tuple[float, float]:
        u = 1 - t
        return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])

    curve = [bezier(k / 8000) for k in range(8001)]  # about 0.03 apart
    for a, b in zip(pts, pts[1:]):  # every chord stays within the tolerance of the curve
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        assert min(math.dist(mid, s) for s in curve) < 0.05 + 0.02
    # S reflects the previous control point, T the previous quadratic control point
    s_path = svgpath.parse_path("M0 0 C0 10 10 10 10 0 S20 -10 20 0").subpaths[0].points
    c_path = svgpath.parse_path("M0 0 C0 10 10 10 10 0 C10 -10 20 -10 20 0").subpaths[0].points
    assert s_path == c_path
    t_path = svgpath.parse_path("M0 0 Q5 10 10 0 T20 0").subpaths[0].points
    q_path = svgpath.parse_path("M0 0 Q5 10 10 0 Q15 -10 20 0").subpaths[0].points
    assert t_path == q_path
    assert svgpath.parse_path("m0 0 q5 10 10 0").subpaths[0].points[-1] == (10, 0)


def test_arcs_including_packed_flags() -> None:
    for d in ("M0 0 A10 10 0 1 1 20 0", "M0 0 a10 10 0 1120 0", "M0,0a10,10,0,1,1,20,0"):
        path = svgpath.parse_path(d, tolerance=0.01)
        assert path.error is None, d
        pts = path.subpaths[0].points
        assert pts[-1] == (20, 0) and len(pts) > 10
        assert all(abs(math.dist(p, (10, 0)) - 10) < 1e-9 for p in pts)
        assert all(p[1] <= 1e-9 for p in pts)  # sweep 1 from (0,0) to (20,0) goes through y = -10
    assert min(p[1] for p in svgpath.parse_path("M0 0 A10 10 0 1 1 20 0").subpaths[0].points) < -9.9
    # radii too small are scaled up; zero radii make a straight line
    small = svgpath.parse_path("M0 0 A1 1 0 0 1 20 0").subpaths[0].points
    assert all(abs(math.dist(p, (10, 0)) - 10) < 1e-9 for p in small)
    assert svgpath.parse_path("M0 0 A0 5 0 0 1 20 0").subpaths[0].points == [(0, 0), (20, 0)]
    assert svgpath.parse_path("M0 0 A5 5 0 0 1 0 0").subpaths == []  # omitted: a lone moveto is left
    assert svgpath.parse_path("M0 0 A5 5 0 0 1 0 0 Z").subpaths[0].points == [(0, 0)]


def test_path_errors_keep_what_came_before() -> None:
    path = svgpath.parse_path("M0 0 L10 10 L20 X 30")
    assert path.error and [s.points for s in path.subpaths] == [[(0, 0), (10, 10)]]
    assert svgpath.parse_path("L10 10").error == "path data does not start with a moveto command"
    assert svgpath.parse_path("10 10").error == "path data does not start with a moveto command"
    assert svgpath.parse_path("M0 0 Z 5 5").error == "numbers after a closepath command"
    assert svgpath.parse_path("M0 0 C1 1 2").error is not None
    assert svgpath.parse_path("M0 0 A1 1 0 2 1 5 5").error is not None  # a flag must be 0 or 1
    assert svgpath.parse_path("M1e 5").error is not None
    assert svgpath.parse_path("").subpaths == [] and svgpath.parse_path(None).subpaths == []


def test_path_point_budget_and_curve_segment_cap() -> None:
    long = "M0 0 l" + " 1 1" * 1000
    path = svgpath.parse_path(long, max_points=100)
    assert path.truncated and path.point_count == 100
    huge = svgpath.parse_path("M0 0 C0 1e9 1e9 1e9 1e9 0", tolerance=1e-9)
    assert len(huge.subpaths[0].points) == svgpath.MAX_CURVE_SEGMENTS + 1
    arc = svgpath.parse_path("M0 0 A1e9 1e9 0 1 1 1 0", tolerance=1e-9)
    assert len(arc.subpaths[0].points) <= svgpath.MAX_CURVE_SEGMENTS + 1
    # commands that add no point are bounded too, and long separator runs cost no Python loop
    assert svgpath.parse_path("M0 0" + " Z" * 100_000, max_points=100).truncated
    assert svgpath.parse_path("M0 0" + " A5 5 0 0 1 0 0" * 10_000, max_points=100).truncated
    assert svgpath.parse_path("M0 0" + " ," * 1_000_000 + "l1 1").subpaths[0].points == [(0, 0), (1, 1)]
    assert svgpath.parse_transform(" " * 1_000_000 + "scale(2)") == (2, 0, 0, 2, 0, 0)


def test_closepath_and_omitted_arcs_cost_budget_units() -> None:
    # every command costs at least one unit, so a file-wide budget bounds the loop work
    path = svgpath.parse_path("M0 0" + "z" * 1000, max_points=100)
    assert path.truncated and path.cost == 100 and path.point_count == 1
    path = svgpath.parse_path("M0 0 l1 1 z")
    assert not path.truncated and path.cost == 3 and path.point_count == 2
    assert svgpath.parse_path("M0 0 A5 5 0 0 1 0 0").cost == 2


def test_extreme_arc_radii_and_curves_never_raise() -> None:
    # radii whose squares underflow are scaled up to fit, as SVG asks: a half circle on the chord
    for r in ("1e-200", "1e-310", "4e-162"):
        path = svgpath.parse_path(f"M0 0 A{r} {r} 0 0 1 10 0", tolerance=0.01)
        pts = path.subpaths[0].points
        assert pts[-1] == (10, 0) and len(pts) > 3
        assert max(abs(math.hypot(x - 5, y) - 5) for x, y in pts) < 1e-9
    # an extremely flat ellipse, zero, infinite and overflowing radii: a straight line
    for d in ("M100 100 A1e-200 50 0 0 1 200 100", "M0 0 A0 5 0 0 1 10 0", "M0 0 A1e999 5 0 0 1 10 0",
              "M0 0 A5 5 1e999 0 1 10 0", "M0 0 A1e300 1e300 0 0 1 10 0", "M0 0 A5 5 0 0 1 1e-300 0"):
        pts = svgpath.parse_path(d).subpaths[0].points
        assert len(pts) == 2, d
    # huge radii with the large-arc flag still go round; non-finite results are left to the caller
    assert len(svgpath.parse_path("M0 0 A1e300 1e300 0 1 1 10 0").subpaths[0].points) > 2
    assert svgpath.parse_path("M0 0 A1e-310 1e-310 0 0 1 1e10 0").subpaths[0].points[-1] == (1e10, 0)
    # a curve whose bend overflows to infinity gets the segment cap instead of an OverflowError
    big = svgpath.parse_path("M0 0 C1e304 0 -1e304 0 1e304 0", tolerance=1e-6)
    assert len(big.subpaths[0].points) == svgpath.MAX_CURVE_SEGMENTS + 1
    # ordinary arcs still match the SVG centre parametrisation
    pts = svgpath.parse_path("M0 0 A30 20 30 0 1 40 10", tolerance=0.01).subpaths[0].points
    phi = math.radians(30)
    centre = _arc_centre((0, 0), 30, 20, 30, False, True, (40, 10))
    for x, y in pts:
        u, v = x - centre[0], y - centre[1]
        ex, ey = (math.cos(phi) * u + math.sin(phi) * v) / 30, (-math.sin(phi) * u + math.cos(phi) * v) / 20
        assert abs(math.hypot(ex, ey) - 1) < 1e-9


def _arc_centre(p0: Tuple[float, float], rx: float, ry: float, phi_deg: float, large: bool, sweep: bool,
                p1: Tuple[float, float]) -> Tuple[float, float]:
    """The centre of an SVG arc whose radii need no scaling (SVG 1.1 F.6.5, computed directly)."""
    phi = math.radians(phi_deg)
    c, s = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = c * dx + s * dy, -s * dx + c * dy
    coef = math.sqrt((rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1) / (rx * rx * y1 * y1 + ry * ry * x1 * x1))
    coef = -coef if large == sweep else coef
    cx1, cy1 = coef * rx * y1 / ry, -coef * ry * x1 / rx
    return c * cx1 - s * cy1 + (p0[0] + p1[0]) / 2, s * cx1 + c * cy1 + (p0[1] + p1[1]) / 2


def test_transform_lists() -> None:
    assert svgpath.parse_transform(None) == svgpath.IDENTITY
    assert svgpath.parse_transform("  ") == svgpath.IDENTITY
    m = svgpath.parse_transform("translate(10,-80) scale(2)")
    assert svgpath.apply(m, 1, 1) == (12, -78)
    m = svgpath.parse_transform("matrix(0.534 0 0 0.534 -160.416 -1226.1)")
    assert all(_close(a, b) for a, b in zip(svgpath.apply(m, 100, 100), (-107.016, -1172.7)))
    m = svgpath.parse_transform("rotate(90 10 10)")
    assert all(_close(a, b) for a, b in zip(svgpath.apply(m, 20, 10), (10, 20)))
    m = svgpath.parse_transform("skewX(45)")
    assert all(_close(a, b) for a, b in zip(svgpath.apply(m, 0, 10), (10, 10)))
    m = svgpath.parse_transform("scale(2 3),translate(1)")
    assert svgpath.apply(m, 0, 0) == (2, 0) and _close(svgpath.scale_of(m), math.sqrt(6))
    for bad in ("translate(1 2 3)", "spin(4)", "translate(1", "matrix(1 0 0 1 0 nan)", "rotate(1e400)",
                "scale(1) junk", "translate(1)" * (svgpath.MAX_TRANSFORMS + 1)):
        assert svgpath.parse_transform(bad) is None, bad


def test_number_lists() -> None:
    assert svgpath.parse_numbers("0 0, 900 1e2") == [0, 0, 900, 100]
    assert svgpath.parse_numbers("1 2 x") is None
    assert svgpath.parse_numbers("1 2 3", limit=2) is None


# --------------------------------------------------------------------------- colours


def test_colours() -> None:
    assert parse_color("#0000FF") == (0, 0, 1, 1)
    assert parse_color("#f00") == (1, 0, 0, 1)
    assert parse_color("#FF000080") == (1, 0, 0, 128 / 255)  # CSS: alpha last
    assert parse_color("rgb(255, 0, 0)") == (1, 0, 0, 1)
    assert parse_color("rgba(0 0 255 / 0.5)") == (0, 0, 1, 0.5)
    assert parse_color("rgb(100%,0%,0%)") == (1, 0, 0, 1)
    assert parse_color("Navy") == (0, 0, 128 / 255, 1)
    assert parse_color("transparent")[3] == 0
    for bad in ("#12345", "nope", "rgb(1,2)", "rgb(a,b,c)", "", None):
        assert parse_color(bad) is None
    # Write's own papercolor / rulecolor: alpha first
    assert parse_write_color("#7F0000FF") == (0, 0, 1, 127 / 255)
    assert parse_write_color("#FFFFFF") == (1, 1, 1, 1)
    assert parse_write_color("blue") is None


# --------------------------------------------------------------------------- reader: synthetic


def test_pen_strokes_transforms_and_units() -> None:
    content = (_pen("M100 50 l10 0 0 10", 2.5) +
               _pen("M0 0 l20 0", 1.5, "#FF0000", ' transform="translate(5,-10)"') +
               '<g transform="scale(2)"><g transform="translate(10 10)">' + _pen("M0 0 L5 5", 1) + "</g></g>")
    doc = read_write_svg(_doc(content))
    page = doc.pages[0]
    assert (page.width, page.height) == (800 * U, 1000 * U) and page.paper == "lined"
    a, b, c = page.strokes
    assert _xy(a.points) == _xy([Point(100 * U, 50 * U), Point(110 * U, 50 * U), Point(110 * U, 60 * U)])
    assert a.kind == "pen" and a.pen == "ballpoint" and _close(a.width, 2.5 * U)
    assert all(_close(p.width, 2.5 * U) for p in a.points) and a.color == (0, 0, 0, 1)
    assert _xy(b.points) == _xy([Point(5 * U, -10 * U), Point(25 * U, -10 * U)]) and b.color == (1, 0, 0, 1)
    assert _xy(c.points) == _xy([Point(20 * U, 20 * U), Point(30 * U, 30 * U)]) and _close(c.width, 2 * U)
    assert doc.title == "" and doc.warnings == []


def test_flat_pen_outline_becomes_centre_line_with_widths() -> None:
    centre = [Point(100, 100, 2.0), Point(120, 103, 3.0), Point(140, 110, 1.0), Point(150, 130, 0.5)]
    outline = outline_vertices(centre)
    assert len(outline) == 8
    d = "M{} {} l".format(*outline[0]) + " ".join(
        f"{b[0] - a[0]!r} {b[1] - a[1]!r}" for a, b in zip(outline, outline[1:]))
    doc = read_write_svg(_doc(f'<path class="write-flat-pen" fill="#0000FF" stroke-width="2.4" d="{d}" />'))
    (stroke,) = doc.pages[0].strokes
    assert stroke.kind == "pen" and stroke.pen == "fountain" and stroke.color == (0, 0, 1, 1)
    assert _close(stroke.width, 2.4 * U)
    assert len(stroke.points) == 4
    for got, want in zip(stroke.points, centre):
        assert _close(got.x, want.x * U, 1e-9) and _close(got.y, want.y * U, 1e-9)
        assert _close(got.width, want.width * U, 1e-9)


def test_flat_pen_without_paired_outline_stays_a_fill() -> None:
    odd = '<path class="write-flat-pen" fill="#000000" stroke-width="2" d="M0 0 l10 0 0 10" />'
    curved = '<path class="write-flat-pen" fill="#000000" stroke-width="2" d="M0 0 c5 5 10 5 10 0 l0 5 -10 0" />'
    doc = read_write_svg(_doc(odd + curved))
    assert [s.kind for s in doc.pages[0].strokes] == ["fill", "fill"]
    assert doc.pages[0].strokes[0].outline == [doc.pages[0].strokes[0].points]
    assert "2 variable-width strokes did not have Write's paired outline and were kept as filled shapes" \
        in doc.warnings


def test_highlighters_links_bookmarks_and_hidden_content() -> None:
    content = (_pen("M0 0 l50 0", 12, "#FFFF00", ' stroke-opacity="0.5"') +
               _pen("M0 20 l50 0", 12, "#FFFF00", ' opacity="0.8"') +
               '<path class="write-stroke-highlight" fill="none" stroke="#00FF00" stroke-width="10" d="M0 40 l9 0"/>' +
               '<path fill="none" stroke="#00FF00" stroke-width="10" style="mix-blend-mode: multiply" d="M0 60 l9 0"/>'
               '<g class="hyperref"><path fill="none" stroke="#0000FF" stroke-width="1.2" d="M0 80 l5 5"/>'
               '<a xlink:href="https://example.com/" target="_top"><rect fill-opacity="0" stroke-opacity="0" '
               'x="0" y="80" width="5" height="5" /></a></g>'
               '<a xlink:href="#b-x"><path fill="none" stroke="#000" d="M0 100 l5 0"/></a>'
               '<path class="bookmark" transform="translate(76,245)" fill="#0000FF" d="M0 0 l16 0 0 30 -8 -5 -8 5 0 -30"/>'
               '<g display="none">' + _pen("M0 0 l1 1") + "</g>" +
               '<path style="display:none" fill="none" stroke="#000" d="M0 0 l1 1"/>' +
               '<path visibility="hidden" fill="none" stroke="#000" d="M0 0 l1 1"/>' +
               '<path fill="none" stroke="none" d="M0 0 l1 1"/>' +
               '<path fill="none" stroke="#000" stroke-width="0" d="M0 0 l1 1"/>')
    doc = read_write_svg(_doc(content))
    strokes = doc.pages[0].strokes
    assert [s.kind for s in strokes] == ["highlighter", "pen", "highlighter", "highlighter", "pen", "pen"]
    assert strokes[0].color == (1, 1, 0, 0.5) and strokes[1].color == (1, 1, 0, 0.8)
    assert strokes[4].color == (0, 0, 1, 1) and strokes[5].width == pytest.approx(U)  # default width 1
    assert "2 hand-drawn links were converted as plain ink (their link targets are dropped)" in doc.warnings
    assert "1 bookmarks were dropped" in doc.warnings


def test_foreign_shapes_become_fills_and_strokes() -> None:
    content = ('<g class="icon" transform="matrix(0.5 0 0 0.5 100 100)">'
               '<rect x="0" y="0" width="20" height="10" />'
               '<circle cx="50" cy="50" r="10" fill="#FF0000" fill-opacity="0.5" stroke="#000" stroke-width="2"/>'
               '<path fill-rule="evenodd" d="M0 0 h10 v10 h-10 z M2 2 h6 v6 h-6 z" fill="currentColor" color="#00F"/>'
               '<ellipse cx="0" cy="0" rx="5" ry="2" fill="none" stroke="#000"/>'
               '<line x1="0" y1="0" x2="10" y2="0" stroke="#000"/>'
               '<polyline points="0,0 5,5 10,0" fill="none" stroke="#000"/>'
               '<polygon points="0,0 5,5 10,0" fill="#0F0"/>'
               '<rect x="0" y="0" width="10" height="10" rx="2" fill="#000"/>'
               "</g>")
    doc = read_write_svg(_doc(content))
    kinds = [s.kind for s in doc.pages[0].strokes]
    assert kinds == ["fill", "fill", "pen", "fill", "pen", "pen", "pen", "fill", "fill"]
    rect, circle_fill, circle_ring, holes = doc.pages[0].strokes[:4]
    assert _xy(rect.points) == _xy([Point(100 * U, 100 * U), Point(110 * U, 100 * U), Point(110 * U, 105 * U),
                                    Point(100 * U, 105 * U)])
    assert rect.color == (0, 0, 0, 1) and circle_fill.color == (1, 0, 0, 0.5)
    assert circle_ring.points[0].x == circle_ring.points[-1].x and _close(circle_ring.width, U)
    assert all(abs(math.dist((p.x, p.y), (125 * U, 125 * U)) - 5 * U) < 0.05 for p in circle_ring.points)
    assert len(holes.outline) == 2 and holes.color == (0, 0, 1, 1)


def test_images_with_aspect_rotation_and_problems() -> None:
    uri = _png_uri(6, 4)
    jpeg = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    gif = "data:image/gif;base64," + base64.b64encode(b"GIF89a" + b"\0" * 20).decode()
    content = (f'<image x="10" y="20" width="60" height="40" xlink:href="{uri}" />'
               f'<image x="0" y="0" width="60" height="60" xlink:href="{uri}" />'  # meet: 60 x 40, centred
               f'<image x="0" y="0" width="60" height="60" preserveAspectRatio="none" href="{uri}" />'
               f'<image transform="rotate(90 50 50)" x="20" y="30" width="60" height="40" '
               f'preserveAspectRatio="xMinYMin meet" xlink:href="{jpeg}" />'
               f'<image x="0" y="0" width="6" height="4" xlink:href="{gif}" />'
               '<image x="0" y="0" width="6" height="4" xlink:href="photo.png" />'
               '<image x="0" y="0" width="6" height="4" xlink:href="data:image/png;base64,@@@" />'
               '<image x="0" y="0" width="6" height="4" xlink:href="data:image/svg+xml,%3Csvg/%3E" />')
    doc = read_write_svg(_doc(content))
    images = doc.pages[0].images
    assert len(images) == 4
    box = [(round(i.x / U, 6), round(i.y / U, 6), round(i.w / U, 6), round(i.h / U, 6), i.rotation, i.fmt)
           for i in images]
    assert box[0] == (10, 20, 60, 40, 0.0, "png")
    assert box[1] == (0, 10, 60, 40, 0.0, "png")
    assert box[2] == (0, 0, 60, 60, 0.0, "png")
    assert box[3][4] == pytest.approx(90.0) and box[3][5] == "jpeg"
    assert images[0].data == png(6, 4)
    for message in ("1 images are linked to files outside the document and were skipped",
                    "2 images are neither PNG nor JPEG and were skipped",
                    "1 images without readable data were skipped"):
        assert message in doc.warnings, message


def test_text_elements_become_text_boxes() -> None:
    content = ('<text x="100" y="200" font-size="20" font-family="\'Comic Neue\', sans-serif" fill="#FF0000">'
               'Hello <tspan font-weight="bold">bold</tspan> world'
               '<tspan x="100" dy="24" font-style="italic">second line</tspan></text>'
               '<text x="300" y="400" text-anchor="middle" transform="rotate(30 300 400)">Centred</text>'
               '<text x="0" y="0">   </text>')
    doc = read_write_svg(_doc(content))
    first, second = doc.pages[0].texts
    assert first.text == "Hello bold world\nsecond line"
    assert [(r.text, r.bold, r.italic) for r in first.runs] == [
        ("Hello ", False, False), ("bold", True, False), (" world", False, False), ("\n", False, False),
        ("second line", False, True)]
    assert first.runs[0].font == "Comic Neue" and first.color == (1, 0, 0, 1)
    assert _close(first.size, 20 * U) and _close(first.x, 100 * U)
    assert _close(first.y, (200 - wr.TEXT_ASCENT * 20) * U)
    assert second.align == "center" and second.rotation == pytest.approx(30.0)
    assert "2 SVG texts became text boxes; Write cannot edit them and their size is estimated" in doc.warnings
    assert "1 empty texts were skipped" in doc.warnings


def test_text_lines_follow_the_baselines() -> None:
    def text(body: str, attrs: str = 'x="10" y="100" font-size="20"') -> TextBox:
        doc = read_write_svg(_doc(f"<text {attrs}>{body}</text>"))
        (box,) = doc.pages[0].texts
        return box

    line = wr.TEXT_LINE * 20
    # absolute baselines: a gap of two lines is an empty line, of three two empty lines
    box = text(f'<tspan x="10" y="100">a</tspan><tspan x="10" y="{100 + line}">b</tspan>'
               f'<tspan x="10" y="{100 + 3 * line}">c</tspan><tspan x="10" y="{100 + 6 * line}">d</tspan>')
    assert box.text == "a\nb\n\nc\n\n\nd"
    # relative ones, and an empty tspan has no characters, so its dy is ignored (as browsers do)
    assert text(f'a<tspan x="10" dy="{line}"></tspan><tspan x="10" dy="{line}">b</tspan>').text == "a\nb"
    assert text(f'a<tspan x="10" dy="{2 * line}">b</tspan>').text == "a\n\nb"
    assert text('a<tspan x="10" dy="2.33em">b</tspan>').text == "a\n\nb"
    # super- and subscripts are baseline shifts, not lines
    assert text('E = mc<tspan dy="-8">2</tspan><tspan dy="8"> !</tspan>').text == "E = mc2 !"
    # evenly spaced lines wider apart than the font's line height are spacing, not empty lines
    box = text("".join(f'<tspan x="10" y="{100 + k * 50}">l{k}</tspan>' for k in range(4)))
    assert box.text == "l0\nl1\nl2\nl3"
    # tighter spacing sets the line height: a doubled gap is an empty line
    box = text("".join(f'<tspan x="10" y="{100 + y}">l{k}</tspan>' for k, y in enumerate((0, 16, 32, 64))))
    assert box.text == "l0\nl1\nl2\n\nl3"
    # lines going up start new lines without empty ones
    assert text('<tspan x="10" y="200">low</tspan><tspan x="10" y="100">high</tspan>').text == "low\nhigh"
    # Inkscape style: the text's own y is not where the first line is
    box = text('<tspan x="30" y="300">first</tspan><tspan x="30" y="324">second</tspan>', 'x="0" y="0" font-size="20"')
    assert box.text == "first\nsecond"
    assert (box.x, box.y) == pytest.approx((30 * U, (300 - wr.TEXT_ASCENT * 20) * U))
    # a hostile jump adds a bounded number of empty lines
    assert text('a<tspan x="10" y="1e300">b</tspan>').text.count("\n") == wr.MAX_BLANK_LINES + 1


def test_paper_from_page_setup() -> None:
    def page(attrs: str, ruleline: str = "") -> Page:
        doc = read_write_svg(_doc("", attrs=attrs, ruleline=ruleline))
        return doc.pages[0]

    lined = page('xruling="0" yruling="40" papercolor="#FFFFFF"')
    assert lined.paper == "lined" and lined.template_is_builtin and lined.background is not None
    grid = page('xruling="25" yruling="25"')
    assert grid.paper == "grid"
    plain = page('xruling="0" yruling="0" papercolor="#FFFFFF"')
    assert plain.paper == "plain" and plain.background is None
    coloured = page('xruling="0" yruling="0" papercolor="#FFF0C0"')
    assert coloured.paper == "plain" and coloured.background is not None and coloured.template_is_builtin
    # fully transparent rules are no ruling
    assert page('xruling="0" yruling="40" rulecolor="#000000FF"').paper == "plain"
    # a custom ruling drawn with dots (the way Write's templates draw custom backgrounds)
    doc = read_write_svg(_custom(
        '<pattern id="p" x="0" y="0" width="25" height="25" patternUnits="userSpaceOnUse">'
        '<circle fill="currentColor" cx="12.5" cy="12.5" r="1.5"/></pattern>'
        '<rect fill="url(#p)" width="800" height="1000"/>'))
    assert doc.pages[0].paper == "dotted" and doc.warnings == []
    # a custom ruling holding nothing but the page rectangle is plain paper, whatever the
    # Page Setup values say (Write draws what g.ruleline holds)
    doc = read_write_svg(_custom(""))
    assert doc.pages[0].paper == "plain" and doc.pages[0].background is None and doc.warnings == []
    # a pattern without a size draws nothing
    doc = read_write_svg(_custom('<pattern id="p"><circle cx="1" cy="1" r="1"/></pattern>'
                                 '<rect fill="url(#p)" width="800" height="1000"/>'))
    assert doc.pages[0].paper == "plain" and doc.warnings == []


def _custom(ruleline: str, attrs: str = 'width="800" height="1000" xruling="25" yruling="25" marginLeft="0" '
            'papercolor="#FFFFFF" rulecolor="#FF0000FF"') -> bytes:
    """A page whose ``g.ruleline`` is a custom ruling (no ``write-std-ruling``) holding the page
    rectangle and ``ruleline``; the ruling colour is set as ``color``, as Write does."""
    return _doc("", attrs=attrs).replace(
        b'class="ruleline write-std-ruling write-scale-down"', b'class="ruleline write-scale-down" color="#336699"'
    ).replace(b'height="1000" /></g>', b'height="1000" />' + ruleline.encode() + b"</g>")


def _paper_ops(pdf: bytes) -> List[str]:
    """The content stream lines of a generated paper PDF."""
    stream = pdf[pdf.index(b"stream\n") + 7:pdf.index(b"\nendstream")]
    return zlib.decompress(stream).decode("ascii").splitlines()


def _rects(ops: List[str], color: str) -> List[Tuple[float, ...]]:
    """``x y w h`` of the rectangles filled after the ``color rg`` operator (until ``Q``)."""
    out: List[Tuple[float, ...]] = []
    active = False
    for line in ops:
        if line.startswith("q ") and line.endswith(" rg") or line.startswith("q ") and " rg " in line:
            active = line.split(" rg")[0][2:] == color
        if active and line.endswith(" re f") or active and line.endswith(" re f Q"):
            nums = line.split(" re f")[0].split()[-4:]
            out.append(tuple(float(v) for v in nums))
        if line == "Q":
            active = False
    return out


def test_standard_ruling_paper_has_write_colours_pitches_and_margin() -> None:
    doc = read_write_svg(_doc("", attrs='width="800" height="1000" xruling="30" yruling="40" marginLeft="100" '
                                        'papercolor="#FFF0C0" rulecolor="#7F0000FF"'))
    page = doc.pages[0]
    assert page.paper == "grid" and page.template_is_builtin and doc.warnings == []
    ops = _paper_ops(doc.pdfs[page.background.pdf_id])
    paper = (1.0, 0xF0 / 255, 0xC0 / 255)
    alpha = 0x7F / 255
    rule = tuple(c * alpha + p * (1 - alpha) for c, p in zip((0.0, 0.0, 1.0), paper))
    margin = tuple(c * alpha + p * (1 - alpha) for c, p in zip((1.0, 0.0, 0.0), paper))
    fmt = " ".join
    assert ops[0] == f"q {fmt(f'{c:.3f}'.rstrip('0').rstrip('.') for c in paper)} rg 0 0 384 480 re f Q"
    rules = _rects(ops, fmt(f"{c:.3f}".rstrip("0").rstrip(".") for c in rule))
    horizontal = sorted(480 - (y + h / 2) for x, y, w, h in rules if w > h)
    vertical = sorted(x + w / 2 for x, y, w, h in rules if h > w)
    assert horizontal == pytest.approx([k * 40 * U for k in range(1, 25)], abs=2e-3)  # every 40 units from the top
    assert vertical == pytest.approx([k * 30 * U for k in range(1, 27)], abs=2e-3)  # every 30 units from the left
    assert all(min(w, h) == pytest.approx(U, abs=1e-3) for x, y, w, h in rules)
    (red,) = _rects(ops, fmt(f"{c:.3f}".rstrip("0").rstrip(".") for c in margin))
    assert red[0] + red[2] / 2 == pytest.approx(100 * U, abs=2e-3) and red[3] == pytest.approx(480)
    # Write 2 pages (no g.ruleline) draw the same ruling from the attributes
    html = ('<html><body><svg width="768px" height="1000px"><g id="page_1" width="768" height="1000" xruling="0" '
            'yruling="40" marginLeft="100" papercolor="#FFFFFF" rulecolor="#FF0000FF">'
            '<path class="ruleline" stroke="#0000FF" d="M0 40 l768 0"/></g></svg></body></html>').encode()
    doc = read_write_svg(html)
    ops = _paper_ops(doc.pdfs[doc.pages[0].background.pdf_id])
    assert len(_rects(ops, "0 0 1")) == 24 and len(_rects(ops, "1 0 0")) == 1


def test_custom_template_backgrounds_are_reported_not_guessed() -> None:
    # the example of the styluslabs/templates README: groups of five short lines from a pattern
    readme = ('<pattern id="ruling1" x="0" y="50" width="768" height="100" patternUnits="userSpaceOnUse">'
              + "".join(f'<path fill="none" stroke="currentColor" stroke-width="1" d="M68 {y} l632 0" />'
                        for y in (10, 20, 30, 40, 50)) +
              '</pattern><rect fill="url(#ruling1)" x="0" y="50" width="768" height="4096" />')
    attrs = 'width="800" height="1000" xruling="0" yruling="10" marginLeft="0" papercolor="#FFFFFF" rulecolor="#FF000000"'
    doc = read_write_svg(_custom(readme, attrs) + b"")
    page = doc.pages[0]
    assert page.paper == "plain" and page.background is None and doc.warnings == [CUSTOM_BACKGROUND]
    # the paper colour of a custom template is kept
    doc = read_write_svg(_custom(readme, attrs.replace("#FFFFFF", "#FFF0C0")))
    ops = _paper_ops(doc.pdfs[doc.pages[0].background.pdf_id])
    assert len(ops) == 1 and doc.pages[0].paper == "plain"
    # explicit straight lines and other drawings in a custom ruling are custom too
    for ruleline in ('<path stroke="#000" d="M0 40 l800 0"/>', '<rect fill="#F00" width="5" height="5"/>',
                     '<rect fill="url(#missing)" width="5" height="5"/>'):
        doc = read_write_svg(_custom(ruleline))
        assert doc.pages[0].paper == "plain" and doc.warnings == [CUSTOM_BACKGROUND], ruleline
    # a dot pattern drawn on top of the standard ruling is reported as well
    doc = read_write_svg(_doc("", ruleline='<pattern id="p" width="25" height="25" patternUnits="userSpaceOnUse">'
                                           '<circle cx="5" cy="5" r="1"/></pattern><rect fill="url(#p)" width="800" '
                                           'height="1000"/>'))
    assert doc.pages[0].paper == "lined" and doc.warnings == [CUSTOM_BACKGROUND]


def test_dot_rulings_are_drawn_where_the_dots_are() -> None:
    # a pattern tile of 25 units starting at 12.5 with its dot in the middle: dots every 25 units
    dots = ('<pattern id="d" x="12.5" y="12.5" width="25" height="25" patternUnits="userSpaceOnUse">'
            '<circle fill="currentColor" cx="12.5" cy="12.5" r="1.5"/></pattern>'
            '<rect fill="url(#d)" x="0" y="0" width="800" height="1000"/>')
    doc = read_write_svg(_custom(dots))
    page = doc.pages[0]
    assert page.paper == "dotted" and doc.warnings == []
    ops = _paper_ops(doc.pdfs[page.background.pdf_id])
    circles = [line for line in ops if line.endswith(" c h f")]
    # centres 0, 25, ..., 800 across and 0, ..., 1000 down: the tiles before the first one put
    # half dots on the rectangle's edges, as SVG renders them
    assert len(circles) == 33 * 41
    first, second = circles[0].split(), circles[1].split()
    r = (1.5 + 0.5) * U  # the stroke the ruling group passes on widens the dots
    assert (float(first[0]) - r, 480 - float(first[1])) == pytest.approx((0, 0), abs=2e-3)
    assert (float(second[0]) - r, 480 - float(second[1])) == pytest.approx((25 * U, 0), abs=2e-3)
    assert "0.2 0.4 0.6 rg" in "\n".join(ops)  # currentColor: the ruling colour (#336699)
    # objectBoundingBox tiles and a rectangle covering part of the page
    part = ('<pattern id="d" width="0.1" height="0.25"><circle cx="5" cy="5" r="2"/></pattern>'
            '<rect fill="url(#d)" x="100" y="100" width="200" height="400"/>')
    doc = read_write_svg(_custom(part))
    circles = [line for line in _paper_ops(doc.pdfs[doc.pages[0].background.pdf_id]) if line.endswith(" c h f")]
    assert len(circles) == 10 * 4
    # circles drawn one by one, some stroked (the stroke widens the dot)
    single = '<circle cx="100" cy="100" r="2"/><circle cx="200" cy="100" r="2" fill="none" stroke="#000" />'
    doc = read_write_svg(_custom(single))
    assert doc.pages[0].paper == "dotted" and doc.warnings == []
    circles = [line for line in _paper_ops(doc.pdfs[doc.pages[0].background.pdf_id]) if line.endswith(" c h f")]
    assert len(circles) == 2
    # the writer's dot paper reads back as the same dots
    written = write_write_svg(Document(pages=[Page(384, 480, paper="dotted")]))
    back = read_write_svg(written)
    assert back.pages[0].paper == "dotted" and back.warnings == []
    circles = [line for line in _paper_ops(back.pdfs[back.pages[0].background.pdf_id]) if line.endswith(" c h f")]
    assert len(circles) == 33 * 41 and float(circles[1].split()[0]) == pytest.approx((25 + 1.5) * U, abs=2e-3)


def test_generated_papers_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # every page a different paper colour: at most MAX_PAPER_PDFS papers are drawn
    pages = "".join(f'<svg class="write-page"><g class="write-content" width="800" height="1000" xruling="0" '
                    f'yruling="40" papercolor="#{i:06X}"></g></svg>' for i in range(wr.MAX_PAPER_PDFS + 8))
    doc = read_write_svg(f'<svg id="write-document">{pages}</svg>'.encode())
    assert len(doc.pdfs) == wr.MAX_PAPER_PDFS and all(p.paper == "lined" for p in doc.pages)
    assert any(w.startswith("8 pages kept only their paper style") for w in doc.warnings)
    # rules or dots too fine for a huge page: the paper colour only
    start = time.perf_counter()
    huge = ('<svg class="write-page"><g class="write-content" width="1000000" height="1000000" papercolor="#EEEEEE">'
            '<g class="ruleline"><pattern id="p" width="20" height="20" patternUnits="userSpaceOnUse">'
            '<circle cx="1" cy="1" r="1"/></pattern><rect fill="url(#p)" width="1000000" height="1000000"/></g></g></svg>'
            '<svg class="write-page"><g class="write-content write-v3" width="1000000" height="1000000" xruling="2" '
            'yruling="2"></g></svg>')
    doc = read_write_svg(f'<svg id="write-document">{huge}</svg>'.encode())
    assert time.perf_counter() - start < 5.0
    assert [p.paper for p in doc.pages] == ["dotted", "grid"]
    assert any(w.startswith("2 pages have rulings or dot grids too fine") for w in doc.warnings)
    assert sum(len(_paper_ops(pdf)) for pdf in doc.pdfs.values()) <= 2
    # the file-wide budget of rules and dots (44 x 44 dots a page)
    monkeypatch.setattr(wr, "MAX_TOTAL_PAPER_PRIMITIVES", 5000)
    dense = "".join(f'<svg class="write-page"><g class="write-content" width="1100" height="1100" '
                    f'papercolor="#{i:06X}"><g class="ruleline"><pattern id="p" width="25" height="25" '
                    f'patternUnits="userSpaceOnUse"><circle cx="12" cy="12" r="1.5"/></pattern>'
                    f'<rect fill="url(#p)" width="1100" height="1100"/></g></g></svg>' for i in range(8))
    doc = read_write_svg(f'<svg id="write-document">{dense}</svg>'.encode())
    assert len(doc.pdfs) == 5000 // (44 * 44) == 2
    assert any(w.startswith("6 pages kept only their paper style") for w in doc.warnings)


def test_dot_patterns_are_read_once_and_their_dots_are_bounded() -> None:
    # one pattern of 1000 circles filling 1000 rectangles: the pattern is read once and the dot
    # grids stop at the paper's primitive budget (10^6 grids were collected before)
    circles = '<circle cx="1" cy="1" r="1"/>' * 1000
    rects = '<rect fill="url(#p)" width="10" height="10"/>' * 1000
    off_page = '<rect fill="url(#p)" x="1000" y="1000" width="10" height="10"/>' * 1000
    hostile = f'<pattern id="p" width="5" height="5" patternUnits="userSpaceOnUse">{circles}</pattern>{rects}{off_page}'
    start = time.perf_counter()
    doc = read_write_svg(gzip.compress(_custom(hostile), mtime=0))
    assert time.perf_counter() - start < 1.0
    assert doc.pages[0].paper == "dotted"
    assert any(w.startswith("1 pages have rulings or dot grids too fine") for w in doc.warnings), doc.warnings
    assert sum(len(_paper_ops(pdf)) for pdf in doc.pdfs.values()) <= 2  # the paper colour only
    # rectangles that draw no dot still count, so a flood of them stops too
    start = time.perf_counter()
    empty = ('<pattern id="p" width="5" height="5" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1"/>'
             '</pattern>' + '<rect fill="url(#p)" x="2000" y="0" width="10" height="10"/>' * 60_000)
    doc = read_write_svg(_custom(empty))
    assert time.perf_counter() - start < 3.0
    assert any(w.startswith("1 pages have rulings or dot grids too fine") for w in doc.warnings), doc.warnings
    # a pattern that is not made of circles stays a custom background however often it is used
    lines = ('<pattern id="q" width="5" height="5" patternUnits="userSpaceOnUse"><path d="M0 0 l5 0"/></pattern>'
             + '<rect fill="url(#q)" width="10" height="10"/>' * 50)
    doc = read_write_svg(_custom(lines))
    assert doc.warnings == [CUSTOM_BACKGROUND] and doc.pages[0].paper == "plain"
    # too many dots on top of Write's standard ruling: the standard ruling stays, one warning
    dense = (f'<pattern id="p" width="5" height="5" patternUnits="userSpaceOnUse">{circles}</pattern>'
             + '<rect fill="url(#p)" width="10" height="10"/>' * 100)
    doc = read_write_svg(_doc("", ruleline=dense))
    assert doc.pages[0].paper == "lined" and doc.warnings == [CUSTOM_BACKGROUND]


def test_nested_svg_in_the_ruling_maps_its_viewbox() -> None:
    # pdftowrite's page: the PDF page is an svg.page-background in g.ruleline whose size is in px
    # (Write units) and whose viewBox is in PDF points; a scanned page is one image filling it
    uri = _png_uri(60, 85)
    background = ('<svg class="page-background" width="793.7px" height="1122.5px" viewBox="0 0 595.28 841.89">'
                  f'<image width="595.28" height="841.89" preserveAspectRatio="none" xlink:href="{uri}" /></svg>')
    page = (f'<svg id="write-document" {SVG_NS}><svg class="write-page" x="10" y="10" width="793.7px" '
            'height="1122.5px"><g class="write-content write-v3" xruling="0" yruling="40" marginLeft="100" '
            'papercolor="#FFFFFF" rulecolor="#9F0000FF"><g class="ruleline " data-pdf-file="scan.pdf">'
            '<rect class="pagerect" fill="#FFFFFF" stroke="none" x="0" y="0" width="793.7px" height="1122.5px" />'
            f'{background}</g>{_pen("M100 100 l600 900")}</g></svg></svg>')
    doc = read_write_svg(page.encode())
    (image,) = doc.pages[0].images
    # (the two ratios differ in the fifth digit: the viewBox is centred, as SVG does, within 0.002 pt)
    assert (image.x, image.y, image.w, image.h) == pytest.approx((0, 0, 793.7 * U, 1122.5 * U), abs=0.005)
    assert doc.pages[0].width == pytest.approx(793.7 * U) and doc.warnings == []
    # x / y of a nested svg in the ruling move it, as in the content
    moved = background.replace('<svg class="page-background"', '<svg class="page-background" x="20" y="30"')
    doc = read_write_svg(page.replace(background, moved).encode())
    assert (doc.pages[0].images[0].x, doc.pages[0].images[0].y) == pytest.approx((20 * U, 30 * U), abs=0.005)


def _pdftowrite_page(background: str, content: str = "") -> bytes:
    """pdftowrite's page (``page.svg``): the PDF page, here as cairo writes it, is an
    ``svg.page-background`` in ``g.ruleline`` whose size is in px (Write units) and whose
    ``viewBox`` is in PDF points."""
    return (f'<svg id="write-document" {SVG_NS}><svg class="write-page" x="10" y="10" width="793.7px" '
            'height="1122.5px"><g class="write-content write-v3" xruling="0" yruling="40" marginLeft="100" '
            'papercolor="#FFFFFF" rulecolor="#9F0000FF"><g class="ruleline " data-pdf-file="scan.pdf" '
            'data-pdf-page="1"><rect class="pagerect" fill="#FFFFFF" stroke="none" x="0" y="0" width="793.7px" '
            'height="1122.5px" /><svg class="page-background" width="793.7px" height="1122.5px" '
            f'viewBox="0 0 595.28 841.89">{background}</svg></g>{content}</g></svg></svg>').encode()


def _pdf_box(x: float, y: float, w: float, h: float) -> Tuple[float, float, float, float]:
    """A box in the PDF points of :func:`_pdftowrite_page`'s viewBox -> pt on the page (the
    viewBox is fitted and centred, as SVG does)."""
    s = min(793.7 / 595.28, 1122.5 / 841.89)
    ox, oy = (793.7 - 595.28 * s) / 2, (1122.5 - 841.89 * s) / 2
    return (ox + x * s) * U, (oy + y * s) * U, w * s * U, h * s * U


def test_cairo_images_in_defs_are_drawn_where_use_puts_them() -> None:
    # cairo (pdftocairo -svg, Inkscape's poppler import, so pdftowrite's default mode) stores a
    # raster image once in <defs> at its pixel size and draws it with <use transform="matrix(...)">
    uri = _png_uri(40, 50)
    cairo = (f'<defs><image id="source-5" x="0" y="0" width="40" height="50" xlink:href="{uri}"/></defs>'
             '<use xlink:href="#source-5" transform="matrix(9.987919, 0, 0, 9.987919, 49.939597, 80.356946)"/>')
    doc = read_write_svg(_pdftowrite_page(cairo))
    (image,) = doc.pages[0].images
    assert (image.x, image.y, image.w, image.h) == pytest.approx(
        _pdf_box(49.939597, 80.356946, 40 * 9.987919, 50 * 9.987919), abs=1e-6)
    assert (image.x, image.y, image.w, image.h) == pytest.approx((31.96, 51.43, 255.69, 319.61), abs=0.01)
    assert doc.pages[0].paper == "plain" and doc.warnings == []
    # a full-page scan inside cairo's page clip: the image fills the page (the clip is warned)
    scan = _png_uri(124, 175)
    full = ('<defs><clipPath id="clip-0"><path clip-rule="nonzero" d="M 0 0.45 L 594.56 0.45 L 594.56 841 L 0 841 Z"/>'
            f'</clipPath><image id="source-6" x="0" y="0" width="124" height="175" xlink:href="{scan}"/></defs>'
            '<g clip-path="url(#clip-0)"><use xlink:href="#source-6" transform="matrix(4.794, 0, 0, 4.794, 0.05, 0.46)"/>'
            '</g>')
    doc = read_write_svg(_pdftowrite_page(full))
    (image,) = doc.pages[0].images
    assert (image.x, image.y, image.w, image.h) == pytest.approx(_pdf_box(0.05, 0.46, 124 * 4.794, 175 * 4.794),
                                                                 abs=1e-6)
    page = doc.pages[0]
    assert image.x < 0.1 and image.y < 0.4 and image.w > 0.99 * page.width and image.h > 0.99 * page.height
    assert doc.warnings == ["1 clipped or masked elements are shown unclipped"]
    # vector content of the PDF page (cairo's glyphs: a symbol per glyph drawn by <use>) is a
    # custom background; next to an image, the warning says the image was kept
    glyphs = ('<defs><symbol overflow="visible" id="glyph-0-1"><path d="M1 0 L5 0 L3 -7 Z"/></symbol></defs>'
              '<g fill="rgb(0%, 0%, 0%)"><use xlink:href="#glyph-0-1" x="72" y="100"/>'
              '<use xlink:href="#glyph-0-1" x="80" y="100"/></g>')
    doc = read_write_svg(_pdftowrite_page(glyphs))
    assert doc.pages[0].images == [] and doc.warnings == [CUSTOM_BACKGROUND]
    doc = read_write_svg(_pdftowrite_page(glyphs + cairo))
    assert len(doc.pages[0].images) == 1 and doc.warnings == [CUSTOM_BACKGROUND_IMAGES]
    # pdftowrite's text layer (the PDF's text, made invisible for searching) draws nothing
    layer = ('<svg id="text-layer-p1" class=" pdftowrite-text-layer" width="595.28" height="841.89" '
             'viewBox="0 0 595.28 841.89"><g transform="matrix(1.333 0 0 -1.333 0 841.89)">'
             '<text style="font-size:12px;fill:#000000;fill-opacity:0;stroke:none;" x="72" y="100">'
             '<tspan x="72 79 86" y="100">abc</tspan></text></g></svg>')
    doc = read_write_svg(_pdftowrite_page(cairo + layer))
    assert len(doc.pages[0].images) == 1 and doc.warnings == []
    for shown in (layer.replace("fill-opacity:0", "fill-opacity:1"),
                  layer.replace("<tspan ", '<tspan fill-opacity="0.5" '),
                  layer.replace("stroke:none", "stroke:#000")):
        doc = read_write_svg(_pdftowrite_page(cairo + shown))
        assert doc.warnings == [CUSTOM_BACKGROUND_IMAGES], shown


def test_definitions_in_the_ruling_draw_nothing_where_they_stand() -> None:
    uri = _png_uri(40, 50)
    hidden = (f'<defs><image id="i" width="40" height="50" xlink:href="{uri}"/><circle cx="5" cy="5" r="1"/>'
              '<clipPath id="c"><path d="M0 0 h10 v10 z"/></clipPath><g><path d="M0 0 C1 1 2 2 3 0"/></g></defs>'
              '<symbol id="s"><circle cx="9" cy="9" r="1"/></symbol><mask id="m"><rect width="5" height="5"/></mask>')
    doc = read_write_svg(_custom(hidden))
    page = doc.pages[0]
    assert page.images == [] and page.paper == "plain" and page.background is None and doc.warnings == []
    # what they define is drawn where a <use> refers to it: x / y move the target, a symbol
    # maps its viewBox into the use's width / height, a group's images come along
    reused = (f'<defs><symbol id="s" viewBox="0 0 40 50"><image width="40" height="50" xlink:href="{uri}"/></symbol>'
              f'<g id="g"><image x="1" y="2" width="4" height="5" xlink:href="{uri}"/></g></defs>'
              '<use xlink:href="#s" x="100" y="200" width="80" height="100"/>'
              '<use href="#g" x="10" y="20" transform="scale(2)"/>')
    doc = read_write_svg(_custom(reused))
    boxes = [v for i in doc.pages[0].images for v in (i.x, i.y, i.w, i.h)]
    assert boxes == pytest.approx([100 * U, 200 * U, 80 * U, 100 * U, 22 * U, 44 * U, 8 * U, 10 * U], abs=1e-9)
    assert doc.pages[0].images[0].data == png(40, 50) and doc.warnings == []
    # reused circles are dots where the use puts them (the ruling's stroke widens them)
    dots = '<defs><circle id="d" r="2"/></defs><use href="#d" x="100" y="100"/><use href="#d" x="200" y="150"/>'
    doc = read_write_svg(_custom(dots))
    assert doc.pages[0].paper == "dotted" and doc.warnings == []
    circles = [line.split() for line in _paper_ops(doc.pdfs[doc.pages[0].background.pdf_id])
               if line.endswith(" c h f")]
    r = (2 + 0.5) * U
    assert [c[:5] for c in circles] == [["q", "0", "0", "1", "rg"]] * 2  # the ruling's stroke colour
    assert [v for c in circles for v in (float(c[5]) - r, 480 - float(c[6]))] == pytest.approx(
        [100 * U, 100 * U, 200 * U, 150 * U], abs=2e-3)


def test_use_in_the_content_draws_its_target() -> None:
    defs = ('<defs><path id="p" class="write-stroke-pen" fill="none" stroke="#000" stroke-width="2" d="M0 0 l10 0"/>'
            '<g id="two"><use href="#p"/><use href="#p" y="5"/></g></defs>')
    content = defs + '<use xlink:href="#p" x="100" y="50"/><use href="#two" transform="translate(0 100)"/>'
    doc = read_write_svg(_doc(content))
    strokes = doc.pages[0].strokes
    assert [_xy(s.points) for s in strokes] == [
        _xy([Point(100 * U, 50 * U), Point(110 * U, 50 * U)]), _xy([Point(0, 100 * U), Point(10 * U, 100 * U)]),
        _xy([Point(0, 105 * U), Point(10 * U, 105 * U)])]
    assert all(s.width == pytest.approx(2 * U) and s.kind == "pen" for s in strokes) and doc.warnings == []
    # references that draw nothing: missing, in another file, to the use itself, to an element
    # holding it, in a cycle of uses, to a definition that is not drawn
    bad = ('<use href="#missing"/><use xlink:href="other.svg#p"/><use id="self" href="#self"/>'
           '<g id="loop"><path fill="none" stroke="#000" d="M0 0 l1 1"/><use href="#loop"/></g>'
           '<defs><g id="a"><use href="#b"/></g><g id="b"><use href="#a"/></g><linearGradient id="grad"/></defs>'
           '<use href="#a"/><use href="#grad"/>')
    doc = read_write_svg(_doc(bad))
    assert len(doc.pages[0].strokes) == 1 and doc.warnings == ["6 " + USE_SKIPPED]
    # uses nested more than MAX_USE_DEPTH deep stop there
    chain = '<defs><path id="l0" fill="none" stroke="#000" d="M0 0 l1 1"/>' + "".join(
        f'<g id="l{i}"><use href="#l{i - 1}"/></g>' for i in range(1, wr.MAX_USE_DEPTH + 1)) + "</defs>"
    doc = read_write_svg(_doc(chain + f'<use href="#l{wr.MAX_USE_DEPTH - 1}"/>'))
    assert len(doc.pages[0].strokes) == 1 and doc.warnings == []
    doc = read_write_svg(_doc(chain + f'<use href="#l{wr.MAX_USE_DEPTH}"/>'))
    assert doc.pages[0].strokes == [] and len(doc.warnings) == 1
    assert doc.warnings[0].startswith("1 reused shapes (<use> elements) beyond the limits of 4 nested reuses")


def test_use_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # nested groups of uses multiply: each instance is charged its target's size against
    # MAX_USE_ELEMENTS per file, in the content and in the ruling alike
    monkeypatch.setattr(wr, "MAX_USE_ELEMENTS", 20_000)
    for leaf in ('<path fill="none" stroke="#000" d="M0 0 l1 1"/>', '<circle r="1" cx="5" cy="5"/>',
                 f'<image width="1" height="1" href="{_png_uri(1, 1)}"/>'):
        laugh = '<defs><g id="l0">' + leaf * 100 + "</g>" + "".join(
            f'<g id="l{i}">' + f'<use href="#l{i - 1}"/>' * 10 + "</g>" for i in (1, 2, 3)) + "</defs>"
        laugh += '<use href="#l3"/>' * 100
        for data in (_doc(laugh), _custom(laugh)):
            start = time.perf_counter()
            doc = read_write_svg(gzip.compress(data, mtime=0))
            assert time.perf_counter() - start < 3.0
            page = doc.pages[0]
            assert len(page.strokes) + len(page.images) <= 20_000
            assert any(" reused shapes (<use> elements) beyond the limits" in w for w in doc.warnings), doc.warnings
    # a large image drawn many times: decoded once, and the image data per file is bounded
    monkeypatch.setattr(wr, "MAX_TOTAL_IMAGE_BYTES", 10 * len(png(64, 64)))
    reused = f'<defs><image id="i" width="10" height="10" href="{_png_uri(64, 64)}"/></defs>' + '<use href="#i"/>' * 15
    doc = read_write_svg(_doc(reused))
    assert len(doc.pages[0].images) == 10 and len({id(i.data) for i in doc.pages[0].images}) == 1
    assert doc.warnings == ["5 images beyond the limit of 256 MB of image data per file (a reused image counting "
                            "each time it is drawn) were skipped"]  # (the message names the real limit)
    # a use deep in the content of a deep target: the walk stops at MAX_DEPTH levels
    deep = ('<defs>' + '<g id="deep">' + "<g>" * 100 + '<path fill="none" stroke="#000" d="M0 0 l1 1"/>' + "</g>" * 100
            + "</g></defs>" + "<g>" * 100 + '<use href="#deep"/>' + "</g>" * 100 + '<use href="#deep"/>')
    doc = read_write_svg(_doc(deep))
    assert len(doc.pages[0].strokes) == 1
    assert any(w.startswith("1 reused shapes (<use> elements) beyond the limits") for w in doc.warnings), doc.warnings


USE_LIMIT = ("reused shapes (<use> elements) beyond the limits of 4 nested reuses, 128 nesting levels, 1000000 "
             "reused elements or 16 MB of reused attribute and text data per file were skipped")


def test_use_is_charged_for_its_targets_data() -> None:
    # one element with megabytes of data reused 10 000 times: every instance parses the data
    # again, so it is charged against MAX_USE_BYTES (16 MB) and reuse stops after 4 instances
    # (before: 111 s for a 5 KB .svgz of a whitespace path, 83 s and 1.6 GB for a text)
    n = 4_000_000
    assert 4 * n < wr.MAX_USE_BYTES < 5 * n
    uses = '<use href="#t"/>' * 10_000
    for target in ('<path id="t" fill="none" stroke="#000" d="' + " " * n + '"/>',
                   '<text id="t" x="10" y="20" font-size="0.0001">' + "a " * (n // 2) + "</text>",
                   '<path id="t" fill="none" stroke="#000" style="' + "x" * n + ':1" d="M0 0 l1 1"/>',
                   '<path id="t" fill="none" stroke="#000" transform="' + " " * n + 'translate(1)" d="M0 0 l1 1"/>'):
        start = time.perf_counter()
        doc = read_write_svg(gzip.compress(_doc(f"<defs>{target}</defs>{uses}"), mtime=0))
        assert time.perf_counter() - start < 1.5, target[:40]
        page = doc.pages[0]
        assert len(page.strokes) + len(page.texts) in (0, 4)
        assert "9996 " + USE_LIMIT in doc.warnings, doc.warnings
    assert len(page.strokes) == 4 and len(page.texts) == 0  # the last target draws a stroke each time
    doc = read_write_svg(_doc('<defs><text id="t" x="10" y="20" font-size="0.0001">' + "a " * (n // 2)
                              + '</text></defs><use href="#t"/>'))
    assert len(doc.pages[0].texts[0].text) == n - 1  # one instance draws the whole text
    # a large JPEG without a size marker is measured once, not once per instance (it was
    # scanned byte by byte each time: 0.19 s an instance), and the rejected instances are
    # cheap; the image's data is not charged as reused data
    jpeg = b"\xff\xd8\xff\x00\x00\x00" + bytes(n)
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    reused = f'<defs><image id="i" width="0" height="10" href="{uri}"/></defs>' + '<use href="#i"/>' * 1000
    start = time.perf_counter()
    doc = read_write_svg(gzip.compress(_doc(reused), mtime=0))
    assert time.perf_counter() - start < 1.5
    assert doc.warnings == ["1000 shapes, images or texts with invalid or out-of-range coordinates were skipped"]
    reused = f'<defs><image id="i" width="10" height="10" href="{_png_uri()}"/></defs>' + '<use href="#i"/>' * 100
    assert len(read_write_svg(_doc(reused)).pages[0].images) == 100
    # nested uses: every instance of the inner target is charged, however it is reached
    nested = ('<defs><path id="t" fill="none" stroke="#000" d="M0 0 l1 1' + " " * 1_000_000 + '"/>'
              '<g id="g">' + '<use href="#t"/>' * 10 + "</g></defs>" + '<use href="#g"/>' * 10)
    doc = read_write_svg(_doc(nested))
    assert len(doc.pages[0].strokes) == 16 and doc.warnings == ["12 " + USE_LIMIT]


def test_long_presentation_values_are_ignored() -> None:
    # a value longer than MAX_PROPERTY_CHARS is never valid: ignored as CSS ignores invalid
    # values, so the shapes below a group do not each parse it again (1000 shapes under a
    # 1 MB fill took 1.9 s, 100 000 would take minutes)
    long = "#" + "0" * 1_000_000
    shapes = '<path d="M0 0 L10 0 L10 10 Z"/>' * 1000
    start = time.perf_counter()
    doc = read_write_svg(gzip.compress(_doc(f'<g fill="#FF0000"><g fill="{long}">{shapes}</g></g>'), mtime=0))
    assert time.perf_counter() - start < 1.5
    strokes = doc.pages[0].strokes
    assert len(strokes) == 1000 and all(s.kind == "fill" and s.color == (1.0, 0.0, 0.0, 1.0) for s in strokes)
    assert doc.warnings == ["1 style values longer than 1024 characters were ignored"]
    # in a style declaration the attribute's value stays; white space around a value is fine
    pad = " " * 2000
    content = (f'<path fill="#00FF00" style="fill: {long}" d="M0 0 L10 0 L10 10 Z"/>'
               f'<path fill="{pad}#0000FF{pad}" d="M0 0 L10 0 L10 10 Z"/>'
               f'<text x="10" y="20" font-family="{"a" * 2000}">x</text>')
    doc = read_write_svg(_doc(content))
    assert [s.color for s in doc.pages[0].strokes] == [(0.0, 1.0, 0.0, 1.0), (0.0, 0.0, 1.0, 1.0)]
    assert doc.pages[0].texts[0].runs[0].font is None
    assert "2 style values longer than 1024 characters were ignored" in doc.warnings


def test_nested_tspans_are_scanned_once() -> None:
    # whether a tspan draws a character is found in one pass, not once per nesting level
    text = '<text x="10" y="20" font-size="0.0001">a' + "<tspan>" * 120 + "b" * 4_000_000 + "</tspan>" * 120 + "</text>"
    start = time.perf_counter()
    doc = read_write_svg(_doc(text))
    assert time.perf_counter() - start < 1.5
    assert doc.pages[0].texts[0].text == "a" + "b" * 4_000_000
    # a tspan of white space draws nothing and starts no line, unless white space is preserved
    lines = ('<text{} x="10" y="20">a<tspan x="10" y="38.6"> </tspan><tspan x="10" y="50"><tspan>\t</tspan>b'
             '</tspan></text>')
    assert read_write_svg(_doc(lines.format(""))).pages[0].texts[0].text == "a\nb"
    assert read_write_svg(_doc(lines.format(' xml:space="preserve"'))).pages[0].texts[0].text == "a\n \n b"


def test_lengths_parse_in_linear_time() -> None:
    # two adjacent optional runs of white space around an empty unit backtracked against each
    # other: a 100 000-space x attribute took 26 s, quadrupling with every doubling
    cases = {"900px": 900.0, "12": 12.0, " 5mm ": 5 / 25.4 * 72 / U, "1e3": 1000.0, "-.5in": -0.5 * 72 / U,
             "10 px": 10.0, "3.": 3.0, "+4pt\n": 4 / U, " \t7\n": 7.0}
    for text, value in cases.items():
        assert wr._length(text) == pytest.approx(value), text
    for text in ("50%", "1e", "1 !", "abc", "", "  ", "1 px !", None):
        assert wr._length(text) is None, text
    start = time.perf_counter()
    assert wr._length("1" + " " * 4_000_000 + "!") is None
    assert wr._length("1" + " " * 4_000_000) == 1.0
    doc = read_write_svg(gzip.compress(_doc(f'<rect x="1{" " * 1_000_000}!" y="0" width="10" height="10"/>'),
                                       mtime=0))
    assert len(doc.pages[0].strokes) == 1  # the invalid x is 0, as SVG takes it
    # an inherited value just within MAX_PROPERTY_CHARS is parsed again by every shape below it
    bad = "1" + " " * (wr.MAX_PROPERTY_CHARS - 2) + "!"
    shapes = '<path d="M0 0 L10 0"/>' * 10_000
    doc = read_write_svg(gzip.compress(_doc(f'<g stroke="#000000" stroke-width="{bad}">{shapes}</g>'), mtime=0))
    assert time.perf_counter() - start < 2.0  # 29 s, and about an hour for a 4 MB value, before
    strokes = doc.pages[0].strokes
    assert len(strokes) == 10_000 and strokes[0].width == pytest.approx(U)  # SVG's initial stroke-width


def test_shared_page_svg_is_parsed_once() -> None:
    # Write 2 puts every g#page_N into the root svg: its viewBox, size and style were parsed
    # again for every page (an 8 MB viewBox over 10 000 pages took 115 s)
    def write2(svg_attrs: str, pages: int, content: str = "") -> bytes:
        body = "".join(f'<g id="page_{k + 1}" width="900" height="1200" xruling="0" yruling="0">{content}</g>'
                       for k in range(pages))
        return gzip.compress(f'<svg {SVG_NS} {svg_attrs}>{body}</svg>'.encode(), mtime=0)

    start = time.perf_counter()
    doc = read_write_svg(write2(f'viewBox="{" " * 8_000_000}x"', 2000))
    assert len(doc.pages) == 2000 and doc.warnings == []
    doc = read_write_svg(write2(f'fill="{"x" * 4_000_000}" style="stroke: {"y" * 4_000_000}"', 2000))
    assert time.perf_counter() - start < 2.0
    assert len(doc.pages) == 2000
    assert doc.warnings == ["2 style values longer than 1024 characters were ignored"]  # once, not per page
    # what the shared svg gives is still inherited by every page, and its viewBox maps each page
    doc = read_write_svg(write2('viewBox="0 0 450 600" stroke="#FF0000" stroke-width="3"', 3, '<path d="M0 0 L10 0"/>'))
    for page in doc.pages:
        (stroke,) = page.strokes
        assert stroke.color == (1.0, 0.0, 0.0, 1.0) and stroke.width == pytest.approx(6 * U)
        assert _xy(stroke.points) == _xy([Point(0, 0), Point(20 * U, 0)])


def test_white_space_only_text_lines() -> None:
    # with xml:space="preserve" a space is a drawn character: a line of only (no-break) spaces is
    # a line, so gnnote's own text boxes come back with their leading white space lines
    for text in ("\xa0\n\xa0\nX", " \nX", "\xa0\n\xa0\n\xa0\nPrint Name", "A\n\xa0\nB", " \n odd  space",
                 "\xa0\n\nX"):
        page = Page(612, 792, texts=[TextBox(72, 72, 300, 80, text, runs=[TextRun(text, size=12.0)], size=12.0)])
        (box,) = read_write_svg(write_write_svg(Document(pages=[page]))).pages[0].texts
        assert box.text == text and (box.x, box.y) == pytest.approx((72, 72), abs=1e-3), repr(text)
    preserve = '<text xml:space="preserve" x="150" y="100" font-size="25"><tspan x="150" y="100"> </tspan>' \
               '<tspan x="150" y="129">Print</tspan></text>'
    (box,) = read_write_svg(_doc(preserve)).pages[0].texts
    assert box.text == " \nPrint" and box.y == pytest.approx((100 - wr.TEXT_ASCENT * 25) * U)
    # default white space handling collapses spaces, tabs and newlines but never a no-break
    # space, which SVG draws (Chromium: 8 characters, "c" after the four no-break spaces)
    content = ('<text x="150" y="300" font-size="25">a&#160;&#160;&#160;  &#160;b</text>'
               '<text x="150" y="400" font-size="25"> &#160;&#160;c&#160; </text>'
               '<text x="10" y="100" font-size="20">a<tspan x="10" y="123.3">&#160;</tspan>'
               '<tspan x="10" y="146.6">b</tspan></text>')
    texts = read_write_svg(_doc(content)).pages[0].texts
    assert [t.text for t in texts] == ["a\xa0\xa0\xa0 \xa0b", "\xa0\xa0c\xa0", "a\n\xa0\nb"]
    assert wr._collapse("\n  a\t\tb\n") == " a b" and wr._collapse("x\xa0 \t\xa0y") == "x\xa0 \xa0y"
    assert wr._collapse("   ") == " " and wr._collapse("\n") == ""


def test_text_lines_start_where_their_first_visible_character_is() -> None:
    # pretty-printed text: the white space before the first positioned tspan is drawn (or, by
    # default, collapsed) at the text's own position, the visible text where the tspan puts it.
    # Chromium places "L" of "Line 1" at (150, 100), "I" at (150, 300), "H" at (150, 400) and
    # (156.3 + 12.5 = 168.8, 600) after three spaces, "F" at (150, 760).
    content = ('<text id="a" xml:space="preserve" font-size="25">\n  <tspan x="150" y="100">Line 1</tspan>\n'
               '  <tspan x="150" y="129">Line 2</tspan>\n</text>'
               '<text id="i" xml:space="preserve" x="0" y="300" font-size="25">\n  '
               '<tspan x="150" y="300">Indented</tspan></text>'
               '<text id="h" y="400" font-size="25">\n  <tspan x="150">Hi</tspan>\n</text>'
               '<text id="y" xml:space="preserve" x="150" y="500" font-size="25">   <tspan y="600">Hi</tspan></text>'
               '<text id="d" xml:space="preserve" font-size="25" x="150" y="700"> '
               '<tspan x="150" y="760">Far</tspan></text>')
    texts = read_write_svg(_doc(content)).pages[0].texts
    assert [t.text for t in texts] == ["Line 1   \nLine 2 ", "Indented", "Hi", "   Hi", " \n\nFar"]
    tops = [100, 300, 400, 600, 700]
    for box, top in zip(texts, tops):
        assert (box.x, box.y) == pytest.approx((150 * U, (top - wr.TEXT_ASCENT * 25) * U)), box.text


def test_long_word_lists_cost_linear_memory() -> None:
    # str.split() on a long list made an object per word (about 20 bytes per character): a
    # 245 kB .svgz with one 252 MB class, x, text, preserveAspectRatio, title or image payload
    # needed 6 GB.  Only the words needed are taken now; the rest costs nothing per word.
    words = "12 " * 1_000_000
    stroke = '<path d="M0 0 L10 10" stroke="#000"/>'
    cases = {
        "class": (f'<g class="{words} x">{stroke}</g>', "strokes"),
        "x": (f'<text x="{words}" y="100" font-size="20">a</text>', "texts"),
        "tspan x": (f'<text x="10" y="100" font-size="20">a<tspan x="{words}" y="200">b</tspan></text>', "texts"),
        "tspan y": (f'<text x="10" y="100" font-size="20">a<tspan y="{words}">b</tspan></text>', "texts"),
        "tspan dy": (f'<text x="10" y="100" font-size="20">a<tspan dy="{words}">b</tspan></text>', "texts"),
        "content": (f'<text x="10" y="100" font-size="20">a {words}</text>', None),
        "aspect": (f'<svg width="100" height="100" viewBox="0 0 10 10" preserveAspectRatio="{words}">{stroke}</svg>',
                   "strokes"),
        "image": (f'<image width="10" height="10" xlink:href="data:image/png;base64,{"AA " * 1_000_000}"/>', None),
    }
    for name, (content, kind) in cases.items():
        data = _doc(content)
        doc, peak = _peak(lambda: read_write_svg(data))
        assert peak < 6 * len(data), (name, peak / len(data))  # 21 to 24 times before
        if kind:
            assert len(getattr(doc.pages[0], kind)) == 1, name
    data = (f'<svg id="write-document" {SVG_NS}><title> {words}</title><svg class="write-page">'
            '<g class="write-content write-v3"/></svg></svg>').encode()
    doc, peak = _peak(lambda: read_write_svg(data))
    assert peak < 6 * len(data) and doc.title == ("12 " * 67)[:200]
    # the helpers keep their results
    assert wr._list_head(" ,\t5mm, 6") == pytest.approx(5 / 25.4 * wr.UNITS_PER_INCH) and wr._list_head(" , ") is None
    assert wr._first_offset("2em,3", 10.0) == 20.0 and wr._first_offset("", 10.0) is None
    assert wr._words("  a b\u2003c ", 2) == ["a", "b"] and wr._words("", 3) == []


def test_white_space_collapses_across_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wr, "COLLAPSE_CHUNK", 5)
    rng = random.Random(7)
    for _ in range(2000):
        text = "".join(rng.choice(["a", "bc", " ", "  ", "\t", "\r", "\n", "\xa0", "\u2003"])
                       for _ in range(rng.randint(0, 30)))
        flat = text.replace("\n", "")
        core = " ".join(w for w in flat.replace("\t", " ").replace("\r", " ").split(" ") if w)
        expected = flat if not flat else (" " if not core else (" " if flat[0] in " \t\r" else "") + core +
                                          (" " if flat[-1] in " \t\r" else ""))
        assert wr._collapse(text) == expected, repr(text)


def test_aspect_ratio_words() -> None:
    uri = _png_uri(20, 10)  # twice as wide as tall, in a square box
    for par, x, y in (("", 0, 25), ("defer xMinYMin meet", 0, 0), ("xMaxYMax", 0, 50), ("bogus", 0, 25)):
        content = f'<image x="0" y="0" width="100" height="100" preserveAspectRatio="{par}" xlink:href="{uri}" />'
        (image,) = read_write_svg(_doc(content)).pages[0].images
        assert (image.x, image.y, image.w, image.h) == pytest.approx((x * U, y * U, 100 * U, 50 * U)), par


def test_paper_module() -> None:
    from gnnote.styluswrite.paper import MAX_PRIMITIVES, Dot, DotGrid, Paper, blend, paper_pdf

    assert blend((0, 0, 1, 0.5), (1, 1, 1)) == (0.5, 0.5, 1.0)
    paper = Paper(100, 50, ypitch=10, xpitch=30, margin=20)
    assert paper.rule_counts() == (4, 3) and paper.primitives() == 8 and not paper.is_blank()
    assert Paper(100, 50).is_blank() and not Paper(100, 50, (1, 1, 0.9)).is_blank()
    grid = DotGrid(-5, -5, 10, 10, 1, 0, 0, 100, 50, (0, 0, 0))
    assert grid.count() == 10 * 5
    assert DotGrid(0, 0, 1e-300, 1, 1, 0, 0, 100, 50, (0, 0, 0)).count() > MAX_PRIMITIVES
    assert DotGrid(0, 0, 0, 1, 1, 0, 0, 100, 50, (0, 0, 0)).count() == 0
    with pytest.raises(ValueError):
        paper_pdf(Paper(100, 50, grids=(DotGrid(0, 0, 0.01, 0.01, 1, 0, 0, 100, 50, (0, 0, 0)),)))
    pdf = paper_pdf(Paper(100, 50, dots=(Dot(10, 10, 2, (1, 0, 0)),)))
    assert read_pdf(pdf).pages[0].width == 100 and b"/Producer (gnnote)" in pdf


def test_ruling_background_images_and_custom_backgrounds() -> None:
    uri = _png_uri()
    doc = read_write_svg(_doc(_pen("M0 0 l1 1"), ruleline=f'<image x="0" y="0" width="800" height="1000" '
                                                         f'xlink:href="{uri}" /><path class="leftmargin" d="M100 0 l0 1000"/>'))
    assert len(doc.pages[0].images) == 1 and doc.pages[0].images[0].w == pytest.approx(800 * U)
    assert doc.warnings == []
    doc = read_write_svg(_doc("", ruleline='<path d="M0 0 C10 10 20 20 30 0"/><text>PDF</text>'))
    assert doc.warnings == [CUSTOM_BACKGROUND]
    assert doc.pages[0].paper == "lined"  # the standard ruling under the custom drawing is kept


def test_write2_html_pages_and_titles() -> None:
    html = ("<!DOCTYPE html PUBLIC '-//W3C//DTD XHTML 1.0 Strict//EN' "
            "'http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd'>"
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Handwritten Document</title>'
            '<script type="text/writeconfig"><int name="pageNum" value="0" /></script></head><body>' +
            "".join(f"<svg width='768px' height='{h}px' {SVG_NS}><g id='page_1' width='768.000' height='{h}.000' "
                    "xruling='0.000' yruling='40.000' marginLeft='100.000' papercolor='#FFFFFF' "
                    "rulecolor='#FF0000FF'><path class='ruleline' fill='none' stroke='#0000FF' "
                    "d='M0.000 40.000 l 768.000 0.000'/><path fill='none' stroke='#000000' stroke-width='1.000' "
                    "d='M10 10 h 0'/></g></svg>" for h in (1024, 1420)) + "</body></html>")
    data = html.encode()
    assert detect_format("Help.html", data) == "styluswrite"
    doc = read_write_svg(data)
    assert [round(p.height / U) for p in doc.pages] == [1024, 1420]
    assert [len(p.strokes) for p in doc.pages] == [1, 1]
    assert _xy(doc.pages[0].strokes[0].points) == _xy([Point(10 * U, 10 * U)] * 2)  # a dot
    assert doc.title == ""  # Write 2's fixed title is not a document name
    named = _doc("").replace(b"<rect id", b"<title>  My   notes </title><rect id")
    assert read_write_svg(named).title == "My notes"


def test_single_page_files_and_empty_documents() -> None:
    single = (f'<svg class="write-page" width="900px" height="420px" {SVG_NS}><g class="write-content write-v3" '
              'xruling="0" yruling="40" marginLeft="0" papercolor="#FFFFFF" rulecolor="#FF0000FF">'
              + _pen("M1 1 l1 1") + "</g></svg>").encode()
    doc = read_write_svg(single)
    assert (round(doc.pages[0].width / U), round(doc.pages[0].height / U)) == (900, 420)
    empty = f'<svg id="write-document" {SVG_NS}></svg>'.encode()
    doc = read_write_svg(empty)
    assert doc.pages == [] and doc.warnings == ["The document has no pages"]
    sized = _doc("", attrs='xruling="0"').replace(b'width="800px" height="1000px"', b'width="100%"').replace(
        b'width="800" height="1000" />', b"/>")
    doc = read_write_svg(sized)
    assert (doc.pages[0].width, doc.pages[0].height) == (768 * U, 1050 * U)
    assert "1 pages had no valid size; Write's default page size was used" in doc.warnings


def test_page_viewbox_and_nested_svg() -> None:
    data = _doc('<svg x="100" y="50" width="20" height="20" viewBox="0 0 10 10">' + _pen("M0 0 l10 10") + "</svg>")
    data = data.replace(b'width="800px" height="1000px"', b'width="800px" height="1000px" viewBox="0 0 400 500"')
    doc = read_write_svg(data)
    # page viewBox scales by 2, the nested svg moves by (100, 50) and scales by 2 again
    assert _xy(doc.pages[0].strokes[0].points) == _xy([Point(200 * U, 100 * U), Point(240 * U, 140 * U)])


def test_compressed_containers() -> None:
    data = _doc(_pen("M0 0 l5 5"))
    for packed in (gzip.compress(data, mtime=0), zlib.compress(data), gzip.compress(data[:40]) + gzip.compress(data[40:])):
        doc = read_write_svg(packed)
        assert len(doc.pages[0].strokes) == 1
    truncated = gzip.compress(data)[:-30]
    doc = read_write_svg(truncated)
    assert "The compressed file is truncated or damaged; only its readable part was converted" in doc.warnings


TRAILER_WARNING = ("The compressed file's gzip checksum or length does not match its content; the content was "
                   "converted as it is")
DAMAGE_WARNING = "The compressed file is truncated or damaged; only its readable part was converted"


def test_damaged_gzip_trailers_keep_the_whole_document() -> None:
    data = _doc(_pen("M0 0 l5 5") + _pen("M9 9 l5 5"), pages=2)
    gz = gzip.compress(data, 9, mtime=0)
    for label, bad in (("zeroed trailer", gz[:-8] + bytes(8)), ("bad CRC", gz[:-8] + bytes(4) + gz[-4:]),
                       ("bad length", gz[:-4] + bytes(4)), ("no trailer", gz[:-8]),
                       ("cut trailer", gz[:-3])):
        doc = read_write_svg(bad)
        assert [len(p.strokes) for p in doc.pages] == [2, 2], label
        assert doc.warnings == [TRAILER_WARNING], label
        assert formats.get("styluswrite").sniff(bad, None), label
    zl = zlib.compress(data)
    doc = read_write_svg(zl[:-4] + b"\0\0\0\0")
    assert [len(p.strokes) for p in doc.pages] == [2, 2] and doc.warnings == [TRAILER_WARNING]
    # optional gzip header fields are skipped by hand
    header = b"\x1f\x8b\x08" + bytes([4 | 8 | 16 | 2]) + bytes(6) + b"\x03\x00abc" + b"name\0" + b"comment\0" + b"\0\0"
    deflated = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = deflated.compress(data) + deflated.flush()
    member = header + body + (zlib.crc32(data)).to_bytes(4, "little") + len(data).to_bytes(4, "little")
    assert len(read_write_svg(member).pages) == 2 and read_write_svg(member).warnings == []


def test_damage_inside_the_compressed_stream_keeps_what_came_before(samples) -> None:
    raw = samples.pinned_file("write-site1_page006.svg").read_bytes()
    gz = gzip.compress(raw, 9, mtime=0)
    cut = gz[: len(gz) * 3 // 4]  # cut in the middle of the deflate data
    doc = read_write_svg(cut)
    assert DAMAGE_WARNING in doc.warnings and 300 < len(doc.pages[0].strokes) < 493
    # corrupt bytes: what came before them is kept, as much as zlib fed byte by byte would give
    from gnnote.styluswrite.container import inflate
    rng = random.Random(5)
    payload = " ".join(rng.choice(("ink", "stroke", "page", "write", "svg", "pen", "paper")) + str(rng.randrange(999))
                       for _ in range(60_000)).encode()
    packed = gzip.compress(payload, 9, mtime=0)
    for offset in (len(packed) // 3, len(packed) // 2, len(packed) * 4 // 5):
        bad = packed[:offset] + bytes(40) + packed[offset + 40:]
        out, problem = inflate(bad, 1 << 30)
        assert problem in ("damaged", "trailer")
        reference = bytearray()
        d = zlib.decompressobj(31)
        try:
            for k in range(len(bad)):
                reference += d.decompress(bad[k:k + 1])
        except zlib.error:
            pass
        assert _common_prefix(out, payload) >= _common_prefix(bytes(reference), payload) - 64 * 1024
        assert _common_prefix(out, payload) > len(payload) * offset // len(packed) // 2
    assert inflate(b"\x1f\x8b\x08\x00" + bytes(6) + b"\xff\xff\xff\xff", 1000) == (b"", "damaged")
    assert inflate(packed, 1000) == (payload[:1000], "too large")
    assert inflate(packed + b"\0" * 16, 1 << 30) == (payload, None)  # trailing padding is ignored
    assert inflate(packed + packed, 1 << 30) == (payload + payload, None)  # concatenated members


def _common_prefix(a: bytes, b: bytes) -> int:
    n = 0
    step = 1 << 16
    while step:
        while a[n:n + step] == b[n:n + step] and n + step <= min(len(a), len(b)):
            n += step
        step //= 2
    return n


def test_tiny_damaged_files_are_still_sniffed() -> None:
    data = _doc(_pen("M0 0 l5 5"))
    gz = gzip.compress(data, 9, mtime=0)
    assert len(gz) < 4096
    for bad in (gz[:-8] + bytes(8), gz[:-4], gz[: len(gz) - 40]):
        assert formats.get("styluswrite").sniff(bad, None)
        assert detect_format("page.bin", bad) == "styluswrite"


# --------------------------------------------------------------------------- reader: hostile input


def test_ordinary_svg_and_non_xml_are_refused(samples) -> None:
    for data, message in ((b"", "neither SVG"), (b"hello", "neither SVG"), (b"\x1f\x8bjunk", "compressed stream"),
                          (b"<svg", "could not be parsed"),
                          (f'<svg {SVG_NS}><path d="M0 0 l1 1"/></svg>'.encode(), "without Write's pages"),
                          (b"<?xml version='1.0'?><svg/>", "without Write's pages")):
        with pytest.raises(ValueError, match="not a Stylus Labs Write document.*" + message):
            read_write_svg(data)
    generic = samples.inkterop_fixture("svg", "tiny-generic.svg").read_bytes()
    with pytest.raises(ValueError, match="not a Stylus Labs Write document"):
        read_write_svg(generic)
    with pytest.raises(TypeError):
        read_write_svg("text")  # type: ignore[arg-type]


def test_entities_and_internal_subsets_are_refused() -> None:
    laughs = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
              b'<svg id="write-document"><g class="write-content"><text>&b;</text></g></svg>')
    external = (b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                b'<svg id="write-document"><g class="write-content"><text>&x;</text></g></svg>')
    subset = b'<!DOCTYPE svg [<!ELEMENT svg ANY>]><svg id="write-document"/>'
    for data in (laughs, external, subset, gzip.compress(laughs)):
        with pytest.raises(ValueError, match="entity declarations and internal DTD subsets are not allowed"):
            read_write_svg(data)
    plain = b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
    assert read_write_svg(plain + _doc(_pen("M0 0 l1 1"))).pages  # an external DTD is named, never fetched


def test_damaged_xml_keeps_what_came_before() -> None:
    data = _doc(_pen("M0 0 l5 5") + _pen("M9 9 l5 5"), pages=2)
    cut = data[: data.index(b"M9 9", data.index(b"M9 9") + 1) - 60]
    doc = read_write_svg(cut)
    assert [len(p.strokes) for p in doc.pages] == [2, 1]
    assert any(w.startswith("The file is damaged (the XML is damaged or truncated at line") for w in doc.warnings)


def test_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    deep = _doc("<g>" * 300 + _pen("M0 0 l1 1") + "</g>" * 300)
    doc = read_write_svg(deep)
    assert any("nested deeper than" in w for w in doc.warnings)
    monkeypatch.setattr(wr, "MAX_ELEMENTS", 10)
    doc = read_write_svg(_doc(_pen("M0 0 l1 1") * 20))
    assert len(doc.pages[0].strokes) < 20 and any("more than 10 XML elements" in w for w in doc.warnings)
    monkeypatch.setattr(wr, "MAX_ELEMENTS", 2_000_000)
    monkeypatch.setattr(wr, "MAX_XML_BYTES", 1000)
    with pytest.raises(ValueError, match="inflates to more than"):
        read_write_svg(gzip.compress(b"<svg>" + b" " * 5000 + b"</svg>"))
    with pytest.raises(ValueError, match="larger than"):
        read_write_svg(b"<svg>" + b" " * 5000 + b"</svg>")
    monkeypatch.setattr(wr, "MAX_XML_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(wr, "MAX_TOTAL_POINTS", 25)
    monkeypatch.setattr(wr, "MAX_POINTS_PER_PATH", 10)
    monkeypatch.setattr(wr, "STROKE_COST", 0)  # vertices only (test_stroke_cost has the rest)
    doc = read_write_svg(_doc(_pen("M0 0" + " l1 1" * 30) + _pen("M0 0 l1 1 1 1") * 20))
    strokes = doc.pages[0].strokes
    assert len(strokes[0].points) == 10 and sum(len(s.points) for s in strokes) <= 25
    assert any(w.startswith("1 paths with more than") and w.endswith("points were shortened") for w in doc.warnings)
    assert any(w.startswith("15 shapes beyond the limit of") for w in doc.warnings)
    monkeypatch.setattr(wr, "MAX_PAGES", 3)
    doc = read_write_svg(_doc("", pages=5))
    assert len(doc.pages) == 3 and any("more than 3 pages" in w for w in doc.warnings)


def test_hostile_numbers_and_attributes() -> None:
    content = (_pen("M1e308 1e308 l1e308 1e308") + _pen("M0 0 l1 1", extra=' transform="scale(0)"') +
               _pen("M0 0 l1 1", extra=' transform="nonsense"') + _pen("M0 0 l1 1", width=1e12) +
               '<path fill="url(#gradient)" d="M0 0 h10 v10 z"/>'
               '<path fill="url(#gradient) #FF0000" d="M0 0 h10 v10 z"/>'
               '<path fill="none" stroke="#XYZ" d="M0 0 l1 1"/>'
               '<path fill="none" stroke="#000" clip-path="url(#c)" d="M0 0 l1 1"/>'
               '<use xlink:href="#x"/><foreignObject/><blink/>'
               '<text x="0" y="0" font-size="1e400">big</text>'
               f'<image x="1e300" y="0" width="1e300" height="1" xlink:href="{_png_uri()}"/>'
               '<path fill="none" stroke="#000" d="M0 0 L5 5 X"/>')
    doc = read_write_svg(_doc(content))
    page = doc.pages[0]
    assert all(abs(p.x) <= 1e7 and abs(p.y) <= 1e7 and p.width <= 1e4 for s in page.strokes for p in s.points)
    assert len(page.images) == 0
    for message in ("1 invalid transforms were ignored", "1 shapes painted with a gradient or pattern were skipped",
                    "1 unknown colours were ignored", "1 clipped or masked elements are shown unclipped",
                    "1 " + USE_SKIPPED,
                    "1 embedded HTML objects (foreignObject) were skipped", "1 unknown SVG elements were ignored",
                    "1 paths have damaged path data; the part before the damage was kept"):
        assert message in doc.warnings, message
    assert any(w.startswith("3 shapes, images or texts with invalid or out-of-range coordinates") for w in doc.warnings)
    for fmt in formats.writable():  # whatever was read, every writer can store
        assert fmt.write(doc, None)


def test_arithmetic_extremes_skip_only_the_shape() -> None:
    content = (_pen("M10 10 l50 50") +
               '<path fill="#000" d="M100 100 A1e-200 50 0 0 1 200 100"/>'
               '<circle r="1e-200" fill="#000"/><ellipse rx="1e-300" ry="1e-300" fill="#000"/>'
               '<rect width="10" height="10" rx="1e-200" fill="#000"/>'
               '<path fill="#000" transform="scale(1e6)" d="M0 0 C1e304 0 -1e304 0 1e304 0"/>'
               '<path class="write-stroke-pen" stroke="#000" fill="none" transform="scale(1e300)" '
               'd="M0 0 A1 1 0 0 1 1e10 0"/>')
    doc = read_write_svg(_doc(content))
    page = doc.pages[0]
    assert page.strokes[0].points[0].x == pytest.approx(10 * U)  # the pen stroke survives
    assert all(math.isfinite(p.x) and math.isfinite(p.y) for s in page.strokes for p in s.points)
    assert any(w.startswith("2 shapes, images or texts with invalid") for w in doc.warnings), doc.warnings


def test_element_handlers_turn_arithmetic_errors_into_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> None:
        raise ZeroDivisionError("float division by zero")

    monkeypatch.setattr(wr._Reader, "image", broken)
    doc = read_write_svg(_doc(_pen("M0 0 l1 1") + f'<image width="5" height="5" xlink:href="{_png_uri()}"/>'))
    assert len(doc.pages[0].strokes) == 1
    assert "1 shapes, images or texts with invalid or out-of-range coordinates were skipped" in doc.warnings


def test_closepath_floods_are_charged_to_the_file_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wr, "MAX_TOTAL_POINTS", 5000)
    monkeypatch.setattr(wr, "MAX_POINTS_PER_PATH", 2000)
    flood = '<path class="write-stroke-pen" stroke="#000" fill="none" d="M0 0' + "z" * 3000 + '"/>'
    doc = read_write_svg(_doc(flood * 50 + _pen("M0 0 l1 1")))
    # two paths spend the per-path budget each and are shortened; the third runs out of the
    # file's budget, so it, the other floods and the pen stroke after them are dropped
    assert any(w.startswith("49 shapes beyond the limit of") for w in doc.warnings), doc.warnings
    assert any(w.startswith("2 paths with more than") for w in doc.warnings), doc.warnings
    assert len(doc.pages[0].strokes) == 2


def test_lone_movetos_draw_nothing() -> None:
    # SVG draws nothing for a subpath that is only a moveto, not even a cap
    doc = read_write_svg(_doc(_pen("M50 50 l100 0 M300 200", 20) + _pen("M10 10") + _pen("M5 5 M6 6 M7 7 l1 0")
                              + '<polyline fill="none" stroke="#000" points="5 5"/>'
                              + '<path fill="#000" d="M0 0 M10 10 l10 0 0 10 z M50 50"/>'))
    strokes = doc.pages[0].strokes
    assert [(s.kind, _xy(s.points)) for s in strokes] == [
        ("pen", [(50 * U, 50 * U), (150 * U, 50 * U)]), ("pen", [(7 * U, 7 * U), (8 * U, 7 * U)]),
        ("fill", [(10 * U, 10 * U), (20 * U, 10 * U), (20 * U, 20 * U)])]
    assert strokes[2].outline is not None and len(strokes[2].outline) == 1
    assert doc.warnings == []
    # a closed or zero-length subpath draws round caps: a dot
    doc = read_write_svg(_doc(_pen("M10 10 Z") + _pen("M20 20 l0 0")))
    assert [len(s.points) for s in doc.pages[0].strokes] == [1, 2]


def test_every_stroke_costs_budget_units(monkeypatch: pytest.MonkeyPatch) -> None:
    # 10 000 lone movetos make no stroke and cost one unit each
    start = time.perf_counter()
    doc = read_write_svg(_doc(_pen("M0 0" * 10_000)))
    assert doc.pages[0].strokes == [] and doc.warnings == [] and time.perf_counter() - start < 1.0
    # tiny strokes: each costs its vertices plus STROKE_COST, beyond the file's budget they are dropped
    monkeypatch.setattr(wr, "MAX_TOTAL_POINTS", 1000)
    dots = _pen("M0 0h0" * 100)  # 100 strokes of 2 points: 200 units of vertices, 100 * 16 for the strokes
    doc = read_write_svg(_doc(dots + _pen("M0 0 l1 1")))
    kept = (1000 - 200) // wr.STROKE_COST
    assert len(doc.pages[0].strokes) == kept == 50
    assert any(w.startswith(f"{100 - kept} strokes and outlines beyond the limit of")
               for w in doc.warnings), doc.warnings
    assert any(w.startswith("1 shapes beyond the limit of") for w in doc.warnings), doc.warnings
    # a shape both filled and stroked makes its points twice and pays twice
    both = '<path fill="#000" stroke="#000" d="M0 0' + " l1 0 0 1 -1 0 z m2 0" * 30 + '"/>'
    doc = read_write_svg(_doc(both))
    strokes = doc.pages[0].strokes
    rings = len(strokes[0].outline or [])
    assert rings == len(strokes) - 1  # one ring and one stroke per kept subpath
    assert 2 * rings * (4 + 1 + wr.STROKE_COST) <= 1000 + 2 * (4 + 1 + wr.STROKE_COST)
    assert any("strokes and outlines beyond the limit" in w for w in doc.warnings), doc.warnings


def test_shortened_flat_pen_outline_is_not_paired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wr, "MAX_POINTS_PER_PATH", 100)
    left = [(10 + i, 20.0) for i in range(100)]
    right = [(10 + i, 24.0) for i in range(100)]
    d = "M" + " L".join(f"{x} {y}" for x, y in left + right[::-1])
    doc = read_write_svg(_doc(f'<path class="write-flat-pen" fill="#000" stroke-width="4" d="{d}"/>'))
    (stroke,) = doc.pages[0].strokes
    assert stroke.kind == "fill"  # not a centre line built from unrelated vertices
    assert any(w.startswith("1 paths with more than") for w in doc.warnings)
    assert any(w.startswith("1 variable-width strokes did not have Write's paired outline") for w in doc.warnings)


def test_svg_length_units_follow_the_unit_constant() -> None:
    assert wr.SVG_UNITS["in"] == pytest.approx(72.0 / PT_PER_UNIT)
    assert wr.SVG_UNITS["pt"] * PT_PER_UNIT == pytest.approx(1.0)
    assert wr.SVG_UNITS["cm"] == pytest.approx(10 * wr.SVG_UNITS["mm"])
    assert wr.SVG_UNITS["pc"] == pytest.approx(12 * wr.SVG_UNITS["pt"])
    doc = read_write_svg(_doc('<rect x="1in" y="72pt" width="2.54cm" height="6pc" fill="#000"/>'))
    xs = [p.x for p in doc.pages[0].strokes[0].points]
    ys = [p.y for p in doc.pages[0].strokes[0].points]
    assert (min(xs), min(ys), max(xs), max(ys)) == pytest.approx((72, 72, 144, 144))


# --------------------------------------------------------------------------- reader: Write's own files


def test_site_page_006_flat_pens_and_links(samples) -> None:
    doc = read_write_svg(samples.pinned_file("write-site1_page006.svg").read_bytes())
    assert len(doc.pages) == 1 and doc.title == ""
    page = doc.pages[0]
    assert (page.width, page.height) == pytest.approx((432.0, 432.0)) and page.paper == "lined"
    pens = [s.pen for s in page.strokes]
    assert (pens.count("ballpoint"), pens.count(None), pens.count("fountain")) == (379, 81, 33)
    assert all(s.kind == "pen" for s in page.strokes) and len(page.strokes) == 493
    assert sum(len(s.points) for s in page.strokes) == 10534
    first = page.strokes[0]  # M252.843 42.281, stroke-width 1.75, black
    assert (first.points[0].x, first.points[0].y) == pytest.approx((252.843 * U, 42.281 * U))
    assert len(first.points) == 96 and first.width == pytest.approx(1.75 * U) and first.color == (0, 0, 0, 1)
    second = page.strokes[1]  # transform="translate(0,-80)" on M465.063 507.063
    assert (second.points[0].x, second.points[0].y) == pytest.approx((465.063 * U, 427.063 * U))
    flat = next(s for s in page.strokes if s.pen == "fountain")
    assert len(flat.points) == 33 and flat.width == pytest.approx(1.2 * U) and flat.color == (0, 0, 1, 1)
    assert (flat.points[0].x, flat.points[0].y) == pytest.approx((250.10952, 99.62664))
    assert flat.points[0].width == pytest.approx(0.0593688, abs=1e-6)
    assert max(p.width for p in flat.points) == pytest.approx(0.4710974, abs=1e-6)  # below the nominal 0.576
    assert doc.warnings == ["15 hand-drawn links were converted as plain ink (their link targets are dropped)"]
    # rulecolor #7F0000FF: blue rules at half opacity over white, every 40 units, no margin
    ops = _paper_ops(doc.pdfs[page.background.pdf_id])
    rules = _rects(ops, "0.502 0.502 1")
    assert len(rules) == 22 and len(ops) == 1 + 1 + 22 + 1
    assert sorted(432 - (y + h / 2) for x, y, w, h in rules)[:2] == pytest.approx([40 * U, 80 * U], abs=2e-3)


def test_site_page_007_images(samples) -> None:
    doc = read_write_svg(samples.pinned_file("write-site1_page007.svg").read_bytes())
    page = doc.pages[0]
    assert (page.width, page.height) == pytest.approx((432.0, 201.6))
    assert len(page.strokes) == 159 and sum(len(s.points) for s in page.strokes) == 2725
    assert all(s.pen == "ballpoint" for s in page.strokes)
    assert [(i.fmt, len(i.data)) for i in page.images] == [("jpeg", 51865), ("jpeg", 64310), ("jpeg", 81550)]
    boxes = [(i.x, i.y, i.w, i.h) for i in page.images]
    assert boxes[0] == pytest.approx((3.7147, 2.5987, 69.4835, 150.6871), abs=1e-4)
    assert boxes[2] == pytest.approx((144.8077, 2.4396, 69.6366, 151.2288), abs=1e-4)
    assert all(i.rotation == 0.0 for i in page.images)


def test_inkterop_write_flavoured_fixture(samples) -> None:
    doc = read_write_svg(samples.inkterop_fixture("svg", "write-mini.svg").read_bytes())
    assert len(doc.pages) == 2
    # page 2 says xruling = yruling = 35, but its g.ruleline is custom (no write-std-ruling) and
    # holds only the page rectangle: Write shows what the ruling holds, a plain page
    assert [(p.width, p.height, p.paper) for p in doc.pages] == [(768 * U, 1050 * U, "lined"),
                                                                 (768 * U, 1050 * U, "plain")]
    a, b = doc.pages[0].strokes
    assert _xy(a.points) == _xy([Point(48, 48), Point(52.8, 38.4), Point(57.6, 48)]) and a.width == 3 * U
    assert _xy(b.points) == _xy([Point(72, 48), Point(72, 33.6)]) and b.color == (2 / 3, 0, 0, 1)
    (h,) = doc.pages[1].strokes
    assert h.kind == "highlighter" and h.color == (1, 1, 0, 1) and h.width == pytest.approx(12 * U)
    assert doc.warnings == ["1 hand-drawn links were converted as plain ink (their link targets are dropped)"]


# --------------------------------------------------------------------------- writer


def _parse_written(data: bytes) -> ET.Element:
    assert data[:2] == b"\x1f\x8b" and data[4:8] == b"\0\0\0\0"  # gzip, no time stamp
    return ET.fromstring(gzip.decompress(data))


def test_writer_skeleton() -> None:
    doc = Document(title="Skeleton", pages=[Page(368.64, 504.0, paper="lined"), Page(595.0, 842.0, paper="grid"),
                                            Page(300.0, 300.0, paper="dotted"), Page(200.0, 100.0)])
    doc.pages[0].strokes.append(Stroke([Point(10, 10, 1.0), Point(20, 30, 1.0)], width=1.0))
    data = write_write_svg(doc)
    assert data == write_write_svg(doc)  # deterministic
    root = _parse_written(data)
    ns = "{http://www.w3.org/2000/svg}"
    assert root.tag == ns + "svg" and root.get("id") == "write-document"
    assert root[0].get("id") == "write-doc-background" and root[1].get("id") == "write-defs"
    config = root[1][0]
    assert config.get("type") == "text/writeconfig"
    assert {(c.get("name"), c.get("value")) for c in config} >= {("docFormatVersion", "2"), ("pageNum", "0")}
    pages = root.findall(ns + "svg")
    assert [p.get("class") for p in pages] == ["write-page"] * 4
    assert [(p.get("x"), p.get("y"), p.get("width"), p.get("height")) for p in pages] == [
        ("10", "10", "768px", "1050px"), ("10", "1080", "1239.583px", "1754.167px"),
        ("10", "2854.167", "625px", "625px"), ("10", "3499.167", "416.667px", "208.333px")]
    content = pages[0][0]
    assert content.get("class") == "write-content write-v3"
    assert {k: content.get(k) for k in ("width", "height", "xruling", "yruling", "marginLeft", "papercolor",
                                        "rulecolor")} == {
        "width": "768", "height": "1050", "xruling": "0", "yruling": "40", "marginLeft": "0",
        "papercolor": "#FFFFFF", "rulecolor": "#FF0000FF"}
    ruleline = content[0]
    assert ruleline.get("class") == "ruleline write-std-ruling write-scale-down"
    assert ruleline[0].get("class") == "pagerect" and ruleline[1].get("class") == "yrule_1"
    assert ruleline[1].get("d") == "M0 40 l768 0" and len(ruleline) == 1 + 26
    stroke = content[1]
    assert stroke.get("class") == "write-stroke-pen" and stroke.get("stroke-linecap") == "round"
    assert stroke.get("d") == "M20.833 20.833 l20.834 41.667" and stroke.get("stroke-width") == "2.083"
    grid = pages[1][0]
    assert (grid.get("xruling"), grid.get("yruling")) == ("25", "25")
    assert any(el.get("d", "").startswith("M25 0 l0") for el in grid[0])
    dots = pages[2][0][0]
    assert "write-std-ruling" not in dots.get("class") and dots.find(ns + "pattern") is not None
    assert pages[3][0].get("yruling") == "0" and len(pages[3][0][0]) == 1  # plain: the page rect only
    assert doc.warnings == []


def test_variable_width_strokes_are_paired_outlines() -> None:
    page = Page(400, 400)
    page.strokes.append(Stroke([Point(10, 10, 1.0), Point(20, 12, 2.0), Point(20, 12, 2.5), Point(30, 20, 0.5)],
                               width=1.5))
    root = _parse_written(write_write_svg(Document(pages=[page])))
    path = root.find(".//{http://www.w3.org/2000/svg}path[@class='write-flat-pen']")
    assert path is not None and path.get("stroke") is None and path.get("fill") == "#000000"
    assert path.get("stroke-width") == "3.125"  # nominal 1.5 pt
    data = svgpath.parse_path(path.get("d"))
    assert len(data.subpaths) == 1 and len(data.subpaths[0].points) == 8 and not data.subpaths[0].closed
    back = read_write_svg(write_write_svg(Document(pages=[page])))
    got = back.pages[0].strokes[0]
    assert len(got.points) == 4
    for a, b in zip(got.points, page.strokes[0].points):
        assert abs(a.x - b.x) < 1e-3 and abs(a.y - b.y) < 1e-3 and abs(a.width - b.width) < 2e-3
    flat = read_write_svg(write_write_svg(Document(pages=[page]), Options(pressure=False)))
    assert flat.pages[0].strokes[0].pen == "ballpoint"
    assert len({round(p.width, 6) for p in flat.pages[0].strokes[0].points}) == 1


def test_round_trip_of_every_element() -> None:
    doc = full_document()
    original = full_document()
    back = read_write_svg(write_write_svg(doc))
    assert len(back.pages) == 4
    for a, b in zip(original.pages, back.pages):
        assert (round(a.width, 2), round(a.height, 2)) == (round(b.width, 2), round(b.height, 2))
    assert [p.paper for p in back.pages] == ["lined", "plain", "plain", "grid"]
    ink = [s for s in back.pages[0].strokes if s.kind != "fill"]
    fills = [s for s in back.pages[0].strokes if s.kind == "fill"]
    assert [s.kind for s in ink] == ["pen", "pen", "highlighter", "pen", "pen", "pen", "pen"]
    assert len(fills) == 2
    pressure, constant, highlighter, bezier, dot, pencil, outline = ink
    src = original.pages[0].strokes
    for got, want in ((pressure, src[0]), (constant, src[1]), (pencil, src[5]), (outline, src[6])):
        assert len(got.points) == len(want.points)
        for a, b in zip(got.points, want.points):
            assert abs(a.x - b.x) < 1e-3 and abs(a.y - b.y) < 1e-3 and abs(a.width - b.width) < 2e-3
        assert got.color == pytest.approx(want.color, abs=1 / 255)
    assert pressure.pen == "fountain" and constant.pen == "ballpoint"
    assert highlighter.color == pytest.approx((1.0, 0.9, 0.0, 0.5), abs=1 / 255)
    assert bezier.points[0].x == pytest.approx(250, abs=1e-3) and bezier.points[-1].x == pytest.approx(350, abs=1e-3)
    assert len(bezier.points) > 10 and bezier.controls is None  # flattened
    assert len(dot.points) == 2 and dot.points[0].x == dot.points[1].x
    assert fills[0].color == pytest.approx((0.2, 0.4, 0.9, 0.25), abs=1 / 255)
    assert [v for p in fills[1].outline[0] for v in (p.x, p.y)] == pytest.approx(
        [v for p in original.pages[0].strokes[8].outline[0] for v in (p.x, p.y)], abs=1e-3)
    images = back.pages[0].images
    assert [i.fmt for i in images] == ["png", "jpeg"]  # the PDF sticker cannot be written
    assert (images[0].x, images[0].y, images[0].w, images[0].h) == pytest.approx((40, 400, 60, 40), abs=1e-3)
    assert (images[1].x, images[1].y, images[1].w, images[1].h, images[1].rotation) == pytest.approx(
        (150, 400, 64, 48, 90.0), abs=1e-3)
    assert images[1].data == JPEG
    t1, t2 = back.pages[0].texts
    assert t1.text == "Hello <ink> & text" and (t1.x, t1.y) == pytest.approx((40, 480), abs=1e-3)
    assert t1.size == pytest.approx(14.0, abs=1e-3)
    assert t2.text == "Turned\nand bold" and t2.align == "center" and t2.rotation == pytest.approx(30.0)
    assert all(r.bold for r in t2.runs) and t2.color == pytest.approx((0.8, 0, 0, 1), abs=1 / 255)
    assert back.pages[1].strokes and not back.pages[2].strokes
    for message in ("2 pages show a PDF background, which a Write file cannot hold without converting the PDF "
                    "(gnnote has no PDF rasteriser); only the ink and the paper style were written",
                    "1 highlighter strokes were written as semi-transparent pen strokes (Write's own highlighter "
                    "encoding is not known)",
                    "1 pencil strokes were written as Write pen strokes",
                    "2 text boxes were written as SVG text, which Write displays but cannot edit",
                    "1 images are neither PNG nor JPEG and were skipped"):
        assert message in doc.warnings, message


def test_translucent_pens_and_highlighters_keep_their_kind() -> None:
    page = Page(400, 400, strokes=[
        Stroke([Point(10, 10, 2), Point(100, 50, 2)], color=(1, 0, 0, 0.5), kind="pen", width=2),
        Stroke([Point(10, 60, 2), Point(100, 90, 2.5), Point(150, 90, 3)], color=(0, 0, 1, 0.4), kind="pen",
               width=2),
        Stroke([Point(10, 120, 8), Point(100, 120, 8)], color=(1, 1, 0, 1.0), kind="highlighter", width=8),
        Stroke([Point(10, 140, 8), Point(100, 140, 9)], color=(1, 1, 0, 0.3), kind="highlighter", width=8),
        Stroke([Point(10, 160, 2), Point(100, 160, 2)], color=(0, 0, 0, 0.8), kind="pen", width=2),
        Stroke([Point(10, 180, 2), Point(100, 180, 2)], color=(0, 0, 0, 1.0), kind="pen", width=2)])
    svg = build_svg(Document(pages=[page]))
    assert svg.count('data-gnnote-kind="pen"') == 2 and svg.count('data-gnnote-kind="highlighter"') == 2
    back = read_write_svg(write_write_svg(Document(pages=[page])))
    strokes = back.pages[0].strokes
    assert [s.kind for s in strokes] == ["pen", "pen", "highlighter", "highlighter", "pen", "pen"]
    assert [s.color[3] for s in strokes] == pytest.approx([0.5, 0.4, 0.5, 0.3, 0.8, 1.0])
    assert [s.pen for s in strokes] == ["ballpoint", "fountain", "ballpoint", "fountain", "ballpoint", "ballpoint"]
    # the mark wins over the opacity rule and over a highlight class; unmarked ink keeps the rule
    marked = (_pen("M0 0 l9 0", extra=' stroke-opacity="0.2" data-gnnote-kind="pen"') +
              _pen("M0 9 l9 0", extra=' data-gnnote-kind="highlighter"') +
              _pen("M0 18 l9 0", extra=' stroke-opacity="0.2"') +
              _pen("M0 27 l9 0", extra=' stroke-opacity="0.2" data-gnnote-kind="other"'))
    assert [s.kind for s in read_write_svg(_doc(marked)).pages[0].strokes] == [
        "pen", "highlighter", "highlighter", "highlighter"]


def test_round_trip_of_write_samples_is_tight(samples) -> None:
    for path in (samples.pinned_file("write-site1_page006.svg"), samples.pinned_file("write-site1_page007.svg"),
                 samples.inkterop_fixture("svg", "write-mini.svg")):
        doc = read_write_svg(path.read_bytes())
        back = read_write_svg(write_write_svg(doc))
        assert len(back.pages) == len(doc.pages), path.name
        for a, b in zip(doc.pages, back.pages):
            assert (round(a.width, 3), round(a.height, 3)) == (round(b.width, 3), round(b.height, 3))
            # every pen stroke is written as Write's constant-width pen, unclassed ones included
            assert [(s.kind, s.pen or "ballpoint", len(s.points)) for s in a.strokes] == \
                [(s.kind, s.pen, len(s.points)) for s in b.strokes], path.name
            for s, t in zip(a.strokes, b.strokes):
                # an opaque highlighter is written half transparent
                want = s.color[:3] + (0.5,) if s.kind == "highlighter" and s.color[3] == 1 else s.color
                assert t.color == pytest.approx(want, abs=1 / 255)
                for p, q in zip(s.points, t.points):
                    assert abs(p.x - q.x) < 5e-4 and abs(p.y - q.y) < 5e-4 and abs(p.width - q.width) < 1e-3
            assert [v for i in a.images for v in (i.x, i.y, i.w, i.h)] == pytest.approx(
                [v for i in b.images for v in (i.x, i.y, i.w, i.h)], abs=1e-3)
            assert [i.data for i in a.images] == [i.data for i in b.images]


def test_writer_edge_cases() -> None:
    doc = Document()
    back = read_write_svg(write_write_svg(doc))
    assert len(back.pages) == 1 and "The document has no pages; one empty page was written" in doc.warnings
    page = Page(float("nan"), -5)
    page.strokes = [Stroke([Point(float("inf"), 0, 1.0)]), Stroke([]), Stroke([Point(1e9, 0, 1), Point(0, 0, 1)]),
                    Stroke([Point(0, 0, 1), Point(5, 5, 1)], color=(2.0, -1.0, 0.5, 1.0)),
                    Stroke([Point(0, 0, 0)], kind="fill", outline=[[Point(0, 0, 0)]])]
    page.images = [Image(0, 0, 10, 10, b""), Image(0, 0, -1, 10, png()), Image(0, 0, 10, 10, b"GIF89a....")]
    page.texts = [TextBox(0, 0, 10, 10, "  "), TextBox(float("nan"), 0, 10, 10, "x"),
                  TextBox(0, 0, 100, 10, "tab\tand\r\nlines", runs=[])]
    doc = Document(pages=[page])
    back = read_write_svg(write_write_svg(doc))
    strokes = back.pages[0].strokes
    assert len(strokes) == 1 and strokes[0].color == (1.0, 0.0, 128 / 255, 1.0)
    assert back.pages[0].texts[0].text == "tab    and\nlines"
    assert "1 pages had no valid size; Write's default page size was used" in doc.warnings
    # the infinite and the far-away stroke, the one-point fill, the negative image, the NaN text box
    assert any(w.startswith("5 strokes, images or text boxes with invalid coordinates") for w in doc.warnings)
    assert "1 empty text boxes were skipped" in doc.warnings and "1 images without data were skipped" in doc.warnings
    # characters XML cannot hold are dropped, markup is escaped
    page = Page(200, 200)
    page.texts = [TextBox(10, 10, 100, 20, "a\x00b<c>&\ud800", runs=[TextRun("a\x00b<c>&\ud800", font='Sans"X')])]
    back = read_write_svg(write_write_svg(Document(pages=[page])))
    assert back.pages[0].texts[0].text == "ab<c>&" and back.pages[0].texts[0].runs[0].font == 'Sans"X'


def test_long_text_is_wrapped_into_the_box() -> None:
    words = "word " * 40
    page = Page(400, 400)
    page.texts = [TextBox(10, 10, 120, 100, words.strip(), runs=[TextRun(words.strip(), size=12.0)], size=12.0)]
    svg = build_svg(Document(pages=[page]))
    assert svg.count("<tspan") > 3
    back = read_write_svg(write_write_svg(Document(pages=[page])))
    lines = back.pages[0].texts[0].text.split("\n")
    assert len(lines) > 3 and " ".join(lines) == words.strip()


def test_empty_text_lines_survive_and_every_line_has_its_own_baseline() -> None:
    for text in ("first\n\nthird", "a\n\n\nb", "a\nb\n\nc", "x\n  \ny", "one\n\n\n\ntwo\nthree"):
        page = Page(400, 400, texts=[TextBox(50, 50, 200, 100, text, runs=[TextRun(text, size=12)], size=12)])
        svg = build_svg(Document(pages=[page]))
        assert "dy=" not in svg and "></tspan>" not in svg  # no empty tspans, absolute y only
        back = read_write_svg(write_write_svg(Document(pages=[page])))
        (box,) = back.pages[0].texts
        assert box.text == text and (box.x, box.y) == pytest.approx((50, 50), abs=1e-3)
    page = Page(400, 400, texts=[TextBox(50, 50, 200, 100, "a\n\nb", runs=[TextRun("a\n\nb", size=12)], size=12)])
    root = _parse_written(write_write_svg(Document(pages=[page])))
    ys = [float(t.get("y")) for t in root.iter("{http://www.w3.org/2000/svg}tspan") if t.get("y")]
    size_u = 12 / U
    assert ys == pytest.approx([50 / U + wr.TEXT_ASCENT * size_u, 50 / U + (wr.TEXT_ASCENT + 2 * wr.TEXT_LINE) * size_u],
                               abs=1e-3)


def test_wrapping_long_text_takes_linear_time() -> None:
    from gnnote.styluswrite.writer import _wrap

    text = "word " * 200_000
    start = time.perf_counter()
    lines = _wrap([TextRun(text, size=12.0)], 0.0)
    assert time.perf_counter() - start < 5.0  # the old in-place concatenation took minutes here
    assert len(lines) == 1 and lines[0][0].text == text
    lines = _wrap([TextRun("ab " * 1000, size=12.0), TextRun("cd " * 1000, size=12.0, bold=True)], 100.0)
    assert len(lines) > 10 and all(line for line in lines)
    assert "".join(r.text for line in lines for r in line).replace(" ", "") == ("ab" * 1000 + "cd" * 1000)
    # spaces before a soft break go, even when they come from several runs
    lines = _wrap([TextRun("aaaa  ", size=10.0), TextRun("  ", size=10.0, bold=True), TextRun("bbbb", size=10.0)],
                  25.0)
    assert [[r.text for r in line] for line in lines] == [["aaaa"], ["bbbb"]]
    # empty lines inside stay, white space lines at the end go, runs of one style are joined
    lines = _wrap([TextRun("a \n\n \nb", size=10.0), TextRun("c ", size=10.0), TextRun(" \n \n\xa0", size=10.0)], 0)
    assert [[r.text for r in line] for line in lines] == [["a "], [], [" "], ["bc  "]]
    assert _wrap([TextRun(" \n ", size=10.0)], 0) == [[TextRun(" ", size=10.0)]] and _wrap([], 0) == [[]]


def test_long_text_is_written_in_linear_memory_within_a_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.styluswrite import writer as ww

    # every word was kept as an object until the whole box was wrapped: 30 times the text in
    # memory (a 117 kB .xopp with a 117 MB text ran out of 4 GB)
    for text, width, lines in (("12 " * 100_000, 300.0, 5_000), ("12 " * 100_000, 0.0, 1),
                                ("a\n" * 100_000, 300.0, 100_000)):
        assert sum(1 for _ in ww._wrap_lines([TextRun(text, size=12.0)], width)) == lines
        if lines < 100_000:
            doc = Document(pages=[Page(612, 792, texts=[TextBox(72, 72, width, 80, text,
                                                                runs=[TextRun(text, size=12.0)], size=12.0)])])
            _, peak = _peak(lambda: write_write_svg(doc))
            assert peak < 12 * len(text), (width, peak / len(text))  # 28 to 33 times before
    # a file holds at most MAX_TEXT_CHARS characters of text, each line costing LINE_COST more
    monkeypatch.setattr(ww, "MAX_TEXT_CHARS", 1000)
    boxes = [TextBox(72, 72 + 100 * i, 300, 80, "word " * 100, runs=[TextRun("word " * 100, size=12.0)], size=12.0)
             for i in range(4)]
    doc = Document(pages=[Page(612, 792, texts=boxes)])
    back = read_write_svg(write_write_svg(doc))
    kept = [t.text.replace("\n", " ") for t in back.pages[0].texts]  # soft wraps are line breaks in Write
    assert len(kept) == 2 and kept[0] == "word " * 100  # the second box is cut short, the others skipped
    assert 0 < len(kept[1]) < 1000 - 500 and ("word " * 100).startswith(kept[1])
    assert any("3 text boxes were cut short or skipped" in w for w in doc.warnings)
    one_line = Document(pages=[Page(612, 792, texts=[TextBox(72, 72, 0, 20, "x" * 5000,
                                                             runs=[TextRun("x" * 5000, size=12.0)], size=12.0)])])
    (box,) = read_write_svg(write_write_svg(one_line)).pages[0].texts
    assert box.text == "x" * (1000 - ww.LINE_COST)
    assert any("1 text boxes were cut short" in w for w in one_line.warnings)


def test_written_rule_lines_are_bounded() -> None:
    from gnnote.styluswrite import writer as ww

    # explicit rule lines had no file-wide limit: 1000 grid pages of 60 000 pt made 322 MB of
    # SVG (17 s, 1.5 GB) that the reader then refused
    limited = ("pages were written without explicit rule lines (more than 2000 per direction on a page, or 200000 "
               "per file); their ruling is kept only in the Page Setup attributes xruling and yruling, which gnnote "
               "reads back but plain SVG viewers ignore")
    doc = Document(pages=[Page(60000.0, 60000.0, paper="grid") for _ in range(1000)])
    start = time.perf_counter()
    data = write_write_svg(doc)
    assert time.perf_counter() - start < 1.0
    assert len(data) < 20_000 and gzip.decompress(data).count(b"<path") == 0
    assert doc.warnings == ["1000 " + limited]
    back = read_write_svg(data)
    assert len(back.pages) == 1000 and {p.paper for p in back.pages} == {"grid"}
    # pages within the per-page limit get all their rules until the file's budget is spent
    doc = Document(pages=[Page(1500.0, 1500.0, paper="grid") for _ in range(1000)])
    data = write_write_svg(doc)
    pages = gzip.decompress(data).decode().split('<svg class="write-page"')[1:]
    per_page = 2 * (int(1500.0 / U / ww.GRID_PITCH) - (1500.0 / U % ww.GRID_PITCH == 0))
    counts = [page.count("<path") for page in pages]
    full = ww.MAX_TOTAL_RULES // per_page
    assert counts == [per_page] * full + [0] * (1000 - full)
    assert doc.warnings == [f"{1000 - full} " + limited]
    back = read_write_svg(data)
    assert {p.paper for p in back.pages} == {"grid"} and back.warnings == []
    # an ordinary lined page keeps every rule
    doc = Document(pages=[Page(612.0, 792.0, paper="lined")])
    svg = build_svg(doc)
    assert svg.count("<path") == int(792.0 / U / ww.LINED_PITCH) and doc.warnings == []


def test_very_long_strokes_are_split_into_joined_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.styluswrite import writer as ww

    monkeypatch.setattr(ww, "MAX_PATH_POINTS", 100)
    pressure = [Point(10 + i * 0.5, 50 + (i % 7), 1.0 + 0.5 * (i % 2)) for i in range(250)]
    constant = [Point(10 + i * 0.5, 150, 2.0) for i in range(250)]
    doc = Document(pages=[Page(400, 400, strokes=[Stroke(pressure, width=1.2), Stroke(constant, width=2.0)])])
    data = write_write_svg(doc)
    assert any(w.startswith("2 strokes longer than") and w.endswith("were split into several Write strokes")
               for w in doc.warnings)
    back = read_write_svg(data).pages[0].strokes
    assert len(back) == 6 and [len(s.points) for s in back] == [100, 100, 52, 100, 100, 52]
    for a, b in ((back[0], back[1]), (back[1], back[2]), (back[3], back[4])):  # the pieces join
        assert (a.points[-1].x, a.points[-1].y) == pytest.approx((b.points[0].x, b.points[0].y), abs=1e-3)
    joined = back[0].points + back[1].points[1:] + back[2].points[1:]
    assert [v for p in joined for v in (p.x, p.y, p.width)] == pytest.approx(
        [v for p in pressure for v in (p.x, p.y, p.width)], abs=1e-3)
    assert max(p.width for s in back[:3] for p in s.points) == pytest.approx(1.5, abs=1e-3)


def test_font_names_become_css_families() -> None:
    from gnnote.styluswrite.writer import _family

    assert _family("HelveticaNeue-Bold") == ("HelveticaNeue", True, False)
    assert _family("Arial Bold Italic") == ("Arial", True, True)
    assert _family("Times-Roman") == ("Times", False, False)
    assert _family("Facebook") == ("Facebook", False, False)  # no separator, no style word
    assert _family("") == (None, False, False)
    page = Page(300, 300)
    page.texts = [TextBox(10, 10, 200, 20, "ab", runs=[TextRun("a", font="Avenir-Heavy"),
                                                      TextRun("b", font="Avenir-Black-Italic")])]
    svg = build_svg(Document(pages=[page]))
    assert 'font-family="Avenir-Heavy, sans-serif"' in svg and 'font-style="italic"' in svg


def test_paper_pdf_background_of_stock_paper_is_not_reported() -> None:
    page = Page(300, 300, background=PdfBackground("paper", 0), template_is_builtin=True, paper="grid")
    doc = Document(pages=[page], pdfs={"paper": b"%PDF-1.4"})
    svg = build_svg(doc)
    assert 'xruling="25"' in svg and doc.warnings == []
    doc = Document(pages=[page], pdfs={"paper": b"%PDF-1.4"})
    build_svg(doc, Options(paper="pdf"))
    assert doc.warnings and doc.warnings[0].startswith("1 pages show a PDF background")


# --------------------------------------------------------------------------- conversions


def _write_samples(samples) -> List[Tuple[str, bytes]]:
    return [(name, samples.pinned_file(name).read_bytes()) for name in ("write-site1_page006.svg",
                                                                         "write-site1_page007.svg")] + \
        [("write-mini.svg", samples.inkterop_fixture("svg", "write-mini.svg").read_bytes())]


def test_samples_convert_to_notability_goodnotes_and_pdf(samples) -> None:
    for name, data in _write_samples(samples):
        doc = read_write_svg(data)
        strokes = [len(p.strokes) for p in doc.pages]
        images = [len(p.images) for p in doc.pages]
        note = convert(data, name, Options(target="notability"))
        assert note.source_format == "styluswrite" and note.filename.endswith(".note")
        back = read_note(note.data)
        assert len(back.pages) == len(doc.pages)
        assert sum(len(p.strokes) for p in back.pages) == sum(strokes), name
        assert sum(len(p.images) for p in back.pages) == sum(images), name
        gn = convert(data, name, Options(target="goodnotes"))
        back = read_goodnotes(gn.data)
        assert [len(p.strokes) for p in back.pages] == strokes, name
        assert [(round(p.width, 1), round(p.height, 1)) for p in back.pages] == \
            [(round(p.width, 1), round(p.height, 1)) for p in doc.pages], name
        pdf = convert(data, name, Options(target="pdf", pdf_ink="annotations"))
        back = read_pdf(pdf.data)
        assert [len(p.strokes) for p in back.pages] == strokes, name
        first = doc.pages[0].strokes[0].points[0]
        got = back.pages[0].strokes[0].points[0]
        assert (got.x, got.y) == pytest.approx((first.x, first.y), abs=0.05), name


def test_other_apps_convert_to_write_and_back(samples) -> None:
    note = samples.notability_template().read_bytes()
    result = convert(note, "example.note", Options(target="styluswrite"))
    assert result.filename == "example.svgz" and result.target_format == "styluswrite"
    assert detect_format(result.filename, result.data) == "styluswrite"
    original = read_note(note)
    back = read_write_svg(result.data)
    assert [len(p.strokes) for p in back.pages] == [len(p.strokes) for p in original.pages]
    a = original.pages[0].strokes[0]
    b = back.pages[0].strokes[0]
    assert (b.points[0].x, b.points[0].y) == pytest.approx((a.points[0].x, a.points[0].y), abs=1e-3)


# --------------------------------------------------------------------------- registry, sniffing, CLI


def test_registry_entry() -> None:
    fmt = formats.get("styluswrite")
    assert (fmt.name, fmt.extension, fmt.input_extensions) == ("Stylus Labs Write", ".svgz", (".svgz", ".svg"))
    assert fmt.readable and fmt.writable
    ids = list(formats.FORMATS)
    assert ids.index("styluswrite") == ids.index("excalidraw") + 1
    assert formats.default_target("styluswrite") == "notability"
    Options(target="styluswrite").validate()


def test_sniffer_on_synthetic_input() -> None:
    sniff = formats.get("styluswrite").sniff
    write3 = _doc("")
    assert sniff(write3, None) and sniff(gzip.compress(write3), None) and sniff(zlib.compress(write3), None)
    assert sniff(b"\xef\xbb\xbf\n<?xml version='1.0'?>" + write3, None)
    assert sniff(b"<svg class='write-page' width='9px'>", None)
    assert sniff(b'<svg><g class="foo write-content write-v3">', None)
    assert sniff(b"<html><head><script type='text/writeconfig'>", None)
    assert sniff(b"<svg width='768px'><g id='page_1' width='768.000' xruling='0.000'>", None)
    for other in (b"<svg xmlns='http://www.w3.org/2000/svg'><path d='M0 0'/></svg>",
                  b"<svg><g id='page_1'/></svg>",
                  b"# docs\n<svg id=\"write-document\">", b"class='write-page'", b"\x1f\x8bnot gzip",
                  b"x^junk", b"", b"<xournal><layer/></xournal>"):
        assert not sniff(other, None), other
    assert not sniff(write3, ["content.xml"])  # a ZIP is never a Write document
    assert detect_format("x.svg", b"<svg/>") == "styluswrite"  # by extension, then the reader explains
    with pytest.raises(ValueError, match="not a Stylus Labs Write document"):
        convert(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "drawing.svg", Options(target="pdf"))


def _sample_files(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)


def test_sniffers_keep_to_their_own_samples(samples) -> None:
    """The Write sniffer claims only Write files among all sample files, and no other format's
    sniffer claims a Write file."""
    write = formats.get("styluswrite")
    known = _write_samples(samples)  # skips when the Write samples are not available
    expected = {"write-site1_page006.svg", "write-site1_page007.svg", "write-mini.svg"}
    claimed = set()
    for path in _sample_files(samples.root):
        with path.open("rb") as fh:
            head = fh.read(256 * 1024)
        if write.sniff(head, None):
            claimed.add(path.name)
    assert claimed == expected
    for name, data in known + [("written.svgz", write_write_svg(full_document()))]:
        names = formats.sniff_zip_names(data)
        for fmt in formats.readable():
            if fmt.id != "styluswrite":
                assert not fmt.sniff(data, names), (fmt.id, name)
        assert detect_format("renamed.bin", data) == "styluswrite", name


def test_xournalpp_files_are_not_write_files(samples) -> None:
    write = formats.get("styluswrite")
    for path in samples.xournalpp_files():
        assert not write.sniff(path.read_bytes(), None), path.name


def test_cli_convert_and_info(tmp_path: Path, samples, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "Download.svg"
    src.write_bytes(samples.pinned_file("write-site1_page006.svg").read_bytes())
    assert main(["convert", str(src), "--to", "goodnotes"]) == 0
    out = capsys.readouterr().out
    assert "styluswrite -> goodnotes" in out and "493 strokes" in out
    assert len(read_goodnotes((tmp_path / "Download.goodnotes").read_bytes()).pages) == 1
    assert main(["convert", str(tmp_path / "Download.goodnotes"), "--to", "styluswrite"]) == 0
    written = tmp_path / "Download.svgz"
    assert sum(len(p.strokes) for p in read_write_svg(written.read_bytes()).pages) == 493
    assert main(["info", str(written)]) == 0
    assert "Format:  styluswrite" in capsys.readouterr().out
    assert main(["formats"]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("styluswrite"))
    assert "Stylus Labs Write" in line and ".svgz" in line
