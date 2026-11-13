# Third-party notices

gnnote is released under the MIT License (see `LICENSE`). It is a clean-room
implementation written from the byte-level research notes in `docs/`.

* Parts of the GoodNotes container synthesis (event ordering and record shapes) and of the
  PencilKit decoder `gnnote/pencilkit.py` (the `PKDrawing` container layout, the channel table
  and the decoding of the channel values, from inkterop's `formats/pencilkit.py`) are
  adapted from **inkterop** (https://github.com/cable729/inkterop), MIT License,
  Copyright (c) 2026 Caleb (cable729). The MIT permission notice of inkterop applies to the
  adapted parts; it is reproduced here as that licence requires: "Permission is hereby
  granted, free of charge, to any person obtaining a copy of this software and associated
  documentation files (the "Software"), to deal in the Software without restriction,
  including without limitation the rights to use, copy, modify, merge, publish, distribute,
  sublicense, and/or sell copies of the Software, and to permit persons to whom the Software
  is furnished to do so, subject to the following conditions: The above copyright notice and
  this permission notice shall be included in all copies or substantial portions of the
  Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND."
  The open-format codecs adapt further parts of inkterop under the same notice: the shape of
  the Xournal++ output in `gnnote/xournalpp/writer.py` follows inkterop's xopp writer
  (`core/src/inkterop/formats/xopp/`); the BSON codec `gnnote/saber/bson.py` and the document
  shape of `gnnote/saber/writer.py` are adapted from inkterop's Saber reader and writer
  (`core/src/inkterop/formats/saber/`); the freedraw width law and the element field set of
  `gnnote/excalidraw/` are adapted from inkterop's Excalidraw codec
  (`core/src/inkterop/formats/excalidraw.py`).
  The Samsung Notes record reader `gnnote/samsungnotes/records.py` ports inkterop's `.sdocx`
  reader (`core/src/inkterop/formats/sdocx.py`, commit
  f16eb8d2a425637aab629e6cc90c00a18f17009f): the cursor and bitfield primitives, the
  fixed-point deltas, the plain and delta-compressed stroke event decoder, the stroke field
  list, the layer and page layout, the page list and the string registry of `note.note`; the
  same notice applies to these parts.
* The Excalidraw codec follows the scene format of **Excalidraw**
  (https://github.com/excalidraw/excalidraw), MIT License, Copyright (c) 2020 Excalidraw; its
  JSON schema documentation and `restore.ts` were used for the element fields.
* **Xournal++** (https://github.com/xournalpp/xournalpp, GPL-2.0) and **Saber**
  (https://github.com/saber-notes/saber, GPL-3.0) were consulted for format facts only (their
  loaders, savers and test files); no code was copied. Their test files are fetched by the test
  suite as external test data and are not part of this repository.
* The Saber ink width law is the documented radius formula of **perfect-freehand**
  (https://github.com/steveruizok/perfect-freehand, Dart port by Adil Hanney, MIT); no code was
  copied.
* **Stylus Labs Write** (`gnnote/styluswrite/`, `docs/styluswrite.md`): the document and page
  skeleton the writer emits (`svg#write-document` with `rect#write-doc-background`,
  `svg.write-page` holding `g.write-content` and a `g.ruleline` that starts with
  `rect.pagerect`) follows the templates of **pdftowrite**
  (https://github.com/kosmospredanie/pdftowrite @ 2e25ee29696e8a93e0e00ab50b10b2b7d6d63d3a,
  `pdftowrite/data/document.svg` and `pdftowrite/data/page.svg`), MIT License, Copyright (c)
  2021 Космическое П. (kosmospredanie@yandex.ru). The MIT permission notice quoted above for
  inkterop applies to those adapted parts in the same way. The reader's handling of Write's
  alpha-first `rulecolor`, its page discovery (any `svg` holding a `g.write-content`) and its
  transform composition follow inkterop's Write reader
  (`core/src/inkterop/formats/svg/write.py` and `reader.py` at
  f16eb8d2a425637aab629e6cc90c00a18f17009f), under the inkterop notice above. Write's FAQ
  (http://www.styluslabs.com/faq/) and the README of **styluslabs/templates**
  (https://github.com/styluslabs/templates, no licence) were used for facts only. The Write
  application's source code (https://github.com/styluslabs/Write, AGPL-3.0) was not consulted;
  its AGPL data files (`scribbleres/Intro.svg`, `Help.html`, `clippings.html`) were only
  inspected for facts and are not part of this repository. The path data and transform parser
  `gnnote/styluswrite/svgpath.py` is written from the SVG 1.1 specification. The test suite
  reads two pages of the Stylus Labs web site (http://www.styluslabs.com/svg/site1_page006.svg
  and site1_page007.svg, no licence stated, SHA-256 pinned in `tests/conftest.py`) and
  inkterop's CC0 fixture `core/tests/fixtures/svg/write-mini.svg` as external test data; none
  of them is committed.
* The PencilKit format description in `docs/collanote.md` section 5 builds on inkterop's
  `docs/formats/pencilkit.md` by Caleb (cable729), licensed CC BY 4.0
  (https://creativecommons.org/licenses/by/4.0/); the stroke transform, deleted-stroke and
  version-2 additions are gnnote's own findings. The tests read inkterop's PencilKit fixtures
  (`core/tests/fixtures/pkdrawing/`, dedicated to the public domain under CC0 1.0) from the
  pinned inkterop checkout; they are not copied into this repository.
* **collanote_cnote_to_pdf_converter** (https://github.com/alarsama/collanote_cnote_to_pdf_converter,
  MIT, JavaScript) informed documented CollaNote facts only (the legacy-page coordinate
  heuristic, the audio fields); no code was copied.
* CollaNote and PencilKit sample files from repositories without a licence
  (`enisogdum/YTU-Archive`, `r987r/Flashcard`, a notebook in
  `Kinjalrk2k/100-Days-of-Machine-Learning-Campus-X`) are test inputs only: the test suite
  fetches them at pinned commits and they are never committed or redistributed.
* `gnnote/applelz4.py` shares its module name and the names of its public helpers with the
  corresponding module of goodparse (the names follow Apple's `libcompression` terminology);
  its implementation was written from `docs/ecosystem.md` section 3 and the LZ4 block format
  description, not from goodparse.
* **goodparse** (https://github.com/franzthiemann/goodparse, GPL-3.0) and
  **parser-for-goodnotes** (https://github.com/Kaih1825/parser-for-goodnotes, MIT) are used
  **only as test oracles**: the test suite runs them in a separate process, in their own
  checkout, to compare results. No code from them is included in, imported by or linked
  into the `gnnote` package.
* The Noteful codec (`gnnote/noteful/`) was written from the format facts in `docs/noteful.md`,
  which were derived from **notesconverter** (https://github.com/samuelsadok/notesconverter),
  MIT License, Copyright (c) 2026 Samuel Sadok: its sample files and its reading of the format.
  The writer follows the record shapes notesconverter writes and its author verified on
  devices, and two of its schemes are adapted: the delta encoding of rich-text attributes
  (each chunk lists the attributes that changed, the font size always) and the generation of
  page ordering tags (a 7-character base-64 counter starting at "+E+++++"). notesconverter's
  parser is also run by the test suite as a separate-process oracle; it is never imported
  into the package. Its licence, which applies to the adapted parts:

  > MIT License
  >
  > Copyright (c) 2026 Samuel Sadok
  >
  > Permission is hereby granted, free of charge, to any person obtaining a copy of this
  > software and associated documentation files (the "Software"), to deal in the Software
  > without restriction, including without limitation the rights to use, copy, modify, merge,
  > publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
  > to whom the Software is furnished to do so, subject to the following conditions:
  >
  > The above copyright notice and this permission notice shall be included in all copies or
  > substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
  > INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
  > PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
  > FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
  > OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
  > DEALINGS IN THE SOFTWARE.
* Other reference material (notability-to-svg, GPL-3.0; inkref and jojo-notes, no licence)
  informed the documented file-format facts only; no code was copied.
* `gnnote/pdf/fonts/DejaVuSans-subset.ttf` is a subset of **DejaVu Sans** 2.37
  (https://dejavu-fonts.github.io/), made with `tools/make_font_subset.py` (glyphs of the
  listed Unicode blocks kept, hinting and layout tables removed, names unchanged). DejaVu
  fonts are (c) 2003 Bitstream, Inc. (Bitstream Vera), with glyphs (c) 2006 Tavmjong Bah
  (Arev fonts); DejaVu changes are in the public domain. They are distributed under the
  Bitstream Vera Fonts licence and the Arev Fonts licence, whose full text (copyright notices
  and permission notices, as those licences require) ships next to the font as
  `gnnote/pdf/fonts/LICENSE-DejaVu.txt`. PDFs written by gnnote embed a further subset of
  this font when their text needs it.
* The advance widths of the base-14 font Helvetica in `gnnote/pdf/text.py` are the standard
  Helvetica font metrics (Adobe Core14 AFM values, also used by every PDF library); no font
  program is included for Helvetica.

## Readers of other apps' formats

* **MyScript Notes / Nebo** (`gnnote/nebo/`, `docs/nebo.md`): the BINK byte layout, the tag
  table and the pressure width law are those documented by inkterop
  (`docs/formats/nebo.md`, CC BY 4.0, Copyright (c) 2026 Caleb (cable729),
  https://github.com/cable729/inkterop); `docs/nebo.md` restates and extends those facts
  in its own words, and the decoder was written from them (no inkterop code is included).
  inkterop's MIT reader runs only as a separate-process test oracle. Further facts come from
  **notein-export** (https://github.com/davidnoronha1/notein-export, MIT, Copyright (c) 2026
  davidnoronha1) and **nebo-ocr** (https://github.com/tkgo11/nebo-ocr, MIT, Copyright (c)
  2025 nebo-ocr contributors). Facts about Kobo notebooks come from **kollate**
  (https://github.com/andrew-lawlor/kollate, GPL-3.0) and **KoboNotebookPlus**
  (https://github.com/MRoiban/KoboNotebookPlus, no licence); no code from either was used.
* **Flexcil** (`gnnote/flexcil/`, `docs/flexcil.md`): the container layout, the point
  encoding, the text-frame convention and the shape types are facts documented or implemented
  by **flexcil-codex-plugin** (https://github.com/jeonghyeon-net/flexcil-codex-plugin, MIT,
  Copyright (c) 2026 Flexcil Codex Plugin contributors), **flexcil-backup-viewer**
  (https://github.com/janptn/flexcil-backup-viewer, MIT, Copyright (c) 2026 Jan Pultin) and
  **FWebViewer** (https://github.com/c0lbarator/FWebViewer, MIT, Copyright (c) 2025 Zaripov
  Bulat). The reader was written from those facts and the sample file; no code was copied.
  flexcil-codex-plugin's codec runs only as a separate-process test oracle.
* **reMarkable** (`gnnote/remarkable/`, `docs/remarkable.md`): `gnnote/remarkable/scene.py`
  is a standard-library re-implementation of the part of **rmscene**'s v6 block reader that
  gnnote needs (block and tag layout, CRDT ids and sequences, lines, glyph ranges, root text,
  scene info), adapted from rmscene (https://github.com/ricklupton/rmscene, MIT License,
  Copyright (c) 2023 Rick Lupton). The palette and the line heights of typed text come from
  **rmc** (https://github.com/ricklupton/rmc, MIT License, Copyright (c) 2023 Rick Lupton).
  The MIT permission notice quoted for inkterop above applies to these adapted parts in the
  same way. The stored-width rule and the Paper Pro geometry are facts from inkterop's
  `docs/formats/remarkable.md` (CC BY 4.0). The `.rmdoc` member layout, `redir` and the
  226-dpi PDF mapping are facts from **RM-Sticker-Press**
  (https://github.com/szainababbas/RM-Sticker-Press, GPL-3.0); no code from it was used, and
  its sample files are only read by the tests from its own checkout. rmscene runs only as a
  separate-process test oracle.
* **Samsung Notes** (`gnnote/samsungnotes/`, `docs/samsungnotes.md`): besides the inkterop
  port above, these parts are adapted from **sdocx2pdf**
  (https://github.com/squ1dd13/sdocx2pdf, commit 372ef723a6857b2b878ee9a1dfb9520a8f83edf8, MIT
  License, Copyright (c) 2026 squ1dd13):
  - the record layouts of the object base, the strokes' optional fields 18 to 25 and property
    bit 12, shapes, lines, pictures (with their border fields), fill effects, rich text, the
    page's optional fields (paper template, imported-PDF placements, handwriting-recognition
    data, custom page objects), the font-size field of `note.note`, `media/mediaInfo.dat` and
    `end_tag.bin`;
  - the paper-template numbers;
  - the stroke rendering model (width = pen size x clamp(pressure, 0.4, 0.7), a constant 0.45
    x pen size for pens without pressure, the highlighter pen families).
  The MIT permission notice quoted for inkterop above applies to these adapted parts in the
  same way. Provenance: sdocx2pdf does not state how its author learned the format, and its
  source comments name functions and libraries of Samsung's own app. Whether its facts are
  acceptable under gnnote's clean-room policy is recorded as an open maintainer decision in
  `docs/samsungnotes.md` section 1. inkterop's reader also states that it learned its facts
  from sdocx2pdf. Format facts were also checked against inkterop's `docs/formats/sdocx.md`
  (CC BY 4.0, Copyright (c) 2026 Caleb (cable729),
  https://creativecommons.org/licenses/by/4.0/); no text from it is reused. No code from
  twangodev/sdocx (GPL-3.0), from bxff/samsung-notes-format (derived from decompiling the app)
  or from inkterop's `device-mods/vendor/` (GPL-3.0) was used. inkterop's reader runs only as
  a separate-process test oracle.
* The Samsung Notes tests read the **SDOCX Compatibility Corpus** by the SDOCX Compatibility
  Corpus contributors (https://huggingface.co/datasets/twangodev/sdocx-compatibility, revision
  0865fd317776fa4fd60388e93724f789476541d4), licensed CC BY 4.0
  (https://creativecommons.org/licenses/by/4.0/). It holds four notes written by Samsung Notes
  and the app's own PDF exports of them. The files are fetched unchanged at test time
  (`tests/conftest.py` `PINNED_FILES`) and are not part of this repository. The text-position
  values in `tests/test_samsungnotes.py` were measured from the corpus's
  `01-basic-formatting.pdf`. The photo in `03-image-placement` is scikit-image's "chelsea"
  image (CC0). The tests also read inkterop's CC0 fixture
  `core/tests/fixtures/sdocx/synthetic-two-page.sdocx` from the pinned inkterop checkout.
* **Noteastic** (`gnnote/noteastic/`, `gnnote/sqliteutil.py`, `tests/ntcbak_builder.py`,
  `docs/noteastic.md`): the following facts are taken from **noteastic-mcp-public**
  (https://github.com/Canxinner/noteastic-mcp-public, commit
  `0182e7886f73a119473f1ffd23e2327ba8c24ad7`, files `src/backup.js`, `src/export.js` and
  `test/backup.test.js`), MIT License, Copyright (c) 2026 Noteastic MCP contributors: the
  layout of the `.ntcbak` library backup (a ZIP holding the SQLite database `noteastic.db`
  plus the imported PDFs and pictures, with Windows-style member paths), the tables and
  columns, the `PointsData` text encoding, the `#AARRGGBB` colours, the element matrices, the
  names of the element, line and shape types, soft deletion and the draw order. The reader and
  the SQLite helper were written new in Python from those facts; no code was copied. The test
  builder `tests/ntcbak_builder.py` creates the same tables and columns as that project's test
  fixture (`test/backup.test.js`), written out with gnnote's own type declarations. The schema
  b-tree check in `gnnote/sqliteutil.py` follows the page layout described in SQLite's file
  format documentation (https://www.sqlite.org/fileformat2.html, public domain); no SQLite
  code was copied. No Noteastic program code was inspected and no backup made by the app was
  available, so the reader is marked experimental.
* The OneNote reader (`gnnote/onenote/`) was written from Microsoft's [MS-ONESTORE],
  [MS-FSSHTTPB] and [MS-ONE] specifications, which Microsoft publishes under the Open
  Specification Promise, and from the reverse-engineered ink layout of the **onenote.rs**
  wiki (https://github.com/msiemens/onenote.rs/wiki, facts only, re-verified on sample
  files; see `docs/onenote.md`). The pressure-to-width factor `1.5 p + 0.25` is the one
  WPF's ink renderer (https://github.com/dotnet/wpf, MIT) uses; it is a formula, no code was
  taken. No code from onenote.rs (MPL-2.0), Joplin (AGPL-3.0 / MPL-2.0), oneconv or
  OneNoteViewer (GPL-3.0) or libmson (LGPL-2.1) was copied or adapted.
* The `.onepkg` unpacking of the OneNote reader (`gnnote/onenote/cab.py`, the Microsoft
  Cabinet reader, and `gnnote/onenote/lzx.py`, the LZX decoder) is ported by hand from
  `src/formats/onenote-file/cabinet/cabinet.ts` and `src/formats/onenote-file/cabinet/lzx.ts`
  of the **Obsidian Importer** (https://github.com/obsidianmd/obsidian-importer, commit
  d2cb052c365999118c75c998254fcbb1c15a13fe), MIT License, Copyright (c) 2023 Obsidian. Those
  files state that they were ported from **OfficeIMO** (https://github.com/EvotecIT/OfficeIMO),
  MIT License, Copyright (c) 2022 Evotec. The port follows their structure (the parsing of the
  cabinet header, folders, files and data blocks; the LZX decoder's block, tree, match and E8
  translation logic) and adds MSZIP folders, table-driven Huffman decoding, budgets and
  hardening. The facts were checked against Microsoft's [MS-CAB] and [MS-PATCH] documentation.
  The test-side cabinet builder and LZX encoder (`tests/cab_builder.py`) were written from
  those specifications. The Rust crate lzxd (https://github.com/Lonami/lzxd, MIT OR
  Apache-2.0) was consulted for facts and run, during development only, as an independent
  decoder; none of its code is used. libmspack, cabextract, 7-Zip/p7zip and wimlib (GPL / LGPL)
  were not consulted. The makecab test fixtures (from OfficeIMO's tests, MIT) are fetched by
  the test suite with the Obsidian Importer's other OneNote samples and are not part of this
  repository. Both licences apply to the ported parts. Their notices are also reproduced at
  the top of `cab.py` and `lzx.py`, so that they travel with the code in the web bundle and
  the wheel:

  > MIT License
  >
  > Copyright (c) 2023 Obsidian
  >
  > Permission is hereby granted, free of charge, to any person obtaining a copy of this
  > software and associated documentation files (the "Software"), to deal in the Software
  > without restriction, including without limitation the rights to use, copy, modify, merge,
  > publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
  > to whom the Software is furnished to do so, subject to the following conditions:
  >
  > The above copyright notice and this permission notice shall be included in all copies or
  > substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
  > INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
  > PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
  > FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
  > OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
  > DEALINGS IN THE SOFTWARE.

  > MIT License
  >
  > Copyright (c) 2022 Evotec
  >
  > Permission is hereby granted, free of charge, to any person obtaining a copy of this
  > software and associated documentation files (the "Software"), to deal in the Software
  > without restriction, including without limitation the rights to use, copy, modify, merge,
  > publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
  > to whom the Software is furnished to do so, subject to the following conditions:
  >
  > The above copyright notice and this permission notice shall be included in all copies or
  > substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
  > INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
  > PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
  > FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
  > OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
  > DEALINGS IN THE SOFTWARE.
* **one2html** (https://github.com/msiemens/one2html, MIT) is used **only as a test oracle**:
  the OneNote tests run its binary in a separate process when it is available. Two layout
  facts were read from its source (where a page title is drawn, and that a nested ink
  container is placed by its own offsets); no code was taken.
* OneNote sample files of onenote.rs (MPL-2.0, including Joplin fixtures under AGPL-3.0),
  Joplin (https://github.com/laurent22/joplin), Microsoft Interop-TestSuites
  (https://github.com/OfficeDev/Interop-TestSuites, MIT), libmson
  (https://github.com/blu-base/libmson, LGPL-2.1), the Obsidian Importer
  (https://github.com/obsidianmd/obsidian-importer, MIT, including Apache-2.0 Apache Tika
  samples) and py-onenote-parser (https://github.com/Kev744/py-onenote-parser, MIT) are
  fetched by the test suite at pinned commits and are not part of this repository.
