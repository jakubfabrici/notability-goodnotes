# `.goodnotes` page content other than ink: images, PDF backgrounds / paper templates, text boxes, shapes, sticky notes, eraser cuts, lasso moves

Companion to `goodnotes-container.md` (container, event log, stroke records). This document specifies the
**non-stroke** page elements at byte level, prioritised for a converter that must READ them (carry images, PDF
backgrounds and text across to Notability) and WRITE the two things a generated file needs: a PDF background
bound to a page, and an image placed on a page.

Status: reverse-engineered from the sample bytes with two schema-free protobuf decoders
(`goodnotes_re.wire`, `goodparse.protobuf`); cross-checked against **GoodNotes' own renders** of the same
documents (`Test5.pdf`, `Test4.pdf` = GoodNotes PDF exports; `ex1.jpg`, `ex3.jpg` = GoodNotes rasters at exactly
2 px per canvas unit). Nothing comes from GoodNotes documentation. Every claim carries an evidence pointer and a
confidence level. Where parser-for-goodnotes documents a structure that is **absent from the corpus** (sticky
notes, "Type 31/35" shapes, LZ4 text payloads, image crop/rotation) it is reported as such, with low confidence.

Evidence base (paths relative to `scratchpad/ref/`):

| file | pages | what it contributes here |
|---|---|---|
| `goodparse/samples/Test5.goodnotes` + `Test5.pdf` (GoodNotes 6 export, 2026-09-21) | 3 | page 3 (`notes/27B0C0FB-…EC7B`): 1 PNG image (rec 24/25), 2 RTF text boxes (rec 33, 37), 1 auto-shape ellipse (rec 11), 3 lasso-moved groups; page 2: a stroke split in two sub-paths (rec 1) |
| `goodparse/samples/Test4.goodnotes` + `Test4.pdf` | 2 | same document before the edits; built-in "Blue" and "Yellow" templates |
| `goodparse/samples/test*.goodnotes` | 1–2 | A4 built-in templates ("Baby Blue", "White") |
| `parser-for-goodnotes/assets/ex1.goodnotes` + `ex1.jpg` | 1 | 3 PNG images (rec 1352/1353, 1762/1763, 3068/3069), 1 auto-shape line (rec 1811), 12 lasso offsets, `#15` events |
| `parser-for-goodnotes/assets/ex3.goodnotes` + `ex3.jpg` | 1 | 2 JPEG images (rec 1704/1705, 4018/4019), lasso offsets |
| `parser-for-goodnotes/assets/ex2.goodnotes` | 1 | auto-shape polyline (rec 15), legacy-format strokes |

Not in the corpus (so unverified): a user-imported PDF, a multi-page PDF attachment, a cropped or rotated image,
a sticky note, a "Type 31/35" shape or text record, an image tombstone. All record indices below are 0-based
indices into the length-prefixed record stream of the named `notes/` member (as printed by
`scratchpad/work/dump.py <file> <member> --records`).

Notation as in the container doc: `#n` = protobuf field n; `f32` = fixed32 little-endian float; `LEN` =
length-delimited; `clock` = `{#1 varint version, #2 varint nonce}`; `rect` = `{#1 {#1 f32 x, #2 f32 y}, #2 {#1 f32 w, #2 f32 h}}`
(10-byte point messages: `0d xxxxxxxx 15 yyyyyyyy`).

---

## 0. Coordinate system (verified against GoodNotes' own export)

All element geometry (image rects, text-box rects, shape centres, stroke points) is in **canvas units**,
origin top-left, y down, `canvas = PDF points × 132/72`. Verified for non-stroke content:

* Text box, Test5 page 3 rec 33: inner frame origin `(499.855, 206.560)` canvas → ×72/132 = `(272.65, 112.67)` pt;
  `Test5.pdf` page 3 places the glyph run "Hallo" at bbox x0 = 272.65, y0 = 112.66 pt. Exact match.
* RTF font size `\fs48` (= 24 "points") renders at 13.09 pt in the export = 24 × 72/132. So **font sizes in the RTF
  are canvas units too**.
* Image, Test5 page 3 rec 25: rect `(205.53, 432.55) + (209.35, 279.13)` canvas; the rendered page cropped at
  that rect (×72/132) correlates 0.96 with the PNG attachment (control offset: 0.1–0.3). Same test on ex1/ex3
  against GoodNotes' JPEG rasters: 0.69–0.91 vs 0.05–0.29 for a +60-unit control shift.

Confidence: high.

---

## 1. Element records recap and the kind switch

A `notes/<N>` member is a stream of (metadata record, content record) pairs, one pair per element, in z-order
(5 677 elements in the corpus, no exception). The **metadata record** is the same for every kind:

```
{ #1 LEN element UUID E, #2 clock, [#3 varint 1 = tombstone], [#4 LEN attachment UUID: images only],
  [#6 LEN uuid: ex2 only, unknown], #8 varint device id, #9 varint element counter, #14 varint 5381, #16 varint 24 }
```

The **content record** has exactly one top-level field and **its number is the element kind**:

| top-level field | kind | occurrences in corpus | spec |
|---|---|---|---|
| `#7` (tag `3a`) | ink stroke (incl. auto-shapes, which are strokes with a `#9` geometry sub-message) | 5 665 | container doc §11.3, shapes §5 below |
| `#1` (tag `0a`) | image placement | 6 | §2 |
| `#8` (tag `42`) | text box, RTF payload | 2 | §4 |
| `#20`, `#21`, `#22` | sticky note / "Type 35" text or shape / "Type 31" shape (parser-for-goodnotes) | 0 | §5.3, §6 — unverified |

