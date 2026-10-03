# End-to-end test of the browser UI

`run.js` drives the built `dist/` page in headless Chromium with Playwright:
it starts a static server for `dist/`, opens the page, uploads a sample
through the file input, clicks **Convert**, waits for the download triggered
by the **Download** button, saves it and prints one JSON line:

```json
{"ok": true, "outputPath": "...", "name": "test2.note", "size": 23820,
 "stats": {"pages": 1, "strokes": 820, "images": 0, "texts": 0, "pdfs": 0},
 "warnings": [], "sourceFormat": "goodnotes", "targetFormat": "notability",
 "engine": "browser", "shareButton": false, "ms": 2802}
```

Exit code 0 on success; on failure the JSON has `ok: false`, `error` and the
last browser console lines.

## Requirements

* Node 18+ and a global Playwright install (`npm i -g playwright`), used via
  `NODE_PATH=$(npm root -g)`.
* A Chromium binary: `--browser PATH` or `$PW_CHROMIUM`, default
  `/opt/pw-browsers/chromium`. It is launched with `--no-sandbox`.
* A built `dist/` (`python scripts/build_web.py`).

## Running

```sh
python scripts/build_web.py                      # dist/ with gnnote.zip (CDN Pyodide)
NODE_PATH=$(npm root -g) node tests/e2e/run.js --dist dist \
    --input tests/.samples/goodparse/samples/test2.goodnotes --expect-ext .note
```

When Chromium cannot reach `cdn.jsdelivr.net` (sandboxes with a
TLS-intercepting proxy, offline CI), serve a local copy of the five Pyodide
files instead; the script mounts the directory under `/pyodide/` and sets
`window.PYODIDE_BASE` through an init script before the page loads:

```sh
NODE_PATH=$(npm root -g) node tests/e2e/run.js --dist dist \
    --pyodide-dir /path/to/pyodide --input FILE --expect-ext .note
```

A build made with `--vendor-pyodide` already contains `dist/pyodide/`; the page
auto-detects it (HEAD `pyodide/pyodide.mjs`), or point at it explicitly with
`--pyodide-base /pyodide/`.

Other options: `--out PATH` (download target, default `tests/e2e/out/<name>`),
`--paper plain|pdf`, `--no-pressure`, `--simplify N`, `--lang sk|en`,
`--timeout MS`, `--port N`, `--verbose` (echo the browser console).

The static server answers 404 for `api/*`, so the page always takes the
in-browser (Pyodide) engine. To test the server engine, run
`python -m gnnote.server` and open the page manually.

## How the page is observed

`app.js` publishes the last outcome on `window.__gnnoteLastResult`
(`{name, size, stats, warnings, sourceFormat, targetFormat, engine}` or
`{error}`); the script waits for that, then clicks `#download` and uses
Playwright's download event. The downloaded file can be checked further with
`python -m gnnote info OUTPUT`.
