"""Tests for gnnote.notability.keyedarchive and gnnote.notability.reader.

Sample-based tests run on every ``.note`` file the reference repositories provide
(Notability 4.2 ... 16.1.5) and cross-check stroke counts against the raw ``numcurves``
fields read independently with plistlib, and page assignment against the app's own
``HandwritingIndex`` cache.  Synthetic tests build a tiny note from scratch so the reader
can be exercised without any sample repository.
"""
from __future__ import annotations

import io
import math
import plistlib
import struct
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from gnnote.model import Document
from gnnote.notability import keyedarchive as ka
from gnnote.notability import x_inset
from gnnote.notability.reader import (
    LEGACY_ASPECT, PLAIN_PAGE_HEIGHT_PT, PLAIN_PAGE_WIDTH_PT, TEXT_PAD_X, read_note,
)

UID = plistlib.UID
INT64_MAX = 0x7FFFFFFFFFFFFFFF


# ----------------------------------------------------------------------------------
# Sample discovery
# ----------------------------------------------------------------------------------


def _all_note_files(samples) -> List[Path]:
    files = list(samples.note_files())
    extra = samples.root / "more-notes" / "notability-lib-private"
    if extra.is_dir():
        files += sorted(p for p in extra.rglob("*.note") if p.is_file())
    return files


def _bdb_note(samples, tmp_path_factory) -> bytes:
    """The extracted 8.4.8 note of notability-reader, zipped the way Notability exports it."""
    folder = samples.repo("notability-reader") / "bdb_transazioni"
    if not (folder / "Session.plist").is_file():
        pytest.skip("bdb_transazioni sample not available")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, "bdb_transazioni/" + path.relative_to(folder).as_posix())
    return buf.getvalue()


def _sample(samples, repo: str, *parts: str) -> Path:
    path = samples.repo(repo).joinpath(*parts)
    if not path.is_file():
        pytest.skip(f"sample {path.name} not available")
    return path


# ----------------------------------------------------------------------------------
# Independent oracle: raw numcurves of the top-level hash plus every group archive
# ----------------------------------------------------------------------------------


def _raw_session(data: bytes) -> Tuple[Dict[str, Any], str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.endswith("Session.plist")]
        name = sorted(names, key=lambda n: (n.count("/"), len(n)))[0]
        root = name[: -len("Session.plist")]
        return plistlib.loads(zf.read(name)), root


def _raw_deref(objs: List[Any], v: Any) -> Any:
    while isinstance(v, UID):
        v = objs[v.data]
    return v


def _raw_array(objs: List[Any], v: Any) -> List[Any]:
    v = _raw_deref(objs, v)
    if not isinstance(v, dict):
        return []
    if "NS.objects" in v:
        return [_raw_deref(objs, x) for x in v["NS.objects"]]
    out, i = [], 0
    while f"NS.object.{i}" in v:
        out.append(_raw_deref(objs, v[f"NS.object.{i}"]))
        i += 1
    return out


def _raw_hash_curves(pl: Dict[str, Any], hash_obj: Any) -> int:
    objs = pl["$objects"]
    h = _raw_deref(objs, hash_obj)
    if not isinstance(h, dict):
        return 0
    total = int(_raw_deref(objs, h.get("numcurves", 0)) or 0)
    for blob in _raw_array(objs, h.get("groupsArrays")):
        group = plistlib.loads(_raw_deref(objs, blob))
        for obj in group["inkGroup"]["inkGroupObjects"]:
            if obj.get("type") == 1:
                nested = plistlib.loads(obj["object"])
                top = nested["$top"]
                root_ref = top.get("root", top.get("$0"))
                total += _raw_hash_curves(nested, root_ref)
    return total


def _oracle_curve_count(data: bytes) -> int:
    pl, _ = _raw_session(data)
    objs = pl["$objects"]
    top = pl["$top"]
    root = _raw_deref(objs, top.get("root", top.get("$0")))
    rich = _raw_deref(objs, root["richText"])
    overlay = _raw_deref(objs, rich.get("Handwriting Overlay"))
    if not isinstance(overlay, dict):
        return 0
    return _raw_hash_curves(pl, overlay.get("SpatialHash"))


def _handwriting_index(data: bytes) -> Dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.endswith("HandwritingIndex/index.plist"):
                return plistlib.loads(zf.read(name)).get("pages", {})
    return {}


# ----------------------------------------------------------------------------------
# keyedarchive unit tests
# ----------------------------------------------------------------------------------


def _archive(objects: List[Any], top_key: str = "$0") -> ka.Archive:
    return ka.Archive({"$version": 100000, "$archiver": "GLKeyedArchiver",
                       "$top": {top_key: UID(1)}, "$objects": objects})


def test_archive_resolves_uid_chains_and_null():
    a = _archive(["$null", {"a": UID(2), "b": UID(0), "c": 5}, UID(3), "hello"])
    assert a.root["c"] == 5
    assert a.get(a.root, "a") == "hello"
    assert a.get(a.root, "b", "dflt") == "dflt"
    assert a.deref(UID(99)) is None
    assert ka.is_null("$null") and ka.is_null(None) and not ka.is_null("")


