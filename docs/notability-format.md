# The Notability `.note` format — enough to READ any version and WRITE files current Notability opens

Status: reverse-engineered from 21 real `.note` files (Notability 4.2 → 16.1.5, 2018 → 2026),
three class-dump headers of the app, and cross-checked against three independent third-party
readers. Nothing comes from Notability documentation. Every claim carries a confidence tag:

* **[verified]** — read directly from bytes of files Notability wrote, checked across versions
* **[inferred]** — consistent with all samples but not provable without running the app
* **[third-party]** — taken from another reverse-engineering project and *not* contradicted here

The one-page PoC (`scratchpad/poc/gn2note_poc.py`, built on the Notability 10.4 template) opened in
the user's current Notability; that is the baseline "known to be accepted" structure. This document
describes everything beyond it.

Notation: `W` = `pageWidthInDocumentCoordsKey`. Struct formats are Python `struct` (`<` = little
endian, `f` = float32, `i` = int32, `Q` = uint64, `e` = float16). `#n` in Part 2 = `$objects[n]`.

---

## 0. Evidence base

| sample (path under `scratchpad/ref/`) | app | `sessionFormatVersion` | written | what it contains |
|---|---|---|---|---|
| `svg2notability/template.note` | 7.2.5 (iOS) | 4 | 2018-03 | 4 strokes, W 537.6, old `NS.key.N` encoding, no `curvesstyles` |
| `notability-reader/bdb_transazioni/` | 8.4.8 | 4 | 2019 | 25-page PDF-backed (720×540 landscape), **audio recording with 183 events**, HandwritingIndex, NBPDFIndex |
| `more-notes/notability-lib-private/*.note` | 4.2.5 / 4.4.3 (Mac), 8.2.3, 9.3.4, 9.3.5 (iOS) | 4 / 5 | 2018–2020 | PNG & JPEG images (`Test Bild File`, `Very Simple Test Img and forms`), shapes |
| `notability-to-svg/example.note` (= PoC template) | 10.4 | 5 | 2021-06 | 399 strokes, W 574 |
| `more-notes/notesconverter/samples/notability/*.note` (11 files) | 10.2.4 | 5 | 2025-12 → 2026-01 | `image-insert` (4 JPEGs, crop, rotation), `text` (9 text boxes), `page-layout` (4 PDFs, blank pages, A4 + landscape), `bookmarks`, `long-pdf-doc` (32 PDF pages), `long-mixed-doc` (64 layout entries), `shapes`, `handwriting` (highlighter), `demo`, `empty` |
| same dir, `demo15.note`, `empty15.note` | 15.3 | 9 | 2025-12 | new paper-layout model, `thumb12x.png`, `curveUUIDs`/forces/azimuth arrays |
| `more-notes/denotability/resources/testfile.note`, `testnote.note` | 11.7.2 | 7 | 2022-10 | PDF template page, **recording (m4a)**, `MathMediaObject` (LaTeX), `TextBlockMediaObject`, W 610 "Letter" |
| `more-notes/Notability-notes-converter/input/NOTE Note Apr 26, 2026.note` | 16.1.5 | 9 | 2026-04 | JPEG+PNG images, GIF asset, stickies (`CanvasMediaObject`), **ink groups**, shapes, dashed/dotted strokes, typed text with lists/fonts, 596+103 strokes |

