"""The "alternative packaging" of OneNote files ([MS-ONESTORE] 2.7 - 2.8 on top of
[MS-FSSHTTPB] 2.2.1): the form OneDrive, SharePoint and OneNote for the web store and serve
``.one`` / ``.onetoc2`` files in, so what an iPad user downloads from onedrive.com.

Layout: the four header GUIDs and four reserved bytes, then one compound stream object of
type 0x7A (storage index extended GUID + cell schema GUID) holding a Data Element Package
(0x15) whose data elements are

* the storage index (cell mappings and revision mappings),
* the storage manifest (root declares: the header cell and the root object space's cell),
* one cell manifest per cell (= object space + context) naming its current revision,
* revision manifests (revision, base revision, root objects, object groups),
* object groups (object declarations and object data, in parallel arrays),
* object data BLOBs (pictures and embedded files),
* data element fragments (large elements split into pieces; reassembled here).

Every OneNote object is two declaration/data pairs: partition 4 carries the JCID and
partition 1 the same ObjectSpaceObjectPropSet as the native format, whose CompactIDs are
matched in order with the object data's extended-GUID array (object references) and cell-ID
array (object-space references) ([MS-ONESTORE] 2.7.6 / 2.7.8).  A file data object has a
partition 2 BLOB as well.

Data elements are located in one pass and decoded only when the MS-ONE layer needs them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .common import (
    FILE_TYPE_ONE, FILE_TYPE_ONETOC2, FORMAT_PACKAGE, NIL, Buf, Damaged, Encrypted, ExtGuid, Obj,
    ObjectSpace, OneNoteError, Store, decode_object_propset, guid_bytes,
)

__all__ = ["PackageStore"]

CellId = Tuple[ExtGuid, ExtGuid]

MAX_DEPTH = 24  # nesting of compound stream objects (real files: 6)
SCHEMA_ONE = guid_bytes("{1F937CB4-B26F-445F-B9F8-17E20160E461}")
SCHEMA_ONETOC2 = guid_bytes("{E4DBFD38-E5C7-408B-A8A1-0E7B421E1F5F}")
DEFAULT_CONTEXT: ExtGuid = (guid_bytes("{84DEFAB9-AAA3-4A0D-A3A8-520C77AC7073}"), 1)
DATA_ROOT: ExtGuid = (guid_bytes("{84DEFAB9-AAA3-4A0D-A3A8-520C77AC7073}"), 2)
HEADER_ROOT: ExtGuid = (guid_bytes("{1A5A319C-C26B-41AA-B9C5-9BD8C44E07D4}"), 1)
ROOT_ROLES = guid_bytes("{4A3717F8-1C14-49E7-9526-81D942DE1741}")  # n = revision store root role
ROLE_ENCRYPTION_KEY = 3

# data element types (2.2.1.12.1)
STORAGE_INDEX, STORAGE_MANIFEST, CELL_MANIFEST, REVISION_MANIFEST, OBJECT_GROUP, FRAGMENT, BLOB = 1, 2, 3, 4, 5, 6, 10

# stream object types (2.2.1.5)
SO_DATA_ELEMENT = 0x01
SO_OBJECT_DATA_BLOB = 0x02
SO_OBJECT_EXCLUDED = 0x03
SO_BLOB_DECLARATION = 0x05
SO_ROOT_DECLARE_STORAGE = 0x07
SO_ROOT_DECLARE_REVISION = 0x0A
SO_CELL_CURRENT_REVISION = 0x0B
SO_SCHEMA_GUID = 0x0C
SO_REVISION_MAPPING = 0x0D
SO_CELL_MAPPING = 0x0E
SO_MANIFEST_MAPPING = 0x11
SO_PACKAGE = 0x15
SO_OBJECT_DATA = 0x16
SO_OBJECT_DECLARATION = 0x18
SO_GROUP_REFERENCE = 0x19
SO_REVISION_MANIFEST = 0x1A
SO_BLOB_REFERENCE = 0x1C
SO_GROUP_DECLARATIONS = 0x1D
SO_GROUP_DATA = 0x1E
SO_FRAGMENT = 0x6A
SO_PACKAGING = 0x7A


class _SO:
    """A stream object: its type, payload range and (for compound objects) its children."""

    __slots__ = ("type", "start", "end", "children")

    def __init__(self, typ: int, start: int, end: int):
        self.type, self.start, self.end = typ, start, end
        self.children: List["_SO"] = []


class _Reader(Buf):
    """:class:`Buf` plus the FSSHTTPB variable-width primitives."""

    def cu64(self) -> int:
        """Compact unsigned 64-bit integer (2.2.1.1)."""
        b0 = self.u8()
        if b0 == 0:
            return 0
        if b0 == 0x80:
            return self.u64()
        for width in range(1, 8):
            if b0 & ((1 << width) - 1) == 1 << (width - 1):
                self.pos -= 1
                return int.from_bytes(self.take(width), "little") >> width
        raise Damaged(f"bad compact integer {b0:#x}")

    def ext_guid(self) -> ExtGuid:
        """Extended GUID (2.2.1.7)."""
        b0 = self.u8()
        if b0 == 0:
            return NIL
        if b0 & 0x07 == 0x04:
            n = b0 >> 3
        elif b0 & 0x3F == 0x20:
            n = (b0 | self.u8() << 8) >> 6
        elif b0 & 0x7F == 0x40:
            self.pos -= 1
            n = int.from_bytes(self.take(3), "little") >> 7
        elif b0 == 0x80:
            n = self.u32()
        else:
            raise Damaged(f"bad extended GUID {b0:#x}")
        return (self.guid(), n)

    def ext_guid_array(self) -> List[ExtGuid]:
        count = self.cu64()
        if count > self.remaining():
            raise Damaged("extended GUID array longer than its data")
        return [self.ext_guid() for _ in range(count)]

    def cell_id(self) -> CellId:
        return (self.ext_guid(), self.ext_guid())

    def cell_id_array(self) -> List[CellId]:
        count = self.cu64()
        if count > self.remaining() // 2:
            raise Damaged("cell ID array longer than its data")
        return [self.cell_id() for _ in range(count)]

    def serial(self) -> None:
        """Serial number (2.2.1.9): skipped, the reader has no use for it."""
        b0 = self.u8()
        if b0 == 0x80:
            self.skip(24)
        elif b0 != 0:
            raise Damaged(f"bad serial number {b0:#x}")


def _header(data: Any, pos: int, limit: int) -> Tuple[bool, int, bool, int, int]:
    """Stream object header at ``pos``: (is_end, type, compound, payload_start, payload_end)."""
    if pos >= limit:
        raise Damaged("stream object truncated")
    b0 = data[pos]
    kind = b0 & 3
    if kind == 1:  # 8-bit end
        return True, b0 >> 2, False, pos + 1, pos + 1
    if pos + 2 > limit:
        raise Damaged("stream object truncated")
    if kind == 3:  # 16-bit end
        return True, int.from_bytes(data[pos:pos + 2], "little") >> 2, False, pos + 2, pos + 2
    if kind == 0:  # 16-bit start
        h = int.from_bytes(data[pos:pos + 2], "little")
        typ, compound, length, pos = (h >> 3) & 0x3F, bool(h & 4), h >> 9, pos + 2
    else:  # 32-bit start
        if pos + 4 > limit:
            raise Damaged("stream object truncated")
        h = int.from_bytes(data[pos:pos + 4], "little")
        typ, compound, length, pos = (h >> 3) & 0x3FFF, bool(h & 4), h >> 17, pos + 4
        if length == 0x7FFF:
            r = _Reader(data, pos, limit)
            length = r.cu64()
            pos = r.pos
    if length > limit - pos:
        raise Damaged("stream object longer than its container")
    return False, typ, compound, pos, pos + length


def _skip(data: Any, pos: int, limit: int, depth: int = 0) -> int:
    """The position just after the stream object at ``pos`` (children and end included)."""
    is_end, _typ, compound, _start, pos = _header(data, pos, limit)
    if is_end:
        raise Damaged("unexpected stream object end")
    if compound:
        if depth >= MAX_DEPTH:
            raise Damaged("stream objects nested too deeply")
        while True:
            is_end, _t, _c, _s, after = _header(data, pos, limit)
            if is_end:
                return after
            pos = _skip(data, pos, limit, depth + 1)
    return pos


def _tree(data: Any, pos: int, limit: int, depth: int = 0) -> Tuple[_SO, int]:
    """The stream object at ``pos`` with all its descendants, and the position after it."""
    is_end, typ, compound, start, pos = _header(data, pos, limit)
    if is_end:
        raise Damaged("unexpected stream object end")
    node = _SO(typ, start, pos)
    if compound:
        if depth >= MAX_DEPTH:
            raise Damaged("stream objects nested too deeply")
        while True:
            is_end, _t, _c, _s, after = _header(data, pos, limit)
            if is_end:
                pos = after
                break
            child, pos = _tree(data, pos, limit, depth + 1)
            node.children.append(child)
    return node, pos


class _Element:
    """A located data element: where it lives (a buffer, maybe a reassembled one)."""

    __slots__ = ("type", "data", "pos", "end", "_children")

    def __init__(self, typ: int, data: Any, pos: int, end: int):
        self.type, self.data, self.pos, self.end = typ, data, pos, end
        self._children: Optional[List[_SO]] = None

    def children(self) -> List[_SO]:
        if self._children is None:
            node, _ = _tree(self.data, self.pos, self.end)
            self._children = node.children
        return self._children

    def reader(self, so: _SO) -> _Reader:
        return _Reader(self.data, so.start, so.end)


class _ObjRecord:
    """What the object groups of a cell's revisions say about one object."""

    __slots__ = ("jcid", "data", "start", "end", "oids", "cells", "blob")

    def __init__(self) -> None:
        self.jcid = 0
        self.data: Any = None
        self.start = self.end = 0
        self.oids: List[ExtGuid] = []
        self.cells: List[CellId] = []
        self.blob: Optional[ExtGuid] = None


