"""Tests for gnnote.pdfutil: page geometry of real sample PDFs, hand-crafted edge cases and
the generated paper PDFs.

Expected values for the sample files were verified independently with PyMuPDF (the
optional ``test_every_sample_pdf_matches_pymupdf`` re-checks them when it is installed).
"""
from __future__ import annotations

import re
import zipfile
import zlib
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pytest

from gnnote import pdfutil
from gnnote.pdfutil import PdfInfo, PdfPage, make_paper_pdf, pdf_info

# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------


def _sizes(info: PdfInfo) -> List[Tuple[float, float, int]]:
    return [(round(p.width, 2), round(p.height, 2), p.rotation) for p in info.pages]


def build_classic(objects: Dict[int, bytes], root: int = 1, info: Optional[int] = None,
                  version: str = "1.4", trailer_extra: bytes = b"") -> bytes:
    """A classic PDF: objects in number order, one xref table with exact offsets."""
    out = bytearray(f"%PDF-{version}\n%\xe2\xe3\xcf\xd3\n".encode("latin-1"))
    offsets: Dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    size = max(objects) + 1
    xref_pos = len(out)
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, size):
        if num in offsets:
            out += f"{offsets[num]:010d} 00000 n \n".encode()
        else:
            out += b"0000000000 00001 f \n"
    trailer = f"<< /Size {size} /Root {root} 0 R"
    if info is not None:
        trailer += f" /Info {info} 0 R"
    out += b"trailer\n" + trailer.encode() + b" " + trailer_extra + b" >>\n"
    out += f"startxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


def append_update(base: bytes, objects: Dict[int, bytes], freed: Iterable[int] = (),
                  root: int = 1, info: Optional[int] = None) -> bytes:
    """Append an incremental update (new xref section with /Prev) to ``base``."""
    prev = int(re.findall(rb"startxref\s+(\d+)", base)[-1])
    out = bytearray(base)
    offsets: Dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n"
    entries: Dict[int, bytes] = {}
    for num, off in offsets.items():
        entries[num] = f"{off:010d} 00000 n \n".encode()
    for num in freed:
        entries[num] = b"0000000000 00001 f \n"
    for num in sorted(entries):
        out += f"{num} 1\n".encode() + entries[num]
    size = max(list(entries) + [0]) + 1
    trailer = f"<< /Size {size} /Root {root} 0 R /Prev {prev}"
    if info is not None:
        trailer += f" /Info {info} 0 R"
    out += b"trailer\n" + trailer.encode() + b" >>\n"
    out += f"startxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


def png_up_predict(rows: List[bytes]) -> bytes:
    """Encode rows with the PNG 'Up' filter (type 2), as pdfTeX / Ghostscript do."""
    out = bytearray()
    prev = bytes(len(rows[0]))
    for row in rows:
        out.append(2)
        out += bytes((row[i] - prev[i]) & 0xFF for i in range(len(row)))
        prev = row
    return bytes(out)


def build_xref_stream_pdf(direct: Dict[int, bytes], packed: Dict[int, bytes], objstm_num: int,
                          xref_num: int, root: int = 1, info: Optional[int] = None,
                          free: Iterable[int] = (), predictor: bool = True) -> bytes:
    """A PDF 1.5 file whose ``packed`` objects live in one object stream and whose
    cross-reference data is a FlateDecode xref stream with /W [1 4 2], optional PNG 'Up'
    predictor (/DecodeParms /Predictor 12 /Columns 7) and a two-range /Index."""
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    entries: Dict[int, Tuple[int, int, int]] = {}
    for num in sorted(direct):
        entries[num] = (1, len(out), 0)
        out += f"{num} 0 obj\n".encode() + direct[num] + b"\nendobj\n"
    # object stream
    header = b""
    body = b""
    for idx, num in enumerate(sorted(packed)):
        header += f"{num} {len(body)} ".encode()
        body += packed[num] + b"\n"
        entries[num] = (2, objstm_num, idx)
    payload = header + b"\n" + body
    first = len(header) + 1
    comp = zlib.compress(payload)
    entries[objstm_num] = (1, len(out), 0)
    out += (f"{objstm_num} 0 obj\n<< /Type /ObjStm /N {len(packed)} /First {first} /Length {len(comp)} "
            f"/Filter /FlateDecode >>\nstream\n".encode() + comp + b"\nendstream\nendobj\n")
    for num in free:
        entries[num] = (0, 0, 0)
    xref_pos = len(out)
    entries[xref_num] = (1, xref_pos, 0)
    size = max(entries) + 1
    # two /Index ranges: object 0 alone, then the rest
    rows: List[bytes] = []
    index = [0, 1, 1, size - 1]
    rows.append(bytes([0]) + (0).to_bytes(4, "big") + (0xFFFF).to_bytes(2, "big"))
    for num in range(1, size):
        t, f2, f3 = entries.get(num, (0, 0, 0))
        rows.append(bytes([t]) + f2.to_bytes(4, "big") + f3.to_bytes(2, "big"))
    raw = png_up_predict(rows) if predictor else b"".join(rows)
    comp = zlib.compress(raw)
    parms = " /DecodeParms << /Predictor 12 /Columns 7 >>" if predictor else ""
    trailer_bits = f"/Root {root} 0 R" + (f" /Info {info} 0 R" if info is not None else "")
    out += (f"{xref_num} 0 obj\n<< /Type /XRef /Size {size} /W [1 4 2] /Index [{index[0]} {index[1]} {index[2]} {index[3]}] "
            f"/Filter /FlateDecode{parms} /Length {len(comp)} {trailer_bits} >>\nstream\n".encode()
            + comp + b"\nendstream\nendobj\n")
    out += f"startxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


