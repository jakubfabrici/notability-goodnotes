"""Robustness and resource-safety regressions: decompression bombs, damaged containers,
hostile coordinates and HTTP protocol details.  Everything here is synthetic."""
from __future__ import annotations

import io
import socket
import struct
import threading
import time
import zipfile
import zlib
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from gnnote import applelz4, pdfutil, protobuf, tpl
from gnnote import server as srv
from gnnote.convert import convert, to_document
from gnnote.goodnotes import reader as gn_reader
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.goodnotes.writer import write_goodnotes
from gnnote.model import Document, Page, Point, Stroke, TextBox, TextRun
from gnnote.notability import writer as nb_writer
from gnnote.notability.reader import read_note
from gnnote.notability.writer import write_note


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


def _doc() -> Document:
    page = Page(455.04, 588.45)
    page.strokes.append(Stroke([Point(30, 30, 2.0), Point(90, 70, 2.0), Point(150, 30, 2.0)], width=2.0))
    return Document(title="Hardening", pages=[page])


def _rezip(data: bytes, replace: Dict[str, bytes] | None = None, drop: Tuple[str, ...] = ()) -> bytes:
    """Rewrite a ZIP with some members replaced or dropped."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            if any(name.endswith(d) for d in drop):
                continue
            payload = src.read(name)
            for key, value in (replace or {}).items():
                if name.endswith(key):
                    payload = value
            dst.writestr(name, payload)
    return out.getvalue()


def _notes_member(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return next(n for n in z.namelist() if n.startswith("notes/"))


# --------------------------------------------------------------------------- protobuf


def test_decode_records_caps_the_record_count():
    assert protobuf.decode_records(b"\x00" * 5) == [b""] * 5
    with pytest.raises(ValueError):
        protobuf.decode_records(b"\x00" * 10, max_records=5)
    with pytest.raises(ValueError):
        protobuf.decode_records(b"\x00" * (protobuf.MAX_RECORDS + 1))


# --------------------------------------------------------------------------- GoodNotes container


def test_zip_member_above_the_size_limit_is_skipped(monkeypatch: pytest.MonkeyPatch):
    data = write_goodnotes(_doc())
    monkeypatch.setattr(gn_reader, "MAX_MEMBER_BYTES", 64)
    doc = read_goodnotes(data)
    assert len(doc.pages) == 1 and doc.pages[0].strokes == []
    assert any("above the 0 MB limit" in w and "notes/" in w for w in doc.warnings)


def test_zip_total_budget_is_enforced(monkeypatch: pytest.MonkeyPatch):
    data = write_goodnotes(_doc())
    monkeypatch.setattr(gn_reader, "MAX_TOTAL_BYTES", 200)
    doc = read_goodnotes(data)
    assert any("inflates to more than 0 MB in total" in w for w in doc.warnings)


def test_zero_filled_ink_layer_is_bounded_in_time():
    """A notes/ member of 2 MB zeros (2 KB deflated) is one empty record per byte."""
    data = write_goodnotes(_doc())
    bomb = _rezip(data, {_notes_member(data): b"\x00" * 2_000_000})
    assert len(bomb) < 20_000
    t = time.monotonic()
    doc = read_goodnotes(bomb)
    assert time.monotonic() - t < 15.0
    assert any("truncated" in w for w in doc.warnings)
    assert doc.pages[0].strokes == []


def _bump_zip_version(data: bytes, version: int = 84) -> bytes:
    """Set 'version needed to extract' of every central-directory entry (8.4: unsupported)."""
    buf = bytearray(data)
    pos = 0
    while True:
        pos = buf.find(b"PK\x01\x02", pos)
        if pos < 0:
            return bytes(buf)
        buf[pos + 6:pos + 8] = struct.pack("<H", version)
        pos += 4


@pytest.mark.parametrize("ext", ["goodnotes", "note"])
def test_unsupported_zip_version_is_a_value_error(ext: str):
    data = write_goodnotes(_doc()) if ext == "goodnotes" else write_note(_doc())
    broken = _bump_zip_version(data)
    with pytest.raises(NotImplementedError):
        zipfile.ZipFile(io.BytesIO(broken))  # what zipfile itself does
    with pytest.raises(ValueError):
        convert(broken, "x." + ext)
    with pytest.raises(ValueError):
        to_document(broken, "x." + ext)


def test_missing_ink_layer_member_warns():
    data = write_goodnotes(_doc())
    member = _notes_member(data)
    doc = read_goodnotes(_rezip(data, drop=(member,)))
    assert len(doc.pages) == 1 and doc.pages[0].strokes == []
    assert any("ink layer" in w and "missing" in w for w in doc.warnings)


def test_non_uuid_notes_member_name_is_tolerated():
    data = write_goodnotes(_doc())
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        content = z.read(_notes_member(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("notes/hand-made", content)
    assert gn_reader.page_uuid_of_notes("hand-made") == "HAND-MADE"
    doc = read_goodnotes(out.getvalue())
    assert len(doc.pages) == 1 and len(doc.pages[0].strokes) == 1
    assert doc.warnings  # no template event: size guessed, with a warning


def test_writer_skips_values_beyond_float32():
    page = Page(455.04, 588.45)
    page.strokes.append(Stroke([Point(1e39, 10, 2.0), Point(20, 20, 2.0)], width=2.0))
    page.strokes.append(Stroke([Point(10, 10, 2.0), Point(20, 20, 2.0)], width=2.0))
    page.texts.append(TextBox(1e39, 10, 100, 20, "x"))
    doc = Document(pages=[page, Page(1e39, 100)])
    data = write_goodnotes(doc)  # no OverflowError
    assert any("stroke was skipped" in w for w in doc.warnings)
    assert any("text box was skipped" in w for w in doc.warnings)
    assert any("standard page size" in w for w in doc.warnings)
    back = read_goodnotes(data)
    assert [len(p.strokes) for p in back.pages] == [1, 0]


def test_encode_flat_rejects_non_finite_anywhere():
    with pytest.raises(ValueError):
        tpl.encode_flat(tpl.FlatStroke(1.0, (0.0, 0.0), [(1.0, float("nan"), 2.0, 2.0)]))
    with pytest.raises(ValueError):
        tpl.encode_flat(tpl.FlatStroke(1.0, (0.0, 0.0), [(1.0, 1.0, float("inf"), 2.0)]))


# --------------------------------------------------------------------------- Apple LZ4


def test_lz4_block_cannot_expand_past_its_declared_size():
    # literal 'a', then a match at offset 1 whose length extension is 4000 x 0xff (~1 MB)
    block = b"\x1fa" + b"\x01\x00" + b"\xff" * 4000 + b"\x00"
    frame = b"bv41" + _u32(1) + _u32(len(block)) + block + b"bv4$"
    t = time.monotonic()
    with pytest.raises(ValueError, match="more than 1 bytes"):
        applelz4.decompress(frame)
    assert time.monotonic() - t < 1.0
    # the budget is checked before the literals too
    block = b"\xf0" + b"\xff" * 10 + b"\x00" + b"x" * 3000
    with pytest.raises(ValueError, match="more than"):
        applelz4.lz4_block_decompress(block, expected_size=10)


def test_lz4_stream_declared_size_is_capped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(applelz4, "MAX_STREAM_OUTPUT", 100)
    with pytest.raises(ValueError, match="declares more than"):
        applelz4.decompress(b"bv41" + _u32(101) + _u32(2) + b"\x10a" + b"bv4$")
    with pytest.raises(ValueError, match="declares more than"):
        applelz4.decompress(b"bv4-" + _u32(101) + b"a" * 101 + b"bv4$")
    assert applelz4.decompress(applelz4.compress(b"x" * 100)) == b"x" * 100


def test_lz4_block_without_declared_size_uses_the_block_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(applelz4, "MAX_BLOCK_OUTPUT", 50)
    block = b"\x1fa" + b"\x01\x00" + b"\xff" * 4 + b"\x00"
    with pytest.raises(ValueError, match="more than 50"):
        applelz4.lz4_block_decompress(block)


# --------------------------------------------------------------------------- PDF streams


def test_flate_stream_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdfutil, "MAX_STREAM_BYTES", 1000)
    assert pdfutil._flate(zlib.compress(b"\x01" * 900)) == b"\x01" * 900
    with pytest.raises(pdfutil.PdfError, match="inflates to more than"):
        pdfutil._flate(zlib.compress(b"\x00" * 5000))
    with pytest.raises(pdfutil.PdfError):
        pdfutil._run_length(b"\x81\x00" * 20)  # 20 x 128 bytes


def test_pdf_with_flate_bomb_xref_stream_degrades_to_a_warning(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdfutil, "MAX_STREAM_BYTES", 10_000)
    payload = zlib.compress(b"\x00" * 100_000)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] >>",
    ]
    out = bytearray(b"%PDF-1.5\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref_at = len(out)
    out += (b"4 0 obj\n<< /Type /XRef /Size 5 /W [1 4 2] /Root 1 0 R /Filter /FlateDecode /Length %d >>\nstream\n"
            % len(payload)) + payload + b"\nendstream\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref_at
    info = pdfutil.pdf_info(bytes(out))
    assert any("inflates to more than" in w for w in info.warnings)
    assert [(p.width, p.height) for p in info.pages] == [(300.0, 400.0)]  # located by scanning


# --------------------------------------------------------------------------- server


@pytest.fixture
def running(tmp_path: Path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    server = srv.make_server("127.0.0.1", 0, root, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _responses_on_one_connection(addr, requests: List[bytes]) -> bytes:
    with socket.create_connection(addr, timeout=10) as sock:
        for req in requests:
            sock.sendall(req)
        sock.settimeout(1.0)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize("path", ["/api/health", "/api/nope"])
def test_head_on_api_paths_has_no_body(running, path: str):
    raw = _responses_on_one_connection(running, [
        f"HEAD {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
        b"GET /version.json HTTP/1.1\r\nHost: x\r\n\r\n",
    ])
    head, _, rest = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 ")
    assert b"content-length:" in head.lower()
    assert rest.startswith(b"HTTP/1.1 200"), rest[:60]  # no JSON leaked in front of the next response


def test_health_advertises_the_upload_limit(running):
    raw = _responses_on_one_connection(running, [b"GET /api/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"])
    body = raw.partition(b"\r\n\r\n")[2]
    import json
    assert json.loads(body)["maxUpload"] == srv.MAX_UPLOAD


def test_handler_has_an_idle_timeout():
    assert srv.GnNoteHandler.timeout == 60


def test_multipart_payload_may_contain_the_boundary_prefix():
    body = (b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.note\"\r\n\r\n"
            b"ab\r\n--Bxyz\r\nmore\r\n--B \t\r\nContent-Disposition: form-data; name=\"g\"\r\n\r\ncd\r\n--B--\r\n")
    parts = srv.parse_multipart(body, b"B")
    assert [(p.name, p.data) for p in parts] == [("file", b"ab\r\n--Bxyz\r\nmore"), ("g", b"cd")]


# --------------------------------------------------------------------------- Notability writer


def _session_fonts(data: bytes) -> List[str]:
    from gnnote.notability.keyedarchive import Archive
    import plistlib
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        pl = plistlib.loads(z.read(next(n for n in z.namelist() if n.endswith("Session.plist"))))
    a = Archive(pl)
    rich = a.get(a.root, "richText")
    fonts = []
    for m in a.array(a.get(rich, "mediaObjects")):
        store = a.get(m, "textStore")
        attributed = a.dictionary(a.get(store, "attributedString"))
        for s in a.array(attributed["subRangesKey"]):
            fonts.append(a.string(a.dictionary(a.dictionary(s)["subRangeFontKey"])["NSFontNameAttribute"]))
    return fonts


def test_font_style_suffix_for_hyphenated_fonts():
    runs = [TextRun("a", font="Helvetica-Light", bold=True), TextRun("b", font="AvenirNext-Regular", bold=True, italic=True),
            TextRun("c", font="HelveticaNeue-Bold", italic=True), TextRun("d", font="HelveticaNeue-Bold", bold=True),
            TextRun("e", font="Helvetica-Light")]
    page = Page(455.04, 588.45, texts=[TextBox(10, 10, 200, 30, "abcde", runs=runs)])
    fonts = _session_fonts(write_note(Document(pages=[page])))
    assert fonts == ["Helvetica-Light-Bold", "AvenirNext-BoldItalic", "HelveticaNeue-BoldItalic",
                     "HelveticaNeue-Bold", "Helvetica-Light"]


def test_lone_surrogates_are_replaced_not_fatal():
    page = Page(455.04, 588.45, texts=[TextBox(10, 10, 200, 30, "a\ud800b", runs=[TextRun("a\ud800b")])])
    doc = Document(title="t\udfffx", pages=[page])
    data = write_note(doc)
    assert any("surrogate" in w for w in doc.warnings)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        folder = z.namelist()[0].split("/")[0]
    assert folder == "t�x"
    assert read_note(data).pages[0].texts[0].text == "a�b"


def test_long_title_is_shortened_for_the_bundle_folder():
    doc = Document(title="ä" * 300, pages=[Page(455.04, 588.45)])
    data = write_note(doc)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        folder = z.namelist()[0].split("/")[0]
    assert folder == "ä" * 100 and len(folder.encode("utf-8")) == nb_writer.MAX_NAME_BYTES
    assert any("shortened" in w for w in doc.warnings)


def test_trailing_blank_plain_pages_survive():
    doc = Document(pages=[_doc().pages[0], Page(455.04, 588.45), Page(455.04, 588.45)])
    back = read_note(write_note(doc))
    assert len(back.pages) == 3
    # a single blank page or content on the last page: the proven layout-less form
    blank = read_note(write_note(Document(pages=[Page(455.04, 588.45)])))
    assert len(blank.pages) == 1
    import plistlib
    from gnnote.notability.keyedarchive import Archive
    for d, expected in ((Document(pages=[Page(455.04, 588.45)]), 0),
                        (Document(pages=[_doc().pages[0], Page(455.04, 588.45)]), 2)):
        data = write_note(d)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            pl = plistlib.loads(z.read(next(n for n in z.namelist() if n.endswith("Session.plist"))))
        a = Archive(pl)
        assert len(a.array(a.get(a.get(a.root, "richText"), "pageLayoutArray"))) == expected


def test_fountain_tapers_are_not_clipped():
    widths = [0.3, 0.8, 1.5, 1.5, 1.5, 1.5, 0.8, 0.3]
    pts = [Point(10 + 10 * i, 50 + 3 * (i % 2), w) for i, w in enumerate(widths)]
    doc = Document(pages=[Page(455.04, 588.45, strokes=[Stroke(pts, width=1.5)])])
    back = read_note(write_note(doc))
    factor = back.pages[0].width / 455.04
    got = [p.width / factor for p in back.pages[0].strokes[0].points]
    assert got == pytest.approx(widths, rel=1e-4)
    assert nb_writer._fractional_base(1.5, [0.1, 2.0]) == 1.5  # range too wide: the clip stays


def test_text_box_round_trip_is_symmetric():
    page = Page(455.04, 588.45, texts=[TextBox(60, 90, 200, 30, "Hi", runs=[TextRun("Hi")])])
    back = read_note(write_note(Document(pages=[page])))
    factor = back.pages[0].width / 455.04
    box = back.pages[0].texts[0]
    assert (box.x, box.y, box.w, box.h) == pytest.approx((60 * factor, 90 * factor, 200 * factor, 30 * factor), abs=1e-6)
    again = read_note(write_note(back))
    b2 = again.pages[0].texts[0]
    assert (b2.x, b2.y, b2.w, b2.h) == pytest.approx((box.x, box.y, box.w, box.h), abs=1e-6)


# --------------------------------------------------------------------------- Notability reader


def test_notability_x_inset_round_trip():
    """The paper's left edge is at document x = -W * 20 / 768 in both codecs."""
    page = Page(455.04, 588.45, strokes=[Stroke([Point(0, 10, 2.0), Point(100, 10, 2.0)], width=2.0)])
    data = write_note(Document(pages=[page]))
    back = read_note(data)
    factor = back.pages[0].width / 455.04
    s = back.pages[0].strokes[0]
    assert s.points[0].x == pytest.approx(0.0, abs=1e-6) and s.points[-1].x == pytest.approx(100 * factor, abs=1e-6)
