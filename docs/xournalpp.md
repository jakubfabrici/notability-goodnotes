# Xournal++ (`.xopp`, `.xoj`): format facts and gnnote mapping

Codec: `gnnote/xournalpp/` (`reader.read_xopp`, `writer.write_xopp`), registry id `xournalpp`,
output extension `.xopp`, inputs `.xopp` and `.xoj`.

Xournal++ is a GPL-2.0 desktop app (Linux, macOS, Windows; no iPad app). Its source and test
files were consulted for **facts only**; no code was taken from them. The facts below come from
the loader and saver of Xournal++ master `9882ffa` (2026-09-29) and of the release tags 1.2.0,
1.2.8, 1.3.0, 1.3.7 and 1.3.8, and from its test files (`test/files`), which the test suite
fetches as external test data (`tests/conftest.py`, never committed). The writer's shape follows
inkterop's MIT xopp writer, which opened without errors in Xournal++ 1.3.5 (inkterop
`docs/validated-writes.md`). Confidence markers: **[verified]** checked against the sources or
files named here; **[inferred]** consistent evidence, not run in the app; **[unverified]** not
checked in the app.

## 1. Containers

| Variant | Bytes | Who writes it | Who reads it |
|---|---|---|---|
| gzip XML | gzip of the XML document, `fileversion="4"` (Xournal++ up to 1.3.8) or `"5"` (master) | every Xournal++ release, original Xournal (`.xoj`) | every release |
| plain XML | the same XML uncompressed | hand-made / test files | every release (it tries gzip, which passes plain data through) |
| ZIP package | `mimetype`, `META-INF/version` (`current=4\nmin=4\n`), `content.xml`, optional `attachments/`, `thumbnails/` | no released version | 1.2.0 .. 1.3.8 and master **[verified in the loader code]** |

* The ZIP loaders of 1.2.0 .. master refuse a `mimetype` member whose content is *exactly*
  `application/xournal++` (the comparison is inverted); Xournal++'s own packaged test files hold
  ` application/xournal++\n` (leading space, trailing newline) and load. gnnote writes those
  exact bytes **[verified in code, unverified in the app]**.
* `fileversion` is read from the root attribute for gzip / XML files and from
  `META-INF/version` for ZIP packages. Version 5 (master only) replaces the position attributes
  of text, images, LaTeX and links by a `matrix` attribute; every release ignores `matrix`.
* The root element is `<xournal creator fileversion>` (Xournal 0.4 wrote `version`); MrWriter
  files use `<MrWriter version>` and Xournal++ parses them the same way.

## 2. Reading (`reader.py`)

**Hardening.** The XML is parsed by expat with a tree builder that raises on any document type
or entity declaration (so no entity can expand; `<!DOCTYPE` is refused with `ValueError`),
limits nesting to 64 levels, 2 000 000 elements, 200 000 points per stroke and 5 000 000 points
per file, and converts every `<page>` as soon as it closes. gzip input inflates to at most
256 MB (`MAX_XML_BYTES`, `ValueError` beyond); ZIP members are size-checked before inflating
(256 MB per member, 1 GB per archive). Damaged or truncated files keep every element that was
complete before the damage, with one warning, as Xournal++ does. Only `ValueError` leaves
`read_xopp` (not a Xournal++ file, refused DTD, over the size limit).

**Units.** PDF points, origin at the page's top-left corner, y down: coordinates and widths
are copied unchanged.

