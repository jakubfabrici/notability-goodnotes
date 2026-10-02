# Third-party notices

gnnote is released under the MIT License (see `LICENSE`). It is a clean-room
implementation written from the byte-level research notes in `docs/`.

* Parts of the GoodNotes container synthesis (event ordering and record shapes) are
  adapted from **inkterop** by cable729 (https://github.com/cable729/inkterop), MIT License,
  Copyright (c) the inkterop authors. Used with attribution as required by that licence.
* **goodparse** (https://github.com/franzthiemann/goodparse, GPL-3.0) and
  **parser-for-goodnotes** (https://github.com/Kaih1825/parser-for-goodnotes, MIT) are used
  **only as test oracles**: the test suite runs them in a separate process, in their own
  checkout, to compare results. No code from them is included in, imported by or linked
  into the `gnnote` package.
* Other reference material (notability-to-svg, GPL-3.0; inkref and jojo-notes, no licence)
  informed the documented file-format facts only; no code was copied.
