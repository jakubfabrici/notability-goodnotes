"""Text boxes in the PDF writer: font choice, metrics, line wrapping and text operators.

Font choice (per document): when every character of every text box is in Windows code page
1252 (PDF's WinAnsiEncoding) the base-14 font Helvetica is used -- nothing is embedded and
:data:`HELVETICA_WIDTHS` (the standard Helvetica advance widths of codes 32..255 in 1/1000
em) drives the wrapping.  Otherwise the shipped DejaVu Sans subset is embedded as a
``/Type0`` font: ``/Encoding /Identity-H`` (2-byte codes = glyph ids), a ``/CIDFontType2``
descendant with ``/CIDToGIDMap /Identity``, a ``/W`` array of the used glyphs' widths, the
font program (glyphs outside the document emptied, see :meth:`TrueTypeFont.subset`) as
``/FontFile2`` and a ``/ToUnicode`` CMap so the text can be searched and copied.
Characters the chosen font lacks become ``?`` (one warning per document).

Layout (in the box's own frame: origin at the text frame's top-left, y down): paragraphs
split at ``\\n``; words wrap greedily at the box width, using real advance widths; a line
may overflow the width by up to :data:`OVERFLOW` (the source app measured with its own font)
and is then condensed horizontally to fit; a word longer than a line breaks between
characters.  The first baseline sits ``0.952 em`` below the top and lines advance by
``1.1646 em`` of the larger neighbouring size (Helvetica Neue's ascender and the line
spacing Cocoa uses for GoodNotes text, ``gnnote.rtf``).  Alignment shifts each line inside
the box width.  Bold is drawn with text render mode 2 (fill + a thin outline, width
``size / 30``), italic with a 12 degree skew in the text matrix, underline as a filled
rectangle below the baseline.  The whole box is drawn under ``cm`` = rotation by
``TextBox.rotation`` (clockwise) about the box's top-left corner.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..model import RGBA, TextBox
from .objects import Name, PdfWriter, fmt_num, make_stream
from .ttf import TrueTypeFont, default_font

__all__ = ["HELVETICA_WIDTHS", "FontChoice", "Helvetica", "EmbeddedFont", "choose_font", "layout",
           "text_box_ops", "normalise", "Line", "Glyph"]

# Standard Helvetica advance widths (1/1000 em) of WinAnsiEncoding codes 32..255; 0 = no glyph.
HELVETICA_WIDTHS: Tuple[int, ...] = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 0,
    556, 0, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333, 1000, 0, 611, 0,
    0, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 0, 500, 667,
    278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333,
    400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611,
    667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,
    722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,
    556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500,
)

ASCENT = 0.952  # first baseline below the box top, in em
DESCENT = 0.2126
LINE = 1.1646  # baseline-to-baseline distance in em
OVERFLOW = 0.12  # a line may be up to 12 % wider than the box before it wraps (then condensed)
ITALIC_SKEW = math.tan(math.radians(12.0))
BOLD_STROKE = 1.0 / 30.0  # outline width of synthetic bold, in em
TAB = "    "
MIN_SIZE, MAX_SIZE, DEFAULT_SIZE = 0.5, 1000.0, 12.0


def normalise(text: str) -> str:
    """NFC, unified line breaks, tabs as spaces, other control characters removed."""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    out = []
    for ch in text:
        if ch == "\n":
            out.append(ch)
        elif ch == "\t":
            out.append(TAB)
        elif ch < " " or "\x7f" <= ch <= "\x9f" or ch == "\u00ad" or "\ud800" <= ch <= "\udfff":
            continue
        else:
            out.append(ch)
    return "".join(out)


def _cp1252(ch: str) -> Optional[int]:
    try:
        b = ch.encode("cp1252")
    except UnicodeEncodeError:
        return None
    return b[0] if len(b) == 1 and b[0] >= 32 and HELVETICA_WIDTHS[b[0] - 32] else None


class Helvetica:
    """The base-14 Helvetica with WinAnsiEncoding (one byte per character)."""

    kind = "helvetica"
    two_byte = False

    def __init__(self) -> None:
        self.missing = 0
        self._q = 63  # '?'

    def code(self, ch: str) -> Tuple[int, float]:
        """``(code, advance in 1/1000 em)``; unknown characters count as missing and print '?'."""
        code = _cp1252(ch)
        if code is None:
            self.missing += 1
            code = self._q
        return code, float(HELVETICA_WIDTHS[code - 32])

    def font_object(self, writer: PdfWriter) -> Dict[str, object]:
        return {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica"),
                "Encoding": Name("WinAnsiEncoding")}


class EmbeddedFont:
    """A TrueType font embedded as Type0 / CIDFontType2 with Identity encodings."""

    kind = "embedded"
    two_byte = True

    def __init__(self, font: TrueTypeFont):
        self.font = font
        self.missing = 0
        self.used: Dict[int, str] = {}  # gid -> first character drawn with it
        self._q = font.glyph(ord("?"))

    def code(self, ch: str) -> Tuple[int, float]:
        gid = self.font.glyph(ord(ch))
        if gid == 0:
            self.missing += 1
            gid = self._q
            ch = "?"
        self.used.setdefault(gid, ch)
        return gid, self.font.width_1000(gid)

    def _tag(self) -> str:
        """Six capitals derived from the used glyph set (the PDF subset-tag convention)."""
        h = 0
        for gid in sorted(self.used):
            h = (h * 131 + gid + 1) % 308915776  # 26 ** 6
        out = ""
        for _ in range(6):
            out += chr(65 + h % 26)
            h //= 26
        return out

    def font_object(self, writer: PdfWriter) -> Dict[str, object]:
        f = self.font
        base = Name(f"{self._tag()}+{f.postscript_name}")
        program = f.subset(self.used)
        file_ref = writer.add(make_stream({"Length1": len(program)}, program))
        bbox = [round(f.scale(v)) for v in f.bbox]
        flags = 32 | (1 if f.fixed_pitch else 0)  # nonsymbolic
        descriptor = writer.add({
            "Type": Name("FontDescriptor"), "FontName": base, "Flags": flags, "FontBBox": bbox,
            "ItalicAngle": f.italic_angle, "Ascent": round(f.scale(f.ascender)),
            "Descent": round(f.scale(f.descender)), "CapHeight": round(f.scale(f.cap_height)),
            "StemV": 80, "FontFile2": file_ref})
        widths: List[object] = []
        run_start: Optional[int] = None
        run: List[int] = []
        for gid in sorted(self.used):
            w = round(f.width_1000(gid))
            if run_start is not None and gid == run_start + len(run):
                run.append(w)
            else:
                if run_start is not None:
                    widths += [run_start, run]
                run_start, run = gid, [w]
        if run_start is not None:
            widths += [run_start, run]
        cid_font = writer.add({
            "Type": Name("Font"), "Subtype": Name("CIDFontType2"), "BaseFont": base,
            "CIDSystemInfo": {"Registry": b"Adobe", "Ordering": b"Identity", "Supplement": 0},
            "FontDescriptor": descriptor, "DW": round(f.width_1000(0)), "W": widths,
            "CIDToGIDMap": Name("Identity")})
        to_unicode = writer.add(make_stream({}, self._cmap()))
        return {"Type": Name("Font"), "Subtype": Name("Type0"), "BaseFont": base,
                "Encoding": Name("Identity-H"), "DescendantFonts": [cid_font], "ToUnicode": to_unicode}

    def _cmap(self) -> bytes:
        entries = sorted(self.used.items())
        lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
                 "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
                 "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
                 "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
        for i in range(0, len(entries), 100):
            chunk = entries[i:i + 100]
            lines.append(f"{len(chunk)} beginbfchar")
            for gid, ch in chunk:
                lines.append(f"<{gid:04X}> <{ch.encode('utf-16-be').hex().upper()}>")
            lines.append("endbfchar")
        lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
        return ("\n".join(lines) + "\n").encode("ascii")


FontChoice = object  # Helvetica | EmbeddedFont


def choose_font(texts: Sequence[str]) -> Tuple[object, Optional[str]]:
    """The font for a document's texts and an optional warning about the choice."""
    needs_unicode = any(_cp1252(ch) is None for text in texts for ch in normalise(text) if ch != "\n")
    if not needs_unicode:
        return Helvetica(), None
    font = default_font()
    if font is None:
        return Helvetica(), "the embedded Unicode font is missing; characters outside Windows-1252 print as '?'"
    return EmbeddedFont(font), None


