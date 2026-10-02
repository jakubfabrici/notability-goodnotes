# Page / template / canvas binding in the latest GoodNotes files (schema.pb 25 and 35)

Status: reverse-engineered from the four new goodparse samples written by the latest GoodNotes
(Test6, Test7, Test8: `schema.pb = 08 19` = 25, iOS 16.7.16 device, 2026-09-28/30; Test9:
`schema.pb = 08 23` = 35, iOS 26.6.1 device, 2026-09-30) and cross-checked against the nine older
samples that `goodnotes-container.md` was derived from. Every claim is tagged with its evidence
and a confidence. GoodNotes itself could not be run; the ground truth for display order, page
size and paper is GoodNotes' own PDF export next to each sample (`Test4.pdf` … `Test9.pdf`,
Quartz PDFContext, iOS 16.7.x / 26.6.1).

This document supplements `goodnotes-container.md` (§4, §5, §10) and does not edit it. Where the
two disagree, this one wins; §11 lists the corrections.

Evidence base (13 GoodNotes-written files, all read with `gnnote.protobuf` strictly — no record
of any `.pb`, `notes/` or `search/` member failed to decode, i.e. none contains the "group wire
types 4/6" that goodparse's code comments claim):

| sample | written | device id | schema.pb | pages (live / listed) | event kinds (count) |
|---|---|---|---|---|---|
| Test6 | 2026-09-28 | 1606161793633522096 | 25 | 5 / 6 | #30×1, #6×3, #2×3, #54×6, **#3×1**, #56×1, #102×5, #10×1 |
| Test7 | 2026-09-28 | same | 25 | 4 / 5 | #30, #6×3, #2×3, #54×5, **#3×1**, #56×1, #103×3, #105×3, #102×4, #10 |
| Test8 | 2026-09-30 | same | 25 | 4 / 4 | #30, #6, #2, #54×4, #102×4, #10 |
| Test9 | 2026-09-30 | 7401413043201209088 | **35** | 7 / 7 | #30, #6×8, #2×6, #54×7, #104×33, #105×34, #102×5, #10 |
| Test4, Test5, test, test2, test3, ex1-3, record | 2024-11 … 2026-09 | various | 24 / 25 | 14 | see `goodnotes-container.md` §10.1 |

Tools: `scratchpad/v35/ev.py` (event dumper), `scratchpad/v35/verify_binding.py` (reference
implementation of §9 + the three checks of §9.2), `scratchpad/v35/reader_patched.py` (a scratch
copy of `gnnote/goodnotes/reader.py` with the §10 changes applied, used only to prove them),
pymupdf 1.28 for rasterising the export PDFs.

---

## 1. Summary

**Nothing structural changed.** The latest GoodNotes still writes the event log, index files and
notes files documented in `goodnotes-container.md`; the field shapes of `#30`, `#6`, `#2`, `#54`,
`#56`, `#10`, `#102`, `#103`–`#105` are byte-for-byte the same shapes as in the 2024–2026 files
(§8.2, census over all 13 files). The binding chain is unchanged:

```
index.notes.pb : N ──(P = N − 1, 128-bit integer)──▶ #54 / #3 (entity P): #3.#1 = T
#2 (entity T) : #4 = attachment id A, #5 = PDF page (1-based), #8 = {canvas W, canvas H}
index.attachments.pb : {#1 A, #2 member}  (member name ≠ A for "internal" papers)
attachments/<member> : PDF, MediaBox of page #5 = page size;  canvas = MediaBox × 11/6
```

What *is* new, and what broke our reader (all three "page X: no template event" warnings and the
mis-attached 135-stroke layer on Test9 have one cause):

1. **`N = P + 1` is 128-bit integer arithmetic, with carry.** Three of the 16 new pages have a
   page UUID ending in `F`: `…D07D3281E44F` → notes `…D07D3281E450` (Test9 grid page, the
   137-element layer), `…607737EDC91F` → `…607737EDC920` (Test6 last page), `…F3671AC8209F` →
   `…F3671AC820A0` (Test7 last page). Our reader matches on the first 35 characters and therefore
   finds no `#54` for them (§4.2). Confidence: high (hex of all 30 page/notes pairs in the corpus
   plus the `search/<N>` members, which store `P` in `#5`).
2. **New event `#3` = "page template re-bound"**: `{#1 D, #2 P, #3 {#1 T, #2 clock v2}, trailer}`.
   It appears in Test6 and Test7 once each, when the user changed a page's paper after creation.
   The export PDF proves the re-bound template is the one shown (§8.3). Our reader ignores `#3`
   and shows the original ruled paper on Test6 page 2 and Test7 page 1 (goodparse has the same
   bug; parser-for-goodnotes handles it).
3. **Attachment id ≠ storage member ("internal paper UUIDs")**: Test9's inserted PDF page is
   bound to attachment id `80973E20-DFB5-5806-A7BD-0DFB71B92012` whose bytes live in
   `attachments/4452790C-9B56-466F-9109-B5EA41F59932`. `index.attachments.pb` and the `#6` event
   both carry the pair; our reader already resolves it through `index.attachments.pb` (§5).
4. **`index.notes.pb` order is neither creation nor display order** in Test9 (§4.3): display
   order must come from the `#54.#4` keys, whose alphabet now includes `!` (`O9t!N`).
5. **`#7` / `#18` in a `#2` event do not mean "the paper has lines"**: GoodNotes' "White" papers
   (`B03314A5-…_standard_1_2 - White`, `…_a4_1_2 - White`) carry `#7 = 29.33` and an `#18` block
   but their PDF is a plain fill and the export shows no lines. Only `#18.#3` (f32 0.9167)
   coincides with a line-drawing PDF, 13/13 templates (§8.5). Our reader's `lined_hint` turns
   these into "lined" pages.
6. **Per-page canvas** has always been per template (`#2.#8`); Test9 is merely the first sample
   mixing sizes (A4, 1280×905 landscape Figma export, 595.2×841.68 Excel form, a 454.91×143.28
   strip). `canvas = float32(MediaBox × 11/6)` holds for all 7 sizes (§8.4).
7. `document.info.pb` is back (0 bytes, Test9 only); `schema.pb = 35` is caused by four
   "letter text" elements (`#21`) whose metadata says `#16 = 35` (§3). Everything a writer emits
   stays at 24.

---

## 2. ZIP member set

Member order in Test9: `document.info.pb`, `index.search.pb`, `index.notes.pb`, `search/*`,
`notes/*`, `index.events.pb`, `thumbnail.jpg`, `index.attachments.pb`, `attachments/*`,
`schema.pb` — exactly the order of `goodnotes-container.md` §1.1 (Test6–8 have no
`document.info.pb` and start with `index.search.pb`). Entry details are unchanged (version made
by 0x0315, flags 0x0800, method 8 for every member including 0-byte ones, external attr
0x81a40000) with one new detail: two of Test9's imported attachments carry 0x81800000
(`-rw-------`), i.e. the mode of the source file is copied. Harmless; write 0x81a40000.

`search/` now contains members for **both** notes layers (named by `N`, 677 B – 58 kB,
handwriting/text recognition) and attachments (named by the attachment *id*, 5 B for an un-indexed
PDF, 3.9–7.8 kB for PDFs with text). The member for the internal paper is `search/80973E20-…`
(the id, not the storage name). Confidence: high.

`thumbnail.jpg`: still a 499-px-high JPEG of the first page in display order (Test9: 353×499 =
A4 aspect, the green A4 page). Confidence: high.

## 3. `schema.pb`

| file | schema.pb | records carrying a value ≠ 24 |
|---|---|---|
| Test6, Test7, Test8 | `08 19` (25) | only stroke content `#21 = 25` / metadata `#16 = 25` on the newer pen type (as in Test5) |
| Test9 | `08 23` (35) | the 4 metadata records (`#16 = 35`) of the `#21` letter-text elements on page `F79D1187` and the `#102` event for that layer (`#15 = 35`, record 46); 21 strokes at 25; everything else, every event, 24 |

The rule from `goodnotes-container.md` §3 ("schema.pb = max version used anywhere") holds
(confidence high). A file that contains no `#21` elements is still written with 24/25 by the
iOS 26 build, so a writer emitting 24 everywhere and `08 18` is still mimicking current GoodNotes.

## 4. `index.notes.pb` — page list

### 4.1 Shape
Unchanged: one 82-byte record `{#1 N, #2 "notes/" + N}` per page, 83 bytes with its length byte
(Test6: 498 = 6 × 83). Deleted pages stay listed (Test6: `05C302B5…`, Test7: `3FE7EF50…`, both
0-byte members). Confidence: high.

### 4.2 `N = P + 1` as a 128-bit integer (carry-aware)
All 36 page/notes pairs in the corpus (33 live pages + 3 deleted ones) satisfy
`int(N) == int(P) + 1` where the UUID is read as a 32-digit hex number. In 33 pairs only the last
digit changes; in three the page UUID ends in `F` and the increment carries:

| file | page UUID `P` (events, `#54.#2`) | notes UUID `N` (`index.notes.pb`, `#102.#1`) |
|---|---|---|
| Test9 | `D298AC1F-CFFB-42D1-A4AE-D07D3281E44F` | `D298AC1F-CFFB-42D1-A4AE-D07D3281E450` |
| Test6 | `A37FF8B6-79CB-4486-B233-607737EDC91F` | `A37FF8B6-79CB-4486-B233-607737EDC920` |
| Test7 | `308EE367-D7BA-472E-AD76-F3671AC8209F` | `308EE367-D7BA-472E-AD76-F3671AC820A0` |

Independent confirmation: every `search/<N>` member of a notes layer starts with `#5 = P`
(Test9: `search/D298AC1F-…E450` → `2a 24 "D298AC1F-CFFB-42D1-A4AE-D07D3281E44F"`, likewise for
`3B1DB163`, `938FFD76`, `A3292C44`). Both reference parsers compare the first 32 hex characters,
which tolerates a carry in the last group only; the integer rule is exact. Confidence: high.
(No page UUID ending in `FFFF…` exists to show a carry across a hyphen group; the integer rule
covers it anyway.)

### 4.3 Order of the records
Test6/7/8 list pages in creation order (as all older samples). **Test9 does not**: the list is
`A3292C44, 3B1DB163, F79D1187, B11A6A6D, D298AC1F, 19BBFBA7, 938FFD76` while creation order
(`#54` sequence) is `19BBFBA7, D298AC1F, F79D1187, 3B1DB163, B11A6A6D, 938FFD76, A3292C44` and
display order (export PDF) is `19BBFBA7, D298AC1F, F79D1187, 3B1DB163, B11A6A6D, A3292C44,
938FFD76`. `index.search.pb` lists the notes layers in the same (apparently hash-table) order as
`index.notes.pb`. A reader must therefore sort by the order keys (§8.6) and must not fall back to
list order for pages that have a `#54`. Confidence: high.

## 5. `index.attachments.pb` — attachment list and "internal paper UUIDs"

Shape unchanged, `{#1 attachment id A, #2 member name}`, but **`#2` is not always
`"attachments/" + #1`**. Test9 record 5 (88 bytes):

```
0a 24 "80973E20-DFB5-5806-A7BD-0DFB71B92012"
12 30 "attachments/4452790C-9B56-466F-9109-B5EA41F59932"
```

and the matching `#6` event (record 72, entity `80973E20-…`):

```
#1 "80973E20-DFB5-5806-A7BD-0DFB71B92012"   -- attachment id (what #2.#4, search/ and index #1 use)
#2 "4452790C-9B56-466F-9109-B5EA41F59932"   -- storage id (the ZIP member name)
#5 779994  #6 D  #10 ts  #11 uuid  #12 {#1 1, #2 1}  #14 dev  #15 seq  #16 24
```

So `#6.#1` is the attachment *id* and `#6.#2` the *storage* id; in the other 44 `#6` events of
the corpus the two are equal, which is why they were documented as "same". The id is a
version-5 (name-based) UUID (`…-5806-…`); the template bound to it (`946B9E23-1748-5766-…`) and
the page created for it (`A3292C44-0C5F-58D4-…`) are v5 as well, every other id in the file is
v4. The attachment is the Excel-generated one-page PDF of a German handball form that was inserted
as a page ("HVS-Vordruck 01-001", from its `search/` text). Interpretation (medium confidence): a
PDF inserted as a page gets content-derived ids (uuid5) for attachment/template/page while the
bytes are stored under a per-document random name; ex2 ("Teat (2)", a duplicated notebook from
2026-06) has v5 ids for *everything* including the document, which fits "derived ids on
copy/import".

Resolution rule (confidence high — it is what both reference parsers converge on and what makes
Test9 page 6 render on its form): look the attachment id up in `index.attachments.pb` (`#1 → #2`);
if absent, in the `#6` events (`#1 → "attachments/" + #2`); if still absent, try
`attachments/<id>` directly. Our reader already does the first and third step
(`_index_pairs` + `self.attachments[u] = member`); adding the `#6` alias is cheap insurance.

Attachment roles in Test9 (type sniffed from bytes; `#6.#12` = `{#1 1, #2 1}` for every PDF used
as a paper, including the Figma and form PDFs, and empty for the 2 MB JPEG photo **and for the
4113-byte sticker PDF** `786977EC`, which is an *image* element's attachment, not a paper):

| id (member) | bytes | kind | role |
|---|---|---|---|
| `03D3A8D4` | 953 | svg2pdf PDF, A4 | catalogue paper "…_a4_1_7 - Green" |
| `B0212B82` | 3492 | svg2pdf PDF, A4 | catalogue paper "9FE8F365…_a4_1_2 - White" (grid) |
| `6F089296` | 893 | svg2pdf PDF, A4 | catalogue paper "B03314A5…_a4_1_2 - White" (plain) |
| `F5503752` | 321483 | PDF 1280×904.9995 (Figma) | imported paper, `#2.#9` = `2942D9EA-…` (bare UUID name) |
| `4043E92A` | 143137 | Quartz PDF 454.9091×143.2785 | inserted PDF page (strip), `#2` without `#6`/`#9` |
| `80973E20` (`4452790C`) | 779994 | Excel PDF 595.2×841.68 | inserted PDF page, internal id (above) |
| `7AEDBFA1` | 2063118 | JPEG | photo image element on page `3B1DB163` |
| `786977EC` | 4113 | PDF (Quartz, iOS 14.1) 254×214 | **die-cut sticker**, referenced as an image element (`#1`) on page `F79D1187` — a PDF in the image role, which our reader rejects as "neither PNG nor JPEG" |

## 6. `index.search.pb` and `search/`

Unchanged shapes (`{#1 uuid, #2 "search/"+uuid [, #3 1 for attachments]}`). Test9 lists the five
notes layers that have content first (no `#3`), then the eight attachment ids including the
internal id `80973E20` and the sticker/photo. Test6 and Test8 have a 0-byte `index.search.pb` and
no `search/` members, written by the same build as Test7, which has three attachment entries: the
index is optional and asynchronous, as before. Confidence: high.

## 7. `document.info.pb`

Present in Test9 (0 bytes, first member, mtime = save time), absent in Test6/7/8 from the
2026-09 iOS 16 build. Still never non-empty in any of the 13 files; optional for a writer, 0 bytes
if emitted. Confidence: high.

## 8. `index.events.pb`

### 8.1 Envelope and trailer
Unchanged (`{#1 entity, #E {body}}`; `#10` f64 ms, `#11` event uuid, device/seq/schema at
`d, d+1, d+2`). `#3` uses `d = 13` like `#54`. The sequence number is **strictly increasing but
not consecutive**: gaps of 2–39 occur in every new file (Test6 diffs {1,3,4,6,7,8,15,27}, Test9
{1…30}); `goodnotes-container.md` §2.4 "+1 per event" only describes Test4. A writer may use +1;
a reader must only rely on monotonicity. Confidence: high.

### 8.2 Field-shape census (all 13 files)
For every event kind the exact nested field shape was computed per file
(`scratchpad/v35/verify_binding.py` has the decoder; the census lives in the session log). Result:
`#30`, `#6`, `#10`, `#54`, `#56`, `#102`, `#103`, `#104`, `#105` have **identical shapes** in the
2024, 2026-06 and 2026-09 files (Test9's `#54` even carries the same 48-byte `#17.#1` colour
block). The only new kind is `#3`; the only new `#2` variant is the "inserted PDF page" form
without `#6`/`#9` (§8.4). `#55`, `#15`, `#31`, `#34`, `#106`, `#160`, `#163` do not occur in the
new files. Confidence: high.

### 8.3 New: `#3` — page template re-bound
Test6 record 12 (233 bytes), entity = page `8AAD87B5-…-AA8D92E2249A`:

```
0a 24 "8AAD87B5-DF1F-4F46-A142-AA8D92E2249A"
1a c0 01 {                                     -- #3 body
  #1  "C55AD5D1-5228-4CCC-BF90-625A9F625835"   -- document D
  #2  "8AAD87B5-DF1F-4F46-A142-AA8D92E2249A"   -- page P
  #3  { #1 "2E6C1D4A-FCAB-4ECB-9FEE-493A38B5F1BB",  -- the NEW template T
        #2 { #1 2, #2 3360410182 } }               -- the page's template register, version 2
  #10 f64 1790595399569.14  #11 uuid  #13 dev  #14 seq  #15 24 }
```

i.e. the same `#2`/`#3` layout as `#54` minus the order key and the `#17` colours, with the
register version bumped from 1 (in the `#54`) to 2. Sequence in Test6: `#54` creates page
`8AAD87B5` on template `7D1E03D2` (ruled "AA273C17…_standard_1_2 - White", 1904-byte PDF with 22
lines); 13 seconds later `#6`+`#2` add a new paper `2E6C1D4A` ("B03314A5…_standard_1_2 - White",
892-byte plain PDF); then `#3` re-binds the page to it. Test7 record 10 does the same for page
`6FD1025B` (`7DDEA5AC` → `571379A8`).

Ground truth: rasterising the exports at 72 dpi and counting full-width grey rows gives Test6.pdf
page 1 → 22 ruled rows, page 2 (the re-bound page) → 0, pages 3–5 → 0; Test7.pdf page 1 (re-bound)
→ 0. The 1904-byte template renders 22 rows, the 892-byte one 0. So the **last** template event
for a page wins. parser-for-goodnotes reads `#3` like `#54` and gets it right; goodparse keys on
`#54` only and shows the ruled paper (its `bg 1904` on Test6 page 2 / Test7 page 1 is wrong);
our reader has the same defect. Confidence: high.

Rule: process the log in order; for kinds 54 and 3 set `page_template[P] = body.#3.#1`; the
register version (`#3.#2.#1`) increases with each re-bind, so "last in log order" and "highest
version" agree (both observed).

### 8.4 `#2` template events: three variants, per-page canvas
All six `#2` events of Test9 (`#2.#5 = 1` in every one — no multi-page PDF import exists in the
corpus, so `#5` = 1-based PDF page index remains parser-for-goodnotes' claim, unverified):

| rec | T | `#4` attachment | `#6` | `#7` | `#8` canvas (f32) | `#9` name | `#18` |
|---|---|---|---|---|---|---|---|
| 2 | `E7070FC0` | `03D3A8D4` (953 B) | 1 | – | 1091.3466796875 × 1543.4649658203125 | `417A3734-…_a4_1_7 - Green` | – |
| 4 | `06788362` | `B0212B82` (3492 B) | 1 | 29.333 | same | `9FE8F365-…_a4_1_2 - White` | `{rect (44, 58.67)+(1003.35, 1426.13), #2 28.4167, #3 0.9167, #5 1}` |
| 30 | `978C4DA3` | `6F089296` (893 B) | 1 | 29.333 | same | `B03314A5-…_a4_1_2 - White` | `{rect (44, 62.33)+(1003.35, 1418.80), #2 29.333, #5 1}` (no `#3`) |
| 61 | `27522DDD` | `F5503752` (Figma, 321 kB) | 1 | – | 2346.666748046875 × 1659.165771484375 | `2942D9EA-9E19-4B3F-BBBA-249E9B4A150B` | – |
| 66 | `76E5FF0D` | `4043E92A` (strip, 143 kB) | **absent** | – | 834.0 × 262.6771545410156 | **absent** | – |
| 73 | `946B9E23` | `80973E20` (internal → `4452790C`) | **absent** | – | 1091.199951171875 × 1543.0799560546875 | **absent** | – |

So: catalogue papers keep the `<builtin UUID>_<size>_<n>_<m> - <colour>` name; a PDF imported
*as a paper template* (Figma) gets `#6 = 1` and a bare UUID as `#9`; a PDF *inserted as a page*
(strip, form) gets neither `#6` nor `#9`, and the `#12`/`#13`/`#17` registers are value-less
`{#2 clock}`. The name is free text for a reader; use "`#9` matches
`^[0-9A-F-]{36}_[a-z0-9]+_\d+_\d+ - `" to recognise catalogue papers (our `BUILTIN_TEMPLATE_RE`
does; the Figma name does not match, correctly).

Canvas: `#2.#8 = float32(MediaBox × 11/6)` with the product computed in double precision, for
all five distinct sizes: 595.28 → 1091.3466796875, 841.89 → 1543.4649658203125, 1280 →
2346.666748046875, 904.999512 → 1659.165771484375, 595.2 → 1091.199951171875, 841.68 →
1543.0799560546875, 454.9091 → 834.0, 455.04 → 834.239990234375 (computing `f32(W) × f32(11/6)`
instead gives 1091.3468…, so GoodNotes multiplies in double). The one exception, the strip's
height 262.6771545410156 instead of f32(143.2785 × 11/6) = 262.67724609375, corresponds to
143.27845 pt, i.e. the MediaBox was rounded to 4 decimals by Quartz after the canvas was computed
from the exact size. Consequence for a reader: derive the scale from the *width* ratio
`page_width_pt / canvas_w` (our reader does) — the relative error is 5e-6, invisible. The scale
is 6/11 for every page in all 13 files, which is also what goodparse hard-codes. Confidence: high.

### 8.5 `#7` / `#18` are not "this paper has lines"
Over the 13 distinct catalogue templates of the corpus:

| template name (`#9`) | `#7` | `#18.#2` | `#18.#3` | PDF size | path ops beyond the fill |
|---|---|---|---|---|---|
| `417A3734…_standard_1_1 - Blue`, `…_a4_1_7 - Green`, `BF443295…_a4_1_2 - Baby Blue` | – | – | – | 945–953 B | 0 (fill + one stripe for Blue) |
| `B03314A5…_standard_1_2 - White`, `…_a4_1_2 - White` (Test6/7/8/9) | 29.33 | 29.33 | **–** | 892/893 B | **0** |
| `AA273C17…_standard_1_2 - White` (Test6/7 ruled) | 29.33 | 44.92 | 0.9167 | 1904 B | 69 |
| `9FE8F365…_standard_1_1 - Yellow`, `…_a4_1_2 - White` (grid) | 29.33 | 28.42 | 0.9167 | 3224/3492 B | 201/276 |
| `CC365888…_standard_1_2 - White` (ex1/ex2, dotted) | – | 28.42 | 0.9167 | 41.8 kB | 4295 |

The export PDFs show no lines on the `B03314A5` pages (Test6 pages 2–5, Test7 all four, Test8
all four: 0 ruled rows at 72 dpi, versus 22 rows on the `AA273C17` page). So GoodNotes draws only
the paper PDF; `#7`/`#18.#2` are the writing-guide line spacing it associates even with blank
"White" paper (29.33 canvas units = 16 pt), and `#18.#3 = 0.9167` is the only field that
coincides with a visibly ruled/grid/dotted PDF (13/13). Semantics of `#18.#3` itself unknown
(line opacity or width?). Confidence that a reader must classify from the PDF content or
`#18.#3`, not from `#7`/`#18` presence: high.

### 8.6 Order keys (`#54.#4.#1`)
All 36 keys in the corpus: `43~lY1 43~lY2 AntJC 4EGLM1 4EGLM2 APaxN FOw4R KnCf0 O0CZ3 4lHdw1
4lHdw2 Ae7L0 F9LqN KrbnF 4HVRp A2JQn FPFSn KeUy7 4mgFG1 4mgFG2 Am5Pm F4uWc KBM_u O9t!N L2FtP
4Ka3C1 4Ka3C2 4Qb_W AnFq6 4ES6R 4rKOY 4gn8n Af~Am`; alphabet seen `!0-9A-Z_a-z~`, i.e. the key
is an arbitrary printable-ASCII string (the `!` in Test9's `O9t!N` is new). Pages created
together at document creation get `<4 random><digit>`; a page appended at the end gets a key whose
first character steps `4 → A → F → K → O` (same in Test6, 7, 8, 9); a page inserted *between*
existing pages at creation gets a key between its neighbours: Test9's form page was created last
(record 74, after the strip, record 67) with `L2FtP`, between `KBM_u` and `O9t!N`, and the export
shows it before the strip. Byte-wise sort of the keys reproduces the export order of all six
exports (Test4–9, 25 pages) and parser-for-goodnotes' order for all 13 files. No `#55` reorder
event exists in the corpus. Confidence: high for "sort bytewise"; medium for the generation
scheme (not needed by a reader).

### 8.7 Deleted pages
Test6 record 7 (236 bytes) and Test7 record 7: `#56 {#1 D, #2 P, #3 {#1 1, #2 clock v1},
#4 "80153E4A-D94B-4FFE-A6E9-35DFF3272942" (an otherwise unreferenced UUID), trailer 10/11/13/14/15}`
— same shape as test.goodnotes (2024). In both files the deleted page is the *first* page of the
new notebook (keys `4EGLM1`, `4lHdw1`), its `notes/` member is 0 bytes, it stays in
`index.notes.pb`, has no `#102`, and the `#10` pointer and thumbnail refer to the surviving
pages. `#56.#2` is the page UUID `P` (`…8A98`, `…DD26`), so the integer rule of §4.2 applies to
deletions too (a deleted page whose `P` ends in `F` would survive the 35-character match).
Confidence: high.

### 8.8 Duplicated and reordered pages
Not present in any sample (no `#55`, no two pages sharing content). What the corpus does show:
copying/importing produces v5 UUIDs (§5), and a page inserted at a position is simply created with
an in-between order key (§8.6). parser-for-goodnotes' `#55 {#2 P, #3 {#1 key, #2 clock}}` remains
the only description of reordering; keep handling it (our reader does). Confidence: low (no data).

### 8.9 Other events in the new files
`#10` (current page), `#102` (per notes layer written; Test9 record 46 carries `#15 = 35` for the
letter-text layer, all others 24), `#103`/`#105` (PDF text-indexed; Test7 has them for all three
papers, including the one bound only to the deleted page), `#104`/`#105` (Test9: 33 + 34 of them, one pair per
recognition pass of a layer, `#105.#1 = 1` always, `#5` hash present on most) — all as documented
in `goodnotes-container.md` §10.3 / Critic 5.

## 9. Resolution algorithm (verified on all 13 samples)

```
attachments := { rec.#1 → rec.#2 }  for rec in index.attachments.pb            (§5)
for ev in index.events.pb, in file order:                                       (§8)
  #6 : alias[ev.#1] = ev.#2                                                     (id → storage id)
  #2 : template[ev.#2] = (att = ev.#4, pdf_page = ev.#5 or 1,
                          canvas = (ev.#8.#1, ev.#8.#2), name = ev.#9,
                          lines = ev.#18.#3 present)
  #54: page_template[ev.#2] = ev.#3.#1 ; order_key[ev.#2] = ev.#4.#1
  #3 : page_template[ev.#2] = ev.#3.#1                                          (re-bind, last wins)
  #55: order_key[ev.#2] = ev.#3.#1
  #56: deleted += ev.#2
pages := []
for (N, member) in index.notes.pb:
  P := uuid(int(N) − 1 mod 2^128)                                               (§4.2)
  if P ∈ deleted: continue
  T := template[page_template[P]]                                               (None → warn, guess)
  member_pdf := attachments[T.att] or attachments[alias[T.att]] or "attachments/" + T.att
  size := MediaBox of page T.pdf_page of member_pdf (rotation applied)
  scale := size.width / T.canvas.width       (== 6/11 in every file)
  pages += (N, member, T, size, T.canvas, order_key[P])
sort pages by order_key bytes; pages without a key go last in index order
```

### 9.1 Results
`scratchpad/v35/verify_binding.py` implements exactly this with `gnnote.protobuf`/`pdfutil` and
reports, for every one of the 13 files:

* **A. page sizes in display order == GoodNotes export PDF** (Test4–9, 25 pages): MATCH.
  Test9: `595.28×841.89 ×4, 1280×905, 595.2×841.68, 454.91×143.28`.
* **B. display order and background member per page == parser-for-goodnotes
  (`Page.uuid`, `background_attachment_path`)**: MATCH for all 13 files (33 live pages incl. the
  internal-paper page → `attachments/4452790C…`; the 3 deleted pages are hidden by both).
* **C. ruled lines visible on the export page == bound paper PDF draws lines** (catalogue papers,
  pymupdf raster): MATCH for Test4–9, which pins the `#3` re-binds and the §8.5 finding.

goodparse (its `_read_page_meta` / `_read_paper_to_attachment`) agrees on sizes and order but
differs on the two re-bound pages (Test6 page 2, Test7 page 1), where it is wrong (§8.3).

### 9.2 Applying it to our reader
A scratch copy of `gnnote/goodnotes/reader.py` with only the changes of §10 applied
(`scratchpad/v35/reader_patched.py`, not committed) reads all 13 files with no "no template
event" warning; Test9 page 2 gets its 123 strokes on the B0212B82 grid paper at 595.28×841.89,
Test6/Test7 last pages get their paper, Test6 pages 2–5 / Test7 / Test8 become `paper = "plain"`
(as exported), and the backgrounds equal parser-for-goodnotes' for every page.

## 10. Concrete changes for `gnnote/goodnotes/reader.py`

1. **Replace the 35-character key with exact page UUIDs** (fixes all three "no template event"
   pages and the mis-attached Test9 layer). Key `_Events.pages` and `_Events.deleted` by the full
   `#54.#2`/`#56.#2` UUID (`_uuid_key(u) = u.upper()`), and on the notes side convert
   `N → P = N − 1` as a 128-bit integer before every lookup (`_page_list`: deleted check and
   order key; `_build_page`: template). Keep a tolerant fallback for hand-made files: if the exact
   `P` is missing, try the 32-hex-character prefix match the two reference parsers use. Evidence
   §4.2.
2. **Handle event `#3` exactly like `#54` for the template** (`#2` = P, `#3.#1` = T), without
   touching the order key; later events overwrite earlier ones (log order). Evidence §8.3.
3. **`_Template.lined` only from `#18.#3`** (`layout = _msg(body, 18); lined = layout is not None
   and protobuf.get(layout, 3) is not None`), so `_paper_style`'s `lined_hint` branch stops
   turning the blank "White" papers of Test6/7/8/9 into "lined". Evidence §8.5.
4. **Attachment alias from `#6`**: record `#6.#1 → #6.#2` and let `_Reader.attachment()` fall
   back to `attachments/<#6.#2>` when the id is in neither `index.attachments.pb` nor the member
   list (today's path works for Test9 only because its index carries the mapping). Evidence §5.
5. Keep sorting order keys bytewise (`key.encode("utf-8")`) — correct as is; keep `#55`.
6. Not binding-related but found on the way (for the element task): image-element attachments can
   be PDFs (die-cut stickers, Test9 `786977EC`, 254×214 pt MediaBox) — rasterise or embed as a
   PDF background instead of "neither PNG nor JPEG"; top-level content kinds `#9` (Test6/7/8, a
   shape record with fields 1,2,3,4,5,6,7,15,18) and `#21` (Test9 letter text, metadata
   `#16 = 35`) are skipped; these do not affect page binding.

`gnnote/goodnotes/writer.py` needs no change for compatibility with these files (see §12); if the
`N = P + 1` rule is ever applied to arbitrary UUIDs there, use the integer form rather than
restricting `P` to `0–E`.

## 11. Corrections to `goodnotes-container.md`

* §4 "N == P except for the last hex digit, which is P's last digit + 1 … choose page UUIDs ending
  in 0–E so the +1 never overflows": GoodNotes itself produces `P` ending in `F` and carries;
  the rule is 128-bit integer `+1`. Readers must implement the carry.
* §4 "Display order is not taken from this list": now also *creation* order is not reflected
  (Test9). Treat the list purely as the set of pages.
* §5 "`#2` is always `"attachments/" + #1`" and §10.3 "`#6`: `#2 LEN A (same)`": false for
  inserted PDF pages (`80973E20` → `4452790C`). `#1` is the id, `#2` the storage name.
* §10.1 event catalogue: add `#3` (page template re-bound), seen twice.
* §10.3 `#2`: `#6` and `#9` are optional (absent for inserted PDF pages); `#9` may be a bare UUID.
* §10.3 `#2` "`#7` present for lined papers only" and reader's `lined` hint: `#7`/`#18` also
  appear on blank "White" papers; only `#18.#3` tracks visible rulings.
* §2.4 "Sequence number … increases by exactly 1 per event": strictly increasing with gaps.
* §10.6 alphabet: add `!`; keys are arbitrary printable ASCII, compared bytewise.
* §7 `document.info.pb`: present again (0 bytes) in the newest file; still optional.
* goodparse's statements "current GoodNotes uses group wire-types (4/6) in its index files" and
  "f54(page, layer) → layer create-info": no wire type 3/4/6/7 occurs anywhere in the 13 files
  (strict decoding succeeded on every record), and the "layer" is the template entity `T` of the
  `#2` event documented here — same chain, new words.

## 12. What the writer (`goodnotes-container.md` §14 recipe, schema 24) should keep or change

Only inference is possible (no GoodNotes to test with); the basis is "the iOS 26 build still
writes the same shapes it has written since 2024, so a file in those shapes is as acceptable as
before".

Keep (all still exactly what GoodNotes 2026-09 writes):
* `schema.pb = 08 18`, every event/element at 24; the 35 in Test9 is tied to `#21` letter-text
  elements we never emit, and Test6–8 from the same month are 25 only because of pen type 25.
* Member order, deflate for every member including 0-byte ones, `index.search.pb` = 0 bytes (Test6
  and Test8 are GoodNotes-written files with exactly that), no `search/`, no `#103`–`#105`
  requirement (Test6/Test8 have none; the writer's `#105` per page is still harmless).
* The event sequence `#30 → #6 → #2 → #54 … → #10 → #102` with +1 sequence numbers (gaps are
  tolerated, consecutive is a subset).
* Blank-paper `#2` variant (`#5 1, #6 1, #8 canvas, #9 catalogue name, #12/#13 {#2 clock},
  #17 {#1 1, #2 clock}`), canvas = f32(MediaBox × 11/6) computed in double.
* `N = P + 1` (any `P`; use integer arithmetic if a random last digit `F` is allowed — GoodNotes
  does exactly that), `#54.#17` colour block, `#10` current page.
* `thumbnail.jpg` 499 px high at the first page's aspect.

May change / optional:
* Order keys: the writer's `"43" + base36` keys are fine (bytewise ASCII, same as GoodNotes'
  compare; alphabet is unrestricted). If an iPad test ever rejects them, mimic
  `<4 random alnum><digit>` for the initial pages and `A…`, `F…`, `K…`, `O…` for appended ones.
* `document.info.pb`: 0 bytes may be added (Test9) or omitted (Test6–8) — both are current.
* For a user PDF used as the page background the two GoodNotes forms are (a) "paper template":
  `#6 1`, `#9 = <some UUID string>`, `#12`/`#13`/`#17` registers as for blank paper, or (b)
  "inserted page": no `#6`, no `#9`, `#17 {#2 clock}` only. The writer currently emits (a)-like
  events with a catalogue-style name; either should open, (b) is what GoodNotes writes for
  "insert PDF page" and is the closer imitation for imported Notability PDFs. Attachment id and
  storage name may stay equal (Figma/strip imports do).
* Do not emit `#3`; bind the final template in `#54` directly.

## 13. Open questions

1. `#2.#5` as a 1-based PDF page index: still unobserved (> 1 never occurs; every imported PDF in
   the corpus is one page). A multi-page import sample would settle it.
2. Meaning of `#18.#3 = 0.9167` and of `#2.#6 = 1` (present for catalogue and template-imported
   papers, absent for inserted pages).
3. Meaning of `#56.#4` (fresh UUID, unreferenced) and whether GoodNotes ever purges deleted pages
   from `index.notes.pb`.
4. Whether the v5 ids of inserted/duplicated content are `uuid5(namespace, document id + …)`;
   only matters if a writer wants to look like an import.
5. `#55` reorder events and page duplication: no sample yet.
6. Whether GoodNotes accepts order keys that do not follow its `<4 random><digit>` /
   `A…F…K…O…` pattern (writer risk carried over from `goodnotes-container.md` §10.6).
