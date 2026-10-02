"""Cocoa RTF: parse into plain text + styled runs, and generate the RTF GoodNotes writes.

GoodNotes stores a text box as the RTF that ``NSAttributedString`` produces
(``docs/goodnotes-elements.md`` section 4.2, Test5 page 3 records 33 and 37)::

    {\\rtf1\\ansi\\ansicpg1252\\cocoartf2709
    \\cocoatextscaling1\\cocoaplatform1{\\fonttbl\\f0\\fnil\\fcharset0 HelveticaNeue;\\f1\\fnil\\fcharset0 HelveticaNeue-Bold;}
    {\\colortbl;\\red255\\green255\\blue255;\\red0\\green0\\blue0;}
    {\\*\\expandedcolortbl;;\\cssrgb\\c0\\c0\\c0;}
    \\pard\\tx560\\tx1120...\\tx6720\\sl-559\\pardirnatural\\partightenfactor0

    \\f0\\fs48 \\cf2 Test
    \\f1\\b 123\\par
    more}

Byte-level facts implemented here (RTF 1.9 syntax as far as Cocoa and GoodNotes use it):

* The file is 7-bit ASCII.  ``{`` ``}`` open and close groups; character formatting is
  restored when a group closes.  Raw CR/LF bytes are formatting whitespace and ignored.
* A control word is ``\\`` + letters + optional signed decimal parameter; one single
  space after it is a delimiter and belongs to the control word (``\\fs48 \\cf2 Hallo``
  is the text ``Hallo``).  A control symbol is ``\\`` + one non-letter byte: ``\\'xx`` is
  one byte in the document code page, ``\\\\`` ``\\{`` ``\\}`` are literals, ``\\*`` marks an
  ignorable destination, ``\\~`` ``\\-`` ``\\_`` are special characters and ``\\`` + CR/LF
  is a paragraph break.
* ``\\ansicpgN`` selects the code page for ``\\'xx`` and raw high bytes (Cocoa writes 1252;
  parser-for-goodnotes saw CP950 from Chinese GoodNotes installs); ``\\fcharsetN`` of the
  current font overrides it (238 = cp1250 for Slovak/Czech, ...).  Consecutive ``\\'xx``
  bytes are decoded together so multi-byte code pages work.
* ``\\uN`` is a signed 16-bit Unicode scalar (negative + 65536; UTF-16 surrogate pairs are
  combined); the following ``\\ucN`` characters (default 1, Cocoa writes ``\\uc0``) are the
  ANSI fallback and are skipped.
* ``\\fonttbl``: ``\\fN ... Name;`` entries (``\\fnil\\fcharset0`` PostScript names such as
  ``HelveticaNeue-Bold``).  ``\\colortbl``: ``\\redN\\greenN\\blueN;`` entries, 0..255, the
  empty first entry is "auto" (index 0 = default colour).  ``\\*\\expandedcolortbl`` is the
  same table as ``\\cssrgb\\cN`` per-cent x 1000 and is skipped (ignorable destination).
* Character formatting: ``\\b`` ``\\b0`` ``\\i`` ``\\i0`` ``\\ul`` (any ``\\ul*`` variant)
  ``\\ulnone`` ``\\ul0`` ``\\fN`` ``\\fsN`` (half-points, in the units of the surrounding
  document -- GoodNotes canvas units) ``\\cfN`` ``\\plain``.  ``\\strike`` has no model
  field and is dropped.  ``\\par`` ``\\line`` ``\\`` + newline ``\\row`` become ``"\\n"``,
  ``\\tab`` becomes ``"\\t"``.
* Destinations that carry no visible text (``\\stylesheet`` ``\\info`` ``\\pict`` headers,
  footers, footnotes, ``\\fldinst`` ...) and every ``\\*`` group are skipped.

The writer reproduces record 33 byte for byte for an ASCII single-run text (the test
checks that), extends the font and colour tables for bold / italic / coloured runs the way
Cocoa does (``HelveticaNeue-Bold`` as ``\\f1`` with ``\\b`` set, colours appended to both
tables, white always at index 1 and the default text colour at index 2), writes every
non-ASCII character as ``\\uN`` after a single ``\\uc0`` and paragraph breaks as ``\\par``.
``\\sl-N`` is the exact line spacing Cocoa derives from HelveticaNeue's metrics
(ascent + descent = 1.1646 em, in twentieths of a unit: 24-unit text -> ``\\sl-559``).

Sizes are never converted here: ``TextRun.size`` is ``\\fsN / 2`` in whatever unit the
document uses (canvas units for GoodNotes, see ``design.md`` section 4.1); the codec
converts to points.
"""
from __future__ import annotations

