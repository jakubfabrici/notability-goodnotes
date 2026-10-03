"""Noteful writer: round trips through our reader, notesconverter's strict parser (subprocess)
on synthetic documents and on GoodNotes / Notability / Noteful samples, determinism, structure."""
from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from gnnote import pdfutil
from gnnote.convert import Options, convert, to_document
from gnnote.geometry import flatten_bezier
from gnnote.model import Document, Image, Page, PdfBackground, Point, Stroke, TextBox, TextRun
from gnnote.noteful import UNITS_PER_POINT, ttv
from gnnote.noteful.reader import read_noteful
from gnnote.noteful.writer import FIXED_TIME, font_family, order_tag, write_noteful

from noteful_oracle import run_oracle, sample_files

U = UNITS_PER_POINT
TOL = 0.01  # pt


class Seeded:
    def __init__(self, seed: int = 1, **extra: Any):
        self.random_seed = seed
        self.title = None
        for k, v in extra.items():
            setattr(self, k, v)


def png(width: int = 3, height: int = 2) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg() -> bytes:
    from gnnote.goodnotes.constants import THUMBNAIL_JPEG
    return THUMBNAIL_JPEG


def two_page_pdf() -> bytes:
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def synthetic() -> Document:
    page = Page(455.04, 588.45, paper="lined")
    page.strokes.append(Stroke([Point(30, 40, 2.0), Point(80, 90, 2.0), Point(130, 40, 2.0)], color=(0.2, 0.4, 0.6, 1.0),
                               width=2.0))
    page.strokes.append(Stroke([Point(30 + i * 5, 150 + (i % 3), 1.0 + i / 10) for i in range(20)],
                               color=(0, 0, 0, 1), width=1.5, pen="fountain"))
    page.strokes.append(Stroke([Point(40, 200, 12.0), Point(240, 205, 12.0)], color=(1.0, 0.9, 0.0, 0.5),
                               kind="highlighter", width=12.0))
    page.strokes.append(Stroke([Point(50, 300, 3.0), Point(150, 300, 3.0)], color=(0, 0.5, 0, 1), width=3.0,
                               controls=[(Point(70, 250, 3.0), Point(130, 350, 3.0))]))
    page.strokes.append(Stroke([Point(300, 300, 4.0)], width=4.0))  # a dot
    square = [Point(300, 400, 0), Point(350, 400, 0), Point(350, 450, 0), Point(300, 450, 0), Point(300, 400, 0)]
    page.strokes.append(Stroke(list(square), color=(0.1, 0.2, 0.9, 0.1), kind="fill", width=0.0, outline=[square]))
    page.texts.append(TextBox(60, 480, 200, 40, "Hello bold\nnext", align="center", runs=[
        TextRun("Hello ", font="HelveticaNeue", size=14.0, color=(0.8, 0, 0, 1)),
        TextRun("bold\nnext", bold=True, italic=True, underline=True, size=10.0, color=(0, 0, 0, 1))]))
    page.texts.append(TextBox(250, 100, 120, 30, "turned", rotation=30.0, size=11.0))
    page.images.append(Image(200, 500, 90, 60, png(), fmt="png", rotation=15.0))
    page.images.append(Image(320, 500, 24, 31, jpeg(), fmt="jpeg"))
    pdf_pages = [Page(400, 300, background=PdfBackground("doc.pdf", 0)),
                 Page(300, 400, background=PdfBackground("doc.pdf", 1),
                      strokes=[Stroke([Point(10, 10, 1.0), Point(50, 50, 1.0)], width=1.0)])]
    return Document(title="Synthetic ä", pages=[page] + pdf_pages + [Page(419.53, 595.28)],
                    pdfs={"doc.pdf": two_page_pdf()})


@pytest.fixture(scope="module")
def written() -> Tuple[Document, bytes, Document]:
    doc = synthetic()
    data = write_noteful(doc, Seeded())
    return doc, data, read_noteful(data)


