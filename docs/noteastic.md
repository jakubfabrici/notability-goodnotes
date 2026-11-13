# Noteastic (Windows) `.ntcbak` — experimental reader

Noteastic is a pen note app for Windows 10/11 by Noteastic OG, Vienna (Microsoft Store
9PP6QXXZS9D1). It keeps every notebook in one private database and has no file per notebook;
the only native file a user can make is a **backup of the whole library** (`.ntcbak`).
gnnote reads such a backup and converts it with the handwriting kept as editable ink
(`gnnote/noteastic/`, `read_ntcbak(data: bytes) -> Document`, registry id `noteastic`).
There is no writer (section 8).

**Status: experimental.** No backup made by the app has been available to gnnote. The reader
follows the one public description of the format (section 4) and every converted document
carries this warning:

> Noteastic support is experimental: it follows a public description of the backup format
> and has not yet been checked against a backup made by the app. Compare the result with
> your notebook.

Confidence markers: **[documented]** stated or implemented by the MIT reference project
(section 4); **[inferred]** consistent evidence, not checked on a real backup; **[assumed]** a
guess gnnote makes until a sample shows otherwise; **[unknown]**.

## 1. Making a backup and converting it

1. In Noteastic open **Settings > Backup** and create a backup (available since version 1.2,
   August 2025). Save the `.ntcbak` file somewhere you can reach.
2. Open the gnnote web page (or run `python3 -m gnnote convert Library.ntcbak --to goodnotes`),
   choose the file and a target app. The whole library becomes **one document**: the
   notebooks follow each other in library order and a warning lists which pages belong to
   which notebook. With a single notebook the document is named after it, otherwise
   "Noteastic library".
3. Compare the result with Noteastic and report differences (section 9 lists the files that
   would help most).

A bare `noteastic.db` (the database on its own) converts too, but without the imported PDFs
and inserted pictures, which live next to it in the backup; a warning says so.

### Privacy

A `.ntcbak` holds **every notebook of the library and every imported PDF**, not just the one
you want to convert. gnnote's web page converts in the browser, so the file stays on your
device. That is not the case when the page is served by `gnnote.server`: the page then sends
the file to that server for conversion. Use the plain web page (or the command line) for a
backup with private notes, or make the backup on a separate Windows user account that holds
only the notebooks you want to convert (each account has its own Noteastic library).

## 2. What is converted

| Noteastic content | Result | Notes |
|---|---|---|
| Notebooks, folders | One document, notebooks in library order | Folders depth-first by name; inside a folder its notebooks (by name) come before its sub-folders; notebooks outside any folder last. Pages by their index in the notebook. Notebooks and pages marked as deleted are skipped; the notebooks of a folder marked as deleted are listed outside any folder. |
| Page size | Page width / height × 0.75 pt **[assumed]** | Noteastic is a UWP app, whose coordinates are effective pixels (1/96 inch); one unit is taken to be 0.75 pt. This is the one unit constant of the reader (`UNIT_PT`). |
| Paper (`Grid.GridType`) | Paper hint | Names containing *line* / *ruled* / *cornell* → lined, *check* / *grid* / *square* → squared, *bullet* / *dot* → dotted, blank → plain; unknown names → plain with a warning. The target app draws its own spacing. Page background colours are not converted (warning). |
| Imported PDF pages (`PDFGrid`) | PDF-backed pages | The page shows its PDF page's visible area (the CropBox), fitted and centred into the Noteastic page as the reference renderer (pdf.js) and Windows' PDF renderer draw it; the converted page takes the PDF page's own size (its MediaBox, as gnnote's writers draw PDF pages) and the ink is mapped onto the visible area inside it, so this needs no unit assumption. A cropped page therefore shows the parts the crop hides too (warning). Ink beside a letterboxed PDF page may fall outside the page (warning). Each PDF is stored and parsed once, however its path is spelled. |
| Pen strokes (`StrokeElement`) | Editable ink | Colour `#AARRGGBB` with its transparency, width from `StrokeWidth` / `StrokeHeight` (the larger), pressure as per-point widths unless *IgnorePressure* is set (section 5). Damaged single points are left out (warning). |
| Highlighter | Highlighter strokes | From `IsHighlighter` or the element type (`Highlighter`, `LineHighlighter`). |
| Lines (`LineElement`) | A straight stroke; arrowheads as filled triangles with an outline | `SingleArrow` / `DoubleArrow`. Dashed lines are drawn solid (warning). |
| Shapes (`ShapeElement`) | A closed stroke, plus a filled shape under it when the fill colour is not transparent | Rectangle, ellipse, triangle, pentagon, hexagon, octagon; the quadrant shapes get their centre lines. Unknown shape names become rectangles (warning). Notability drops fills. |
| Pictures (`ImageElement`) | Images (PNG, JPEG) | Position, size and rotation from the element's matrix; skew and mirroring are dropped (warning): a mirrored picture stays upright over the area Noteastic shows it in. Other picture formats are skipped (warning). Every placement of a picture is written out again by the target formats, so a document takes at most the backup's size plus 64 MB of pictures (512 MB at most); further placements are skipped (warning). |
| Text boxes (`TextElement`) | Text boxes | Text, font, size, colour, alignment, rotation; one style per box. Rich-text runs (`Spans`) are ignored (warning). Sizes and line breaks are approximate, because Noteastic lays text out itself. A mirrored box stays upright (readable) over the area Noteastic shows it in. |
| Erased elements | Skipped | Elements with `IsDeleted = 1`. |
| Anything else | Skipped with a warning naming the element type | E.g. a protractor or a type gnnote does not know. |

