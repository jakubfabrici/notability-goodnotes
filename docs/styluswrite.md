# Stylus Labs Write (`.svgz`, `.svg`): format facts and gnnote mapping

Codec: `gnnote/styluswrite/` (`reader.read_write_svg`, `writer.write_write_svg`, `svgpath`),
registry id `styluswrite`, output extension `.svgz`, inputs `.svgz` and `.svg` (Write's
single-file HTML is recognised by its content too).

Stylus Labs Write is a handwriting app for Windows, macOS, Linux, iPad and Android. It keeps a
document as one SVG file. Its source code is AGPL-3.0 and was **not** read. The facts below come
from Write's FAQ (styluslabs.com/faq), the README of `styluslabs/templates` (no licence; facts
only), the MIT `pdftowrite` tool (kosmospredanie/pdftowrite, its document skeleton), inkterop's
MIT reader (`core/src/inkterop/formats/svg/write.py`) and the bytes of documents Write saved:
pages of the Stylus Labs web site (no licence stated; `site1_page006.svg` and
`site1_page007.svg` are pinned by SHA-256 in `tests/conftest.py` and never committed, five more
pages of the site were inspected locally) and, for inspection only, the AGPL data files
`Intro.svg`, `Help.html` and `clippings.html` of the Write repository. Confidence markers: **[verified]** seen in those files or documents; **[inferred]**
consistent evidence, not checked in the app; **[unverified]** not checked in the app.

**gnnote's Write files have not been opened in Write yet.** Section 6 lists what to check.

## 1. Containers