import codecs
import re
from typing import Dict, List, Optional, Sequence, Tuple

from gnnote.model import RGBA, TextRun

__all__ = [
    "parse_rtf",
    "make_rtf",
    "line_spacing",
    "line_height",
    "LINE_HEIGHT_FACTOR",
    "DEFAULT_FONT",
]

DEFAULT_FONT = "HelveticaNeue"
BLACK: RGBA = (0.0, 0.0, 0.0, 1.0)
WHITE: RGBA = (1.0, 1.0, 1.0, 1.0)

#: Line height / font size for HelveticaNeue as Cocoa lays it out (559 / 20 / 24).
LINE_HEIGHT_FACTOR = 559.0 / 20.0 / 24.0

# ``\fcharsetN`` -> Python codec (RTF 1.9 table; only entries with a Python codec).
_CHARSET_CODECS: Dict[int, str] = {
    0: "cp1252", 2: "cp1252", 77: "mac_roman", 128: "cp932", 129: "cp949", 130: "cp1361",
    134: "cp936", 136: "cp950", 161: "cp1253", 162: "cp1254", 163: "cp1258", 177: "cp1255",
    178: "cp1256", 186: "cp1257", 204: "cp1251", 222: "cp874", 238: "cp1250", 254: "cp437",
    255: "cp850",
}

# Control symbols / words that stand for one character.
_SPECIAL_CHARS: Dict[str, str] = {
    "~": "\u00a0", "_": "\u2011", "-": "",  # optional hyphen: invisible
    "tab": "\t", "emdash": "\u2014", "endash": "\u2013", "emspace": "\u2003",
    "enspace": "\u2002", "qmspace": "\u2005", "bullet": "\u2022", "lquote": "\u2018",
    "rquote": "\u2019", "ldblquote": "\u201c", "rdblquote": "\u201d", "zwj": "\u200d",
    "zwnj": "\u200c", "zwbo": "\u200b", "zwnbo": "\u2060", "lbr": "", "ltrmark": "\u200e",
    "rtlmark": "\u200f", "chdate": "", "chtime": "", "chpgn": "",
}

# Destinations whose content is never visible text.
_SKIP_DESTINATIONS = frozenset({
    "stylesheet", "info", "pict", "object", "header", "footer", "headerl", "headerr",
    "headerf", "footerl", "footerr", "footerf", "footnote", "fldinst", "xe", "tc", "pn",
    "pntext", "txe", "rxe", "bkmkstart", "bkmkend", "docvar", "userprops", "listtable",
    "listoverridetable", "revtbl", "rsidtbl", "generator", "template", "nonshppict",
    "shpinst", "shprslt", "themedata", "colorschememapping", "latentstyles", "datastore",
    "xmlnstbl", "mmathPr", "fchars", "lchars", "ud", "nestrow", "nesttableprops",
})

_TOKEN = re.compile(
    rb"\\([a-zA-Z]+)(-?[0-9]+)? ?"   # control word (+ optional delimiter space)
    rb"|\\'([0-9a-fA-F]{2})"        # hex escape
    rb"|\\(\r\n|\r|\n|.)"            # control symbol (\ + newline = \par)
    rb"|([{}])"                      # group delimiters
    rb"|([\r\n]+)"                   # ignored line breaks
    rb"|([^\\{}\r\n]+)",             # run of plain bytes
    re.DOTALL,
)


# --------------------------------------------------------------------------- parsing