Every element's points go through its 2-D matrix (section 4.3), so moved, scaled and rotated
items land where Noteastic shows them; line widths scale with the matrix **[assumed]**.

## 3. Bridging through PDF (no backup needed)

Noteastic imports and exports PDF, so PDF is the other way between Noteastic and the iPad
apps. Neither direction keeps the handwriting editable at both ends today.

* **gnnote → Noteastic.** Convert the GoodNotes or Notability notebook to **PDF** with the
  option **"As editable annotations"** (`--pdf-ink annotations` on the command line). Drag the
  PDF into a Noteastic library folder and import all pages. The handwriting is visible and you
  can write on top of it, but it is most probably **not editable** in Noteastic: Noteastic
  shows markup made by other PDF apps as part of the page (its version 1.10 notes say such
  markup is now included in its own PDF exports). Annotations are still the better choice than
  flattened ink: if Noteastic keeps them as annotations in its exports, gnnote can read them
  back as editable ink later **[unknown]**.
* **Noteastic → gnnote.** In the Noteastic library right-click the notebook and choose
  **Export** (whole notebook; choose the variant without the grid if the target app should
  supply its own paper). Convert the PDF with gnnote. The pages arrive, typed text is real
  PDF text, but Noteastic's handwriting most probably arrives **as part of the page**:
  visible, not editable. gnnote turns PDF ink *annotations* into strokes, not drawing
  operations in the page content, and how Noteastic writes its ink into a PDF is
  **[unknown]** (drawing operations are the more likely). The `.ntcbak` route keeps the ink
  editable.

Noteastic's developers build features from user requests (Settings > Feedback in the app,
the r/Noteastic community, or the contact address on noteastic.app). Asking them to **import
PDF ink annotations as editable strokes** would make gnnote's existing PDF output an editable
route into Noteastic; asking them to **export ink as PDF ink annotations** would do the same
in the other direction.

## 4. Format facts and where they come from

The only public technical description of the backup is the source code of
**noteastic-mcp-public** (https://github.com/Canxinner/noteastic-mcp-public, commit
`0182e7886f73a119473f1ffd23e2327ba8c24ad7`, MIT License, Copyright (c) 2026 Noteastic MCP
contributors), an unofficial read-only tool that lists and renders backups. Its
`src/backup.js` (the SQL queries), `src/export.js` (how it draws pages, strokes, lines, shapes,
pictures and text) and `test/backup.test.js` (its synthetic fixture database) were read for
facts. gnnote's reader and SQLite helper are new Python code; no code was copied. The test
builder (`tests/ntcbak_builder.py`) creates the same tables and columns as that fixture (the
list in section 4.2, a fact gnnote needs to test against), written out by gnnote with its own
type declarations. No Noteastic program code was looked at, and no Noteastic file has been
inspected.

