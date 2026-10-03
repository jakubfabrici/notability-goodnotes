# MyScript Notes (Nebo) `.nebo` — format notes and gnnote's reader

gnnote reads `.nebo` packages (`gnnote/nebo/`): handwriting becomes editable ink in GoodNotes
or Notability. Writing `.nebo` is out of scope (it needs MyScript's undecoded layout data;
no public evidence that a synthesised package imports).

MyScript Notes (renamed from Nebo in September 2025) exports `.nebo` for notebooks, boards,
documents and annotated PDFs (iOS Share sheet, Android *Import…*, Windows double-click). The
same container is what Kobo stylus e-readers store as `My Notebooks/<name>.nebo`.

Confidence markers: **[verified]** checked against bytes of a real file or by an independent
decoder; **[inferred]** consistent evidence without a controlled experiment; **[unknown]**.

Sources (facts; see `NOTICE.md`): inkterop's `docs/formats/nebo.md` (CC BY 4.0) and reader
(MIT), notein-export's `NEBO_FORMAT.md` (MIT), nebo-ocr (MIT), kollate's `SPEC.md` (GPL-3.0,
facts only, Kobo files), KoboNotebookPlus notes (no licence, facts only). No code was copied;
`gnnote/nebo/bink.py` is written from the byte layout below.

## 1. Container [verified]

A ZIP:

| Member | Content |
|---|---|
| `meta.json` | `pageTitle`, `pageExtent [x, y, w, h]` in millimetres (A4 = `[0, 0, 210, 297]`), app version, `Type` (`Raw Content` for notebooks) |
| `rel.json` | `{"pages": {"<id>": {"version": 5}, ...}}` — the pages, in order; may list `objects` |
| `index.bdom` | document layout, MyScript's binary DOM (`BDOM` magic, version 2) — not decoded |
| `pages/<id>/ink.bink` | the page's ink (section 2) |
| `pages/<id>/page.bdom` | the page's layout: text blocks, typeset shapes, math, recognition lattices — not decoded |
| `pages/<id>/meta.json` | the page's `pageExtent` (and `raw-content.page-size`) |
| `pages/<id>/style.css` | the stylesheet the ink's tags refer to (section 3) |
| `objects/<uuid>.png` | imported images (reported by a third party; not in any sample) [inferred] |

Kobo notebooks (`format-version 4.0`) record the canvas in pixels and its resolution in the
root `meta.json` (`iink-user-metadata.kobo.geometry` and `.dpi`: 1404 x 1872 at 228 dpi on an
Elipsa 2E, 300 dpi on a Libra Colour) [kollate/KoboNotebookPlus, not seen by us].

## 2. BINK ink [verified on the CC0 sample]

All little-endian and byte-packed; `str` = `u32` length + UTF-8 bytes.

```
"BINK\0"  u32 version (5)  u8 0  u32 1
u32 nchannels (4); per channel: str name (X, Y, F, T), 4-byte type, u32 has_unit, [str unit (mm, ms)]
u32 layout_len (0x44) + layout table (per channel: offset, block, stride, type)
u32 precision_x (1000)  u32 precision_y (1000)  u32 3  u8 0
u32 nrecords, then nrecords stroke records:
  u32 0xFFFFFFFF                 erased stroke; it still counts as a record (tags index records)
  u32 0x80000000  packed stroke  u64 t0 (microseconds since 1970), f32 x0, f32 y0 (mm),
                                 u16 tilt, u16 0x0C49, u16 0, u32 n,
                                 i16 dx[n], i16 dy[n], u8 force[n]
  u32 0           plain stroke   u64 t0, u32 pen (0x0C49), u32 n, n x (f32 x, f32 y) mm,
                                 n x u32 force, n x u32 time            [Kobo, inferred, no sample]
tag table (section 3), then a short trailing record (not read)
```

* Points: `x = x0 + cumsum(dx) / u`, `u = precision / 2` units per millimetre — **500, one
  unit = 2 µm**, at the precision 1000 of every known file. inkterop verified /500 by overlay
  against the app's own SVG export and kollate on Kobo devices; notein-export's /512 was
  checked only visually. gnnote uses `precision / 2` and warns when a file declares another
  precision (a file with precision 1024 would explain the /512 report) [inferred].
* `force` is 255 everywhere for capacitive input; Apple Pencil strokes carry real values
  [verified by inkterop]. The declared `T` channel has no per-point data in packed strokes.
* Erased strokes leave one `0xFFFFFFFF` word ("runs of ff bytes" in Kobo files).
* When the header is not understood (Kobo files are described as declaring "one or two
  stroke formats"), the decoder looks for the first record whose pen field is 0x0C49 (constant
  in every observed stroke) within the first 64 KB and reads records back to back until one
  is implausible [inferred].

## 3. Tags and styles [verified on the CC0 sample; semantics inferred]

```
u32 0  u32 count  u8 0
count records; all but the first start with u32 kind, u32 id, u32 0:
  str name
  u32 ngroups; per group: u16 3, u16 start sample, u32 first record,
                          u16 namespace (0xFF05), u16 end sample, u32 last record
  str payload
```

