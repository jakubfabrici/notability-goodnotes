"""Tests for gnnote.rtf: the real GoodNotes payloads of Test5 page 3, hand-written
Cocoa RTF with code-page and Unicode escapes, tolerance to broken input, and the
writer's byte shape / round trip."""
from __future__ import annotations

import zipfile
from typing import List

import pytest

from gnnote import protobuf as pb
from gnnote import rtf
from gnnote.model import TextRun

TEST5_PAGE3 = "notes/27B0C0FB-E6F5-4C9E-BDA0-98553E08EC7B"


# --------------------------------------------------------------------------- helpers


def _text_box_rtfs(path) -> List[bytes]:
    """RTF payloads (#8.#6) of every text-box content record of Test5 page 3, in order."""
    with zipfile.ZipFile(path) as z:
        records = pb.decode_records(z.read(TEST5_PAGE3))
    out = []
    for rec in records:
        fields = pb.try_decode_message(rec)
        if not fields:
            continue
        f8 = pb.get(fields, 8)
        if f8 is None or f8.wire_type != pb.WIRE_LEN:
            continue
        inner = pb.try_decode_message(f8.value) or []
        f6 = pb.get(inner, 6)
        if f6 is not None and f6.wire_type == pb.WIRE_LEN:
            out.append(f6.value)
    return out


@pytest.fixture(scope="module")
def test5_rtfs(samples) -> List[bytes]:
    path = samples.repo("goodparse") / "samples" / "Test5.goodnotes"
    if not path.is_file():
        pytest.skip("Test5.goodnotes not available")
    payloads = _text_box_rtfs(path)
    assert len(payloads) == 2, "expected the two text boxes of Test5 page 3 (rec 33, rec 37)"
    return payloads


def _words(data: bytes) -> List[str]:
    """Control-word skeleton: names only, in order, without parameters or text."""
    return [g[0].decode() for g in rtf._TOKEN.findall(data) if g[0]]


def wrap(body: str, header: str = "", cpg: int = 1252) -> bytes:
    return (("{\\rtf1\\ansi\\ansicpg%d\\cocoartf2709\n" % cpg) +
            "{\\fonttbl\\f0\\fnil\\fcharset0 HelveticaNeue;\\f1\\fnil\\fcharset0 HelveticaNeue-Bold;}\n"
            "{\\colortbl;\\red255\\green255\\blue255;\\red0\\green0\\blue0;\\red255\\green0\\blue0;}\n"
            "{\\*\\expandedcolortbl;;\\cssrgb\\c0\\c0\\c0;\\cssrgb\\c100000\\c0\\c0;}\n" + header +
            "\\pard\\tx560\\sl-559\\partightenfactor0\n\n" + body + "}").encode("latin-1")


# --------------------------------------------------------------------------- real payloads


def test_parse_test5_rec33(test5_rtfs):
    text, runs = rtf.parse_rtf(test5_rtfs[0])
    assert text == "Hallo"
    assert len(runs) == 1
    r = runs[0]
    assert (r.font, r.size, r.bold, r.italic, r.underline) == ("HelveticaNeue", 24.0, False, False, False)
    assert r.color == (0.0, 0.0, 0.0, 1.0)


def test_parse_test5_rec37(test5_rtfs):
    text, runs = rtf.parse_rtf(test5_rtfs[1])
    # Raw newlines in the RTF are formatting whitespace and the spaces after \i0 and \ul
    # are control-word delimiters: Test5.pdf shows one paragraph with the five glyph runs
    # "Test", "123", "italic", "underline ", "Strike " and no space before "underline".
    assert text == "Test123italicunderline Strike "
    assert [r.text for r in runs] == ["Test", "123", "italic", "underline ", "Strike "]
    assert [(r.bold, r.italic, r.underline) for r in runs] == [
        (False, False, False), (True, False, False), (False, True, False),
        (False, False, True), (False, False, False)]
    assert [r.font for r in runs] == ["HelveticaNeue", "HelveticaNeue-Bold", "HelveticaNeue-Italic",
                                      "HelveticaNeue", "HelveticaNeue"]
    assert all(r.size == 24.0 and r.color == (0.0, 0.0, 0.0, 1.0) for r in runs)


def test_make_rtf_matches_goodnotes_bytes(test5_rtfs):
    """A plain ASCII run reproduces record 33 byte for byte."""
    assert rtf.make_rtf("Hallo", [], size_half_points=48) == test5_rtfs[0]


