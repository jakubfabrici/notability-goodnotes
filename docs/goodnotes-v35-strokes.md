# Stroke encodings in schema-25 / schema-35 `.goodnotes` files (Test6–Test9)

Scope: every `#7` ink-stroke record of the four samples written by the latest GoodNotes
(goodparse `samples/Test6..Test9.goodnotes`, `schema.pb` = `08 19` (25) for Test6/7/8 and
`08 23` (35) for Test9), decoded with **our own** `gnnote.applelz4` + `gnnote.tpl` and checked
against GoodNotes' own PDF exports (`Test6.pdf` … `Test9.pdf`, rasterised and vector-extracted with
pymupdf 1.28). Everything below was measured; nothing is recalled from the older documents unless
marked "unchanged". Confidence is stated per claim.

Scripts (session scratchpad `v35strokes/`): `census.py` (wire + TPL census, python-lz4
cross-check), `stats.py`, `shapes.py`, `match2.py` (bbox match against PDF objects),
`ellipse.py` (ellipse fit), `tape.py`, `profile.py` + `pencilwidth.py` (raster width of pencil
strokes), `curve.py`, `kind9.py`, `final.py`, `refrun.py` (both reference parsers on the odd
frames). Page → export-page mapping comes from the binding algorithm of
`goodnotes-v35-binding.md` (`v35/resolve.py`); the export page sizes match it 20/20.

Page map used throughout (notes-layer UUID prefix → export page):

| file | p1 | p2 | p3 | p4 | p5 | p6 | p7 |
|---|---|---|---|---|---|---|---|
| Test6 | EDADA780 | 8AAD87B5 | 50448C65 | 38DDB059 | A37FF8B6 | | |
| Test7 | 6FD1025B | BAB94F89 | A82BCE55 | 308EE367 | | | |
| Test8 | 69A3F28E | 9B8338EE | D90BAFEE | 0A300572 | | | |
| Test9 | 19BBFBA7 (no layer) | **D298AC1F** (A4, grid) | F79D1187 | 3B1DB163 | B11A6A6D (no layer) | A3292C44 | 938FFD76 |

All four files use `canvas = pt × 132/72` (Test9 p2–p4: 1091.35 × 1543.46 for A4 595.28 × 841.89;
p5 2346.67 × 1659.17 for 1280 × 905; p7 834.0 × 262.68 for 454.91 × 143.28). Unchanged.

---

## 1. Census

254 `#7` records in total. **Every one decodes with `gnnote.tpl.decode` — no `ValueError`,
no unknown TPL format string, no unknown `#3`/`#21` value** (high confidence, 254/254).

| file | `#7` records | tombstones (meta `#3`=1 ⇔ stroke `#14`=1) | live auto-shapes (`#9` geometry, empty TPL) | live ink strokes | multi-block LZ4 | `bv4-` |
|---|---:|---:|---:|---:|---:|---:|
| Test6 | 22 | 4 | 5 | 13 | 1 | 0 |
| Test7 | 36 | 12 | 10 | 14 | 1 | 0 |
| Test8 | 10 | 4 | 4 | 2 | 0 | 0 |
| Test9 | 186 | 24 | 11 | 151 | 0 | 1 |

(Our reader's per-page stroke counts equal "live shapes + live ink strokes" on every page:
Test6 13/1/3/0/1, Test7 14/4/1/5, Test8 2/2/1/1, Test9 p2 123, p4 11, p6 22, p7 6. No stroke
of the four files is dropped by `tpl.decode`; see §10 for what *is* lost.)

### 1.1 TPL format strings × stroke `#3` × `#21` × `#5`

