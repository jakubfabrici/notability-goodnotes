"""Flexcil ``.flx`` / ``.flex`` -> :class:`gnnote.model.Document` (tolerant reader).

A ``.flx`` document is a ZIP (``docs/flexcil.md``)::

    info                              JSON: key, name, version ("0.0.5"), ...
    pages.index                       JSON list: per page frame {x, y, width, height} in pt,
                                      attachmentPage {file, index}, rotate, key
    attachment/PDF/<file>             the page backgrounds (Flexcil pages are PDF pages)
    attachment/image/<key>            inserted images
    objects/<page key>.objects        z-order: [{type, key}, ...]
    objects/<page key>.drawings       ink: start {x, y}, points (base64), strokeColor (ARGB), mode, ...
    objects/<page key>.shapes         shapes: shapeType, start, points, controlPoints, sides, fillColor
    objects/<page key>.texts          text boxes: frame, text, columns[].p.span[] {style, text}
    objects/<page key>.images         images: key, frame, cropBox, rotate
    objects/<page key>.maskings / .hyperlinks / *_back copies, thumbnails, .itemInfo ...

A ``.flex`` backup is a ZIP of ``.flx`` documents plus ``documents.list`` (u64 length + raw
DEFLATE JSON: the library's folder tree).  One document is converted: the first one of the
library tree (or the one ``read_flexcil(data, document=...)`` names), with a warning that
lists the others.

Mapping (``W`` = page frame width, ``H`` = height, both in pt):

* Ink: ``points`` decode to ``u32 n`` + ``n`` x (f32 x, f32 y, f32 width); ``x``/``y`` are
  offsets from ``start`` (the stroke's bounding-box corner) and every value, ``start`` and
  the width included, is normalised by ``W`` (both axes).  ``mode`` 2 is the highlighter.
* Shapes become strokes: 1 ellipse, 3 rectangle, 4 regular polygon (``sides``), 5 line,
  6 arc (a quadratic Bezier through ``controlPoints[0]``), 7 arrow, 9 closed polygon;
  closed shapes with a visible ``fillColor`` also get a fill.
* Text boxes and images use frames normalised by ``W`` horizontally and ``H`` vertically
  (font sizes by ``W``); an image whose frame only matches its pixel aspect when both axes
  are normalised by ``W`` is read that way instead.
"""
from __future__ import annotations

import base64
import binascii
import math
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..model import RGBA, Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from ..readutil import (PointBudget, ZipBundle, ellipse_points, image_format, image_pixel_size, load_json,
                        num, quad_to_cubic, straight_controls)

__all__ = ["read_flexcil", "list_flexcil_documents", "FlexcilEntry", "decode_points"]

DEFAULT_FRAME = (768.0, 1024.0)  # the iPad page of the samples, used when a page has no usable frame
MAX_PAGES = 10_000
MAX_DOCUMENTS = 10_000
MAX_OBJECTS = 200_000  # per page and layer
MAX_POINTS_PER_STROKE = 1_000_000
MAX_LIST_BYTES = 64 * 1024 * 1024
MAX_TREE_NODES = 100_000
MAX_TREE_DEPTH = 100
MAX_TEXT_CHARS = 1_000_000
MAX_NORMALISED = 100.0  # coordinates beyond 100 page widths are damage
MIN_WIDTH_PT = 0.1
ELLIPSE_SAMPLES = 64
LAYERS = ("drawings", "shapes", "texts", "images", "maskings", "hyperlinks")
BLACK: RGBA = (0.0, 0.0, 0.0, 1.0)
XY = Tuple[float, float]


@dataclass
class FlexcilEntry:
    """One document of a ``.flx`` / ``.flex`` file, in the order :func:`read_flexcil` uses."""

    index: int
    title: str
    key: str  # the document UUID as far as it is known
    member: str  # the backup member holding it ("" for a bare .flx)


# --------------------------------------------------------------------------- small decoders


