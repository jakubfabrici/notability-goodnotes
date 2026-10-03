# Noteful (`.noteful`) — format notes and gnnote's mapping

Noteful (iPad and Apple-silicon Mac, Noteful Technologies Ltd) exports a notebook as a native
`.noteful` file next to "Flattened PDF" and images, and imports such files back with the
handwriting editable. gnnote reads and writes that file (`gnnote/noteful/`).

Sources: the 10 app-written files of
[notesconverter](https://github.com/samuelsadok/notesconverter) (MIT, pinned at `95be29d` in
`tests/conftest.py`), each with Noteful's own PDF export; notesconverter's parser, run as a
separate process in its strict mode, as the test oracle; hands-on decoding of those files. The
samples were written by Noteful 1.4.25 (format 1.18, object version 280; dated 2025-08 to
2026-01) and 1.4.33 (format 1.2, object version 288; `image-insert`, 2026-06). Nothing here
was checked on a device by this project (see section 9).

Lengths are **Noteful units of 1/132 inch** (`pt × 132/72`, the scale GoodNotes uses too),
origin at the page's top-left corner, y down, one frame per page. Timestamps are microseconds
since 2001-01-01 UTC (Apple's reference date); dates are seconds since then (float64).

## 1. Container

```
offset 0        AA BB CC DE                        magic
                blob, blob, ...                    raw blobs back to back, no per-blob header
R               root record (L bytes)
end - 16        AA BB CC DE 00 00 00 00 u32 R u32 L  trailer, big-endian
```

Root record: `0x0001` f32 format version (1.18 or 1.2), `0x0002` [str] the notebook UUID
`N`, `0x0003` [str] embedded file UUIDs (thumbnail JPEG, PDFs, pictures), `0x0004` [str]
annotation UUIDs (absent when no page has content), `0x000a` [str] blob names, `0x000b`
[u64] offsets, `0x000c` [u64] lengths. Blob names: `n:N` (header), `d:N` (notebook), and the
plain UUIDs of files and annotations. UUIDs are 32 upper-case hex digits without dashes. The
app writes the blobs in the byte order `n:`, thumbnail, `d:`, other files, annotations.

## 2. TTV records

A record is a run of entries `u16 tag, u16 type, value [, u64 stamp]`, all big-endian, whose
end is given by the enclosing length. Type = base type | `0x0400` (a list: u32 count, then
the values) | `0x0800` (a u64 timestamp follows the value). Base types: `0x01` bool (1 byte),
`0x02` u64, `0x03` f32, `0x04` f64 date, `0x05` UTF-8 string, `0x06` bytes, `0x07` nested
record (each of the last three: u32 length, then the bytes), `0x11` u16, `0x12` u32, `0x13`
u64, `0x14` i32, `0x20` f64, `0x21` size (2 × f64).

Fixed-size types carry no length, so a value of an unknown type cannot be skipped: the rest
of the record it sits in is lost. Nested records are length-prefixed, so that damage never
spreads further out (`gnnote/noteful/ttv.py`).

Keyed collections (pages, layers, bookmarks, a page's objects, three always-empty ones):
`0x0001` [keys] (string UUIDs; u32 ids for layers), `0x0002` [u64 stamps], `0x0003` [bool
present], `0x0000` [records]. A key marked not present is a deleted entry (not seen in the
samples; the reader skips such values).

## 3. Header and notebook

* `n:` — `0x0001` N, `0x0003` title (+stamp), `0x0004` device UUID (`""` from macOS),
  `0x0005` modified date, `0x0006` six false flags, `0x0007` {`0x0001` thumbnail file,
  `0x0002` link UUID of the cover page}, `0x000a` a random UUID, `0x000b` {0, {[0,0,0,0]}},
  `0x000c` created date, `0x000d` an integer close to the content size (meaning unknown;
  notesconverter writes 0), the rest constant.
* `d:` — `0x0001` N, `0x0002` pages, `0x0003` layers (the samples: one, "Layer 1", id 0,
  ordering tag "0"), `0x0004` bookmarks (page UUID, ordering tag), `0x0005` / `0x0007` /
  `0x000a` collections that are empty in every sample (probably outlines, tags, recordings),
  `0x0006` a random UUID, `0x0008` u32 0, `0x0009` 0.

## 4. Pages and backgrounds

Page: `0x0001` UUID, `0x0002` {`0x0000` annotation UUID or "", `0x0002` [picture file
UUIDs shown on the page], `0x0003` []}, `0x0004` background (+stamp), `0x0005` ordering tag
(+stamp), `0x0006` link UUID, `0x0007` / `0x0009` false. Ordering tags are 7 characters of
`+/0-9A-Za-z`, an alphabet in ASCII order, so pages sort as plain strings.

Background type 1 = **PDF page**: `0x0001` display size (units; the PDF page is stretched to
it), `0x0003` **0-based** page index, `0x0004` PDF file UUID (the user's PDF, byte for byte).
Type 2 = **paper template**: `0x0001` size, `0x0003` UUID of a one-page PDF of the template
**rendered by the app**, `0x0004` SHA-1 hex of `0x0006`, `0x0005` `"cb.simpleline"`, `0x0006`
JSON such as `{"lh":0,"lt":0,"lw":0.5,"name":"Blank","pc":16777215,"pi":"…","si":[w,h],"type":0}`.
Only "Blank" was sampled; the keys of lined, grid or dotted templates are unknown.

## 5. Ink (annotation `0x0002`)

Annotation record: `0x0001` u64 object version (280 / 288), `0x0002` the ink blob,
`0x0003` / `0x0004` empty u64 lists, `0x0005` the objects. The ink blob is a sequence of
records without lengths (an unknown record ends it):

| record | layout |
|---|---|
| style `F1 02` (44 bytes, applies to the strokes after it) | 4 × f64 RGBA, u16 blend (0 normal = pen, 1 multiply = highlighter), u64 dash (0 solid, 1 dashed, 2 dotted) |
| stroke `F1 01` | u16 + u32 + u16 random id; u16 1 = a radius per point; u64 save time; u64 creation time; u64 **z key**; u32 0; f64 nominal radius; u32 0; u32 point count N; then N ≤ 4: N × f32 (x, y[, r]); N > 4: f32 (min, span) per dimension, then N × u16 with value = min + q / 65535 × span |

Strokes are polylines (the app smooths them when drawing); a dot is two identical points.
The stored width is the **radius**: in the PDF export the ink extends r on either side of the
centre line, and dashed ink is stroked 2r wide. A highlighter stores alpha 1.0; the app draws
blend multiply at 50 % opacity. No field names the tool (ballpoint or fountain pen).

## 6. Objects (annotation `0x0005`)

Object: `0x0001` UUID, `0x0002` {`0x0001` [centre x, centre y, width, height, rotation
rad], `0x0002` i32 flip bits (1 horizontal, 2 vertical)} (+stamp), `0x0004` u32 0, `0x0005`
**z key** (+stamp; orders objects against ink), `0x0006` data, `0x0007` "", `0x0008`
opacity, `0x0009` u16 0, `0x000a` u64. Rotation turns the box about its centre, clockwise on
the page (checked on a rotated rectangle in the export). Data: `0x0016` version, `0x0001`
type — **1 image, 2 text box, 3 rectangle, 6 ellipse, 12 polygon, 20 line, 21 cubic Bezier**
— and `0x0002` size (+stamp).

* **Shapes**: outline `0x0007` {`0x0007` {[f32 RGBA]}, `0x0002` thickness (full width),
  `0x0003` {dash}, `0x0004`..`0x0006` i32 1, 1, 0, `0x0008` {arrow head 0 / 1}}; fill
  `0x0005` {`0x0001` {[f32 RGBA]}, `0x0002` flag, [`0x0003` f64]} — when the flag is true the
  colour is the outline's and nothing is filled. Polygons, lines and Beziers carry `0x000d`
  {[f64 x, y, …] relative to the box's top-left corner in the data size, [i32 commands]:
  0 move, 1 line, 3 cubic (CoreGraphics path element types; 2 quadratic and 4 close)}.
  Rectangles have `0x0014` corner radius (the app draws continuous corners). Ellipses are
  drawn as four Bezier quarter arcs. An arrow head is a filled triangle 4.5 line widths long
  and wide whose tip is the path's end point; the shaft is shortened to its base.
* **Text box**: `0x0004` rich text = `0x0002` [chunks], `0x0003` [attribute count per
  chunk], `0x0004` [i32 keys], value lists `0x0005` strings, `0x0006` bools, `0x0007`
  colours, `0x0008` integers, `0x0009` records, `0x000a` f64. Keys: 1 font family, 2 bold,
  3 italic, 4 underline, 5 strikethrough, 6 colour, 7 a second colour, 8 alignment (0 left,
  1 centre, 2 right), 9 an always-empty record, 10 font size (units), 11 / 12 line spacing,
  13 / 14 font face / family (newer files). Each chunk lists only the keys that change from
  the chunk before (the font size every time); each key takes the next value of its type's
  list. The app may append a zero-width-space chunk. The text sits 5 units inside the box
  horizontally and 2 vertically. `0x0005` background colour, `0x0009` {0}, `0x0013` {0, 1 or
  2}, `0x0014` corner radius, `0x0015` margin.
* **Image**: `0x000a` picture file, `0x0002` the whole picture's size at import (units),
  `0x000b` pixel size, `0x000c` crop polygon in that size's frame, `0x0018` 0 (288 only).
  The box is the **visible (cropped) area**; the whole picture is drawn at its natural size ×
  box / crop and clipped; a flip mirrors about the box centre. The page's files-on-page list
  must name the picture (notesconverter: otherwise Noteful often does not show it).

## 7. Mapping

| Model | Noteful → model (reader) | model → Noteful (writer) |
|---|---|---|
| `Document.title` | `n:` `0x0003` | same |
| page order | ordering tags | tags in ASCII order |
| `Page.width / height` | background size × 72/132 | pt × 132/72 |
| `PdfBackground` | type-1 background; the PDF goes to `Document.pdfs` | type-1 background; one file per `pdf_id` (the source app's stock paper PDFs are carried too) |
| stock paper | type-2 template: the app's rendered PDF as background, `template_is_builtin`; `paper` from the template name (only "Blank" seen). A one-page PDF produced by gnnote is stock paper too (its ruling is read back as `paper`) | a page without PDF: `pdfutil.make_paper_pdf(w, h, paper)` as a type-1 background (what notesconverter does) |
| ink `Stroke` | polyline, width = 2 r × 72/132 per point, `Stroke.width` from the nominal radius; blend 1 → `highlighter`, alpha × 0.5; `pen` unknown (None) | Bezier strokes flattened (1 pt), r = width / 2 × 132/72, per-point radii when widths vary; highlighter → blend 1, alpha 1.0 |
| shapes | strokes with exact cubic `controls` (lines as thirds handles, ellipses as 16 arcs, rounded corners as quarter arcs); fill → `kind = "fill"` before its outline; arrow head → a stroke tracing the triangle | — (model shapes arrive as ink) |
| `kind == "fill"` | — | a filled polygon object per outline ring, coded like the app's filled ellipse |
| `TextBox` | runs (font, size, bold, italic, underline, colour), alignment of the first run; frame = box − (5, 2) inset; rotation pivot moved from the centre to the frame's top-left corner | the reverse; font names become family names ("HelveticaNeue-Bold" → "Helvetica Neue" + bold); the box grows to at least 1.2 font sizes per line |
| `Image` | the whole picture at the scale shown, `rotation` in degrees | type-1 object (box = model box, natural size = box, pixel size from the header), files-on-page entry, one file per distinct picture |
| z-order | ink, shapes, text and images interleave by z key; the model keeps them in separate lists | z keys grow in the order images, ink and fills, text boxes |

## 8. Lossy steps (each one warning, counted per document)

Reader: dashed / dotted ink and outlines are drawn solid; cropped images are placed whole
(the hidden parts show again); mirrored images are placed unmirrored; translucent images
become opaque; text box background colours and strikethrough are dropped; stacking other
than images < ink < text is simplified; layers are merged; bookmarks, the unknown
collections and embedded files of other kinds (audio) are dropped; objects of unknown type
and records of an unknown object version are reported. Writer: PDF images (vector stickers)
and images that are neither PNG nor JPEG are dropped; a page whose PDF is missing or lacks the
page gets generated paper; a PDF page of another shape than its page is stretched by Noteful;
EXIF-rotated photos warn (whether Noteful applies the orientation on top is unverified).

Hardening (`docs/design.md` §1): every offset, length and list count is checked against the
bytes that remain; nesting is limited to 32 levels; one file yields at most 10 000 000 TTV
values, 10 000 000 ink points (1 000 000 per stroke) and 10 000 pages; an unknown TTV type or
ink record costs only the record it sits in (a page's objects or the rest of its ink); a
missing page list rebuilds the pages from the annotation records. Only a missing magic, an
unreadable root index or a file without any page information raise `ValueError`.

## 9. Not verified on a device

The writer follows the structure notesconverter's author imported on iPads and Macs with
Noteful 1.4.25 and 1.4.33 (format 1.18, object version 280), checked here only against
notesconverter's strict parser. To be confirmed on Noteful 1.5:

* the file opens at all (format 1.18 / 280 rather than the 1.2 / 288 of current files), with
  random stroke ids and real timestamps (notesconverter writes zeros);
* ink is lasso-selectable and recolourable, highlighters render translucent, variable-width
  strokes keep their taper;
* filled polygons (`kind = "fill"`; only a filled ellipse was sampled), PNG pictures (only
  JPEG was sampled), text boxes with several runs, fonts the device lacks, rotated text boxes
  (the centre pivot was inferred from images and rectangles);
* pages on gnnote-generated paper PDFs and the thumbnail JPEG (any JPEG is accepted
  structurally);
* the reader's handling of multi-layer pages, hidden layers, deleted objects, tape, stickers,
  audio, tags, outlines and page links, and of lined, grid, dotted and coloured templates —
  none of these is in the samples.

Samples that would close the biggest gaps (Noteful 1.5, each exported as "Noteful" and as
"Flattened PDF"): `pens.noteful` (ballpoint and fountain pen, highlighter, dots, dashes),
`mixed-2pages.noteful` (a PNG and a JPEG, a rotated text box with two runs, a filled rectangle,
a lined template), `papers.noteful` (grid, dotted, coloured paper), `layers.noteful` (two
layers, one hidden, an erased object), `extras.noteful` (tape, a sticker, audio, a tag, an
outline entry, a page link), and a gnnote-written file re-exported by the app after import.
