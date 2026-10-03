#!/usr/bin/env python3
"""Build the static browser bundle of gnnote into dist/.

Output layout (served by any static host, ``python -m gnnote.server`` or GitHub Pages)::

    dist/
      index.html app.js worker.js styles.css i18n.js   copied from web/
      gnnote.zip      the Python package, importable after pyodide.unpackArchive
                      (top-level directory inside the zip is the package name,
                      __pycache__, *.pyc and tests/ are excluded; entries are
                      sorted with a fixed timestamp so the build is reproducible)
      version.json    {"version": ..., "built": ISO-8601 UTC, "pyodide": {...}}
      pyodide/        only with --vendor-pyodide: pyodide.mjs, pyodide.asm.mjs,
                      pyodide.asm.wasm, python_stdlib.zip, pyodide-lock.json

Usage::

    python scripts/build_web.py [--out dist] [--package gnnote] [--web web]
                                [--vendor-pyodide [--pyodide-from DIR] [--pyodide-version V]]
                                [--clean]

Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, List

PYODIDE_VERSION = "314.0.7"
PYODIDE_FILES = (
    "pyodide.mjs",
    "pyodide.asm.mjs",
    "pyodide.asm.wasm",
    "python_stdlib.zip",
    "pyodide-lock.json",
)
EXCLUDED_DIRS = {"__pycache__", "tests", ".pytest_cache", ".git"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej", "~")
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)

REPO_ROOT = Path(__file__).resolve().parent.parent


def pyodide_cdn(version: str) -> str:
    return "https://cdn.jsdelivr.net/pyodide/v%s/full/" % version


def package_files(package_dir: Path) -> List[Path]:
    """All files of the package, sorted, without caches and tests."""
    out: List[Path] = []
    for root, dirs, files in os.walk(package_dir):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        for name in sorted(files):
            if name.endswith(EXCLUDED_SUFFIXES):
                continue
            out.append(Path(root) / name)
    return out


def zip_package(package_dir: Path, zip_path: Path) -> int:
    """Write package_dir into zip_path as <package name>/...; returns the member count."""
    package_dir = package_dir.resolve()
    if not (package_dir / "__init__.py").is_file():
        raise SystemExit("not a Python package (no __init__.py): %s" % package_dir)
    top = package_dir.name
    count = 0
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in package_files(package_dir):
            arcname = top + "/" + path.relative_to(package_dir).as_posix()
            info = zipfile.ZipInfo(arcname, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
            count += 1
    return count


def read_version(package_dir: Path) -> str:
    init = package_dir / "__init__.py"
    try:
        m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", init.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    except OSError:
        pass
    return "0.0.0"


def copy_web(web_dir: Path, out_dir: Path) -> List[str]:
    copied: List[str] = []
    for path in sorted(web_dir.rglob("*")):
        if path.is_dir():
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(web_dir).parts):
            continue
        rel = path.relative_to(web_dir)
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        copied.append(rel.as_posix())
    return copied


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "gnnote-build"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh, length=1 << 20)


def vendor_pyodide(out_dir: Path, version: str, source_dir: Path | None, files: Iterable[str] = PYODIDE_FILES) -> None:
    dest_dir = out_dir / "pyodide"
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = pyodide_cdn(version)
    for name in files:
        dest = dest_dir / name
        if source_dir is not None:
            src = source_dir / name
            if not src.is_file():
                raise SystemExit("missing %s in --pyodide-from directory %s" % (name, source_dir))
            shutil.copy2(src, dest)
            print("copied   %s (%d bytes)" % (name, dest.stat().st_size))
        else:
            print("download %s%s" % (base, name))
            _download(base + name, dest)
            print("         %d bytes" % dest.stat().st_size)


def write_formats(package_dir: Path, out_dir: Path) -> bool:
    """Regenerate formats.js from the package's format registry.

    Runs in a subprocess so the build does not import the package into this interpreter.
    A package without ``formats.py`` (e.g. a test stub) keeps the copied web/formats.js.
    """
    if not (package_dir / "formats.py").is_file():
        return False
    code = ("import sys; sys.path.insert(0, %r); from %s.formats import web_formats_js; "
            "sys.stdout.write(web_formats_js())" % (str(package_dir.parent), package_dir.name))
    text = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout
    (out_dir / "formats.js").write_text(text, encoding="utf-8")
    return True


def build(out_dir: Path, package_dir: Path, web_dir: Path, vendor: bool,
          pyodide_from: Path | None, pyodide_version: str, clean: bool) -> dict:
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    copied = copy_web(web_dir, out_dir)
    print("web:      %d files -> %s" % (len(copied), out_dir))

    zip_path = out_dir / "gnnote.zip"
    n = zip_package(package_dir, zip_path)
    print("package:  %s (%d members, %d bytes)" % (zip_path, n, zip_path.stat().st_size))
    if write_formats(package_dir, out_dir):
        print("formats:  %s" % (out_dir / "formats.js"))

    if vendor:
        vendor_pyodide(out_dir, pyodide_version, pyodide_from)

    version = read_version(package_dir)
    info = {
        "version": version,
        "built": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "package": package_dir.name,
        "pyodide": {"version": pyodide_version, "vendored": bool(vendor), "cdn": pyodide_cdn(pyodide_version)},
    }
    (out_dir / "version.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print("version:  %s" % version)
    return info


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(REPO_ROOT / "dist"), help="output directory (default: dist/)")
    ap.add_argument("--package", default=str(REPO_ROOT / "gnnote"), help="Python package directory to zip")
    ap.add_argument("--web", default=str(REPO_ROOT / "web"), help="static UI source directory")
    ap.add_argument("--vendor-pyodide", action="store_true", help="put the Pyodide runtime into dist/pyodide/")
    ap.add_argument("--pyodide-from", default=None, metavar="DIR",
                    help="copy the Pyodide files from DIR instead of downloading them")
    ap.add_argument("--pyodide-version", default=PYODIDE_VERSION)
    ap.add_argument("--clean", action="store_true", help="remove the output directory first")
    args = ap.parse_args(argv)

    out_dir = Path(args.out).resolve()
    package_dir = Path(args.package).resolve()
    web_dir = Path(args.web).resolve()
    if not web_dir.is_dir():
        print("web directory not found: %s" % web_dir, file=sys.stderr)
        return 2
    pyodide_from = Path(args.pyodide_from).resolve() if args.pyodide_from else None
    vendor = bool(args.vendor_pyodide or pyodide_from)
    build(out_dir, package_dir, web_dir, vendor, pyodide_from, args.pyodide_version, args.clean)
    return 0


if __name__ == "__main__":
    sys.exit(main())
