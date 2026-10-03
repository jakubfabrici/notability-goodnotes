"""Tests for the command-line interface (``gnnote.cli`` and ``python -m gnnote``).

Synthetic files are built with the project's own writers so the tests run without the
reference samples; the sample-based test only asserts invariants per file plus an exact
allow-list for files whose content is pinned elsewhere, so unknown sample files cannot
break it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from gnnote import __version__
from gnnote.cli import build_parser, describe, main
from gnnote.convert import GOODNOTES, NOTABILITY, Options
from gnnote.goodnotes.reader import read_goodnotes
from gnnote.goodnotes.writer import write_goodnotes
from gnnote.model import Document, Page, Point, Stroke, TextBox
from gnnote.notability.reader import read_note
from gnnote.notability.writer import write_note

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- helpers

def _doc(title: str = "Mini") -> Document:
    page = Page(455.04, 588.45)
    page.strokes.append(Stroke([Point(30, 30, 2.0), Point(90, 70, 2.0), Point(150, 30, 2.0)], width=2.0))
    page.strokes.append(Stroke([Point(40, 200, 8.0), Point(240, 200, 8.0)], color=(1.0, 0.9, 0.0, 0.5),
                               kind="highlighter", width=8.0))
    page.texts.append(TextBox(30, 300, 200, 40, "hello"))
    return Document(title=title, pages=[page], source_format=NOTABILITY)


@pytest.fixture
def note_file(tmp_path: Path) -> Path:
    path = tmp_path / "Mini.note"
    path.write_bytes(write_note(_doc(), Options()))
    return path


@pytest.fixture
def goodnotes_file(tmp_path: Path) -> Path:
    path = tmp_path / "Mini.goodnotes"
    path.write_bytes(write_goodnotes(_doc(), Options()))
    return path


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """``python -m gnnote ARGS`` in a subprocess with the repository on ``PYTHONPATH``."""
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "gnnote", *args], capture_output=True, text=True,
                          env=env, cwd=str(cwd or ROOT), timeout=600)


# --------------------------------------------------------------------------- parser / usage

def test_version_and_help_exit_zero() -> None:
    proc = run_cli("--version")
    assert proc.returncode == 0 and proc.stdout.strip() == f"gnnote {__version__}"
    proc = run_cli("--help")
    assert proc.returncode == 0
    for word in ("convert", "info", "batch"):
        assert word in proc.stdout
    proc = run_cli("convert", "--help")
    assert proc.returncode == 0
    for flag in ("--paper", "--no-pressure", "--simplify", "--ribbon", "--title", "-o"):
        assert flag in proc.stdout
    proc = run_cli("batch", "--help")
    assert "--to" in proc.stdout and "--ribbon" not in proc.stdout and "--title" not in proc.stdout


def test_usage_errors_exit_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err
    assert main(["convert"]) == 2
    assert main(["convert", "x.note", "--paper", "lined"]) == 2
    assert main(["batch", "d", "--to", "pdf"]) == 2
    assert main(["frobnicate"]) == 2
    assert main(["--help"]) == 0
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip().endswith(__version__)
    proc = run_cli()
    assert proc.returncode == 2 and "usage" in proc.stderr
    proc = run_cli("convert", "a.note", "--simplify", "abc")
    assert proc.returncode == 2


def test_build_parser_defaults() -> None:
    args = build_parser().parse_args(["convert", "in.note"])
    assert (args.paper, args.pressure, args.simplify, args.ribbon, args.title, args.output) == \
        ("plain", True, 0.0, False, None, None)
    args = build_parser().parse_args(["batch", "dir", "--no-pressure", "--simplify", "0.5", "--paper", "pdf"])
    assert (args.paper, args.pressure, args.simplify, args.target) == ("pdf", False, 0.5, None)


# --------------------------------------------------------------------------- convert

def test_convert_default_output_next_to_input(note_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["convert", str(note_file)]) == 0
    out = note_file.with_name("Mini.goodnotes")
    assert out.is_file()
    captured = capsys.readouterr()
    assert "Mini.note -> " in captured.out and "notability -> goodnotes" in captured.out
    assert "1 pages, 2 strokes, 0 images, 1 texts, 0 PDFs" in captured.out
    for line in captured.err.splitlines():
        assert line.startswith("warning: ")
    back = read_goodnotes(out.read_bytes())
    assert back.title == "Mini" and [len(p.strokes) for p in back.pages] == [2]


def test_convert_output_file_and_directory_and_options(goodnotes_file: Path, note_file: Path,
                                                       tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    proc = run_cli("convert", str(goodnotes_file), "-o", str(out_dir) + os.sep, "--paper", "pdf",
                   "--no-pressure", "--simplify", "0.5", "--title", "Renamed")
    assert proc.returncode == 0, proc.stderr
    produced = out_dir / "Mini.note"
    assert produced.is_file()
    note = read_note(produced.read_bytes())
    assert note.title == "Renamed"
    assert all(p.background is not None for p in note.pages)  # --paper pdf: PDF-backed pages
    for page in note.pages:
        for s in page.strokes:
            widths = [pt.width for pt in s.points]
            assert max(widths) - min(widths) < 1e-6  # --no-pressure: constant widths

    explicit = tmp_path / "custom name.note"
    proc = run_cli("convert", str(goodnotes_file), "-o", str(explicit))
    assert proc.returncode == 0, proc.stderr
    assert explicit.is_file() and read_note(explicit.read_bytes()).title == "Mini"
    # --ribbon is experimental and currently falls back to flat strokes with a warning
    proc = run_cli("convert", str(note_file), "-o", str(tmp_path / "r.goodnotes"), "--ribbon")
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "r.goodnotes").is_file() and "ribbon" in proc.stderr.lower()


def test_convert_failures_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["convert", str(tmp_path / "missing.note")]) == 1
    assert "cannot read" in capsys.readouterr().err
    junk = tmp_path / "junk.txt"
    junk.write_bytes(b"hello")
    assert main(["convert", str(junk)]) == 1
    assert "not a supported note file" in capsys.readouterr().err
    bad = tmp_path / "bad.note"
    bad.write_bytes(b"PK\x03\x04 not really a zip")
    assert main(["convert", str(bad)]) == 1
    assert capsys.readouterr().err.startswith("error:")
    proc = run_cli("convert", str(bad))
    assert proc.returncode == 1 and "Traceback" not in proc.stderr


# --------------------------------------------------------------------------- info

def test_info_text_and_json(note_file: Path, goodnotes_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["info", str(note_file)]) == 0
    text = capsys.readouterr().out
    assert "Format:  notability" in text and "Title:   Mini" in text and "Pages:   1" in text
    assert "strokes     2" in text and "texts   1" in text

    proc = run_cli("info", str(goodnotes_file), "--json")
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout)
    assert info["format"] == GOODNOTES and info["title"] == "Mini"
    assert info["totals"] == {"pages": 1, "strokes": 2, "images": 0, "texts": 1, "pdfs": 0}
    page = info["pages"][0]
    assert page["index"] == 1 and page["builtin_template"] is True and page["paper"] == "plain"
    assert page["background"]["page"] == 1
    assert (page["width"], page["height"]) == (455.04, 588.45)
    assert isinstance(info["warnings"], list)

    described = describe(note_file.read_bytes(), note_file.name)
    assert described["format"] == NOTABILITY and described["pages"][0]["background"] is None
    assert json.dumps(described)  # JSON-serialisable


def test_info_failures_exit_one(tmp_path: Path) -> None:
    assert main(["info", str(tmp_path / "nope.goodnotes")]) == 1
    junk = tmp_path / "x.bin"
    junk.write_bytes(b"nothing")
    assert main(["info", str(junk)]) == 1
    proc = run_cli("info", str(junk))
    assert proc.returncode == 1 and proc.stderr.startswith("error:")


# --------------------------------------------------------------------------- batch

def test_batch_converts_both_kinds(note_file: Path, goodnotes_file: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "converted"
    proc = run_cli("batch", str(tmp_path), "-o", str(out_dir))
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in out_dir.iterdir()) == ["Mini.goodnotes", "Mini.note"]
    assert "2 converted, 0 failed" in proc.stdout
    assert read_note((out_dir / "Mini.note").read_bytes()).title == "Mini"
    assert read_goodnotes((out_dir / "Mini.goodnotes").read_bytes()).title == "Mini"


def test_batch_to_filter_and_failure(note_file: Path, goodnotes_file: Path, tmp_path: Path,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "only-notes"
    assert main(["batch", str(tmp_path), "-o", str(out_dir), "--to", NOTABILITY, "--paper", "pdf"]) == 0
    assert [p.name for p in out_dir.iterdir()] == ["Mini.note"]
    captured = capsys.readouterr()
    assert "ok    Mini.goodnotes -> Mini.note" in captured.out and "1 converted, 0 failed" in captured.out

    (tmp_path / "broken.note").write_bytes(b"PK\x03\x04 broken")
    assert main(["batch", str(tmp_path), "-o", str(tmp_path / "mixed")]) == 1
    captured = capsys.readouterr()
    assert "FAIL  broken.note" in captured.out and "2 converted, 1 failed" in captured.out

    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["batch", str(empty)]) == 0
    assert "no .goodnotes or .note files" in capsys.readouterr().out
    assert main(["batch", str(tmp_path / "missing-dir")]) == 1
    assert "not a directory" in capsys.readouterr().err


def test_batch_default_output_is_the_input_directory(note_file: Path) -> None:
    proc = run_cli("batch", str(note_file.parent), "--to", GOODNOTES)
    assert proc.returncode == 0, proc.stderr
    assert (note_file.parent / "Mini.goodnotes").is_file()


# --------------------------------------------------------------------------- samples

# Exact totals for the pinned sample files (the GoodNotes ones agree with GOODNOTES_EXPECTED
# in test_convert.py; Test6 .. Test9 from docs/goodnotes-v35-elements.md section 0, fills
# counted as strokes, the Test9 sticker PDF and photo as images, its Figma / form / strip
# PDFs as user PDFs); every other file only has to satisfy the invariants below.
INFO_EXPECTED: Dict[str, Dict[str, int]] = {
    "Test4.goodnotes": {"pages": 2, "strokes": 5, "images": 0, "texts": 0, "pdfs": 0},
    "Test5.goodnotes": {"pages": 3, "strokes": 65, "images": 1, "texts": 2, "pdfs": 0},
    "Test6.goodnotes": {"pages": 5, "strokes": 21, "images": 0, "texts": 5, "pdfs": 0},
    "Test7.goodnotes": {"pages": 4, "strokes": 27, "images": 0, "texts": 14, "pdfs": 0},
    "Test8.goodnotes": {"pages": 4, "strokes": 10, "images": 0, "texts": 0, "pdfs": 0},
    "Test9.goodnotes": {"pages": 7, "strokes": 162, "images": 2, "texts": 4, "pdfs": 3},
    "test.goodnotes": {"pages": 1, "strokes": 1, "images": 0, "texts": 0, "pdfs": 0},
    "test2.goodnotes": {"pages": 1, "strokes": 1, "images": 0, "texts": 0, "pdfs": 0},
    "test3.goodnotes": {"pages": 1, "strokes": 2, "images": 0, "texts": 0, "pdfs": 0},
    "ex1.goodnotes": {"pages": 1, "strokes": 1494, "images": 3, "texts": 0, "pdfs": 0},
    "ex2.goodnotes": {"pages": 1, "strokes": 28, "images": 0, "texts": 0, "pdfs": 0},
    "ex3.goodnotes": {"pages": 1, "strokes": 2459, "images": 2, "texts": 0, "pdfs": 0},
    "record.goodnotes": {"pages": 2, "strokes": 12, "images": 0, "texts": 0, "pdfs": 0},
    "example.note": {"pages": 1, "strokes": 399, "images": 0, "texts": 0, "pdfs": 0},
}

# Page sizes and papers as ``info --json`` lists them, for the files whose GoodNotes export
# (TestN.pdf next to the sample) fixes them: Test9 mixes A4 catalogue papers with a Figma
# PDF, an Excel form and a photo strip; Test6 page 1 is the only ruled page of Test6 .. Test8
# (docs/goodnotes-v35-binding.md sections 8.3, 8.5 and 9.1).
A4 = (595.28, 841.89)
STD = (455.04, 588.45)
PAGES_EXPECTED: Dict[str, List[Tuple[float, float, str, bool]]] = {
    "Test6.goodnotes": [(*STD, "lined", True)] + [(*STD, "plain", True)] * 4,
    "Test7.goodnotes": [(*STD, "plain", True)] * 4,
    "Test8.goodnotes": [(*STD, "plain", True)] * 4,
    "Test9.goodnotes": [(*A4, "plain", True), (*A4, "grid", True), (*A4, "grid", True), (*A4, "plain", True),
                        (1280.0, 905.0, "plain", False), (595.2, 841.68, "plain", False),
                        (454.91, 143.28, "plain", False)],
    "Test5.goodnotes": [(*STD, "plain", True), (*STD, "grid", True), (*STD, "grid", True)],
}


def test_info_json_on_every_sample(samples) -> None:
    files: List[Path] = []
    try:
        files += samples.goodnotes_files()
    except pytest.skip.Exception:
        pass
    try:
        files += samples.note_files()
    except pytest.skip.Exception:
        pass
    if not files:
        pytest.skip("no sample files available")
    for path in files:
        proc = run_cli("info", str(path), "--json")
        assert proc.returncode == 0, f"{path.name}: {proc.stderr[-500:]}"
        info = json.loads(proc.stdout)
        expected_format = GOODNOTES if path.suffix == ".goodnotes" else NOTABILITY
        assert info["format"] == expected_format, path.name
        assert info["file"] == path.name
        assert len(info["pages"]) == info["totals"]["pages"] >= 1, path.name
        for page in info["pages"]:
            assert page["width"] > 0 and page["height"] > 0, path.name
            assert page["paper"] in ("plain", "lined", "grid", "dotted"), path.name
            if page["builtin_template"]:
                assert page["background"] is not None, path.name
        totals = info["totals"]
        assert totals["strokes"] == sum(p["strokes"] for p in info["pages"]), path.name
        assert totals["images"] == sum(p["images"] for p in info["pages"]), path.name
        assert totals["texts"] == sum(p["texts"] for p in info["pages"]), path.name
        assert all(isinstance(w, str) and "\n" not in w for w in info["warnings"]), path.name
        expected = samples.expected_for(path, INFO_EXPECTED)
        if expected is not None:
            assert totals == expected, path.name
        pages = samples.expected_for(path, PAGES_EXPECTED)
        if pages is not None:
            listed = [(round(p["width"], 2), round(p["height"], 2), p["paper"], p["builtin_template"]) for p in info["pages"]]
            assert listed == pages, path.name
