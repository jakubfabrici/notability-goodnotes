"""Microsoft OneNote -> :class:`gnnote.model.Document` (tolerant reader).

Input: one section file (``.one``, in either packaging: the desktop revision store or the
OneDrive "alternative packaging") or a ZIP of a notebook folder as onedrive.com and OneNote
for the web download it (``.one`` sections, ``.onetoc2`` tables of contents, section groups
as sub-folders, a ``OneNote_RecycleBin`` folder).  ``.onepkg`` packages (CAB archives) are
refused with a :class:`ValueError` that explains how to get a ZIP instead; so are
password-protected sections, whose content is never decoded.

Content model ([MS-ONE]): section -> page series -> one object space per page -> page
manifest -> page.  A page holds outlines (typed text, pictures, tables), pictures, embedded
files and ink containers directly, and its title in a title node.  Conflict pages hang off
the page manifest and version-history pages live in other contexts; neither is followed.

Mapping (``docs/onenote.md``):

* every page of every section becomes a :class:`~gnnote.model.Page`; a ZIP's sections are
  merged in table-of-contents order (file-name order where no ``.onetoc2`` says otherwise);
* ink keeps its strokes, colours, widths (pressure as an approximation) and highlighter
  flag; ink written inline in text paragraphs is laid out approximately;
* pictures (PNG / JPEG) become images; PDF printouts become their page pictures;
* outlines and titles become text boxes with runs; OneNote lays text out itself, so their
  positions and line breaks are approximate;
* a OneNote page is an unbounded canvas: the page grows to hold its content.

Everything that cannot be converted (tables, math, note tags, attachments, recordings,
other picture formats) is dropped with one warning per kind.
"""
from __future__ import annotations

import io
import math
import posixpath
import struct
import zipfile
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..model import Document, Image, Page, Stroke, TextBox, TextRun
from . import schema as S
from .common import (
    FILE_TYPE_ONE, FILE_TYPE_ONETOC2, FORMAT_NATIVE, FORMAT_PACKAGE, Encrypted, Obj, ObjectSpace,
    OneNoteError, Store, p_bool, p_bytes, p_f32, p_ref, p_refs, p_sets, p_text, p_u8, p_u16, p_u32,
    p_u32_array,
)
from .ink import InkBudget, InkTransform, ink_bounds, ink_data_strokes
from .native import NativeStore
from .package import PackageStore

__all__ = ["read_onenote", "open_store", "is_onenote", "ONEPKG_MESSAGE", "ENCRYPTED_MESSAGE"]

DEFAULT_PAGE_WIDTH = 754.0  # 20.944 half-inches: OneNote's usual page width
DEFAULT_PAGE_HEIGHT = 783.0  # 21.75 half-inches
PAGE_MARGIN = 36.0  # room kept around the content when a page grows to hold it
LARGE_PAGE_FACTOR = 3.0  # pages larger than this many default pages get a warning
TITLE_ORIGIN = (36.0, 18.0)  # where OneNote draws a title whose offsets are zero
DEFAULT_FONT_SIZE = 11.0  # OneNote's default body text: Calibri 11 pt
LINE_SPACING = 1.25  # line height / font size used to estimate text heights
CHAR_WIDTH = 0.5  # average character width / font size used to estimate line breaks
INDENT_PT = 18.0  # indentation per outline level
DEFAULT_OUTLINE_WIDTH = 6.0 * S.HALF_INCH_PT
MIN_IMAGE_PT = 1.0  # smallest picture side written (a zero-sized picture breaks other apps)
MAX_INLINE_PT = 2000.0  # largest inline item (handwritten word, ink space) laid out on a line
MAX_COORD = 1.0e6  # pt; content further away is treated as damage and skipped
MAX_DEPTH = 32  # nesting of outlines, outline elements and ink containers
MAX_PAGES = 10_000
MAX_SECTIONS = 1_000
MAX_IMAGE_BYTES = 512 * 1024 * 1024  # picture bytes one document may hold
MAX_MEMBER_BYTES = 256 * 1024 * 1024  # declared size above which a ZIP member is skipped
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # bytes one notebook ZIP may inflate to in total
RECYCLE_BIN = "onenote_recyclebin"
DELETED_PAGES = "onenote_deletedpages.one"

# .onetoc2 properties (not in the [MS-ONE] tables; the IDs real tables of contents use)
TOC_ENTRIES = 0x24001CF6
TOC_FILE_NAME = 0x1C001D6B
TOC_ORDERING_ID = 0x14001CB9
TOC_FILE_IDENTITY = 0x1C001D94

ONEPKG_MESSAGE = (
    "This is a OneNote package (.onepkg), which gnnote cannot read. Download the notebook from "
    "OneDrive instead: open onedrive.com, select the notebook's folder and choose Download; "
    "convert the .zip you get (or one .one section from it).")
ENCRYPTED_MESSAGE = (
    "This OneNote section is password protected, so its pages cannot be read. Remove the "
    "password in OneNote (or copy the pages into an unprotected section), download it again "
    "and convert that file.")
TOC_MESSAGE = (
    "This is a OneNote table of contents (.onetoc2), which holds no pages. Convert the .one "
    "section files of the notebook, or the whole notebook folder as a .zip.")
NOT_ONENOTE_MESSAGE = "not a OneNote file (no OneNote section header)"


# ----------------------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------------------


def is_onenote(data: bytes) -> bool:
    """``True`` for a ``.one`` / ``.onetoc2`` file in either packaging (header GUIDs)."""
    return (len(data) >= 64 and bytes(data[0:16]) in (FILE_TYPE_ONE, FILE_TYPE_ONETOC2)
            and bytes(data[48:64]) in (FORMAT_NATIVE, FORMAT_PACKAGE))


def is_onepkg(data: bytes) -> bool:
    """A CAB archive (``MSCF``) that names OneNote files: a ``.onepkg`` notebook package."""
    if not data.startswith(b"MSCF"):
        return False
    head = bytes(data[:65536]).lower()
    return b".one\x00" in head or b".onetoc2\x00" in head


