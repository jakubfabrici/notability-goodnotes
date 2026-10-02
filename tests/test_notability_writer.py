"""Tests for gnnote.notability.writer and gnnote.notability.archivebuilder."""
from __future__ import annotations

import importlib.util
import io
import math
import os
import plistlib
import struct
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from gnnote.notability.archivebuilder import (
    INT64_MAX, ArchiveBuilder, color_string, nsrgb_bytes, point_string, range_string, rect_string,
)
from gnnote.notability.writer import LEGACY_ASPECT, image_pixel_size, white_png, write_note

W = 574.0
PLAIN_H = LEGACY_ASPECT * W
GN_W, GN_H = 455.04, 588.45  # GoodNotes "standard" page


class Opts:
    def __init__(self, **kw):
        self.paper = "plain"
        self.pressure = True
        self.simplify = 0.0
        self.title = None
        self.notability_page_width = W
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------------------
# helpers

def line(x0, y0, x1, y1, n=12, w=1.5, **kw) -> Stroke:
    pts = [Point(x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * i / (n - 1), w * (1 + 0.3 * math.sin(i)))
           for i in range(n)]
    return Stroke(pts, **kw)


def minimal_pdf(width: float, height: float) -> bytes:
    """A hand-written one-page PDF (used when gnnote.pdfutil is unavailable)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:g} {height:g}] >>".encode(),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def paper_pdf(width: float, height: float) -> bytes:
    try:
        from gnnote import pdfutil
        return pdfutil.make_paper_pdf(width, height, "lined")
    except Exception:  # noqa: BLE001 - module not integrated yet
        return minimal_pdf(width, height)


def synthetic_document() -> Document:
    """3 pages: plain (ink + image + text box), PDF-backed A4, plain (dot + diagonal)."""
    doc = Document(title="Synthetic/test: note", source_format="goodnotes")
    doc.pdfs["pdf-a"] = paper_pdf(595.0, 842.0)
    p1 = Page(GN_W, GN_H,
              strokes=[line(10, 10, 100, 100), line(50, 50, 60, 52, kind="highlighter", color=(1, 0.6, 0, 1), w=8)],
              images=[Image(20, 200, 100, 60, white_png(40, 24), "png")],
              texts=[TextBox(30, 300, 150, 40, "Hello\nworld", runs=[TextRun("Hello\n", bold=True), TextRun("world")], size=12)])
    p2 = Page(595.0, 842.0, strokes=[line(100, 700, 200, 800)], background=PdfBackground("pdf-a", 0))
    p3 = Page(GN_W, GN_H, strokes=[Stroke([Point(5, 5, 1)]), line(0, 0, GN_W, GN_H)])
    doc.pages = [p1, p2, p3]
    return doc


def members(data: bytes) -> Dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n.split("/", 1)[1]: z.read(n) for n in z.namelist()}


def load_session(data: bytes):
    """(plist, Archive) of Session.plist using the project's keyed-archive reader."""
    from gnnote.notability.keyedarchive import Archive
    pl = plistlib.loads(members(data)["Session.plist"])
    return pl, Archive(pl)


def hash_of(archive, root) -> Dict[str, Any]:
    rich = archive.get(root, "richText")
    return archive.get(archive.get(rich, "Handwriting Overlay"), "SpatialHash")


def key_set(obj: Dict[str, Any]) -> set:
    return {k for k in obj if k != "$class"}


def parse_curves(archive, hs) -> List[Dict[str, Any]]:
    nc = archive.integer(hs["numcurves"])
    pts = struct.unpack(f"<{len(hs['curvespoints']) // 4}f", hs["curvespoints"])
    npts = struct.unpack(f"<{nc}i", hs["curvesnumpoints"])
    widths = struct.unpack(f"<{nc}f", hs["curveswidth"])
    fw = struct.unpack(f"<{len(hs['curvesfractionalwidths']) // 4}f", hs["curvesfractionalwidths"])
    out, p, f = [], 0, 0
    for i in range(nc):
        n = npts[i]
        k = (n - 1) // 3
        out.append(dict(points=[(pts[2 * (p + j)], pts[2 * (p + j) + 1]) for j in range(n)],
                        width=widths[i], fractional=list(fw[f:f + k + 1]),
                        rgba=tuple(hs["curvescolors"][4 * i:4 * i + 4]), style=hs["curvesstyles"][i]))
        p += n
        f += k + 1
    return out


