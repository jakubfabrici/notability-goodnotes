# Flexcil `.flx` / `.flex` — format notes and gnnote's reader

gnnote reads Flexcil documents (`.flx`) and Flexcil backups (`.flex`, a ZIP of documents)
(`gnnote/flexcil/`). Writing is out of scope for now: a `.flx` is easy to synthesise, but
Flexcil imports single documents only through Restore (`.flex`) or Google Drive sync, and the
required backup metadata is unverified.

Confidence markers: **[verified]** checked on the bytes of a real file or by an independent
codec; **[inferred]** consistent evidence without a controlled experiment; **[unknown]**.

Sources (see `NOTICE.md`; all MIT): flexcil-codex-plugin (a Python codec whose authors tested
edits on Android; `docs/format-and-sync.md`), flexcil-backup-viewer (TypeScript viewer;
`forms.flx`), FWebViewer (a JavaScript viewer whose README calls itself an unreviewed
prototype). No code was copied.

## 1. Document container (`.flx`) [verified on `forms.flx`, format version 0.0.5]

| Member | Content |
|---|---|
| `info` | JSON: `name`, `key` (document UUID), `version` (`0.0.4`/`0.0.5`), `type`, dates, `attachments` |
| `pages.index` | JSON list, one entry per page: `frame {x, y, width, height}` in pt (768 x 1024 = the PDF MediaBox), `rotate`, `attachmentPage {file, index}`, `key` (page UUID) |
| `attachment/PDF/<file>` | the page backgrounds: every Flexcil page shows a PDF page (templates are PDFs too) |
| `attachment/image/<key>` | inserted images (PNG or JPEG) [inferred: two viewers] |
| `objects/<page>.objects` | z-order of the page's objects: `[{type, key}]` (1 drawing, 32 shape, 30 text) |
| `objects/<page>.drawings` | ink (section 2) |
| `objects/<page>.shapes` | shapes (section 3) |
| `objects/<page>.texts` | text boxes (section 4) |
| `objects/<page>.images` | images: `key`, `frame`, `cropBox`, `rotate` [inferred] |
| `objects/<page>.maskings`, `.hyperlinks` | masking objects, links (not converted) |
| `*_back`, `thumbnail*`, `template.info`, `outlines`, `.itemInfo` | backups, previews, template list, outline, item metadata (u64 length + raw DEFLATE JSON; may occur twice) |

## 2. Ink (`.drawings`) [verified]

```
{"start": {"x": 0.4628, "y": 0.4469}, "points": "<base64>", "strokeColor": 4284626687,
 "mode": 5, "figure": 0, "dashtype": 0, "fillColor": 0, "scale": {"x": 1, "y": 1},
 "rotate": 0, "type": 1, "key": "<UUID>"}
points = base64( u32 n, n x (f32 x, f32 y, f32 w) )
```