def _pair(cids: List[int], values: List[Any]) -> List[Any]:
    """Pair the CompactIDs of a stream, in order, with an Object Data array (2.7.8).

    The mapping table ignores nulls: when the array is shorter than the stream, the null
    CompactIDs have no entry and map to ``None``.
    """
    if len(values) >= len(cids):
        return [None if values[i] == NIL else values[i] for i in range(len(cids))]
    if len(values) == sum(1 for c in cids if c):
        it = iter(values)
        return [next(it) if c else None for c in cids]
    raise Damaged("object data holds fewer references than its property set uses")


def _resolver(oids: List[ExtGuid], cells: List[CellId]):
    def resolve(oid_cids: List[int], osid_cids: List[int], ctx_cids: List[int]):
        osids = _pair(osid_cids, cells)
        used = len(osid_cids) if len(cells) >= len(osid_cids) else sum(1 for c in osid_cids if c)
        return _pair(oid_cids, oids), osids, _pair(ctx_cids, cells[used:])

    return resolve


class PackageSpace(ObjectSpace):
    def __init__(self, store: "PackageStore", key: CellId, objects: Dict[ExtGuid, _ObjRecord],
                 roots: Dict[int, ExtGuid]):
        super().__init__(key)
        self._store = store
        self._objects = objects
        self.roots = dict(roots)
        self._cache: Dict[ExtGuid, Optional[Obj]] = {}

    def get(self, oid: Optional[ExtGuid]) -> Optional[Obj]:
        if oid is None:
            return None
        try:
            return self._cache[oid]
        except KeyError:
            pass
        rec = self._objects.get(oid)
        obj: Optional[Obj] = None
        if rec is not None:
            props: Optional[Dict[int, Any]] = {}
            if rec.data is not None:
                try:
                    props = decode_object_propset(rec.data, rec.start, rec.end, _resolver(rec.oids, rec.cells))
                except Damaged:
                    self._store.warn_once("Some OneNote objects could not be decoded and were skipped")
                    props = None
            if props is not None:
                obj = Obj(oid, rec.jcid, props)
        self._cache[oid] = obj
        return obj

    def file_data(self, oid: Optional[ExtGuid]) -> Optional[bytes]:
        rec = self._objects.get(oid) if oid is not None else None
        if rec is None or rec.blob is None:
            return None
        return self._store.blob(rec.blob)


