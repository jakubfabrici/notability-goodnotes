"""Top-level conversion API: :func:`detect_format`, :func:`to_document`, :func:`convert`.

This module is the only entry point the CLI, the HTTP server and the browser worker use.
It stays import-light (the readers and writers are imported lazily) so that
``from gnnote.convert import convert, Options`` is cheap under Pyodide.

Formats are identified by ``"goodnotes"`` and ``"notability"``.  A ``.goodnotes`` file is a
ZIP with ``schema.pb`` / ``index.notes.pb`` at its root; a ``.note`` file is a ZIP holding
``<name>/Session.plist``.  :func:`detect_format` looks at the content first (so a renamed or
``.zip``-suffixed file still converts) and falls back to the file extension.
"""
from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import Document

GOODNOTES = "goodnotes"
NOTABILITY = "notability"

EXTENSIONS: Dict[str, str] = {GOODNOTES: ".goodnotes", NOTABILITY: ".note"}
PAPER_MODES = ("plain", "pdf")

__all__ = ["Options", "ConvertResult", "detect_format", "to_document", "convert",
           "document_stats", "other_format", "GOODNOTES", "NOTABILITY", "EXTENSIONS"]


@dataclass
class Options:
    """Conversion options (see ``docs/design.md`` section 3).

    ``paper``: ``"plain"`` turns built-in GoodNotes papers into Notability paper (pages scale
    to fit); ``"pdf"`` makes every page a PDF-backed Notability page.  User-imported PDFs are
    always carried as PDF backgrounds whatever this says.
    ``pressure``: keep per-anchor widths when writing Notability ink (``False``: constant).
    ``simplify``: RDP tolerance in pt applied to polylines before Bezier fitting (0 = off).
    ``ribbon``: experimental per-point-width GoodNotes strokes (falls back to flat strokes
    with a warning when unavailable).
    ``title``: override the note / notebook name.
    ``notability_page_width``: ``pageWidthInDocumentCoordsKey`` written into ``.note`` files.
    """

    paper: str = "plain"
    pressure: bool = True
    simplify: float = 0.0
    ribbon: bool = False
    title: Optional[str] = None
    notability_page_width: float = 574.0

    def validate(self) -> None:
        """Raise :class:`ValueError` for values the writers would not understand."""
        if self.paper not in PAPER_MODES:
            raise ValueError(f"paper must be one of {', '.join(PAPER_MODES)}; got {self.paper!r}")
        if self.simplify < 0:
            raise ValueError("simplify must be >= 0")
        if self.notability_page_width <= 0:
            raise ValueError("notability_page_width must be positive")


@dataclass
class ConvertResult:
    """What :func:`convert` returns: the output bytes plus everything a UI wants to show."""

    data: bytes
    filename: str
    source_format: str
    target_format: str
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)


def other_format(fmt: str) -> str:
    """The conversion target for a source format."""
    if fmt == GOODNOTES:
        return NOTABILITY
    if fmt == NOTABILITY:
        return GOODNOTES
    raise ValueError(f"unknown format {fmt!r}")


def _format_from_extension(filename: str) -> Optional[str]:
    ext = os.path.splitext(os.path.basename(filename or ""))[1].lower()
    for fmt, known in EXTENSIONS.items():
        if ext == known:
            return fmt
    return None


def _format_from_content(data: bytes) -> Optional[str]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes")
    data = bytes(data)
    if not data.startswith(b"PK"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return None
    if any(n in ("schema.pb", "index.notes.pb") for n in names):
        return GOODNOTES
    if any(n.startswith("notes/") or n.startswith("attachments/") for n in names) and \
            any(n.endswith(".pb") for n in names):
        return GOODNOTES
    for n in names:
        if n == "Session.plist" or n.endswith("/Session.plist"):
            return NOTABILITY
    return None


def detect_format(filename: str, data: bytes) -> str:
    """Return ``"goodnotes"`` or ``"notability"`` for ``data`` named ``filename``.

    The ZIP content decides when it is recognisable; otherwise the extension
    (``.goodnotes`` / ``.note``) decides.  :class:`ValueError` when neither matches.
    """
    by_content = _format_from_content(data)
    if by_content is not None:
        return by_content
    by_ext = _format_from_extension(filename)
    if by_ext is not None:
        return by_ext
    raise ValueError(f"{os.path.basename(filename) or 'input'} is neither a .goodnotes nor a .note file")


def to_document(data: bytes, filename: str) -> Document:
    """Read either format into the shared :class:`~gnnote.model.Document` model."""
    fmt = detect_format(filename, data)
    if fmt == GOODNOTES:
        from .goodnotes.reader import read_goodnotes
        doc = read_goodnotes(bytes(data))
    else:
        from .notability.reader import read_note
        doc = read_note(bytes(data))
    doc.source_format = fmt
    return doc


def document_stats(doc: Document) -> Dict[str, int]:
    """Counts shown to the user: pages, strokes, images, texts and distinct user PDFs.

    ``pdfs`` counts the distinct PDF files shown behind pages that are not the source app's
    stock paper (those are what survive a conversion as PDF backgrounds).
    """
    pdf_ids = {p.background.pdf_id for p in doc.pages
               if p.background is not None and not p.template_is_builtin and p.background.pdf_id in doc.pdfs}
    return {
        "pages": len(doc.pages),
        "strokes": sum(len(p.strokes) for p in doc.pages),
        "images": sum(len(p.images) for p in doc.pages),
        "texts": sum(len(p.texts) for p in doc.pages),
        "pdfs": len(pdf_ids),
    }


def output_filename(filename: str, target: str) -> str:
    """``filename`` with its extension swapped for the target format's one."""
    base = os.path.basename(filename or "") or "converted"
    stem, ext = os.path.splitext(base)
    if not stem:  # e.g. ".note"
        stem = base
    return stem + EXTENSIONS[target]


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def convert(data: bytes, filename: str, options: Optional[Options] = None) -> ConvertResult:
    """Convert ``data`` (a ``.goodnotes`` or ``.note`` file) to the other format.

    Raises :class:`ValueError` when the input is neither format or the options are invalid;
    anything the readers cannot interpret becomes a warning rather than an exception.
    """
    options = options or Options()
    options.validate()
    source = detect_format(filename, data)
    target = other_format(source)
    doc = to_document(data, filename)
    if options.title:
        doc.title = str(options.title)
    if target == NOTABILITY:
        from .notability.writer import write_note
        out = write_note(doc, options)
    else:
        from .goodnotes.writer import write_goodnotes
        out = write_goodnotes(doc, options)
    return ConvertResult(
        data=out,
        filename=output_filename(filename, target),
        source_format=source,
        target_format=target,
        warnings=_dedupe(list(doc.warnings)),
        stats=document_stats(doc),
    )
