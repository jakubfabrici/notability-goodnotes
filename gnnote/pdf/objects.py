"""PDF object layer: read existing files, serialise objects, write new files and updates.

Reading reuses the tolerant parser of :mod:`gnnote.pdfutil` (classic and stream
cross-reference sections, object streams, the scanning fallback for damaged files, the
decompression limits); :class:`PdfFile` adds what the PDF codec needs on top of it:

* the page list with every page's *object number* and its inherited ``/MediaBox`` (all four
  corners, not only the size), ``/CropBox``, ``/Rotate`` and ``/Resources``.  The walk
  mirrors :func:`gnnote.pdfutil.pdf_info` step for step (page tree from the trailer's
  ``/Root``, then from the newest scanned trailer, then every ``/Type /Page`` object found
  by scanning), so page *n* here is page *n* there, damaged files included;
* where the newest cross-reference section starts and whether it is a table or a stream,
  which an incremental update needs.

Writing:

* :func:`serialize` turns Python values into PDF syntax: ``None`` -> ``null``, ``bool``,
  ``int``, ``float`` (fixed point, never an exponent), :class:`Name` (``/A#20B`` escaping of
  delimiters, whitespace, ``#`` and bytes outside 0x21..0x7E), ``bytes`` (a literal string
  when it is printable ASCII, else a hex string), ``list``, ``dict`` (keys are names),
  :class:`Ref` (``n g R``) and :class:`Stream` (dictionary with the exact ``/Length``,
  ``stream`` EOL, raw data, EOL ``endstream``).
* :class:`PdfWriter` allocates object numbers and writes a complete file: ``%PDF-1.7``, a
  binary comment line, ``n 0 obj ... endobj`` bodies in number order, one classic ``xref``
  table of 20-byte entries with exact offsets, ``trailer`` (``/Size /Root /Info /ID``),
  ``startxref`` and ``%%EOF``.
* :class:`Copier` copies an object graph from a :class:`PdfFile` into a :class:`PdfWriter`,
  renumbering every indirect object once (a memo keyed by the source object number), so
  resources shared by several imported pages are written once.  Indirect objects are copied
  from a work list, never by recursion, so long ``/Next`` chains cannot exhaust the stack.
* :func:`incremental_update` appends replacement objects to an existing file followed by a
  new cross-reference section of the same kind as the newest one (a table, or an
  ``/Type /XRef`` stream with ``/W [1 4 2]``) whose trailer repeats ``/Root``, ``/Info`` and
  ``/ID`` and names the previous section with ``/Prev`` (PDF 32000-1 section 7.5.6).
  :func:`rewrite` is the fallback for files whose cross-reference data is unusable: it
  copies the catalog and every page into a fresh file with a flat page tree (inherited
  page attributes made explicit).
"""
from __future__ import annotations

import hashlib
import math
import zlib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..pdfutil import (  # the tolerant parser this module builds on
    _OBJ_HEADER_RE,
    _PAGE_TYPE_RE,
    _PAGES_TYPE_RE,
    _STARTXREF_RE,
    MAX_STREAM_BYTES,
    Keyword,
    Name,
    PdfError,
    Ref,
    Stream,
    _decode_stream,
    _Document,
    _Lexer,
    _text,
)

__all__ = ["Name", "Ref", "Stream", "Raw", "PdfError", "PdfFile", "PageObj", "PdfWriter", "Copier",
           "serialize", "fmt_num", "incremental_update", "rewrite", "text_string", "decode_text",
           "MAX_STREAM_BYTES", "Matrix", "mat_mul", "mat_apply", "page_matrix"]


Matrix = Tuple[float, float, float, float, float, float]

_MAX_PAGES = 100000
_MAX_DEPTH = 64
DEFAULT_BOX = (0.0, 0.0, 612.0, 792.0)  # US Letter, as pdfutil assumes for a missing MediaBox


# ----------------------------------------------------------------------------------
# matrices (PDF row-vector convention: [x y 1] x M)
# ----------------------------------------------------------------------------------