A tag covers stroke records `first .. last` inclusive (erased records count). Names seen:
pen classes `pen-015 … pen-100` and `brush-0125 … brush-1750`, `component-brush`,
`brush-oriented`, input markers (`capacitive-pen-input`, `active-pen-input`), `.STYLE` with a
CSS payload (`"color:#000000ff;-myscript-pen-pressure-sensitivity: 0.57;"`), grouping
(`HIGHLIGHT_STROKES`, `TEXT_STROKES`, `LAYOUT_STROKES`) and recognition output (`CHAR`,
`WORD`, `TEXT_LINE`, `DIAGRAM` with JSON such as `"DWLabel": "W"`).

`style.css` maps the classes to widths in millimetres: on the iPad sample `.pen-025` is
`-myscript-pen-width: 0.35`, `.pen-035` 0.625 (the app's default pen, "0.35" in its UI),
`.pen-050` 0.9, `.brush-0500` 5; `.component-brush` sets `-myscript-pen-brush: Highlighter`.
Kobo's stylesheet maps the same names to other widths, so the class name alone is not the
width.

## 4. Mapping to gnnote's model

| Model | From |
|---|---|
| `Document.title` | `meta.json` `pageTitle` (else `automaticPageTitle`) |
| pages | `rel.json` order; without it, the `pages/<id>/` folders in archive order |
| `Page.width/height` | `pageExtent` of the page, else of the document, else `raw-content.page-size`, else the Kobo geometry, else A4 (warning); mm x 72/25.4 |
| page growth | ink more than 1 mm outside the page enlarges it (5 mm margin), one warning; MyScript pages scroll |
| `Stroke.points` | packed / plain points, mm x 72/25.4, origin = the extent's top-left |
| `Stroke.color` | CSS `color` `#RRGGBBAA` (alpha kept: the highlighter is `#FFDD3366`) |
| `Stroke.kind` | `highlighter` for `HIGHLIGHT_STROKES`, a `Highlighter` brush or a `highlighter-*` tag; else `pen` |
| `Stroke.width` | `-myscript-pen-width` of the cascade (below); highlighters ignore the page's default pen width |
| `Point.width` | the stroke width; with real pressure (forces that vary) and sensitivity > 0: `base x (1 + s x 2.43 x (force/255 - 0.29))`, clamped to 0.2 .. 3 x base, `base` = the width the class name encodes (`pen-025` = 0.25 mm) |
| `Stroke.pen` | `fountain` / `brush` / `pencil` from the brush name, else none |

The style cascade per stroke: the stylesheet's `ink` and `stroke` rules, then the rules of
the classes named by the stroke's tags in table order, then the declarations of the `.STYLE`
tags covering it (later wins). Defaults: pen 0.625 mm, highlighter 5 mm, sensitivity 0.8 for
pens (the app's default, inkterop [inferred]) and 0 for highlighters.

**Width law** [inferred]: inkterop fitted `rendered = base x (1 + s x 2.43 x (force - 0.29))`
against the app's SVG export of a calibration page (sensitivity 0.8 and 0), with `base` the
class name's width. gnnote applies the law with that base, so the fitted absolute widths are
reproduced; at mid pressure it gives about the stylesheet width (0.35 mm for `pen-025`), which
is what strokes without pressure get. inkterop draws pressure-less strokes at the class-name
width (0.25 mm); gnnote uses the stylesheet's 0.35 mm, which inkterop's own overlay residual
(maximum 0.19 mm from the centre line) supports better.

## 5. What is not converted

One warning each:

* the layout data (`*.bdom`): typed text, handwriting converted to typeset text, typeset
  shapes and diagrams, math, tables — always reported, because whether a page holds any of
  it cannot be told without decoding BDOM;
* embedded objects (`objects/`, or `objects` listed in `rel.json`): images and files, whose
  placement is in the layout data;
* the BINK defects the decoder met (truncation, unknown record type, unusual precision);
* members refused by the ZIP size guards.

Recognition results (`DWLabel`), timestamps, tilt and the input-device markers are ignored.

## 6. Limits and hardening

ZIP members above 256 MB (declared) or 1 GB in total are skipped before inflating; at most
200 000 stroke records per page, 1 000 000 points per stroke, 10 000 000 points per document,
100 000 tags with 10 000 span groups each and 5 000 000 (tag, stroke) pairs; coordinates
beyond 100 m are dropped; JSON that does not parse (or nests too deeply) counts as absent.
The only exception `read_nebo` raises is `ValueError` for data that is not a `.nebo`
package (not a ZIP, or neither `rel.json` nor a `pages/` folder).

## 7. Samples, tests and open questions

* `inkterop/core/tests/fixtures/nebo/nebo-ipad-pen-highlighter.nebo` (CC0; Nebo 7.4.3 on an
  iPad, capacitive pen): one A4 page, a 280-point pen stroke and a 178-point highlighter.
  `tests/test_nebo.py` pins its counts, colours, widths and bounding boxes and compares every
  point with inkterop's decoder (run in its own process).
* Everything else (pressure, erased records, tag ranges, plain records, other precisions,
  header variants, page growth, Kobo geometry) is covered by synthetic packages built in the
  tests.
* Wanted samples: an Apple Pencil page with pressure and an erased stroke (to check the width
  law and tombstones on a second device); a page with typed text, a converted text block, a
  diagram and an image (to see what the BINK keeps and to start on BDOM); a non-A4 page and
  an infinite board; a Kobo notebook (plain records, two-format headers, geometry); an
  annotated PDF exported as `.nebo` (where the PDF lives).
