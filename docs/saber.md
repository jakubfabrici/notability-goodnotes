# Saber (`.sba`, `.sbn2`, `.sbn`): format facts and gnnote mapping

Codec: `gnnote/saber/` (`reader.read_saber`, `writer.write_saber`, `bson`), registry id
`saber`, output extension `.sba`, inputs `.sba`, `.sbn2` and `.sbn`.

Saber is a GPL-3.0 note app for iPad, Android, Windows, macOS and Linux. Its source was read for
**facts only** (key names, defaults, version history); no code was taken from it. Facts are from
Saber `f143d84` (2026-10-02): `lib/data/editor/editor_core_info.dart`, `page.dart`,
`lib/components/canvas/_stroke.dart`, `_circle_stroke.dart`, `_rectangle_stroke.dart`,
`image/*.dart`, `lib/data/tools/*.dart`, `lib/data/file_manager/file_manager.dart`,
`packages/sbn`; and from perfect-freehand 2.5.2 (MIT), the ink engine whose `StrokeOptions`
JSON keys and radius formula Saber uses. Saber's example notes (`test/sbn_examples`,
`test/demo_notes`) are fetched by the test suite as external test data (never committed);
inkterop's CC0 fixture `saber-mac-pens-text.sba` (a Saber for Mac export) is used too. The BSON
codec and the writer's document shape are adapted from inkterop's MIT Saber codec, whose output
opened in Saber for Mac once it was byte-identical to the app's own `main.sbn2` (inkterop
`docs/validated-writes.md`). gnnote's BSON codec re-encodes all 23 app-written sample notes
byte for byte (`tests/test_saber.py`).

## 1. Containers [verified]

* `.sbn2`: one BSON document (format version `v` 13 .. 19). Since version 19 images and PDFs
  are separate asset files next to the note, `<note>.sbn2.0`, `.1`, ... (and a preview
  `<note>.sbn2.p`), which a single `.sbn2` file does not carry.
* `.sba`: a ZIP with `main.sbn2` and its assets `main.sbn2.N` (Saber's own export). Saber's
  "Import note" takes the first member whose name ends in `sbn2` / `sbn` and every member whose
  last extension is a number.
* `.sbn`: the same structure as JSON (versions up to 12), with assets inline (base64 in the
  top-level `a`, or a list of byte values in the image's `b` before version 11); the oldest
  notes are a bare JSON list of strokes.

## 2. Structure and reading (`reader.py`)

Top level (in Saber's order): `v` version, `ni` next image id, `b` background colour (ARGB or
null), `p` background pattern (`""`, `college`, `college-rtl`, `lined`, `grid`, `dots`,
`staffs`, `tablature`, `cornell`), `l` line height (int, default 40), `lt` line thickness (int),
`z` pages, `c` current page; `a` inline assets (before 19); `s` / `i` / `w` / `h` strokes,
images and page size at the top level before version 8.

Page: `w`, `h` (doubles, default 1000 x 1400), `s` strokes, `i` images, `q` Quill delta (the
page's text), `b` background image; empty lists are left out.

Stroke: `shape` (null, `circle` with `cx cy r`, `rect` with `rl rt rw rh`), `p` points,
`i` page index, `ty` tool (`fountainPen`, `ballpointPen`, `Pencil`, `Highlighter`, `ShapePen`;
`Pen` is the old name of the fountain pen), `pe` pressure enabled (default true), `c` colour
(ARGB as an unsigned integer, int64 above 2^31; default black), `ox` / `oy` point offset, and
the perfect-freehand options with Saber's defaults: `s` size (10), `t` thinning (0.5), `sm`
smoothing (0), `sl` streamline (0.5), `ts` / `te` taper, `cs` / `ce` caps, `sp` simulate
pressure (true), `f` complete. Points are BSON binaries of little-endian float32 `x y` or
`x y pressure` (version 13+), `{x, y, p}` maps before.

Image: `id`, `e` extension (`.png`, `.jpg`, `.svg`, `.pdf`, ...), `i` page, `v` invertible,
`f` Flutter `BoxFit` index for backgrounds (fill, contain, cover, fitWidth, fitHeight, none,
scaleDown), `x y w h` box, `sx sy sw sh` source rectangle (crop), `nw nh` natural size, `a` asset
index; PDF images also `pdfi` (0-based PDF page).

**Units [inferred].** Saber pages are 1000 units wide; Saber scales an imported PDF page (which
it measures as 595 x 841 pt for A4) to 1000 units, and prints a page at 1 unit = 1 pt.
gnnote reads a page whose background is a PDF page at that PDF page's size (scale = PDF width /
1000) and every other page at `PT_PER_UNIT` = 0.595 pt per unit (A4 width), so a default page is
595 x 833 pt. y grows down from the top-left corner.

**Ink.** Saber draws ink with perfect-freehand: diameter = `s * (1 - t * (1 - 2 * pressure))`
(identity easing), i.e. `s * (0.5 + pressure)` with the default thinning. gnnote gives every
point that width (strokes without stored pressure, including speed-simulated ones, get `s`),
`Stroke.width = s`, `Highlighter` -> `kind = "highlighter"` (alpha from the colour, 100/255 by
default), `Pencil` -> `pen = "pencil"` (its texture and tapers are not reproduced),
`fountainPen` / `Pen` -> `"fountain"`, `ballpointPen` -> `"ballpoint"`. Circles become four
exact cubic arcs, rectangles a closed outline with straight cubic handles. Eraser, laser-pointer
and selection strokes (never saved by Saber) are dropped with a warning.

**Images.** PNG / JPEG at `x y w h`; PDF images showing page 1 as `Image(fmt="pdf")`; SVG and
other formats, and PDF images of later pages, are skipped (warning); crops are not applied
(warning). A page background `b` that is a PDF page becomes `PdfBackground(pdf_id, pdfi)`
(`template_is_builtin = False`); a raster background becomes a full-page image fitted with its
`BoxFit` (centred), below the page's content. Assets of a bare `.sbn2` are missing (one
warning: export as `.sba`).

**Text [inferred].** A page's Quill text is laid out from the top of the page with the line
height `l` as font size and line pitch, padding 1.2 l on top and 0.5 l on the sides. gnnote
makes one `TextBox` per page at that position (leading blank lines move it down), spanning the
page width minus the padding, with bold (also headers), italic and underline runs; embedded
objects are dropped (warning). One warning says the layout is approximated.

**Paper.** `p` sets every page's `Page.paper` (college, lined, cornell, staffs, tablature ->
lined; grid -> grid; dots -> dotted; Cornell / staves / tablature approximated, warning). A
background colour other than white becomes a generated paper PDF of that colour
(`template_is_builtin = True`).

