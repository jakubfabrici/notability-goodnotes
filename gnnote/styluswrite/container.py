"""Tolerant gzip / zlib inflation for Stylus Labs Write's ``.svgz`` (``docs/styluswrite.md`` section 1).

Write's FAQ warns that some programs cannot open its ``.svgz`` files because they do not
implement all of gzip; which detail that is, is not known.  :func:`inflate` therefore reads the
container by hand and as leniently as a renderer would: the gzip header is skipped field by
field (``FEXTRA``, ``FNAME``, ``FCOMMENT``, ``FHCRC``), the body is inflated as raw deflate (so
zlib never rejects the stream over its trailer), and the CRC-32 and length in the trailer are
checked afterwards and only reported.  Concatenated gzip members are joined; bytes after the
last member are ignored, as zlib's ``gzread`` does.  The input is fed in small pieces, so damage
in the middle of the stream loses only the last piece before it: everything inflated up to
there is returned.  Output is bounded by ``limit``.

Written from RFC 1950 / RFC 1952 and Python's :mod:`zlib` documentation.
"""
from __future__ import annotations

import zlib
from typing import Optional, Tuple

__all__ = ["inflate", "is_zlib_header", "CHUNK"]

CHUNK = 4096  # compressed bytes fed to zlib at a time (damage loses at most one piece's output)

# gzip header flags (RFC 1952 section 2.3.1)
_FHCRC, _FEXTRA, _FNAME, _FCOMMENT = 2, 4, 8, 16


def is_zlib_header(data: bytes) -> bool:
    """A zlib stream header (RFC 1950): deflate method and a valid check value."""
    return len(data) >= 2 and data[0] & 0x0F == 8 and data[0] >> 4 <= 7 and (data[0] * 256 + data[1]) % 31 == 0


def _gzip_body(data: bytes, pos: int) -> Optional[int]:
    """Offset of the deflate data of the gzip member at ``pos``; ``None`` when its header is
    cut short or names another compression method."""
    if len(data) < pos + 10 or data[pos + 2] != 8:
        return None
    flags = data[pos + 3]
    i = pos + 10
    if flags & _FEXTRA:
        if len(data) < i + 2:
            return None
        i += 2 + int.from_bytes(data[i:i + 2], "little")
    for flag in (_FNAME, _FCOMMENT):
        if flags & flag:
            end = data.find(b"\0", i)
            if end < 0:
                return None
            i = end + 1
    if flags & _FHCRC:
        i += 2
    return i if i <= len(data) else None


def inflate(data: bytes, limit: int) -> Tuple[bytes, Optional[str]]:
    """Inflate a gzip (all members) or zlib stream, producing at most ``limit`` bytes.

    Returns ``(output, problem)``.  ``problem`` is ``None`` for a sound stream, ``"trailer"``
    when the data inflated completely but a checksum or length in a trailer is wrong or
    missing, ``"damaged"`` when the stream is cut short or corrupt (``output`` then holds what
    was inflated before the damage) and ``"too large"`` when the output would exceed ``limit``
    (``output`` holds the first ``limit`` bytes).
    """
    out = bytearray()
    pos = 0
    problem: Optional[str] = None
    gzip = data[:2] == b"\x1f\x8b"
    if not gzip and not is_zlib_header(data):
        return b"", "damaged"
    while True:
        if gzip:
            start = _gzip_body(data, pos)
        else:
            start = None if data[1] & 0x20 else 2  # a preset dictionary cannot be supplied
        if start is None:
            return bytes(out), "damaged"
        d = zlib.decompressobj(-15)
        check = 0 if gzip else 1  # running CRC-32 (gzip) or Adler-32 (zlib)
        size = 0
        i = start
        while not d.eof:
            if i >= len(data):  # the input ends inside the deflate data
                try:
                    out += d.flush()
                except zlib.error:
                    pass
                return bytes(out[:limit]), ("too large" if len(out) > limit else "damaged")
            piece = data[i:i + CHUNK]
            i += len(piece)
            try:
                chunk = d.decompress(piece, limit - len(out) + 1)
            except zlib.error:
                return bytes(out), "damaged"
            if len(out) + len(chunk) > limit or d.unconsumed_tail:
                out += chunk
                return bytes(out[:limit]), "too large"
            out += chunk
            size += len(chunk)
            check = zlib.crc32(chunk, check) if gzip else zlib.adler32(chunk, check)
        end = i - len(d.unused_data)  # the first byte after the deflate data
        if gzip:
            trailer = data[end:end + 8]
            if len(trailer) < 8 or int.from_bytes(trailer[:4], "little") != check \
                    or int.from_bytes(trailer[4:], "little") != size & 0xFFFFFFFF:
                problem = "trailer"
            pos = end + len(trailer)
            if data[pos:pos + 2] != b"\x1f\x8b":
                return bytes(out), problem  # whatever follows the last member is ignored
        else:
            if data[end:end + 4] != check.to_bytes(4, "big"):
                problem = "trailer"
            return bytes(out), problem
