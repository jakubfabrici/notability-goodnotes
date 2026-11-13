# Microsoft OneNote (read only)

gnnote reads OneNote notes and converts them to GoodNotes or Notability with the
handwriting kept as editable ink. Writing OneNote files is not supported (section 6).

* Inputs: a OneNote **section** (`.one`, as OneDrive serves it or as OneNote for Windows
  exports it), a **notebook folder downloaded as a `.zip`** from onedrive.com or OneNote
  for the web, or a **notebook package** (`.onepkg`) as OneNote for Windows exports it
  (section 5).
* Refused with an explanation: a `.onetoc2` table of contents on its own, password-protected
  sections, packages split over several cabinet files or compressed with Quantum (OneNote
  writes neither).
* Code: `gnnote/onenote/` (`read_onenote(data: bytes) -> Document`), registry id `onenote`.

## 1. Getting the files on an iPad

OneNote for iPad has no file export of its own (its "Send a copy" makes a flattened PDF).
Its notebooks live on OneDrive, though, and OneDrive hands out the real section files.

**A whole notebook (recommended)**

1. Open **onedrive.live.com** in Safari and sign in with the account OneNote uses.
2. Go to **My files**. Notebooks are usually in the **Documents** folder; each notebook is a
   folder with the notebook's name.
3. Select the notebook's folder (tap the circle next to it, do not open it) and choose
   **Download**. Safari saves `<Notebook>.zip` to **Files > Downloads**.
4. Open the gnnote web page, choose the `.zip` (not the folder Files may unpack it into),
   pick GoodNotes or Notability and convert. Every section of the notebook becomes part of
   one document, in the notebook's own section order.

**A single section**: open the notebook's folder on onedrive.live.com, select one
`<Section>.one` file and choose Download; convert that file.

**OneNote for the web** (personal Microsoft accounts only): right-click the notebook in the
notebook list and choose *Export notebook*; the ZIP converts the same way.

Work or school accounts (SharePoint) were not tested. A password-protected section must be
unlocked and its password removed in OneNote first (or its pages copied into an unprotected
section). The download route is documented by Microsoft for OneNote for Windows; that Safari
on an iPad produces the same ZIP is expected but has not been verified on a device yet.

On a PC with OneNote for Windows, *File > Export > Section > OneNote 2010-2016 Section
(.one)* produces a desktop `.one`, and *File > Export > Notebook > OneNote Package (.onepkg)*
a whole notebook in one file; both convert (section 5).

## 2. What is converted

| OneNote content | Result | Notes |
|---|---|---|
| Pages | One page each, in section order | Conflict pages, page-version history, pages marked as deleted and the notebook's recycle bin are skipped. |
| Page size | OneNote's page size, grown to hold the content | A OneNote page is an unbounded canvas: the page becomes as large as its content plus a margin (default 754 x 783 pt). Pages more than 3 times the usual size get a warning; they are not split. Content left of or above the page origin is moved onto the page. |
| Ink | Editable strokes | Position, colour, transparency and pen width are kept; **highlighter** strokes stay highlighters. Pen pressure becomes per-point widths with an approximate curve (section 4.4). |
| Handwriting written inline in typed text (desktop "ink words") | Strokes, placed approximately | OneNote lays these words out with the text; gnnote flows them along their paragraph's lines (one warning). |
| Pictures (PNG, JPEG) | Images | Position and size (OneNote's maximum display size applied). |
| PDF printouts | One picture per printed page | The PDF itself is not carried over yet (one warning). |
| Typed text, page titles | Text boxes with runs | Bold, italic, underline, font, size, colour, alignment, bullets / numbering as text. OneNote reflows text itself, so positions and line breaks are approximate (one warning). |
| Tables | Dropped | Warning (their text and pictures are lost). |
| Math | Dropped | Warning. |
| Note tags (to-do boxes, stars ...) | Dropped | Warning; the tagged text is kept. |
| Attached files, audio and video recordings | Dropped | Warning. |
| Pictures in other formats (EMF, WMF, TIFF, BMP, GIF) | Dropped | Warning; OneNote's web copy of the picture is used when it is PNG or JPEG. |

A notebook ZIP or package is merged into **one document**; a warning lists the sections in
the order they were merged. The order comes from the notebook's `.onetoc2` files (one per folder;
section groups are sub-folders) and falls back to file-name order, with a warning, when
there is none. Password-protected sections inside a ZIP or package are skipped with a
warning; a damaged section is skipped with a warning and the others are converted.

