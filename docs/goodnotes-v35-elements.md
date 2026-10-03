# `.goodnotes` page elements in schema-25 / schema-35 files (GoodNotes 6 of 2026-10): shape fills, "Type 35" text, die-cut stickers, EXIF-rotated photos

Companion to `goodnotes-elements.md` (written from nine older samples) and `goodnotes-container.md`. This
document covers everything the four **newest** goodparse samples add or change at the **element** level:
`Test6/7/8.goodnotes` (`schema.pb` = `08 19` = 25, GoodNotes 6, written 2026-10-01/02) and `Test9.goodnotes`
(`schema.pb` = `08 23` = 35, 2026-09-30, 7 pages of mixed sizes). Each file ships with GoodNotes' own PDF
export (`Test6.pdf` … `Test9.pdf`), which is the ground truth for every geometry claim below. Nothing here comes
from GoodNotes documentation; goodparse (GPL) was read for facts only, nothing was copied.

Paths: samples are `scratchpad/ref-new/goodparse/samples/<name>.goodnotes`; the helper scripts are in
`scratchpad/v35elem/` (`census.py` kind census, `rdump.py` record tree dump with absolute byte offsets,
`verify.py` export cross-checks). Record indices are 0-based into the length-prefixed record stream of the
named `notes/<N>` member; byte offsets are absolute in that member. `k = 6/11` is the canvas→pt scale of every
page here (`canvas = MediaBox × 11/6` holds for all 20 pages, including the 1280×905 Figma page and the
454.91×143.28 photo page). Notation as in the earlier docs: `#n` = protobuf field n, `f32` = little-endian
float32, `rect` = `{#1 {#1 f32 x, #2 f32 y}, #2 {#1 f32 w, #2 f32 h}}`, `clock` = `{#1 varint version, #2 varint nonce}`.

Summary of what is new (each verified against the export unless marked otherwise):

| finding | where | confidence |
|---|---|---|
| top-level `#9` content record = **fill of an auto-shape** (10 % alpha of the stroke colour), linked to its parent stroke by UUID | Test6/7/8, 13 records | high |
| top-level `#21` content record = **"Type 35" text element** (sticker letters *and* ordinary typed text in schema 35), text in an Apple-LZ4 `bv41` protobuf blob | Test9 p3, 4 records | high |
| the "one text box per page" on Test6/7 is **real**: ordinary RTF `#8` boxes holding the page captions (19 live + 1 tombstoned); the oracle that reported none (parser-for-goodnotes) does not parse `#8` | Test6/7, 20 records | high |
| image attachments can be a **die-cut sticker PDF**; `#2` size == its MediaBox in canvas units; new image fields `#6` (1 = JPEG photo, 3 = sticker PDF) and `#7` (group UUID) | Test9 p3 | high |
| photos keep their raw JPEG; GoodNotes applies **EXIF orientation** (6 → 90° CW) and `#2` holds the rotated size | Test9 p4 | high |
| `#7.#9.#2 {#1 P0, #2 P1, #3 P2}` is a **quadratic Bézier** (P1 = control point), not a polyline | Test9 p2 | medium-high |
| `#9.#2` of a fill record is the bbox of the ellipse's *rotated a×b rectangle*, exact bbox for polygons/rects | all fills | high |
| the "no template event" on the last page of Test6/7 and on Test9's 123-stroke page is the **UUID carry** case (`N = P + 1` with hex carry) | 3 pages | high |

---

## 0. Page map (needed to compare anything with the exports)

Page order comes from the `#54.#4.#1` order keys; the page UUID `P` is the notes-layer UUID `N` **minus one as a
128-bit integer** (see §8). Export page sizes all match the bound paper PDF's MediaBox.

| file | export page | `notes/` member (prefix) | key | paper | elements (live / tombstoned) |
|---|---|---|---|---|---|
| Test6 | 1 | `EDADA780` | `4EGLM2` | ruled std | 13 strokes (2 auto-shapes), 2 `#9` fills, 1 `#8` text / 3 strokes, 1 fill |
| Test6 | 2 | `8AAD87B5` | `APaxN` | blank std | 1 stroke, 1 `#8` / 1 stroke |
| Test6 | 3 | `50448C65` | `FOw4R` | blank std | 3 strokes (3 ellipses), 1 `#9` fill, 1 `#8` |
| Test6 | 4 | `38DDB059` | `KnCf0` | blank std | 1 `#8` (mixed bold/italic/underline/strike) / 1 `#8` |
| Test6 | 5 | `A37FF8B6` (`…EDC920`) | `O0CZ3` | blank std | 1 stroke, 1 `#8` — **page `P` = `…EDC91F`** (carry) |
| Test7 | 1 | `6FD1025B` | `4lHdw2` | blank std | 14 strokes (6 line shapes), 7 `#8` (the numbers 0.3 … 6.2) / 9 strokes |
| Test7 | 2 | `BAB94F89` | `Ae7L0` | blank std | 4 shapes, 3 `#9` fills, 4 `#8` / 2 strokes |
| Test7 | 3 | `A82BCE55` | `F9LqN` | blank std | 1 pencil stroke, 1 `#8` |
| Test7 | 4 | `308EE367` (`…C820A0`) | `KrbnF` | blank std | 5 strokes, 2 `#8` / 1 — **`P` = `…C8209F`** (carry) |
| Test8 | 1 | `69A3F28E` | `4HVRp` | blank std | 2 shapes, 2 fills / 2 shapes, 1 fill |
| Test8 | 2 | `9B8338EE` | `A2JQn` | blank std | 2 ellipses, 2 fills / 1 ellipse, 1 fill |
| Test8 | 3, 4 | `D90BAFEE`, `0A300572` | | blank std | 1 stroke each |
| Test9 | 1 | `19BBFBA7` | `4mgFG1` | A4 "Green" | empty member (0 B) |
| Test9 | 2 | `D298AC1F` (`…E450`) | `4mgFG2` | A4 grid "White" | 123 strokes (11 auto-shapes) / 14 — **`P` = `…E44F`** (carry) |
| Test9 | 3 | `F79D1187` | `Am5Pm` | A4 grid "White" | 1 image (sticker PDF) + 4 `#21` texts |
| Test9 | 4 | `3B1DB163` | `F4uWc` | A4 blank | 11 strokes + 1 image (JPEG, EXIF 6) |
| Test9 | 5 | `B11A6A6D` | `KBM_u` | Figma PDF 1280×905 | empty member |
| Test9 | 6 | `A3292C44` | `L2FtP` | form PDF 595.2×841.68 (paper `80973E20` → attachment `4452790C`) | 22 strokes / 10 |
| Test9 | 7 | `938FFD76` | `O9t!N` | photo-PDF 454.91×143.28 | 6 strokes |