def test_make_rtf_skeleton_matches_rec37(test5_rtfs):
    text, runs = rtf.parse_rtf(test5_rtfs[1])
    ours = rtf.make_rtf(text, runs, size_half_points=48)
    ignore = {"strike", "strikec", "pardirnatural"}  # strike-through is not in the model
    assert [w for w in _words(ours) if w not in ignore] == [w for w in _words(test5_rtfs[1]) if w not in ignore]
    # header is identical up to the font table contents
    assert ours.split(b"\\pard")[0] == test5_rtfs[1].split(b"\\pard")[0].replace(b";\n}", b";}")
    text2, runs2 = rtf.parse_rtf(ours)
    assert text2 == text
    assert [(r.text, r.bold, r.italic, r.underline, r.font) for r in runs2] == \
        [(r.text, r.bold, r.italic, r.underline, r.font) for r in runs]


# --------------------------------------------------------------------------- escapes and code pages


def test_unicode_escapes_slovak_czech():
    body = "\\f0\\fs48 \\cf2 \\uc0\\u381 lto\\u269 k\\u253  \\u357 a\\u318  \\u345 e\\u269 "
    text, runs = rtf.parse_rtf(wrap(body))
    assert text == "\u017dlto\u010dk\u00fd \u0165a\u013e \u0159e\u010d"  # Žltočký ťaľ řeč
    assert len(runs) == 1 and runs[0].size == 24.0


def test_cp1252_hex_escapes_german():
    body = "\\f0\\fs48 \\cf2 Gr\\'fc\\'dfe \\'e4\\'f6\\'fc \\'c4 \\'80"
    text, _ = rtf.parse_rtf(wrap(body))
    assert text == "Gr\u00fc\u00dfe \u00e4\u00f6\u00fc \u00c4 \u20ac"


def test_ansicpg1250_hex_escapes_slovak():
    body = "\\f0\\fs48 \\cf2 \\'8a\\'9a \\'8e\\'9e \\'e8 \\'ec \\'bc \\'e5"
    text, _ = rtf.parse_rtf(wrap(body, cpg=1250))
    assert text == "\u0160\u0161 \u017d\u017e \u010d \u011b \u013d \u013a"  # Šš Žž č ě Ľ ĺ


def test_fcharset_overrides_document_codepage():
    data = ("{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0\\fnil\\fcharset238 Arial CE;}{\\f1\\fnil\\fcharset0 Arial;}}"
            "\\f0 \\'8a\\'e8 \\f1 \\'e9}").encode("latin-1")
    text, runs = rtf.parse_rtf(data)
    assert text == "\u0160\u010d \u00e9"
    assert [r.font for r in runs] == ["Arial CE", "Arial"]


def test_multibyte_codepage_cp950():
    chinese = "\u4e2d\u6587"  # 中文
    escaped = "".join("\\'%02x" % b for b in chinese.encode("cp950"))
    data = ("{\\rtf1\\ansi\\ansicpg950{\\fonttbl\\f0\\fnil PingFang TC;}\\f0 " + escaped + " ok}").encode("ascii")
    text, _ = rtf.parse_rtf(data)
    assert text == chinese + " ok"


def test_uc_fallback_is_skipped():
    assert rtf.parse_rtf(wrap("\\uc1\\u381 ?lto"))[0] == "\u017dlto"
    assert rtf.parse_rtf(wrap("\\uc2\\u20013 \\'d6\\'d0ok"))[0] == "\u4e2d" + "ok"
    assert rtf.parse_rtf(wrap("\\uc1\\u381 \\'8e\\'9eok"))[0] == "\u017d\u017eok"  # \'8e skipped, \'9e = z-caron
    assert rtf.parse_rtf(wrap("\\uc0\\u381 \\u382 ."))[0] == "\u017d\u017e."
    # a control word or brace ends the fallback early
    assert rtf.parse_rtf(wrap("\\uc1\\u381 \\b x"))[0] == "\u017dx"
    assert rtf.parse_rtf(wrap("\\uc1\\u381 {x}"))[0] == "\u017dx"


def test_surrogate_pairs_and_negative_scalars():
    text, _ = rtf.parse_rtf(wrap("\\uc0\\u-10179 \\u-8704 \\u-26 "))  # U+1F600 (surrogates) + U+FFE6
    assert text == "\U0001f600\uffe6"
    assert rtf.parse_rtf(wrap("\\uc0\\u-10179 x"))[0] == "x"  # lone high surrogate dropped


