"""SVG geometry for the Stylus Labs Write codec: path data, transform lists, affine matrices.

Path data follows the SVG 1.1 grammar (section 8.3): every command ``M L H V Z C S Q T A`` in
absolute and relative form, implicit command repetition (extra pairs after a moveto are
linetos), and the number grammar with signs, fractions, exponents and packed numbers
(``"0.5.5"`` is ``0.5 0.5``, ``"-1-2"`` is ``-1 -2``; arc flags may be packed too, as in
``"a1 1 0 0150 50"``).  Curves and arcs are flattened to polylines within a caller-chosen
tolerance; pen ink in Write files uses only ``M`` and ``l`` and comes out vertex for vertex.

As the SVG specification asks for path data errors, parsing stops at the first error and keeps
what was parsed before it (:attr:`PathData.error` says why).  A subpath that is only a moveto
draws nothing and is left out.  The work one call may do is bounded: every vertex and every
command that adds none (``Z``, an arc to its own start) costs one unit of ``max_points``
(:attr:`PathData.cost`, :attr:`PathData.truncated`), and one curve or arc is cut into at most
:data:`MAX_CURVE_SEGMENTS` segments, so hostile input cannot cost unbounded memory or time.  Extreme numbers (overflowing, underflowing or infinite) never raise:
arcs whose radii are too small or too large to compute become straight lines.

Everything here is written from the SVG 1.1 specification (paths, the arc implementation notes
of appendix F.6 and coordinate system transformations); no code was taken from elsewhere.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

__all__ = ["Matrix", "IDENTITY", "multiply", "apply", "scale_of", "translate", "parse_transform",
           "Subpath", "PathData", "parse_path", "parse_numbers", "MAX_CURVE_SEGMENTS", "MAX_TRANSFORMS"]

XY = Tuple[float, float]
Matrix = Tuple[float, float, float, float, float, float]  # a b c d e f: x' = a x + c y + e, y' = b x + d y + f
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

MAX_CURVE_SEGMENTS = 256  # one curve or arc becomes at most this many line segments
MAX_TRANSFORMS = 64  # transform functions in one attribute
MIN_TOLERANCE = 1e-6
ARC_RATIO_LIMIT = 1e9  # radii beyond this multiple of the chord (or ellipses flatter than its inverse) draw a line

_NUMBER = re.compile(r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")
_SKIP = re.compile(r"[ \t\r\n\f,]*")  # separators are skipped at C speed (long runs cost no Python loop)
_COMMANDS = "MmLlHhVvCcSsQqTtAaZz"
_ARG_COUNT = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


# --------------------------------------------------------------------------------------
# affine matrices


def multiply(m: Matrix, n: Matrix) -> Matrix:
    """``m · n``: apply ``n`` first, then ``m``."""
    return (m[0] * n[0] + m[2] * n[1],
            m[1] * n[0] + m[3] * n[1],
            m[0] * n[2] + m[2] * n[3],
            m[1] * n[2] + m[3] * n[3],
            m[0] * n[4] + m[2] * n[5] + m[4],
            m[1] * n[4] + m[3] * n[5] + m[5])


def apply(m: Matrix, x: float, y: float) -> XY:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def scale_of(m: Matrix) -> float:
    """The mean length scale of ``m`` (square root of the absolute determinant)."""
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


def translate(tx: float, ty: float) -> Matrix:
    return 1.0, 0.0, 0.0, 1.0, tx, ty


def parse_numbers(text: str, limit: int = 1_000_000) -> Optional[List[float]]:
    """A whitespace / comma separated number list (``points``, ``viewBox``, transform arguments);
    ``None`` when anything else is in the way or more than ``limit`` numbers follow."""
    out: List[float] = []
    i, n = 0, len(text)
    while True:
        i = _SKIP.match(text, i).end()  # type: ignore[union-attr]
        if i >= n:
            return out
        m = _NUMBER.match(text, i)
        if m is None or len(out) >= limit:
            return None
        out.append(float(m.group(0)))
        i = m.end()


_TRANSFORM = re.compile(r"[ \t\r\n\f,]*([A-Za-z]+)[ \t\r\n\f]*\(([^()]*)\)")


def parse_transform(text: Optional[str]) -> Optional[Matrix]:
    """The matrix of an SVG ``transform`` list; the identity for an empty attribute and ``None``
    for one that cannot be parsed (unknown function, wrong argument count, non-finite value,
    more than :data:`MAX_TRANSFORMS` functions)."""
    if text is None:
        return IDENTITY
    m = IDENTITY
    pos, count = 0, 0
    while True:
        pos = _SKIP.match(text, pos).end()  # type: ignore[union-attr]
        if pos >= len(text):
            return m
        match = _TRANSFORM.match(text, pos)
        count += 1
        if match is None or count > MAX_TRANSFORMS:
            return None
        pos = match.end()
        name = match.group(1)
        args = parse_numbers(match.group(2), 8)
        if args is None or not all(math.isfinite(v) for v in args):
            return None
        k = len(args)
        if name == "matrix" and k == 6:
            t: Matrix = (args[0], args[1], args[2], args[3], args[4], args[5])
        elif name == "translate" and k in (1, 2):
            t = translate(args[0], args[1] if k == 2 else 0.0)
        elif name == "scale" and k in (1, 2):
            t = (args[0], 0.0, 0.0, args[1] if k == 2 else args[0], 0.0, 0.0)
        elif name == "rotate" and k in (1, 3):
            a = math.radians(args[0])
            c, s = math.cos(a), math.sin(a)
            t = (c, s, -s, c, 0.0, 0.0)
            if k == 3:
                t = multiply(multiply(translate(args[1], args[2]), t), translate(-args[1], -args[2]))
        elif name == "skewX" and k == 1:
            t = (1.0, 0.0, math.tan(math.radians(args[0])), 1.0, 0.0, 0.0)
        elif name == "skewY" and k == 1:
            t = (1.0, math.tan(math.radians(args[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            return None
        m = multiply(m, t)
        if not all(math.isfinite(v) for v in m):
            return None


# --------------------------------------------------------------------------------------
# path data


@dataclass
class Subpath:
    """One subpath: its vertices (curves already flattened), whether it was closed with ``Z``
    and whether it contained curve or arc segments."""

    points: List[XY] = field(default_factory=list)
    closed: bool = False
    curved: bool = False


@dataclass
class PathData:
    subpaths: List[Subpath] = field(default_factory=list)
    error: Optional[str] = None  # why parsing stopped early (the subpaths before it are kept)
    truncated: bool = False  # the point budget ran out
    cost: int = 0  # budget units spent: one per vertex and per command that added no vertex

    @property
    def point_count(self) -> int:
        return sum(len(s.points) for s in self.subpaths)


class _Scanner:
    __slots__ = ("d", "i", "n")

    def __init__(self, d: str):
        self.d = d
        self.i = 0
        self.n = len(d)

    def skip(self) -> None:
        self.i = _SKIP.match(self.d, self.i).end()  # type: ignore[union-attr]

    def at_end(self) -> bool:
        self.skip()
        return self.i >= self.n

    def command(self) -> Optional[str]:
        self.skip()
        if self.i < self.n and self.d[self.i] in _COMMANDS:
            self.i += 1
            return self.d[self.i - 1]
        return None

    def number(self) -> Optional[float]:
        self.skip()
        m = _NUMBER.match(self.d, self.i)
        if m is None:
            return None
        self.i = m.end()
        return float(m.group(0))

    def flag(self) -> Optional[float]:
        self.skip()
        if self.i < self.n and self.d[self.i] in "01":
            self.i += 1
            return float(self.d[self.i - 1] == "1")
        return None

    def starts_number(self) -> bool:
        self.skip()
        return self.i < self.n and _NUMBER.match(self.d, self.i) is not None


class _PathBuilder:
    def __init__(self, tolerance: float, max_points: int):
        self.tol = max(MIN_TOLERANCE, tolerance) if math.isfinite(tolerance) else 0.1
        self.left = max_points
        self.out = PathData()
        self.current: Optional[Subpath] = None

    def add(self, x: float, y: float) -> bool:
        if self.left <= 0:
            self.out.truncated = True
            return False
        self.left -= 1
        assert self.current is not None
        self.current.points.append((x, y))
        return True

    def charge(self) -> bool:
        """One unit of the budget for a command that adds no vertex (``Z``, an omitted arc)."""
        if self.left <= 0:
            self.out.truncated = True
            return False
        self.left -= 1
        return True

    def start(self, x: float, y: float) -> bool:
        self.flush()
        self.current = Subpath()
        return self.add(x, y)

    def flush(self) -> None:
        """End the current subpath.  A lone moveto (one vertex, no ``Z``) draws nothing in SVG,
        not even a cap, so it is left out (its vertex was still paid for); ``M x y Z`` and a
        zero-length segment stay, as they draw caps."""
        sub = self.current
        if sub is not None and (len(sub.points) > 1 or (sub.points and sub.closed)):
            self.out.subpaths.append(sub)
        self.current = None

    def ensure(self, x: float, y: float) -> bool:
        """A drawing command right after ``Z`` starts a new subpath at the current point."""
        if self.current is None:
            return self.start(x, y)
        return True

    def segments(self, extent: float) -> int:
        """How many line segments keep a curve whose control polygon bends by ``extent``
        within the tolerance (uniform parameter steps; the error of one step of size h is at
        most extent * h^2 / 8)."""
        if not extent > 0 or not math.isfinite(extent):
            return 1
        ratio = extent / (8.0 * self.tol)
        if not ratio < MAX_CURVE_SEGMENTS * MAX_CURVE_SEGMENTS:  # also an overflow to infinity
            return MAX_CURVE_SEGMENTS
        return max(1, min(MAX_CURVE_SEGMENTS, math.ceil(math.sqrt(ratio))))

    def cubic(self, p0: XY, p1: XY, p2: XY, p3: XY) -> bool:
        # second derivative of a cubic is bounded by 6 * the largest second difference
        bend = 6.0 * max(math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
                         math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]))
        n = self.segments(bend)
        self.current.curved = True  # type: ignore[union-attr]
        for k in range(1, n + 1):
            t = k / n
            u = 1.0 - t
            a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
            if not self.add(a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]):
                return False
        return True

    def quad(self, p0: XY, p1: XY, p2: XY) -> bool:
        bend = 2.0 * math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1])
        n = self.segments(bend)
        self.current.curved = True  # type: ignore[union-attr]
        for k in range(1, n + 1):
            t = k / n
            u = 1.0 - t
            if not self.add(u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]):
                return False
        return True

    def arc(self, p0: XY, rx: float, ry: float, phi_deg: float, large: bool, sweep: bool, p1: XY) -> bool:
        """An elliptical arc (SVG 1.1 appendix F.6.5 / F.6.6: endpoint to centre form with
        out-of-range radii scaled up).  The arithmetic runs in units of the half chord, so radii
        that would overflow or underflow when squared still give the right arc; the small arc of
        radii far larger than the chord, an ellipse flatter than 1 : 10^9 and zero or infinite
        radii draw a straight line."""
        self.current.curved = True  # type: ignore[union-attr]
        if p0 == p1:
            return self.charge()  # the arc is omitted
        try:
            points = self._arc_points(p0, abs(rx), abs(ry), phi_deg, large, sweep, p1)
        except (ArithmeticError, ValueError):
            points = []
        for x, y in points:
            if not self.add(x, y):
                return False
        return self.add(p1[0], p1[1])  # end exactly on the endpoint

    def _arc_points(self, p0: XY, rx: float, ry: float, phi_deg: float, large: bool, sweep: bool,
                    p1: XY) -> List[XY]:
        """The inner vertices of the flattened arc (empty for a straight line)."""
        r_max = max(rx, ry)
        if not (math.isfinite(r_max) and math.isfinite(phi_deg)) or min(rx, ry) <= r_max / ARC_RATIO_LIMIT:
            return []  # zero, infinite or degenerate radii: a straight line
        phi = math.radians(phi_deg % 360.0)
        cphi, sphi = math.cos(phi), math.sin(phi)
        dx, dy = (p0[0] - p1[0]) / 2.0, (p0[1] - p1[1]) / 2.0
        h = math.hypot(dx, dy)  # half the chord, the unit of the computation below
        if not (h > 0 and math.isfinite(h)) or (r_max > h * ARC_RATIO_LIMIT and not large):
            return []  # the small arc of so large an ellipse is indistinguishable from its chord
        x1, y1 = (cphi * dx + sphi * dy) / h, (-sphi * dx + cphi * dy) / h  # a unit vector
        ux, uy = rx / r_max, ry / r_max  # the radii's shape: one is 1, the other >= 1e-9
        # radii too small to reach the endpoints are scaled up until they just do (lambda > 1)
        q = max(r_max / h, math.hypot(x1 / ux, y1 / uy))
        a, b = ux * q, uy * q  # radii in half chords
        num = a * a * b * b - a * a * y1 * y1 - b * b * x1 * x1
        den = a * a * y1 * y1 + b * b * x1 * x1
        coef = math.sqrt(max(0.0, num / den)) if den > 0 else 0.0
        if large == sweep:
            coef = -coef
        cx1, cy1 = coef * a * y1 / b, -coef * b * x1 / a
        theta1 = math.atan2((y1 - cy1) / b, (x1 - cx1) / a)
        theta2 = math.atan2((-y1 - cy1) / b, (-x1 - cx1) / a)
        delta = theta2 - theta1
        if sweep and delta < 0:
            delta += 2 * math.pi
        elif not sweep and delta > 0:
            delta -= 2 * math.pi
        rx, ry = a * h, b * h
        cx = (cphi * cx1 - sphi * cy1) * h + (p0[0] + p1[0]) / 2.0
        cy = (sphi * cx1 + cphi * cy1) * h + (p0[1] + p1[1]) / 2.0
        r = max(rx, ry)
        if not all(math.isfinite(v) for v in (r, delta, cx, cy)):
            return []
        step = 2.0 * math.acos(max(-1.0, 1.0 - self.tol / r)) if r > self.tol else math.pi / 2
        n = max(1, min(MAX_CURVE_SEGMENTS, math.ceil(abs(delta) / max(step, 1e-9))))
        out: List[XY] = []
        for k in range(1, n):
            t = theta1 + delta * k / n
            ex, ey = rx * math.cos(t), ry * math.sin(t)
            out.append((cphi * ex - sphi * ey + cx, sphi * ex + cphi * ey + cy))
        return out


def parse_path(d: Optional[str], tolerance: float = 0.1, max_points: int = 1_000_000) -> PathData:
    """Path data -> subpaths in user units.  Curves and arcs are flattened so that no point of
    the polyline is farther than about ``tolerance`` from the curve.  ``max_points`` bounds the
    work: each vertex and each command that adds none costs one unit; when the budget runs out
    parsing stops and :attr:`PathData.truncated` is set (:attr:`PathData.cost` is what was spent)."""
    b = _PathBuilder(tolerance, max_points)
    if not d:
        return b.out
    s = _Scanner(d)
    cx = cy = sx = sy = 0.0
    prev: Optional[str] = None  # previous command letter (upper case) for S / T reflection
    ctrl: Optional[XY] = None  # last second control point (C/S) or control point (Q/T)
    cmd: Optional[str] = None

    def fail(message: str) -> PathData:
        b.flush()
        b.out.error = message
        b.out.cost = max_points - b.left
        return b.out

    # every command costs at least one unit of the budget (b.charge for those adding no
    # vertex); this loop guard only backs that up
    steps = 2 * max(0, max_points) + 64
    while not s.at_end():
        steps -= 1
        if steps < 0:
            b.out.truncated = True
            break
        letter = s.command()
        if letter is not None:
            if cmd is None and letter not in "Mm":
                return fail("path data does not start with a moveto command")
            cmd = letter
        elif cmd is None:
            return fail("path data does not start with a moveto command")
        elif cmd in "Zz":
            return fail("numbers after a closepath command")
        upper = cmd.upper()
        rel = cmd != upper
        if upper == "Z":
            if not b.charge():
                break
            if b.current is not None:
                b.current.closed = True
                b.flush()
            cx, cy = sx, sy
            prev, ctrl = "Z", None
            continue
        args: List[float] = []
        for k in range(_ARG_COUNT[upper]):
            v = s.flag() if upper == "A" and k in (3, 4) else s.number()
            if v is None:
                if letter is not None or k > 0:
                    return fail(f"command {cmd!r} lacks its arguments")
                return fail("unexpected character in path data")
            args.append(v)
        if upper == "M":
            x, y = args
            if rel:
                x, y = cx + x, cy + y
            if not b.start(x, y):
                break
            cx, cy, sx, sy = x, y, x, y
            cmd = "l" if rel else "L"  # further pairs are implicit linetos
            prev, ctrl = "M", None
            continue
        if not b.ensure(cx, cy):
            break
        ok = True
        if upper in ("L", "H", "V"):
            if upper == "L":
                x, y = args
                if rel:
                    x, y = cx + x, cy + y
            elif upper == "H":
                x, y = (cx + args[0] if rel else args[0]), cy
            else:
                x, y = cx, (cy + args[0] if rel else args[0])
            ok = b.add(x, y)
            ctrl = None
        elif upper in ("C", "S"):
            pts = [(args[i] + cx, args[i + 1] + cy) if rel else (args[i], args[i + 1]) for i in range(0, len(args), 2)]
            if upper == "S":
                first = (2 * cx - ctrl[0], 2 * cy - ctrl[1]) if prev in ("C", "S") and ctrl else (cx, cy)
                pts = [first] + pts
            ok = b.cubic((cx, cy), pts[0], pts[1], pts[2])
            ctrl = pts[1]
            x, y = pts[2]
        elif upper in ("Q", "T"):
            if upper == "Q":
                q = (args[0] + cx, args[1] + cy) if rel else (args[0], args[1])
                x, y = (args[2] + cx, args[3] + cy) if rel else (args[2], args[3])
            else:
                q = (2 * cx - ctrl[0], 2 * cy - ctrl[1]) if prev in ("Q", "T") and ctrl else (cx, cy)
                x, y = (args[0] + cx, args[1] + cy) if rel else (args[0], args[1])
            ok = b.quad((cx, cy), q, (x, y))
            ctrl = q
        else:  # A
            x, y = (args[5] + cx, args[6] + cy) if rel else (args[5], args[6])
            ok = b.arc((cx, cy), args[0], args[1], args[2], bool(args[3]), bool(args[4]), (x, y))
            ctrl = None
        if not ok:
            break
        cx, cy = x, y
        prev = upper
    b.flush()
    b.out.cost = max_points - b.left
    return b.out