### 4.1 Container [documented]

A ZIP archive with the `.ntcbak` extension. The member `noteastic.db` is a complete SQLite 3
database holding the library. The imported PDFs and inserted pictures are further members,
named by the `FilePath` columns of the database; those paths may use Windows separators
(`PDFs\analysis.pdf`), so `\` is read as `/`. gnnote also finds the database one folder down
and in any letter case, and looks a stored path up relative to the database's folder, then
the archive root, then by its longest trailing part (for absolute Windows paths) **[assumed]**.
Whether real backups carry other members (settings, thumbnails, a manifest) is **[unknown]**;
gnnote ignores them, and reports a non-empty `noteastic.db-wal` member as unread changes.

### 4.2 Tables and columns [documented]

```
Folder(ID, Name, Color, CreationDate)          FolderFolderContent(FolderID, ChildFolderID)
Book(ID, Name, TagColor, CreationDate, LastEdited)       FolderBookContent(FolderID, ChildBookID)
BookContent(BookID, PageIndex, PageID)          page order, PageIndex counts from 0
Page(ID, GridID, Width, Height, BackgroundColor, CreationDate)
Grid(ID, GridType, IsDeleted)                   GridType 'PDF' for imported PDF pages
PDFGrid(GridID, FilePath, PageIndex, Password)  page PageIndex (from 0) of the PDF FilePath
PageContent(PageID, ElementID)
Element(ID, ElementType, CreationDate, M11, M12, M21, M22, M31, M32, IsDeleted)
StrokeElement(ElementID, StrokeWidth, StrokeHeight, Color, IsHighlighter, IgnorePressure, PointsData)
LineElement(ElementID, LineType, X0, Y0, X1, Y1, StrokeColor, StrokeWidth, StrokeDashed, IsHighlighter)
ShapeElement(ElementID, ShapeType, X0, Y0, X1, Y1, StrokeColor, StrokeWidth, StrokeDashed, FillColor)
ImageElement(ElementID, ImageWidth, ImageHeight, FilePath)
TextElement(ElementID, Text, Width, FontFamily, FontSize, Color, HorizontalTextAlignment, Spans)
```

These are the columns the reference project queries and its fixture creates; the real
database almost certainly has more. Element types seen there: `InkStroke`, `Highlighter`,
`Line`, `LineHighlighter`, `Shape`, `Image`, `Text`. Line types: `SingleArrow`,
`DoubleArrow` (anything else is a plain line). Shape types: `Ellipse`, `Triangle`,
`OneQuadrant`, `FourQuadrant`, everything else drawn as a rectangle. Elements are erased by
setting `IsDeleted`, and drawn in `CreationDate`, then `ID` order.

gnnote reads only these named columns. A missing table or column is reported in one warning
and read as empty or as a default; only `Page`, `BookContent` (with `BookID` and `PageID`)
and, for content, the element tables' `ElementID` are required. Without a usable `Book` table
every `BookID` of `BookContent` becomes a notebook named "Notebook <id>". IDs may be numbers
or text (sqlite-net stores GUID keys as text); text IDs match case-insensitively. Optional
`IsDeleted` columns on `Folder`, `Book` and `Page` are honoured when present **[assumed]**.

### 4.3 Geometry [documented]

* Points: `x' = M11·x + M21·y + M31`, `y' = M12·x + M22·y + M32` (the row-vector form of a
  .NET `Matrix3x2`, applied by the reference renderer as a canvas transform). Missing matrix
  values read as the identity; unreadable or absurd ones (|M| above 10 000, offsets above
  10^6 units) drop the element with a warning.
* Pictures are drawn at `(0, 0)` with size `ImageWidth × ImageHeight` in element space; text
  boxes start at `(0, 0)` with their `Width`. gnnote maps the centre (pictures) or the
  top-left corner (text) through the matrix and takes the rotation from `atan2(M12, M11)`.
  A mirrored matrix (negative determinant) is a turn plus a flip; gnnote's model has no flip,
  so it keeps the turn nearer upright (that of the x axis or of the y axis) and places the box
  over the area Noteastic shows: pictures by their centre, text boxes by the centre of their
  estimated area. A left-right mirrored text box thus stays readable where Noteastic shows it.
* Page, stroke and shape coordinates share one unit. For a PDF page the reference renderer
  scales the PDF page by `min(Width / pdf_width, Height / pdf_height)` and centres it, where
  the PDF page size is that of pdf.js's viewport: the CropBox (clipped to the MediaBox, turned
  by `/Rotate`). Windows' own PDF renderer (`Windows.Data.Pdf`) sizes pages by the CropBox
  too. gnnote fits the CropBox the same way and maps the ink onto the MediaBox-sized page its
  writers draw.

### 4.4 Stroke encoding [documented, with gaps]

* `PointsData` is text: `"<n>;x,y,p;x,y,p;..."`. The reference reader always skips the first
  `;`-separated part, reads the first three comma-separated values of every other part as
  `x,y,pressure` (further values are ignored) and drops a point whose `x` or `y` is not a
  number; in its fixture the first part is the point count. gnnote does the same, with two
  differences: it keeps the first part as a point when it looks exactly like the points after
  it (a stroke saved without a header), and a stroke where most points cannot be read is
  skipped as damaged. A first part without a comma (a count, a version) or with a different
  number of values (`1,0` before `x,y,p` points) is a header. Points without pressure, empty
  parts and a trailing `;` are accepted; extra values and left-out points are reported.
* Colours are `#AARRGGBB` (Windows' colour notation). gnnote also accepts `#RRGGBB`, an ARGB
  integer and `transparent`; anything else is black (or no fill) with a warning.