Tombstones: metadata `#3 = 1` (+ content `#14 = 1` on strokes). parser-for-goodnotes also skips images whose
metadata has `#3 = 1` ("moved to another page"); no image tombstone exists in the corpus. A reader must skip any
element whose metadata carries `#3 = 1`, regardless of kind.

---

## 2. Embedded images

### 2.1 Record layout (6/6 instances identical in shape)

Metadata record (Test5 page 3 rec 24, 106 bytes + 1 length byte; byte offsets are absolute in the member):

```
@25217  #1  LEN 36  "82D245D7-4D07-4DB2-BA3D-DB1CCCADD750"      element UUID E
@25255  #2  LEN 8   {#1 varint 6, #2 varint 3624172184}          clock (version 6 = edited 6 times)
@25265  #4  LEN 36  "8F5D3EF0-5FB0-459F-9ABE-9AF8B356073B"      attachment UUID A  (only images have #4 here)
@25303  #8  varint 1606161793633522096                            device id
@25313  #9  varint 141493                                         element counter
@25317  #14 varint 5381
@25320  #16 varint 24
```

Content record (rec 25, 155 bytes): one top-level `#1` wrapping

```
#1  LEN 36  E                                   (== metadata #1)
#2  rect    {#1 {#1 f32 205.5316, #2 f32 432.5455}, #2 {#1 f32 209.3464, #2 f32 279.1284}}
            = placement: top-left x,y and width,height in canvas units      (bytes: 12 18 0a 0a 0d.. 15.. 12 0a 0d.. 15..)
#3  rect    {#1 {#1 f32 310.2047, #2 f32 572.1097}, #2 {#1 f32 209.3463, #2 f32 279.1284}}
            = visible (crop) rectangle given as CENTRE + size; see 2.3
#4  LEN 36  A                                   (== metadata #4)
#5  LEN     {#1 {#1 varint 10, #2 varint 251044997}}             a second clock (version = number of move/resize edits:
                                                                   10, 6, 1183, 901, 1875, 1874 seen; ex1 rec 1353 has {#1 {#2 nonce}} only)
[#6 varint 1]                                   ex3 only (both JPEG photos); absent on the four PNGs. Meaning unknown.
#15 LEN     {#1 varint 6, #2 varint 3624172184}  == metadata #2 (6/6)
#18 varint 24                                   schema
```

Field order is as listed; there is no colour, opacity, z-index, rotation or flip field in any sample. Evidence:
Test5 p3 rec 24–25; ex1 rec 1352–53, 1762–63, 3068–69; ex3 rec 1704–05, 4018–19. Confidence: high.

### 2.2 Placement (`#2`) — verified

`#2.#1` = top-left corner, `#2.#2` = size, canvas units. The size always has the raster's aspect ratio
(6/6: 0.7500/0.7500, 9.7062/9.7062, 10.2915/10.2915, 5.1587/5.1587, 1.3188/1.3188, 1.3313/1.3313), i.e. GoodNotes
scales uniformly; the raster is simply stretched into this rect. Verified against the GoodNotes renders (§0).
A converter should map the image to Notability as `rect × 72/132` pt, then into Notability's page coordinates.

### 2.3 Crop rectangle (`#3`) — partially verified

`#3.#1` = centre, `#3.#2` = size of the visible region. In all six samples `#3.#1 == #2.#1 + #2.#2/2` (to f32
precision) and `#3.#2 == #2.#2`, i.e. **uncropped**. parser-for-goodnotes (`element.py:parse_image_elements`,
wiki 05 §5) reads an optional `#3.#3 f32 rotation (radians)` and renders the crop as the intersection of the
crop rect with the image when `#3.#2 != #2.#2`; neither case exists in this corpus (**low confidence** for
cropped/rotated images; a reader should at least handle `#3.#2 != #2.#2` by clipping, and treat `#3.#3` as
rotation about the crop centre). A writer emits `#3` = centre + same size (uncropped).

### 2.4 Attachment and events

* The raster is `attachments/<A>` byte-for-byte: PNG (`89 50 4e 47`: 1536×2048 RGBA, 2048×211, 2048×199, 2048×397
  RGBA) or JPEG (`ff d8 ff e0` JFIF: 1696×1286, 1704×1280). No other raster type in the corpus. Pixel density is
  not stored anywhere (3.5–11.6 px per canvas unit seen); size comes only from `#2`.
