"""Bounded, read-only access to an SQLite database given as bytes.

Some apps keep their notes in an SQLite database (Noteastic's ``noteastic.db``, Microsoft
Journal's ``.journal``).  This module opens such a database from memory, without temporary
files (the browser build has none to spare), and reads it defensively, because the bytes come
from an untrusted file:

* The bytes are loaded with ``sqlite3.connect(":memory:")`` and
  :meth:`sqlite3.Connection.deserialize` (Python 3.11+, SQLite 3.23+; both are in the Pyodide
  build gnnote's web page uses).  A database above :data:`MAX_DB_BYTES` is refused before it
  is copied.  The write-ahead-log flag in the header is cleared on the copy, so a database
  saved in WAL mode opens (its ``-wal`` file is never part of the bytes anyway).
* Before SQLite sees the bytes, the ``sqlite_master`` b-tree (the schema, which SQLite reads
  in full before the first statement runs) is walked in Python: a schema whose pages or
  overflow chains are reached twice, or that spans more than :data:`MAX_SCHEMA_BYTES` of pages
  or :data:`MAX_SCHEMA_ROWS` cells, is refused, because loading it would repeat its rows over
  and over (or take long for no real database's sake).  The walk counts pages the way SQLite
  does: a file that ends inside a page still has that page, read with zeros for the missing
  bytes.
* The connection is read only (``PRAGMA query_only``), does not trust the schema
  (``PRAGMA trusted_schema=OFF``: no functions with side effects in views, triggers or
  generated columns), ignores CHECK constraints and checks cell sizes, and a progress handler
  aborts any statement once the database has used its work budget: :data:`STEPS_PER_BYTE`
  virtual-machine steps per byte of the database plus :data:`STEP_SLACK`, at most
  :data:`MAX_VM_STEPS` (reading a sound database takes about 4 steps per byte).  Pages that
  hold nothing but zeros do not count (:func:`content_bytes`): they give SQLite no steps to
  take, and a ZIP archive stores thousands of them in a few bytes.
* Tables are discovered only from ``sqlite_master`` rows of type ``table`` whose SQL creates a
  real (not virtual) table and whose root page is a page number.  SQLite refuses to load a
  schema whose row names a table other than the one its SQL creates, and two schema objects
  cannot share a name, so a view or a virtual table never stands in for a table the caller
  asks for.  Views are never prepared (expanding a crafted chain of views can take minutes).
  Statements name the ``main`` schema explicitly.
* Before the first scan of a table, SQLite's ``quick_check`` of that table verifies its
  b-trees: a crafted file can make two parents share a page or two rows share an overflow
  chain, so that a scan returns the same rows over and over.  Only the check's first result
  row is fetched, so a damaged table is refused as soon as the b-tree walk reports it (the
  check's row-by-row pass never runs over a damaged tree).  A table that fails the check is
  not read, and neither is a table with a NOT NULL virtual computed column, which the check
  would compute for every row (an unusual definition: the documented Noteastic tables have
  no computed columns at all).
* :meth:`SqliteDatabase.select` reads named columns only.  Columns are checked with
  ``pragma_table_info`` first (generated columns do not count), so a missing optional column
  reads as ``None`` and is reported instead of failing the statement; every TEXT / BLOB value
  is capped in bytes (:data:`DEFAULT_MAX_VALUE`, more where the caller allows it) and replaced
  by :data:`TOO_LARGE` above the cap without being handed to Python; rows are capped
  (``max_rows``, :data:`DEFAULT_MAX_ROWS`).  All selects together may load at most
  :data:`DATA_FACTOR` times the database's size (plus :data:`DATA_SLACK`) in text and blob
  values, counted in bytes whether or not a value was over its cap (SQLite loads it either
  way to measure it), so damage the check misses can neither fill memory nor keep SQLite busy
  copying.  The statements are plain table scans without joins or sorting; callers join and
  sort in Python.
* Text that is not valid UTF-8 is decoded with replacement characters instead of failing.

Everything goes wrong as a :class:`SqliteError` (a :class:`ValueError`) with an English message.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

__all__ = ["SQLITE_MAGIC", "MAX_DB_BYTES", "MAX_VM_STEPS", "STEPS_PER_BYTE", "STEP_SLACK", "DEFAULT_MAX_ROWS",
           "DEFAULT_MAX_VALUE", "TOO_LARGE", "DATA_FACTOR", "DATA_SLACK", "SqliteError", "SqliteUnavailable",
           "SqliteTooComplex", "Selection", "SqliteDatabase", "is_sqlite", "sqlite_available", "schema_area",
           "schema_tree_problem", "content_bytes"]

SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_DB_BYTES = 256 * 1024 * 1024  # a database larger than this is not opened
MAX_VM_STEPS = 2_000_000_000  # SQLite virtual-machine steps one database may use in total, at most ...
STEPS_PER_BYTE = 64  # ... this many per byte of the database (reading a sound one takes about 4) ...
STEP_SLACK = 20_000_000  # ... plus this many (about half a second)
PROGRESS_INTERVAL = 10_000  # steps between two calls of the progress handler
DEFAULT_MAX_ROWS = 1_000_000  # rows one select returns unless the caller allows more
DEFAULT_MAX_VALUE = 64 * 1024  # bytes of one TEXT or BLOB value unless allowed more
DATA_FACTOR = 2  # text and blob bytes all selects may return, per byte of the database ...
DATA_SLACK = 16 * 1024 * 1024  # ... plus this much
FETCH_BATCH = 1_000  # rows fetched from SQLite at a time
MAX_SCHEMA_ROWS = 100_000  # cells of the sqlite_master b-tree a database may have
MAX_SCHEMA_BYTES = 8 * 1024 * 1024  # bytes of pages (tree and overflow) of the sqlite_master b-tree
MAX_COLUMNS = 2_000  # columns of one table looked at (SQLite's own default limit)
MAX_SCHEMA_PAGES = 256  # pages of the sqlite_master b-tree schema_area() collects


class _TooLarge:
    """The value of a cell that exceeded its size cap (it was not read)."""

    _instance: Optional["_TooLarge"] = None

    def __new__(cls) -> "_TooLarge":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "TOO_LARGE"

    def __bool__(self) -> bool:
        return True


TOO_LARGE: Any = _TooLarge()


class SqliteError(ValueError):
    """The database cannot be opened or read (damaged, not SQLite, or over a limit)."""


class SqliteUnavailable(SqliteError):
    """This Python has no usable ``sqlite3`` module (or one without ``deserialize``)."""


class SqliteTooComplex(SqliteError):
    """The database used up its work budget (:data:`MAX_VM_STEPS`) or its data budget."""


def is_sqlite(data: bytes) -> bool:
    """``True`` when ``data`` starts with the SQLite 3 file header."""
    return bytes(data[:16]) == SQLITE_MAGIC


def sqlite_available() -> bool:
    """``True`` when this Python can open databases from memory."""
    try:
        import sqlite3
    except ImportError:
        return False
    return hasattr(sqlite3.Connection, "deserialize")


def _page_size(data: bytes) -> Optional[int]:
    """The page size the header of the SQLite file ``data`` declares; ``None`` when it is not one
    SQLite accepts (a power of two from 512 to 65536) or ``data`` is not SQLite."""
    if not is_sqlite(data) or len(data) < 18:
        return None
    size = int.from_bytes(data[16:18], "big")
    size = 65536 if size == 1 else size
    if size < 512 or size & (size - 1):
        return None
    return size


def _page_count(data: bytes, size: int) -> int:
    """The number of pages SQLite sees in ``data``: a file that ends inside a page has that page
    too (SQLite rounds the file's size up and reads the missing bytes as zeros)."""
    return -(-len(data) // size)


def _page(data: bytes, number: int, size: int) -> bytes:
    """Page ``number`` (1 is the first) of ``data``, filled up with zeros to ``size`` bytes the way
    SQLite reads the last page of a file that ends inside it."""
    start = (number - 1) * size
    page = data[start:start + size]
    if len(page) < size:
        page = bytes(page) + bytes(size - len(page))
    return page


def content_bytes(data: bytes) -> int:
    """The bytes of the SQLite file ``data`` outside pages that hold nothing but zeros (all of
    ``data`` when it is not SQLite or its page size is not valid).

    A page of zeros gives SQLite no statement steps to take: it is not a b-tree page, and as an
    overflow or free page it is copied or skipped inside one step.  A ZIP archive, though,
    stores thousands of such pages in a few bytes, so the work budget of
    :class:`SqliteDatabase` is based on this size rather than on ``len(data)``.
    """
    size = _page_size(data)
    if size is None:
        return len(data)
    zero = bytes(size)
    full = len(data) - len(data) % size
    empty = 0
    for start in range(size, full, size):  # page 1 holds the header
        if data.startswith(zero, start):
            empty += size
    tail = len(data) - full
    if tail and full and data.startswith(zero[:tail], full):
        empty += tail
    return len(data) - empty


def schema_area(data: bytes, max_pages: int = MAX_SCHEMA_PAGES) -> bytes:
    """The raw pages of the ``sqlite_master`` b-tree of the SQLite file ``data`` (page 1 and the
    pages it points to), joined; ``b""`` when ``data`` is not SQLite.  A sniffer can look for
    table names in it without opening the database: a schema that outgrew page 1 moves its
    rows to pages anywhere in the file.  At most ``max_pages`` pages, each visited once (a last
    page the file ends inside is filled up with zeros, as SQLite reads it).
    """
    size = _page_size(data)
    if size is None or len(data) < 512:
        return b""
    npages = _page_count(data, size)
    out: List[bytes] = []
    stack = [1]
    seen: set = set()
    while stack and len(out) < max_pages:
        number = stack.pop()
        if number in seen or not 1 <= number <= npages:
            continue
        seen.add(number)
        page = _page(data, number, size)
        out.append(page)
        head = 100 if number == 1 else 0
        if page[head] != 0x05:  # only interior table pages point further
            continue
        cells = int.from_bytes(page[head + 3:head + 5], "big")
        children = [int.from_bytes(page[head + 8:head + 12], "big")]
        for i in range(min(cells, (size - head - 12) // 2)):
            at = int.from_bytes(page[head + 12 + 2 * i:head + 14 + 2 * i], "big")
            if at + 4 <= size:
                children.append(int.from_bytes(page[at:at + 4], "big"))
        stack += reversed(children)
    return b"".join(out)


def _varint(data: bytes, at: int, end: int) -> Tuple[int, int]:
    """The SQLite varint at ``at`` (ending before ``end``) and the position after it."""
    value = 0
    for i in range(9):
        if at + i >= end:
            raise IndexError("varint beyond the page")
        byte = data[at + i]
        if i == 8:
            return (value << 8) | byte, at + 9
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, at + i + 1
    raise AssertionError("unreachable")


def schema_tree_problem(data: bytes, max_pages: Optional[int] = None,
                        max_cells: int = MAX_SCHEMA_ROWS) -> Optional[str]:
    """Why SQLite must not load the schema of the database ``data``; ``None`` when it may.

    Walks the ``sqlite_master`` b-tree (page 1, the pages it points to and the overflow chains
    of its rows) without SQLite.  SQLite reads every schema row before the first statement
    runs and does not notice, while doing so, a page that two parents share or an overflow
    chain that two rows share, so a few kilobytes of crafted file could make it read the same
    rows millions of times.  The walk returns a description when a page is reached twice or
    the schema spans more than ``max_pages`` pages (by default :data:`MAX_SCHEMA_BYTES` worth)
    or ``max_cells`` cells.  Other damage (a page beyond the file, a page of the wrong kind)
    is left for SQLite to report.  Pages are counted and read as SQLite reads them: the
    file's size is rounded up to whole pages and a last page that the file ends inside is
    filled up with zeros (``deserialize`` accepts such a file).  The page layout follows
    SQLite's published file format.
    """
    size = _page_size(data)
    if size is None or len(data) < 100:
        return None  # SQLite refuses such a file itself
    usable = size - data[20]
    if usable < 480:
        return None  # SQLite refuses such a file itself
    npages = _page_count(data, size)  # with a last page the file ends inside, as SQLite counts
    if max_pages is None:
        max_pages = MAX_SCHEMA_BYTES // size
    max_local = usable - 35  # a leaf cell's payload up to this size has no overflow chain
    min_local = (usable - 12) * 32 // 255 - 23
    seen: set = set()
    cells = 0
    stack = [1]

    def visit(number: int) -> Optional[str]:
        if number in seen:
            return f"its schema reaches page {number} twice"
        seen.add(number)
        if len(seen) > max_pages:
            return f"its schema spans more than {max_pages} pages"
        return None

    while stack:
        number = stack.pop()
        if not 1 <= number <= npages:
            continue
        problem = visit(number)
        if problem:
            return problem
        page = _page(data, number, size)  # page-relative offsets from here on
        head = 100 if number == 1 else 0
        kind = page[head]
        if kind not in (0x05, 0x0D):  # neither an interior nor a leaf page of a table b-tree
            continue
        pointers = head + (12 if kind == 0x05 else 8)
        count = int.from_bytes(page[head + 3:head + 5], "big")
        if pointers + 2 * count > size:
            continue
        cells += count
        if cells > max_cells:
            return f"its schema has more than {max_cells} entries"
        children: List[int] = []
        for i in range(count):
            at = int.from_bytes(page[pointers + 2 * i:pointers + 2 * i + 2], "big")
            if kind == 0x05:  # an interior cell: the left child's page number, then a key
                if at + 4 <= size:
                    children.append(int.from_bytes(page[at:at + 4], "big"))
                continue
            try:  # a leaf cell: the payload's size, the rowid, then the payload
                payload, at = _varint(page, at, size)
                _rowid, at = _varint(page, at, size)
            except IndexError:
                continue
            if payload <= max_local:
                continue
            local = min_local + (payload - min_local) % (usable - 4)
            if local > max_local:
                local = min_local
            if at + local + 4 > size:
                continue
            link = int.from_bytes(page[at + local:at + local + 4], "big")
            for _ in range(-(-(payload - local) // (usable - 4))):  # the overflow chain
                if not 1 <= link <= npages:
                    break
                problem = visit(link)
                if problem:
                    return problem
                start = (link - 1) * size
                link = int.from_bytes(data[start:start + 4].ljust(4, b"\0"), "big")
        if kind == 0x05:
            children.append(int.from_bytes(page[head + 8:head + 12], "big"))  # the right-most child
        stack += reversed(children)
    return None


def _decode_text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass
class Selection:
    """The result of :meth:`SqliteDatabase.select`.

    ``rows`` hold one tuple per row with the requested ``columns`` in order; ``present`` is
    ``False`` when the table does not exist or lacks a required column (``rows`` is then
    empty).  ``missing`` lists the requested columns the table lacks (they read as ``None``),
    ``truncated`` says that only the first ``max_rows`` rows were returned and ``oversized``
    counts, per column, the values replaced by :data:`TOO_LARGE`.
    """

    table: str
    columns: Tuple[str, ...]
    rows: List[Tuple[Any, ...]] = field(default_factory=list)
    present: bool = False
    missing: Tuple[str, ...] = ()
    truncated: bool = False
    oversized: Dict[str, int] = field(default_factory=dict)

    def dicts(self) -> Iterable[Dict[str, Any]]:
        """The rows as ``{column: value}`` dictionaries (built lazily)."""
        cols = self.columns
        for row in self.rows:
            yield dict(zip(cols, row))


class SqliteDatabase:
    """A read-only SQLite database opened from bytes (see the module docstring).

    ``what`` names the data in error messages ("the Noteastic database").  Raises
    :class:`SqliteUnavailable` when ``sqlite3`` or ``Connection.deserialize`` is missing and
    :class:`SqliteError` when the bytes are not a readable SQLite database.
    """

    def __init__(self, data: bytes, what: str = "SQLite database", max_bytes: Optional[int] = None,
                 max_steps: Optional[int] = None):
        self.what = what
        max_bytes = MAX_DB_BYTES if max_bytes is None else max_bytes
        data = bytes(data)
        if not is_sqlite(data):
            raise SqliteError(f"{what} is not an SQLite database (no SQLite header)")
        if len(data) > max_bytes:
            raise SqliteError(f"{what} is {len(data) // (1024 * 1024)} MB, more than the "
                              f"{max_bytes // (1024 * 1024)} MB gnnote reads")
        if max_steps is None:  # proportional to the data: a tiny file cannot keep SQLite busy for long
            max_steps = min(MAX_VM_STEPS, STEPS_PER_BYTE * content_bytes(data) + STEP_SLACK)
        problem = schema_tree_problem(data)
        if problem:
            raise SqliteError(f"{what} is damaged ({problem})")
        try:
            import sqlite3
        except ImportError as exc:  # a Python built without SQLite
            raise SqliteUnavailable(f"cannot read {what}: this Python has no sqlite3 module") from exc
        if not hasattr(sqlite3.Connection, "deserialize"):
            raise SqliteUnavailable(f"cannot read {what}: this Python's sqlite3 module cannot open a database "
                                    "from memory (Python 3.11 or newer with SQLite 3.23 or newer is needed)")
        self._sqlite3 = sqlite3
        # The progress handler counts down in a list it closes over, not through a bound
        # method, so the connection and this object form no reference cycle.
        steps = [max(1, max_steps // PROGRESS_INTERVAL)]

        def progress() -> int:
            steps[0] -= 1
            return 1 if steps[0] < 0 else 0

        self._steps = steps
        self._data_left = DATA_FACTOR * len(data) + DATA_SLACK
        self._data_exhausted = False
        if len(data) >= 20 and (data[18] == 2 or data[19] == 2):
            patched = bytearray(data)
            patched[18] = patched[19] = 1  # WAL -> rollback journal; harmless for reading
            data = bytes(patched)
        con = sqlite3.connect(":memory:", isolation_level=None)
        try:
            con.text_factory = _decode_text
            con.set_progress_handler(progress, PROGRESS_INTERVAL)
            for pragma in ("PRAGMA trusted_schema=OFF", "PRAGMA query_only=ON", "PRAGMA cell_size_check=ON",
                           "PRAGMA mmap_size=0", "PRAGMA ignore_check_constraints=ON"):
                con.execute(pragma)
            if hasattr(con, "setconfig"):  # Python 3.12+: defensive mode (no schema writes, no shadow-table edits)
                defensive = getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", None)
                if defensive is not None:
                    con.setconfig(defensive, True)
            con.deserialize(data)
            con.execute("PRAGMA query_only=ON")
        except (sqlite3.Error, OverflowError, MemoryError, ValueError) as exc:
            con.close()
            raise SqliteError(f"{what} could not be opened ({exc})") from exc
        self._con: Any = con
        self._columns: Dict[str, Dict[str, str]] = {}
        self._checked: Dict[str, Optional[str]] = {}  # table -> None when sound, else why it is not read
        try:
            self.tables: Dict[str, str] = self._discover()
        except BaseException:
            self.close()  # do not keep the copy of the database alive until a garbage collection
            raise

    # -- plumbing ----------------------------------------------------------------------

    @property
    def exhausted(self) -> bool:
        """``True`` once the work or data budget is used up (every further query fails)."""
        return self._steps[0] < 0 or self._data_exhausted

    def _check(self) -> None:
        if self._con is None:
            raise SqliteError(f"{self.what} is closed")
        if self.exhausted:
            raise self._too_complex()

    def _too_complex(self) -> SqliteTooComplex:
        if self._data_exhausted:
            return SqliteTooComplex(f"{self.what} is too large or complex to read completely (reading it returned "
                                    "more data than it holds, so it is probably damaged)")
        return SqliteTooComplex(f"{self.what} is too large or complex to read completely (gnnote's work limit "
                                "was reached)")

    def _take_data(self, n: int) -> None:
        self._data_left -= n
        if self._data_left < 0:
            self._data_exhausted = True
            raise self._too_complex()

    def _query(self, sql: str, params: Sequence[Any] = (), max_rows: Optional[int] = None) -> List[Tuple[Any, ...]]:
        self._check()
        sqlite3 = self._sqlite3
        try:
            cursor = self._con.execute(sql, tuple(params))
            if max_rows is None:
                return cursor.fetchall()
            return cursor.fetchmany(max_rows)
        except (sqlite3.Error, OverflowError, MemoryError) as exc:
            if self.exhausted:
                raise self._too_complex() from exc
            raise SqliteError(f"{self.what} is damaged ({exc})") from exc

    def _discover(self) -> Dict[str, str]:
        rows = self._query("SELECT type, name, rootpage, sql FROM main.sqlite_master", max_rows=MAX_SCHEMA_ROWS)
        tables: Dict[str, str] = {}
        for kind, name, root, sql in rows:
            if kind != "table" or not isinstance(name, str) or not isinstance(sql, str):
                continue
            if name.lower().startswith("sqlite_") or isinstance(root, bool) or not isinstance(root, int) or root < 1:
                continue
            head = " ".join(sql[:64].split()).upper()
            if not head.startswith("CREATE TABLE"):  # CREATE VIRTUAL TABLE, or a schema row that lies
                continue
            tables.setdefault(name.lower(), name)
        return tables

    def _verify(self, stored: str) -> None:
        """Check a table's b-trees once (SQLite's ``quick_check``); raise :class:`SqliteError` when
        the table is damaged or cannot be checked safely."""
        if stored not in self._checked:
            reason: Optional[str] = None
            try:
                computed = self._query("SELECT count(*) FROM pragma_table_xinfo(?, 'main') WHERE hidden = 2 AND "
                                       '"notnull"', (stored,))
            except SqliteTooComplex:
                raise
            except SqliteError:  # SQLite before 3.26 (no table_xinfo) has no computed columns either
                computed = []
            if computed and computed[0][0]:
                # The check would compute such a column for every row, and an unchecked table
                # may be damaged in ways that make a scan repeat its rows.
                reason = (f"{self.what} cannot be read safely: its {stored} table has an unusual definition "
                          "(a computed column declared NOT NULL)")
            else:
                # The first result row only: the b-tree walk reports its findings first, and
                # fetching on would run the check's row-by-row pass over the damaged tree.
                result = self._query("SELECT * FROM pragma_quick_check(?, 'main') LIMIT 1", (stored,))
                if [tuple(r) for r in result] != [("ok",)]:
                    lines = [line.strip() for r in result for line in str(r[0] if r else "").splitlines()]
                    lines = [line for line in lines if line and not line.startswith("***")]
                    problem = (lines[0] if lines else "no result")[:200]
                    reason = f"{self.what} is damaged (SQLite's check of its {stored} table reports: {problem})"
            self._checked[stored] = reason
        reason = self._checked[stored]
        if reason is not None:
            raise SqliteError(reason)

    # -- public API ----------------------------------------------------------------------

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.set_progress_handler(None, 0)
            finally:
                self._con.close()
                self._con = None

    def __enter__(self) -> "SqliteDatabase":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def table(self, name: str) -> Optional[str]:
        """The stored spelling of table ``name`` (case-insensitive), ``None`` when absent."""
        return self.tables.get(name.lower())

    def has_table(self, name: str) -> bool:
        return name.lower() in self.tables

    def columns(self, table: str) -> Dict[str, str]:
        """``{lower-case name: stored name}`` of the table's ordinary columns ({} when absent)."""
        stored = self.table(table)
        if stored is None:
            return {}
        if stored not in self._columns:
            rows = self._query("SELECT name FROM pragma_table_info(?, 'main')", (stored,), max_rows=MAX_COLUMNS)
            self._columns[stored] = {str(r[0]).lower(): str(r[0]) for r in rows if isinstance(r[0], str)}
        return self._columns[stored]

    def count(self, table: str) -> Optional[int]:
        """The number of rows of ``table`` (``None`` when the table is absent)."""
        stored = self.table(table)
        if stored is None:
            return None
        self._verify(stored)
        rows = self._query(f"SELECT count(*) FROM main.{_quote(stored)} NOT INDEXED")
        return int(rows[0][0]) if rows else 0

    def select(self, table: str, columns: Sequence[str], required: Sequence[str] = (),
               max_rows: int = DEFAULT_MAX_ROWS, caps: Optional[Mapping[str, int]] = None,
               max_value: int = DEFAULT_MAX_VALUE,
               accept: Optional[Callable[[Tuple[Any, ...]], bool]] = None) -> Selection:
        """Rows of ``table`` with the named ``columns`` (in table order, at most ``max_rows``).

        Column names match case-insensitively.  ``required`` columns must exist, else the
        selection is not ``present``; other missing columns read as ``None``.  A TEXT or BLOB
        value longer than ``caps[column]`` (else ``max_value``) bytes reads as :data:`TOO_LARGE`.
        ``accept(row)`` filters the rows while they are fetched, so rows the caller does not need
        are never kept (they still count against ``max_rows`` and the data budget, as values
        over their cap do).  Raises :class:`SqliteError` when the table cannot be read (damaged,
        failing its check, or over a budget).
        """
        cols = tuple(columns)
        result = Selection(table=table, columns=cols)
        stored = self.table(table)
        if stored is None:
            result.missing = cols
            return result
        known = self.columns(stored)
        result.missing = tuple(c for c in cols if c.lower() not in known)
        if any(r.lower() not in known for r in required):
            return result
        self._verify(stored)
        result.present = True
        caps = {k.lower(): v for k, v in (caps or {}).items()}
        parts: List[str] = []
        layout: List[Tuple[str, Optional[int], int]] = []  # (column, index of its value, cap) per requested column
        for col in cols:
            name = known.get(col.lower())
            if name is None:
                layout.append((col, None, 0))
                continue
            q = _quote(name)
            cap = int(caps.get(col.lower(), max_value))
            layout.append((col, len(parts), cap))
            # bytes, not characters: length() of text stops at the first NUL character
            measure = f"CASE WHEN typeof({q}) IN ('text', 'blob') THEN length(CAST({q} AS BLOB)) ELSE 0 END"
            parts.append(f"CASE WHEN {measure} > {cap} THEN NULL ELSE {q} END")
            parts.append(measure)
        if not parts:
            parts = ["1"]
        sql = f"SELECT {', '.join(parts)} FROM main.{_quote(stored)} NOT INDEXED LIMIT ?"  # table order
        limit = max(0, max_rows)
        oversized: Dict[str, int] = {}
        rows: List[Tuple[Any, ...]] = []
        seen = 0
        for batch in self._batches(sql, (limit + 1,)):
            for record in batch:
                seen += 1
                if seen > limit:
                    result.truncated = True
                    break
                out = []
                for col, index, cap in layout:
                    if index is None:
                        out.append(None)
                        continue
                    value, size = record[index], record[index + 1]
                    size = size if isinstance(size, int) else 0
                    if isinstance(value, (str, bytes)):
                        size = max(size, len(value))
                    if size > 0:  # charged over the cap too: SQLite loaded the value to measure it
                        self._take_data(size)
                    if size > cap:
                        out.append(TOO_LARGE)
                        oversized[col] = oversized.get(col, 0) + 1
                    else:
                        out.append(value)
                row = tuple(out)
                if accept is None or accept(row):
                    rows.append(row)
        result.rows = rows
        result.oversized = oversized
        return result

    def _batches(self, sql: str, params: Sequence[Any]) -> Iterator[List[Tuple[Any, ...]]]:
        """The result of ``sql`` in batches of :data:`FETCH_BATCH` rows (errors as :class:`SqliteError`)."""
        self._check()
        sqlite3 = self._sqlite3
        try:
            cursor = self._con.execute(sql, tuple(params))
            while True:
                batch = cursor.fetchmany(FETCH_BATCH)
                if not batch:
                    return
                yield batch
        except (sqlite3.Error, OverflowError, MemoryError) as exc:
            if self.exhausted:
                raise self._too_complex() from exc
            raise SqliteError(f"{self.what} is damaged ({exc})") from exc
