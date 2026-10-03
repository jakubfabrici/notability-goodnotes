"""The PDF object layer (``gnnote.pdf.objects``), the TrueType subsetter and the text layout."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import List, Tuple

import pytest

from gnnote.model import TextBox, TextRun
from gnnote.pdf.objects import (Copier, Name, PdfFile, PdfWriter, Raw, Ref, Stream, decode_text, fmt_num,
                                incremental_update, page_matrix, rewrite, serialize, text_string)
from gnnote.pdf.text import HELVETICA_WIDTHS, EmbeddedFont, Helvetica, choose_font, layout
from gnnote.pdf.ttf import DEFAULT_FONT_PATH, TrueTypeFont, default_font
from gnnote.pdfutil import pdf_info

from tests.test_pdf_helpers import build_pdf, corpus_files, one_page_pdf


# --------------------------------------------------------------------------- serialisation


def test_serialize_values() -> None:
    assert serialize(None) == b"null" and serialize(True) == b"true" and serialize(False) == b"false"
    assert serialize(12) == b"12" and serialize(-0.0) == b"0" and serialize(1e-7) == b"0.0000001"
    assert serialize(1e20) == b"100000000000000000000" and serialize(float("nan")) == b"0"
    assert serialize(Name("A B#(x)/é")) == b"/A#20B#23#28x#29#2F#E9"
    assert serialize(b"a (b) \\c") == b"(a \\(b\\) \\\\c)"
    assert serialize(b"\x00\xff\n") == b"<00FF0A>"
    assert serialize(Ref(3, 1)) == b"3 1 R" and serialize(Raw(b"[1 2]")) == b"[1 2]"
    assert serialize({"Type": Name("X"), "K": [1, 2.5, b"s"]}) == b"<</Type /X /K [1 2.5 (s)] >>"
    assert serialize(Stream({"Length": 99, "F": 1}, b"abc")) == b"<</Length 3 /F 1 >>\nstream\nabc\nendstream"
    with pytest.raises(TypeError):
        serialize(object())
    assert fmt_num(0.1 + 0.2, 3) == "0.3" and fmt_num(5, 3) == "5" and fmt_num(True) == "1"
    assert text_string("Plain") == b"Plain" and text_string("Ľ") == b"\xfe\xff\x01\x3d"
    assert decode_text(b"\xfe\xff\x01\x3d") == "Ľ" and decode_text(b"\x80 \xa0") == "• €"
    assert decode_text(b"\xef\xbb\xbfa\xc3\xa9") == "aé" and decode_text(None) == ""


def test_page_matrix_maps_the_mediabox_onto_the_displayed_page() -> None:
    box = (10.0, 20.0, 110.0, 220.0)  # 100 x 200
    corners = {"bl": (10, 20), "tl": (10, 220), "tr": (110, 220)}
    expected = {0: {"bl": (0, 0), "tl": (0, 200), "tr": (100, 200)},
                90: {"bl": (0, 100), "tl": (200, 100), "tr": (200, 0)},
                180: {"bl": (100, 200), "tl": (100, 0), "tr": (0, 0)},
                270: {"bl": (200, 0), "tl": (0, 0), "tr": (0, 100)}}
    for rotate, points in expected.items():
        m = page_matrix(box, rotate)
        for name, (x, y) in corners.items():
            got = (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])
            assert got == pytest.approx(points[name]), (rotate, name)


# --------------------------------------------------------------------------- reading / writing


def _sample_pdfs(samples) -> List[Tuple[str, bytes]]:
    items: List[Tuple[str, bytes]] = []
    root = Path(samples.root)
    for path in corpus_files(root):
        if not path.is_file() or path.stat().st_size > 30_000_000:
            continue
        if path.suffix.lower() == ".pdf":
            items.append((str(path.relative_to(root)), path.read_bytes()))
        elif path.suffix.lower() in (".goodnotes", ".note"):
            try:
                with zipfile.ZipFile(path) as z:
                    for name in z.namelist():
                        if not name.endswith("/"):
                            data = z.read(name)
                            if data.startswith(b"%PDF"):
                                items.append((f"{path.name}:{name}", data))
            except (zipfile.BadZipFile, OSError):
                continue
    if not items:
        pytest.skip("no sample PDFs")
    return items


def test_page_walk_matches_pdf_info_on_every_sample_and_damaged_variant(samples) -> None:
    """The writer imports page *n* and the reader numbers pages exactly like pdf_info does,
    so PdfBackground.page_index means the same page everywhere -- damaged files included."""
    variants = {
        "healthy": lambda d: d,
        "no startxref": lambda d: d.replace(b"startxref", b"startxxxx"),
        "offsets shifted": lambda d: d[:9] + b"%junk junk junk\n" + d[9:],
        "tail truncated": lambda d: d[: d.rfind(b"startxref")],
    }
    for name, data in _sample_pdfs(samples):
        for label, mutate in variants.items():
            if label != "healthy" and len(data) > 3_000_000:
                continue
            damaged = mutate(data)
            try:
                expected = [(p.width, p.height, p.rotation) for p in pdf_info(damaged).pages]
            except ValueError:
                expected = []
            pages = PdfFile(damaged).pages()
            assert [(p.width, p.height, p.rotate) for p in pages] == expected, (name, label)
        assert PdfFile(data).healthy(), name


def test_writer_numbers_objects_and_copier_handles_deep_graphs() -> None:
    # a 5000-long /Next chain and a reference cycle: copied without recursion
    chain = {i: b"<< /Next %d 0 R /Self %d 0 R >>" % (i + 1, i) for i in range(10, 5010)}
    chain[5010] = b"<< /Prev 10 0 R >>"
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R /Outlines 10 0 R >>",
               2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>", **chain}
    pdf = PdfFile(build_pdf(objects))
    writer = PdfWriter()
    copier = Copier(pdf, writer)
    head = copier.convert(Ref(10, 0))
    copier.flush()
    assert len(writer) == 5001 and writer.get(head)["Self"] == head
    out = rewrite(pdf)
    assert pdf_info(out).pages[0].width == 100 and len(PdfFile(out).xref) >= 5004


def test_incremental_update_keeps_the_old_bytes_and_chains_sections() -> None:
    base = one_page_pdf(b"0 g 0 0 10 10 re f", info=b"<< /Title (Old) >>")
    pdf = PdfFile(base)
    page = pdf.pages()[0]
    new_page = dict(page.dict)
    new_page["Rotate"] = 90
    first = incremental_update(pdf, {page.ref: new_page})
    assert first.startswith(base) and b"/Prev" in first[len(base):]
    second_pdf = PdfFile(first)
    assert second_pdf.healthy() and second_pdf.pages()[0].rotate == 90
    assert decode_text(second_pdf.resolve(second_pdf.info()["Title"])) == "Old"
    page2 = second_pdf.pages()[0]
    third = incremental_update(second_pdf, {page2.ref: dict(page2.dict, Rotate=180)})
    assert PdfFile(third).pages()[0].rotate == 180 and pdf_info(third).pages[0].rotation == 180
    damaged = PdfFile(base.replace(b"startxref", b"startxxxx"))
    with pytest.raises(ValueError):
        incremental_update(damaged, {page.ref: new_page})


# --------------------------------------------------------------------------- fonts


def test_shipped_font_subset() -> None:
    font = default_font()
    assert font is not None and DEFAULT_FONT_PATH.stat().st_size < 250_000
    assert font.embeddable and font.units_per_em == 2048
    for ch in "aščťžĽĎŇ іїєґІЇЄҐ αβγ №€…–—“”„":
        assert ch == " " or font.glyph(ord(ch)), ch
    assert font.glyph(ord("世")) == 0
    licence = (DEFAULT_FONT_PATH.parent / "LICENSE-DejaVu.txt").read_text(encoding="utf-8")
    assert "Bitstream Vera" in licence and "Arev" in licence


def test_glyph_subset_keeps_ids_and_composite_components() -> None:
    pymupdf = pytest.importorskip("pymupdf")
    font = default_font()
    gids = [font.glyph(ord(c)) for c in "ї Ľ"]
    data = font.subset(gids)
    sub = TrueTypeFont(data)
    assert sub.num_glyphs == font.num_glyphs and sub.cmap == font.cmap
    kept = font.closure(gids)
    assert len(kept) > len(set(gids)) + 1  # composite glyphs pulled in their components
    for gid in range(font.num_glyphs):
        size = sub.loca[gid + 1] - sub.loca[gid]
        assert (size > 0) == (gid in kept and font.loca[gid + 1] > font.loca[gid]), gid
    mu = pymupdf.Font(fontbuffer=data)  # MuPDF loads the subset program
    assert mu.has_glyph(ord("ї")) and mu.glyph_advance(ord("ї")) == pytest.approx(font.advance(gids[0]) / 2048)
    with pytest.raises(ValueError):
        TrueTypeFont(b"OTTO" + bytes(100))


def test_font_choice_and_metrics() -> None:
    font, warning = choose_font(["Grüße – « café » €", "naïve"])
    assert isinstance(font, Helvetica) and warning is None
    assert font.code("€") == (0x80, 556.0) and font.code("W") == (87, 944.0)
    assert len(HELVETICA_WIDTHS) == 224
    font2, _ = choose_font(["Ťažký", "plain"])
    assert isinstance(font2, EmbeddedFont)
    gid, width = font2.code("Ť")
    assert gid > 0 and width > 500


def test_layout_breaks_long_words_and_keeps_blank_lines() -> None:
    font = Helvetica()
    box = TextBox(0, 0, 50, 100, "Supercalifragilistic\n\nend", size=12)
    lines = layout(box, font)
    texts = ["".join(g.ch for g in line.glyphs) for line in lines]
    assert texts[-2:] == ["", "end"] and "".join(texts[:-2]) == "Supercalifragilistic"
    assert all(line.width <= 50 * 1.12 + 0.01 for line in lines)
    assert lines[1].baseline - lines[0].baseline == pytest.approx(1.1646 * 12)
    runs = TextBox(0, 0, 0, 0, "ab", runs=[TextRun("a", size=10), TextRun("b", size=30)])
    (line,) = layout(runs, font)
    assert line.size == 30 and line.baseline == pytest.approx(0.952 * 30)
