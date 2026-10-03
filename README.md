# gnnote

Converts notes between **GoodNotes** (`.goodnotes`), **Notability** (`.note`) and **Noteful**
(`.noteful`) in every direction while keeping the handwriting **editable** in the target app:
strokes are written as native ink, not as a flattened PDF or image.

None of these apps imports another's format, and they export only PDF or images for exchange.
gnnote reads the real container formats (documented in `docs/`) and writes files the other
apps open as their own.

| App | File | Read | Write |
|---|---|---|---|
| GoodNotes 5 / 6 | `.goodnotes` | yes | yes |
| Notability 4 to 16 | `.note` | yes | yes |
| Noteful (files of 1.4.25 to 1.4.33) | `.noteful` | yes | yes (not yet opened in Noteful, see below) |
| CollaNote | `.cnote` (the file, or the package folder zipped) | yes | not yet (needs tests in the app) |
| PDF | `.pdf` | yes: ink, shape, highlight and text annotations become editable | yes: ink drawn into the pages, or kept as editable ink annotations |

PDF works in both directions: written PDFs carry the ink either flattened into the pages or as
ink annotations that PDF apps can still move and delete, and a PDF that is read becomes
PDF-backed pages whose ink, shape, highlight and text annotations turn into editable ink
and text boxes.

