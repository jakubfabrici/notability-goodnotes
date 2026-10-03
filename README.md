# gnnote

Converts notes between **GoodNotes** (`.goodnotes`) and **Notability** (`.note`) in both
directions while keeping the handwriting **editable** in the target app: strokes are written
as native ink, not as a flattened PDF or image.

Neither app imports the other's format, and both export only PDF or images for exchange.
gnnote reads the real container formats (documented in `docs/`) and writes files the other
app opens as its own. It also writes **PDF** (ink flattened into the pages, or kept as
editable ink annotations) and reads PDF: the pages become PDF-backed pages and the PDF's ink,
shape, highlight and text annotations become editable ink and text boxes.

* Pure Python 3.11+, standard library only, MIT licence, clean-room implementation.
* Runs as a web page in the browser (Pyodide, nothing is uploaded anywhere), as a command-line
  tool, as a Python library, or as a small self-hosted server (also as a Docker container).

## What converts, and what is lossy

| Content | GoodNotes to Notability | Notability to GoodNotes |
|---|---|---|
| Handwriting | Editable Bezier ink; per-point **pressure (width) kept** | Editable ink; widths **flattened to one constant width** per stroke (GoodNotes' flat pen has no per-point width) |
| Highlighter | Kept as highlighter (Notability's own translucency) | Kept as highlighter (alpha 0.5) |
| Pencil | Written as a pen stroke (constant width, tilt dropped) | Written as a ball pen stroke |
| Images (PNG/JPEG) | Kept, position and size; EXIF-rotated photos keep their rotation (unverified in Notability) | Kept, position and size; a rotation is kept only when the JPEG's EXIF orientation prescribes it |
| Vector stickers (PDF images) | Dropped | Kept as PDF image attachments |
| Shape fills (the translucent fill of GoodNotes auto-shapes) | Dropped (Notability has no filled shapes) | Kept |
| Text boxes | Kept with bold/italic/underline, font, size, colour, alignment and rotation; includes the text stickers of the 2026 GoodNotes builds | Kept (RTF) with alignment; rotation dropped |
| User-imported PDFs | Always carried as PDF-backed pages | Always carried as PDF pages (multi-page PDFs stay one attachment) |
| Stock paper | `--paper plain` (default): Notability plain paper, page scaled to fit; `--paper pdf`: the GoodNotes paper PDF behind every page | Plain generated paper (Notability's own template PDFs are carried as PDF pages) |

GoodNotes notebooks written by GoodNotes 5 and 6 up to the 2026 builds (container schema 24,
25 and 35) and Notability notes from version 4 to 16 are read.

### PDF

| Content | Note to PDF (`--to pdf`) | PDF to note (`gnnote convert file.pdf`) |
|---|---|---|
| Pages | One PDF page per page, same size | One PDF-backed page per PDF page (rotated pages as displayed) |
| Handwriting | `--pdf-ink flatten` (default): drawn into the page, per-point widths kept; `--pdf-ink annotations`: one ink annotation per stroke, editable in PDF apps, drawn exactly like the flattened ink | Ink, line, polyline, polygon, square and circle annotations become editable strokes (GoodNotes' shapes keep their exact curves); ink drawn *into* the page content stays part of the background |
| Highlighter | Translucent, multiplied over the page | Highlight annotations and translucent / multiply ink become highlighter strokes |
| Images | JPEG unchanged, PNG with transparency, PDF stickers as vectors; photos upright | Stay part of the PDF page |
| Text boxes | Real text (searchable): Helvetica, or an embedded DejaVu Sans subset for Slovak, Czech, Ukrainian, Greek, ...; bold, italic, underline, alignment, rotation | Free-text annotations become text boxes |
| Backgrounds | User PDFs and paper templates imported as vector pages (rotation and crop origin honoured) | -- |
| Other annotations | -- | Links, stamps, notes and form fields stay in the PDF; the converted annotations are removed from it by an incremental update |

Encrypted PDFs are carried as they are, with their annotations left in the pages. Details:
`docs/pdf.md`.

Dropped, with a warning in the output: audio recordings, stickers / sticky notes, stroke
dash patterns (drawn solid), Notability vector shapes, math objects, image crops and flips.
GoodNotes auto-shapes are converted silently: they become ordinary strokes drawn along the
shape (straight sides stay straight; ellipses are sampled), only their translucent fill is
dropped, with a warning.  Every other lossy step adds one human-readable warning, shown in
the web page, printed by the CLI and returned in the `X-GnNote-Warnings` header of the server.

## Verification status

GoodNotes -> Notability ink import confirmed on the author's iPad with a one-page test;
everything else is validated against third-party parsers and sample files only, not on
devices. PDF output and input are checked with MuPDF (PyMuPDF, as a test oracle: renders,
text extraction, no warnings) and against GoodNotes' own PDF exports of the sample notebooks.

## Quick start

### Web page (no installation)

`.github/workflows/pages.yml` publishes the page to GitHub Pages at
`https://<user>.github.io/notability-goodnotes/` on every push to `main`. Two one-time
settings are needed first: Settings -> Pages -> Source "GitHub Actions", and either a public
repository or a paid GitHub plan (GitHub Free has no Pages for private repositories). Until
then the workflow ends with a notice and deploys nothing. Open the page on the iPad (or any
browser), choose or drop a `.goodnotes` or `.note` file, pick the options, press Convert and
download or share the result into the other app. The conversion runs in the browser with
Pyodide (about 13.5 MB downloaded on first use); files never leave the device.

To serve the page yourself:

```sh
python3 scripts/build_web.py --out dist          # web/ + gnnote.zip, Pyodide from the CDN
python3 -m gnnote.server --dist dist             # http://127.0.0.1:8000/
```

`--vendor-pyodide` downloads the Pyodide runtime into `dist/pyodide/` for an offline
deployment.

### Command line

```sh
python3 -m gnnote convert Notebook.goodnotes                  # writes Notebook.note next to it
python3 -m gnnote convert Note.note -o out/ --title "Maths"   # writes out/Note.goodnotes
python3 -m gnnote convert Notebook.goodnotes --paper pdf --no-pressure --simplify 0.3
python3 -m gnnote info Note.note [--json]                     # format, title, pages, counts, warnings
python3 -m gnnote batch ~/Notes -o ~/Converted --to notability
python3 -m gnnote convert Notebook.goodnotes --to pdf --pdf-ink annotations   # editable ink in PDF apps
python3 -m gnnote convert Annotated.pdf                       # Annotated.note with the PDF's ink editable
python3 -m gnnote batch ~/PDFs --include-pdf --to goodnotes   # batch skips .pdf files unless asked
```

Exit codes: 0 success, 1 a file could not be read or converted, 2 usage error. Warnings go
to stderr, one per line. `pip install .` also installs a `gnnote` console script.

### Python

```python
from gnnote.convert import convert, Options

result = convert(open("Notebook.goodnotes", "rb").read(), "Notebook.goodnotes", Options(paper="plain"))
open(result.filename, "wb").write(result.data)   # Notebook.note
print(result.stats, result.warnings)
```

### Server

```sh
python3 -m gnnote.server --host 0.0.0.0 --port 8000 [--dist dist]
curl -F file=@Notebook.goodnotes "http://127.0.0.1:8000/api/convert?paper=plain" -o Notebook.note
```

`GET /api/health` answers `{"ok": true, "version": ..., "maxUpload": bytes}`; `POST /api/convert` takes a multipart `file` (up to
300 MB; larger uploads are refused by the page before uploading) plus `to`, `paper`, `pressure`, `simplify`, `pdf_ink` and `title` as query or form parameters and
returns the converted file with `X-GnNote-Warnings` and `X-GnNote-Stats` JSON headers. The
web page uses the server automatically when it is served by `gnnote.server`, and Pyodide
otherwise.

### Docker

```sh
docker compose up -d        # builds the image (vendored Pyodide) and serves port 8000
```

## Options

| CLI | Web / API | Meaning |
|---|---|---|
| `--paper plain` (default) | `paper=plain` | GoodNotes stock paper becomes Notability plain paper; pages are scaled to Notability's page. User PDFs are always kept. |
| `--paper pdf` | `paper=pdf` | Every page becomes a PDF-backed Notability page using the GoodNotes paper PDF (keeps ruled paper and page size; untested on devices). |
| `--no-pressure` | `pressure=false` | Constant-width Notability strokes instead of per-point widths. |
| `--simplify PT` | `simplify=PT` | Simplify polylines with this tolerance (points) before Bezier fitting; 0 = off. |
| `--title T` | `title=T` (API only; the web page has no title field) | Name of the output note / notebook (default: the source title). |
| `--ribbon` | | Experimental variable-width GoodNotes strokes; currently falls back to flat strokes with a warning. |
| `--to FORMAT` | `to=FORMAT` | Output format: `goodnotes`, `notability` or `pdf` (default: GoodNotes and Notability swap, PDFs become Notability notes). |
| `--pdf-ink flatten` (default) | `pdf_ink=flatten` | PDF output: ink drawn into the pages (looks the same in every viewer). |
| `--pdf-ink annotations` | `pdf_ink=annotations` | PDF output: every stroke an ink annotation (movable / erasable in PDF apps; drawn identically). |
| `--include-pdf` (`batch` only) | | Also convert the `.pdf` files of the folder (off by default: PDF exports usually sit next to their notebooks and would map to the same output name). |

## Development

```sh
python3 -m pip install -e ".[test]"
python3 -m pytest -q
```

Sample files and oracle parsers come from third-party repositories and are never vendored.
Each repository is pinned to one commit (`REPOS` in `tests/conftest.py`); the test fixture
checks that commit out into `tests/.samples/` on first use, or uses the directory named by
`GNNOTE_SAMPLES` (one sub-directory per repository, left untouched even when it sits at
another commit, in which case the exact per-file expectations are withheld and only the
invariants run; the pytest header reports it). Tests that need a repository that is
unavailable are skipped; `GNNOTE_OFFLINE=1` disables cloning. Oracle parsers (goodparse,
parser-for-goodnotes) run in a subprocess with their own `PYTHONPATH` and are never
imported into the package; parser-for-goodnotes needs `numpy`.

Browser end-to-end test (Playwright, see `tests/e2e/README.md`):

```sh
npm install -g playwright && npx playwright install --with-deps chromium
python3 scripts/build_web.py --out dist
NODE_PATH=$(npm root -g) node tests/e2e/run.js --dist dist --input Notebook.goodnotes --expect-ext .note
```

CI (`.github/workflows/ci.yml`) runs the test suite on Python 3.11 and 3.12 and the browser
test with Chromium, once per pull-request change and on pushes to `main`; `pages.yml` deploys
`dist/` to GitHub Pages on pushes to `main` when Pages is set up.

## Documentation

* `docs/design.md`: module contract, public API, conversion rules, decisions.
* `docs/goodnotes-container.md`, `docs/goodnotes-stroke.md`, `docs/goodnotes-elements.md`:
  the `.goodnotes` container, stroke geometry and page elements, byte by byte.
* `docs/goodnotes-v35-binding.md`, `docs/goodnotes-v35-strokes.md`, `docs/goodnotes-v35-elements.md`:
  what the 2026 GoodNotes builds (container schema 25/35) changed in page binding, strokes and elements.
* `docs/notability-format.md`: the `.note` package and its `Session.plist` object graph.
* `docs/pdf.md`: what the PDF writer produces and what the PDF reader converts, byte by byte.
* `docs/ecosystem.md`: import/export capabilities of both apps, existing tools, licensing.
* `tests/e2e/README.md`: the browser end-to-end test.

## Acknowledgements

The format research was checked against these projects, used as documentation and as
separate-process test oracles (see `NOTICE.md`):
[goodparse](https://github.com/franzthiemann/goodparse),
[parser-for-goodnotes](https://github.com/Kaih1825/parser-for-goodnotes),
[inkterop](https://github.com/cable729/inkterop),
[inkref](https://github.com/juliank1m/inkref),
[notability-to-svg](https://github.com/HuyNguyenAu/notability-to-svg),
[notability-reader](https://github.com/xrayshan/notability-reader),
[notesconverter](https://github.com/samuelsadok/notesconverter),
[Notability-notes-converter](https://github.com/nokcha0/Notability-notes-converter),
[svg2notability](https://github.com/jvns/svg2notability) and
[denotability](https://github.com/miroreo/denotability).
GoodNotes and Notability are trademarks of their respective owners; this project is not
affiliated with either.

## Licence

MIT, see `LICENSE`. Third-party notices are in `NOTICE.md`.
