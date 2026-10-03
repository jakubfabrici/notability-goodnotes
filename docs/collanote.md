# CollaNote (`.cnote`): format notes and the reader

gnnote **reads** CollaNote notes (`gnnote/collanote/reader.py`, `read_cnote(data) -> Document`) and
converts them to Notability and GoodNotes with the ink kept editable. It does **not write** `.cnote`
files: that needs test rounds in the app first (section 9).

CollaNote ("CollaNote: Notes & PDF Markup", App Store id 1540956268, iPad / iPhone / Mac) stores ink
as vector strokes in its own protobuf; CollaNote 1.x (2021–2023) stored Apple PencilKit drawings,
which `gnnote/pencilkit.py` decodes (section 5).

Confidence labels: **verified** = checked against bytes of app-written files (or Apple's own
PencilKit output); **inferred** = consistent with all evidence but not tested in the app;
**unverified** = no sample.

Evidence used:

* `enisogdum/YTU-Archive` @ `81e30df` (`1-2/Semiconductor/slide/`): 7 format-1 notes and 3 format-2
  packages (CollaNote 2025–2026, lecture slides as PDFs), 315 pages, 4,249 strokes, 7 images.
* `Kinjalrk2k/100-Days-of-Machine-Learning-Campus-X` @ `22642ef`, `_backup/Notes.cnote` (Git LFS,
  100 MB): a 112-page notebook on lined A4 paper, 116,394 strokes, 59 images, one text box.
* Apple PencilKit: inkterop's fixtures (written by Apple's framework, with ground truth) and the 854
  drawings in `r987r/Flashcard` @ `f74f6e8` (`Cards.cards`, iOS).
* `alarsama/collanote_cnote_to_pdf_converter` (MIT, JavaScript): facts only.

None of these files is in this repository; the tests fetch them at the pinned commits (section 10).

## 1. Containers

| Variant | Layout | Confidence |
|---|---|---|
| Format 1, a ZIP file `X.cnote` | `note without pdf.cnote` (note JSON), `0.cpage` … `N.cpage` (page JSON; the number is the page order), `0.pdf` … (imported PDFs, by `pdfIndex`). Stored members, ZIP64 extras and data descriptors; Python's `zipfile` reads them. Member order is arbitrary. | verified |
| Format 2 (CollaNote 4.3+), a directory package `X.cnote/` | `manifest.cnm` (`{"format": 2, "minReader": 2, "writer": "CollaNote 4.3.3 (2)", "writtenAt", "pageCount", "pdfCount", "audioCount"}`), `basenote.cdat` (note JSON, same schema), `N.cpage`, `N.pdf`. | verified |
| Format 2, zipped | The package as a ZIP, with the `X.cnote/` folder in the member names or without it. | inferred (how iOS shares packages) |
| A ZIP holding one `.cnote` file | Opened once (a compressed note). | inferred |
| Bare JSON | The note JSON with the pages inline in `pages` and the PDFs inline (base64) in `importedPdfDatas`; early notes. Read on a best-effort basis with a warning. | unverified |

The registry entry (`gnnote/formats.py`, id `collanote`) recognises a note by its member names at
the top level or under one folder (`note without pdf.cnote`, `basenote.cdat`, `manifest.cnm`,
`N.cpage`), a ZIP with a single `.cnote` member, or a JSON object with CollaNote keys, whatever the
file is called: a zipped package named `X.cnote.zip` converts to `X.note` / `X.goodnotes`.
`__MACOSX/`, `._*` and `.DS_Store` members are ignored. An archive with several notes yields the
top-level one (else the folder with the most pages) and a warning. The CLI also takes a package
folder as it lies on disk (`gnnote convert "Week 1.cnote"`, also in `info` and `batch`): it
zips the folder in memory first.

## 2. Note JSON (`note without pdf.cnote` / `basenote.cdat`)