Third-party readers used for cross-checks (all cloned under `scratchpad/ref/more-notes/`):
`samuelsadok/notesconverter` (`converter/notability.py`, the most careful one, validated against
Notability's own PDF export), `nokcha0/Notability-notes-converter` (Rust, 2026, groups/shapes/stickies),
`lh/notability-to-pdf` (`docs/NOTABILITY_FORMAT.md`), `marvinborner/zathura-note` (C),
`cable729/inkterop`, `itscrystalline/NotabilityShortcuts` (class-dump headers of Notability
≈ 2023: `NoteTakingSession.h`, `FormattedString.h`, `InkedSpatialHash.h`, `PDFFile.h`,
`NBReflowState.h`, `GenericMediaObject.h`).

Scripts used (scratchpad `work/`): `nsdump.py` (UID-resolving tree dump), `unarchive.py` (archive →
plain Python), `inkstats.py` (array census), `hwipages.py` (HandwritingIndex vs ink), `notereader.py`
(the reader sketch of Part 9, run successfully on all 21 samples).

---

## 1. The ZIP container

A `.note` is a ZIP whose members all live under one top-level folder `<Name>/`. **Locate
`Session.plist` and treat its parent as the bundle root** — the folder name usually equals
`packagePath` inside Session.plist but was observed to differ (notesconverter saw an earlier note
name; `demo15.note` has folder `demo2`). **[verified]**

Members, by the version that wrote the file (✓ = present, – = absent):

| member | 7.2.5 | 8.4.8 | 10.2.4 | 10.4 (PoC template) | 11.7.2 | 15.3 / 16.1.5 | required to write? |
|---|---|---|---|---|---|---|---|
| `Session.plist` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | **yes** |
| `metadata.plist` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | **yes** (PoC keeps it) |
| `thumb.png`, `thumb2x.png`, `thumb3x.png`, `thumb6x.png` | ✓ | ✓ | ✓ | ✓ | ✓ | only if created in an older version | PoC writes all four → **keep** |
| `thumb12x.png` | – | – | – | – | – | ✓ (the only thumb in a fresh 15.3/16.x note) | optional |
| `thumbnail`, `thumbnail2x` (NSKeyedArchiver `UIImage` bplists) | ✓ | ✓ | ✓ | **–** | – | – | **no** (template lacks them and opens) |
| `Recordings/library.plist` (XML plist) | ✓ | – | ✓ | ✓ | ✓ | ✓ | keep (template has it); bdb lacks it although it *has* a recording event log |
| `Recordings/<name>.m4a` | | | | | ✓ | | only with recordings |
| `Assets/` (dir) | ✓ | – | ✓ | ✓ | ✓ | ✓ | empty dir entry is what the template has |
| `Assets/<assetsIdKey>/media.gif` | | | | | | ✓ (16.1.5) | only for GIF images |
| `Images/`, `Images/Image .jpg`, `Image 1.jpg`, `Image N.png` … | ✓(dir) | – | ✓ | ✓(dir) | ✓(dir) | ✓ | only with images |
| `PDFs/<UUID>.pdf` | – | ✓ | ✓ | `PDFs/` dir only | ✓ | ✓ | only with PDF pages |
| `HandwritingIndex/index.plist` | – | ✓ | ✓ | dir only | ✓ | – (16.1.5: dir only) | **no** (recognition cache) |
| `NBPDFIndex/NoteDocumentPDFMetadataIndex.plist`, `NBPDFIndex/PDFIndex.zip` | – | ✓ (extracted form) | ✓ | – | **dir only although the note has a PDF** | ✓ in demo15 | **no** (11.7.2 proves it optional) **[verified]** |
| `` (empty-name entry) | | | | | | ✓ (15.3, 16.1.5 write one) | no |

ZIP encoding **[verified]**: files straight from the app (11.7.2, 15.3, 16.1.5) are **stored**
(method 0), flag bit 0x800 (UTF-8 names), "made by" UNIX, with explicit directory entries.
Older samples in the wild are deflated (re-zipped). The PoC writes deflate and opens, so either works.
No archive comment. Member order is irrelevant.

Thumbnails **[verified]**: PNG, 8-bit RGBA (colour type 6) in app-written files; the PoC's RGB PNG is
accepted. Size = the first page fitted into a 48×63 box at 1×, i.e. `thumb.png` 48×63 for the legacy
1.3125 page, 44×63 for an A4 PDF page (ratio 1.4318 = 63/44), 48×62 for a true Letter page;
`2x` = 96×126, `3x` = 144×189, `6x` = 288×378, `12x` = 576×756. The ratio of the thumbnail is
therefore a usable fallback for the page aspect ratio. `thumbnail`/`thumbnail2x` are
`NSKeyedArchiver` archives of a `UIImage` (`UIImageData` = the same PNG, `UIScale` 1.0/2.0).

`Recordings/library.plist` is an **XML** plist **[verified]**:

```xml
{'application version': '4631', 'library-format-version': '1.0', 'recordings': {}}
```

with, when a recording exists (11.7.2), `recordings = {'1': {createddate: <date>, duration: 1462.05,
filepath: 'Recording 1.m4a', filesize: 5939942, filetype: 'm4a', identifier: 1, name: 'Recording 1',
userFacingName: ''}}`. `application version` is the app build number (3392 = 7.2.5, 4631 = 10.4,
5254 = 11.7.2, 6594 = 15.3).

---

## 2. Keyed-archive conventions (how to read/write Session.plist with `plistlib`)

`Session.plist` is a binary plist (`bplist00`) holding a keyed archive **[verified]**:

```
{'$version': 100000, '$archiver': 'GLKeyedArchiver',      # Ginger Labs subclass; metadata.plist uses 'NSKeyedArchiver'
 '$top': {'$0': UID(1)},                                   # Session.plist uses '$0'; metadata.plist and nested archives use 'root'
 '$objects': ['$null', <root dict>, ...]}                  # $objects[0] is always the string '$null'
```

* Object references are `plistlib.UID(n)` → `$objects[n]`. A value may be a UID *or* an inline
  scalar (ints, floats, bools, bytes, short strings are inlined by GLKeyedArchiver; e.g.
  `numcurves` is a UID to an int while `curvespoints` is inline `bytes`). **Always `deref` first.**
* Class tags: `'$class': UID → {'$classname': 'InkedSpatialHash', '$classes': [...]}`; `$classes`
  may be missing (10.4 writes `InkedSpatialHash` without it). Swift classes are module-prefixed:
  `Notability.PaperAttributes` (11.7), `GLModel.PaperAttributes` (15.3+), `Notability.MathMediaObject`.
* `NSArray`/`NSMutableArray`: **either** `{'NS.objects': [..]}` **or** `{'NS.object.0': .., 'NS.object.1': ..}`.
  `NSDictionary`: **either** parallel `NS.keys`/`NS.objects` lists **or** `NS.key.N`/`NS.object.N`.
  Which form is used is *not* tied to `sessionFormatVersion`: 7.2.5, 8.4.8 and the 10.2.4 samples
  use the indexed form, 4.2.5, 9.3.4, 10.4, 11.7.2, 15.3, 16.1.5 use the list form. Indexed keys are
  not necessarily in order (`NS.object.13` before `NS.object.12` in bdb). Readers must accept both.
* Strings: plain `str`, **or** `NSMutableString` `{'NS.bytes': b'utf-8'}` (10.4 template:
  `packagePath`, `assetsIdKey`, backing strings), **or** `{'NS.string': '..'}`. `'$null'` (UID 0)
  means nil.
* `NSDate` = `{'NS.time': <seconds since 2001-01-01 UTC>}`; `NSUUID` = `{'NS.uuidbytes': 16 bytes}`;
  `UIColor` = `{UIColorComponentCount: 4, UIRed, UIGreen, UIBlue, UIAlpha, NSColorSpace: 2, NSRGB: b'0 0 0'}`
  (greyscale variant: `UIColorComponentCount: 2, UIWhite, NSColorSpace: 4`; Mac 4.x writes `NSColor`
  with an ICC profile). 15.3 adds `UIRed-Double` etc.
* Geometry is stored as **strings**: points `'{x, y}'`, sizes `'{w, h}'`, rects `'{{x, y}, {w, h}}'`,
  ranges `'{location, length}'`; colours also as `'r,g,b,a'` strings with 15 decimals
  (`...CrossPlatform` keys).
* `plistlib.dumps(pl, fmt=plistlib.FMT_BINARY)` round-trips all of this (UIDs included); the PoC
  relies on it.

---

## 3. `NoteTakingSession` (root of Session.plist)

Keys observed, with the versions that write them (**[verified]** unless noted):

| key | type | 7.2.5 | 8.4.8 | 10.2.4 / 10.4 | 11.7.2 | 15.3 / 16.1.5 | meaning |
|---|---|---|---|---|---|---|---|
| `richText` | `FormattedString` | ✓ | ✓ | ✓ | ✓ | ✓ | **the document** (Part 4) |
| `name`, `packagePath` | str / NSMutableString | ✓ | ✓ | ✓ | ✓ | ✓ | note title; both equal (NFC-normalised) |
| `subject` | str | ✓ | ✓ | ✓ | ✓ | ✓ | divider/subject; `'unsortedNotesKey'` = none, `'Unfiled Notes'` also seen |
| `tags` | str | ✓ | ✓ | ✓ | ✓ | ✓ | `''` |
| `creationDate` | NSDate | ✓ | ✓ | ✓ | ✓ | ✓ | |
| `isReadOnly` | bool | ✓ | ✓ | ✓ | ✓ | ✓ | False |
| `sessionFormatVersion` | int | 4 | 4 | 5 | 7 | 9 | format generation |
| `NBNoteTakingSessionBundleVersionNumberKey` | str | '7.2.5' | '8.4.8' | '10.2.4'/'10.4' | '11.7.2' | '15.3'/'16.1.5' | app version that saved |
| `NBNoteTakingSessionMinorVersionNumberKey` | int | 3 | 3 | 4 / 5 | 0 | 0 | |
| `NBNoteTakingSessionHandwritingLanguageKey` | str | – | 'it_IT' | 'en_US' | ✓ | ✓ | recognition language |
| `paperIndex` | int | 12 | 12 | 12 (14 in 10.4 template) | 12 / -1 (PDF template) | **–** | paper style id (12 = white) |
| `paperLineStyle` | int | 0 | 0 | 0 (2 in 10.4 template) | 0 | **–** | line style (0 = none) |
| `NBPaperMajorVersionNumber`, `NBPaperMinorVersionNumber` | int | – | – | 1, 1 | ✓ | 3, 0 (new note) / 1, 1 (migrated) | paper engine version |
| `NBNoteTakingSessionIsHighlighterBehindTextKey` | bool | – | – | True | True | True | |
| `NBNoteTakingSessionAllowHighlighterConversionKey` | bool | – | – | False | ✓ | False | |
| `contentPlaybackEventManager` | `NBCPEventManager` | ✓ | ✓ | ✓ | ✓ | ✓ | recording ↔ stroke timeline (Part 8) |
| `NBNoteTakingSessionDocumentPaperLayoutModelKey` | `NoteDocumentPaperLayoutModel` | – | – | – | ✓ | ✓ | **new home of paper attributes and `pageLayoutArray`** (Part 5.3) |
| `NBNoteTakingSessionLastUsedFontKey` | `GLFontDescriptor` | – | – | – | ✓ | ✓ | UI state (family, name, size 12, bold/italic/underline/strikethrough, indent, colour) |
| `NBNoteTakingSessionHidePDFAnnotationsKey` | bool | – | – | – | – | True | |
| `kNBNoteTakingSessionNoteLinkStoreKey` | – | – | – | – | `$null` | – | note links |

Writer rule: the PoC template (10.4, format 5) root is sufficient for current Notability; keep it as
is, change only `name`/`packagePath`/`subject`/`creationDate`.

---

## 4. `richText` — `FormattedString`

| key | type | meaning |
|---|---|---|
| `formatVersion` | int | 4 (7.2.5 … 11.7.2), 5 (15.3+) |
| `Handwriting Overlay` | `HandwritingObject{SpatialHash: InkedSpatialHash}` | **all free ink** (Part 6) |
| `reflowState` | `NBReflowStateLocked{pageWidthInDocumentCoordsKey: float, nativeLayoutDeviceStringKey: str}` or `NBReflowStateReflowable{}` | **defines the coordinate system** (Part 5). `Reflowable` is written for a note that was never drawn on (`empty.note`, `empty15.note`) and for text-box stores. `nativeLayoutDeviceStringKey`: `'iPad'`, `'Letter'`, (third-party: `'Mac'`, `'iPhone'`, `'A4'`, `'iPad (Default)'`) |
| `didBecomeReflowable` | bool | True everywhere |
| `pdfFiles` | NSArray of `PDFFile` | imported PDFs (Part 5.3) |
| `pageLayoutArray` | NSArray of NSDictionary | page list **when the note has PDF pages or bookmarks/blank-page markers**; empty for plain notes. 15.3+ keeps this array *and* `NBNoteTakingSessionDocumentPaperLayoutModelKey.pageLayoutArray` (demo15: root one empty, model one has 1 entry) |
| `mediaObjects` | NSArray | images, text boxes, stickies, math (Part 7) |
| `attributedString` | NSDictionary `{stringKey, subRangesKey}` | the **flow text** of the note (Part 7.5) |
| `NBAttributedBackingString` | `NBAttributedString{NBAttributedBackingStringCodingKey, NBAttributedLayoutStringCodingKey}` | two more copies of the same attributed string (identical in every sample; ranges sometimes split differently [third-party]) |
| `recordingTimestampString` | NSDictionary `{stringKey, subRangesKey}` | a parallel attributed string, **same length as `attributedString.stringKey`** (even the same characters), one sub-range `{0, len}` with empty attributes; stores per-character recording timestamps |
| `formattedStringTextAlignmentKey` | int | 0 left, 1 centre, 2 right (missing before 10.x) |
| `Handwriting Objects` | NSMutableArray | always empty; written up to 10.4, gone in 11.7.2+ |
| `indentDictionaryAtEndOfString` | dict | [third-party] list state after the last character |

All arrays are present even when empty (the template has them empty): **a writer must keep every
one of these keys**; the PoC did and was accepted.

---

## 5. Coordinates and pages

### 5.1 The document coordinate system **[verified]**

One continuous 2-D space for the whole note, y down, unit = "document points". Pages are stacked
vertically with **no gap**; the page separator line occupies no space. A stroke's page is implied
by its y coordinate. `W = pageWidthInDocumentCoordsKey` is the width of a page in these units and
depends on the device/app that created the note: 537.6 (7.2.5), 565 (8.4.8 … 15.3 iPad; the most
common), 574 (10.4 template), 583.8 / 652.8 (9.3 / Mac 4.x), 610 (11.7.2 "Letter"), 667.8, 679
(16.1.5 iPad), [third-party: 768 for 2013 iPad, 256 for iPhone]. Everything (ink, media origins,
widths) scales with `W`; a note exports to PDF at 8.5 in width regardless of `W` [third-party,
consistent with the 1.3125 ratio below].

x = 0 is not the paper's left edge: ink reaches x = −14.5 (`page-layout.note`, written at the left
margin) and HandwritingIndex origins of −15.27 occur. notesconverter derives
`left_edge = −W·20/768` (= −14.7 for W 565) from Notability's PDF export. **[third-party, consistent]**
Right edge ≈ `W − W·20/768`. For a converter: map GoodNotes page x ∈ [0, pw] to
`x_doc = −0.026·W + (x/pw)·W`, or ignore the 2.6 % offset (the PoC ignores it and looks fine).