def _container(data: bytes) -> Dict[str, Any]:
    start, length = struct.unpack(">II", data[-8:])
    decoder = ttv.Decoder(data)
    root = decoder.decode(start, start + length)
    blobs = {n: (s, ln) for n, s, ln in zip(root.strings(10), root.integers(11), root.integers(12))}

    def record(name: str) -> ttv.Record:
        s, ln = blobs[name]
        return decoder.decode(s, s + ln)

    return {"root": root, "blobs": blobs, "record": record}


# --------------------------------------------------------------------------- round trip


def test_ink_round_trip_within_a_hundredth_of_a_point(written) -> None:
    doc, _data, back = written
    src = doc.pages[0].strokes
    got = back.pages[0].strokes
    assert [s.kind for s in got] == ["pen", "pen", "highlighter", "pen", "pen", "fill"]
    for ours, theirs in zip(got[:5], src[:5]):
        expected = flatten_bezier(theirs.points, theirs.controls, 1.0) if theirs.controls else list(theirs.points)
        if len(expected) == 1:
            expected = expected * 2  # a dot is written as two identical points
        assert len(ours.points) == len(expected)
        for p, q in zip(ours.points, expected):
            assert abs(p.x - q.x) < TOL and abs(p.y - q.y) < TOL and abs(p.width - q.width) < TOL
        assert ours.controls is None
    assert got[0].color == (0.2, 0.4, 0.6, 1.0) and got[0].width == pytest.approx(2.0)
    assert got[1].width == pytest.approx(1.5)  # the nominal width of a variable-width stroke
    assert got[2].color == pytest.approx((1.0, 0.9, 0.0, 0.5))  # Noteful's 50 % highlighter
    fill = got[5]
    assert fill.outline and fill.color == pytest.approx((0.1, 0.2, 0.9, 0.1))
    xs = [p.x for p in fill.outline[0]]
    ys = [p.y for p in fill.outline[0]]
    assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((300, 350, 400, 450), abs=TOL)


def test_text_and_images_round_trip(written) -> None:
    doc, _data, back = written
    for ours, theirs in zip(back.pages[0].texts, doc.pages[0].texts):
        assert (ours.x, ours.y, ours.w, ours.h) == pytest.approx((theirs.x, theirs.y, theirs.w, theirs.h), abs=TOL)
        assert ours.rotation == pytest.approx(theirs.rotation) and ours.text == theirs.text
        assert ours.align == theirs.align
    first = back.pages[0].texts[0]
    assert [(r.text, r.bold, r.italic, r.underline, r.font) for r in first.runs] == [
        ("Hello ", False, False, False, "Helvetica Neue"), ("bold\nnext", True, True, True, "Helvetica")]
    assert [r.size for r in first.runs] == pytest.approx([14.0, 10.0])
    assert first.runs[0].color == pytest.approx((0.8, 0.0, 0.0, 1.0))
    assert back.pages[0].texts[1].runs[0].size == pytest.approx(11.0)
    for ours, theirs in zip(back.pages[0].images, doc.pages[0].images):
        assert (ours.x, ours.y, ours.w, ours.h) == pytest.approx((theirs.x, theirs.y, theirs.w, theirs.h), abs=TOL)
        assert ours.rotation == pytest.approx(theirs.rotation) and ours.data == theirs.data
        assert ours.fmt == theirs.fmt
    assert not back.warnings