def mat_mul(a: Sequence[float], b: Sequence[float]) -> Matrix:
    """``a`` then ``b`` (the matrix of applying ``a`` first)."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def mat_apply(m: Sequence[float], x: float, y: float) -> Tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def page_matrix(box: Sequence[float], rotate: int) -> Matrix:
    """Default user space of a page -> its displayed space (origin bottom-left, y up).

    ``box`` is the normalised MediaBox ``(x0, y0, x1, y1)``; ``rotate`` the page's
    ``/Rotate`` (0/90/180/270, clockwise).  The displayed page is ``w x h`` (``h x w`` for
    90/270) with the MediaBox corner moved to the origin.
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if rotate == 90:
        return (0.0, -1.0, 1.0, 0.0, -y0, w + x0)
    if rotate == 180:
        return (-1.0, 0.0, 0.0, -1.0, w + x0, h + y0)
    if rotate == 270:
        return (0.0, 1.0, -1.0, 0.0, h + y0, -x0)
    return (1.0, 0.0, 0.0, 1.0, -x0, -y0)


# ----------------------------------------------------------------------------------
# serialisation
# ----------------------------------------------------------------------------------

class Raw(bytes):
    """Bytes written verbatim by :func:`serialize` (pre-formatted PDF syntax, e.g. a long
    number array)."""

    __slots__ = ()


_NAME_REGULAR = frozenset(b"!\"$&'*+,-.0123456789:;=?@ABCDEFGHIJKLMNOPQRSTUVWXYZ\\^_`abcdefghijklmnopqrstuvwxyz|~")
_PRINTABLE = frozenset(range(0x20, 0x7F))


def fmt_num(x: float, digits: int = 6) -> str:
    """A PDF number: integers stay integers, reals are fixed point without an exponent."""
    if isinstance(x, bool):
        return "1" if x else "0"
    if isinstance(x, int):
        return str(x)
    if not math.isfinite(x):
        return "0"
    s = f"{x:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def _name(name: str) -> bytes:
    raw = name.encode("latin-1", "replace") if not isinstance(name, bytes) else name
    out = bytearray(b"/")
    for c in raw:
        if c in _NAME_REGULAR:
            out.append(c)
        else:
            out += b"#%02X" % c
    return bytes(out)


def _string(data: bytes) -> bytes:
    if all(c in _PRINTABLE for c in data):
        return b"(" + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"
    return b"<" + data.hex().upper().encode("ascii") + b">"


def serialize(value: Any) -> bytes:
    """PDF syntax for ``value`` (see the module docstring for the mapping)."""
    out: List[bytes] = []
    _ser(value, out, 0)
    return b"".join(out)


def _ser(value: Any, out: List[bytes], depth: int) -> None:
    if depth > 256:
        raise PdfError("object nesting too deep to serialise")
    if value is None:
        out.append(b"null")
    elif isinstance(value, bool):
        out.append(b"true" if value else b"false")
    elif isinstance(value, Raw):
        out.append(bytes(value))
    elif isinstance(value, Keyword):
        out.append(b"null")  # junk keywords from damaged files
    elif isinstance(value, Ref):
        out.append(b"%d %d R" % (value[0], value[1]))
    elif isinstance(value, str):  # Name (or a plain str used as a name)
        out.append(_name(value))
    elif isinstance(value, int):
        out.append(str(value).encode("ascii"))
    elif isinstance(value, float):
        out.append(fmt_num(value, 10).encode("ascii"))
    elif isinstance(value, (bytes, bytearray)):
        out.append(_string(bytes(value)))
    elif isinstance(value, dict):
        out.append(b"<<")
        for key, item in value.items():
            out.append(_name(key))
            out.append(b" ")
            _ser(item, out, depth + 1)
            out.append(b" ")
        out.append(b">>")
    elif isinstance(value, (list, tuple)):
        out.append(b"[")
        first = True
        for item in value:
            if not first:
                out.append(b" ")
            first = False
            _ser(item, out, depth + 1)
        out.append(b"]")
    elif isinstance(value, Stream):
        d = dict(value.dict)
        d["Length"] = len(value.raw)
        _ser(d, out, depth + 1)
        out.append(b"\nstream\n")
        out.append(bytes(value.raw))
        out.append(b"\nendstream")
    else:
        raise TypeError(f"cannot serialise {type(value).__name__} as a PDF object")