Swift `Codable` output (the key `moddificationInfo` keeps the developer's spelling).

| Key | Meaning | Use |
|---|---|---|
| `name` | note title | `Document.title` (Unicode NFC; CollaNote writes some names decomposed); fallback: the package folder, then "Untitled" |
| `size` | `[W, H]`, the page canvas in CollaNote units, shared by every page | geometry (section 4) |
| `paperOrTemplate` | e.g. `"Gray Notelined S"` (optional) | `Page.paper`: "line" → lined, "grid"/"square" → grid, "dot" → dotted (only "Notelined" is verified) |
| `audios` | `[{audioData (base64 m4a), duration}]` | warning, not converted |
| `bookmarks` | `[{title, systemImageName, color, pageID, ID}]` | warning, not converted |
| `pages`, `importedPdfDatas` | `[]` in containers | bare-JSON notes only |
| `thumbnail`, `favTools`, `_dkFavTools`, `dateCreated`, `dateEdited`, `id`, `isCollaborationActivated`, `isUsingCustomCover`, `rememberedScrollOffset` | | ignored |

## 3. Page JSON (`N.cpage`)

| Key | Meaning | Confidence |
|---|---|---|
| `_dkDrawing` | list of base64 strings, each one `Drawing` message (section 3.1); `[""]` on a page without ink | verified |
| `drawing` | base64 PencilKit drawing: an empty drawing (64 or 42 bytes) on every current page; CollaNote 1.x kept its ink there | verified (empty); legacy content unverified |
| `pdfPointer` | `{pdfIndex, pageIndex}` (0-based): page `pageIndex` of `<pdfIndex>.pdf` lies behind the page; absent on blank pages | verified |
| `attachments` | images and text boxes, in z-order (section 3.2) | verified |
| `strokeCountBeforeSaving` | equals the number of stroke records in `_dkDrawing` (all 427 pages); a mismatch is reported | verified |
| `id` | 10 characters | ignored |

### 3.1 `_dkDrawing`: CollaNote's stroke protobuf

Plain protobuf wire format with proto3 behaviour: zero values are omitted (black is `{4: 1.0}`), so
a missing field reads as 0.

* `Drawing`: `#1` (repeated) `Stroke`, in z-order.
* `Stroke`: `#1` UUID string, `#2` (repeated) `Point`, `#3` `Style`, `#4` f64 creation time (Unix
  seconds), `#6` varint 0/1 (1 on straightened lines and shapes; the points already are the shape),
  `#7` varint 0.
* `Point`: `#1` x, `#2` y, `#3` width (all f32, canvas units, origin top-left, y down); `#4` f64
  time; `#5`, `#6`, `#7` f32 force / angles (pressure pens only). The reader takes x, y and the
  width; a missing or zero width falls back to the style width.
* `Style`: `#1` f32 nominal width, `#2` colour `{#1 r, #2 g, #3 b, #4 a}` f32 0..1, `#3` varint
  inkType.

| inkType | Seen | Meaning | Model |
|---|---|---|---|
| 1 | 119,031 strokes, opaque, width mostly 1–3 | pen | `kind="pen"` |
| 5 | 1,337 strokes, alpha 0.4946, width 13, point widths 1.0–2.2 × that | translucent wide pen (crosses over slides) | `kind="pen"` with its alpha |
| 27 | 262 strokes, yellow, alpha 1.0, width 10 | highlighter (the app adds the translucency) | `kind="highlighter"` |
| 4 | 13 two-point lines, green, width 2 | unknown | pen, one warning listing the codes |
| other | | unknown (CollaNote has 25+ pens) | pen, one warning listing the codes |

Points become a polyline (`Stroke.controls = None`), each with its own width; ink outside the page
is kept (crosses over whole slides run past the edges: in the PDF notes 3 % of the points lie more
than 5 % outside the page).

### 3.2 Attachments

`{type, id, center: [cx/W, cy/H], bound: [[0, 0], [w/W, h/H]], rotatedDegree, imageInData,
attStringData}`. Centre and size are normalised separately by the canvas width and height
(verified: the box matches the pixel aspect of 65 of 66 images; the other one was stretched in the
app).

* `type: "image"`: `imageInData` = base64 PNG or JPEG (57 PNG and 9 JPEG seen) → `Image` at
  `x = (cx·W − w·W/2)·s`, `y = (cy·H − h·H/2)·s`, size `w·W·s × h·H·s`; a PDF payload becomes
  `Image(fmt="pdf")`; anything else is skipped with a warning. `attStringData` holds the placeholder
  "This Sticker should be a image, not a TextView". `rotatedDegree` → `Image.rotation`, assumed
  clockwise (UIKit) about the centre: **unverified**, every sample has 0, so a rotated image adds a
  warning.
* `type: "text"` (one sample): `imageInData` is `""`, `attStringData` is an NSKeyedArchiver
  `NSAttributedString` (`NSString`, `NSAttributes` = `{NSFont: UIFont {NSName, NSSize, UIFontTraits},
  NSColor: UIColor}`; several runs would use `NSAttributeInfo`, unverified) → `TextBox` in the same
  frame, font family, size `NSSize · s` (the font size is taken to be in canvas units, so it scales
  with the page: **inferred**), colour, bold / italic from the font name or traits; the trailing
  newline of the text view is dropped. A rotation is applied about the centre (unverified, the
  sample is not rotated; the model turns text about the top-left corner, so the corner is moved
  accordingly). One warning says the layout is inferred from a single sample.
* Anything else (another `type`, a text box whose string cannot be read) is skipped with a warning
  naming the kind.

## 4. Geometry

* **PDF-backed page**: the page has the PDF page's size in pt (rotation applied, `gnnote.pdfutil`)
  and `s = pdf_width / W` pt per canvas unit for ink, images and text. In every sample `W = 1485`
  and `W : H` equals the PDF page's proportions (960 × 540 → `[1485, 835.3125]`, 720 × 540 →
  `[1485, 1113.75]`, 1180 × 820 → `[1485, 1031.95]`), **verified** by renders of the decoded ink
  over the slides. When a PDF page's proportions differ from the canvas by more than 1 %, the page
  is still read with `s = pdf_width / W` and a warning says the placement is unverified (how
  CollaNote fits mixed page sizes is unknown).