* Point `i` is at `((start.x + x_i) * W, (start.y + y_i) * W)` with `W` the page frame width:
  **both axes are normalised by the page width**, and the offsets are absolute offsets from
  `start`, not deltas. On `forms.flx` every stroke's offsets have a minimum of exactly 0 on
  both axes (`start` is the stroke's bounding-box corner), handwriting keeps its proportions
  and a drawn circle stays round; normalising y by the page height (one viewer's reading)
  stretches the writing [verified].
* The third float is the width at the point, also normalised by `W` (`0.0029 x 768` = 2.2 pt;
  it tapers along handwriting) [inferred from the sample; the codex plugin leaves it open].
* `strokeColor` is ARGB in a 32-bit integer (JSON may hold it signed). `mode 2` is the
  highlighter (codex plugin and research note); `mode 5` is ordinary handwriting and `mode 1`
  with `figure 1` a straight line drawn with the line tool on the sample.
* One viewer accepts an 8-byte header before the triples in older files; gnnote does the same
  when the count does not match the length [inferred].

## 3. Shapes (`.shapes`) [inferred from flexcil-backup-viewer, checked by rendering the sample]

`points` hold two triples (offsets from `start`, width-normalised; the third value is the
line width), so the shape is defined by its two corner points:

| `shapeType` | Drawn by gnnote as |
|---|---|
| 1 | ellipse inscribed in the box of the two points (64 samples) |
| 3 | rectangle of the box (`sides 4`) |
| 4 | regular polygon with `sides` corners inscribed in the box, a corner on top (5 if missing) |
| 5 | straight line between the points (`sides 1`) |
| 6 | arc: one quadratic Bezier through the points with `controlPoints[0]` (absolute, width-normalised) as control |
| 7 | arrow: a line to the last point plus a 30-degree head (one stroke) |
| 9 | closed polygon through the points |
| other | an open polyline through the points, with a warning |

Straight-sided shapes carry exact cubic handles at the segment thirds (as GoodNotes
auto-shapes do), so writers keep their corners sharp. Closed shapes with a visible
`fillColor` (ARGB alpha > 0) also get a `Stroke(kind="fill")` right after their outline.
On `forms.flx` this reading gives a circle, a square, a pentagon, an arrow, a line and two
arcs on a grid — a plausible "shapes" test page; the arc's control point semantics and the
arrow head size are guesses.

## 4. Text boxes (`.texts`) [verified by the codex plugin's Android round trips]

`{"type": 30, "frame": {x, y, width, height}, "text": "...", "rotate": 0, "columns":
[{"p": {"span": [{"style": {"font-family": "Helvetica, Helvetica-Light", "font-size": 0.023},
"text": "..."}], ...}}]}`. Unlike ink, the frame is normalised by the page **width
horizontally and the page height vertically**; the font size by the page width. gnnote keeps
the spans as runs (font family, size, bold/italic from the family name or `font-weight` /
`font-style`, colour when it is an ARGB integer) when they spell the `text`, else one run.

## 5. Images (`.images`) [unknown: no sample]

`{"key": ..., "frame": {...}, "cropBox": {x, y, width, height}, "rotate": ...}` with the
bytes in `attachment/image/<key>` (two viewers agree). gnnote reads the frame like a text
frame (x by `W`, y by `H`) unless only the width-normalised reading matches the picture's
pixel aspect ratio (crop included); `rotate` is read as radians (a viewer passes it to a
canvas `rotate`). Every converted image adds a warning that its placement is unverified.

## 6. Backups (`.flex`) [inferred from the viewers; no public sample]

A ZIP of `.flx` documents (members ending in `.flx`; documents stored as folders with their
own `pages.index` are accepted too), `documents.list` and `.trash.list` (u64 uncompressed
length + raw DEFLATE JSON: the library tree, folders with `children`, documents with
`document` = UUID or `UUID.flx`, `state: "removed"` in the trash) and `.fab` recordings.

gnnote converts **one document**: the first of the library tree (depth first, trash
skipped), then documents the tree does not mention, in archive order. The warning names the
others. `read_flexcil(data, document=...)` picks another one by position, title or UUID, and
`list_flexcil_documents(data)` lists them; the CLI, the web page and the server always take
the first.

## 7. Mapping summary and warnings

| Model | From |
|---|---|
| `Document.title` | the library tree's name, else `info.name` |
| `Page.width/height` | `frame.width/height` (pt); the PDF page size when the frame is unusable, else 768 x 1024 |
| `Page.background` | `attachmentPage` → `PdfBackground(file, index)` with the PDF in `Document.pdfs`; `template_is_builtin` is always false (templates are carried as PDFs) |
| strokes | drawings and shapes in `.objects` order, then any not listed |
| `Stroke.width` | median point width |

Warnings: shapes converted to strokes (count), unknown shape types, unreadable points,
rotated or scaled objects drawn untransformed (`scale`/`rotate` not identity), dashed lines
drawn solid, masking objects and links dropped, page rotation ignored, a frame that differs
from its PDF page (ink may be offset), rotated text boxes placed unrotated, image crops not
applied, image placement unverified, the other documents of a backup, unreadable
documents, size-guard skips, and the point budget.

## 8. Limits and hardening

ZIP members above 256 MB (declared) or 1 GB in total (nested documents share the budget) are
skipped before inflating; `documents.list` may declare at most 64 MB; at most 10 000 pages,
200 000 objects per layer, 1 000 000 points per stroke, 10 000 000 points per document,
100 000 library nodes nested at most 100 deep; normalised coordinates beyond 100 page widths
are dropped. `read_flexcil` raises `ValueError` only for data that is not a Flexcil file (no
ZIP, neither `pages.index` nor `.flx` documents), for a backup none of whose documents opens,
and for a `document=` that names no document.

## 9. Samples, tests and open questions

* `flexcil-backup-viewer/forms.flx` (MIT): one page, 13 ink strokes, 7 shapes, a grid PDF.
  `tests/test_flexcil.py` pins every stroke's point count, bounding box and colour, and
  compares page frames, backgrounds, object counts and every ink point with
  flexcil-codex-plugin's codec run in its own process.
* Synthetic documents and backups built in the tests cover highlighters, colours, z-order,
  every shape type, fills, text runs, images, PDF problems, transforms, backups with a library
  tree, trash, folder documents and broken members.
* Wanted samples: a real `.flex` backup with two or three documents in folders (to confirm
  member names, `documents.list` and the trash); a page with a highlighter, an inserted photo
  (portrait, cropped and rotated), a text box and a masking object; a document imported from
  a PDF and one created from a built-in template (to tell user PDFs from stock paper); strokes
  that were moved, scaled and rotated with the lasso.
