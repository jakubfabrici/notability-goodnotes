"""Tests for gnnote.server (stdlib HTTP server) and the project packaging metadata."""
from __future__ import annotations

import http.client
import io
import json
import socket
import sys
import threading
import tomllib
import types
import zipfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

from gnnote import __version__
from gnnote import server as srv

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- fakes

class FakeOptions:
    def __init__(self, **kw: object):
        self.kw = kw


class FakeResult:
    def __init__(self, data: bytes, filename: str, warnings: List[str], stats: Dict[str, int]):
        self.data = data
        self.filename = filename
        self.warnings = warnings
        self.stats = stats
        self.source_format = "goodnotes"
        self.target_format = "notability"


def _fake_convert_module(calls: List[Tuple[bytes, str, FakeOptions]]) -> types.ModuleType:
    mod = types.ModuleType("gnnote.convert")

    def convert(data: bytes, filename: str, options: Optional[FakeOptions] = None) -> FakeResult:
        calls.append((data, filename, options))
        if filename.endswith(".bad"):
            raise ValueError("neither a .goodnotes nor a .note file")
        if filename.endswith(".boom"):
            raise RuntimeError("converter crashed")
        stem = filename.rsplit(".", 1)[0]
        return FakeResult(b"ZIP" + data[::-1], stem + ".note",
                          ["page 2: pencil strokes approximated", "titul čárka — ok"],
                          {"pages": 2, "strokes": 5})

    mod.Options = FakeOptions  # type: ignore[attr-defined]
    mod.convert = convert  # type: ignore[attr-defined]
    return mod


# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def web_root(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>gnnote test</title>", encoding="utf-8")
    (root / "app.mjs").write_text("export const x = 1;\n", encoding="utf-8")
    (root / "styles.css").write_text("body{}", encoding="utf-8")
    (root / "pyodide").mkdir()
    (root / "pyodide" / "pyodide.asm.wasm").write_bytes(b"\0asm\1\0\0\0")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    return root


@pytest.fixture
def calls() -> List[Tuple[bytes, str, FakeOptions]]:
    return []


@pytest.fixture
def running(web_root: Path, calls: List[Tuple[bytes, str, FakeOptions]],
            monkeypatch: pytest.MonkeyPatch) -> Iterator[Tuple[str, int]]:
    monkeypatch.setitem(sys.modules, "gnnote.convert", _fake_convert_module(calls))
    server = srv.make_server("127.0.0.1", 0, web_root, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(addr: Tuple[str, int], method: str, path: str, body: Optional[bytes] = None,
             headers: Optional[Dict[str, str]] = None) -> Tuple[int, http.client.HTTPMessage, bytes]:
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


def _multipart(fields: Dict[str, str], file: Optional[Tuple[str, str, bytes]],
               boundary: str = "----gnnoteBoundary42") -> Tuple[bytes, str]:
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode())
        out.write(value.encode("utf-8") + b"\r\n")
    if file is not None:
        name, filename, data = file
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                  f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------- multipart parser

def test_parse_multipart_fields_and_file() -> None:
    body, ctype = _multipart({"paper": "pdf", "title": "Poznámky"},
                             ("file", "My Notes.goodnotes", b"\x00\xff--not-a-boundary\r\n--x"))
    boundary = srv.multipart_boundary(ctype)
    assert boundary == b"----gnnoteBoundary42"
    parts = srv.parse_multipart(body, boundary)
    assert [p.name for p in parts] == ["paper", "title", "file"]
    assert parts[1].data.decode("utf-8") == "Poznámky"
    assert parts[2].filename == "My Notes.goodnotes"
    assert parts[2].content_type == "application/octet-stream"
    assert parts[2].data == b"\x00\xff--not-a-boundary\r\n--x"


def test_parse_multipart_quoted_boundary_and_escapes() -> None:
    assert srv.multipart_boundary('multipart/form-data; boundary="a b"') == b"a b"
    assert srv.multipart_boundary("application/json") is None
    assert srv.multipart_boundary(None) is None
    body = (b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"q\\\"x.note\"\r\n\r\n"
            b"data\r\n--B--\r\n")
    (part,) = srv.parse_multipart(body, b"B")
    assert part.filename == 'q"x.note'
    assert part.data == b"data"


