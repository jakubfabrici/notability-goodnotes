"""Shared pytest fixtures.

Reference sample files come from third-party repositories.  They are never vendored:
set ``GNNOTE_SAMPLES`` to a directory that already holds the clones (one sub-directory per
repository, named after the repository) or let the fixture clone them shallowly into
``tests/.samples``.  Tests that need a repository that is unavailable are skipped.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPOS: Dict[str, str] = {
    "goodparse": "https://github.com/franzthiemann/goodparse",
    "parser-for-goodnotes": "https://github.com/Kaih1825/parser-for-goodnotes",
    "notability-to-svg": "https://github.com/HuyNguyenAu/notability-to-svg",
    "notability-reader": "https://github.com/xrayshan/notability-reader",
    "svg2notability": "https://github.com/jvns/svg2notability",
    "notesconverter": "https://github.com/samuelsadok/notesconverter",
    "Notability-notes-converter": "https://github.com/nokcha0/Notability-notes-converter",
    "inkterop": "https://github.com/cable729/inkterop",
    "denotability": "https://github.com/miroreo/denotability",
}


def _samples_root() -> Path:
    env = os.environ.get("GNNOTE_SAMPLES")
    return Path(env) if env else ROOT / "tests" / ".samples"


def _find_repo(root: Path, name: str) -> Path | None:
    """A repository may sit directly under root or under root/more-notes."""
    for candidate in (root / name, root / "more-notes" / name):
        if candidate.is_dir():
            return candidate
    return None


def _clone(root: Path, name: str) -> Path | None:
    if os.environ.get("GNNOTE_OFFLINE"):
        return None
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", REPOS[name], str(target)],
            check=True, timeout=300, capture_output=True,
        )
    except Exception:  # noqa: BLE001 - offline or blocked; the caller skips
        return None
    return target


@pytest.fixture(scope="session")
def samples() -> "SampleSet":
    return SampleSet(_samples_root())


class SampleSet:
    """Lazily resolves reference repositories and the sample files inside them."""

    def __init__(self, root: Path):
        self.root = root

    def repo(self, name: str) -> Path:
        found = _find_repo(self.root, name) or _clone(self.root, name)
        if found is None:
            pytest.skip(f"reference repository {name} not available (set GNNOTE_SAMPLES or allow network)")
        return found

    def goodnotes_files(self) -> List[Path]:
        files: List[Path] = []
        for name, sub in (("goodparse", "samples"), ("parser-for-goodnotes", "assets")):
            try:
                files += sorted((self.repo(name) / sub).glob("*.goodnotes"))
            except pytest.skip.Exception:
                pass
        if not files:
            pytest.skip("no .goodnotes sample files available")
        return files

    def note_files(self) -> List[Path]:
        files: List[Path] = []
        for name in ("notability-to-svg", "svg2notability", "notesconverter",
                     "Notability-notes-converter", "denotability"):
            try:
                files += sorted(p for p in self.repo(name).rglob("*.note") if p.is_file())
            except pytest.skip.Exception:
                pass
        if not files:
            pytest.skip("no .note sample files available")
        return files

    def notability_template(self) -> Path:
        path = self.repo("notability-to-svg") / "example.note"
        if not path.is_file():
            pytest.skip("Notability 10.4 template note not available")
        return path
