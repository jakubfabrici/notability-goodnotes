"""Command-line interface: ``python -m gnnote`` / ``gnnote``.

Sub-commands::

    gnnote convert IN [-o OUT] [--paper plain|pdf] [--no-pressure] [--simplify PT]
                   [--ribbon] [--title T]
    gnnote info FILE [--json]
    gnnote batch DIR [-o OUTDIR] [--to goodnotes|notability] [--paper plain|pdf]
                 [--no-pressure] [--simplify PT]

Exit codes: 0 success, 1 a conversion failed (or a file could not be read), 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .convert import EXTENSIONS, GOODNOTES, NOTABILITY, Options, convert, detect_format, document_stats, to_document

__all__ = ["main", "build_parser", "describe"]


class _ArgumentParser(argparse.ArgumentParser):
    """argparse that reports usage errors with exit status 2 without raising SystemExit."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(f"{self.prog}: error: {message}\n{self.format_usage()}")


class _UsageError(Exception):
    pass


def _add_write_options(parser: argparse.ArgumentParser, ribbon: bool = True, title: bool = True) -> None:
    parser.add_argument("--paper", choices=("plain", "pdf"), default="plain",
                        help="GoodNotes -> Notability: 'plain' turns stock paper into Notability paper "
                             "(default); 'pdf' keeps every page as a PDF-backed page")
    parser.add_argument("--no-pressure", dest="pressure", action="store_false",
                        help="write constant-width Notability strokes instead of per-point widths")
    parser.add_argument("--simplify", type=float, default=0.0, metavar="PT",
                        help="simplify polylines with this tolerance in pt before fitting (default 0 = off)")
    if ribbon:
        parser.add_argument("--ribbon", action="store_true",
                            help="experimental: per-point-width GoodNotes strokes (falls back to flat)")
    if title:
        parser.add_argument("--title", default=None, help="override the note / notebook name")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="gnnote",
                             description="Convert between GoodNotes (.goodnotes) and Notability (.note) "
                                         "files, keeping handwriting editable.")
    parser.add_argument("--version", action="version", version=f"gnnote {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p_convert = sub.add_parser("convert", help="convert one file to the other format")
    p_convert.add_argument("input", metavar="IN", help=".goodnotes or .note file")
    p_convert.add_argument("-o", "--output", metavar="OUT", default=None,
                           help="output file or directory (default: next to IN, extension swapped)")
    _add_write_options(p_convert)

    p_info = sub.add_parser("info", help="describe a file: format, title, pages, counts, warnings")
    p_info.add_argument("file", metavar="FILE")
    p_info.add_argument("--json", action="store_true", help="print a JSON object instead of text")

    p_batch = sub.add_parser("batch", help="convert every file in a directory")
    p_batch.add_argument("directory", metavar="DIR")
    p_batch.add_argument("-o", "--output", metavar="OUTDIR", default=None,
                         help="directory for the converted files (default: DIR)")
    p_batch.add_argument("--to", dest="target", choices=(GOODNOTES, NOTABILITY), default=None,
                         help="only produce this format (default: convert both kinds of file)")
    _add_write_options(p_batch, ribbon=False, title=False)
    return parser


def _options_from_args(args: argparse.Namespace) -> Options:
    return Options(paper=args.paper, pressure=args.pressure, simplify=args.simplify,
                   ribbon=bool(getattr(args, "ribbon", False)), title=getattr(args, "title", None))


def _output_path(source: Path, filename: str, output: Optional[str]) -> Path:
    if output is None:
        return source.with_name(filename)
    out = Path(output)
    if out.is_dir() or output.endswith(os.sep) or (os.altsep and output.endswith(os.altsep)):
        return out / filename
    return out


def _print_warnings(warnings: Sequence[str], stream: Any) -> None:
    for w in warnings:
        print(f"warning: {w}", file=stream)


def _cmd_convert(args: argparse.Namespace, out: Any, err: Any) -> int:
    source = Path(args.input)
    try:
        data = source.read_bytes()
    except OSError as exc:
        print(f"error: cannot read {source}: {exc}", file=err)
        return 1
    try:
        result = convert(data, source.name, _options_from_args(args))
    except ValueError as exc:
        print(f"error: {exc}", file=err)
        return 1
    target = _output_path(source, result.filename, args.output)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(result.data)
    except OSError as exc:
        print(f"error: cannot write {target}: {exc}", file=err)
        return 1
    s = result.stats
    print(f"{source.name} -> {target} ({result.source_format} -> {result.target_format}; "
          f"{s['pages']} pages, {s['strokes']} strokes, {s['images']} images, {s['texts']} texts, "
          f"{s['pdfs']} PDFs)", file=out)
    _print_warnings(result.warnings, err)
    return 0


def describe(data: bytes, filename: str) -> Dict[str, Any]:
    """The ``info`` sub-command's view of a file as a JSON-serialisable dict."""
    fmt = detect_format(filename, data)
    doc = to_document(data, filename)
    pages: List[Dict[str, Any]] = []
    for index, page in enumerate(doc.pages):
        entry: Dict[str, Any] = {
            "index": index + 1,
            "width": round(page.width, 3),
            "height": round(page.height, 3),
            "paper": page.paper,
            "builtin_template": bool(page.template_is_builtin),
            "background": None,
            "strokes": len(page.strokes),
            "images": len(page.images),
            "texts": len(page.texts),
        }
        if page.background is not None:
            entry["background"] = {"pdf": page.background.pdf_id, "page": page.background.page_index + 1}
        pages.append(entry)
    return {
        "file": filename,
        "format": fmt,
        "title": doc.title,
        "pages": pages,
        "totals": document_stats(doc),
        "warnings": list(doc.warnings),
    }


def _format_info(info: Dict[str, Any]) -> str:
    lines = [f"File:    {info['file']}",
             f"Format:  {info['format']}",
             f"Title:   {info['title']}",
             f"Pages:   {len(info['pages'])}"]
    for p in info["pages"]:
        bg = p["background"]
        if bg is None:
            paper = f"paper ({p['paper']})"
        elif p["builtin_template"]:
            paper = f"stock paper ({p['paper']})"
        else:
            paper = f"PDF {bg['pdf']} page {bg['page']}"
        lines.append(f"  {p['index']:>3}  {p['width']:g} x {p['height']:g} pt  "
                     f"strokes {p['strokes']:>5}  images {p['images']:>3}  texts {p['texts']:>3}  {paper}")
    t = info["totals"]
    lines.append(f"Totals:  strokes {t['strokes']}, images {t['images']}, texts {t['texts']}, PDFs {t['pdfs']}")
    if info["warnings"]:
        lines.append("Warnings:")
        lines += [f"  - {w}" for w in info["warnings"]]
    return "\n".join(lines)


def _cmd_info(args: argparse.Namespace, out: Any, err: Any) -> int:
    source = Path(args.file)
    try:
        data = source.read_bytes()
    except OSError as exc:
        print(f"error: cannot read {source}: {exc}", file=err)
        return 1
    try:
        info = describe(data, source.name)
    except ValueError as exc:
        print(f"error: {exc}", file=err)
        return 1
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2), file=out)
    else:
        print(_format_info(info), file=out)
    return 0


