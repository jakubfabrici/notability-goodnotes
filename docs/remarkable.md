# reMarkable `.rmdoc` and v6 `.rm` — format notes and gnnote's reader

gnnote reads reMarkable documents exported by the desktop app (`.rmdoc`) and single page
files (`.rm`, scene format version 6, written by firmware 3.x) (`gnnote/remarkable/`). The
ink becomes editable strokes in GoodNotes or Notability. Writing is out of scope for now:
`.rmdoc` imports work only through the desktop app or the USB web interface, and the pen
rendering of synthesised files still needs calibration on a device.

Confidence markers: **[verified]** checked on the bytes of real files or against rmscene;
**[inferred]** consistent evidence without a controlled experiment; **[unknown]**.

Sources (see `NOTICE.md`): rmscene (MIT; the v6 block format; `gnnote/remarkable/scene.py`
re-implements the subset gnnote needs with the standard library), rmc (MIT; palette, line
heights of typed text), inkterop's `docs/formats/remarkable.md` (CC BY 4.0; stored widths,
Paper Pro geometry) and RM-Sticker-Press (GPL-3.0, facts only: the `.rmdoc` member layout,
`redir`, the 226-dpi PDF mapping it calibrated on a Paper Pro).

## 1. `.rmdoc` container [verified on RM-Sticker-Press's samples; PDF members inferred]

A ZIP named after the document UUID:

| Member | Content |
|---|---|
| `<doc>.metadata` | JSON: `visibleName` (the title), `type`, `parent`, dates |
| `<doc>.content` | JSON: `fileType` (`notebook` / `pdf` / `epub`), `orientation` (`portrait` / `landscape`), `formatVersion`, the page list (below) |
| `<doc>.pagedata` | template names, one line per page (older files) |
| `<doc>.pdf`, `<doc>.epub` | the annotated document (PDF documents; EPUB documents may carry a PDF rendition) [inferred] |
| `<doc>/<page>.rm` | one v6 scene per page — only pages with content have one |

Page list: `formatVersion 2` stores `cPages.pages[]`, each `{id, idx: {value}, template:
{value}, redir: {value}, deleted: {value}}` — pages are shown in `idx` order (fractional
index strings, compared as strings), `deleted` pages are skipped, `template.value` names the
paper (`Blank`, `P Lines medium`, `P Dots S`, ...), `redir.value` is the 0-based PDF page a
page shows (absent for pages inserted into a PDF). Older files list `pages[]` (ids) with a
parallel `redirectionPageMap`. Without a usable page list gnnote reads the `.rm` members in
archive order, with a warning.

## 2. v6 scene format (`.rm`) [verified: gnnote's parser agrees with rmscene on 44 pages]

```
"reMarkable .lines file, version=6" padded with spaces to 43 bytes
blocks: u32 length, u8 0, u8 min_version, u8 version, u8 type, then `length` bytes
values: varuint tag = index << 4 | type; type 0xF CRDT id (u8 author + varuint counter),
        0xC sub-block (u32 length + content), 0x8 / 0x4 / 0x1 eight / four / one bytes;
        last-write-wins values are sub-blocks {id timestamp (1), value (2)}
```

| Block | Content gnnote reads |
|---|---|
| 0x01 scene tree | a group (layer) node and its parent |
| 0x02 tree node | label, visibility, text anchor: anchor id (7), type (8), threshold (9), origin x (10) |
| 0x03 glyph range | text highlight: colour (4), text (5), rectangles (6: count + 4 x f64 x, y, w, h), RGBA (10) |
| 0x04 group item | a child group (value: its node id) inside a parent's CRDT sequence |
| 0x05 line | tool (1), colour (2), thickness (3, f64), points (5), RGBA (8) |
| 0x07 root text | typed text: CRDT items of strings (and formatting codes), paragraph styles, position (f64 x, y), width |
| 0x0D scene info | paper size (5: u32 width, u32 height) — 1620 x 2160 on a Paper Pro, 1404 x 1872 on a reMarkable 2 |

Every item block carries `parent (1), item id (2), left (3), right (4), deleted length (5)`
and an optional value sub-block (6) starting with the item type. Children are ordered by
their left/right links (a topological sort; concurrent inserts put the higher author id
first; a cycle keeps its items in id order). A deleted item has no value; a group whose
group item is deleted is not drawn. Unknown blocks, unknown fields and bytes a block leaves
unread (newer firmware) are skipped.

Points: version 2 (block version >= 2) is 14 bytes `f32 x, f32 y, u16 speed, u16 width,
u8 direction, u8 pressure`, the stored width being 4 x the rendered width; version 1 is six
f32 `x, y, speed, direction, width, pressure` with the width itself.

## 3. Geometry

