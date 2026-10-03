"""Registry of the note formats gnnote can read and write.

Every supported app is one :class:`NoteFormat` entry: its id, display name, file extensions,
a content sniffer and the dotted paths of its reader and writer.  Readers and writers are
imported lazily, so importing this module (and :mod:`gnnote.convert`) stays cheap under
Pyodide.  Adding an app means writing ``reader.py`` / ``writer.py`` for it and adding one
entry to :data:`FORMATS`; the CLI, the HTTP server and the web page pick it up from here
(the web page through ``web/formats.js``, which ``scripts/build_web.py`` regenerates).
"""
from __future__ import annotations

import importlib
import io
import os
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .model import Document

__all__ = ["NoteFormat", "FORMATS", "get", "readable", "writable", "sniff_zip_names",
           "format_for_extension", "default_target", "formats_info", "web_formats_js"]

Sniffer = Callable[[bytes, Optional[List[str]]], bool]


@dataclass(frozen=True)
class NoteFormat:
    """One note app's file format.

    ``sniff(data, names)`` decides from the content; ``names`` is the ZIP member list when the
    input is a ZIP archive, else ``None``.  ``reader`` / ``writer`` are ``"module:function"``
    paths; ``None`` means the direction is not supported for this app.
    """

    id: str
    name: str
    extension: str
    input_extensions: Tuple[str, ...]
    sniff: Sniffer
    reader: Optional[str] = None
    writer: Optional[str] = None

    @property
    def readable(self) -> bool:
        return self.reader is not None

    @property
    def writable(self) -> bool:
        return self.writer is not None

    def read(self, data: bytes) -> Document:
        if self.reader is None:
            raise ValueError(f"{self.name} files cannot be read")
        doc = _load(self.reader)(bytes(data))
        doc.source_format = self.id
        return doc

    def write(self, doc: Document, options: Any) -> bytes:
        if self.writer is None:
            raise ValueError(f"{self.name} files cannot be written")
        return _load(self.writer)(doc, options)


def _load(path: str) -> Callable[..., Any]:
    module, _, func = path.partition(":")
    return getattr(importlib.import_module(module), func)


def _sniff_goodnotes(data: bytes, names: Optional[List[str]]) -> bool:
    if not names:
        return False
    if any(n in ("schema.pb", "index.notes.pb") for n in names):
        return True
    return any(n.startswith(("notes/", "attachments/")) for n in names) and \
        any(n.endswith(".pb") for n in names)


def _sniff_notability(data: bytes, names: Optional[List[str]]) -> bool:
    return bool(names) and any(n == "Session.plist" or n.endswith("/Session.plist") for n in names)


def _sniff_nebo(data: bytes, names: Optional[List[str]]) -> bool:
    """MyScript Notes / Nebo: ``rel.json`` plus BINK ink or BDOM layout parts."""
    if not names or "rel.json" not in names:
        return False
    return any(n.startswith("pages/") and n.endswith(("/ink.bink", "/page.bdom")) for n in names) or \
        "index.bdom" in names


def _sniff_flexcil(data: bytes, names: Optional[List[str]]) -> bool:
    """Flexcil: a ``.flx`` document (``info`` + ``pages.index``) or a ``.flex`` backup of them."""
    if not names:
        return False
    if "pages.index" in names and "info" in names:
        return True
    return any(n.lower().endswith(".flx") or n == "documents.list" or n.endswith("/documents.list")
               for n in names)


def _sniff_remarkable(data: bytes, names: Optional[List[str]]) -> bool:
    """reMarkable: a v6 page (``.rm`` header) or an ``.rmdoc`` (``<uuid>.content`` + metadata/pages)."""
    if names is None:
        return data.startswith(b"reMarkable .lines file, version=")
    top = [n for n in names if "/" not in n]
    return any(n.endswith(".content") for n in top) and (
        any(n.endswith(".metadata") for n in top) or any(n.endswith(".rm") for n in names))