def scratchpad_file(samples, *parts: str) -> Optional[Path]:
    """Files next to the reference clones (scratchpad layout: ref/, work/, poc/)."""
    candidates = [Path(samples.root).parent.joinpath(*parts), Path(samples.root).joinpath(*parts)]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_notereader(samples):
    env = os.environ.get("GNNOTE_NOTEREADER")
    path = Path(env) if env else scratchpad_file(samples, "work", "notereader.py")
    if path is None or not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("notereader_oracle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------------------
# archive builder

def test_geometry_strings():
    assert point_string(1.0, 2.5) == "{1, 2.5}"
    assert rect_string(0, 0, 2592, 1728) == "{{0, 0}, {2592, 1728}}"
    assert range_string(32, 10) == "{32, 10}"
    assert color_string(0, 0.435294121503830, 1, 1) == "0.000000000000000,0.435294121503830,1.000000000000000,1.000000000000000"
    assert nsrgb_bytes(0, 0, 0, 1) == b"0 0 0"
    assert nsrgb_bytes(0, 0, 0, 0) == b"0 0 0 0"
    assert nsrgb_bytes(0.5882353, 0.5882353, 0.5882353, 1) == b"0.588 0.588 0.588"


def test_builder_roundtrip():
    b = ArchiveBuilder("NSKeyedArchiver", "root")
    root = b.reserve()
    d = b.dictionary([("a", 1), ("b", b.string("x"))])
    b.object("Thing", {"items": b.array([d, b.string("x")]), "when": b.date(978307200.0 + 10)}, uid=root)
    b.set_root(root)
    pl = plistlib.loads(b.dumps())
    assert pl["$archiver"] == "NSKeyedArchiver" and pl["$top"] == {"root": plistlib.UID(1)}
    assert pl["$objects"][0] == "$null"
    from gnnote.notability.keyedarchive import Archive
    a = Archive(pl)
    assert a.classname(a.root) == "Thing"
    items = a.array(a.get(a.root, "items"))
    assert a.dictionary(items[0]) == {"a": 1, "b": "x"} and items[1] == "x"
    assert a.date(a.get(a.root, "when")) == pytest.approx(978307210.0)
    # interned strings share a UID
    assert b.string("x") == b.string("x")


# ---------------------------------------------------------------------------------------
# structure against the Notability 10.4 template

def test_structure_matches_template(samples):
    template = samples.notability_template()
    from gnnote.notability.keyedarchive import Archive
    with zipfile.ZipFile(template) as z:
        t_pl = plistlib.loads(z.read(next(n for n in z.namelist() if n.endswith("Session.plist"))))
        t_members = sorted(n.split("/", 1)[1] for n in z.namelist())
    t = Archive(t_pl)
    data = write_note(synthetic_document(), Opts())
    pl, a = load_session(data)

    assert pl["$archiver"] == "GLKeyedArchiver" and pl["$top"] == {"$0": plistlib.UID(1)}
    assert key_set(a.root) == key_set(t.root)
    assert key_set(a.get(a.root, "richText")) == key_set(t.get(t.root, "richText"))
    assert key_set(hash_of(a, a.root)) == key_set(hash_of(t, t.root))
    assert key_set(a.get(a.root, "contentPlaybackEventManager")) == key_set(t.get(t.root, "contentPlaybackEventManager"))
    assert a.get(a.root, "sessionFormatVersion") == 5
    assert a.string(a.get(a.root, "NBNoteTakingSessionBundleVersionNumberKey")) == "10.4"
    reflow = a.get(a.get(a.root, "richText"), "reflowState")
    assert a.classname(reflow) == "NBReflowStateLocked"
    assert reflow["pageWidthInDocumentCoordsKey"] == W
    # every $class resolves to a registration with $classname, and the chains match the template's
    t_chains = {o["$classname"]: o.get("$classes") for o in t_pl["$objects"] if isinstance(o, dict) and "$classname" in o}
    for obj in pl["$objects"]:
        if isinstance(obj, dict) and "$class" in obj:
            assert isinstance(obj["$class"], plistlib.UID)
            cls = pl["$objects"][obj["$class"].data]
            assert isinstance(cls, dict) and "$classname" in cls
            if cls["$classname"] in t_chains:
                assert cls.get("$classes") == t_chains[cls["$classname"]]
    # same member set as the template plus the PDF and image we added
    got = sorted(members(data))
    extra = [m for m in got if m.startswith("PDFs/") and m.endswith(".pdf")] + [m for m in got if m.startswith("Images/Image")]
    assert len(extra) == 2
    assert sorted(set(got) - set(extra)) == t_members
    # metadata key set matches the template's SessionInfo
    with zipfile.ZipFile(template) as z:
        t_meta = Archive(plistlib.loads(z.read(next(n for n in z.namelist() if n.endswith("metadata.plist")))))
    meta = Archive(plistlib.loads(members(data)["metadata.plist"]))
    assert key_set(meta.root) == key_set(t_meta.root)
    assert meta.string(meta.get(meta.root, "noteName")) == "Synthetictest note"
    assert plistlib.loads(members(data)["Recordings/library.plist"]) == {
        "application version": "4631", "library-format-version": "1.0", "recordings": {}}


def test_array_invariants():
    data = write_note(synthetic_document(), Opts())
    pl, a = load_session(data)
    hs = hash_of(a, a.root)
    nc = a.integer(hs["numcurves"])
    npts = a.integer(hs["numpoints"])
    nfw = a.integer(hs["numfractionalwidths"])
    assert nc == 5
    assert len(hs["curvesnumpoints"]) == 4 * nc
    assert len(hs["curveswidth"]) == 4 * nc
    assert len(hs["curvescolors"]) == 4 * nc
    assert len(hs["eventTokens"]) == 4 * nc
    assert len(hs["curvesstyles"]) == nc
    assert len(hs["curvespoints"]) == 8 * npts
    assert len(hs["curvesfractionalwidths"]) == 4 * nfw
    counts = struct.unpack(f"<{nc}i", hs["curvesnumpoints"])
    assert all(n % 3 == 1 and n >= 4 for n in counts)
    assert sum(counts) == npts
    assert sum((n - 1) // 3 + 1 for n in counts) == nfw
    assert 3 * nfw - 2 * nc == npts
    assert struct.unpack(f"<{nc}i", hs["eventTokens"]) == (-1,) * nc
    assert set(hs["curvesstyles"]) <= {3, 4}
    fw = struct.unpack(f"<{nfw}f", hs["curvesfractionalwidths"])
    assert all(0.25 <= f <= 4.0 for f in fw)
    assert all(w > 0 for w in struct.unpack(f"<{nc}f", hs["curveswidth"]))
    assert a.array(hs["groupsArrays"]) == [] and a.dictionary(hs["bezierPathsDataDictionary"]) == {}


# ---------------------------------------------------------------------------------------
# synthetic 3-page document read back

def test_synthetic_roundtrip(samples, tmp_path):
    doc = synthetic_document()
    data = write_note(doc, Opts())
    assert doc.warnings == []
    pl, a = load_session(data)
    rich = a.get(a.root, "richText")
    curves = parse_curves(a, hash_of(a, a.root))
    assert len(curves) == 5

    # page layout: blank, PDF page 1, blank
    layout = [a.dictionary(d) for d in a.array(a.get(rich, "pageLayoutArray"))]
    assert layout[0] == {"kPageLayoutPDFPageNumberKey": INT64_MAX}
    assert layout[2] == {"kPageLayoutPDFPageNumberKey": INT64_MAX}
    assert layout[1]["kPageLayoutPDFPageNumberKey"] == 1 and layout[1]["kPageLayoutPDFIsOriginalPageKey"] is True
    pdf_obj = layout[1]["kPageLayoutPDFFileKey"]
    assert a.classname(pdf_obj) == "PDFFile"
    assert key_set(pdf_obj) == {"pageNumbers", "pdfFileName", "version_4_1_OrLater", "version", "contentBoxVersion", "highlights"}
    pdf_name = a.string(pdf_obj["pdfFileName"])
    assert pdf_name.endswith(".pdf") and pdf_name[:-4] == pdf_name[:-4].upper() and len(pdf_name) == 40
    files = a.array(a.get(rich, "pdfFiles"))
    assert len(files) == 1 and a.string(files[0]["pdfFileName"]) == pdf_name
    assert members(data)["PDFs/" + pdf_name] == doc.pdfs["pdf-a"]

    # y offsets: page 2 slot = ceil(842 * W / 595) with the PDF bottom-aligned in it, page 3 after it
    slot2 = math.ceil(842.0 * W / 595.0)
    gap2 = slot2 - 842.0 * W / 595.0
    s1 = W / GN_W
    y_of = [c["points"][0][1] for c in curves]
    x_of = [c["points"][0][0] for c in curves]
    assert y_of[0] == pytest.approx(10 * s1, abs=0.05) and x_of[0] == pytest.approx(10 * s1, abs=0.05)
    assert y_of[2] == pytest.approx(PLAIN_H + gap2 + 700 * W / 595.0, abs=0.05)
    assert x_of[2] == pytest.approx(100 * W / 595.0, abs=0.05)
    assert y_of[3] == pytest.approx(PLAIN_H + slot2 + 5 * s1, abs=0.05)
    assert y_of[4] == pytest.approx(PLAIN_H + slot2, abs=0.05)
    assert PLAIN_H + slot2 <= max(p[1] for p in curves[4]["points"]) <= PLAIN_H + slot2 + PLAIN_H
    # highlighter: style 4, alpha 0x6b, fractional widths 1.0; pen: style 3, pressure profile
    assert curves[1]["style"] == 4 and curves[1]["rgba"] == (255, 153, 0, 0x6B)
    assert all(f == 1.0 for f in curves[1]["fractional"])
    assert curves[0]["style"] == 3 and curves[0]["rgba"] == (0, 0, 0, 255)
    assert max(curves[0]["fractional"]) > 1.0 > min(curves[0]["fractional"])
    assert curves[0]["width"] == pytest.approx(1.5 * s1, rel=0.3)
    # dot became a tiny segment
    assert len(curves[3]["points"]) == 4
    # the chain is a plain Bezier input: anchors are the original points
    assert len(curves[0]["points"]) == 1 + 3 * 11

    # media
    media = a.array(a.get(rich, "mediaObjects"))
    assert [a.classname(m) for m in media] == ["ImageMediaObject", "TextBlockMediaObject"]
    img = media[0]
    assert a.string(img["documentOrigin"]) == point_string(20 * s1, 200 * s1)
    assert a.string(img["unscaledContentSize"]) == point_string(100 * s1, 60 * s1)
    snap = a.get(a.get(a.get(img, "figure"), "FigureBackgroundObjectKey"), "kImageObjectSnapshotKey")
    assert a.string(snap["relativePath"]) == "Images/Image .png" and snap["saveAsJPEG"] is False
    assert a.string(a.get(img, "figure")["FigureCropRectKey"]) == "{{0, 0}, {40, 24}}"
    assert members(data)["Images/Image .png"] == doc.pages[0].images[0].data
    assert [img["zIndex"], media[1]["zIndex"]] == [0, 1]
    box = media[1]
    store = a.get(box, "textStore")
    assert a.classname(a.get(store, "reflowState")) == "NBReflowStateReflowable"
    assert key_set(store) == key_set(rich)
    attributed = a.dictionary(a.get(store, "attributedString"))
    assert a.string(attributed["stringKey"]) == "Hello\nworld"
    subs = [a.dictionary(s) for s in a.array(attributed["subRangesKey"])]
    assert [a.string(s["subRangeRangeKey"]) for s in subs] == ["{0, 6}", "{6, 5}"]
    fonts = [a.dictionary(s["subRangeFontKey"]) for s in subs]
    assert a.string(fonts[0]["NSFontNameAttribute"]) == "HelveticaNeue-Bold"
    assert a.string(fonts[1]["NSFontNameAttribute"]) == "HelveticaNeue"
    assert fonts[0]["NSFontSizeAttribute"] == pytest.approx(12 * s1, abs=0.01)
    ts = a.dictionary(a.get(store, "recordingTimestampString"))
    assert a.string(ts["stringKey"]) == "Hello\nworld"
    assert [a.string(a.dictionary(s)["subRangeRangeKey"]) for s in a.array(ts["subRangesKey"])] == ["{0, 11}"]
    backing = a.get(store, "NBAttributedBackingString")
    for key in ("NBAttributedBackingStringCodingKey", "NBAttributedLayoutStringCodingKey"):
        d = a.dictionary(a.get(backing, key))
        assert a.string(d["stringKey"]) == "Hello\nworld" and len(a.array(d["subRangesKey"])) == 2
    assert a.get(box, "paperIndex") == -1 and a.classname(a.get(box, "paperStyleObject")) == "Notability.NBPaperStyle"
    assert a.integer(a.get(hash_of(a, store), "numcurves")) == 0

    # thumbnails
    for name, w, h in (("thumb.png", 48, 63), ("thumb2x.png", 96, 126), ("thumb3x.png", 144, 189), ("thumb6x.png", 288, 378)):
        assert image_pixel_size(members(data)[name]) == (w, h)

    # the scratchpad oracle reader, when available, agrees
    oracle = load_notereader(samples)
    if oracle is not None:
        tmp = tmp_path / "synthetic.note"
        tmp.write_bytes(data)
        d = oracle.read_note(str(tmp))
        assert len(d["curves"]) == 5 and d["width"] == W
        assert [p["kind"] for p in d["pages"]] == ["blank", "pdf", "blank"]
        bounds = oracle.page_bounds(d)
        assert bounds[1] == (PLAIN_H, PLAIN_H + slot2)
        assert all(bounds[i][0] <= c["points"][0][1] < bounds[i][1] for i, c in ((0, curves[0]), (1, curves[2]), (2, curves[3])))
        assert [m["$class"] for m in d["media"]] == ["ImageMediaObject", "TextBlockMediaObject"]
        assert d["media"][1]["text"] == "Hello\nworld"


def test_project_reader_roundtrip():
    """Cross-check with gnnote.notability.reader when it is integrated."""
    try:
        from gnnote.notability.reader import read_note
    except Exception:  # noqa: BLE001
        pytest.skip("gnnote.notability.reader not available")
    doc = synthetic_document()
    back = read_note(write_note(doc, Opts()))
    assert len(back.pages) == 3
    assert [len(p.strokes) for p in back.pages] == [2, 1, 2]
    assert len(back.pages[0].images) == 1 and len(back.pages[0].texts) == 1
    assert back.pages[0].texts[0].text == "Hello\nworld"
    assert back.pages[1].background is not None
    assert back.pages[0].strokes[1].kind == "highlighter"
    # positions: plain pages are 612 x 803.25 pt, 455.04 pt page scaled by W/455.04 then back by 612/W
    factor = 612.0 / GN_W
    p = back.pages[0].strokes[0].points[0]
    assert (p.x, p.y) == pytest.approx((10 * factor, 10 * factor), abs=0.1)
    q = back.pages[1].strokes[0].points[0]
    assert (q.x, q.y) == pytest.approx((100.0, 700.0), abs=0.1)
    assert back.pages[0].strokes[0].points[0].width == pytest.approx(1.5 * factor, rel=0.35)


# ---------------------------------------------------------------------------------------
# comparison with the proof-of-concept output

def test_matches_poc_output(samples):
    env = os.environ.get("GNNOTE_POC_NOTE")
    poc = Path(env) if env else scratchpad_file(samples, "poc", "GoodNotes test import.note")
    if poc is None or not poc.is_file():
        pytest.skip("PoC .note output not available")
    from gnnote.notability.keyedarchive import Archive
    with zipfile.ZipFile(poc) as z:
        poc_members = sorted(n.split("/", 1)[1] for n in z.namelist())
        p_pl = plistlib.loads(z.read(next(n for n in z.namelist() if n.endswith("Session.plist"))))
        p_meta = Archive(plistlib.loads(z.read(next(n for n in z.namelist() if n.endswith("metadata.plist")))))
    p = Archive(p_pl)

    doc = Document(title="GoodNotes test import", source_format="goodnotes")
    doc.pages = [Page(GN_W, GN_H, strokes=[line(20, 30, 200, 60), line(40, 80, 300, 400, n=30), Stroke([Point(10, 10, 2)])])]
    data = write_note(doc, Opts(title="GoodNotes test import"))
    pl, a = load_session(data)

    assert sorted(members(data)) == poc_members
    classes = lambda plist: {o["$classname"] for o in plist["$objects"] if isinstance(o, dict) and "$classname" in o}
    assert classes(pl) == classes(p_pl)
    assert key_set(a.root) == key_set(p.root)
    assert key_set(a.get(a.root, "richText")) == key_set(p.get(p.root, "richText"))
    assert key_set(hash_of(a, a.root)) == key_set(hash_of(p, p.root))
    assert a.string(a.get(a.root, "name")) == p.string(p.get(p.root, "name")) == "GoodNotes test import"
    assert a.string(a.get(a.root, "subject")) == p.string(p.get(p.root, "subject"))
    assert a.array(a.get(a.get(a.root, "richText"), "pageLayoutArray")) == []
    meta = Archive(plistlib.loads(members(data)["metadata.plist"]))
    assert key_set(meta.root) == key_set(p_meta.root)
    assert len(meta.string(meta.get(meta.root, "uuidKey"))) == 36
    # the PoC pads a dot to n = 4, so do we
    curves = parse_curves(a, hash_of(a, a.root))
    assert [len(c["points"]) for c in curves] == [1 + 3 * 11, 1 + 3 * 29, 4]


# ---------------------------------------------------------------------------------------
# options

def test_pressure_off_and_simplify():
    doc = Document(pages=[Page(GN_W, GN_H, strokes=[line(0, 0, 100, 0, n=40, w=2.0)])])
    pl, a = load_session(write_note(doc, Opts(pressure=False)))
    c = parse_curves(a, hash_of(a, a.root))[0]
    assert all(f == 1.0 for f in c["fractional"]) and len(c["points"]) == 1 + 3 * 39
    doc = Document(pages=[Page(GN_W, GN_H, strokes=[line(0, 0, 100, 0, n=40, w=2.0)])])
    pl, a = load_session(write_note(doc, Opts(simplify=0.5)))
    c = parse_curves(a, hash_of(a, a.root))[0]
    assert len(c["points"]) == 4  # a straight line collapses to one segment
    assert c["points"][0] == pytest.approx((0, 0)) and c["points"][-1][0] == pytest.approx(100 * W / GN_W, abs=0.01)


def test_bezier_input_kept_exactly():
    anchors = [Point(10, 10, 1), Point(50, 20, 1), Point(90, 10, 1)]
    controls = [(Point(20, 0, 1), Point(40, 0, 1)), (Point(60, 40, 1), Point(80, 40, 1))]
    doc = Document(pages=[Page(W, W * LEGACY_ASPECT, strokes=[Stroke(anchors, controls=controls)])])  # scale 1
    pl, a = load_session(write_note(doc, Opts()))
    c = parse_curves(a, hash_of(a, a.root))[0]
    assert [tuple(round(v, 3) for v in pt) for pt in c["points"]] == [
        (10, 10), (20, 0), (40, 0), (50, 20), (60, 40), (80, 40), (90, 10)]
    assert doc.warnings == []


def test_scale_to_fit_tall_pages():
    doc = Document(pages=[Page(595.0, 842.0, strokes=[line(0, 0, 595, 842)])])
    pl, a = load_session(write_note(doc, Opts()))
    c = parse_curves(a, hash_of(a, a.root))[0]
    ys = [p[1] for p in c["points"]]
    xs = [p[0] for p in c["points"]]
    assert max(ys) <= PLAIN_H + 1e-6 and max(ys) == pytest.approx(PLAIN_H, abs=0.01)
    assert min(xs) > 0 and max(xs) < W  # centred horizontally
    assert any("scaled down" in w for w in doc.warnings)


def test_paper_pdf_mode_backs_every_page():
    doc = Document(title="pdf mode", source_format="goodnotes")
    doc.pdfs["tpl"] = paper_pdf(GN_W, GN_H)
    doc.pages = [
        Page(GN_W, GN_H, strokes=[line(0, 0, 10, 10)], background=PdfBackground("tpl", 0), template_is_builtin=True, paper="lined"),
        Page(595.0, 842.0, strokes=[line(0, 0, 10, 10)], paper="grid"),
        Page(595.0, 842.0, strokes=[line(0, 0, 10, 10)], paper="grid"),
    ]
    data = write_note(doc, Opts(paper="pdf"))
    pl, a = load_session(data)
    rich = a.get(a.root, "richText")
    layout = [a.dictionary(d) for d in a.array(a.get(rich, "pageLayoutArray"))]
    assert len(layout) == 3 and all("kPageLayoutPDFFileKey" in d for d in layout)
    files = a.array(a.get(rich, "pdfFiles"))
    assert len(files) == 2  # the built-in template plus one generated paper shared by pages 2-3
    assert len([m for m in members(data) if m.startswith("PDFs/") and m.endswith(".pdf")]) == 2
    assert members(data)["PDFs/" + a.string(files[0]["pdfFileName"])] == doc.pdfs["tpl"]
    curves = parse_curves(a, hash_of(a, a.root))
    slot1 = math.ceil(GN_H * W / GN_W)
    slot2 = math.ceil(842.0 * W / 595.0)
    gap1 = slot1 - GN_H * W / GN_W
    gap2 = slot2 - 842.0 * W / 595.0
    assert curves[0]["points"][0][1] == pytest.approx(gap1, abs=0.01)
    assert curves[1]["points"][0][1] == pytest.approx(slot1 + gap2, abs=0.01)
    assert curves[2]["points"][0][1] == pytest.approx(slot1 + slot2 + gap2, abs=0.01)
    # in plain mode the same document keeps the built-in template as plain paper
    doc2 = Document(title="plain", pdfs=dict(doc.pdfs), pages=[Page(GN_W, GN_H, background=PdfBackground("tpl", 0), template_is_builtin=True)])
    pl, a = load_session(write_note(doc2, Opts(paper="plain")))
    assert a.array(a.get(a.get(a.root, "richText"), "pageLayoutArray")) == []


def test_tolerant_of_bad_input():
    doc = Document(title="odd", source_format="goodnotes")
    doc.pages = [
        Page(GN_W, GN_H, strokes=[Stroke([]), Stroke([Point(1, 1, 0), Point(1, 1, 0)]),
                                  Stroke([Point(0, 0), Point(5, 5), Point(9, 1)], controls=[(Point(1, 1), Point(2, 2))])],
             images=[Image(0, 0, 10, 10, b"", "png"), Image(0, 0, 10, 10, b"not an image", "jpeg")],
             texts=[TextBox(0, 0, 10, 10, "ab", runs=[TextRun("zz")])],
             background=PdfBackground("missing", 3)),
        Page(0, 0),
    ]
    data = write_note(doc, Opts(paper="pdf", notability_page_width=0))
    pl, a = load_session(data)
    curves = parse_curves(a, hash_of(a, a.root))
    assert len(curves) == 2  # empty stroke dropped, degenerate dot kept, bad controls re-fitted
    assert any("missing" in w for w in doc.warnings)
    assert any("re-fitted" in w for w in doc.warnings)
    media = a.array(a.get(a.get(a.root, "richText"), "mediaObjects"))
    assert [a.classname(m) for m in media] == ["ImageMediaObject", "TextBlockMediaObject"]
    assert write_note(Document(), None)  # no pages, no options


def test_title_sanitised_and_unicode():
    doc = Document(title="Poznámky 2026/10: test", pages=[Page(GN_W, GN_H)])
    data = write_note(doc, Opts())
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        prefixes = {n.split("/", 1)[0] for n in z.namelist()}
    assert prefixes == {"Poznámky 202610 test"}
    pl, a = load_session(data)
    assert a.string(a.get(a.root, "packagePath")) == "Poznámky 202610 test"
    data = write_note(Document(title="x", pages=[Page(GN_W, GN_H)]), Opts(title="Custom"))
    pl, a = load_session(data)
    assert a.string(a.get(a.root, "name")) == "Custom"


def test_image_pixel_size_jpeg_and_png():
    assert image_pixel_size(white_png(7, 9)) == (7, 9)
    jpeg = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xc0\x00\x11\x08\x00\x20\x00\x30\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9")
    assert image_pixel_size(jpeg) == (48, 32)
    assert image_pixel_size(b"garbage") is None
