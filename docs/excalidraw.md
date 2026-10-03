# Excalidraw (`.excalidraw`): format facts and gnnote mapping

Codec: `gnnote/excalidraw/` (`reader.read_excalidraw`, `writer.write_excalidraw`), registry id
`excalidraw`, extension `.excalidraw`.

Excalidraw is an MIT-licensed whiteboard (excalidraw.com, the ExcalidrawZ iPad app, the
Obsidian plugin). Facts are from its JSON schema documentation
(`dev-docs/docs/codebase/json-schema.mdx`), `packages/element/src/types.ts` and
`packages/excalidraw/data/restore.ts` at `ed10ac7` (2026-10-01). The freedraw width law and the
element field set come from inkterop's MIT Excalidraw codec, whose writer loaded through the
official `@excalidraw/excalidraw` 0.18.0 package's file-open path (`loadFromBlob`) with no
element dropped and rendered like the source after its width fix (inkterop
`docs/validated-writes.md`). inkterop's CC0 fixture `scribble.excalidraw` is a test sample.

## 1. Scene [verified]

UTF-8 JSON `{"type": "excalidraw", "version": 2, "source", "elements": [...], "appState":
{...}, "files": {fileId: {mimeType, id, dataURL, created, lastRetrieved}}}` (clipboard data
uses `"type": "excalidraw/clipboard"`, also read). One infinite canvas in CSS pixels, y down.
Common element fields: `id, type, x, y, width, height, angle` (radians, clockwise about the
element's centre), `strokeColor, backgroundColor` (`#rrggbb`, `transparent`), `fillStyle`
(`solid`, `hachure`, `cross-hatch`, `zigzag`), `strokeWidth, strokeStyle` (`solid`, `dashed`,
`dotted`), `roughness, opacity` (0..100), `groupIds, frameId, roundness, seed, version,
versionNonce, isDeleted, boundElements, updated, link, locked`, optional `customData`.
`freedraw` adds `points` (relative to `x, y`), `pressures`, `simulatePressure`;
`line` / `arrow` `points`, arrowheads and bindings (`polygon` for closed lines); `text` adds
`text` (with soft line breaks), `originalText`, `fontSize`, `fontFamily` (1 Virgil,
2 Helvetica, 3 Cascadia, 5 Excalifont, 6 Nunito, 7 Lilita One, 8 Comic Shanns, 9 Liberation
Sans), `textAlign`, `verticalAlign`, `containerId`, `autoResize`, `lineHeight`; `image` adds
`fileId`, `status`, `scale`, `crop`; `frame` adds `name`. `restore()` fills in defaults for
missing fields and keeps unknown ones.

**Freedraw width law [verified by inkterop against 0.18.0].** Excalidraw draws freedraw with
perfect-freehand, size `strokeWidth * 4.25`, thinning 0.6, easing `sin(t * pi / 2)`:
thickness = `strokeWidth * 8.5 * sin(pi / 2 * (0.5 + 0.6 * (p - 0.5)))`, i.e. 8.08 x at
pressure 1, 6.01 x at 0.5, 2.63 x at 0, and about 6.9 x for speed-simulated pressure (measured at
a uniform speed). `line`, `arrow` and shapes are stroked 1:1 with `strokeWidth`.

**Units [inferred].** CSS px; gnnote uses 1 px = 0.75 pt (96 px per inch), so a 612 pt page is
816 px wide.

## 2. Reading (`reader.py`)

* **Pages**: every `frame` / `magicframe` is a page of its size, ordered top to bottom, then left
  to right; it holds the elements whose `frameId` names it, or, without a `frameId`, whose
  bounding-box centre lies inside it. Elements outside every frame, or all elements of a scene
  without frames, form one more page: their bounding box plus a 20 px margin (an empty scene: one
  A4 page). Coordinates are relative to the page's top-left corner, x 0.75.
* **freedraw** -> polyline `Stroke`: `angle` applied about the centre of the points' bounding
  box; widths by the law (per pressure; simulated: 6.9 x `strokeWidth`; no pressures and no
  simulation: 6.01 x); colour = `strokeColor` with `opacity` (and an 8-digit colour's alpha).
  `customData.gnnote.kind` / `.pen` (written by gnnote) restore highlighters and pen names.
* **line / arrow** -> polyline of width `strokeWidth`; arrowheads dropped (warning). A closed
  line (`polygon`, or first point = last point) with a `backgroundColor` adds a shape fill.
* **rectangle / diamond / stickynote / ellipse** -> outline strokes with exact cubic handles
  (straight sides; four arcs for the ellipse); a `backgroundColor` adds a
  `Stroke(kind="fill")` (ellipse sampled at 64 points) right after the outline; a transparent
  stroke colour leaves the fill alone. Hatched / zigzag fills become solid, dashes solid, rough
  (hand-drawn) and rounded geometry clean (one warning each).
* **text** -> `TextBox` with `originalText` (the unwrapped text), size x 0.75, colour, the
  font's name, `textAlign`; Excalidraw rotates text about its centre, the model about the
  top-left corner, so the corner is moved to where Excalidraw's rotation puts it.
* **image** -> `Image` from the data URL (PNG / JPEG; SVG, GIF, WebP and missing files are
  skipped, warning), box and `angle`; crops and flips are not applied (warning).
* `appState.viewBackgroundColor` other than white -> a generated paper PDF of that colour.
* Deleted elements are ignored; embeds and unknown types are dropped (warning).
* **Hardening**: at most 256 MB of JSON (`ValueError` beyond; JSON nested too deeply is a
  `ValueError` too), 500 000 elements, 200 000 points per element, 5 000 000 points in all,
  256 MB per decoded image and 1 GB for all images; coordinates beyond 10^7 px or not finite
  are skipped. Only `ValueError` leaves `read_excalidraw`.

## 3. Writing (`writer.py`)

* JSON with two-space indent (like Excalidraw's export), `source` = `gnnote <version>`,
  `appState = {gridSize 20, viewBackgroundColor #ffffff}`.
* Pages become frames named `Page N`, stacked top to bottom with 80 px between them; each page's
  elements carry its `frameId` and follow its frame in the element list.
* Ink -> `freedraw` (Bezier chains flattened to about 1 pt): `x, y` = first point, `points`
  relative, `strokeWidth` = widest width / 8.08 and per-point `pressures` through the inverse
  law, so Excalidraw draws the model's widths; one freedraw spans widths 1 : 3.08, thinner points
  are widened (warning). `simulatePressure` false, `lastCommittedPoint` the last point, opacity =
  alpha x 100, roughness 0. A one-point stroke gets a second point 0.0001 px away, as Excalidraw
  stores its own dots. Highlighters and pen names go to `customData.gnnote`.
* Shape fills -> closed `line` elements (`polygon` true, first point repeated) with the fill
  colour as `backgroundColor`, a transparent stroke and the fill's alpha as opacity.
* Images: PNG / JPEG -> `image` elements with `fileId` = SHA-1 of the bytes and a data URL in
  `files`; rotation as `angle`. PDF images are dropped (Excalidraw cannot show PDF; warning).
* Text boxes -> `text`: Helvetica (`fontFamily` 2) unless the model names one of Excalidraw's
  fonts; size / 0.75; colour and alignment kept; bold, italic, underline and mixed runs become
  one plain style (warning). A box whose longest line looks wider than the box (0.55 em per
  character) is word-wrapped into `text` with `autoResize` false and the unwrapped
  `originalText` (Excalidraw re-wraps it with real metrics when edited); otherwise `autoResize`
  is true. The model's top-left rotation pivot is moved to Excalidraw's centre pivot.
* PDF backgrounds (user PDFs; stock paper with `--paper pdf`) and ruled / grid / dotted paper are
  dropped (Excalidraw has neither; warning). Rasterising pages is out of scope.
* `id` (21 characters), `seed` and `versionNonce` are random; `Options.random_seed` makes them
  and the `updated` stamps reproducible.

## 4. Limitations and what to check in the app

* **Unverified in Excalidraw**: frames as pages (a standard element type, but not part of
  inkterop's checked output; frames clip their children), `customData`, closed `line`
  polygons as fills, the text wrap estimate, `lastCommittedPoint` on freedraw (dropped by
  newer Excalidraw versions, which ignore it), the `strokeOptions` field of current Excalidraw
  master (not written; `restore()` defaults it). The freedraw law was measured on 0.18.0; a
  later change of Excalidraw's freedraw rendering would change the drawn widths.
* No PDF, paper, rich text or highlighter tool in Excalidraw; hand-drawn roughness and
  arrowheads are not converted.

Open in excalidraw.com, ExcalidrawZ (iPad) and Obsidian: a converted GoodNotes notebook with
pressure ink, highlighter, images, text and shape fills; check the frames ("Page N"), the ink
widths, the highlighter translucency, image rotation and text placement.