def _oracle_sizes(data: bytes):
    """(sizes, producer, creator) from PyMuPDF; None when PyMuPDF is unavailable."""
    try:
        import pymupdf  # type: ignore
    except ImportError:
        return None
    doc = pymupdf.open(stream=data, filetype="pdf")
    sizes = []
    for page in doc:
        w, h = page.mediabox.width, page.mediabox.height
        if page.rotation in (90, 270):
            w, h = h, w
        sizes.append((round(w, 2), round(h, 2), page.rotation))
    meta = doc.metadata or {}
    return sizes, meta.get("producer") or "", meta.get("creator") or ""


def iter_goodnotes_pdfs(samples) -> Iterable[Tuple[str, bytes]]:
    for path in samples.goodnotes_files():
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if "attachments/" in name and not name.endswith("/"):
                    data = z.read(name)
                    if data.startswith(b"%PDF"):
                        yield f"{path.name}:{name.rsplit('/', 1)[-1]}", data


def iter_note_pdfs(samples) -> Iterable[Tuple[str, bytes]]:
    for path in samples.note_files():
        try:
            z = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            continue  # some repositories keep non-zip fixtures with the .note extension
        with z:
            for name in z.namelist():
                if name.lower().endswith(".pdf") and not name.endswith("/"):
                    yield f"{path.name}:{name.rsplit('/', 1)[-1]}", z.read(name)