The document is named after the section (as OneDrive records it), after the notebook folder
of a ZIP, or else after the input file (a `.onepkg` is named after its file, or after its
only section).

## 3. Limitations

* Text and inline-handwriting layout are approximations: OneNote positions only the outline
  (the text block); everything inside it is laid out by OneNote at display time.
* The pressure-to-width curve is an approximation (section 4.4); strokes written with a mouse
  or finger carry a constant default pressure (0.75) and come out about 40 % wider than the pen's
  nominal width.
* Nested ink containers (not seen in any sample) are placed by their own offsets and
  scaling, as the reference renderer does.
* Printouts arrive as pictures; tables, math, tags and attachments are dropped.
* Very large pages are not split; a page side above 14 400 pt (200 inches, the PDF page
  limit many apps keep to) gets its own note in the warning.
* No device-made iPad sample has been tested yet; the OneDrive (FSSHTTPB) samples come from
  OneNote for the web, Windows and Android. See section 8 for the files that would close the
  gaps.

## 4. Format facts the reader relies on

Sources: Microsoft's [MS-ONESTORE] (v20250520), [MS-FSSHTTPB] (v20240820) and [MS-ONE]
(v20221115), published under the Open Specification Promise; the ink layout, which no
specification documents, from the onenote.rs wiki's "Ink Object Format" with five errata
re-verified on real files (section 4.3). No third-party code was used for sections; the
`.onepkg` unpacking is a port of MIT-licensed code (section 5).

### 4.1 Packagings

| Variant | Header GUID at 0 | Header GUID at 48 | Where it comes from |
|---|---|---|---|
| Desktop revision store, section | `{7B5C52E4-D88C-4DA7-AEB1-5378D02996D3}` | `{109ADD3F-911B-49F5-A5D0-1791EDC8AED8}` | OneNote for Windows exports and backups |
| Desktop revision store, table of contents | `{43FF2FA1-EFD9-4C76-9EE2-10EA5722765F}` | same | `.onetoc2` |
| Alternative packaging (MS-FSSHTTPB) | `{7B5C52E4-...}` | `{638DE92F-A6D4-4BC1-9A36-B3FC2511A5B7}` | **OneDrive / SharePoint downloads**; the cell schema `{1F937CB4-...}` (`.one`) / `{E4DBFD38-...}` (`.onetoc2`) tells them apart |
| `.onepkg` | `MSCF` cabinet (LZX) of desktop `.one` / `.onetoc2` files (section 5) | - | OneNote for Windows *Export > Notebook* |

