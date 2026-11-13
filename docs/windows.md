# Windows note apps and gnnote

This page is for people who write notes on an iPad (GoodNotes, Notability, Noteful and the
other apps in the README) and also use a Windows PC with a pen. For each Windows app it says
how notes get from the iPad to the app and back through gnnote, and whether the handwriting
stays **editable** on the way: arriving as the app's own ink that can be selected, moved,
recoloured and erased, rather than as part of a page picture.

State: November 2026. **Nothing on this page has been checked on a Windows PC with gnnote's
files yet.** The routes come from the apps' own help pages, from the file formats gnnote reads
and writes, and from gnnote's tests. Every route says how sure it is; "not yet checked on
Windows" means exactly that. The [test kit](#how-to-test-on-windows) is there to close these
gaps, and [Sample files we would like](#sample-files-we-would-like) lists the files that would
help most.

## At a glance

"Editable" means the strokes arrive as ink of the target app. "Visible only" means they are
drawn into the page: you see them and can write on top, but cannot change them.

| Windows app | iPad → Windows app | Windows app → iPad | Checked on Windows? |
|---|---|---|---|
| [Goodnotes for Windows](#goodnotes-for-windows) | `.goodnotes` import: editable (GoodNotes' own export, or gnnote's from Notability and others) | PDF export "Editable" → gnnote: editable, one width per stroke | not yet |
| [Noteastic](#noteastic) | PDF with ink annotations: visible, probably not editable | `.ntcbak` backup → gnnote (experimental): editable. PDF export → gnnote: probably visible only | not yet |
| [Microsoft OneNote](#microsoft-onenote) | not possible (gnnote does not write OneNote) | `.one`, `.onepkg` or OneDrive `.zip` → gnnote: editable | not yet (the reader is tested on sample files) |
| [Microsoft Journal](#microsoft-journal) | PDF: probably visible only | *Export to OneNote*, then `.one` → gnnote: probably editable | not yet |
| [PDF apps](#pdf-apps-drawboard-xodo-acrobat-edge-and-others): Drawboard PDF, Xodo, Adobe Acrobat, Edge, Foxit, PDF-XChange, Firefox | PDF with ink annotations: editable | Save the PDF (not flattened) → gnnote: editable, one width per stroke | not yet |
| [LiquidText](#liquidtext-chrome-and-other-pdf-tools) | PDF: unknown | PDF export: unknown | not yet |
| [Xournal++, Rnote, Saber, Excalidraw](#xournal-rnote-saber-and-excalidraw) | `.xopp`, `.sba`, `.excalidraw`: editable, pressure kept | the same files: editable, pressure kept | not yet |
| [Stylus Labs Write](#stylus-labs-write) | `.svgz`: editable ink expected | `.svgz` / `.svg` → gnnote: editable | not yet |
| [Samsung Notes](#samsung-notes) | not possible | `.sdocx` → gnnote: editable | not yet |
| [Nebo](#nebo-myscript) | not possible | `.nebo` → gnnote: editable | not yet |
| [Whiteboard, Paint and others](#apps-gnnote-cannot-help-with) | no | no | |

The iPad side is always the same: open gnnote's web page (or the command line), choose the
file the Windows app produced, choose the iPad app as the target and share the result into
that app. In the other direction, convert the iPad app's file (`.goodnotes`, `.note`,
`.noteful`, ...) to the format in the second column.

## Running gnnote on Windows

* **Web page:** open it in Edge, Chrome or Firefox and use it as on the iPad. The conversion
  runs inside the browser and the file does not leave the PC. The one exception is a page
  served by `gnnote.server`: there the file is sent to that server for conversion.
* **Command line:** gnnote is plain Python (3.11 or newer, from python.org or the Microsoft
  Store) with no other dependencies. In a command prompt inside the gnnote folder:

  ```bat
  py -m gnnote convert "%USERPROFILE%\Downloads\Notebook.goodnotes" --to pdf --pdf-ink annotations
  py -m gnnote convert "%USERPROFILE%\Downloads\Notebook.pdf" --to notability
  ```

  The command line has not been run on Windows by the maintainers yet.
* Moving files between the iPad and the PC: any cloud drive both can reach (iCloud for
  Windows, OneDrive, Google Drive), e-mail, or a USB stick through the iPad's Files app.

## Three kinds of route

1. **Native files** (Xournal++, Saber, Excalidraw, Stylus Labs Write, GoodNotes import,
   OneNote, Samsung Notes, Nebo, Noteastic backups): gnnote reads, and for some apps writes,
   the app's own file. Ink stays ink, usually with per-point pressure.
2. **PDF ink annotations**: most Windows PDF apps store pen strokes as *ink annotations*, a
   standard PDF object that sits on top of the page. gnnote turns them into editable strokes,
   and with the option **"As editable annotations"** (command line `--pdf-ink annotations`) it
   writes every stroke as one. Two limits:
   * An ink annotation stores the stroke's path and **one width**. Pen pressure therefore
     does not survive the way back: gnnote reads every stroke with one (typical) width.
     gnnote's own annotations also carry a drawing of the stroke with its pressure, so apps
     that show that drawing display them like the original; an app that edits a stroke may
     redraw it at one width.
   * Ink a PDF app **draws into the page** (the "flatten" choice, or apps that never use
     annotations) is part of the page picture. gnnote keeps it as the page background:
     visible, not editable (`docs/pdf.md` section 3.1).
3. **PDF without annotations**: gnnote's default PDF output ("Draw into the page",
   `--pdf-ink flatten`) looks the same everywhere, but the strokes are background in every app.
   Use it only to read or print.

## Goodnotes for Windows

Microsoft Store 9N92MC09DB30. Not the same program as the iPad app: it has its own code base.

* **Goodnotes Cloud:** with a **Goodnotes Pro** subscription the same library syncs between
  the iPad and Windows, and no conversion is needed at all (vendor help page). The other plans
  do not include this sync.
* **iPad → Windows:** the Windows app imports PDF, images and `.goodnotes` documents
  (Study Sets excepted) through **Import** in its library (vendor help page, May 2026).
  * From GoodNotes on the iPad: export the notebook as a GoodNotes document and import it;
    gnnote is not needed.
  * From Notability, Noteful or another app: convert the note with gnnote to **GoodNotes**,
    then import the `.goodnotes` file.
  * Size limits: 200 MB per `.goodnotes` file for subscribers; the free plan imports at most
    **5 MB per file** and keeps at most 3 notebooks. A notebook with an imported PDF or
    photos quickly exceeds 5 MB.
  * **Not yet checked on Windows:** no `.goodnotes` file written by gnnote has been opened in
    the Windows app. A user reported in 2023 that `.goodnotes` files from the iPad were
    refused there; whether that still happens is unknown.
* **Windows → iPad:** Goodnotes for Windows **cannot export `.goodnotes`** (vendor FAQ,
  updated September 2026). Use **Export > PDF** with the PDF data format **Editable**
  (not *Flattened*), then convert the PDF with gnnote to GoodNotes or Notability.
  * Goodnotes says handwriting and highlights of an Editable PDF can be selected and moved in
    other PDF apps, which points to ink annotations. That gnnote turns them into editable ink
    is likely but **not yet checked on Windows**. GoodNotes on the iPad stores its editable PDF
    ink as annotations that gnnote reads, including an estimate of the drawn width.
  * Pressure is reduced to one width per stroke (see
    [Three kinds of route](#three-kinds-of-route)).
  * A *Flattened* export gives pages with the ink as background only.

## Noteastic

Microsoft Store 9PP6QXXZS9D1 (Noteastic OG, Vienna; sometimes written "Notastic"). A Windows
only pen app. It keeps every notebook in one private database; there is no file for a single
notebook. It can export **PDF** (a page, a selection or a whole notebook, with or without the
paper pattern) and make a **backup of the whole library** (`.ntcbak`, *Settings > Backup*).

* **iPad → Noteastic (PDF):** convert the notebook with gnnote to **PDF** with **"As editable
  annotations"** (`--pdf-ink annotations`). Drag the PDF into a library folder in Noteastic and
  import all pages.
  * You see every page and all the handwriting and can write on top.
  * The handwriting from the iPad is most likely **not editable** in Noteastic: Noteastic shows
    markup made in other apps as part of the imported PDF (its version 1.10 changelog). How
    sure: medium; **not yet checked on Windows.**
  * Prefer annotations to the flattened PDF anyway: since version 1.10 Noteastic includes
    markup from other apps when it exports a PDF, so the strokes may come back to gnnote as
    editable annotations later (unverified).
* **Noteastic → iPad, editable (experimental):** create a backup in **Settings > Backup**
  and convert the `.ntcbak` file with gnnote. The whole library becomes one document, the
  notebooks one after another, with the handwriting as editable ink (`docs/noteastic.md`).
  * **Experimental:** the reader follows the one public description of the backup format (an
    unofficial MIT-licensed project) and has **not yet been checked against a backup made by
    the app**. Every converted document carries a warning saying so. Compare the result with
    the notebook in Noteastic.
  * **Privacy:** a backup holds **every notebook and every imported PDF** of the library.
    Convert it with the web page (it stays on your device) or the command line, not through a
    `gnnote.server` you do not run yourself. To share only some notebooks, make the backup on a
    separate Windows user account that holds only those (each account has its own library).
* **Noteastic → iPad, PDF:** in the library, right-click the notebook, **Export**, the whole
  notebook (the menu as a review describes it; not yet checked here). Choose "without grid" if
  the iPad app should supply its own paper, then convert the PDF with gnnote. Pages, imported
  PDFs and typed text arrive (Noteastic writes real PDF text); Noteastic's own handwriting
  most likely arrives as background only (medium; one exported PDF would settle it).
* **No `.ntcbak` writer is planned.** Restoring a backup probably **replaces the whole
  library**, and the full database layout is unknown, so a gnnote-made backup could destroy
  notes. The practical way to editable iPad ink in Noteastic is a feature request to the
  developers (contact@noteastic.app or the r/Noteastic community): import PDF ink annotations
  as Noteastic strokes. gnnote's existing PDF output would then work unchanged.

## Microsoft OneNote

gnnote **reads** OneNote and keeps handwriting, highlighter and pressure as editable ink
(`docs/onenote.md`). It does not write OneNote, so the route is one way: OneNote → iPad.

* **OneNote desktop** (Microsoft 365, OneNote 2016 to 2024) on Windows:
  * one section: *File > Export*, *Section*, **OneNote 2010-2016 Section (\*.one)**;
  * the whole notebook: *File > Export*, *Notebook*, **OneNote Package (\*.onepkg)**.
  Both convert with gnnote.
* **OneNote for Windows 10** (the older Store app) has been **read-only** since 14 October 2025:
  it still shows notes but no longer edits or syncs them. Its notebooks live on OneDrive; get
  them as below or open them in OneNote desktop.
* **Personal Microsoft account:** notebooks are folders on OneDrive. On onedrive.live.com
  select the notebook's folder and choose **Download** to get a `.zip`, or in OneNote for the
  web right-click the notebook and choose **Export notebook**. gnnote converts the `.zip`. The
  same download should also work in Safari on the iPad, without Windows (expected, not yet
  verified on a device; `docs/onenote.md`).
* **Work or school account:** Microsoft documents the notebook download and export for personal
  accounts only. Use the desktop exports (`.one`, `.onepkg`) instead. No work or school
  notebook has been tested.
* **iPad → OneNote:** not possible with editable ink. OneNote can insert a PDF as a printout,
  which is a picture.

## Microsoft Journal

Microsoft Store 9N318R854RHH; Windows only. Its journals are `.ink` files in
*Documents\Journals*. gnnote **cannot read `.ink`** yet: no sample file has been available
(see [Sample files we would like](#sample-files-we-would-like)).

* **Journal → iPad:** use **Export to OneNote**. It needs the *desktop* OneNote (2013 or
  newer) with the target notebook opened once on the same PC; *OneNote for Windows 10* is not
  supported. Then export the section from OneNote as `.one` (or the notebook as `.onepkg`)
  and convert it with gnnote. That the ink stays editable on this path is likely (medium) but
  **not yet checked on Windows**; Microsoft notes that exported ink may not support search or
  ink-to-text in OneNote.
* Journal's **Export as PDF**: whether the ink becomes annotations or page content is unknown.
  If it is page content, gnnote shows it as background only.
* **iPad → Journal:** Journal copies the pages of an inserted PDF into the journal. Whether a
  gnnote PDF's ink annotations become Journal ink is unknown; most likely they are flattened
  (low confidence, **not yet checked on Windows**).

## PDF apps: Drawboard, Xodo, Acrobat, Edge and others

These apps keep handwriting as PDF ink annotations, which is the route gnnote supports in
both directions:

| App | Store ID | How sure that its pen ink is an ink annotation |
|---|---|---|
| Drawboard PDF (also on the iPad) | 9WZDNCRFHWQT | medium |
| Xodo PDF Reader & Editor | 9WZDNCRDJXP4 | high |
| Adobe Acrobat / Acrobat Reader | | high |
| Microsoft Edge (built-in PDF viewer) | | medium |
| Foxit PDF Reader / Editor, PDF-XChange Editor, UPDF | | medium |
| Firefox (built-in PDF viewer) | | checked in its source code |

* **iPad → PDF app:** convert with gnnote to **PDF** with **"As editable annotations"**
  (`--pdf-ink annotations`). Each stroke is one annotation that the app should let you select,
  move, recolour and delete; erasing part of a stroke depends on the app.
* **PDF app → iPad:** draw, then **Save** the PDF with the annotations kept (in Drawboard:
  *Save*, not flatten; Drawboard can also export annotations as XFDF, which gnnote does not
  read). Convert the PDF with gnnote: ink, lines, shapes, highlights and text boxes become
  editable strokes and text boxes; highlighters stay highlighters.
* Caveats: one width per stroke on the way back (pressure lost); pages and imported PDFs stay
  PDF backgrounds; "flatten" or "print to PDF" turns ink into background.
* **Not yet checked on Windows** for any of these apps with gnnote's files.

## LiquidText, Chrome and other PDF tools

* **LiquidText** (Store 9N9Z9NSV47FL, also on the iPad): works with PDFs and can export them,
  but whether its ink is stored as ink annotations is unknown. **Not yet checked**; one PDF
  exported from LiquidText would settle it.
* **Google Chrome's PDF "Annotate"** draws its ink into the page content (Chromium's source
  code and test files show this), so gnnote shows it as background only. Chrome displays
  gnnote's annotations but cannot edit them.
* **PDF Annotator** (GRAHL) keeps its own annotations in a separate layer; whether gnnote can
  read them is unknown.
* **Noteshelf for Windows** cannot import or export the iPad app's `.noteshelf` / `.nsa` files
  (vendor help). Its PDF export works with gnnote only if the ink is saved as annotations,
  which is unknown.

## Xournal++, Rnote, Saber and Excalidraw

Open-source apps whose files gnnote reads **and** writes, with per-point pressure where the
format has it (`docs/xournalpp.md`, `docs/saber.md`, `docs/excalidraw.md`):

* **Xournal++** (`.xopp`): the official Windows installer from xournalpp.github.io; the
  Microsoft Store listing 9NN1HHCT0P1X is a community package. Convert iPad notes with gnnote
  to **Xournal++** and open the `.xopp`; save in Xournal++ and convert the `.xopp` back.
* **Rnote** (Windows installer): imports and exports Xournal++ `.xopp` files (Rnote's own
  documentation); use the Xournal++ route. Rnote's own `.rnote` format is not read.
* **Saber** (`.sba`) runs on the iPad and on Windows, so its notes move between both without
  gnnote; gnnote converts them to and from GoodNotes, Notability and the others.
* **Excalidraw** (`.excalidraw`) runs in any browser. The `.excalidraw.md` files of the
  Obsidian Excalidraw plugin are not read.

No file written by gnnote has been opened in these apps on Windows yet.

## Stylus Labs Write

Write (from styluslabs.com, Windows installer or zip; also on the iPad, macOS, Linux and
Android) keeps a document as an SVG file, `.svgz` by default. gnnote reads `.svgz` and `.svg`
documents with editable ink and writes `.svgz` (`docs/styluswrite.md`).

* **Files written by gnnote have not been opened in Write yet.** Known losses on the way in:
  PDF page backgrounds are dropped, text boxes show but are not editable in Write, and
  highlighters are written as translucent ink (Write's own highlighter encoding is unknown).
* The iPad version of Write was last updated in 2023.

## Samsung Notes

The Windows app (Store 9NBLGGH43VHV) is officially supported only on Samsung PCs (Galaxy
Book); reports from May 2025 say it runs on other PCs too (medium). There is no iPad version.

* **Samsung Notes → iPad:** open the note, then **Save as file > Samsung Notes file**, and
  convert the `.sdocx` with gnnote: handwriting stays editable (`docs/samsungnotes.md`).
  gnnote's reader has **not yet been tried with a `.sdocx` made on Windows**.
  *Save as file > PDF* is no substitute: Samsung's PDF export turns handwriting into pictures.
* **iPad → Samsung Notes:** not possible; gnnote does not write `.sdocx`.

## Nebo (MyScript)

Nebo for Windows (Store 9NBLGGH4NLB0) still works but gets no new features; on the iPad the
app is now called MyScript Notes and uses the same `.nebo` file. Export a page or notebook as
`.nebo` and convert it with gnnote (handwriting only; `docs/nebo.md`). gnnote's reader has been
tested with files from iOS and from other tools, **not yet with a `.nebo` made on Windows**.
gnnote does not write `.nebo`.

## Apps gnnote cannot help with

* **Microsoft Whiteboard:** personal accounts can no longer create or edit boards since
  25 September 2026, and the standalone Windows, iOS and Android apps are being retired
  (unsupported from 16 October 2026). Whiteboard continues inside Teams and on the web for work
  and school accounts. Its only exports are pictures (PNG; SVG on the web).
* **Picture-only apps:** Paint, Snipping Tool, Windows Ink Sketchpad, Fresh Paint and
  Sketchbook save pixels, not strokes.
* **Concepts:** the Windows and Android `.concepts` file is not compatible with the iPad's
  `.concept` file and is undocumented.
* **Undocumented or discontinued:** Zoho Notebook sketches, Evernote ink notes, Inkodo,
  Penbook, Plumbago. Inkodo's and Penbook's PDF exports may still be worth one test (see
  [Sample files we would like](#sample-files-we-would-like)); Plumbago exports pictures only,
  and Zoho and Evernote make PDFs only by printing.
* **Not on Windows:** Squid (Android and ChromeOS), Flexcil (only its Android app under an
  emulator; gnnote reads Flexcil files from the iPad), Notability for Windows (a meeting-notes
  app without pen ink; gnnote reads the iPad's `.note` files).

## How to test on Windows

The test kit is one small, made-up notebook in every format a Windows app might open, plus a
checklist in plain language. It contains nothing but generated test content.

1. On a computer with gnnote's source and Python 3.11 or newer:

   ```sh
   python3 tools/make_windows_testkit.py gnnote-kit
   ```

   (On Windows: `py tools\make_windows_testkit.py gnnote-kit`.) The folder then holds:

   | File | What it is for |
   |---|---|
   | `gnnote-ink-annots.pdf` | the notebook as PDF with every stroke an ink annotation: PDF apps, Noteastic, Journal, LiquidText |
   | `gnnote-ink-flat.pdf` | the same with the ink drawn into the pages, for comparison |
   | `gnnote-test.goodnotes` | Goodnotes for Windows import |
   | `gnnote-test.note` | Notability on the iPad (round-trip check): ink on Notability's plain paper, the way gnnote's ink has already been seen working on an iPad (in a one-page test); its text box and second page have not been tested on an iPad yet |
   | `gnnote-test-pdfpages.note` | the same with A4 PDF pages; no gnnote `.note` with PDF pages has been opened on an iPad yet, so the first question is whether it opens at all |
   | `gnnote-test.xopp` | Xournal++ and Rnote |
   | `gnnote-test.svgz` | Stylus Labs Write (only when gnnote has the Write writer) |
   | `CHECKLIST.txt` | the steps for each app and what to send back |

   Page 1 holds the text "gnnote p1" under a yellow highlighter stroke, black, red and blue pen
   strokes at three widths that are thin at the ends and thick in the middle (pressure), and a
   straight line exactly 1 inch (72 pt, 2.54 cm) long for checking sizes. Page 2 is lined
   paper with two strokes. Some files differ on purpose. gnnote writes GoodNotes strokes with
   one even width each, so in `gnnote-test.goodnotes` the wavy strokes are not thinner at the
   ends (expected, not a fault of Goodnotes for Windows). `gnnote-test.note` uses Notability's
   own plain paper (gnnote's default, `--paper plain`), which is a little smaller than A4, so
   everything on it is about 5 % smaller and page 2 has no lines. `gnnote-test-pdfpages.note`
   keeps A4 and the lines as PDF pages (`--paper pdf`), but that mode is untested on devices:
   no gnnote `.note` with PDF pages has been opened on an iPad yet. Both `.note` files also go
   beyond what an iPad has shown working (ink on one plain page): each has a text box and a
   second page. So if only `gnnote-test-pdfpages.note` fails to open or looks wrong, the PDF
   pages are the likely cause; if both fail, look first at what they share, the text box and
   the second page.
2. Copy the folder to the Windows PC and follow `CHECKLIST.txt` for the apps you have. For
   each app it asks: does the file open, is everything there, can a stroke be selected,
   moved, recoloured and erased, does pressure survive an edit; then it asks for one file
   saved or exported by the app. Several apps open the same `gnnote-ink-annots.pdf`, so keep
   the kit's files unchanged: save with *Save as* (or *Save a copy*) under a new name, and if
   an app saved into the open file anyway, copy that file again from the kit before the next
   app.
3. Send the answers, screenshots and files back (an issue on the project page, or to whoever
   gave you the kit).

What the answers settle, app by app:

* Drawboard, Xodo, Edge, Acrobat: whether their pen strokes are ink annotations, whether
  gnnote's annotations can be edited there, and what happens to pressure after an edit.
* Goodnotes for Windows: whether it imports a gnnote `.goodnotes`, and whether its Editable
  PDF uses ink annotations.
* Noteastic and Microsoft Journal: whether imported PDF ink becomes their own ink, and how
  their PDF exports store handwriting.
* Xournal++, Rnote and Write: whether gnnote's files open with editable, pressure-sensitive
  ink.
* Notability on the iPad: whether a gnnote `.note` with a text box and a second page opens
  with everything on it, and whether `.note` files with PDF pages (`--paper pdf`) open at all.

## Sample files we would like

**Privacy first.** Make every sample on a **separate Windows user account** or with test
content only. A Noteastic backup contains the **whole library**, and Journal files contain
real notes. Install apps only from the Microsoft Store or the developer's own website. Never
send files with private notes.

| App | What it would settle | Steps | Files to send |
|---|---|---|---|
| **Noteastic** (most wanted) | Confirms or corrects the experimental `.ntcbak` reader | 1. *Settings > About*: screenshot of the version and the licence list; note the Windows display language. 2. A folder "gnnote" with a notebook "basic". Page 1: three pen strokes in black, red and blue at different thicknesses, pressing hard and then lightly, and one yellow highlighter stroke. Page 2: a plain line, an arrow, a double arrow, a dashed line, a rectangle, an ellipse, a triangle, a pentagon and one filled shape. Page 3: one PNG and one JPEG picture. Pages 4 to 6: lined, squared and dotted paper, one stroke each. Page 7: a stroke moved with the lasso, one made bigger, one rotated, one partly erased and one deleted. 3. Drag `gnnote-ink-annots.pdf` from the test kit into the folder, import all pages, write one stroke on each page; note whether gnnote's handwriting is visible and whether the lasso can select or recolour it. 4. *Settings > Backup*: save `gnnote-sample-1.ntcbak`. Add one stroke on page 1 of "basic" and back up again as `gnnote-sample-2.ntcbak`. 5. In the library, right-click "basic", *Export*, whole notebook, once with and once without the grid; the same for the notebook made from the test PDF. 6. Optional, in PowerShell (names and sizes only): `Get-ChildItem -Recurse "$env:LOCALAPPDATA\Packages\45850BK.Noteastic_fx0e6khbrmsga" \| Select-Object FullName,Length`. 7. Optional, on the test account only: restore `gnnote-sample-1.ntcbak` and say whether the library was replaced or merged | `gnnote-sample-1.ntcbak`, `gnnote-sample-2.ntcbak`, `basic-grid.pdf`, `basic-nogrid.pdf`, `pdf-export.pdf`, the About screenshot, the answers to 3 and 7 |
| **Microsoft Journal** | A reader for `.ink`, without the OneNote detour | 1. A new journal: pen, pencil and highlighter, pressure on and off, three colours, one straight line exactly 1 inch long drawn with the ruler, one inserted PDF page. 2. Find the file in *Documents\Journals* (it ends in `.ink`). 3. *Export as PDF*. 4. *Export to OneNote* (desktop OneNote), then in OneNote *File > Export > Section* as `.one`. 5. A screenshot of each page. 6. If you have an old journal not edited since 2022, a copy of it too | `.ink`, PDF, `.one`, screenshots |
| **OneNote desktop** | Confidence that real `.onepkg` packages convert | A small notebook with pen and highlighter: *File > Export > Notebook > OneNote Package (.onepkg)*, and *File > Export > Section* of the same section as `.one` | `.onepkg`, `.one` |
| **Samsung Notes for Windows** | A Windows-made `.sdocx`; whether the app runs on other PCs | Open a note with handwriting, *Save as file > Samsung Notes file*, and *Save as file > PDF*. Say whether the app runs on your PC at all | `.sdocx`, PDF |
| **Stylus Labs Write** | Whether gnnote-written Write files are editable | Install Write from styluslabs.com. Open `gnnote-test.svgz` from the kit; select, recolour and partly erase a stroke, draw one stroke with each pen type, then *Save as* `write-resaved.svgz` (not over the kit's file). Also draw one line with each pen type in a new document and save it as `write-pens.svgz` | `write-resaved.svgz`, `write-pens.svgz` |
| **Goodnotes for Windows, Drawboard PDF, Xodo, Edge, Rnote** | Confirms the routes above | Follow `CHECKLIST.txt` | the saved PDFs and `.xopp`, with yes / no answers |
| **Nebo for Windows** | That a Windows-made `.nebo` reads | Export one page as `.nebo` and as SVG | `.nebo`, `.svg` |
| **LiquidText, PDF Annotator, Noteshelf, Penbook, Inkodo, Chrome** (optional) | Whether any of them needs work | Draw a pen stroke and a highlighter and save or export a PDF (PDF Annotator: "always editable"; Inkodo: the Pro "container" save and a GIF export; Chrome: *Annotate* with pen, highlighter and a text box, then *Save*) | the PDFs and files |

## Sources

Vendor pages, read in November 2026; the dates are the pages' own update dates.

* Goodnotes support: "Import files into Goodnotes" (7353717816463, May 2026), "Export documents
  or pages" (7353742824975, November 2026), "Frequently requested features for Android,
  Windows, and Web" (7922907940367, September 2026), "Choose Between Editable and Flattened PDF
  Export" (8537070839183), "Goodnotes Cloud" (10277366719759); support.goodnotes.com.
* Noteastic: FAQ and changelog (version 1.10, 21 September 2026), noteastic.app; backup layout
  from github.com/Canxinner/noteastic-mcp-public (MIT), see `docs/noteastic.md`.
* Microsoft: "Export to OneNote" and "Export as PDF" for Microsoft Journal; "Export OneNote
  notebooks"; the OneNote for Windows 10 end-of-support notice (Microsoft 365 Insider blog,
  October 2025); Message Center MC1441775 on Whiteboard; support.microsoft.com.
* Drawboard, "How to save and store document annotations" (drawboard.com); Apryse
  documentation on free-hand annotations (Xodo); Mozilla pdf.js `src/core/annotation.js`;
  Chromium `pdf/pdfium/pdfium_ink_writer.cc` and its test PDFs.
* Noteshelf support "Cross Platform Sync?"; MyScript help "Nebo becomes MyScript Notes";
  Concepts FAQ on export formats; Samsung Notes Store listing; Rnote README; Xournal++
  "Relation to other software" (xournalpp.github.io).
* Microsoft Store catalogue entries for the Store IDs named on this page.