| TPL format | `#3` | `#21` | `#5` | Test6 | Test7 | Test8 | Test9 | note |
|---|---|---|---|---:|---:|---:|---:|---|
| `vuA(v)A(S(uu))A(S(uuuu))vA(f)` (flat, word0 = 2) | — | 24 | — | 4 (all empty) | 11 (all empty) | 7 (all empty) | 58 (36 pts + 22 empty) | ball pen |
| same | — | 24 | 1 | 3 (1 pts) | 15 (7 pts) | 2 (1 pts) | 9 (all pts) | highlighter, α 0.5 |
| **`vuA(v)A(S(uu))A(S(uuuu))` (short, word0 = 1)** | — | 24 | **1** | 0 | 0 | 0 | **33** (all pts) | highlighter inside a moved group (§3.3) |
| `vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` (ribbon) | 1 | 24 | — | 1 | 9 (6 pts) | 0 | 65 (52 pts) | fountain pen; 4 of them are the `#20` marker/"tape" variant (§6) |
| `vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)` (pencil) | 5 | 25 | — | 14 (12 pts) | 1 | 1 | 21 | all word0 = 1 |

No `#3 = 4` (ribbon-with-width-word) stroke exists in these files; the five format strings of
`goodnotes-stroke.md` remain the complete set. `#21` is 24 or 25 only and equals metadata
`#16` 254/254. `#5 = 1` ⇔ colour alpha = 0.5 (62/62; all 192 other strokes have α = 1.0).
Empty geometry ⇔ (tombstone ∨ auto-shape) 100/100 — there is no empty live stroke.

### 1.2 Stroke-message field sets

Observed signatures (wire types; `L` = length-delimited, `v` = varint):

```
Test6/7/8:  #1L #2L [#3v] #4L [#5v] #6L #7L #9L [#14v] #15L #20L #21v
Test9 adds: #10L (55 strokes), #18v (16 strokes), and non-empty #6 (44 strokes)
```

| field | what the four files show | confidence |
|---|---|---|
| `#2` | Apple LZ4 frame in **all 254** strokes (never absent, never a point list) | high |
| `#4` | **always** the RGBA colour message `{#1 R #2 G #3 B #4 A}` (fixed32, 0.0 omitted). 115 Test9 strokes carry only `#4 = 1.0` (= opaque black). It is **never** a `{x, y}` point and never repeated. goodparse's "f4 point-message fountain-pen strokes" (commit fb9d4b9) do not exist in these files — its fallback can only trigger on a stroke whose frame it fails to find, and its own `_find_geometry_blob` accepts `bv41`/`bv4-`; the fountain-pen 'o' it cites is an ordinary ribbon stroke that its byte-scanning heuristic lost. | high |
| `#6` | `""` (empty) on 210 strokes; `{#1 f32 dx, #2 f32 dy}` on 44 Test9 strokes, all with `#10 = 2FFFB185…`, all `(−488.608, 627.542)`. Adding it to every TPL point puts the strokes exactly where the export draws them (§3.3) — it is the group's translation, as the old doc guessed. | high |
| `#9` | `""` on 217; on 37 strokes a shape message (§8) — 30 live, 7 erased | high |
| `#10` | group UUID: `EB54562B…` on all 11 strokes of Test9 p4 (no `#6`), `2FFFB185…` on 44 strokes of p2 (with `#6`). Grouping only; no geometric effect by itself. | high |
| `#14` | varint 1 on exactly the tombstones (24 + 12 + 4 + 24 = metadata `#3 = 1`) | high |
| `#18` | varint **1** on 16 ribbon strokes of Test9 p2 (records 15, 45, 59, 61, 107, 135, 141, 145, 159, 169, 173, 185, 187, 189, 211, 215: black, r 0.35–1.0, consecutive metadata `#9` counters 6789824–6789839, a single handwriting row at y ≈ 1370–1410). Geometry/colour/width identical to the other ribbon strokes in the export. Unknown flag — possibly "converted / recognised" — ignore it. | medium (presence), low (meaning) |
| `#20` | `""` on 250; `{#1 ""}` (`0a 00`) on 4 ribbon strokes (Test7 p4 rec 5 — a tombstone — and Test9 p2 rec 47/55/119): the marker / "tape" family, §6 | high |

Metadata records are unchanged: `{#1 uuid, #2 clock, [#3 1], #8 device, #9 counter, #14 5381, #16 type}`.

---

## 2. Apple LZ4 frames: multi-block streams and the first real `bv4-` block