class _State:
    """Character formatting that a group push/pop saves and restores."""

    __slots__ = ("font", "size", "bold", "italic", "underline", "color", "uc", "dest")

    def __init__(self) -> None:
        self.font: Optional[int] = None      # index into the font table
        self.size: Optional[float] = None    # \fsN / 2
        self.bold = False
        self.italic = False
        self.underline = False
        self.color: Optional[int] = None     # \cfN; 0 or None = auto
        self.uc = 1                          # \ucN skip count
        self.dest: Optional[str] = None      # "skip", "fonttbl", "colortbl" or None

    def copy(self) -> "_State":
        s = _State()
        s.font, s.size, s.bold, s.italic, s.underline = (
            self.font, self.size, self.bold, self.italic, self.underline)
        s.color, s.uc, s.dest = self.color, self.uc, self.dest
        return s


class _Runs:
    """Accumulates characters, merging neighbours with the same attributes."""

    def __init__(self) -> None:
        self.runs: List[TextRun] = []
        self._key: Optional[tuple] = None
        self._buf: List[str] = []

    def add(self, text: str, key: tuple) -> None:
        if not text:
            return
        if key != self._key:
            self.flush()
            self._key = key
        self._buf.append(text)

    def flush(self) -> None:
        if self._buf and self._key is not None:
            bold, italic, underline, font, size, color = self._key
            self.runs.append(TextRun("".join(self._buf), bold=bold, italic=italic,
                                     underline=underline, font=font, size=size, color=color))
        self._buf = []


def _codec(name: str) -> str:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return "cp1252"


def _decode_bytes(raw: bytes, codec: str) -> str:
    try:
        return raw.decode(codec, errors="replace")
    except Exception:  # noqa: BLE001 - a codec that cannot decode at all
        return raw.decode("latin-1", errors="replace")


def parse_rtf(data: bytes) -> Tuple[str, List[TextRun]]:
    """Parse Cocoa/GoodNotes RTF into ``(plain_text, runs)``.

    ``"".join(run.text for run in runs) == plain_text``; paragraph and line breaks are
    ``"\\n"`` inside the runs.  ``TextRun.font`` is the font-table name (``None`` when the
    index is unknown), ``size`` is ``\\fsN / 2`` in the document's units, ``color`` is
    ``None`` for the automatic colour.  Never raises: malformed input yields whatever
    could be read.
    """
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    data = bytes(data)
    if b"\\rtf" not in data[:64]:
        # Not RTF at all: best effort, treat as plain text.
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        return text, ([TextRun(text)] if text else [])
    runs = _Runs()
    try:
        _parse(data, runs)
    except Exception:  # noqa: BLE001 - tolerant reader: keep what was decoded so far
        pass
    runs.flush()
    text = "".join(r.text for r in runs.runs)
    return text, runs.runs