**Desktop revision store** (`native.py`): header (1024 bytes) -> transaction log (only the
committed number of FileNodes of each file node list is read) -> root file node list
(object spaces, the root object space, the file data store) -> per object space: object
space manifest list -> its last revision manifest list -> the revision last labelled with
the default context and revision role 1 -> that revision plus the revisions it depends on
(oldest first; later declarations win) -> object groups -> object declarations. CompactIDs
resolve through the global identification table in effect where they are declared
(`.onetoc2` revision manifests may copy entries from their dependency's table). Pictures are
`<ifndf>{GUID}` references into the file data store. A revision manifest with
`odcsDefault = 2` or an `ObjectDataEncryptionKeyV2FNDX` node marks the section as encrypted.

**Alternative packaging** (`package.py`): header GUIDs, then a 0x7A stream object (storage
index ID, cell schema) holding a data element package. Data elements: storage index (cell
and revision mappings), storage manifest (root declares; the data root
`{84DEFAB9-AAA3-4A0D-A3A8-520C77AC7073},2` names the section's cell), cell manifests (current
revision), revision manifests (base revision, root objects
`{4A3717F8-1C14-49E7-9526-81D942DE1741},role`, object groups), object groups (declarations
and data in parallel arrays: partition 4 = JCID, partition 1 = the same property set as the
desktop format, partition 2 = BLOB), object data BLOBs and data element fragments
(reassembled). Property-set CompactIDs pair in order with the object data's extended-GUID
array (objects) and cell-ID array (object spaces). OneDrive files prefix every BLOB with its
length as a compact integer, which the specification does not mention. A root object with
role 3 (encryption key) marks the section as encrypted.

Both packagings carry byte-identical [MS-ONE] property sets, so everything above the
container is shared.

### 4.2 Content model ([MS-ONE])

Section `0x00060007` -> page series `0x00060008` (`ChildGraphSpaceElementNodes 0x2C001D63`:
one object space per page) -> page manifest `0x00060037` -> page `0x0006000B`. The page's
`ElementChildNodes 0x24001C20` hold outlines `0x0006000C`, pictures `0x00060011`, embedded
files `0x00060035` and ink containers `0x00060014`; `StructureElementChildNodes 0x24001D5F`
holds the title `0x0006002C`. Outline -> outline elements `0x0006000D` (indentation by
nesting) -> rich text `0x0006000E` (UTF-16 text with a trailing NUL, run ends
`TextRunIndex`, run styles `0x0012004D`), pictures, tables `0x00060022`. Layout values are
floats in half-inches (36 pt); offsets are page-absolute; a title is drawn at
(36 pt + offset, 18 pt + offset). The section display name is `0x1C00349B` (or `0x1C001D69`).
Conflict pages hang off the page manifest and version-history pages live in other
contexts; neither is followed. A `.onetoc2` lists its sections in `0x24001CF6` entries
with file name `0x1C001D6B`, ordering ID `0x14001CB9` and file identity `0x1C001D94`
(not in the [MS-ONE] tables; observed in every sample).

### 4.3 Ink

```
InkContainer 0x00060014: OffsetFromParentHoriz/Vert (half-inches), InkScalingX/Y 0x14001C46/47
  InkData 0x20003415 -> InkDataNode 0x0002003B: InkStrokes 0x24003416, InkBoundingBox 0x1C003418
    InkStrokeNode 0x00020047: InkPath 0x1C00340B, InkStrokeProperties 0x20003409
      StrokePropertiesNode 0x00120048: InkDimensions 0x1C00340A, InkWidth 0x1400340D,
        InkHeight 0x1400340C, InkColor 0x1400340F, InkTransparency 0x0C003414,
        InkRasterOperation 0x0C003413, InkPenTip 0x0C003412, InkIgnorePressure 0x08003411
```

* `InkPath`: 7-bit little-endian varints; the first is twice the value count, every further
  one is `+-(u >> 1)` with the sign in bit 0; dimension-major (all X, all Y, then further
  channels); within a channel the first value is absolute and the rest are deltas.
* `InkDimensions`: 32-byte entries (packet GUID, minimum, maximum, unit, resolution). X
  `{598A6A8F-...}` and Y `{B53F9F75-...}` are HIMETRIC (0.01 mm); pressure is
  `{7307502D-F9F4-4E18-B3F2-2CE1B1A3610C}` (0..32767). Other channels are skipped.
* Page position in points: `36 * offset + 72 / 2540 * scaling * value`. A nested container
  uses its own offsets and scaling.
* Width: `max(InkWidth, InkHeight)` HIMETRIC -> pt (the highlighter is 56 x 400, a chisel).
* Colour: `InkColor` is a COLORREF `0x00BBGGRR` (absent = black); alpha
  `1 - InkTransparency / 255` (0 or absent = opaque).
* Highlighter: `InkRasterOperation = 9` (mask pen); without a raster operation, a
  rectangular tip with some transparency.
* Errata of the wiki (re-verified): colour is COLORREF, not ARGB; transparency 0 is opaque;
  the real pressure GUID is the one above; the anti-aliasing / fit-to-curve flags are
  `0x0800340E` / `0x08003410`; the "reserved" bytes of a dimension are unit + resolution.
* Inline ink words: U+FFFC runs in rich text whose `TextRunDataObject 0x24003458` entry is
  an InkDataNode and whose `TextRunData 0x40003499` entry carries `EmbeddedInkStartX/Y
  0x1400349E/9F` (the ink's origin, half-inches) and `EmbeddedInkWidth/Height 0x140034A0/A1`;
  ink spaces carry `EmbeddedInkSpaceWidth/Height 0x14001C27/28`. gnnote flows them along the
  paragraph's lines inside the outline's width.

### 4.4 Pressure

Per-point width = nominal width x `(1.5 p + 0.25)`, `p` = pressure normalised by its
dimension range (the factor WPF's ink renderer uses; OneNote's curve is undocumented), unless
`InkIgnorePressure` is set or the stroke has no pressure channel. The model's `Stroke.width`
stays the nominal width.

### 4.5 Hardening

Every offset, length and count is checked against the bytes that are present before
anything is allocated; reference loops (file node lists, revision chains, object graphs) are
cut; nesting is bounded (stream objects 24, property sets 16, outlines and ink containers
32); fragments may not reassemble to more than the file holds; ZIP and package members
above 256 MB (declared) or beyond 1 GB in total are skipped (section 5 adds the package's own
limits). An encrypted section is detected from its
revision structure and refused before any property set is decoded. Nothing but `ValueError`
leaves `read_onenote`.

Bounds checks alone do not bound the cost of a crafted file: a few megabytes can hold
millions of two-byte structures, or make one object group, stroke, picture or paragraph count
on thousands of pages. So:

* nothing is built per structure: file node lists and stream objects are walked where they
  lie, and a section holds one page's objects at a time;
* a **work budget** bounds time: about one unit per microsecond (a file node or stream object
  costs 1, applying or decoding an object 10). A document starts with 2 million units and
  every section adds one unit per byte, up to 30 million (real files use under 0.2 units per
  byte). When it runs out, the pages read so far are kept and a warning says so;
* a **memory budget** per page: about one unit per 100 bytes kept (objects, references,
  properties, revisions), 1 million units per page. Real pages use 30 - 50 units per stroke;
  a page beyond the budget is read as far as it fits or skipped, with a warning;
* what a document can hold grows with its sections' size: one ink point per 2 bytes (a point
  takes at least a byte per coordinate; real files use 11 bytes or more), one byte of
  pictures and of typed text per byte, at most 4 million points, 512 MB of pictures, 64 MB of
  text and 200 000 pictures, text boxes and runs. A picture used many times is one copy.

Measured on crafted files of up to 63 MB, a read then takes at most about 30 seconds and a
page's structures at most about 150 MB; ink costs what the same number of points costs from a
real notebook (about 160 bytes per point in the model). `tests/test_onenote_budgets.py`
checks each mechanism on small crafted files.

## 5. Notebook packages (`.onepkg`)

*File > Export > Notebook > OneNote Package* in OneNote for Windows saves a whole notebook as
one `.onepkg`: a Microsoft Cabinet archive of the notebook folder (`.one` sections,
`.onetoc2` tables of contents, section groups as sub-folders, the recycle bin), compressed
with LZX. gnnote unpacks it (`cab.py`, `lzx.py`) and then reads the files exactly as it reads
a notebook ZIP (section 2). Only the cabinet layer is new; the sections inside are the
desktop format of section 4.1.

**Cabinet** ([MS-CAB]). A 36-byte header (`MSCF`, the cabinet size, the offset of the file
table, folder and file counts, flags; with flag 4, reserve sizes for the header, the folder
records and the data blocks follow), one 8-byte record per folder (offset of its first data
block, number of blocks, compression), one record per file (size, offset in its folder's
expanded data, folder index, attributes, NUL-terminated name with `\` between folders) and,
per folder, its data blocks (checksum, compressed and expanded sizes, payload). A folder's
blocks expand, in order, to one stream that its files are cut from. Names are UTF-8 when
attribute 0x80 says so; otherwise UTF-8 is tried, then Windows-1252. Checksums are verified
when present (XOR of the payload's little-endian 32-bit words, the last 1-3 bytes packed most
significant first, then the two size fields). Compression: none, MSZIP (each block `CK` and
a raw deflate stream whose dictionary is the 32 KB of output before it) and LZX. A cabinet
that is one part of a set spanning several files, and Quantum compression, are refused with
an explanation.

**LZX** (the bitstream of [MS-PATCH] without its delta features, windows of 2^15 to 2^21
bytes, the window named by the folder's compression field). Every data block expands to
32 KB (the last may be shorter) and its bits start on a fresh 16-bit boundary, but the
decoder's state (window, code lengths, recent offsets, the current block) carries on from
one data block to the next. Bits are read from little-endian 16-bit words, most significant
bit first. The stream opens with the x86 call translation flag (and its 32-bit size); then
come blocks of three kinds: verbatim, aligned-offset (part of every long offset is coded
with an extra 8-symbol tree) and uncompressed (1 to 16 bits of padding, the three recent
offsets, the bytes, a padding byte after an odd size). Code lengths are sent as changes of
the previous block's through a 20-symbol pretree, with run codes for zeros and repeats. After
decoding, x86 call translation is undone frame by frame (`E8` and a 32-bit absolute target
becomes a relative one again; not in the last 10 bytes of a frame, not after 1 GB).

The decoder is table driven (one lookup per symbol for codes up to 12 bits, which covers
nearly all of them). Measured in CPython 3.11 on a shared 4-CPU machine: about 6 MB/s on
incompressible data, about 8-12 MB/s on typical binary data and about 20-30 MB/s on the
makecab-made fixtures; Pyodide is slower.

**Limits and hostile input.** Expanded folders may add up to 1 GB (the notebook ZIP budget),
and so may the files read; a file above 256 MB is skipped before its folder is expanded. A
folder's output grows data block by data block as it is decoded (declared sizes alone
allocate nothing), and a folder is dropped once its last file was read; a runtime that runs
out of memory on a folder treats it as too large. A cabinet may list at most 16 384 files,
with names of at most 1 024 bytes. Every offset and size is checked against the bytes
present; the data blocks of all folders together (headers and payloads) may not add up to
more than the file holds, and two folders may not share data blocks, so all folders together
decode at most the file's own bytes; a compressed block may not expand to more than 32 KB,
and a folder may not declare more than 1 100 times its compressed size (LZX and deflate stay
near 1 030). In LZX data every code length, tree (an oversubscribed one is refused), match
offset (inside the window and the output so far), match length (not across a block or data
block) and block header is checked; at most 32 blocks may start within one data block
(rebuilding the trees is the costly part of a block). The call translation steps over the
markers it leaves alone with a regular expression, in C, and hands Python only the markers
that may need rewriting: those whose target is below the translation size (checked on all
four bytes, so a target such as `00E80000` is left alone although its top byte is 0) or is
negative and reaches back no further than the frame's last marker, rounded up to the next
megabyte of output. Most `E8` bytes of data that is not x86 code fall outside both, runs of
`E8` and the uniform bitmap areas tried (8-, 16-, 24- and 32-bit pixels holding the byte
232, such as #E80000, #0000E8, #FF00E8 or #9FC5E8) included; the rounding makes a negative
target count, without being rewritten, for at most about a megabyte of output before the
place where it starts to be rewritten. The expression uses no possessive quantifier or
atomic group (CPython 3.11.0 to 3.11.4 match those wrongly) and never backtracks into an
earlier pass of its loop; a check at import time compares it with the plain loop on a small
buffer and, should they ever differ, the plain loop does all the work, one unit per marker.

The call translation has a work budget of its own, in units of about 0.3 microseconds. The
whole package may spend 4 million units plus 4 per byte of the file. A marker that may need
rewriting costs one unit (rewriting takes 0.2 to 0.4 microseconds), and so does each run of
such markers. Stepping over markers in C is cheap but not free: 35 to 60 nanoseconds per
marker in the uniform bitmap areas above, up to about 95 for targets crafted to fail the
most tests, and LZX can expand a 1.3 MB package to 1 GB of them, which once took 21 seconds.
So every frame pays, before the work, one unit per three markers it may step over (counted
from its `E8` bytes, at most one per five bytes), and each megabyte of output with markers
pays 1 000 units for its expression, since compiling one takes about as long. Finding the
markers adds about a nanosecond per byte of output (at most about a second for 1 GB, less
than decoding it). Real files need well under one unit per byte (the fixtures and the sample
sections packed by the tests' encoder use 0.003 to 0.5, mostly for the expressions). A
uniform bitmap area whose colour holds the byte 232 compresses about 1000:1 and costs at
most one unit per 15 bytes, so up to about 50 MB of it fits the 4 million units of a small
package. Data that keeps a rewritten marker every few bytes and still compresses about
1000:1 (a crafted stream, or a contrived repeating pattern that the encoder translates to
one constant) can exceed the budget. Past the budget the remaining folders are refused with
a message that does not call them damaged, since such data can be valid.

A folder that fails any check makes its files unreadable, with one warning naming them and
the first reason; the other folders' sections are read. Measured in CPython 3.11, the
slowest LZX stream (nothing but two-byte matches) decodes at about 2 microseconds per
compressed byte, the most compressible streams at about 3 nanoseconds per byte of output,
and the call translation stops at its budget (0.1 to 0.3 microseconds per unit). A crafted
package therefore costs a few microseconds per byte of the file at most: a 1.3 MB package
that expands to 1 GB of the costliest markers is refused after about 5 seconds, 3 of them
spent decoding the folder and 2 in the call translation.

**Sources and licences.** `cab.py` and `lzx.py` are ported by hand from the TypeScript
`src/formats/onenote-file/cabinet/cabinet.ts` and `lzx.ts` of the Obsidian Importer
(https://github.com/obsidianmd/obsidian-importer, commit
`d2cb052c365999118c75c998254fcbb1c15a13fe`, MIT, Copyright (c) 2023 Obsidian), whose headers
say they were ported from OfficeIMO (https://github.com/EvotecIT/OfficeIMO, MIT, Copyright (c)
2022 Evotec); both MIT notices are reproduced at the top of `cab.py` and `lzx.py` (so they
travel with the files in the web bundle and the wheel) and in `NOTICE.md`. The port adds MSZIP folders, table-driven
Huffman decoding, faster bit reading, budgets and per-folder expansion, and follows [MS-PATCH]
where the two differ (1 to 16 padding bits before an uncompressed block; trees with a single
code are accepted). The facts were checked against Microsoft's [MS-CAB] and [MS-PATCH]
documentation. The Rust crate `lzxd` (MIT OR Apache-2.0) was consulted for facts and run as an
independent decoder of the tests' own LZX encoder during development; none of its code is
used. libmspack, cabextract, 7-Zip/p7zip and wimlib (GPL / LGPL) were not consulted.

**Validation.** The makecab fixtures of the Obsidian Importer (made by Windows `makecab`,
published by OfficeIMO under MIT): seven cabinets with the same 4 096-byte pattern and three
x86 calls at every window size, two that expand byte for byte to Apache Tika's
`testOneNote2016.one` and `testOneNoteFromOffice365-2.one`, and a small notebook package.
`tests/cab_builder.py` builds stored, MSZIP and LZX cabinets in the tests; its LZX encoder
(written from [MS-PATCH]) reaches what the fixtures do not (uncompressed and aligned blocks,
blocks across data blocks, run codes, every window size) and its output was decoded
identically by `lzxd`. A package exported by OneNote itself has not been tested yet
(section 8).

## 6. Why writing OneNote is not supported

* OneNote for iPad and Mac cannot open `.one` files; they only sync notebooks from OneDrive.
  Microsoft says opening an exported notebook is not supported on iOS.
* Uploading a `.one` into a notebook folder on OneDrive was reported not to become a section
  (2016); whether a freshly written cloud-format section would is unverified.
* The Microsoft Graph API documents page creation with HTML and pictures only; writing ink
  (an InkML part) appeared only in a 2017 beta and is not in the current documentation.
* A desktop `.one` writer is a large piece of work (revision store, transactions, CRCs,
  object spaces) and can only be validated with OneNote for Windows.

Two cheap experiments would decide whether a write path is worth building: posting a page
with an InkML part through Graph Explorer, and uploading a cloud-format section into a
OneDrive notebook folder and checking whether OneNote for iPad lists it.

## 7. Tests

`tests/test_onenote_synthetic.py` and `tests/test_onenote_budgets.py` build sections from
scratch (`tests/onenote_builder.py`) and need no samples: exact unit mapping, layout rules,
error paths, and crafted files that are small but expensive (dense structures, shared object
groups, a stroke, picture or paragraph used many times).
`tests/test_onepkg.py` covers `.onepkg` packages: the cabinet reader, the LZX decoder and
the package path, on the makecab fixtures and on cabinets built by `tests/cab_builder.py`
(section 5), with crafted and randomly damaged cabinets.
`tests/test_onenote.py` and `tests/test_onenote_hardening.py` use the sample sections of
onenote.rs, Joplin's onenote-converter, Microsoft's Interop-TestSuites, libmson, the Obsidian
Importer and py-onenote-parser, fetched at pinned commits as sparse checkouts (`REPOS`,
`SPARSE` and `LARGE` in `tests/conftest.py`; never committed). py-onenote-parser's 46 MB
section is only used when present under `$GNNOTE_SAMPLES` or with `GNNOTE_LARGE_SAMPLES=1`.
Per-file page, stroke, highlighter and picture counts equal those of one2html, which the
suite also runs as a subprocess oracle (per-page stroke counts and stroke colours) when its
binary is found (`GNNOTE_ONE2HTML`, `PATH`, or `$GNNOTE_SAMPLES/one2html/target/release/`).

## 8. Sample files that would close the gaps

From an iPad with OneNote, on a personal Microsoft account:

1. Make a test notebook with one page each of: pen strokes in three colours and three
   widths written with the Apple Pencil; the highlighter (yellow and one other colour) over
   typed text; the pencil tool if offered; a typed text box with bold and italic; a photo;
   a PDF inserted as a printout with ink on it; ink far below and far to the right of the
   first screen; ink that was lasso-moved and resized; a partly erased stroke; ink converted
   to shapes; and a second section.
2. Download the notebook folder from onedrive.live.com in Safari (section 1) and send the
   ZIP as it is; optionally also *Export notebook* from OneNote for the web.
3. Add a screenshot or *Send a copy* PDF of every page as ground truth.

From a PC with OneNote for Windows: the same notebook exported as a OneNote Package (*File >
Export > Notebook > OneNote Package (.onepkg)*), to check the package path on a file OneNote
made itself.
