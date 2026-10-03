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
* The OneNote reader (`gnnote/onenote/`) was written from Microsoft's [MS-ONESTORE],
  [MS-FSSHTTPB] and [MS-ONE] specifications, which Microsoft publishes under the Open
  Specification Promise, and from the reverse-engineered ink layout of the **onenote.rs**
  wiki (https://github.com/msiemens/onenote.rs/wiki, facts only, re-verified on sample
  files; see `docs/onenote.md`). The pressure-to-width factor `1.5 p + 0.25` is the one
  WPF's ink renderer (https://github.com/dotnet/wpf, MIT) uses; it is a formula, no code was
  taken. No code from onenote.rs (MPL-2.0), Joplin (AGPL-3.0 / MPL-2.0), oneconv or
  OneNoteViewer (GPL-3.0), libmson (LGPL-2.1), OfficeIMO or the Obsidian Importer (MIT) was
  copied or adapted.
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