def iter_loose_pdfs(samples, limit_bytes: int = 50_000_000) -> Iterable[Tuple[str, bytes]]:
    root = Path(samples.root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.pdf")):
        if path.is_file() and path.stat().st_size <= limit_bytes:
            yield str(path.relative_to(root)), path.read_bytes()


def iter_all_sample_pdfs(samples) -> List[Tuple[str, bytes]]:
    items: List[Tuple[str, bytes]] = []
    for it in (iter_goodnotes_pdfs, iter_note_pdfs):
        try:
            items += list(it(samples))
        except pytest.skip.Exception:
            pass
    items += list(iter_loose_pdfs(samples))
    if not items:
        pytest.skip("no sample PDFs available")
    return items


# ---------------------------------------------------------------------------------
# real sample files
# ---------------------------------------------------------------------------------

GOODNOTES_TEMPLATE_SIZES = {(455.04, 588.45), (595.28, 841.89)}  # "standard" and A4


def test_goodnotes_template_pdfs(samples):
    seen = 0
    for name, data in iter_goodnotes_pdfs(samples):
        info = pdf_info(data)
        assert len(info.pages) == 1, name
        page = info.pages[0]
        assert (round(page.width, 2), round(page.height, 2)) in GOODNOTES_TEMPLATE_SIZES, name
        assert page.rotation == 0
        assert info.producer == "svg2pdf", name
        assert info.creator == ""
        assert info.warnings == [], name
        seen += 1
    assert seen >= 1


# values verified with PyMuPDF (LibreOffice writes A4 as 595.3 x 841.89)
NOTE_PDF_EXPECTED = {
    "39FC6242-89B7-4102-ACA7-594A6ED3E94F.pdf": (1, (841.89, 595.3), "LibreOffice 24.2", "Writer"),  # landscape
    "729D956B-A091-4E9B-90D6-9936D6331ABA.pdf": (2, (595.3, 841.89), "LibreOffice 24.2", "Writer"),
    "E89CBD11-BF7B-415C-AB5C-0E0A73E7E36E.pdf": (1, (841.89, 595.3), "LibreOffice 24.2", "Writer"),
    "F745FFFF-8304-4BA3-B38E-991B00386ADD.pdf": (2, (595.3, 841.89), "LibreOffice 24.2", "Writer"),
    "E435B4A3-D13F-467F-AE5A-6BBABBEF2259.pdf": (32, (595.3, 841.89), "LibreOffice 24.2", "Writer"),  # long-pdf-doc
    "EC415357-F088-4616-853C-B685ED2F4533.pdf": (1, (595.0, 842.0), "Qt 5.15.9", "MuseScore Version: 4.1.1"),
    "B56F5E2C-59F6-5996-BAC0-AD1584F3D55E.pdf": (1, (612.0, 792.0), "Adobe PDF Library 16.0.3",
                                                 "Adobe InDesign 17.0 (Macintosh)"),
}


def test_notability_sample_pdfs(samples):
    checked = 0
    for name, data in iter_note_pdfs(samples):
        info = pdf_info(data)
        assert info.pages, name
        assert all(p.width > 0 and p.height > 0 for p in info.pages)
        base = name.rsplit(":", 1)[-1]
        expected = NOTE_PDF_EXPECTED.get(base)
        if expected is None:
            continue
        count, size, producer, creator = expected
        assert len(info.pages) == count, name
        assert all((round(p.width, 2), round(p.height, 2)) == size for p in info.pages), name
        assert info.producer == producer, name
        assert info.creator == creator, name
        assert info.warnings == [], name
        checked += 1
    assert checked >= 1


def test_page_layout_note_has_a4_and_landscape(samples):
    path = samples.repo("notesconverter") / "samples" / "notability" / "page-layout.note"
    if not path.is_file():
        pytest.skip("page-layout.note missing")
    with zipfile.ZipFile(path) as z:
        pdfs = [z.read(n) for n in z.namelist() if n.lower().endswith(".pdf")]
    assert len(pdfs) == 4
    shapes = sorted(_sizes(pdf_info(d))[0][:2] for d in pdfs)
    assert shapes == [(595.3, 841.89), (595.3, 841.89), (841.89, 595.3), (841.89, 595.3)]


def test_bdb_transazioni_25_page_landscape_pdf(samples):
    pdf_dir = samples.repo("notability-reader") / "bdb_transazioni" / "PDFs"
    pdfs = sorted(p for p in pdf_dir.glob("*.pdf") if p.is_file())
    if not pdfs:
        pytest.skip("bdb_transazioni PDF missing")
    info = pdf_info(pdfs[0].read_bytes())
    assert len(info.pages) == 25
    assert all((round(p.width, 2), round(p.height, 2), p.rotation) == (720.0, 540.0, 0) for p in info.pages)
    assert info.producer == "Microsoft® PowerPoint® 2010"  # UTF-16 text string with BOM
    assert info.creator == "Microsoft® PowerPoint® 2010"
    assert info.warnings == []


def test_every_sample_pdf_matches_pymupdf(samples):
    pytest.importorskip("pymupdf")
    for name, data in iter_all_sample_pdfs(samples):
        info = pdf_info(data)
        expected = _oracle_sizes(data)
        assert expected is not None
        sizes, producer, creator = expected
        assert _sizes(info) == sizes, name
        assert info.producer == producer, name
        assert info.creator == creator, name


def test_damaged_sample_pdfs_fall_back_to_scanning(samples):
    """Break the cross-reference data of every sample in three ways; the scan fallback must
    still report the same pages (and producer) as the healthy file."""
    variants = {
        "no startxref": lambda d: d.replace(b"startxref", b"startxxxx"),
        "offsets shifted": lambda d: d[:9] + b"%junk junk junk\n" + d[9:],
        "tail truncated": lambda d: d[: d.rfind(b"startxref")],
    }
    for name, data in iter_all_sample_pdfs(samples):
        if len(data) > 3_000_000:
            continue  # keep the test quick
        healthy = pdf_info(data)
        for label, mutate in variants.items():
            info = pdf_info(mutate(data))
            assert _sizes(info) == _sizes(healthy), f"{name} ({label})"
            assert info.producer == healthy.producer, f"{name} ({label})"
            if label != "tail truncated":  # a linearized file keeps a valid first-page startxref
                assert info.warnings, f"{name} ({label}) should record a warning"


# ---------------------------------------------------------------------------------
# hand-crafted files
# ---------------------------------------------------------------------------------

CONTENT = b"<< /Length 0 >>\nstream\n\nendstream"


def _simple(mediabox: bytes = b"[0 0 300 400]", page_extra: bytes = b"", pages_extra: bytes = b"") -> Dict[int, bytes]:
    return {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 " + pages_extra + b" >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox " + mediabox + b" /Contents 4 0 R " + page_extra + b" >>",
        4: CONTENT,
    }


def test_simple_classic_pdf():
    info = pdf_info(build_classic(_simple(), info=None))
    assert _sizes(info) == [(300.0, 400.0, 0)]
    assert info.producer == "" and info.creator == ""
    assert info.warnings == []
    assert info.page_count == 1


def test_info_strings_literal_escapes_hex_and_utf16():
    objs = _simple()
    objs[5] = (b"<< /Producer (Ab\\(c\\)\\\\d\\101\\\r\nE) "
               b"/Creator <FEFF004D006F006E00E9> >>")
    info = pdf_info(build_classic(objs, info=5))
    assert info.producer == "Ab(c)\\dAE"  # escapes, octal \101 = 'A', line continuation
    assert info.creator == "Moné"  # UTF-16BE with byte order mark, hex string
    objs[5] = b"<< /Producer <FFFE41004200> /Creator (plain) >>"
    info = pdf_info(build_classic(objs, info=5))
    assert (info.producer, info.creator) == ("AB", "plain")


def test_rotate_inherited_and_overridden():
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R 5 0 R 6 0 R 7 0 R 8 0 R] /Count 5 /Rotate 90 /MediaBox [0 0 300 400] >>",
        3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",  # inherits 90
        4: CONTENT,
        5: b"<< /Type /Page /Parent 2 0 R /Rotate 0 >>",
        6: b"<< /Type /Page /Parent 2 0 R /Rotate -90 >>",  # == 270
        7: b"<< /Type /Page /Parent 2 0 R /Rotate 180 >>",
        8: b"<< /Type /Page /Parent 2 0 R /Rotate 450 /MediaBox [10 20 110 220] >>",  # 450 -> 90
    }
    info = pdf_info(build_classic(objs))
    assert _sizes(info) == [
        (400.0, 300.0, 90),
        (300.0, 400.0, 0),
        (400.0, 300.0, 270),
        (300.0, 400.0, 180),
        (200.0, 100.0, 90),
    ]
    assert info.warnings == []


