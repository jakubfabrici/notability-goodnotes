"""JPEG and PNG images as PDF image XObjects (pure Python, no pixel loops where avoidable).

JPEG (passed through untouched, ``/Filter /DCTDecode``)
    The marker segments before the first scan are walked: ``SOFn`` (C0-C3, C5-C7, C9-CB,
    CD-CF; payload ``precision u8, height u16, width u16, components u8``) gives the size
    and colour space (1 = DeviceGray, 3 = DeviceRGB, 4 = DeviceCMYK), an ``APP14`` segment
    starting ``Adobe`` marks Adobe's inverted CMYK, which needs ``/Decode [1 0 1 0 1 0 1 0]``,
    and an ``APP1`` ``Exif\\0\\0`` segment carries the orientation (TIFF IFD0 tag 0x0112).
    PDF viewers ignore EXIF: the raw pixel grid is what gets drawn.

PNG (``\\x89PNG\\r\\n\\x1a\\n``, then chunks ``length u32, type[4], data, crc u32``)
    ``IHDR`` = ``width u32, height u32, bit depth, colour type, compression, filter,
    interlace``; ``PLTE`` = RGB triples; ``tRNS`` = per-palette-entry alpha (type 3) or one
    16-bit colour key (types 0 / 2); ``IDAT`` chunks concatenate to one zlib stream of rows,
    each ``filter type u8`` + filtered bytes (None / Sub / Up / Average / Paeth, all
    predicting from the *same byte of the previous pixel* (``bpp`` bytes back) and the row
    above).  How each kind reaches the PDF:

    * grey / RGB / palette, not interlaced: the IDAT bytes are copied as they are with
      ``/FlateDecode /DecodeParms << /Predictor 15 /Colors c /BitsPerComponent b /Columns w >>``
      (PDF's PNG predictors are the same filters); a palette becomes ``[/Indexed /DeviceRGB
      n-1 <PLTE>]``, a grey / RGB ``tRNS`` key becomes a ``/Mask [min max ...]`` colour key.
    * grey + alpha / RGBA, not interlaced: because every filter predicts per channel, the
      *filtered* rows can be split byte-wise into a colour stream and an alpha stream that
      are still valid PNG-predicted data (``bpp`` 3 / 1 instead of 4, ...): only inflate,
      slice and deflate, no unfiltering.  The alpha becomes the ``/SMask``.
    * palette + ``tRNS``, and every interlaced (Adam7) image: decoded (inflate, unfilter --
      Up and Sub as whole-row big-integer arithmetic, Average and Paeth byte by byte --,
      de-interlace by strided slice assignment, sub-byte samples unpacked with translation
      tables), then written as raw 8/16-bit samples; palette transparency becomes an
      ``/SMask`` through a 256-entry translation table.

Images above :data:`MAX_PIXELS` are refused with :class:`ImageError`.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .objects import Name

__all__ = ["ImageError", "PdfImage", "image_kind", "jpeg_info", "jpeg_image", "png_image",
           "decode_png", "MAX_PIXELS", "EXIF_ROTATION"]

MAX_PIXELS = 60_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
# EXIF orientation -> clockwise display rotation of the raw pixels (mirrored ones: rotation part)
EXIF_ROTATION = {1: 0, 2: 0, 3: 180, 4: 180, 5: 270, 6: 90, 7: 90, 8: 270}


class ImageError(ValueError):
    """The image cannot be embedded (unsupported, damaged or too large)."""


@dataclass
class PdfImage:
    """An image XObject ready to be written: dictionary (without ``/Length``), stream data
    and an optional soft mask (another dictionary + data)."""

    width: int
    height: int
    dict: Dict[str, Any]
    data: bytes
    smask: Optional[Tuple[Dict[str, Any], bytes]] = None
    notes: List[str] = field(default_factory=list)


def image_kind(data: bytes) -> Optional[str]:
    """``"png"``, ``"jpeg"`` or ``"pdf"`` from the bytes; ``None`` for anything else."""
    if data[:8] == PNG_SIGNATURE:
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if b"%PDF-" in data[:1024]:
        return "pdf"
    return None


def _image_dict(width: int, height: int, colorspace: Any, bpc: int) -> Dict[str, Any]:
    return {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": width, "Height": height,
            "ColorSpace": colorspace, "BitsPerComponent": bpc}


def _check_size(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ImageError("image without pixels")
    if width * height > MAX_PIXELS:
        raise ImageError(f"image of {width} x {height} pixels is larger than {MAX_PIXELS} pixels")


# ----------------------------------------------------------------------------------
# JPEG
# ----------------------------------------------------------------------------------


def jpeg_info(data: bytes) -> Tuple[int, int, int, int, bool, int]:
    """``(width, height, components, precision, adobe, exif_orientation)`` of a JPEG."""
    if data[:2] != b"\xff\xd8":
        raise ImageError("not a JPEG")
    pos = 2
    n = len(data)
    adobe = False
    orientation = 1
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            raise ImageError("damaged JPEG marker structure")
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xD9, 0xDA):
            break
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if length < 2:
            raise ImageError("damaged JPEG segment length")
        seg = data[pos + 4:pos + 2 + length]
        if marker == 0xEE and seg[:5] == b"Adobe":
            adobe = True
        elif marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
            orientation = _exif_orientation(seg[6:])
        elif marker in _SOF_MARKERS:
            if len(seg) < 6:
                raise ImageError("truncated JPEG frame header")
            precision = seg[0]
            height, width = struct.unpack(">HH", seg[1:5])
            comps = seg[5]
            return width, height, comps, precision, adobe, orientation
        pos += 2 + length
    raise ImageError("JPEG without a frame header")


def _exif_orientation(tiff: bytes) -> int:
    try:
        if tiff[:4] == b"II*\x00":
            order = "<"
        elif tiff[:4] == b"MM\x00*":
            order = ">"
        else:
            return 1
        ifd = struct.unpack(order + "I", tiff[4:8])[0]
        count = struct.unpack(order + "H", tiff[ifd:ifd + 2])[0]
        for i in range(min(count, 512)):
            entry = tiff[ifd + 2 + 12 * i:ifd + 14 + 12 * i]
            if len(entry) < 12:
                return 1
            tag, kind, _n = struct.unpack(order + "HHI", entry[:8])
            if tag == 0x0112:
                if kind == 3:
                    value = struct.unpack(order + "H", entry[8:10])[0]
                elif kind == 4:
                    value = struct.unpack(order + "I", entry[8:12])[0]
                else:
                    return 1
                return value if 1 <= value <= 8 else 1
    except (struct.error, IndexError):
        pass
    return 1


def jpeg_image(data: bytes) -> PdfImage:
    width, height, comps, precision, adobe, _orientation = jpeg_info(data)
    _check_size(width, height)
    if precision != 8:
        raise ImageError(f"{precision}-bit JPEG (PDF supports 8-bit JPEG only)")
    spaces = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}
    if comps not in spaces:
        raise ImageError(f"JPEG with {comps} colour components")
    d = _image_dict(width, height, Name(spaces[comps]), 8)
    d["Filter"] = Name("DCTDecode")
    if comps == 4 and adobe:
        d["Decode"] = [1, 0, 1, 0, 1, 0, 1, 0]
    return PdfImage(width, height, d, bytes(data))


# ----------------------------------------------------------------------------------
# PNG: parsing and unfiltering
# ----------------------------------------------------------------------------------

_VALID_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


@dataclass
class _Png:
    width: int
    height: int
    depth: int
    ctype: int
    interlace: int
    palette: bytes
    trns: Optional[bytes]
    idat: bytes

    @property
    def channels(self) -> int:
        return _CHANNELS[self.ctype]

    @property
    def bpp(self) -> int:
        """Bytes per complete pixel for the filters (at least 1)."""
        return max(1, self.channels * self.depth // 8)

    def rowlen(self, width: int) -> int:
        return (width * self.channels * self.depth + 7) // 8


def _parse_png(data: bytes) -> _Png:
    if data[:8] != PNG_SIGNATURE:
        raise ImageError("not a PNG")
    pos = 8
    n = len(data)
    ihdr = None
    palette = b""
    trns: Optional[bytes] = None
    idat: List[bytes] = []
    while pos + 8 <= n:
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr = body
        elif ctype == b"PLTE":
            palette = body[:len(body) // 3 * 3]
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
    if ihdr is None or len(ihdr) < 13:
        raise ImageError("PNG without a header")
    width, height, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr[:13])
    if ctype not in _VALID_DEPTHS or depth not in _VALID_DEPTHS[ctype]:
        raise ImageError(f"PNG with colour type {ctype} and bit depth {depth}")
    if comp != 0 or filt != 0 or interlace not in (0, 1):
        raise ImageError("PNG with an unknown compression, filter or interlace method")
    if ctype == 3 and not palette:
        raise ImageError("palette PNG without a palette")
    if not idat:
        raise ImageError("PNG without image data")
    _check_size(width, height)
    return _Png(width, height, depth, ctype, interlace, palette, trns, b"".join(idat))


def _expected_size(png: _Png) -> int:
    if not png.interlace:
        return png.height * (1 + png.rowlen(png.width))
    total = 0
    for x0, y0, dx, dy in _ADAM7:
        pw = (png.width - x0 + dx - 1) // dx if png.width > x0 else 0
        ph = (png.height - y0 + dy - 1) // dy if png.height > y0 else 0
        if pw and ph:
            total += ph * (1 + png.rowlen(pw))
    return total


def _idat_ok(png: _Png) -> bool:
    """The zlib stream is intact and holds exactly the expected rows, so passing it through
    is safe (inflated in 4 MB pieces that are counted, not kept)."""
    expected = _expected_size(png)
    d = zlib.decompressobj()
    total = 0
    pending = png.idat
    try:
        while pending:
            total += len(d.decompress(pending, 1 << 22))
            if total > expected:
                return False
            pending = d.unconsumed_tail
    except zlib.error:
        return False
    return total == expected and d.eof


def _inflate(png: _Png) -> bytes:
    expected = _expected_size(png)
    d = zlib.decompressobj()
    try:
        out = d.decompress(png.idat, expected + 1)
    except zlib.error:
        out = b""
        d = zlib.decompressobj()
        try:  # salvage what can be inflated from a damaged stream
            for i in range(0, len(png.idat), 65536):
                out += d.decompress(png.idat[i:i + 65536], expected + 1 - len(out))
                if len(out) > expected:
                    break
        except zlib.error:
            pass
    if len(out) < expected:
        out += bytes(expected - len(out))  # truncated image: the missing rows stay black
    return out[:expected]


_MASKS: Dict[int, Tuple[int, int]] = {}


def _masks(n: int) -> Tuple[int, int]:
    m = _MASKS.get(n)
    if m is None:
        m = (int.from_bytes(b"\x7f" * n, "big"), int.from_bytes(b"\x80" * n, "big"))
        if len(_MASKS) < 64:
            _MASKS[n] = m
    return m


def _add_rows(a: bytes, b: bytes) -> bytes:
    """Bytewise ``(a + b) mod 256`` as one big-integer operation (SWAR, no carries)."""
    n = len(a)
    if n == 0:
        return b""
    lo, hi = _masks(n)
    x = int.from_bytes(a, "big")
    y = int.from_bytes(b, "big")
    return (((x & lo) + (y & lo)) ^ ((x ^ y) & hi)).to_bytes(n, "big")


def _sub_row(row: bytes, bpp: int) -> bytes:
    """Undo the Sub filter: a running sum with stride ``bpp`` (log-step scan on big ints)."""
    n = len(row)
    if n == 0:
        return b""
    lo, hi = _masks(n)
    x = int.from_bytes(row, "big")
    shift = bpp
    while shift < n:
        y = x >> (8 * shift)
        x = ((x & lo) + (y & lo)) ^ ((x ^ y) & hi)
        shift *= 2
    return x.to_bytes(n, "big")


def _avg_row(row: bytes, prev: bytes, bpp: int) -> bytearray:
    cur = bytearray(row)
    n = len(cur)
    for i in range(min(bpp, n)):
        cur[i] = (cur[i] + (prev[i] >> 1)) & 255
    for i in range(bpp, n):
        cur[i] = (cur[i] + ((cur[i - bpp] + prev[i]) >> 1)) & 255
    return cur


def _paeth_row(row: bytes, prev: bytes, bpp: int) -> bytearray:
    cur = bytearray(row)
    n = len(cur)
    for i in range(min(bpp, n)):
        cur[i] = (cur[i] + prev[i]) & 255
    for i in range(bpp, n):
        a = cur[i - bpp]
        b = prev[i]
        c = prev[i - bpp]
        pa = b - c
        pb = a - c
        pc = pa + pb
        if pa < 0:
            pa = -pa
        if pb < 0:
            pb = -pb
        if pc < 0:
            pc = -pc
        if pa <= pb and pa <= pc:
            p = a
        elif pb <= pc:
            p = b
        else:
            p = c
        cur[i] = (cur[i] + p) & 255
    return cur


def _unfilter(data: bytes, pos: int, rows: int, rowlen: int, bpp: int) -> Tuple[bytes, int]:
    """Unfilter ``rows`` rows starting at ``pos``; returns the raw rows and the next offset."""
    out: List[bytes] = []
    prev = bytes(rowlen)
    for _ in range(rows):
        ft = data[pos] if pos < len(data) else 0
        row = data[pos + 1:pos + 1 + rowlen]
        if len(row) < rowlen:
            row = row + bytes(rowlen - len(row))
        pos += 1 + rowlen
        if ft == 1:
            cur: bytes = _sub_row(row, bpp)
        elif ft == 2:
            cur = _add_rows(row, prev)
        elif ft == 3:
            cur = bytes(_avg_row(row, prev, bpp))
        elif ft == 4:
            cur = bytes(_paeth_row(row, prev, bpp))
        else:
            cur = bytes(row)
        out.append(cur)
        prev = cur
    return b"".join(out), pos


_UNPACK: Dict[int, List[bytes]] = {}


def _unpack(row: bytes, depth: int, count: int) -> bytes:
    """Sub-byte samples (1/2/4 bits) -> one byte per sample (raw values)."""
    if depth >= 8:
        return row
    tables = _UNPACK.get(depth)
    if tables is None:
        per = 8 // depth
        mask = (1 << depth) - 1
        tables = [bytes((b >> (8 - depth * (k + 1))) & mask for b in range(256)) for k in range(per)]
        _UNPACK[depth] = tables
    per = len(tables)
    out = bytearray(len(row) * per)
    for k in range(per):
        out[k::per] = row.translate(tables[k])
    return bytes(out[:count])


def _raw_pixels(png: _Png, data: bytes) -> bytes:
    """Unfiltered (and de-interlaced) rows without filter bytes.  Sub-byte samples are
    unpacked to one byte each (raw values); 8/16-bit samples keep their bytes."""
    w, h = png.width, png.height
    if not png.interlace:
        raw, _pos = _unfilter(data, 0, h, png.rowlen(w), png.bpp)
        if png.depth >= 8:
            return raw
        rl = png.rowlen(w)
        return b"".join(_unpack(raw[r * rl:(r + 1) * rl], png.depth, w) for r in range(h))
    ps = png.channels * png.depth // 8 if png.depth >= 8 else png.channels
    full = [bytearray(w * ps) for _ in range(h)]
    pos = 0
    for x0, y0, dx, dy in _ADAM7:
        pw = (w - x0 + dx - 1) // dx if w > x0 else 0
        ph = (h - y0 + dy - 1) // dy if h > y0 else 0
        if not pw or not ph:
            continue
        rl = png.rowlen(pw)
        rows, pos = _unfilter(data, pos, ph, rl, png.bpp)
        for j in range(ph):
            row = rows[j * rl:(j + 1) * rl]
            if png.depth < 8:
                row = _unpack(row, png.depth, pw)
            target = full[y0 + j * dy]
            step = dx * ps
            for k in range(ps):
                target[x0 * ps + k::step] = row[k::ps]
    return b"".join(full)


def decode_png(data: bytes) -> Tuple[int, int, int, int, bytes]:
    """``(width, height, channels, bits, samples)`` with palette expanded to RGB(A) and
    sub-byte grey scaled to 8 bits -- a reference decoder (the writer avoids it when it can)."""
    png = _parse_png(data)
    raw = _raw_pixels(png, _inflate(png))
    if png.ctype == 3:
        pal = png.palette
        entries = len(pal) // 3
        trns = png.trns or b""
        if png.trns is not None:
            lut = [bytes(pal[3 * i:3 * i + 3]) + bytes([trns[i] if i < len(trns) else 255]) if i < entries
                   else b"\0\0\0\xff" for i in range(256)]
            return png.width, png.height, 4, 8, b"".join(lut[i] for i in raw)
        lut3 = [bytes(pal[3 * i:3 * i + 3]) if i < entries else b"\0\0\0" for i in range(256)]
        return png.width, png.height, 3, 8, b"".join(lut3[i] for i in raw)
    if png.depth < 8:
        scale = 255 // ((1 << png.depth) - 1)
        raw = raw.translate(bytes(min(255, v * scale) for v in range(256)))
        return png.width, png.height, png.channels, 8, raw
    return png.width, png.height, png.channels, png.depth, raw


# ----------------------------------------------------------------------------------
# PNG -> PDF
# ----------------------------------------------------------------------------------


def _predictor(colors: int, bpc: int, columns: int) -> Dict[str, Any]:
    return {"Predictor": 15, "Colors": colors, "BitsPerComponent": bpc, "Columns": columns}


def _split_filtered(png: _Png, data: bytes) -> Tuple[bytes, bytes]:
    """Grey+alpha / RGBA filtered rows -> (colour rows, alpha rows), both still filtered."""
    w = png.width
    bytes_per_sample = png.depth // 8
    color_ch = png.channels - 1
    ps = png.channels * bytes_per_sample
    cs = color_ch * bytes_per_sample
    rl = w * ps
    color_rows: List[bytes] = []
    alpha_rows: List[bytes] = []
    for r in range(png.height):
        start = r * (rl + 1)
        ft = data[start:start + 1]
        row = data[start + 1:start + 1 + rl]
        color = bytearray(w * cs)
        alpha = bytearray(w * bytes_per_sample)
        for k in range(cs):
            color[k::cs] = row[k::ps]
        for k in range(bytes_per_sample):
            alpha[k::bytes_per_sample] = row[cs + k::ps]
        color_rows.append(ft + bytes(color))
        alpha_rows.append(ft + bytes(alpha))
    return b"".join(color_rows), b"".join(alpha_rows)


def _split_raw(raw: bytes, channels: int, bytes_per_sample: int) -> Tuple[bytes, bytes]:
    ps = channels * bytes_per_sample
    cs = (channels - 1) * bytes_per_sample
    n = len(raw) // ps
    color = bytearray(n * cs)
    alpha = bytearray(n * bytes_per_sample)
    for k in range(cs):
        color[k::cs] = raw[k::ps]
    for k in range(bytes_per_sample):
        alpha[k::bytes_per_sample] = raw[cs + k::ps]
    return bytes(color), bytes(alpha)


def png_image(data: bytes) -> PdfImage:
    png = _parse_png(data)
    w, h = png.width, png.height
    gray_or_rgb = Name("DeviceGray") if png.ctype in (0, 4) else Name("DeviceRGB")
    flate = Name("FlateDecode")

    # 1. passthrough: grey / RGB / palette without alpha channel, not interlaced, intact data
    if not png.interlace and (png.ctype in (0, 2) or (png.ctype == 3 and png.trns is None)) and _idat_ok(png):
        if png.ctype == 3:
            cs: Any = [Name("Indexed"), Name("DeviceRGB"), len(png.palette) // 3 - 1, png.palette]
        else:
            cs = gray_or_rgb
        d = _image_dict(w, h, cs, png.depth)
        d["Filter"] = flate
        d["DecodeParms"] = _predictor(png.channels, png.depth, w)
        if png.ctype in (0, 2) and png.trns is not None:
            key = _colour_key(png)
            if key is not None:
                d["Mask"] = key
        return PdfImage(w, h, d, png.idat)

    # 2. alpha channel, not interlaced: split the filtered rows
    if not png.interlace and png.ctype in (4, 6):
        inflated = _inflate(png)
        color, alpha = _split_filtered(png, inflated)
        d = _image_dict(w, h, gray_or_rgb, png.depth)
        d["Filter"] = flate
        d["DecodeParms"] = _predictor(png.channels - 1, png.depth, w)
        sd = _image_dict(w, h, Name("DeviceGray"), png.depth)
        sd["Filter"] = flate
        sd["DecodeParms"] = _predictor(1, png.depth, w)
        return PdfImage(w, h, d, zlib.compress(color, 6), (sd, zlib.compress(alpha, 6)))

    # 3. decode: palette with transparency, interlaced, or damaged data (salvaged, missing rows black)
    raw = _raw_pixels(png, _inflate(png))
    if png.ctype == 3:
        cs = [Name("Indexed"), Name("DeviceRGB"), len(png.palette) // 3 - 1, png.palette]
        d = _image_dict(w, h, cs, 8)
        d["Filter"] = flate
        image = PdfImage(w, h, d, zlib.compress(raw, 6))
        if png.trns is not None:
            trns = png.trns
            table = bytes(trns[i] if i < len(trns) else 255 for i in range(256))
            sd = _image_dict(w, h, Name("DeviceGray"), 8)
            sd["Filter"] = flate
            image.smask = (sd, zlib.compress(raw.translate(table), 6))
        return image
    if png.depth < 8:  # sub-byte grey (interlaced or salvaged): unpacked raw values, scaled to 8 bits
        scale = 255 // ((1 << png.depth) - 1)
        raw = raw.translate(bytes(min(255, v * scale) for v in range(256)))
        d = _image_dict(w, h, gray_or_rgb, 8)
        d["Filter"] = flate
        if png.trns is not None and len(png.trns) >= 2:
            v = struct.unpack(">H", png.trns[:2])[0] * scale
            d["Mask"] = [v, v]
        return PdfImage(w, h, d, zlib.compress(raw, 6))
    bps = png.depth // 8
    if png.ctype in (4, 6):
        color, alpha = _split_raw(raw, png.channels, bps)
        d = _image_dict(w, h, gray_or_rgb, png.depth)
        d["Filter"] = flate
        sd = _image_dict(w, h, Name("DeviceGray"), png.depth)
        sd["Filter"] = flate
        return PdfImage(w, h, d, zlib.compress(color, 6), (sd, zlib.compress(alpha, 6)))
    d = _image_dict(w, h, gray_or_rgb, png.depth)
    d["Filter"] = flate
    if png.trns is not None:
        key = _colour_key(png)
        if key is not None:
            d["Mask"] = key
    return PdfImage(w, h, d, zlib.compress(raw, 6))


def _colour_key(png: _Png) -> Optional[List[int]]:
    """``tRNS`` of a grey / RGB image as a ``/Mask`` colour-key array."""
    trns = png.trns or b""
    n = 1 if png.ctype == 0 else 3
    if len(trns) < 2 * n:
        return None
    limit = (1 << png.depth) - 1
    values = [min(limit, v) for v in struct.unpack(f">{n}H", trns[:2 * n])]
    out: List[int] = []
    for v in values:
        out += [v, v]
    return out
