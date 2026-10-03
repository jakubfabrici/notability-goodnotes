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
