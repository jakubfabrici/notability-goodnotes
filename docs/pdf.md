# PDF: what gnnote writes and reads

`gnnote/pdf/` turns the shared model into PDF files and PDF files back into the model. The
writer produces a PDF 1.7 that looks like the note in the app (ink drawn into the pages, or
kept editable as ink annotations); the reader turns a PDF into PDF-backed pages whose ink
annotations become editable strokes for GoodNotes and Notability. Everything is pure Python
(standard library only) and runs under Pyodide.

```
gnnote/pdf/
  objects.py   PdfFile (reading, on top of pdfutil's tolerant parser), serialize, PdfWriter,
               Copier (object graph copy with renumbering), incremental_update, rewrite
  writer.py    write_pdf(doc, options) -> bytes
  reader.py    read_pdf(data) -> Document
  images.py    JPEG / PNG -> image XObjects (+ a reference PNG decoder)
  text.py      font choice, Helvetica widths, line layout, text operators
  ttf.py       TrueType reader (cmap, hmtx, metrics) and glyph subsetter
  fonts/       DejaVuSans-subset.ttf + LICENSE-DejaVu.txt (tools/make_font_subset.py)
```

Facts about other producers' files below were checked against GoodNotes' own exports in the
goodparse samples (`Test4.pdf` ... `Test9.pdf`, iOS Quartz PDFContext) with PyMuPDF.

## 1. Coordinates

The model is top-left / y-down in points. PDF default user space is bottom-left / y-up and a
page shows its `/MediaBox` (corners `x0 y0 x1 y1`, any origin) turned by `/Rotate` (clockwise,
multiples of 90). The *displayed* page is `w x h` (`h x w` for 90 / 270), which is what
`pdfutil.pdf_info` reports and what model pages use. User space -> displayed space (y up):

| `/Rotate` | matrix `[a b c d e f]` (`x' = a x + c y + e`, `y' = b x + d y + f`) |
|---|---|
| 0 | `[1 0 0 1 -x0 -y0]` |
| 90 | `[0 -1 1 0 -y0 w+x0]` |
| 180 | `[-1 0 0 -1 w+x0 h+y0]` |
| 270 | `[0 1 -1 0 h+y0 -x0]` |

then `y_model = H - y'`. `CropBox` and `UserUnit` are ignored (the apps' page sizes come from
the MediaBox too).

## 2. Writer (`write_pdf`)

### 2.1 File

`%PDF-1.7`, a binary comment, objects `1..n` in number order, one classic `xref` table with
exact 20-byte entries, `trailer << /Size /Root /Info /ID >>` (`/ID` = MD5 of the object bodies,
so equal input gives equal bytes), `startxref`, `%%EOF`. Catalog `1 0 R`, one flat `/Pages`
node `2 0 R`. `/Info << /Title /Producer (gnnote x.y.z) >>`; the title is PDFDocEncoding when
it is printable ASCII, else UTF-16BE with a byte order mark. No dates, nothing else. Content
streams are Flate-compressed. Every page: `/MediaBox [0 0 w h]` (the model page size), one
content stream, `/Resources` with only what the page uses.

Model points are written as `(x, h - y)`, three decimals. Page content, bottom to top:

1. background, 2. images, 3. text boxes, 4. ink in `Page.strokes` order.

### 2.2 Backgrounds

A page whose `background` names a PDF in `Document.pdfs` -- a user PDF or a stock paper
(`template_is_builtin`), handled alike -- gets that PDF's page as a **Form XObject**:

* content: the page's `/Contents` (a stream or an array of streams) decoded and joined with
  a newline (PDF splits contents only between tokens);
* `/BBox` = the MediaBox in source user space, `/Resources` = the page's inherited resources,
  `/Group` copied when the page has one;
* every object those reference is copied once per source PDF (a memo keyed by object
  number, so fonts and images shared by pages are written once), renumbered, streams with
  their original filters;
* visible annotations (`/F` without Hidden 2 / NoView 32, not `/Popup`) are drawn on top:
  their normal appearance (`/AP /N`, or the `/AS` state of an appearance dictionary) is
  placed per PDF 32000-1 section 12.5.5 (the form's `/BBox` transformed by its `/Matrix`,
  mapped onto `/Rect`). `/CA` is not applied: with an appearance stream "the appearance
  stream shall specify any transparency" (table 170), and viewers (MuPDF) do the same;