* Canvas units to points: **72 / 226** (226 dpi) [inferred: rmc's reMarkable 2 constant; the
  Paper Pro's official export uses 685 pt / 2160 units = 227 dpi, 0.5 % apart].
* Notebook pages: x is centred on 0 and y grows downwards; the canvas is the scene info's
  paper size, else 1404 x 1872, swapped for `orientation: landscape` (strokes are stored in
  display orientation [inferred by inkterop on a Paper Pro]). Ink more than 8 units beyond
  the canvas grows the page by a 48-unit margin ("adjustable page height", and wide pages);
  one warning. A bare `.rm` has no orientation and is read as portrait.
* PDF pages: the page is the PDF page (`pdfutil`, display orientation) with that page as
  `PdfBackground`; ink maps as the PDF rendered at 226 dpi with x centred on the page:
  `x_pt = x x 72/226 + width_pt / 2`, `y_pt = y x 72/226` [inferred: RM-Sticker-Press's
  on-device calibration; no PDF sample here]. Pages inserted into a PDF (no `redir`) are
  notebook pages. An EPUB without a PDF rendition becomes notebook pages, with a warning.
* Groups anchored to typed text are shifted by their `anchor_origin_x` and the y of the
  anchor: the first text line sits at `pos_y + 33.6` and each paragraph moves down by its
  style's line height (70; heading 150; bullets and checkboxes 35, rmc's values); the special
  ids `0:0xFFFFFFFFFFFE` / `0:0xFFFFFFFFFFFF` are the top and the bottom of the text (0 and
  the canvas height without text). The 33.6 is measured on rmscene's `Lines_v2.rm` /
  `Lines_v2_updated.rm` pair (the same strokes before and after the firmware anchored them,
  267.59 units apart, `pos_y` 234); rmc's `-88` misplaces that pair by 121 units [verified on
  one pair only]. One warning.

## 4. Mapping to gnnote's model

| v6 | Model |
|---|---|
| tools 15/2 ballpoint, 17/4 fineliner, 16/3 marker, 14/1 pencil, 13/7 mechanical pencil, 12/0 paintbrush, 21 calligraphy | `kind="pen"`, `pen` = ballpoint / none / marker / pencil / pencil / brush / fountain |
| 18/5 highlighter, 23 shader | `kind="highlighter"` |
| 6, 8 erasers | skipped (warning) |
| unknown tool | a pen (warning) |
| point width | `Point.width` = the stored rendered width (floor 0.5 units) — pressure, speed and tilt are already folded in by the device [verified by inkterop against the official export] |
| colour | the line's RGBA when present (highlighters and shaders carry it on newer firmware, alpha included), else the palette: black, grey 144, white, yellow 251/247/25, green, pink, blue 78/105/201, red 179/62/57, grey 125, highlight 255/237/117, green 161/216/125, cyan 139/208/229, magenta 183/130/205, yellow 247/232/81 |
| glyph ranges | one highlighter stroke through the middle of each rectangle, as wide as the rectangle is tall (warning with the count) |
| root text | one `TextBox` per page at `(pos_x, pos_y)`, `width` wide: paragraphs joined by newlines, bullets and checkboxes as characters, headings bold 22 pt, other text 11 pt, inline bold/italic kept (warning: layout approximated) |
| hidden layers | skipped (warning) |
| template | `Page.paper`: `Blank` plain, `*Lines*`/ruled lined, `*Grid*`/squared grid, `*Dots*` dotted; other templates plain (warning naming them) |
| `.metadata` `visibleName` | `Document.title` |

## 5. Limits and hardening

ZIP members above 256 MB (declared) or 1 GB in total are skipped before inflating; at most
2 000 000 blocks per page, 2 000 000 items per group, 1 000 000 points per line,
10 000 000 points per document, 100 000 characters of typed text (deleted runs included),
100 000 highlight rectangles per glyph range, 64 nested groups and 10 000 pages; canvas
coordinates beyond 10^6 units or not finite are dropped. A damaged block is skipped and
counted. `read_remarkable` raises `ValueError` only for data that is neither a ZIP with a
`.content` or `.rm` member nor a v6 page (a version-3 or version-5 page is named as such).

## 6. Samples, tests and open questions

* Pages: rmscene's `tests/data` (13, MIT), rmc's `tests/rm` (20, MIT) and inkterop's CC0
  captures from a Paper Pro (8); documents: RM-Sticker-Press's three `.rmdoc` samples (GPL,
  read as external test data at test time, never copied). `tests/test_remarkable.py` pins
  every sample's page size, stroke counts by kind and ink bounding box, compares every line,
  glyph, point, paragraph and paper size with rmscene run in its own process, and converts
  every sample to both apps and back.
* Synthetic pages and documents built by a small v6 writer in the tests cover tools,
  palettes, version-1 points, paper sizes, landscape, growth, hidden layers, glyphs, typed
  text, anchors, page order and deletion, templates, PDF pages and `redir`, EPUBs, legacy
  page lists and damaged data.
* Wanted samples: an `.rmdoc` of an annotated PDF (to confirm the 226-dpi mapping and
  `redir`) and of an EPUB (what the bundle holds); a landscape notebook and a page with typed
  text and handwriting placed between its lines (to calibrate the anchor offset and line
  heights on current firmware); a page of a reMarkable 2 exported by the desktop app; a page
  with an inserted image (firmware 3.27+), which gnnote does not read yet.
