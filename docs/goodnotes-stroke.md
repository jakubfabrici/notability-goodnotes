# The `.goodnotes` STROKE payload, byte-exact (read + write)

Scope: one ink stroke inside a `notes/<UUID>` page file — the protobuf message, the Apple
framed-LZ4 container in it, the Troy Hanson TPL image inside that, the point/width arrays,
the colour, the coordinate system, and a verified re-encoding recipe. Container-level topics
(ZIP members, `index.*.pb`, event log, clocks) are in `goodnotes-container.md` and are only
referenced here.

Evidence base (everything below was checked against these bytes, not recalled from memory):

| Source | What it contributed |
|---|---|
| `ref/goodparse/samples/{test,test2,test3,Test4,Test5}.goodnotes` (GoodNotes 5, Nov 2020) | 56 strokes; Test5 is used for the two hand decodes |
| `ref/parser-for-goodnotes/assets/{ex1,ex2,ex3,record}.goodnotes` (GoodNotes 6, has `document.info.pb`) | 5621 strokes |
| `ref/goodparse/samples/Test5.pdf` (GoodNotes' own PDF export of Test5, Quartz "iOS 16.7.16") | ground truth for coordinates and widths |
| `ref/goodparse/src/goodparse/{goodnotes,applelz4,protobuf}.py` | reference parser 1 |
| `ref/parser-for-goodnotes/src/goodnotes_re/{stroke,tpl,compression,wire,page}.py` + `wiki/03`, `04`, `09` | reference parser 2 |
| `scratchpad/experiments/gn_stroke_roundtrip.py` | independent decoder + re-encoder + verification with both parsers |

Census of all 5677 strokes in the 9 samples (my own protobuf walk, `experiments/census.py`):

| TPL format string | strokes | stroke `#21` | stroke `#3` | meaning (see §5) |
|---|---:|---|---|---|
| `vuA(v)A(S(uu))A(S(uuuu))vA(f)` | 5619 | 24 | absent | constant-width polyline ("flat" pen / highlighter) |
| `vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` | 34 | 24 | 1 | per-point-radius "ribbon" pen + prebuilt CGPath outline |
| `vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)` | 8 | **25** | 5 | nib-angle pen (tool 25) |
| `vuA(v)A(S(uu))A(S(uuuu))` (no trailing `vA(f)`) | 7 | 24 | absent | same as row 1, older sub-version (TPL word 0 = 1) |
| `vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` | 1 | 24 | 4 | ribbon pen with a leading width word |

Every one of the 5677 geometry blobs is exactly **one `bv41` block followed by `bv4$`**. No `bv4-`
block and no multi-block stream was ever observed. There are zero trailing bytes after `bv4$`
inside field `#2`.

---

## 1. Where a stroke lives

```
notes/<PAGE-UUID>            = concatenation of records: <varint len><record bytes> …
  record k   (metadata)      = { #1 uuid, #2 clock, [#3], #8 ts, #9 seq, [#14], #16 type }
  record k+1 (stroke)        = { #7 LEN <stroke message> }           <- exactly one field
    stroke message           = { #1 uuid, #2 LEN <Apple LZ4 frame>, [#3], #4 colour, [#5], #6, #7, #9, [#10], [#14], #15, #20, #21 }
      Apple LZ4 frame        = 'bv41' u32 dsize u32 csize <LZ4 block> 'bv4$'
        decompressed         = TPL image: 'tpl\0' u32 size <format string>\0 <packed values>
```

The metadata record always immediately precedes its stroke record and carries the same UUID
(5677/5677). parser-for-goodnotes identifies strokes by scanning **every** field value of every
record for the bytes `bv41` (and also sub-fields of `#7`); goodparse walks `record.#7 → #2` and
checks the 4-byte magic. Both therefore depend on the magic being `bv41` (see §4.4).

### 1.1 The metadata record (record k)

Observed field sets: `(1,2,8,9,14,16)` ×5593, `(1,2,3,8,9,14,16)` ×1626 (ex1/ex3/record/test),
`(1,2,8,9,16)` (test2/test3, no `#14`), `(1,2,4,8,9,14,16)` ×6 (image records, no stroke).

| field | wire | tag bytes | observed | meaning / confidence |
|---|---|---|---|---|
| `#1` | LEN 36 | `0a 24` | `6C008FF2-04F1-4455-B2F5-5AE0C232CB8A` | stroke UUID, ASCII upper-case (high) |
| `#2` | LEN 8 | `12 08` | `{#1 varint 12, #2 varint 4216713141}` | clock register `{device, seq}`; identical to stroke `#15` in 5677/5677 (high) |
| `#3` | varint | `18 01` | 1 | parser-for-goodnotes: "erased/tombstone", it skips such strokes; goodparse ignores it. 1313/2807 of ex1 have it. Medium |
| `#8` | varint | `40` | `1606161793633522096` | creation time, **ns since Unix epoch** (= 2020-11-23 20:03:13.633 UTC) (high) |
| `#9` | varint | `48` | 141448, 141459, 141470 … | monotonically increasing per element: z-order / sequence (medium) |
| `#14` | varint | `70` | 5381 | unknown, constant inside a file (low) |
| `#16` | varint | `80 01` | 24 or 25 | element type; equals stroke `#21` in 5677/5677 (high) |

### 1.2 The stroke message (inside record k+1, field `#7`)

Field order is always ascending. Observed signature of the two hand-decoded strokes plus the
optional fields seen elsewhere:

| field | wire | tag | value | meaning | confidence |
|---|---|---|---|---|---|
| `#1` | LEN 36 | `0a 24` | UUID string | same UUID as metadata `#1` | high |
| `#2` | LEN | `12 <len>` | Apple LZ4 frame | **geometry** (§4, §5) | high |
| `#3` | varint | `18` | 1 / 4 / 5 / absent | **geometry encoding version**, 1:1 with the TPL format (table above): absent ⇔ `vuA(v)A(S(uu))A(S(uuuu))[vA(f)]`, 1 ⇔ ribbon pen, 4 ⇔ ribbon pen with width word, 5 ⇔ tool-25 format | high (5677/5677) |
| `#4` | LEN 5–20 | `22 <len>` | colour message | RGBA float32, sub-fields `#1 R #2 G #3 B #4 A`, each `fixed32` (tags `0d 15 1d 25`), **a component equal to 0.0 is omitted** (e.g. pure blue = `15 … 1d … 25 …`, black = only `25 00 00 80 3f`) | high |
| `#5` | varint | `28 01` | 1 / absent | **highlighter flag**. Present ⇔ `A == 0.5` in 12/12 cases, absent in all 5665 alpha-1.0 strokes | high |
| `#6` | LEN 0 or 10 | `32` | `{#1 fixed32 x, #2 fixed32 y}` e.g. `(-299.438, -385.445)` | translation added to every point (goodparse: group origin; parser-for-goodnotes: lasso move). Empty (`32 00`) in most strokes | medium (both parsers add it; not verified against a render) |
| `#7` | LEN 10 | `3a 0a` | `{#1 {#1 varint 3, #2 varint 3639689413}}` | clock register (see container doc §2.3) | medium |
| `#9` | LEN 0/35/40/47/59 | `4a` | `{#1|#2|#4 {points…}, #5 {#2 1}, #15 fixed32 W}` | optional; `#15` = nominal pen width in canvas units (equals TPL word `values[1]` when both exist, e.g. 5.456693); point lists look like shape-recognition hints | low (structure observed, semantics guessed) |
| `#10` | LEN 36 | `52 24` | UUID | group/layer id shared by several strokes (Test5 page 3 only) | medium |
| `#14` | varint | `70 01` | 1 / absent | unknown flag (1313 strokes in ex1, 281 in ex3, 37 in record) | low |
| `#15` | LEN 8 | `7a 08` | `{#1 varint 12, #2 varint 4216713141}` | same clock as metadata `#2` | high |
| `#20` | LEN 0 (once LEN 2 `0a 00`) | `a2 01 00` | empty | unknown | low |
| `#21` | varint | `a8 01` | **24** or **25** | tool / element type. 24 = ink stroke (pens *and* highlighter — highlighter is 24 + `#5`=1 + alpha 0.5). 25 = the nib-angle pen (format row 3; angles π/6, π/3 per point; black, alpha 1 in all 8 cases). goodparse calls 25 "highlighter" — **that is wrong for these samples** (the real highlighters are `#21`=24, `#5`=1) | high for 24/`#5`; medium for what 25 is |

No timestamp is stored in the stroke message itself; use metadata `#8`.

---

## 2. Hand decode 1 — pen stroke (Test5, page `F51610D9…`, record 19)

Blue ribbon-pen stroke, 9 points, GoodNotes 5 (2020). File offset 16237; record length varint
`f6 04` = 630; the record is one field: tag `3a` (field 7, LEN), length `f3 04` = 627.
Offsets below are relative to the 627-byte stroke message.

```
  off  bytes                                              meaning
    0  0a 24                                              #1 LEN 36
    2  36 43 30 30 38 46 46 32 2d … 43 42 38 41           "6C008FF2-04F1-4455-B2F5-5AE0C232CB8A"
   38  12 97 04                                           #2 LEN 535           (0x97 0x04 = 535)
   41  62 76 34 31 92 02 00 00 07 02 00 00 f3 01 74 70 …  Apple LZ4 frame (535 B) -> §2.1
  576  18 01                                              #3 varint 1          (geometry version: ribbon)
  578  22 0f                                              #4 LEN 15            colour
  580  15 f5 f4 f4 3e                                       #2 fixed32 0.478431  G   (R omitted = 0.0)
  585  1d 00 00 80 3f                                       #3 fixed32 1.0       B
  590  25 00 00 80 3f                                       #4 fixed32 1.0       A
  595  32 00                                              #6 LEN 0             (no offset)
  597  3a 0a 0a 08 08 03 10 c5 81 c5 c7 0d                #7 LEN 10  {#1 {#1 3, #2 3639689413}}
  609  4a 00                                              #9 LEN 0
  611  7a 08 08 0c 10 b5 df d7 da 0f                      #15 LEN 8 {#1 12, #2 4216713141}
  621  a2 01 00                                           #20 LEN 0
  624  a8 01 18                                           #21 varint 24
  627  (end)
```

### 2.1 The Apple LZ4 frame (535 bytes, offsets relative to the frame)

```
    0  62 76 34 31      'bv41'  compressed block
    4  92 02 00 00      uint32 LE decompressed size = 658
    8  07 02 00 00      uint32 LE stored (compressed) size = 519
   12  f3 …             LZ4 block, 519 bytes: 34 sequences (see trace below)
  531  62 76 34 24      'bv4$'  end of stream          (12 + 519 + 4 = 535, nothing after)
```

LZ4 sequence trace (`experiments/lz4trace.py`): token `f3` = 15 literals (+ ext byte `01` → 16)
then a match (offset 4, length 3+4 = 7) … the **last** sequence (`@508: token a0`) is 10 literals
with no match — the standard LZ4 "last sequence is literals only" ending. The block is a
plain LZ4 *block* (no LZ4 *frame* header, no block checksum).

### 2.2 The decompressed TPL image (658 bytes, offsets relative to the image)

```
    0  74 70 6c          'tpl'
    3  00                flags: bit0 = big-endian → 0 = little-endian
    4  92 02 00 00       uint32 LE total image size = 658  (must equal the LZ4 dsize)
    8  76 41 28 76 29 41 28 75 29 41 28 75 29 41 28 76 29 41 28 76 29 41 28 75 29 41 28 75 29 41 28 75 29 41 28 75 29 41 28 76 29
                         format string "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)" (41 chars)
   49  00                NUL terminator            ← packed values start at 50
   50  02 00             values[0]  'v'  uint16 = 2                  (sub-version)
   52  05 00 00 00       values[1]  A(v) count 5
   56  00 00 01 00 01 00 01 00 01 00        flags [0,1,1,1,1]        0 = pen-down/start, 1 = continuation panel
   66  03 00 00 00       values[2]  A(u) count 3   = ONE start point (x,y,r)
   70  ea 4a 7d 43  41 55 ad 43  c2 99 3c 40        (253.2926, 346.6660, r=2.9469)
   82  18 00 00 00       values[3]  A(u) count 24  = 8 points × (x,y,r), 2 per panel
   86  0b da 74 43  f4 bb a9 43  b0 23 3c 40        (244.8517, 339.4684, 2.9397)
   98  ba 77 70 43  67 33 a6 43  1f 2d 48 40        (240.4677, 332.4016, 3.1278)
  110  …                                            (236.0837, 325.3348, 3.3158)
                                                    (235.2984, 318.8569, 3.5486)
                                                    (234.5132, 312.3790, 3.7814)
                                                    (237.0651, 305.8356, 3.8588)
                                                    (239.6171, 299.2923, 3.9361)
  170  4a c9 74 43  dd 1c 92 43  df db 73 40        (244.7863, 292.2255, 3.8103)
  182  04 00 00 00       values[4]  A(v) count 4   = commands per panel: [5,5,5,5]
  194  14 00 00 00       values[5]  A(v) count 20  = CGPath command codes
  198  00 00 02 00 03 00 02 00 03 00 | 00 00 02 00 03 00 02 00 03 00 | … ×4
                                                    per panel: 0 MoveTo, 2 CubicTo, 3 ArcTo, 2 CubicTo, 3 ArcTo
  238  08 00 00 00       values[6]  A(u) count 8   = 4 MoveTo points (x,y)
  242  0a d9 6d 43  18 0e a7 43                     (237.8478, 334.1101)   ← on the circle of radius r₁ around point 1
  274  00 00 00 00       values[7]  A(u) count 0   (always empty in the samples)
  278  30 00 00 00       values[8]  A(u) count 48  = 8 cubics × (c1x,c1y,c2x,c2y,x,y)
  282  22 83 71 43 56 dd a9 43 c4 4b 76 43 96 48 ac 43 0e 61 7b 43 1e 74 ae 43
                                                    (241.512,339.729, 246.296,344.567, 251.379,348.907)
  474  28 00 00 00       values[9]  A(u) count 40  = 8 arcs × (cx,cy,r,a0,a1)  radians
  478  ea 4a 7d 43 41 55 ad 43 c2 99 3c 40 a0 c2 11 40 f1 88 5d bf
                                                    (253.2926,346.6660, r 2.9469, a0 2.2775 (130.5°), a1 −0.8654 (−49.6°)) = 180° cap around the start point
  498  …                                            (240.4677,332.4016, 3.1278, −0.5326, 2.5637)  = cap around point 1
  638  08 00 00 00       values[10] A(v) count 8   = per-arc clockwise flags, all 1
  642  01 00 ×8
  658  (end of image)
```

Geometry of this format, verified numerically (`experiments`, "poly" test):

* The **centre-line** is the ordered point list `values[2]` ∪ `values[3]` consumed by the flags:
  flag 0 → take one `(x,y,r)` from `values[2]`; flag 1 → take the next **two** `(x,y,r)` from
  `values[3]`. For this stroke: 9 points. Several flag-0 entries mean several sub-paths in one
  stroke (Test5 F516 record 1: flags `[0,1×9,0,1×9]`, `values[2]` holds 2 start points).
* **`r` is the half-width (radius) in canvas units**, not the full width and not raw pressure:
  every arc in `values[9]` has `r` equal to the third float of the point it is centred on, and
  GoodNotes' own PDF export draws this stroke as filled outline panels whose cap arcs have
  radius 1.607 pt = 2.9469 × 72/132. `r` varies smoothly along the stroke (2.94 → 3.94): the
  pen pressure/velocity response is already baked in.
* The side cubics of each panel are offsets of the **polyline through all 9 points** (including
  the even-indexed "middle" points): min-distance / r = 1.000 (min 1.000, max 1.001) for this
  stroke, versus 0.83–1.14 for a quadratic-control-point hypothesis. So treat `values[3]` as
  on-curve points; a renderer that does not want the CGPath can draw the polyline with per-point
  width `2r` and round caps.
* `values[4..10]` are a pre-rasterised **CGPath outline**: one closed panel per flag-1 entry,
  `MoveTo(values[6][i])`, `CubicTo(values[8][2i])`, `ArcTo(values[9][2i] + flag values[10][2i])`,
  `CubicTo(values[8][2i+1])`, `ArcTo(values[9][2i+1])`. Dots (1 panel) use commands `[0,3,3]`
  with 0 cubics and 2 arcs. Multi-sub-path strokes carry one extra cubic (37 cubics for 18
  panels in F516 record 1), so **do not** assume `cubics == 2 × panels`.
* Variant with flags `4/5` and command `6` (Test5 27B0 record 7, ex2 records 9/13/19): start
  tuple has 4 floats `(x,y,r,0.0)`, each panel 9 floats `(x,y,r, x,y,r, 0,0,0.1)`, panels have
  8 commands `[0,2,2,6,2,2,2,6]`, and the arc pool has **7** floats per arc
  `(cx,cy, 1.8, 18.0, a0, a1, 0.0)`; r = 18 (a 36-unit-wide chisel-like marker). Tool identity
  unknown; low confidence on everything except the sizes.

---

## 3. Hand decode 2 — highlighter stroke (Test5, page `27B0C0FB…`, record 57)

Yellow, alpha 0.5, `#5 = 1`. File offset 60557; record length `e7 02` = 359; tag `3a`,
message length `e4 02` = 356. Offsets relative to the 356-byte stroke message.

```
    0  0a 24  "ED0FFAFD-044C-4070-BEBA-3F67EA4EABF1"      #1 UUID
   38  12 88 02                                           #2 LEN 264
   41  62 76 34 31 0e 01 00 00 f8 00 00 00 f3 06 74 70 …  Apple LZ4 frame: 'bv41', dsize 270, csize 248, block, 'bv4$' at frame offset 260
  305  22 0f                                              #4 LEN 15
  307  0d 00 00 80 3f                                       #1 fixed32 1.0  R
  312  15 00 00 80 3f                                       #2 fixed32 1.0  G     (B omitted = 0.0)
  317  25 00 00 00 3f                                       #4 fixed32 0.5  A
  322  28 01                                              #5 varint 1          ← highlighter flag
  324  32 00                                              #6 LEN 0
  326  3a 0a 0a 08 08 05 10 fc ab d6 e1 07                #7 {#1 {#1 5, #2 2083886588}}
  338  4a 00                                              #9 LEN 0
  340  7a 08 08 02 10 bc c0 ab b1 0a                      #15 {#1 2, #2 2787827772}
  350  a2 01 00                                           #20 LEN 0
  353  a8 01 18                                           #21 varint 24        ← still "24"; highlighter ≠ tool 25
  356  (end)
```

Decompressed TPL image (270 bytes):

```
    0  74 70 6c 00  0e 01 00 00                 'tpl', flags 0, size 270
    8  "vuA(v)A(S(uu))A(S(uuuu))vA(f)" 00      format (29 chars + NUL) → values start at 38
   38  02 00                                    values[0] 'v' = 2
   40  00 00 c0 41                              values[1] 'u' = 0x41c00000 = float32 24.0   ← nominal width W (canvas units)
   44  0c 00 00 00                              values[2] A(v) count 12 = 1 start + 11 quads
   48  00 00 01 00 01 00 … 01 00                flags [0, 1×11]
   72  01 00 00 00                              values[3] A(S(uu)) count 1
   76  84 d0 87 43 f1 b6 0d 42                  start point (271.6290, 35.4287)
   84  0b 00 00 00                              values[4] A(S(uuuu)) count 11
   88  e0 db 88 43 76 2f 14 42 a5 6e 8a 43 c9 83 22 42   (273.7178, 37.0463, 276.8644, 40.6287)
  104  af 70 8b 43 ec b1 2b 42 94 8c 8d 43 ec 47 41 42   (278.8803, 42.9238, 283.0983, 48.3202)
  120  …                                        9 more quads, 16 bytes each
  248  ce 7b ad 43 6e db 3e 43 86 3b ae 43 c8 f3 40 43   (346.9672, 190.8571, 348.4650, 192.9523)
  264  01 00                                    values[5] 'v' = 1
  266  00 00 00 00                              values[6] A(f) count 0  (dash pattern, doubles; always empty here)
  270  (end)
```

Meaning: the centre-line is `start` followed by the 22 quad points in order (23 points, a
polyline; each quad is simply "the next two points", the same 2-points-per-panel layout as the
ribbon format — consecutive quads are *not* contiguous segments). No per-point width exists;
the stroke is `W = 24` canvas units wide (full width, see §7.3). `values[5]` = 1 and `A(f)`
empty in all 5619 strokes of this format. The no-`vA(f)` sibling format has `values[0] = 1`
and stops after `values[4]`.

The tool-25 format (Test5 F516 record 23, `#3 = 5`) is laid out the same way: `values[1]` = W
(1.559055), `values[2]` flags `[0, 1×13]`, `values[3]` one 5-tuple `(x, y, 0.5236, 1.0472, 0)`,
`values[4]` 13 11-tuples `(u32 seed, x1,y1, π/6, π/3, 0, x2,y2, π/6, π/3, 0)`; the trailing
`A(S(uu)) A(v) A(S(uu)) A(S(uuuu)) A(u)` arrays are empty except on page 27B0 record 1/39
(`[1, 11, 1, 10, 0]`). The first uint32 of each 11-tuple is not a float (random-looking); the
two constant angles are 30° and 60°. Centre-line = `(x,y)` of values[3] then `(x1,y1),(x2,y2)`
of each tuple. Semantics of the angles: unknown (nib angle?), low confidence.

---

## 4. Apple framed LZ4 (`libcompression` COMPRESSION_LZ4 stream)

### 4.1 Block grammar

```
stream  := block* 'bv4$'
block   := 'bv41' u32le n_raw_bytes u32le n_payload_bytes <n_payload_bytes of LZ4 block>   (12-byte header)
         | 'bv4-' u32le n_raw_bytes <n_raw_bytes stored verbatim>                           (8-byte header, see 4.4)
```

* Magic bytes: `62 76 34 31` / `62 76 34 2d` / `62 76 34 24`.
* Sizes are little-endian uint32. `n_raw_bytes` must equal the TPL image size (both parsers
  check the LZ4 output length; parser-for-goodnotes additionally checks `tpl` size == length).
* Observed: exactly one `bv41` block per stroke, 5677/5677; largest `n_raw_bytes` 30 520
  (ex2 record 9). Whether GoodNotes itself emits/accepts multi-block streams is untested; both
  reference decoders concatenate blocks, keeping a 64 KiB history across blocks.

### 4.2 LZ4 block grammar (standard, as both decoders implement it)

```
sequence := token(1) [lit_ext*] literals(lit_len) [offset(2 LE) [match_ext*]]
token    := (lit_len_nibble << 4) | match_len_nibble
lit_len  := nibble; if 15: add bytes until a byte != 255
match    := offset uint16 LE (1..65535, never 0); match_len = nibble (+ext same rule) + 4
end      := a sequence whose literals reach the end of the block has no offset/match
```

### 4.3 Writing: the all-literal block (verified)

For a payload `P` of length n ≥ 15: `token = 0xF0`, then `(n − 15)` encoded as `0xFF` bytes while
≥ 255 followed by the remainder byte, then `P` verbatim. Frame:
`'bv41' + u32(n) + u32(len(block)) + block + 'bv4$'`. Examples produced by the experiment:

```
pen        : 62 76 34 31  92 02 00 00  96 02 00 00  f0 ff ff 85 <658 bytes of TPL>  62 76 34 24   (678 B)
highlighter: 62 76 34 31  0e 01 00 00  11 01 00 00  f0 ff 00 74 <270 bytes of TPL>  62 76 34 24   (289 B)
```

(Extension rule: emit `ff` for every full 255 of `n − 15`, then one byte with the remainder,
even if the remainder is 0. 658 − 15 = 643 = 255 + 255 + 133 → `ff ff 85`; 270 − 15 = 255 + 0
→ `ff 00`. The byte after the extension is the first TPL byte `74` = `t`. Both reference
decoders and the LZ4 specification accept a final sequence with no match.)

### 4.4 `bv4-` (stored block) — do not emit

The two reference parsers disagree: goodparse reads an **8-byte** header (`magic, n_raw_bytes`),
parser-for-goodnotes reads a **12-byte** header (`magic, uncompressed, stored`) *and* only
recognises a stroke if the bytes `bv41` occur in the field. Apple's xnu `lz4.h` declares
`lz4_uncompressed_block_header {magic, n_raw_bytes}` (8 bytes) — from memory, medium
confidence; no sample contains one. Experiment result: a file whose strokes use an 8-byte
`bv4-` block decodes identically in goodparse and yields **0 strokes** in parser-for-goodnotes.
Therefore a writer must use `bv41` with an all-literal block (4.3) — it is as simple as a
stored block and is accepted by both parsers and (by construction) by any conformant LZ4 decoder.

---

## 5. The TPL image (Troy Hanson `tpl`, little-endian)

```
  0   't' 'p' 'l'
  3   flags: bit0 = TPL_FL_BIGENDIAN (always 0 here); other bits 0
  4   uint32 LE  total image length (header + format + values)
  8   format string, ASCII, NUL-terminated
  …   packed values, no padding, in format order
```

| char | bytes | struct | notes |
|---|---|---|---|
| `c` | 1 | `<b` | not seen |
| `j` / `v` | 2 | `<h` / `<H` | `v` used for version words, flags, command codes |
| `i` / `u` | 4 | `<i` / `<I` | **`u` carries IEEE-754 float32 bit patterns** everywhere a coordinate/width is stored (`struct.unpack('<f', struct.pack('<I', u))`); there are no `f` scalars for coordinates |
| `I` / `U` | 8 | `<q` / `<Q` | not seen |
| `f` | 8 | `<d` | double; only inside `A(f)` dash array (always empty) |
| `A(x)` | 4 + n·size | `<I` count, then n elements | arrays of scalars or of `S()` structs |
| `S(…)` | sum of members | packed | no count, no padding |

A file whose `format` differs from the five strings in the census should be rejected rather than
guessed.

Per-format value index (0-based position in the top-level value list):

**`vuA(v)A(S(uu))A(S(uuuu))vA(f)`** (and the 5-value `vuA(v)A(S(uu))A(S(uuuu))`):
`[0]` v sub-version (2; 1 for the short form) · `[1]` u = float32 W, full stroke width ·
`[2]` A(v) flags, length = 1 + N, `[0, 1, 1, …]` · `[3]` A(S(uu)) start point(s), one per 0-flag ·
`[4]` A(S(uuuu)) N quads = next two centre-line points each · `[5]` v = 1 · `[6]` A(f) dash
pattern (empty). Centre-line = start + 2N points; constant width W.

**`vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)`** (ribbon pen): see §2.2. With the leading `u`
(`vuA(v)…`, `#3 = 4`) everything shifts by one and `[1]` is a float32 width word (parser-for-
goodnotes "variation A": command codes become 2/4/5 instead of 0/2/3).

**Tool-25 format**: see §3 last paragraph.

---

## 6. How the reference parsers find the points (and what they lose)

**goodparse** (`goodnotes.py: extract_points / _points_after_flag / extract_curve`): no TPL
parsing. For `#21 == 24` it first looks for a run of ≥ 30 bytes that are all `00`/`01` (the
flag array) and, if found, reads `[x0][y0][u32 count][count × (x,y)]` after it; otherwise it
scans the raw bytes from offset 64 for the first run of ≥ 2 "valid" float32 triplets at stride
12 (`1 < x,y < 10000`, `0 ≤ w < 200`). For `#21 == 25` it takes the longest run of valid
`(x,y)` pairs at stride 8 and assigns width 10. Consequences measured on Test5:
ribbon pen → it returns `values[3]` only, i.e. **drops the first point** `values[2]`; highlighter
→ it returns 11 of the 23 points (the stride-12 scan happens to hit every other quad point) with
width 2.0 (hard-coded default), and the start point is lost. Colour is read correctly from `#4`.
Canvas→page scale is `page_w / canvas_w` from the MediaBox and the events file (= 72/132).

**parser-for-goodnotes** (`stroke.py: parse_stroke_field / extract_points_from_tpl`): finds
`bv41` in any field value, decodes the frame and the TPL properly, then chooses a point array
heuristically: for formats containing `A(S(uuuu))` it takes `values[3]` + `values[4]` (all 23
highlighter points, pressure = W/2 = 12.0, `width = 24.0`, `is_highlighter = alpha < 0.95`);
for the ribbon format it scores "candidate" arrays with index ≤ 5 whose length is a multiple of
3/5/2 and prefers the longest one with pressure → `values[3]` (8 points, pressure = r), **also
dropping `values[2]`**, `width` left at the default 1.0; it additionally decodes the CGPath
panels (`native_cgpaths`) and renders those. Everything after the frame inside the stroke
message (`#4`, `#6`, …) is parsed as "the trailer" — it is simply the remaining fields of the
stroke message. It skips strokes whose metadata record has `#3 == 1`.

An exact decoder (as in the experiment script) should read the TPL by its format string and
apply the flag-driven consumption rule from §2.2 / §5; that recovers every point.

---

## 7. Coordinate system and widths

### 7.1 Units and origin

* All geometry (points, `r`, `W`, `#6` offsets, `#9.#15`) is in **canvas units**, origin
  **top-left, y down**.
* **canvas units = PDF points × 132/72 (= 11/6)**. Evidence: Test5 paper MediaBox
  `455.04 × 588.45 pt`, canvas size in `index.events.pb` = `834.24 × 1078.82`
  (834.24 × 72/132 = 455.04 exactly); test.goodnotes: A4 595.28 pt ↔ canvas 1091.3.
  GoodNotes' own PDF export of the blue stroke places its points at
  `x_pt = x × 72/132` and `y_pt = 588.45 − y × 72/132` (e.g. highlighter start (271.629, 35.429)
  → `148.161, 569.125` in the PDF), and the pen's cap arcs have radius `r × 72/132`.
  parser-for-goodnotes hard-codes `dpi_scale = 72/132` (`export.py:270`); goodparse derives
  the same number from the files.
* Page size discovery: the page's paper PDF `attachments/<UUID>` `/MediaBox [0 0 W H]`
  (parser-for-goodnotes also handles inherited `/Pages` MediaBox; defaults 612×792 when
  absent). Canvas size: `index.events.pb`, the template event whose body has
  `#4 = <paper attachment UUID>` and `#8 = {#1 fixed32 W_canvas, #2 fixed32 H_canvas}`
  (Test5 record 2: `#2.#8.#1 = 834.24`, `#2.#8.#2 = 1078.82`). If the events file is missing,
  use `MediaBox × 11/6`. Details of that event in `goodnotes-container.md` §9.1/§10.

### 7.2 Per-point width (ribbon pen)

The third float of each point is the **radius r in canvas units** (rendered diameter `2r`).
Pressure is not stored separately; it is already folded into r (r varies 2.94 → 3.94 within one
stroke; nominal pen width for ribbon strokes is not stored in the stroke — `#9.#15`, when
present, equals the W of flat-format strokes, not r). For Notability: `curveswidth = 2·r_median × scale`,
fractional widths `= r_i / r_median` (the PoC does exactly this with goodparse's `w = r`).

### 7.3 Constant width (flat format, highlighter, tool 25)

`values[1]` (= `#9.#15` when present) is the **full width W in canvas units**: GoodNotes' PDF
export pads every stroke's Form-XObject bounding box by exactly `W × 72/132` (e.g. W = 24 →
13.09 pt; W = 36 → 19.64 pt), i.e. its own geometry treats W as the diameter in canvas units.
Caveat: the same export *strokes* these paths with line width `W/2` points (12 for W = 24,
18 for W = 36, 3.8976 for the tool-25 pens), 8.3 % thinner than `W × 72/132`; which of the two
matches the on-screen rendering is not decidable from the files (both parsers use W as the full
width). Recommended: `width_pt = W × 72/132`, and note the ±9 % uncertainty.

### 7.4 Conversion to Notability document coordinates (as in the PoC)

`scale = (72/132) × (NB_PAGE_WIDTH / page_width_pt)`; `x_nb = x × scale`, `y_nb = y × scale`
(both y-down), widths × scale. Add `#6` (x,y) to every point before scaling when non-empty.

---

## 8. Writing a stroke — byte-exact recipe (verified by both reference parsers)

1. **TPL image.** For a constant-width stroke with points `p0 … p_{2N}` (odd count; duplicate
   the last point if even), build
   `tpl\0 + u32(size) + b"vuA(v)A(S(uu))A(S(uuuu))vA(f)\0" + <H 2> + <I bits(W)> + <I N+1> + <H 0> + <H 1>×N + <I 1> + <I bits(x0)><I bits(y0)> + <I N> + N × 4×<I bits> + <H 1> + <I 0>`
   where `bits(f) = struct.unpack('<I', struct.pack('<f', f))[0]`; `size = 8 + 30 + 2 + 4 + (4 + 2(N+1)) + (4 + 8) + (4 + 16N) + 2 + 4`.
   For a per-point-width stroke the ribbon format is required, which means also emitting the
   CGPath panels (§2.2); untested on device — start with the flat format.
2. **Frame** it as in §4.3.
3. **Stroke message**, fields in ascending order:
   `0a 24 <uuid>` · `12 <varint len> <frame>` · (`18 01` only for ribbon) · `22 <len> <colour: 0d R 15 G 1d B 25 A, each fixed32, omit 0.0 components>` ·
   (`28 01` for highlighter, with A = 0.5) · `32 00` · `3a 0a 0a 08 08 <dev> 10 <u32 varint>` · `4a 00` ·
   `7a 08 08 <dev> 10 <u32 varint>` · `a2 01 00` · `a8 01 18`.
4. **Records**: metadata `0a 24 <uuid> 12 08 08 <dev> 10 <u32> 40 <ns varint> 48 <seq varint> 70 <85 2a = 5381> 80 01 18`,
   then `3a <varint len> <stroke message>`; each prefixed by its own length varint; append to the
   page file. (What else GoodNotes requires in `index.events.pb` for a new stroke is a container
   question — see the container doc; the two reference parsers need nothing else.)

## 9. Round-trip experiment — exactly what worked

`scratchpad/experiments/gn_stroke_roundtrip.py` (pure Python 3.11, no third-party modules;
numpy is installed but neither parser's core needs it):

1. Independent decoder (protobuf wire → `bv41` frame → TPL by format string) decoded the pen
   stroke (`6C008FF2…`, 9 points incl. `values[2]`, RGBA (0, 0.4784, 1, 1)) and the highlighter
   (`ED0FFAFD…`, 23 points, W = 24, RGBA (1, 1, 0, 0.5), `#5 = 1`).
2. `tpl_encode(fmt, values)` reproduced both TPL images **byte-identically** (asserted).
3. Re-framed each as one all-literal `bv41` block + `bv4$` (678 B and 289 B instead of the
   original 535 B / 264 B), rebuilt the stroke messages with every other field byte-for-byte,
   wrote page files containing only `[metadata record, stroke record]`, and produced
   `experiments/roundtrip_out.goodnotes` (all other ZIP members copied from Test5).
4. **goodparse** on the rebuilt file: 2 strokes, points/colour/kind identical to its decode of
   the original file (pen: 8 points = my 9 minus `values[2]`; highlighter: 11 points, width 2.0 —
   its known lossy path).
5. **parser-for-goodnotes** on the rebuilt file: 2 strokes, `points`, `color_hex`, `alpha`,
   `width` identical to its decode of the original (pen 8 pts `#007aff`; highlighter 23 pts
   `#ffff00` α 0.5 width 24.0 `is_highlighter=True`, pressures 12.0 = W/2).
6. Informational `bv4-` file (`roundtrip_out_bv4raw.goodnotes`, 8-byte header): goodparse
   identical to (4); parser-for-goodnotes 0 strokes (§4.4).

Not verified (no device here): that GoodNotes itself opens a file containing an all-literal
`bv41` block or a hand-written page — the format is standard LZ4 so this is low-risk, but it is
the first thing to test on the iPad, together with whether a newly written stroke needs an
event-log entry.

## 10. Open questions

* Tool 25 identity (nib-angle pen, 30°/60° constants) and the meaning of the uint32 seed per
  segment; the flags-4/5 / command-6 ribbon variant (chisel marker?).
* `#9` (shape hints?), `#14`, `#20`, metadata `#14 = 5381`.
* Whether `W × 72/132` or `W/2 pt` is the on-screen highlighter width (§7.3).
* Whether GoodNotes writes `bv4-` blocks or multi-block streams for large strokes (largest
  observed single block 30 520 B).
* Whether the ribbon format's CGPath pools are required by GoodNotes or regenerated on load.

## Critic additions

Checked 2026-10-02 against the same nine samples (scripts: `scratchpad/critic/clocks.py`,
`midpoint.py`, `quadcheck.py`). Four statements above are corrected by evidence:

1. **Metadata `#8` is the device id, not a creation timestamp** (§1.1). It has exactly one
   value per file in all 9 files (Test5: `1606161793633522096` on all 49 elements, drawn
   2026-06 → 2026-09; ex1: `4391726093147120022` on 2810 elements) and that value is one of the
   varints in the event log's `#13/#14/#15` device slots (9/9). The "GoodNotes 5, Nov 2020" dating
   of the goodparse samples in the evidence table came from reading `#8` as nanoseconds and is
   wrong; the container doc's dates (2024-11 and 2026) are right. Confidence: high.
2. **The clock registers are `{#1 version counter, #2 random nonce}`, not `{device, seq}`**
   (§1.1 `#2`, §1.2 `#7`/`#15`, §8 step 3/4). `#2` is distinct for every one of the 5 677
   elements (5 677 distinct values, max 4 294 845 840 → uniform uint32); `#1` is 2 on untouched
   strokes in every file (minimum observed is 2, never 1), 3–17 on edited/moved ones. A writer
   must emit `08 02 10 <random u32 varint>` (or version 1 as inkterop does — accepted by GoodNotes
   Mac 6), identical in metadata `#2` and stroke `#15`; **never put the 63-bit device id there**.
   Stroke `#7.#1` is `{#1 small int, #2 nonce}` where `#1` is the per-page draw-order index
   (Test4: 1–4, Test5 page 1: 1–15; inkterop confirmed on a calibration page), absent on one
   element per page.
3. **Flat format `A(S(uuuu))` = quadratic Bézier segments, not pairs of on-curve points** (§3,
   §5). Over all 3 972 flat strokes with geometry (43 206 segments) the deviation of `(cx,cy)` from
   the midpoint of `prev_end → (ex,ey)` has median 0.09, p99 1.25, max 11.4 canvas units — so they
   are not midpoints — and GoodNotes' own Test5.pdf export draws each segment as the cubic
   `c1 = P0 + 2/3·(C − P0)`, `c2 = P1 + 2/3·(C − P1)` with `C = (cx,cy)`, `P1 = (ex,ey)`: 9/9
   matched strokes on page 3, 1 017 segments, max error 0.0002 pt (two strokes differ in segment
   count by 1–3 degenerate segments). Consequences: a reader produces an exact Notability cubic
   chain by degree elevation (no Catmull-Rom needed for this format); a writer from a polyline
   puts the control at the midpoint (inkref's "quadTo with control at midpoint" is exactly this);
   a writer from Notability cubics must approximate each cubic by one or more quadratics (e.g.
   `C = (3(c1 + c2) − P0 − P1)/4` for a single quad, split when the error exceeds ~0.3 units).
   Both reference parsers (and the PoC via goodparse) treat `(cx,cy)` as an on-curve point, which
   is only a ~0.1-unit error for app-written strokes but wrong in principle. Confidence: high.
4. **Width of flat strokes and shapes on the page is `W/2` points, not `W × 72/132`** (§7.3).
   Every stroked path in GoodNotes' Test5.pdf export has line width exactly `W/2`: 24 → 12.0,
   36 → 18.0, 14.064 → 7.032, 11.433 → 5.7165, 2.858 → 1.4291, ellipse `#9.#15` 5.4567 → 2.7283
   (page 3, 9 strokes + 1 shape, all matched); inkref calibrated the same factor against the
   Mac app ("2 units per point, spread 0.0000 over a 48× range"). So for the flat/shape family
   `W` is in **1/144 in** while coordinates are in **1/132 in**; the ribbon family's `r` remains
   `× 72/132` (cap-arc radii in the same export). Writer: `W = 2 × width_pt`. The tool-25
   (pencil) stroke `W = 1.559` is exported with line width 3.8976 pt (= 2.5 × W); its unit is
   unknown (1 data point). Confidence: high for flat/shape, low for pencil.
5. **`#3 = 1` elements are empty**: all 1 636 of them (ex1 1 313, ex3 281, record 37, ex2 5,
   test 1) decompress to the 62-byte empty TPL header (no points), carry `#14 = 1`, have UUIDs
   that occur nowhere else in the file, and all but 4 have an empty `#9`. Whether GoodNotes calls
   them "erased" (inkref, parser-for-goodnotes) or "re-records" (inkterop) is immaterial to a
   reader: skip every element whose geometry has no points. Never emit `#3`/`#14`.
6. **Tool identity** (§10): inkterop's iPad calibration page (one row per tool, GoodNotes 7.1.2,
   2026-07-10) maps stroke `#3` absent = ball pen, `#3 = 1` = fountain / brush / marker (ribbon
   geometry), `#3 = 5` (`#21 = 25`, the 11-float layout with the π/6, π/3 constants) = **pencil**
   with real tilt data, `#5 = 1` = highlighter, `#20 = {#1 ""}` = marker. Medium-high confidence
   (third-party, consistent with every field here). Pencil strokes force `schema.pb = 25`; a
   writer should map Notability pencils (`curvesstyles 5`) to the ball-pen flat format instead.