* drawn with `q <M> cm /BgN Do Q`, `M` = the table of section 1 scaled to the model page
  (`W / w_displayed`, `H / h_displayed`), so the page fills the model page exactly as PDF
  viewers show it.

A page without a background but with `paper` lined / grid / dotted imports
`pdfutil.make_paper_pdf(w, h, paper)` the same way. An encrypted (trailer `/Encrypt`),
unreadable or missing PDF, or a page index past its end, leaves the background blank with a
warning.

### 2.3 Images

* **JPEG** -- the bytes unchanged, `/Filter /DCTDecode`. Size, components and precision come
  from the first `SOFn` marker (C0-C3, C5-C7, C9-CB, CD-CF); 1 / 3 / 4 components ->
  DeviceGray / DeviceRGB / DeviceCMYK; an `APP14 Adobe` marker with 4 components gets
  `/Decode [1 0 1 0 1 0 1 0]` (Adobe stores inverted CMYK). 12-bit JPEGs are refused.
* **PNG** -- by kind:
  * grey / RGB / palette, not interlaced, zlib stream intact: the concatenated IDAT bytes
    as they are, `/FlateDecode` + `/DecodeParms << /Predictor 15 /Colors c /BitsPerComponent
    b /Columns w >>` (PDF's PNG predictors are PNG's filters); palette ->
    `[/Indexed /DeviceRGB n-1 <PLTE>]`; a grey / RGB `tRNS` -> colour-key `/Mask [v v ...]`.
  * grey+alpha / RGBA, not interlaced: every PNG filter predicts a byte from the *same byte
    of the previous pixel* and the row above, so the filtered rows are split by slicing
    into a colour stream and an alpha stream that stay valid predicted data for 3 (1)
    colours instead of 4 (2): inflate, slice, deflate -- no unfiltering. Alpha -> `/SMask`.
    A 2000 x 1500 RGBA PNG takes well under a second.
  * palette with `tRNS`, interlaced (Adam7) or damaged data: decoded -- inflate (bounded to
    the expected size; a truncated stream is padded), unfilter (None; Up as one big-integer
    byte-wise add; Sub as a log-step prefix sum on big integers; Average and Paeth byte by
    byte), de-interlace by strided slice assignment, unpack 1/2/4-bit samples with
    translation tables -- and written raw; palette transparency becomes the `/SMask`
    through a 256-entry translation table.
  * more than 60 million pixels: refused (warning).
* **PDF images** (`fmt == "pdf"`, vector stickers): page 1 as a Form XObject (section 2.2)
  mapped onto the box.
* Identical image bytes are embedded once.