def test_parse_multipart_rfc5987_filename_and_lf_only() -> None:
    body = (b"--B\nContent-Disposition: form-data; name=\"file\"; filename*=UTF-8''%C4%8Darka.note\n\n"
            b"abc\n--B--\n")
    (part,) = srv.parse_multipart(body, b"B")
    assert part.filename == "čarka.note"
    assert part.data == b"abc"


def test_parse_multipart_errors() -> None:
    with pytest.raises(srv.MultipartError):
        srv.parse_multipart(b"no boundary here", b"B")
    with pytest.raises(srv.MultipartError):
        srv.parse_multipart(b"--B\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\nunterminated", b"B")
    assert srv.parse_multipart(b"--B--\r\n", b"B") == []


def test_build_options_validation() -> None:
    assert srv.build_options({}) == {}
    assert srv.build_options({"paper": "PDF", "pressure": "false", "simplify": "0.5", "title": " T "}) == {
        "paper": "pdf", "pressure": False, "simplify": 0.5, "title": "T"}
    assert srv.build_options({"pressure": "1"}) == {"pressure": True}
    for bad in ({"paper": "ruled"}, {"simplify": "abc"}, {"simplify": "-1"}, {"pressure": "maybe"}):
        with pytest.raises(ValueError):
            srv.build_options(bad)


def test_header_json_is_ascii_and_truncates() -> None:
    text = srv.header_json(["čárka", "line\nbreak"])
    assert text.isascii() and "\n" not in text
    assert json.loads(text) == ["čárka", "line\nbreak"]
    huge = ["w" * 1000] * 200
    truncated = json.loads(srv.header_json(huge))
    assert len(json.dumps(truncated)) <= srv.MAX_HEADER_JSON
    assert truncated[-1].startswith("...") and "omitted" in truncated[-1]


def test_content_disposition_non_ascii() -> None:
    assert srv.content_disposition("a.note") == 'attachment; filename="a.note"'
    value = srv.content_disposition('Poznámky "x".note')
    assert value.startswith('attachment; filename="Pozn_mky _x_.note"')
    assert "filename*=UTF-8''Pozn%C3%A1mky" in value
    assert "\n" not in value


def test_content_type_table() -> None:
    assert srv.content_type_for("worker.mjs").startswith("text/javascript")
    assert srv.content_type_for("app.js").startswith("text/javascript")
    assert srv.content_type_for("pyodide.asm.wasm") == "application/wasm"
    assert srv.content_type_for("gnnote.zip") == "application/zip"
    assert srv.content_type_for("version.json").startswith("application/json")
    assert srv.content_type_for("index.html").startswith("text/html")
    assert srv.content_type_for("styles.css").startswith("text/css")
    assert srv.content_type_for("icon.svg") == "image/svg+xml"
    assert srv.content_type_for("unknown.xyz123") == "application/octet-stream"


# --------------------------------------------------------------------------- HTTP

def test_health(running: Tuple[str, int]) -> None:
    status, headers, body = _request(running, "GET", "/api/health")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    from gnnote.formats import formats_info
    assert json.loads(body) == {"ok": True, "version": __version__, "maxUpload": srv.MAX_UPLOAD,
                                "formats": formats_info()}


def test_static_index_and_mime(running: Tuple[str, int]) -> None:
    status, headers, body = _request(running, "GET", "/")
    assert status == 200 and b"gnnote test" in body
    assert headers["Content-Type"].startswith("text/html")
    status, headers, body = _request(running, "GET", "/index.html")
    assert status == 200
    status, headers, body = _request(running, "GET", "/app.mjs")
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    assert body == b"export const x = 1;\n"
    status, headers, _ = _request(running, "GET", "/pyodide/pyodide.asm.wasm")
    assert status == 200 and headers["Content-Type"] == "application/wasm"
    assert "max-age" in headers["Cache-Control"]
    assert "Cross-Origin-Opener-Policy" not in headers
    assert "Cross-Origin-Embedder-Policy" not in headers