# ----------------------------------------------------------------------------------
# layout
# ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Style:
    size: float
    color: RGBA
    bold: bool = False
    italic: bool = False
    underline: bool = False


@dataclass
class Glyph:
    ch: str
    code: int
    advance: float  # pt at the glyph's size
    style: Style


@dataclass
class Line:
    glyphs: List[Glyph]
    baseline: float = 0.0  # distance from the box top
    x: float = 0.0  # start offset inside the box
    scale: float = 1.0  # horizontal condensing factor
    size: float = DEFAULT_SIZE

    @property
    def width(self) -> float:
        return sum(g.advance for g in _strip(self.glyphs))


def _strip(glyphs: List[Glyph]) -> List[Glyph]:
    end = len(glyphs)
    while end and glyphs[end - 1].ch == " ":
        end -= 1
    return glyphs[:end]


def _clamp_size(value: Optional[float], default: float) -> float:
    try:
        v = float(value) if value is not None else default
    except (TypeError, ValueError):
        v = default
    if not math.isfinite(v) or v <= 0:
        v = default
    return min(MAX_SIZE, max(MIN_SIZE, v))


def _runs(box: TextBox) -> List[Tuple[str, Style]]:
    size = _clamp_size(box.size, DEFAULT_SIZE)
    color = tuple(box.color) if box.color else (0.0, 0.0, 0.0, 1.0)
    runs = [r for r in (box.runs or []) if r.text]
    if runs and "".join(r.text for r in runs) == box.text or (runs and not box.text):
        return [(normalise(r.text), Style(_clamp_size(r.size, size), tuple(r.color) if r.color else color,  # type: ignore[arg-type]
                                          bool(r.bold), bool(r.italic), bool(r.underline))) for r in runs]
    first = runs[0] if runs else None
    style = Style(size, color,  # type: ignore[arg-type]
                  bool(first.bold) if first else False, bool(first.italic) if first else False,
                  bool(first.underline) if first else False)
    return [(normalise(box.text or ""), style)]


