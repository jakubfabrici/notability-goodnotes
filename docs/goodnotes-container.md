# The `.goodnotes` container, byte level — enough to WRITE a file GoodNotes 6 should open

Status: reverse-engineered from real files (hex dumps + the two reference parsers). Nothing here
comes from GoodNotes documentation. Every claim is tagged with the evidence it rests on and a
confidence level. GoodNotes itself could not be run in this environment; the final check is the
user's iPad.

Evidence base (all files were produced by GoodNotes itself; dates are the event timestamps inside
the files):

| sample | made | schema.pb | pages | notes |
|---|---|---|---|---|
| `goodparse/samples/test2.goodnotes`, `test3.goodnotes` | 2024-11, `en_US` | 24 | 1 | A4 white paper, 1 / 2 dots. The smallest complete files (1650-byte event log). |
| `goodparse/samples/test.goodnotes` (= extracted `samples/test1/test`) | 2024-11, `de_DE` | 24 | 2 (1 deleted) | contains a *page deleted* event |
| `goodparse/samples/Test4.goodnotes` (+ `Test4.pdf` exported by GoodNotes on iOS 16.7) | 2026-06 | 24 | 2 | **0-byte `index.search.pb`, no `search/`, no `document.info.pb`**, one empty page |
| `goodparse/samples/Test5.goodnotes` | 2026-06 → 2026-09 | 25 | 3 | Test4 after more edits: added page, PNG image, text box, grouped strokes |
| `parser-for-goodnotes/assets/ex1..ex3.goodnotes` | 2025-02 → 2026-08, two devices | 24 / 25 / 24 | 1 | big pages (5 000+ records), PNG/JPEG images, `document.info.pb` present (0 bytes) |
| `parser-for-goodnotes/assets/record.goodnotes` | 2026-08 | 24 | 2 | audio recording (M4A attachment) |

Tools used: `goodnotes_re.wire` (parser-for-goodnotes) and `goodparse.protobuf` as schema-free
wire decoders, plus small census scripts kept in the session scratchpad
(`scratchpad/work/dump.py`, `events_schema.py`, `notes_census.py`, `build_min.py`).

Notation: `#n` = protobuf field number n. Wire types: `varint`, `fixed32` (float32 LE unless
said otherwise), `fixed64` (float64 LE), `LEN` (length-delimited: UTF-8 string, raw bytes or a
nested message). `{...}` = nested message. `clock` = the 8-byte register-version message
described in §2.3. Tag byte = `(field << 3) | wiretype` as a varint.

---

## 1. The ZIP container

### 1.1 Member list

A `.goodnotes` file is a plain ZIP (local headers + central directory, no comment, no extra
fields). Observed members, in the order GoodNotes writes them:

| member | present in | size seen | what it is | needed in a new file? |
|---|---|---|---|---|
| `document.info.pb` | ex1, ex2, ex3, record only | always **0 bytes** | unknown; never non-empty | **No** (absent in GoodNotes-written Test4/Test5 from 2026-06). Harmless to add as 0 bytes. |
| `index.search.pb` | all 9 | 0 bytes (Test4, test, test2, test3) or a record stream | list of `search/<UUID>` members | **Must exist; may be 0 bytes** (Test4 is a GoodNotes-written file with a 0-byte one). |
| `index.notes.pb` | all 9 | 83 bytes per page | page list: UUID → `notes/<UUID>` | **Yes** |
| `search/<UUID>` | files with non-empty index.search.pb | 4 B … 1 MB | OCR / handwriting-recognition results | No (only if listed in index.search.pb) |
| `notes/<UUID>` | all | **0 bytes for an empty page**, else a record stream | the ink layer of one page | **Yes, one per page listed in index.notes.pb** (0 bytes allowed) |
| `index.events.pb` | all 9 | 1.6 kB … 147 kB | the document's event log (creation of document, templates, pages, attachments, …). This is where pages are bound to templates, the canvas size lives, page order and deletions are recorded. | **Yes** |
| `thumbnail.jpg` | all 9 | 6–71 kB | JPEG preview of the first page, height 499 px | Present in every file; requirement unknown (see §8). Include one. |
| `index.attachments.pb` | all 9 | 89 bytes per attachment | attachment list: UUID → `attachments/<UUID>` | **Yes** (the paper template PDF is an attachment) |
| `attachments/<UUID>` | all | | PDF paper templates / imported PDFs, PNG/JPEG images, M4A audio. No extension; type is sniffed. | **Yes, at least the template PDF** |
| `schema.pb` | all 9 | 2 bytes | format version | **Yes** |

No directory entries are stored. Member order is as in the table (document.info.pb first when
present, schema.pb last); readers do not depend on order, but mimic it.

### 1.2 ZIP entry details (all members, all samples)

```
local header: 50 4b 03 04 | version needed 0x0014 (2.0) | flags 0x0800 (UTF-8 names; no data descriptor)
              | method 8 (deflate) for every member, including the 0-byte ones
central dir : version made by 0x0315 (UNIX, 2.1) | external attr 0x81a40000 (-rw-r--r--) | extra = empty
```
Python's `zipfile` with `ZIP_DEFLATED` produces the same thing (set `ZipInfo.create_system=3`
and `external_attr=0o100644<<16` to match exactly). Attachment entries keep their own mtime
(the time the PDF was generated), all other entries carry the save time. CRC-32 is the only
checksum in the whole format.