def test_static_head_and_404(running: Tuple[str, int]) -> None:
    status, headers, body = _request(running, "HEAD", "/app.mjs")
    assert status == 200 and body == b"" and headers["Content-Length"] == "20"
    status, _, _ = _request(running, "GET", "/missing.js")
    assert status == 404
    status, _, _ = _request(running, "GET", "/pyodide/")
    assert status == 404
    status, _, body = _request(running, "GET", "/api/nothing")
    assert status == 404 and json.loads(body)["error"]


def test_static_path_traversal_blocked(running: Tuple[str, int]) -> None:
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/pyodide/../../secret.txt"):
        status, _, body = _request(running, "GET", path)
        assert status == 404, path
        assert b"nope" not in body


def test_version_json_generated_when_missing(running: Tuple[str, int], web_root: Path) -> None:
    status, headers, body = _request(running, "GET", "/version.json")
    assert status == 200 and json.loads(body)["version"] == __version__
    (web_root / "version.json").write_text('{"version": "from-disk"}', encoding="utf-8")
    status, headers, body = _request(running, "GET", "/version.json")
    assert status == 200 and json.loads(body)["version"] == "from-disk"


def test_gnnote_zip_built_from_package(running: Tuple[str, int]) -> None:
    status, headers, body = _request(running, "GET", "/gnnote.zip")
    assert status == 200 and headers["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
        assert "gnnote/__init__.py" in names
        assert "gnnote/server.py" in names
        assert "gnnote/model.py" in names
        assert all(n.startswith("gnnote/") and n.endswith(".py") for n in names)
        assert not any("__pycache__" in n for n in names)
        assert f'__version__ = "{__version__}"' in zf.read("gnnote/__init__.py").decode("utf-8")
    # cached: identical bytes the second time
    status2, _, body2 = _request(running, "GET", "/gnnote.zip")
    assert status2 == 200 and body2 == body


def test_convert_roundtrip_headers(running: Tuple[str, int],
                                   calls: List[Tuple[bytes, str, FakeOptions]]) -> None:
    payload = b"PK\x03\x04fake goodnotes bytes \x00\xff"
    body, ctype = _multipart({"title": "Môj zošit"},
                             ("file", "Poznámky.goodnotes", payload))
    status, headers, out = _request(
        running, "POST", "/api/convert?paper=pdf&pressure=0&simplify=1.5",
        body, {"Content-Type": ctype, "Content-Length": str(len(body))})
    assert status == 200, out
    assert out == b"ZIP" + payload[::-1]
    assert headers["Content-Type"] == "application/octet-stream"
    disposition = headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''Pozn%C3%A1mky.note" in disposition
    assert disposition.isascii()
    warnings = headers["X-GnNote-Warnings"]
    assert warnings.isascii()
    assert json.loads(warnings) == ["page 2: pencil strokes approximated", "titul čárka — ok"]
    assert json.loads(headers["X-GnNote-Stats"]) == {"pages": 2, "strokes": 5}
    assert headers["X-GnNote-Source-Format"] == "goodnotes"
    assert headers["X-GnNote-Target-Format"] == "notability"
    assert len(calls) == 1
    data, filename, options = calls[0]
    assert data == payload and filename == "Poznámky.goodnotes"
    assert options.kw == {"paper": "pdf", "pressure": False, "simplify": 1.5, "title": "Môj zošit"}


def test_convert_form_fields_override_query(running: Tuple[str, int],
                                            calls: List[Tuple[bytes, str, FakeOptions]]) -> None:
    body, ctype = _multipart({"paper": "plain", "pressure": "true"}, ("file", "a.note", b"x"))
    status, _, _ = _request(running, "POST", "/api/convert?paper=pdf&pressure=false", body,
                            {"Content-Type": ctype, "Content-Length": str(len(body))})
    assert status == 200
    assert calls[0][2].kw == {"paper": "plain", "pressure": True}


def test_convert_bad_requests(running: Tuple[str, int]) -> None:
    # no file part
    body, ctype = _multipart({"paper": "plain"}, None)
    status, headers, out = _request(running, "POST", "/api/convert", body,
                                    {"Content-Type": ctype, "Content-Length": str(len(body))})
    assert status == 400 and headers["Content-Type"].startswith("application/json")
    assert "file" in json.loads(out)["error"]
    # bad option
    body, ctype = _multipart({"simplify": "lots"}, ("file", "a.goodnotes", b"x"))
    status, _, out = _request(running, "POST", "/api/convert", body,
                              {"Content-Type": ctype, "Content-Length": str(len(body))})
    assert status == 400 and "simplify" in json.loads(out)["error"]
    # not multipart
    status, _, out = _request(running, "POST", "/api/convert", b"{}",
                              {"Content-Type": "application/json", "Content-Length": "2"})
    assert status == 400 and "multipart" in json.loads(out)["error"]
    # malformed multipart
    status, _, out = _request(running, "POST", "/api/convert", b"garbage",
                              {"Content-Type": "multipart/form-data; boundary=B", "Content-Length": "7"})
    assert status == 400 and "multipart" in json.loads(out)["error"]
    # converter says the input is invalid -> 400 with the message
    body, ctype = _multipart({}, ("file", "weird.bad", b"x"))
    status, _, out = _request(running, "POST", "/api/convert", body,
                              {"Content-Type": ctype, "Content-Length": str(len(body))})
    assert status == 400 and json.loads(out)["error"] == "neither a .goodnotes nor a .note file"
    # converter crashes -> 500 JSON with traceback
    body, ctype = _multipart({}, ("file", "weird.boom", b"x"))
    status, _, out = _request(running, "POST", "/api/convert", body,
                              {"Content-Type": ctype, "Content-Length": str(len(body))})
    assert status == 500
    payload = json.loads(out)
    assert "converter crashed" in payload["error"] and "Traceback" in payload["traceback"]
    # wrong endpoint
    status, _, _ = _request(running, "POST", "/api/other", b"", {"Content-Length": "0"})
    assert status == 404


def test_convert_too_large_rejected_before_reading(running: Tuple[str, int]) -> None:
    huge = srv.MAX_UPLOAD + 1
    with socket.create_connection(running, timeout=10) as sock:
        request = (f"POST /api/convert HTTP/1.1\r\nHost: localhost\r\n"
                   f"Content-Type: multipart/form-data; boundary=B\r\n"
                   f"Content-Length: {huge}\r\n\r\n").encode()
        sock.sendall(request)
        sock.sendall(b"--B\r\n")  # only a few bytes; the server must answer without the rest
        sock.settimeout(10)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if b"\r\n\r\n" in b"".join(chunks):
                head, _, rest = b"".join(chunks).partition(b"\r\n\r\n")
                length = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0].split(b":")[1])
                if len(rest) >= length:
                    break
    raw = b"".join(chunks)
    assert raw.startswith(b"HTTP/1.1 413")
    assert b'"status": 413' in raw


