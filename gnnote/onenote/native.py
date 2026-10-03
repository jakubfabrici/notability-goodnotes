"""The native OneNote revision store ([MS-ONESTORE] 2.1 - 2.6): desktop ``.one`` sections and
``.onetoc2`` tables of contents as OneNote for Windows writes them to disk.

Structure (offsets and field layouts from the specification):

* a 1024-byte header with the transaction log, the root file node list and the expected
  file length;
* the transaction log says how many FileNodes of every file node list are committed; nodes
  beyond that count are left-overs of an interrupted write and are ignored;
* the root file node list names every object space (one per page plus the section itself),
  the root object space and the file data store (embedded pictures and files);
* an object space's manifest list points at its revision manifest list; the current
  revision is the one last labelled with the default context and revision role 1, and a
  revision includes the objects of the revision it depends on unless it redeclares them;
* objects are declared in object groups (``.one``) or directly in the revision manifest
  (``.onetoc2``); their CompactIDs resolve through the global identification table in
  effect where they are declared.

Objects are decoded lazily: only the objects the MS-ONE layer asks for are turned into
property sets, and superseded revisions are never read.  File node lists are read as they
are walked, never kept: every node counts against the document's work budget, and every
node, declaration and decoded property of an object space against the space's budget.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

from .common import (
    FILE_TYPE_ONE, FILE_TYPE_ONETOC2, FORMAT_NATIVE, MAX_SPACE_ITEMS, NIL, Budget, Buf, Damaged, Encrypted,
    ExtGuid, Obj, ObjectSpace, OneNoteError, Store, TooLarge, guid_bytes,
)

# what keeping one costs in a space's memory budget (about 100 bytes per unit)
REVISION_COST = 6  # a revision and its tables
DECLARATION_COST = 3  # an object declaration and its entry

__all__ = ["NativeStore"]

HEADER_SIZE = 1024
LIST_MAGIC = 0xA4567AB1F5F7F4C4
LIST_FOOTER = 0x8BC215C38233BA4B
STP_NIL = 0xFFFFFFFFFFFFFFFF
FILE_DATA_HEADER = guid_bytes("{BDE316E7-2665-4511-A4C4-8D4D0B7A9EAC}")

MAX_FRAGMENTS = 100_000  # fragments of one file node list
MAX_REVISION_CHAIN = 10_000

JCID_IS_PROPERTY_SET = 0x00020000
JCID_IS_FILE_DATA = 0x00080000

# FileNodeIDs ([MS-ONESTORE] 2.4.3)
OBJECT_SPACE_MANIFEST_ROOT = 0x004
OBJECT_SPACE_MANIFEST_LIST_REF = 0x008
OBJECT_SPACE_MANIFEST_LIST_START = 0x00C
REVISION_MANIFEST_LIST_REF = 0x010
REVISION_MANIFEST_LIST_START = 0x014
REVISION_MANIFEST_START4 = 0x01B
REVISION_MANIFEST_END = 0x01C
REVISION_MANIFEST_START6 = 0x01E
REVISION_MANIFEST_START7 = 0x01F
GLOBAL_ID_TABLE_START = 0x021
GLOBAL_ID_TABLE_START2 = 0x022
GLOBAL_ID_TABLE_ENTRY = 0x024
GLOBAL_ID_TABLE_ENTRY2 = 0x025
GLOBAL_ID_TABLE_ENTRY3 = 0x026
GLOBAL_ID_TABLE_END = 0x028
OBJECT_DECLARATION_WITH_REFCOUNT = 0x02D
OBJECT_DECLARATION_WITH_REFCOUNT2 = 0x02E
OBJECT_REVISION_WITH_REFCOUNT = 0x041
OBJECT_REVISION_WITH_REFCOUNT2 = 0x042
ROOT_OBJECT_REFERENCE2 = 0x059
ROOT_OBJECT_REFERENCE3 = 0x05A
REVISION_ROLE_DECLARATION = 0x05C
REVISION_ROLE_AND_CONTEXT_DECLARATION = 0x05D
OBJECT_DECLARATION_FILE_DATA3 = 0x072
OBJECT_DECLARATION_FILE_DATA3_LARGE = 0x073
OBJECT_DATA_ENCRYPTION_KEY = 0x07C
FILE_DATA_STORE_LIST_REF = 0x090
FILE_DATA_STORE_OBJECT_REF = 0x094
OBJECT_DECLARATION2 = 0x0A4
OBJECT_DECLARATION2_LARGE = 0x0A5
OBJECT_GROUP_LIST_REF = 0x0B0
OBJECT_GROUP_START = 0x0B4
OBJECT_GROUP_END = 0x0B8
READ_ONLY_OBJECT_DECLARATION2 = 0x0C4
READ_ONLY_OBJECT_DECLARATION2_LARGE = 0x0C5
CHUNK_TERMINATOR = 0x0FF

_DECLARATIONS = {
    OBJECT_DECLARATION_WITH_REFCOUNT, OBJECT_DECLARATION_WITH_REFCOUNT2, OBJECT_REVISION_WITH_REFCOUNT,
    OBJECT_REVISION_WITH_REFCOUNT2, OBJECT_DECLARATION2, OBJECT_DECLARATION2_LARGE,
    READ_ONLY_OBJECT_DECLARATION2, READ_ONLY_OBJECT_DECLARATION2_LARGE, OBJECT_DECLARATION_FILE_DATA3,
    OBJECT_DECLARATION_FILE_DATA3_LARGE,
}


class _Node:
    """One FileNode: its id, reference formats and where its ``fnd`` field lies."""

    __slots__ = ("fid", "base", "stp_fmt", "cb_fmt", "start", "end")

    def __init__(self, fid: int, base: int, stp_fmt: int, cb_fmt: int, start: int, end: int):
        self.fid, self.base, self.stp_fmt, self.cb_fmt, self.start, self.end = fid, base, stp_fmt, cb_fmt, start, end


class _Decl:
    """An object declaration: type, where its property set lies and how to resolve its IDs."""

    __slots__ = ("jcid", "stp", "cb", "table", "file_ref")

    def __init__(self, jcid: int, stp: Optional[int], cb: int, table: Dict[int, bytes], file_ref: Optional[str] = None):
        self.jcid, self.stp, self.cb, self.table, self.file_ref = jcid, stp, cb, table, file_ref


class _Revision:
    __slots__ = ("rid", "dependent", "encrypted", "groups", "objects", "roots", "table")

    def __init__(self, rid: ExtGuid, dependent: ExtGuid, encrypted: bool):
        self.rid = rid
        self.dependent = dependent
        self.encrypted = encrypted
        self.groups: List[Tuple[int, int]] = []  # object group lists, parsed only when needed
        self.objects: Dict[ExtGuid, _Decl] = {}  # objects declared directly (.onetoc2)
        self.roots: Dict[int, ExtGuid] = {}
        self.table: Dict[int, bytes] = {}  # last global identification table (for 0x025 / 0x026)


def _resolve(cid: int, table: Dict[int, bytes]) -> Optional[ExtGuid]:
    """CompactID -> ExtendedGUID through a global identification table (2.2.2).

    The all-zero CompactID is a null reference (OneNote writes it, for example, for the
    text runs of inline ink that have no ink object).
    """
    if cid == 0:
        return None
    guid = table.get(cid >> 8)
    if guid is None:
        return None
    return (guid, cid & 0xFF)


class NativeSpace(ObjectSpace):
    def __init__(self, store: "NativeStore", key: ExtGuid, objects: Dict[ExtGuid, _Decl],
                 roots: Dict[int, ExtGuid], budget: Budget):
        super().__init__(key, budget)
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
        decl = self._objects.get(oid)
        obj: Optional[Obj] = None
        if decl is not None:
            if decl.jcid & JCID_IS_PROPERTY_SET and decl.stp is not None:
                table = decl.table

                def resolve(oids: List[int], osids: List[int], ctxs: List[int]):
                    return ([_resolve(c, table) for c in oids], [_resolve(c, table) for c in osids],
                            [_resolve(c, table) for c in ctxs])

                props = self._store.decode(self, self._store.data, decl.stp, decl.stp + decl.cb, resolve)
                if props is not None:
                    obj = Obj(oid, decl.jcid, props)
            else:
                obj = Obj(oid, decl.jcid, {})
        self._cache[oid] = obj
        return obj

    def file_data(self, oid: Optional[ExtGuid]) -> Optional[bytes]:
        decl = self._objects.get(oid) if oid is not None else None
        if decl is None or decl.file_ref is None:
            return None
        return self._store.file_data_by_reference(decl.file_ref)


class NativeStore(Store):
    """A native revision store (``guidFileFormat`` = {109ADD3F-...})."""

    def __init__(self, data: bytes, work: Optional[Budget] = None):
        super().__init__(work)
        if len(data) < HEADER_SIZE:
            raise OneNoteError("not a OneNote file (shorter than the 1024-byte header)")
        if bytes(data[48:64]) != FORMAT_NATIVE:
            raise OneNoteError("not a native OneNote revision store")
        self.data = data
        self.file_type = bytes(data[0:16])
        if self.file_type not in (FILE_TYPE_ONE, FILE_TYPE_ONETOC2):
            raise OneNoteError("not a OneNote section or table of contents (unknown file type GUID)")
        head = Buf(data, 0, HEADER_SIZE)
        head.pos = 96
        self.c_transactions = head.u32()
        head.pos = 160
        self.fcr_transaction_log = (head.u64(), head.u32())
        self.fcr_root = (head.u64(), head.u32())
        head.pos = 196
        expected = head.u64()
        if expected and expected > len(data):
            self.warn_once("The OneNote file is truncated; content in the missing part is lost")
        self._counts = self._committed_counts()
        self._spaces: Dict[ExtGuid, Optional[Tuple[int, int]]] = {}  # gosid -> manifest list ref
        self._file_store_ref: Optional[Tuple[int, int]] = None
        self._file_store: Optional[Dict[bytes, Tuple[int, int]]] = None
        self._file_data: Dict[bytes, Optional[bytes]] = {}
        self.root_gosid: Optional[ExtGuid] = None
        self._read_root_list()

    # ------------------------------------------------------------------ helpers

    def _ref(self, buf: Buf, stp_fmt: int, cb_fmt: int) -> Tuple[int, int]:
        """A FileNodeChunkReference in the formats the FileNode header announces (2.2.4.2)."""
        if stp_fmt == 0:
            stp = buf.u64()
        elif stp_fmt == 1:
            stp = buf.u32()
        elif stp_fmt == 2:
            stp = buf.u16() * 8
        else:
            stp = buf.u32() * 8
        if cb_fmt == 0:
            cb = buf.u32()
        elif cb_fmt == 1:
            cb = buf.u64()
        elif cb_fmt == 2:
            cb = buf.u8() * 8
        else:
            cb = buf.u16() * 8
        return stp, cb

    def _chunk(self, stp: int, cb: int) -> Optional[Tuple[int, int]]:
        """``(stp, cb)`` if it is a usable reference inside the file, ``None`` for fcrNil/fcrZero."""
        if stp == STP_NIL or (stp == 0 and cb == 0) or cb == 0:
            return None
        if stp + cb > len(self.data):
            raise Damaged(f"reference to bytes {stp}..{stp + cb} beyond the end of the file")
        return stp, cb

    # ------------------------------------------------------------------ transaction log (2.3.3)

    def _committed_counts(self) -> Dict[int, int]:
        """FileNodeListID -> number of committed FileNodes, from the committed transactions."""
        counts: Dict[int, int] = {}
        pending: Dict[int, int] = {}
        remaining = self.c_transactions
        stp, cb = self.fcr_transaction_log
        seen = set()
        try:
            while remaining > 0:
                chunk = self._chunk(stp, cb)
                if chunk is None or stp in seen or len(seen) >= MAX_FRAGMENTS or cb < 12:
                    break
                seen.add(stp)
                self.work.spend(1 + cb // 8)  # one unit per transaction entry
                data = self.data
                for pos in range(stp, stp + cb - 12 - 7, 8):
                    src = int.from_bytes(data[pos:pos + 4], "little")
                    switch = int.from_bytes(data[pos + 4:pos + 8], "little")
                    if src == 1:
                        counts.update(pending)
                        pending = {}
                        remaining -= 1
                        if remaining == 0:
                            break
                    elif src >= 0x10:
                        pending[src] = switch
                if remaining == 0:
                    break
                tail = Buf(self.data, stp + cb - 12, stp + cb)
                stp, cb = tail.u64(), tail.u32()
        except Damaged:
            self.warn_once("The OneNote transaction log is damaged; uncommitted changes may be shown")
        return counts

    # ------------------------------------------------------------------ file node lists (2.4)

    def _nodes(self, stp: int, cb: int) -> Iterator[_Node]:
        """Every committed FileNode of the file node list starting at ``stp``, read as the
        caller walks them (a list is never kept, however long it is).

        A damaged fragment, a node that overruns it or a loop of fragments ends the list
        with a warning; the nodes read before it count.  :class:`Damaged` when not even the
        first node is readable; :class:`TooLarge` when the document's budget runs out.
        """
        count = 0
        limit: Optional[int] = None
        list_id: Optional[int] = None
        seen = set()
        data = self.data
        work = self.work
        try:
            while True:
                chunk = self._chunk(stp, cb)
                if chunk is None:
                    return
                if stp in seen or len(seen) >= MAX_FRAGMENTS:
                    raise Damaged("file node list fragments form a loop")
                seen.add(stp)
                if cb < 36:
                    raise Damaged("file node list fragment too small")
                head = Buf(data, stp, stp + cb)
                if head.u64() != LIST_MAGIC:
                    raise Damaged(f"no file node list at byte {stp}")
                fragment_list = head.u32()
                if list_id is None:
                    list_id = fragment_list
                    limit = self._counts.get(list_id)
                elif fragment_list != list_id:
                    raise Damaged("file node list fragment belongs to another list")
                pos = stp + 16
                end = stp + cb - 20
                while pos + 4 <= end:
                    if limit is not None and count >= limit:
                        return
                    header = int.from_bytes(data[pos:pos + 4], "little")
                    fid = header & 0x3FF
                    size = (header >> 10) & 0x1FFF
                    if fid == CHUNK_TERMINATOR:
                        break
                    if size < 4 or pos + size > end:
                        if header == 0:
                            break  # zero padding before the next-fragment reference
                        raise Damaged(f"FileNode at byte {pos} overruns its fragment")
                    work.spend(1)
                    count += 1
                    yield _Node(fid, (header >> 27) & 0xF, (header >> 23) & 3, (header >> 25) & 3, pos + 4, pos + size)
                    pos += size
                if limit is not None and count >= limit:
                    return
                tail = Buf(data, stp + cb - 20, stp + cb)
                stp, cb = tail.u64(), tail.u32()
        except Damaged:
            if not count:
                raise
            self.warn_once("Part of the OneNote file is damaged; the content stored there is lost")

    # ------------------------------------------------------------------ root list, object spaces

    def _read_root_list(self) -> None:
        stp, cb = self.fcr_root
        if self._chunk(stp, cb) is None:
            raise OneNoteError("damaged OneNote file: no root file node list")
        try:
            for node in self._nodes(stp, cb):
                try:
                    buf = Buf(self.data, node.start, node.end)
                    if node.fid == OBJECT_SPACE_MANIFEST_ROOT:
                        self.root_gosid = buf.ext_guid20()
                    elif node.fid == OBJECT_SPACE_MANIFEST_LIST_REF:
                        ref = self._ref(buf, node.stp_fmt, node.cb_fmt)
                        gosid = buf.ext_guid20()
                        self._spaces[gosid] = self._chunk(*ref)
                    elif node.fid == FILE_DATA_STORE_LIST_REF:
                        self._file_store_ref = self._chunk(*self._ref(buf, node.stp_fmt, node.cb_fmt))
                except Damaged:
                    self.warn_once("Part of the OneNote root file node list is damaged and was skipped")
        except Damaged as exc:
            raise OneNoteError(f"damaged OneNote file: the root file node list is unreadable ({exc})") from exc
        if self.root_gosid is None and len(self._spaces) == 1:
            self.root_gosid = next(iter(self._spaces))
        if self.root_gosid is None:
            raise OneNoteError("damaged OneNote file: no root object space")

    def root_space(self) -> Optional[NativeSpace]:
        return self.space(self.root_gosid)

    def space(self, ref: Any) -> Optional[NativeSpace]:
        if not isinstance(ref, tuple) or len(ref) != 2 or not isinstance(ref[0], bytes):
            return None
        if ref in self._space_cache:
            return self._space_cache[ref]
        self._space_cache[ref] = None  # also guards against re-entrance
        manifest = self._spaces.get(ref)
        space = None
        if manifest is not None:
            try:
                space = self._open_space(ref, manifest)
            except Encrypted:
                raise
            except Damaged:
                self.warn_once("A damaged part of the OneNote file was skipped")
            except TooLarge:
                if self.work.exhausted:
                    raise
                self.space_too_large()
        self._space_cache[ref] = space
        return space

    def _open_space(self, gosid: ExtGuid, manifest: Tuple[int, int]) -> Optional[NativeSpace]:
        budget = Budget(MAX_SPACE_ITEMS)
        revision_list = None
        for node in self._nodes(*manifest):
            if node.fid == REVISION_MANIFEST_LIST_REF:  # all but the last MUST be ignored
                revision_list = self._chunk(*self._ref(Buf(self.data, node.start, node.end), node.stp_fmt, node.cb_fmt))
        if revision_list is None:
            return None
        revisions, labels, order = self._revision_list(*revision_list, budget)
        current = labels.get((NIL, 1))
        if current is None or current not in revisions:
            if not order:
                return None
            current = order[-1]
            self.warn_once("A OneNote object space has no revision labelled as current; its last revision was used")
        chain: List[_Revision] = []
        seen = set()
        rid: Optional[ExtGuid] = current
        while rid is not None and rid != NIL and rid in revisions and rid not in seen:
            if len(chain) >= MAX_REVISION_CHAIN:
                raise Damaged("revision dependency chain too long")
            seen.add(rid)
            chain.append(revisions[rid])
            rid = revisions[rid].dependent
        if any(rev.encrypted for rev in chain):
            self.encrypted = True
            raise Encrypted("the section is password protected")
        objects: Dict[ExtGuid, _Decl] = {}
        roots: Dict[int, ExtGuid] = {}
        revs = chain[::-1]  # the oldest dependency first; later revisions override
        # a group listed more than once is read once, at its last place (which gives the same
        # objects as reading it each time)
        last = {group: (ri, gi) for ri, rev in enumerate(revs) for gi, group in enumerate(rev.groups)}
        for ri, rev in enumerate(revs):
            for gi, group in enumerate(rev.groups):
                if last[group] == (ri, gi):
                    for oid, decl in self._object_group(*group, budget).items():
                        self._merge(objects, oid, decl)
            for oid, decl in rev.objects.items():
                self._merge(objects, oid, decl)
            roots.update(rev.roots)
        return NativeSpace(self, gosid, objects, roots, budget)

    @staticmethod
    def _merge(objects: Dict[ExtGuid, _Decl], oid: ExtGuid, decl: _Decl) -> None:
        if decl.jcid == 0:  # ObjectRevisionWithRefCount: same type as the earlier declaration
            older = objects.get(oid)
            if older is None:
                return
            decl = _Decl(older.jcid, decl.stp, decl.cb, decl.table, older.file_ref)
        objects[oid] = decl

    # ------------------------------------------------------------------ revisions (2.1.9 - 2.1.12)

    def _revision_list(self, stp: int, cb: int, budget: Budget):
        revisions: Dict[ExtGuid, _Revision] = {}
        labels: Dict[Tuple[ExtGuid, int], ExtGuid] = {}
        order: List[ExtGuid] = []
        current: Optional[_Revision] = None
        table: Dict[int, bytes] = {}
        pending_roots: List[Tuple[int, int]] = []  # (role, CompactID) of .onetoc2 root references
        data = self.data
        for node in self._nodes(stp, cb):
            budget.spend(1)  # what a node may leave behind: a label, a table entry, a root
            fid = node.fid
            buf = Buf(data, node.start, node.end)
            if fid in (REVISION_MANIFEST_START4, REVISION_MANIFEST_START6, REVISION_MANIFEST_START7):
                budget.spend(REVISION_COST)
                rid = buf.ext_guid20()
                dependent = buf.ext_guid20()
                if fid == REVISION_MANIFEST_START4:
                    buf.skip(8)  # timeCreation
                role = buf.u32()
                odcs = buf.u16()
                context = buf.ext_guid20() if fid == REVISION_MANIFEST_START7 else NIL
                current = _Revision(rid, dependent, odcs == 2)
                revisions[rid] = current
                order.append(rid)
                labels[(context, role)] = rid
                table = {}
                pending_roots = []
            elif fid == REVISION_MANIFEST_END:
                if current is not None:
                    current.table = table
                    # RootObjectReference2FNDX precedes the identification table of its
                    # revision manifest; resolve it with that table, else the dependency's.
                    base = revisions.get(current.dependent)
                    for role, cid in pending_roots:
                        ref = _resolve(cid, table) or (_resolve(cid, base.table) if base is not None else None)
                        if ref is not None:
                            current.roots[role] = ref
                current = None
                table = {}
                pending_roots = []
            elif fid == REVISION_ROLE_DECLARATION:
                rid = buf.ext_guid20()
                if rid in revisions:
                    labels[(NIL, buf.u32())] = rid
            elif fid == REVISION_ROLE_AND_CONTEXT_DECLARATION:
                rid = buf.ext_guid20()
                role = buf.u32()
                context = buf.ext_guid20()
                if rid in revisions:
                    labels[(context, role)] = rid
            elif current is None:
                continue
            elif fid == OBJECT_DATA_ENCRYPTION_KEY:
                current.encrypted = True
            elif fid == OBJECT_GROUP_LIST_REF:
                group = self._chunk(*self._ref(buf, node.stp_fmt, node.cb_fmt))
                if group is not None:
                    current.groups.append(group)
            elif fid in (GLOBAL_ID_TABLE_START, GLOBAL_ID_TABLE_START2):
                table = {}
            elif fid == GLOBAL_ID_TABLE_ENTRY:
                index = buf.u32()
                table[index] = buf.guid()
            elif fid in (GLOBAL_ID_TABLE_ENTRY2, GLOBAL_ID_TABLE_ENTRY3):
                base = revisions.get(current.dependent)
                previous = base.table if base is not None else {}
                if fid == GLOBAL_ID_TABLE_ENTRY2:
                    src, dst = buf.u32(), buf.u32()
                    if src in previous:
                        table[dst] = previous[src]
                else:
                    src, count, dst = buf.u32(), buf.u32(), buf.u32()
                    for i in range(min(count, len(previous))):
                        if src + i in previous:
                            table[dst + i] = previous[src + i]
            elif fid == ROOT_OBJECT_REFERENCE3:
                oid = buf.ext_guid20()
                current.roots[buf.u32()] = oid
            elif fid == ROOT_OBJECT_REFERENCE2:
                cid = buf.u32()
                pending_roots.append((buf.u32(), cid))
            elif fid in _DECLARATIONS:
                budget.spend(DECLARATION_COST)
                self.work.spend(DECLARATION_COST)
                self._declare(node, table, current.objects)
        return revisions, labels, order

    # ------------------------------------------------------------------ object groups (2.1.13)

    def _object_group(self, stp: int, cb: int, budget: Budget) -> Dict[ExtGuid, _Decl]:
        objects: Dict[ExtGuid, _Decl] = {}
        table: Dict[int, bytes] = {}
        try:
            for node in self._nodes(stp, cb):
                budget.spend(1)
                fid = node.fid
                try:
                    if fid in (GLOBAL_ID_TABLE_START, GLOBAL_ID_TABLE_START2):
                        table = {}
                    elif fid == GLOBAL_ID_TABLE_ENTRY:
                        buf = Buf(self.data, node.start, node.end)
                        index = buf.u32()
                        table[index] = buf.guid()
                    elif fid in _DECLARATIONS:
                        budget.spend(DECLARATION_COST)
                        self.work.spend(DECLARATION_COST)
                        self._declare(node, table, objects)
                    elif fid == OBJECT_GROUP_END:
                        break
                except Damaged:
                    self.warn_once("Some OneNote object declarations are damaged and were skipped")
        except Damaged:
            self.warn_once("A damaged part of the OneNote file was skipped")
        return objects

    def _declare(self, node: _Node, table: Dict[int, bytes], objects: Dict[ExtGuid, _Decl]) -> None:
        fid = node.fid
        buf = Buf(self.data, node.start, node.end)
        file_ref: Optional[str] = None
        stp: Optional[int] = None
        cb = 0
        if fid in (OBJECT_DECLARATION_FILE_DATA3, OBJECT_DECLARATION_FILE_DATA3_LARGE):
            cid = buf.u32()
            jcid = buf.u32()
            buf.skip(1 if fid == OBJECT_DECLARATION_FILE_DATA3 else 4)  # cRef
            file_ref = self._storage_string(buf)
        else:
            stp, cb = self._ref(buf, node.stp_fmt, node.cb_fmt)
            if self._chunk(stp, cb) is None:
                stp = None
            cid = buf.u32()
            if fid in (OBJECT_REVISION_WITH_REFCOUNT, OBJECT_REVISION_WITH_REFCOUNT2):
                jcid = 0  # revises an object declared earlier; keeps its JCID
            elif fid in (OBJECT_DECLARATION_WITH_REFCOUNT, OBJECT_DECLARATION_WITH_REFCOUNT2):
                jcid = JCID_IS_PROPERTY_SET | (buf.u16() & 0x3FF)  # jci: JCID.index only (2.6.15)
            else:
                jcid = buf.u32()
        oid = _resolve(cid, table)
        if oid is None:
            self.warn_once("Some OneNote objects refer to unknown IDs and were skipped")
            return
        objects[oid] = _Decl(jcid, stp, cb, table, file_ref)

    @staticmethod
    def _storage_string(buf: Buf) -> str:
        """StringInStorageBuffer (2.2.3): a u32 character count and UTF-16LE characters."""
        count = buf.u32()
        if count > buf.remaining() // 2:
            raise Damaged("string longer than its node")
        return buf.take(2 * count).decode("utf-16-le", "replace")

    # ------------------------------------------------------------------ file data (2.5.21, 2.6.13)

    def file_data_by_reference(self, reference: str) -> Optional[bytes]:
        if not reference.startswith("<ifndf>"):
            if reference.startswith("<file>"):
                self.warn_once("Some pictures or files are stored outside the section (in a 'onefiles' "
                               "folder) and are missing")
            return None
        try:
            key = guid_bytes(reference[len("<ifndf>"):].strip())
        except ValueError:
            return None
        if self._file_store is None:
            self._file_store = {}
            if self._file_store_ref is not None:
                try:
                    for node in self._nodes(*self._file_store_ref):
                        if node.fid == FILE_DATA_STORE_OBJECT_REF:
                            buf = Buf(self.data, node.start, node.end)
                            ref = self._chunk(*self._ref(buf, node.stp_fmt, node.cb_fmt))
                            guid = buf.guid()
                            if ref is not None:
                                self._file_store[guid] = ref
                except Damaged:
                    self.warn_once("The OneNote file data store is damaged; some pictures are missing")
        if key in self._file_data:  # one copy, however often the file is used
            return self._file_data[key]
        ref = self._file_store.get(key)
        data: Optional[bytes] = None
        if ref is not None:
            stp, cb = ref
            try:
                buf = Buf(self.data, stp, stp + cb)
                if buf.guid() != FILE_DATA_HEADER:
                    raise Damaged("bad file data header")
                length = buf.u64()
                buf.skip(12)  # unused, reserved
                data = buf.take(length)
            except Damaged:
                self.warn_once("Some OneNote file data objects are damaged and were skipped")
        self._file_data[key] = data
        return data