def text_string(text: str) -> bytes:
    """A PDF text string: ASCII as is, anything else UTF-16BE with a byte order mark."""
    if all(32 <= ord(ch) < 127 for ch in text):
        return text.encode("ascii")
    return b"\xfe\xff" + text.encode("utf-16-be", "replace")


# PDFDocEncoding (PDF 32000-1 Annex D.2) where it differs from Latin-1
_PDFDOC = {0x18: "˘", 0x19: "ˇ", 0x1A: "ˆ", 0x1B: "˙", 0x1C: "˝", 0x1D: "˛",
           0x1E: "˚", 0x1F: "˜", 0x80: "•", 0x81: "†", 0x82: "‡", 0x83: "…",
           0x84: "—", 0x85: "–", 0x86: "ƒ", 0x87: "⁄", 0x88: "‹", 0x89: "›",
           0x8A: "−", 0x8B: "‰", 0x8C: "„", 0x8D: "“", 0x8E: "”", 0x8F: "‘",
           0x90: "’", 0x91: "‚", 0x92: "™", 0x93: "ﬁ", 0x94: "ﬂ", 0x95: "Ł",
           0x96: "Œ", 0x97: "Š", 0x98: "Ÿ", 0x99: "Ž", 0x9A: "ı", 0x9B: "ł",
           0x9C: "œ", 0x9D: "š", 0x9E: "ž", 0xA0: "€"}


def decode_text(value: Any) -> str:
    """A text string from a PDF (UTF-16 with BOM, UTF-8 with BOM, else PDFDocEncoding)."""
    if isinstance(value, Name):
        return str(value)
    if not isinstance(value, (bytes, bytearray)):
        return ""
    raw = bytes(value)
    if raw.startswith((b"\xfe\xff", b"\xff\xfe", b"\xef\xbb\xbf")):
        return _text(raw)
    return "".join(_PDFDOC.get(c) or chr(c) for c in raw).rstrip("\x00")


def make_stream(dictionary: Dict[str, Any], data: bytes, compress: bool = True, level: int = 6) -> Stream:
    d = dict(dictionary)
    if compress:
        d["Filter"] = Name("FlateDecode")
        data = zlib.compress(data, level)
    return Stream(d, data)


# ----------------------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------------------


@dataclass
class PageObj:
    """One page of an existing PDF with its inherited attributes resolved."""

    ref: Optional[Ref]  # the page object's reference (None for a direct dictionary in /Kids)
    dict: Dict[str, Any]  # the page dictionary as parsed (never mutate: it is the parser's cache)
    box: Tuple[float, float, float, float]  # normalised MediaBox (x0, y0, x1, y1)
    rotate: int  # 0 / 90 / 180 / 270
    resources: Any  # the inherited /Resources value (a Ref, a dict or None)
    crop: Optional[Tuple[float, float, float, float]] = None
    has_box: bool = True  # False when the US Letter default was assumed

    @property
    def width(self) -> float:
        """Displayed width in pt (rotation applied), as :func:`pdfutil.pdf_info` reports it."""
        w, h = self.box[2] - self.box[0], self.box[3] - self.box[1]
        return h if self.rotate in (90, 270) else w

    @property
    def height(self) -> float:
        w, h = self.box[2] - self.box[0], self.box[3] - self.box[1]
        return w if self.rotate in (90, 270) else h

    @property
    def matrix(self) -> Matrix:
        """User space -> displayed space (origin bottom-left, y up)."""
        return page_matrix(self.box, self.rotate)