python-lz4 4.4.5 (`lz4.block.decompress` per block, previous output as `dict`) reproduces
`gnnote.applelz4.decompress` **byte for byte on 254/254 strokes** (high).

Block patterns: 251 × `bv41 bv4$`, 1 × `bv4- bv4$`, 1 × 4 `bv41` + `bv4$`, 1 × 5 `bv41` + `bv4$`.
Zero trailing bytes after `bv4$` in all 254.

### 2.1 Multi-block frames (ribbon strokes > 32 KiB)

| stroke | TPL size | blocks (dsize / csize) |
|---|---:|---|
| Test6 p5 `A37FF8B6` rec 3 (1439-point fountain pen, 100 212 B TPL) | 100 212 | 32768/19929, 32768/32527, 32768/22521, 1908/19 |
| Test7 p4 `308EE367` rec 3 (1699 points) | 133 538 | 32768/22826, 32768/31512, 32768/32897, 32768/23936, 2466/21 |

* GoodNotes (libcompression) splits at **32 KiB**, not 64 KiB. Our `applelz4.compress` writes
  64 KiB blocks; that is still valid LZ4 and both reference decoders concatenate blocks, but a
  byte-exact writer would use `BLOCK_SIZE = 32768` (medium: two samples).
* **Every block decodes on its own** (`lz4.block.decompress` without `dict` succeeds for all 9
  blocks) — no match reaches back into a previous block, so a writer may emit independent blocks
  (what ours does) and a reader may stream them. (High for these samples; the shared-history
  reader stays correct either way.)
* The third block of Test7 is *larger* than its input (32 897 > 32 768): libcompression does not
  fall back to a stored block there, so `csize > dsize` must be accepted (our decoder does).
* The last tiny block (1908 B → 19 B, 2466 B → 21 B) is the zero-filled tail of the arc-flag
  pool, hence the extreme ratio.

### 2.2 The `bv4-` stored block — header is 8 bytes (now proven)

Test9 p2 `D298AC1F` record 21 (stroke `69BA149C…`, a 7-point grey highlighter dot inside the
moved group): `#2` is 127 bytes:

```
  0  62 76 34 2d  73 00 00 00              'bv4-'  u32 n_raw_bytes = 115        (8-byte header)
  8  74 70 6c 00  73 00 00 00  76 75 41 28 76 29 41 28 53 28 75 75 29 29 41 28 53 28 75 75 75 75 29 29 00
                                           TPL image: 'tpl\0', size 115, "vuA(v)A(S(uu))A(S(uuuu))\0"
 41  01 00                                 word0 = 1 (short flat format)
 43  23 71 d4 40                           W = 6.638811
 47  04 00 00 00  00 00 01 00 01 00 01 00  flags [0,1,1,1]
 59  01 00 00 00  20 dd 36 44 4a f1 2f 44  1 start (731.4551, 703.7701)
 71  03 00 00 00  85 c8 36 44 … a6 fb 2f 44  3 quads (731.133,703.910,731.086,703.931) …
123  62 76 34 24                           'bv4$'
```

`8 + 115 + 4 = 127` and the TPL size word (115) equals the stored length, which settles the
`bv4-` header layout: `magic + u32 size` (as `goodnotes-stroke.md` §4.4 assumed from xnu, and as
goodparse implements). Consequences, verified by running both parsers on this record
(`refrun.py`): `gnnote.applelz4` and goodparse decode it; **parser-for-goodnotes raises
`CompressionError: truncated Apple LZ4 block`** (12-byte header) and additionally never looks at a
field without the bytes `bv41`, so it silently drops this stroke. GoodNotes emits `bv4-` when the
LZ4 block would not be smaller than the input (115-byte TPL). A writer may keep emitting
all-literal `bv41` blocks (§4.3 of the old doc) — those are what the other 253 small strokes use.
(High.)

---

## 3. Flat format (`#3` absent): what changed

Nothing in the layout. New observations:

### 3.1 Widths — `W/2` points holds for every value in the four files

Every stroked path in the four exports has line width exactly `W/2` (pymupdf `width`, cap 1 /
join 1 = round; 87 flat strokes with points + 30 live line/quadratic/rect/ellipse shapes):

