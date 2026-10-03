# gnnote — design and module contract

Bidirectional converter between GoodNotes notebooks (`.goodnotes`) and Notability notes
(`.note`) that keeps handwriting **editable** in the target app, usable as a Python library,
a CLI, a browser page (Pyodide, no server) and a small self-hosted server.

This document is the contract between the modules. Byte-level facts live in the research
documents (`goodnotes-container.md`, `goodnotes-stroke.md`, `goodnotes-elements.md`,
`goodnotes-v35-binding.md`, `goodnotes-v35-strokes.md`, `goodnotes-v35-elements.md`,
`notability-format.md`, `ecosystem.md`); when they disagree with this file on a byte-level
detail, the research documents (including their "Critic additions") win.

## 1. Ground rules

* **Pure Python 3.11+ standard library only** at runtime (`zipfile`, `plistlib`, `struct`,
  `zlib`, `uuid`, `re`, `math`, `dataclasses`). The same package runs unchanged under Pyodide
  in the browser. Optional extras (numpy, Pillow) are never required.
* **MIT licence, clean-room.** Write from the research documents. Code from `inkterop`
  (MIT) may be adapted with attribution in `NOTICE.md`. Code from `goodparse`,
  `notability-to-svg` (GPL-3.0), `inkref` and Federwerk (no licence) must **not** be copied;
  those projects are only used as separate-process test oracles / documented facts.
* **Readers are tolerant, writers are strict.** Readers skip what they do not understand and
  record a warning on `Document.warnings`; writers emit exactly the byte shapes the research
  documents verified against app-written files. A damaged container (not a ZIP, a ZIP feature
  `zipfile` does not implement) is the one `ValueError`; nothing else escapes `read_*`.
* **Bounded decompression.** A crafted file must not cost unbounded memory or time: ZIP
  members above 256 MB (declared size) or beyond 1 GB per container are skipped with a
  warning before inflating; a record stream holds at most 200 000 records
  (`protobuf.MAX_RECORDS`); an Apple-LZ4 block may not produce more than its declared size
  (checked before every copy) and a stream not more than 256 MB; PDF Flate / LZW / RunLength
  streams stop at 128 MB (`pdfutil.MAX_STREAM_BYTES`, a `PdfError`); a Notability page width
  below 16 units falls back to the default and any content is clamped to page 10 000.
* **Units at the model boundary:** PDF points, origin top-left, y down, colours as RGBA floats
  0..1 (see `gnnote/model.py`). GoodNotes canvas units = points × 132/72; GoodNotes flat stroke
  width `W` = points × 2; Notability document coordinates = points × `W_doc / page_width_pt`.
* Every lossy step adds one human-readable warning (English, one line) via `Document.warn()`.
* No file written by the converter may embed a model identifier, the session URL or anything
  but the project name and version.

## 2. Package layout

```
gnnote/
  __init__.py            version
  model.py               Document / Page / Stroke / Image / TextBox / PdfBackground  (exists)
  geometry.py            chord-length Catmull-Rom fit (clamped handles), Bezier flattening, RDP, resampling
  protobuf.py            wire encode/decode, length-delimited record streams
  applelz4.py            Apple framed LZ4 (bv41/bv4-/bv4$): decode; encode all-literal (+ optional real compression)
  tpl.py                 Troy Hanson TPL images used by GoodNotes strokes: decode flat + ribbon + pencil, encode flat (+ ribbon experiment)
  pdfutil.py             page count / sizes / rotation / producer of a PDF; generate 1-page paper PDFs
  rtf.py                 Cocoa RTF parse -> (plain, runs) and generate
  goodnotes/__init__.py
  goodnotes/constants.py field numbers, member names and unit constants of the container
  goodnotes/reader.py    read_goodnotes(data: bytes) -> Document
  goodnotes/writer.py    write_goodnotes(doc: Document, options: Options | None = None) -> bytes
  notability/__init__.py x_inset(): the paper's left edge in document units (section 4.2)
  notability/keyedarchive.py   NSKeyedArchiver object-graph helpers (read)
  notability/archivebuilder.py NSKeyedArchiver object-graph builder (write)
  notability/reader.py   read_note(data: bytes) -> Document
  notability/writer.py   write_note(doc: Document, options: Options | None = None) -> bytes
  readutil.py            bounded ZIP / JSON helpers shared by the other apps' readers
  nebo/bink.py           MyScript BINK ink -> strokes + tag table
  nebo/reader.py         read_nebo(data: bytes) -> Document            (MyScript Notes / Nebo, read only)
  flexcil/reader.py      read_flexcil(data: bytes, document=None) -> Document  (Flexcil .flx / .flex, read only)
  remarkable/scene.py    reMarkable v6 scene: tagged blocks, CRDT order, lines, glyphs, text
  remarkable/reader.py   read_remarkable(data: bytes) -> Document      (reMarkable .rmdoc / .rm, read only)
  convert.py             detect_format, Options, ConvertResult, convert()
  cli.py                 python -m gnnote
  server.py              stdlib HTTP server: static web UI + POST /api/convert
web/                     browser UI (Pyodide): index.html app.js worker.js styles.css i18n.js
scripts/build_web.py     builds dist/ (web/ + gnnote.zip [+ vendored pyodide])
tests/                   pytest; tests/e2e/ Playwright (node) end-to-end
docs/                    this file + research documents
Dockerfile, docker-compose.yml, .github/workflows/{ci,pages}.yml
```