* `.svgz` (Write's default) is gzip-compressed SVG, `.svg` the same uncompressed. The FAQ also
  names `.htm` (one HTML file with one `<svg>` per page) and `.html` (an HTML file plus one
  `.svg` per page; not supported by gnnote, it is several files) [verified FAQ].
* The FAQ warns that some apps cannot open Write's `.svgz` because they "do not fully implement
  the gzip specification"; which gzip feature that is, is unknown. gnnote therefore reads the
  container by hand (`container.py`, from RFC 1950 / 1952): the gzip header is skipped field by
  field, the body is inflated as raw deflate in 4 KB pieces, and the CRC-32 and length of the
  trailer are only checked afterwards. A wrong, zeroed or missing trailer keeps the whole
  document (one warning); damage inside the stream keeps everything inflated before it (one
  warning); concatenated members are joined, bytes after the last member are ignored, and a
  zlib stream is accepted too. The sniffer inflates the head the same way, so a damaged small
  file is still recognised [inferred tolerance; no real `.svgz` was available].
* Write 2 (until 2017) saved XHTML (`Help.html`): a `<!DOCTYPE html PUBLIC ...>`, a
  `<script type="text/writeconfig">` header with `pageWidth`, `xRuling`, `ruleColor` (a signed
  ARGB integer) and similar, then per page `<svg width height><g id="page_1" width height
  xruling yruling marginLeft papercolor rulecolor>` (every page group has the id `page_1`) with
  `path.ruleline` rule lines and unclassed stroked paths [verified `Help.html`].

## 2. Document structure (Write 3)

```
<svg id="write-document" xmlns=SVG xmlns:xlink=XLINK>
  <rect id="write-doc-background" width="100%" height="100%" fill="#808080"/>
  <defs id="write-defs">
    <script type="text/writeconfig"> <int name="docFormatVersion" value="2"/> <int name="pageNum" value="0"/>
      <float name="xOffset" value="-10"/> <float name="yOffset" value="-10"/> </script>
  </defs>
  <svg class="write-page" color-interpolation="linearRGB" x="10" y="10" width="900px" height="1820px">
    <g class="write-content write-v3" width="900" height="1820" xruling="0" yruling="40" marginLeft="100"
       papercolor="#FFFFFF" rulecolor="#FF0000FF">
      <g class="ruleline write-std-ruling write-scale-down" fill="none" stroke="#0000FF" ...>
        <rect class="pagerect" .../> <path class="yrule_1" d="M0 40 l900 0"/> <path d="M0 80 l900 0"/> ...
        <path class="leftmargin" stroke="#FF0000" d="M100 0 l0 1820"/>
      </g>
      ... editable content ...
    </g>
  </svg>
  ... one svg.write-page per page ...
</svg>
```

* A single-page web export is just the page `svg` (with or without the `write-page` class); its
  `g.write-content` may lack `width` / `height` (the page `svg` has them) [verified site pages].
  `Intro.svg`'s page `svg` has no class at all, so gnnote takes every `g.write-content` as a page,
  wherever it is.
* The `g.write-content` attributes are the Page Setup values; width, height and paper colour are
  repeated on `rect.pagerect` and the page `svg` [verified templates README and files].
* `papercolor` is `#RRGGBB`; `rulecolor` is **alpha first**, `#AARRGGBB`: `#7F0000FF` goes with
  `stroke-opacity="0.498"` on the rule lines (0x7F / 255) and Write 2's `ruleColor=-16776961`
  is 0xFF0000FF [verified].
* `g.ruleline` is the page background; Write's standard ruling carries `write-std-ruling`, custom
  templates draw anything there (patterns with `currentColor`), pdftowrite puts a whole PDF page
  converted to SVG there [verified README, pdftowrite].
* The FAQ: only content inside `svg.write-page` is kept, only content inside `g.write-content`
  can be edited; the README: content after `g.ruleline` is editable.
* Element attributes Write adds: `__timestamp` (hex milliseconds since 1970), `__comx` (the
  centre of the stroke's bounding box in x, measured: 93 % of the strokes of `site1_page006` and
  all of `site1_page007` within 0.01 unit) and `__comy` (shared by the strokes of one written line; not the stroke's own
  centre) [verified measurements; meaning of `__comy` inferred].

## 3. Units

The FAQ says the Page Setup units are 1/150 inch, and the templates README says the Page Setup
values are the numbers in the file. gnnote therefore reads **1 unit = 72 / 150 = 0.48 pt**
(`gnnote.styluswrite.PT_PER_UNIT`, the only place the factor is defined). The samples agree
better with 1/150 inch than with the CSS pixel (1/96 inch) other tools assume (pdftowrite,
inkterop):

| Value in Write's files | at 1/150 inch | at 1/96 inch |
|---|---|---|
| default ruled pitch `yruling="40"` (Write 2 and 3) | 6.8 mm (college-ruled paper is about 7 mm) | 10.6 mm (wider than any common ruling) |
| the `Dot grid 25` template | 4.2 mm (dot grids are usually 5 mm) | 6.6 mm |
| pen widths 1.2 to 1.75 | 0.20 to 0.30 mm | 0.32 to 0.46 mm |
| the template page 768 x 1050 | 130 x 178 mm | 203 x 278 mm |

The page sizes do not decide it: 768 is the iPad's screen width in points, not a paper size,
and the web site's pages are 900 units wide because the site shows them 900 CSS pixels wide. The
check in Write: export a page with a known Page Setup size to PDF and read its MediaBox.

## 4. Reading (`reader.py`)

**Hardening.** The XML is parsed by expat into an `xml.etree` tree; any entity declaration
(internal or external) and any internal DTD subset is refused before it can expand
(`ValueError`), a `<!DOCTYPE>` that only names an external DTD (Write 2's XHTML, Inkscape) is
allowed and never fetched. Limits: 256 MB inflated XML (`ValueError` beyond), nesting depth 128,
2 000 000 elements, 10 000 pages, 200 000 vertices per path, 10 000 000 per file (curves
included; every path command that adds no vertex, such as `Z`, costs one unit of that budget
too, so a flood of `zzzz` cannot keep the parser busy), 256 segments per flattened curve or arc,
64 functions per transform list, 64 MB per embedded image, 32 generated paper PDFs and 250 000
rules and dots drawn into them per file (50 000 per paper), 50 empty text lines inferred per
baseline gap and 100 000 per file, 256 MB of decoded image data per file (an image drawn again
by `<use>` counts each time; it is decoded once and its bytes are shared). `<use>` is bounded
too: at most 4 reuses nested in one another, a walk at most 128 levels deep however the targets
nest, 1 000 000 reused elements and 16 MB of reused attribute values and text per file (each
target's elements are counted when it is first examined and again by every instance that draws
it, and every instance is charged the length of the target's attribute values and texts, which
drawing it parses again, so nested groups of reuses cannot multiply the work, and neither can
one element with megabytes of path data, style or text; an image's data is decoded and measured
once per element and is charged only by the image data budget above; the first instance over a
budget ends all reuse, one warning); a reference to the use itself, to an element holding it or
to a target already being drawn is a cycle and draws nothing (one warning each kind). A
presentation value (a colour, length, font name, ...) longer than 1024 characters is never
valid; it is ignored as CSS ignores invalid values (one warning), so a huge value on a group is
not parsed again by every shape inheriting it. Lengths are matched with one run of white space
between number and unit (a pattern with two adjacent optional runs backtracks quadratically on a
long run of spaces), and the size, `viewBox` and inherited properties of a page `svg` are parsed
once however many pages share it (Write 2 puts every page group straight into the root `svg`).
Long word lists are never split whole: `str.split` makes an object per word, about 20 bytes per
character, so a 250 KB `.svgz` with a 250 MB `class`, `x` / `y` / `dy` list, text, title,
`preserveAspectRatio` or image payload used to need 6 GB. Only the words a value needs are taken
(the first 64 class names, the first item of a position list, the first three words of
`preserveAspectRatio`, the title's first 200 words), default-mode text is collapsed 64 KB at a
time, and base64 decoding skips white space itself. Every stroke or ring the reader makes costs 16 units of the
10 000 000-point budget on top of its vertices, and a shape that is both filled and stroked pays
for its points twice (it makes them twice), so a flood of tiny subpaths cannot cost more memory
than the same budget spent on long strokes (beyond it: one warning). A dot `<pattern>` is read
once however many rectangles it fills, and every circle of every filled rectangle counts
towards the paper's 50 000 dots, so a pattern of many circles filling many rectangles stops
early (the paper is drawn without dots, one warning). A damaged or truncated file keeps everything before the
damage (one warning). Points beyond 10^7 pt are skipped, widths and font sizes are clamped to
10^4 pt. Extreme numbers never raise: arc radii that underflow or overflow when squared are
computed in units of the chord (tiny radii are scaled up to a half ellipse as SVG asks, the
small arc of a huge radius is a straight line), curve bends that overflow get the segment cap,
and any arithmetic error left in one element's handler skips that element with the "invalid"
warning. Only `ValueError` leaves `read_write_svg`: not XML, not a Write document (an ordinary
SVG without `g.write-content` included: "not a Stylus Labs Write document"), refused DTD, over
the size limit.

**Path data** (`svgpath.py`, written from the SVG 1.1 specification): all commands absolute and
relative, implicit repetition, the full number grammar (signs, exponents, packed numbers such as
`0.5.5` and `-1-2`, packed arc flags); curves and arcs are flattened to 0.1 pt; parsing stops at
the first error and keeps the part before it, as SVG viewers do. A subpath that is only a moveto
(`M x y` followed by another `M` or the end, or an arc to its own start) draws nothing in SVG,
not even a cap, and is left out; `M x y Z` and a zero-length segment draw caps and become dots. Write's own ink uses only
`M x y l dx dy ...` (one subpath, no `Z`) [verified every ink path of the samples].

| Write | Model |
|---|---|
| `g.write-content` / Write 2 `g#page_N` with ruling attributes | one `Page` each, in document order; size from its `width` / `height`, else the page `svg`, else `rect.pagerect`, else 768 x 1050 units (warning); a `viewBox` on the page `svg` is honoured |
| standard ruling (`g.ruleline.write-std-ruling`, or no `g.ruleline` as in Write 2): `xruling`, `yruling` | `Page.paper`: both > 0 grid, `yruling` lined, vertical rules alone plain; a fully transparent `rulecolor` is no ruling |
| standard ruling: `papercolor`, `rulecolor`, `xruling`, `yruling`, `marginLeft` | a generated one-page paper PDF (`paper.py`; `PdfBackground`, `template_is_builtin = True`) that draws what Write draws: the paper colour, horizontal rules every `yruling` from one pitch below the top, vertical rules every `xruling`, a red margin line at `marginLeft`, rules one unit wide in the alpha-first `rulecolor` (the margin keeps its red and takes the rules' alpha), colours blended over the paper so the PDF needs no transparency; writers that draw stock paper keep the rules under the handwriting [verified against `Intro.svg` rendered in Chromium] |
| custom ruling (no `write-std-ruling`) made only of circles: a `<pattern>` of circles filling a rectangle (Write's dot templates, gnnote's dot paper) or circles drawn one by one | `Page.paper = "dotted"`; the paper PDF draws the dots where SVG puts them (tile origin, pitch, radius, `currentColor` = the ruling's `color`; a stroke widens a dot) |
| custom ruling holding only the page rectangle | plain paper of the paper colour (Write draws what `g.ruleline` holds, whatever the Page Setup values say) |
| any other custom ruling (pattern rulings such as the templates README example, explicit lines, drawings, vector PDF pages) | one warning; plain paper of the paper colour; the `xruling` / `yruling` values are not used to guess a ruling. Non-rule content on top of a standard ruling gets the same warning and keeps the standard ruling |
| images inside `g.ruleline` | page images below the ink (pdftowrite's raster pages: the PDF page is an `svg.page-background` whose `width` / `height` are in Write units and whose `viewBox` is in PDF points; nested `svg` elements map their `x`, `y` and `viewBox` here as in the content). pdftowrite's default mode imports the PDF through poppler and cairo, which stores each raster image once in `<defs>` at its pixel size and draws it with `<use xlink:href="#id" transform="matrix(...)">`: the image is placed where the `use` draws it [verified against Chromium on pages made with `pdftocairo -svg`]. Glyphs drawn the same way (`symbol` + `use`) are vector content: a custom background. The invisible text layer pdftowrite adds for searching (`fill-opacity: 0`, no stroke) draws nothing and is skipped. When a custom background had images, the warning says that only its images were kept |
| `defs`, `symbol`, `clipPath`, `mask`, `pattern`, `marker`, gradients, `filter` (in the content and in `g.ruleline`) | never drawn where they stand; `g.ruleline` takes its dot patterns from them |
| `path.write-stroke-pen`, unclassed stroked paths and shapes | constant-width `Stroke` per subpath, width = `stroke-width` x transform scale; `Z` repeats the first point; `pen = "ballpoint"` for Write's pen class |
| `path.write-flat-pen` | the filled outline has an even vertex count *n*; vertex *i* faces vertex *n - 1 - i*: centre line = their midpoints, `Point.width` = their distance, `Stroke.width` = `stroke-width`, `pen = "fountain"` (measured on 2 060 strokes: all even, widths at most 1.32 x nominal, end widths 0.07 to 0.61 x nominal for 90 % of the ends, median 0.33) [verified]. Any other shape of the outline, and an outline shortened by the 200 000-vertex limit (its return side is gone, so its vertices no longer pair up), stays a filled shape (warning) |
| gnnote's own mark `data-gnnote-kind="pen"` / `"highlighter"` | that kind, whatever the opacity (see section 5) |
| unmarked translucent ink (alpha after `opacity`, `stroke-opacity`, colour alpha <= 0.6), a `highlight` class, `mix-blend-mode: multiply` | `kind = "highlighter"`. No sample has a highlighter, so how Write marks one is unknown [unverified] |
| other filled shapes (pasted SVG art: `path`, `rect`, `circle`, `ellipse`, `polygon`, ...) | `Stroke(kind="fill")` with one ring per subpath |
| `transform` (`matrix`, `translate`, `scale`, `rotate`, `skewX`, `skewY`), nested `svg` | applied to the geometry (moved strokes keep their `d` and gain a `translate`) [verified] |
| `<image>` with a PNG / JPEG `data:` URI | `Image`; `preserveAspectRatio` (default `xMidYMid meet`) fitted, rotation from the matrix; linked files, other formats and `slice` cropping are warned |
| `<text>` / `<tspan>` | `TextBox`: lines come from the baselines. A `tspan` whose `y` / `dy` moves the baseline by half a line or more starts a line (smaller moves are super- and subscripts); a `tspan` without characters is ignored, positions included, as browsers do; a `tspan` before the line's first visible character places the line instead: its `x` and baseline become the line's (white space drawn before it at another `x` is dropped, so the indentation of pretty-printed SVG does not move the text; with no `x` the spaces stay, as they move the text after them). With `xml:space="preserve"` spaces are drawn characters, so a line of only spaces followed by a new line at the same `x` is a line of its own (gnnote's writer writes such lines); default white space handling collapses spaces, tabs and newlines, never a no-break space, which SVG draws like any other character [checked in Chromium]; a gap of about two line heights or more between lines puts the empty lines back (the line height is `1.1646 em`, gnnote's, when every gap is a whole number of it, else the text's own smallest line distance); the box starts at the first line's `x` and baseline. Styled chunks become runs (bold, italic, underline, family, size, colour), `text-anchor` gives the alignment; the box size is estimated (warning) |
| `g.hyperref`, `<a>` | the ink is kept, the link target is dropped (one warning); the transparent hit rectangle is invisible and skipped |
| `path.bookmark` | dropped (warning) |
| `<use xlink:href="#id">` (content and `g.ruleline`) | the target is drawn as if it were a child of the `use`: the use's `transform`, then a move by its `x` / `y`; a `symbol` or `svg` target maps its `viewBox` into the use's `width` / `height` (else its own). References to other files, to missing or non-drawing elements, cycles and reuses beyond the limits above are skipped (one warning) |
| gradients and patterns as paint, `clip-path` / `mask`, `foreignObject` | skipped or drawn unclipped (one warning each) |
| `<title>` of the document | `Document.title` (Write 2's fixed "Handwritten Document" is ignored; otherwise the file name is used) |

## 5. Writing (`writer.py`)

The skeleton of section 2: `svg#write-document`, `rect#write-doc-background`,
`defs#write-defs` with the `text/writeconfig` block (`docFormatVersion` 2, `pageNum` 0, view
offset -10 / -10 as in `Intro.svg`), one `svg.write-page` per page with `color-interpolation=
"linearRGB"`, `x = 10` and `y` stacked downwards with a 20-unit gap (pdftowrite puts every page
at 10 / 10 and Write accepts it, so Write lays pages out itself; stacking lets a browser show
them all), and `g.write-content write-v3` with the Page Setup attributes. The ruling:

| Model paper | Written |
|---|---|
| lined / ruled | `yruling="40"` (Write's default), `g.ruleline write-std-ruling` with `rect.pagerect`, `path.yrule_1` and the other horizontal lines |
| grid | `xruling = yruling = 25`, horizontal and vertical lines |
| dotted | `xruling = yruling = 25`, a custom ruling (no `write-std-ruling`): a dot `<pattern>` filling the page, the way Write's custom templates draw backgrounds |
| plain | rulings 0, the page rectangle only |

Rule colour `#FF0000FF` (Write's default), paper white, margin 0 (the source paper's colours
and margin are not carried over: the model has only the paper style). Content after the ruling:
images, ink, text. Coordinates are written with three decimals through integer thousandths, so
a long relative `l` list does not drift.

* A stroke whose widths vary by more than 1 % (and `Options.pressure`, on by default) ->
  `path.write-flat-pen`: the left side forward and the right side backward (2 n vertices, no
  `Z`), offsets along the normal of the chord between the nearest distinct neighbours,
  `stroke-width` = the nominal width. The reader's pairing gives the points back exactly (to
  0.001 unit). Its ends are square: the outline has no round caps (Write's own outlines taper).
* Other strokes -> `path.write-stroke-pen` (round caps and joins, the median width); a single
  point becomes a zero-length segment. Bezier strokes are flattened to about 1 pt.
* Highlighters -> pen paths with `stroke-opacity` = their alpha (0.5 when opaque or above 0.6)
  and `data-gnnote-kind="highlighter"`; one warning, as Write's own encoding is unknown.
* Translucent pens (alpha 0.6 or less, such as CollaNote's 0.49 pens or Excalidraw's 60 %
  opacity) keep their alpha and carry `data-gnnote-kind="pen"`, because without it the reader's
  opacity rule would take them for highlighters. gnnote's reader trusts this mark over the
  opacity rule, so both kinds come back as what they were; whether Write keeps an unknown
  attribute when it saves a file is not known (section 6), and without the mark the opacity rule
  applies again.
* Shape fills -> unclassed `<path fill>` with one `Z` subpath per ring.
* Images -> `<image x y width height xlink:href="data:image/png|jpeg;base64,...">`, rotation as
  a `matrix` about the box centre, `preserveAspectRatio="none"` when the box and the picture
  differ by more than 1 %. PDF images are dropped (warning).
* Text boxes -> `<text xml:space="preserve">` with one `tspan` per line and nested `tspan` runs;
  SVG text does not wrap, so lines are broken at the box width with Helvetica's widths (as the
  PDF writer does, in linear time). Every line's `tspan` has its own absolute `x` and `y`
  (first baseline + *k* x 1.1646 em); an empty line gets no `tspan` at all, because positions
  on a `tspan` without characters are ignored by SVG renderers, and the lines after it keep
  their own `y` (the reader puts the empty line back from the gap). Write shows such text but
  cannot edit it (FAQ; one warning). Words are wrapped one at a time and each line is joined as
  soon as it is complete, so memory stays linear in the text; a file holds at most 8 000 000
  characters of text, each line counting 32 more (its `tspan`), and text boxes beyond that are
  cut short or skipped (one warning).
* Explicit rule lines are written for viewers that do not compute them from `xruling` /
  `yruling`, all of a page's or none: at most 2 000 per direction on a page and 200 000 per file
  (about 8 MB of SVG). Pages beyond either limit keep only `rect.pagerect` and their Page Setup
  attributes (one warning); gnnote's reader takes the ruling from those attributes, whether Write
  redraws it from them is to be checked (section 6).
* Strokes longer than 50 000 points are split into consecutive paths that share their end
  points (one warning), so no path exceeds the reader's 200 000-vertex limit (a pressure
  outline has two vertices per point).
* Not written: `__comx` / `__comy` / `__timestamp`, a thumbnail, bookmarks.
* Output: UTF-8 SVG, gzip level 9 with time stamp 0 (byte-identical for identical input).

**Known losses (warnings).** PDF page backgrounds (no PDF rasteriser or PDF-to-SVG converter
in the standard library; pdftowrite can turn the PDF itself into a Write document), PDF and
other non-PNG/JPEG images, editable text (SVG text only), pencil texture, the exact ruling pitch
and colour of the source paper. Reading: custom template backgrounds other than dot grids (one
warning; only the paper colour is kept), the vector part of PDF pages pdftowrite put in the
background (its raster images are kept as page images), papers beyond the budgets above (only
`Page.paper` is kept, one warning).

## 6. To check in Write (Windows, Linux or iPad, Write 3)

1. Open a gnnote `.svgz` with both pen kinds: are strokes selectable, recolourable, erasable
   (stroke and free eraser) and reflowed by Ruled Insert Space? Does a save and reopen keep them?
2. Does Write need `__comx` / `__comy` (ruled reflow) or does it compute them?
3. Pages 2 and later: which `x` / `y` does Write itself write, and is gnnote's stacking fine?
4. Units: export a page to PDF and compare its MediaBox with the Page Setup size (section 3).
5. Highlighter and translucent pens: draw one, save, and look at the attributes. Does Write
   keep gnnote's `data-gnnote-kind` attribute when it saves?
6. Can Python's `gzip` / `zlib` read a `.svgz` Write saved (the FAQ's gzip remark)?
7. Text written by gnnote: shown at the right place, with the right size, and with the empty
   lines (absolute `y` on every line's `tspan`)?
8. The dot pattern ruling: shown, and kept when pages are added?
9. A ruled page whose `g.ruleline write-std-ruling` holds only `rect.pagerect` (what gnnote
   writes beyond its rule-line limits): does Write draw the rules from `xruling` / `yruling`?
10. A pdftowrite document of a scanned PDF (default mode): does Write show the scan where gnnote
   puts the page image (the `<use>` placement was checked in Chromium only)?

## 7. Sources and licences

| Source | Licence | Used for |
|---|---|---|
| styluslabs.com/faq | none stated | facts (formats, units, editability, text, command line) |
| github.com/styluslabs/templates README | none (no licence file) | facts (structure, attributes, ruling) |
| styluslabs.com/svg/*.svg | none stated | test input only (two pages pinned in `tests/conftest.py`), measurements |
| github.com/styluslabs/Write `scribbleres/Intro.svg`, `Help.html` | AGPL-3.0 (data) | inspected for facts, never copied or committed |
| github.com/styluslabs/Write source code | AGPL-3.0 | **not read** |
| kosmospredanie/pdftowrite @ 2e25ee2 | MIT | the document / page skeleton (`data/document.svg`, `data/page.svg`); facts about the pages it makes (`pdftowrite.py`, `docs.py`: the poppler / cairo import of the default mode, images drawn by `<use>`, the invisible text layer) |
| cable729/inkterop @ f16eb8d `core/src/inkterop/formats/svg/write.py`, `reader.py` | MIT | consulted: alpha-first `rulecolor`, page discovery, transform composition; its CC0 fixture `write-mini.svg` is a test input |
| SVG 1.1 specification (W3C) | W3C document licence | path grammar, arc conversion, pattern tiling and text positioning, implemented in our own code |
| RFC 1950 (zlib), RFC 1952 (gzip) | IETF | the container fields `container.py` reads by hand |