def open_store(data: bytes) -> Store:
    """The revision store of a ``.one`` / ``.onetoc2`` file, whatever its packaging."""
    if len(data) < 64 or bytes(data[0:16]) not in (FILE_TYPE_ONE, FILE_TYPE_ONETOC2):
        raise OneNoteError(NOT_ONENOTE_MESSAGE)
    fmt = bytes(data[48:64])
    if fmt == FORMAT_NATIVE:
        return NativeStore(data)
    if fmt == FORMAT_PACKAGE:
        return PackageStore(data)
    raise OneNoteError(NOT_ONENOTE_MESSAGE)


# ----------------------------------------------------------------------------------
# Document-wide state
# ----------------------------------------------------------------------------------


class _Context:
    """Budgets and counters shared by every section of one document."""

    def __init__(self, doc: Document):
        self.doc = doc
        self.ink = InkBudget()
        self.image_bytes = MAX_IMAGE_BYTES
        self.counts: Counter = Counter()
        self.pages = 0

    def warn(self, message: str) -> None:
        self.doc.warn(message)

    def report(self) -> None:
        """One warning per kind of lossy step, with counts (called once at the end)."""
        c = self.counts
        msgs = [
            ("inline_ink", "{n} handwritten word(s) written inline in OneNote text were placed approximately"),
            ("text_boxes", "OneNote typed text became {n} text box(es); OneNote lays text out itself, so their "
                           "positions and line breaks are approximate"),
            ("pressure", "Pen pressure of {n} stroke(s) was turned into widths with an approximate curve"),
            ("tables", "{n} table(s) were dropped (tables are not converted)"),
            ("math", "{n} math equation(s) were dropped (math is not converted)"),
            ("note_tags", "{n} note tag(s) were dropped (to-do boxes, stars and other tags are not converted)"),
            ("attachments", "{n} attached file(s) were dropped (attachments are not converted)"),
            ("recordings", "{n} audio or video recording(s) were dropped"),
            ("printouts", "{n} PDF printout page(s) were converted as pictures; the PDF itself is not carried over"),
            ("bad_images", "{n} picture(s) in formats other than PNG or JPEG were dropped"),
            ("missing_images", "{n} picture(s) without readable picture data were dropped"),
            ("unreadable_ink", "{n} ink stroke(s) could not be decoded and were dropped"),
            ("conflict_pages", "{n} conflict page(s) (copies OneNote made when edits collided) were skipped"),
            ("deleted_pages", "{n} page(s) marked as deleted were skipped"),
            ("unreadable_pages", "{n} page(s) could not be read and were skipped"),
            ("far_content", "{n} object(s) placed absurdly far off the page were dropped"),
            ("unknown", "{n} OneNote object(s) of unknown kinds were dropped"),
        ]
        for key, template in msgs:
            if c[key]:
                self.warn(template.format(n=c[key]))
        if c["large_pages"]:
            self.warn(f"{c['large_pages']} page(s) are much larger than a OneNote page usually is; OneNote pages are "
                      "unbounded canvases and gnnote does not split them, so the other app may show them small")
        if self.ink.exhausted:
            self.warn("The notes hold more ink than gnnote reads in one go; the rest was dropped")
        if c["image_budget"]:
            self.warn(f"{c['image_budget']} picture(s) were dropped because the pictures exceed "
                      f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB in total")


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------


def _image_format(data: Optional[bytes]) -> Optional[str]:
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def _pixel_size(data: bytes, fmt: str) -> Optional[Tuple[int, int]]:
    """Pixel size from a PNG IHDR or a JPEG SOFn marker (no decoding)."""
    try:
        if fmt == "png" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if fmt == "jpeg":
            pos = 2
            while pos + 9 < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                marker = data[pos + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                    pos += 2 if marker != 0xFF else 1
                    continue
                length = int.from_bytes(data[pos + 2:pos + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return int.from_bytes(data[pos + 7:pos + 9], "big"), int.from_bytes(data[pos + 5:pos + 7], "big")
                if length < 2:
                    return None
                pos += 2 + length
    except (IndexError, ValueError):
        return None
    return None


def _colorref(value: Optional[int]) -> Optional[Tuple[float, float, float, float]]:
    """COLORREF 0x00BBGGRR -> RGBA; ``None`` for absent or "automatic" (0xFF000000)."""
    if value is None or value & 0xFF000000:
        return None
    return ((value & 0xFF) / 255.0, ((value >> 8) & 0xFF) / 255.0, ((value >> 16) & 0xFF) / 255.0, 1.0)


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) and abs(v) <= MAX_COORD for v in values)


def _half_inch(props: Dict[int, Any], pid: int) -> Optional[float]:
    v = p_f32(props, pid)
    return None if v is None else v * S.HALF_INCH_PT


def _positional_refs(props: Dict[int, Any], pid: int) -> List[Any]:
    """An ArrayOfObjectIDs keeping its null entries (they keep text runs aligned)."""
    v = props.get(pid)
    if isinstance(v, list):
        return [x if isinstance(x, tuple) else None for x in v]
    return []


def _has_prop_id(props: Dict[int, Any], prop_id: int) -> bool:
    return any((k & 0x3FFFFFF) == prop_id for k in props)


# ----------------------------------------------------------------------------------
# Text
# ----------------------------------------------------------------------------------


class _Style:
    """Character formatting of a run, falling back to the paragraph style."""

    __slots__ = ("bold", "italic", "underline", "font", "size", "color", "math", "hidden")

    def __init__(self, run: Dict[int, Any], para: Dict[int, Any]):
        def pick(fn: Any, pid: int) -> Any:
            v = fn(run, pid)
            return fn(para, pid) if v is None else v

        self.bold = p_bool(run, S.BOLD) or p_bool(para, S.BOLD)
        self.italic = p_bool(run, S.ITALIC) or p_bool(para, S.ITALIC)
        self.underline = p_bool(run, S.UNDERLINE) or p_bool(para, S.UNDERLINE)
        self.font = pick(p_text, S.FONT) or None
        half_points = pick(p_u16, S.FONT_SIZE)
        self.size = half_points / 2.0 if half_points and 2 <= half_points <= 2000 else None
        self.color = _colorref(pick(p_u32, S.FONT_COLOR))
        self.math = p_bool(run, S.MATH_FORMATTING)
        self.hidden = p_bool(run, S.HIDDEN)

    def run(self, text: str) -> TextRun:
        return TextRun(text=text, bold=self.bold, italic=self.italic, underline=self.underline,
                       font=self.font, size=self.size, color=self.color)


class _Para:
    """One paragraph of an outline: its runs and, for inline ink, its inline items."""

    __slots__ = ("indent", "prefix", "runs", "align", "size", "inline", "has_ink", "math")

    def __init__(self, indent: int):
        self.indent = indent
        self.prefix = ""
        self.runs: List[TextRun] = []
        self.align = 0
        self.size = DEFAULT_FONT_SIZE
        # ("text", width, size) | ("ink", oid, x0, y0, w, h) | ("container", obj) | ("space", w, h) | ("break",)
        self.inline: List[Tuple[Any, ...]] = []
        self.has_ink = False
        self.math = False

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    def line_height(self) -> float:
        return self.size * LINE_SPACING

    def height(self, width: float) -> float:
        avail = max(width - self.indent * INDENT_PT, self.size)
        per_line = max(1, int(avail / (self.size * CHAR_WIDTH)))
        text = self.prefix + self.text
        lines = sum(max(1, math.ceil(len(part) / per_line)) for part in text.split("\n"))
        return lines * self.line_height()


# ----------------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------------


class _PageReader:
    def __init__(self, ctx: _Context, space: ObjectSpace):
        self.ctx = ctx
        self.space = space
        self.page = Page(width=DEFAULT_PAGE_WIDTH, height=DEFAULT_PAGE_HEIGHT)
        self.pens: Dict[Any, Any] = {}
        self.ink_stats: Dict[str, int] = {}
        self.visited: set = set()

    def get(self, ref: Any) -> Optional[Obj]:
        return self.space.get(ref) if ref is not None else None

    # -------------------------------------------------------------- entry

    def read(self) -> Optional[Page]:
        root = self.space.root(S.ROLE_CONTENT)
        if root is None:
            return None
        node: Optional[Obj] = root
        if root.jcid == S.PAGE_MANIFEST_NODE:
            node = next((o for o in (self.get(r) for r in p_refs(root.props, S.CONTENT_CHILD_NODES))
                         if o is not None and o.jcid == S.PAGE_NODE), None)
        if node is None or node.jcid != S.PAGE_NODE:
            return None
        if p_bool(node.props, S.IS_CONFLICT_PAGE):
            self.ctx.counts["conflict_pages"] += 1
            return None
        meta = self.space.root(S.ROLE_METADATA)
        if meta is not None and meta.jcid == S.CONFLICT_PAGE_METADATA:
            self.ctx.counts["conflict_pages"] += 1
            return None
        if meta is not None and _deleted(meta.props):
            self.ctx.counts["deleted_pages"] += 1
            return None
        for ref in p_refs(node.props, S.ELEMENT_CHILD_NODES):
            self.element(ref)
        for ref in p_refs(node.props, S.STRUCTURE_ELEMENT_CHILD_NODES):
            title = self.get(ref)
            if title is not None and title.jcid == S.TITLE_NODE:
                self.title(title)
        self.finish(node.props)
        for key, count in self.ink_stats.items():
            self.ctx.counts["unreadable_ink" if key == "unreadable" else key] += count
        return self.page

    def element(self, ref: Any) -> None:
        obj = self.get(ref)
        if obj is None or ref in self.visited:
            return
        self.visited.add(ref)
        jcid = obj.jcid
        if jcid == S.OUTLINE_NODE:
            x = _half_inch(obj.props, S.OFFSET_FROM_PARENT_HORIZ) or 0.0
            y = _half_inch(obj.props, S.OFFSET_FROM_PARENT_VERT) or 0.0
            self.outline(obj, x, y)
        elif jcid == S.IMAGE_NODE:
            x = _half_inch(obj.props, S.OFFSET_FROM_PARENT_HORIZ) or 0.0
            y = _half_inch(obj.props, S.OFFSET_FROM_PARENT_VERT) or 0.0
            self.image(obj, x, y)
        elif jcid == S.INK_CONTAINER:
            self.ink_container(obj, None, 0)
        elif jcid == S.EMBEDDED_FILE_NODE:
            self.embedded_file(obj)
        else:
            self.ctx.counts["unknown"] += 1

    def title(self, title: Obj) -> None:
        x = TITLE_ORIGIN[0] + (_half_inch(title.props, S.OFFSET_FROM_PARENT_HORIZ) or 0.0)
        y = TITLE_ORIGIN[1] + (_half_inch(title.props, S.OFFSET_FROM_PARENT_VERT) or 0.0)
        for ref in p_refs(title.props, S.ELEMENT_CHILD_NODES):
            outline = self.get(ref)
            if outline is None or ref in self.visited or outline.jcid != S.OUTLINE_NODE:
                continue
            self.visited.add(ref)
            y = self.outline(outline, x, y)

    # -------------------------------------------------------------- ink

    def ink_container(self, obj: Obj, parent: Optional[Tuple[float, float, float, float]], depth: int) -> None:
        """Strokes of an InkContainer and of the containers nested in it.

        A nested container is placed by its own offsets and scaling (as one2html does);
        missing values are inherited from the enclosing container.
        """
        if depth > MAX_DEPTH:
            return
        props = obj.props
        px, py, psx, psy = parent if parent is not None else (0.0, 0.0, 1.0, 1.0)
        x = _half_inch(props, S.OFFSET_FROM_PARENT_HORIZ)
        y = _half_inch(props, S.OFFSET_FROM_PARENT_VERT)
        sx = p_f32(props, S.INK_SCALING_X)
        sy = p_f32(props, S.INK_SCALING_Y)
        place = (px if x is None else x, py if y is None else y,
                 psx if not sx or sx <= 0 else sx, psy if not sy or sy <= 0 else sy)
        data = p_ref(props, S.INK_DATA)
        if data is not None:
            self.add_strokes(ink_data_strokes(self.space, data, InkTransform(*place), self.ctx.ink,
                                              self.pens, self.ink_stats))
        for key in (S.CONTENT_CHILD_NODES, S.ELEMENT_CHILD_NODES):
            for ref in p_refs(props, key):
                child = self.get(ref)
                if child is not None and child.jcid == S.INK_CONTAINER and ref not in self.visited:
                    self.visited.add(ref)
                    self.ink_container(child, place, depth + 1)

    def add_strokes(self, strokes: Sequence[Stroke]) -> None:
        for stroke in strokes:
            x0, y0, x1, y1 = stroke.bbox()
            if not _finite(x0, y0, x1, y1):
                self.ctx.counts["far_content"] += 1
                continue
            self.page.strokes.append(stroke)

    # -------------------------------------------------------------- pictures, files

    def picture(self, obj: Obj) -> Optional[Tuple[bytes, str]]:
        data = self.space.file_data(p_ref(obj.props, S.PICTURE_CONTAINER_REF))
        fmt = _image_format(data)
        if fmt is None:
            web = self.space.file_data(p_ref(obj.props, S.WEB_PICTURE_CONTAINER))
            if _image_format(web) is not None:
                data, fmt = web, _image_format(web)
        if fmt is None or data is None:
            self.ctx.counts["bad_images" if data and len(data) > 16 else "missing_images"] += 1
            return None
        if len(data) > self.ctx.image_bytes:
            self.ctx.counts["image_budget"] += 1
            return None
        self.ctx.image_bytes -= len(data)
        return data, fmt

    def image_size(self, obj: Obj, data: bytes, fmt: str) -> Tuple[float, float]:
        props = obj.props
        w = _half_inch(props, S.PICTURE_WIDTH)
        h = _half_inch(props, S.PICTURE_HEIGHT)
        if not (w and h and w > 0 and h > 0):
            px = _pixel_size(data, fmt)
            if px and px[0] > 0 and px[1] > 0:
                w, h = px[0] * 0.75, px[1] * 0.75  # 96 dpi
            else:
                w, h = 144.0, 144.0
        max_w = _half_inch(props, S.LAYOUT_MAX_WIDTH)
        max_h = _half_inch(props, S.LAYOUT_MAX_HEIGHT)
        scale = 1.0
        if max_w and max_w > 0:
            scale = min(scale, max_w / w)
        if max_h and max_h > 0:
            scale = min(scale, max_h / h)
        return max(w * scale, MIN_IMAGE_PT), max(h * scale, MIN_IMAGE_PT)

    def image(self, obj: Obj, x: float, y: float) -> float:
        """Place a picture with its top-left at (x, y); returns its height (0 when dropped)."""
        if _has_prop_id(obj.props, S.NOTE_TAG_STATES_ID):
            self.ctx.counts["note_tags"] += 1
        file_name = (p_text(obj.props, S.IMAGE_FILENAME) or "").lower()
        if file_name.endswith(".pdf") or obj.props.get(S.PRINTOUT_SOURCE) is not None:
            self.ctx.counts["printouts"] += 1
        found = self.picture(obj)
        if found is None:
            return 0.0
        data, fmt = found
        w, h = self.image_size(obj, data, fmt)
        if not _finite(x, y, w, h):
            self.ctx.counts["far_content"] += 1
            return 0.0
        self.page.images.append(Image(x=x, y=y, w=w, h=h, data=data, fmt=fmt))
        return h

    def embedded_file(self, obj: Obj) -> None:
        name = (p_text(obj.props, S.EMBEDDED_FILE_NAME) or "").lower()
        audio = (".wma", ".mp3", ".wav", ".wmv", ".avi", ".mpg", ".m4a", ".mp4", ".aac", ".mov")
        if obj.props.get(S.IRECORD_MEDIA) is not None or name.endswith(audio):
            self.ctx.counts["recordings"] += 1
        else:
            self.ctx.counts["attachments"] += 1

    # -------------------------------------------------------------- outlines

    def outline(self, outline: Obj, x: float, y: float) -> float:
        """Lay out an outline from (x, y) downwards; returns the y below it."""
        props = outline.props
        width = (_half_inch(props, S.LAYOUT_MAX_WIDTH) or _half_inch(props, S.LAYOUT_OUTLINE_RESERVED_WIDTH)
                 or _half_inch(props, S.LAYOUT_MINIMUM_OUTLINE_WIDTH) or DEFAULT_OUTLINE_WIDTH)
        width = max(width, 2 * DEFAULT_FONT_SIZE)
        items: List[Tuple[Any, ...]] = []
        self.outline_items(props, 0, items, 0, Counter())
        block: List[_Para] = []
        block_y = y
        cursor = y

        def flush() -> None:
            nonlocal block
            if block:
                self.text_box(block, x, block_y, width)
            block = []

        for item in items:
            kind = item[0]
            if kind == "para":
                para: _Para = item[1]
                if para.has_ink:
                    flush()
                    cursor = self.inline_ink(para, x, cursor, width)
                    block_y = cursor
                else:
                    if not block:
                        block_y = cursor
                    block.append(para)
                    cursor += para.height(width)
            elif kind == "image":
                flush()
                cursor += self.image(item[1], x + item[2] * INDENT_PT, cursor)
                block_y = cursor
            elif kind == "ink":
                flush()
                cursor = self.flow_ink(item[1], x + item[2] * INDENT_PT, cursor)
                block_y = cursor
        flush()
        return cursor

    def outline_items(self, props: Dict[int, Any], indent: int, items: List[Tuple[Any, ...]], depth: int,
                      numbers: Counter) -> None:
        if depth > MAX_DEPTH:
            return
        for ref in p_refs(props, S.ELEMENT_CHILD_NODES):
            obj = self.get(ref)
            if obj is None or ref in self.visited:
                continue
            self.visited.add(ref)
            if obj.jcid == S.OUTLINE_GROUP:
                self.outline_items(obj.props, indent + 1, items, depth + 1, numbers)
            elif obj.jcid == S.OUTLINE_ELEMENT_NODE:
                prefix = self.list_prefix(obj.props, indent, numbers)
                for cref in p_refs(obj.props, S.CONTENT_CHILD_NODES):
                    content = self.get(cref)
                    if content is None or cref in self.visited:
                        continue
                    self.visited.add(cref)
                    jcid = content.jcid
                    if jcid == S.RICH_TEXT_NODE:
                        para = self.paragraph(content, indent)
                        para.prefix = prefix
                        items.append(("para", para))
                        prefix = ""
                    elif jcid == S.IMAGE_NODE:
                        items.append(("image", content, indent))
                    elif jcid == S.INK_CONTAINER:
                        items.append(("ink", content, indent))
                    elif jcid == S.TABLE_NODE:
                        self.ctx.counts["tables"] += 1
                    elif jcid == S.EMBEDDED_FILE_NODE:
                        self.embedded_file(content)
                    else:
                        self.ctx.counts["unknown"] += 1
                self.outline_items(obj.props, indent + 1, items, depth + 1, numbers)
            else:
                self.ctx.counts["unknown"] += 1

    def list_prefix(self, props: Dict[int, Any], indent: int, numbers: Counter) -> str:
        refs = p_refs(props, S.LIST_NODES)
        if not refs:
            numbers[indent] = 0
            return ""
        node = self.get(refs[0])
        fmt = p_text(node.props, S.NUMBER_LIST_FORMAT) if node is not None else None
        if fmt and "\ufffd" in fmt:
            numbers[indent] += 1
            return f"{numbers[indent]}. "
        numbers[indent] = 0
        return "\u2022 "

    def paragraph(self, node: Obj, indent: int) -> _Para:
        props = node.props
        para = _Para(indent)
        style_obj = self.get(p_ref(props, S.PARAGRAPH_STYLE_REF))
        para_style = style_obj.props if style_obj is not None else {}
        align = p_u8(props, S.PARAGRAPH_ALIGNMENT)
        if align is None:
            align = p_u8(para_style, S.PARAGRAPH_ALIGNMENT)
        para.align = align if align in (0, 1, 2) else 0
        if _has_prop_id(props, S.NOTE_TAG_STATES_ID):
            self.ctx.counts["note_tags"] += 1
        raw = p_bytes(props, S.RICH_EDIT_TEXT_UNICODE)
        if raw is not None:
            unit, codec = 2, "utf-16-le"
            if len(raw) % 2:
                raw = raw[:-1]
        else:
            raw = p_bytes(props, S.TEXT_EXTENDED_ASCII) or b""
            unit, codec = 1, "cp1252"
        while len(raw) >= unit and raw[-unit:] == b"\x00" * unit:  # WzInAtom: null-terminated
            raw = raw[:-unit]
        n_units = len(raw) // unit
        ends = [e for e in p_u32_array(props, S.TEXT_RUN_INDEX)]
        styles = [self.get(r) for r in _positional_refs(props, S.TEXT_RUN_FORMATTING)]
        run_data = p_sets(props, S.TEXT_RUN_DATA)
        data_objects = _positional_refs(props, S.TEXT_RUN_DATA_OBJECT)
        bounds: List[Tuple[int, int]] = []
        start = 0
        for end in ends + [n_units]:
            end = max(start, min(end, n_units))
            bounds.append((start, end))
            start = end
        sizes: List[float] = []
        for i, (a, b) in enumerate(bounds):
            seg = raw[a * unit:b * unit].decode(codec, "replace")
            style_node = styles[i] if i < len(styles) else (styles[-1] if styles else None)
            style = _Style(style_node.props if style_node is not None else {}, para_style)
            data = run_data[i] if i < len(run_data) else {}
            if "\ufffc" in seg:
                self.embedded_run(para, seg.count("\ufffc"), data, data_objects[i] if i < len(data_objects) else None,
                                  style)
                seg = seg.replace("\ufffc", "")
            seg = seg.replace("\x00", "")
            if not seg or style.hidden:
                continue
            if style.math:
                para.math = True
                continue
            seg = seg.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n")
            para.runs.append(style.run(seg))
            size = style.size or DEFAULT_FONT_SIZE
            sizes.append(size)
            para.inline.append(("text", len(seg) * size * CHAR_WIDTH, size))
        if sizes:
            para.size = max(sizes)
        else:
            base = _Style({}, para_style)
            para.size = base.size or DEFAULT_FONT_SIZE
        if para.math:
            self.ctx.counts["math"] += 1
        return para

    def embedded_run(self, para: _Para, count: int, data: Dict[int, Any], obj_ref: Any, style: _Style) -> None:
        """A run of U+FFFC placeholders: inline ink, an ink space or another embedded object."""
        start_x = p_f32(data, S.EMBEDDED_INK_START_X)
        width = p_f32(data, S.EMBEDDED_INK_WIDTH)
        space_w = p_f32(data, S.EMBEDDED_INK_SPACE_WIDTH)
        target = self.get(obj_ref)
        if target is not None and target.jcid == S.INK_DATA_NODE:
            start_y = p_f32(data, S.EMBEDDED_INK_START_Y)
            height = p_f32(data, S.EMBEDDED_INK_HEIGHT)
            para.inline.append(("ink", obj_ref, start_x, start_y, width, height))
            para.has_ink = True
        elif target is not None and target.jcid == S.INK_CONTAINER:
            para.inline.append(("container", target))
            para.has_ink = True
        elif space_w is not None:
            space_h = p_f32(data, S.EMBEDDED_INK_SPACE_HEIGHT) or 0.0
            para.inline.append(("space", space_w * S.HALF_INCH_PT, space_h * S.HALF_INCH_PT))
        elif p_u32(data, S.EMBEDDED_OBJECT_TYPE) == 0x00020027:
            para.inline.append(("break",))
        elif style.math:
            para.math = True
        elif target is not None or data:
            self.ctx.counts["unknown"] += count

    def text_box(self, paras: List[_Para], x: float, y: float, width: float) -> None:
        runs: List[TextRun] = []
        height = 0.0
        for i, para in enumerate(paras):
            if i:
                runs.append(TextRun("\n", size=para.size))
            if para.prefix or para.indent:
                runs.append(TextRun("    " * para.indent + para.prefix, size=para.size))
            runs.extend(para.runs)
            height += para.height(width)
        runs = _merge_runs(runs)
        text = "".join(r.text for r in runs)
        if not text.strip():
            return
        weights: Counter = Counter()
        for r in runs:
            weights[r.size or DEFAULT_FONT_SIZE] += len(r.text.strip())
        size = weights.most_common(1)[0][0] if weights else DEFAULT_FONT_SIZE
        aligns = {p.align for p in paras if p.text.strip()}
        align = {0: "left", 1: "center", 2: "right"}.get(aligns.pop() if len(aligns) == 1 else 0, "left")
        if not _finite(x, y, width, height):
            self.ctx.counts["far_content"] += 1
            return
        self.page.texts.append(TextBox(x=x, y=y, w=width, h=max(height, size * LINE_SPACING), text=text,
                                       runs=runs, size=size, align=align))
        self.ctx.counts["text_boxes"] += 1

    # -------------------------------------------------------------- inline ink (approximate)

    def inline_ink(self, para: _Para, x: float, y: float, width: float) -> float:
        """Lay out a paragraph holding handwritten words on lines from (x, y); returns the y below.

        Every inline item advances a cursor by its width: ink words by their recorded box
        (EmbeddedInkWidth), ink spaces by their recorded width, typed text by an estimate.
        A line wraps when the next item would cross the outline's width.  Each ink word's
        strokes are moved so that its box starts at the cursor on the current line.
        """
        left = x + para.indent * INDENT_PT
        right = x + max(width, 2 * DEFAULT_FONT_SIZE)
        cursor_x = left
        line_top = y
        line_h = para.line_height()
        text_runs: List[TextRun] = list(para.runs)
        for item in para.inline:
            kind = item[0]
            if kind == "break":
                cursor_x, line_top, line_h = left, line_top + line_h, para.line_height()
                continue
            if kind == "text":
                w, h = item[1], item[2] * LINE_SPACING
            elif kind == "space":
                w, h = _clamp(item[1]), _clamp(item[2])
            elif kind == "container":
                bottom = self.flow_ink(item[1], cursor_x, line_top)
                h = bottom - line_top
                w = 0.0
                line_h = max(line_h, h)
                continue
            else:
                _k, ref, sx, sy, ew, eh = item
                bounds = ink_bounds(self.space, ref)
                if sx is None or sy is None:
                    if bounds is None:
                        continue
                    sx, sy = bounds[0] / 1270.0, bounds[1] / 1270.0
                if ew is None or eh is None:
                    ew = (bounds[2] - bounds[0]) / 1270.0 if bounds else 1.0
                    eh = (bounds[3] - bounds[1]) / 1270.0 if bounds else 1.0
                w, h = _clamp(ew * S.HALF_INCH_PT), _clamp(eh * S.HALF_INCH_PT)
            if cursor_x > left and cursor_x + w > right:
                cursor_x, line_top, line_h = left, line_top + line_h, para.line_height()
            if kind == "ink":
                transform = InkTransform(cursor_x - sx * S.HALF_INCH_PT, line_top - sy * S.HALF_INCH_PT, 1.0, 1.0)
                strokes = ink_data_strokes(self.space, ref, transform, self.ctx.ink, self.pens, self.ink_stats)
                self.add_strokes(strokes)
                self.ctx.counts["inline_ink"] += 1
            cursor_x += w
            line_h = max(line_h, h)
        if "".join(r.text for r in text_runs).strip():
            self.text_box([para], x, y, width)
        return line_top + line_h

    def flow_ink(self, container: Obj, x: float, y: float) -> float:
        """An ink container inside an outline: its strokes moved to start at (x, y)."""
        before = len(self.page.strokes)
        self.ink_container(container, None, 0)
        added = self.page.strokes[before:]
        if not added:
            return y
        x0 = min(s.bbox()[0] for s in added)
        y0 = min(s.bbox()[1] for s in added)
        y1 = max(s.bbox()[3] for s in added)
        dx, dy = x - x0, y - y0
        for stroke in added:
            for p in stroke.points:
                p.x += dx
                p.y += dy
        return y + (y1 - y0)

    # -------------------------------------------------------------- page geometry

    def finish(self, props: Dict[int, Any]) -> None:
        """Size the page: its recorded size grown to hold the content (unbounded canvas)."""
        page = self.page
        width = _half_inch(props, S.PAGE_WIDTH)
        height = _half_inch(props, S.PAGE_HEIGHT)
        width = width if width and 0 < width <= MAX_COORD else DEFAULT_PAGE_WIDTH
        height = height if height and 0 < height <= MAX_COORD else DEFAULT_PAGE_HEIGHT
        boxes = [s.bbox() for s in page.strokes]
        boxes += [(i.x, i.y, i.x + i.w, i.y + i.h) for i in page.images]
        boxes += [(t.x, t.y, t.x + t.w, t.y + t.h) for t in page.texts]
        if boxes:
            min_x = min(b[0] for b in boxes)
            min_y = min(b[1] for b in boxes)
            dx = PAGE_MARGIN - min_x if min_x < 0 else 0.0
            dy = PAGE_MARGIN - min_y if min_y < 0 else 0.0
            if dx or dy:  # content above or left of the page origin: shift it onto the page
                _shift(page, dx, dy)
                boxes = [(b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy) for b in boxes]
            width = max(width, max(b[2] for b in boxes) + PAGE_MARGIN)
            height = max(height, max(b[3] for b in boxes) + PAGE_MARGIN)
        page.width, page.height = width, height
        if width > LARGE_PAGE_FACTOR * DEFAULT_PAGE_WIDTH or height > LARGE_PAGE_FACTOR * DEFAULT_PAGE_HEIGHT:
            self.ctx.counts["large_pages"] += 1


def _clamp(value: float) -> float:
    """An inline item's recorded size, limited to something a line can hold."""
    return min(max(value, 0.0), MAX_INLINE_PT)


def _shift(page: Page, dx: float, dy: float) -> None:
    for stroke in page.strokes:
        for p in stroke.points:
            p.x += dx
            p.y += dy
    for image in page.images:
        image.x += dx
        image.y += dy
    for text in page.texts:
        text.x += dx
        text.y += dy


def _merge_runs(runs: List[TextRun]) -> List[TextRun]:
    """Join neighbouring runs with the same formatting (fewer runs, same text)."""
    out: List[TextRun] = []
    for r in runs:
        if not r.text:
            continue
        if out:
            last = out[-1]
            if (last.bold, last.italic, last.underline, last.font, last.size, last.color) == \
                    (r.bold, r.italic, r.underline, r.font, r.size, r.color):
                out[-1] = TextRun(last.text + r.text, last.bold, last.italic, last.underline, last.font, last.size,
                                  last.color)
                continue
        out.append(r)
    return out


def _deleted(props: Dict[int, Any]) -> bool:
    """IsDeletedGraphSpaceContent ([MS-ONE] 2.3.73): set and not false / zero."""
    value = props.get(S.IS_DELETED_GRAPH_SPACE_CONTENT)
    if value is None:
        return any((k & 0x3FFFFFF) == (S.IS_DELETED_GRAPH_SPACE_CONTENT & 0x3FFFFFF) and v is True
                   for k, v in props.items())
    if isinstance(value, (bytes, bytearray)):
        return any(value)
    return bool(value)


# ----------------------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------------------


SECTION_NAME = 0x1C001D69  # the section's name as OneNote for the web stores it (not in [MS-ONE])


def _section_title(space: ObjectSpace, section: Obj) -> Optional[str]:
    meta = space.root(S.ROLE_METADATA)
    for pid in (S.SECTION_DISPLAY_NAME, SECTION_NAME):
        for props in ((meta.props if meta is not None else {}), section.props):
            name = (p_text(props, pid) or "").strip()
            if name:
                return name[:-4] if name.lower().endswith(".one") else name
    return None


def _read_section(ctx: _Context, data: bytes) -> Tuple[Optional[str], List[Page]]:
    """Pages of one section file (raises :class:`OneNoteError` / :class:`Encrypted`)."""
    store = open_store(data)
    if store.is_toc:
        raise OneNoteError(TOC_MESSAGE)
    root = store.root_space()
    for message in store.warnings:
        ctx.warn(message)
    if root is None:
        raise OneNoteError("damaged OneNote section: its section object is missing")
    section = root.root(S.ROLE_CONTENT)
    if section is None or section.jcid != S.SECTION_NODE:
        raise OneNoteError("damaged OneNote section: its section object is missing")
    title = _section_title(root, section)
    pages: List[Page] = []
    for series_ref in p_refs(section.props, S.ELEMENT_CHILD_NODES):
        series = root.get(series_ref)
        if series is None or series.jcid != S.PAGE_SERIES_NODE:
            ctx.counts["unknown"] += 1
            continue
        for space_ref in p_refs(series.props, S.CHILD_GRAPH_SPACE_ELEMENT_NODES):
            if ctx.pages >= MAX_PAGES:
                ctx.warn(f"Only the first {MAX_PAGES} pages were read")
                return title, pages
            space = store.space(space_ref)  # Encrypted propagates
            if space is None:
                ctx.counts["unreadable_pages"] += 1
                continue
            try:
                page = _PageReader(ctx, space).read()
            except Encrypted:
                raise
            except Exception:  # noqa: BLE001 - tolerant reader: one bad page never loses the others
                ctx.counts["unreadable_pages"] += 1
                continue
            if page is not None:
                pages.append(page)
                ctx.pages += 1
    for message in store.warnings:
        ctx.warn(message)
    return title, pages


# ----------------------------------------------------------------------------------
# Notebook ZIPs
# ----------------------------------------------------------------------------------


class _Zip:
    def __init__(self, data: bytes):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zip.infolist()
        except (zipfile.BadZipFile, NotImplementedError, OSError, ValueError, RuntimeError, EOFError) as exc:
            raise OneNoteError(f"not a OneNote notebook: not a readable ZIP archive ({exc})") from exc
        self.budget = MAX_TOTAL_BYTES
        self.files: Dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            name = info.filename.replace("\\", "/").lstrip("/")
            if info.is_dir() or name.endswith("/") or any(p in ("..", "") for p in name.split("/")[:-1]):
                continue
            if name.split("/")[0] == "__MACOSX" or posixpath.basename(name).startswith("._"):
                continue
            self.files[name] = info

    def read(self, ctx: _Context, name: str) -> Optional[bytes]:
        info = self.files[name]
        if info.file_size > MAX_MEMBER_BYTES or info.file_size > self.budget:
            ctx.warn(f"{name} inflates above the {MAX_MEMBER_BYTES // (1024 * 1024)} MB limit and was skipped")
            return None
        self.budget -= info.file_size
        try:
            return self.zip.read(info)
        except Exception as exc:  # noqa: BLE001 - corrupt member, unsupported compression, ...
            ctx.warn(f"{name} could not be unpacked and was skipped ({exc.__class__.__name__})")
            return None


def _in_recycle_bin(name: str) -> bool:
    parts = [p.lower() for p in name.split("/")]
    return RECYCLE_BIN in parts[:-1] or parts[-1] == DELETED_PAGES


def _toc_entries(ctx: _Context, data: bytes) -> List[Tuple[int, int, str, Optional[bytes]]]:
    """(ordering ID, position, file name, file identity) of every entry of a ``.onetoc2``."""
    store = open_store(data)
    root = store.root_space()
    toc = root.root(S.ROLE_CONTENT) if root is not None else None
    if toc is None:
        return []
    out = []
    for position, ref in enumerate(p_refs(toc.props, TOC_ENTRIES)):
        entry = root.get(ref)
        if entry is None:
            continue
        name = (p_text(entry.props, TOC_FILE_NAME) or "").strip()
        order = p_u32(entry.props, TOC_ORDERING_ID)
        identity = p_bytes(entry.props, TOC_FILE_IDENTITY)
        out.append((order if order is not None else 1 << 32, position, name, identity))
    return out


def _ordered_sections(ctx: _Context, archive: _Zip, sections: List[str], tocs: List[str]) -> List[str]:
    """Sections in notebook order: every folder by its ``.onetoc2`` where readable, else by name."""
    by_folder: Dict[str, List[str]] = {}
    for name in sections:
        by_folder.setdefault(posixpath.dirname(name), []).append(name)
    toc_of: Dict[str, str] = {}
    for name in sorted(tocs):
        toc_of.setdefault(posixpath.dirname(name), name)
    subfolders: Dict[str, set] = {}
    for folder in list(by_folder):  # every ancestor folder may order the ones below it
        while folder:
            parent = posixpath.dirname(folder)
            subfolders.setdefault(parent, set()).add(folder)
            folder = parent
    used_toc = False

    def children(folder: str) -> List[str]:
        return sorted(subfolders.get(folder, ()), key=str.lower)

    def visit(folder: str, depth: int) -> List[str]:
        nonlocal used_toc
        if depth > MAX_DEPTH:
            return []
        files = sorted(by_folder.get(folder, []), key=str.lower)
        subs = children(folder)
        ordered: List[str] = []
        toc = toc_of.get(folder)
        if toc is not None:
            raw = archive.read(ctx, toc)
            try:
                entries = sorted(_toc_entries(ctx, raw)) if raw is not None else []
            except Exception:  # noqa: BLE001 - an unreadable table of contents only loses the order
                entries = []
            if entries:
                used_toc = True
            for _order, _pos, entry, _identity in entries:
                low = entry.lower()
                match = next((f for f in files if posixpath.basename(f).lower() == low), None)
                if match is not None:
                    files.remove(match)
                    ordered.append(match)
                    continue
                sub = next((s for s in subs if posixpath.basename(s).lower() == low), None)
                if sub is not None:
                    subs.remove(sub)
                    ordered.extend(visit(sub, depth + 1))
        ordered.extend(files)
        for sub in subs:
            ordered.extend(visit(sub, depth + 1))
        return ordered

    result = visit("", 0)
    if not used_toc and len(sections) > 1:
        ctx.warn("No readable notebook table of contents (.onetoc2); sections were merged in file-name order")
    return result


def _read_zip(ctx: _Context, data: bytes) -> Tuple[str, List[Page]]:
    archive = _Zip(data)
    names = list(archive.files)
    sections = [n for n in names if n.lower().endswith(".one") and not _in_recycle_bin(n)]
    tocs = [n for n in names if n.lower().endswith(".onetoc2") and not _in_recycle_bin(n)]
    skipped_bin = [n for n in names if n.lower().endswith(".one") and _in_recycle_bin(n)]
    if not sections:
        if skipped_bin:
            raise OneNoteError("this notebook ZIP holds only OneNote's recycle bin, no sections to convert")
        if tocs:
            raise OneNoteError("this notebook ZIP holds a table of contents (.onetoc2) but no sections (.one files)")
        raise OneNoteError("not a OneNote notebook: the ZIP holds no .one section files")
    if len(sections) > MAX_SECTIONS:
        ctx.warn(f"The notebook has {len(sections)} sections; only the first {MAX_SECTIONS} were read")
        sections = sorted(sections, key=str.lower)[:MAX_SECTIONS]
    ordered = _ordered_sections(ctx, archive, sections, tocs)
    pages: List[Page] = []
    read_names: List[str] = []
    encrypted: List[str] = []
    failed: List[str] = []
    for name in ordered:
        raw = archive.read(ctx, name)
        label = posixpath.basename(name)[:-4]
        if raw is None:
            failed.append(label)
            continue
        try:
            title, section_pages = _read_section(ctx, raw)
        except Encrypted:
            encrypted.append(label)
            continue
        except OneNoteError as exc:
            failed.append(label)
            ctx.warn(f"Section {label} could not be read and was skipped: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one damaged section never loses the others
            failed.append(label)
            ctx.warn(f"Section {label} could not be read and was skipped ({exc.__class__.__name__})")
            continue
        read_names.append(label)
        pages.extend(section_pages)
    if encrypted:
        ctx.warn("Password-protected section(s) skipped (their content is encrypted): " + ", ".join(encrypted))
    if skipped_bin:
        ctx.warn(f"OneNote's recycle bin ({len(skipped_bin)} file(s) of deleted pages) was not converted")
    if not read_names:
        if encrypted and not failed:
            raise OneNoteError(ENCRYPTED_MESSAGE)
        raise OneNoteError("none of the notebook's sections could be read")
    if len(read_names) > 1:
        ctx.warn(f"{len(read_names)} sections were merged into one document, in this order: " + ", ".join(read_names))
    top = {n.split("/")[0] for n in names if "/" in n}
    root_files = [n for n in names if "/" not in n]
    if len(top) == 1 and not root_files:
        title = next(iter(top))
    elif len(read_names) == 1:
        title = read_names[0]
    else:
        title = ""  # the notebook's name is the ZIP's file name, which the caller knows
    return title, pages


# ----------------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------------


def read_onenote(data: bytes) -> Document:
    """Read a OneNote section (``.one``, either packaging) or a notebook ZIP.

    Raises :class:`ValueError` when the data is not OneNote (or a ZIP without sections), is
    a ``.onepkg`` package or a ``.onetoc2`` table of contents, is password protected, or is
    damaged beyond reading.  Everything else that is lost on the way is described on
    ``Document.warnings``.  ``Document.title`` is the section's (or notebook folder's) name
    when the file records one and empty otherwise: a desktop section is named by its file
    name, which :func:`gnnote.convert.convert` then uses.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("read_onenote expects bytes")
    data = bytes(data)
    doc = Document(title="", source_format="onenote")
    ctx = _Context(doc)
    try:
        if is_onepkg(data):
            raise OneNoteError(ONEPKG_MESSAGE)
        if data.startswith(b"PK"):
            title, pages = _read_zip(ctx, data)
        elif is_onenote(data):
            try:
                section_title, pages = _read_section(ctx, data)
            except Encrypted as exc:
                raise OneNoteError(ENCRYPTED_MESSAGE) from exc
            title = section_title or ""  # a desktop section's name is its file name
        else:
            raise OneNoteError(NOT_ONENOTE_MESSAGE)
    except OneNoteError:
        raise
    except RecursionError as exc:
        raise OneNoteError("damaged OneNote file: structures nested too deeply") from exc
    except (LookupError, TypeError, AttributeError, ArithmeticError, UnicodeError, struct.error) as exc:
        raise OneNoteError(f"damaged OneNote file ({exc.__class__.__name__}: {exc})") from exc
    doc.title = title
    doc.pages = pages
    if not pages:
        ctx.warn("The OneNote file holds no pages")
    ctx.report()
    return doc
