"""The web page's translation table: every language has every string, with the same placeholders."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

WEB = Path(__file__).resolve().parent.parent / "web"
# deliberately empty: the converter's warnings are in English, so English needs no note
ALLOWED_EMPTY = {("en", "result.warnings.lang")}


def _tables() -> Dict[str, Dict[str, str]]:
    text = (WEB / "i18n.js").read_text(encoding="utf-8")
    body = text[text.index("export const STRINGS = {"):]
    tables: Dict[str, Dict[str, str]] = {}
    lang = None
    for line in body.splitlines():
        start = re.match(r"^  ([a-z]{2}): \{$", line)
        if start:
            lang = start.group(1)
            tables[lang] = {}
            continue
        if line == "  },":
            lang = None
            continue
        entry = re.match(r'^    "([^"]+)": "((?:[^"\\]|\\.)*)",$', line)
        if lang and entry:
            assert entry.group(1) not in tables[lang], f"{lang}: duplicate key {entry.group(1)}"
            tables[lang][entry.group(1)] = entry.group(2)
    return tables


def _declared_langs() -> list:
    text = (WEB / "i18n.js").read_text(encoding="utf-8")
    return re.findall(r'"([a-z]{2})"', re.search(r"export const LANGS = \[([^\]]*)\]", text).group(1))


def test_every_language_has_every_key_with_the_same_placeholders() -> None:
    tables = _tables()
    assert list(tables) == _declared_langs()
    reference = tables["sk"]
    assert len(reference) > 50
    for lang, table in tables.items():
        assert set(table) == set(reference), (lang, set(table) ^ set(reference))
        for key, value in table.items():
            assert value.strip() or (lang, key) in ALLOWED_EMPTY, (lang, key)
            assert set(re.findall(r"\{(\w+)\}", value)) == set(re.findall(r"\{(\w+)\}", reference[key])), (lang, key)


def test_page_and_script_use_only_known_keys() -> None:
    keys = set(_tables()["sk"])
    html = (WEB / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n(?:-title|-aria|-placeholder)?="([^"]+)"', html))
    script = (WEB / "app.js").read_text(encoding="utf-8")
    used |= set(re.findall(r'\bt\("([^"]+)"\s*[,)]', script))  # literal keys, not t("stats." + k)
    used |= set(re.findall(r'"(engine\.[a-z]+)"', script))
    assert used and not used - keys, sorted(used - keys)
