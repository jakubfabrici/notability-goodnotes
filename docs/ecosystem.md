# Ecosystem and feasibility checks

Scope: native import/export in GoodNotes and Notability, existing `.goodnotes`
writers, Apple framed LZ4 (`bv41`), Pyodide in the browser (incl. iPad Safari),
and licensing. Date of research: 2026-10-02. Everything marked **[verified]**
was checked against code, bytes or a primary document fetched today;
**[inferred]** is a conclusion from consistent evidence without a direct
experiment; **[unknown]** is open.

Working copies used below live under
the research scratch directory (not part of the repository)
(`ref/`, `pyo/`); the GoodNotes samples are `ref/goodparse/samples/*.goodnotes`
and `ref/parser-for-goodnotes/assets/*.goodnotes`.

## TL;DR

| Question | Answer | Confidence |
|---|---|---|
| Does GoodNotes 6 import Notability `.note` with editable ink? | **No.** Import list (article updated 2026-05-22): PDF, JPG/PNG, DOC/DOCX, PPT/PPTX (iOS only), `.goodnotes`, `.goodnotes.zip`, CSV/TSV (flashcards). Android/Windows/Web: PDF, images, `.goodnotes`. | verified (support article) |
| Does Notability import `.goodnotes`? | **No.** Import article (updated 2026-09-03) lists PDFs, PowerPoints, spreadsheets, bulk folders, and Notability's own `.note`; nothing third-party. | verified |
| Any editable interchange format exported by either app? | **No.** GoodNotes exports `.goodnotes` (iOS/Mac only), PDF, image; Android/Windows/Web export PDF only. Notability exports PDF, Note (`.note`), JPEG, PNG, NTB (Notability Cloud). No SVG/InkML/XOPP on either side. | verified |
| Existing software that *writes* `.goodnotes`? | **Yes, three**: `juliank1m/inkref` (Python, **confirmed imports and renders in Goodnotes 7.0.34 macOS**, no license file), `cable729/inkterop` (MIT, writer exists; same-format round-trip imports in GoodNotes Mac 6, foreign ink does not render yet), `Jojoistauchdabei/notes` "Federwerk" (JS, no license, never tested against the app). All three cloned under `ref/`. | verified |
| Apple framed LZ4 | `bv41` + u32 raw + u32 comp + standard LZ4 block; `bv4-` + u32 size + raw bytes; `bv4$` end. All LE. Apple's own docs confirm; 5 677/5 677 real GoodNotes blobs are single `bv41` block + `bv4$`. All-literal LZ4 block encoder below verified against 3 decoders + python-lz4. | verified |
| Pyodide feasible? | **Yes.** 314.0.7 (CPython 3.14.2, 2026-09-14), `https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs`; `plistlib/zipfile/zlib/struct/uuid/hashlib` present (tested); goodparse + plistlib round trip ran under Pyodide in ~0.6 s with a 54 MB wasm heap. Must use a **module** worker. iPad: a[download] works since iOS 13; share-sheet "Save to Files" via `navigator.share({files})` since Safari 15. | verified (Node run + docs) |
| Licensing | goodparse is GPL-3.0: importing it makes the distributed converter GPL. Use MIT sources (parser-for-goodnotes, inkterop) and format *facts* from inkref; write our own reader/writer under MIT. The current PoC imports goodparse and is therefore GPL-bound as-is. | verified (GNU FAQ) |

---

## 1. Native import/export in the two apps (2025–2026)

### 1.1 GoodNotes

Fetched via the Zendesk article API (`support.goodnotes.com/api/v2/help_center/en-us/articles/<id>.json`).

**"Import files into Goodnotes"** (id 7353717816463, `updated_at` 2026-05-22)
<https://support.goodnotes.com/hc/en-us/articles/7353717816463-Import-files-into-Goodnotes>

> Supported formats on iOS: PDF; Image (.jpg, .png); Word (.doc, .docx) and
> PowerPoint (.ppt, .pptx) (iPadOS/iOS only); Goodnotes document (.goodnotes);
> Goodnotes backup (.goodnotes.zip); Comma- or Tab-Separated Values file
> (.csv, .tsv) [flashcards].
> Supported formats on Android, Windows and Web: PDF; Image (.jpg, .png);
> Goodnotes document (.goodnotes) – Study Set is not supported.
> […] the import file size limit is 1GB per PDF file, and 200Mb per Goodnotes
> file (.goodnotes). Free users have a limit of 3 notebooks, the import file
> size limit is 5Mb per file.

No mention of Notability, `.note`, `.ntb`, SVG, InkML or XOPP. The companion
article "How to import documents?" says that for other apps "you will need to
export all the notes in that app as PDFs" (search snippet; not independently
fetched).

**"Export documents or pages"** (id 7353742824975, updated 2026-05-22)
<https://support.goodnotes.com/hc/en-us/articles/7353742824975-Export-documents-or-pages>

> [iOS] The supported file formats for the export are: Goodnotes document –
> can only be opened in Goodnotes; PDF (editable/flattened); Image.
> [Android, Windows, and Web] The app allows you to effortlessly export
> individual notebooks as PDF file.