class PdfFile(_Document):
    """An existing PDF opened for reading (tolerant; see :mod:`gnnote.pdfutil`)."""

    def __init__(self, data: bytes):
        super().__init__(bytes(data))
        self.startxref: Optional[int] = None
        self.newest_is_stream = False
        self._pages: Optional[List[PageObj]] = None
        try:
            self.load_xref()
        except Exception as exc:  # noqa: BLE001 - tolerant reader, as pdf_info
            self.warn(f"PDF cross-reference parse failed: {exc}")
            self.xref_ok = False
        tail = self.data[-2048:]
        matches = list(_STARTXREF_RE.finditer(tail)) or list(_STARTXREF_RE.finditer(self.data))
        if matches:
            self.startxref = int(matches[-1].group(1))
            lexer = _Lexer(self.data, min(self.startxref, len(self.data)))
            lexer._skip_space()
            self.newest_is_stream = not self.data.startswith(b"xref", lexer.pos)

    # -- convenience ------------------------------------------------------------------

    @property
    def encrypted(self) -> bool:
        """An ``/Encrypt`` entry in the trailer (or, for a damaged file, in any trailer or
        cross-reference stream dictionary found by scanning)."""
        if "Encrypt" in self.trailer:
            return True
        if self.xref_ok:
            return False
        self.build_scan_map()
        return any(isinstance(tr, dict) and "Encrypt" in tr for tr in (self.scan_roots or []))

    def number(self, value: Any) -> Optional[float]:
        value = self.resolve(value)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            f = float(value)
        except OverflowError:  # an absurdly long integer literal
            return None
        return f if math.isfinite(f) else None

    def numbers(self, value: Any) -> List[float]:
        """A resolved array of numbers (non-numbers skipped); [] for anything else."""
        arr = self.resolve(value)
        if not isinstance(arr, list):
            return []
        out = []
        for item in arr:
            n = self.number(item)
            if n is not None:
                out.append(n)
        return out

    def dict_of(self, value: Any) -> Dict[str, Any]:
        value = self.resolve(value)
        if isinstance(value, Stream):
            return value.dict
        return value if isinstance(value, dict) else {}

    def stream_data(self, stream: Stream) -> bytes:
        """Decoded stream data (:class:`PdfError` for unsupported filters / bombs)."""
        return _decode_stream(stream, self.resolve)

    def info(self) -> Dict[str, Any]:
        return self.dict_of(self.trailer.get("Info")) if self.xref_ok else {}

    def root(self) -> Dict[str, Any]:
        return self.dict_of(self.trailer.get("Root")) if self.xref_ok else {}

    def max_object_number(self) -> int:
        """The highest object number in use (cross-reference entries and scanned objects)."""
        top = max(self.xref) if self.xref_ok and self.xref else 0
        if self.scan_map:
            top = max(top, max(self.scan_map))
        return top

    def healthy(self) -> bool:
        """True when the cross-reference data located every object without scanning, so an
        incremental update can chain to it."""
        if not self.xref_ok or self.startxref is None or self.header_offset:
            return False
        bad = ("damaged", "wrong", "scanning", "unusable", "parse failed", "no startxref")
        return not any(any(word in w for word in bad) for w in self.warnings)

    # -- page tree ----------------------------------------------------------------------

    def _box(self, value: Any) -> Optional[Tuple[float, float, float, float]]:
        arr = self.resolve(value)
        if not isinstance(arr, list) or len(arr) < 4:
            return None
        nums = [self.number(x) for x in arr[:4]]
        if any(n is None for n in nums):
            return None
        x1, y1, x2, y2 = nums  # type: ignore[misc]
        w, h = abs(x2 - x1), abs(y2 - y1)
        if not (w > 0 and h > 0) or w > 1e7 or h > 1e7:
            return None
        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

    def _attrs(self, node: Dict[str, Any], inherited: Dict[str, Any]) -> Dict[str, Any]:
        attrs = dict(inherited)
        if "MediaBox" in node:
            box = self._box(node["MediaBox"])
            if box is not None:
                attrs["MediaBox"] = box
        if "CropBox" in node:
            box = self._box(node["CropBox"])
            if box is not None:
                attrs["CropBox"] = box
        if "Rotate" in node:
            r = self.number(node["Rotate"])
            attrs["Rotate"] = 0 if r is None else (int(round(r / 90.0)) * 90) % 360
        if "Resources" in node and node["Resources"] is not None:
            attrs["Resources"] = node["Resources"]
        return attrs

    def _page_obj(self, ref: Optional[Ref], node: Dict[str, Any], attrs: Dict[str, Any]) -> PageObj:
        box = attrs.get("MediaBox")
        has_box = box is not None
        if box is None:
            self.warn("PDF page without a usable /MediaBox; US Letter assumed")
            box = DEFAULT_BOX
        return PageObj(ref=ref, dict=node, box=box, rotate=int(attrs.get("Rotate", 0)),
                       resources=attrs.get("Resources"), crop=attrs.get("CropBox"), has_box=has_box)

    def _walk(self, node_ref: Any, inherited: Dict[str, Any], depth: int, visited: set,
              out: List[PageObj]) -> None:
        if len(out) >= _MAX_PAGES or depth > _MAX_DEPTH:
            return
        if isinstance(node_ref, Ref):
            if node_ref.num in visited:
                return
            visited.add(node_ref.num)
        node = self.resolve(node_ref)
        if not isinstance(node, dict):
            return
        attrs = self._attrs(node, inherited)
        ntype = self.resolve(node.get("Type"))
        kids = self.resolve(node.get("Kids"))
        if isinstance(kids, list) and ntype != "Page":
            for kid in kids:
                self._walk(kid, attrs, depth + 1, visited, out)
            return
        if ntype == "Page" or (ntype != "Pages" and ("Contents" in node or "MediaBox" in node)):
            out.append(self._page_obj(node_ref if isinstance(node_ref, Ref) else None, node, attrs))

    def walk_from(self, root: Any) -> List[PageObj]:
        out: List[PageObj] = []
        self._walk(root, {}, 0, set(), out)
        return out

    def _inherit(self, node: Dict[str, Any]) -> Dict[str, Any]:
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
            attrs = self._attrs(n, attrs)
        return attrs

    def _pages_by_scan(self) -> List[PageObj]:
        """Port of ``pdfutil._Document.pages_by_scan`` that keeps object numbers."""
        import bisect

        scan = self.build_scan_map()
        data = self.data
        page_nums: List[int] = []
        pages_nums: List[int] = []
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
        leaves = [num for num in page_nums
                  if isinstance(self.get_object(num), dict) and self.get_object(num).get("Type") == "Page"]
        if not leaves:
            return []
        ordered: List[PageObj] = []
        covered: set = set()
        roots: List[int] = []
        for num in pages_nums:
            obj = self.get_object(num)
            if isinstance(obj, dict) and obj.get("Type") == "Pages" and not isinstance(obj.get("Parent"), Ref):
                roots.append(num)
        if not roots:
            for num in pages_nums:
                obj = self.get_object(num)
                if isinstance(obj, dict) and obj.get("Type") == "Pages":
                    if not isinstance(self.resolve(obj.get("Parent")), dict):
                        roots.append(num)
        for root_num in roots:
            found: List[PageObj] = []
            self._walk_collect(Ref(root_num, 0), {}, 0, set(), found)
            for page in found:
                if page.ref is not None and page.ref.num not in covered:
                    covered.add(page.ref.num)
                    ordered.append(page)
        for num in leaves:
            if num in covered:
                continue
            obj = self.get_object(num)
            gen = 0
            ordered.append(self._page_obj(Ref(num, gen), obj, self._inherit(obj)))
            covered.add(num)
        return ordered

    def _walk_collect(self, node_ref: Any, inherited: Dict[str, Any], depth: int, visited: set,
                      out: List[PageObj]) -> None:
        if len(out) >= _MAX_PAGES or depth > _MAX_DEPTH or not isinstance(node_ref, Ref):
            return
        if node_ref.num in visited:
            return
        visited.add(node_ref.num)
        node = self.resolve(node_ref)
        if not isinstance(node, dict):
            return
        attrs = self._attrs(node, inherited)
        ntype = self.resolve(node.get("Type"))
        kids = self.resolve(node.get("Kids"))
        if isinstance(kids, list) and ntype != "Page":
            for kid in kids:
                self._walk_collect(kid, attrs, depth + 1, visited, out)
            return
        if ntype == "Page":
            out.append(self._page_obj(node_ref, node, attrs))

    def pages(self) -> List[PageObj]:
        """Every page in document order (the same pages :func:`pdfutil.pdf_info` lists)."""
        if self._pages is not None:
            return self._pages
        pages: List[PageObj] = []
        if self.xref_ok:
            try:
                root = self.resolve(self.trailer.get("Root"))
                if isinstance(root, dict):
                    pages = self.walk_from(root.get("Pages"))
            except Exception as exc:  # noqa: BLE001
                self.warn(f"PDF page tree walk failed: {exc}")
                pages = []
        if not pages:
            try:
                self.build_scan_map()
                if self.xref_ok:
                    self.warn("PDF page tree not reachable through the cross-reference data; pages located by scanning")
                for tr in reversed(self.scan_roots or []):
                    root = self.resolve(tr.get("Root"))
                    if isinstance(root, dict):
                        pages = self.walk_from(root.get("Pages"))
                        if pages:
                            break
                if not pages:
                    pages = self._pages_by_scan()
            except Exception as exc:  # noqa: BLE001
                self.warn(f"PDF scan failed: {exc}")
                pages = []
        self._pages = pages
        return pages

    def object_gen(self, num: int) -> int:
        entry = self.xref.get(num) if self.xref_ok else None
        if entry is not None and entry[0] == 1:
            return int(entry[2])
        return 0

    def content_bytes(self, page: PageObj) -> bytes:
        """The page's content streams decoded and joined (``PdfError`` when one cannot be
        decoded)."""
        contents = self.resolve(page.dict.get("Contents"))
        streams: List[Stream] = []
        if isinstance(contents, Stream):
            streams = [contents]
        elif isinstance(contents, list):
            for item in contents[:10000]:
                item = self.resolve(item)
                if isinstance(item, Stream):
                    streams.append(item)
        parts: List[bytes] = []
        total = 0
        for stream in streams:
            data = self.stream_data(stream)
            total += len(data)
            if total > MAX_STREAM_BYTES:
                raise PdfError(f"page content larger than {MAX_STREAM_BYTES} bytes")
            parts.append(data)
        return b"\n".join(parts)