`1.299 → 0.6496`, `1.559 → 0.7795`, `4.677 → 2.3386`, `5.4567 → 2.7283`, `6.6388 → 3.3194`,
`9.631 → 4.8154`, `10.3937 → 5.1968`, `10.788 → 5.394`, `11.086 → 5.543`, `12 → 6`,
`14.789 → 7.394`, `16.268 → 8.134`, `24 → 12`, `32.22 → 16.11`, `33.357 → 16.679`, `36 → 18`,
`38.398 → 19.199`. (High.) The reader's `width_pt = W / 2` is right; the writer's `W = 2 × pt` too.

### 3.2 Positions — exact

For all 87 flat strokes with points, the bounding box of the quadratic chain (start + control
+ end points, `× 72/132`, plus `#6`) equals pymupdf's path rect to **≤ 0.05 pt** (Test9 p6/p7:
median 0.03, max 1.06 — the 1.06 is a 2-point stroke whose single control point sticks out of
pymupdf's rect by design). Straight highlighter lines (Test9 p2 rec 29/83/123/133, W 12/36) are
flat strokes with collinear points, matched to 0.00 pt. (High.)

### 3.3 The short format is alive, and it comes with `#6` + `#10`

33 strokes of Test9 p2 use `vuA(v)A(S(uu))A(S(uuuu))` with word0 = **1**, no trailing `v A(f)`.
All are highlighters (`#5 = 1`, α 0.5, W 6.6388 or 10.788), all belong to group
`#10 = 2FFFB185…`, all carry `#6 = (−488.608, 627.542)`, and the 11 ribbon strokes of that group
have TPL word0 = 1 as well (versus 2 everywhere else). So word0 is a *stroke-serialisation
version* that survives copy/paste: the group was pasted from older content and GoodNotes kept the
old serialisation while adding the translation in `#6`. Export check: with `#6` added the 33
strokes land on the export's grey/yellow 3.319-pt paths to ≤ 0.05 pt (e.g. rec 7: ours
(54.0, 667.7)–(147.2, 759.1) vs PDF (56.98, 670.61)–(144.22, 756.18) after removing the W/2 pad);
without `#6` they would sit 266 pt to the right and 342 pt too high. (High.) Our reader already
decodes the short format and adds `#6`.

---

## 4. Ribbon format (`#3 = 1`, fountain pen)

* 59 live ribbon strokes (1 + 6 + 52). Layout unchanged: flags, start pool stride 3 `(x, y, r)`,
  panel pool stride 6, then the CGPath pools (commands 0/2/3, 5 floats per arc, `pool7` empty).
  Version-1 strokes (the 11 pasted ones) have all seven CGPath pools populated too.
* **`r` is the half-width in canvas units — exact.** For 55 of the 59 live strokes the export's
  fill rect equals `(min(x − r), min(y − r), max(x + r), max(y + r)) × 72/132` to ≤ 0.3 pt
  (Test6 p5 100-KiB stroke: 0.04 pt; Test7 p4: 0.03/0.27 pt; Test9 median 0.14 pt). The 4
  exceptions are the `#20` marker strokes (§6) and 3 grey strokes my matcher filtered as "paper".
  r ranges: Test9 handwriting 0.35–1.0 (fountain pen, pressure-driven), Test6/7 big strokes
  2.3–3.0. The reader's `width = 2 r × scale` with per-point widths is right. (High.)
* Exported as filled outlines (fill, α 1.0), i.e. GoodNotes renders the stored CGPath, not a
  stroked centre line.
* parser-for-goodnotes still drops the first point (1438 vs 1439 on Test6 p5); goodparse's
  heuristic returns `None` for the Test7 133-KiB stroke. Ours keeps all points.

---

## 5. Pencil (`#3 = 5`, `#21 = 25`)

35 pencil strokes (12 + 1 + 1 + 21 with points; 2 tombstones). Layout exactly as before:
`W`, flags `[0, 1 × N]`, one 5-tuple start `(x, y, a, b, c)`, N 11-tuples
`(u32 seed, x1, y1, a, b, c, x2, y2, a, b, c)`, five trailing arrays always empty. Flags never
contain 2/3; one sub-path per stroke.