def test_pages_backgrounds_and_paper(written) -> None:
    doc, data, back = written
    assert back.title == "Synthetic ä" and len(back.pages) == 4
    for ours, theirs in zip(back.pages, doc.pages):
        assert (ours.width, ours.height) == pytest.approx((theirs.width, theirs.height), abs=1e-9)
    plain, pdf1, pdf2, a5 = back.pages
    assert plain.template_is_builtin and plain.paper == "lined"  # gnnote paper reads back as stock paper
    assert a5.template_is_builtin and a5.paper == "plain"
    assert not pdf1.template_is_builtin and pdf1.background.pdf_id == pdf2.background.pdf_id
    assert (pdf1.background.page_index, pdf2.background.page_index) == (0, 1)
    assert back.pdfs[pdf1.background.pdf_id] == doc.pdfs["doc.pdf"]  # carried byte for byte, stored once
    c = _container(data)
    blobs = c["blobs"]
    pdf = doc.pdfs["doc.pdf"]
    holders = [f for f in c["root"].strings(3) if data[blobs[f][0]:blobs[f][0] + blobs[f][1]] == pdf]
    assert len(holders) == 1
    assert len(pdf2.strokes) == 1


def test_structure_follows_the_app(written) -> None:
    _doc, data, _back = written
    assert data[:4] == b"\xaa\xbb\xcc\xde" and data[-16:-12] == b"\xaa\xbb\xcc\xde" and data[-12:-8] == b"\0" * 4
    c = _container(data)
    root = c["root"]
    assert root.number(1) == pytest.approx(1.18, abs=1e-6) and root.entry(1).type == ttv.F32
    names = root.strings(10)
    meta = root.strings(2)[0]
    assert names[0] == "n:" + meta and names[2] == "d:" + meta
    starts = root.integers(11)
    assert starts == sorted(starts) and starts[0] == 4
    header = c["record"]("n:" + meta)
    thumb = header.record(7).string(1)
    assert thumb == root.strings(3)[0] == names[1]
    s, ln = c["blobs"][thumb]
    assert data[s:s + 3] == b"\xff\xd8\xff"
    notebook = c["record"]("d:" + meta)
    layers = notebook.record(3).records(0)
    assert [layer.string(1) for layer in layers] == ["Layer 1"]
    pages = notebook.record(2).records(0)
    tags = [p.string(5) for p in pages]
    assert tags == sorted(tags) and len(set(tags)) == len(tags) and all(len(t) == 7 for t in tags)
    assert header.record(7).string(2) == pages[0].string(6)  # the cover's thumbnail link
    for name in root.strings(4):
        assert c["record"](name).integer(1) == 280
    # every UUID is 32 upper-case hex digits and unique
    uuids = [meta] + root.strings(3) + root.strings(4) + [p.string(1) for p in pages]
    assert all(len(u) == 32 and u == u.upper() and int(u, 16) >= 0 for u in uuids)
    assert len(set(uuids)) == len(uuids)
    # images are listed on their page
    first = pages[0].record(2)
    assert len(first.strings(2)) == 2 and set(first.strings(2)) <= set(root.strings(3))


def test_timestamps_are_microseconds_since_2001(written) -> None:
    _doc, data, _back = written
    c = _container(data)
    header = c["record"]("n:" + c["root"].strings(2)[0])
    created = header.number(12)
    assert created == pytest.approx(FIXED_TIME - 978307200)
    assert header.stamp(3) > created * 1e6
    data2 = write_noteful(synthetic(), Seeded(timestamp=1_800_000_000))
    c2 = _container(data2)
    assert c2["record"]("n:" + c2["root"].strings(2)[0]).number(12) == pytest.approx(1_800_000_000 - 978307200)


def test_output_is_deterministic_for_a_seed() -> None:
    assert write_noteful(synthetic(), Seeded(5)) == write_noteful(synthetic(), Seeded(5))
    assert write_noteful(synthetic(), Seeded(5)) != write_noteful(synthetic(), Seeded(6))
    a, b = write_noteful(synthetic()), write_noteful(synthetic())
    assert a != b and read_noteful(a).title == read_noteful(b).title


def test_helpers() -> None:
    tags = [order_tag(i, 50_000) for i in range(0, 50_000, 997)] + [order_tag(49_999, 50_000)]
    assert tags == sorted(tags) and all(len(t) == 7 for t in tags)
    assert [order_tag(i, 3) for i in range(3)] == ["+E3xS2+", "+E9uw6+", "+EFsOA+"]
    assert font_family(None) == "Helvetica" and font_family("HelveticaNeue-Bold") == "Helvetica Neue"
    assert font_family("TimesNewRomanPSMT") == "Times New Roman" and font_family("ArialMT") == "Arial"
    assert font_family("Helvetica Neue") == "Helvetica Neue" and font_family("Papyrus") == "Papyrus"