* `StrokeWidth` / `StrokeHeight` look like the size of a Windows Ink pen tip, and
  `IsHighlighter` / `IgnorePressure` like the matching Windows Ink drawing attributes
  **[inferred]**.

## 5. Unknowns and gnnote's assumptions

| Question | gnnote's assumption | Settled by |
|---|---|---|
| Unit of `Width`, `Height` and coordinates | Effective pixels: 1 unit = 0.75 pt (`UNIT_PT`), the coordinate unit of UWP apps. The reference project reads it the other way: its PDF export makes `Width × Height` pt pages (1 unit = 1 pt), and its fixture pairs a 100 × 200 unit page with a 100 × 200 pt PDF page. Neither was checked against the app, so a tester with a real backup should check both readings | An A4 blank page and a PDF page of known size in a real backup |
| First part of `PointsData`: point count or format version? | Skipped either way when it holds no comma or differs from the points after it | A real backup |
| More values per point than `x,y,pressure` (tilt, a timestamp)? | Ignored, as the reference reader does; a warning counts such strokes | A real backup |
| Number format on a German or Slovak Windows (decimal comma)? | Numbers are expected with a dot. If a stroke has no dot at all and points of 4 to 6 comma-separated tokens, gnnote guesses where each number ends (pressure must be 0..1; ambiguous points take the reading nearest their neighbour) and warns; points it cannot decode are left out. The guessing is slow, so a document decodes at most 1 000 000 such points (further strokes are skipped with a warning) | A backup made with a decimal-comma locale |
| Pressure → width | Width × (1.5 p + 0.25) for pressure p in 0..1 (the rule of WPF's ink renderer, which gnnote's OneNote reader uses too); p = 0.5 gives the nominal width | Noteastic's PDF export of the same strokes |
| Tip width vs height | The larger of the two, drawn round | A highlighter stroke in a real backup |
| Do lasso moves and resizes only change the matrix? | Yes; widths scale with the square root of the matrix's area scale | A moved and a scaled stroke |
| Date format of `CreationDate` | Sorted as stored (numbers before text) | A real backup |
| Names of pentagon / hexagon / octagon, protractor arcs, Cornell and separator grids | `Pentagon`, `Hexagon`, `Octagon`; Cornell as lined; anything unknown is warned | A real backup |
| `Spans` encoding (rich text) | Ignored; a non-empty value gives a warning | A text box with mixed styles |
| `HorizontalTextAlignment` values | Names (`Left`, `Center`, `Right`, `Justify`) or Windows' numbering (0 centre, 1 left, 2 right) | A right-aligned text box |
| Grid spacing and colour, page background colours | Not converted | A real backup |
| Password column of `PDFGrid` | Never shown or copied; a warning says the target app may ask for the PDF's password | A password-protected PDF import |
| Restore semantics (replace or merge the library) | Not needed for reading | A restore on a test account |