* `index.attachments.pb` lists it like any attachment (`{#1 A, #2 "attachments/"+A}`).
* Event `#6` declares it: `{#1 A, #2 A, #5 varint byte size, #6 D, #10 ts, #11 uuid, #12 LEN "" (EMPTY for
  rasters; `{#1 1,#2 1}` for PDFs), #14 dev, #15 seq, #16 24}` — Test5 events rec 15, ex1 rec 4–6, ex3 rec 4–5.
* Event `#15` `{#1 D, #2 LEN uuid, #3 varint 2, #4 fixed64 1.0, 10/11/13/14/15}` appears in ex1 (16×) and ex3
  (3×) **only**; its `#2` is the image attachment, the template PDF attachment, *or* a UUID that is neither an
  attachment, an element nor a page in the file (12 of 16 in ex1). Test5 has images and no `#15`. It is
  therefore optional bookkeeping (plausibly "attachment uploaded/synced"); a writer does not emit it. Confidence
  that it is optional: high (Test5 written by GoodNotes 6 without it).
* There is no page-level reference to the image; the only binding is metadata `#4` / content `#4` → attachment.

### 2.5 WRITE recipe — place a raster on a page

Validated in `scratchpad/work/build_elems.py` (both reference parsers read the image back at the written
rect; parser-for-goodnotes `ImageElement(x=100, y=200, w=500, h=96.9)`, goodparse identical):

1. `attachments/<A>` = the PNG/JPEG bytes; add `{#1 A, #2 "attachments/"+A}` to `index.attachments.pb`;
   add event `#6` with `#5` = exact byte size and `#12 = ""` (tag `62 00`).
2. In `notes/<N>` append the pair
   `meta = {#1 E, #2 ck, #4 A, #8 dev, #9 n, #14 5381, #16 24}` and
   `content = {#1 {#1 E, #2 rect(x,y,w,h), #3 rect(x+w/2, y+h/2, w, h), #4 A, #5 {#1 {#1 1, #2 nonce}}, #15 ck, #18 24}}`
   with `ck = {#1 1, #2 nonce}` identical in both records, `w/h` = raster aspect, all in canvas units.
3. Emit (or keep) the page's `#102` event.

Tag bytes of the content record: `0a <len>` | `0a 24 <E>` | `12 18 0a 0a 0d.. 15.. 12 0a 0d.. 15..` | `1a 18 …` | `22 24 <A>` | `2a 09 0a 07 08 01 10 <nonce>` | `7a 08 08 01 10 <nonce>` | `90 01 18`.

---

## 3. PDF page backgrounds and paper templates

### 3.1 How a page points to its background (verified, 14/14 pages)

```
index.notes.pb  N ──(N = P with last hex digit + 1)──▶ event #54 (entity P): #3.#1 = template UUID T
event #2 (entity T): #4 = attachment UUID A,  #5 = PDF page index (1-based),  #8 = canvas size (f32 W, f32 H)
index.attachments.pb: A → attachments/A  (a PDF; the page size is its /MediaBox)
```

Template event body (`#2`, tag `12`), all fields with the observed values (Test4/Test5 event rec 2 and 4, test* rec 2, ex* rec 2):

```
#1  LEN D (document)      #2 LEN T          #4 LEN A
#5  varint 1              -- PDF page index inside attachment A (1-based). Only 1 observed; parser-for-goodnotes
                             archive.py:360-377 reads it as the page index and its tests cover values > 1. medium.
#6  varint 1              -- always 1 (unknown)
#7  fixed64 29.333333     -- OPTIONAL, lined papers only: line pitch in canvas units (29.33 = 16 pt × 11/6; the PDF draws lines every 16 pt)
#8  {#1 f32 W, #2 f32 H}  -- CANVAS SIZE = MediaBox × 11/6  (834.24×1078.82 for 455.04×588.45; 1091.35×1543.46 for A4)
#9  LEN name              -- "<builtin UUID>_<size>_<n>_<m> - <Colour>", see 3.3
#10 ts, #11 uuid
#12 {[#1 fixed64 29.333,] #2 clock}     -- lined: {#1 29.333 #2 clock}; blank: {#2 clock}
#13 {[#1 varint 1,]       #2 clock}     -- lined: {#1 1 #2 clock};    blank: {#2 clock}
#15 dev, #16 seq
#17 {#1 varint 1, #2 clock}
#18 OPTIONAL {#1 {#1 {#1 f32 44.0, #2 f32 58.667}, #2 {#1 f32 746.24, #2 f32 961.49}}, #2 f32 28.4167, #3 f32 0.91667, #5 varint 1}
     -- writable area (origin 44,58.67; size = canvas − 88, canvas − 117.33) + two constants; present on ruled
        AND on the 41 kB "standard_1_2 - White" paper, absent on the 950-byte blank "Blue"/"Baby Blue" papers
#19 {#2 clock}
#21 varint 24
```

The page event `#54` binds `P → T` through `#3 {#1 T, #2 clock}`; several pages may share one `T` (Test5 pages 2
and 3 both use the "Yellow" template `2291FFED-…`). Changing a page's paper later would rewrite `#54.#3` (not
observed).

### 3.2 What a built-in paper template is, as an attachment (verified on all 9 template PDFs)

Every template attachment in the corpus is a one-page PDF produced by GoodNotes itself:

```
%PDF-1.7  /Producer (svg2pdf)  no /Creator, no /Title, no /CreationDate
1 0 obj /Catalog, 2 0 obj /Pages /Count 1, 3 0 obj /Page /MediaBox [0 0 W H] /Contents 4 0 R
   /Resources << /ColorSpace << /srgb [/CalRGB << /WhitePoint [0.9505 1 1.0888] /Gamma [2.2 2.2 2.2] … >>] >> /XObject … >>
4 0 obj  FlateDecode content stream: "q /srgb cs /srgb CS r g b scn 0 H m W H l W 0 l 0 0 l h f Q"  (paper colour fill)
         then "q /xo0 Do Q" [q /xo1 Do Q]  for ruled/grid papers
5.. obj  /XObject /Subtype /Form /BBox [0 0 W H] /Group << /S /Transparency >>: the ruling, one filled 0.5-pt-high
         rectangle per line (e.g. y = 572.95…572.45, pitch 16 pt, colour 0.8157 0.8235 0.8275), vertical lines in a second form
```