### 5.2 Page height **[verified]**

Plain-paper ("legacy") pages: `H = 1.3125 · W` (= 803.25/612). Evidence: every legacy thumbnail is
48×63 (ratio exactly 1.3125) across 4.2.5 → 16.1.5; notesconverter measured Notability's PDF export
as 612 × 803.25 pt; and `image-insert.note` page 2 ink starts at `y − 741.5625 = 93.4` where the
HandwritingIndex says the page content origin is 93.2 — a Letter ratio (792/612) would be 10 pt off.
15.3/16.1.5 record this as `paperIdentifier: 'Legacy:13'`, `paperSizingBehavior: 'lockedWidth:565.0:iPad'`.

True Letter pages: the 11.7.2 note created with `nativeLayoutDeviceStringKey 'Letter'`, W 610,
`paperSizingBehavior: 'staticWidth'`, `paperSize: 'letter'` has 48×62 thumbnails (1.2917);
nokcha0 uses `H = floor(W · 11 / 8.5)` for that combination (= 789 for W 610, ratio 1.2934).
**[inferred]** Other `paperSize` values (A4…) presumably follow their paper ratio — not observed.

PDF-backed pages (Part 5.3): the page is scaled to width `W` keeping its aspect ratio and occupies
`H_i = ceil(pdf_h · W / pdf_w)` document points **[verified]**: on `page-layout.note` (2 blank +
2 A4 + 1 landscape A4 pages) the ink-vs-HandwritingIndex offset is a constant 1.47 pt on every page
only with `ceil`; with the exact float height pages 5–6 drift by 3.4–3.9 pt and one stroke lands on
the wrong page. The same holds for the 2019 bdb note (720×540 → 424 per page). notesconverter
reports the PDF content is bottom-aligned inside the rounded-up slot.

Page bounds therefore are:

```
y = 0
for item in pageLayoutArray:            # may be shorter than the real page count
    h = ceil(pdf_h*W/pdf_w) if item is a PDF page else H_plain
    page = (y, y+h); y += h
# anything below the last laid-out page lies on implicit plain pages of height H_plain
page_index(y) = index of the slot containing y, or len(layout) + floor((y - y_end)/H_plain)
```

