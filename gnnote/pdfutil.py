"""Minimal PDF inspection and paper generation (standard library only).

The converter needs two things from PDF files: the *page geometry* of PDFs that are
carried as page backgrounds (count, size, rotation, producer) and the ability to
*generate* a one-page paper PDF for GoodNotes.  Nothing here renders or parses content
streams.

Byte layout that :func:`pdf_info` understands
---------------------------------------------

* ``%PDF-x.y`` header, body of ``N G obj ... endobj`` indirect objects, then one or more
  cross-reference sections and a ``startxref <offset>`` + ``%%EOF`` tail.  The *last*
  ``startxref`` names the newest section; every section may name an older one with
  ``/Prev`` (incremental updates); hybrid files add ``/XRefStm``.  Entries are merged
  newest-first: a section's entry (including a *free* entry, type ``f`` / 0) hides the
  same object number in older sections.
* Classic ``xref`` tables: ``xref`` then ``start count`` sub-sections of 20-byte entries
  ``oooooooooo ggggg n|f`` (19-byte entries with a lone LF are tolerated), then
  ``trailer << /Root .. /Info .. /Prev .. /XRefStm .. >>``.
* Cross-reference streams (PDF 1.5+): ``N G obj << /Type /XRef /W [a b c] /Index [..]
  /Size n /Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns k >> >> stream``.
  Each row holds ``a+b+c`` big-endian bytes: type (1 = in file at offset, 2 = inside
  object stream, 0 = free; default 1 when ``a`` is 0), field 2 and field 3.  PNG
  predictors 10..15 (row filter byte: None/Sub/Up/Average/Paeth) and the TIFF predictor
  2 are undone before the rows are read.
* Object streams: ``<< /Type /ObjStm /N n /First f >> stream``; the decoded data starts
  with ``n`` pairs ``objnum offset`` followed by the objects at ``f + offset``.
* Filters honoured for xref / object streams: FlateDecode, LZWDecode, ASCIIHexDecode,
  ASCII85Decode, RunLengthDecode (abbreviations included).  Stream data is taken from
  ``/Length`` (direct or indirect) and verified against ``endstream``; when the length is
  wrong the ``endstream`` keyword is searched instead.
* The page tree: ``/Root -> /Pages`` with ``/Kids`` recursion; ``/MediaBox`` and
  ``/Rotate`` are inheritable, every array element may be an indirect reference, the
  MediaBox may have a non-zero origin or reversed corners (width = ``|x2 - x1|``), and
  ``/Rotate`` 90 / 270 swaps width and height in the reported page.  A page without a
  usable MediaBox is reported as US Letter and a warning is recorded.
* Fallback when the cross-reference data is missing, damaged or points at the wrong
  objects: the whole file is scanned for ``N G obj`` headers (the last occurrence of an
  object number wins, which is what an incremental update means) and for ``/Type /ObjStm``
  streams whose members are expanded.  The page tree is then walked from the newest
  ``trailer`` / xref-stream ``/Root`` that resolves, else from a parentless ``/Type /Pages``
  node, else every ``/Type /Page`` object in object-number order with attributes inherited
  through its ``/Parent`` chain.

Strings (``/Producer``, ``/Creator``) are decoded from UTF-16 when they carry a byte order
mark, otherwise as PDFDocEncoding (approximated by Latin-1).  Encrypted files are read
structurally (page sizes come from unencrypted numbers) but their strings are not
decrypted.

:func:`make_paper_pdf` writes a PDF 1.4 with five objects (catalog, pages, page,
FlateDecode content stream, info dictionary), a classic xref table with exact byte offsets
and ``/Producer (gnnote)``.  The content stream fills the page with the paper colour and
draws the ruling (0.5 pt high filled rectangles every ``pitch`` points from the top edge,
vertical rules for ``grid``, 0.75 pt radius filled circles at the intersections for
``dotted``) in the light grey GoodNotes uses.
"""
from __future__ import annotations

import base64
import bisect
import re
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

__all__ = ["PdfPage", "PdfInfo", "pdf_info", "make_paper_pdf", "PAPER_STYLES"]


# ----------------------------------------------------------------------------------
# Public data types
# ----------------------------------------------------------------------------------


@dataclass
class PdfPage:
    width: float  # pt, already rotated into display orientation
    height: float
    rotation: int = 0  # 0 / 90 / 180 / 270 (normalised /Rotate)


@dataclass
class PdfInfo:
    pages: List[PdfPage]
    producer: str = ""
    creator: str = ""
    warnings: List[str] = field(default_factory=list)  # one line per tolerated defect

    @property
    def page_count(self) -> int:
        return len(self.pages)


PAPER_STYLES = ("plain", "lined", "grid", "dotted")
RULE_COLOR = (0.8157, 0.8235, 0.8275)  # GoodNotes' ruling grey
DEFAULT_PAGE = (612.0, 792.0)  # US Letter, the PDF default for a missing MediaBox

_MAX_PAGES = 100000
_MAX_DEPTH = 64


# ----------------------------------------------------------------------------------
# Object model of the parser
# ----------------------------------------------------------------------------------


class Name(str):
    """A PDF name object (``/Foo``); stored without the slash."""

    __slots__ = ()


class Ref(tuple):
    """An indirect reference ``num gen R``."""

    __slots__ = ()

    def __new__(cls, num: int, gen: int) -> "Ref":
        return tuple.__new__(cls, (num, gen))

    @property
    def num(self) -> int:
        return self[0]

    @property
    def gen(self) -> int:
        return self[1]


class Keyword(str):
    """A bare keyword token (``obj``, ``stream``, ``R`` ...)."""

    __slots__ = ()


class Stream:
    """A stream object: its dictionary and the *raw* (still encoded) data."""

    __slots__ = ("dict", "raw")

    def __init__(self, dictionary: Dict[str, Any], raw: bytes):
        self.dict = dictionary
        self.raw = raw


PdfObject = Union[None, bool, int, float, bytes, Name, Ref, list, dict, Stream, Keyword]


class PdfError(ValueError):
    """Raised internally when a structure cannot be parsed; never leaves pdf_info."""


# ----------------------------------------------------------------------------------
# Lexer / parser
# ----------------------------------------------------------------------------------