def _parse(data: bytes, out: _Runs) -> None:
    fonts: Dict[int, str] = {}
    charsets: Dict[int, int] = {}
    colors: List[Optional[RGBA]] = []
    doc_codec = "cp1252"
    state = _State()
    stack: List[_State] = []
    pending = bytearray()      # \'xx / raw high bytes waiting for a complete decode
    skip_chars = 0             # \uN fallback characters still to skip
    high_surrogate: Optional[int] = None
    # font table scratch
    ft_index: Optional[int] = None
    ft_name: List[str] = []
    # colour table scratch
    ct_cur: List[Optional[int]] = [None, None, None]
    ct_seen = False
    ignorable_next = False     # the previous token was \* (next control word is ignorable)

    def key() -> tuple:
        font = fonts.get(state.font) if state.font is not None else None
        color: Optional[RGBA] = None
        if state.color and 0 < state.color < len(colors):
            color = colors[state.color]
        return (state.bold, state.italic, state.underline, font, state.size, color)

    def current_codec() -> str:
        cs = charsets.get(state.font) if state.font is not None else None
        if cs is not None and cs in _CHARSET_CODECS and cs != 0:
            return _CHARSET_CODECS[cs]
        return doc_codec

    def flush_pending() -> None:
        nonlocal pending
        if pending:
            emit(_decode_bytes(bytes(pending), current_codec()))
            pending = bytearray()

    def emit(text: str) -> None:
        if state.dest == "fonttbl":
            ft_name.append(text)
        elif state.dest is None:
            out.add(text, key())

    def ft_finish() -> None:
        nonlocal ft_name
        if ft_index is not None:
            name = "".join(ft_name).strip()
            if name and ft_index not in fonts:
                fonts[ft_index] = name
        ft_name = []

    def ct_finish() -> None:
        nonlocal ct_cur, ct_seen
        if ct_cur == [None, None, None]:
            colors.append(None if not ct_seen else BLACK)
        else:
            r, g, b = (max(0, min(255, c or 0)) / 255.0 for c in ct_cur)
            colors.append((r, g, b, 1.0))
        ct_cur = [None, None, None]
        ct_seen = True

    for m in _TOKEN.finditer(data):
        word, param, hexpair, symbol, brace, _nl, text = m.groups()
        if hexpair is not None:
            if skip_chars > 0:
                skip_chars -= 1
                continue
            if state.dest == "skip":
                continue
            pending.append(int(hexpair, 16))
            continue
        if text is not None:
            if state.dest == "skip":
                continue
            if skip_chars > 0:
                drop = min(skip_chars, len(text))
                skip_chars -= drop
                text = text[drop:]
                if not text:
                    continue
            if state.dest == "colortbl":
                for _i in range(text.count(b";")):
                    ct_finish()
                continue
            if state.dest == "fonttbl":
                for i, part in enumerate(text.split(b";")):
                    if i:
                        ft_finish()
                    if part:
                        ft_name.append(part.decode("ascii", errors="replace"))
                continue
            # keep high bytes with the pending hex bytes (same code page), emit ASCII directly
            if any(b >= 0x80 for b in text):
                pending.extend(text)
            else:
                flush_pending()
                emit(text.decode("ascii"))
            continue
        if _nl is not None:
            continue
        # anything below is a structural token: the fallback skip ends here
        skip_chars = 0
        if brace is not None:
            flush_pending()
            if brace == b"{":
                stack.append(state)
                state = state.copy()
                ignorable_next = False
                if state.dest == "fonttbl":
                    ft_finish()
            else:
                if state.dest == "fonttbl":
                    ft_finish()
                if stack:
                    state = stack.pop()
                ignorable_next = False
            continue
        if symbol is not None:
            flush_pending()
            if symbol in (b"\r\n", b"\r", b"\n"):
                if state.dest is None:
                    emit("\n")
            elif symbol == b"*":
                ignorable_next = True
                continue
            elif symbol in (b"\\", b"{", b"}"):
                emit(symbol.decode("ascii"))
            else:
                ch = _SPECIAL_CHARS.get(symbol.decode("latin-1"))
                if ch:
                    emit(ch)
            ignorable_next = False
            continue
        # control word
        name = word.decode("ascii")
        n = int(param) if param is not None else None
        if ignorable_next:
            ignorable_next = False
            if name not in ("ud",):  # \*\ud holds the Unicode variant of a \upr group: keep it
                state.dest = "skip"
            continue
        if state.dest == "skip":
            continue
        if name == "u" and n is not None:
            flush_pending()
            high_surrogate, cp = _combine_surrogates(high_surrogate, n + 65536 if n < 0 else n)
            if cp is not None:
                emit(chr(cp))
            skip_chars = state.uc
            continue
        flush_pending()
        if name == "uc":
            state.uc = n if n is not None and n >= 0 else 1
        elif name == "f" and n is not None:
            if state.dest == "fonttbl":
                ft_finish()
                ft_index = n
            else:
                state.font = n
        elif name == "fs" and n is not None:
            state.size = n / 2.0 if n > 0 else None
        elif name == "b":
            state.bold = n != 0
        elif name == "i":
            state.italic = n != 0
        elif name == "ulnone":
            state.underline = False
        elif name.startswith("ul") and name not in ("ulc", "ultab"):
            state.underline = n != 0  # \ul, \uld, \ulw ... all draw an underline; \ul0 clears
        elif name == "plain":
            state.bold = state.italic = state.underline = False
            state.font = None
            state.size = None
            state.color = None
        elif name == "cf":
            state.color = n or 0
        elif name in ("par", "line", "row", "sect", "page"):
            emit("\n")
        elif name in _SPECIAL_CHARS:
            emit(_SPECIAL_CHARS[name])
        elif name == "fonttbl":
            state.dest = "fonttbl"
            ft_index = None
            ft_name = []
        elif name == "colortbl":
            state.dest = "colortbl"
            ct_cur = [None, None, None]
            ct_seen = False
        elif name == "fcharset" and n is not None and state.dest == "fonttbl" and ft_index is not None:
            charsets[ft_index] = n
        elif state.dest == "colortbl" and name in ("red", "green", "blue") and n is not None:
            ct_cur[("red", "green", "blue").index(name)] = n
        elif name == "ansicpg" and n is not None:
            doc_codec = _codec("cp%d" % n)
        elif name == "mac":
            doc_codec = "mac_roman"
        elif name == "pc":
            doc_codec = "cp437"
        elif name == "pca":
            doc_codec = "cp850"
        elif name in _SKIP_DESTINATIONS:
            state.dest = "skip"
    flush_pending()