## 3. Public API (frozen)

```python
# gnnote/convert.py
@dataclass
class Options:
    paper: str = "plain"        # "plain": built-in GoodNotes papers become Notability paper, pages scale to fit;
                                # "pdf": every page becomes a PDF-backed Notability page using the GoodNotes paper PDF
                                # (user-imported PDFs are ALWAYS carried as PDF backgrounds, whatever this says)
    pressure: bool = True       # Notability writer: per-anchor fractional widths (False -> constant width)
    simplify: float = 0.0       # RDP tolerance in pt applied to polylines before fitting (0 = off)
    ribbon: bool = False        # GoodNotes writer: experimental per-point-width ("ribbon") strokes
    title: Optional[str] = None # override the note / notebook name
    notability_page_width: float = 574.0   # pageWidthInDocumentCoordsKey written

@dataclass
class ConvertResult:
    data: bytes
    filename: str               # suggested output name, extension swapped
    source_format: str          # "goodnotes" | "notability"
    target_format: str
    warnings: List[str]
    stats: Dict[str, int]       # pages, strokes, images, texts, pdfs

def detect_format(filename: str, data: bytes) -> str      # raises ValueError if neither
def convert(data: bytes, filename: str, options: Optional[Options] = None) -> ConvertResult
def to_document(data: bytes, filename: str) -> Document  # read either format
```

Readers and writers (all take/return `bytes`, never paths):

```python
gnnote.goodnotes.reader.read_goodnotes(data: bytes) -> Document
gnnote.goodnotes.writer.write_goodnotes(doc: Document, options: Options) -> bytes
gnnote.notability.reader.read_note(data: bytes) -> Document
gnnote.notability.writer.write_note(doc: Document, options: Options) -> bytes
gnnote.nebo.reader.read_nebo(data: bytes) -> Document                 # read only
gnnote.flexcil.reader.read_flexcil(data: bytes, document=None) -> Document   # read only
gnnote.flexcil.reader.list_flexcil_documents(data: bytes) -> List[FlexcilEntry]
gnnote.remarkable.reader.read_remarkable(data: bytes) -> Document     # read only
```

Every format is one entry of `gnnote/formats.py` (`FORMATS`): id, name, extensions, a content
sniffer and the reader / writer paths (`None` for a read-only app).

Primitives:

```python
# protobuf.py
class Field(NamedTuple): number: int; wire_type: int; value: Union[int, bytes]
def decode_message(data: bytes) -> List[Field]
def decode_records(data: bytes) -> List[bytes]           # varint-length-prefixed record stream
def encode_records(records: Iterable[bytes]) -> bytes
def varint(n: int) -> bytes
def field_varint(num, n) / field_bytes(num, b) / field_fixed32(num, f: float) / field_fixed64(num, x) / field_message(num, body: bytes) -> bytes
def get(fields, number) -> Optional[Field]; def get_all(fields, number) -> List[Field]
def fixed32_float(field) -> float
# applelz4.py
def is_apple_lz4(data: bytes) -> bool
def decompress(data: bytes) -> bytes                     # bv41 (standard LZ4 block), bv4- (one u32 size + raw), bv4$ ; multi-block
def compress(data: bytes, level: int = 0) -> bytes       # level 0 = all-literal bv41 blocks of <= 32 KiB (GoodNotes 2026 framing); level 1 = greedy LZ4 (optional)
# tpl.py
@dataclass class FlatStroke: width: float; start: (x, y); quads: List[(cx, cy, ex, ey)]           # 'vuA(v)A(S(uu))A(S(uuuu))vA(f)'
@dataclass class RibbonStroke: points: List[(x, y, r)]; ...                                      # 'vA(v)A(u)A(u)...' legacy per-point radius
@dataclass class PencilStroke: ...                                                                 # '#21 == 25' layout, points + width
def decode(data: bytes) -> Union[FlatStroke, RibbonStroke, PencilStroke, None]   # None for the empty 62/67-byte header
def encode_flat(stroke: FlatStroke) -> bytes
# pdfutil.py
@dataclass class PdfPage: width: float; height: float; rotation: int        # width/height already rotated into display orientation
@dataclass class PdfInfo: pages: List[PdfPage]; producer: str; creator: str
def pdf_info(data: bytes) -> PdfInfo                      # xref tables, xref streams, object streams, inherited MediaBox/Rotate; regex fallback
def make_paper_pdf(width: float, height: float, style: str = "plain", color: (r,g,b) = (1,1,1), pitch: float = 16.0) -> bytes
# rtf.py
def parse_rtf(data: bytes) -> Tuple[str, List[TextRun]]  # cp1252 \'xx, \uN with \ucN, \fonttbl, \colortbl, \fsN (half-points), \b \i \ul \cfN \par \line
def make_rtf(text: str, runs: List[TextRun], font: str = "HelveticaNeue", size_half_points: int = 24, color: RGBA = black) -> bytes
```