A plain-paper note with no `pageLayoutArray` is simply `page_index = floor(y / H)`. Notes can have
ink above page 1 (y < 0, the title area) — `text.note` has a dot at y = −1.

### 5.3 PDF-backed notes **[verified]**

`PDFFile` object (in `richText.pdfFiles`, referenced from layout entries):

| key | 8.4.8 | 10.2.4 | 11.7.2 / 15.3 | meaning |
|---|---|---|---|---|
| `pdfFileName` | `'350DE7DB-….pdf'` | ✓ | ✓ | file under `PDFs/`, always `<UPPERCASE-UUID>.pdf` in the samples (arbitrary names are reported to work [third-party]) |
| `contentBoxVersion` | 1 | 1 | 1 | |
| `highlights` | `[]` | `[]` | `[]` | |
| `version_4_1_OrLater` | True | True | – | |
| `pageNumbers` | `$null` | `$null` or `[1]`, `[1, 2]` | – | which pages are used (informational) |
| `version` | **absent** | 2 | 2 | 2 = scale to width keeping aspect (observed everywhere); third-party: 1 = old "8.5 in width, keep height" behaviour. The 8.4.8 note lacks the key yet behaves as 2 (page height 424 = ceil(540·565/720)) — treat absent as 2 but verify with the HandwritingIndex if present |
| `type` | – | – | 0 (imported) / 1 (paper template PDF) | |

`pageLayoutArray` entries (NSDictionary, one per document page, in page order):

```
PDF page (old, ≤ 10.4):  {kPageLayoutPDFPageNumberKey: 1-based page in the PDF,
                          kPageLayoutPDFFileKey: <PDFFile object>,
                          kPageLayoutPDFIsOriginalPageKey: bool (optional; True = page was in the PDF as imported, False = added/duplicated)}
PDF page (15.3+):        {kPageLayoutPDFPageNumberKey: n, kPageLayoutPDFFileNameKey: 'UUID.pdf',
                          kPageLayoutDocumentPageNumberKey: 1-based position in the note,
                          kPageLayoutPDFIsOriginalPageKey: bool, kPageLayoutPageIsBookmarkedKey: bool}
blank page:              {kPageLayoutPDFPageNumberKey: 9223372036854775807}   # INT64_MAX (2013 files: 0x7fffffff)
                          + optional kPageLayoutPageIsBookmarkedKey: True (bookmarks.note), kPageLayoutPDFIsOriginalPageKey: False
```

Trailing blank entries may exist without content and content may exist beyond the last entry
(implicit plain pages). Multiple PDFs and any page order are allowed (`page-layout.note`: pages =
blank, blank, PDF-A p1, PDF-A p2, PDF-B p1; `NBPDFIndex` lists `{PDF-A: [1,3,2,4], PDF-B: [1,5]}`).

`NBPDFIndex/` is a search/layout cache: `NoteDocumentPDFMetadataIndex.plist` =
`{'version': 26, 'pdfFilesMD5': md5-hex, 'pageNumbers': {'UUID.pdf': [doc page numbers…]}}` (the
list has each page twice in multi-page samples; demo15 carries the MD5 of the empty string, i.e. it
is stale) and `PDFIndex.zip` containing per-PDF `PDFMetadataIndex.plist` (media/crop/… boxes per
page, `rotation`, link storage), `PDFTextIndex.txt` (extracted text) and `PDFLayoutIndex.nbpdflayout`
(uint16 stream, undecoded). **Optional**: the 11.7.2 note has a PDF and only an empty `NBPDFIndex/`
directory.

### 5.4 Paper attributes (11.7.2+)

`NBNoteTakingSessionDocumentPaperLayoutModelKey = NoteDocumentPaperLayoutModel{documentPaperAttributes, pageLayoutArray}`
with `PaperAttributes{paperIdentifier: 'Legacy:13' | 'Color:#FFF48E' | 'TemplatePDF:<UUID>:#FFFFFF',
drawRotatedForOrientation: True, paperOrientation: 'portrait', paperSize: 'letter',
paperSizingBehavior: 'lockedWidth:565.0:iPad' | 'staticWidth' | 'deviceBasedWidth',
alignsTextToLines: bool, lineStyle2: 'No Lines' | 'Lines:false:false:0.25', lineStyle: 0}`.
`lockedWidth:<W>:<device>` repeats `W` (useful when `reflowState` is `Reflowable`). Not needed for
writing a 10.4-format file.

---

## 6. Ink — `InkedSpatialHash` (the "Handwriting Overlay")

### 6.1 Locating it

`root.richText['Handwriting Overlay'].SpatialHash`. **Do not search `$objects` for the first dict
containing `curvespoints`**: every `TextBlockMediaObject.textStore` is a full `FormattedString` with
its own (empty) overlay, and it can precede the real one in `$objects` (`demo15`, `testfile`); the
PoC's `next(o for o in objs if 'curvespoints' in o)` would read 0 strokes from those files.
Grouped ink (16.x) lives in nested archives inside `groupsArrays` (6.6).

### 6.2 Core parallel arrays **[verified on 21 files, 0 violations]**

Let `N = numcurves`, `P = numpoints`, `F = numfractionalwidths` (ints, usually UIDs → `$objects`).

| key | bytes | struct | per | meaning |
|---|---|---|---|---|
| `curvesnumpoints` | 4N | `<Ni` | curve | points in curve i: **`n_i = 1 + 3·k_i`** (k_i ≥ 0 cubic Bézier segments; n = 1 occurs for dots, max seen 673) |
| `curvespoints` | 8P | `<{2P}f` | point | x,y pairs, all curves concatenated, Σn_i = P. Curve = anchor, (c1, c2, anchor)×k |
| `curvesfractionalwidths` | 4F | `<Ff` | **anchor** | width multiplier at each anchor: **k_i + 1 values per curve**, Σ(k_i+1) = F exactly (equivalently `3F − 2N = P`). Range seen 0.27 … 2.13; constant 1.0 for non-pressure pens |
| `curveswidth` | 4N | `<Nf` | curve | base width in document points (pen 0.37 … 3.7, highlighter 4.4 … 22). Effective width at an anchor = `curveswidth[i] · fractional` |
| `curvescolors` | 4N | raw bytes | curve | **R, G, B, A as 4 uint8** (not floats): `ed3624ff` red, `fa9d00ff` orange, `006fffff` blue, `1749b3ff`, `000000ff` black |
| `curvesstyles` | N (nominal) | uint8 | curve | **3 = pen, 4 = highlighter, 5 = third tool (16.1.5 only, opaque colours, widths 1.3–1.8 → pencil/fountain [inferred])**. Absent in 7.2.5, 8.2.3, 8.4.8 (→ treat as pen, or highlighter if alpha < 255). **May be shorter than N**: the 16.1.5 sample has 149 bytes for 596 curves — index with a bounds check |
| `eventTokens` | 4N | `<Ni` | curve | recording event id linking the stroke to `NBCPEventManager` (Part 8); `-1` (0xffffffff) = none. Written through 10.4; absent in 11.7.2+ (replaced by `curveUUIDs`) |
| `numcurves`, `numpoints`, `numfractionalwidths` | int | | | counts; `numpoints` = total points (P), `numfractionalwidths` = F |