def test_lossy_steps_warn() -> None:
    page = Page(300, 400)
    page.images.append(Image(10, 10, 50, 50, two_page_pdf(), fmt="pdf"))
    page.images.append(Image(10, 10, 50, 50, b"GIF89a....", fmt="png"))
    page.images.append(Image(float("nan"), 10, 50, 50, png()))
    page.strokes.append(Stroke([Point(float("inf"), 1, 1), Point(2, 2, 1)]))
    page.strokes.append(Stroke([], kind="fill", outline=[[Point(1, 1, 0), Point(2, 2, 0)]]))
    page.texts.append(TextBox(10, 10, 100, 20, ""))
    page.texts.append(TextBox(10, 10, 100, 20, "abc", runs=[TextRun("xyz")]))
    pdf_page = Page(300, 400, background=PdfBackground("missing.pdf", 0))
    bad_index = Page(300, 400, background=PdfBackground("doc.pdf", 7))
    doc = Document(pages=[page, pdf_page, bad_index, Page(float("nan"), 3)], pdfs={"doc.pdf": two_page_pdf()})
    back = read_noteful(write_noteful(doc, Seeded()))
    for fragment in ("1 PDF images", "neither PNG nor JPEG", "invalid position", "not finite or too large",
                     "without a usable outline", "1 empty text boxes", "did not cover their text",
                     "is missing; paper was generated", "does not exist", "no valid size"):
        assert any(fragment in w for w in doc.warnings), fragment
    assert back.pages[0].texts[0].text == "abc" and back.pages[0].images == []
    assert (back.pages[3].width, back.pages[3].height) == pytest.approx((595.28, 841.89), abs=0.01)
    empty = Document()
    assert len(read_noteful(write_noteful(empty)).pages) == 1 and any("no pages" in w for w in empty.warnings)


def test_exif_photos_and_stretched_pdf_pages_warn() -> None:
    exif = b"\xff\xd8\xff\xe1" + struct.pack(">H", 2 + 6 + 8 + 2 + 12) + b"Exif\x00\x00" + b"MM\x00*" + \
        struct.pack(">I", 8) + struct.pack(">H", 1) + struct.pack(">HHIHH", 0x0112, 3, 1, 6, 0) + jpeg()[2:]
    doc = Document(pages=[Page(300, 400, background=PdfBackground("doc.pdf", 0),
                               images=[Image(10, 10, 30, 40, exif, fmt="jpeg", rotation=90.0)])],
                   pdfs={"doc.pdf": two_page_pdf()})
    write_noteful(doc, Seeded())
    assert any("EXIF orientation" in w for w in doc.warnings)
    assert any("Noteful stretches the PDF" in w for w in doc.warnings)


# --------------------------------------------------------------------------- strict oracle


def test_oracle_accepts_synthetic_output(samples, written, tmp_path: Path) -> None:
    doc, data, _back = written
    path = tmp_path / "synthetic.noteful"
    path.write_bytes(data)
    ref = run_oracle(samples, [path])[str(path)]
    assert "error" not in ref, ref.get("error")
    assert ref["title"] == "Synthetic ä" and ref["layers"] == ["Layer 1"]
    pages = ref["pages"]
    assert [p["background"] for p in pages] == ["pdf"] * 4
    assert [p["pdf_page"] for p in pages] == [0, 0, 1, 0]
    first = pages[0]
    assert len(first["ink"]) == 5 and len(first["objects"]) == 5
    assert [o["type"] for o in sorted(first["objects"], key=lambda o: o["z"])] == [1, 1, 12, 2, 2]
    assert sorted(first["files_on_page"]) == sorted(o["image"]["uuid"] for o in first["objects"] if o["type"] == 1)
    highlighter = first["ink"][2]
    assert highlighter["blend"] == 1 and highlighter["rgba"][3] == 1.0
    assert first["ink"][1]["variable"] and not first["ink"][0]["variable"]
    assert len({tuple(s["id"]) for p in pages for s in p["ink"]}) == sum(len(p["ink"]) for p in pages)
    zs = [s["z"] for s in first["ink"]] + [o["z"] for o in first["objects"]]
    assert len(set(zs)) == len(zs)
    images = [o for o in first["objects"] if o["type"] == 1]
    assert sorted(tuple(o["image"]["native"]) for o in images) == [(3.0, 2.0), (24.0, 31.0)]
    assert all(o["version"] == 280 for o in first["objects"])