## 4. Conversion rules (decided)

### 4.1 GoodNotes → model (`goodnotes/reader.py`)

* Pages: `index.notes.pb` members ordered by `#54.#4.#1` order keys (ASCII sort), minus
  `#56` deleted pages, falling back to index order. Title from `#30`/`#31`.
* Page size: the template PDF's page (`#2.#4` attachment, `#2.#5` 1-based page) via
  `pdfutil.pdf_info`; canvas scale from `#2.#8` (`scale = page_w_pt / canvas_w`); if the event
  is missing use `72/132` and warn.
* Background classification: built-in paper iff `#2.#9` matches the catalogue pattern **and**
  the attachment is a 1-page PDF produced by `svg2pdf` (`goodnotes-elements.md` §3.3) or by
  `gnnote` (paper generated by our own writer, §4.4, is stock paper too). Built-in
  → `Page.template_is_builtin = True`, `Page.paper` ∈ {plain, lined, grid, dotted} (lined when
  `#2.#7`/`#18` present; grid/dotted only if detectable, else plain; the detector counts thin
  rectangles drawn as `m l l l h f` paths (svg2pdf) and as `x y w h re` operators
  (`make_paper_pdf`), and `c` curves for dots, so our own lined/grid/dotted paper reads back
  with its style), the PDF is still stored in
  `Document.pdfs` and referenced by `Page.background` so the "pdf" paper option can use it.
  Otherwise → user PDF: `Page.background = PdfBackground(attachment_uuid, page_index)`.
* Strokes (`#7`): skip elements whose geometry has no points (`#3 = 1` tombstones). Flat format:
  quads → exact cubic Bezier chain (`c1 = P0 + 2/3(C−P0)`, `c2 = P1 + 2/3(C−P1)`), widths
  `W/2` pt at every anchor, `controls` set. Ribbon format: polyline with per-point width
  `2·r·72/132` pt, `controls = None`. Pencil (`#21 = 25`): polyline, width `2.5·W` pt, `pen =
  "pencil"`, warn once. `#5 = 1` → `kind = "highlighter"`. `#6` translation added to every point.
  `#3` absent → `pen = "ballpoint"`, `#3 = 1` → `"fountain"`, `#20 {#1 ""}` → `"marker"`.
  Colour from `#4` (missing components = 0.0). Canvas → pt by the page scale.
* Auto-shapes (`#9` present): emit as strokes sampled from the geometry (line, polyline, rect,
  ellipse at 64 points), width `#9.#15 / 2` pt. Straight-sided forms (line, polyline, rect)
  carry exact cubic handles at the thirds of every segment (`controls` set) so the Notability
  writer keeps their sides straight and corners sharp; the ellipse samples stay a polyline
  (`controls = None`, the Catmull-Rom fit is smooth there).
* Images (`#1`): placement `#2` (top-left + size, canvas units) → `Image`; raster bytes from the
  attachment; `fmt` sniffed. Crop/rotation: use `#3` only when it differs from `#2` and warn.
* Text boxes (`#8`): outer rect `#2` inset by `#10`; RTF `#6` → `TextBox` with runs; sizes
  (`\fsN/2`) are canvas units → pt.
* Unknown record kinds (`#20/#22` …) → warning with the field number, never an exception.