## 6. Limits and hardening

The backup is untrusted input. gnnote reads it without temporary files, from memory, and
within fixed budgets; anything beyond them is skipped with a warning, and only "this is not a
Noteastic backup", "the database cannot be opened" and "its `Page` or `BookContent` table
cannot be read" raise `ValueError`.

* ZIP members through `readutil.ZipBundle` (256 MB per member as declared, 1 GB per backup,
  checked before inflating).
* The database through `gnnote/sqliteutil.py`, a generic helper for SQLite bytes:
  `sqlite3.connect(":memory:")` plus `Connection.deserialize` (a clear error where the Python
  build lacks either); at most 256 MB; `PRAGMA query_only=ON`, `trusted_schema=OFF`,
  `ignore_check_constraints=ON`, `cell_size_check=ON` and, on Python 3.12 and newer, SQLite's
  defensive mode; a progress handler that stops a database once it has used 64
  virtual-machine steps per byte of the database plus 2 × 10^7 (at most 2 × 10^9; reading a
  sound database takes about 4 per byte), so the work SQLite does stays proportional to the
  input. Pages that hold nothing but zeros do not count towards that size: they give SQLite
  no steps to take, and a ZIP archive stores thousands of them in a few bytes, so a small
  backup cannot buy itself the full budget by padding its database with them.
* Before SQLite sees the bytes, gnnote walks the schema b-tree (`sqlite_master`, page 1 and
  the pages and overflow chains it leads to) itself, following SQLite's published file
  format. SQLite reads every schema row before the first statement runs, and a crafted schema
  whose pages are shared would make it read the same rows millions of times; a schema that
  reaches a page twice, or spans more than 8 MB of pages or 100 000 entries, is refused at once.
  The walk counts and reads pages as SQLite does: a file that ends inside a page still has
  that page, read with zeros for the missing bytes.
* Tables are found only from `sqlite_master` rows of type `table` whose SQL creates an
  ordinary table and whose root page is a page number. SQLite itself refuses to load a schema
  whose row names a table other than the one its SQL creates (checked with SQLite 3.39 and
  3.45), and no two schema objects share a name, so a view, a virtual table or a lying schema
  row never stands in for a table. Views are never prepared (nor is `PRAGMA table_list` used,
  which prepares every view): SQLite expands a view while preparing it, and a chain of views
  that each name the one before twice takes seconds per chain.
* Before a table is first read, SQLite's `quick_check` of that table checks its b-trees. A
  crafted file can make interior pages share a child, or rows share an overflow chain, so
  that a scan returns the same rows again and again from a few kilobytes of file; such a
  table is not read (a warning, or an error for `Page` and `BookContent`). Only the check's
  first result row is fetched (`SELECT * FROM pragma_quick_check(?) LIMIT 1`): the b-tree
  walk reports its findings first, and fetching further would run the check's row-by-row pass
  over the damaged tree. A table with a NOT NULL virtual computed column is not read either,
  because the check would compute that column for every row and an unchecked table could
  repeat its rows; none of the documented Noteastic tables has a computed column, and an
  app's database library would hardly declare one.