def test_archive_root_key_variants():
    assert _archive(["$null", {"x": 1}], "root").root == {"x": 1}
    assert _archive(["$null", {"x": 1}], "$0").root == {"x": 1}
    assert ka.Archive({}).root is None


def test_archive_strings():
    a = _archive(["$null", {}, {"NS.bytes": b"caf\xc3\xa9"}, {"NS.string": UID(4)}, "plain", UID(0)])
    assert a.string(UID(2)) == "café"
    assert a.string(UID(3)) == "plain"
    assert a.string(UID(5)) is None
    assert a.string("$null") is None
    assert a.string(12) is None


def test_archive_array_both_encodings():
    a = _archive(["$null", {}, {"NS.objects": [UID(4), 2]}, {"NS.object.1": "b", "NS.object.0": UID(4), "NS.object.2": "c"}, "x"])
    assert a.array(UID(2)) == ["x", 2]
    assert a.array(UID(3)) == ["x", "b", "c"]
    assert a.array(UID(0)) == []
    assert a.array([UID(4), 1]) == ["x", 1]


def test_archive_dictionary_both_encodings():
    objs = ["$null", {},
            {"NS.keys": [UID(4), "k2"], "NS.objects": [UID(5), 7]},
            {"NS.key.1": "second", "NS.object.1": UID(5), "NS.key.0": UID(4), "NS.object.0": 1},
            "k1", "value", {"NS.keys": [3], "NS.objects": ["three"]}]
    a = _archive(objs)
    assert a.dictionary(UID(2)) == {"k1": "value", "k2": 7}
    assert a.dictionary(UID(3)) == {"k1": 1, "second": "value"}
    assert a.dictionary(UID(6)) == {"3": "three"}  # numeric keys become strings
    assert a.dictionary(UID(0)) == {}


def test_archive_classname_and_scalars():
    objs = ["$null", {"$class": UID(2), "n": UID(3), "d": {"NS.time": 0.0}},
            {"$classname": "InkedSpatialHash", "$classes": ["InkedSpatialHash", "NSObject"]}, 42]
    a = _archive(objs)
    assert a.classname(a.root) == "InkedSpatialHash"
    assert a.classes(a.root) == ["InkedSpatialHash", "NSObject"]
    assert a.classname("str") is None
    assert a.integer(UID(3)) == 42 and a.number(UID(3)) == 42.0
    assert a.integer("oops", -1) == -1
    assert a.boolean(1) is True and a.boolean("x", True) is True
    assert a.data(b"ab") == b"ab" and a.data("ab") == b""
    assert a.date(a.root["d"]) == ka.APPLE_EPOCH_OFFSET


def test_geometry_strings():
    assert ka.parse_point("{144.98763002386684, 270.54752563027648}") == (144.98763002386684, 270.54752563027648)
    assert ka.parse_point("{0, 0}") == (0.0, 0.0)
    assert ka.parse_point("{-15.2, 1e2}") == (-15.2, 100.0)
    assert ka.parse_point("nope") is None and ka.parse_point(None) is None
    assert ka.parse_rect("{{0, 797.5}, {2250, 1550.52}}") == (0.0, 797.5, 2250.0, 1550.52)
    assert ka.parse_rect("{0, 0}") is None
    assert ka.parse_range("{32, 10}") == (32, 10)
    assert ka.parse_color_string("0.000000000000000,0.435294121503830,1.000000000000000,1.000000000000000") == (0.0, 0.43529412150383, 1.0, 1.0)
    assert ka.parse_color_string("0.5,1") == (0.5, 0.5, 0.5, 1.0)
    assert ka.parse_color_string("x,y,z,w") is None


def test_color_from_uicolor():
    a = _archive(["$null", {}])
    assert ka.color_from_uicolor(a, {"UIColorComponentCount": 4, "UIRed": 1.0, "UIGreen": 0.5, "UIBlue": 0.0, "UIAlpha": 0.42}) == (1.0, 0.5, 0.0, 0.42)
    assert ka.color_from_uicolor(a, {"UIColorComponentCount": 2, "UIWhite": 0.3, "UIAlpha": 1.0}) == (0.3, 0.3, 0.3, 1.0)
    assert ka.color_from_uicolor(a, {"NSRGB": b"0 0.5 1\x00"}) == (0.0, 0.5, 1.0, 1.0)
    assert ka.color_from_uicolor(a, "$null") is None


def test_load_archive_roundtrip():
    pl = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": UID(1)},
          "$objects": ["$null", {"name": UID(2)}, {"NS.bytes": b"n"}]}
    a = ka.load_archive(plistlib.dumps(pl, fmt=plistlib.FMT_BINARY))
    assert a.string(a.get(a.root, "name")) == "n"
    with pytest.raises(ValueError):
        ka.load_archive(plistlib.dumps([1, 2]))


# ----------------------------------------------------------------------------------
# Synthetic notes (no sample repository needed)
# ----------------------------------------------------------------------------------


def _ns_array(objs: List[Any], items: List[Any]) -> UID:
    objs.append({"$class": UID(2), "NS.objects": items})
    return UID(len(objs) - 1)


def _ns_dict(objs: List[Any], d: Dict[str, Any]) -> UID:
    objs.append({"$class": UID(3), "NS.keys": list(d.keys()), "NS.objects": list(d.values())})
    return UID(len(objs) - 1)


