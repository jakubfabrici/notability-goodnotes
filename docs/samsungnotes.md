# Samsung Notes `.sdocx` — format notes and gnnote's reader

gnnote reads the notes of **Samsung Notes** (`gnnote/samsungnotes/`): the app that comes with
Galaxy phones and tablets, and its Windows version from the Microsoft Store. Handwriting stays
editable ink. gnnote does **not** write `.sdocx`: two integrity values of the format (the page
and layer hashes, section 7) cannot be reproduced yet, and no note written by any third-party
tool has been checked in the app.

Confidence markers: **[verified]** checked on the bytes of the public sample notes or against
the app's own PDF export of them; **[source]** taken from one of the MIT-licensed readers named
below and consistent with the samples, but not exercised by them; **[unknown]**.

## 0. Getting a file out of the app

* **Android (Galaxy phone or tablet):** open the note, tap the menu (three dots), **Save as
  file**, then **Samsung Notes file**. The `.sdocx` lands in the device storage (My Files) or
  can be shared directly (*Share* offers the same file types).
* **Windows:** open the note, use the menu, **Save as file**, then **Samsung Notes file**
  (reportedly several notes selected in the list can be saved at once, one file each). The
  Windows app is officially supported only on Samsung PCs (Galaxy Book): its Store listing
  warns that other computers may have problems, and Samsung said in July 2025 that other PCs
  are not supported. Reports from May 2025 say it runs on other PCs after signing in through
  the Samsung Account app; this is not verified here.
* The Windows app keeps notes that were never exported as **folders** with the same members
  as an `.sdocx` (under `%LOCALAPPDATA%\Packages\SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy\LocalState\sdoc`)
  [source: sdocx2pdf's README; the folder layout is unverified]. gnnote reads a **ZIP of such a
  folder** (the members at the top of the ZIP or under exactly one folder); it does not read a
  folder directly. Prefer *Save as file*.
* *Save as file → PDF* is no substitute: Samsung's PDF export draws handwriting as pictures
  (section 9), so nothing in it is editable.

## 1. Sources and licences

| Source | Licence | Used for |
|---|---|---|
| cable729/inkterop, `core/src/inkterop/formats/sdocx.py` at commit `f16eb8d2a425637aab629e6cc90c00a18f17009f` | MIT, (c) 2026 Caleb (cable729) | **Ported** (`records.py`): the cursor and bitfield primitives, the fixed-point deltas, the stroke event decoder (plain and delta-compressed), the stroke field list up to bit 17, the layer header, the page layout, `pageIdInfo.dat` and the string registry in `note.note` |
| inkterop `docs/formats/sdocx.md` | CC BY 4.0 | facts only; no text reused |
| squ1dd13/sdocx2pdf (Rust) at commit `372ef723a6857b2b878ee9a1dfb9520a8f83edf8` | MIT, (c) 2026 squ1dd13 | **Layouts adapted** (`records.py`): the object base, stroke fields 18–25 and property bit 12, the shape base, shapes, lines, pictures (with their border fields), fill effects, the rich-text record, the page's optional fields (paper template, imported-PDF placements, canvas cache, handwriting-recognition data, custom page objects), the font-size field of `note.note`, `media/mediaInfo.dat`, `end_tag.bin`; the paper-template numbers; **the rendering model** of strokes (`reader.py`: pressure clamp, highlighter families). See the provenance note below |
| "SDOCX Compatibility Corpus" (Hugging Face `twangodev/sdocx-compatibility`, revision `0865fd317776fa4fd60388e93724f789476541d4`) | CC BY 4.0, by the SDOCX Compatibility Corpus contributors | test data: four notes written by Samsung Notes and the app's own PDF exports of them; fetched by the tests (`tests/conftest.py` `PINNED_FILES`), never committed |
| inkterop `core/tests/fixtures/sdocx/synthetic-two-page.sdocx` | CC0 | test data (a synthetic note, never opened in the app) |

Not used: `twangodev/sdocx` (GPL-3.0; neither its code nor its repository samples),
`bxff/samsung-notes-format` (derived from decompiling the app), inkterop's
`device-mods/vendor/` (GPL-3.0). inkterop's reader runs in the tests only as a separate-process
oracle (`tests/test_samsungnotes.py`).

**Provenance of sdocx2pdf: accepted as a fact source (maintainer decision, 2026-11-13).**
gnnote's research named sdocx2pdf as the fact reference for this format. Its README does not
say how its author learned the format, but its source comments name functions and libraries of
Samsung's own app (for example `SPen::ObjectStrokeBinaryHandler::m_ApplyBinary_FlexibleData`,
`_document.dll`, `libSpen_worddoc.dll`, and "the JVM code"), so its author evidently studied the
app's binaries. inkterop's reader in turn says it learned its facts from sdocx2pdf. gnnote's
clean-room rule excludes code and facts derived from decompiling an app (which is why bxff is
not used), so the question was put to the maintainer before release. The decision: sdocx2pdf
is an MIT-licensed project that publishes its code and states no decompilation, and the parts
adapted from it are credited in `NOTICE.md`, so it stays as a fact source. If that ever has to
be revisited, the markers in sections 2 to 6 say which facts the samples confirm (**[verified]**: the object
base; strokes with optional fields 1, 2, 3, 7, 8, 10 and 13 and both event encodings; shapes,
their paths and the line object of sample 02; pictures with crop and uncropped rectangle and
the picture fill of sample 03; the rich text with spans, paragraph attributes and objects in
the text of sample 01; the page header, layers and paper template; the `EOFX` media list; the
end tag) and which come only from sdocx2pdf (**[source]**: everything else, among it the
imported-PDF placements, colour fills, picture borders, custom page objects, stroke fields 14
to 25, the older `EOF` media list).