def test_converter_missing_gives_503(web_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "import_convert_module",
                        lambda: (_ for _ in ()).throw(ImportError("no module named gnnote.convert")))
    server = srv.make_server("127.0.0.1", 0, web_root, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body, ctype = _multipart({}, ("file", "a.note", b"x"))
        status, _, out = _request(server.server_address, "POST", "/api/convert", body,
                                  {"Content-Type": ctype, "Content-Length": str(len(body))})
        assert status == 503 and "converter not available" in json.loads(out)["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_main_rejects_missing_dist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert srv.main(["--dist", str(tmp_path / "nowhere")]) == 2
    assert "not found" in capsys.readouterr().err


def test_default_root_points_at_web_or_dist() -> None:
    root = srv.default_root()
    assert root.name in ("dist", "web")
    assert root.parent == ROOT


# --------------------------------------------------------------------------- packaging

def test_pyproject_metadata() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "gnnote"
    assert "version" in data["project"]["dynamic"]
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "gnnote.__version__"}
    assert data["project"]["scripts"]["gnnote"] == "gnnote.cli:main"
    assert data["project"]["dependencies"] == []
    assert any(dep.startswith("pytest") for dep in data["project"]["optional-dependencies"]["test"])
    assert data["tool"]["pytest"]["ini_options"]["pythonpath"] == ["."]
    assert data["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_main_module_delegates_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import gnnote.__main__ as entry

    seen: List[object] = []
    fake_cli = types.ModuleType("gnnote.cli")

    def main(argv: Optional[List[str]] = None) -> int:
        seen.append(argv)
        return 3

    fake_cli.main = main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gnnote.cli", fake_cli)
    assert entry.main(["info", "x.note"]) == 3
    assert seen == [["info", "x.note"]]


# --------------------------------------------------------------------------- real converter

@pytest.fixture
def real_server(web_root: Path) -> Iterator[Tuple[str, int]]:
    """A live server using the real ``gnnote.convert`` (no fake in ``sys.modules``)."""
    import gnnote.convert as real_module
    assert getattr(real_module, "__file__", None), "a fake gnnote.convert leaked into sys.modules"
    server = srv.make_server("127.0.0.1", 0, web_root, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _tiny_note() -> bytes:
    """A one-page Notability note with two strokes and a text box built by our own writer."""
    from gnnote.model import Document, Page, Point, Stroke, TextBox
    from gnnote.notability.writer import write_note

    page = Page(612.0, 803.25)
    page.strokes.append(Stroke([Point(50, 50, 2.0), Point(120, 90, 2.5), Point(200, 50, 2.0)], width=2.0))
    page.strokes.append(Stroke([Point(60, 300, 8.0), Point(260, 300, 8.0)], color=(1.0, 0.9, 0.0, 0.42),
                               kind="highlighter", width=8.0))
    page.texts.append(TextBox(40, 400, 200, 40, "Ahoj, svet"))
    return write_note(Document(title="Mini", pages=[page], source_format="notability"), None)


def test_convert_real_note_over_http(real_server: Tuple[str, int]) -> None:
    from gnnote.convert import detect_format
    from gnnote.goodnotes.reader import read_goodnotes

    note = _tiny_note()
    body, ctype = _multipart({}, ("file", "Mini.note", note))
    status, headers, out = _request(
        real_server, "POST", "/api/convert?paper=plain&pressure=true&simplify=0",
        body, {"Content-Type": ctype, "Content-Length": str(len(body))})
    assert status == 200, out[:300]
    assert headers["X-GnNote-Source-Format"] == "notability"
    assert headers["X-GnNote-Target-Format"] == "goodnotes"
    assert 'filename="Mini.goodnotes"' in headers["Content-Disposition"]
    stats = json.loads(headers["X-GnNote-Stats"])
    assert stats == {"pages": 1, "strokes": 2, "images": 0, "texts": 1, "pdfs": 0}
    assert isinstance(json.loads(headers["X-GnNote-Warnings"]), list)
    assert detect_format("Mini.goodnotes", out) == "goodnotes"
    back = read_goodnotes(out)
    assert back.title == "Mini"
    assert [len(p.strokes) for p in back.pages] == [2]
    assert back.pages[0].strokes[1].kind == "highlighter"
    assert back.pages[0].texts[0].text == "Ahoj, svet"
    assert back.pages[0].template_is_builtin  # gnnote paper is stock paper when read back

    # the other direction over the same server: GoodNotes bytes come back as a .note
    body2, ctype2 = _multipart({"title": "Renamed"}, ("file", "Mini.goodnotes", out))
    status2, headers2, out2 = _request(
        real_server, "POST", "/api/convert", body2,
        {"Content-Type": ctype2, "Content-Length": str(len(body2))})
    assert status2 == 200, out2[:300]
    assert headers2["X-GnNote-Target-Format"] == "notability"
    from gnnote.notability.reader import read_note
    again = read_note(out2)
    assert again.title == "Renamed" and sum(len(p.strokes) for p in again.pages) == 2

    # invalid options and non-files are 400s from the real module too
    status3, _h3, out3 = _request(
        real_server, "POST", "/api/convert?paper=lined", body,
        {"Content-Type": ctype, "Content-Length": str(len(body))})
    assert status3 == 400 and "paper" in json.loads(out3)["error"]
    body4, ctype4 = _multipart({}, ("file", "junk.note", b"not a zip at all"))
    status4, _h4, out4 = _request(
        real_server, "POST", "/api/convert", body4,
        {"Content-Type": ctype4, "Content-Length": str(len(body4))})
    assert status4 == 400 and json.loads(out4)["status"] == 400
