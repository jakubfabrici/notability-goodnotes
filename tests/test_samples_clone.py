"""tests/conftest.py's pinned clone helper, against a local repository (no network)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import clone_pinned

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture()
def source(tmp_path: Path) -> tuple:
    """A two-commit repository that serves single commits and blob-less fetches."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "uploadpack.allowFilter", "true")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    for rel, text in (("keep/a.txt", "a1"), ("keep/deep/b.txt", "b"), ("skip/c.txt", "c"), ("top.txt", "t")):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    pinned = _git(repo, "rev-parse", "HEAD")
    (repo / "keep" / "a.txt").write_text("a2")
    _git(repo, "commit", "-q", "-am", "second")
    return "file://" + str(repo), pinned


def test_full_clone_checks_out_the_pinned_commit(source, tmp_path: Path) -> None:
    url, pinned = source
    target = tmp_path / "full"
    clone_pinned(target, url, pinned)
    assert _git(target, "rev-parse", "HEAD") == pinned
    assert (target / "keep" / "a.txt").read_text() == "a1"
    assert (target / "skip" / "c.txt").is_file()


def test_sparse_clone_checks_out_only_the_listed_directories(source, tmp_path: Path) -> None:
    url, pinned = source
    target = tmp_path / "sparse"
    clone_pinned(target, url, pinned, sparse=("keep",))
    assert _git(target, "rev-parse", "HEAD") == pinned
    assert (target / "keep" / "a.txt").read_text() == "a1"
    assert (target / "keep" / "deep" / "b.txt").read_text() == "b"
    assert not (target / "skip").exists()
    assert (target / "top.txt").is_file()  # cone mode always includes the top-level files
