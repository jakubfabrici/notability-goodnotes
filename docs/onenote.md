# Microsoft OneNote (read only)

gnnote reads OneNote notes and converts them to GoodNotes or Notability with the
handwriting kept as editable ink. Writing OneNote files is not supported (section 6).

* Inputs: a OneNote **section** (`.one`, as OneDrive serves it or as OneNote for Windows
  exports it) or a **notebook folder downloaded as a `.zip`** from onedrive.com or OneNote
  for the web.
* Refused with an explanation: `.onepkg` packages, a `.onetoc2` table of contents on its own,
  password-protected sections.
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
(.one)* produces a desktop `.one`, which converts too; *Notebook > OneNote Package
(.onepkg)* does not (see section 5).

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

A notebook ZIP is merged into **one document**; a warning lists the sections in the order
they were merged. The order comes from the notebook's `.onetoc2` files (one per folder;
section groups are sub-folders) and falls back to file-name order, with a warning, when
there is none. Password-protected sections inside a ZIP are skipped with a warning; a
damaged section is skipped with a warning and the others are converted.

The document is named after the section (as OneDrive records it), after the notebook folder
of a ZIP, or else after the input file.

## 3. Limitations

* Text and inline-handwriting layout are approximations: OneNote positions only the outline
  (the text block); everything inside it is laid out by OneNote at display time.
* The pressure-to-width curve is an approximation (section 4.4); strokes written with a mouse
  or finger carry a constant default pressure (0.75) and come out about 40 % wider than the pen's
  nominal width.
* Nested ink containers (not seen in any sample) are placed by their own offsets and
  scaling, as the reference renderer does.
* Printouts arrive as pictures; tables, math, tags and attachments are dropped.
* Very large pages are not split.
* No device-made iPad sample has been tested yet; the OneDrive (FSSHTTPB) samples come from
  OneNote for the web, Windows and Android. See section 8 for the files that would close the
  gaps.

## 4. Format facts the reader relies on

Sources: Microsoft's [MS-ONESTORE] (v20250520), [MS-FSSHTTPB] (v20240820) and [MS-ONE]
(v20221115), published under the Open Specification Promise; the ink layout, which no
specification documents, from the onenote.rs wiki's "Ink Object Format" with five errata
re-verified on real files (section 4.3). No third-party code was used.

### 4.1 Packagings

| Variant | Header GUID at 0 | Header GUID at 48 | Where it comes from |
|---|---|---|---|
| Desktop revision store, section | `{7B5C52E4-D88C-4DA7-AEB1-5378D02996D3}` | `{109ADD3F-911B-49F5-A5D0-1791EDC8AED8}` | OneNote for Windows exports and backups |
| Desktop revision store, table of contents | `{43FF2FA1-EFD9-4C76-9EE2-10EA5722765F}` | same | `.onetoc2` |
| Alternative packaging (MS-FSSHTTPB) | `{7B5C52E4-...}` | `{638DE92F-A6D4-4BC1-9A36-B3FC2511A5B7}` | **OneDrive / SharePoint downloads**; the cell schema `{1F937CB4-...}` (`.one`) / `{E4DBFD38-...}` (`.onetoc2`) tells them apart |
| `.onepkg` | `MSCF` cabinet of desktop `.one` / `.onetoc2` files | - | OneNote for Windows *Export > Notebook* |

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
32); fragments may not reassemble to more than the file holds; a document holds at most 4
million ink points and 512 MB of pictures; ZIP members above 256 MB (declared) or beyond
1 GB in total are skipped. An encrypted section is detected from its revision structure and
refused before any property set is decoded. Nothing but `ValueError` leaves `read_onenote`.

## 5. Why `.onepkg` is refused

A `.onepkg` is a Microsoft Cabinet archive whose files are LZX-compressed; Python's standard
library has neither, and an iPad user cannot make one anyway (only OneNote for Windows
exports it). The OneDrive ZIP of the same notebook carries the same sections. A CAB/LZX
reader is possible later (an MIT-licensed reference exists).

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