| Xournal++ | Model |
|---|---|
| `<page width height>` | `Page(width, height)`; missing / invalid size -> A4 595.27559 x 841.88976 pt (warning) |
| `<background type="solid" color style>` | `Page.paper`: `plain` -> plain; `ruled` (rules only), `lined` (rules + margin), `staves` -> lined; `graph`, `isograph` -> grid; `dotted`, `isodotted` -> dotted (iso / staves: one warning). A colour other than white (`#rrggbbaa` or the Xournal names blue, pink, green, orange, yellow) becomes a generated one-page paper PDF of that colour and style (`PdfBackground`, `template_is_builtin = True`) |
| `<background type="pdf" domain filename pageno>` | `PdfBackground(pdf_id, pageno - 1)`, `template_is_builtin = False`. The first PDF background that names a file defines the document's single PDF. Inside a ZIP package (`domain="attach"`) the PDF is read from the archive; a gzip file only *references* `<file>.xopp.bg.pdf` (`attach`) or a path (`absolute`), which are not part of the input: plain paper and one warning |
| `<background type="pixmap">` | a full-page `Image` below the page content when the image is inside the package or cloned from an earlier page (`domain="clone"`, file name = page index); otherwise dropped (warning) |
| `<layer>` | all layers merged in document order (one warning per page with several non-empty layers) |
| `<stroke tool color width [fill] [capStyle] [style]>x y ...</stroke>` | one `Stroke` (polyline, `controls = None`) |
| `<text font size x y color [matrix] [wrap] [align]>` | `TextBox` with one `TextRun` |
| `<image left top right bottom [natural_size]>` / `<image matrix>` | `Image` (PNG / JPEG; base64 text or an `<attachment path>` child in a package) |
| `<teximage text ...>` (LaTeX) | `Image(fmt="pdf")` (the stored PDF; PNG for very old files); the LaTeX source is dropped (warning) |
| `<link ... url>` | plain `TextBox` (warning: link target dropped) |
| `<audio>`, `fn` / `ts` attributes, `<timestamp>` | dropped (one warning) |
| `<title>` | `Document.title`, unless it is Xournal++'s boilerplate "Xournal++ document - see ..." |

**Strokes [verified against the loader].**

* `tool`: `pen` -> `kind = "pen"`, `highlighter` -> `kind = "highlighter"`, `eraser` (whiteout
  strokes) -> dropped with one warning (ink they covered becomes visible).
* `color`: `#RRGGBBAA` (alpha kept: Xournal++ writes `ff` for pens and `7f` for highlighters),
  `#RRGGBB`, or Xournal's names (black, blue #3333cc, red, green #008000, gray, lightblue #00c0ff,
  lightgreen #00ff00, magenta, orange #ff8000, yellow, white).
* `width`: the first value is the nominal width (`Stroke.width`). Further values are the widths
  of the points except the last (`n - 1` values for `n` points); Xournal++ draws the segment
  from point *i* to *i + 1* with width *i*. Model point *i* gets width *i*, the last point repeats
  the last segment's width. MrWriter's separate `pressures` attribute is read the same way.
  Like Xournal++: with fewer values than `n - 1` the stroke is shortened to `values + 1` points
  (warning); points whose width is zero, negative or NaN are removed and the stroke is split
  around them (those segments are invisible in Xournal++, so no warning; Xournal++ 1.0 wrote a
  zero for the last segment of most strokes); strokes with fewer than two points are skipped.