def test_newlines_and_special_characters():
    body = "\\f0 a\\par\nb\\line c\\\nd\\tab e\\~f\\emdash g\\{\\}\\\\ \\-h\\_i"
    text, _ = rtf.parse_rtf(wrap(body))
    assert text == "a\nb\nc\nd\te\u00a0f\u2014g{}\\ h\u2011i"


def test_runs_attributes_and_nesting():
    body = ("\\f0\\fs48 \\cf2 plain {\\f1\\b\\fs60 \\cf3 bold} back {\\i it}\\ul u \\ulnone \\plain p")
    text, runs = rtf.parse_rtf(wrap(body))
    assert text == "plain bold back itu p"
    expect = [
        ("plain ", False, False, False, "HelveticaNeue", 24.0, (0.0, 0.0, 0.0, 1.0)),
        ("bold", True, False, False, "HelveticaNeue-Bold", 30.0, (1.0, 0.0, 0.0, 1.0)),
        (" back ", False, False, False, "HelveticaNeue", 24.0, (0.0, 0.0, 0.0, 1.0)),
        ("it", False, True, False, "HelveticaNeue", 24.0, (0.0, 0.0, 0.0, 1.0)),
        ("u ", False, False, True, "HelveticaNeue", 24.0, (0.0, 0.0, 0.0, 1.0)),
        ("p", False, False, False, None, None, None),
    ]
    assert [(r.text, r.bold, r.italic, r.underline, r.font, r.size, r.color) for r in runs] == expect


def test_ignorable_destinations_and_auto_colour():
    data = (b"{\\rtf1\\ansi{\\fonttbl{\\f0\\fswiss Arial;}}{\\colortbl;\\red1\\green2\\blue3;}"
            b"{\\*\\bkmkstart x}{\\info{\\author nobody}}{\\stylesheet{\\s0 Normal;}}"
            b"\\f0\\cf0 auto\\cf1 c\\cf9 d}")
    text, runs = rtf.parse_rtf(data)
    assert text == "autocd"
    assert [r.color for r in runs] == [None, (1 / 255, 2 / 255, 3 / 255, 1.0), None]
    assert runs[0].font == "Arial"


def test_upr_keeps_unicode_variant():
    data = b"{\\rtf1\\ansi{\\upr{\\f0 ansi text}{\\*\\ud{\\f0 \\uc0\\u8364  text}}}}"
    assert rtf.parse_rtf(data)[0] == "ansi text\u20ac text"


@pytest.mark.parametrize("data", [
    b"", b"{", b"}", b"{\\rtf1", b"{\\rtf1\\ansi\\u", b"{\\rtf1 \\'z1\\'", b"\\rtf1}}}}{{{",
    b"{\\rtf1\\ansicpg99999 \\'e9}", b"{\\rtf1{\\fonttbl\\f}{\\colortbl\\red;}}",
    b"{\\rtf1\\fs \\b-1 \\uc-5\\u70000 \\u99999999999999999999 x}", bytes(range(256)),
    b"{\\rtf1 " + b"{" * 5000 + b"x" + b"}" * 10, b"{\\rtf1\\ansi\\ansicpg1252 \\'ff\\'fe\\'e9}",
])
def test_never_raises(data):
    text, runs = rtf.parse_rtf(data)
    assert isinstance(text, str)
    assert "".join(r.text for r in runs) == text


def test_non_rtf_input_is_plain_text():
    text, runs = rtf.parse_rtf("Ahoj\r\nsvet \u017e".encode("utf-8"))
    assert text == "Ahoj\nsvet \u017e"
    assert runs == [TextRun("Ahoj\nsvet \u017e")]
    assert rtf.parse_rtf(b"") == ("", [])


def test_truncated_real_payload(test5_rtfs):
    for cut in (10, 100, 200, 300, 360):
        text, runs = rtf.parse_rtf(test5_rtfs[1][:cut])
        assert "".join(r.text for r in runs) == text


# --------------------------------------------------------------------------- writer


def test_make_rtf_unicode_as_uN_with_uc0():
    data = rtf.make_rtf("\u017dlto\u010dk\u00fd \u20ac \U0001f600", [], size_half_points=48)
    assert data.isascii()
    body = data.split(b"\\partightenfactor0\n\n")[1]
    assert body == b"\\f0\\fs48 \\cf2 \\uc0\\u381 lto\\u269 k\\u253  \\u8364  \\u-10179 \\u-8704 }"
    assert b"\\'" not in data
    assert rtf.parse_rtf(data)[0] == "\u017dlto\u010dk\u00fd \u20ac \U0001f600"