def decode_points(value: Any) -> Optional[List[Tuple[float, float, float]]]:
    """Base64 ``u32 n`` + ``n`` x (f32, f32, f32) -> triples; ``None`` when unusable."""
    if not isinstance(value, str) or len(value) > (MAX_POINTS_PER_STROKE * 12 + 8) * 4 // 3 + 8:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) < 16:
        return None
    count = struct.unpack_from("<I", raw)[0]
    if len(raw) == 4 + 12 * count:
        body = raw[4:]
    elif (len(raw) - 4) % 12 == 0 and count == 0:
        body = raw[4:]
    elif (len(raw) - 8) % 12 == 0:
        body = raw[8:]  # an 8-byte header (seen by a third-party viewer in older files)
    else:
        return None
    triples = list(struct.iter_unpack("<fff", body))
    if not triples or len(triples) > MAX_POINTS_PER_STROKE:
        return None
    if not all(math.isfinite(v) and abs(v) <= MAX_NORMALISED for t in triples for v in t):
        return None
    return triples


def _argb(value: Any, default: RGBA = BLACK) -> RGBA:
    """ARGB packed in an integer (JSON may store it signed)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    v = value & 0xFFFFFFFF
    return ((v >> 16 & 255) / 255.0, (v >> 8 & 255) / 255.0, (v & 255) / 255.0, (v >> 24 & 255) / 255.0)


def _alpha(value: Any) -> float:
    return _argb(value, (0.0, 0.0, 0.0, 0.0))[3]


def _decode_list(raw: Optional[bytes]) -> Any:
    """``documents.list`` / ``.trash.list``: u64 length + raw DEFLATE (zlib or plain JSON accepted)."""
    if raw is None or len(raw) < 8:
        return None
    length = struct.unpack_from("<Q", raw)[0]
    if length > MAX_LIST_BYTES:
        return None
    for wbits in (-15, 15):
        try:
            d = zlib.decompressobj(wbits)
            plain = d.decompress(raw[8:], length + 1)
        except zlib.error:
            continue
        if len(plain) == length:
            return load_json(plain)
    return load_json(raw) if raw[:1] in (b"[", b"{") else None


def _normalise_id(value: str) -> str:
    stem = value.replace("\\", "/").rsplit("/", 1)[-1]
    if stem.lower().endswith((".flx", ".pdf")):
        stem = stem[:-4]
    return stem.upper()


def _walk_tree(root: Any, documents: List[Tuple[str, str]], removed: set) -> None:
    """Depth-first ``(id, title)`` of the documents in a library tree (bounded)."""
    nodes = root if isinstance(root, list) else (root.get("children") if isinstance(root, dict) else None)
    if not isinstance(nodes, list):
        return
    stack: List[Tuple[Any, int, bool]] = [(n, 0, False) for n in reversed(nodes)]
    seen = 0
    while stack:
        node, depth, gone = stack.pop()
        seen += 1
        if seen > MAX_TREE_NODES:
            return
        if isinstance(node, dict) and isinstance(node.get("item"), dict):  # .trash.list records
            node = node["item"]
            gone = True
        if not isinstance(node, dict):
            continue
        gone = gone or node.get("state") == "removed"
        doc = node.get("document")
        if isinstance(doc, str) and doc.strip():
            ident = _normalise_id(doc.strip())
            if gone:
                removed.add(ident)
            else:
                documents.append((ident, str(node.get("name") or "")))
        children = node.get("children")
        if isinstance(children, list) and depth < MAX_TREE_DEPTH:
            stack.extend((c, depth + 1, gone) for c in reversed(children))


# --------------------------------------------------------------------------- containers


class _Source:
    """Where one document's members live: the outer archive (``prefix``) or a nested ``.flx``."""

    def __init__(self, kind: str, member: str, stem: str):
        self.kind = kind  # "root" | "folder" | "nested"
        self.member = member  # folder prefix or nested member name
        self.stem = stem
        self.title = ""
        self.key = stem


class _DocFiles:
    def __init__(self, bundle: ZipBundle, prefix: str = ""):
        self.bundle = bundle
        self.prefix = prefix

    def read(self, name: str) -> Optional[bytes]:
        return self.bundle.read(self.prefix + name)

    def json(self, name: str) -> Any:
        return load_json(self.read(name))

    def has(self, name: str) -> bool:
        return self.bundle.has(self.prefix + name)


