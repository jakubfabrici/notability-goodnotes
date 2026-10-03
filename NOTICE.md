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