* `fill="N"` (0..255): the stroke is also a filled shape. The reader adds a
  `Stroke(kind="fill", outline=[points], color=(r, g, b, N / 255))` right after the ink stroke
  (the model's shape-fill convention) unless the polygon has no area. A filled stroke whose
  outline is 0.1 pt or thinner (how gnnote writes a fill that has no outline) becomes the fill
  alone.
* `style` (dash patterns) is drawn solid (warning); `capStyle` is ignored.

**Text.** `size` in pt; the Pango font name gives the family plus bold / italic
(`"Times New Roman, Bold"`, `"Sans Bold Italic"`); `align` left / center / right. Xournal++
stores no box size: the width is `wrap` (when present) or an estimate of 0.6 em per character
of the longest line, the height 1.25 em per line. With a `matrix` (file version 5) the text's
origin is the matrix translation, the rotation is `atan2(yx, xx)` (clockwise, about the
top-left corner, as in the model) and the font size is scaled by the matrix's x scale.

**Images.** Version 4: the box `left top right bottom`. Version 5: `matrix` maps the image's
natural size (pixel size; for LaTeX the PDF page size) to the page; gnnote decomposes it into
the model's box, centre and clockwise rotation (shear or mirroring: approximated, warning).
GIF / BMP / WebP images are skipped (warning).

## 3. Writing (`writer.py`)

Output is the gzip variant (`fileversion="4"`, `creator="gnnote <version>"`, gzip header
timestamp 0, so the bytes are reproducible) unless a page shows a background PDF: then the
ZIP package with the PDF in `attachments/bg.pdf`.

* **Pages**: `<page width height>` in pt; `<background type="solid" color="#ffffffff"
  style>` from `Page.paper` (plain -> plain, lined -> ruled, grid -> graph, dotted -> dotted).
  Pages showing a PDF (user PDFs always; stock paper templates only with `--paper pdf`) get
  `<background type="pdf" pageno>`; the first one also carries `domain="attach"
  filename="attachments/bg.pdf"`, exactly as Xournal++'s saver writes it. Xournal++ has one
  background PDF per document: pages of a second PDF get plain paper (warning).
* **One layer per page**: images, then strokes, then text (the model's z-order). Empty pages
  get `<layer/>` (old Xournal needs a layer).
* **Strokes**: `tool="pen"` or `"highlighter"`; colour `#rrggbbaa` (highlighters `7f`, as
  Xournal++ writes them; pens the model's alpha); Bezier chains flattened with
  `geometry.flatten_bezier` at about 1 pt; constant widths as one value, varying widths as
  `nominal w0 ... w(n-2)` (the exact inverse of the reader, so Xournal++ -> gnnote ->
  Xournal++ keeps every width); a one-point stroke becomes two identical points (Xournal++
  ignores one-point strokes; a dot is stored this way by Xournal++ itself). Coordinates are
  written with 4 decimals. Pencil strokes become pen strokes (warning).
* **Shape fills** (`Stroke.kind == "fill"`): merged into the `fill` attribute (alpha x 255) of the
  ink stroke right before or after it when that stroke's bounding box matches the fill's
  polygon (2 pt + 2 %) and has the same colour; otherwise written as their own closed stroke
  with a 0.1 pt outline in the fill's colour (warning), which the reader reads back as the fill
  alone.
* **Images**: PNG / JPEG as `<image left top right bottom>` with base64 data; PDF images
  (vector stickers) as `<teximage text="">` holding the PDF, which Xournal++ renders like a
  LaTeX formula. A rotation is written as a version-5 `matrix` attribute next to the version-4
  box: released Xournal++ (up to 1.3.8) ignores it and shows the image unrotated (warning);
  master uses it.
* **Text**: `<text font size x y color>` with the first run's font (PostScript names such as
  `HelveticaNeue-Bold` become `HelveticaNeue Bold`; bold / italic runs add ` Bold` / ` Italic`),
  size and colour; several differently styled runs keep the first run's style (warning);
  `align` for centred / right-aligned boxes; `wrap` = the box width when the longest line looks
  wider than the box (estimate 0.5 em per character), which Xournal++ 1.3 and later honour; a
  rotation as a version-5 `matrix` (released versions show the text unrotated). Characters XML
  1.0 forbids are dropped; carriage returns are kept as `&#13;`.

## 4. Limitations and what to check in the app

* **Unverified in Xournal++**: the ZIP package written for PDF backgrounds (the loader code of
  1.2.0 .. 1.3.8 and master accepts it, and Xournal++'s own test files of this shape load; no
  release writes it); `teximage` elements holding a converted PDF sticker with an empty LaTeX
  text; the `matrix` attributes on master; fills written as their own hairline stroke;
  `wrap` widths. The gzip output follows inkterop's writer, which passed an open check in
  Xournal++ 1.3.5.
* One background PDF per document; one layer per page on output; no audio, links, LaTeX
  source, dash patterns, iso / stave papers or background images on gzip files with sibling
  files.
* Text box sizes are estimates on read (Xournal++ stores none).

Open in Xournal++ (1.3.x and master): a GoodNotes notebook with a user PDF (packaged output),
a Notability note with highlighters, images and text, and a document with shape fills;
check that pages, ink widths, highlighter translucency, fills, images, the LaTeX-rendered PDF
sticker and text positions look as in the source app.