def test_mediabox_inherited_indirect_reversed_and_offset():
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [10 0 R 6 0 R] /Count 3 /MediaBox 7 0 R >>",
        # intermediate Pages node with its own (indirect-element) MediaBox
        10: b"<< /Type /Pages /Parent 2 0 R /Kids [3 0 R 5 0 R] /Count 2 /MediaBox [8 0 R 0 9 0 R 500] >>",
        3: b"<< /Type /Page /Parent 10 0 R /Contents 4 0 R >>",  # 250 x 500 via obj 8/9
        4: CONTENT,
        5: b"<< /Type /Page /Parent 10 0 R /MediaBox [600.5 800.25 100.5 -0.25] >>",  # reversed corners
        6: b"<< /Type /Page /Parent 2 0 R >>",  # inherits obj 7 from the root
        7: b"[20 30 320 430]",  # non-zero origin -> 300 x 400
        8: b"250",
        9: b"0",
    }
    info = pdf_info(build_classic(objs))
    assert _sizes(info) == [(250.0, 500.0, 0), (500.0, 800.5, 0), (300.0, 400.0, 0)]
    assert info.warnings == []


def test_missing_mediabox_defaults_to_letter_with_warning():
    objs = _simple()
    objs[3] = b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>"
    info = pdf_info(build_classic(objs))
    assert _sizes(info) == [(612.0, 792.0, 0)]
    assert any("MediaBox" in w for w in info.warnings)
    # a degenerate box is treated the same way
    objs[3] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 0 400] >>"
    info = pdf_info(build_classic(objs))
    assert _sizes(info) == [(612.0, 792.0, 0)]


def test_xref_stream_object_stream_and_png_predictor():
    direct = {4: CONTENT, 9: b"<< /Producer (packed test) >>"}
    packed = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 /MediaBox [0 0 200 100] >>",
        3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
        5: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 50 60] /Rotate 90 >>",
    }
    for predictor in (True, False):
        data = build_xref_stream_pdf(direct, packed, objstm_num=6, xref_num=7, info=9, free=[8],
                                     predictor=predictor)
        assert b"/Type /XRef" in data and b"/Type /ObjStm" in data
        info = pdf_info(data)
        assert _sizes(info) == [(200.0, 100.0, 0), (60.0, 50.0, 90)]
        assert info.producer == "packed test"
        assert info.warnings == []
    # internal check: the entries were decoded through the predictor and the free entry
    doc = pdfutil._Document(data)
    doc.load_xref()
    assert doc.xref[3] == (2, 6, 2)  # type 2: object stream 6, index 2 (members 1, 2, 3, 5)
    assert doc.xref[6][0] == 1
    assert doc.xref[8] == (0, 0, 0)