def _add(objs: List[Any], obj: Any) -> UID:
    objs.append(obj)
    return UID(len(objs) - 1)


def _ink_arrays(curves: List[Dict[str, Any]], styles: bytes | None = b"") -> Dict[str, Any]:
    """Build the parallel arrays from a list of {points, width, fractional, rgba}."""
    pts = b"".join(struct.pack("<2f", *p) for c in curves for p in c["points"])
    nums = b"".join(struct.pack("<i", len(c["points"])) for c in curves)
    widths = b"".join(struct.pack("<f", c["width"]) for c in curves)
    fw = b"".join(struct.pack("<f", v) for c in curves for v in c["fractional"])
    colors = b"".join(bytes(c["rgba"]) for c in curves)
    d = {"numcurves": len(curves), "numpoints": sum(len(c["points"]) for c in curves),
         "numfractionalwidths": sum(len(c["fractional"]) for c in curves),
         "curvespoints": pts, "curvesnumpoints": nums, "curveswidth": widths,
         "curvesfractionalwidths": fw, "curvescolors": colors,
         "eventTokens": b"\xff\xff\xff\xff" * len(curves)}
    if styles is not None:
        d["curvesstyles"] = styles
    return d


def _build_note(width: float = 565.0, curves=None, styles: bytes | None = b"", layout=None,
                pdfs: Dict[str, bytes] | None = None, media=None, flow_text: str = "",
                groups: List[bytes] | None = None, name: str = "Synthetic",
                extra_hash: Dict[str, Any] | None = None, metadata: bool = True) -> bytes:
    objs: List[Any] = ["$null", None,
                       {"$classname": "NSArray", "$classes": ["NSArray", "NSObject"]},
                       {"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]}]
    hash_dict: Dict[str, Any] = {"$class": _add(objs, {"$classname": "InkedSpatialHash"})}
    hash_dict.update(_ink_arrays(curves or [], styles))
    if groups:
        hash_dict["groupsArrays"] = _ns_array(objs, [_add(objs, g) for g in groups])
    if extra_hash:
        hash_dict.update(extra_hash)
    hash_uid = _add(objs, hash_dict)
    overlay = _add(objs, {"$class": _add(objs, {"$classname": "HandwritingObject"}), "SpatialHash": hash_uid})
    reflow = _add(objs, {"$class": _add(objs, {"$classname": "NBReflowStateLocked"}),
                         "pageWidthInDocumentCoordsKey": width, "nativeLayoutDeviceStringKey": "iPad"})
    pdf_cls = _add(objs, {"$classname": "PDFFile"})
    pdf_objs: Dict[str, UID] = {}
    for fname in (pdfs or {}):
        pdf_objs[fname] = _add(objs, {"$class": pdf_cls, "pdfFileName": fname, "contentBoxVersion": 1,
                                      "version": 2, "highlights": _ns_array(objs, []), "pageNumbers": UID(0)})
    layout_items = []
    for entry in (layout or []):
        if entry is None:
            layout_items.append(_ns_dict(objs, {"kPageLayoutPDFPageNumberKey": INT64_MAX}))
        else:
            fname, page = entry
            layout_items.append(_ns_dict(objs, {"kPageLayoutPDFPageNumberKey": page,
                                                "kPageLayoutPDFFileKey": pdf_objs[fname],
                                                "kPageLayoutPDFIsOriginalPageKey": True}))
    flow = _ns_dict(objs, {"stringKey": flow_text, "subRangesKey": _ns_array(objs, [
        _ns_dict(objs, {"subRangeRangeKey": "{0, %d}" % len(flow_text),
                        "subRangeFontKey": _ns_dict(objs, {"NSFontNameAttribute": "HelveticaNeue-Bold", "NSFontSizeAttribute": 12}),
                        "subRangeColorCrossPlatformKey": "0.000000000000000,0.435294121503830,1.000000000000000,1.000000000000000"})])})
    rich = _add(objs, {"$class": _add(objs, {"$classname": "FormattedString"}), "formatVersion": 4,
                       "Handwriting Overlay": overlay, "reflowState": reflow, "didBecomeReflowable": True,
                       "pdfFiles": _ns_array(objs, list(pdf_objs.values())),
                       "pageLayoutArray": _ns_array(objs, layout_items),
                       "mediaObjects": _ns_array(objs, [m(objs) for m in (media or [])]),
                       "attributedString": flow})
    objs[1] = {"$class": _add(objs, {"$classname": "NoteTakingSession"}), "richText": rich, "name": name,
               "packagePath": name, "sessionFormatVersion": 5, "subject": "unsortedNotesKey"}
    session = plistlib.dumps({"$version": 100000, "$archiver": "GLKeyedArchiver", "$top": {"$0": UID(1)},
                              "$objects": objs}, fmt=plistlib.FMT_BINARY)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/Session.plist", session)
        if metadata:
            meta = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": UID(1)},
                    "$objects": ["$null", {"$class": UID(2), "noteName": UID(3), "noteHasRecordingKey": False},
                                 {"$classname": "SessionInfo"}, {"$class": UID(4), "NS.bytes": name.encode()},
                                 {"$classname": "NSMutableString"}]}
            zf.writestr(f"{name}/metadata.plist", plistlib.dumps(meta, fmt=plistlib.FMT_BINARY))
        for fname, data in (pdfs or {}).items():
            zf.writestr(f"{name}/PDFs/{fname}", data)
    return buf.getvalue()


def _minimal_pdf(width: float, height: float, pages: int = 1) -> bytes:
    """A tiny valid PDF with ``pages`` pages of the given MediaBox."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    for _ in range(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:g} {height:g}] >>".encode())
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


def _curve(points, width=2.0, fractional=None, rgba=(0, 0, 0, 255)):
    k = (len(points) - 1) // 3
    return {"points": points, "width": width, "fractional": fractional or [1.0] * (k + 1), "rgba": rgba}


def test_synthetic_plain_note_strokes_scale_and_pages():
    W = 565.0
    H = W * LEGACY_ASPECT
    curves = [
        _curve([(10, 20), (12, 22), (14, 24), (16, 26)], width=2.0, fractional=[0.5, 1.5], rgba=(237, 54, 36, 255)),
        _curve([(100, H + 30)], width=1.0),  # a dot on page 2
        _curve([(5, 2 * H + 5), (6, 2 * H + 6), (7, 2 * H + 7), (8, 2 * H + 8)], width=3.0, rgba=(250, 157, 0, 107)),
    ]
    doc = read_note(_build_note(W, curves, styles=bytes([3, 3, 4])))
    assert doc.source_format == "notability"
    assert doc.title == "Synthetic"
    assert len(doc.pages) == 3
    assert all((p.width, p.height) == (PLAIN_PAGE_WIDTH_PT, PLAIN_PAGE_HEIGHT_PT) for p in doc.pages)
    scale = PLAIN_PAGE_WIDTH_PT / W
    inset = -x_inset(W)  # the paper's left edge sits at document x = -W * 20 / 768
    s0 = doc.pages[0].strokes[0]
    assert s0.is_bezier and len(s0.controls) == 1 and len(s0.points) == 2
    assert s0.points[0].x == pytest.approx((10 + inset) * scale) and s0.points[0].y == pytest.approx(20 * scale)
    assert s0.points[1].x == pytest.approx((16 + inset) * scale)
    assert s0.controls[0][0].x == pytest.approx((12 + inset) * scale) and s0.controls[0][1].y == pytest.approx(24 * scale)
    assert s0.width == pytest.approx(2.0 * scale)
    assert s0.points[0].width == pytest.approx(1.0 * scale) and s0.points[1].width == pytest.approx(3.0 * scale)
    assert s0.color == pytest.approx((237 / 255, 54 / 255, 36 / 255, 1.0))
    assert s0.kind == "pen" and s0.pen is None
    dot = doc.pages[1].strokes[0]
    assert len(dot.points) == 1 and dot.controls == [] and dot.points[0].y == pytest.approx(30 * scale)
    hl = doc.pages[2].strokes[0]
    assert hl.kind == "highlighter" and hl.points[0].y == pytest.approx(5 * scale)


def test_synthetic_styles_short_or_absent_fall_back_to_alpha():
    curves = [_curve([(1, 1)], rgba=(0, 0, 0, 255)), _curve([(2, 2)], rgba=(0, 0, 255, 0x6B)),
              _curve([(3, 3)], rgba=(9, 9, 9, 255))]
    doc = read_note(_build_note(curves=curves, styles=bytes([5])))  # shorter than numcurves
    kinds = [(s.kind, s.pen) for s in doc.pages[0].strokes]
    assert kinds == [("pen", "pencil"), ("highlighter", None), ("pen", None)]
    doc = read_note(_build_note(curves=curves, styles=None))  # key absent (Notability <= 8.x)
    assert [s.kind for s in doc.pages[0].strokes] == ["pen", "highlighter", "pen"]


def test_synthetic_pdf_layout_slots_and_sizes():
    W = 565.0
    a4 = _minimal_pdf(595.0, 842.0, pages=2)
    land = _minimal_pdf(842.0, 595.0)
    slot_a4 = math.ceil(842.0 * W / 595.0)  # 800
    slot_land = math.ceil(595.0 * W / 842.0)  # 400
    H = W * LEGACY_ASPECT
    curves = [
        _curve([(10, 10)]),                       # blank page 1
        _curve([(10, H + slot_a4 - 1)]),          # last row of PDF page 2 (A4 p1)
        _curve([(10, H + slot_a4 + 10)]),         # PDF page 3 (A4 p2)
        _curve([(10, H + 2 * slot_a4 + 10)]),     # landscape page 4
        _curve([(10, H + 2 * slot_a4 + slot_land + 10)]),  # implicit plain page 5
    ]
    data = _build_note(W, curves, layout=[None, ("A.pdf", 1), ("A.pdf", 2), ("L.pdf", 1)],
                       pdfs={"A.pdf": a4, "L.pdf": land})
    doc = read_note(data)
    assert set(doc.pdfs) == {"A", "L"}
    assert len(doc.pages) == 5
    kinds = [(p.background.pdf_id, p.background.page_index) if p.background else None for p in doc.pages]
    assert kinds == [None, ("A", 0), ("A", 1), ("L", 0), None]
    assert (doc.pages[1].width, doc.pages[1].height) == (595.0, 842.0)
    assert (doc.pages[3].width, doc.pages[3].height) == (842.0, 595.0)
    assert (doc.pages[4].width, doc.pages[4].height) == (PLAIN_PAGE_WIDTH_PT, PLAIN_PAGE_HEIGHT_PT)
    assert all(len(p.strokes) == 1 for p in doc.pages)
    # the stroke on the last row of the A4 slot maps inside the 842 pt page (content bottom-aligned)
    y = doc.pages[1].strokes[0].points[0].y
    assert 0 <= y <= 842.0 and y == pytest.approx((slot_a4 - 1 - (slot_a4 - 842.0 * W / 595.0)) * 595.0 / W)
    assert not doc.pages[1].template_is_builtin


def test_synthetic_damaged_arrays_warn_instead_of_raising():
    good = _curve([(1, 1), (2, 2), (3, 3), (4, 4)])
    data = _build_note(curves=[good, good])
    # Truncate curvespoints: announce two curves but store one
    pl, root = _raw_session(data)
    objs = pl["$objects"]
    for o in objs:
        if isinstance(o, dict) and "curvespoints" in o:
            o["curvespoints"] = o["curvespoints"][:32]
            o["curvesfractionalwidths"] = b""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(root + "Session.plist", plistlib.dumps(pl, fmt=plistlib.FMT_BINARY))
    doc = read_note(buf.getvalue())
    assert len(doc.pages[0].strokes) == 1
    assert any("curvespoints" in w for w in doc.warnings)
    assert doc.pages[0].strokes[0].points[0].width == pytest.approx(2.0 * PLAIN_PAGE_WIDTH_PT / 565.0)


def test_synthetic_group_transform_and_pencil():
    inner_objs: List[Any] = ["$null", None, {"$classname": "InkObjectEncoder"}]
    inner_hash = {"$class": UID(2)}
    inner_hash.update(_ink_arrays([_curve([(10, 10), (11, 11), (12, 12), (13, 13)], width=2.0)], styles=bytes([5])))
    inner_objs[1] = inner_hash
    inner = plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": UID(1)},
                            "$objects": inner_objs}, fmt=plistlib.FMT_BINARY)
    group = plistlib.dumps({"index": 0, "inkGroup": {"transform": [0.5, 0.0, 0.0, 0.5, 100.0, 200.0],
                                                      "inkGroupObjects": [{"type": 1, "object": inner},
                                                                          {"type": 2, "object": {"kind": "line"}}]}})
    top = [_curve([(1, 1)])]
    doc = read_note(_build_note(curves=top, groups=[group]))
    strokes = doc.pages[0].strokes
    assert len(strokes) == 2
    grouped = strokes[1]
    scale = PLAIN_PAGE_WIDTH_PT / 565.0
    assert grouped.pen == "pencil"
    assert grouped.points[0].x == pytest.approx((0.5 * 10 + 100 - x_inset(565.0)) * scale)
    assert grouped.points[1].y == pytest.approx((0.5 * 13 + 200) * scale)
    assert grouped.width == pytest.approx(2.0 * 0.5 * scale)
    assert any("shape" in w for w in doc.warnings)


def test_synthetic_flow_text_and_dash_warning():
    dash = plistlib.dumps({"objectPatterns": {"0": {"pattern": 1}}})
    doc = read_note(_build_note(curves=[_curve([(1, 1)])], flow_text="Hello\nWorld", extra_hash={"dashStyles": dash}))
    page = doc.pages[0]
    assert len(page.texts) == 1
    box = page.texts[0]
    assert box.text == "Hello\nWorld"
    assert box.x == 36.0 and box.w == pytest.approx(PLAIN_PAGE_WIDTH_PT - 72.0)
    assert box.runs[0].bold and not box.runs[0].italic
    assert box.runs[0].size == pytest.approx(12 * PLAIN_PAGE_WIDTH_PT / 565.0)
    assert box.color == pytest.approx((0.0, 0.43529412150383, 1.0, 1.0))
    assert any("Dashed" in w for w in doc.warnings)
    assert any("Typed page text" in w for w in doc.warnings)


def test_title_falls_back_to_session_name():
    doc = read_note(_build_note(name="FromSession", metadata=False))
    assert doc.title == "FromSession"


def test_rejects_non_notes():
    with pytest.raises(ValueError):
        read_note(b"not a zip at all")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x/other.plist", b"hi")
    with pytest.raises(ValueError):
        read_note(buf.getvalue())
    with pytest.raises(TypeError):
        read_note("string")  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------
# Sample files
# ----------------------------------------------------------------------------------


def _check_document(doc: Document, data: bytes) -> None:
    assert doc.source_format == "notability"
    assert doc.pages, "a note always has at least one page"
    assert len(doc.pages) <= 200
    assert doc.title
    strokes = [s for p in doc.pages for s in p.strokes]
    assert len(strokes) == _oracle_curve_count(data)
    for page in doc.pages:
        assert page.width > 0 and page.height > 0
        if page.background is not None:
            assert page.background.pdf_id in doc.pdfs
            assert page.background.page_index >= 0
        for s in page.strokes:
            assert s.points
            first = s.points[0]
            assert -0.05 * page.width <= first.x <= 1.05 * page.width, (first.x, page.width)
            assert -2.0 <= first.y < page.height + 1.0, (first.y, page.height)
            assert s.width > 0
            assert all(0.0 <= c <= 1.0 for c in s.color)
            if s.is_bezier:
                assert len(s.controls) == len(s.points) - 1
            assert all(p.width > 0 for p in s.points)
        for img in page.images:
            assert img.data and img.fmt in ("png", "jpeg", "gif")
            assert img.w > 0 and img.h > 0
        for box in page.texts:
            assert box.text and box.w > 0 and box.h > 0
    for w in doc.warnings:
        assert "\n" not in w


def test_every_sample_reads(samples):
    files = _all_note_files(samples)
    assert len(files) >= 18
    failures = []
    for path in files:
        data = path.read_bytes()
        try:
            doc = read_note(data)
            _check_document(doc, data)
        except Exception as exc:  # noqa: BLE001 - collected so one bad file shows all failures
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)


def test_bdb_pdf_backed_recording_note(samples, tmp_path_factory):
    data = _bdb_note(samples, tmp_path_factory)
    doc = read_note(data)
    _check_document(doc, data)
    assert doc.title == "bdb_transazioni"
    assert len(doc.pages) == 25
    assert len(doc.pdfs) == 1
    pdf_id = next(iter(doc.pdfs))
    assert pdf_id == "350DE7DB-7F68-4140-8E4D-54B6A8C0C2AA"
    for i, page in enumerate(doc.pages):
        assert page.background == type(page.background)(pdf_id, i)
        assert (page.width, page.height) == (720.0, 540.0)
        assert not page.template_is_builtin
    strokes = [s for p in doc.pages for s in p.strokes]
    assert len(strokes) == 294
    # No curvesstyles in 8.4.8: translucent (alpha 0x44) wide strokes are highlighters
    highlighters = [s for s in strokes if s.kind == "highlighter"]
    assert len(highlighters) == 63
    assert min(s.width for s in highlighters) > max(s.width for s in strokes if s.kind == "pen")
    assert any("recording" in w.lower() for w in doc.warnings)


def test_page_layout_note(samples):
    path = _sample(samples, "notesconverter", "samples", "notability", "page-layout.note")
    doc = read_note(path.read_bytes())
    backgrounds = [(p.background.pdf_id, p.background.page_index) if p.background else None for p in doc.pages]
    assert backgrounds == [None, None,
                           ("729D956B-A091-4E9B-90D6-9936D6331ABA", 0),
                           ("729D956B-A091-4E9B-90D6-9936D6331ABA", 1),
                           ("39FC6242-89B7-4102-ACA7-594A6ED3E94F", 0),
                           None]
    assert set(doc.pdfs) == {"729D956B-A091-4E9B-90D6-9936D6331ABA", "39FC6242-89B7-4102-ACA7-594A6ED3E94F"}
    sizes = [(round(p.width), round(p.height)) for p in doc.pages]
    assert sizes == [(612, 803), (612, 803), (595, 842), (595, 842), (842, 595), (612, 803)]
    counts = [len(p.strokes) for p in doc.pages]
    assert sum(counts) == 69 and counts[0] == 0 and counts[1] == 22 and counts[5] == 2
    assert sum(1 for p in doc.pages for s in p.strokes if s.kind == "highlighter") == 1
    assert any("not shown on any page" in w for w in doc.warnings)


def test_long_pdf_doc(samples):
    path = _sample(samples, "notesconverter", "samples", "notability", "long-pdf-doc.note")
    doc = read_note(path.read_bytes())
    assert len(doc.pages) == 32
    assert len(doc.pdfs) == 1
    assert [p.background.page_index for p in doc.pages] == list(range(32))
    assert all(round(p.width) == 595 and round(p.height) == 842 for p in doc.pages)


def test_long_mixed_doc_alternates_plain_and_pdf(samples):
    path = _sample(samples, "notesconverter", "samples", "notability", "long-mixed-doc.note")
    doc = read_note(path.read_bytes())
    assert len(doc.pages) == 64
    assert [p.background is not None for p in doc.pages] == [i % 2 == 1 for i in range(64)]
    assert [p.background.page_index for p in doc.pages if p.background] == list(range(32))


def test_template_pdf_paper(samples):
    path = _sample(samples, "denotability", "resources", "testfile.note")
    doc = read_note(path.read_bytes())
    assert len(doc.pages) == 1
    page = doc.pages[0]
    assert page.template_is_builtin
    assert page.background is not None and page.background.page_index == 0
    assert page.background.pdf_id in doc.pdfs
    assert (page.width, page.height) == (612.0, 792.0)
    assert len(page.strokes) == 203
    assert len(page.texts) == 1  # TextBlockMediaObject; MathMediaObjects are dropped
    assert any("Math" in w for w in doc.warnings)
    assert any("recording" in w.lower() for w in doc.warnings)


def test_image_insert_note(samples):
    path = _sample(samples, "notesconverter", "samples", "notability", "image-insert.note")
    doc = read_note(path.read_bytes())
    assert len(doc.pages) == 2
    images = [(i, img) for i, p in enumerate(doc.pages) for img in p.images]
    assert len(images) == 4
    assert all(img.fmt == "jpeg" and img.data[:3] == b"\xff\xd8\xff" for _, img in images)
    scale = 612.0 / 565.0
    first = doc.pages[0].images[0]
    assert first.x == pytest.approx((45.979817879621493 - x_inset(565.0)) * scale)
    assert first.y == pytest.approx(54.807942912508835 * scale)
    assert first.w == pytest.approx(250 * scale) and first.h == pytest.approx(166.66666666666666 * scale)
    assert first.rotation == 0.0
    rotated = [img for _, img in images if abs(img.rotation) > 1]
    assert rotated and rotated[0].rotation == pytest.approx(math.degrees(0.17077375948429108))
    assert any(i == 1 for i, _ in images), "one image sits on page 2"
    for i, img in images:
        page = doc.pages[i]
        assert 0 <= img.y < page.height
    assert any("crop" in w for w in doc.warnings)


def test_text_note(samples):
    path = _sample(samples, "notesconverter", "samples", "notability", "text.note")
    doc = read_note(path.read_bytes())
    boxes = [t for p in doc.pages for t in p.texts]
    assert len(boxes) == 9
    styled = next(t for t in boxes if t.text.startswith("Normal text 12pt Helvetica Neue"))
    assert styled.text.count("\n") == 6
    joined = "".join(r.text for r in styled.runs)
    assert joined == styled.text
    bold = next(r for r in styled.runs if r.text.startswith("Bold text"))
    assert bold.bold and not bold.italic and bold.font == "HelveticaNeue-Bold"
    italic = next(r for r in styled.runs if r.text.startswith("Italic"))
    assert italic.italic and not italic.bold
    underlined = next(r for r in styled.runs if r.text.startswith("Underlined"))
    assert underlined.underline
    both = next(r for r in styled.runs if r.text.startswith("Bold italic"))
    assert both.bold and both.italic and both.underline
    blue = next(r for r in styled.runs if r.text.startswith("Blue"))
    assert blue.color == pytest.approx((0.0, 0.43529412150383, 1.0, 1.0))
    papyrus = next(r for r in styled.runs if r.text == "Papyrus")
    assert papyrus.font == "Papyrus"
    assert styled.size == pytest.approx(12 * 612.0 / 565.0)
    # box origin + the (5, 2) text padding, measured from the paper's left edge
    assert styled.x == pytest.approx((63.9672737411572 + TEXT_PAD_X - x_inset(565.0)) * 612.0 / 565.0)
    # the all-newline flow text of this note must not become a text box
    assert not any("Typed page text" in w for w in doc.warnings)
    # the title dot drawn above page 1 stays on page 1 (y slightly negative, not clipped)
    assert min(s.points[0].y for s in doc.pages[0].strokes) < 0


def test_notability_16_groups_highlighters_and_media(samples):
    path = _sample(samples, "Notability-notes-converter", "input", "NOTE Note Apr 26, 2026.note")
    data = path.read_bytes()
    doc = read_note(data)
    strokes = [s for p in doc.pages for s in p.strokes]
    assert len(strokes) == 699 == _oracle_curve_count(data)  # 596 top-level + 103 grouped
    assert sum(1 for s in strokes if s.kind == "highlighter") == 21
    assert sum(1 for s in strokes if s.pen == "pencil") == 35
    # style 4 decides the tool; most highlighters also store alpha 0x6b, a few are opaque
    assert sum(1 for s in strokes if s.kind == "highlighter" and s.color[3] < 1.0) >= 15
    images = [img for p in doc.pages for img in p.images]
    assert len(images) == 8
    assert {img.fmt for img in images} == {"jpeg", "png"}
    assert any("Sticky" in w for w in doc.warnings)
    assert any("Dashed" in w for w in doc.warnings)
    assert any("shape" in w for w in doc.warnings)
    assert sum(len(p.texts) for p in doc.pages) == 1  # flow text with fonts and lists
    # grouped strokes are scaled by the group transform (0.592) and land inside their page
    assert len(doc.pages) == 2


def test_template_note_widths_match_raw_arrays(samples):
    path = samples.notability_template()
    data = path.read_bytes()
    doc = read_note(data)
    pl, _ = _raw_session(data)
    objs = pl["$objects"]
    root = _raw_deref(objs, pl["$top"]["$0"])
    rich = _raw_deref(objs, root["richText"])
    h = _raw_deref(objs, _raw_deref(objs, rich["Handwriting Overlay"])["SpatialHash"])
    W = _raw_deref(objs, rich["reflowState"])["pageWidthInDocumentCoordsKey"]
    widths = struct.unpack("<399f", _raw_deref(objs, h["curveswidth"]))
    nums = struct.unpack("<399i", _raw_deref(objs, h["curvesnumpoints"]))
    fw = struct.unpack(f"<{len(_raw_deref(objs, h['curvesfractionalwidths'])) // 4}f", _raw_deref(objs, h["curvesfractionalwidths"]))
    pts = struct.unpack(f"<{len(h['curvespoints']) // 4}f", h["curvespoints"])
    strokes = [s for p in doc.pages for s in p.strokes]
    assert len(strokes) == 399 and len(doc.pages) == 1
    scale = 612.0 / W
    f = p = 0
    for i, s in enumerate(strokes):
        k = (nums[i] - 1) // 3
        assert len(s.points) == k + 1 and len(s.controls) == k
        assert s.width == pytest.approx(widths[i] * scale)
        for j, pt in enumerate(s.points):
            assert pt.width == pytest.approx(widths[i] * fw[f + j] * scale)
            assert pt.x == pytest.approx((pts[2 * (p + 3 * j)] - x_inset(W)) * scale)
            assert pt.y == pytest.approx(pts[2 * (p + 3 * j) + 1] * scale)
        f += k + 1
        p += nums[i]
    assert doc.title == "Note 10 Jun 2021 09:39:40"


def test_old_indexed_encoding_note(samples):
    path = _sample(samples, "svg2notability", "template.note")
    doc = read_note(path.read_bytes())
    assert doc.title == "reverse"
    assert len(doc.pages) == 1 and len(doc.pages[0].strokes) == 4
    assert all(s.kind == "pen" for s in doc.pages[0].strokes)


def test_metadata_title_wins_over_folder(samples):
    path = _sample(samples, "notesconverter", "samples", "notability", "demo15.note")
    doc = read_note(path.read_bytes())
    assert doc.title == "demo2"  # metadata noteName; the ZIP folder is also demo2 but Session name differs
    assert len(doc.pages) == 1 and doc.pages[0].background is not None
    assert (round(doc.pages[0].width), round(doc.pages[0].height)) == (595, 842)


def test_empty_notes(samples):
    for name in ("empty.note", "empty15.note"):
        path = _sample(samples, "notesconverter", "samples", "notability", name)
        doc = read_note(path.read_bytes())
        assert len(doc.pages) == 1 and not doc.pages[0].strokes
        assert (doc.pages[0].width, doc.pages[0].height) == (PLAIN_PAGE_WIDTH_PT, PLAIN_PAGE_HEIGHT_PT)


def test_page_assignment_matches_handwriting_index(samples, tmp_path_factory):
    """HandwritingIndex pages are 1-based; its pageContentOrigin is the ink bbox corner."""
    files = [(p.name, p.read_bytes()) for p in _all_note_files(samples)]
    files.append(("bdb", _bdb_note(samples, tmp_path_factory)))
    compared = 0
    for name, data in files:
        pages = _handwriting_index(data)
        if not pages:
            continue
        doc = read_note(data)
        W = None
        pl, _ = _raw_session(data)
        objs = pl["$objects"]
        root = _raw_deref(objs, pl["$top"].get("root", pl["$top"].get("$0")))
        rich = _raw_deref(objs, root["richText"])
        reflow = _raw_deref(objs, rich.get("reflowState"))
        if isinstance(reflow, dict):
            W = reflow.get("pageWidthInDocumentCoordsKey")
        agree = total = 0
        for key, entry in pages.items():
            index = int(key) - 1
            if index < 0:
                continue  # ink above page 1 (text.note); kept on page 1 by the reader
            assert index < len(doc.pages), (name, key, len(doc.pages))
            page = doc.pages[index]
            if not page.strokes or W is None:
                continue
            total += 1
            scale = page.width / W
            ox, oy = entry["pageContentOrigin"]
            # ink drawn above the page top (text.note's title dot) is kept on the page but
            # indexed under key '0' by Notability, so leave it out of the comparison
            on_page = [s for s in page.strokes if s.points[0].y >= 0] or page.strokes
            min_x = min(p.x for s in on_page for p in s.points) / scale + x_inset(W)  # back to document x
            min_y = min(p.y for s in on_page for p in s.points) / scale
            if abs(ox - min_x) < 12 and abs(oy - min_y) < 12:
                agree += 1
        if total:
            compared += 1
            assert agree >= 0.8 * total, (name, agree, total)
    assert compared >= 5


# --------------------------------------------------------------------------- hostile geometry


def test_tiny_page_width_is_replaced_not_exploded():
    """A sub-unit pageWidthInDocumentCoordsKey made every stroke land on an astronomically
    distant page index (hundreds of millions of Page objects, or OverflowError for 5e-324)."""
    from gnnote.notability.reader import DEFAULT_PAGE_WIDTH, MAX_PAGES
    for width in (5e-324, 1e-6, 1e-4):
        doc = read_note(_build_note(width=width, curves=[_curve([(10, 1000), (11, 1001), (12, 1002), (13, 1003)])]))
        assert len(doc.pages) == 2  # y = 1000 is on page 2 of the default-width layout
        assert any("not usable" in w and f"{DEFAULT_PAGE_WIDTH:g}" in w for w in doc.warnings)
    # a huge, infinite or NaN coordinate is clamped to the last allowed page
    for y in (1e38, float("inf")):  # float32 fields: 1e38 is the largest representable magnitude class
        doc = read_note(_build_note(curves=[_curve([(10, y), (11, y), (12, y), (13, y)])]))
        assert len(doc.pages) == MAX_PAGES and len(doc.pages[-1].strokes) == 1
        assert any("beyond page" in w for w in doc.warnings)
    doc = read_note(_build_note(curves=[_curve([(10, float("nan")), (11, 1), (12, 2), (13, 3)])]))
    assert len(doc.pages) == 1