* **Blank page**: `W × H` units. In a note with PDF pages it takes the scale of the nearest PDF page
  (the one before it, else the one after), so a page inserted between slides gets their size, as in
  the app; in a note without PDFs, 0.2 mm per unit (`s = 72 / 25.4 × 0.2 = 0.56693`): a
  `[1050, 1485]` notebook is A4 (595.28 × 841.89 pt), **inferred** from the 112-page notebook.
* Coordinates are only scaled, never flipped: both CollaNote and the model use a top-left origin.

## 5. PencilKit drawings (`gnnote/pencilkit.py`)

`PKDrawing.dataRepresentation()` is used by CollaNote 1.x pages and by Apple Notes, Freeform and
other PencilKit apps. The module is app-neutral: `parse_pkdrawing(data) -> PKDrawing` (version, ink
table, strokes, counts of skipped records), `decode_pkdrawing(data) -> List[PKStroke]` and
`to_model_stroke(stroke, scale, dx, dy)` / `to_model_strokes(...)`.

Layout (inkterop's `docs/formats/pencilkit.md`, validated there against Apple's framework):
`wrd\xf0` magic, `u16` LE container version, one protobuf message; `#4` ink table `{#1 RGBA f32,
#2 "com.apple.ink.pen" …}`, `#5` strokes `{#4 ink index, #5 path, #6 renderBounds}`; path `{#2
creation date, #3 count, #4 per-point channel mask, #5 constant channel mask (complementary, union
0x7FF), #6 constant block, #7 point records}` with eleven channels in bit order (location 2×f32,
time f32, width f32, aspect u16/1000, ?, force u16/1000, azimuth, altitude, opacity 2v/65535,
secondary width f32, ?).

Additions found on the 854 real iOS drawings (all **verified** on that corpus):

* **Stroke `#7`** is the lasso transform, a message of six f32 `{#1 a, #2 b, #3 c, #4 d, #5 tx,
  #6 ty}` (all six always written); positions are `(a·x + c·y + tx, b·x + d·y + ty)`. The 2,013
  transformed strokes lie inside their stored `renderBounds` only with the transform applied
  (2,013 of 2,013, and 0 without it or with the opposite sign); the corpus has translations only,
  so rotation and scale follow the `CGAffineTransform` convention unverified (widths scale by
  `sqrt(|ad − bc|)`).
* **Path-less strokes** (543, fields `#1 #2 #3 #4 #8` only) are deleted strokes / CRDT tombstones
  and are skipped.
* **Container version 2** (one drawing): an ink entry carries `#4 "fixed-width"`; the point layout
  is unchanged. Paths may carry `#9 = 0` in both versions.
* The corpus decodes with no damaged record and no leftover byte (record sizes must match their
  count exactly): 16,401 strokes, 444,971 points. inkterop's four fixtures are reproduced on every
  channel to 1e-15.

Model mapping: the stored points (PencilKit's control points) become a polyline; ink
`com.apple.ink.marker` → highlighter, `pencil` / `crayon` → `pen="pencil"`, `fountainpen` →
`"fountain"`, `watercolor` → `"brush"`, `pen` / `monoline` / unknown → plain pen; point width =
`size.width`; a median per-point opacity below 1 (pencil) lowers the stroke's alpha.

Hardening: at most 200,000 strokes, 2,000,000 points and 10,000 inks per drawing; anything that is
not a PKDrawing raises `ValueError` and nothing else; a single undecodable stroke is skipped and
counted.

**Legacy CollaNote pages** (unverified, no sample): ink in `drawing` is read with the page's scale
and drawn below the `_dkDrawing` ink. The MIT converter reports that 1.x notes used one continuous
canvas, so when all of a page's legacy ink lies in the band of page `i` (`max y > 1.25 H` and
`min y ≥ i·H − 0.25 H`) it is moved up by `i·H`. Such pages add a warning.

## 6. Mapping summary