def test_xref_stream_damaged_falls_back_to_scanning_object_streams():
    direct = {4: CONTENT}
    packed = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 123 456] >>",
    }
    data = build_xref_stream_pdf(direct, packed, objstm_num=5, xref_num=6)
    broken = data.replace(b"startxref", b"startxxxx")
    info = pdf_info(broken)
    assert _sizes(info) == [(123.0, 456.0, 0)]
    assert info.warnings
    # even the xref stream dictionary itself may be damaged: pages still come from scanning
    broken = data.replace(b"/Type /XRef", b"/Type /Junk")
    assert _sizes(pdf_info(broken)) == [(123.0, 456.0, 0)]


def test_incremental_update_newest_wins_and_prev_chain():
    base = build_classic(_simple(b"[0 0 300 400]"), info=None)
    # 1st update: replace the page with a different size
    upd1 = append_update(base, {3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 200] >>"})
    assert _sizes(pdf_info(upd1)) == [(100.0, 200.0, 0)]
    # 2nd update: add a second page via a new Pages object and an Info dictionary
    upd2 = append_update(upd1, {
        2: b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        6: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 50 50] >>",
        7: b"<< /Producer (updated) >>",
    }, info=7)
    info = pdf_info(upd2)
    assert _sizes(info) == [(100.0, 200.0, 0), (50.0, 50.0, 0)]
    assert info.producer == "updated"
    assert info.warnings == []
    doc = pdfutil._Document(upd2)
    doc.load_xref()
    assert doc.trailer["Prev"] == int(re.findall(rb"startxref\s+(\d+)", upd1)[-1])


def test_free_entry_hides_older_object():
    base = build_classic(_simple(), info=None)
    objs = {2: b"<< /Type /Pages /Kids [6 0 R] /Count 1 >>",
            6: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 20] >>"}
    upd = append_update(base, objs, freed=[3])
    doc = pdfutil._Document(upd)
    doc.load_xref()
    assert doc.xref[3][0] == 0
    assert doc.get_object(3) is None  # freed in the newest section although bytes still exist
    assert isinstance(doc.get_object(6), dict)
    assert _sizes(pdf_info(upd)) == [(10.0, 20.0, 0)]