Highlighter **[verified]**: style 4 **and** alpha = 0x6b (107/255 ≈ 0.42) in 10.2.4 / 15.3 / 16.1.5
(`fa9d006b`, `a9ff176b`, `14c7de6b`, `ed36246b`, `2fda776b`), alpha 0x44 (68) in 8.4.8 with width
11.0 vs pen 0.74. The alpha is the *stored* colour; the style byte decides tool behaviour. Writer:
set both (`style 4`, alpha 0x6b, wide width, fractional widths all 1.0); 15.3 shows highlighter
widths 7.36 and 11.04 (W 565), 10.2.4 shows 22.07.

Bézier semantics **[verified by geometry]**: for the 2021 template, anchor→control distance is
≈ 1/3 of anchor→anchor (0.56 vs 1.66), i.e. a smoothing fit with C1 continuity; rendering as
`M a0 C c1 c2 a1 C …` reproduces the thumbnails. The PoC converts polylines with Catmull-Rom →
cubic, which Notability accepted and renders smoothly. Points are **absolute** document coordinates
(no per-stroke origin, unlike the newer `.ntb` format).

Width profile: notesconverter linearly interpolates the fractional width between anchors along each
segment (third-party; rendering detail only). Observed pens with pressure: multipliers 0.53 … 1.88,
median ≈ 0.9 relative to `curveswidth`.

### 6.3 Per-curve extras (11.7.2 / 15.3 / 16.1.5) **[verified lengths]**

