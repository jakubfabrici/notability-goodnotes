"""The format registry and the target-format plumbing (convert API, CLI, server, web list)."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from gnnote import formats
from gnnote.cli import main
from gnnote.convert import Options, convert, detect_format, other_format, output_filename
from gnnote.model import Document, Page, Point, Stroke
from gnnote.notability.writer import write_note
from gnnote.server import build_options

ROOT = Path(__file__).resolve().parent.parent


def _note_bytes(title: str = "Registry") -> bytes:
    page = Page(width=455.04, height=588.45)
    page.strokes.append(Stroke([Point(40, 40, 2.0), Point(120, 90, 2.0), Point(200, 40, 2.0)], width=2.0))
    return write_note(Document(title=title, pages=[page]), Options())


def test_registry_entries_are_consistent() -> None:
    assert list(formats.FORMATS) == [f.id for f in formats.FORMATS.values()]
    for fmt in formats.FORMATS.values():
        assert fmt.extension.startswith(".") and fmt.extension == fmt.extension.lower()
        assert fmt.extension in fmt.input_extensions or not fmt.readable
        assert fmt.readable or fmt.writable
        if fmt.readable:
            assert callable(formats._load(fmt.reader))
        if fmt.writable:
            assert callable(formats._load(fmt.writer))
    extensions = [e for f in formats.FORMATS.values() for e in f.input_extensions]
    assert len(extensions) == len(set(extensions)), "two formats claim the same input extension"


def test_default_targets_never_return_the_source() -> None:
    for fmt in formats.readable():
        target = formats.default_target(fmt.id)
        assert target != fmt.id and formats.get(target).writable
    assert formats.default_target("goodnotes") == "notability"
    assert formats.default_target("notability") == "goodnotes"
    assert other_format("goodnotes") == "notability"
    with pytest.raises(ValueError):
        other_format("nope")


def test_unknown_format_id_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="unknown format"):
        formats.get("onenote-2003")


def test_web_formats_js_matches_the_registry() -> None:
    checked_in = (ROOT / "web" / "formats.js").read_text(encoding="utf-8")
    assert checked_in == formats.web_formats_js(), \
        "web/formats.js is stale: regenerate it with python3 -c 'from gnnote.formats import web_formats_js; ...'"
    payload = checked_in[checked_in.index("["): checked_in.rindex("]") + 1]
    assert json.loads(payload) == formats.formats_info()


def test_build_script_regenerates_formats_js(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_web.py"), "--out", str(out)],
                   check=True, capture_output=True, text=True)
    assert (out / "formats.js").read_text(encoding="utf-8") == formats.web_formats_js()
    with zipfile.ZipFile(out / "gnnote.zip") as zf:
        assert "gnnote/formats.py" in zf.namelist()


def test_detect_format_uses_registry_sniffers_and_extensions() -> None:
    note = _note_bytes()
    assert detect_format("x.note", note) == "notability"
    assert detect_format("renamed.zip", note) == "notability"
    assert detect_format("x.goodnotes", b"not a zip") == "goodnotes"
    with pytest.raises(ValueError, match="not a supported note file"):
        detect_format("x.txt", b"hello")


def test_options_target_validation() -> None:
    Options(target="goodnotes").validate()
    with pytest.raises(ValueError, match="unknown format"):
        Options(target="pages").validate()


def test_convert_honours_the_target_and_refuses_same_format() -> None:
    note = _note_bytes()
    result = convert(note, "Registry.note", Options(target="goodnotes"))
    assert (result.source_format, result.target_format) == ("notability", "goodnotes")
    assert result.filename == "Registry.goodnotes"
    with pytest.raises(ValueError, match="already is a Notability file"):
        convert(note, "Registry.note", Options(target="notability"))


def test_output_filename_uses_the_target_extension() -> None:
    assert output_filename("a.b.note", "goodnotes") == "a.b.goodnotes"
    assert output_filename("", "notability") == "converted.note"


def test_cli_formats_and_to(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["formats"]) == 0
    listed = capsys.readouterr().out
    for fmt in formats.FORMATS.values():
        assert fmt.id in listed and fmt.name in listed
    src = tmp_path / "Mini.note"
    src.write_bytes(_note_bytes("Mini"))
    assert main(["convert", str(src), "--to", "goodnotes"]) == 0
    assert (tmp_path / "Mini.goodnotes").is_file()
    assert main(["convert", str(src), "--to", "notability"]) == 1  # same format
    assert main(["convert", str(src), "--to", "nope"]) == 2  # usage error


def test_server_accepts_a_target() -> None:
    assert build_options({"to": "GoodNotes"})["target"] == "goodnotes"
    assert build_options({"target": "notability"})["target"] == "notability"
    assert "target" not in build_options({})
    with pytest.raises(ValueError, match="to must be one of"):
        build_options({"to": "keynote"})


def test_sniffer_errors_do_not_break_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(data: bytes, names):  # noqa: ANN001
        raise RuntimeError("broken sniffer")

    broken = formats.NoteFormat(id="broken", name="Broken", extension=".broken", input_extensions=(".broken",),
                                sniff=boom, reader="gnnote.notability.reader:read_note")
    monkeypatch.setitem(formats.FORMATS, "broken", broken)
    monkeypatch.setattr(formats, "FORMATS", {"broken": broken, **{k: v for k, v in formats.FORMATS.items()
                                                                   if k != "broken"}})
    assert detect_format("x.note", _note_bytes()) == "notability"


def test_zip_names_helper() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/Session.plist", b"x")
    assert formats.sniff_zip_names(buf.getvalue()) == ["a/Session.plist"]
    assert formats.sniff_zip_names(b"PK broken") is None
    assert formats.sniff_zip_names(b"%PDF-1.4") is None


def test_noteful_is_sniffed_from_its_magic_and_trailer() -> None:
    from gnnote.noteful.writer import write_noteful

    data = write_noteful(Document(title="Sniff", pages=[Page(width=300, height=400)]))
    noteful = formats.get("noteful")
    assert noteful.sniff(data, None) and noteful.extension == ".noteful"
    assert not noteful.sniff(data, ["Session.plist"])  # a ZIP is never a Noteful file
    assert not noteful.sniff(b"\xaa\xbb\xcc\xde" + b"\x00" * 40, None)  # no trailer magic
    assert not noteful.sniff(b"\xaa\xbb\xcc\xde", None)
    assert detect_format("renamed.zip", data) == "noteful"
    assert detect_format("x.NOTEFUL", b"junk") == "noteful"  # unrecognised content: the extension decides
    assert detect_format("x.note", data) == "noteful"  # content wins over the extension
    assert formats.default_target("noteful") == "notability"
    result = convert(data, "Sniff.noteful", Options(target="goodnotes"))
    assert (result.source_format, result.filename) == ("noteful", "Sniff.goodnotes")