Sizes seen: 950 B (blank "Blue": fill 0.3176 0.8431 0.9608 plus a 22.75-pt-wide vertical band), 945 B (blank
"Baby Blue" A4), 3 224 / 3 492 B (ruled "Yellow"/"White": 37 horizontal + 29 vertical rules, 53 + 38 on A4 — a grid), 41 791 / 41 951 B ("standard_1_2 -
White": 40 kB of small circles = dotted grid). MediaBoxes: `[0 0 455.04 588.45]` ("standard": 6.32 × 8.17 in,
the GoodNotes default) and `[0 0 595.28 841.89]` (A4). Test5.pdf page 1 (export of the "Blue" page) consists of
exactly the two fills of the template, confirming the attachment *is* the rendered paper.

### 3.3 Template names (`#2.#9`) and telling a user-imported PDF from a built-in paper

Names observed (5 distinct): `417A3734-B870-5E89-8055-DA029834F25E_standard_1_1 - Blue`,
`9FE8F365-4BEE-5057-8573-1A56C77CAC19_standard_1_1 - Yellow`, `9FE8F365-4BEE-5057-8573-1A56C77CAC19_a4_1_2 - White`,
`BF443295-F2FD-55E0-975E-3E718F509B91_a4_1_2 - Baby Blue`, `CC365888-75C9-5DBF-B1D4-A7E8E0AD3B7F_standard_1_2 - White`.
The leading UUID is GoodNotes' catalogue id of the paper design (same id `9FE8F365…` for ruled Yellow and ruled
White, so it identifies the ruling, not the colour); `standard`/`a4` is the size; the two digits are unknown
(`1_1` for the two blank/ruled "standard" papers, `1_2` for the others — perhaps orientation/version).

No user-imported PDF exists in the corpus, so the following is **inference (medium confidence)**. A reader can
classify a page background by, in order:
1. `#2.#9` matches `^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}_(standard|a4|[a-z0-9]+)_\d+_\d+ - .+$` **and** the
   attachment is a 1-page PDF with `/Producer (svg2pdf)` and no `/Creator` → built-in paper. Carry it over as a
   Notability paper style (colour from the content-stream fill, ruled if `#7`/`#18` present) rather than as a PDF.
2. Otherwise (other producer, more than one page, `#5 > 1`, a non-template name) → user PDF; copy the PDF page
   into Notability as the page's background PDF.
Reading the paper colour: first `scn` triple of the page content stream (e.g. `0.972549 0.96862745 0.9137255` =
Yellow). Ruled papers: `#2.#7` = 29.333 canvas units (16 pt) line pitch.

### 3.4 Multi-page PDFs

Expected (per parser-for-goodnotes `archive.py:349-441` and wiki 09 §3): one attachment holds the whole PDF,
every page gets its own `#2` template event with the same `#4 = A` and `#5` = 1, 2, 3…; `#8` = that page's
MediaBox × 11/6; `#6` event `#12 = {#1 1, #2 1}` as for templates (whether `#12` encodes the page count is
unknown — it is `{1,1}` for every one-page PDF, which is consistent with either reading). **Unverified on real
GoodNotes output.** Both reference parsers handle `#5 > 1` (parser-for-goodnotes reports
`pdf_page_index`; goodparse ignores `#5` and uses the whole attachment).

### 3.5 WRITE recipe — attach a PDF background to a page

Validated in `scratchpad/work/build_elems.py`: a 3-page file whose page 1 uses the built-in blank paper, page 2
is bound to **page 2 of a 2-page PDF** (`#5 = 2`), page 3 to a 1-page PDF; parser-for-goodnotes reads back
`PageDimensions(595.28, 841.89)`, `background_attachment_path` = the right attachment and `pdf_page_index = 2`
for page 2. (goodparse reads only a single document-wide page size and therefore mis-sizes mixed documents —
a limitation of that parser, not of the file.)

Per distinct (PDF, page) pair:
1. `attachments/<A>` = the PDF bytes; `index.attachments.pb` entry; event `#6` with `#5` = byte size, `#12 = {#1 1, #2 1}` (bytes `62 04 08 01 10 01`).
2. Event `#2` as in 3.1 with `#4 = A`, `#5 = page index`, `#6 = 1`, `#8 = (MediaBox W × 11/6, H × 11/6)` as f32,
   `#9` = any string (use the PDF filename; keep it free of the built-in pattern so GoodNotes does not take it for a
   catalogue paper), `#12 = {#2 clock}`, `#13 = {#2 clock}`, `#17 = {#1 1, #2 clock}`, `#19 = {#2 clock}`, `#21 = 24`;
   no `#7`, no `#18`.
3. Event `#54` for the page with `#3 = {#1 T, #2 clock}`.
4. Safest variant for the iPad test (every observed template is a one-page PDF): split the user PDF into
   one-page PDFs, one attachment per page, `#5 = 1` everywhere. Try the shared multi-page attachment second.
Rotation: the page is rendered at the MediaBox size; a `/Rotate` entry was never observed and should be baked
into the PDF page before attaching (confidence that GoodNotes honours `/Rotate`: unknown).

---

## 4. Text boxes

### 4.1 RTF form — top-level `#8` (Test5 page 3 rec 33 and 37; GoodNotes 6, 2026-09)

Metadata record (rec 32, 66 bytes): `{#1 E, #2 {#2 nonce} (no version field!), #8 dev, #9 n, #14 5381, #16 24}`.
Content record (rec 33, 557 bytes), top-level `#8` wrapping, in this order:

```
#1  LEN 36  E
#2  rect    {#1 {489.8554, 196.5603}, #2 {74.99997, 49.0}}    -- OUTER box: origin + size, canvas units
#3  rect    {#1 {499.8554, 206.5603}, #2 {55.0, 29.0}}        -- TEXT FRAME = outer inset by #10 on all four sides (verified: glyphs start exactly here)
#4  LEN     {#1 f32 1.0, #4 f32 1.0}                           -- unknown; NOT the text colour (text is black in the export; RTF carries the colour). Copy verbatim.
#5  LEN     {#1 {#1 varint 11, #2 varint 83236510}}            -- clock (version = edit count)
#6  LEN     RTF bytes (361 / 506 B), see 4.2
#7  LEN     {#1 f32 1.0, #2 f32 1.0, #3 f32 1.0}               -- RGB white, alpha omitted (= 0): box fill colour, transparent  (nothing is drawn around the text in Test5.pdf)
#9  LEN     {#1 f32 1.0, #2 f32 1.0, #3 f32 1.0}               -- RGB white, alpha 0: border colour, transparent  (inferred from GoodNotes' text-box UI: fill / border / padding)
#10 f32 10.0                                                   -- padding (== #2 → #3 inset)
#15 LEN     {#2 varint 2288351305}                             -- == metadata #2
#18 LEN     {#2 f32 5.0}                                       -- unknown (border width? corner radius?)
#19 LEN     {#4 f32 0.2}                                       -- unknown (an alpha 0.2 — shadow?)
#20 LEN     ".tb-0"                                            -- style/preset id string
#21 LEN     ""                                                 
#27 varint  24                                                 -- schema
```

Byte image of the first 60 bytes of rec 33: `42 aa 04 | 0a 24 <E> | 12 18 0a 0a 0d 7d ed f4 43 15 6f 8f 44 43 12 0a 0d fc ff 95 42 15 00 00 44 42 | 1a 18 …`.
rec 37 is identical in shape (box `(540.54, 288.77) + (285.80, 78.0)`, frame inset 10, 3 fonts in the RTF).
Both boxes: `#2.#2.y` = 49 / 78 = 29 / 58 + 20 → the box height grows with the text; width 75 / 285.8 (user-set).
Confidence: high for `#1,#2,#3,#6,#10,#15,#27`; medium for the semantics of `#7/#9`; low for `#4,#18,#19`.

### 4.2 The RTF payload (`#8.#6`)

Cocoa RTF exactly as `NSAttributedString` writes it (rec 33, 361 bytes):

```
{\rtf1\ansi\ansicpg1252\cocoartf2709
\cocoatextscaling1\cocoaplatform1{\fonttbl\f0\fnil\fcharset0 HelveticaNeue;}
{\colortbl;\red255\green255\blue255;\red0\green0\blue0;}
{\*\expandedcolortbl;;\cssrgb\c0\c0\c0;}
\pard\tx560\tx1120\tx1680\tx2240\tx2800\tx3360\tx3920\tx4480\tx5040\tx5600\tx6160\tx6720\sl-559\pardirnatural\partightenfactor0

\f0\fs48 \cf2 Hallo}
```

rec 37 adds `\f1 … HelveticaNeue-Bold; \f2 … HelveticaNeue-Italic;` to the font table and the body
`\f0\fs48 \cf2 Test\n\f1\b 123\n\f2\i\b0 italic\n\f0\i0 \ul underline \ulnone \strike \strikec2 Strike }` —
GoodNotes exported it as HelveticaNeue 13.09 pt regular / Bold (flags 20) / Italic (flags 6), i.e. the
RTF is authoritative for runs, fonts and styles. Facts a converter needs:

* Font size `\fsN` is N half-points **in canvas units**: `\fs48` → 24 canvas units → 24 × 72/132 = 13.09 pt on the page.
* Colour: `\cfK` indexes `\colortbl` (index 0 = default/black, 1 = white, 2 = black here); `\colortbl` entries are
  0–255 RGB. Both samples are black; the colour-table shape is NSAttributedString's default.
* `\sl-559` = exact line spacing 27.95 units (559/20), i.e. GoodNotes fixes the line height (24-unit font → 27.95).
* Paragraphs end with `\par` or a raw `\n`+`\` continuation; `\ul … \ulnone`, `\strike … \strikec2`, `\b/\b0`, `\i/\i0`.
* Fonts seen: HelveticaNeue, HelveticaNeue-Bold, HelveticaNeue-Italic (PostScript names, `\fnil\fcharset0`).
  Non-ASCII text is written as `\'xx` (cp1252) or `\uN` escapes; parser-for-goodnotes decodes `\'xx` as CP950 for
  its Chinese samples — a converter should honour `\ansicpg` and `\uN` instead.
* goodparse `text_rtf.parse_rtf_runs` already turns this into per-line styled runs (bold/italic/underline/strike/colour);
  parser-for-goodnotes `text.rtf_to_text` gives plain text.

### 4.3 LZ4 "Type 35" rich-text form — NOT in the corpus

parser-for-goodnotes `text.py:parse_text_elements` (wiki 05 §3, 09 §5) describes a newer text element in which the
content record's top-level field is `#21` holding `{#1 E, #20 {#1 {#1 f32 x, #2 f32 y}}, #30 {#1 {#1 {RGBA fill}}},
#32 {#1 {#2 LEN "bv41…" Apple-LZ4 frame}, #2 {#1 f32 w, #2 f32 h}, #5 {#1 {#30 LEN default font, #40 f32 default size}},
#10 {#1 f32 inset x, #2 f32 inset y}}}`; the decompressed payload is a protobuf list of runs
`{#1 LEN utf-8 text, #2 {#1 1 strike, #2 1 underline, #3 {RGBA}, #30 LEN font, #40 f32 size, #50 1 italic, #60 bold},
#3 {#3 list type, #4 alignment 1/2/3}}`; visible box = content size + 2 × inset. Zero instances in the nine samples
(GoodNotes 6 of 2026-09 still wrote RTF `#8`), so a reader should implement `#8` first and treat `#21`-text per that
parser as best effort. **Low confidence**; evidence pointer is only the parser code.