| key | bytes | struct | per | observed |
|---|---|---|---|---|
| `curveUUIDs` | 16N | raw | curve | random 16-byte UUIDs; in a note with a recording they are the join key to `NBCPTimeManagerSOAEventUUIDsKey` [inferred, the 11.7.2 sample's ink predates its recording] |
| `options` | 8N | `<NQ` | curve | all 0 (16.1.5: 8 × 149, i.e. same short count as `curvesstyles`) |
| `curvesforces` | 4F | `<Ff` | anchor | Pencil force, 1.0 when unknown |
| `curvesaltitudeangles` | 4F | `<Ff` | anchor | radians, π/2 (1.5708) when unknown |
| `curvesazimuthunitvector` | 8F | `<{2F}f` | anchor | unit vector, (1, 0) when unknown |
| `bezierPathsDataDictionary` | NSMutableDictionary int → bytes | | | render cache keyed by curve index (keys 77, 154, … in 16.1.5), values are Apple `CGPath` data (magic `25 b3 e5 48`). **Empty in every ≤ 15.3 file; write it empty** |
| `dashStyles` | bplist bytes | | | `{'objectPatterns': {'<curve index>': {'pattern': 1 dashed / 2 dotted}}}`; solid curves are absent |
| `shapes` | bplist bytes | | | `{'shapes': [..], 'indices': [z-order ints], 'kinds': ['line','rectangle','square','triangle','polygon','circle','ellipse', 'partialshape'…]}`; each shape dict: `appearance{style: 3/4, strokeColor{rgba:[4 floats]}, strokeWidth, fillColor?, dashStyle{pattern, arrowHead}}`, `rect [[x,y],[w,h]]`, plus `points`+`isClosed` (polygons), `startPt`/`endPt`(+`controlPoint1/2`) (lines), `rotatedRect{corners:[4 pts]}` (ellipses); 15.3+ adds `uuid`, `options` |
| `groupsArrays` | NSMutableArray of bplist bytes | | | see 6.6; empty array in ≤ 15.3 |

The 2026 anomaly (styles/options for 149 of 596 curves) means a writer must not rely on Notability
tolerating *missing* arrays the other way round: write **all** of `curvespoints`, `curvesnumpoints`,
`curveswidth`, `curvesfractionalwidths`, `curvescolors`, `curvesstyles`, `eventTokens` with exact
lengths (the template set).

### 6.4 Writing rules (what the PoC proved + constraints above)

* n_i ≡ 1 (mod 3); exactly k_i+1 fractional widths; `numpoints`/`numcurves`/`numfractionalwidths`
  must match the byte lengths; all little-endian float32/int32.
* One dot: n = 1 works in app-written files (demo15, text.note), but the PoC pads to a tiny segment
  (n = 4) — either is acceptable.
* Colour = 4 bytes RGBA; width in document points (scale GoodNotes pt widths by `W/page_width_pt`).
* `eventTokens` all `-1`; `curvesstyles` 3 or 4; `groupsArrays`/`bezierPathsDataDictionary` empty.
* Keep the `$class` UID of the hash pointing at `{'$classname': 'InkedSpatialHash'}`.

### 6.5 Shapes — write support not attempted; GoodNotes shapes should be emitted as ordinary curves.

### 6.6 Ink groups (16.1.5) **[verified]**

Each `groupsArrays` element is a bplist `{'inkGroup': {...}, 'index': int}`; `inkGroup` =
`{inkGroupObjects: [{type: 1, object: <bytes>} | {type: 2, object: <shape dict>}], transform: [a, b, c, d, tx, ty] (CGAffineTransform, e.g. [0.592,0,0,0.592,8.77,161.86] for a scaled group, [0,-1,1,0,…] for a rotated one), isCustomSticker: False, appearance: {style, strokeColor, strokeWidth}, options: 0, uuid: 16 bytes}`.
A type-1 `object` is a complete **`NSKeyedArchiver` archive whose root is another `InkedSpatialHash`**
(same keys as 6.2/6.3, one or a few curves, coordinates in group space). Reader: unarchive, parse,
apply `transform` to points and scale widths by `hypot(a, b)` (nokcha0 does the same). `index` is
the z-position among top-level curves [inferred].

---

## 7. Media objects, images, text

### 7.1 Common `GenericMediaObject` fields **[verified]**

| key | type | meaning |
|---|---|---|
| `documentOrigin`, `documentContentOrigin` | `'{x, y}'` strings | top-left corner in document coordinates before rotation (both always equal) |
| `unscaledContentSize` | `'{w, h}'` | displayed size (document points) after crop, before rotation |
| `rotationDegrees` | float | **radians** despite the name (values 0.17, 0.93, −0.55 seen) [verified by magnitude, third-party agrees]; rotation about the centre |
| `zIndex` | int | stacking order |
| `cornerMode` | int | 1 = square corners, 0/2 = rounded (third-party) |
| `textWrapMode` | int | 1 |
| `minDimension` / `maxDimension` | float | 8.0 / 1024.0 (resize limits) |
| `assetsIdKey` | NSMutableString UUID | id, also the `Assets/<id>/` folder name for GIFs |
| `UUID` | NSUUID | 11.7.2+ |
| `handwritingZIndecesKey` | NSMutableArray of int | indices of ink curves drawn on top of this object (ties ink to the object for move/delete) — empty is fine |
| `recordingEventID` | int | −1 (≤ 10.2.4) |
| `isFlippedHorizontal`, `isFlippedVertical`, `locked` | bool | 15.3+ / 16.1.5 |
| caption: `isCaptionEnabled` False, `captionFieldText` `$null`, `captionFontName` 'HelveticaNeue', `captionFontSize` 18/12, `captionFontColor` UIColor, `captionFontColorCrossPlatform` '0,0,0,1', `captionIsUnderlined` False | | |

### 7.2 `ImageMediaObject` **[verified in 4.2.5 (PNG), 9.3.4, 10.2.4, 15.3, 16.1.5]**

```
ImageMediaObject
 ├ (common fields)                       documentOrigin '{145.0, 270.5}', unscaledContentSize '{177.2, 118.1}', rotationDegrees 0.1708
 ├ indexable: False
 ├ figure: Figure
 │   ├ FigureObjectTypeKey: 1
 │   ├ FigureCanvasSizeKey: '{0, 0}'
 │   ├ kFigurePrimitiveObjectsArrayKey: []        (NSMutableArray)
 │   ├ FigureCropRectKey: '{{0, 0}, {2592, 1728}}'  crop in source-image PIXELS; full image when uncropped
 │   ├ $0: $null | bytes(93)                       CGPath of the crop (magic 25 b3 e5 48); present only when cropped
 │   └ FigureBackgroundObjectKey: ImageObject (DrawObject)
 │       ├ kImageObjectSnapshotKey: GLSnapshot{relativePath: 'Images/Image 2.jpg', saveAsJPEG: True|False, imageIsMissing: False}
 │       ├ rect: '{{0, 0}, {0, 0}}'
 │       ├ fillColor UIColor(0,0,0,0), fillAlpha 1.0, fillColorCrossPlatform '0,0,0,0'
 │       └ strokeColor UIColor black, strokeWidth 1.0, strokeAlpha 1.0, strokeColorCrossPlatform '0,0,0,1'   (no visible stroke)
 ├ contentSnapshot: GLSnapshot            (older versions: a second, downscaled copy 'Images/Image .png')
 └ isGif / remoteUrl                      (9.3.4 GIFs; the animated file is Assets/<assetsIdKey>/media.gif, Images/ holds a JPEG frame)
```

Image files **[verified]**: `Images/Image .jpg` (first, note the space before the dot), `Images/Image 1.jpg`,
`Images/Image 2.png` … — plain JPEG or PNG at source resolution (`saveAsJPEG` tells which; 16.1.5
stores both a `.jpg` and `.png` of some images). Display: place the crop rect of the source image at
`documentOrigin` scaled to `unscaledContentSize`, rotate by `rotationDegrees` radians around the
centre, apply flips. Image scale is therefore `unscaledContentSize / cropRect size` (an image 2592 px
wide shown at 250 pt). Alpha-less JPEGs are used for photos; PNG keeps transparency.

### 7.3 `TextBlockMediaObject` (text box) and `CanvasMediaObject` (sticky) **[verified 10.2.4, 15.3, 16.1.5]**

`TextBlockMediaObject` ⊂ `CanvasMediaObject` ⊂ `GenericMediaObject`. Extra keys: `textStore` — a
complete `FormattedString` (all keys of Part 4 with empty ink/PDF/media and `reflowState`
`NBReflowStateReflowable`) whose `attributedString` holds the box text; `paperIndex: -1`,
`lineStyle: 0`, `paperStyleObject: NBPaperStyle{paperColor: UIColor | $null, lineStyle: 0, paperImageIndex: -1}`
(box background colour), 15.3+ `kCanvasMediaObjectPaperAttributes: PaperAttributes{paperIdentifier: 'Color:#9DBBD8', paperSizingBehavior: 'deviceBasedWidth', …}`.
A sticky is a `CanvasMediaObject` without `textStore` (ink is drawn on it via `handwritingZIndecesKey`).
Text padding inside a box: 5 pt x, 2 pt y [third-party]. `Notability.MathMediaObject` (11.7.2) adds
`latex: 'f\\left( x\\right) =x^{4}…'` and `color`.

### 7.4 Typed flow text (`richText.attributedString`) **[verified]**

```
{'stringKey': 'Normal text 12pt Helvetica Neue\nBold text\n…',
 'subRangesKey': [ {'subRangeRangeKey': '{0, 32}',                       # location, length (UTF-16 units)
                    'subRangeFontKey': {'NSFontNameAttribute': 'HelveticaNeue-Bold', 'NSFontSizeAttribute': 12},
                    'subRangeColorCrossPlatformKey': '0.000000000000000,0.435294121503830,1.000000000000000,1.000000000000000',
                    'subRangeColorKey': UIColor,
                    'subRangeOtherAttributesKey': {'NSUnderline': 0|1, 'NSStrikethrough': 0|1, 'NSBaselineOffset': 0.0,
                                                   'line-spacing': 1.0, 'checklist-checked': 0|1, 'highlight': UIColor,
                                                   'indent-level': 0, 'indent-decoration-style': 1 bullet|2 numbered|3 checklist,
                                                   'indent-decoration-number': 1, 'indent-range-id': 3941517288}}, … ] }
```

Sub-ranges tile the string exactly (first at 0, contiguous, last ends at `len`). Bold/italic are
encoded in the PostScript font name suffix (`-Bold`, `-Italic`, `-BoldItalic`). The flow text has
**no stored position**: Notability lays it out from the top of page 1 in the page's text column,
line by line, and ink/media do not move with it. Consequence for a converter: emit GoodNotes text
boxes as `TextBlockMediaObject`s (positioned), keep `richText.attributedString` empty, and keep
`NBAttributedBackingString` and `recordingTimestampString` consistent with it (same string length;
all three equal in app-written files). An all-newline `stringKey` (e.g. 32 `\n`) is how Notability
pads the flow so that boxes/ink further down have "lines" behind them (`text.note`); not required.

---

## 8. Recordings and the event manager **[verified on 8.4.8 and 11.7.2]**

`contentPlaybackEventManager = NBCPEventManager` with `NBCPTimeManagerSOANumEventsKey = E` and
structure-of-arrays blobs (all little-endian, E entries each):

| key | struct | meaning |
|---|---|---|
| `NBCPTimeManagerSOATimestampsKey` | `<Ef` | seconds into the recording when the event (stroke) started |
| `NBCPTimeManagerSOADurationsKey` | `<Ef` | event duration (0.05 … 1.9 s) |
| `NBCPTimeManagerSOARecordingIDsKey` | `<Ei` | recording `identifier` from `Recordings/library.plist` (1, 2, 3) |
| `NBCPTimeManagerSOAEventIDsKey` (≤ 10.4) | `<Ei` | event id; a curve's `eventTokens[i]` equals one of these (bdb: 183 of 294 curves tokenised, ids 1…186, not sorted) |
| `NBCPTimeManagerSOAEventUUIDsKey` (11.7.2+) | 16E bytes | replaces ids; joins on `curveUUIDs` |

`metadata.plist` mirrors this with `noteHasRecordingKey`. An empty manager (E = 0, all blobs
`b''`) is what the template carries; keep it. Note that 8.4.8 wrote a recording timeline without a
`Recordings/library.plist` member (the audio had been deleted).

---

## 9. `metadata.plist` — `SessionInfo` **[verified]**

`NSKeyedArchiver`, `$top {'root': UID(1)}`:

| key | type | versions | meaning |
|---|---|---|---|
| `noteName`, `notePackagePath` | str / NSMutableString | all | title (same object) |
| `noteSubject` | str | all | 'unsortedNotesKey' / divider name / 'Unfiled Notes' (post-CloudKit notes always 'unsortedNotesKey' [third-party]) |
| `noteTags` | str | all | '' |
| `noteCreationDateKey`, `noteModifiedDateKey` | NSDate | all | |
| `noteHasRecordingKey` | bool | all | |
| `uuidKey` | str | 8.4.8+ | uppercase UUID (version-5 style `xxxxxxxx-xxxx-5xxx-…` in all samples; the PoC's random v4 was accepted) |
| `noteLastChangeDatePerTypeKey` | NSMutableDictionary int → NSDate | 8.4.8+ | last change per content type; keys 1, 2, 4, 8, 16 (bit flags; 1 and 4 are set at creation, 2 on later edits — exact semantics unknown) |
| `associatedProductsKey`, `galleryPublishHistoryKey` | NSArray | 10.4+ / 15.3+ | empty |
| `documentVersion` | int 1 | 7.2.5, 8.4.8, 10.2.4 | |
| `noteSizeKey` | int 0 | 7.2.5, 8.4.8, 10.2.4 | |
| `exportedSinceLastSave` | bool | 7.2.5, 8.4.8, 10.2.4 | |
| `noteCreatedDateStringKey` | str | 8.4.8 | localised date |

The 10.4 template's set (name/path, subject, tags, dates, hasRecording, uuid, per-type dates,
associatedProducts) is sufficient.

---

## 10. `HandwritingIndex/index.plist` **[verified]**

Plain (non-keyed) binary plist, the handwriting-recognition cache:

```
{'version': 7, 'minCompatibleVersion': 7,
 'pages': {'1': {'text': 'Demo File\n(PDF Background …',          # recognised text, '\n' between lines
                 'pageContentOrigin': [31.92, 25.37],              # page-relative top-left of the recognised block ≈ ink bbox min (within 0.2 pt)
                 'characterRects': bytes,                          # 8 bytes per character of 'text': 4 × float16 (x, y, w, h) relative to pageContentOrigin; '\n' and ' ' get (inf, inf, 0, 0)
                 'returnIndexes': {'indexes': [{'location': 9, 'length': 1}, …]},   # positions of '\n'
                 'sha256Hash': 32 bytes}}}                         # hash of the page's ink, for invalidation
```

Page keys are **1-based**: key = `floor(y / H) + 1`; key `'0'` exists for ink above page 1
(`text.note`). Verified against ink positions in 8.4.8, 10.2.4 and 15.3 files. Pages without ink are
absent. Notability rebuilds it; a writer can omit the file (keep the empty directory as the
template does).

---

## 11. Tolerant READER sketch (tested: `scratchpad/work/notereader.py` runs on all 21 samples)

Rules it embodies:

1. Find the member ending in `Session.plist`; bundle root = its directory (not the zip name).
2. `plistlib.loads`; root = `$objects[$top['$0' or 'root']]`; resolve UIDs lazily and repeatedly.
3. Containers: accept `NS.objects` / `NS.object.N` and `NS.keys` / `NS.key.N`; strings: `str`,
   `NS.bytes`, `NS.string`; `'$null'` → None; dict keys are strings that may themselves be UIDs.
4. `W` from `richText.reflowState.pageWidthInDocumentCoordsKey`; if the state is
   `NBReflowStateReflowable`, fall back to `paperSizingBehavior 'lockedWidth:<W>:…'`, then 565.
5. Page height: `staticWidth`+`letter` → `floor(W·11/8.5)`, otherwise `1.3125·W`; PDF slots
   `ceil(h·W/w)`; layout from `richText.pageLayoutArray`, else the paper-layout model.
6. Ink only via `richText['Handwriting Overlay'].SpatialHash`; then `groupsArrays` nested archives.
7. Lengths are authoritative: `n_i = 1+3k_i`, `k_i+1` widths; `curvesstyles` may be absent or
   short → style = 4 if alpha < 255 else 3; `eventTokens`/`curveUUIDs` optional.
8. Media: class name from `$class`; parse `'{x, y}'` strings; image path from
   `figure.FigureBackgroundObjectKey.kImageObjectSnapshotKey.relativePath`; crop from
   `FigureCropRectKey` only if `figure['$0']` is not null; rotation in radians.

```python
class Archive:                                   # NSKeyedArchiver / GLKeyedArchiver resolver on plistlib output
    def __init__(self, pl):
        self.objs = pl['$objects']; top = pl['$top']
        self.root = self.deref(top.get('root', top.get('$0')))
    def deref(self, v):
        while isinstance(v, plistlib.UID): v = self.objs[v.data]
        return v
    def get(self, d, key, default=None):
        d = self.deref(d); return self.deref(d[key]) if isinstance(d, dict) and key in d else default
    def string(self, v):
        v = self.deref(v)
        if isinstance(v, dict):
            if 'NS.bytes' in v: return v['NS.bytes'].decode('utf-8')
            if 'NS.string' in v: return self.deref(v['NS.string'])
        return None if v == '$null' else v
    def array(self, v):
        v = self.deref(v)
        if v in (None, '$null'): return []
        if 'NS.objects' in v: return [self.deref(x) for x in v['NS.objects']]
        out, i = [], 0
        while f'NS.object.{i}' in v: out.append(self.deref(v[f'NS.object.{i}'])); i += 1
        return out
    def dictionary(self, v):
        v = self.deref(v)
        if v in (None, '$null'): return {}
        if 'NS.keys' in v: return {self.string(k): self.deref(x) for k, x in zip(v['NS.keys'], v['NS.objects'])}
        out, i = {}, 0
        while f'NS.key.{i}' in v: out[self.string(v[f'NS.key.{i}'])] = self.deref(v[f'NS.object.{i}']); i += 1
        return out

def parse_hash(a, hs):                          # hs = resolved InkedSpatialHash dict (top-level or nested group archive root)
    nc = int(a.get(hs, 'numcurves', 0) or 0)
    if nc == 0: return []
    data = lambda k: a.deref(hs.get(k)) or b''
    pts = struct.unpack(f'<{len(data("curvespoints"))//4}f', data('curvespoints'))
    npts = struct.unpack(f'<{nc}i', data('curvesnumpoints')[:4*nc])
    widths = struct.unpack(f'<{nc}f', data('curveswidth')[:4*nc])
    fw = struct.unpack(f'<{len(data("curvesfractionalwidths"))//4}f', data('curvesfractionalwidths'))
    colors, styles = data('curvescolors'), data('curvesstyles')
    out, p, f = [], 0, 0
    for i in range(nc):
        n = npts[i]; k = (n - 1) // 3; assert n % 3 == 1
        r, g, b, al = colors[4*i:4*i+4]
        out.append(dict(points=[(pts[2*(p+j)], pts[2*(p+j)+1]) for j in range(n)], width=widths[i],
                        fractional=fw[f:f+k+1], rgba=(r, g, b, al),
                        style=styles[i] if i < len(styles) else (4 if al < 255 else 3)))
        p += n; f += k + 1
    assert p == len(pts)//2 and f == len(fw)
    return out
```

Page assignment per curve: use the stroke's mean y (notesconverter) or its first point; strokes
crossing a page boundary are rare and belong to one page in Notability's own export.

---

## 12. WRITER recipe (beyond the PoC)

Base everything on the 10.4 template archive (`notability-to-svg/example.note`), which current
Notability opens: rewrite its arrays and counts, keep all other objects.

1. **Multi-page plain notes**: one `InkedSpatialHash`; page p content gets `y += p · 1.3125·W`
   (W = 574 in the template; or set `pageWidthInDocumentCoordsKey` to 565 and scale accordingly).
   No `pageLayoutArray` entries are needed; Notability derives the page count from content. To force
   trailing empty pages, append blank entries `{kPageLayoutPDFPageNumberKey: 9223372036854775807}`
   (NSDictionary with `NS.keys`/`NS.objects`, class `NSDictionary`) — **[inferred from bookmarks.note]**.
2. **PDF-backed pages** (GoodNotes documents with PDF templates): put each PDF under
   `PDFs/<UUID>.pdf`; add one `PDFFile` object per file (`pdfFileName`, `contentBoxVersion 1`,
   `highlights []`, `version_4_1_OrLater True`, `version 2`, `pageNumbers $null`) to
   `richText.pdfFiles`; add one layout dict per page in order with `kPageLayoutPDFPageNumberKey`
   (1-based page in that PDF), `kPageLayoutPDFFileKey` → the PDFFile UID, `kPageLayoutPDFIsOriginalPageKey True`.
   Ink for page i goes at `y_i = Σ_{j<i} ceil(h_j·W/w_j)`, x scaled by `W/w_j` (PDF points → doc).
   `NBPDFIndex` can be omitted. **[inferred: structure verified from 10.2.4 files; acceptance of a
   hand-written one untested on the iPad]**
3. **Images**: write `Images/Image .jpg` (`Image 1.jpg`, …; PNG when alpha matters) and one
   `ImageMediaObject` per image (7.2) with the full nested `Figure → ImageObject → GLSnapshot`,
   `FigureCropRectKey '{{0, 0}, {px_w, px_h}}'`, `$0 $null`, `documentOrigin` in doc coords,
   `unscaledContentSize` = displayed size, `rotationDegrees` in radians, `zIndex` ascending,
   `assetsIdKey` fresh UUID, caption fields as listed, `handwritingZIndecesKey []`. Register the
   classes `ImageMediaObject`(classes `[ImageMediaObject, GenericMediaObject, NSObject]`), `Figure`,
   `ImageObject`(`[ImageObject, DrawObject, NSObject]`), `GLSnapshot`, `UIColor` in `$objects`.
   **[inferred — structure verified across 5 versions, writing untested]**
4. **Typed text boxes**: one `TextBlockMediaObject` per GoodNotes text element with a `textStore`
   FormattedString (copy the template's richText skeleton with empty ink, set `reflowState` to class
   `NBReflowStateReflowable` with no keys, fill `attributedString` + the two backing copies +
   `recordingTimestampString` of equal length, `formattedStringTextAlignmentKey`), `paperIndex -1`,
   `lineStyle 0`, `paperStyleObject{paperColor $null, lineStyle 0, paperImageIndex -1}`
   (class `Notability.NBPaperStyle`). **[inferred]**
5. **Highlighter**: style 4, alpha 0x6b, width ≈ 4–12 doc points, fractional widths 1.0.
6. `metadata.plist`: as the PoC (new UUID, dates, name). Thumbnails: render the first page into
   48×63 / 96×126 / 144×189 / 288×378 PNGs (white is accepted).

---

## 13. Open questions / risks

* Acceptance by current Notability of hand-written `pageLayoutArray`/`PDFFile`, `ImageMediaObject`
  and `TextBlockMediaObject` objects in a 10.4-format file is untested (only ink has been proven).
  Mitigation: these structures are copied field-for-field from 10.2.4 app output, which 15.3
  re-saved without complaint (`demo.note` → `demo15.note` are the same note).
* `curvesstyles` value 5 (16.1.5) is assumed to be a pen variant (pencil/fountain) — not identified.
* `PDFFile.version` absent (≤ 8.4.8): the only sample behaves as version 2; notesconverter claims
  version-1 semantics (keep height, 8.5 in width) for older files — if an old PDF note mis-pages,
  try that rule.
* `noteLastChangeDatePerTypeKey` key meanings (1, 2, 4, 8, 16) unknown.
* The exact left-edge inset (`−W·20/768`) is third-party; it only matters for x alignment of ink
  on PDF pages (≈ 2.6 % of width).
* `bezierPathsDataDictionary`, `Figure.$0` and `PDFLayoutIndex.nbpdflayout` are opaque caches
  (CGPath data / uint16 stream); writing them empty/absent is what all pre-16 files do.
* Notability ≥ 16 on Mac exports `.ntb` (FlatBuffers) by default; `.note` import still works in
  the user's current iPad app (PoC), and 16.1.5 still *writes* `.note` (the 2026 sample).

## Critic additions

* **`curvesstyles` 5 = pencil** (Part 6.2 open question): nokcha0's 2026 reader
  (`src/converter/model.rs`: `STROKE_STYLE_PENCIL = 5`, `STROKE_STYLE_NOT_EXPORTED = 6`) names it
  so; consistent with the opaque colours and 1.3–1.8 pt widths seen here. Third-party, medium
  confidence. GoodNotes' pencil is stroke `#3 = 5` / `#21 = 25` (see the stroke doc); a converter
  can map pencil ↔ pencil for reading but should write GoodNotes pencils as ball-pen strokes
  (schema 24).
* **GoodNotes → Notability curve conversion is exact for current GoodNotes ink**: the flat
  format stores quadratic Béziers, so each segment becomes one Notability cubic by degree
  elevation (`c1 = P0 + 2/3(C − P0)`, `c2 = P1 + 2/3(C − P1)`), giving `n_i = 1 + 3k` with
  `k` = number of GoodNotes segments. The PoC's Catmull-Rom fit is only needed for the legacy
  ribbon format (polyline with per-point radius).
* **Page-height caveat for the converter** (Part 5.2): a GoodNotes "standard" page
  (455.04 × 588.45 pt, ratio 1.293) fits inside a legacy Notability page (ratio 1.3125) when
  scaled to `W`, but an A4 GoodNotes page (ratio 1.414) does not: at `W = 574` its ink extends
  to 811.8 doc pt against a page height of 753.4, so the bottom 7 % lands on the next page.
  Either attach the GoodNotes paper PDF as a Notability PDF page (slot = `ceil(h·W/w)`, Part 5.3,
  untested on the iPad) or scale such pages uniformly by `1.3125/ratio` and centre them.