# ----------------------------------------------------------------------------------
# writing
# ----------------------------------------------------------------------------------


class PdfWriter:
    """Collects numbered objects and writes a complete PDF file."""

    def __init__(self) -> None:
        self._objects: List[Any] = [None]  # index = object number; slot 0 is the free head
        self._set: List[bool] = [True]

    def alloc(self) -> Ref:
        self._objects.append(None)
        self._set.append(False)
        return Ref(len(self._objects) - 1, 0)

    def set(self, ref: Ref, value: Any) -> Ref:
        self._objects[ref.num] = value
        self._set[ref.num] = True
        return ref

    def add(self, value: Any) -> Ref:
        return self.set(self.alloc(), value)

    def get(self, ref: Ref) -> Any:
        return self._objects[ref.num]

    def __len__(self) -> int:
        return len(self._objects) - 1

    def to_bytes(self, root: Ref, info: Optional[Ref] = None, version: str = "1.7") -> bytes:
        out = bytearray(f"%PDF-{version}\n%\xe2\xe3\xcf\xd3\n".encode("latin-1"))
        offsets: List[int] = [0]
        digest = hashlib.md5()
        for num in range(1, len(self._objects)):
            offsets.append(len(out))
            body = serialize(self._objects[num])  # unset slots serialise as null
            digest.update(body)
            out += b"%d 0 obj\n" % num
            out += body
            out += b"\nendobj\n"
        xref_pos = len(out)
        size = len(self._objects)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % size
        out += b"".join(b"%010d 00000 n \n" % off for off in offsets[1:])
        file_id = digest.hexdigest().upper().encode("ascii")
        trailer = b"<< /Size %d /Root %d %d R" % (size, root.num, root.gen)
        if info is not None:
            trailer += b" /Info %d %d R" % (info.num, info.gen)
        trailer += b" /ID [<" + file_id + b"> <" + file_id + b">] >>"
        out += b"trailer\n" + trailer + b"\nstartxref\n%d\n%%%%EOF\n" % xref_pos
        return bytes(out)


