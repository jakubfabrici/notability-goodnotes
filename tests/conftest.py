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
    # The Saber (GPL-3.0) and Xournal++ (GPL-2.0) app repositories are used only for their
    # test files, as external test data fetched here; none of their files or code is part of
    # gnnote.
    "saber": ("https://github.com/saber-notes/saber",
              "f143d84b46cb6faf795b13c00aee6d31f69e5da9"),
    "xournalpp": ("https://github.com/xournalpp/xournalpp",
                  "9882ffaaf2c012a1de4c33161eb4284468d84b9d"),
    # OneNote sample sections (docs/onenote.md): MPL-2.0 / AGPL / LGPL / MIT / Apache-2.0
    # files used only as external test data, never copied into this repository.
    "onenote.rs": ("https://github.com/msiemens/onenote.rs",
                   "fa4d7a044324af3bfe68727704a9789a08b36a3c"),
    "joplin": ("https://github.com/laurent22/joplin",
               "b04a5f04890a71c8929db4b0ccd45369ea983ae4"),
    "Interop-TestSuites": ("https://github.com/OfficeDev/Interop-TestSuites",
                           "fe87ed3253de01804a2ae6e1d0015943da6023f8"),
    "libmson": ("https://github.com/blu-base/libmson",
                "37bc22d6c98f17eac451c4330aac494e60990a6c"),
    "obsidian-importer": ("https://github.com/obsidianmd/obsidian-importer",
                          "d2cb052c365999118c75c998254fcbb1c15a13fe"),
    "py-onenote-parser": ("https://github.com/Kev744/py-onenote-parser",
                          "04c935cd79c5290e758418817d792f18c33c4fbe"),
}

# Repositories too large to check out whole: only these directories are checked out (a
# partial clone without blobs plus a cone-mode sparse checkout, so only their files are
# downloaded; cone mode also brings the files directly in each listed directory's parents).
# Repositories not listed here are checked out completely.
SPARSE: Dict[str, Tuple[str, ...]] = {
    "flexcil-backup-viewer": ("puplic",),  # cone mode adds the top-level files, forms.flx among them
    "flexcil-codex-plugin": ("plugins/flexcil-codex-plugin/src",),
    "rmscene": ("src", "tests/data"),
    "rmc": ("tests/rm",),
    "RM-Sticker-Press": ("RM-sticker-press/samples",),
    "YTU-Archive": ("1-2/Semiconductor/slide",),  # 63 MB of a much larger repository
    "saber": ("test/sbn_examples", "test/demo_notes"),
    "xournalpp": ("test/files/load", "test/files/packaged_xopp"),
    "onenote.rs": ("crates/parser/tests/samples",),
    "joplin": ("packages/onenote-converter/test-data",),
    "Interop-TestSuites": ("FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources",),
    "libmson": ("resources",),
    "obsidian-importer": ("tests/onenote-file/fixtures",),
}

# Repositories whose download is large (their sample sits at the top level, so a sparse
# checkout cannot leave it out): used when present under $GNNOTE_SAMPLES, cloned only with
# GNNOTE_LARGE_SAMPLES=1 (CI skips them), so the tests that need them usually skip.
LARGE: Dict[str, str] = {
    "py-onenote-parser": "a 46 MB OneNote section (performance test)",
}

# name -> (URL at a pinned commit, SHA-256, size in bytes); see the module docstring.
LARGE_FILES: Dict[str, Tuple[str, str, int]] = {
    # Kinjalrk2k/100-Days-of-Machine-Learning-Campus-X @ 22642ef, _backup/Notes.cnote (Git LFS,
    # no licence; test input only): a 112-page CollaNote notebook on lined paper, without PDFs.
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
    if name in LARGE and not os.environ.get("GNNOTE_LARGE_SAMPLES"):
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

    def xournalpp_files(self) -> List[Path]:
        """Xournal++'s own test files (``test/files``): gzip, plain-XML and ZIP-packaged."""
        root = self.repo("xournalpp") / "test" / "files"
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in (".xopp", ".xoj"))
        if not files:
            pytest.skip("no Xournal++ sample files available")
        return files

    def saber_files(self) -> List[Path]:
        """Saber's own example notes (``test/sbn_examples``, ``test/demo_notes``)."""
        root = self.repo("saber") / "test"
        files = sorted(p for sub in ("sbn_examples", "demo_notes") for p in (root / sub).glob("*")
                       if p.is_file() and p.suffix in (".sbn", ".sbn2"))
        if not files:
            pytest.skip("no Saber sample files available")
        return files

    def inkterop_fixture(self, *parts: str) -> Path:
        """A CC0 fixture of inkterop (``core/tests/fixtures/...``)."""
        path = self.repo("inkterop").joinpath("core", "tests", "fixtures", *parts)
        if not path.is_file():
            pytest.skip(f"inkterop fixture {'/'.join(parts)} not available")
        return path

    # (repository, sample directory) of the OneNote sections, both packagings
    ONENOTE_DIRS = (("onenote.rs", "crates/parser/tests/samples"), ("joplin", "packages/onenote-converter/test-data"),
                    ("Interop-TestSuites", "FileSyncandWOPI/Source/MS-ONESTORE/TestSuite/Resources"),
                    ("libmson", "resources"), ("obsidian-importer", "tests/onenote-file/fixtures"),
                    ("py-onenote-parser", "."))

    def onenote_files(self, pattern: str = "*.one") -> List[Path]:
        """Every OneNote sample file of the available repositories (``LARGE`` ones only when
        present).  Includes the encrypted section and two Git LFS pointer files (not OneNote)."""
        files: List[Path] = []
        for name, sub in self.ONENOTE_DIRS:
            try:
                base = self.repo(name) / sub
            except pytest.skip.Exception:
                continue
            files += sorted(p for p in base.rglob(pattern) if p.is_file() and ".git" not in p.parts)
        if not files:
            pytest.skip("no OneNote sample files available")
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