* Named columns are checked with `pragma_table_info` (generated columns do not count);
  statements are plain table scans (`NOT INDEXED`, no joins, no sorting in SQL) with a row
  limit per query and a size cap in bytes per text or blob value (values above it are not
  handed to Python; bytes, because SQLite's `length()` of text stops at a NUL character).
  All reads together may load at most twice the database's size plus 16 MB of text and
  blobs, counted in bytes whether or not a value was over its cap (SQLite loads a value to
  measure it), so damage the check misses can neither fill memory nor keep SQLite copying
  for long. A database saved in write-ahead-log mode opens (the header flag is cleared on the
  copy). Text that is not UTF-8 is decoded with replacement characters. A database that fails
  to open is closed at once. The helper and the reader were run in the browser runtime gnnote
  uses (Pyodide 314.0.7: Python 3.14.2 with SQLite 3.39.0, which has `deserialize`, defensive
  mode and `pragma_quick_check` of one table) on the demo backup and on hostile databases.
* Rows: 100 000 folders or notebooks, 200 000 page links and pages, 1 000 000 elements per
  table. Values: 64 KB by default, `PointsData` up to 64 MB with at most 1 000 000 points and
  256 characters per point, text boxes up to 1 000 000 characters, names 1 000 characters.
* PDFs: each archive member is parsed once, however many pages name it and however its path
  is spelled. Pictures: at most the backup's size plus 64 MB (and 512 MB) of picture bytes per
  document, counted per placement.
* Documents: at most 10 000 pages and 10 000 000 decoded ink points (a stroke the remaining
  budget cannot hold is skipped before it is decoded; the points of a stroke count whether or
  not they could be read), 1 000 000 of them decoded by the decimal-comma guess (whether or
  not the guess succeeds); folder trees deeper than 64 and folder cycles are cut.
* Coordinates beyond 10^6 units, widths outside 0.1 to 1000 pt and page sides outside 1 to
  100 000 units (replaced by A4) are treated as damage.

## 7. Why the sniffer looks inside

The content decides the format: a ZIP with a member `noteastic.db` (any case, at the top or
one folder down), or a bare SQLite file whose schema names `StrokeElement` together with
`PageContent` or `BookContent`. The sniffer reads the schema's pages without opening the
database (`sqliteutil.schema_area`: page 1 and the pages its b-tree points to), because a
schema that outgrows page 1 moves to new pages anywhere in the file, for example after an app
update adds tables to a grown library. Other SQLite databases (for example Microsoft Journal's,
with its `pages` and `blobs` tables) are not claimed. `tests/test_noteastic.py` checks that
this sniffer claims none of the sample files of the other formats and that no other sniffer
claims a Noteastic backup.

## 8. Why there is no writer

Noteastic takes notebooks in only through PDF import and through *restore backup*, which most
probably **replaces the whole library**. A gnnote-made backup would need the complete real
schema (every table, column and version marker) and a restore test, and could still wipe a
user's notes after an app update. Until the developers document an import, the PDF route of
section 3 is the way into Noteastic.

## 9. Tests and the files that would close the gaps

`tests/ntcbak_builder.py` builds synthetic backups with the standard library's `sqlite3` from
the schema of section 4.2; `python3 tests/ntcbak_builder.py OUT.ntcbak` writes a demo backup
(pen strokes with pressure, a highlighter, arrows, a filled shape, a text box, a PNG picture
and a PDF-backed page). `tests/test_noteastic.py` covers the mapping, deleted rows, matrices,
backslash paths, cropped and turned PDF pages, several notebooks, missing tables and columns,
malformed `PointsData`, the budgets, hostile databases (views and lying schema rows posing as
tables, chains of views, triggers, b-trees that share pages, huge values, corrupt and
non-SQLite files), the SQLite helper and the sniffer.

One real backup would settle most of section 5. Made on a **separate Windows user account
with test notes only** (the backup holds the whole library):

1. Settings > About: the app version and its list of third-party components; the Windows
   display language and region.
2. A notebook on A4 paper with: pen strokes in three colours and widths, written with varying
   pressure; one stroke with *Ignore pressure*; a highlighter stroke; plain, arrow, double
   arrow, dashed and highlighter lines; one of each shape, one with a fill; a PNG and a JPEG
   picture; a text box with two sizes, a bold word and right alignment; lined, squared and
   dotted pages; a stroke moved, one scaled and one rotated with the lasso; a stroke drawn and
   erased.
3. A second notebook made by importing a two-page PDF of known page size, with one stroke on
   each page.
4. The backup (`Settings > Backup`), a second backup after adding one stroke, and the PDF
   export of both notebooks with and without the grid.