## 2. Container [verified on the four corpus notes]

An `.sdocx` is a ZIP archive (Deflate; media stored) of little-endian binary records:

| Member | Content |
|---|---|
| `note.note` | document header: format version, id, times, note width/height, **title and body text** (two text-box records), then flag-gated fields: app name and version, author, location, template URI, last edited page, and the **string registry** (id → string; pen class names and pen settings live here). Ends with SHA-256 of everything before it |
| `pageIdInfo.dat` | `note.note`'s hash, a u16 page count, then per page a short string (the page UUID) and the page member's trailing hash. **This is the page order** |
| `<uuid>.page` | one page (section 4) |
| `media/mediaInfo.dat` | media registry: bind id → file name in `media/` (plus a hash, a reference count, a time). The current form ends with `EOFX` and frames each entry with a u32 size; an older form ends with `EOF` [source] |
| `media/*` | pictures (`0@paste_….png`), imported PDFs (`…@pdf_….pdf` [source]), and `.spi` page previews in Samsung's own picture format |
| `end_tag.bin` | u16 size, then format version, note UUID, time, flags (landscape), cover picture, width and height, app name and version (empty and `0xFFFFFFFF` in all four samples), created, last viewed page, **page model** (u16: 1 = one endless page), document type and lock data; ends with the ASCII text `Document for S-Pen SDK` |

## 3. Primitives

* *bitfield*: a u8 byte count (0 to 4), then that many bytes, little-endian.
* *short string*: u16 count of UTF-16LE code units. Object UUIDs use a u16 count of UTF-8
  bytes instead. *Long string*: u32 count.