Census of content-record kinds (`census.py`), top-level field number = kind, over all 20 members:
`#7` stroke 210 live + 44 tombstoned; `#8` RTF text 19 + 1; `#9` shape fill 10 + 3; `#1` image 2; `#21` text 4.
No `#20` (sticky note) and no `#22` ("Type 31" line/arrow) record exists in these files either.

Metadata records are unchanged in shape: `{#1 E, #2 clock, [#3 1 tombstone], [#4 attachment UUID (images)], #8 device,
#9 counter, #14 5381, #16 schema}` with **`#16 = 35` on the four `#21` elements** (Test9 p3 rec 0/2/4/6) and 24 or 25
on everything else, including the two images of Test9. So "Type 35" is simultaneously the metadata schema value, the
value of `#21.#2`, and the schema.pb of the file. `#16 = 25` marks pencil strokes (`#7.#3 = 5`, `#21 = 25`) in all four
files, exactly as in Test5.

---

## 1. Kind `#9`: shape fill record (Test6/7/8; GoodNotes "fill shape" feature)

### 1.1 What it is

When a recognised auto-shape (a stroke whose `#7.#9` holds geometry, §3) is closed — ellipse, rectangle, closed
polygon — and the user keeps the default "fill" setting, GoodNotes writes a **second element** whose content
record has top-level `#9`. It carries a copy of the shape geometry, the parent stroke's UUID, and the fill colour
= the stroke RGB at **alpha 0.1**. The export draws it as a filled path with `fill_opacity = 0.1` and the stroke's
RGB, under or over the outline (both orders occur). Open shapes (lines, the 3-point curve) and shapes drawn with
the fill option off (Test7 p2 "Blue oval on axis not filled", Test6 p3's two unfilled ellipses) have no `#9`
companion. 13/13 fill records (10 live, 3 tombstoned) resolve to a stroke **on the same page** (5 of them
precede their parent in the stream, 8 follow it); the geometry bytes inside the fill's `#4` are identical to the
parent's `#7.#9.#1/#3/#4` (10/10 live cases, checked bytewise), and the RGB equals the parent's `#7.#4` RGB (13/13).

### 1.2 Record layout (Test6 p1 `notes/EDADA780…` rec 13, 185 B incl. the 2-byte length prefix, metadata = rec 12)

```
@17064  b9 01                     record length 185
@17066  4a b6 01                  #9  LEN 182                                   <- the kind
@17069  0a 24 "E3C1A05D-72B7-4ABA-A707-A244644C8697"   #1  element UUID E (== metadata #1)
@17107  12 18 0a 0a 0d.. 15.. 12 0a 0d.. 15..          #2  rect  (218.538, 102.091) + (346.314, 345.796)   see 1.4
@17133  1a 0a 0a 08 08 01 10 <nonce>                   #3  {#1 clock {#1 1, #2 3887495530}}   edit clock (version 1–2 seen)
@17145  22 1f 22 1d 0a 0a .. 12 0a .. 1d ..            #4  geometry: {#4 ellipse {#1 centre, #2 semi-axes, #3 rotation}}  == parent's #7.#9.#4 byte for byte
@17178  2a 24 "37D2336F-C400-4036-8299-12B9661FFD9E"   #5  PARENT STROKE UUID (the auto-shape; rec 6/7 of this page)
@17216  32 00                                          #6  ""   (same slot as the stroke lasso offset; empty in all 13)
@17218  3a 14 0d fd fc fc 3e 15 c7 c6 46 3f 1d d9 d8 58 3e 25 cd cc cc 3d
                                                       #7  RGBA fill colour (0.4941, 0.7765, 0.2118, 0.1000)
@17240  7a 06 10 <nonce>                               #15 {#2 nonce}  == metadata #2 (text-box style clock without version)
@17248  90 01 18                                       #18 = 24  schema
```

The three geometry forms seen inside `#4` are exactly the stroke forms: `#4 {#1 centre, #2 semi-axes, #3 f32 rotation}`
ellipse (9 records), `#3 {#1 centre, #2 (w, h)}` axis-aligned rectangle (2: Test7 p2 rec 5, Test8 p1 rec 9) and
`#1 {repeated #1 point}` closed polygon, first point repeated last (1: Test6 p1 rec 37, 5 points = quadrilateral;
Test7 p2 rec 15, 4 points = triangle). Colour components: `0.0` is omitted (Test8 red fill `#7 = {#1 0.8235, #4 0.1}`,
blue `{#2 0.4784, #3 1.0, #4 0.1}`), as in strokes.

Field order is fixed: `#1 #2 #3 #4 #5 #6 #7 [#14] #15 #18`. The `#2`/`#4` values are those of the parent at the time
of the last edit (the clock `#3.#1` version bumps when the shape is resized: versions 1 and 2 seen).

**Tombstoned fills** (Test6 p1 rec 29, Test8 p1 rec 11, Test8 p2 rec 1): metadata `#3 = 1`; content has `#2` with all
four floats `NaN` (`0x7fc00000`), **no `#4`**, `#14 = 1`, `#15 = {#1 1, #2 nonce}`; their `#5` parent is tombstoned too.
A reader must skip them (the metadata flag suffices).

### 1.3 Verification against the exports (`verify.py`)

* Colour and alpha: every fill drawing in `Test6.pdf` p1, `Test7.pdf` p2, `Test8.pdf` p1/p2 has `fill = stroke RGB`,
  `fill_opacity = 0.1` (PyMuPDF `get_drawings()`), e.g. Test6 p1 `[24] fill=(0.494, 0.776, 0.212) fo=0.1`.
* Ellipse geometry: sampling the exported fill Béziers at 44 points and measuring the radial deviation from the
  ellipse `centre/axes/rotation` stored in `#4.#4` (× k): max |r − 1| = **0.0003** on all five live ellipse fills
  (Test6 p1, Test7 p2, Test8 p1, Test8 p2 ×2). Rotation sign convention: standard rotation matrix in the y-down
  canvas (same as the ellipse stroke rule in `goodnotes-elements.md` §5.1).
* Rectangle: Test7 p2 fill rec 5 `#4.#3` centre (610.74, 458.09), size (293.50, 340.74) → export rect
  (253.09, 156.94)–(413.18, 342.80) pt = canvas (464.0, 287.7)–(757.5, 628.5): centre (610.7, 458.1), size (293.5, 340.7). Exact.
* Triangle: Test7 p2 fill rec 15 points (53.39, 728.94), (362.99, 728.94), (362.99, 392.28) → export fill rect
  (29.12, 213.97)–(197.99, 397.60) pt = canvas (53.39, 392.28)–(362.98, 728.93). Exact.
* Outline width of the parent shape: `#7.#9.#15` = 10.3937 → export stroke width 5.1968 pt; 5.4567 → 2.7283 pt, i.e.
  **W/2 pt** (confirms the critic rule). The fill has no outline of its own.

### 1.4 `#9.#2` (bbox) — explained, not needed for rendering

For polygons and rectangles `#2` is the tight bounding box of the points. For ellipses it is the axis-aligned bbox
of the **rotated a×b rectangle**, i.e. half-width `a|cos θ| + b|sin θ|`, half-height `a|sin θ| + b|cos θ|`, centred on
the ellipse centre — reproduced to 3 decimals on all 5 live ellipse fills (e.g. Test6 p1: stored
`(218.538, 102.091)+(346.314, 345.796)`, predicted `(218.538, 102.091)+(346.314, 345.796)`). It is therefore larger
than the ellipse's true bbox; do not use it as a clip.

### 1.5 Reading recipe

1. On a `#9` content record (metadata not tombstoned): read `#5` (parent UUID), `#7` (RGBA), `#4` (geometry).
2. Build the closed polygon from `#4` exactly as for the stroke's `#7.#9` (ellipse → sampled polygon, rect → 4
   corners, `#1` points as given), scale by `k`, and emit a **filled** shape: in gnnote's model a `Stroke` whose
   `outline` holds the polygon, `color = #7` (alpha 0.1), `width = 0`, and no centre-line points; Notability has
   no fill primitive, so a writer should render it as a filled outline stroke or drop it (10 % alpha, visually minor).
