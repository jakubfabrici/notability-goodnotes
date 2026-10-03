"""Excalidraw codec: :func:`gnnote.excalidraw.reader.read_excalidraw` and
:func:`gnnote.excalidraw.writer.write_excalidraw` (``docs/excalidraw.md``).

An ``.excalidraw`` file is a JSON scene ``{"type": "excalidraw", "version": 2, "source",
"elements": [...], "appState": {...}, "files": {...}}`` on one infinite canvas in CSS pixels
(y down).  gnnote maps pages to Excalidraw *frames* stacked top to bottom.

Excalidraw is MIT-licensed; the element fields follow its JSON schema documentation and
``restore.ts``.  The freedraw width law below was measured by inkterop (MIT) against the
official ``@excalidraw/excalidraw`` 0.18.0 package; it is perfect-freehand's radius formula with
Excalidraw's options (size = strokeWidth x 4.25, thinning 0.6, easing sin(t * pi / 2)).
"""
from __future__ import annotations

import math
from typing import Dict

PT_PER_PX = 0.75  # CSS px (1/96 in) -> pt (1/72 in)
SIMULATED_FACTOR = 6.9  # rendered thickness / strokeWidth of speed-simulated freedraw (uniform speed)
PAGE_GAP_PX = 80.0  # vertical gap between stacked page frames
FONT_SIZE_PX = 20.0  # Excalidraw's default font size
LINE_HEIGHT = 1.25  # Excalidraw's line height for its sans-serif fonts

# fontFamily ids (Excalidraw's FONT_FAMILY) <-> names; 2 (Helvetica) is the plain sans-serif
FONT_NAMES: Dict[int, str] = {1: "Virgil", 2: "Helvetica", 3: "Cascadia", 5: "Excalifont", 6: "Nunito",
                              7: "Lilita One", 8: "Comic Shanns", 9: "Liberation Sans"}
DEFAULT_FONT_FAMILY = 2


def thickness_factor(pressure: float) -> float:
    """Rendered freedraw thickness / ``strokeWidth`` at ``pressure`` (0..1)."""
    p = min(1.0, max(0.0, pressure)) if math.isfinite(pressure) else 0.5
    return 8.5 * math.sin(math.pi / 2.0 * (0.5 + 0.6 * (p - 0.5)))


MAX_FACTOR = thickness_factor(1.0)  # 8.08
MIN_FACTOR = thickness_factor(0.0)  # 2.63: one freedraw spans widths 1 : 3.08


def pressure_for_factor(factor: float) -> float:
    """Inverse of :func:`thickness_factor`, clamped to the law's range (pressure 0..1)."""
    r = min(1.0, max(0.0, factor / 8.5))
    p = 0.5 + (2.0 / math.pi * math.asin(r) - 0.5) / 0.6
    return min(1.0, max(0.0, p))