def test_hybrid_file_with_xrefstm():
    """A classic table whose trailer names an xref stream (/XRefStm) that locates objects
    living in an object stream, as Acrobat writes hybrid-reference files."""
    objstm_payload = b"3 0 << /Type /Page /Parent 2 0 R /MediaBox [0 0 77 88] >>\n"
    first = objstm_payload.index(b"<<")
    comp = zlib.compress(objstm_payload)
    out = bytearray(b"%PDF-1.5\n")
    offsets = {}
    def add(num: int, body: bytes) -> None:
        offsets[num] = len(out)
        out.extend(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")
    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add(5, f"<< /Type /ObjStm /N 1 /First {first} /Length {len(comp)} /Filter /FlateDecode >>\nstream\n".encode()
        + comp + b"\nendstream")
    row = bytes([2]) + (5).to_bytes(4, "big") + (0).to_bytes(2, "big")
    xs = zlib.compress(row)
    add(6, f"<< /Type /XRef /Size 7 /W [1 4 2] /Index [3 1] /Length {len(xs)} /Filter /FlateDecode >>\nstream\n".encode()
        + xs + b"\nendstream")
    xref_pos = len(out)
    out += b"xref\n0 7\n0000000000 65535 f \n"
    for num in range(1, 7):
        if num in offsets:
            out += f"{offsets[num]:010d} 00000 n \n".encode()
        else:
            out += b"0000000000 65535 f \n"  # object 3 and 4 are free in the table
    out += f"trailer\n<< /Size 7 /Root 1 0 R /XRefStm {offsets[6]} >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    info = pdf_info(bytes(out))
    assert _sizes(info) == [(77.0, 88.0, 0)]
    assert info.warnings == []


def test_broken_xref_offsets_fall_back_per_object():
    data = build_classic(_simple(b"[0 0 111 222]"))
    # shift every object by inserting a comment after the header; xref offsets are now wrong
    shifted = data[:9] + b"%shift\n" + data[9:]
    info = pdf_info(shifted)
    assert _sizes(info) == [(111.0, 222.0, 0)]
    assert any("scanning" in w for w in info.warnings)
    # garbage startxref
    garbage = re.sub(rb"startxref\s+\d+", b"startxref\n999999999", data)
    info = pdf_info(garbage)
    assert _sizes(info) == [(111.0, 222.0, 0)]
    assert info.warnings
    # no trailer / xref at all
    cut = data[: data.index(b"xref\n")]
    assert _sizes(pdf_info(cut)) == [(111.0, 222.0, 0)]
    # xref table with wrong start number (a common writer bug): the entry for object 3
    # points at object 2 ... every object is then located by scanning
    wrong = data.replace(b"xref\n0 5\n", b"xref\n1 5\n")
    assert _sizes(pdf_info(wrong)) == [(111.0, 222.0, 0)]


def test_scan_fallback_without_catalog_orders_pages_and_inherits():
    """No trailer, no catalog: pages are found by /Type /Page and attributes come from
    the /Parent chain."""
    body = (
        b"%PDF-1.4\n"
        b"7 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [7 0 R 3 0 R] /Count 2 /MediaBox [0 0 40 50] /Rotate 90 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 20] /Rotate 0 >>\nendobj\n"
        b"4 0 obj\n<< /Type /Font /BaseFont /Helvetica >>\nendobj\n"
    )
    info = pdf_info(body)
    assert _sizes(info) == [(50.0, 40.0, 90), (10.0, 20.0, 0)]  # tree order, not object order
    # without any Pages node: object-number order and the page's own attributes
    body2 = (
        b"%PDF-1.4\n"
        b"9 0 obj\n<< /Type /Page /MediaBox [0 0 1 2] >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /MediaBox [0 0 3 4] >>\nendobj\n"
    )
    assert _sizes(pdf_info(body2)) == [(3.0, 4.0, 0), (1.0, 2.0, 0)]


def test_scan_fallback_ignores_page_text_inside_streams():
    objs = _simple(b"[0 0 20 30]")
    objs[4] = (b"<< /Length 60 >>\nstream\n"
               b"BT (fake << /Type /Page /MediaBox [0 0 999 999] >>) Tj ET      \nendstream")
    data = build_classic(objs).replace(b"startxref", b"startxxxx")
    assert _sizes(pdf_info(data)) == [(20.0, 30.0, 0)]


def test_kids_cycle_and_junk_do_not_hang():
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [2 0 R 3 0 R 99 0 R (junk) 3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 5 6] >>",
    }
    info = pdf_info(build_classic(objs))
    assert _sizes(info) == [(5.0, 6.0, 0)]


def test_stream_with_wrong_length_is_still_read():
    direct = {4: b"<< /Length 5 >>\nstream\n0123456789\nendstream"}
    packed = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 8] >>",
    }
    data = build_xref_stream_pdf(direct, packed, objstm_num=5, xref_num=6)
    # corrupt the object stream's /Length so the endstream search is needed
    data = re.sub(rb"(/Type /ObjStm /N 3 /First \d+ /Length )\d+", rb"\g<1>1", data)
    assert _sizes(pdf_info(data)) == [(9.0, 8.0, 0)]


def test_no_pages_raises_value_error():
    with pytest.raises(ValueError):
        pdf_info(b"")
    with pytest.raises(ValueError):
        pdf_info(b"this is not a pdf at all " * 100)
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [] /Count 0 >>"}
    with pytest.raises(ValueError):
        pdf_info(build_classic(objs))


def test_junk_before_header_offsets_relative_to_header():
    data = build_classic(_simple(b"[0 0 33 44]"))
    info = pdf_info(b"GARBAGE LINE\r\n" + data)
    assert _sizes(info) == [(33.0, 44.0, 0)]


def test_other_filters_on_object_streams():
    payload = b"3 0 << /Type /Page /Parent 2 0 R /MediaBox [0 0 15 16] >>\n"
    first = payload.index(b"<<")
    import base64
    encodings = {
        b"/ASCIIHexDecode": payload.hex().encode() + b">",
        b"/ASCII85Decode": base64.a85encode(payload) + b"~>",
        b"[/ASCIIHexDecode /FlateDecode]": zlib.compress(payload).hex().encode() + b">",
        b"/RunLengthDecode": b"".join(bytes([len(payload[i:i + 100]) - 1]) + payload[i:i + 100]
                                      for i in range(0, len(payload), 100)) + b"\x80",
    }
    for flt, enc in encodings.items():
        objs = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            5: (b"<< /Type /ObjStm /N 1 /First " + str(first).encode() + b" /Length " + str(len(enc)).encode()
                + b" /Filter " + flt + b" >>\nstream\n" + enc + b"\nendstream"),
        }
        data = build_classic(objs)
        # register object 3 as living in object stream 5 through an xref stream update
        row = bytes([2]) + (5).to_bytes(4, "big") + (0).to_bytes(2, "big")
        xs = zlib.compress(row)
        prev = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
        pos = len(data)
        data += (f"6 0 obj\n<< /Type /XRef /Size 7 /W [1 4 2] /Index [3 1] /Root 1 0 R /Prev {prev} "
                 f"/Filter /FlateDecode /Length {len(xs)} >>\nstream\n".encode() + xs
                 + f"\nendstream\nendobj\nstartxref\n{pos}\n%%EOF\n".encode())
        info = pdf_info(data)
        assert _sizes(info) == [(15.0, 16.0, 0)], flt
        assert info.warnings == [], flt