* *frame*: u32 size counting the size field itself, u16 data type, u32 **flex offset** (from
  the frame's first byte), a property bitfield and a field bitfield, then fixed fields. The
  optional fields that the field bitfield announces start at the flex offset, in bit order
  (shape frames store bit 22 before bit 12 [source]). A bit gnnote does not know stops the
  reading of the optional fields at that bit, because the size of what follows is unknown.

## 4. Pages, layers, objects [verified]

* **Page**: u32 offset of the layer list, u32 flex offset (from the page start), properties,
  fields, orientation, **width, height** (canvas units), offsets, UUID, time, format version,
  minimum format version. Optional fields: drawn rectangle, tags, template URI, background
  picture and its mode, background colour (BGRA), background width and rotation, **imported-PDF
  placements** (bit 8: per placement the media bind id, the PDF page index and the rectangle,
  as four f64 before format version 2034 and four i32 from it [source]), **paper template**
  (bit 9: 0 none, 1–3 lined, 4–6 grid, 7–9 dotted, 10 and up planners and custom papers), canvas
  cache, imported data height, theme. Then u16 layer count, u16 current layer, the layers, a
  32-byte hash and the ASCII text `Page for SAMSUNG S-Pen SDK`.
* **Layer**: a header frame whose flex offset counts from the **page** start (property bit 0:
  hidden; optional alpha, background colour, name, …), a u32 object count, the objects, a
  32-byte hash.
* **Object**: u8 type, u16 child count, u32 size, then the object's frames and a 32-byte
  SHA-256 of the UTF-8 text `uuid + modification time in µs` [verified on 124 objects]. Every
  object starts with the **object base** frame (data type 0): format version, UUID, time,
  bounding rectangle (four f64), a counter, the resize mode; optional rotation angle
  (degrees), bundles, page index (bit 20), … Property bit 3 says the object is visible.
  Types: 1 stroke, 2 text box, 3 picture, 7 shape, 8 line; 10 voice recording, 22 table,
  23 code block and others are not converted.
* A **stroke** (type 1) adds a stroke frame: property bits 0 delta-compressed events, 2 tilt
  present, 3 eraser, 4 fixed width, 11 fixed opacity; a u16 event count; the events; a u16
  tool type (0 unknown, 1 finger, 2 S Pen, 3 mouse, 4 eraser); optional fields: 1 pen settings
  (registry id), 2 **colour BGRA**, 3 **pen size** (f32, canvas units), 4 a u32 of unknown
  meaning, 7 **pen class name** (registry id), 8–25 fixed width, size level, particle settings,
  dash type (bit 14), stroke type, pattern, rainbow and gradient settings. No public reader
  decodes bits 0, 5 and 6.
* **Events**, plain: `x, y` as f64 pairs, then f32 pressures (0–1), u32 times (ms), and with
  tilt f32 tilts and f32 orientations (radians). Delta-compressed: the first `x, y` (f64),
  then `2(n-1)` u16 point deltas (x and y alternating; sign bit, 10 integer bits, 5 fraction
  bits), the first pressure (f32) and `n-1` u16 deltas (sign bit, 3 integer bits, 12 fraction
  bits), the first time (u32) and `n-1` u16 time deltas, then tilt and orientation like the
  pressure.
* A **shape** (type 7) is the object base, a shape-base frame (type 6: connection points, then
  optional line colour effect (bit 2) and line style (bit 3: width f32, compound, dash, cap,
  join, arrow types and sizes)), and the shape frame (type 7: shape type, rectangle, angle,
  the outline path, control points, the drawn rectangle; optional text (bit 0), fill effect
  (bit 5: none, colour, picture, pattern, background), hint text, …). A **text box** (type 2)
  and a **picture** (type 3) have the same frames plus a last frame of their own type; the
  picture's carries the crop rectangle (bit 1, source pixels as i32) and the uncropped
  rectangle (bit 17). The picture itself is the shape's picture fill (a media bind id). A
  **line** (type 8) is the object base, a shape-base frame and a line frame (start, end,
  rectangles, an optional path). Paths are lists of move, line, quadratic, cubic, arc, close
  and oval segments in canvas units, already rotated [source; the circle, polygons and the
  line of sample 02 match the app's export].
* **Rich text** (the note body, text boxes, text in shapes): a u32-sized record with the text
  (long string), **spans** (kind, start, end in UTF-16 units, data: 1 colour, 3 size, 4 font,
  5 bold, 6 italic, 7 underline, 9 link, 17 background colour, 20 strikethrough, …),
  **paragraph attributes** (kind, first and last paragraph, data: 2 indent level, 3 alignment,
  4 line spacing, 5 bullet, 8 space before, 9 space after, …), four margins, the vertical
  gravity, section data and, from format version 2035, the **objects anchored in the text**
  (pictures, tables, code blocks; each with its type, its index in the text, a layout and a
  page-break constraint). Font sizes, margins and spacings are in units of a page 360 units
  wide [verified: the app's export draws 15-unit text as 25 pt and 16-unit margins as 26.7 pt].

## 5. Geometry [verified]

* Canvas units, origin top-left, y down. The samples' pages are 1080 × 1527 and 1848 × 2613
  units.
* gnnote makes every page **600 pt wide**: `pt = unit × 600 / W` (W = the page width), the page
  is `600 × H × 600 / W` pt. This is the scale of Samsung's own PDF export (MediaBox 600 × 848
  for both page widths). Checked: in samples 02 and 04 the strokes' bounding boxes lie inside
  the ink pictures of the export, each edge within 3.5 pt once the stroke width is included;
  the first baselines of all 51 paragraphs of typed text in sample 01 (five pages, with
  lists, a table and a code block) match the export's within 0.1 pt.
  (inkterop maps the short page edge to 595.276 pt, 0.8 % smaller.)
* A note whose `end_tag.bin` page model is 1 is one endless page [source]; gnnote keeps it as
  one tall page.

## 6. Mapping to gnnote's model

| Samsung Notes | gnnote |
|---|---|
| Pages, in `pageIdInfo.dat` order | `Page`, 600 pt wide; landscape pages too (wider than high, or a page orientation other than 0: the app's export scale for them is unknown, warned) |
| A pageless note (`end_tag.bin` page model 1) | its one page record as one long page, made longer when the body text needs it (warned as unchecked) [source; no sample] |
| The empty last page the app keeps after a paged note | dropped, as the app's PDF export does (6 → 5, 2 → 1, 4 → 3, 2 → 1 pages in the corpus) [verified] |
| Paper template 1–3 / 4–6 / 7–9 | `paper` = lined / grid / dotted (samples 04 and 02); other templates: plain, with a warning |
| Imported PDF page filling its page (bit 8, `media/*@pdf_*`) | `PdfBackground` [source; no sample: warned as unchecked]; a placement that does not fill the page is not converted (warning) |
| Stroke | polyline `Stroke` in pt; colour from BGRA |
| `FountainPen`, `InkPen2` (unless fixed width), `Pencil2` (unless fixed opacity) | per-point width = pen size × clamp(pressure, 0.4, 0.7) (sdocx2pdf's model; warned as approximate) |
| other pens (`ObliquePen`, `BrushPen`, `TapePen`, …) | constant width 0.45 × pen size |
| `Marker3`, `Marker4`, `StraightMarker`, `StraightHighlighter` | `kind="highlighter"`, width = pen size (measured on sample 04's export: about 1.0 × pen size; sdocx2pdf uses 1.125 ×). The straight ones run from the first to the last point |
| Pen names | `pen`: fountain, ballpoint, pencil, brush, marker |
| Eraser strokes (property bit 3 or tool type 4) | skipped (warning) |
| Gradient, pattern or rainbow stroke colours (field bits 24 and 25, property bit 12) | drawn in the stroke's one colour (warning) [source] |
| Strokes with field bits 0, 5, 6 or above 25 | skipped one by one (warning), the rest of the note is read |
| Objects without the visible flag | skipped (warning) |
| Shapes and lines | strokes along the outline (exact Bezier handles; arcs and ovals built by gnnote, warned); a line style without a colour draws black (as the app does in sample 02); solid fills become `kind="fill"` strokes (unchecked: warned); arrowheads, gradient/picture/pattern fills and dashes are not converted (warnings) |
| Pictures (page objects and pictures in the text) | `Image` (PNG or JPEG), rotation clockwise about the centre; a PNG crop is applied to the pixels, keeping the PNG's colour-key transparency, colour profile and pixel size chunks (sample 03); JPEG crops, flips, borders and picture frames are not (warnings); `.spi` pictures are not converted |
| Lines bent or curved through control points (connectors) without a stored path | drawn straight from start to end (warning) [source] |
| Sticky notes and other custom page objects (page field bit 18) | not converted (warning) [source] |
| Text boxes and text in shapes | `TextBox` inside the margins, with bold / italic / underline / size / colour / font runs (unchecked: warned); pictures, strokes and other objects anchored in their text are not converted (counted warning) |
| The note's body text | laid out by gnnote, since the app stores no line positions: one `TextBox` per paragraph at its first line, lines wrapped with Helvetica widths, the paragraph's line spacing, spacing before and after (none between two list items, as the app does), indent level × 16 units; pictures in the text are blocks; a table or code block that the app stored up to four font sizes below gnnote's position keeps the stored position (the app keeps margins around tables that gnnote does not know); text that runs past the last page adds pages (warning), in a pageless note it makes the one page longer |
| The body text's font-size change of `note.note` (field bit 11; 0 in every sample) | not applied (warning when it is not 0) [source] |
| Note title (`note.note`) | `Document.title`; objects anchored in it are not converted (counted warning) |

## 7. Known losses and unverified points

* Typed text positions are approximate (gnnote re-flows the text; target apps re-wrap each
  paragraph with their own fonts and line heights). On sample 01 every paragraph starts where
  the app's export puts it, but the rules behind that come from one note: the margin the app
  keeps around a table (about 33 pt above and below it in sample 01, where gnnote keeps 0.35
  font sizes below it) is not modelled, so text that follows a table directly sits up to
  about 60 pt too high (in sample 01 a code block with a stored position follows the table,
  which puts everything after it back in place). Strikethrough, links (the text stays),
  text highlight colours, list bullets and numbers, and paragraph alignment are dropped
  (one warning lists them). Tables, code blocks, voice recordings and other objects are not
  converted (counted warnings). Objects anchored in the text of a text box, of a shape or of
  the note's title (pictures, strokes, tables, ...) are not converted either (counted
  warnings); only the body text places its pictures.
* Pen rendering is approximate: Samsung's pens are textured brushes; gnnote keeps the centre
  line, the colour and a width model. Tilt and timing are read but not used.
* Hidden layers and grouped objects (objects with children) are not converted.
* Not drawn, with a warning: picture borders and frames, gradient, pattern and rainbow stroke
  colours (drawn solid), the bends of connector lines without a stored path (drawn straight),
  sticky notes and other custom page objects.
* Not verified for lack of samples: imported PDF backgrounds, text-box objects on pages, shape
  fills, rotated shapes, landscape pages, pageless notes, notes written by the **Windows** app
  (`end_tag.bin` names no app in any sample), locked notes (their content may be encrypted;
  gnnote warns), the older `EOF` media registry, notes of format versions other than 4000 and
  5400, and Windows note folders. Each of the first six, and locked notes, gets a warning
  when a note uses it. Landscape pages are made 600 pt wide like the others (inkterop scales
  the short edge instead; which one the app's PDF export does is unknown). A pageless note
  keeps its one page record as one long page; body text that runs past it makes the page
  longer (up to 1,000 times its width) instead of adding pages.
* Integrity values: the `note.note` trailer, the page hashes in `pageIdInfo.dat` and the
  object hashes are reproducible; the **page and layer trailing hashes are not** SHA-256 of any
  byte range tried. gnnote reads none of the hashes. This is what blocks a writer.

## 8. Limits and hardening

* `ValueError` only for "not a Samsung Notes file" (not a ZIP; no `note.note` with
  `pageIdInfo.dat` or `end_tag.bin`); everything else degrades with a warning: a damaged
  `note.note` loses pen names and typed text, a damaged page is left empty or keeps the layers
  read before the damage, a damaged object is skipped and counted.
* ZIP members through `readutil.ZipBundle` (256 MB per member, 1 GB per file); every record
  read is bounds-checked before anything is allocated; list counts are checked against the
  bytes left; at most 2,000,000 objects per note, 10,000 pages (plus at most 1,000 more after
  the note's last page for the body text), 100,000 path segments per shape, 2,000,000 UTF-16
  units per text, objects nested at most 4 deep in text; the work of applying text formatting
  is capped (20,000,000 units), so overlapping spans cannot cost quadratic time. `note.note`'s
  title and body are read in place, not copied.
* **Text formatting records**: character spans and paragraph attributes take 14 to 18 bytes
  each and compress to almost nothing (10 million fit in a 440 KB file). Only the kinds gnnote
  uses are stored, with a non-empty range, and only their first 8 data bytes (of a font name,
  its first 64 units); at most 100,000 per text and 500,000 per note. Records of other kinds
  are stepped over (gnnote would not use them); records beyond the caps are stepped over too,
  and the note gets the "more formatting than gnnote reads" warning.
* **Typed text**: all the texts of a note together (title, body, text boxes, text in shapes
  and in objects anchored in text) keep at most 1,000,000 UTF-16 units or 64 per byte of the
  file, whichever is more, and never more than 8,000,000. Two million repeated `x` compress
  to about 4 KB, so a 1 MB file used to hold 500 million characters (minutes and gigabytes in
  the reader and the writers); natural text compresses to about two units per byte. Texts
  are decoded in file order (`note.note`'s title and body, then the pages in order); the text
  that reaches the limit keeps its beginning, the rest is stepped over without being decoded,
  and a warning counts the units that were not converted. Styled runs are found from the ends
  of the formatting spans, not unit by unit.
* **Text boxes**: text-box objects and body-text paragraphs together give at most 2,000 text
  boxes plus one per 8 bytes of the file, and never more than 20,000 (a few KB of repeated
  `x` + newline used to become 11,000 pages and 264,000 text boxes, which some writers turn
  into gigabytes). Text-box objects beyond that are skipped with a counted warning; the body
  layout stops at that cap or at the page cap without laying out the rest, and a warning
  counts the paragraphs and objects it left out.
* **Ink points**: one budget of 10,000,000 points per note (`readutil.MAX_POINTS`) covers
  stroke events, shape outlines and fill polygons. Stroke events are charged *before* they
  are decoded (delta-compressed events take 8 bytes and compress to almost nothing, so a
  small file could otherwise describe billions); strokes beyond the budget are skipped with a
  warning, and so are strokes that are skipped anyway (erasers, unknown fields), without
  being decoded. Decoded events are kept as 8-byte floats and each page's records are
  dropped once the page is converted. A fill polygon gets one vertex per straight edge and
  about one per 2 pt of curve, at most about 2,000 for the curves of one outline (longer
  outlines get a coarser spacing).
* **Pictures**: each picture crop is made once however often it is used, each picture is
  decoded once in a row, and decoding and copying pixels for crops share 24,000,000 pixels
  per note (beyond that, the whole picture is shown at its uncropped size, with a warning);
  pictures above 4,000,000 pixels are not cropped. Picture bytes handed to the model, every
  reference counted, may exceed the file's own size by at most 256 MB (as for Noteful); more
  references to the same pictures are skipped with a warning, so that writers that embed
  every picture separately cannot turn a small note into gigabytes.
* **Body text layout**: linear in the text's length and the number of objects in it; a
  table or code block that runs over page breaks skips the pages after the note's last one
  in one step; a page lower than 36 pt (once 600 pt wide) counts as damaged and becomes an
  A4-shaped page.
* Coordinates that are not finite or lie beyond 10^7 pt drop that point or object; widths and
  font sizes are clamped.

## 9. Samples, tests and open questions

* `tests/test_samsungnotes.py`: record primitives; synthetic notes built in code
  (`tests/sdocx_builder.py`) for every mapping rule and warning; the four corpus notes with
  exact expectations (6, 2, 4 and 2 page records; 0, 77, 0 and 41 strokes; 12 `Marker4` and
  29 `FountainPen` strokes in sample 04; pictures, shapes and text); the strokes against the
  ink pictures of the app's PDF exports; inkterop's reader as a subprocess oracle (stroke
  counts and coordinates); conversions to Notability, GoodNotes and PDF; mutated pages and
  headers; sniffing against every other sample file.
* The app's PDF export: `Creator PDFium`, pages 600 × 848 pt, the paper as a 1800 × 2545 JPEG,
  typed text as real text (Roboto), ink as Flate pictures at about one pixel per canvas unit,
  highlighters composited with *Darken*; the empty last page is left out.
* Wanted from a real device (Windows and Galaxy): a note with every pen, an eraser, a text box
  on the page, a filled and a rotated shape, an imported PDF, a pageless note and a locked
  note, each saved as `.sdocx` and as PDF; a Windows note folder; the Windows app's version
  and page width.
