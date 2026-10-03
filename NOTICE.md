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