_WHITESPACE = b"\x00\t\n\x0c\r "
_DELIMITERS = b"()<>[]{}/%"
_REGULAR_RE = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]+")
_NAME_RE = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")
_NUMBER_RE = re.compile(rb"^[+-]?(?:\d+\.?\d*|\.\d+)$")
_HEX_RE = re.compile(rb"[0-9A-Fa-f\s]*>")
_NAME_ESCAPE_RE = re.compile(rb"#([0-9A-Fa-f]{2})")
_OBJ_HEADER_RE = re.compile(rb"(\d+)[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]+obj\b")
_OBJ_HEADER_TAIL_RE = re.compile(rb"(\d+)[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]+$")
_STREAM_EOL_RE = re.compile(rb"[ \t]*(?:\r\n|\n|\r)?")


class _Lexer:
    """Tokeniser over a bytes buffer.  Tokens: numbers, Name, bytes (strings), Keyword,
    and the delimiters ``<<`` ``>>`` ``[`` ``]`` ``{`` ``}`` as Keyword."""

    __slots__ = ("data", "pos", "end")

    def __init__(self, data: bytes, pos: int = 0, end: Optional[int] = None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def _skip_space(self) -> None:
        data, pos, end = self.data, self.pos, self.end
        while pos < end:
            c = data[pos]
            if c in _WHITESPACE:
                pos += 1
            elif c == 0x25:  # '%' comment
                nl = data.find(b"\n", pos)
                cr = data.find(b"\r", pos)
                cands = [x for x in (nl, cr) if x != -1]
                pos = min(cands) if cands else end
            else:
                break
        self.pos = pos

    def next(self) -> Optional[PdfObject]:
        """Return the next token or None at the end of the buffer."""
        data = self.data
        while True:
            self._skip_space()
            pos = self.pos
            if pos >= self.end:
                return None
            c = data[pos]
            if c == 0x29 or (c == 0x3E and data[pos + 1 : pos + 2] != b">"):
                self.pos = pos + 1  # stray ')' or '>' : ignore it
                continue
            break
        if c == 0x2F:  # '/'
            m = _NAME_RE.match(data, pos + 1, self.end)
            raw = m.group(0)
            self.pos = m.end()
            if b"#" in raw:
                raw = _NAME_ESCAPE_RE.sub(lambda mm: bytes([int(mm.group(1), 16)]), raw)
            return Name(raw.decode("latin-1"))
        if c == 0x28:  # '('
            return self._literal_string(pos + 1)
        if c == 0x3C:  # '<'
            if data[pos + 1 : pos + 2] == b"<":
                self.pos = pos + 2
                return Keyword("<<")
            return self._hex_string(pos + 1)
        if c == 0x3E:  # '>>' (a lone '>' was skipped above)
            self.pos = pos + 2
            return Keyword(">>")
        if c in b"[]{}":
            self.pos = pos + 1
            return Keyword(chr(c))
        m = _REGULAR_RE.match(data, pos, self.end)
        if m is None:  # cannot happen (c is a regular character) but stay safe
            self.pos = pos + 1
            return Keyword("")
        raw = m.group(0)
        self.pos = m.end()
        if _NUMBER_RE.match(raw):
            try:
                if b"." in raw:
                    return float(raw)
                return int(raw)
            except ValueError:
                pass
        if raw == b"true":
            return True
        if raw == b"false":
            return False
        if raw == b"null":
            return Keyword("null")
        return Keyword(raw.decode("latin-1"))

    def _literal_string(self, pos: int) -> bytes:
        data, end = self.data, self.end
        out = bytearray()
        depth = 1
        while pos < end:
            c = data[pos]
            if c == 0x5C:  # backslash
                pos += 1
                if pos >= end:
                    break
                e = data[pos]
                if e in b"01234567":
                    digits = bytes([e])
                    while len(digits) < 3 and pos + 1 < end and data[pos + 1] in b"01234567":
                        pos += 1
                        digits += bytes([data[pos]])
                    out.append(int(digits, 8) & 0xFF)
                elif e == 0x6E:
                    out.append(0x0A)
                elif e == 0x72:
                    out.append(0x0D)
                elif e == 0x74:
                    out.append(0x09)
                elif e == 0x62:
                    out.append(0x08)
                elif e == 0x66:
                    out.append(0x0C)
                elif e == 0x0D:  # line continuation (CR or CRLF)
                    if data[pos + 1 : pos + 2] == b"\n":
                        pos += 1
                elif e == 0x0A:
                    pass
                else:
                    out.append(e)
                pos += 1
            elif c == 0x28:
                depth += 1
                out.append(c)
                pos += 1
            elif c == 0x29:
                depth -= 1
                pos += 1
                if depth == 0:
                    break
                out.append(c)
            elif c == 0x0D:  # EOL normalisation
                out.append(0x0A)
                pos += 1
                if data[pos : pos + 1] == b"\n":
                    pos += 1
            else:
                out.append(c)
                pos += 1
        self.pos = pos
        return bytes(out)

    def _hex_string(self, pos: int) -> bytes:
        m = _HEX_RE.match(self.data, pos)
        if m is None:
            # unterminated: take what looks like hex and give up at the first other byte
            close = self.data.find(b">", pos)
            raw = self.data[pos : close if close != -1 else self.end]
            self.pos = (close + 1) if close != -1 else self.end
        else:
            raw = m.group(0)[:-1]
            self.pos = m.end()
        hexdigits = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
        if len(hexdigits) % 2:
            hexdigits += b"0"
        try:
            return bytes.fromhex(hexdigits.decode("ascii"))
        except ValueError:
            return b""


def _reduce_refs(items: List[Any]) -> None:
    """Replace trailing ``int int R`` triples in a token list by :class:`Ref` in place."""
    if (
        len(items) >= 3
        and isinstance(items[-1], Keyword)
        and items[-1] == "R"
        and isinstance(items[-2], int)
        and isinstance(items[-3], int)
        and not isinstance(items[-2], bool)
        and not isinstance(items[-3], bool)
    ):
        num, gen = items[-3], items[-2]
        del items[-3:]
        items.append(Ref(num, gen))


def _parse_value(lexer: _Lexer, token: Any, depth: int = 0) -> Any:
    """Turn ``token`` (already read) into a value, reading more for arrays / dicts."""
    if depth > 200:
        raise PdfError("nesting too deep")
    if isinstance(token, Keyword):
        if token == "<<":
            items: List[Any] = []
            while True:
                t = lexer.next()
                if t is None:
                    break
                if isinstance(t, Keyword):
                    if t == ">>":
                        break
                    if t == "R":
                        items.append(t)
                        _reduce_refs(items)
                        continue
                    if t in ("endobj", "stream", "endstream"):
                        lexer.pos -= len(t)  # unterminated dict: leave the keyword
                        break
                items.append(_parse_value(lexer, t, depth + 1))
            result: Dict[str, Any] = {}
            i = 0
            while i < len(items):
                key = items[i]
                if isinstance(key, Name):
                    if i + 1 < len(items):
                        value = items[i + 1]
                        result[str(key)] = None if isinstance(value, Keyword) and value == "null" else value
                        i += 2
                    else:
                        i += 1
                else:
                    i += 1  # junk where a key was expected
            return result
        if token == "[":
            arr: List[Any] = []
            while True:
                t = lexer.next()
                if t is None:
                    break
                if isinstance(t, Keyword):
                    if t == "]":
                        break
                    if t == "R":
                        arr.append(t)
                        _reduce_refs(arr)
                        continue
                    if t in (">>", "endobj", "stream", "endstream"):
                        if t != ">>":
                            lexer.pos -= len(t)
                        break
                    if t == "null":
                        arr.append(None)
                        continue
                arr.append(_parse_value(lexer, t, depth + 1))
            return arr
        if token == "null":
            return None
        return token  # other keywords are returned as-is
    return token


def _parse_object_sequence(lexer: _Lexer, stop: Sequence[str]) -> Tuple[List[Any], Optional[str]]:
    """Parse values until one of the ``stop`` keywords (or EOF).  Returns the values and
    the keyword that stopped the parse."""
    items: List[Any] = []
    while True:
        t = lexer.next()
        if t is None:
            return items, None
        if isinstance(t, Keyword):
            if t in stop:
                return items, str(t)
            if t == "R":
                items.append(t)
                _reduce_refs(items)
                continue
        items.append(_parse_value(lexer, t))


# ----------------------------------------------------------------------------------
# Stream filters
# ----------------------------------------------------------------------------------


def _flate(data: bytes) -> bytes:
    start = 0
    while start < len(data) and data[start] in _WHITESPACE:
        start += 1
    data = data[start:]
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    # damaged or truncated: salvage what can be inflated
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        d = zlib.decompressobj(wbits)
        out = bytearray()
        try:
            for i in range(0, len(data), 4096):
                out += d.decompress(data[i : i + 4096])
        except zlib.error:
            pass
        if out:
            return bytes(out)
    return b""


def _lzw(data: bytes, early: int = 1) -> bytes:
    out = bytearray()
    table: List[bytes] = []

    def reset() -> None:
        table[:] = [bytes([i]) for i in range(256)] + [b"", b""]

    reset()
    width = 9
    prev: Optional[bytes] = None
    bitbuf = 0
    nbits = 0
    for byte in data:
        bitbuf = (bitbuf << 8) | byte
        nbits += 8
        while nbits >= width:
            code = (bitbuf >> (nbits - width)) & ((1 << width) - 1)
            nbits -= width
            bitbuf &= (1 << nbits) - 1
            if code == 256:
                reset()
                width = 9
                prev = None
                continue
            if code == 257:
                return bytes(out)
            if prev is None:
                if code >= len(table):
                    return bytes(out)
                entry = table[code]
            elif code < len(table):
                entry = table[code]
                table.append(prev + entry[:1])
            elif code == len(table):
                entry = prev + prev[:1]
                table.append(entry)
            else:
                return bytes(out)
            out += entry
            prev = entry
            # code width grows when the next code to assign (+1 with /EarlyChange 1)
            # reaches the current range
            if len(table) + early >= (1 << width) and width < 12:
                width += 1
    return bytes(out)


def _ascii_hex(data: bytes) -> bytes:
    end = data.find(b">")
    if end != -1:
        data = data[:end]
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", data)
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii"))


def _ascii85(data: bytes) -> bytes:
    data = re.sub(rb"\s", b"", data)
    if data.startswith(b"<~"):
        data = data[2:]
    end = data.find(b"~>")
    if end != -1:
        data = data[:end]
    try:
        return base64.a85decode(data)
    except ValueError:
        return b""


def _run_length(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        length = data[i]
        i += 1
        if length == 128:
            break
        if length < 128:
            out += data[i : i + length + 1]
            i += length + 1
        else:
            if i < n:
                out += bytes([data[i]]) * (257 - length)
            i += 1
    return bytes(out)


def _unpredict(data: bytes, params: Dict[str, Any], resolve) -> bytes:
    predictor = int(resolve(params.get("Predictor", 1)) or 1)
    if predictor <= 1:
        return data
    colors = int(resolve(params.get("Colors", 1)) or 1)
    bpc = int(resolve(params.get("BitsPerComponent", 8)) or 8)
    columns = int(resolve(params.get("Columns", 1)) or 1)
    bpp = max(1, (colors * bpc + 7) // 8)
    rowlen = (colors * bpc * columns + 7) // 8
    if predictor == 2:  # TIFF predictor
        if bpc != 8:
            return data
        out = bytearray(data)
        for r in range(0, len(out) - rowlen + 1, rowlen):
            for i in range(bpp, rowlen):
                out[r + i] = (out[r + i] + out[r + i - bpp]) & 0xFF
        return bytes(out)
    # PNG predictors: every row starts with a filter type byte
    out = bytearray()
    prev = bytearray(rowlen)
    pos = 0
    n = len(data)
    while pos + 1 <= n:
        ft = data[pos]
        row = bytearray(data[pos + 1 : pos + 1 + rowlen])
        pos += 1 + rowlen
        if len(row) < rowlen:
            if not row:
                break
            row += bytes(rowlen - len(row))
        if ft == 1:
            for i in range(bpp, rowlen):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(rowlen):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(rowlen):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(rowlen):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pred = a
                elif pb <= pc:
                    pred = b
                else:
                    pred = c
                row[i] = (row[i] + pred) & 0xFF
        out += row
        prev = row
    return bytes(out)


_FILTER_ALIASES = {
    "Fl": "FlateDecode",
    "LZW": "LZWDecode",
    "AHx": "ASCIIHexDecode",
    "A85": "ASCII85Decode",
    "RL": "RunLengthDecode",
}


def _decode_stream(stream: Stream, resolve) -> bytes:
    """Apply the stream's filters.  Unknown filters raise :class:`PdfError`."""
    filters = resolve(stream.dict.get("Filter"))
    if filters is None:
        return stream.raw
    if not isinstance(filters, list):
        filters = [filters]
    params = resolve(stream.dict.get("DecodeParms", stream.dict.get("DP")))
    if not isinstance(params, list):
        params = [params]
    data = stream.raw
    for i, f in enumerate(filters):
        f = resolve(f)
        if not isinstance(f, str):
            continue
        name = _FILTER_ALIASES.get(str(f), str(f))
        parm = resolve(params[i]) if i < len(params) else None
        parm = parm if isinstance(parm, dict) else {}
        if name == "FlateDecode":
            data = _flate(data)
        elif name == "LZWDecode":
            data = _lzw(data, int(resolve(parm.get("EarlyChange", 1)) or 0) if parm else 1)
        elif name == "ASCIIHexDecode":
            data = _ascii_hex(data)
        elif name == "ASCII85Decode":
            data = _ascii85(data)
        elif name == "RunLengthDecode":
            data = _run_length(data)
        elif name == "Crypt":
            continue
        else:
            raise PdfError(f"unsupported stream filter /{name}")
        if parm and name in ("FlateDecode", "LZWDecode"):
            data = _unpredict(data, parm, resolve)
    return data


# ----------------------------------------------------------------------------------
# Document: cross-reference data, object access, page tree
# ----------------------------------------------------------------------------------

_STARTXREF_RE = re.compile(rb"startxref\s+(\d+)")
_XREF_ENTRY_RE = re.compile(rb"[\x00\t\n\x0c\r ]*(\d{1,10})[ \t]+(\d{1,5})[ \t]+([nf])")
_XREF_SUBSECTION_RE = re.compile(rb"[\x00\t\n\x0c\r ]*(\d+)[ \t]+(\d+)[ \t]*(?:\r\n|\n|\r|(?=\d))")
_TRAILER_RE = re.compile(rb"trailer")
_OBJSTM_RE = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/ObjStm\b")
_XREF_TYPE_RE = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/XRef\b")
_PAGE_TYPE_RE = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/Page(?![a-zA-Z0-9])")
_PAGES_TYPE_RE = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/Pages(?![a-zA-Z0-9])")


class _Document:
    def __init__(self, data: bytes):
        self.data = data
        self.warnings: List[str] = []
        self.xref: Dict[int, Tuple[int, int, int]] = {}  # num -> (type, field2, field3)
        self.trailer: Dict[str, Any] = {}
        self.cache: Dict[int, Any] = {}
        self.objstm_cache: Dict[int, Optional[Tuple[Dict[int, int], bytes]]] = {}
        self.scan_map: Optional[Dict[int, Tuple[int, Optional[Tuple[int, int]]]]] = None
        self.scan_roots: Optional[List[Any]] = None
        self.xref_ok = False
        self._loading: set = set()
        # offsets in damaged files are sometimes relative to the %PDF header
        k = data.find(b"%PDF", 0, 1024)
        self.header_offset = k if k > 0 else 0

    # -- warnings -------------------------------------------------------------------

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # -- cross-reference parsing --------------------------------------------------

    def load_xref(self) -> None:
        data = self.data
        tail = data[-2048:]
        matches = list(_STARTXREF_RE.finditer(tail))
        if not matches:
            matches = list(_STARTXREF_RE.finditer(data))
        if not matches:
            self.warn("PDF has no startxref; objects located by scanning")
            return
        offset = int(matches[-1].group(1))
        seen_sections: set = set()
        queue: List[int] = [offset]
        if self.header_offset:
            queue.append(offset + self.header_offset)
        sections = 0
        while queue and sections < 512:
            off = queue.pop(0)
            if off in seen_sections or off < 0 or off >= len(data):
                continue
            seen_sections.add(off)
            sections += 1
            try:
                trailer = self._read_xref_section(off, queue)
            except PdfError as exc:
                self.warn(f"damaged cross-reference section at {off}: {exc}")
                continue
            if trailer is None:
                continue
            for key, value in trailer.items():
                self.trailer.setdefault(key, value)
            self.xref_ok = True
            prev = trailer.get("Prev")
            if isinstance(prev, (int, float)) and not isinstance(prev, bool):
                queue.append(int(prev))
        if not self.xref_ok:
            self.warn("PDF cross-reference data unusable; objects located by scanning")

    def _read_xref_section(self, offset: int, queue: List[int]) -> Optional[Dict[str, Any]]:
        data = self.data
        lexer = _Lexer(data, offset)
        lexer._skip_space()
        if data.startswith(b"xref", lexer.pos):
            return self._read_xref_table(lexer.pos + 4, queue)
        m = _OBJ_HEADER_RE.match(data, lexer.pos)
        if m is None:
            # tolerate an offset that is off by a few bytes
            window_start = max(0, offset - 64)
            window = data[window_start : offset + 64]
            k = window.find(b"xref")
            if k != -1 and not window.startswith(b"startxref", max(0, k - 5)):
                return self._read_xref_table(window_start + k + 4, queue)
            m2 = _OBJ_HEADER_RE.search(window)
            if m2 is None:
                raise PdfError("neither an xref table nor an xref stream")
            m = _OBJ_HEADER_RE.match(data, window_start + m2.start())
            if m is None:
                raise PdfError("neither an xref table nor an xref stream")
        num, obj = self._parse_indirect(m.start())
        if not isinstance(obj, Stream):
            raise PdfError("xref stream object is not a stream")
        return self._read_xref_stream(obj, merge=True)

    def _read_xref_table(self, pos: int, queue: List[int]) -> Dict[str, Any]:
        data = self.data
        section: Dict[int, Tuple[int, int, int]] = {}
        while True:
            lexer = _Lexer(data, pos)
            lexer._skip_space()
            pos = lexer.pos
            if data.startswith(b"trailer", pos):
                lexer.pos = pos + 7
                t = lexer.next()
                trailer = _parse_value(lexer, t) if t is not None else {}
                if not isinstance(trailer, dict):
                    trailer = {}
                break
            m = _XREF_SUBSECTION_RE.match(data, pos)
            if m is None:
                if not section:
                    raise PdfError("xref table without sub-sections")
                trailer = {}
                break
            start, count = int(m.group(1)), int(m.group(2))
            pos = m.end()
            if count > 50_000_000:
                raise PdfError("absurd xref sub-section size")
            for i in range(count):
                e = _XREF_ENTRY_RE.match(data, pos)
                if e is None:
                    break
                pos = e.end()
                num = start + i
                if num not in section:
                    if e.group(3) == b"n":
                        section[num] = (1, int(e.group(1)), int(e.group(2)))
                    else:
                        section[num] = (0, 0, 0)
        xrefstm = trailer.get("XRefStm")
        if isinstance(xrefstm, (int, float)) and not isinstance(xrefstm, bool):
            try:
                m = _OBJ_HEADER_RE.match(data, int(xrefstm))
                if m is None:
                    lex = _Lexer(data, int(xrefstm))
                    lex._skip_space()
                    m = _OBJ_HEADER_RE.match(data, lex.pos)
                if m is not None:
                    _num, obj = self._parse_indirect(m.start())
                    if isinstance(obj, Stream):
                        hidden = self._read_xref_stream(obj, merge=False)
                        for num, entry in hidden.items():
                            if num not in section or section[num][0] == 0:
                                section[num] = entry
            except PdfError as exc:
                self.warn(f"damaged hybrid xref stream: {exc}")
        for num, entry in section.items():
            self.xref.setdefault(num, entry)
        return trailer

    def _read_xref_stream(self, stream: Stream, merge: bool):
        d = stream.dict
        w = self.resolve(d.get("W"))
        if not isinstance(w, list) or not w:
            raise PdfError("xref stream without /W")
        widths = [min(8, max(0, int(self.resolve(x) or 0))) for x in w[:3]]
        size = self.resolve(d.get("Size"))
        index = self.resolve(d.get("Index"))
        if not isinstance(index, list) or len(index) < 2:
            index = [0, int(size) if isinstance(size, (int, float)) else 0]
        index = [int(self.resolve(x) or 0) for x in index]
        try:
            payload = _decode_stream(stream, self.resolve)
        except PdfError as exc:
            raise PdfError(f"cannot decode xref stream: {exc}")
        rowlen = sum(widths)
        if rowlen <= 0:
            raise PdfError("xref stream with empty rows")
        entries: Dict[int, Tuple[int, int, int]] = {}
        pos = 0
        for k in range(0, len(index) - 1, 2):
            start, count = index[k], index[k + 1]
            for i in range(count):
                if pos + rowlen > len(payload):
                    break
                fields = []
                for width in widths:
                    if width == 0:
                        fields.append(None)
                    else:
                        fields.append(int.from_bytes(payload[pos : pos + width], "big"))
                        pos += width
                etype = 1 if fields[0] is None else fields[0]
                f2 = fields[1] if len(fields) > 1 and fields[1] is not None else 0
                f3 = fields[2] if len(fields) > 2 and fields[2] is not None else 0
                num = start + i
                if num not in entries:
                    entries[num] = (etype, f2, f3)
        if merge:
            for num, entry in entries.items():
                self.xref.setdefault(num, entry)
            return {k: v for k, v in d.items() if k in ("Root", "Info", "Prev", "Size", "Encrypt", "ID", "XRefStm")}
        return entries

    # -- scanning fallback ----------------------------------------------------------

    def build_scan_map(self) -> Dict[int, Tuple[int, Optional[Tuple[int, int]]]]:
        if self.scan_map is not None:
            return self.scan_map
        data = self.data
        scan: Dict[int, Tuple[int, Optional[Tuple[int, int]]]] = {}
        offsets: List[int] = []
        # locate every "obj" keyword with bytes.find (fast in C) and check the "N G "
        # prefix with a short anchored regex; a plain regex over the whole file is slow on
        # binary data
        n = len(data)
        pos = 0
        while True:
            i = data.find(b"obj", pos)
            if i == -1:
                break
            pos = i + 3
            if pos < n and data[pos] not in _WHITESPACE and data[pos] not in _DELIMITERS:
                continue
            m = _OBJ_HEADER_TAIL_RE.search(data, max(0, i - 40), i)
            if m is None:
                continue
            start = m.start()
            if start > 0 and data[start - 1] not in _WHITESPACE and data[start - 1] not in _DELIMITERS:
                continue
            scan[int(m.group(1))] = (start, None)
            offsets.append(start)
        self.scan_map = scan
        # expand object streams found anywhere in the file
        stm_nums: List[Tuple[int, int]] = []
        for m in _OBJSTM_RE.finditer(data):
            k = bisect.bisect_right(offsets, m.start()) - 1
            if k < 0:
                continue
            off = offsets[k]
            hm = _OBJ_HEADER_RE.match(data, off)
            if hm is None:
                continue
            stm_nums.append((int(hm.group(1)), off))
        for stm_num, off in stm_nums:
            if scan.get(stm_num, (None, None))[0] != off:
                continue  # an older copy of a stream that was redefined later
            table = self._load_objstm(stm_num)
            if table is None:
                continue
            for member in table[0]:
                cur = scan.get(member)
                if cur is None or cur[0] < off:
                    scan[member] = (off, (stm_num, 0))
        # candidate trailer dictionaries (classic trailers and xref-stream dictionaries),
        # ordered by file position so the newest comes last
        positioned: List[Tuple[int, Dict[str, Any]]] = []
        for m in _TRAILER_RE.finditer(data):
            lexer = _Lexer(data, m.end())
            t = lexer.next()
            if t is None:
                continue
            try:
                tr = _parse_value(lexer, t)
            except PdfError:
                continue
            if isinstance(tr, dict):
                positioned.append((m.start(), tr))
        for m in _XREF_TYPE_RE.finditer(data):
            k = bisect.bisect_right(offsets, m.start()) - 1
            if k < 0:
                continue
            try:
                _num, obj = self._parse_indirect(offsets[k])
            except PdfError:
                continue
            d = obj.dict if isinstance(obj, Stream) else obj
            if isinstance(d, dict) and "Root" in d:
                positioned.append((offsets[k], d))
        positioned.sort(key=lambda x: x[0])
        self.scan_roots = [tr for _pos, tr in positioned]
        return scan

    # -- object access ----------------------------------------------------------------

    def _parse_indirect(self, offset: int) -> Tuple[int, Any]:
        """Parse ``num gen obj ... endobj`` at ``offset``; returns (num, value)."""
        data = self.data
        m = _OBJ_HEADER_RE.match(data, offset)
        if m is None:
            raise PdfError(f"no object header at {offset}")
        num = int(m.group(1))
        lexer = _Lexer(data, m.end())
        items, stop = _parse_object_sequence(lexer, ("endobj", "stream", "obj"))
        if stop == "obj":  # missing endobj: the trailing "N G" belong to the next header
            items = items[:-2]
        value = items[-1] if items else None
        if stop == "stream":
            if not isinstance(value, dict):
                value = {}
            raw = self._stream_data(value, lexer.pos)
            return num, Stream(value, raw)
        return num, value

    def _stream_data(self, d: Dict[str, Any], pos: int) -> bytes:
        data = self.data
        m = _STREAM_EOL_RE.match(data, pos)
        start = m.end() if m else pos
        length = d.get("Length")
        if isinstance(length, Ref):
            try:
                length = self.resolve(length)
            except PdfError:
                length = None
        end = -1
        if isinstance(length, (int, float)) and not isinstance(length, bool) and length >= 0:
            end = start + int(length)
            probe = data[end : end + 20]
            if end > len(data) or not re.match(rb"\s*endstream", probe):
                end = -1
        if end == -1:
            k = data.find(b"endstream", start)
            if k == -1:
                end = len(data)
            else:
                end = k
                # drop the EOL that precedes endstream
                if data[end - 2 : end] == b"\r\n":
                    end -= 2
                elif data[end - 1 : end] in (b"\n", b"\r"):
                    end -= 1
        return data[start:end]

    def get_object(self, num: int) -> Any:
        if num in self.cache:
            return self.cache[num]
        if num in self._loading:
            return None
        self._loading.add(num)
        try:
            value = self._load_object(num)
        finally:
            self._loading.discard(num)
        self.cache[num] = value
        return value

    def _load_object(self, num: int) -> Any:
        entry = self.xref.get(num) if self.xref_ok else None
        if entry is not None:
            etype, f2, f3 = entry
            if etype == 1:
                for off in (f2, f2 + self.header_offset) if self.header_offset else (f2,):
                    try:
                        got, value = self._parse_indirect(off)
                        if got == num:
                            return value
                    except PdfError:
                        pass
                self.warn("PDF cross-reference offsets are wrong; objects located by scanning")
            elif etype == 2:
                value = self._objstm_member(f2, num)
                if value is not _MISSING:
                    return value
                self.warn("PDF object stream entry unusable; objects located by scanning")
            else:
                return None  # free object
        scan = self.build_scan_map()
        hit = scan.get(num)
        if hit is None:
            return None
        offset, inside = hit
        if inside is None:
            try:
                got, value = self._parse_indirect(offset)
            except PdfError:
                return None
            return value if got == num else None
        value = self._objstm_member(inside[0], num)
        return None if value is _MISSING else value

    def _load_objstm(self, stm_num: int) -> Optional[Tuple[Dict[int, int], bytes]]:
        if stm_num in self.objstm_cache:
            return self.objstm_cache[stm_num]
        self.objstm_cache[stm_num] = None
        stream = self.get_object(stm_num)
        if not isinstance(stream, Stream):
            return None
        try:
            payload = _decode_stream(stream, self.resolve)
        except PdfError as exc:
            self.warn(f"cannot decode object stream {stm_num}: {exc}")
            return None
        n = self.resolve(stream.dict.get("N"))
        first = self.resolve(stream.dict.get("First"))
        if not isinstance(n, (int, float)) or not isinstance(first, (int, float)):
            return None
        lexer = _Lexer(payload, 0, int(first) if 0 < first <= len(payload) else len(payload))
        table: Dict[int, int] = {}
        for _ in range(int(n)):
            a = lexer.next()
            b = lexer.next()
            if not isinstance(a, int) or not isinstance(b, int):
                break
            table[a] = int(first) + b
        result = (table, payload)
        self.objstm_cache[stm_num] = result
        return result

    def _objstm_member(self, stm_num: int, num: int) -> Any:
        loaded = self._load_objstm(stm_num)
        if loaded is None:
            return _MISSING
        table, payload = loaded
        off = table.get(num)
        if off is None or off < 0 or off >= len(payload):
            return _MISSING
        lexer = _Lexer(payload, off)
        try:
            t = lexer.next()
            if t is None:
                return None
            return _parse_value(lexer, t)
        except PdfError:
            return _MISSING

    def resolve(self, value: Any, depth: int = 0) -> Any:
        while isinstance(value, Ref):
            if depth > 32:
                return None
            value = self.get_object(value.num)
            depth += 1
        if isinstance(value, Keyword):
            return None
        return value

    # -- page tree ------------------------------------------------------------------

    def _number(self, value: Any) -> Optional[float]:
        value = self.resolve(value)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def _mediabox(self, value: Any) -> Optional[Tuple[float, float]]:
        box = self.resolve(value)
        if not isinstance(box, list) or len(box) < 4:
            return None
        nums = [self._number(x) for x in box[:4]]
        if any(n is None for n in nums):
            return None
        x1, y1, x2, y2 = nums  # type: ignore[misc]
        w, h = abs(x2 - x1), abs(y2 - y1)
        if not (w > 0 and h > 0) or w > 1e7 or h > 1e7:
            return None
        return (w, h)

    def _rotation(self, value: Any) -> int:
        r = self._number(value)
        if r is None:
            return 0
        r = int(round(r / 90.0)) * 90
        return r % 360

    def _page_from(self, attrs: Dict[str, Any]) -> PdfPage:
        box = attrs.get("MediaBox")
        if box is None:
            self.warn("PDF page without a usable /MediaBox; US Letter assumed")
            box = DEFAULT_PAGE
        rotation = attrs.get("Rotate", 0)
        w, h = box
        if rotation in (90, 270):
            w, h = h, w
        return PdfPage(w, h, rotation)

    def _node_attrs(self, node: Dict[str, Any], inherited: Dict[str, Any]) -> Dict[str, Any]:
        attrs = dict(inherited)
        if "MediaBox" in node:
            box = self._mediabox(node["MediaBox"])
            if box is not None:
                attrs["MediaBox"] = box
        if "Rotate" in node:
            attrs["Rotate"] = self._rotation(node["Rotate"])
        return attrs

    def walk_pages(self, root: Any) -> List[PdfPage]:
        pages: List[PdfPage] = []
        visited: set = set()
        self._walk(root, {}, 0, visited, pages)
        return pages

    def _walk(self, node_ref: Any, inherited: Dict[str, Any], depth: int, visited: set, out: List[PdfPage]) -> None:
        if len(out) >= _MAX_PAGES or depth > _MAX_DEPTH:
            return
        if isinstance(node_ref, Ref):
            if node_ref.num in visited:
                return
            visited.add(node_ref.num)
        node = self.resolve(node_ref)
        if not isinstance(node, dict):
            return
        attrs = self._node_attrs(node, inherited)
        ntype = self.resolve(node.get("Type"))
        kids = self.resolve(node.get("Kids"))
        if isinstance(kids, list) and ntype != "Page":
            for kid in kids:
                self._walk(kid, attrs, depth + 1, visited, out)
            return
        if ntype == "Page" or (ntype != "Pages" and ("Contents" in node or "MediaBox" in node)):
            out.append(self._page_from(attrs))

    def _inherit_up(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """Attributes for a leaf found by scanning: walk its /Parent chain upwards."""
        chain: List[Dict[str, Any]] = [node]
        seen: set = set()
        cur = node
        for _ in range(_MAX_DEPTH):
            parent_ref = cur.get("Parent")
            if isinstance(parent_ref, Ref):
                if parent_ref.num in seen:
                    break
                seen.add(parent_ref.num)
            parent = self.resolve(parent_ref)
            if not isinstance(parent, dict):
                break
            chain.append(parent)
            cur = parent
        attrs: Dict[str, Any] = {}
        for n in reversed(chain):
            attrs = self._node_attrs(n, attrs)
        return attrs

    def pages_by_scan(self) -> List[PdfPage]:
        """The regex fallback: every /Type /Page object in the file, tree order if possible."""
        scan = self.build_scan_map()
        data = self.data
        page_nums: List[int] = []
        pages_nums: List[int] = []
        # top-level objects: classify by the dictionary text between header and body
        offsets = sorted(off for off, inside in scan.values() if inside is None)
        for m in _PAGE_TYPE_RE.finditer(data):
            k = bisect.bisect_right(offsets, m.start()) - 1
            if k >= 0:
                hm = _OBJ_HEADER_RE.match(data, offsets[k])
                if hm is not None:
                    num = int(hm.group(1))
                    if scan.get(num, (None, None))[0] == offsets[k] and scan[num][1] is None:
                        page_nums.append(num)
        for m in _PAGES_TYPE_RE.finditer(data):
            k = bisect.bisect_right(offsets, m.start()) - 1
            if k >= 0:
                hm = _OBJ_HEADER_RE.match(data, offsets[k])
                if hm is not None:
                    pages_nums.append(int(hm.group(1)))
        # members of object streams
        for num, (_off, inside) in scan.items():
            if inside is None:
                continue
            obj = self.get_object(num)
            if isinstance(obj, dict):
                t = obj.get("Type")
                if t == "Page":
                    page_nums.append(num)
                elif t == "Pages":
                    pages_nums.append(num)
        page_nums = sorted(set(page_nums))
        pages_nums = sorted(set(pages_nums))
        # verify candidates (regex may hit text inside strings / streams)
        leaves: List[int] = []
        for num in page_nums:
            obj = self.get_object(num)
            if isinstance(obj, dict) and obj.get("Type") == "Page":
                leaves.append(num)
        if not leaves:
            return []
        ordered: List[PdfPage] = []
        covered: set = set()
        # prefer a parentless /Pages node that reaches leaves
        roots: List[int] = []
        for num in pages_nums:
            obj = self.get_object(num)
            if isinstance(obj, dict) and obj.get("Type") == "Pages" and not isinstance(obj.get("Parent"), Ref):
                roots.append(num)
        if not roots:
            # nodes whose parent does not resolve to a dictionary
            for num in pages_nums:
                obj = self.get_object(num)
                if isinstance(obj, dict) and obj.get("Type") == "Pages":
                    parent = self.resolve(obj.get("Parent"))
                    if not isinstance(parent, dict):
                        roots.append(num)
        for root_num in roots:
            visited: set = set()
            found: List[Tuple[int, PdfPage]] = []
            self._walk_collect(Ref(root_num, 0), {}, 0, visited, found)
            for num, page in found:
                if num not in covered:
                    covered.add(num)
                    ordered.append(page)
        for num in leaves:
            if num in covered:
                continue
            obj = self.get_object(num)
            ordered.append(self._page_from(self._inherit_up(obj)))
            covered.add(num)
        return ordered

    def _walk_collect(self, node_ref: Any, inherited: Dict[str, Any], depth: int, visited: set, out: List[Tuple[int, PdfPage]]) -> None:
        if len(out) >= _MAX_PAGES or depth > _MAX_DEPTH or not isinstance(node_ref, Ref):
            return
        if node_ref.num in visited:
            return
        visited.add(node_ref.num)
        node = self.resolve(node_ref)
        if not isinstance(node, dict):
            return
        attrs = self._node_attrs(node, inherited)
        ntype = self.resolve(node.get("Type"))
        kids = self.resolve(node.get("Kids"))
        if isinstance(kids, list) and ntype != "Page":
            for kid in kids:
                self._walk_collect(kid, attrs, depth + 1, visited, out)
            return
        if ntype == "Page":
            out.append((node_ref.num, self._page_from(attrs)))

    # -- document info ----------------------------------------------------------------

    def info_strings(self) -> Tuple[str, str]:
        info = None
        if self.xref_ok:
            if "Info" not in self.trailer:
                return "", ""  # a healthy file without an information dictionary
            info = self.resolve(self.trailer.get("Info"))
        if not isinstance(info, dict):
            self.build_scan_map()
            for tr in reversed(self.scan_roots or []):
                cand = self.resolve(tr.get("Info"))
                if isinstance(cand, dict):
                    info = cand
                    break
        if not isinstance(info, dict):
            # last resort: a dictionary with /Producer anywhere in the file
            m = None
            for m in re.finditer(rb"/Producer[\x00\t\n\x0c\r ]*[(<]", self.data):
                pass
            if m is not None:
                lexer = _Lexer(self.data, m.end() - 1)
                producer = lexer.next()
                creator = b""
                cm = None
                for cm in re.finditer(rb"/Creator[\x00\t\n\x0c\r ]*[(<]", self.data):
                    pass
                if cm is not None:
                    lexer = _Lexer(self.data, cm.end() - 1)
                    creator = lexer.next()
                return _text(producer), _text(creator)
            return "", ""
        return _text(self.resolve(info.get("Producer"))), _text(self.resolve(info.get("Creator")))


_MISSING = object()


def _text(value: Any) -> str:
    if isinstance(value, Name):
        return str(value)
    if not isinstance(value, bytes):
        return ""
    if value.startswith(b"\xfe\xff"):
        return value[2:].decode("utf-16-be", "replace").rstrip("\x00")
    if value.startswith(b"\xff\xfe"):
        return value[2:].decode("utf-16-le", "replace").rstrip("\x00")
    if value.startswith(b"\xef\xbb\xbf"):
        return value[3:].decode("utf-8", "replace")
    return value.decode("latin-1").rstrip("\x00")


# ----------------------------------------------------------------------------------
# pdf_info
# ----------------------------------------------------------------------------------


def pdf_info(data: bytes) -> PdfInfo:
    """Page sizes (pt, display orientation), rotations and the producer / creator strings.

    Never raises on a parseable-looking PDF; raises :class:`ValueError` only when no page
    can be located at all.  Tolerated defects are listed in ``PdfInfo.warnings``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("pdf_info expects bytes")
    data = bytes(data)
    doc = _Document(data)
    if b"%PDF" not in data[:1024]:
        doc.warn("PDF header missing")
    pages: List[PdfPage] = []
    try:
        doc.load_xref()
    except Exception as exc:  # noqa: BLE001 - tolerant reader
        doc.warn(f"PDF cross-reference parse failed: {exc}")
        doc.xref_ok = False
    if doc.xref_ok:
        try:
            if "Encrypt" in doc.trailer:
                doc.warn("PDF is encrypted; strings are not decrypted and compressed objects may be unreadable")
            root = doc.resolve(doc.trailer.get("Root"))
            if isinstance(root, dict):
                pages = doc.walk_pages(root.get("Pages"))
        except Exception as exc:  # noqa: BLE001
            doc.warn(f"PDF page tree walk failed: {exc}")
            pages = []
    if not pages:
        try:
            doc.build_scan_map()
            if doc.xref_ok:
                doc.warn("PDF page tree not reachable through the cross-reference data; pages located by scanning")
            for tr in reversed(doc.scan_roots or []):
                root = doc.resolve(tr.get("Root"))
                if isinstance(root, dict):
                    pages = doc.walk_pages(root.get("Pages"))
                    if pages:
                        break
            if not pages:
                pages = doc.pages_by_scan()
        except Exception as exc:  # noqa: BLE001
            doc.warn(f"PDF scan failed: {exc}")
            pages = []
    if not pages:
        raise ValueError("no PDF page found")
    try:
        producer, creator = doc.info_strings()
    except Exception:  # noqa: BLE001
        producer, creator = "", ""
    return PdfInfo(pages=pages, producer=producer, creator=creator, warnings=list(doc.warnings))


# ----------------------------------------------------------------------------------
# make_paper_pdf
# ----------------------------------------------------------------------------------


def _fmt(x: float) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    if s in ("", "-0"):
        s = "0"
    return s


def _circle(cx: float, cy: float, r: float) -> str:
    k = 0.5523 * r
    return (
        f"{_fmt(cx + r)} {_fmt(cy)} m "
        f"{_fmt(cx + r)} {_fmt(cy + k)} {_fmt(cx + k)} {_fmt(cy + r)} {_fmt(cx)} {_fmt(cy + r)} c "
        f"{_fmt(cx - k)} {_fmt(cy + r)} {_fmt(cx - r)} {_fmt(cy + k)} {_fmt(cx - r)} {_fmt(cy)} c "
        f"{_fmt(cx - r)} {_fmt(cy - k)} {_fmt(cx - k)} {_fmt(cy - r)} {_fmt(cx)} {_fmt(cy - r)} c "
        f"{_fmt(cx + k)} {_fmt(cy - r)} {_fmt(cx + r)} {_fmt(cy - k)} {_fmt(cx + r)} {_fmt(cy)} c h f\n"
    )


def make_paper_pdf(
    width: float,
    height: float,
    style: str = "plain",
    color: Tuple[float, float, float] = (1.0, 1.0, 1.0),
    pitch: float = 16.0,
) -> bytes:
    """A one-page PDF 1.4 of ``width`` x ``height`` pt filled with ``color``.

    ``style``: ``plain`` (fill only), ``lined`` (horizontal rules every ``pitch`` pt from
    the top), ``grid`` (horizontal + vertical rules), ``dotted`` (dots at the grid
    intersections).  Rules are 0.5 pt high in GoodNotes' light grey.
    """
    if style == "ruled":
        style = "lined"
    if style not in PAPER_STYLES:
        raise ValueError(f"unknown paper style {style!r}; expected one of {PAPER_STYLES}")
    try:
        width = float(width)
        height = float(height)
        pitch = float(pitch)
    except (TypeError, ValueError):
        raise ValueError("width, height and pitch must be numbers") from None
    if not (width > 0 and height > 0) or width != width or height != height:
        raise ValueError("page size must be positive")
    if len(color) < 3:
        raise ValueError("color must be an (r, g, b) triple")
    rgb = tuple(min(1.0, max(0.0, float(c))) for c in color[:3])
    w, h = width, height

    content = [
        f"q {_fmt(rgb[0])} {_fmt(rgb[1])} {_fmt(rgb[2])} rg 0 0 {_fmt(w)} {_fmt(h)} re f Q\n",
    ]
    if style != "plain":
        if not pitch > 0 or pitch != pitch:
            raise ValueError("pitch must be positive")
        if (w / pitch) * (h / pitch) > 250_000 or w / pitch > 5000 or h / pitch > 5000:
            raise ValueError("pitch too small for the page size")
        content.append(f"q {_fmt(RULE_COLOR[0])} {_fmt(RULE_COLOR[1])} {_fmt(RULE_COLOR[2])} rg\n")
        ys: List[float] = []
        y = pitch
        while y < h - 1e-6:
            ys.append(h - y)  # top-left origin -> PDF user space
            y += pitch
        xs: List[float] = []
        x = pitch
        while x < w - 1e-6:
            xs.append(x)
            x += pitch
        if style in ("lined", "grid"):
            for yy in ys:
                content.append(f"0 {_fmt(yy - 0.25)} {_fmt(w)} 0.5 re f\n")
        if style == "grid":
            for xx in xs:
                content.append(f"{_fmt(xx - 0.25)} 0 0.5 {_fmt(h)} re f\n")
        if style == "dotted":
            for yy in ys:
                for xx in xs:
                    content.append(_circle(xx, yy, 0.75))
        content.append("Q\n")
    stream = zlib.compress("".join(content).encode("ascii"), 9)

    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_fmt(w)} {_fmt(h)}] "
            f"/Resources << /ProcSet [/PDF] >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length " + str(len(stream)).encode("ascii") + b" /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream",
        b"<< /Producer (gnnote) /Creator (gnnote) >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: List[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 5 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)
