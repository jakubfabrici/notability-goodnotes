"""docs/windows.md, the README's Windows section and the web page's "Windows apps" card: they
say only what gnnote does, and say how sure each unchecked route is."""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from tests.test_web_i18n import _tables

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "windows.md"
TOOL = ROOT / "tools" / "make_windows_testkit.py"


def _kit():
    name = "make_windows_testkit"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while the class is created
    spec.loader.exec_module(module)
    return module


def _sections() -> Dict[str, str]:
    """``{"## heading": body}`` of docs/windows.md."""
    text = DOC.read_text(encoding="utf-8")
    parts = re.split(r"^(## .+)$", text, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _card() -> List[Tuple[str, str]]:
    """``(app names, i18n key)`` for every line of the Windows card in web/index.html."""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    card = re.search(r'<details class="card windows" id="windows">(.*?)</details>', html, re.S).group(1)
    return re.findall(r'<li><strong>([^<]+)</strong>: <span data-i18n="([^"]+)"></span></li>', card)


# ---------------------------------------------------------------------------------------------
# the web card


def test_card_is_short_and_fully_translated() -> None:
    lines = _card()
    assert 6 <= len(lines) <= 10
    tables = _tables()
    for _, key in lines:
        for lang, table in tables.items():
            assert table.get(key, "").strip(), (lang, key)


def test_card_names_the_files_gnnote_reads_for_rnote() -> None:
    """Rnote's own .rnote files are not supported: the card must send Rnote users to .xopp in
    every language, not promise "their own files"."""
    rnote = [key for names, key in _card() if "Rnote" in names]
    assert len(rnote) == 1
    for lang, table in _tables().items():
        text = table[rnote[0]]
        assert "Rnote" in text and ".xopp" in text and ".rnote" in text, (lang, text)
        for extension in (".sba", ".excalidraw"):
            assert extension in text, (lang, text)


def test_rnote_files_are_not_a_supported_format() -> None:
    """What the card and the docs say: gnnote has no .rnote reader (update both if one comes)."""
    from gnnote import formats

    assert not any(".rnote" in f.input_extensions for f in formats.FORMATS.values())
    assert "Rnote's own `.rnote` format is not read" in _flat(_sections()["## Xournal++, Rnote, Saber and Excalidraw"])


# ---------------------------------------------------------------------------------------------
# docs/windows.md


APP_SECTIONS = ("## Goodnotes for Windows", "## Noteastic", "## Microsoft OneNote", "## Microsoft Journal",
                "## PDF apps: Drawboard, Xodo, Acrobat, Edge and others", "## LiquidText, Chrome and other PDF tools",
                "## Xournal++, Rnote, Saber and Excalidraw", "## Stylus Labs Write", "## Samsung Notes",
                "## Nebo (MyScript)")


def test_every_app_section_says_what_is_unchecked() -> None:
    sections = _sections()
    for heading in APP_SECTIONS:
        body = _flat(sections[heading])
        assert re.search(r"\byet\b|\bunknown\b", body, re.I), heading


def test_unverified_claims_are_labelled() -> None:
    sections = _sections()
    glance = sections["## At a glance"]
    noteastic = next(line for line in glance.splitlines() if line.startswith("| [Noteastic]"))
    assert "PDF export → gnnote: probably visible only" in noteastic
    onenote = _flat(sections["## Microsoft OneNote"])
    assert "not yet verified on a device" in onenote and "also works on the iPad" not in onenote
    samsung = _flat(sections["## Samsung Notes"])
    assert "not yet been tried with a `.sdocx` made on Windows" in samsung
    others = _flat(sections["## Apps gnnote cannot help with"])
    assert "Plumbago exports pictures only" in others and "Their PDF exports" not in others


def test_how_to_test_matches_the_kit() -> None:
    kit = _kit()
    body = _sections()["## How to test on Windows"]
    listed = re.findall(r"^   \| `([^`]+)` \|", body, re.M)
    expected = [f.name for f in kit.kit_files() if f.format_id != "styluswrite"]
    assert [name for name in listed if name not in ("gnnote-test.svgz", "CHECKLIST.txt")] == expected
    assert listed[-1] == "CHECKLIST.txt" and "gnnote-test.svgz" in listed
    flat = _flat(body)
    # what a tester sees that is gnnote's doing, not the Windows app's
    assert "in `gnnote-test.goodnotes` the wavy strokes are not thinner at the ends" in flat
    # the PDF-page .note is untested on devices (README.md, docs/design.md): the page says so,
    # and the plain-paper .note is described as what it is
    assert "`gnnote-test.note` uses Notability's own plain paper" in flat
    assert "page 2 has no lines" in flat
    assert "that mode is untested on devices: no gnnote `.note` with PDF pages has been opened on an iPad yet" in flat
    # only ink on one plain page has been seen working on an iPad (README.md): the text box and
    # second page both .note files carry are named as untested, and a failure is not pinned on
    # the PDF pages alone
    assert "each has a text box and a second page" in flat
    assert "if both fail, look first at what they share, the text box and the second page" in flat
    assert "not about the other files" not in flat and "already seen working" not in flat
    rows = dict(re.findall(r"^   \| `([^`]+)` \| (.+) \|$", body, re.M))
    assert kit.PLAIN_NOTE_PROVEN in rows["gnnote-test.note"]
    assert kit.PLAIN_NOTE_UNTESTED in rows["gnnote-test.note"]
    assert "whether it opens at all" in rows["gnnote-test-pdfpages.note"]
    assert "python3 tools/make_windows_testkit.py gnnote-kit" in body


def test_links_inside_the_page_resolve() -> None:
    text = DOC.read_text(encoding="utf-8")
    anchors = set()
    for heading in re.findall(r"^#{1,3} (.+)$", text, re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        anchors.add(slug)
    targets = re.findall(r"\]\(#([^)]+)\)", text)
    assert targets
    assert not [t for t in targets if t not in anchors], sorted(set(targets) - anchors)


# ---------------------------------------------------------------------------------------------
# README


def test_readme_section_hedges_and_links() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _flat(readme.split("\n## Windows apps\n")[1].split("\n## ")[0])
    assert "[docs/windows.md](docs/windows.md)" in section
    assert "medium for Drawboard PDF and Edge" in section
    assert "Rnote through its `.xopp` import and export" in section
    assert "none has been checked yet" in section