**Hardening.** BSON lengths are checked against their enclosing document before use, nesting
is limited to 32 levels and the decoded values to 5 000 000; ZIP members are size-checked before
inflating (256 MB per member, 1 GB per archive); JSON nested too deeply is a `ValueError`; at
most 10 000 pages, 200 000 points per stroke and 5 000 000 per note. Page sides outside
1 .. 10^6 units and JSON integers too large for a float are treated as missing; stroke sizes
are clamped to 1000 units, thinning to perfect-freehand's -1 .. 1; stroke points, shapes and
images beyond 10^7 pt are skipped, widths and text sizes clamped to 10^4 pt. Every page,
stroke, image and page text is converted on its own, so one damaged element costs only itself
(warning). Only `ValueError` leaves `read_saber` (not a Saber note, unreadable BSON / JSON,
over the size limit).

## 3. Writing (`writer.py`)

Output: `.sba` (a ZIP with `main.sbn2` and `main.sbn2.N`, deflated), what Saber exports and
imports; it is self-contained, which a bare `.sbn2` would not be once a note has images or PDFs.

* Top level exactly `v 19, ni, b null, p, l, lt 3, z, c 0` in that order. `p` is the most
  common paper of the pages that do not show a PDF (one pattern per note; other pages: warning).
  `l` (int) is the median text size in units (20 .. 100; 40 without text).
* Pages: `w` = 1000.0, `h` = page height x `k` with `k = 1000 / page width in pt`; `s`, `i`, `q`,
  `b` only when not empty, in Saber's order.
* Strokes (`shape, p, i, ty, pe, c, s, sl, sp` like the app's): Bezier chains flattened to about
  1 pt. Constant width -> `ballpointPen`, `pe` false, 8-byte points, `s` = width. Varying width ->
  `fountainPen`, `pe` true, 12-byte points with `pressure = width / s - 0.5`, `s` = the middle of
  the stroke's width range: ranges up to 3:1 are exact, wider ones keep the widest point and
  widen the thinnest to a third of it (warning). Highlighters -> `Highlighter` without pressure
  (the model's alpha; an opaque one gets Saber's 100/255). Pencil -> `Pencil` with pressure and
  the app's pencil options `sl 0.1, ts 1, te 1`. Other tools get `sl` 0 (no streamline lag, so
  Saber draws through the stored points) and every stroke `sp` false. Colours are unsigned ARGB
  (int64 when above 2^31, as Dart stores them; the signed form made Saber reject inkterop's
  first attempt).
* Images: PNG / JPEG as assets (`e .png` / `.jpg`, `v` true, `f` 1, box, `sw sh nw nh` = pixel
  size); PDF images as Saber PDF images (`e .pdf`, `nw nh` = PDF page size, `pdfi` 0). Rotations
  are dropped (warning); other formats are skipped (warning). Identical assets are stored once.
* PDF backgrounds (user PDFs always, stock paper with `--paper pdf`): `b = {id, e .pdf, i, v,
  f 1, x 0, y 0, w, h, nw, nh, a, pdfi}`; one asset per PDF.
* Text boxes become the page's Quill text: sorted top to bottom, each starting on the text line
  nearest its top (`round(y / l - 1.2)`, blank lines inserted), bold / italic / underline as
  Quill attributes, every box ending in a newline. Horizontal position, box size, colour,
  alignment and rotation are lost (warning).
* Shape fills are dropped (Saber has no filled shapes; warning).
* No random identifiers: image ids count from 0; with `random_seed` set the ZIP timestamps are
  fixed too, so the output is reproducible.

## 4. Limitations and what to check in the app

* **Unverified in Saber**: the pt scale (Saber has no physical page size); the Quill text
  position; `fountainPen` strokes with `sl` 0; how Saber shows a raster background with `cover`
  (gnnote's image extends past the page). The overall shape (BSON key sets and order, `.sba`
  layout, unsigned colours, pencil options) follows the app-checked inkterop writer.
* Saber has no positioned text, filled shapes or image rotation; one paper pattern per note;
  pressure ratios above 3:1 are compressed.

Open in Saber (iPad or desktop, "Import note"): a GoodNotes notebook with pressure ink,
highlighter, an image, text and a user PDF; a Notability note with a PDF background; check
stroke widths against the source, the PDF pages, images and where the text lands.