def layout(box: TextBox, font) -> List[Line]:
    """Wrapped lines of ``box`` in its own frame (see the module docstring)."""
    paragraphs: List[List[Glyph]] = [[]]
    for text, style in _runs(box):
        for ch in text:
            if ch == "\n":
                paragraphs.append([])
                continue
            code, adv = font.code(ch)
            paragraphs[-1].append(Glyph(ch, code, adv * style.size / 1000.0, style))
    max_w = float(box.w) if box.w and box.w > 0 and math.isfinite(box.w) else 0.0
    limit = max_w * (1.0 + OVERFLOW) + 0.01 if max_w > 0 else float("inf")
    default_size = _clamp_size(box.size, DEFAULT_SIZE)
    lines: List[Line] = []
    for para in paragraphs:
        lines += _wrap(para, limit)
    if lines and not any(line.glyphs for line in lines):
        return []
    baseline = 0.0
    prev_size = None
    for line in lines:
        size = max((g.style.size for g in line.glyphs), default=default_size)
        line.size = size
        if prev_size is None:
            baseline = ASCENT * size
        else:  # descent of the line above + the rest of the line height of this one
            baseline += DESCENT * prev_size + (LINE - DESCENT) * size
        line.baseline = baseline
        prev_size = size
        width = line.width
        if max_w > 0 and width > max_w:
            line.scale = max_w / width
            width = max_w
        if box.align == "center" and max_w > 0:
            line.x = (max_w - width) / 2.0
        elif box.align == "right" and max_w > 0:
            line.x = max_w - width
    return lines


def _tokens(glyphs: List[Glyph]) -> List[List[Glyph]]:
    tokens: List[List[Glyph]] = []
    for g in glyphs:
        is_space = g.ch == " "
        if tokens and (tokens[-1][0].ch == " ") == is_space:
            tokens[-1].append(g)
        else:
            tokens.append([g])
    return tokens