def test_make_rtf_escapes_and_paragraphs():
    text = "a{b}\\c\td\n\n  e\n"
    data = rtf.make_rtf(text, [])
    assert b"a\\{b\\}\\\\c\\tab d\\par\n\\par\n  e\\par\n}" in data
    assert rtf.parse_rtf(data)[0] == text


def test_make_rtf_roundtrip_runs():
    runs = [
        TextRun("Nadpis\n", bold=True, size=30.0),
        TextRun("Text so ", size=24.0),
        TextRun("\u010diarkou", italic=True, underline=True, size=24.0, color=(1.0, 0.0, 0.0, 1.0)),
        TextRun(" a ", size=24.0),
        TextRun("tu\u010dn\u00fdm kurz\u00edvou", bold=True, italic=True, size=24.0),
        TextRun(" bielym", size=24.0, color=(1.0, 1.0, 1.0, 1.0)),
        TextRun("\nTimes", font="Times", italic=True, size=20.0),
    ]
    text = "".join(r.text for r in runs)
    data = rtf.make_rtf(text, runs, size_half_points=48)
    assert data.isascii()
    head = data.split(b"\\pard")[0]
    assert (b"{\\fonttbl\\f0\\fnil\\fcharset0 HelveticaNeue;\\f1\\fnil\\fcharset0 HelveticaNeue-Bold;"
            b"\\f2\\fnil\\fcharset0 HelveticaNeue-Italic;\\f3\\fnil\\fcharset0 HelveticaNeue-BoldItalic;"
            b"\\f4\\fnil\\fcharset0 Times-Italic;}") in head
    assert b"{\\colortbl;\\red255\\green255\\blue255;\\red0\\green0\\blue0;\\red255\\green0\\blue0;}" in head
    assert b"{\\*\\expandedcolortbl;;\\cssrgb\\c0\\c0\\c0;\\cssrgb\\c100000\\c0\\c0;}" in head
    assert b"\\sl-699\\" in data  # exact line spacing from the largest font (30 units)
    assert b"\\cf1 " in data  # white reuses colour index 1
    text2, runs2 = rtf.parse_rtf(data)
    assert text2 == text
    assert len(runs2) == len(runs)
    for a, b in zip(runs, runs2):
        assert (a.text, a.bold, a.italic, a.underline, a.size) == (b.text, b.bold, b.italic, b.underline, b.size)
        assert b.color == (a.color or (0.0, 0.0, 0.0, 1.0))
    assert [r.font for r in runs2] == ["HelveticaNeue-Bold", "HelveticaNeue", "HelveticaNeue-Italic",
                                       "HelveticaNeue", "HelveticaNeue-BoldItalic", "HelveticaNeue",
                                       "Times-Italic"]
    # feeding the parsed runs back in does not double the style suffixes
    assert rtf.make_rtf(text2, runs2, size_half_points=48) == data


def test_make_rtf_defaults_and_mismatched_runs():
    data = rtf.make_rtf("abc", [TextRun("ab", bold=True)])  # runs do not cover the text: ignored
    assert b"\\b" not in data.split(b"\\pard")[1]
    assert rtf.parse_rtf(data) == ("abc", [TextRun("abc", font="HelveticaNeue", size=12.0,
                                                   color=(0.0, 0.0, 0.0, 1.0))])
    data = rtf.make_rtf("x", [], font="Helvetica", size_half_points=20, color=(0.0, 0.5, 1.0, 0.5))
    assert b"\\fcharset0 Helvetica;" in data and b"\\fs20 " in data
    assert b"\\colortbl;\\red255\\green255\\blue255;\\red0\\green128\\blue255;" in data
    assert b"\\cssrgb\\c0\\c50000\\c100000;" in data
    assert rtf.make_rtf("", []) .endswith(b"\\f0\\fs24 \\cf2 }")
    assert rtf.parse_rtf(rtf.make_rtf("", [])) == ("", [])


def test_line_spacing_matches_goodnotes():
    assert rtf.line_spacing(48) == 559
    assert rtf.line_height(48) == pytest.approx(27.95)
    assert rtf.line_spacing(24) == 280