def test_lzw_decoder_round_trip():
    # LZW-encode with a tiny reference encoder (early change, 9..12 bit codes)
    def lzw_encode(data: bytes) -> bytes:
        table = {bytes([i]): i for i in range(256)}
        next_code = 258
        width = 9
        bits: List[Tuple[int, int]] = [(256, 9)]
        w = b""
        for byte in data:
            wc = w + bytes([byte])
            if wc in table:
                w = wc
                continue
            bits.append((table[w], width))
            table[wc] = next_code
            next_code += 1
            # the decoder lags one entry behind, so it widens after entry 510 is created;
            # the encoder therefore switches once its next code is 512
            if next_code + 1 > (1 << width) and width < 12:
                width += 1
            w = bytes([byte])
        if w:
            bits.append((table[w], width))
        bits.append((257, width))
        acc = 0
        nacc = 0
        out = bytearray()
        for code, wd in bits:
            acc = (acc << wd) | code
            nacc += wd
            while nacc >= 8:
                out.append((acc >> (nacc - 8)) & 0xFF)
                nacc -= 8
        if nacc:
            out.append((acc << (8 - nacc)) & 0xFF)
        return bytes(out)

    text = b"TOBEORNOTTOBEORTOBEORNOT" * 40 + bytes(range(256)) * 3
    assert pdfutil._lzw(lzw_encode(text)) == text
    # the worked example of ISO 32000-1 section 7.4.4.2: codes 256 45 258 258 65 259 66 257
    codes = [256, 45, 258, 258, 65, 259, 66, 257]
    acc = 0
    for c in codes:
        acc = (acc << 9) | c
    nbits = 9 * len(codes)
    packed = (acc << (-nbits % 8)).to_bytes((nbits + 7) // 8, "big")
    assert pdfutil._lzw(packed) == bytes([45, 45, 45, 45, 45, 65, 45, 45, 45, 66])


def test_png_predictors_all_filter_types():
    rows = [bytes([1, 2, 3, 4, 5, 6, 7]), bytes([9, 9, 9, 0, 0, 0, 1]), bytes([5, 4, 3, 2, 1, 0, 255])]
    columns = 7
    bpp = 1
    # encode with Sub (1), Up (2), Average (3), Paeth (4) in turn
    def paeth(a: int, b: int, c: int) -> int:
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if pa <= pb and pa <= pc else (b if pb <= pc else c)
    encoded = bytearray()
    prev = bytes(columns)
    for ft, row in zip((1, 2, 3, 4, 0), rows + rows[:2]):
        encoded.append(ft)
        for i in range(columns):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pred = {0: 0, 1: a, 2: b, 3: (a + b) >> 1, 4: paeth(a, b, c)}[ft]
            encoded.append((row[i] - pred) & 0xFF)
        prev = row
    out = pdfutil._unpredict(bytes(encoded), {"Predictor": 12, "Columns": columns}, lambda v: v)
    assert out == b"".join(rows + rows[:2])
    # TIFF predictor 2, 8 bit, 2 colours
    tiff = bytes([10, 20, 1, 2, 3, 4]) + bytes([0, 0, 5, 5, 5, 5])
    out = pdfutil._unpredict(tiff, {"Predictor": 2, "Columns": 3, "Colors": 2}, lambda v: v)
    assert out == bytes([10, 20, 11, 22, 14, 26, 0, 0, 5, 5, 10, 10])


def test_lexer_names_strings_and_refs():
    lx = pdfutil._Lexer(b"<< /A#20B 1 0 R /C [1 2 R 3.5 -.5 true null (x\\)y) <414>] /D /E#2F >>")
    d = pdfutil._parse_value(lx, lx.next())
    assert d["A B"] == pdfutil.Ref(1, 0)
    assert d["C"] == [pdfutil.Ref(1, 2), 3.5, -0.5, True, None, b"x)y", b"A@"]
    assert d["D"] == "E/"


# ---------------------------------------------------------------------------------
# make_paper_pdf
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["plain", "lined", "grid", "dotted", "ruled"])
def test_paper_pdf_round_trip(style):
    data = make_paper_pdf(455.04, 588.45, style, (0.972549, 0.96862745, 0.9137255))
    assert data.startswith(b"%PDF-1.4\n")
    assert data.rstrip().endswith(b"%%EOF")
    info = pdf_info(data)
    assert _sizes(info) == [(455.04, 588.45, 0)]
    assert info.producer == "gnnote"
    assert info.creator == "gnnote"
    assert info.warnings == []
    # the regex fallback must reach the same result
    broken = data.replace(b"startxref", b"startxxxx")
    info2 = pdf_info(broken)
    assert _sizes(info2) == [(455.04, 588.45, 0)]
    assert info2.producer == "gnnote"
    assert info2.warnings