class _Container:
    def __init__(self, data: bytes):
        self.bundle = ZipBundle(data, "Flexcil")
        names = self.bundle.names
        self.sources: List[_Source] = []
        self.backup = False
        if self.bundle.has("pages.index"):
            root = _Source("root", "", "")
            info = load_json(self.bundle.read("info"))
            if isinstance(info, dict):
                if isinstance(info.get("key"), str):
                    root.key = root.stem = info["key"].strip().upper()[:256]
                if isinstance(info.get("name"), str):
                    root.title = info["name"].strip()
            self.sources.append(root)
            return
        self.backup = True
        for name in names:
            if name.endswith("/"):
                continue
            base = name.rsplit("/", 1)[-1]
            if base.lower().endswith(".flx") and len(base) > 4:
                self.sources.append(_Source("nested", name, _normalise_id(base)))
            elif base == "pages.index" and "/" in name:
                prefix = name[: -len("pages.index")]
                folder = prefix.rstrip("/").rsplit("/", 1)[-1]
                self.sources.append(_Source("folder", prefix, _normalise_id(folder)))
            if len(self.sources) > MAX_DOCUMENTS:
                break
        if not self.sources:
            raise ValueError("not a Flexcil file: no pages.index and no .flx documents found")
        self.order_by_library()

    def order_by_library(self) -> None:
        listed: List[Tuple[str, str]] = []
        removed: set = set()
        for name in self.bundle.names:
            base = name.rsplit("/", 1)[-1]
            if base == "documents.list":
                _walk_tree(_decode_list(self.bundle.read(name)), listed, removed)
            elif base == ".trash.list":
                _walk_tree(_decode_list(self.bundle.read(name)), [], removed)
        by_stem: Dict[str, _Source] = {}
        for source in self.sources:
            by_stem.setdefault(source.stem, source)
        ordered: List[_Source] = []
        placed: set = set()
        for ident, title in listed:
            source = by_stem.get(ident)
            if source is not None and id(source) not in placed:
                placed.add(id(source))
                source.title = title
                ordered.append(source)
        rest = [s for s in self.sources if id(s) not in placed]
        trashed = [s for s in rest if s.stem in removed]
        ordered += [s for s in rest if s.stem not in removed]
        self.sources = ordered or trashed  # a backup holding only trashed documents still converts

    def files(self, source: _Source) -> Optional[_DocFiles]:
        if source.kind != "nested":
            return _DocFiles(self.bundle, source.member)
        raw = self.bundle.read(source.member)
        if raw is None:
            return None
        try:
            inner = ZipBundle(raw, "Flexcil", budget=self.bundle.budget)
        except ValueError:
            return None
        if not inner.has("pages.index"):
            return None
        return _DocFiles(inner, "")


# --------------------------------------------------------------------------- document reader