Rules added for the schema-25/35 files (`goodnotes-v35-binding.md`, `goodnotes-v35-elements.md`,
`goodnotes-v35-strokes.md`); where they differ from the bullets above, these win:

* Page binding: `P = N − 1` as a **128-bit integer with carry** (`...E450 → ...E44F`), events
  keyed by the exact page UUID; a 31-hex-digit prefix match is only the fallback for hand-made
  files. Event `#3` re-binds a page to another template exactly like `#54.#3.#1` (later events
  win, order key untouched). Attachment ids resolve through `index.attachments.pb`, then the
  `#6.#1 → #6.#2` storage alias, then `attachments/<id>`. Order keys are sorted bytewise; a page
  that has a key never falls back to index order. `Page.paper` is "lined/grid/dotted" only when
  the catalogue PDF draws it (or `#2.#18.#3` is present); `#7`/`#18.#2` alone mean blank paper.
* Shape fills (content kind `#9`, Test6–8): `Stroke(kind="fill", outline=[closed polygon],
  points=the polygon, color=#7 RGBA (alpha 0.1), width=0, pen=None)` inserted right after the
  last stroke of its outline element (`#5`); dropped when erased (`#14 = 1` / NaN bbox) or when
  the outline is not on the page (warning).
* Auto-shape `#9.#2 {P0, C, P1}` is one quadratic Bezier → a 2-anchor stroke with the elevated
  cubic handles; `#9.#1` lists are drawn as given (first == last = closed polygon, two identical
  points = a dot). Ribbon strokes whose flags contain 4/5 are `pen = "marker"` even without `#20`.
* Schema-35 text (content kind `#21`): runs from the Apple-LZ4 blob `#32.#1.#2`; `TextBox` at
  `(#20.#1 + inset) × k`, size `(box − 2 inset) × k × #20.#3`, font size `(run #40 or default
  #32.#5.#1.#40) × k × #20.#3`, colour from the run (default black), `rotation =
  degrees(#20.#2)` (clockwise, about the frame origin), `align = "center"` when `#3.#4 = 2`.
  Font family from the run or the default style; `#60 = −30` is read as bold (low confidence).
* Images: a `%PDF` attachment is a vector sticker → `Image(fmt="pdf")` sized by `#2` (1 PDF pt
  = 1 canvas unit). JPEGs get their EXIF orientation parsed (APP1, tag 0x0112): `#2` is the
  displayed box, `Image.rotation` = 90 / 180 / 270 says how the raw pixels must be turned to fill
  it (mirrored variants warn and keep the rotation). Never guess from aspect ratios.
* No warning for anything listed here; the only warning the schema-25/35 samples produce is the
  pencil approximation.

### 4.2 Model → Notability (`notability/writer.py`)

* Build the whole `Session.plist` object graph in code mirroring the Notability 10.4 template
  (`notability-format.md` Parts 3, 4, 6, 12). `sessionFormatVersion 5`, bundle version
  `"10.4"`, `pageWidthInDocumentCoordsKey = options.notability_page_width` (W).
* Page slots in order: a plain page occupies `H = 1.3125·W`; a PDF-backed page occupies
  `ceil(h·W/w)`. `y_offset(page_i) = Σ slots before it`.
  * `paper == "plain"` and the page has no user PDF: plain slot; scale = `W / page.width` but
    if `page.height·scale > H` use uniform scale `H / page.height` (warn once); no layout entry
    unless the note also contains PDF pages, in which case plain pages get the blank entry
    `{kPageLayoutPDFPageNumberKey: INT64_MAX}`.
  * `paper == "pdf"`, or the page has a user PDF (`not template_is_builtin`): PDF-backed slot.
    The PDF bytes go to `PDFs/<UUID>.pdf` (one file per distinct `pdf_id`), one `PDFFile`
    object per file (`pdfFileName, contentBoxVersion 1, highlights [], version_4_1_OrLater
    True, version 2, pageNumbers $null`), layout dict `{kPageLayoutPDFPageNumberKey: 1-based,
    kPageLayoutPDFFileKey: PDFFile, kPageLayoutPDFIsOriginalPageKey: True}`.
    `x_doc = x_inset + x_pt·W/w`, `y_doc = y_offset + y_pt·W/w`.
  * **x inset** (plain and PDF slots alike): Notability's paper starts at document
    `x_inset = −W·20/768` (2.6 % of W), not at 0 — its PDF export draws ink under the matrix
    `612/W 0 0 612/W 15.9375 …` (15.9375 = 612·20/768) and PDF backgrounds at x = 0
    (`notability-format.md` §5.1). `notability.x_inset(W)` is the single source of the value;
    the plain-page centring offset of a scaled-down page is added on top of it.
  * All-plain notes write no `pageLayoutArray` (the page count follows from the content; the
    path proven on the iPad) unless the last page is blank: then every page gets the blank
    entry `{kPageLayoutPDFPageNumberKey: INT64_MAX}` so trailing empty pages survive.