Placement: the image fills its box `(x, y, w, h)` turned by `Image.rotation` clockwise about
the box centre. GoodNotes stores an EXIF-rotated photo with its *displayed* box and the
rotation the raw pixels need (`design.md` 4.1); PDF viewers ignore EXIF. So when a JPEG's EXIF
orientation prescribes exactly the image's quarter turn (5 / 8 -> 270, 6 / 7 -> 90), the raw
pixels go into the box with width and height swapped and the turn makes them fill it upright
(this reproduces the photo of GoodNotes' own `Test9.pdf` page 4). Every other image fills the
box and the box turns (Notability's convention).

### 2.4 Text boxes

* **Fonts.** When every character of every text box is in Windows code page 1252 (PDF's
  WinAnsiEncoding) the base-14 `/Helvetica` with `/Encoding /WinAnsiEncoding` is used, nothing
  embedded; `text.HELVETICA_WIDTHS` holds the standard Helvetica advance widths of codes
  32..255 for the layout. Otherwise the DejaVu Sans subset is embedded: `/Type0`,
  `/Encoding /Identity-H`, descendant `/CIDFontType2` with `/CIDToGIDMap /Identity`,
  `/CIDSystemInfo (Adobe) (Identity) 0`, `/W` widths of the used glyphs, `/FontDescriptor`
  with `/FontFile2` = the font program with every unused glyph emptied (`ttf.subset`: glyph
  ids kept, composite components kept, long `loca`, table checksums and
  `checkSumAdjustment` recomputed), base font `XXXXXX+DejaVuSans` (subset tag from the glyph
  set) and a `/ToUnicode` CMap (`beginbfchar` blocks of at most 100 entries), so the text can
  be searched and copied. Slovak needs this font (c, d, l, n, t with caron are not in 1252);
  German or French text does not. Characters the font lacks print as `?`, one warning.
* **Shipped subset** (`gnnote/pdf/fonts/DejaVuSans-subset.ttf`, 155 KB, built by
  `tools/make_font_subset.py`): Basic Latin, Latin-1, Latin Extended-A/B/Additional, spacing
  modifiers, combining diacritics, Greek, Cyrillic (+ Supplement; Ukrainian i, yi, ye, ghe
  with upturn), general punctuation, super/subscripts, currency, letterlike symbols (numero
  sign), number forms, arrows, mathematical operators, geometric shapes, ballot boxes and
  check marks; no hinting, no OpenType layout tables (glyphs are placed by advance width;
  no kerning, no shaping). When the file is missing (a package installed without its data
  files) the writer falls back to Helvetica with `?` and a warning.
* **Layout** (box frame: origin at the text frame's top-left, y down). Text is NFC
  normalised, tabs become four spaces, other control characters are dropped. Paragraphs
  split at `\n`; words wrap greedily at the box width; a line may be up to 12 % wider than
  the box (the source app measured with its own font) and is then condensed horizontally to
  fit; a word longer than a line breaks between characters. First baseline `0.952 em` below
  the top, then `0.2126 em` of the line above + `0.952 em` of the next (= `1.1646 em` for
  equal sizes: Helvetica Neue's ascender and Cocoa's line spacing for GoodNotes text). Each
  line is shifted for `align` left / center / right.
* **Operators.** `q [cos -sin sin cos x h-y] cm` rotates the box by `TextBox.rotation`
  clockwise about its top-left corner, then one `BT ... ET` with `/F1 1 Tf` and per style
  run `r g b rg r g b RG`, `[s*k 0 skew s x -baseline] Tm` (`k` the condensing factor, `skew =
  s * tan 12 deg` for italic) and a hex string (`<cc..>` one byte per character for
  Helvetica, `<gggg..>` glyph ids for the Type0 font). Bold: `2 Tr` with a `s/30` outline in
  the fill colour. Underline: a filled rectangle `0.06 s` high, `0.12 s` below the baseline.
  A colour with alpha < 1 sets an ExtGState with `/CA` and `/ca`.
* Fonts named in `TextRun.font` are not used (one sans-serif family for everything).

### 2.5 Ink

`Options.pdf_ink` decides where strokes go:

* **`flatten`** (default): into the page content.
* **`annotations`**: one `/Ink` annotation per stroke (fills stay in the content):

  ```
  << /Type /Annot /Subtype /Ink /Rect [bbox + width/2 + 1] /InkList [[x y x y ...]]
     /BS << /Type /Border /W median-width /S /S >> /C [r g b] [/CA alpha] /F 4
     /P page /NM (gnnote-<page>-<stroke>) /AP << /N form >> >>
  ```

  `/InkList` is the centre line in default user space (Bezier strokes sampled every 1 pt).
  The appearance (`/BBox` = `/Rect`, identity matrix) draws exactly what flatten mode draws,
  alpha and blend mode included: viewers paint `/AP` as it is (PDF 32000-1 table 170;
  MuPDF ignores `/CA` when an appearance exists), `/CA` is there for apps that regenerate
  appearances. Annotations are drawn above all page content.

Drawing (both modes):

* Stroke colour `r g b RG`, round caps and joins (`1 J 1 j`). Widths below 0.1 pt are drawn at
  0.1 pt. A single-point stroke is a zero-length subpath, which round caps paint as a disc
  (PDF 32000-1 8.5.3.2).
* **Constant width** (all anchor widths within 2 %): one path -- `m` then `l` per segment, or
  `c` per segment for Bezier strokes (`Stroke.controls`) -- stroked once with the median
  width.
* **Variable width**: every segment is cut into pieces whose width changes by at most 5 %
  (cubics split by de Casteljau, at most 32 pieces per segment), each piece gets the width at
  its middle, consecutive pieces of (almost) the same width form one subpath stroked with
  that width; the round caps hide the joints.
* **Highlighters**: an ExtGState `<< /CA a /ca a /BM /Multiply >>` with `a` = the colour's alpha
  (0.5 when it is opaque), so text under the highlighter stays readable. Translucent pens use
  the same without the blend mode.
* A translucent **variable-width** stroke is drawn opaque inside a transparency group Form
  XObject (`/Group << /S /Transparency >>`) painted with the alpha, so the overlapping pieces
  do not darken.
* **Fills** (`kind == "fill"`): the `outline` rings as `m l ... h` subpaths, filled with the
  non-zero rule, colour `rg`, alpha in `/ca`.

Strokes, images and text boxes with non-finite or absurd (> 10^6 pt) coordinates are left out
with one counted warning.

## 3. Reader (`read_pdf`)

### 3.1 Pages

One model page per PDF page, in the order `pdfutil.pdf_info` lists them (the walk is the same
code path, including the scanning fallbacks for damaged files), size = displayed size,
`background = PdfBackground(pdf_id, index)`, `template_is_builtin = False`, `paper = "plain"`.
`pdf_id` is a UUID derived from the SHA-1 of the input; `Document.pdfs[pdf_id]` holds the PDF
without the converted annotations. Title = `/Info /Title`, else `"PDF"`. Ink drawn into the
page content stays part of the background: gnnote does not vectorise page content.

### 3.2 Annotations

Converted (unless Hidden or NoView): `/Ink`, `/Line`, `/PolyLine`, `/Polygon`, `/Square`,
`/Circle`, `/FreeText`, `/Highlight`. Common rules:

* coordinates through section 1's matrix, then `y = H - y'`;
* width: `/BS /W`, else the third number of `/Border`, else 1 (non-positive values fall
  through);
* colour `/C`: 1 number = grey, 3 = RGB, 4 = CMYK (naive conversion), empty or missing =
  black;
* alpha: `/CA`, else the lowest `/CA` / `/ca` below 1 among the ExtGStates of the normal
  appearance (and of forms it draws, two levels deep), else 1;
* `kind = "highlighter"` when alpha <= 0.6 or an appearance ExtGState has `/BM /Multiply`.

| Subtype | Model |
|---|---|
| `/Ink` | one polyline stroke per `/InkList` path (consecutive duplicates dropped). When the normal appearance strokes exactly one path and it agrees with `/InkList` (same first point, bounding boxes within 2 pt or 10 %), that path is used instead: its exact cubic handles become `Stroke.controls` (GoodNotes writes only the Bezier anchors of its shapes into `/InkList` -- an ellipse becomes 4 points -- and gnnote's own constant-width strokes come back exact). When the appearance instead *fills* outlines (GoodNotes draws fountain pens and markers as one filled blob per segment, with `/BS /W` about 45 % of the drawn width), the width is estimated from the blobs: a stadium of area A and perimeter P has width `w` with `pi/4 w^2 - P/2 w + A = 0`; the median over the blobs is used when it is larger than `/W` (at most `4 W + 10`). |
| `/Line` | `/L` as a two-anchor stroke with handles at the thirds (an exact straight Bezier, so the Notability writer keeps it straight); line endings `/LE` are dropped (warning). |
| `/PolyLine`, `/Polygon` | `/Vertices`, straight-sided Bezier; a polygon is closed; `/IC` adds a `kind = "fill"` stroke right after the outline. |
| `/Square`, `/Circle` | `/Rect` minus `/RD`, inset by half the border width: a closed rectangle (straight Bezier) or the inscribed ellipse sampled at 64 points; `/IC` adds a fill. |
| `/FreeText` | `TextBox`: frame = `/Rect` minus `/RD`, text `/Contents` (`\r` -> `\n`), colour from `/DA` (`g` / `rg` / `k`, the last one wins) or `/DS` (`color:#rrggbb`), size = the size the normal appearance shows (first text-showing operator: `Tf` x the `Tm` / `cm` scale x the appearance placement) else `/DA` `Tf` else `/DS` `NNpt` else 12; alignment `/Q` (0 left, 1 centre, 2 right); rotation = the page's `/Rotate` (the box turns with the page); alpha `/CA`. GoodNotes' `/DA` always says 13 pt (its "GOOD" sticker is 30 pt). |
| `/Highlight` | one highlighter stroke per `/QuadPoints` quadrilateral: the text runs from the first to the second corner (true for the specification's counter-clockwise order and Acrobat's TL TR BL BR order alike); the stroke follows the quad's middle along that direction, as wide as the quad is high, shortened by half that width at both ends so its round caps end at the quad. Colour `/C` (default yellow), alpha `/CA` when below 1, else 0.5. A stroke rather than a fill because both target apps keep highlighter ink editable, while Notability has no fills. |

Dashed borders (`/BS /S /D`) are drawn solid (warning). Everything else stays in the PDF
(links, stamps, sticky notes, form fields, ...); a warning counts what stays (GoodNotes
exports images, shape fills and stickers as `/Stamp` annotations).

### 3.3 Removing the converted annotations

The pages that had annotations converted get a new page dictionary: the old one with
`/Annots` = the kept entries (or no `/Annots`). A `/Popup` whose `/Parent` is a converted
annotation, or that a converted annotation names in `/Popup`, is removed too. The new
dictionaries are written by an **incremental update**: the original bytes, then `N G obj
... endobj` for each page (same object number and generation), then a cross-reference section
of the kind the file's newest section has -- a classic table (`xref`, sub-sections of 20-byte
entries, `trailer << /Size /Root /Info /ID /Prev >>`) or a cross-reference stream (`<< /Type
/XRef /W [1 4 2] /Index [...] /Size /Prev /Root /Info /ID /Filter /FlateDecode >>`, listing
itself) -- and `startxref` / `%%EOF`. Pages stored in object streams are fine: the new entry
supersedes the old one. A file whose cross-reference data needed repair (wrong offsets,
missing `startxref`, junk before the header) is **rewritten** instead: catalog (everything but
`/Pages`) and pages copied into a fresh file with one flat page tree, inherited `/MediaBox`,
`/CropBox`, `/Rotate` and `/Resources` made explicit, `/Info` copied.

The result is re-read and checked (same page count, the rewritten pages hold exactly the kept
annotations). If anything fails, the original bytes are kept and nothing is converted
(warning), so ink never appears twice.

Encrypted PDFs (trailer `/Encrypt`) are carried byte for byte with nothing converted and a
warning that their annotations stay part of the pages.

### 3.4 Limits

`read_pdf` raises `ValueError` only when there is no `%PDF` in the first 1024 bytes or no
page can be found; damaged input degrades to warnings (`pdfutil`'s decompression limits
apply). At most 100 000 annotations per page and 5 000 000 converted points per document;
appearance streams above 2 MB are not interpreted.

## 4. Options and plumbing

* Registry entry `pdf` (`gnnote/formats.py`): name "PDF", extension `.pdf`, sniffed by `%PDF`
  in the first 1024 bytes of something that is not a ZIP archive; default target Notability.
* `Options.pdf_ink`: `"flatten"` | `"annotations"` (validated).
* CLI: `--pdf-ink flatten|annotations` on `convert` and `batch`; `batch --include-pdf` also
  converts `.pdf` files (off by default because a notes folder usually holds the PDF exports
  of its notebooks, and both would be written to the same output name).
* Server: `pdf_ink` form / query parameter (validated) next to `to=pdf`.

## 5. Known limitations

* Page content is never vectorised: ink that a PDF draws into its pages stays background.
* Fonts of text runs are not preserved (Helvetica or DejaVu Sans), no kerning or complex
  script shaping; right-to-left text is laid out left to right.
* Annotation types other than the eight above, line endings, dash patterns and FreeText
  rich text (`/RC`) are not converted; FreeText callouts lose their callout line.
* Encrypted PDFs: annotations are not converted (no decryption).
* `CropBox` and `UserUnit` are ignored; pages are the MediaBox.
* Image crops are not part of the model; mirrored EXIF orientations are drawn without the
  mirror (as the GoodNotes reader models them).