### 4.4 WRITE recipe — text box

Validated in `build_elems.py` (both parsers read back the rect and the text): copy the rec-33 shape exactly —
`meta = {#1 E, #2 {#2 nonce}, #8, #9, #14 5381, #16 24}`;
`content = {#8 {#1 E, #2 rect(x,y,w,h), #3 rect(x+10,y+10,w-20,h-20), #4 {#1 1.0,#4 1.0}, #5 {#1 {#1 1,#2 nonce}}, #6 RTF,
#7 {1,1,1}, #9 {1,1,1}, #10 10.0, #15 {#2 nonce}, #18 {#2 5.0}, #19 {#4 0.2}, #20 ".tb-0", #21 "", #27 24}}`,
with the RTF generated from the template in 4.2 (`\fsN` = 2 × size in canvas units; `\cfK` into a `\colortbl` you
emit; `\par` between paragraphs; height = lines × 27.95 + 20). Note the metadata clock of a text box has **no
version field** in both samples; mirror that (`{#2 nonce}` in metadata `#2` and content `#15`).

---

## 5. Shapes

### 5.1 Auto-shapes = ink strokes with a `#9` geometry (3 instances, verified)

When GoodNotes recognises a drawn shape it stores an ordinary stroke record whose `#7.#9` (otherwise the empty
string on every stroke) becomes a message, and whose LZ4 blob degenerates to a 67-byte header with **no points**
(`bv41` → 62-byte TPL `vuA(v)A(S(uu))A(S(uuuu))vA(f)` with `v[0]=2`, `v[1]=u32` holding the **width as float32
bits**, all arrays empty, `v[5]=1`). The three shapes:

```
Test5 p3 rec 11 (ellipse, 40-byte #9):  #9 = { #4 {#1 {#1 f32 244.183, #2 f32 251.847}     -- centre
                                                   #2 {#1 f32 48.248,  #2 f32 96.593}      -- semi-axes rx, ry
                                                   #3 f32 -0.62772}                        -- rotation, radians (CCW-negative, y-down)
                                               #5 {#2 varint 1}                            -- unknown flag (present on all 3)
                                               #15 f32 5.4567 }                            -- stroke width (== blob v[1] as f32)
   colour #7.#4 = (0.494, 0.776, 0.212, 1.0). Test5.pdf page 3: stroked green bezier drawing, bbox (90.5,86.2)-(175.9,188.6) pt;
   predicted from centre/axes/rotation ×72/132: (95.6,92.0)-(170.8,182.7) — matches (difference = stroke width + bezier bbox).
ex1 rec 1811 (straight line, 35 B):     #9 = { #1 { #1 {#1 f32 134.09, #2 f32 985.22}, #2 {#1 f32 142.84, #2 f32 985.22} }, #5 {#2 1}, #15 f32 1.5591 }
ex2 rec 15 (polyline, 3 points, 46 B):  #9 = { #2 { #1 {237.92, 184.69}, #2 {373.87, 109.95}, #3 {468.62, 196.67} }, #5 {#2 1}, #15 f32 1.8189 }
```

So inside `#9`: `#1` = line / open polyline, `#2` = polyline (closed? parser-for-goodnotes treats both as point
lists; the ex2 one is a triangle outline), `#3` = axis-aligned rectangle `{#1 centre, #2 (w,h)}` (parser; not in
corpus), `#4` = ellipse `{#1 centre, #2 radii, #3 rotation}`, `#15` = width. Fill: none observed (`#5 {#2 1}`
might be "unfilled"); parser-for-goodnotes reads an optional dash array from `#9.#5.#1`. Hand-drawn polygons
inside `#9.#1`/`#9.#2` may contain curve commands (parser `shape.py:_parse_curves`: sub-fields 3/4 = quadratic /
cubic control points) — not in the corpus. The stroke's lasso offset `#7.#6`, colour `#7.#4` and alpha apply to
the shape. Confidence: high for ellipse/line/polyline/width, medium for rectangle, low for curves/dash/fill.

Reading recipe for the converter: ellipse → 144-point polygon `(cx + rx cos t cos θ − ry sin t sin θ, cy + rx cos t sin θ + ry sin t cos θ)`;
line/polyline → its points; emit as a stroke of constant width `#15` and colour `#7.#4`. (A reader that ignores
`#9` and decodes the blob gets an empty stroke — goodparse drops these shapes silently.)

### 5.2 Writing shapes

Not needed by the converter: write any shape as a plain stroke (polyline) — strictly within observed behaviour.
Writing a `#9` ellipse would require reproducing the 62-byte empty TPL header; untested.

### 5.3 "Type 31" / "Type 35" shapes (top-level `#22` / `#21`) — NOT in the corpus

parser-for-goodnotes `shape.py:_parse_type31_shape/_parse_type35_shape`: `#22 {#1 E, #2 varint 31, #20/#21 points
or bezier segments, #30/#31 varint arrow-head codes (1 open V, 2 filled, 3+ dot), #32 {#1 f32 width, #2 {#2 {dash f32s}},
#3 {#1 {RGBA}}}}` for lines/arrows; `#21 {#1 E, #2 varint 35, #20 {#1 {x,y}}, #21 {#2 {w,h}}, #22 {#1 {#1 corner radius} |
#2 ellipse | #3 {#1 {#1 {#1 {x,y}} …}} normalised polygon}, #30 fill, #31 stroke}` for rectangles/capsules/polygons.
Low confidence; unverifiable here. The "35"/"31" are the values of sub-field `#2`, not schema numbers.

