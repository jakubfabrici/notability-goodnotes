"""Shared pytest fixtures.

Reference sample files come from third-party repositories.  They are never vendored:
set ``GNNOTE_SAMPLES`` to a directory that already holds the clones (one sub-directory per
repository, named after the repository, directly under it or under ``more-notes/``) or let
the fixture clone them into ``tests/.samples``.  Every repository is pinned to one commit
(:data:`REPOS`) so the corpus, and with it every exact expectation in the tests, is the
same on every machine; a clone is fetched at exactly that commit.  A pre-existing clone is
never modified: :meth:`SampleSet.repo_commit` tells the tests which commit it is at, and
:meth:`SampleSet.expected_for` withholds exact per-file expectations from a repository that
sits at a different commit (the invariants still apply).  Tests that need a repository
that is unavailable are skipped; ``GNNOTE_OFFLINE=1`` disables cloning.

Single files too large for every test run (:data:`LARGE_FILES`, e.g. a 100 MB notebook kept
in Git LFS) are used when they sit directly under the samples directory, and downloaded
there from a URL naming a pinned commit only when ``GNNOTE_LARGE_SAMPLES=1`` is set (and
``GNNOTE_OFFLINE`` is not); a file is used only when its size and SHA-256 match the pin.
CI never sets the variable, so those tests skip there.

Some sample repositories carry no licence (``YTU-Archive``, ``Flashcard`` and the notebook
in :data:`LARGE_FILES`): their files are test inputs fetched at test time only and are
never committed to this repository.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

T = TypeVar("T")

# name -> (clone URL, pinned commit).  goodparse 779ead4 (2026-10-02) is the first commit with
# the schema-25/35 samples Test6 .. Test9 and GoodNotes' own PDF exports next to them.
REPOS: Dict[str, Tuple[str, str]] = {
    "goodparse": ("https://github.com/franzthiemann/goodparse",
                  "779ead480a9f3922a5e51f81b178732119ff8631"),
    "parser-for-goodnotes": ("https://github.com/Kaih1825/parser-for-goodnotes",
                             "66028455fd7fc61b7931679915d811a25485e3ad"),
    "notability-to-svg": ("https://github.com/HuyNguyenAu/notability-to-svg",
                          "15a327e244d4f25bf063a93898b8517a1cd534d1"),
    "notability-reader": ("https://github.com/xrayshan/notability-reader",
                          "db442a5b7effcf09f46a93a5b504bb35f5853db2"),
    "svg2notability": ("https://github.com/jvns/svg2notability",
                       "5a9f4ff13d8d8674f9dbef5bea889de36a41ae45"),
    "notesconverter": ("https://github.com/samuelsadok/notesconverter",
                       "95be29df27b182a130daf825d92bab01ed27b640"),
    "Notability-notes-converter": ("https://github.com/nokcha0/Notability-notes-converter",
                                   "bb875838f481c9df11951d9ee6aef4c9eb2a5eb4"),
    "inkterop": ("https://github.com/cable729/inkterop",
                 "f16eb8d2a425637aab629e6cc90c00a18f17009f"),
    "denotability": ("https://github.com/miroreo/denotability",
                     "7c44cfd5627b4875b7bcbd262c3997fb130b001e"),
    # CollaNote notes (no licence; test input only): 7 ZIP notes and 3 format-2 packages
    # of 2025-2026 over lecture slides, in 1-2/Semiconductor/slide.
    "YTU-Archive": ("https://github.com/enisogdum/YTU-Archive",
                    "81e30df7e8f54a4d7a178eb0baef97f56c12d0e2"),
    # Cards.cards (no licence; test input only): a bplist holding 854 PencilKit drawings
    # written on iOS (lasso transforms, deleted strokes, one version-2 drawing).
    "Flashcard": ("https://github.com/r987r/Flashcard",
                  "f74f6e8df09265f5a0f3cd2def49a5d9353bd40d"),
}

# Repositories too large to check out whole: only these directories are checked out (a
# partial clone without blobs plus a cone-mode sparse checkout, so only their files are
# downloaded).  Repositories not listed here are checked out completely.
SPARSE: Dict[str, Tuple[str, ...]] = {
    "YTU-Archive": ("1-2/Semiconductor/slide",),  # 63 MB of a much larger repository
}

# name -> (URL at a pinned commit, SHA-256, size in bytes); see the module docstring.
LARGE_FILES: Dict[str, Tuple[str, str, int]] = {
    # Kinjalrk2k/100-Days-of-Machine-Learning-Campus-X @ 22642ef, _backup/Notes.cnote (Git LFS,
    # no licence; test input only): a 112-page CollaNote notebook on blank paper.
    "collanote-notebook.cnote": (
        "https://media.githubusercontent.com/media/Kinjalrk2k/100-Days-of-Machine-Learning-Campus-X/"
        "22642ef38b5c61baa48446cb406ebd56c0870f2b/_backup/Notes.cnote",
        "66ac18e02f18fddf83015247d41f6919a8de0f84cd6ed51c33551ce0f5061445",
        100_110_925,
    ),
}


def _samples_root() -> Path:
    env = os.environ.get("GNNOTE_SAMPLES")
    return Path(env) if env else ROOT / "tests" / ".samples"


def _find_repo(root: Path, name: str) -> Optional[Path]:
    """A repository may sit directly under root or under root/more-notes."""
    for candidate in (root / name, root / "more-notes" / name):
        if candidate.is_dir():
            return candidate
    return None


def clone_pinned(target: Path, url: str, sha: str, sparse: Optional[Sequence[str]] = None,
                 timeout: float = 300.0) -> None:
    """Check out exactly commit ``sha`` of ``url`` into the new directory ``target``.

    Uses a shallow fetch of the single commit (GitHub serves any full SHA that way) and
    falls back to fetching the whole history when the server refuses an unadvertised
    object.  ``sparse`` (see :data:`SPARSE`) limits the checkout to those directories and
    fetches no other file contents.  Raises ``subprocess.CalledProcessError`` / ``OSError``
    on failure; the directory is left to the caller to clean up.
    """
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(target), *args], check=True, timeout=timeout, capture_output=True)

    target.mkdir(parents=True, exist_ok=False)
    subprocess.run(["git", "init", "-q", str(target)], check=True, timeout=timeout, capture_output=True)
    git("remote", "add", "origin", url)
    blobs: Tuple[str, ...] = ()
    if sparse:
        git("sparse-checkout", "set", "--cone", *sparse)
        blobs = ("--filter=blob:none",)  # the checkout then fetches only the sparse files
    try:
        git("fetch", "-q", "--depth", "1", *blobs, "origin", sha)
    except subprocess.CalledProcessError:
        git("fetch", "-q", *blobs, "origin")
        git("checkout", "-q", "--detach", sha)
        return
    git("checkout", "-q", "--detach", "FETCH_HEAD")


def _clone(root: Path, name: str) -> Optional[Path]:
    if os.environ.get("GNNOTE_OFFLINE"):
        return None
    target = root / name
    url, sha = REPOS[name]
    try:
        clone_pinned(target, url, sha, SPARSE.get(name))
    except Exception:  # noqa: BLE001 - offline, blocked or git missing; the caller skips
        shutil.rmtree(target, ignore_errors=True)
        return None
    return target


def fetch_large_file(target: Path, url: str, sha256: str, size: int, timeout: float = 600.0) -> None:
    """Download ``url`` to ``target``; keep it only when its size and SHA-256 match the pin.

    Raises ``OSError`` / ``ValueError`` on failure (no partial file is left behind).
    """
    import urllib.request

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, open(part, "wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
                if out.tell() > size:
                    raise ValueError(f"{url} is larger than the pinned {size} bytes")
        if part.stat().st_size != size or digest.hexdigest() != sha256:
            raise ValueError(f"{url} does not match its pinned SHA-256")
        part.replace(target)
    finally:
        if part.exists():
            part.unlink()


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head(path: Path) -> Optional[str]:
    """HEAD of the repository rooted exactly at ``path`` (``None`` for a plain directory)."""
    if not (path / ".git").exists():
        return None  # never report the enclosing repository's HEAD for a bare copy
    try:
        proc = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=30)
    except Exception:  # noqa: BLE001 - git missing or unusable
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


@pytest.fixture(scope="session")
def samples() -> "SampleSet":
    return SampleSet(_samples_root())


class SampleSet:
    """Lazily resolves reference repositories and the sample files inside them."""

    def __init__(self, root: Path):
        self.root = root
        self._commits: Dict[str, Optional[str]] = {}
        self._large: Dict[str, bool] = {}

    def repo(self, name: str) -> Path:
        found = _find_repo(self.root, name) or _clone(self.root, name)
        if found is None:
            pytest.skip(f"reference repository {name} not available (set GNNOTE_SAMPLES or allow network)")
        return found

    def repo_commit(self, name: str) -> Optional[str]:
        """The commit a present clone of ``name`` is at; ``None`` when absent, not a git
        checkout, or git cannot tell.  Never clones."""
        if name not in self._commits:
            path = _find_repo(self.root, name)
            self._commits[name] = _git_head(path) if path is not None else None
        return self._commits[name]

    def at_pinned_commit(self, name: str) -> Optional[bool]:
        """``True``/``False`` when the clone's commit is known, ``None`` when it is not."""
        commit = self.repo_commit(name)
        return None if commit is None else commit == REPOS[name][1]

    def repo_name_of(self, path: Path) -> Optional[str]:
        """The reference repository a sample file belongs to (``None`` when it is loose)."""
        resolved = Path(path).resolve()
        for name in REPOS:
            repo = _find_repo(self.root, name)
            if repo is not None and repo.resolve() in resolved.parents:
                return name
        return None

    def expected_for(self, path: Path, table: Mapping[str, T]) -> Optional[T]:
        """``table[path.name]`` unless the file's repository is known to be at a commit other
        than the pinned one (its content may then differ from what the table pins)."""
        name = self.repo_name_of(path)
        if name is not None and self.at_pinned_commit(name) is False:
            return None
        import unicodedata

        base = Path(path).name
        found = table.get(base)
        return found if found is not None else table.get(unicodedata.normalize("NFC", base))  # macOS: NFD names

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

    def collanote_notes(self) -> List[Path]:
        """YTU-Archive's CollaNote notes: ``.cnote`` ZIP files and format-2 package directories."""
        folder = self.repo("YTU-Archive") / "1-2" / "Semiconductor" / "slide"
        notes = sorted(folder.glob("*.cnote")) if folder.is_dir() else []
        if not notes:
            pytest.skip("no CollaNote sample notes available")
        return notes

    def pkdrawing_fixtures(self) -> List[Path]:
        """inkterop's PencilKit fixtures (CC0), each next to its ``.truth.json``."""
        folder = self.repo("inkterop") / "core" / "tests" / "fixtures" / "pkdrawing"
        blobs = sorted(folder.glob("*.pkdrawing")) if folder.is_dir() else []
        if not blobs:
            pytest.skip("inkterop PencilKit fixtures not available")
        return blobs

    def large_file(self, name: str) -> Path:
        """A :data:`LARGE_FILES` entry, verified against its pin (downloaded on request)."""
        url, sha256, size = LARGE_FILES[name]
        path = self.root / name
        if not path.is_file():
            if os.environ.get("GNNOTE_OFFLINE") or not os.environ.get("GNNOTE_LARGE_SAMPLES"):
                pytest.skip(f"large sample {name} not available (GNNOTE_LARGE_SAMPLES=1 downloads it)")
            try:
                fetch_large_file(path, url, sha256, size)
            except Exception as exc:  # noqa: BLE001 - offline or blocked: skip
                pytest.skip(f"large sample {name} could not be downloaded ({exc})")
        if name not in self._large:
            self._large[name] = path.stat().st_size == size and _sha256_of(path) == sha256
        if not self._large[name]:
            pytest.skip(f"{path} does not match the pinned SHA-256 of {name}")
        return path


def pytest_report_header(config: pytest.Config) -> List[str]:
    """Show which reference clones are present and whether they sit at the pinned commit."""
    root = _samples_root()
    found = SampleSet(root)
    lines: List[str] = []
    for name, (_url, sha) in REPOS.items():
        if _find_repo(root, name) is None:
            continue
        commit = found.repo_commit(name)
        if commit is None:
            state = "commit unknown"
        elif commit == sha:
            state = f"{commit[:7]} (pinned)"
        else:
            state = f"{commit[:7]} (pinned commit is {sha[:7]}; exact per-file expectations withheld)"
        lines.append(f"  {name}: {state}")
    for name in LARGE_FILES:
        if (root / name).is_file():
            lines.append(f"  {name}: present (checked against its pinned SHA-256 when used)")
    if lines:
        lines.insert(0, f"reference samples: {root}")
    return lines