def _combine_surrogates(high: Optional[int], cp: int) -> Tuple[Optional[int], Optional[int]]:
    """Fold a ``\\uN`` scalar into UTF-16 surrogate pairing.

    Returns ``(pending_high, scalar_to_emit)``: a high half is held back, a low half
    completes the pair, anything else is emitted as is (lone halves are dropped).
    """
    if 0xD800 <= cp <= 0xDBFF:
        return cp, None
    if high is not None and 0xDC00 <= cp <= 0xDFFF:
        return None, 0x10000 + ((high - 0xD800) << 10) + (cp - 0xDC00)
    if 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
        return None, None
    return None, cp


# --------------------------------------------------------------------------- writing


def line_spacing(size_half_points: int) -> int:
    """The ``\\sl`` magnitude (twentieths of a unit) Cocoa writes for HelveticaNeue text."""
    return int(round(size_half_points / 2.0 * LINE_HEIGHT_FACTOR * 20.0))


def line_height(size_half_points: int) -> float:
    """Height of one line in the font-size unit (24-unit text -> 27.95), for box sizing."""
    return line_spacing(size_half_points) / 20.0


_STYLE_SUFFIX = {
    (False, False): "", (True, False): "-Bold", (False, True): "-Italic", (True, True): "-BoldItalic",
}
_SUFFIX_FLAGS = {
    "-BoldItalic": (True, True), "-BoldOblique": (True, True), "-Bold": (True, False),
    "-Italic": (False, True), "-Oblique": (False, True), "-Regular": (False, False),
}
_OBLIQUE_FAMILIES = {"Helvetica", "Courier", "AvenirNext", "Avenir"}


def _font_name(family: Optional[str], bold: bool, italic: bool, default: str) -> str:
    """PostScript name for a family + style the way Cocoa names HelveticaNeue variants."""
    name = family or default
    base = name
    for suffix, (sb, si) in _SUFFIX_FLAGS.items():
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            bold, italic = bold or sb, italic or si
            break
    suffix = _STYLE_SUFFIX[(bold, italic)]
    if base in _OBLIQUE_FAMILIES and italic:
        suffix = "-BoldOblique" if bold else "-Oblique"
    return base + suffix


def _escape(text: str, uc_emitted: List[bool]) -> str:
    parts: List[str] = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        o = ord(ch)
        if ch == "\\":
            parts.append("\\\\")
        elif ch == "{":
            parts.append("\\{")
        elif ch == "}":
            parts.append("\\}")
        elif ch == "\n":
            parts.append("\\par\n")
        elif ch == "\t":
            parts.append("\\tab ")
        elif 0x20 <= o < 0x7F:
            parts.append(ch)
        elif o < 0x20:
            continue
        else:
            if not uc_emitted[0]:
                parts.append("\\uc0")
                uc_emitted[0] = True
            if o > 0xFFFF:
                o -= 0x10000
                for half in (0xD800 + (o >> 10), 0xDC00 + (o & 0x3FF)):
                    parts.append("\\u%d " % (half - 65536))
            else:
                parts.append("\\u%d " % (o - 65536 if o >= 0x8000 else o))
    return "".join(parts)