---

## 2. Encoding primitives

### 2.1 Protobuf wire format
Standard: varint = base-128 little-endian with continuation bit; `fixed32`/`fixed64` little-endian;
`LEN` = varint length + payload. Groups (wire types 3/4) never occur. Unknown fields are not
expected to break readers (the two reference parsers are schema-free), but a writer should only
emit what is documented here.

### 2.2 Record streams (length-prefixed framing)
`index.notes.pb`, `index.attachments.pb`, `index.search.pb`, `search/<UUID>`, `index.events.pb`
and `notes/<UUID>` are **not** a single message. They are a concatenation of

```
<varint L><L bytes of one protobuf message>  <varint L><L bytes>  ...
```
e.g. `index.notes.pb` begins `52 0a 24 ...` = record length 82, then field 1 (`0a`) of length 36 (`24`).
A 0-byte file is a valid, empty stream.

### 2.3 The version "clock" register
Nearly every mutable value is wrapped in a two-field message, used as a last-writer-wins version
stamp (CRDT-style):

```
{ #1 varint version   -- 1 on creation, +1 per modification (seen 1..17 on strokes, 2/3 after renames)
  #2 varint nonce     -- random uint32 (0..2^32-1), unique per register value }
```
Exact bytes for version 1: `08 01 10 <varint nonce>` (8 bytes when the nonce ≥ 2^28, otherwise
shorter — lengths 7 and 8 both occur). Some registers omit `#1` (`{#2 nonce}` only, e.g. the
page-colour register of the document and `#19` of templates); copy the observed shape per field.

### 2.4 Identifiers, time, devices, sequence numbers

* **UUIDs** are 36-char uppercase strings `XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX` (version 4 mostly;
  GoodNotes also emits v5-shaped ones). Always stored as `LEN` UTF-8, never as 16 raw bytes.
* **Timestamps** (`#10` of every event): `fixed64` **float64 = milliseconds since the Unix epoch**
  (`0x4279373138b9d67f` = 1732802481053.4 = 2024-11-28T14:01:21Z).
* **Device id**: a 63-bit random constant per device/installation, stored as varint
  (`6238500132173488370`, `1606161793633522096`, …). It appears in every event and in every
  element metadata record (`#8`). A file edited on two devices carries two ids (ex1).
* **Sequence number**: a 64-bit varint that increases by exactly 1 per event on a device
  (`1696603468623, …624, …625`). Its magnitude looks like a millisecond timestamp used as the seed.
  Writer: seed with `now_ms`, then `+1` per event.
* **Element counter** (`#9` of element metadata): a smaller per-device counter (`5872234`, `141468`,
  …), unique per element, not required to be increasing in file order.
* **Schema version**: varint 24 or 25 (§3). Every event and every element record repeats it.

---

## 3. `schema.pb`

Exactly one varint field: `08 18` = `{#1 = 24}` or `08 19` = `{#1 = 25}`.

* 24 in every file except Test5 and ex2 (25). In those two files only a handful of ink records carry
  `#21 = 25` (strokes made with a newer tool type), everything else still says 24, and `schema.pb`
  holds the **maximum** version used anywhere in the file (verified in Test5: its `#102` event that
  rewrote the notes file says 25, and schema.pb went 24 → 25 between Test4 and Test5).
* **Write `08 18` (24)** and use 24 in every record you emit. The GoodNotes 6 builds of 2026-06/08
  still write 24 for ordinary strokes, so 24 is the safest choice. (The parser-for-goodnotes wiki
  says schema.pb contains 35; that is wrong for all nine samples.)

---

## 4. `index.notes.pb` — page list

Record stream; one record per page, in creation order:

```
{ #1 LEN  page-notes UUID   (36 chars)
  #2 LEN  "notes/" + same UUID  (42 chars) }
```
Bytes of one record (82 + 1 length byte):
`52 | 0a 24 <36 ASCII> | 12 2a 6e 6f 74 65 73 2f <36 ASCII>`

Facts (all 9 samples):
* `#2` is always `"notes/" + #1`; the member always exists (0 bytes for a page without ink).
* The UUID here is the **notes-layer UUID** (`N`), not the page UUID (`P`) used in the event log.
  `N == P` except for the last hex digit, which is **P's last digit + 1** (14/14 pages:
  `…A9EB`→`…A9EC`, `…5D14`→`…5D15`, `…EC7A`→`…EC7B`). No example of a page UUID ending in `F`;
  choose page UUIDs ending in `0`–`E` so the `+1` never overflows. Both reference parsers match pages
  by the first 32 characters, so they are tolerant, but a writer should reproduce the `+1` rule.
* Deleted pages are **not** removed from this list (test.goodnotes still lists the deleted page);
  the deletion lives in the event log (§10.7).
* Display order is *not* taken from this list when the event log has order keys (§10.6); for a new
  file write the records in display order **and** give consistent order keys.

---

## 5. `index.attachments.pb` — attachment list

Record stream, one record per attachment member actually present (set equality verified in all 9):

```
{ #1 LEN attachment UUID (36)
  #2 LEN "attachments/" + UUID (48) }
```
`58 | 0a 24 <36> | 12 30 61 74 74 61 63 68 6d 65 6e 74 73 2f <36>`