def test_paper_pdf_xref_offsets_are_exact():
    data = make_paper_pdf(595.28, 841.89, "grid")
    xref_pos = int(re.search(rb"startxref\s+(\d+)", data).group(1))
    assert data[xref_pos:xref_pos + 4] == b"xref"
    m = re.match(rb"xref\n0 (\d+)\n", data[xref_pos:])
    count = int(m.group(1))
    assert count == 6
    entries = re.findall(rb"(\d{10}) (\d{5}) ([nf]) \n", data[xref_pos + m.end():])
    assert len(entries) == count
    assert entries[0] == (b"0000000000", b"65535", b"f")
    for num, (off, gen, kind) in enumerate(entries[1:], start=1):
        assert kind == b"n" and gen == b"00000"
        assert data[int(off):].startswith(f"{num} 0 obj".encode()), num
    # the stream length is exact
    m = re.search(rb"/Length (\d+) /Filter /FlateDecode >>\nstream\n", data)
    start = m.end()
    length = int(m.group(1))
    assert data[start + length:].startswith(b"\nendstream")
    content = zlib.decompress(data[start:start + length])
    assert content.startswith(b"q 1 1 1 rg 0 0 595.28 841.89 re f Q\n")
    assert b"0.816 0.824 0.828 rg" in content  # GoodNotes' ruling grey
    # 52 horizontal rules (16 pt pitch, 841.89 high) and 37 vertical ones (595.28 wide)
    assert content.count(b" 595.28 0.5 re f\n") == 52
    assert content.count(b" 0 0.5 841.89 re f\n") == 37


def test_paper_pdf_content_by_style():
    def content(style: str, **kw) -> bytes:
        data = make_paper_pdf(100, 50, style, **kw)
        m = re.search(rb"/Length (\d+) /Filter /FlateDecode >>\nstream\n", data)
        return zlib.decompress(data[m.end():m.end() + int(m.group(1))])

    plain = content("plain", color=(0.3176, 0.8431, 0.9608))
    assert plain == b"q 0.318 0.843 0.961 rg 0 0 100 50 re f Q\n"
    lined = content("lined", pitch=10)
    assert lined.count(b" re f") == 1 + 4  # fill + rules at y = 10, 20, 30, 40 from the top
    assert b"0 39.75 100 0.5 re f\n" in lined  # first rule 10 pt below the top edge
    grid = content("grid", pitch=10)
    assert grid.count(b" re f") == 1 + 4 + 9
    dotted = content("dotted", pitch=25)
    assert dotted.count(b" re f") == 1
    assert dotted.count(b" c h f\n") == 3 * 1  # x = 25, 50, 75 ; y = 25
    # colours are clamped, the alias and bad arguments are handled
    assert content("plain", color=(2, -1, 0.5)).startswith(b"q 1 0 0.5 rg")
    with pytest.raises(ValueError):
        make_paper_pdf(100, 50, "striped")
    with pytest.raises(ValueError):
        make_paper_pdf(0, 50)
    with pytest.raises(ValueError):
        make_paper_pdf(100, 50, "lined", pitch=0)


def test_paper_pdf_opens_in_pymupdf():
    pymupdf = pytest.importorskip("pymupdf")
    for style in ("plain", "lined", "grid", "dotted"):
        data = make_paper_pdf(455.04, 588.45, style)
        doc = pymupdf.open(stream=data, filetype="pdf")
        assert doc.page_count == 1
        page = doc[0]
        assert (round(page.rect.width, 2), round(page.rect.height, 2)) == (455.04, 588.45)
        pix = page.get_pixmap(dpi=72)  # renders without raising
        assert abs(pix.width - 455.04) <= 1 and abs(pix.height - 588.45) <= 1
        assert (doc.metadata or {}).get("producer") == "gnnote"
        assert pymupdf.TOOLS.mupdf_warnings() == ""