3. Draw it directly beneath its parent stroke (the export does both orders; at alpha 0.1 it is invisible either way).
4. If `#5` does not resolve to a live stroke on the page, drop the fill (never observed, but the 3 tombstoned fills
   show the pattern: both die together).
5. Writer: do not emit `#9`; unfilled shapes are valid (Test7 p2 blue oval).

---

## 2. The "one text box per page" on Test6/Test7 — real RTF boxes, reader is right

Every Test6/Test7 page carries ordinary `#8` RTF text boxes (layout identical to `goodnotes-elements.md` §4.1,
fields `#1 #2 #3 #4 #5 #6 #7 #9 #10 #15 #18 #19 #20 ".tb-0" #21 "" #27 24`, metadata clock with or without a version).
They hold the page captions, and GoodNotes' exports contain them as text (PyMuPDF `get_text`):

| member, rec | `#3` text frame (canvas) → pt | export span bbox (pt) | text |
|---|---|---|---|
| Test6 `EDADA780` rec 21 | (94.18, 64.62) → (51.37, 35.25) | (51.37, 35.24)–… HelveticaNeue 13.09 | "Pencil (orange) above some filled shapes (green)" |
| Test6 `8AAD87B5` rec 3 | (89.66, 27.77) → (48.91, 15.15) | (48.91, 15.14) | "Isolated marker stroke (line, thickness 4.6)" |
| Test6 `50448C65` rec 5 | (125.5, 52.46) → (68.45, 28.61) | (68.45, 28.61) | "Ovals with marker pencil and ink" |
| Test6 `38DDB059` rec 3 | (184.79, 354.33) → (100.80, 193.27) | (100.8, 193.21) regular/Italic/Bold runs | "A short note of mixed text …" |
| Test6 `A37FF8B6` rec 1 | (87.5, 36.45) → (47.73, 19.88) | (47.73, 19.87) | "A really long ink stroke" |
| Test7 `BAB94F89` rec 7 | (139.65, 34.02) → (76.17, 18.56) | (76.17, 18.55) | "Green filled oval (off axis)" |
| Test7 `6FD1025B` rec 39 | (743.03, 46.83) → (405.29, 25.54) | (405.29, 25.54) | "0.3" (and 0.9, 1.05, 2, 2.3, 4.6, 6.2 likewise) |