* Ink: one `InkedSpatialHash`. Each stroke: anchors + controls (fit with
  `geometry.polyline_to_bezier` when `controls is None`, after `geometry.simplify(options.simplify)`;
  the fit is a chord-length Catmull-Rom with every handle clamped to a third of its segment,
  so short segments after long ones do not hook or loop and a two-point stroke is an exact
  line — the RDP tolerance bounds the polyline, the fitted curve may deviate slightly more),
  `n = 1 + 3k` points, `k + 1` fractional widths = `width_i / base` clipped to [0.25, 4]
  (`base = median anchor width`, moved towards the extremes when the stroke's width range
  fits inside the clip but the median would not, e.g. fountain-pen tapers; all 1.0 when
  `options.pressure` is False), `curveswidth =
  base` in doc units, colour 4×uint8, `curvesstyles` 3 (pen/pencil) or 4 (highlighter; alpha
  forced to 0x6b), `eventTokens` −1. Strokes whose bbox is empty become a 0.5 pt dash.
* Images → `ImageMediaObject` (+ `Images/Image N.jpg|png`), text boxes → `TextBlockMediaObject`
  (Part 12 items 3–4); both are written only when present, with the class registrations the
  template needs. Highlighter behind text flag kept as in the template.
* Content the latest GoodNotes files add (`goodnotes-v35-elements.md`), decided 2026-10:
  * `Stroke.kind == "fill"` is dropped — Notability has no filled shapes — with **one** warning
    `"N shape fills dropped (Notability has no filled shapes)"` counting the whole document.
  * `Image.fmt == "pdf"` (vector stickers; also any image whose bytes start with `%PDF-`) is
    dropped with one warning `"N PDF images dropped (Notability images must be PNG or JPEG)"`.
  * `Image.rotation` and `TextBox.rotation` (degrees, clockwise) → `rotationDegrees` in
    **radians**. Notability rotates about the object's centre; the model's text box rotates
    about its top-left corner, so the writer moves `documentOrigin` to where a centre-pivot
    rotation puts that corner back (`text_origin_for_centre_pivot`). An EXIF-rotated JPEG
    (orientation ≠ 1) is written byte for byte with `rotationDegrees` from the model; whether
    Notability applies the EXIF orientation on top is unverified → one warning.
  * `TextBox.align` → the text store's `formattedStringTextAlignmentKey` (0 left, 1 centre,
    2 right); the note's own flow-text key stays 0.
* `metadata.plist` (new UUID, dates = now, name, subject "GoodNotes import" unless given),
  `Recordings/library.plist`, empty `Assets/ Images/ PDFs/ HandwritingIndex/` entries, white
  thumbnails 48×63 / 96×126 / 144×189 / 288×378 (RGB PNG via zlib). Member path prefix = note
  name; ZIP stored or deflated (both accepted).

### 4.3 Notability → model (`notability/reader.py`)

* Locate ink by walking root → `richText` → `'Handwriting Overlay'` → `SpatialHash` (never
  "first dict with curvespoints"). Handle `NS.objects` lists and `NS.object.N` dicts, strings
  as `str`/`NS.string`/`NS.bytes`, `$null`, UID indirection; bounds-check `curvesstyles`.
* Pages: `pageLayoutArray` (both key styles) + `pdfFiles` + `PDFs/` sizes (`pdfutil`), plain
  `H = 1.3125·W` (or `floor(W·11/8.5)` when `paperSize == 'letter'` and `staticWidth`); implicit
  plain pages after the layout. Assign a curve to the slot containing the y of its first anchor;
  clip nothing. Plain page size in pt = **612 × 803.25** (`scale_pt = 612 / W`); PDF page size =
  the PDF page's size (`scale_pt = w / W`). `x_pt = (x_doc − x_inset)·scale_pt` with the
  same `x_inset = −W·20/768` as the writer (§4.2), so margin ink at `x_doc = −14.5` lands on
  the paper's edge instead of outside the page. Text boxes lose the (5, 2) document-unit
  padding the writer adds (origin + padding, size − 2·padding), keeping the two codecs
  symmetric. `pageWidthInDocumentCoordsKey` below 16 units is treated as damage (default
  width, warning) and the page index of any content is clamped to 10 000 pages, so a
  corrupt width or coordinate cannot explode the page list.