def _color_entry(color: RGBA) -> str:
    r, g, b = (int(round(max(0.0, min(1.0, c)) * 255)) for c in color[:3])
    return "\\red%d\\green%d\\blue%d;" % (r, g, b)


def _expanded_entry(color: RGBA) -> str:
    r, g, b = (int(round(max(0.0, min(1.0, c)) * 100000)) for c in color[:3])
    return "\\cssrgb\\c%d\\c%d\\c%d;" % (r, g, b)


def _rgb_key(color: RGBA) -> Tuple[int, int, int]:
    return tuple(int(round(max(0.0, min(1.0, c)) * 255)) for c in color[:3])  # type: ignore[return-value]


def make_rtf(text: str, runs: Sequence[TextRun] = (), font: str = DEFAULT_FONT,
             size_half_points: int = 24, color: RGBA = BLACK) -> bytes:
    """Generate the Cocoa RTF GoodNotes writes for a text box.

    ``runs`` carry the styling; they are used when their texts concatenate to ``text``,
    otherwise ``text`` is written as one run with the defaults.  ``TextRun.size`` is in the
    same unit as ``size_half_points / 2`` (GoodNotes canvas units); ``TextRun.font`` is a
    family or PostScript name (``HelveticaNeue`` becomes ``HelveticaNeue-Bold`` for a bold
    run, as Cocoa does).  Alpha of colours is not representable and is dropped.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    run_list = [r for r in runs if r.text]
    if not run_list or "".join(r.text for r in run_list) != text:
        run_list = [TextRun(text)]

    # font table in order of first use, \f0 = default regular font
    fonts: List[str] = [_font_name(None, False, False, font)]
    colors: List[RGBA] = [WHITE, color]
    color_keys = [_rgb_key(WHITE), _rgb_key(color)]
    for r in run_list:
        name = _font_name(r.font, r.bold, r.italic, font)
        if name not in fonts:
            fonts.append(name)
        if r.color is not None and _rgb_key(r.color) not in color_keys:
            colors.append(r.color)
            color_keys.append(_rgb_key(r.color))

    max_half = max([size_half_points] + [int(round(r.size * 2)) for r in run_list if r.size])
    header = (
        "{\\rtf1\\ansi\\ansicpg1252\\cocoartf2709\n"
        "\\cocoatextscaling1\\cocoaplatform1{\\fonttbl"
        + "".join("\\f%d\\fnil\\fcharset0 %s;" % (i, name) for i, name in enumerate(fonts))
        + "}\n{\\colortbl;" + "".join(_color_entry(c) for c in colors) + "}\n"
        "{\\*\\expandedcolortbl;;" + "".join(_expanded_entry(c) for c in colors[1:]) + "}\n"
        "\\pard" + "".join("\\tx%d" % (560 * i) for i in range(1, 13))
        + "\\sl-%d\\pardirnatural\\partightenfactor0\n\n" % line_spacing(max_half)
    )

    body: List[str] = []
    uc_emitted = [False]
    cur_font = cur_size = cur_color = None
    cur_bold = cur_italic = cur_ul = False
    for r in run_list:
        fi = fonts.index(_font_name(r.font, r.bold, r.italic, font))
        half = int(round(r.size * 2)) if r.size else size_half_points
        ci = 1 + color_keys.index(_rgb_key(r.color)) if r.color is not None else 2
        group: List[str] = []
        if fi != cur_font:
            if body:
                body.append("\n")
            group.append("\\f%d" % fi)
            cur_font = fi
        if r.italic != cur_italic:
            group.append("\\i" if r.italic else "\\i0")
            cur_italic = r.italic
        if r.bold != cur_bold:
            group.append("\\b" if r.bold else "\\b0")
            cur_bold = r.bold
        if half != cur_size:
            group.append("\\fs%d" % half)
            cur_size = half
        if group:
            body.append("".join(group) + " ")
        if ci != cur_color:
            body.append("\\cf%d " % ci)
            cur_color = ci
        if r.underline != cur_ul:
            body.append("\\ul " if r.underline else "\\ulnone ")
            cur_ul = r.underline
        body.append(_escape(r.text, uc_emitted))
    return (header + "".join(body) + "}").encode("ascii")