Frame origin × 6/11 matches the nearest glyph-run origin to ≤ 0.01 pt in x and ≤ 0.06 pt in y for **all 19 live
boxes** (checked programmatically, 19/19); font size `\fs48` → 13.09 pt.
The one tombstoned `#8` (Test6 `38DDB059` rec 1, "Test", metadata `#3 = 1`, content `#14 = 1`) is absent from the
export, as expected. Our reader already reports exactly these boxes (`read_goodnotes`: Test6 1/1/1/1/1, Test7 7/4/1/2).

The "oracle reports none": parser-for-goodnotes (`goodnotes_re.archive.GoodNotesDocument.open(...).pages()[i].text_elements`)
returns `()` for all Test6/Test7 pages because `text.py:parse_text_elements` only decodes `#21` ("Type 35 / bv41")
payloads and never looks at `#8`. The export proves the boxes exist. No reader change is needed for `#8`; the
harness should not use parser-for-goodnotes as the text oracle for schema ≤ 25 files.

---

## 3. Auto-shape geometry `#7.#9` — additions from Test6–9 (38 shapes: 30 live, 8 tombstoned)

All 38 shape strokes follow the §5.1 layout of the elements doc: empty 62-byte TPL blob, `#9 = {geometry, [#5 {#2 1}], #15 f32 W}`.
New facts:

* `#9.#1` with ≥ 3 points and first == last is a **closed polygon** (Test6 p1 rec 5: 5 points; Test7 p2 rec 25: 4 points
  = triangle; Test9 p2 rec 39: 4 points). With 2 points it is a line (Test7 p1: six horizontal lines of widths W =
  1.559, 4.677, 5.457, 10.394, 12.0, 24.0; Test9 p2 rec 81/273 vertical lines). With 2 **identical** points it is a dot
  (Test9 p2 rec 209 `(795.2, 1282.6)` twice; exported as a 2-point InkList at that point, round caps → a disc of
  diameter W/2 pt). Unclosed 3–4 point `#1` lists exist only on tombstoned records (Test7 p2 rec 19/21 = abandoned
  triangle attempts).