It also reads and writes the open formats of other note apps, with the ink kept editable
(see [Other apps](#other-apps)): **Xournal++** (`.xopp`, `.xoj`), **Saber** (`.sba`, `.sbn2`,
`.sbn`) and **Excalidraw** (`.excalidraw`).

* Pure Python 3.11+, standard library only, MIT licence, clean-room implementation.
* Runs as a web page in the browser (Pyodide, nothing is uploaded anywhere), as a command-line
  tool, as a Python library, or as a small self-hosted server (also as a Docker container).
* Also reads other apps' files and converts them to GoodNotes or Notability with the ink
  still editable (see [Other apps](#other-apps-read-only)).

## Supported apps

| App | Files | Read | Write | Notes |
|---|---|---|---|---|
| GoodNotes | `.goodnotes` | yes | yes | |
| Notability | `.note` | yes | yes | |
| MyScript Notes (Nebo) | `.nebo` | yes | no | `docs/nebo.md` |
| Flexcil | `.flx`, `.flex` (backup) | yes | no | `docs/flexcil.md` |
| reMarkable | `.rmdoc`, `.rm` (v6 page) | yes | no | `docs/remarkable.md` |

`python3 -m gnnote formats` prints the same list. Files of a read-only app convert to
Notability unless `--to goodnotes` is given.

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

### Noteful

| Content | Noteful to GoodNotes / Notability | GoodNotes / Notability to Noteful |
|---|---|---|
| Handwriting | Editable ink with per-point width (Noteful stores polylines; widths as in the other columns above) | Editable ink; Bezier strokes flattened to 1 pt, per-point width kept |
| Highlighter | Kept (Noteful draws highlighters at 50 % opacity) | Kept as Noteful highlighter |
| Shapes (lines, Beziers, arrows, rectangles, ellipses, polygons) | Ordinary strokes along the exact outline; fills become shape fills (kept by GoodNotes, dropped by Notability); dashes drawn solid | Arrive as ink |
| Shape fills | see Shapes | Filled polygons (unverified in Noteful) |
| Text boxes | Kept with runs (font, size, bold, italic, underline, colour), alignment and rotation; box background colour dropped | Kept as Noteful rich text |
| Images | PNG/JPEG kept; a crop is undone (the whole picture at the scale shown), a flip is dropped | PNG/JPEG kept with rotation; PDF stickers dropped |
| PDF pages and paper | PDF pages carried; Noteful templates carried as the PDF the app rendered | PDF pages carried; plain pages get generated paper PDFs |
| Layers, bookmarks, audio, tags | Layers merged; the rest dropped | Not written |

### CollaNote (read only)

| Content | CollaNote to GoodNotes / Notability |
|---|---|
| Handwriting | Editable ink with per-point widths (GoodNotes then flattens them to one width per stroke) |
| Pens | Pen, translucent wide pen (keeps its transparency) and highlighter; pen types not identified yet are read as pens, with a warning |
| Imported PDFs | Carried as PDF-backed pages; blank pages inserted between slides keep the slides' size |
| Blank paper | Page size from the note (a CollaNote A4 notebook stays A4); ruled paper is kept as a hint |
| Images (PNG/JPEG) | Kept, position and size (rotation direction unverified) |
| Text boxes | Kept with font, size and colour (layout inferred from one sample) |
| Notes from CollaNote 1.x (Apple PencilKit ink) | Read on a best-effort basis (no sample yet), with a warning |
| Audio, bookmarks | Dropped, with a warning |

Both container layouts are read: the `.cnote` ZIP file and the newer `.cnote` package folder,
which arrives zipped (`Note.cnote.zip` converts as is). See `docs/collanote.md` for what is
verified and what is not.

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

## Other apps (read and write)

Every supported app converts to every other one (`--to FORMAT`; without it, files of these
apps become Notability notes). `gnnote formats` lists them.

| Content | Xournal++ (`.xopp`, `.xoj`) | Saber (`.sba`; reads `.sbn2`, `.sbn`) | Excalidraw (`.excalidraw`) |
|---|---|---|---|
| Handwriting | Read and written with **per-point widths** (Xournal++'s segment widths); Bezier ink is flattened to 1 pt polylines | Pressure ink both ways (perfect-freehand width law; ranges above 3:1 compressed); pencil, ballpoint and fountain pens | Freedraw ink with per-point pressure through Excalidraw's width law (ranges above about 3:1 compressed) |
| Highlighter | Kept (Xournal++ highlighter tool) | Kept | Translucent ink (kind kept for gnnote in `customData`) |
| Shape fills | Kept (`fill` attribute of the outline stroke) | Dropped (warning); Saber's circle / rectangle shapes are read | Kept as filled closed lines; Excalidraw's shapes and fills are read |
| Images | PNG / JPEG kept; PDF stickers become LaTeX-style PDF images; rotations need Xournal++ newer than 1.3.8 | PNG / JPEG and PDF images kept; rotations and crops dropped | PNG / JPEG kept with rotation; PDF images dropped |
| Text boxes | Kept with font, size, colour, alignment; box sizes estimated on read | Saber has page text only: boxes become text lines near their position (bold / italic / underline kept) | Kept with size, colour, alignment and rotation; one plain style |
| PDF backgrounds | Read from the ZIP-packaged `.xopp`; a gzip `.xopp` only references its PDF (plain paper, warning). Written into a ZIP-packaged `.xopp` (Xournal++ 1.2 and later); one PDF per document | Kept both ways (assets of the `.sba`; a bare `.sbn2` has none) | Dropped (Excalidraw cannot show PDF) |
| Paper | plain / ruled / graph / dotted styles both ways; other colours as generated paper | One pattern per note (lined, grid, dots); background colour as generated paper | None (pages become frames named "Page N"); canvas colour read as paper colour |
| Dropped (warning) | layers (merged), eraser strokes, audio, links (text kept), LaTeX source, dash styles | SVG images, embedded text objects, image crops | arrowheads, hand-drawn roughness, hatched fills (solid), SVG images, embeds |
| Units | PDF points (1:1) | pages 1000 units wide; read at 0.595 pt per unit (A4 width), PDF pages at the PDF's size | CSS px x 0.75 = pt |

Facts, mapping and what is still unverified in each app: `docs/xournalpp.md`, `docs/saber.md`,
`docs/excalidraw.md`.

## Other apps (read only)

Their formats are reverse-engineered by third parties; gnnote reads them from the documented
facts (sources and confidence in the linked notes) and writes ordinary GoodNotes or
Notability files. Nothing here has been checked on a device of the source app.

| App (files) | Handwriting | Pages and backgrounds | Not converted (one warning each) |
|---|---|---|---|
| MyScript Notes / Nebo (`.nebo`; also Kobo notebooks) | Editable ink with the stylesheet's pen widths, colours, Apple Pencil pressure (MyScript's width law) and highlighters | Page size from the file (A4 by default), enlarged to fit ink beyond it | Typed and converted text, typeset shapes, math and images (MyScript's layout data is not decoded) |
| Flexcil (`.flx`; `.flex` backups: the first document of the library, the others are named in a warning) | Editable ink with per-point widths, colours and highlighters; shapes (lines, rectangles, ellipses, polygons, arcs, arrows) become ink strokes; text boxes and images are kept | Every page keeps its PDF page as a PDF background (Flexcil templates included) | Masking objects, links, audio; lasso transforms and dashes are drawn untransformed and solid; image placement is unverified |
| reMarkable (`.rmdoc` from the desktop app; single v6 `.rm` pages) | Editable ink with the device's rendered widths, colours (palette or RGBA), highlighters and shaders; text highlights become highlighter strokes | Notebook pages at 226 dpi (reMarkable 2 or Paper Pro canvas, grown to fit ink), template style as paper; PDF pages keep their PDF background | Erasers, hidden layers, inserted images; typed text and strokes anchored to it are placed approximately; EPUBs without a PDF rendition become plain pages |

## Verification status

Noteful files are read and written per the files of Noteful 1.4.25 and 1.4.33 and checked
against notesconverter's strict parser and Noteful's own PDF exports; no gnnote-written
`.noteful` file has been opened in Noteful yet (`docs/noteful.md` section 9 lists what to
check on a device).
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
browser), choose or drop a `.goodnotes`, `.note` or `.noteful` file, pick the options, press Convert and
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
python3 -m gnnote convert Lecture.cnote --to goodnotes          # CollaNote -> GoodNotes (default: Notability);
                                                              # a .cnote package folder works too
python3 -m gnnote formats                                     # the supported apps, read / write
python3 -m gnnote info Note.note [--json]                     # format, title, pages, counts, warnings
python3 -m gnnote convert Notebook.noteful --to goodnotes      # writes Notebook.goodnotes
python3 -m gnnote batch ~/Notes -o ~/Converted --to notability
python3 -m gnnote convert Notebook.goodnotes --to pdf --pdf-ink annotations   # editable ink in PDF apps
python3 -m gnnote convert Annotated.pdf                       # Annotated.note with the PDF's ink editable
python3 -m gnnote batch ~/PDFs --include-pdf --to goodnotes   # batch skips .pdf files unless asked
python3 -m gnnote convert Lecture.xopp --to goodnotes             # any supported app to any other
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
| `--to FORMAT` | `to=FORMAT` (the page: "Convert to") | Output format: `goodnotes`, `notability` or `noteful` (default: GoodNotes and Notability swap, other apps' files become Notability notes). |
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
unavailable are skipped; `GNNOTE_OFFLINE=1` disables cloning. Large repositories are checked
out sparsely (`SPARSE`), and single large files (`LARGE_FILES`, a 100 MB CollaNote notebook)
are only downloaded with `GNNOTE_LARGE_SAMPLES=1`, so CI skips them. Oracle parsers (goodparse,
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
* `docs/noteful.md`: the `.noteful` container, its records, ink and objects, and gnnote's mapping.
* `docs/collanote.md`: CollaNote's `.cnote` containers, stroke protobuf and geometry, Apple
  PencilKit drawings (`gnnote/pencilkit.py`), what the reader verifies and what it infers.
* `docs/pdf.md`: what the PDF writer produces and what the PDF reader converts, byte by byte.
* `docs/ecosystem.md`: import/export capabilities of both apps, existing tools, licensing.
* `docs/nebo.md`: MyScript Notes / Nebo packages and BINK ink, and how gnnote maps them.
* `docs/flexcil.md`: Flexcil documents and backups, and how gnnote maps them.
* `docs/remarkable.md`: reMarkable documents and v6 pages, and how gnnote maps them.
* `docs/xournalpp.md`: the Xournal++ `.xopp` / `.xoj` codec.
* `docs/saber.md`: the Saber `.sba` / `.sbn2` / `.sbn` codec.
* `docs/excalidraw.md`: the Excalidraw `.excalidraw` codec.
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
[svg2notability](https://github.com/jvns/svg2notability),
[denotability](https://github.com/miroreo/denotability) and
[collanote_cnote_to_pdf_converter](https://github.com/alarsama/collanote_cnote_to_pdf_converter).
GoodNotes, Notability, Noteful, CollaNote and the other apps named here are trademarks of their
respective owners; this project is not affiliated with any of them.

## Licence

MIT, see `LICENSE`. Third-party notices are in `NOTICE.md`.
