"""Small self-hosted HTTP server for gnnote: static web UI + conversion API.

Run with ``python -m gnnote.server --host 0.0.0.0 --port 8000 [--dist DIR]``.
Standard library only (``http.server.ThreadingHTTPServer``); no third-party code.

What the server speaks
----------------------

* **Static files** from ``--dist`` (default: ``dist/`` next to the package root if it
  exists, else ``web/``).  ``/`` maps to ``index.html``.  MIME types are chosen from a
  fixed table (``.mjs``/``.js`` -> ``text/javascript``, ``.wasm`` ->
  ``application/wasm``, ``.zip``, ``.json``, ``.html``, ``.css``, ``.svg`` ...) with
  ``mimetypes`` as a fallback.  Paths are resolved inside the root only; anything
  escaping it is a 404.  No COOP/COEP headers (Pyodide does not need SharedArrayBuffer).
* ``GET /gnnote.zip`` -- a ZIP archive of the *installed* ``gnnote`` package (``.py``
  files only, member names ``gnnote/...``) built on first request and cached in
  memory; it is what the browser worker unpacks on ``sys.path``.
* ``GET /version.json`` -- served from disk when the build wrote one, else generated.
* ``GET /api/health`` -> ``{"ok": true, "version": ..., "maxUpload": ..., "formats": [...]}``.
* ``POST /api/convert`` -- ``multipart/form-data`` with a ``file`` part (the
  ``.goodnotes``/``.note`` bytes, filename taken from the part) and optional parameters
  ``to`` (target format id), ``paper`` (``plain``|``pdf``), ``pressure`` (bool),
  ``simplify`` (float pt), ``title``
  (string), given either as form fields or as query-string parameters (form fields
  win).  Bodies above ``MAX_UPLOAD`` (300 MB) are refused with 413 before being read.
  On success the converted file is returned as ``application/octet-stream`` with
  ``Content-Disposition: attachment`` and the headers ``X-GnNote-Warnings`` (JSON list,
  ASCII-safe), ``X-GnNote-Stats`` (JSON object), ``X-GnNote-Source-Format`` and
  ``X-GnNote-Target-Format``.  Errors are JSON ``{"error": "...", "status": N}``; a 500
  also carries ``"traceback"``.

Multipart bodies are parsed by :func:`parse_multipart`, a byte-oriented parser written
here because :mod:`cgi` is gone in Python 3.13: parts are delimited by
``CRLF--boundary``, each part is a header block (``Content-Disposition`` with
``name``/``filename`` parameters, optional ``Content-Type``) followed by ``CRLFCRLF`` and
the raw payload; the closing delimiter is ``--boundary--``.

The converter (``gnnote.convert``) is imported lazily inside the request handler via
:func:`import_convert_module`, so this module imports without it and tests can install a
fake module under ``sys.modules["gnnote.convert"]`` (exposing ``Options`` and
``convert(data, filename, options)``).
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import re
import sys
import threading
import traceback
import zipfile
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

from gnnote import __version__

MAX_UPLOAD = 300 * 1024 * 1024
"""Largest accepted request body for ``POST /api/convert`` (bytes)."""

MAX_HEADER_JSON = 60_000
"""Longest JSON value placed into a response header; longer lists are truncated."""

MIME_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
    ".zip": "application/zip",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".data": "application/octet-stream",
    ".whl": "application/zip",
    ".tar": "application/x-tar",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent

_zip_lock = threading.Lock()
_zip_cache: Dict[str, bytes] = {}


# --------------------------------------------------------------------------- helpers

def content_type_for(path: str) -> str:
    """MIME type for a file name, from :data:`MIME_TYPES` then :mod:`mimetypes`."""
    ext = os.path.splitext(path)[1].lower()
    if ext in MIME_TYPES:
        return MIME_TYPES[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def default_root() -> Path:
    """``dist/`` when it exists next to the package, otherwise ``web/``.

    For a package installed outside the source tree the same two names are tried in
    the current working directory before giving up (``web/`` next to the package).
    """
    for base in (_PROJECT_ROOT, Path.cwd()):
        for name in ("dist", "web"):
            candidate = base / name
            if candidate.is_dir() and (candidate / "index.html").is_file():
                return candidate
    return _PROJECT_ROOT / "web"


def build_package_zip() -> bytes:
    """ZIP the installed ``gnnote`` package (``.py`` only) as ``gnnote/...`` members.

    The result is cached for the lifetime of the process.
    """
    with _zip_lock:
        cached = _zip_cache.get("zip")
        if cached is not None:
            return cached
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(_PACKAGE_DIR.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                rel = path.relative_to(_PACKAGE_DIR).as_posix()
                info = zipfile.ZipInfo("gnnote/" + rel, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, path.read_bytes())
        data = buf.getvalue()
        _zip_cache["zip"] = data
        return data


def import_convert_module() -> Any:
    """Lazily import ``gnnote.convert`` (tests may put a fake into ``sys.modules``)."""
    import importlib

    return importlib.import_module("gnnote.convert")


@dataclass
class Part:
    """One body part of a ``multipart/form-data`` request."""

    name: str
    filename: Optional[str]
    content_type: Optional[str]
    data: bytes


class MultipartError(ValueError):
    """Malformed multipart body or missing boundary."""


_PARAM_RE = re.compile(r';\s*([A-Za-z0-9_*-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]*))')


def _header_params(value: str) -> Tuple[str, Dict[str, str]]:
    """Split ``form-data; name="a"; filename="b"`` into the main value and parameters."""
    main, sep, rest = value.partition(";")
    params: Dict[str, str] = {}
    if sep:
        for match in _PARAM_RE.finditer(";" + rest):
            key = match.group(1).lower()
            if match.group(2) is not None:
                val = re.sub(r"\\(.)", r"\1", match.group(2))
            else:
                val = (match.group(3) or "").strip()
            if key.endswith("*"):
                # RFC 5987 ``filename*=UTF-8''...``
                key = key[:-1]
                charset, _, encoded = val.partition("'")
                _, _, encoded = encoded.partition("'")
                try:
                    val = unquote(encoded, encoding=charset or "utf-8", errors="replace")
                except LookupError:
                    val = unquote(encoded, errors="replace")
                params[key] = val
            elif key not in params:
                params[key] = val
    return main.strip().lower(), params


def multipart_boundary(content_type: Optional[str]) -> Optional[bytes]:
    """The boundary from a ``multipart/form-data; boundary=...`` header, or None."""
    if not content_type:
        return None
    main, params = _header_params(content_type)
    if main != "multipart/form-data":
        return None
    boundary = params.get("boundary")
    if not boundary:
        return None
    return boundary.encode("ascii", "replace")


def _find_delimiter(body: bytes, delim: bytes, start: int) -> Optional[Tuple[int, int]]:
    """``(data_end, after)`` of the next real delimiter at or after ``start``.

    RFC 2046: a delimiter is a line break, ``--boundary``, optional transport padding and a
    line break (or ``--`` for the closing one).  ``--boundary`` followed by anything else is
    payload and is skipped.  ``data_end`` excludes the line break before the delimiter.
    """
    pos = start
    n = len(body)
    while True:
        i = body.find(delim, pos)
        if i < 0:
            return None
        after = i + len(delim)
        while after < n and body[after] in b" \t":
            after += 1
        tail = body[after:after + 2]
        if tail == b"--" or tail == b"\r\n" or tail[:1] == b"\n":
            end = i
            if body[end - 2:end] == b"\r\n":
                end -= 2
            elif body[end - 1:end] == b"\n":
                end -= 1
            return max(end, start), after
        pos = i + len(delim)


def parse_multipart(body: bytes, boundary: bytes) -> List[Part]:
    """Parse a ``multipart/form-data`` body into its parts (byte-oriented, stdlib only)."""
    delim = b"--" + boundary
    parts: List[Part] = []
    found = _find_delimiter(body, delim, 0)
    if found is None:
        raise MultipartError("multipart body does not start with the boundary")
    _, pos = found
    while True:
        if body[pos:pos + 2] == b"--":
            break
        if body[pos:pos + 2] == b"\r\n":
            pos += 2
        elif body[pos:pos + 1] == b"\n":
            pos += 1
        else:
            raise MultipartError("unexpected bytes after the multipart boundary")
        header_end = body.find(b"\r\n\r\n", pos)
        sep_len = 4
        alt = body.find(b"\n\n", pos)
        if header_end < 0 or (0 <= alt < header_end):
            header_end, sep_len = alt, 2
        if header_end < 0:
            raise MultipartError("multipart part without a header block")
        header_text = body[pos:header_end].decode("utf-8", "replace")
        data_start = header_end + sep_len
        found = _find_delimiter(body, delim, data_start)
        if found is None:
            raise MultipartError("unterminated multipart part")
        data_end, pos = found
        data = body[data_start:data_end]
        name = ""
        filename: Optional[str] = None
        ctype: Optional[str] = None
        for line in header_text.replace("\r\n", "\n").split("\n"):
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key = key.strip().lower()
            if key == "content-disposition":
                _, params = _header_params(value)
                name = params.get("name", "")
                filename = params.get("filename")
            elif key == "content-type":
                ctype = value.strip()
        parts.append(Part(name=name, filename=filename, content_type=ctype, data=data))
    return parts


_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


def parse_bool(value: str, default: bool = True) -> bool:
    text = value.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return default if text == "" else False
    raise ValueError(f"not a boolean: {value!r}")


def build_options(params: Dict[str, str]) -> Dict[str, Any]:
    """Validate request parameters into keyword arguments for ``Options``.

    Raises ``ValueError`` with a user-facing message on bad input.
    """
    kwargs: Dict[str, Any] = {}
    paper = params.get("paper", "").strip().lower()
    if paper:
        if paper not in ("plain", "pdf"):
            raise ValueError("paper must be 'plain' or 'pdf'")
        kwargs["paper"] = paper
    if "pressure" in params:
        kwargs["pressure"] = parse_bool(params["pressure"])
    simplify = params.get("simplify", "").strip()
    if simplify:
        try:
            value = float(simplify)
        except ValueError:
            raise ValueError("simplify must be a number (points)") from None
        if not (value >= 0.0) or value > 1000.0:
            raise ValueError("simplify must be between 0 and 1000 points")
        kwargs["simplify"] = value
    if "ribbon" in params:
        kwargs["ribbon"] = parse_bool(params["ribbon"], default=False)
    title = params.get("title", "").strip()
    if title:
        kwargs["title"] = title[:200]
    target = (params.get("to") or params.get("target") or "").strip().lower()
    if target:
        from . import formats as _formats  # stdlib-only, cheap
        ids = [f.id for f in _formats.writable()]
        if target not in ids:
            known = _formats.FORMATS.get(target)
            prefix = f"{known.name} files can be read but not written; " if known is not None else ""
            raise ValueError(prefix + "to must be one of " + ", ".join(ids))
        kwargs["target"] = target
    return kwargs


def header_json(value: Any) -> str:
    """ASCII-safe single-line JSON for a response header, truncated when huge."""
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if len(text) <= MAX_HEADER_JSON or not isinstance(value, list):
        return text
    kept: List[Any] = []
    size = 2
    for item in value:
        piece = json.dumps(item, ensure_ascii=True, separators=(",", ":"))
        if size + len(piece) + 60 > MAX_HEADER_JSON:
            break
        kept.append(item)
        size += len(piece) + 1
    kept.append(f"... {len(value) - len(kept)} more warnings omitted from the header")
    return json.dumps(kept, ensure_ascii=True, separators=(",", ":"))


def content_disposition(filename: str) -> str:
    """``attachment`` header with an ASCII fallback and an RFC 5987 UTF-8 name."""
    safe = filename.replace("\\", "_").replace('"', "_").replace("\r", "").replace("\n", "")
    ascii_name = safe.encode("ascii", "replace").decode("ascii").replace("?", "_") or "output"
    value = f'attachment; filename="{ascii_name}"'
    if ascii_name != safe:
        value += "; filename*=UTF-8''" + quote(safe, safe="")
    return value


# --------------------------------------------------------------------------- handler

class GnNoteHandler(BaseHTTPRequestHandler):
    """Request handler; ``server.root`` is the static directory."""

    server_version = f"gnnote/{__version__}"
    protocol_version = "HTTP/1.1"
    timeout = 60  # seconds a request may stay idle: a half-sent upload must not hold a thread forever

    # ---- plumbing -------------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None, head_only: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only and self.command != "HEAD":  # RFC 9110: no body on HEAD, whichever branch answers
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Dict[str, Any],
                   extra: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _send_error_json(self, status: int, message: str, **more: Any) -> None:
        payload: Dict[str, Any] = {"error": message, "status": int(status)}
        payload.update(more)
        self._send_json(status, payload, {"Cache-Control": "no-store"})

    @property
    def root(self) -> Path:
        return Path(getattr(self.server, "root", default_root()))

    # ---- GET / HEAD -----------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self._handle_get(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        self._handle_get(head_only=False)

    def _handle_get(self, head_only: bool) -> None:
        url = urlsplit(self.path)
        path = unquote(url.path)
        if path == "/api/health":
            from . import formats as _formats
            self._send_json(HTTPStatus.OK, {"ok": True, "version": __version__, "maxUpload": MAX_UPLOAD,
                                            "formats": _formats.formats_info()},
                            {"Cache-Control": "no-store"})
            return
        if path.startswith("/api/"):
            self._send_error_json(HTTPStatus.NOT_FOUND, "unknown API endpoint")
            return
        if path == "/gnnote.zip":
            data = build_package_zip()
            self._send(HTTPStatus.OK, data, MIME_TYPES[".zip"],
                       {"Cache-Control": "no-cache"}, head_only)
            return
        target = self._resolve_static(path)
        if target is None:
            if path == "/version.json":
                payload = json.dumps({"version": __version__}).encode("utf-8")
                self._send(HTTPStatus.OK, payload, MIME_TYPES[".json"],
                           {"Cache-Control": "no-cache"}, head_only)
                return
            self._send(HTTPStatus.NOT_FOUND, b"404 Not Found\n", "text/plain; charset=utf-8",
                       {"Cache-Control": "no-store"}, head_only)
            return
        try:
            data = target.read_bytes()
        except OSError:
            self._send(HTTPStatus.NOT_FOUND, b"404 Not Found\n", "text/plain; charset=utf-8",
                       None, head_only)
            return
        cache = "public, max-age=86400" if "/pyodide/" in target.as_posix() else "no-cache"
        self._send(HTTPStatus.OK, data, content_type_for(target.name),
                   {"Cache-Control": cache}, head_only)

    def _resolve_static(self, url_path: str) -> Optional[Path]:
        """Map a URL path to a file inside the root, or None (missing / escaping)."""
        root = self.root
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            return None
        rel = url_path.lstrip("/")
        if rel == "":
            rel = "index.html"
        if "\x00" in rel or ".." in rel.split("/"):
            return None
        candidate = root_resolved / rel
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if root_resolved != resolved and root_resolved not in resolved.parents:
            return None
        if resolved.is_dir():
            index = resolved / "index.html"
            return index if index.is_file() else None
        return resolved if resolved.is_file() else None

    # ---- POST -----------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        if unquote(url.path) != "/api/convert":
            self._send_error_json(HTTPStatus.NOT_FOUND, "unknown API endpoint")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_error_json(HTTPStatus.LENGTH_REQUIRED, "Content-Length header required")
            self.close_connection = True
            return
        if length > MAX_UPLOAD:
            self._send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                  f"upload larger than {MAX_UPLOAD // (1024 * 1024)} MB")
            self.close_connection = True
            return
        if length < 0:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            self.close_connection = True
            return
        boundary = multipart_boundary(self.headers.get("Content-Type"))
        if boundary is None:
            self._send_error_json(HTTPStatus.BAD_REQUEST,
                                  "expected a multipart/form-data body with a boundary")
            self.close_connection = True
            return
        body = self._read_body(length)
        if body is None:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "request body was cut short")
            self.close_connection = True
            return
        try:
            parts = parse_multipart(body, boundary)
        except MultipartError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, f"malformed multipart body: {exc}")
            return
        del body

        params: Dict[str, str] = {}
        for key, values in parse_qs(url.query, keep_blank_values=True).items():
            if values:
                params[key] = values[-1]
        file_part: Optional[Part] = None
        for part in parts:
            if part.name == "file":
                if file_part is None or part.filename is not None:
                    file_part = part
            elif part.filename is None:
                params[part.name] = part.data.decode("utf-8", "replace")
        if file_part is None or not file_part.data:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "missing 'file' part with the input file")
            return
        filename = os.path.basename((file_part.filename or "").replace("\\", "/")) or "input"
        if "filename" in params and params["filename"].strip():
            filename = os.path.basename(params["filename"].strip().replace("\\", "/"))
        try:
            option_kwargs = build_options(params)
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        try:
            mod = import_convert_module()
        except ImportError as exc:
            self._send_error_json(HTTPStatus.SERVICE_UNAVAILABLE,
                                  f"converter not available: {exc}")
            return
        try:
            options = mod.Options(**option_kwargs)
            result = mod.convert(file_part.data, filename, options)
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc) or exc.__class__.__name__,
                                  traceback=traceback.format_exc())
            return
        except Exception as exc:  # noqa: BLE001 - reported to the client as JSON
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                  f"{exc.__class__.__name__}: {exc}",
                                  traceback=traceback.format_exc())
            return

        out_name = str(getattr(result, "filename", "") or "output")
        headers = {
            "Content-Disposition": content_disposition(out_name),
            "X-GnNote-Warnings": header_json([str(w) for w in getattr(result, "warnings", [])]),
            "X-GnNote-Stats": header_json(dict(getattr(result, "stats", {}) or {})),
            "X-GnNote-Source-Format": str(getattr(result, "source_format", "")),
            "X-GnNote-Target-Format": str(getattr(result, "target_format", "")),
            "X-GnNote-Filename": quote(out_name, safe=""),
            "Cache-Control": "no-store",
        }
        self._send(HTTPStatus.OK, bytes(result.data), "application/octet-stream", headers)

    def _read_body(self, length: int) -> Optional[bytes]:
        chunks: List[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


class GnNoteServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` carrying the static root and a quiet flag."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], root: Optional[Path] = None,
                 quiet: bool = False):
        super().__init__(address, GnNoteHandler)
        self.root = Path(root) if root is not None else default_root()
        self.quiet = quiet


def make_server(host: str = "127.0.0.1", port: int = 8000, root: Optional[Path] = None,
                quiet: bool = False) -> GnNoteServer:
    """Create (but do not start) a server; ``port=0`` picks a free port."""
    return GnNoteServer((host, port), root=root, quiet=quiet)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gnnote.server",
        description="Serve the gnnote web UI and the POST /api/convert endpoint.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="TCP port (default 8000)")
    parser.add_argument("--dist", default=None,
                        help="directory with the static UI (default: dist/ if present, else web/)")
    parser.add_argument("--quiet", action="store_true", help="do not log requests")
    args = parser.parse_args(argv)
    root = Path(args.dist) if args.dist else default_root()
    if not root.is_dir():
        print(f"gnnote.server: static directory not found: {root}", file=sys.stderr)
        return 2
    server = make_server(args.host, args.port, root, quiet=args.quiet)
    host, port = server.server_address[0], server.server_address[1]
    print(f"gnnote {__version__} serving {root} on http://{host}:{port}/", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