class PackageStore(Store):
    """A ``.one`` / ``.onetoc2`` in the alternative packaging ({638DE92F-...})."""

    def __init__(self, data: bytes):
        super().__init__()
        if len(data) < 72 or bytes(data[48:64]) != FORMAT_PACKAGE:
            raise OneNoteError("not a OneNote file in the OneDrive packaging")
        self.data = data
        self.file_type = bytes(data[0:16])
        self._elements: Dict[ExtGuid, _Element] = {}
        self._cell_map: Dict[CellId, ExtGuid] = {}
        self._revision_map: Dict[ExtGuid, ExtGuid] = {}
        self._roots: Dict[ExtGuid, CellId] = {}
        self._space_cache: Dict[CellId, Optional[PackageSpace]] = {}
        self.schema: Optional[bytes] = None
        try:
            self._scan()
        except Damaged as exc:
            raise OneNoteError(f"damaged OneNote file: {exc}") from exc
        if self.schema == SCHEMA_ONETOC2:
            self.file_type = FILE_TYPE_ONETOC2
        elif self.schema == SCHEMA_ONE:
            self.file_type = FILE_TYPE_ONE
        if self.file_type not in (FILE_TYPE_ONE, FILE_TYPE_ONETOC2):
            raise OneNoteError("not a OneNote section or table of contents (unknown file type GUID)")
        self._index()

    def warn_once(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # ------------------------------------------------------------------ locating data elements

    def _scan(self) -> None:
        data = self.data
        limit = len(data)
        is_end, typ, compound, start, pos = _header(data, 68, limit)
        if is_end or typ != SO_PACKAGING or not compound:
            raise Damaged("no packaging stream object after the header")
        head = _Reader(data, start, pos)
        head.ext_guid()  # storage index extended GUID
        self.schema = head.guid()
        is_end, typ, compound, _start, pos = _header(data, pos, limit)
        if is_end or typ != SO_PACKAGE or not compound:
            raise Damaged("no data element package")
        fragments: Dict[ExtGuid, Tuple[int, Dict[int, bytes]]] = {}
        while True:
            try:
                is_end, typ, _compound, _start, after = _header(data, pos, limit)
                if is_end:
                    break
                end = _skip(data, pos, limit)
            except Damaged:
                if not self._elements:
                    raise
                self.warn_once("The OneNote file is truncated or damaged at its end; content stored there is lost")
                break
            if typ == SO_DATA_ELEMENT:
                try:
                    self._locate(data, pos, end, fragments)
                except Damaged:
                    self.warn_once("Some damaged OneNote data elements were skipped")
            pos = end
        budget = len(data)  # every fragment's bytes come from this file
        for fid, (size, pieces) in fragments.items():
            if size > budget:
                self.warn_once("Fragmented OneNote data elements add up to more than the file holds; "
                               "the excess was skipped")
                continue
            budget -= size
            self._reassemble(fid, size, pieces)

    def _locate(self, data: Any, pos: int, end: int, fragments: Dict[ExtGuid, Tuple[int, Dict[int, bytes]]]) -> None:
        _is_end, _typ, _compound, start, children = _header(data, pos, end)
        head = _Reader(data, start, children)
        eid = head.ext_guid()
        head.serial()
        dtype = head.cu64()
        if dtype == FRAGMENT:
            element = _Element(dtype, data, pos, end)
            for so in element.children():
                if so.type != SO_FRAGMENT:
                    continue
                r = element.reader(so)
                target = r.ext_guid()
                total = r.cu64()
                chunk_start = r.cu64()
                chunk_length = r.cu64()
                piece = r.take(min(chunk_length, r.remaining()))
                if total > len(self.data) or chunk_start + len(piece) > total:
                    raise Damaged("data element fragment outside its element")
                known = fragments.setdefault(target, (total, {}))
                if known[0] == total:
                    known[1][chunk_start] = piece
            return
        self._elements[eid] = _Element(dtype, data, pos, end)

    def _reassemble(self, fid: ExtGuid, total: int, pieces: Dict[int, bytes]) -> None:
        buf = bytearray(total)
        covered = 0
        for start in sorted(pieces):
            piece = pieces[start]
            buf[start:start + len(piece)] = piece
            covered += len(piece)
        if covered < total:
            self.warn_once("A fragmented OneNote data element is incomplete; part of the content may be missing")
        data = bytes(buf)
        try:
            is_end, typ, _c, start, children = _header(data, 0, len(data))
            if is_end or typ != SO_DATA_ELEMENT:
                raise Damaged("reassembled fragment is not a data element")
            end = _skip(data, 0, len(data))
            head = _Reader(data, start, children)
            eid = head.ext_guid()
            head.serial()
            dtype = head.cu64()
            self._elements[eid] = _Element(dtype, data, 0, end)
        except Damaged:
            self.warn_once("A fragmented OneNote data element could not be reassembled and was skipped")

    # ------------------------------------------------------------------ storage index and manifest

    def _index(self) -> None:
        for element in list(self._elements.values()):
            try:
                if element.type == STORAGE_INDEX:
                    for so in element.children():
                        r = element.reader(so)
                        if so.type == SO_CELL_MAPPING:
                            cell = r.cell_id()
                            self._cell_map[cell] = r.ext_guid()
                        elif so.type == SO_REVISION_MAPPING:
                            rid = r.ext_guid()
                            self._revision_map[rid] = r.ext_guid()
                elif element.type == STORAGE_MANIFEST:
                    for so in element.children():
                        r = element.reader(so)
                        if so.type == SO_ROOT_DECLARE_STORAGE:
                            root = r.ext_guid()
                            self._roots[root] = r.cell_id()
            except Damaged:
                self.warn_once("Part of the OneNote storage index is damaged and was skipped")
        if not self._cell_map:
            raise OneNoteError("damaged OneNote file: no object spaces in the storage index")

    def root_space(self) -> Optional[PackageSpace]:
        cell = self._roots.get(DATA_ROOT)
        if cell is not None:
            space = self.space(cell)
            if space is not None:
                return space
        # no usable root declare: the cell whose content root is a section / TOC node
        for cell in self._cell_map:
            if self._roots.get(HEADER_ROOT) == cell:
                continue
            space = self.space(cell)
            root = space.root(1) if space is not None else None
            if root is not None and root.jcid in (0x00060007, 0x00020001):
                return space
        return None

    def space(self, ref: Any) -> Optional[PackageSpace]:
        if not (isinstance(ref, tuple) and len(ref) == 2 and isinstance(ref[0], tuple)):
            return None
        cell: CellId = ref
        if cell not in self._cell_map:
            cell = (DEFAULT_CONTEXT, ref[1])
            if cell not in self._cell_map:
                return None
        return self._open(cell)

    def _open(self, cell: CellId) -> Optional[PackageSpace]:
        if cell in self._space_cache:
            return self._space_cache[cell]
        self._space_cache[cell] = None
        try:
            space = self._build(cell)
        except Encrypted:
            raise
        except Damaged:
            self.warn_once("A damaged part of the OneNote file was skipped")
            space = None
        self._space_cache[cell] = space
        return space

    # ------------------------------------------------------------------ cells and revisions

    def _build(self, cell: CellId) -> Optional[PackageSpace]:
        manifest = self._elements.get(self._cell_map.get(cell, NIL))
        if manifest is None or manifest.type != CELL_MANIFEST:
            return None
        current = NIL
        for so in manifest.children():
            if so.type == SO_CELL_CURRENT_REVISION:
                current = manifest.reader(so).ext_guid()
        chain: List[Tuple[_Element, ExtGuid, List[ExtGuid], Dict[int, ExtGuid]]] = []
        seen = set()
        rid = current
        while rid != NIL and rid not in seen:
            seen.add(rid)
            element = self._elements.get(self._revision_map.get(rid, NIL))
            if element is None or element.type != REVISION_MANIFEST:
                break
            base = NIL
            groups: List[ExtGuid] = []
            roots: Dict[int, ExtGuid] = {}
            for so in element.children():
                r = element.reader(so)
                if so.type == SO_REVISION_MANIFEST:
                    r.ext_guid()
                    base = r.ext_guid()
                elif so.type == SO_GROUP_REFERENCE:
                    groups.append(r.ext_guid())
                elif so.type == SO_ROOT_DECLARE_REVISION:
                    root = r.ext_guid()
                    obj = r.ext_guid()
                    if root[0] == ROOT_ROLES:
                        roots[root[1]] = obj
            chain.append((element, base, groups, roots))
            rid = base
        if any(ROLE_ENCRYPTION_KEY in roots for _e, _b, _g, roots in chain):
            self.encrypted = True
            raise Encrypted("the section is password protected")
        objects: Dict[ExtGuid, _ObjRecord] = {}
        all_roots: Dict[int, ExtGuid] = {}
        for _element, _base, groups, roots in reversed(chain):
            for gid in groups:
                self._apply_group(gid, objects)
            all_roots.update(roots)
        return PackageSpace(self, cell, objects, all_roots)

    def _apply_group(self, gid: ExtGuid, objects: Dict[ExtGuid, _ObjRecord]) -> None:
        element = self._elements.get(gid)
        if element is None or element.type != OBJECT_GROUP:
            self.warn_once("Some OneNote object groups are missing; part of the content is lost")
            return
        declarations: List[Tuple[ExtGuid, int, Optional[ExtGuid]]] = []
        payloads: List[Tuple[int, _SO]] = []
        try:
            for so in element.children():
                if so.type == SO_GROUP_DECLARATIONS:
                    for d in so.children:
                        r = element.reader(d)
                        if d.type == SO_OBJECT_DECLARATION:
                            oid = r.ext_guid()
                            declarations.append((oid, r.cu64(), None))
                        elif d.type == SO_BLOB_DECLARATION:
                            oid = r.ext_guid()
                            blob = r.ext_guid()
                            declarations.append((oid, r.cu64(), blob))
                elif so.type == SO_GROUP_DATA:
                    for d in so.children:
                        if d.type in (SO_OBJECT_DATA, SO_BLOB_REFERENCE, SO_OBJECT_EXCLUDED):
                            payloads.append((d.type, d))
        except Damaged:
            self.warn_once("A damaged OneNote object group was skipped")
            return
        for (oid, partition, blob), (ptype, so) in zip(declarations, payloads):
            try:
                rec = objects.get(oid)
                if rec is None:
                    rec = objects[oid] = _ObjRecord()
                if ptype == SO_OBJECT_EXCLUDED:
                    continue
                r = element.reader(so)
                oids = r.ext_guid_array()
                cells = r.cell_id_array()
                if ptype == SO_BLOB_REFERENCE:
                    rec.blob = r.ext_guid()
                    continue
                length = r.cu64()
                r.need(length)
                if partition == 4:
                    if length >= 4:
                        rec.jcid = int.from_bytes(element.data[r.pos:r.pos + 4], "little")
                elif partition == 1:
                    rec.data, rec.start, rec.end = element.data, r.pos, r.pos + length
                    rec.oids, rec.cells = oids, cells
                elif partition == 2 and blob is not None:
                    rec.blob = blob
            except Damaged:
                self.warn_once("Some OneNote objects are damaged and were skipped")

    def blob(self, bid: ExtGuid) -> Optional[bytes]:
        """The bytes of an Object Data BLOB element.

        The specification calls the payload a plain byte stream; OneDrive files prefix it
        with its length as a compact integer (a binary item), which is stripped when it
        matches.
        """
        element = self._elements.get(bid)
        if element is None or element.type != BLOB:
            return None
        try:
            for so in element.children():
                if so.type == SO_OBJECT_DATA_BLOB:
                    r = element.reader(so)
                    try:
                        length = r.cu64()
                    except Damaged:
                        length = -1
                    if length == r.remaining():
                        return r.take(length)
                    return bytes(element.data[so.start:so.end])
        except Damaged:
            self.warn_once("Some OneNote pictures or files are damaged and were skipped")
        return None