**No MIME type, size or role is stored here.** Type is inferred from content (`%PDF-`, `\x89PNG`,
`\xff\xd8`, `....ftyp`); size and ownership are in the `#6` event (§10.3); the role (paper
template vs. image vs. audio) is established only by which event references the UUID (`#2`
template event → template PDF; image placement record in a `notes/` file → image; `#160` → audio).
Attachment UUIDs of recordings that were later deleted still appear in `#6` events although the
member and the index entry are gone (record.goodnotes), so readers must not assume every `#6`
has a member; a writer should keep index ⇔ members ⇔ `#6` events consistent.

---

## 6. `index.search.pb` and `search/<UUID>` — search index (optional)

`index.search.pb` record:
```
{ #1 LEN UUID            -- either an attachment UUID (PDF text) or a notes-layer UUID (handwriting)
  #2 LEN "search/" + UUID (43)
  #3 varint 1 }          -- present on attachment entries only (absent on notes entries)
```
`55 | 0a 24 <36> | 12 2b 73 65 61 72 63 68 2f <36> | 18 01` (attachment) or `53 | … ` without `18 01` (notes).

`search/<UUID>` for an attachment is a record stream with one record
`{#1 LEN text (e.g. 65 spaces), #2 varint 1, #3 LEN ""}`, minimal form `04 10 01 1a 00` (5 bytes,
no `#1`). For a notes layer it is a single message `{#5 LEN page UUID P, #6 {recognition result:
bounding boxes as {#1{#1 f32,#2 f32},#2{…}}, strings, stroke references …}}` (record.goodnotes,
4917 bytes) — contents are irrelevant to a writer.

**Required?** No. Test4 (written by GoodNotes 6, 2026-06) has a 0-byte `index.search.pb`, no
`search/` members and no search-related events (`#103`, `#104`, `#105`). Write a 0-byte
`index.search.pb` and nothing else; GoodNotes rebuilds the index itself (Test5 = Test4 reopened,
gained `search/` members and `#103`/`#105` events).

---

## 7. `document.info.pb`

Always 0 bytes when present; absent in the two most recent GoodNotes-written samples. Omit it
(or write 0 bytes). Confidence that it is optional: high (Test4/Test5 lack it).

---

## 8. `thumbnail.jpg`

Baseline JPEG, JFIF 1.01 + EXIF + Photoshop APP13 (iOS ImageIO output), 3 components, 4:2:0,
restart interval 25. Dimensions: height **499 px**, width = 499 × page aspect (386 for 455.04×588.45,
353 for A4). It is a render of the first page including the paper. Requirement for import is
unknown (present in 9/9 files); include one — any baseline JPEG of the right aspect should do
(copying a thumbnail from a sample is acceptable for testing; a white JPEG is safer than none).
Confidence: medium.

---

## 9. `attachments/<UUID>`

### 9.1 Paper template PDF (mandatory for a page)
Every page is bound (via a template entity, §10.4) to a one-page PDF. GoodNotes' own templates are
tiny `svg2pdf` PDFs: `/Producer (svg2pdf)`, PDF-1.7, one page, `/MediaBox [0 0 W H]`, a
FlateDecode content stream that fills the page with the paper colour (and draws lines for ruled
paper via Form XObjects). Example: `Test4.goodnotes/attachments/B9F5E2EA-…` (950 bytes, blank
"Blue" paper, 455.04 × 588.45 pt). A4 templates use `[0 0 595.28 841.89]`.

**The page size is the PDF MediaBox.** The ink **canvas size** stored in the template event is
`MediaBox × 132/72` (= × 11/6), float32:

| MediaBox (pt) | canvas `#8` (f32) |
|---|---|
| 455.04 × 588.45 | 834.239990234375 × 1078.824951171875 |
| 595.28 × 841.89 | 1091.3466796875 × 1543.4649658203125 |

All stroke/image/text coordinates in `notes/` files are in canvas units (origin top-left, y down),
i.e. `canvas = pdf_points × 11/6`. Confidence: high (three independent sizes, both parsers).

Imported multi-page PDFs are also attachments; each page then has its own template entity with
`#5` = 1-based PDF page index (per parser-for-goodnotes; only index 1 observed here).

### 9.2 Images
PNG or JPEG bytes as-is (1536×2048 PNG, 2048×199 PNG, 1696×1286 JPEG seen). Declared by a `#6`
event (`#12` = empty) and referenced from an image element in the page (§11.5).

### 9.3 Audio
M4A (`ftyp` box) bytes; declared by `#6` (`#12` empty) and bound by `#160` events. Not needed.

---

## 10. `index.events.pb` — the event log

### 10.1 Envelope
Record stream. Every record is

```
{ #1  LEN  entity UUID          -- the object the event is about (document, template, attachment, page or notes layer)
  #E  LEN  { event body } }     -- exactly one more field; its NUMBER E is the event type
```
Event tags seen as bytes: `#2`=`12`, `#6`=`32`, `#10`=`52`, `#15`=`7a`, `#30`=`f2 01`, `#31`=`fa 01`,
`#34`=`92 02`, `#54`=`b2 03`, `#56`=`c2 03`, `#102`=`b2 06`, `#103`=`ba 06`, `#104`=`c2 06`,
`#105`=`ca 06`, `#106`=`d2 06`, `#160`=`82 0a`, `#163`=`9a 0a`.