* `#9.#2 {#1 P0, #2 P1, #3 P2}` (numbered sub-fields, not repeated `#1`) is a **quadratic Bézier with P1 as the control
  point**. Evidence: Test9 p2 rec 65 `P = (795.69, 1263.84), (793.76, 1277.88), (800.68, 1290.97)`; GoodNotes exports
  that stroke as an `/Ink` annotation with an 11-point InkList (xref 93). Max distance of the InkList points to the
  quadratic `B(t) = (1−t)²P0 + 2(1−t)t P1 + t²P2` (× k) is **0.36 pt**, to a quadratic *through* P1 at t = ½ 1.05 pt,
  to the polyline P0–P1–P2 0.80 pt; the InkList midpoint (433.89, 696.13) sits at the Bézier midpoint (434.17, 696.90),
  not at P1 (432.96, 697.03). rec 181 is nearly straight and cannot discriminate (0.43/0.42/0.43). One decisive
  sample → medium-high confidence. The ex2 rec 15 "polyline (triangle)" of the elements doc is the same `#2` form and is
  most likely a curve too; `goodnotes-elements.md` §5.1 should be corrected. (goodparse's commit "line/triangle/polygon
  from f7.f9 control points" treats `#2` as a polyline; the export says otherwise.)
* `#9.#5 {#2 1}` is present on 37/38 shapes; the one without it (Test6 p3 rec 9, orange ellipse W = 10.39) renders
  identically → still "unknown flag", harmless.
* `#9.#3` rectangle: `{#1 centre, #2 (w, h)}` confirmed by the Test7 p2 / Test8 p1 exports (§1.3). Rotated rectangles
  were not drawn; a rotation field would presumably be `#3.#3` like the ellipse's (unverified).
* Width: `#15` = W, rendered at W/2 pt (10.3937 → 5.1968, 5.4567 → 2.7283, 24.0 → 12.0 for the magenta highlighter
  line, alpha 0.5). Highlighter shapes carry `#7.#5 = 1` like highlighter strokes.
* Z-order and lasso offsets: unchanged. (No shape here has a non-empty `#6`.)

---

## 4. Kind `#21`: "Type 35" text element (Test9 p3, 4 records)

### 4.1 Where it occurs

Test9 p3 (`notes/F79D1187…`, 2 484 B, 10 records) holds one sticker image (§5) and four `#21` text elements: the
three sticker labels "S U C H" / "GOOD" / "F R I E N D S" (Futura-Bold, grouped with the sticker PDF) and a typed
box "Hallo Franz…,!,,!," (Helvetica Neue). In the schema-35 file **typed text is written as `#21`, not as RTF `#8`**
(no `#8` record exists in Test9). Both `#8` (schema ≤ 25) and `#21` (35) must therefore be read.

### 4.2 Record layout (rec 1 @70, 505 B; metadata rec 0: `{#1 E, #2 {#1 17, #2 2617489745}, #8 dev, #9 6789783, #14 5381, #16 35}`)

```
@70   f9 03                         record length 505
@72   aa 01 f5 03                   #21 LEN 501                                         <- the kind
@76   0a 24 "8982AAC3-86FA-55FB-8AA8-1836BB2B16A8"      #1  element UUID E
@114  10 23                         #2  = 35            element type code ("Type 35")
@116  1a 08 08 11 10 <nonce>        #3  clock {#1 17, #2 2617489745}  == metadata #2   (the typed box: {#2 nonce} only)
@126  3a 0a 0a 08 10 <nonce> 18 02  #7  {#1 {#2 group nonce 1065099080, #3 member index 2}}   sticker labels only
@138  42 24 "F3D9B628-9664-407A-9700-A4EBAA87A00A"      #8  GROUP UUID (sticker labels only; == the sticker image's #7)
@176  48 01                         #9  = 1             all four; meaning unknown
@178  a2 01 16  { 0a 0a 0d x 15 y | 15 f32 | 1d f32 }   #20 TRANSFORM: #1 origin (131.328, 206.518) canvas, top-left of the box;
                                                        #2 f32 rotation −0.0042 rad (omitted when 0); #3 f32 uniform scale 1.00574 (1.0 when untouched)
@203  aa 01 11  { 12 0f { 0d w 15 h 1d inf } }          #21 {#2 fixed box size (221.173, 50.0, +inf)}            sticker labels
      (typed box instead: aa 01 16 { 1a 14 { 0d 32 15 600 1d 32 25 inf } }   #21 {#3 auto-size constraints (32, 600, 32, inf)})
@223  b2 01 07  { 0a 05 { 0d 7.0 } }                    #22 {#1 {#1 f32 7.0}}   unknown (corner radius?); {#1 ""} on the typed box
@233  f2 01 13  { 0a 11 { 0a 0f { 0d 1 15 1 1d 1 } } }  #30 box fill colour RGB (1,1,1), alpha omitted = 0 → transparent ("GOOD": {#1 {#1 ""}})
@255  fa 01 17  { 12 02 {0a 00}, 1a 11 {0a 0f {RGB 1,1,1}} }   #31 border {#2 {#1 ""}, #3 {#1 {RGB}}}  (transparent)
@281  82 02 93 02                   #32 TEXT CONTENT (275 B):
@285    0a 91 01                      #1 { 12 82 01 <130-byte "bv41" Apple-LZ4 frame>,   #2 = the runs, see 4.3
                                          1a 08 <8 bytes 17 34 7c b5 04 8a 87 f1>,       #3 = 64-bit value, not md5/sha1/sha256/crc32/adler32/fnv1a of the blob or of its plaintext (unknown; likely a content hash/ID)
                                          20 01 }                                       #4 = 1
@433    12 0a 0d.. 15..               #2 content size (201.173, 30.0) canvas  = box size − left/right and top/bottom insets
@445    2a 52                         #5 default style {#1 {#3 {#4 f32 1.0}, #30 "Helvetica Neue", #40 f32 24.0, #60 varint −60, #70 f32 −20.0},
                                                        #2 {#1 {#1 ""}, #2 "", #3 {#1 −1, #2 −1}}}
@529    52 14 0d 10 15 10 1d 10 25 10 #10 insets (10, 10, 10, 10) canvas  (all equal here, so the side order is unknown)
@551    5a 07 08 01 15 <30.0>         #11 {#1 1, #2 f32 30.0}   on the two 30-high labels; "" on "GOOD"; absent on the typed box (line-height lock?)
@560  8a 02 0e  { 0a 05 25 <0.2>, 1a 05 15 <5.0> }      #33 {#1 {#4 f32 0.2}, #3 {#2 f32 5.0}}  == the RTF box's #19/#18 pair (shadow alpha / radius)
```

Varints such as `#60 = 18446744073709551556` are 64-bit two's complement: −60 (default style), −30 on the Futura
runs, −404 (= "not set") on the typed run. The sentinel **−404** (as f32 `0xc3ca0000` or varint `2^64 − 404`)
means "inherit from `#32.#5`" throughout `#21`.

### 4.3 The `bv41` run blob (`#32.#1.#2`)

`62 76 34 31 | 7a 00 00 00 | 72 00 00 00 | <LZ4 block> | 62 76 34 24` = `"bv41"`, u32 LE decompressed size (122),
u32 LE compressed size (114), block, `"bv4$"` — the same Apple-LZ4 framing as stroke geometry; `gnnote.applelz4.decompress`
handles it unchanged. The plaintext is a protobuf message:

```
#1 run  {                                   (repeated per run; every sample has one run)
   #1  LEN  UTF-8 text              "F R I E N D S"  /  "S U C H"  /  "GOOD"  /  "Hallo Franz…,!,,!,"   (the "…" is UTF-8 e2 80 a6)
   #2  style {
        #3  {#1 f32 R, #2 f32 G, #3 f32 B, #4 f32 A}   text colour  (0.2941 ×3, 1.0) / (0.7614, 0.9729, 0.6978, 1) / (0.1176, 0.1059, 0.1059, 1)
        [#30 LEN font family]              "Futura" on the three labels; absent on the typed run (→ default "Helvetica Neue")
        #40 f32 size                       55.0 on "GOOD"; −404 (inherit → #32.#5.#1.#40 = 24.0) on the others   (canvas units)
        #60 varint                         −30 on the Futura runs, −404 on the typed run (default −60)   unknown; see 4.5
        #70 f32                            −404 (default −20.0)                                              unknown
   }
   #3  paragraph {
        #1 {#1 ""}
        #2 {#1 1, #2 f32 30.0, #3 f32 −1.0, #4 1}     on the 30-high labels (line height 30 canvas); {#1 −1} on "GOOD" and the typed run
        #3 {#1 −1, #2 −1}
        [#4 varint 2]                      alignment: 2 = centred (three labels); absent = left (typed box)
   }
}
```

Decoded with `gnnote.protobuf.decode_message` directly (no heuristic byte scanning is needed; goodparse's flat
scan mis-reads the typed run's B component as 8.9e−38 because it keys on tag bytes).

### 4.4 Geometry — verified against `Test9.pdf` page 3 (`verify.py`)

Rule: text frame = `#20.#1` + insets `#32.#10`; width = box width − 2 × inset (`#21.#2.#1` for fixed boxes, else
content width `#32.#2.#1`); the box's `#20.#3` scale and `#20.#2` rotation are applied **about `#20.#1`** (the
origin), rotation as the standard matrix in the y-down canvas (`dir = (cos θ, sin θ)`). Font size = run `#40`
(or the default) in canvas units × k × scale. Alignment `#3.#4 = 2` centres each line in the frame.

| element | predicted (pt) | export (PyMuPDF spans, pt) |
|---|---|---|
| "Hallo Franz…" (left, no transform) | frame x0 = (493.751 + 10)·k = **274.77**, y0 = (78.959 + 10)·k = **48.52**, size 24·k = **13.09** | span bbox x0 = 274.77, y0 = 48.52, HelveticaNeue **13.09**, colour `#1e1b1b` = (30, 27, 27)/255 ✓ |
| "S U C H" (centred) | frame centre x = (132.206 + 221.173/2)·k = **132.43**, size 13.09 | glyph bbox centre x = **132.43**, Futura-Bold 13.09, `#4b4b4b` = 0.2941·255 = 75 ✓ |
| "GOOD" (centred, 55) | centre x = (127.126 + 231.166/2)·k = **132.39**, frame y0 = (117.839 + 10)·k = **69.73**, size 55·k = **30.00** | centre 132.39, bbox y0 = **69.73**, Futura-Bold **30.00**, `#c2f8b2` = (194, 248, 178) ✓ |
| "F R I E N D S" (rot −0.0042, scale 1.00574) | glyph centre, scale+rotate about origin: **(132.36, 125.78)**; about the box centre: (131.95, 125.96) | glyph bbox centre **(132.36, 125.79)**, line dir (0.99999, −0.00420), size 13.17 = 13.09 × 1.00574 ✓ |

Positions match to 0.01 pt; the pivot test singles out the origin. The glyph bbox top sits ≈ 0.65 pt above the frame
top for 13-pt Futura (ascender), 0.0 for "GOOD" — i.e. the frame y0 is the line box top, the first baseline is at
y0 + ascent. The transparent fill/border (`#30`/`#31`, alpha omitted) draws nothing in the export (no box outline).

### 4.5 Unknowns (low confidence)

`#9 = 1`; `#22.#1.#1 = 7.0` (corner radius of the box? invisible because the fill is transparent); style `#60`
(−60 default, −30 on runs that render as **Futura-Bold** although the family string is plain "Futura" — the value
may encode a weight/trait; the typed run with −404 renders regular) and `#70` (−20); `#32.#1.#3` 8 bytes; `#32.#1.#4 = 1`;
`#32.#11`. None of them changes position, size, colour or face in these samples. Bold must therefore be inferred:
the labels are GoodNotes "letter" stickers, exported in Futura-Bold; a reader that cannot resolve `#60` may map
family "Futura" + `#60 = −30` → bold, else regular (goodparse hard-codes bold for all `#21` text, which is wrong for
the typed box: it exports as HelveticaNeue regular, PyMuPDF flags = 4, not 20).

### 4.6 Reading recipe (`#21`)

1. Metadata not tombstoned; content top-level `#21`; require `#2 = 35`.
2. Origin `o = #20.#1`, `θ = #20.#2` (0 if absent), `s = #20.#3` (1 if absent). Insets `#32.#10` (default 10).
   Box size: `#21.#2 (w, h)` if present, else `#32.#2 + 2·inset` (typed, auto-sized boxes).
3. Decompress `#32.#1.#2` (Apple LZ4), decode as protobuf, iterate `#1` runs: text `#1`, colour `#2.#3`
   (default: `#32.#5.#1.#3` → here only alpha 1.0 → treat as black), family `#2.#30` (default `#32.#5.#1.#30`),
   size `#2.#40` (default `#32.#5.#1.#40`; treat ≤ 0 as "inherit"), alignment `#3.#4` (2 = centre; 1/3 presumably left/right per
   parser-for-goodnotes, unverified).
4. Emit a `TextBox` at `(o + inset) · k`, size `(w − 2·inset, h − 2·inset) · k · s`, font size `size · k · s`; if `θ ≠ 0`,
   rotate about `o·k` (the model has no text rotation: record it as a warning, or bake the frame origin as the rotated
   top-left corner; the four samples are within 0.25° so either is invisible).
5. Grouping (`#7`/`#8`) and `#9`, `#22`, `#30`, `#31`, `#33` can be ignored for conversion.

---

## 5. Images: die-cut sticker PDFs, `#6`/`#7`, EXIF-rotated photos (Test9)

### 5.1 Record layout additions

Both Test9 images use the §2 layout of the elements doc (`#1 E, #2 rect, #3 crop rect, #4 A, #5 {#1 clock}, #15 clock, #18 24`)
plus:

```
Test9 p3 rec 9 (sticker)  @2431  30 03                 #6 = 3
                          @2433  3a 24 "F3D9B628-…"    #7 = group UUID (shared with the three #21 labels; metadata #4 = attachment as before)
Test9 p4 rec 11 (photo)   @4220  30 01                 #6 = 1       (no #7: not grouped)
```

`#6` values seen so far: absent on the four PNGs (Test5, ex1), **1 on every JPEG photo** (ex3 ×2, Test9), **3 on the
sticker PDF**. Reading: `#6` is an attachment-kind enum (1 photo/JPEG, 3 vector sticker); do not rely on it to sniff
the bytes — sniff the attachment. Medium confidence on the enum meaning, high that the values exist.

### 5.2 Die-cut sticker = a one-page vector PDF attachment

* Attachment `786977EC-14A7-40D5-A7B7-3F0172C3D8E6`, 4 113 B, `%PDF`, `/Producer (iOS Version 14.1 (Build 18A8395) Quartz PDFContext)`,
  one `/Page` with `/MediaBox [0 0 254 214]` (the `/Pages` node carries a stray `/MediaBox [0 0 612 792]` — read the page,
  not the tree), content = three filled paths: a rounded rectangle (0.7725, 0.9647, 0.7255) covering the whole page, an
  inset rounded rectangle and a middle band in grey (0.2941), ICC `/Cs1` colour space, no images, no text. The letters
  are *not* in the PDF: they are the three `#21` elements (§4), grouped with the image via `#7`/`#8` = `F3D9B628…`.
* Event `#6` for it: `{#1 A, #2 A, #5 4113, #6 D, #10 ts, #11 uuid, #12 "" (EMPTY), #14, #15, #16 24}` — i.e. the same
  `#12 = ""` as rasters, not the `{#1 1, #2 1}` of paper PDFs (§8). Also a `#105` event (`#3 = A`) that the other
  attachments get too (event bookkeeping, outside this doc).
* Placement `#2` = `(115.647, 57.847) + (254.000, 214.000)` canvas, `#3` = centre `(242.647, 164.847)` + same size
  (uncropped). The size equals the MediaBox **in canvas units**, i.e. the sticker is placed at 1 PDF pt = 1 canvas unit
  = 6/11 pt on the page (138.55 × 116.73 pt). Export check: the outermost fill of the sticker in `Test9.pdf` p3 (drawn
  inside form XObjects `Fm3/Fm4`) has rect **(63.08, 31.55)–(201.63, 148.28)** pt; predicted `#2 × k` =
  (63.08, 31.55)–(201.63, 148.28). Exact.
* Reading recipe: when `attachments/<A>` starts with `%PDF`, treat the image as a PDF page placed in `#2`: either
  rasterise page 1 at the MediaBox aspect (PyMuPDF/`pdfutil` are not available in the core package — `gnnote.pdfutil`
  can parse the MediaBox, rendering needs an external rasteriser) or carry the PDF through as a vector image if the
  target supports it. The rounded corners are transparent (the content never paints outside the rounded rectangle).
  Our reader currently warns "neither PNG nor JPEG; image skipped" (Test9 p3) — it should at least place a PDF image.
  The `Image.fmt` of the model only knows png/jpeg; a "pdf" value (or a rasterised PNG) is needed.

### 5.3 Photo with EXIF orientation (Test9 p4 rec 10/11)

* Attachment `7AEDBFA1…`: 2 063 118 B JPEG, SOF0 **3840 × 2160** (landscape), **EXIF Orientation = 6** (rotate 90° CW
  to display), stored byte-for-byte (event `#6.#5 = 2063118`, `#12 = ""`).
* Placement `#2` = `(516.006, 91.923) + (360.0, 640.0)` canvas = portrait 9:16: the **displayed (post-EXIF) size**.
  `#3` = centre `(696.006, 411.923)` + same size (uncropped), no `#3.#3` rotation.
* Export (`Test9.pdf` p4): the photo is a `/Stamp` annotation (xref 170) with `/Rect [281.4577 442.6591 477.8213 791.7501]`
  (y-up) = (281.46, 50.14)–(477.82, 399.23) y-down; predicted `#2 × k` = (281.46, 50.14) + (196.36, 349.09) → x1 477.82,
  y1 399.23. Exact. Its appearance form (`/BBox [0 0 196.3636 349.0909]`) draws the **unmodified 3840×2160 JPEG**
  (`/Im4`, `/DCTDecode`, `/Length 2063118`) through `0.0000264 -349.0909 196.3636 0.0000148 -0.0000132 349.0909 cm`:
  image top-left → form top-right, i.e. a **90° clockwise** rotation = exactly what EXIF 6 prescribes.
* Rule for a reader: decode the raster, apply its EXIF orientation (6 → 90° CW, 8 → 90° CCW, 3 → 180°, mirrored
  variants accordingly), then stretch the result into `#2`. Do **not** guess from aspect ratios (goodparse's
  `_raster_to_jpeg` rotates CCW when the aspects are swapped — it happens to look right here only because the photo is
  symmetric enough; the EXIF tag is authoritative and also distinguishes 6 from 8). Our reader passes the raw JPEG
  through with `rotation = 0`, so a Notability writer that ignores EXIF shows the photo squashed landscape-in-portrait;
  either rotate the pixels (needs a JPEG decoder, not in the package) or emit `Image.rotation = 90` with the JPEG's
  native box swapped (w↔h) about the same centre — `#3` gives the centre directly.
* Confidence: high for all of the above (one sample, but every number is exact).

### 5.4 Side note on how the export is built (affects stroke verification, not elements)

In `Test9.pdf` the ink of pages 2 and 4 is written as `/Ink` annotations (`/Border [0 0 W/2]`, InkList of
sampled points, appearance streams with the Béziers) and pencil as `/Stamp` annotations; `page.get_drawings()`
therefore sees only the paper on those pages. Pages 3 and 5–7 and all of Test6–8 use ordinary content streams.

---

## 6. Grouping fields seen on non-stroke elements

* `#21.#7 {#1 {#2 nonce, #3 index}}` + `#21.#8 group UUID`, and image `#7 group UUID`: the sticker PDF and its three
  labels form one group (`F3D9B628…`; member indices 1, 2, 3 on the labels; the image carries the same nonce `1065099080`
  in its `#5.#1.#2`). This is the same mechanism as `#7.#10` + `#7.#7.#1.#3` on strokes (elements doc §8.2) with
  different field numbers per kind. No effect on geometry; ignore when converting.
* Test9 strokes additionally show `#7.#18 = 1` (16 records) and `#7.#20 {#1 ""}` (3) — stroke-level, outside this doc.

---

## 7. `search/` members and `document.info.pb` (new container members, informational)

Test7 and Test9 contain `index.search.pb` (`{#1 X, #2 "search/" + X}` per entry) and `search/<X>` members, where `X`
is a notes-layer UUID **or** an attachment UUID. `search/<N>` for Test9 p3 = `{#3 "S U C H", #3 "F R I E N D S", #3 "GOOD",
#3 "Hallo Franz…,!,,!,", #4 {#2 {#1 element index, #2 1, #3 rect}}}`: the page's text strings plus per-word rectangles
in box-local canvas units (e.g. (62.81, 10) + (15.50, 30.72) for "S" inside the 221-wide box) — a derived search index.
`search/<sticker attachment>` = `{#2 1, #3 ""}`. `document.info.pb` is 0 bytes in Test9. None of this is needed to
read a page and none is authoritative; a writer may omit all of it (Test6/Test8 have no `search/` members).

---

## 8. Container facts that decide whether elements land on the right page

* **Page UUID = notes UUID − 1 with carry.** `N` and `P` differ in the last hex digit *as an integer*; when that digit
  is `0` the borrow propagates: Test6 p5 `N = A37FF8B6-…-6ECDEDC920` ↔ `P = …EDC91F`, Test7 p4 `…C820A0` ↔ `…C8209F`,
  Test9 p2 `D298AC1F-CFFB-42D1-A4AE-D07D3281E450` ↔ `…E44F`. `reader._uuid_key` compares the first 35 characters and
  therefore misses these three pages ("no template event; page size and paper are guessed"), which is why Test9's
  123-stroke grid page is reported as a 455×588 blank page at the end. Fix: `P = format((int(N.replace('-',''), 16) − 1)
  mod 2¹²⁸, '032X')` re-hyphenated (as the sibling `scratchpad/v35/resolve.py` does; it reproduces all 20 export page sizes).
* Event `#6.#12`: `{#1 1, #2 1}` on the five paper-backing PDFs of Test9 (03D3A8D4, B0212B82, 6F089296, F5503752 Figma,
  80973E20 form) but `""` on the JPEG, on the sticker PDF **and on the photo-PDF 4043E92A that backs page 7**. So `#12`
  is neither "is a PDF" nor "is used as paper"; the earlier docs' "`{1,1}` for PDFs" is only a correlation. Low
  confidence on its meaning; a writer should keep emitting what the matching sample does.
* Paper alias: page 6's template names paper `80973E20`, whose bytes live in `attachments/4452790C` (event `#6` with
  `#1 ≠ #2`); `index.attachments.pb` maps it, so `reader._index_pairs` already resolves it (Test9 p5 reads as 595.2 × 841.68).

