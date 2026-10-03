# Third-party notices

gnnote is released under the MIT License (see `LICENSE`). It is a clean-room
implementation written from the byte-level research notes in `docs/`.

* Parts of the GoodNotes container synthesis (event ordering and record shapes) are
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
* `gnnote/applelz4.py` shares its module name and the names of its public helpers with the
  corresponding module of goodparse (the names follow Apple's `libcompression` terminology);
  its implementation was written from `docs/ecosystem.md` section 3 and the LZ4 block format
  description, not from goodparse.
* **goodparse** (https://github.com/franzthiemann/goodparse, GPL-3.0) and
  **parser-for-goodnotes** (https://github.com/Kaih1825/parser-for-goodnotes, MIT) are used
  **only as test oracles**: the test suite runs them in a separate process, in their own
  checkout, to compare results. No code from them is included in, imported by or linked
  into the `gnnote` package.
* Other reference material (notability-to-svg, GPL-3.0; inkref and jojo-notes, no licence)
  informed the documented file-format facts only; no code was copied.

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
