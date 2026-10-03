"""Top-level conversion API: :func:`detect_format`, :func:`to_document`, :func:`convert`.

This module is the only entry point the CLI, the HTTP server and the browser worker use.
It stays import-light (the readers and writers are imported lazily) so that
``from gnnote.convert import convert, Options`` is cheap under Pyodide.

Formats are the ids registered in :mod:`gnnote.formats` (``"goodnotes"``, ``"notability"``,
...).  :func:`detect_format` asks every format's content sniffer first (so a renamed or
``.zip``-suffixed file still converts) and falls back to the file extension.  The target is
``Options.target``; without one, GoodNotes and Notability swap and every other app goes to
Notability (:func:`gnnote.formats.default_target`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import formats as _formats
from .model import Document

GOODNOTES = "goodnotes"
NOTABILITY = "notability"

EXTENSIONS: Dict[str, str] = {f.id: f.extension for f in _formats.FORMATS.values()}
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
    ``target``: id of the output format (see :mod:`gnnote.formats`); ``None`` picks the
    default target for the source format.
    """

    paper: str = "plain"
    pressure: bool = True
    simplify: float = 0.0
    ribbon: bool = False
    title: Optional[str] = None
    notability_page_width: float = 574.0
    target: Optional[str] = None

    def validate(self) -> None:
        """Raise :class:`ValueError` for values the writers would not understand."""
        if self.target is not None:
            fmt = _formats.get(self.target)
            if not fmt.writable:
                raise ValueError(f"{fmt.name} files cannot be written yet")
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
    """The default conversion target for a source format."""
    _formats.get(fmt)
    return _formats.default_target(fmt)


def _format_from_content(data: bytes) -> Optional[str]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes")
    data = bytes(data)
    names = _formats.sniff_zip_names(data)
    for fmt in _formats.readable():
        try:
            if fmt.sniff(data, names):
                return fmt.id
        except Exception:  # noqa: BLE001 - a sniffer must never break detection of the others
            continue
    return None


def _supported_extensions() -> str:
    exts = [e for f in _formats.readable() for e in f.input_extensions]
    return ", ".join(exts[:-1]) + (" or " + exts[-1] if len(exts) > 1 else "".join(exts))


def detect_format(filename: str, data: bytes) -> str:
    """Return the format id of ``data`` named ``filename`` (e.g. ``"goodnotes"``).

    The content decides when a format recognises it; otherwise the file extension decides.
    :class:`ValueError` when neither matches a readable format.
    """
    by_content = _format_from_content(data)
    if by_content is not None:
        return by_content
    by_ext = _formats.format_for_extension(filename)
    if by_ext is not None and by_ext.readable:
        return by_ext.id
    raise ValueError(f"{os.path.basename(filename) or 'input'} is not a supported note file "
                     f"(expected {_supported_extensions()})")


def _name_after_file(doc: Document, filename: str) -> None:
    """Give an untitled document the file's name (a OneNote section is named by its file)."""
    if not (doc.title or "").strip():
        doc.title = os.path.splitext(os.path.basename(filename or ""))[0] or "Untitled"


def to_document(data: bytes, filename: str) -> Document:
    """Read any supported format into the shared :class:`~gnnote.model.Document` model."""
    fmt = _formats.get(detect_format(filename, data))
    doc = fmt.read(bytes(data))
    _name_after_file(doc, filename)
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
    return stem + _formats.get(target).extension


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def convert(data: bytes, filename: str, options: Optional[Options] = None) -> ConvertResult:
    """Convert ``data`` to ``options.target`` (default: see :func:`other_format`).

    Raises :class:`ValueError` when the input is not a supported format, the target equals
    the source or the options are invalid; anything the readers cannot interpret becomes a
    warning rather than an exception.
    """
    options = options or Options()
    options.validate()
    source = detect_format(filename, data)
    target = options.target or _formats.default_target(source)
    if target == source:
        raise ValueError(f"the file already is a {_formats.get(source).name} file; choose another target")
    doc = _formats.get(source).read(bytes(data))
    _name_after_file(doc, filename)
    if options.title:
        doc.title = str(options.title)
    out = _formats.get(target).write(doc, options)
    return ConvertResult(
        data=out,
        filename=output_filename(filename, target),
        source_format=source,
        target_format=target,
        warnings=_dedupe(list(doc.warnings)),
        stats=document_stats(doc),
    )