def _wrap(glyphs: List[Glyph], limit: float) -> List[Line]:
    """Greedy word wrap of one paragraph at ``limit`` pt."""
    if not glyphs:
        return [Line([])]
    lines: List[Line] = []
    cur: List[Glyph] = []
    width = 0.0
    for token in _tokens(glyphs):
        tw = sum(g.advance for g in token)
        if token[0].ch == " ":
            if cur or not lines:  # spaces are dropped at the start of a wrapped line
                cur += token
                width += tw
            continue
        if width + tw <= limit:
            cur += token
            width += tw
            continue
        if any(g.ch != " " for g in cur):
            lines.append(Line(_strip(cur)))
            cur, width = [], 0.0
        if width + tw <= limit:
            cur += token
            width += tw
            continue
        for g in token:  # a word longer than a line breaks between characters
            if width + g.advance > limit and any(x.ch != " " for x in cur):
                lines.append(Line(_strip(cur)))
                cur, width = [], 0.0
            cur.append(g)
            width += g.advance
    lines.append(Line(_strip(cur)))
    return lines


# ----------------------------------------------------------------------------------
# operators
# ----------------------------------------------------------------------------------


def _n(x: float) -> str:
    return fmt_num(x, 3)


def _c(x: float) -> str:
    return fmt_num(min(1.0, max(0.0, x)), 4)


def text_box_ops(box: TextBox, font, font_name: str, page_height: float,
                 gs_name: Callable[[float, float, Optional[str]], str]) -> List[str]:
    """Content stream operators drawing ``box`` (``[]`` for an empty box)."""
    lines = layout(box, font)
    if not lines:
        return []
    try:
        degrees = float(box.rotation or 0.0)
    except (TypeError, ValueError):
        degrees = 0.0
    theta = math.radians(degrees % 360.0) if math.isfinite(degrees) else 0.0
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    x0, y0 = float(box.x), page_height - float(box.y)
    ops = ["q", f"{_n(cos_t)} {_n(-sin_t)} {_n(sin_t)} {_n(cos_t)} {_n(x0)} {_n(y0)} cm", "BT",
           f"/{font_name} 1 Tf"]
    underlines: List[Tuple[float, float, float, float, Style]] = []
    state: Dict[str, object] = {}
    for line in lines:
        x = line.x
        segments: List[Tuple[Style, List[Glyph]]] = []
        for g in line.glyphs:
            if segments and segments[-1][0] == g.style:
                segments[-1][1].append(g)
            else:
                segments.append((g.style, [g]))
        for style, glyphs in segments:
            s = style.size
            seg_w = sum(g.advance for g in glyphs) * line.scale
            r, gr, b = style.color[0], style.color[1], style.color[2]
            alpha = style.color[3] if len(style.color) > 3 else 1.0
            colour = f"{_c(r)} {_c(gr)} {_c(b)}"
            if state.get("colour") != colour:
                ops.append(f"{colour} rg {colour} RG")
                state["colour"] = colour
            alpha = 1.0 if alpha is None or not math.isfinite(alpha) else min(1.0, max(0.0, alpha))
            if state.get("alpha", 1.0) != alpha:
                ops.append(f"/{gs_name(alpha, alpha, None)} gs")
                state["alpha"] = alpha
            mode = 2 if style.bold else 0
            if state.get("mode", 0) != mode:
                ops.append(f"{mode} Tr")
                state["mode"] = mode
            if style.bold and state.get("lw") != s:
                ops.append(f"{_n(s * BOLD_STROKE)} w")
                state["lw"] = s
            skew = s * ITALIC_SKEW if style.italic else 0.0
            ops.append(f"{_n(s * line.scale)} 0 {_n(skew)} {_n(s)} {_n(x)} {_n(-line.baseline)} Tm")
            if font.two_byte:
                hexcodes = "".join(f"{g.code:04X}" for g in glyphs)
            else:
                hexcodes = "".join(f"{g.code:02X}" for g in glyphs)
            ops.append(f"<{hexcodes}> Tj")
            if style.underline:
                underlines.append((x, -line.baseline - 0.12 * s, seg_w, 0.06 * s, style))
            x += seg_w
    ops.append("ET")
    for ux, uy, uw, uh, style in underlines:
        ops.append(f"{_c(style.color[0])} {_c(style.color[1])} {_c(style.color[2])} rg "
                   f"{_n(ux)} {_n(uy)} {_n(uw)} {_n(uh)} re f")
    ops.append("Q")
    return ops