def _written_from(samples, path: Path, tmp_path: Path) -> Tuple[Document, Path]:
    data = path.read_bytes()
    doc = to_document(data, path.name)
    out = tmp_path / (path.name.replace(".", "_") + ".noteful")
    out.write_bytes(write_noteful(doc, Seeded(3)))
    return doc, out


def _sources(samples) -> List[Path]:
    files: List[Path] = []
    for loader in (samples.goodnotes_files, samples.note_files, lambda: sample_files(samples)):
        try:
            files += loader()
        except pytest.skip.Exception:
            pass
    if not files:
        pytest.skip("no sample files available")
    return files


def test_oracle_accepts_files_written_from_every_sample(samples, tmp_path: Path) -> None:
    pairs = [_written_from(samples, path, tmp_path) for path in _sources(samples)]
    result = run_oracle(samples, [out for _doc, out in pairs])
    for doc, out in pairs:
        ref = result[str(out)]
        assert "error" not in ref, (out.name, ref.get("error"))
        assert len(ref["pages"]) == max(1, len(doc.pages))
        for page, rp in zip(doc.pages, ref["pages"]):
            ink = [s for s in page.strokes if s.kind != "fill" and s.points]
            assert len(rp["ink"]) == len(ink), out.name
            texts = [t for t in page.texts if t.text]
            rasters = [im for im in page.images if im.data[:3] == b"\xff\xd8\xff" or im.data[:8] == b"\x89PNG\r\n\x1a\n"]
            kinds = [o["type"] for o in rp["objects"]]
            assert kinds.count(2) == len(texts) and kinds.count(1) == len(rasters), out.name
            assert all(o["version"] == 280 for o in rp["objects"])
            assert (rp["size"][0], rp["size"][1]) == pytest.approx((page.width * U, page.height * U))


def test_noteful_samples_survive_a_write_read_round_trip(samples) -> None:
    for path in sample_files(samples):
        doc = read_noteful(path.read_bytes())
        back = read_noteful(write_noteful(doc, Seeded(9)))
        assert [len(p.strokes) for p in back.pages] == [len(p.strokes) for p in doc.pages]
        assert [len(p.texts) for p in back.pages] == [len(p.texts) for p in doc.pages]
        assert [len(p.images) for p in back.pages] == [len(p.images) for p in doc.pages]
        for page, again in zip(doc.pages, back.pages):
            assert (again.width, again.height) == pytest.approx((page.width, page.height), abs=1e-9)
            for s0, s1 in zip(page.strokes, again.strokes):
                if s0.controls is None and s0.kind != "fill":  # ink: identical polylines
                    assert len(s1.points) == len(s0.points)
                    for p, q in zip(s0.points, s1.points):
                        assert abs(p.x - q.x) < TOL and abs(p.y - q.y) < TOL and abs(p.width - q.width) < TOL
                    assert s1.color == pytest.approx(s0.color) and s1.kind == s0.kind
            for t0, t1 in zip(page.texts, again.texts):
                assert (t1.x, t1.y, t1.w, t1.h) == pytest.approx((t0.x, t0.y, t0.w, t0.h), abs=TOL)
                assert t1.text == t0.text and t1.align == t0.align
            for i0, i1 in zip(page.images, again.images):
                assert (i1.x, i1.y, i1.w, i1.h) == pytest.approx((i0.x, i0.y, i0.w, i0.h), abs=TOL)