def _cmd_batch(args: argparse.Namespace, out: Any, err: Any) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"error: {directory} is not a directory", file=err)
        return 1
    out_dir = Path(args.output) if args.output else directory
    wanted = {EXTENSIONS[GOODNOTES]: NOTABILITY, EXTENSIONS[NOTABILITY]: GOODNOTES}
    if args.target:
        wanted = {ext: tgt for ext, tgt in wanted.items() if tgt == args.target}
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in wanted)
    if not files:
        print(f"no {' or '.join(sorted(wanted))} files in {directory}", file=out)
        return 0
    options = _options_from_args(args)
    failed = 0
    for path in files:
        try:
            result = convert(path.read_bytes(), path.name, options)
            target = out_dir / result.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(result.data)
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            failed += 1
            print(f"FAIL  {path.name}: {exc.__class__.__name__}: {exc}", file=out)
            continue
        s = result.stats
        print(f"ok    {path.name} -> {target.name} ({s['pages']} pages, {s['strokes']} strokes"
              f"{', ' + str(len(result.warnings)) + ' warnings' if result.warnings else ''})", file=out)
        for w in result.warnings:
            print(f"      warning: {w}", file=err)
    print(f"{len(files) - failed} converted, {failed} failed", file=out)
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI; returns the exit status instead of calling :func:`sys.exit`."""
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except _UsageError as exc:
        print(str(exc), file=sys.stderr, end="")
        return 2
    except SystemExit as exc:  # --help / --version
        return int(exc.code or 0)
    handlers = {"convert": _cmd_convert, "info": _cmd_info, "batch": _cmd_batch}
    return handlers[args.command](args, sys.stdout, sys.stderr)


if __name__ == "__main__":  # pragma: no cover - ``python -m gnnote`` goes through __main__
    sys.exit(main())