---

## 6. Sticky notes — NOT in the corpus

parser-for-goodnotes `element.py:parse_sticky_notes` (wiki 05 §4, 09 §9): a content record with top-level `#20`
wrapping `{#1 E, #2 varint 35, #20 {#1 {#1 f32 x, #2 f32 y}}, #21 {#2 {#1 f32 w, #2 f32 h}} (default 256×256),
#30 {#1 {#1 {RGB card colour}}}, #31 {#1 {#2 LEN "bv41…" LZ4 text payload as 4.3}}, #33 LEN author, #40 varint 1 =
expanded}`; child strokes/shapes carry the note's UUID in their metadata `#20`/`#21` and are positioned relative
to the note's origin. Low confidence. A converter can carry such a note over as a text box at `(x, y)` with the
card colour as background; nothing in the nine samples lets this be verified.

---

## 7. Eraser cuts and "native CGPath" geometry (inside stroke blobs)

This is stroke geometry, summarised here only because partial erasing changes how a stroke must be read.
Census of the decompressed TPL format strings over all 5 677 content strokes:

| TPL format string | count | where | role |
|---|---|---|---|
| `vuA(v)A(S(uu))A(S(uuuu))vA(f)` | 5 625 | every 2025/2026 file | current pen/highlighter: `v[2]` = per-segment flags, `v[3]` = start points `(x,y)`, `v[4]` = one `(x1,y1,x2,y2)` per segment, `v[5]`=1, `v[6]` dash floats (empty) |
| `vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` | 42 | Test4, Test5 page 2, test | legacy pen (`#7.#3 = 1`): see below |
| `vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` | 1 | ex2 rec 37 (`#3 = 4`) | same with an extra leading `u`; command codes shifted +2 (2/4/5) |
| `vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)` | 8 | Test5 (`#3 = 5`, `#21 = 25`) | newest pen; 5-float points `(x,y,p,?,0)` |

Sub-paths: in both the current and the legacy format the first flag array (`v[2]` / `v[1]`) holds one entry per
segment and **each 0 starts a new sub-path**; the number of 0s equals the number of entries in the start-point
array (5 619/5 619 current-format strokes; legacy strokes likewise). A stroke that was **cut by the eraser** keeps
one element but several sub-paths: Test5 page 2 (`notes/F51610D9-…6206`) rec 1 — legacy format, flags
`[0,1,1,1,1,1,1,1,1,1,0,1,…]`, two start points `(247.0, 46.21)` and `(261.86, 129.56)`, 36 points, 18 segments —
parser-for-goodnotes yields one stroke with 36 points and 18 `native_cgpaths`. No current-format stroke in the
corpus has more than one sub-path, so for the eraser the only direct evidence is this legacy record
(confidence medium; the parser author verified the mechanism on their own corpus at 99.79 % raster agreement, wiki 09 §12).

Legacy-format value layout (Test4 page 2 rec 1, 11 values, verified by decoding): `v[0]=2`; `v[1]` flags;
`v[2]` start points as `(x, y, pressure)` float32 bits; `v[3]` all points `(x, y, pressure)` stride 3 (what goodparse
reads at stride 12 bytes); `v[4]` command count per segment (5–7); `v[5]` CoreGraphics path commands
`0 = moveTo, 2 = cubicTo, 3 = arcTo` (`2/4/5` in the 12-value variant); `v[6]` move-to points `(x,y)`;
`v[7]` empty; `v[8]` cubic control points `(x,y)` ×3 per cubic; `v[9]` 5-float records read by parser-for-goodnotes as arcs
`(cx, cy, r, a0, a1)` — the values seen (`131.75, 37.07, 0.75, 1.28, -1.86` = a point, its pressure and a ±2-unit
vector) look more like per-point ribbon normals, so treat the arc reading as unverified; `v[10]` one flag per record. These describe the **outline** of the rendered ribbon (filled, non-zero winding); a converter
that only needs the centre-line reads `v[3]` (legacy) or `v[3]`+`v[4]` (current: start point, then per segment
the two floats pairs — parser-for-goodnotes `stroke.py:extract_points_from_tpl`), splits at every 0 in the flag
array, applies `#7.#6` (§8) and writes each sub-path as its own Notability curve.

---

## 8. Lasso-moved and grouped elements

### 8.1 Strokes: `#7.#6` = translation added to every stored point (verified)

`#7.#6` is `""` (unmoved) or `{#1 f32 dx, #2 f32 dy}`. 1 205 strokes in the corpus carry a non-empty offset
(24 distinct values, e.g. ex1: `(126.19, 2.10)` on 258 strokes; Test5 page 3: `(-315.95, 329.51)` on 11,
`(-299.44, -385.44)` on 3, `(-579.5, -92.0)` on 4). Test against GoodNotes' own renders (ink colour sampled at the
stroke points, raw vs. shifted): shifted wins 1 126 : 16 over the 18 offsets tested (ex1 12/12 offsets, ex3 3/3, Test5 3/3;
the few "raw" votes are strokes that overlap other ink). Therefore `point_on_page = stored_point + (dx, dy)`;
offsets accumulate (moving again rewrites `#6`, the clock version `#15/#2` increments — versions 10–17 on moved
strokes vs 2 on untouched ones). Confidence: high. The same offset is read by parser-for-goodnotes from the
protobuf trailer after `bv4$` — in this corpus that trailer is always empty (5 677/5 677); the offset lives in `#7.#6`.