Counts over the nine samples: `#2`×12, `#6`×25, `#10`×11, `#15`×19, `#30`×9, `#31`×3, `#34`×1,
`#54`×14, `#56`×1, `#102`×11, `#103`×3, `#104`×17, `#105`×18, `#106`×3, `#160`×7, `#163`×6.

### 10.2 Common trailer of every event body
```
#10 fixed64  float64 ms since epoch
#11 LEN      event UUID (fresh random UUID per event)
#d  varint   device id          ┐ three consecutive field numbers d, d+1, d+2;
#d+1 varint  sequence number    │ d = 13 for most types, 14 for #6, 15 for #2, 17 for #104, 18 for #103
#d+2 varint  schema (24)        ┘ (#30 uses 13, 14 and then 20)
```
Field numbers are per event type (tables below). The sequence number must be strictly increasing
across the whole file (observed +1 per event); timestamps non-decreasing.

### 10.3 Event catalogue

Legend: **R** = emit in a new file; **O** = optional (observed, not needed); `?` = semantics inferred.

**`#30` — document created (R).** Entity = document UUID `D`.
```
#1  LEN D
#2  {#1 LEN name, #2 clock}                      -- document title ("Untitled Notebook (1)")
#3  {#1 LEN "5A53E89E-F4C2-4548-8DD3-E9DF9FB4592E", #2 clock}   -- constant in 7/9 files (ex1/ex3 differ: cover?) ?
#6  {#1 LEN "P", #2 clock}                       -- "P" in 9/9 files; orientation Portrait? ?
#7  {#1 LEN "5A53E89E-F4C2-4548-8DD3-E9DF9FB4592E", #2 clock}   -- constant in 9/9 files ?
#9  LEN "en_US" | "de_DE" | "auto"              -- handwriting-recognition language ?
#10 fixed64 ts, #11 LEN event uuid, #13 varint device, #14 varint seq
#17 LEN ""   #18 LEN ""                          -- always empty
#19 {#2 clock}                                   -- register without value
#20 varint 24
```
Copy the two constant UUIDs verbatim; their meaning is unknown but every file has them.

**`#31` — document renamed (O).** `{#1 D, #2 {#1 new name, #2 clock(version 2,3,…)}, #10, #11, #13, #14, #15 = 24}`.

**`#34` (O, 1×).** `{#1 D, #2 {#2 clock(version 2)}, trailer 10/11/13/14/15}` — unknown.

**`#6` — attachment added (R, one per attachment).** Entity = attachment UUID `A`.
```
#1 LEN A          #2 LEN A (same)
#5 varint  byte size of attachments/A     -- == ZIP member size in 25/25 cases (consistency rule!)
#6 LEN D                                   -- owning document
#10 ts, #11 event uuid
#12 LEN  {#1 varint 1, #2 varint 1}  for PDFs  (bytes 08 01 10 01)   -- page count? ?
         ""  (empty)                 for PNG/JPEG/M4A
#14 device, #15 seq, #16 schema
```

**`#2` — template (paper) entity created (R, one per distinct paper/PDF page).** Entity = template UUID `T`.
```
#1 LEN D            #2 LEN T            #4 LEN A (the PDF attachment)
#5 varint 1         -- 1-based PDF page index (parser-for-goodnotes); only 1 observed
#6 varint 1         -- always 1 ?
#7 fixed64 29.333333  -- OPTIONAL: ruled line spacing (canvas units); present for lined papers only
#8 {#1 fixed32 canvas W, #2 fixed32 canvas H}        -- MediaBox × 11/6, see §9.1   **(the canvas size)**
#9 LEN name  e.g. "417A3734-B870-5E89-8055-DA029834F25E_standard_1_1 - Blue",
                  "9FE8F365-4BEE-5057-8573-1A56C77CAC19_a4_1_2 - White",
                  "CC365888-75C9-5DBF-B1D4-A7E8E0AD3B7F_standard_1_2 - White"
                  -- "<builtin template UUID>_<size>_<?>_<?> - <colour>"; free text as far as parsers care
#10 ts, #11 event uuid
#12 {#1 fixed64 29.333 (lined only), #2 clock}     -- blank paper: {#2 clock} only
#13 {#1 varint 1 (lined only),       #2 clock}     -- blank paper: {#2 clock} only
#15 device, #16 seq
#17 {#1 varint 1, #2 clock}
#18 OPTIONAL lined-paper layout: {#1 {#1 {#1 f32 44.0, #2 f32 58.667}, #2 {#1 f32 746.24, #2 f32 961.49}},
                                  #2 f32 28.4167, #3 f32 0.91667, #5 varint 1}
     -- #1 = writable rect (origin, size) inset in the canvas; absent for blank paper
#19 {#2 clock}
#21 varint 24
```
For a new file use the blank variant (no `#7`, no `#18`, `#12`/`#13` = `{#2 clock}`), exactly as
Test4's "Blue" template (record 2 of its event log, bytes 560–908).