FORMATS: Dict[str, NoteFormat] = {
    f.id: f
    for f in (
        NoteFormat(
            id="goodnotes", name="GoodNotes", extension=".goodnotes",
            input_extensions=(".goodnotes",), sniff=_sniff_goodnotes,
            reader="gnnote.goodnotes.reader:read_goodnotes",
            writer="gnnote.goodnotes.writer:write_goodnotes",
        ),
        NoteFormat(
            id="notability", name="Notability", extension=".note",
            input_extensions=(".note",), sniff=_sniff_notability,
            reader="gnnote.notability.reader:read_note",
            writer="gnnote.notability.writer:write_note",
        ),
        NoteFormat(
            id="nebo", name="MyScript Notes (Nebo)", extension=".nebo",
            input_extensions=(".nebo",), sniff=_sniff_nebo,
            reader="gnnote.nebo.reader:read_nebo",
        ),
        NoteFormat(
            id="flexcil", name="Flexcil", extension=".flx",
            input_extensions=(".flx", ".flex"), sniff=_sniff_flexcil,
            reader="gnnote.flexcil.reader:read_flexcil",
        ),
        NoteFormat(
            id="remarkable", name="reMarkable", extension=".rmdoc",
            input_extensions=(".rmdoc", ".rm"), sniff=_sniff_remarkable,
            reader="gnnote.remarkable.reader:read_remarkable",
        ),
    )
}

# Where a file goes when the caller names no target: the two original formats swap, every
# other app converts to Notability (the app this project was started for).
_DEFAULT_TARGETS = {"goodnotes": "notability", "notability": "goodnotes"}
_FALLBACK_TARGET = "notability"


def get(format_id: str) -> NoteFormat:
    """The registered format ``format_id``; :class:`ValueError` for an unknown id."""
    try:
        return FORMATS[format_id]
    except KeyError:
        known = ", ".join(FORMATS)
        raise ValueError(f"unknown format {format_id!r}; known formats: {known}") from None


def readable() -> List[NoteFormat]:
    return [f for f in FORMATS.values() if f.readable]


def writable() -> List[NoteFormat]:
    return [f for f in FORMATS.values() if f.writable]


def sniff_zip_names(data: bytes) -> Optional[List[str]]:
    """Member names when ``data`` is a readable ZIP archive, else ``None``."""
    if not data.startswith(b"PK"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.namelist()
    except (zipfile.BadZipFile, NotImplementedError, OSError, ValueError, RuntimeError):
        return None


def format_for_extension(filename: str) -> Optional[NoteFormat]:
    ext = os.path.splitext(os.path.basename(filename or ""))[1].lower()
    for fmt in FORMATS.values():
        if ext in fmt.input_extensions:
            return fmt
    return None


def default_target(source_id: str) -> str:
    """The target used when none is given (never the source itself)."""
    target = _DEFAULT_TARGETS.get(source_id, _FALLBACK_TARGET)
    if target == source_id or target not in FORMATS or not FORMATS[target].writable:
        for fmt in writable():
            if fmt.id != source_id:
                return fmt.id
        raise ValueError(f"no format to convert {source_id!r} to")
    return target


def formats_info() -> List[Dict[str, Any]]:
    """Plain data describing every format, for the web page and ``/api/health``."""
    return [
        {
            "id": f.id,
            "name": f.name,
            "extension": f.extension,
            "inputExtensions": list(f.input_extensions),
            "readable": f.readable,
            "writable": f.writable,
            "defaultTarget": default_target(f.id) if f.readable else None,
        }
        for f in FORMATS.values()
    ]


def web_formats_js(info: Optional[Sequence[Dict[str, Any]]] = None) -> str:
    """The text of ``web/formats.js``: the registry as an ES module for the browser UI."""
    import json

    payload = json.dumps(list(info if info is not None else formats_info()), indent=2, ensure_ascii=False)
    return (
        "// Generated from gnnote/formats.py by scripts/build_web.py; do not edit by hand.\n"
        "// tests/test_formats.py checks that this file matches the Python registry.\n"
        f"export const FORMATS = {payload};\n"
    )