class Copier:
    """Copies objects of a :class:`PdfFile` into a :class:`PdfWriter`, renumbering them.

    ``memo`` maps source object numbers to the references they got in the writer; callers
    may pre-seed it (e.g. to redirect old page references to rewritten pages).
    ``overrides`` replaces the *content* of a source object by another value when it is
    copied (the value is converted like any other).
    """

    def __init__(self, src: PdfFile, dst: PdfWriter, max_objects: int = 2_000_000):
        self.src = src
        self.dst = dst
        self.memo: Dict[int, Ref] = {}
        self.overrides: Dict[int, Any] = {}
        self._queue: List[int] = []
        self._max = max_objects
        self.copied = 0

    def ref_for(self, num: int) -> Ref:
        ref = self.memo.get(num)
        if ref is None:
            ref = self.dst.alloc()
            self.memo[num] = ref
            self._queue.append(num)
        return ref

    def convert(self, value: Any, depth: int = 0) -> Any:
        """A direct value with every reference replaced by its copy's reference."""
        if depth > 200:
            return None
        if isinstance(value, Ref):
            return self.ref_for(value.num)
        if isinstance(value, Keyword):
            return None
        if isinstance(value, dict):
            return {k: self.convert(v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [self.convert(v, depth + 1) for v in value]
        if isinstance(value, Stream):
            d = {k: self.convert(v, depth + 1) for k, v in value.dict.items() if k != "Length"}
            return Stream(d, value.raw)
        return value

    def flush(self) -> None:
        """Copy every indirect object reached so far (and everything they reach)."""
        while self._queue:
            num = self._queue.pop()
            self.copied += 1
            if self.copied > self._max:
                raise PdfError("too many objects to copy")
            if num in self.overrides:
                value = self.overrides[num]
            else:
                value = self.src.get_object(num)
            self.dst.set(self.memo[num], self.convert(value))


# ----------------------------------------------------------------------------------
# incremental update / rewrite
# ----------------------------------------------------------------------------------


def incremental_update(pdf: PdfFile, changes: Dict[Ref, Any]) -> bytes:
    """``pdf``'s bytes followed by ``changes`` (object -> new value) and a new
    cross-reference section chained to the newest existing one.

    Only valid for :meth:`PdfFile.healthy` files; values may reference existing objects.
    """
    if not pdf.healthy():
        raise PdfError("the cross-reference data cannot be chained to")
    data = pdf.data
    out = bytearray(data)
    if not out.endswith((b"\n", b"\r")):
        out += b"\n"
    entries: Dict[int, Tuple[int, int]] = {}
    for ref in sorted(changes, key=lambda r: r.num):
        entries[ref.num] = (len(out), ref.gen)
        out += b"%d %d obj\n" % (ref.num, ref.gen)
        out += serialize(changes[ref])
        out += b"\nendobj\n"
    size_val = pdf.number(pdf.trailer.get("Size"))
    if size_val is None or not 0 < size_val <= 8_388_607:  # PDF's object number limit
        size_val = 0
    size = max(int(size_val), pdf.max_object_number() + 1, max(entries) + 1 if entries else 0)
    trailer: Dict[str, Any] = {}
    for key in ("Root", "Info", "ID"):
        if key in pdf.trailer and pdf.trailer[key] is not None:
            trailer[key] = pdf.trailer[key]
    prev = int(pdf.startxref or 0)
    if pdf.newest_is_stream:
        xref_num = size
        size += 1
        xref_pos = len(out)
        entries[xref_num] = (xref_pos, 0)
        rows = bytearray()
        index: List[int] = []
        width = max(4, (xref_pos.bit_length() + 7) // 8)
        for start, run in _runs(sorted(entries)):
            index += [start, len(run)]
            for num in run:
                off, gen = entries[num]
                rows += bytes([1]) + off.to_bytes(width, "big") + min(gen, 65535).to_bytes(2, "big")
        stream_dict: Dict[str, Any] = {"Type": Name("XRef"), "Size": size, "W": [1, width, 2], "Index": index,
                                       "Prev": prev}
        stream_dict.update(trailer)
        stream = make_stream(stream_dict, bytes(rows))
        out += b"%d 0 obj\n" % xref_num + serialize(stream) + b"\nendobj\n"
        out += b"startxref\n%d\n%%%%EOF\n" % xref_pos
        return bytes(out)
    xref_pos = len(out)
    out += b"xref\n"
    for start, run in _runs(sorted(entries)):
        out += b"%d %d\n" % (start, len(run))
        for num in run:
            off, gen = entries[num]
            out += b"%010d %05d n \n" % (off, min(gen, 65535))
    trailer_d: Dict[str, Any] = {"Size": size}
    trailer_d.update(trailer)
    trailer_d["Prev"] = prev
    out += b"trailer\n" + serialize(trailer_d) + b"\nstartxref\n%d\n%%%%EOF\n" % xref_pos
    return bytes(out)


def _runs(nums: Iterable[int]) -> List[Tuple[int, List[int]]]:
    runs: List[Tuple[int, List[int]]] = []
    for num in nums:
        if runs and runs[-1][1][-1] + 1 == num:
            runs[-1][1].append(num)
        else:
            runs.append((num, [num]))
    return runs


def rewrite(pdf: PdfFile, page_dicts: Optional[Dict[int, Dict[str, Any]]] = None) -> bytes:
    """A fresh file with ``pdf``'s catalog and pages; ``page_dicts`` maps a page index to a
    replacement page dictionary (source references inside it stay valid).

    The page tree becomes one flat ``/Pages`` node and every page gets its inherited
    ``/MediaBox``, ``/CropBox``, ``/Rotate`` and ``/Resources`` explicitly.
    """
    pages = pdf.pages()
    if not pages:
        raise PdfError("no page to rewrite")
    page_dicts = page_dicts or {}
    w = PdfWriter()
    copier = Copier(pdf, w)
    catalog_ref = w.alloc()
    pages_ref = w.alloc()
    new_refs: List[Ref] = []
    for page in pages:
        ref = w.alloc()
        new_refs.append(ref)
        if page.ref is not None and page.ref.num not in copier.memo:
            copier.memo[page.ref.num] = ref
    for index, page in enumerate(pages):
        src = page_dicts.get(index, page.dict)
        d = {k: v for k, v in src.items() if k not in ("Parent", "Type", "MediaBox", "CropBox", "Rotate", "Resources")}
        d = copier.convert(d)
        d["Type"] = Name("Page")
        d["Parent"] = pages_ref
        d["MediaBox"] = list(page.box)
        if page.crop is not None:
            d["CropBox"] = list(page.crop)
        if page.rotate:
            d["Rotate"] = page.rotate
        d["Resources"] = copier.convert(page.resources) if page.resources is not None else {}
        w.set(new_refs[index], d)
    w.set(pages_ref, {"Type": Name("Pages"), "Kids": list(new_refs), "Count": len(new_refs)})
    root = pdf.dict_of(pdf.trailer.get("Root")) if pdf.trailer.get("Root") is not None else {}
    if not root:
        for tr in reversed(pdf.scan_roots or []):
            root = pdf.dict_of(tr.get("Root"))
            if root:
                break
    catalog = copier.convert({k: v for k, v in root.items() if k not in ("Pages", "Type")})
    catalog["Type"] = Name("Catalog")
    catalog["Pages"] = pages_ref
    w.set(catalog_ref, catalog)
    info_dict = pdf.dict_of(pdf.trailer.get("Info")) if pdf.trailer.get("Info") is not None else {}
    info_ref = w.add(copier.convert(info_dict)) if info_dict else None
    copier.flush()
    return w.to_bytes(catalog_ref, info_ref)