| CollaNote | Model |
|---|---|
| note `name` | `Document.title` |
| `N.pdf` shown by a page | `Document.pdfs[str(N)]`, `Page.background = PdfBackground(str(N), pageIndex)` |
| page | `Page` (section 4); `paper` from `paperOrTemplate` on blank pages |
| `_dkDrawing` stroke | `Stroke` polyline, per-point widths, RGBA, kind by inkType |
| `drawing` (legacy) | `Stroke`s via `gnnote.pencilkit` |
| image attachment | `Image` (PNG / JPEG / PDF), rotation |
| text attachment | `TextBox` with one `TextRun` per attribute run |
| audio, bookmarks, unknown attachments | dropped, one warning each |
| thumbnail, tool presets, stroke times, force and tilt | ignored (no visible content is lost) |

## 7. Warnings

One line each (English): unknown inkType codes (with the codes and the stroke count), damaged ink
data or strokes, stroke counts that disagree with `strokeCountBeforeSaving`, legacy PencilKit pages,
PDF pages with other proportions, missing or unreadable PDFs and out-of-range PDF references, PDFs
shown on no page, rotated images, unreadable images, text boxes converted (layout inferred),
unreadable or unknown attachments, audio, bookmarks, a newer package format or a manifest page
count that differs, several notes in one archive, the early single-file layout, and every limit
below.

## 8. Hardening

* ZIP members above 256 MB (declared size) or beyond 1 GB per note are skipped before inflating
  (pages reach 3.8 MB each, notes 100 MB).
* A base64 payload (ink layer, image, embedded PDF) may decode to at most 64 MB; a page holds at
  most 200,000 strokes and 2,000,000 points; at most 10,000 pages are read.
* JSON that is not an object, deeply nested JSON, non-numeric sizes and indices, widths and sizes
  out of range all degrade to warnings. Ink points that are not a number, infinite or more than
  1,000,000 canvas units off the page are dropped (one counted warning; legacy PencilKit points
  too), and a PDF page side above 1,000,000 pt makes the page blank.
* A page that cannot be read becomes a blank page with a warning; only data that is not a CollaNote
  note at all raises `ValueError`, and nothing else escapes `read_cnote`.

## 9. Limitations and open questions

* **No writer.** Unknown: how strict the app's `Codable` decoding is, which import route accepts a
  `.cnote` (share sheet, Files, the iCloud "home folder"), whether it wants format 1 or a zipped
  package, the inkType table, text box keys beyond the one sample.
* Pen types other than 1, 5 and 27 (and the unknown 4) are read as plain pens; widths are taken as
  the rendered width in canvas units, uncalibrated against CollaNote's own PDF export.
* Text boxes: one sample; the font scale, insets, alignment and multi-run strings are inferred.
* Image rotation direction, mixed PDF page sizes, portrait PDFs and paper names other than
  "Notelined" have no sample.
* Legacy (CollaNote 1.x) PencilKit pages and the bare-JSON layout have no sample.
* Audio, bookmarks, the eraser's partial masks and CollaNote's pen effects (dashed, outline, 3D
  pens) are not converted.

Samples that would close the gaps (CollaNote ≥ 5.0; please give the app and iPadOS version):

1. **Calibration note** (new A4 notebook, plain paper): one labelled stroke per pen in the pen menu
   (ballpoint, fountain, calligraphy, felt, outline, dashed, dotted, waterbrush, pencil, sketch
   pencil, the 3D and seasonal pens) at the smallest and largest width, each in another colour;
   freehand and straight highlighter; a straightened line, a recognised rectangle and circle; a
   lasso-moved group and a lasso-rotated one; a photo rotated about 30°; a text box with two lines,
   bold, italic and a colour, rotated if possible; optionally a 3-second audio recording. Export it
   as a CollaNote file **and** as PDF (the PDF is the visual reference for widths, colours and
   positions).
2. **PDF note**: import one PDF mixing A4 portrait, US Letter and 16:9 pages, draw a corner-to-corner
   cross on each, export as `.cnote` and as PDF.
3. **Library copy**: in Files, compress a note from the CollaNote iCloud folder and send the `.zip`
   (shows what the on-disk format-2 package looks like when shared).
4. **Writer start**: once a writer prototype exists, report which import route (share sheet, Import
   menu, iCloud folder) opens a generated format-1 file and a zipped format-2 package with editable
   ink.
5. Any note last edited with CollaNote 1.x or 2.0 (legacy PencilKit pages), exported unopened.

## 10. Tests

`tests/test_collanote_reader.py` and `tests/test_pencilkit.py` build synthetic notes and drawings
with `tests/cnote_builders.py` (no samples needed), and use the real files when present:

* `YTU-Archive` and `Flashcard` are pinned in `tests/conftest.py` (`REPOS`; YTU-Archive with a
  sparse checkout of `1-2/Semiconductor/slide` only), inkterop's fixtures come from its pinned
  clone; all are fetched at test time (CI clones them) and never committed.
* The 100 MB notebook is in `LARGE_FILES`: used when it sits in the samples directory, downloaded
  (from the pinned commit, SHA-256 checked) only with `GNNOTE_LARGE_SAMPLES=1`; CI skips it.