class _Reader:
    def __init__(self, files: _DocFiles, title: str):
        self.files = files
        self.doc = Document(source_format="flexcil")
        info = files.json("info")
        self.info = info if isinstance(info, dict) else {}
        name = self.info.get("name")
        self.doc.title = title or (name.strip() if isinstance(name, str) and name.strip() else "") or "Untitled"
        self.budget = PointBudget()
        self.counts: Dict[str, int] = {}
        self.pdf_pages: Dict[str, int] = {}
        self.pdf_sizes: Dict[str, List[Tuple[float, float]]] = {}

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    def count(self, what: str, n: int = 1) -> None:
        self.counts[what] = self.counts.get(what, 0) + n

    def layer(self, key: str, layer: str) -> List[dict]:
        rows = self.files.json(f"objects/{key}.{layer}")
        if not isinstance(rows, list):
            return []
        return [r for r in rows[:MAX_OBJECTS] if isinstance(r, dict)]

    def read(self) -> Document:
        pages = self.files.json("pages.index")
        if not isinstance(pages, list):
            self.warn("The document's page list (pages.index) is unreadable; no pages were converted")
            pages = []
        if len(pages) > MAX_PAGES:
            self.warn(f"The document lists {len(pages)} pages; only the first {MAX_PAGES} are read")
            pages = pages[:MAX_PAGES]
        for number, entry in enumerate(pages, 1):
            self.doc.pages.append(self.page(number, entry if isinstance(entry, dict) else {}))
        self.report()
        return self.doc

    # -- pages -------------------------------------------------------------------------

    def background(self, number: int, entry: Dict[str, Any]) -> Optional[PdfBackground]:
        ref = entry.get("attachmentPage")
        if not isinstance(ref, dict):
            return None
        name, index = ref.get("file"), ref.get("index", 0)
        if not isinstance(name, str) or not name or "/" in name or isinstance(index, bool) \
                or not isinstance(index, int) or index < 0:
            self.warn(f"Page {number}: unusable PDF background reference ignored")
            return None
        if name not in self.pdf_pages:
            data = self.files.read("attachment/PDF/" + name)
            if data is None or not data.startswith(b"%PDF-"):
                self.warn(f"Page {number}: its PDF background {name} is missing; the page is plain paper")
                self.pdf_pages[name] = 0
                return None
            sizes: List[Tuple[float, float]] = []
            try:
                from .. import pdfutil  # imported lazily: the PDF parser is large

                sizes = [(p.width, p.height) for p in pdfutil.pdf_info(data).pages]
            except Exception:  # noqa: BLE001 - an unreadable PDF must not stop the reader
                sizes = []
            if not sizes:
                self.warn(f"PDF background {name} could not be read; its pages are plain paper")
                self.pdf_pages[name] = 0
                return None
            self.pdf_pages[name] = len(sizes)
            self.pdf_sizes[name] = sizes
            self.doc.pdfs[name] = data
        if self.pdf_pages[name] == 0:
            return None
        if index >= self.pdf_pages[name]:
            self.warn(f"Page {number}: PDF page {index + 1} of {name} does not exist; the page is plain paper")
            return None
        return PdfBackground(name, index)

    def page(self, number: int, entry: Dict[str, Any]) -> Page:
        frame = entry.get("frame") if isinstance(entry.get("frame"), dict) else {}
        w, h = num(frame.get("width")), num(frame.get("height"))
        background = self.background(number, entry)
        if w is None or h is None or not (1 <= w <= 100_000 and 1 <= h <= 100_000):
            if background is not None:
                w, h = self.pdf_sizes[background.pdf_id][background.page_index]
            else:
                w, h = DEFAULT_FRAME
            self.warn(f"Page {number}: no usable page frame; {w:g} x {h:g} pt used")
        elif background is not None:
            pw, ph = self.pdf_sizes[background.pdf_id][background.page_index]
            if abs(pw - w) > 1 + 0.01 * w or abs(ph - h) > 1 + 0.01 * h:
                self.count("frame-mismatch")
        rotate = num(entry.get("rotate"), 0.0)
        if rotate:
            self.count("rotated-pages")
        page = Page(width=w, height=h, background=background)
        key = entry.get("key")
        if not isinstance(key, str) or not key or "/" in key or len(key) > 256:
            return page
        self.objects(page, key, w, h)
        return page

    def objects(self, page: Page, key: str, w: float, h: float) -> None:
        layers = {name: self.layer(key, name) for name in LAYERS}
        by_key: Dict[str, Tuple[str, dict]] = {}
        for name in ("drawings", "shapes"):
            for obj in layers[name]:
                k = obj.get("key")
                if isinstance(k, str):
                    by_key.setdefault(k, (name, obj))
        ordered: List[Tuple[str, dict]] = []
        used: set = set()
        refs = self.files.json(f"objects/{key}.objects")
        for ref in refs[:MAX_OBJECTS] if isinstance(refs, list) else []:
            k = ref.get("key") if isinstance(ref, dict) else None
            if isinstance(k, str) and k in by_key and k not in used:
                used.add(k)
                ordered.append(by_key[k])
        for name in ("drawings", "shapes"):
            for obj in layers[name]:
                k = obj.get("key")
                if not (isinstance(k, str) and k in used):
                    ordered.append((name, obj))
        for name, obj in ordered:
            strokes = self.drawing(obj, w) if name == "drawings" else self.shape(obj, w)
            page.strokes.extend(strokes)
        for obj in layers["texts"]:
            box = self.text(obj, w, h)
            if box is not None:
                page.texts.append(box)
        for obj in layers["images"]:
            image = self.image(obj, w, h)
            if image is not None:
                page.images.append(image)
        if layers["maskings"]:
            self.count("maskings", len(layers["maskings"]))
        if layers["hyperlinks"]:
            self.count("hyperlinks", len(layers["hyperlinks"]))

    # -- ink and shapes ------------------------------------------------------------------

    def transform_check(self, obj: dict) -> None:
        scale = obj.get("scale")
        sx = num(scale.get("x"), 1.0) if isinstance(scale, dict) else 1.0
        sy = num(scale.get("y"), 1.0) if isinstance(scale, dict) else 1.0
        if num(obj.get("rotate"), 0.0) or sx != 1.0 or sy != 1.0:
            self.count("transformed")
        if num(obj.get("dashtype"), 0.0):
            self.count("dashed")

    def drawing(self, obj: dict, w: float) -> List[Stroke]:
        triples = decode_points(obj.get("points"))
        start = obj.get("start") if isinstance(obj.get("start"), dict) else {}
        sx, sy = num(start.get("x"), 0.0), num(start.get("y"), 0.0)
        if triples is None or abs(sx) > MAX_NORMALISED or abs(sy) > MAX_NORMALISED:
            self.count("unreadable")
            return []
        if not self.budget.take(len(triples)):
            return []
        self.transform_check(obj)
        points = [Point((sx + x) * w, (sy + y) * w, max(t * w, MIN_WIDTH_PT)) for x, y, t in triples]
        widths = sorted(p.width for p in points)
        highlighter = obj.get("mode") == 2
        return [Stroke(points, color=_argb(obj.get("strokeColor")), kind="highlighter" if highlighter else "pen",
                       width=widths[len(widths) // 2])]

    def shape(self, obj: dict, w: float) -> List[Stroke]:
        triples = decode_points(obj.get("points"))
        start = obj.get("start") if isinstance(obj.get("start"), dict) else {}
        sx, sy = num(start.get("x"), 0.0), num(start.get("y"), 0.0)
        kind = obj.get("shapeType")
        if triples is None or abs(sx) > MAX_NORMALISED or abs(sy) > MAX_NORMALISED or isinstance(kind, bool) \
                or not isinstance(kind, int):
            self.count("unreadable")
            return []
        self.transform_check(obj)
        pts = [((sx + x) * w, (sy + y) * w) for x, y, _ in triples]
        width = max(triples[0][2] * w, MIN_WIDTH_PT)
        color = _argb(obj.get("strokeColor"))
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        left, right, top, bottom = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        cx, cy, rx, ry = (left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2
        closed = False
        if kind == 1:  # ellipse in the box of the two points
            outline = ellipse_points(cx, cy, rx, ry, ELLIPSE_SAMPLES)
            stroke = self.polyline(outline, width, color, straight=False)
            closed = True
        elif kind == 3:  # rectangle
            outline = [(left, top), (right, top), (right, bottom), (left, bottom), (left, top)]
            stroke = self.polyline(outline, width, color)
            closed = True
        elif kind == 4:  # regular polygon with ``sides`` corners inscribed in the box, a corner on top
            sides = obj.get("sides")
            n = sides if isinstance(sides, int) and not isinstance(sides, bool) and 3 <= sides <= 64 else 5
            outline = ellipse_points(cx, cy, rx, ry, n, start_angle=-math.pi / 2)
            stroke = self.polyline(outline, width, color)
            closed = True
        elif kind == 5:  # straight line
            outline = [pts[0], pts[-1]]
            stroke = self.polyline(outline, width, color)
        elif kind == 6 and isinstance(obj.get("controlPoints"), list) and obj["controlPoints"] \
                and isinstance(obj["controlPoints"][0], dict):
            c = obj["controlPoints"][0]
            cxn, cyn = num(c.get("x")), num(c.get("y"))
            if cxn is None or cyn is None or abs(cxn) > MAX_NORMALISED or abs(cyn) > MAX_NORMALISED:
                outline = [pts[0], pts[-1]]
                stroke = self.polyline(outline, width, color)
            else:
                control = (cxn * w, cyn * w)
                c1, c2 = quad_to_cubic(pts[0], control, pts[-1])
                stroke = Stroke([Point(*pts[0], width), Point(*pts[-1], width)], color=color, width=width,
                                controls=[(Point(*c1, width), Point(*c2, width))])
                outline = [pts[0], pts[-1]]
        elif kind == 7:  # arrow: the shaft, then the head drawn from the tip
            outline = self.arrow(pts, width)
            stroke = self.polyline(outline, width, color)
        else:
            if kind not in (6, 9):
                self.count("unknown-shapes")
            outline = list(pts)
            closed = kind == 9 and len(outline) >= 3
            if closed and outline[0] != outline[-1]:
                outline.append(outline[0])
            stroke = self.polyline(outline, width, color)
        self.count("shapes")
        out = [stroke]
        fill = obj.get("fillColor")
        if closed and _alpha(fill) > 0.0:
            fill_points = [Point(x, y, 0.0) for x, y in outline]
            out.append(Stroke(points=fill_points, color=_argb(fill), kind="fill", width=0.0,
                              outline=[list(fill_points)]))
        return out

    @staticmethod
    def arrow(pts: Sequence[XY], width: float) -> List[XY]:
        tip, prev = pts[-1], pts[-2] if len(pts) >= 2 else pts[-1]
        dx, dy = tip[0] - prev[0], tip[1] - prev[1]
        length = math.hypot(dx, dy)
        out = list(pts)
        if length <= 1e-9:
            return out
        ux, uy = dx / length, dy / length
        head = min(max(3.0 * width, 6.0), 0.5 * length)
        ca, sa = math.cos(math.radians(30)), math.sin(math.radians(30))
        left = (tip[0] - head * (ux * ca - uy * sa), tip[1] - head * (uy * ca + ux * sa))
        right = (tip[0] - head * (ux * ca + uy * sa), tip[1] - head * (uy * ca - ux * sa))
        return out + [left, tip, right]

    @staticmethod
    def polyline(outline: Sequence[XY], width: float, color: RGBA, straight: bool = True) -> Stroke:
        points = [Point(x, y, width) for x, y in outline]
        return Stroke(points, color=color, width=width,
                      controls=straight_controls(points) if straight and len(points) >= 2 else None)

    # -- text boxes and images -------------------------------------------------------------

    def text(self, obj: dict, w: float, h: float) -> Optional[TextBox]:
        frame = obj.get("frame") if isinstance(obj.get("frame"), dict) else {}
        fx, fy, fw, fh = (num(frame.get(k)) for k in ("x", "y", "width", "height"))
        if fx is None or fy is None or fw is None or fh is None or fw <= 0 or fh <= 0 \
                or max(abs(fx), abs(fy), fw, fh) > MAX_NORMALISED:
            self.count("unplaced-texts")
            return None
        spans: List[Tuple[str, dict]] = []
        columns = obj.get("columns")
        for column in columns[:10_000] if isinstance(columns, list) else []:
            paragraphs = column.get("p") if isinstance(column, dict) else None
            for paragraph in paragraphs if isinstance(paragraphs, list) else [paragraphs]:
                items = paragraph.get("span") if isinstance(paragraph, dict) else None
                for span in items[:10_000] if isinstance(items, list) else []:
                    if isinstance(span, dict) and isinstance(span.get("text"), str):
                        spans.append((span["text"], span.get("style") if isinstance(span.get("style"), dict) else {}))
        text = obj.get("text")
        if not isinstance(text, str):
            text = "".join(t for t, _ in spans)
        text = text[:MAX_TEXT_CHARS]
        if not text.strip():
            return None
        runs = [self.run(t, style, w) for t, style in spans]
        if "".join(r.text for r in runs) != text:
            first = spans[0][1] if spans else {}
            runs = [self.run(text, first, w)]
        size = next((r.size for r in runs if r.size), 12.0)
        color = next((r.color for r in runs if r.color), BLACK)
        if num(obj.get("rotate"), 0.0):
            self.count("rotated-texts")
        return TextBox(fx * w, fy * h, fw * w, fh * h, text, runs=runs, color=color, size=size)

    @staticmethod
    def run(text: str, style: dict, w: float) -> TextRun:
        size = num(style.get("font-size"))
        size = size * w if size is not None and 0 < size * w <= 1000 else None
        family = style.get("font-family")
        font = (family.split(",")[0].strip() or None) if isinstance(family, str) else None
        weight = str(style.get("font-weight", ""))[:16].lower()
        italic = "italic" in str(style.get("font-style", ""))[:32].lower()
        bold = weight in ("bold", "bolder") or (weight.isdigit() and len(weight) <= 4 and int(weight) >= 600)
        if font and "bold" in font.lower():
            bold = True
        if font and ("italic" in font.lower() or "oblique" in font.lower()):
            italic = True
        color = style.get("color")
        rgba = _argb(color, BLACK) if isinstance(color, int) and not isinstance(color, bool) else None
        underline = "underline" in str(style.get("text-decoration", ""))[:64].lower()
        return TextRun(text, bold=bold, italic=italic, underline=underline, font=font, size=size, color=rgba)

    def image(self, obj: dict, w: float, h: float) -> Optional[Image]:
        key = obj.get("key")
        frame = obj.get("frame") if isinstance(obj.get("frame"), dict) else {}
        fx, fy, fw, fh = (num(frame.get(k)) for k in ("x", "y", "width", "height"))
        if not isinstance(key, str) or not key or "/" in key or fx is None or fy is None or fw is None \
                or fh is None or fw <= 0 or fh <= 0 or max(abs(fx), abs(fy), fw, fh) > MAX_NORMALISED:
            self.count("unplaced-images")
            return None
        data = self.files.read("attachment/image/" + key)
        fmt = image_format(data) if data else None
        if fmt not in ("png", "jpeg", "pdf") or data is None:
            self.count("unreadable-images")
            return None
        crop = obj.get("cropBox")
        cw = ch = 1.0
        if isinstance(crop, dict):
            values = [num(crop.get(k)) for k in ("x", "y", "width", "height")]
            if all(v is not None for v in values) and values != [0.0, 0.0, 1.0, 1.0]:
                self.count("cropped-images")
                cw, ch = max(values[2] or 1.0, 1e-6), max(values[3] or 1.0, 1e-6)
        # Frames are normalised like text frames (x by W, y by H) unless only the
        # width-normalised reading matches the picture's own aspect ratio.
        y_scale = h
        pixels = image_pixel_size(data)
        if pixels is not None and h != w:
            aspect = pixels[0] * cw / (pixels[1] * ch)
            by_h = abs(math.log((fw * w) / (fh * h) / aspect))
            by_w = abs(math.log((fw * w) / (fh * w) / aspect))
            if by_w + 0.05 < by_h:
                y_scale = w
        rotation = num(obj.get("rotate"), 0.0) or 0.0
        degrees = math.degrees(rotation) % 360.0 if rotation else 0.0
        self.count("images")
        return Image(fx * w, fy * y_scale, fw * w, fh * y_scale, data, fmt=fmt, rotation=round(degrees, 6))

    # -- warnings ----------------------------------------------------------------------------

    def report(self) -> None:
        c = self.counts
        if c.get("shapes"):
            self.warn(f"{c['shapes']} shape(s) were converted to ink strokes")
        if c.get("unknown-shapes"):
            self.warn(f"{c['unknown-shapes']} shape(s) of an unknown type were drawn through their points")
        if c.get("unreadable"):
            self.warn(f"{c['unreadable']} stroke(s) or shape(s) with unreadable points were skipped")
        if c.get("transformed"):
            self.warn(f"{c['transformed']} rotated or scaled object(s) were drawn untransformed")
        if c.get("dashed"):
            self.warn("Dashed or dotted strokes are drawn solid")
        if c.get("maskings"):
            self.warn(f"{c['maskings']} masking object(s) were not converted")
        if c.get("hyperlinks"):
            self.warn(f"{c['hyperlinks']} link(s) were not converted")
        if c.get("rotated-pages"):
            self.warn(f"{c['rotated-pages']} page rotation(s) were ignored")
        if c.get("frame-mismatch"):
            self.warn(f"{c['frame-mismatch']} page(s) differ in size from their PDF page; ink may be offset")
        if c.get("rotated-texts"):
            self.warn("Rotated text boxes are placed unrotated")
        if c.get("unplaced-texts") or c.get("unplaced-images"):
            self.warn(f"{c.get('unplaced-texts', 0) + c.get('unplaced-images', 0)} text box(es) or image(s) "
                      f"without a usable frame were skipped")
        if c.get("unreadable-images"):
            self.warn(f"{c['unreadable-images']} image(s) missing or not PNG/JPEG/PDF were skipped")
        if c.get("cropped-images"):
            self.warn("Image crops are not applied; the whole image fills the crop's frame")
        if c.get("images"):
            self.warn(f"Placement of the {c['images']} image(s) follows the text-frame convention; it is "
                      f"unverified for Flexcil images")
        if self.budget.exhausted:
            self.warn("The document holds more ink points than the converter reads; the remaining strokes "
                      "were skipped")
        for message in (self.files.bundle.size_warning("Flexcil"), self.files.bundle.failure_warning("Flexcil")):
            if message:
                self.warn(message)


# --------------------------------------------------------------------------- public API


def _entries(container: _Container) -> List[FlexcilEntry]:
    out = []
    for index, source in enumerate(container.sources):
        out.append(FlexcilEntry(index, source.title or source.stem or "", source.key, source.member))
    return out


def list_flexcil_documents(data: bytes) -> List[FlexcilEntry]:
    """The documents of a ``.flx`` (one) or ``.flex`` backup, in conversion order."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("list_flexcil_documents expects bytes")
    return _entries(_Container(bytes(data)))


def read_flexcil(data: bytes, document: Union[int, str, None] = None) -> Document:
    """Parse a Flexcil ``.flx`` document or ``.flex`` backup given as bytes.

    ``document`` picks one document of a backup: its 0-based position in
    :func:`list_flexcil_documents`, or its title or UUID; by default the first readable one.
    Raises :class:`ValueError` when the data is not a Flexcil file, or when ``document``
    names no document.  Everything else is tolerated with a line on ``Document.warnings``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_flexcil expects bytes")
    container = _Container(bytes(data))
    sources = container.sources
    if document is not None:
        if isinstance(document, int) and not isinstance(document, bool):
            if not 0 <= document < len(sources):
                raise ValueError(f"the file holds {len(sources)} Flexcil document(s); there is no document {document}")
            candidates = [sources[document]]
        else:
            wanted = str(document).strip()
            candidates = [s for s in sources if wanted.upper() in (s.stem, s.key.upper())
                          or (s.title and s.title.strip().lower() == wanted.lower())]
            if not candidates:
                raise ValueError(f"no Flexcil document {wanted!r} in the file")
    else:
        candidates = sources
    unreadable: List[str] = []
    for source in candidates:
        files = container.files(source)
        if files is None:
            unreadable.append(source.title or source.stem or source.member)
            continue
        doc = _Reader(files, source.title).read()
        if container.backup:
            others = [s.title or s.stem for s in sources if s is not source]
            if others:
                shown = ", ".join(repr(t) for t in others[:5]) + (", ..." if len(others) > 5 else "")
                doc.warn(f"This Flexcil backup holds {len(sources)} documents; only {doc.title!r} was converted "
                         f"(the others: {shown})")
        if unreadable:
            doc.warn(f"{len(unreadable)} document(s) of the backup could not be read: "
                     + ", ".join(repr(t) for t in unreadable[:5]))
        size_warning = container.bundle.size_warning("Flexcil backup") if container.backup else None
        if size_warning:
            doc.warn(size_warning)
        return doc
    detail = " (" + size_note + ")" if (size_note := container.bundle.size_warning("Flexcil backup")) else ""
    raise ValueError(f"not a readable Flexcil file: none of its documents could be opened{detail}")