Consequences for us **[verified]**:
- A `.goodnotes` we write can be imported on **every** GoodNotes platform
  (iPad, iPhone, Mac, Android, Windows, Web). Free tier: 3 notebooks, 5 MB.
- Only iOS/Mac can *produce* `.goodnotes` (our GoodNotes→Notability input).

### 1.2 Notability

**"Importing Files"** (id 206061357, updated 2026-09-03)
<https://support.gingerlabs.com/hc/en-us/articles/206061357-Importing-Files>

> Import files from other apps like PDFs, PowerPoints, and spreadsheets into
> Notability. […] Bulk Import: Import a whole folder at once […]
> Importing .NBN Files (iOS) Press and hold on + New in the library. Select
> Import. Select Files. (Mac) […] From Finder > Right Click on the file >
> Open With > Notability.

"`.NBN`" is almost certainly a typo for `.note`/`.ntb` (the section describes
importing Notability's own files from the Files app) **[inferred]**. The
PoC already proved that a `.note` placed in Files opens in the user's current
Notability on iPad, so this is the path we rely on.

**"Sharing Notes"** (id 205228298, updated 2026-08-05)
<https://support.gingerlabs.com/hc/en-us/articles/205228298-Sharing-Notes>

> File Format: Choose from PDF, Note, JPEG, PNG, or NTB (Notability Cloud)

(RTF with recording is mentioned in older third-party write-ups but not in the
current article.) Notability launched on Android (beta April 2026, GA August
2026) and has a web client
(<https://9to5google.com/2026/08/03/notability-app-android-release/>,
<https://www.businesswire.com/news/home/20260811736505/en/Notability-Brings-Fully-Native-AI-Assisted-Note-taking-To-Android>);
whether `.note` files can be imported there is **[unknown]** (Notability Cloud
sync uses `.ntb`).

### 1.3 Community confirmation

- MPU Talk "How to transfer my existing notes from Goodnotes to Notability?":
  only PDF works; audio can be salvaged by unzipping `.goodnotes`
  (<https://talk.macpowerusers.com/t/how-to-transfer-my-existing-notes-from-goodnotes-to-notability/42590>).
- `swiftsaneai/sanenotes` issue #805 plans a "tier-A" GoodNotes/Notability
  importer but publishes no format details and is gated on a legal decision
  (<https://github.com/swiftsaneai/sanenotes/issues/805>).

**Conclusion:** there is no native editable path in either direction and no
open interchange format. A third-party writer for each native container is
the only way to keep ink editable. This confirms the project premise.

---

## 2. Existing software that writes `.goodnotes`

Searched GitHub (code search for `bv41`, `index.events.pb`, repository search
for "goodnotes converter", "to goodnotes", "2goodnotes") and the web
("remarkable to goodnotes", "xournal to goodnotes", "goodnotes writer",
"inkconvert"). "noteshrink" is an image-cleanup tool for scanned notes, not a
format writer. Results:

| Project | Reads | **Writes** `.goodnotes` | License | Verified in the app? |
|---|---|---|---|---|
| `juliank1m/inkref` (cloned → `ref/inkref`, HEAD 2026-08-12) | yes | **yes** (template clone-and-patch) | **no LICENSE file** (all rights reserved by default) | **YES** – Goodnotes 7.0.34 macOS: synthetic strokes import, render, lasso-select, erase |
| `cable729/inkterop` (already in `ref/more-notes/inkterop`, HEAD 2026-07-11) | yes | **yes** (`formats/goodnotes/writer.py`, `validated=False`) | MIT | partial – same-format round-trip imports and renders in GoodNotes Mac 6; synthesized geometry "imports but does not render"; flat schema-24 containers rejected by the Mac app |
| `Jojoistauchdabei/notes` "Federwerk/Grimoire" (cloned → `ref/jojo-notes`, HEAD 2026-10-01) | yes (JS port of parser-for-goodnotes) | **yes** (`js/goodnotes.js: exportGoodNotes`) | no license field/file | **no** – header says "GoodNotes-App-Kompatibilität ist ohne echte App nicht verifizierbar" |
| `franzthiemann/goodparse` | yes | no | GPL-3.0 | – |
| `Kaih1825/parser-for-goodnotes` | yes | no | MIT (needs numpy) | – |
| `RobinSp5/FileConverter`, `skoblens5/goodnotes2pdf`, `fakeminjun7321/goodnotes-pdf-engine`, `alinuxpengui/goodnotes-extractor`, `WMK965/Note-Web`, `4MBs/echo-ios` | yes (to PDF/SVG/MD) | no | various | – |
| `shashwatgokhe/goodnotes-to-starnotes-converter` (cloned → `ref/gn2starnotes`) | GoodNotes **PDF exports** only | no (writes StarNote `.note`) | MIT | – |
| `AgentApache/GoodnotesStudysetCreatorTool` | – | flashcard study sets (file 404 today) | ? | – |

Nothing commercial was found that writes `.goodnotes` (the "universal
converters" on the web all go through PDF).

### 2.1 How `inkref` builds the file (the only app-confirmed writer) **[verified from source + its FINDINGS.md]**

Files: `ref/inkref/inkref/goodnotes/{writer.py,strokes.py,records.py,document.py,lz4.py,tplfmt.py}`, `ref/inkref/FINDINGS.md`.

- **Strategy: clone-and-patch.** It opens a *template* `.goodnotes` that
  already contains at least one live stroke per target page, overwrites that
  stroke's geometry/colour, and adds further strokes by cloning the template
  record (`writer.py: GoodNotesWriter.write`). "Whether a wholly synthesized
  archive — no cloned record — imports" is listed as an open question.
- **Container (schema 24, 10 members in writer order):**
  `index.search.pb` (0 B), `index.notes.pb` (uuid→path per page),
  `notes/<uuid>` (page content), `index.events.pb`, `thumbnail.jpg`,
  `index.attachments.pb`, `attachments/<uuid>`, `schema.pb` = bytes `08 18`
  (= protobuf `{1: 24}`). Schema 25 (Mac) adds a 0-byte `document.info.pb`,
  a non-empty `index.search.pb`, `search/<uuid>` and `schema.pb` = `08 19`.
  "ZIP member order is not enforced on import."
- **Page stream:** `<varint len><message>` records, strictly alternating
  **(descriptor, item)** pairs. Descriptor: `1` uuid, `2 {1: replicaId, 2: lamportClock}` (byte-identical to item field 15), `8` stable id, `9` paint order, `14` = 5381, `16` = 24. Item: top-level field number is the type; `7` = pen stroke, `1` = image, `8` = text (RTF), `11` = math group, `21/22` = element groups.
- **Pen-stroke body (inside field 7):** `1` uuid, `2` Apple-LZ4 → tpl blob,
  `3` varint **absent for `PConstantWidthStroke`, 1 for `PVariableWidthStroke`**
  (must agree with the tpl signature or the stroke silently never draws),
  `4 {1..4: float32 RGBA}` (zero components omitted), `6/9/20` empty,
  `7` nested version msg, `15 {1,2}`, `21` = 24.
- **Tombstone = descriptor field `3` = 1 AND item field `14` = 1**
  (11 003/11 003 deleted vs 0/58 064 live records in a real library). A
  cloned tombstone imports with correct bytes and is invisible.
- **Geometry written: `PConstantWidthStroke` v2**, tpl signature
  `vuA(v)A(S(uu))A(S(uuuu))vA(f)`:
  `v`=2 (version), `u`=float32 width, `A(v)` opcodes (0 moveTo, 1 quadTo, one
  per point), `A(S(uu))` exactly one start point, `A(S(uuuu))` quadratic
  segments (cx, cy, ex, ey) with the **control point at the segment
  midpoint** (reproduces a polyline exactly; "Goodnotes applies no smoothing"),
  trailing `v`=1, `A(f)` doubles (pressures, always empty). Re-encoding 2 000
  real strokes reproduced the app's bytes 2 000/2 000.
- **Units:** coordinates are **1/132 inch = 11/6 units per PDF point**
  (checked three ways, e.g. paper record 834.24×1078.825 units ↔ exported
  `/MediaBox [0 0 455.04 588.45]`); stroke **width is 1/144 inch = 2 units per
  point** (calibrated, spread 0.0000 over a 48× range). Origin top-left, y down.
- **Paint order** is renumbered on import by stream position.
- **Compression:** calls `/usr/lib/libcompression.dylib` `COMPRESSION_LZ4`
  (0x100) through ctypes (macOS only); 330/500 of its frames were
  byte-identical to the app's own, the rest differ only in the `bv41`/`bv4-`
  choice for tiny inputs.
- **Scale:** 1 000 synthetic strokes on one page import fine (~356 KB page member).

Note the unit finding reconciles with our PoC: goodparse exposes
`page.scale = page_size/canvas` from `index.events.pb`, and the PoC multiplies
by it; Test5 page 2 reports `455.04 x 588.45 pt`, i.e. goodparse already
divides out the 11/6 **[verified: PoC output]**. inkref's remark that
goodparse "treats these as 72 dpi points and is wrong by 1.8333×" applies to
files without an events log (scale falls back to 1.0).

### 2.2 How `inkterop` builds the file (MIT, reusable) **[verified from source]**

`ref/more-notes/inkterop/core/src/inkterop/formats/goodnotes/{writer.py,wire.py}`,
`docs/formats/goodnotes.md`, `docs/validated-writes.md`.

- Writes a **full synthetic container** (no template): `document.info.pb`
  (0 B), `index.search.pb` + `search/<uuid>`, `index.notes.pb`, one
  `notes/<uuid>` per page, `index.events.pb`, `thumbnail.jpg` (needs PIL),
  `index.attachments.pb` + `attachments/<uuid>` (a blank one-page PDF made
  with reportlab), `schema.pb`.
- `index.events.pb` must contain, or the Mac app refuses/empties the import:
  document-created (field 30: doc uuid, title, first page ref, "P", "auto",
  timestamps, device id), attachment-added (6), **paper definition** (2:
  attachment ref, scale 29.333…, page size float32 w,h, template name
  `"<uuid>_standard_1_1 - Yellow"`), **page-created** (54: page ENTITY uuid,
  paper ref, lexicographic order key `"43elQ2"`), **page-link** (105: page
  number, doc uuid, page CONTENT uuid = `notes/<uuid>` name). Entity uuid =
  content uuid with the last hex group decremented by 1 (both inkref and
  inkterop observed this).
- Schema-25 page files are **journals of (header, payload) pairs**; the stroke
  uuid repeats the header's event uuid and field 15 echoes the header's field
  2; unpaired stroke records → `SwiftProtobuf.BinaryDecodingError error 2`.
  Flat schema-24 page files were rejected by the Mac app in their tests.
- Geometry: every stroke written with the pressure-pen signature
  `vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` and flat (x, y, w) float32
  triplets in section 3, LZ4-framed as raw `bv4-` blocks. Result: "the app
  accepts but does not RENDER our minimal tpl sections". Round-trips that
  replay the original stroke bytes verbatim render correctly.
- Pen-style mapping (from an iPad calibration page): field 3 absent = ball
  pen, 1 = fountain/brush/marker (pressure), 5 = pencil; field 5 = 1 =
  highlighter; field 20 = `{1: ""}` = marker.

**What this means for our writer [inferred, high confidence]:** combine
inkterop's container/events synthesis (MIT) with inkref's confirmed
`PConstantWidthStroke` encoding (field 3 absent, 11/6 unit scale, width ×2,
midpoint-control quadratics). That is the one geometry family known to
render from fully synthesized bytes. Variable-width (pressure) ink from
Notability would be flattened to a constant width per stroke until
`PVariableWidthStroke` sections 4/5/8/9/10 are understood. The first iPad
import test should A/B: (a) schema-24 flat vs (b) schema-25 journal, since
inkref (schema 24, Mac 7.0.34) and inkterop (Mac 6, schema 25 required) disagree.

### 2.3 Federwerk (`ref/jojo-notes/js/goodnotes.js`)

Browser-side JS exporter: `index.notes.pb`, per-page `notes/<uuid>` with a
meta record + stroke/shape/text/image records, `search/<uuid>`,
`index.events.pb`, empty `document.info.pb`, `schema.pb`, `index.attachments.pb`,
`index.search.pb`, `thumbnail.jpg`, stored (non-deflated) ZIP. Its LZ4 writer
`bv4n()` emits **`bv41` + all-literal LZ4 block + `bv4$`** (same approach as
§3.4). Untested against GoodNotes; no license → do not copy code.

---

## 3. Apple framed LZ4 (`bv41` / `bv4-` / `bv4$`)

### 3.1 Primary source: Apple documentation **[verified]**

Fetched from Apple's doc JSON endpoint
`https://developer.apple.com/tutorials/data/documentation/compression/compression_lz4.json`
(the HTML page is JS-rendered). Rendered page:
<https://developer.apple.com/documentation/compression/compression_lz4>

> The encoded format that the Compression library produces and consumes is
> compatible with the open-source version, apart from the addition of a very
> simple frame to the raw stream […] An LZ4-encoded buffer is a sequence of
> blocks, each beginning with a header.
> A compressed block header consists of the octets [0x62, 0x76, 0x34, 0x31].
> Following that is the size (in bytes) of the decoded (plaintext) data the
> block represents, and the size (in bytes) of the encoded data stored in the
> block. The header stores both sizes as (potentially unaligned) 32-bit
> little-endian values. The actual LZ4-encoded data stream immediately
> follows the compressed block header.
> An uncompressed block header consists of the octets [0x62, 0x76, 0x34,
> 0x2d]. Following that is a single 32-bit little-endian value representing
> the plaintext data's size (in bytes), and then the plaintext data itself.
> An end-of-stream block header consists of the octets [0x62, 0x76, 0x34,
> 0x24] and identifies the end of the LZ4 frame. Don't attempt to read or
> write data beyond this header.
> If you're implementing a wrapper for a raw LZ4 decoder, keep in mind that a
> compressed block may refer to data from the previous block, so the
> (decoded) previous block must be available to the decoder.

(The octet values are rendered as inline code in the JSON and were stripped by
my extractor; they are `bv41`, `bv4-`, `bv4$` in ASCII — confirmed by every
implementation below.) **Caveat:** the Swift-flavoured page
(`…/compression/algorithm/lz4.json`) says the uncompressed header is
"followed by two sizes". Four independent implementations that were validated
against real libcompression output use a **single** u32 for `bv4-`:
`go-compressions/lz4` (`apple.go`, validated byte-for-byte against a macOS
Tart disk layer), `ydkhatri/spotlight_parser` (forensic Spotlight DBs),
`franzthiemann/goodparse`, `cable729/inkterop`, and `juliank1m/inkref`
(compares its frames to the app's). `Kaih1825/parser-for-goodnotes`
(`compression.py`) is the outlier and reads two u32s after `bv4-` — it will
raise `truncated Apple LZ4 block` on a single-u32 stored block (tested). Since
our writer emits `bv41` only (below), this discrepancy cannot affect us.

Block size: Apple's text states no cap. `go-compressions/lz4` and inkterop
assume **≤ 64 KiB decoded per block** (LZ4 offsets are 16-bit; libcompression
is observed to chunk at 64 KiB) **[inferred]**. We chunk at 65 536 bytes.

### 3.2 Exact byte layout

All integers little-endian, no alignment, no checksum.

```
offset  size  content
0       4     b"bv41"
4       4     u32 decoded_size   (bytes of plaintext this block represents)
8       4     u32 encoded_size   (bytes of the LZ4 block that follows)
12      n     standard LZ4 block (lz4_Block_format.md), n == encoded_size
...     repeat any mix of bv41 / bv4- blocks
k       4     b"bv4-"
k+4     4     u32 size
k+8     size  plaintext bytes
...
end     4     b"bv4$"            (no sizes; nothing follows inside the frame)
```

Python struct formats: header `<4sII` for `bv41`, `<4sI` for `bv4-`.

### 3.3 Evidence from real GoodNotes files **[verified]**

Scan of all 9 sample notebooks (`goodparse/samples/*.goodnotes`,
`parser-for-goodnotes/assets/*.goodnotes`): 5 677 `bv41` blocks, 0 `bv4-`,
5 677 `bv4$`; **every frame is exactly one `bv41` block immediately followed by
`bv4$`**; largest `decoded_size` seen = 30 520 B. Example from
`Test5.goodnotes` `notes/F51610D9-…`, offset 115:

```
62 76 34 31  46 0a 00 00  97 07 00 00  f3 01 74 70 ...
"bv41"       raw=2630     comp=1943    LZ4 block (token f3 → 15+1 literals "tpl\0F\n..."…)
```

The block decodes with `python-lz4` (`lz4.block.decompress(blk, uncompressed_size=2630)`)
to a `tpl\0` image, and the 4 bytes after the block are `bv4$`. So Apple's
block payload is plain standard LZ4 **[verified]**.

### 3.4 Minimal pure-Python encoder (all-literal blocks)

LZ4 block rules used (from `lz4/doc/lz4_Block_format.md`,
<https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md>): a sequence is
`token` (high nibble = literal length, low nibble = match length), literal
length ≥ 15 is extended by bytes of 0–255 (255 means "read another"); "The
last sequence contains only literals. The block ends right after the literals
(no offset field)"; a block of only literals is valid ("Even empty input can be
represented"). An all-literal block is therefore `token | ext... | literals`.

```python
import struct

def lz4_literal_block(data: bytes) -> bytes:
    """Standard LZ4 block: one sequence, all literals, no match."""
    n = len(data)
    if n < 15:
        return bytes([n << 4]) + data
    out = bytearray([0xF0]); n -= 15
    while n >= 255:
        out.append(255); n -= 255
    out.append(n)
    return bytes(out) + data

def apple_lz4_frame(data: bytes, block: int = 65536) -> bytes:
    """Apple libcompression COMPRESSION_LZ4 frame, bv41 blocks, zero compression."""
    out = bytearray()
    for i in range(0, len(data), block):
        chunk = data[i:i + block]
        blk = lz4_literal_block(chunk)
        out += b"bv41" + struct.pack("<II", len(chunk), len(blk)) + blk
    return bytes(out) + b"bv4$"
```

Worked examples (hex, **[verified]** output of the code above):

```
payload b"hello world!!" (13 B):
62763431 0d000000 0e000000 d0 68656c6c6f20776f726c642121 62763424
  bv41   raw=13   comp=14  token 0xd0 (13 literals)        bv4$
payload bytes(range(20)):
62763431 14000000 16000000 f0 05 000102...13 62763424
  bv41   raw=20   comp=22  token 0xf0 + ext 5 = 20 literals
```

Verification run (`scratchpad` test, random payloads of 0, 1, 14, 15, 16, 269,
270, 271, 1 000, 65 535, 65 536, 65 537, 200 000 bytes): decoded identically by
`goodparse.applelz4.apple_decompress`, `goodnotes_re.compression.decode_apple_lz4`,
`inkterop…wire.apple_lz4_decompress`, and the single-block payload by
`lz4.block.decompress` (python-lz4 4.4.5, installed with `pip install lz4`).
An empty payload encodes as just `bv4$` (inkterop's tests assert the same).

Why `bv41`+literals rather than `bv4-`: it is what GoodNotes itself writes
(100 % of observed blobs), it is decodable by every known reader including the
outlier, and it costs 1–3 bytes per 64 KiB; the ZIP's deflate removes the
redundancy. A real LZ4 match-finder is an optional later optimisation (the
window may cross block boundaries, but an encoder that never emits matches
never needs the window).

---

## 4. Pyodide feasibility (pure-Python converter in the browser, iPad Safari included)

### 4.1 Version, CDN, size **[verified]**

- Latest: **Pyodide 314.0.7** (jsDelivr npm tag `latest`; `stable-0.29` is
  0.29.5). Changelog: 314.0.7 released 2026-09-14, "Upgraded to Python
  3.14.2"; the 314.x numbering started with 314.0.0 (2026-06-09) and mirrors
  CPython 3.14 (<https://pyodide.org/en/stable/project/changelog.html>).
- CDN (docs, "Recommended, cached by the browser"):
  `https://cdn.jsdelivr.net/pyodide/v314.0.7/full/` →
  `pyodide.mjs` (ESM) / `pyodide.js` (UMD), `pyodide.asm.mjs` (1.25 MB),
  `pyodide.asm.wasm` (9.6 MB), `python_stdlib.zip` (2.5 MB), `pyodide-lock.json`
  (`{'abi_version': '2026_0', 'arch': 'wasm32', 'platform': 'emscripten_5_0_3', 'python': '3.14.2'}`).
  Self-hosting: "Any service that hosts static files and that correctly sets
  the WASM MIME type and CORS headers will work"
  (<https://pyodide.org/en/stable/usage/downloading-and-deploying.html>). No
  COOP/COEP needed (we do not use SharedArrayBuffer/pthreads).
- License: MPL-2.0 (npm `pyodide@314.0.7` package.json).

### 4.2 Stdlib availability **[verified by execution]**

Ran `pyodide@314.0.7` under Node 22 (`scratchpad/pyo/test.mjs`):

```
loadPyodide ms 2286  python 3.14.2
stdlib ok: plistlib, zipfile, zlib, struct, uuid, hashlib
pages=3 page2 strokes=28 size=455.04x588.45 note_bytes=144169  convert ms 593
wasm heap bytes 54460416
```

i.e. goodparse (unpacked from a zip with `pyodide.unpackArchive`) parsed
`Test5.goodnotes` from the Emscripten FS, and `plistlib.loads/dumps(FMT_BINARY)`
+ `zipfile` (ZIP_DEFLATED, so `zlib` is built in) rebuilt a `Session.plist`
container — the PoC's whole I/O surface. Docs list of removed stdlib modules
(<https://pyodide.org/en/stable/usage/wasm-constraints.html>): "curses, dbm,
ensurepip, fcntl, grp, idlelib, lib2to3, msvcrt, pwd, resource, syslog,
termios, tkinter, turtle.py, turtledemo, venv, winreg, winsound"; only
`pydoc_data` and `tzdata` need `loadPackage()`; `ssl` is a stub; "Hash
algorithms that are depending on OpenSSL are not available" (md5/sha* are
fine). `numpy` is a Pyodide package (lock file) if parser-for-goodnotes were
used, at the cost of a multi-MB extra download.

### 4.3 Loading our package from a zip **[verified: docs + run]**

<https://pyodide.org/en/stable/usage/loading-custom-python-code.html>:

```js
let buffer = await (await fetch("gnnote.zip")).arrayBuffer();
await pyodide.unpackArchive(buffer, "zip");   // unpacks into cwd (/home/pyodide), importable
pyodide.pyimport("gnnote");
```

`unpackArchive(buffer, format, {extractDir})` accepts "zip", "gztar", "tar",
"bztar", "xztar"; a wheel is a zip too (no dependency resolution, unlike
`micropip.install`).

### 4.4 Web Worker **[verified: docs]**

<https://pyodide.org/en/stable/usage/webworker.html>: "Pyodide requires a
module-type worker because `pyodide.asm.mjs` is an ES module. Classic workers
using `importScripts()` are not supported." → `new Worker("worker.js", {type: "module"})`
and `import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs"`.
Module workers are supported in Safari ≥ 15 (so iPadOS 15+) **[inferred from
memory of WebKit release notes; not re-verified today]**.

### 4.5 iOS/iPadOS Safari constraints

- **Memory [verified quotes]:** WebKit engineer Ben Nham (bug 268816,
  2024-03-13): iPhone WebContent limit "is 1.5GB (although if the system is
  not under memory pressure, the process may be able to exceed this limit)";
  "On iPad, the WebContent process has a higher memory limit, which differs by
  device. For an 8GB device, the limit will be in the 4GB+ range"; "Currently
  the Gigacage supports 2GB of allocations on iOS" (wasm memory lives there)
  (<https://bugs.webkit.org/show_bug.cgi?id=268816>). Empirically much lower on
  older hardware: Lapcat Software (2026-01-07) crashed pages "on my 8th
  generation iPad at around 200 MB" and an iPhone SE 3 at ~100 MB, iOS 26.2,
  with plain JS arrays (<https://lapcatsoftware.com/articles/2026/1/7.html>).
  Our measured footprint (54 MB wasm heap after a conversion, ~10 MB wasm
  code) fits under that with room for a ~50 MB notebook if we stream pages and
  avoid keeping both ZIPs plus decoded plists in memory **[inferred]**.
- **Known regression:** Pyodide 0.27.1–0.27.2 crashed/hung on iOS 18 Safari
  and Chrome ("A problem repeatedly occurred"), fixed in 0.28.0
  (<https://github.com/pyodide/pyodide/issues/5428>,
  <https://github.com/pyodide/pyodide/issues/5670>, changelog 0.28.0). 314.x
  is past that, but pin the version and test on the user's iPad before
  upgrading.
- **Download / "Save to Files" [verified]:** `<a download>` on iOS is WebKit
  bug 167341, RESOLVED FIXED, "enabled in the iOS 13 Developer beta 1, along
  with true downloads support in MobileSafari" (2019-06-03)
  (<https://bugs.webkit.org/show_bug.cgi?id=167341>); a blob URL + `download="x.note"`
  shows Safari's download sheet and lands in Files › Downloads. Blob filename
  bugs were reported afterwards, so always set `download` and give the Blob a
  real MIME type. Preferred on iPad: Web Share Level 2 — Safari 15: "Web Share
  level 2 enhancements to Web Share enable sharing files from a web page to an
  app" (<https://webkit.org/blog/11989/new-webkit-features-in-safari-15/>) →
  `navigator.share({files: [new File([bytes], "x.note")]})` opens the share
  sheet with "Save to Files" and the Notability / GoodNotes app targets
  directly. Must be called from a user gesture; feature-detect with
  `navigator.canShare?.({files})`.

### 4.6 Skeleton

`index.html`

```html
<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<input id="f" type="file" accept=".goodnotes,.note"> <button id="go" disabled>Convert</button>
<pre id="log"></pre>
<script type="module">
const log = m => document.getElementById('log').textContent += m + '\n';
const w = new Worker('./worker.js', { type: 'module' });   // module worker: required by Pyodide
let ready = false, result;
w.onmessage = async ({ data }) => {
  if (data.type === 'ready') { ready = true; go.disabled = false; log('python ready'); }
  if (data.type === 'log') log(data.msg);
  if (data.type === 'error') log('ERROR ' + data.msg);
  if (data.type === 'done') {
    const file = new File([data.bytes], data.name, { type: 'application/octet-stream' });
    if (navigator.canShare?.({ files: [file] })) {           // iPadOS 15+: share sheet -> Save to Files / Notability
      await navigator.share({ files: [file] }).catch(e => log(String(e)));
    } else {                                                 // iOS 13+ / desktop: download attribute
      const a = Object.assign(document.createElement('a'),
        { href: URL.createObjectURL(file), download: data.name });
      document.body.appendChild(a); a.click(); a.remove();
    }
  }
};
go.onclick = async () => {
  const src = f.files[0]; if (!src) return;
  const bytes = new Uint8Array(await src.arrayBuffer());
  w.postMessage({ type: 'convert', name: src.name, bytes }, [bytes.buffer]);  // transfer, no copy
};
</script>
```

`worker.js`

```js
import { loadPyodide } from 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs';

const pyodide = await loadPyodide({ indexURL: 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/' });
pyodide.unpackArchive(await (await fetch('./gnnote.zip')).arrayBuffer(), 'zip');  // our pure-Python package
pyodide.FS.mkdirTree('/work');
self.postMessage({ type: 'ready' });

self.onmessage = async ({ data }) => {
  if (data.type !== 'convert') return;
  try {
    const inPath = '/work/' + data.name;
    pyodide.FS.writeFile(inPath, data.bytes);
    const outName = /\.goodnotes$/i.test(data.name)
      ? data.name.replace(/\.goodnotes$/i, '.note')
      : data.name.replace(/\.note$/i, '.goodnotes');
    const outPath = '/work/' + outName;
    pyodide.globals.set('in_path', inPath); pyodide.globals.set('out_path', outPath);
    await pyodide.runPythonAsync(`
import gnnote
gnnote.convert(in_path, out_path)   # dispatches on extension
`);
    const bytes = pyodide.FS.readFile(outPath);              // Uint8Array
    pyodide.FS.unlink(inPath); pyodide.FS.unlink(outPath);
    self.postMessage({ type: 'done', name: outName, bytes }, [bytes.buffer]);
  } catch (e) {
    self.postMessage({ type: 'error', msg: e.message });
  }
};
```

Serve `gnnote.zip` next to the page; GitHub Pages / any static host
works. First load: ~13.5 MB of CDN assets (cached afterwards), ~2–3 s to
`loadPyodide` on a desktop, expect 5–10 s on an iPad **[inferred]**.

---

## 5. Licensing

### 5.1 Facts **[verified]**

- `goodparse` — GPL-3.0 (`ref/goodparse/LICENSE`).
- `parser-for-goodnotes` — MIT (`ref/parser-for-goodnotes/LICENSE`), runtime
  dependency `numpy>=2.0.2` (pyproject); numpy 2.4.6 is already installed in
  this environment and the package imports (`PYTHONPATH=src python3 -c "import goodnotes_re"`).
- `inkterop` — MIT code, CC BY 4.0 docs; its own policy: "GPL/AGPL/unlicensed
  references: format facts only, never read/port source", and its GoodNotes
  doc credits goodparse's README for facts while stating "goodparse's source
  code was deliberately not read or reused".
- `inkref` and `Jojoistauchdabei/notes` — **no license** → copyrighted, no
  right to copy code; using the documented *facts* (unit scales, field
  semantics, signatures) is fine (facts/interfaces are not protected
  expression; this is the same stance inkterop takes).
- `notability-to-svg` — GPL; `svg2notability`, `notability-reader` — no
  license file; `notesconverter` (Notability→Noteful, has a Python
  NSKeyedArchiver writer) — MIT. Pyodide — MPL-2.0 (file-level copyleft; we
  do not modify it). python-lz4 — BSD; numpy — BSD-3.
- GNU GPL FAQ (<https://www.gnu.org/licenses/gpl-faq.html>): "If a library is
  released under the GPL (not the LGPL), does that mean that any software
  which uses it has to be under the GPL or a GPL-compatible license? — Yes,
  because the program actually links to the library. As such, the terms of
  the GPL apply to the entire combination." Compatible means "you can combine
  code released under the other license with code released under the GNU GPL
  in one larger program" — i.e. MIT code may go *into* a GPL program, but GPL
  code cannot go into a program that stays MIT. Mere aggregation (separate
  programs talking via pipes/CLI) is the only exception; `import goodparse`
  inside the same Python process is not aggregation.

### 5.2 Implications

| Option | Licence of our project | Cost |
|---|---|---|
| A. Keep importing goodparse | must be **GPL-3.0** (or not distributed at all) | none now; blocks MIT |
| B. parser-for-goodnotes (MIT) | MIT | numpy in Pyodide (+ several MB download, slower start); its decoder is heavier and has the `bv4-` quirk |
| C. Own pure-Python reader/writer | MIT | 1–2 days: protobuf wire walker, Apple-LZ4 (decoder: ~60 lines; encoder: §3.4), tpl parser/encoder, events-log synthesis (port from inkterop, MIT), stroke encoding per inkref facts |

### 5.3 Recommendation

Choose **C**, seeded from MIT code: take inkterop's `wire.py` (varint/field
encoders, `apple_lz4_decompress`, `encode_tpl`), its `_events_log`/container
synthesis, and its reader for `.note`/`.ntb` as a starting point (keep the MIT
notice), use parser-for-goodnotes only as a second reference, and implement
the `PConstantWidthStroke` geometry from inkref's documented facts. Do not
copy goodparse code; stop importing it (the PoC `gn2note_poc.py` currently
does, so the PoC is GPL-bound if ever shipped — fine as a throwaway).
Because the PoC author has read goodparse's source, write the new reader from
the format notes in `docs/goodnotes-*.md` and inkterop's `docs/formats/goodnotes.md`
rather than from memory of goodparse's structure; keep goodparse only as a
differential test oracle run in a separate process (mere aggregation), which
the GPL permits. Drop PIL/reportlab (inkterop uses them for the thumbnail and
blank PDF): the PoC already has a pure-Python PNG writer, and a minimal blank
PDF is ~300 bytes of text; a JPEG thumbnail can be a fixed embedded 4×4 JPEG.

---

## 6. Open items for the next steps

1. iPad A/B import test of the first synthesized `.goodnotes`: schema-24 flat
   vs schema-25 journal; both with `PConstantWidthStroke`, field 3 absent.
2. Whether a fully synthesized archive (no cloned record) imports — inkterop
   says yes for the container (Mac 6), inkref lists it as open (Mac 7.0.34).
3. Variable-width ink in GoodNotes needs `PVariableWidthStroke` sections
   4/5/8/9/10 decoded; until then Notability per-point widths collapse to
   one width per stroke.
4. Notability Android/web `.note` import: unknown; the iPad path is proven.
5. Memory budget on the user's actual iPad model: Safari has no
   `performance.memory`; log `pyodide._module.HEAPU8.length` from the worker
   after each conversion and test with the largest notebook the user has.

## Critic additions

* §2.2/§6 item 1 ("A/B test schema-24 flat vs schema-25 journal") is moot: every GoodNotes-
  written page file in the corpus, schema 24 or 25, is a (metadata, content) pair stream, and
  inkref's schema-24 pair stream imports in GoodNotes 7.0.34. inkterop's "flat" stream was simply
  unpaired. Write pairs + `schema.pb = 24`.
* inkref's width calibration ("2 units per point", 1/144 in) is confirmed by GoodNotes' own PDF
  export of Test5 (all flat strokes and the shape are stroked at exactly `W/2` pt), and its
  "control point at the midpoint" is the correct way to encode a polyline because the flat
  format's segments are true quadratic Béziers (verified to 0.0002 pt against the export). The
  `goodnotes-stroke.md` §7.3 recommendation (`W × 72/132`) is superseded.
* inkterop's Mac-6 iterations also show: `#105` with `#1 = page number`, `#30.#3/#7` = first
  page UUID, a built-in template name on a foreign blank PDF, and a 4×4 white `thumbnail.jpg` are
  all accepted — useful lower bounds on GoodNotes' strictness (Mac only; iPad untested).