def test_convert_api_writes_noteful(samples) -> None:
    files = samples.goodnotes_files()[:1]
    result = convert(files[0].read_bytes(), files[0].name, Options(target="noteful"))
    assert result.filename.endswith(".noteful") and result.target_format == "noteful"
    back = read_noteful(result.data)
    assert len(back.pages) == result.stats["pages"]
    with pytest.raises(ValueError, match="already is a Noteful file"):
        convert(result.data, result.filename, Options(target="noteful"))
    assert io.BytesIO(result.data).read(4) == b"\xaa\xbb\xcc\xde"


def test_flattening_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.noteful import writer as nf_writer

    huge = Stroke([Point(0, 0, 1), Point(1e6, 0, 1)], controls=[(Point(0, 1e6, 1), Point(1e6, 1e6, 1))])
    small = Stroke([Point(10, 10, 1), Point(30, 10, 1)], controls=[(Point(10, 20, 1), Point(30, 20, 1))])
    doc = Document(pages=[Page(300, 400, strokes=[huge, small])])
    back = read_noteful(write_noteful(doc, Seeded()))
    big, little = back.pages[0].strokes
    assert len(big.points) <= nf_writer.MAX_STROKE_SAMPLES + 2
    assert len(little.points) == len(flatten_bezier(small.points, small.controls, 1.0))  # 1 pt spacing kept
    monkeypatch.setattr(nf_writer, "MAX_WRITTEN_POINTS", 50)
    doc = Document(pages=[Page(300, 400, strokes=[small, small, small, huge])])
    back = read_noteful(write_noteful(doc, Seeded()))
    assert len(back.pages[0].strokes) == 1
    assert any("limit of 50 million ink points" in w for w in doc.warnings)


def test_a_failing_stroke_leaves_the_ink_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    from gnnote.noteful import writer as nf_writer

    calls = {"n": 0}
    real = nf_writer._Ids.stroke_id

    def flaky(self):  # noqa: ANN001 - the second stroke fails after its style record was built
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("boom")
        return real(self)

    monkeypatch.setattr(nf_writer._Ids, "stroke_id", flaky)
    strokes = [Stroke([Point(10, 10, 1), Point(20, 20, 1)], color=(1, 0, 0, 1)),
               Stroke([Point(30, 30, 1), Point(40, 40, 1)], color=(0, 1, 0, 1)),
               Stroke([Point(1e12, 0, 1), Point(0, 0, 1)]),
               Stroke([Point(50, 50, 1), Point(60, 60, 1)], color=(0, 0, 1, 1))]
    doc = Document(pages=[Page(300, 400, strokes=strokes)])
    back = read_noteful(write_noteful(doc, Seeded()))
    assert [s.color for s in back.pages[0].strokes] == [(1, 0, 0, 1), (0, 0, 1, 1)]
    assert any("a stroke was skipped (boom)" in w for w in doc.warnings)
    assert any("not finite or too large" in w for w in doc.warnings)
    assert not back.warnings


def test_paper_generator_used_for_plain_pages() -> None:
    doc = Document(pages=[Page(300, 400, paper="dotted"), Page(300, 400, paper="dotted"), Page(300, 400, paper="grid")])
    data = write_noteful(doc, Seeded())
    back = read_noteful(data)
    assert [p.paper for p in back.pages] == ["dotted", "dotted", "grid"]
    assert back.pages[0].background.pdf_id == back.pages[1].background.pdf_id != back.pages[2].background.pdf_id
    info = pdfutil.pdf_info(back.pdfs[back.pages[0].background.pdf_id])
    assert info.producer == "gnnote" and (info.pages[0].width, info.pages[0].height) == pytest.approx((300, 400))
    assert len(back.pdfs) == 2
