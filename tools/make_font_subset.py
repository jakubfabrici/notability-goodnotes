#!/usr/bin/env python3
"""Build gnnote/pdf/fonts/DejaVuSans-subset.ttf (dev-time tool; needs fontTools).

The PDF writer embeds this font when a document's text does not fit WinAnsiEncoding (the
base-14 Helvetica case).  The subset keeps the glyphs of the Unicode blocks below, drops
hinting, OpenType layout tables and glyph names (the writer positions glyphs by their
advance widths only) and keeps the copyright and licence name records the DejaVu / Bitstream
Vera licence requires (name IDs 0-6, 13, 14).  The family name stays "DejaVu Sans": the
licence only forbids the names "Bitstream", "Vera", "Arev" and "Tavmjong Bah" for modified
fonts.

Usage::

    python3 -m pip install fonttools
    python3 tools/make_font_subset.py [--source /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf]

It also writes gnnote/pdf/fonts/LICENSE-DejaVu.txt from the font's own licence record.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "gnnote" / "pdf" / "fonts"
DEFAULT_SOURCE = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# (first, last) code points, inclusive
RANGES = [
    (0x0020, 0x007E),  # Basic Latin
    (0x00A0, 0x00FF),  # Latin-1 Supplement
    (0x0100, 0x017F),  # Latin Extended-A (Slovak, Czech, Polish, Hungarian, ...)
    (0x0180, 0x024F),  # Latin Extended-B
    (0x02B0, 0x02FF),  # Spacing Modifier Letters (caron, breve, ...)
    (0x0300, 0x036F),  # Combining Diacritical Marks
    (0x0370, 0x03FF),  # Greek and Coptic
    (0x0400, 0x04FF),  # Cyrillic (incl. Ukrainian i, yi, ye, ghe with upturn)
    (0x0500, 0x052F),  # Cyrillic Supplement
    (0x1E00, 0x1EFF),  # Latin Extended Additional (Vietnamese, Welsh, ...)
    (0x2000, 0x206F),  # General Punctuation
    (0x2070, 0x209F),  # Superscripts and Subscripts
    (0x20A0, 0x20CF),  # Currency Symbols
    (0x2100, 0x214F),  # Letterlike Symbols (numero sign, trade mark, ...)
    (0x2150, 0x218F),  # Number Forms
    (0x2190, 0x21FF),  # Arrows
    (0x2200, 0x22FF),  # Mathematical Operators
    (0x25A0, 0x25FF),  # Geometric Shapes (bullets, boxes)
    (0x2610, 0x2612),  # ballot boxes
    (0x2713, 0x2718),  # check marks and crosses
    (0xFFFD, 0xFFFD),  # replacement character
]


def build(source: str) -> bytes:
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError:  # pragma: no cover - dev tool
        sys.exit("fontTools is required: python3 -m pip install fonttools")
    opts = subset.Options()
    opts.hinting = False
    opts.glyph_names = False
    opts.notdef_outline = True
    opts.notdef_glyph = True
    opts.layout_features = []
    opts.drop_tables += ["GSUB", "GPOS", "GDEF", "MATH", "kern", "FFTM", "gasp", "BASE", "JSTF"]
    opts.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14]
    opts.name_languages = [0x409]
    opts.name_legacy = False
    font = TTFont(source, recalcTimestamp=False)  # keep head.modified: reproducible output
    subsetter = subset.Subsetter(opts)
    unicodes = set()
    for first, last in RANGES:
        unicodes.update(range(first, last + 1))
    subsetter.populate(unicodes=unicodes)
    subsetter.subset(font)
    buf = io.BytesIO()
    font.save(buf, reorderTables=True)
    return buf.getvalue()


def licence_text(source: str) -> str:
    from fontTools.ttLib import TTFont

    font = TTFont(source)
    name = font["name"]
    copyright_ = name.getDebugName(0) or ""
    licence = name.getDebugName(13) or ""
    url = name.getDebugName(14) or ""
    return ("DejaVu Sans (subset shipped with gnnote as DejaVuSans-subset.ttf)\n"
            "Source: https://dejavu-fonts.github.io/ " + (f"({url})" if url else "") + "\n\n"
            + copyright_.strip() + "\n\n" + licence.strip() + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="DejaVuSans.ttf to subset")
    ap.add_argument("--out", default=str(OUT_DIR), help="output directory")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = build(args.source)
    (out / "DejaVuSans-subset.ttf").write_bytes(data)
    (out / "LICENSE-DejaVu.txt").write_text(licence_text(args.source), encoding="utf-8")
    print(f"wrote {out / 'DejaVuSans-subset.ttf'} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