### 5.1 The three per-point attributes

| file | `a` | `b` | `c` | input |
|---|---|---|---|---|
| Test6/7/8 (all 14) | 0.5236 = π/6 const | 1.0472 = π/3 const | 0.0 const | no tilt/force data |
| Test9 (21) | 0.281–0.640, varies slowly inside a stroke (e.g. 0.439–0.456) | 0.877–1.030, nearly constant per stroke | **0.073–2.376**, varies point to point | Apple Pencil |

Interpretation (medium-low): `c` is the **force** (UITouch force, 0–~4), `a` the altitude angle
(16°–37°, the usual writing tilt) and `b` the azimuth, with (π/6, π/3, 0) as the defaults written
when no Pencil data exists. Nothing in the export can confirm which of `a`/`b` is which; only `c`
visibly matters for rendering (see 5.3).

### 5.2 goodparse's "stride-12 x,y,pressure" and "zero-pressure sentinels" — explained

There is no stride-12 array in a pencil TPL. The 11-tuples are 44 bytes; the seed word and the
(a, b, c) triples at offsets 4/8/12… are what a stride-12 scan sees as "x, y, pressure" — the
"spurious near-origin reset records with x < 1 or y < 1" are the `a, b` angles (0.28–1.05) and the
"subnormal-pressure sentinel" is the force of a light point or a 0.0 attribute. Decoding the TPL by
its format string yields no sentinel: min force in Test9 is 0.073, min coordinate 373 (p2 rec 261).
The only pencil stroke with points at the origin is Test6 p3 rec 9, whose geometry is a 3-point
placeholder `(0,0)` × 3 because the stroke is a **recognised ellipse** (`#9.#4`, §8) — render the
shape, never the points (our reader does). (High.)

### 5.3 Rendered width — W is only a nominal size

Pencils are exported as rasters (one image per stroke; 21 images on Test9 p2 = 21 pencil
strokes). Perpendicular intensity profiles at 600 dpi (`profile.py`) give the full width at half
maximum:

| stroke(s) | W | W/2 pt | measured FWHM (median) | FWHM / W |
|---|---|---|---|---|
| Test6 p1, 9 strokes, force 0 | 7.795 | 3.90 | 4.2–7.7 pt | 0.54–0.99 |
| Test6 p1 rec 19/39, force 0 | 10.394 | 5.20 | 9.2 pt | 0.88 |
| Test8 p3 rec 1, force 0 | 10.394 | 5.20 | 3.8 pt (very faint) | 0.37 |
| Test9 p2, 21 strokes, force 0.07–2.4 | 1.559 | 0.78 | 0.3–1.5 pt, median 0.7 pt | 0.19–0.96, median 0.45 |

The raster bbox exceeds the centre line by 0.6–1.4 pt (Test9) and 4–9 pt (Test6/8) — a blur
margin, not the ink width. So the pencil's visible width is a textured brush ≈ 0.4–0.9 W pt
depending on force and (apparently) on something else that differs between Test6 and Test8
(same W, same constant attributes, 2.4× different width — colour/opacity or a brush setting
that is not in the stroke record). `PENCIL_WIDTH_FACTOR = 0.5` is inside the observed band;
keep it, keep the warning, and if per-point widths are ever wanted scale `c` (force) into
`[0.5, 1.2] × W/2` (low confidence; untested against a render).

---

## 6. Marker / "tape" (`#20 = {#1 ""}`): the flags-4/5 ribbon variant, now understood

Four strokes (Test7 p4 rec 5, erased; Test9 p2 rec 47, 55, 119 — the three red bands at
y ≈ 430–506 pt, colour (0.925, 0.325, 0.294), α 1). All use the ribbon format with
flags `[4, 5, 5, …]`, metadata `#16 = 24`, `#3 = 1`, `#9` empty. Test9 rec 47 in full:

```
start pool  (4 floats)   (206.347, 803.849, 9.0, 0.0)
panel pool  (9/panel)    (210.976, 801.366, 9.0,  216.521, 799.569, 9.0,  0.0, 0.0, 0.1) …  15 panels
panel counts             8 per panel; commands [0, 2, 2, 6, 2, 2, 2, 6]  (MoveTo, 2 cubics, 6, 3 cubics, 6)
cubics                   5 per panel (450 floats)      moves 2 per panel
arcs (7 floats each)     (216.521, 799.569, 0.9, 9.0, 1.570, −1.571, 0.0), (206.347, 803.849, 0.9, 9.0, −1.571, 1.570, 0.0) …
arc flags                2 per panel, all 1
```

* `r` is constant 9.0 on all 31 points (18 canvas = **9.82 pt full width**); the export's band
  is exactly `y ± r` (0.00/0.04 pt). Command **6 is an elliptical arc** `(cx, cy, rx, ry, a0, a1, 0)`
  with `rx = r/10`, `ry = r`: the caps are 1.8-unit-thin ellipses, so the band ends flat and
  extends only `r/10 = 0.49 pt` beyond the first/last point (export x0 112.06 vs point 112.55 —
  the matcher's "4.42 pt" discrepancy is precisely `r − r/10` of round-cap padding that is not
  there). The earlier guess "(cx, cy, 1.8, 18.0, …) with r = 18" in `goodnotes-stroke.md` §2.2 was
  the same structure at twice the size. (High.)
* goodparse calls these "red tape" and found the width by trial ("stores 9.0 → true 2×"); the
  structure above is the reason. Whether the GoodNotes UI calls the tool *marker* (inkterop's
  calibration) or *tape* is not decidable from the file; `#20 {#1 ""}` + flags 4/5 + constant r is
  the signature either way.
* Our reader: `_ribbon` emits the stroke with width `2 r × scale` (= 9.82 pt, correct) and pen
  `"marker"`; only the cap shape (round instead of flat) differs from the export. The `start_extra`
  `[0.0]` and `panel_extra` `[0, 0, 0.1]` floats are ignored (they are constants here).

---

## 7. Highlighters

Unchanged: `#5 = 1`, α = 0.5, flat format (long or short), `W/2` pt, round caps/joins in the
export (cap 1, join 1 on all 54 translucent paths). The export draws them as ordinary stroked
paths with `CA 0.5`, one path per stroke. Note Test7 p1 rec 11/13/55 and Test9 p2 rec 81/209/273
are highlighter/pen *lines* stored as `#9.#1` auto-shapes with an empty TPL (§8), not as ink.

---

## 8. Auto-shapes (`#9`) — exact fields, units and the new fill record

30 live + 7 erased strokes carry `#9`. The TPL is a 62-byte empty flat image (or the (0,0)
pencil placeholder) and the TPL `W` is 0.0 on 6 of them, so **the width must come from
`#9.#15`** (our reader does this). `#9.#5 = {#2 1}` is present on all shapes except the pencil
one (meaning unknown, ignore). Sub-messages, all coordinates in canvas units, `#6` applies:

| sub | layout | count | export |
|---|---|---:|---|
| `#1` | repeated `{#1 x, #2 y}` point messages (12 B each); 2 = line, 3–5 = polyline; closed when first == last | 20 | stroked polyline, width `#15/2`, bbox error 0.00 pt on all 20 |
| `#2` | `{#1 P0, #2 C, #3 P1}` — **one quadratic Bézier** (start, control, end) | 2 (Test9 p2 rec 65, 181) | the export draws the cubic `c1 = P0 + ⅔(C − P0)`, `c2 = P1 + ⅔(C − P1)`: rec 65 → (794.40, 1273.20), (796.07, 1282.24) vs stored C (793.76, 1277.88), match to 0.01 |
| `#3` | `{#1 centre, #2 size}` axis-aligned rectangle | 3 | corners `centre ± size/2`, 0.00 pt (the `corner + size` reading is wrong by size/2) |
| `#4` | `{#1 centre, #2 (a, b) semi-axes, #3 θ rad}` | 12 | least-squares fit of the 4-cubic export path: centre exact, axes = (a, b) × 1.0001, major-axis angle = θ (mod π/2) on all 12, including (8.86, 8.86) dots. Rotation is the plain matrix `(x cosθ − y sinθ, x sinθ + y cosθ)` in y-down canvas coordinates — our `_shape` uses exactly that |
| `#15` | f32 nominal width W → stroked at `W/2` pt | all | 10.3937 → 5.1968 etc. |

