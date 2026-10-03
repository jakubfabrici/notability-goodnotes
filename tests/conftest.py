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
"""
from __future__ import annotations

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
    # Flexcil: forms.flx (MIT repository); only the top-level files are needed
    "flexcil-backup-viewer": ("https://github.com/janptn/flexcil-backup-viewer",
                              "8b1c30f432a34ef315ef669cf5eb87a589ae6394"),
    # Flexcil: an MIT Python codec, run in a subprocess as a test oracle
    "flexcil-codex-plugin": ("https://github.com/jeonghyeon-net/flexcil-codex-plugin",
                             "b04abb543d72031e2c1be2ef7c7fc5bd52585c30"),
    # reMarkable: rmscene (MIT) is the v6 oracle (subprocess) and holds sample pages; rmc (MIT)
    # holds more pages; RM-Sticker-Press (GPL-3.0) is used only for its .rmdoc sample files,
    # read as external test data (never copied into this repository)
    "rmscene": ("https://github.com/ricklupton/rmscene", "d7d86ca3a8ca4965d911886a1660bc8acf654c1a"),
    "rmc": ("https://github.com/ricklupton/rmc", "da87813a31496d156ca6ea8a27bf5128670fb45a"),
    "RM-Sticker-Press": ("https://github.com/szainababbas/RM-Sticker-Press",
                         "4ecc7387a07ca384e62cfefd1d670a548cea5314"),
}

# Repositories too large to check out whole: only these directories are checked out (a
# partial clone without blobs plus a cone-mode sparse checkout, so only their files are
# downloaded).  Repositories not listed here are checked out completely.
SPARSE: Dict[str, Tuple[str, ...]] = {
    "flexcil-backup-viewer": ("puplic",),  # cone mode adds the top-level files, forms.flx among them
    "flexcil-codex-plugin": ("plugins/flexcil-codex-plugin/src",),
    "rmscene": ("src", "tests/data"),
    "rmc": ("tests/rm",),
    "RM-Sticker-Press": ("RM-sticker-press/samples",),
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
        return table.get(Path(path).name)

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

    def nebo_files(self) -> List[Path]:
        """MyScript Notes / Nebo packages: inkterop's CC0 fixtures."""
        files = sorted((self.repo("inkterop") / "core" / "tests" / "fixtures" / "nebo").glob("*.nebo"))
        if not files:
            pytest.skip("no .nebo sample files available")
        return files

    def flexcil_files(self) -> List[Path]:
        """Flexcil documents: forms.flx of flexcil-backup-viewer."""
        files = sorted(p for p in self.repo("flexcil-backup-viewer").glob("*.flx") if p.is_file())
        if not files:
            pytest.skip("no .flx sample files available")
        return files

    def remarkable_pages(self) -> List[Path]:
        """reMarkable v6 pages: rmscene's and rmc's test pages and inkterop's CC0 captures."""
        files: List[Path] = []
        for name, sub in (("rmscene", "tests/data"), ("rmc", "tests/rm"),
                          ("inkterop", "core/tests/fixtures/remarkable")):
            try:
                files += sorted((self.repo(name) / sub).glob("*.rm"))
            except pytest.skip.Exception:
                pass
        if not files:
            pytest.skip("no .rm sample files available")
        return files

    def remarkable_documents(self) -> List[Path]:
        """reMarkable .rmdoc documents: RM-Sticker-Press's samples (external test data)."""
        files = sorted((self.repo("RM-Sticker-Press") / "RM-sticker-press" / "samples").glob("*.rmdoc"))
        if not files:
            pytest.skip("no .rmdoc sample files available")
        return files

    def notability_template(self) -> Path:
        path = self.repo("notability-to-svg") / "example.note"
        if not path.is_file():
            pytest.skip("Notability 10.4 template note not available")
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
    if lines:
        lines.insert(0, f"reference samples: {root}")
    return lines