**`#54` — page created (R, one per page).** Entity = page UUID `P`.
```
#1 LEN D            #2 LEN P
#3 {#1 LEN T, #2 clock}                  -- the page's template  **(page → template → PDF binding)**
#4 {#1 LEN order key, #2 clock}          -- sort key, §10.6
#10 ts, #11 event uuid, #13 device, #14 seq, #15 schema 24
#17 {#1 {#2 {#1 {#1 f32 0.8666667, #2 f32 0.8666667, #3 f32 0.8666667, #4 f32 1.0},
              #2 {#1 f32 1.0, #2 f32 1.0, #3 f32 1.0, #4 f32 1.0}}}, #2 clock}
     -- two RGBA colours (light grey, white); the 48-byte #17.1 payload is BYTE-IDENTICAL in all
        10 occurrences (2025–2026 files) and absent in the 2024 files. Copy it:
        0a2e122c0a140ddedd5d3f15dedd5d3f1ddedd5d3f250000803f12140d0000803f150000803f1d0000803f250000803f
```

**`#102` — notes layer (ink file) written (R for every page that has ink).** Entity = notes UUID `N`.
```
{#1 LEN N, #10 ts, #11 event uuid, #13 device, #14 seq, #15 schema used for notes/N, #16 LEN D}
```
Emitted when `notes/N` is first written (not for empty pages: Test4's empty page has none) and
again when rewritten with a newer schema (Test5). Cheap — emit one per non-empty page.

**`#10` — current page pointer (O, present in 9/9).** Entity = D.
`{#1 D, #2 LEN P (page, not notes, UUID), #3 LEN "PagingViewServiceUpdater:" + random UUID, 10/11/13/14/15}`.
Last viewed page. One stale pointer to a non-existent page exists in ex1, so GoodNotes tolerates it.

**`#56` — page deleted (O).** Entity = P. `{#1 D, #2 LEN P, #3 {#1 varint 1, #2 clock}, #4 LEN uuid (unknown), 10/11/13/14/15}`.
The page's `index.notes.pb` entry and `notes/` member remain; readers hide the page.

**`#15` (O, ex1/ex3).** `{#1 D, #2 LEN image attachment UUID, #3 varint 2, #4 fixed64 1.0, 10/11/13/14/15}` — unknown, image-related ?

**`#103` / `#104` / `#105` / `#106` (O) — search / recognition bookkeeping.**
`#103 {#1 A, #2 D, #14 varint 1, #16 uuid, #18 dev, #19 seq, #20 24}` after a PDF was text-indexed;
`#104 {#1 D, #2 N, #12 varint 64-bit hash, #13 LEN lang, #15 uuid, #17 dev, #18 seq, #19 24}` handwriting recognised;
`#105 {#1 varint 1 (notes) , #2 D, #3 LEN A | #4 LEN N, #5 varint hash, #6 LEN lang, 10/11/13/14/15}` search entry updated;
`#106 {#1 N, repeated #2 {#1 element UUID, #2 clock}, 10/11/13/14/15, #16 D}` = inventory of all
elements on the page with their clock versions (ex2: 25 entries = 25 elements).
These accompany `search/` members and are absent from Test4; do not emit. (goodparse's docstring
treats `#105` as "paper applied to page"; the dumps show it is the search-index update of an
attachment or notes layer — test2 has a paper but no `#105`.)

**`#160` / `#163` (O) — audio recording start/stop.** `#160 {#1 recording uuid, #2 A (M4A), #3 D, #4 varint ticks,
#5 "" | repeated {#1 stroke uuid, #2 {#1 varint ts, #2 LEN P}}, #6 {#2 clock}, …}`, `#163 {#1 recording uuid, #2 D, …}`.

### 10.4 How a page gets its paper (the binding chain)
```
index.notes.pb : N ──(N = P + 1 in last hex digit)──▶ event #54 (entity P): #3.#1 = T
event #2 (entity T): #4 = A, #5 = pdf page, #8 = canvas size
index.attachments.pb : A → attachments/A  (PDF, MediaBox = page size)
```
Verified for all 14 pages. There is no page-size field anywhere outside the PDF and the canvas
pair; the notes file carries no page metadata at all (§11).

### 10.5 Canvas size
Only in `#2.#8` (two float32). Must equal MediaBox × 11/6 for strokes to land where expected.

### 10.6 Page order keys (`#54.#4.#1`)
Short ASCII strings compared lexicographically (parser-for-goodnotes; consistent with every
sample): one page → `4Qb_W`, `4ES6R`, `4rKOY`, `4gn8n`, `AnFq6`; two pages created together →
`43~lY1`,`43~lY2` and `4Ka3C1`,`4Ka3C2`; a page appended later → `AntJC`, `Af~Am` (sorts after
`4…` in ASCII). Alphabet seen: `0-9 A-Z a-z _ ~` (looks like a fractional-index scheme over printable
ASCII). Reordering pages appends a `#55 {#2 P, #3 {#1 new key, #2 clock}}` event (per
parser-for-goodnotes; not in these samples). Recommendation for a writer: fixed-width keys using
only digits and uppercase letters (e.g. `A0001`, `A0002`, …) so that ASCII order == intended order
== order in `index.notes.pb`. Risk: if GoodNotes validates key syntax, mimic the observed shape
(`<5 random chars>` + digit) instead. Confidence that lexicographic ASCII compare is used: medium.

### 10.7 Deleted pages
`#56` with `#2 = P`. Everything else stays. A writer never emits it.

### 10.8 Minimal event sequence for a new document (as built and validated, §14)
```
#30 document  →  #6 attachment (template PDF)  →  #2 template  →  #54 page (×N)
→  #10 current page (optional)  →  #102 notes layer (×N non-empty pages)
```
Sequence numbers +1 in this order; timestamps now_ms (non-decreasing).

---

## 11. `notes/<N>` — one page's ink layer

### 11.1 Stream structure
Record stream (§2.2). A page without content is a **0-byte member**. Otherwise the stream is a
sequence of **element pairs**: a *metadata record* immediately followed by a *content record* for
the same element UUID (verified for all 5 677 elements in the corpus; no other arrangement seen).
There is **no page-level metadata record**: no page size, no template reference, nothing but
elements. Element order = drawing (z-) order.

### 11.2 Metadata record (one per element)
```
{ #1  LEN   element UUID
  #2  clock {#1 version, #2 nonce}         -- the element's version register
  #3  varint 1       OPTIONAL              -- tombstone/erased (parser-for-goodnotes skips these; paired 1:1 with content #14=1). Never emit.
  #4  LEN   attachment UUID  OPTIONAL      -- image elements only (the picture's attachments/ member)
  #6  LEN   UUID             OPTIONAL      -- seen only in ex2 (20/25 elements); parent/layer? Never emit.
  #8  varint device id                     -- same value as the events' device id (verified 9/9)
  #9  varint element counter (per device, unique per element)
  #14 varint 5381                          -- constant in every 2025/2026 file, absent in 2024 files. Emit 5381.
  #16 varint 24 }                          -- schema
```
Byte image of the test2 metadata record (66 bytes + 1-byte length):
`42 | 0a 24 <36> | 12 08 08 02 10 a6 db 92 aa 09 | 40 <varint device> | 48 ea b4 e6 02 | 80 01 18`.

### 11.3 Content record: ink stroke (`#7`)
Exactly one top-level field `#7` (tag `3a`) whose payload is:
```
{ #1  LEN  element UUID (== metadata #1)
  #2  LEN  compressed geometry: Apple LZ4 frame (§11.4)
  #3  varint tool type  OPTIONAL   -- 1 (pen) in the 2024 and Test4 files; absent on most 2025 strokes; 4/5 appear only with #21=25
  #4  LEN  colour {#1 f32 R, #2 f32 G, #3 f32 B, #4 f32 A}; components equal to 0.0 are OMITTED
            (black opaque = {#4 1.0} = bytes 22 05 25 00 00 80 3f)
  #5  varint 1  OPTIONAL           -- seen on translucent (alpha 0.5) strokes: highlighter flag ?
  #6  LEN  ""  or {#1 f32 dx, #2 f32 dy}   -- lasso-move offset added to every point (goodparse/parser-for-goodnotes)
  #7  LEN  {#1 {#1 varint version, #2 varint nonce [, #3 varint index]}}
            -- a second clock; unique per stroke, or shared by all members of a group (then #3 = member index)
  #9  LEN  ""  (35–59-byte message on a few strokes: dash/shape style ?)
  #10 LEN  group UUID  OPTIONAL    -- strokes drawn as one group (same #7 clock)
  #14 varint 1  OPTIONAL           -- paired with metadata #3 = 1 (tombstone). Never emit.
  #15 clock                        -- MUST equal metadata #2 (5 677/5 677)
  #20 LEN  ""                      -- (once b"\n\x00")
  #21 varint 24 }                  -- schema
```
Minimal stroke content record actually written by GoodNotes (test2, black dot):
`#1 uuid, #2 blob(139 B), #3=1, #4={#4 1.0}, #6="", #7={#1{#2 nonce}}, #9="", #15 clock, #20="", #21=24`.

### 11.4 Framing of the geometry blob (`#7.#2`) — reference only
```
"bv41" u32LE uncompressedSize u32LE compressedSize <LZ4 block>   (repeat; "bv4-" = stored block)
"bv4$"                                                           end marker
[optional protobuf trailer after bv4$ : #4 colour, #6 move offset (parser-for-goodnotes stroke.py)]
```
Decompressed payload = a Troy-Hanson TPL image: `"tpl" 0x00 | u32LE total size | NUL-terminated
format string | values`. Pen strokes: format `vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)`, points as
float32 bit patterns in the `A(u)` arrays (x, y, pressure/width at stride 12 → goodparse), newer
pens `vuA(v)A(S(uu))A(S(uuuu))vA(f)`. See parser-for-goodnotes `compression.py`, `tpl.py`,
`stroke.py` and goodparse `goodnotes.py` for the point layouts; out of scope for this document.

### 11.5 Image element (two records)
```
metadata : {#1 E, #2 clock, #4 LEN A, #8, #9, #14 5381, #16 24}
content  : {#1 LEN {#1 E, #2 {#1 {#1 f32 x, #2 f32 y}, #2 {#1 f32 w, #2 f32 h}},     -- placement: top-left + size (canvas units)
                    #3 {#1 {#1 f32 cx, #2 f32 cy}, #2 {#1 f32 cw, #2 f32 ch}},        -- crop centre + crop size (== x+w/2, y+h/2 when uncropped)
                    #4 LEN A, #5 {#1 clock}, #15 clock, #18 varint 24}}
```
Top-level field is `#1` (tag `0a`), e.g. Test5 page 3 records 24–25 (1536×2048 PNG placed at
(205.5, 432.5) size (209.3, 279.1)).

### 11.6 Text box (top-level `#8`)
```
{#1 E, #2 rect{origin,size}, #3 rect (inner frame), #4 {#1 1.0, #4 1.0}, #5 {#1 clock},
 #6 LEN RTF bytes ("{\rtf1\ansi\ansicpg1252\cocoartf…"), #7 {#1 1.0,#2 1.0,#3 1.0}, #9 {…}, #10 f32 10.0,
 #15 clock, #18 {#2 f32 5.0}, #19 {#4 f32 0.2}, #20 LEN ".tb-0", #21 "", #27 varint 24}
```
(Test5 page 3 record 33; GoodNotes 6 also has an LZ4 "type 35" rich-text form, see parser-for-goodnotes `text.py`.)

### 11.7 Other element types
Only `#7` (ink), `#8` (text) and `#1` (image placement) occur as top-level content fields in the
eleven sampled pages. Shapes (parser-for-goodnotes: fields 9/21/22, type codes 31/35) and sticky
notes were not present in this corpus.

### 11.8 Minimal new page file
```
0 bytes                                            -- for a page with no ink, or
for each stroke: record(metadata §11.2 without #3/#4/#6) + record(content §11.3 without #3?/#5/#10/#14)
```
i.e. per stroke: `{#1 E, #2 clock, #8 dev, #9 n, #14 5381, #16 24}` then
`{#7 {#1 E, #2 blob, #3 1, #4 colour, #6 "", #7 {#1 {#1 1, #2 nonce}}, #9 "", #15 same clock, #20 "", #21 24}}`.

---

## 12. UUID cross-reference map

| UUID | defined by | referenced from |
|---|---|---|
| document `D` | `#30` entity/#1 | `#6.#6`, `#2.#1`, `#54.#1`, `#10.#1`, `#102.#16`, `#31`, `#56.#1`, `#104/#105/#106` |
| attachment `A` | `index.attachments.pb #1`, member name, `#6` entity | `#2.#4` (paper), image metadata `#4` + placement `#4`, `#160.#2` (audio), `index.search.pb` |
| template `T` | `#2` entity/#2 | `#54.#3.#1` |
| page `P` | `#54` entity/#2 | `#10.#2`, `#56.#2`, `search/<N>.#5`, `#160.#5[].#2.#2`; `N = P` with last hex digit +1 |
| notes layer `N` | `index.notes.pb #1`, member `notes/N` | `#102` entity/#1, `#104.#2`, `#105.#4`, `#106.#1`, `search/N` name |
| element `E` | metadata `#1` | content `#1`, `#106.#2[].#1`, `#160.#5[].#1` |
| event uuid | `#11` of each event | nowhere |

---

## 13. Consistency rules a writer must honour

1. `index.notes.pb` entries ⇔ `notes/` members (0-byte allowed); `index.attachments.pb` entries ⇔ `attachments/` members.
2. `#6.#5` == exact byte size of the attachment member (25/25).
3. Every page: `#54` with `#3.#1` naming a `#2` template whose `#4` names a `#6`-declared attachment that is a PDF with a MediaBox; `#2.#8` = MediaBox × 11/6 (float32).
4. `N` (notes/index) = `P` (events) + 1 in the last hex digit; `#102` keyed by `N`, `#10`/`#56` keyed by `P`.
5. Per element: metadata `#1` == content `#1`; metadata `#2` == content `#15` (same 2 varints); records adjacent.
6. Metadata `#8` == a device id used in the event log; sequence numbers strictly increasing; timestamps ms-epoch doubles.
7. `schema.pb` == max schema value used in any record (use 24 everywhere → `08 18`).
8. UUIDs uppercase, 36 chars; order keys ASCII-sortable in the intended page order.
9. No hashes or checksums other than ZIP CRC-32; clock nonces are random uint32 (uniqueness per register is what GoodNotes produces); the 64-bit hashes in `#104/#105` belong to the optional search index.

---

## 14. Minimal writable file (recipe) and validation

Members, in this order, deflate-compressed:
```
index.search.pb        0 bytes
index.notes.pb         record {#1 N, #2 "notes/"+N}                                    (83 B per page)
notes/<N>              element pairs (§11.8) or 0 bytes
index.events.pb        #30, #6, #2, #54 (per page), #10, #102 (per non-empty page)      (§10.8)
thumbnail.jpg          JPEG, 499 px high, page aspect
index.attachments.pb   record {#1 A, #2 "attachments/"+A}                              (89 B)
attachments/<A>        one-page PDF, /MediaBox [0 0 W H]  (e.g. the 950-byte "Blue" template from Test4)
schema.pb              08 18
```
A builder implementing exactly this (`scratchpad/work/build_min.py`, 1 page, 1 stroke whose LZ4
blob is copied from test2, random UUIDs/device/sequence/nonces, 455.04×588.45 blank template)
produced a 1647-byte event log and a file that both reference parsers read back correctly:
goodparse → 1 page 455.04×588.45, canvas (834.2, 1078.8), scale 0.5455, 1 stroke at (484.3, 362.7);
parser-for-goodnotes → 1 page, `PageDimensions(455.04, 588.45)`, background = the template
attachment, stroke at the same point. (goodparse reports no background only because its
`_read_page_backgrounds` heuristic keys on `#105` search events — it also fails on GoodNotes' own
test2.) `zipfile.testzip()` passes.

What this does **not** prove: that GoodNotes 6 accepts it. Things the iPad test should vary, in
order of suspicion:
1. the order-key format (§10.6) — fall back to `<5 random chars><digit>`;
2. whether `#10` / `#102` / `#54.#17` / the `#30` constants may be omitted (keep them all first);
3. whether `thumbnail.jpg` may be omitted or synthetic;
4. whether the metadata device id must match an events device id (keep it equal);
5. whether the `N = P+1` rule matters (keep it);
6. whether `document.info.pb` must exist on some platforms (Test4/Test5 say no; add 0 bytes if an import fails).

---

## 15. Corrections to the reference material
* parser-for-goodnotes wiki 02: "schema.pb … value 35". Observed: 24 (7 files) and 25 (2 files).
* goodparse `goodnotes.py` `_read_page_backgrounds`: `#105` is **not** "paper applied"; it is the
  search-index update event (`#3` = attachment or `#4` = notes layer, `#5` hash, `#6` language). The
  page→paper binding is `#54.#3.#1 → #2.#4` (§10.4).
* goodparse treats `index.notes.pb #1` as the member name — correct — but the event log uses the
  page UUID `P`, hence the "one nibble differs" note in both parsers (§4).
* parser-for-goodnotes `archive.py` calls `#54` "page created, order key at #4" and `#55` "page
  reordered, key at #3"; `#54` confirmed, `#55` not present in this corpus.

## Critic additions

Checked 2026-10-02 (`scratchpad/critic/clocks.py`, `events105.py`; inkterop's
`docs/formats/goodnotes.md` and `writer.py` read for comparison).

1. **§11.8 recipe trap**: the minimal stroke content record is written with `#3 1`. That is
   only correct when the LZ4 blob is a *ribbon* TPL (`vA(v)A(u)…`, as the test2 blob that
   `build_min.py` copies). For the flat constant-width format (`vuA(v)A(S(uu))A(S(uuuu))vA(f)`,
   the one a converter should write) `#3` must be **absent** — 5 619/5 619 flat strokes have no
   `#3`, and inkref reports that a mismatch "silently never renders" in GoodNotes 7 (Mac).
2. **Clock version**: §2.3 says "1 on creation"; the minimum observed on any element in the 9
   files is **2** (untouched strokes are at 2 in every file, inkterop sees the same). Both 1
   (inkterop, accepted by GoodNotes Mac 6) and 2 (what GoodNotes writes) are therefore safe;
   prefer 2. The nonce is distinct on every one of the 5 677 elements (random uint32).
3. **Metadata `#8` = device id** is confirmed: one value per file, equal to an event-log device
   id, 9/9 (the stroke doc's "creation time in ns" reading is withdrawn there).
4. **"schema-24 flat vs schema-25 journal" is a false dichotomy.** inkterop's doc describes
   schema-24 page files as "one metadata record then stroke records"; in all nine
   GoodNotes-written files (schema 24 *and* 25) every element is a (metadata, content) pair
   (§11.1), and inkref's schema-24 pair stream imports and renders in GoodNotes 7.0.34. inkterop's
   `SwiftProtobuf.BinaryDecodingError` came from its own unpaired stream, not from schema 24.
   Write pairs with `schema.pb = 08 18`; no A/B test is needed on this point.
5. **`#105`** has two shapes: `{#2 D, #3 A, trailer}` after a PDF attachment, and
   `{#1 1, #2 D, #4 N, [#5 u64 hash], #6 "auto", trailer}` for a notes layer. `#1` is **1 for
   both pages of record.goodnotes**, so it is not a page number (inkterop writes `i+1` there;
   GoodNotes Mac 6 accepted it). `#5` is absent on 9 of the 11 notes-form events in
   record.goodnotes, so no hash is required. Test4 (GoodNotes iPad 2026-06, two pages) has no
   `#105` at all; inkterop found that GoodNotes Mac 6 shows zero pages when `#2`, `#54` and
   `#105` are all missing but did not isolate `#105`. Cheap insurance for a writer: emit one
   `{#1 1, #2 D, #4 N, #6 "auto", #10 ts, #11 uuid, #13 dev, #14 seq, #15 24}` per page after
   the `#54` events, exactly the shape GoodNotes itself writes without `#5`.
6. **`#30.#3` / `#30.#7`** are not validated against the page list: ex1 and ex3 carry UUIDs
   (`12AEC24B-…`, `C1179D7B-…`) that are neither a page, notes layer, template nor attachment of
   the file, and inkterop writes the first page's UUID in both and imports fine on Mac 6. Copy the
   constant `5A53E89E-F4C2-4548-8DD3-E9DF9FB4592E`.
7. **`#2.#9` template name and PDF content are not cross-checked** (GoodNotes Mac 6 imported
   inkterop's reportlab blank PDF named `9FE8F365-…_standard_1_1 - Yellow` with `#7`/`#18`
   lined-paper fields present). **`thumbnail.jpg`**: a 4×4 white JPEG was accepted by GoodNotes
   Mac 6 (inkterop), so a fixed embedded JPEG suffices on that platform.
8. The two reference parsers were re-run on nothing new here; items 1–7 come from the sample
   bytes and from inkterop's documented Mac-app iterations (MIT, 2026-07-09/10).