(High for all rows; the large bbox "errors" of rotated ellipses in a naive comparison come from
pymupdf's rect including the Bézier control points, not from the data.)

Zero-length shapes exist (Test9 p2 rec 209: `#9.#1` with two identical points; the export draws a
zero-length stroked line, i.e. a round dot of diameter W/2).

### 8.1 Top-level record kind `#9` = the translucent fill of a closed auto-shape

Test6/7/8 have 13 `#9` **top-level** records (3 per page on Test6 p1, Test7 p2, Test8 p1/p2, 1 on
Test6 p3). They are not strokes; each is

```
{#1 uuid, #2 {#1 {x, y} top-left, #2 {w, h}} bbox, #3 clock, #4 {#1|#3|#4 same shape sub-message as the
 stroke's #9}, #5 uuid of the OUTLINE stroke, #6 "", #7 {#1 R #2 G #3 B #4 A} fill colour,
 [#14 1 when erased], #15 clock, #18 24}
```

`#5` resolves to the shape stroke on the same page in 13/13 cases; `#7` is the outline colour
with **A = 0.1**, and the export indeed draws a 10 % alpha fill under every closed polygon,
rectangle and ellipse that has such a record (Test6 p1: 2 green fills; Test7 p2: 3; Test8: 4).
Lines and open polylines have no fill record. Erased fills have `#14 = 1` and a NaN bbox.
**Test9 (schema 35) contains no kind-`#9` record at all**, and its closed shapes (p2 rec 39
triangle, the six ellipses) are exported without any fill — so the fill is a per-shape option,
not implied by closedness. (High.) Our reader skips kind `#9` with a warning → the 10 live fills
of Test6/7/8 are lost; the stroke outline is kept.

---

## 9. Reference-parser facts cross-checked (facts only; nothing copied)

* goodparse (commit 969cdd5 onward) uses the same constant scale 6/11 and the same events-log
  binding as ours; its stroke decoding is byte scanning, which is why it needed the "f4
  point-message", "stride-12", "sentinel", "offset-40 marker width" and "2× tape" fixes — all of
  which are artefacts of not parsing the TPL. Its commit `8fd44b6` claims marker width
  "offset-40 × 0.25 = pt" (12 → 3.0, 24 → 6.0, 32.22 → 8.1); offset 40 of the long flat image
  *is* the `W` word, but GoodNotes' own export strokes those Test7 p1 highlighters at 6.0, 12.0
  and 16.11 pt (`W/2`, §3.1), so its factor is half the exported width.
* parser-for-goodnotes: 12-byte `bv4-` header (fails on Test9 rec 21), `bv41`-substring
  detection (never sees that record), drops the first ribbon point.

---

## 10. What `gnnote.goodnotes.reader` currently gets wrong on these four files (strokes only)

Verified by running `read_goodnotes` and by emulating its per-record decisions (`final.py`):

1. **Test9 p2 `D298AC1F` (137 records, 123 live strokes incl. 11 shapes) is attached to a
   guessed page** ("no template event; page size and paper are guessed"): it is emitted as page
   7 of 7 with 455.04 × 588.45 pt and `scale = 72/132`. The scale happens to be right for A4
   (canvas 1091.35 = 595.28 × 132/72), so every stroke keeps its correct absolute position, but
   the page is 253 pt too short: **75 of the 123 live strokes/shapes lie entirely below
   y = 1078.8 canvas** (the moved group `2FFFB185` at y 1224–1387 after its `#6` offset, the
   `#18` fountain-pen row at 1370–1410, the lines/quadratics/dots at 1260–1310) and 2 more
   (rec 161 ellipse, rec 267 line) are cut; the page also lacks its grid paper and sits at the
   wrong index. The pencil row ("Hello Franz Frieda", y 650–710) and the red bands (y ≈ 800) stay
   inside the guessed page.
   Cause and fix are in `goodnotes-v35-binding.md` (`#54` page → layer binding), not in the
   stroke decoder. Same mechanism for Test6 p5 `A37FF8B6` (1 stroke) and Test7 p4 `308EE367`
   (5 strokes): both are standard-size pages so nothing is clipped, but paper and page order are
   guessed.
2. **Auto-shape fills (top-level kind `#9`) are skipped** on Test6 p1/p3, Test7 p2, Test8 p1/p2
   — 10 live 10 %-alpha fills lost (outline kept).
3. **`#9.#2` quadratic shapes** (Test9 p2 rec 65, 181) are emitted as a 3-point polyline through
   the control point; the control point lies ≈ 2 canvas units off the true curve (0.4 pt in the
   export). Cosmetic.
4. **Marker/"tape" caps**: 3 strokes get round caps instead of the flat elliptical ends
   (≈ 4.4 pt of extra band at each end at this width). Cosmetic.
5. **Pencil width**: 35 strokes at `W/2` pt with no force modulation (observed 0.2–1.0 W; see 5.3).
6. **Ribbon width** is the median `2r` per stroke, i.e. the per-point radii are kept in
   `Point.width` but the stroke width is a single number — unchanged behaviour, fine for
   Notability's fractional widths.
7. Nothing is dropped by the decoders: 0 `ValueError`s, the multi-block and the `bv4-` frames
   decode, every TPL format is known, `#6` and `#10` are handled. The flag `#18` is ignored
   harmlessly.

So for strokes the only *load-bearing* defect is (1), which is a binding problem; (2) is the one
real omission in the content model.

---

## 11. Recommended reader changes (ordered)

1. Fix the page/layer binding first (separate document); until then, when a layer has no
   template event and `index.events.pb` is present, derive the page size from the maximum
   stroke extent rounded up to a known paper instead of `DEFAULT_PAGE_SIZE` — or at least warn
   with the canvas extent so the clipping is visible.
2. Handle top-level kind `#9`: parse `{#4 shape, #5 outline uuid, #7 RGBA}`; when `#7.A < 1` and
   `#14` is absent, add the shape as a filled polygon/ellipse (alpha from `#7`) beneath the outline
   stroke, or at least attach `fill_color` to the matching shape stroke. Skip `#14 = 1`.
3. In `_shape`, treat `#9.#2` as a quadratic: emit `points = [P0, P1]` with the cubic controls
   `c1 = P0 + ⅔(C − P0)`, `c2 = P1 + ⅔(C − P1)` (same elevation the flat decoder already uses).
4. (Optional) For `RibbonStroke` with flags containing 4/5 (`start_extra` 1 float,
   `panel_extra` 3 floats) set `pen = "marker"` even without `#20`, and mark the stroke
   `flat_caps = True` if the model ever grows a cap attribute.
5. (Optional) Pencil: keep `PENCIL_WIDTH_FACTOR = 0.5`; if per-point widths are wanted, use
   `attrs[i][2]` (force) when it is non-zero: `w_i = W/2 × clamp(0.5 + 0.35 × force, 0.5, 1.2)`.
   Low confidence; verify on a device before shipping.
6. Writer: optionally switch `applelz4.BLOCK_SIZE` to 32768 to match GoodNotes' own framing
   (not required for correctness).

No change is needed in `gnnote.tpl` or `gnnote.applelz4`.

---

## 12. Open questions

* Meaning of stroke `#18 = 1` (16 strokes, one handwriting row) and of `#9.#5 {#2 1}`.
* Which of pencil `a`/`b` is altitude vs azimuth, and why two force-0 pencils with identical
  attributes (Test6 W 10.39 vs Test8 W 10.39) render 2.4× apart.
* UI name of the `#20 {#1 ""}` + flags-4/5 tool (marker vs tape) and whether its elliptical caps
  depend on a nib setting.
* Whether GoodNotes accepts 64 KiB `bv41` blocks on read (it writes 32 KiB).