---

## 9. Corrections to the existing docs

* `goodnotes-elements.md` §1 kind table: add `#9` = shape fill (§1 here) and `#21` = Type-35 text (§4); `#20`/`#22` remain unseen.
* §2.4 "No other raster type in the corpus": attachments referenced by image elements can be **PDF** (vector sticker).
* §2.3: image rotation does occur, but via the raster's **EXIF orientation**, not `#3.#3` (still never seen non-zero).
* §4.3 "LZ4 Type 35 rich-text form — not in the corpus": now verified; field numbers as parser-for-goodnotes describes
  (`#20` transform, `#21` size, `#30`/`#31` fill/border, `#32 {#1 {#2 blob}, #2 size, #5 default style, #10 insets}`), plus
  `#2 = 35`, `#3` clock, `#7`/`#8` group, `#9`, `#22`, `#33`, and the rotation/scale in `#20.#2/#3`. Typed text in schema 35
  uses it too (no RTF).
* §5.1: `#9.#2` is a quadratic Bézier (control point in `#2.#2`), not a polyline; `#9.#1` closed polygons repeat the first
  point; the shape width is W/2 pt (critic rule confirmed on the Test6/7/8 shape exports: 10.3937 → 5.1968, 5.4567 → 2.7283, 24.0 → 12.0).
