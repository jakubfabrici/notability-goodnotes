"""Minimal TrueType reader and glyph subsetter for embedding a font in a PDF.

Byte layout used (OpenType / TrueType specification):

* Offset table: ``sfntVersion u32`` (0x00010000 or ``true``), ``numTables u16``, three u16
  search fields, then ``numTables`` records ``tag[4] checksum u32 offset u32 length u32``.
* ``head``: ``unitsPerEm`` u16 at 18, ``xMin yMin xMax yMax`` i16 at 36..43,
  ``indexToLocFormat`` i16 at 50 (0 = u16 offsets / 2, 1 = u32 offsets),
  ``checkSumAdjustment`` u32 at 8.
* ``hhea``: ``ascender`` i16 at 4, ``descender`` i16 at 6, ``numberOfHMetrics`` u16 at 34.
* ``hmtx``: ``numberOfHMetrics`` pairs ``advanceWidth u16, lsb i16``; later glyphs repeat the
  last advance.
* ``maxp``: ``numGlyphs`` u16 at 4.
* ``OS/2``: ``fsType`` u16 at 8 (embedding permission), ``sTypoAscender``/``sTypoDescender``
  i16 at 68/70, ``sCapHeight`` i16 at 88 when version >= 2.
* ``post``: ``italicAngle`` Fixed 16.16 at 4, ``isFixedPitch`` u32 at 12.
* ``name``: records ``platformID, encodingID, languageID, nameID, length, offset`` (u16
  each) after ``format, count, stringOffset``; Windows names are UTF-16BE.
* ``cmap``: encoding records ``platformID, encodingID, offset u32``; subtable format 4
  (segments: endCode[], reserved pad, startCode[], idDelta[], idRangeOffset[]) and format
  12 (groups ``startCharCode, endCharCode, startGlyphID`` u32).
* ``loca`` / ``glyf``: glyph *g* is ``glyf[loca[g]:loca[g + 1]]``; a glyph with
  ``numberOfContours < 0`` is composite: components ``flags u16, glyphIndex u16`` then
  arguments (2 or 4 bytes, ``ARG_1_AND_2_ARE_WORDS`` 0x0001) and an optional scale (2, 4 or
  8 bytes for ``WE_HAVE_A_SCALE`` 0x0008 / ``X_AND_Y_SCALE`` 0x0040 / ``TWO_BY_TWO`` 0x0080),
  repeated while ``MORE_COMPONENTS`` 0x0020 is set.

:meth:`TrueTypeFont.subset` keeps glyph ids unchanged (a PDF ``/CIDToGIDMap /Identity``
font addresses glyphs by id) and empties every glyph outside the requested set and the
composite components it needs, rewriting ``glyf`` and a long-format ``loca`` and fixing the
table checksums and ``head.checkSumAdjustment``.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

__all__ = ["TrueTypeFont", "TrueTypeError", "default_font", "DEFAULT_FONT_PATH"]

DEFAULT_FONT_PATH = Path(__file__).resolve().parent / "fonts" / "DejaVuSans-subset.ttf"

_ARG_WORDS = 0x0001
_HAVE_SCALE = 0x0008
_MORE = 0x0020
_XY_SCALE = 0x0040
_TWO_BY_TWO = 0x0080


class TrueTypeError(ValueError):
    """The font file is not a TrueType font this module can use."""


def _checksum(data: bytes) -> int:
    padded = data + b"\0" * (-len(data) % 4)
    total = 0
    for (value,) in struct.iter_unpack(">I", padded):
        total = (total + value) & 0xFFFFFFFF
    return total


class TrueTypeFont:
    """Metrics, character map and glyph data of one TrueType font."""

    def __init__(self, data: bytes):
        self.data = bytes(data)
        try:
            self._parse()
        except (struct.error, IndexError) as exc:
            raise TrueTypeError(f"damaged TrueType font: {exc}") from None

    # -- parsing ------------------------------------------------------------------------

    def _parse(self) -> None:
        d = self.data
        version, num = struct.unpack(">IH", d[:6])
        if version not in (0x00010000, 0x74727565):
            raise TrueTypeError("not a TrueType font (no glyf outlines)")
        self.tables: Dict[str, Tuple[int, int]] = {}
        for i in range(num):
            tag, _cs, off, length = struct.unpack(">4sIII", d[12 + 16 * i:28 + 16 * i])
            self.tables[tag.decode("latin-1")] = (off, length)
        for tag in ("head", "hhea", "hmtx", "maxp", "cmap", "loca", "glyf"):
            if tag not in self.tables:
                raise TrueTypeError(f"TrueType font without a {tag!r} table")
        head = self.table("head")
        self.units_per_em = struct.unpack(">H", head[18:20])[0] or 1000
        self.bbox = struct.unpack(">hhhh", head[36:44])
        self.loca_format = struct.unpack(">h", head[50:52])[0]
        hhea = self.table("hhea")
        self.ascender, self.descender = struct.unpack(">hh", hhea[4:8])
        n_hmetrics = struct.unpack(">H", hhea[34:36])[0]
        self.num_glyphs = struct.unpack(">H", self.table("maxp")[4:6])[0]
        hmtx = self.table("hmtx")
        advances = [struct.unpack(">H", hmtx[4 * i:4 * i + 2])[0] for i in range(min(n_hmetrics, len(hmtx) // 4))]
        last = advances[-1] if advances else 0
        self.advances: List[int] = advances + [last] * max(0, self.num_glyphs - len(advances))
        self.cap_height = int(self.ascender * 0.7)
        self.fs_type = 0
        if "OS/2" in self.tables:
            os2 = self.table("OS/2")
            self.fs_type = struct.unpack(">H", os2[8:10])[0]
            os2_version = struct.unpack(">H", os2[0:2])[0]
            if os2_version >= 2 and len(os2) >= 90:
                self.cap_height = struct.unpack(">h", os2[88:90])[0]
        self.italic_angle = 0.0
        self.fixed_pitch = False
        if "post" in self.tables:
            post = self.table("post")
            self.italic_angle = struct.unpack(">i", post[4:8])[0] / 65536.0
            self.fixed_pitch = struct.unpack(">I", post[12:16])[0] != 0
        self.postscript_name = self._name(6) or "Font"
        self.cmap: Dict[int, int] = self._parse_cmap()
        loca = self.table("loca")
        if self.loca_format == 0:
            self.loca = [2 * v for v in struct.unpack(f">{len(loca) // 2}H", loca[:len(loca) // 2 * 2])]
        else:
            self.loca = list(struct.unpack(f">{len(loca) // 4}I", loca[:len(loca) // 4 * 4]))
        if len(self.loca) < self.num_glyphs + 1:
            raise TrueTypeError("loca table shorter than the glyph count")

    def table(self, tag: str) -> bytes:
        off, length = self.tables[tag]
        return self.data[off:off + length]

    def _name(self, name_id: int) -> Optional[str]:
        if "name" not in self.tables:
            return None
        t = self.table("name")
        _fmt, count, string_off = struct.unpack(">HHH", t[:6])
        fallback = None
        for i in range(count):
            pid, eid, _lang, nid, length, off = struct.unpack(">6H", t[6 + 12 * i:18 + 12 * i])
            if nid != name_id:
                continue
            raw = t[string_off + off:string_off + off + length]
            if pid == 3 or pid == 0:
                return raw.decode("utf-16-be", "replace")
            fallback = raw.decode("latin-1")
        return fallback

    def _parse_cmap(self) -> Dict[int, int]:
        t = self.table("cmap")
        count = struct.unpack(">H", t[2:4])[0]
        best: Optional[Tuple[int, int]] = None  # (rank, offset)
        for i in range(count):
            pid, eid, off = struct.unpack(">HHI", t[4 + 8 * i:12 + 8 * i])
            fmt = struct.unpack(">H", t[off:off + 2])[0]
            rank = {(3, 10, 12): 0, (0, 4, 12): 1, (0, 6, 12): 1, (3, 1, 4): 2, (0, 3, 4): 3}.get((pid, eid, fmt))
            if rank is None and fmt in (4, 12) and pid in (0, 3):
                rank = 4
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, off)
        if best is None:
            raise TrueTypeError("no Unicode cmap subtable")
        off = best[1]
        fmt = struct.unpack(">H", t[off:off + 2])[0]
        out: Dict[int, int] = {}
        if fmt == 4:
            seg_x2 = struct.unpack(">H", t[off + 6:off + 8])[0]
            n = seg_x2 // 2
            ends = struct.unpack(f">{n}H", t[off + 14:off + 14 + seg_x2])
            starts_at = off + 16 + seg_x2
            starts = struct.unpack(f">{n}H", t[starts_at:starts_at + seg_x2])
            deltas = struct.unpack(f">{n}h", t[starts_at + seg_x2:starts_at + 2 * seg_x2])
            ranges_at = starts_at + 2 * seg_x2
            ranges = struct.unpack(f">{n}H", t[ranges_at:ranges_at + seg_x2])
            for k in range(n):
                start, end, delta, ro = starts[k], ends[k], deltas[k], ranges[k]
                if start == 0xFFFF:
                    continue
                for c in range(start, end + 1):
                    if ro == 0:
                        g = (c + delta) & 0xFFFF
                    else:
                        pos = ranges_at + 2 * k + ro + 2 * (c - start)
                        g = struct.unpack(">H", t[pos:pos + 2])[0]
                        if g:
                            g = (g + delta) & 0xFFFF
                    if g:
                        out[c] = g
        elif fmt == 12:
            ngroups = struct.unpack(">I", t[off + 12:off + 16])[0]
            for k in range(ngroups):
                s, e, g = struct.unpack(">III", t[off + 16 + 12 * k:off + 28 + 12 * k])
                for c in range(s, min(e, 0x10FFFF) + 1):
                    out[c] = g + (c - s)
        else:
            raise TrueTypeError(f"unsupported cmap format {fmt}")
        return {c: g for c, g in out.items() if 0 < g < self.num_glyphs}

    # -- metrics --------------------------------------------------------------------------

    def glyph(self, codepoint: int) -> int:
        """Glyph id of a code point (0 = .notdef when the font lacks it)."""
        return self.cmap.get(codepoint, 0)

    def advance(self, gid: int) -> int:
        """Advance width in font units."""
        return self.advances[gid] if 0 <= gid < len(self.advances) else 0

    def width_1000(self, gid: int) -> float:
        """Advance width in 1/1000 em (PDF glyph space)."""
        return self.advance(gid) * 1000.0 / self.units_per_em

    def scale(self, v: float) -> float:
        return v * 1000.0 / self.units_per_em

    @property
    def embeddable(self) -> bool:
        """fsType allows embedding (bit 1 = restricted licence embedding forbids it)."""
        return not (self.fs_type & 0x0002) or bool(self.fs_type & 0x000C)

    # -- subsetting -----------------------------------------------------------------------

    def _components(self, gid: int) -> List[int]:
        glyf_off, _ = self.tables["glyf"]
        start, end = self.loca[gid], self.loca[gid + 1]
        if end - start < 10:
            return []
        g = self.data[glyf_off + start:glyf_off + end]
        if struct.unpack(">h", g[:2])[0] >= 0:
            return []
        out: List[int] = []
        pos = 10
        while pos + 4 <= len(g):
            flags, comp = struct.unpack(">HH", g[pos:pos + 4])
            out.append(comp)
            pos += 4 + (4 if flags & _ARG_WORDS else 2)
            if flags & _HAVE_SCALE:
                pos += 2
            elif flags & _XY_SCALE:
                pos += 4
            elif flags & _TWO_BY_TWO:
                pos += 8
            if not flags & _MORE:
                break
        return out

    def closure(self, gids: Iterable[int]) -> Set[int]:
        """``gids`` plus .notdef plus every composite component they use (recursively)."""
        keep: Set[int] = {0}
        todo = [g for g in gids if 0 <= g < self.num_glyphs]
        while todo:
            g = todo.pop()
            if g in keep and g != 0:
                continue
            keep.add(g)
            for comp in self._components(g):
                if 0 <= comp < self.num_glyphs and comp not in keep:
                    todo.append(comp)
        return keep

    def subset(self, gids: Iterable[int]) -> bytes:
        """The font with every glyph outside ``closure(gids)`` emptied (ids unchanged)."""
        keep = self.closure(gids)
        glyf_off, _ = self.tables["glyf"]
        glyf = bytearray()
        loca: List[int] = []
        for g in range(self.num_glyphs):
            loca.append(len(glyf))
            if g in keep:
                start, end = self.loca[g], self.loca[g + 1]
                glyf += self.data[glyf_off + start:glyf_off + end]
                glyf += b"\0" * (-len(glyf) % 4)
        loca.append(len(glyf))
        tables: Dict[str, bytes] = {}
        for tag in self.tables:
            if tag in ("glyf", "loca", "DSIG", "hdmx", "LTSH", "VDMX"):
                continue
            tables[tag] = self.table(tag)
        tables["glyf"] = bytes(glyf)
        tables["loca"] = struct.pack(f">{len(loca)}I", *loca)
        head = bytearray(tables["head"])
        head[8:12] = b"\0\0\0\0"
        head[50:52] = struct.pack(">h", 1)
        tables["head"] = bytes(head)
        return _build_sfnt(tables)


def _build_sfnt(tables: Dict[str, bytes]) -> bytes:
    tags = sorted(tables)
    num = len(tags)
    entry_selector = max(0, num.bit_length() - 1)
    search_range = (1 << entry_selector) * 16
    header = struct.pack(">IHHHH", 0x00010000, num, search_range, entry_selector, num * 16 - search_range)
    offset = 12 + 16 * num
    directory = bytearray()
    body = bytearray()
    head_pos = None
    for tag in tags:
        data = tables[tag]
        if tag == "head":
            head_pos = offset + len(body)
        directory += struct.pack(">4sIII", tag.encode("latin-1"), _checksum(data), offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    font = bytearray(header + directory + body)
    if head_pos is not None:
        adjust = (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF
        font[head_pos + 8:head_pos + 12] = struct.pack(">I", adjust)
    return bytes(font)


_DEFAULT: Dict[str, Optional[TrueTypeFont]] = {}


def default_font() -> Optional[TrueTypeFont]:
    """The shipped DejaVu Sans subset, or ``None`` when the file is missing or unreadable
    (e.g. a package installed without its data files)."""
    if "font" not in _DEFAULT:
        try:
            _DEFAULT["font"] = TrueTypeFont(DEFAULT_FONT_PATH.read_bytes())
        except (OSError, TrueTypeError):
            _DEFAULT["font"] = None
    return _DEFAULT["font"]