### 8.2 Groups

Strokes selected and moved together share a group UUID in `#7.#10` and the same `#7.#7.#1 {#1 version, #2 nonce}`
clock, with `#7.#7.#1.#3` = 1-based member index (Test5 page 3 group `6A79F203-…`: 18 strokes, indices 1–17 plus
one without; three different offsets inside one group because the members were moved at different times). `#10`
has no effect on geometry; a reader may ignore it, a writer never emits it.

### 8.3 Images, text boxes, shapes

Images and text boxes store absolute rectangles; moving them rewrites `#2`/`#3` and bumps the `#5` clock version
(ex1 image rec 1763: version 1183 after many drags). No offset field. Auto-shape geometry in `#7.#9` is in the
stroke's frame; apply the stroke's `#7.#6` to it as parser-for-goodnotes does (`shape.py:extract_move_offset_from_message`,
reading `#14` then `#6`) — unverified in the corpus (the three shapes have empty `#6`), medium confidence.

---

## 9. Converter checklist

READ (GoodNotes → Notability), per page:
1. Resolve `P → T → A` (§3.1); page size = MediaBox; `scale = 72/132`. Classify the background (§3.3): built-in
   paper → Notability paper style; user PDF → Notability PDF background (page `#5`).
2. Walk element pairs; skip metadata `#3 = 1`. `#1` → image (rect `#2`, clip to `#3` if it differs, raster =
   `attachments/<#4>`); `#8` → text (rect `#3` is the text frame, runs from the RTF, size = `\fs/2 × 72/132`);
   `#7` with `#9` message → shape polyline (§5.1); other `#7` → strokes, split at sub-path starts (§7), add `#6` (§8.1).
3. Z-order = record order (images and text are interleaved with ink; Test5 page 3: image at pair 12 of 33).

WRITE (Notability → GoodNotes), additions to the container recipe: §2.5 for every raster, §3.5 for every PDF
page, §4.4 for every text box. Keep `#6.#5` = byte size, `#12` = `""` for rasters and `{1,1}` for PDFs,
`index.attachments.pb` ⇔ members, and emit `#102` for every non-empty notes layer.

---

## 10. Corrections and gaps relative to the reference material

* goodparse docstring: "a companion record carries a placement matrix" — it is the element's own content record
  (same UUID, always the next record); `f3` is the crop rect (centre+size), not a second point pair of the matrix.
* goodparse `TextBox.color`: `#8.#4`/`#7`/`#9` are not the text colour; the RTF `\cf` is (verified by the export).
* parser-for-goodnotes wiki 05 §3: "Text boxes are stored within the Type 35 Record" — the GoodNotes 6 files of
  2026 in this corpus store text boxes as RTF in `#8`; "Type 35" is an additional, unverified form.
* parser-for-goodnotes `_parse_text_elements`/`parse_sticky_notes` key on `#16 == 35` of the metadata record;
  `#16` is the schema (24/25) in every record here.
* `#15` events are not required for images (Test5).
* Not determinable from the corpus: image crop/rotation semantics, multi-page PDF attachments, user-imported PDF
  naming, sticky notes, Type 31/35 records, meaning of image `#6 = 1`, text `#4/#18/#19`, shape `#9.#5`.

Scripts used (scratchpad): `work/dump.py` (tree dumps with byte offsets), `work/notes_census.py` (record
signatures), `work/build_elems.py` (writer validation: image + text + PDF-backed pages, read back by both
parsers), ad-hoc checks against `Test5.pdf`/`ex1.jpg`/`ex3.jpg` with PyMuPDF (`pip install pymupdf numpy` works
in this environment).

## Critic additions

* **Shape and flat-stroke width unit**: `#9.#15` and the TPL width word `W` are in 1/144 in
  (page width in points = `W/2`), not `W × 72/132`: the Test5 ellipse (`#15` = 5.4567) is
  stroked at exactly 2.7283 pt in GoodNotes' own Test5.pdf export, and all nine flat strokes on
  that page follow the same rule (see `goodnotes-stroke.md`, Critic additions 4). The §5.1 remark
  "difference = stroke width" is consistent with this.
* **Flat-format segments are quadratic Béziers** (`(cx,cy)` control, `(ex,ey)` end), verified
  against the export to 0.0002 pt; §7's "the two float pairs" per segment are not both on-curve.
  Sub-path splitting at 0-flags is unaffected.
* **`#3 = 1` elements** (1 636 in the corpus) all have an empty 62-byte TPL and unique UUIDs;
  skipping them (as §1 says) loses no visible ink whatever their exact semantics are.

## Addendum (schema 25/35)

`goodnotes-v35-elements.md` (Test6–Test9, GoodNotes 2026-09/10) adds the element kinds and
corrections that this document could not see (its §9 lists them): content kind `#9` = the 10 %
alpha fill of a closed auto-shape, linked to its outline stroke by `#5`; content kind `#21` =
the schema-35 text element (sticker letters and typed text, runs in an Apple-LZ4 blob under
`#32.#1.#2`, transform `#20`, insets `#32.#10`); image attachments may be die-cut sticker PDFs
(`#6 = 3`) and JPEG photos keep their EXIF orientation with `#2` holding the displayed size;
`#7.#9.#2 {P0, C, P1}` is one quadratic Bézier, not a polyline; shape widths render at `W/2`
pt. `goodnotes-v35-strokes.md` covers the stroke-level findings (32 KiB LZ4 blocks, the 8-byte
`bv4-` header, the flags-4/5 marker band, pencil force attributes).