* §5.3 "Type 31/35 shapes (`#22`/`#21`)": `#21` is the text element; its `#2 = 35`; "Type 31" (`#22`) is still unseen.
* §10 "parser-for-goodnotes keys on `#16 == 35`": correct for schema-35 files — the metadata of `#21` elements does carry
  `#16 = 35`.
* `goodnotes-container.md` page binding: "N = P with last hex digit + 1" must read "N = P + 1 as a 128-bit integer".
* goodparse facts worth not copying: its `#21` colour scan can mis-read a component (typed box B = 8.9e−38); it renders
  all `#21` text bold; it rotates photos by an aspect heuristic instead of EXIF; it treats `#9.#2` as a polyline.

---

## 10. Reader checklist (ordered)

1. Page binding: carry-aware `P = N − 1` (§8) — restores paper, size and the 123 strokes of Test9 p2, and the last pages of Test6/7.
2. `#9` records → filled outline stroke of the parent's geometry, colour `#7` (alpha 0.1), skip tombstones (§1.5).
3. `#21` records → `TextBox` per §4.6 (`#8` RTF handling stays as is).
4. `#1` images: accept `%PDF` attachments (place as PDF/rasterised image, §5.2); apply EXIF orientation before fitting
   into `#2`, or emit `rotation` with swapped box (§5.3); keep reading `#4` from content or metadata.
5. `#7.#9.#2` → quadratic Bézier (convert to the cubic chain exactly as `_flat` does), not a polyline (§3).
6. Keep: `#3 = 1` tombstone skipping, `_as_metadata` (it already accepts `#16 = 35`), text frame rule for `#8`.