* Curves → `Stroke` with `controls` (exact cubic chain) and per-anchor widths
  `curveswidth × fractional × scale_pt`; `curvesstyles` 4 or (styles absent and alpha < 255)
  → highlighter; 5 → `pen = "pencil"`; groups (`groupsArrays`) decoded recursively with their
  affine transform applied; dash styles and shape flags ignored (warn once each).
* `ImageMediaObject` → `Image` (file from `Images/`, `documentOrigin`, `unscaledContentSize`,
  rotation radians → degrees); `TextBlockMediaObject` → `TextBox`; non-empty flow text
  (`attributedString`) → one `TextBox` per page at the top of page 1 spanning `W − 2·36` pt,
  font size from the first run (warn: layout approximated).
* Recordings, stickers (`CanvasMediaObject`), `MathMediaObject` → warning, dropped.

### 4.4 Model → GoodNotes (`goodnotes/writer.py`)

Synthesise the full container (`goodnotes-container.md` §14 + Critic additions, inkterop's
event synthesis, inkref's confirmed stroke encoding):

* Members (deflate): `index.search.pb` (0 B), `index.notes.pb`, `notes/<N>` per page,
  `index.events.pb`, `thumbnail.jpg` (embedded constant small white baseline JPEG),
  `index.attachments.pb`, `attachments/<A>`, `schema.pb = 08 18`.
* IDs: uppercase UUID4 everywhere; page entity `P` ends in 0–E and notes layer `N = P` with the
  last hex digit + 1; one random 63-bit device id shared by all records and events; event
  sequence increasing; clocks `{#1 2, #2 random uint32}` identical in metadata `#2` and content
  `#15`; `#7 = {#1 {#1 draw-index, #2 nonce}}`.
* Events, in order: `#30` (constants as observed, title), `#6` per attachment (`#5` = byte
  size; `#12 = {#1 1, #2 1}` for PDFs, empty for rasters), `#2` per (PDF, page) (canvas =
  MediaBox × 11/6 as fixed32; built-in-style name `"<UUID>_standard_1_1 - White"` for generated
  paper, the plain filename for every carried PDF, including another app's stock template), `#54` per page (`#3.#1 = T`, `#4.#1` = order key
  `"43" + base36(i).rjust(4,"0")` using digits+uppercase so ASCII order = page order, `#17`
  constant colour block), `#105` per page (notes form without `#5`), `#10` current page =
  first page, `#102` per non-empty page.
* Paper: a page with a `PdfBackground` attaches that PDF once per `pdf_id` and binds `#2.#5` =
  page index + 1 (shared multi-page attachment; the research found no evidence GoodNotes needs
  per-page splitting). Otherwise `pdfutil.make_paper_pdf(width, height, paper)`.
* Strokes: flat format only (`tpl.encode_flat`), `#3` **absent**, `W = 2·width_pt` where
  `width_pt` = median anchor width (warn once when widths vary and `options.ribbon` is False),
  colour fixed32 RGBA omitting 0.0, highlighter → `#5 = 1` and alpha 0.5. Geometry: Bezier
  input → each cubic approximated by quadratics (`C = (3(c1+c2) − P0 − P1)/4`, split at t = ½
  while the max deviation > 0.3 canvas units, depth ≤ 4); polyline input → quads with the
  control at the segment midpoint; a single point → a 0.3-unit segment. Odd point count rule of
  `goodnotes-stroke.md` §8 honoured. Elements are (metadata, content) pairs; never emit
  `#3`/`#14` tombstone flags.
* Images: `goodnotes-elements.md` §2.5 (attachment + `#6` event + record pair). Text boxes:
  §4.4 (RTF via `rtf.make_rtf`, outer rect = text rect + 10 padding).
* Content the latest GoodNotes files add (`goodnotes-v35-elements.md`, decided 2026-10):
  * `Stroke.kind == "fill"` → a top-level `#9` fill record (§1.2 layout: `#1 E, #2 bbox,
    #3 {#1 {#1 1, #2 nonce}}, #4 {#1 closed polygon}, #5 parent UUID, #6 "", #7 RGBA with the
    model's alpha, #15 {#2 nonce} = metadata clock, #18 24`; metadata without `#4`) written
    right after its parent, **only when the ink stroke just before it in `Page.strokes` — or,
    failing that, the one just after it (GoodNotes stores both orders) — is its parent** (a
    non-fill stroke whose bounding box matches the fill's outline within 2 pt + 2 %). Any
    other fill is skipped with one counted warning. Polygon = the first `outline`
    ring in canvas units, first point repeated last; GoodNotes itself only writes fills for
    `#7.#9` auto-shapes, so acceptance of a fill under a plain ink stroke is unverified. Fills
    take no draw index.
  * `Image.fmt == "pdf"` → the PDF bytes become an ordinary image attachment (`#6` event with
    `#12 = ""`, like a raster) and the record gets `#6 = 3`; JPEGs get `#6 = 1`, PNGs no `#6`
    (§5.1).
  * `Image.rotation`: GoodNotes has no verified rotation field (`#3.#3` is never written). A
    JPEG whose EXIF orientation prescribes the same quarter-turn (6 → 90, 3 → 180, 8 → 270) is
    written unchanged with `#2`/`#3` = the **displayed** box (w/h swapped about the centre for
    90/270), exactly as GoodNotes stores EXIF photos (§5.3); one counted warning. Every other
    rotation is dropped with a counted warning.
  * `TextBox.align` "center"/"right" → `\qc`/`\qr` in the RTF paragraph header;
    `TextBox.rotation` is dropped with a counted warning.
  * Container: schema stays 24 everywhere (`goodnotes-v35-binding.md` §12); `document.info.pb`
    is **not** written (0 bytes in the one schema-35 sample, absent from the schema-25 ones —
    the schema-24 generation we imitate has none). Nothing else changed.
* `options.ribbon` (experimental, off by default): per-point widths via the ribbon TPL with
  synthesised CGPath pools (`goodnotes-stroke.md` §2.2); only enabled if the pools regenerated
  for the hand-decoded sample stroke match GoodNotes' own bytes (test), otherwise the option
  falls back to flat with a warning.

### 4.5 Other apps (read only)

Readers of formats other apps write follow the same rules as the two original readers
(tolerant, one warning per lossy step, `ValueError` only for "not a <format> file", bounded
decompression through `readutil.ZipBundle`, at most 10 000 000 ink points per document).
Their byte layouts, mappings and open questions are in their own notes:

* MyScript Notes / Nebo `.nebo`: `docs/nebo.md` (BINK ink with pen classes, colours and the
  pressure width law; the BDOM layout data is not decoded).
* Flexcil `.flx` / `.flex`: `docs/flexcil.md` (width-normalised ink, shapes as strokes, PDF
  backgrounds, text boxes, images; one document per backup).
* reMarkable `.rmdoc` / `.rm`: `docs/remarkable.md` (v6 scenes ported from rmscene, stored
  rendered widths, 226-dpi pages or PDF pages, highlights, approximate typed-text anchors).

## 5. Web UI (`web/`)

* Static, no build step for development: `index.html` + `app.js` (main thread) + `worker.js`
  (**module** worker) + `styles.css` + `i18n.js` (Slovak default, English toggle, strings in
  one table). Works from `file://`? No — served over HTTP (`python -m gnnote.server` or any
  static host). Phone-width layout, dark mode via `prefers-color-scheme`.
* Flow: choose/drop a `.goodnotes` or `.note` → options (paper: plain/pdf; pressure; simplify)
  → "Convert" → progress (loading Python ~13.5 MB on first use, converting) → download card with
  the output name, stats, warnings → buttons: **Download** (`a[download]`) and, when
  `navigator.canShare({files})`, **Share / Save to Files** (`navigator.share`). Errors show the
  Python traceback in a collapsible block.
* Engine selection: on load `GET api/health`; if it answers `{"ok": true}` use `POST
  api/convert` (multipart `file`, query `paper=…&pressure=…&simplify=…`; a file larger than
  the advertised `maxUpload` is refused on the page with a translated message instead of an
  aborted upload), else Pyodide. Pyodide base: `window.PYODIDE_BASE` if set; else the sibling
  `pyodide/` directory when `version.json` says `pyodide.vendored` is true (written by
  `build_web.py --vendor-pyodide`) or a `HEAD pyodide/pyodide.mjs` probe answers 2xx with a
  non-HTML content type (a static host with a SPA fallback answers 200 + `index.html` for
  every path and must not be mistaken for a vendored copy); else the CDN
  (`version.json`'s `pyodide.cdn`, default `https://cdn.jsdelivr.net/pyodide/v314.0.7/full/`).
  The worker retries once from the CDN when the chosen base fails to import. The package arrives as
  `gnnote.zip` (built by `scripts/build_web.py`, also served by `server.py` on the fly) and is
  installed with `pyodide.unpackArchive` into `/lib/python3.14/site-packages`-independent path
  added to `sys.path`. File buffers are transferred to the worker and freed after use.
* While a conversion runs, Reset, the file picker and drops are disabled and a result whose
  file is no longer the selected one is discarded, so the result card always belongs to the
  file shown. Converter warnings are English; the Slovak UI says so above the list.
* Worker protocol: `{type:"init", pyodideBase, pyodideCdn, packageUrl}` → `{type:"ready"}`;
  `{type:"convert", name, buffer, options}` → `{type:"progress", stage, message}`* →
  `{type:"done", name, buffer, warnings, stats, sourceFormat, targetFormat}` |
  `{type:"error", message, traceback}`.

## 6. Server, build, deployment

* `gnnote/server.py`: `python -m gnnote.server --host 0.0.0.0 --port 8000 [--dist DIR]`;
  serves `dist/` if present else `web/` + `/gnnote.zip` generated from the installed package;
  `GET /api/health` (`{"ok": true, "version", "maxUpload"}`), `POST /api/convert` (multipart,
  ≤ 300 MB, returns the file as an attachment with `X-GnNote-Warnings` JSON header). HEAD
  never writes a body (keep-alive clients would read it as the next response); idle sockets
  time out after 60 s; a multipart delimiter is only a line break + `--boundary` followed by
  a line break or `--` (RFC 2046), so payload bytes that merely start with the boundary are
  data. No third-party dependencies.
* `scripts/build_web.py [--vendor-pyodide] [--out dist]`: copies `web/`, zips `gnnote/` into
  `dist/gnnote.zip`, writes `dist/version.json`; with `--vendor-pyodide` downloads the five
  Pyodide files into `dist/pyodide/`.
* `Dockerfile` (python:3.12-slim, builds dist with vendored Pyodide, runs the server on 8000),
  `docker-compose.yml`.
* CI (`.github/workflows/ci.yml`): pytest on 3.11 and 3.12 with the reference repositories
  cloned for oracle tests; e2e job with Node + Playwright Chromium against the built `dist/`
  using CDN Pyodide. Pages (`.github/workflows/pages.yml`): build and deploy `dist/` to GitHub
  Pages on pushes to `main`.

## 7. Tests

* `tests/conftest.py`: fixture `samples` → directory with the reference repositories
  (`franzthiemann/goodparse`, `Kaih1825/parser-for-goodnotes`, `HuyNguyenAu/notability-to-svg`,
  `xrayshan/notability-reader`, `jvns/svg2notability`, `samuelsadok/notesconverter`,
  `nokcha0/Notability-notes-converter`, `cable729/inkterop`, `miroreo/denotability`, and for the
  other apps `janptn/flexcil-backup-viewer`, `jeonghyeon-net/flexcil-codex-plugin`,
  `ricklupton/rmscene`, `ricklupton/rmc`, `szainababbas/RM-Sticker-Press`), taken from `$GNNOTE_SAMPLES` if set, else fetched at pinned commits into
  `tests/.samples/` (skipped when offline). Oracle parsers run **in a subprocess** with their
  own `PYTHONPATH` (never imported into our package).
* Unit tests per primitive (hand-built vectors + round trips). Reader tests over every sample
  file of both formats: no exception, stroke counts match the oracle parser where one exists,
  every stroke inside its page bounds (± 5 %), colours match.
* Writer tests: write → read back with our reader (positions within 0.1 pt, widths within 5 %,
  colours exact) and with the oracle parsers; `.note` output structurally compared to the 10.4
  template (same key sets per class). Cross-format round trips in both directions.
* End-to-end: `tests/e2e/run.js` serves `dist/`, uploads a sample, waits for the download,
  checks the output with `python -m gnnote info`.

## 8. Decisions log

| # | Decision | Why |
|---|----------|-----|
| 1 | Own MIT codecs, no goodparse import | GPL would propagate to the whole tool |
| 2 | Default paper mode "plain" for GoodNotes → Notability | Ink on plain paper is the path proven on the user's iPad; "pdf" mode preserves ruled paper but PDF-backed hand-written notes are untested |
| 3 | User PDFs always carried as PDF backgrounds | Dropping them would lose the content being annotated |
| 4 | GoodNotes writer emits constant-width flat strokes by default | Only geometry family confirmed to render from synthesised bytes |
| 5 | Shared multi-page PDF attachments in GoodNotes output | Matches the format's design (`#2.#5` page index); splitting needs a PDF page extractor |
| 6 | Notability plain page = 612 × 803.25 pt | Notability's own PDF export size; keeps 1.3125 ratio |
| 7 | Browser-first (Pyodide) with optional server | Runs on the iPad itself; no hosting needed; server only for speed on a homelab |
