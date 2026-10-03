"""notesconverter (MIT) as a subprocess oracle for the Noteful tests.

The script below runs in its own interpreter with notesconverter's ``converter/`` directory
on ``PYTHONPATH``; it parses each file in notesconverter's strict mode (the equivalent of
``noteful.py --pedantic --check-reserialization``: every violated format assumption raises
and every record is re-serialised and compared with the original bytes) and prints a JSON
summary.  Nothing from notesconverter is imported into the test process or the package.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

NOTEFUL_SAMPLES = ("bookmarks", "dot-strokes", "empty", "handwriting", "image-insert", "pdf-pages",
                   "shapes", "text", "three-pages", "z-order")

ORACLE_SCRIPT = r"""
import json, sys
from noteful import (DecodeOptions, NotefulFile, PathPenStroke, PathStyle, PdfPageBackground,
                     decode_options)
from utils import assume_strict

assume_strict.set(True)
decode_options.set(DecodeOptions(check_reserialization=True))


def rgba(c):
    return None if c is None else [float(v) for v in c]


def summary(path):
    f = NotefulFile.from_bytes(open(path, "rb").read())
    pages = []
    for i, page in enumerate(f.pages):
        bg = page.background.value
        entry = {"uuid": page.uuid, "tag": page.ordering_tag.value,
                 "size": [bg.background_size.width, bg.background_size.height],
                 "background": "pdf" if isinstance(bg, PdfPageBackground) else "template",
                 "pdf": bg.pdf_uuid, "pdf_page": getattr(bg, "pdf_page", 0),
                 "files_on_page": list(page.resources.files_on_page), "ink": [], "objects": []}
        ann = f.get_annotations(i)
        if ann is not None:
            entry["version"] = ann.version
            style = None
            for item in ann.path.items:
                if isinstance(item, PathStyle):
                    style = item
                    continue
                entry["ink"].append({
                    "z": item.z_index, "variable": item.variable_width, "nominal": item.nominal_width,
                    "points": [list(p) for p in item.points], "rgba": rgba(style.rgba) if style else None,
                    "blend": int(style.blend_mode) if style else 0, "dash": int(style.dash_style) if style else 0,
                    "id": [item.unknown_word_02, item.unknown_int_04, item.unknown_word_08],
                    "times": [item.timestamp_0, item.timestamp_1]})
            for shape in ann.shapes.values():
                d = shape.data
                obj = {"uuid": shape.uuid, "z": shape.z_index.value, "type": int(d.shape_type),
                       "box": list(shape.bounding_box.value.points), "flip": shape.bounding_box.value.flip,
                       "size": [d.size.value.width, d.size.value.height], "version": d.version}
                if d.stroke is not None:
                    st = d.stroke.value
                    obj["stroke"] = {"rgba": rgba(st.rgba), "thickness": st.thickness, "dash": int(st.dash_style),
                                     "arrow": bool(st.arrow_head)}
                if d.background is not None:
                    obj["fill"] = {"rgba": rgba(d.background.value.rgba), "flag": d.background.value.unknown_0002_bool}
                if d.points is not None:
                    obj["points"] = [list(p) for p in d.points.value.points]
                    obj["commands"] = list(d.points.value.commands)
                if d.corner_radius is not None:
                    obj["corner"] = d.corner_radius.value
                if d.rich_text is not None:
                    rt = d.rich_text
                    obj["text"] = {"strings": rt.strings, "counts": rt.num_attr_per_chunk, "keys": rt.attr_key_list,
                                   "str": rt.str_list, "bool": rt.bool_list,
                                   "color": [rgba(c) for c in rt.color_list], "int": rt.int_list,
                                   "float": rt.float_list}
                if d.image_uuid is not None:
                    obj["image"] = {"uuid": d.image_uuid,
                                    "native": [d.image_native_size.width, d.image_native_size.height]}
                    if d.crop_rect is not None:
                        obj["image"]["crop"] = [list(p) for p in d.crop_rect.value.points]
                entry["objects"].append(obj)
        pages.append(entry)
    layers = [layer.name.value for layer in f.metadata_d.layers.values()]
    return {"title": f.title, "files": sorted(f.files), "annotations": sorted(f.annotations),
            "layers": layers, "bookmarks": len(f.metadata_d.bookmarks), "pages": pages}


out = {}
for path in sys.argv[1:]:
    try:
        out[path] = summary(path)
    except Exception as exc:
        out[path] = {"error": f"{type(exc).__name__}: {exc}"}
print(json.dumps(out))
"""


def converter_dir(samples: Any) -> Path:
    return samples.repo("notesconverter") / "converter"


def sample_files(samples: Any) -> List[Path]:
    """The 10 app-written ``.noteful`` samples (each with Noteful's own PDF export)."""
    folder = samples.repo("notesconverter") / "samples" / "noteful"
    files = [folder / f"{name}.noteful" for name in NOTEFUL_SAMPLES]
    missing = [f.name for f in files if not f.is_file()]
    if missing:
        pytest.skip(f"notesconverter samples missing: {', '.join(missing)}")
    return files


def run_oracle(samples: Any, paths: Sequence[Path]) -> Dict[str, Any]:
    """notesconverter's strict summary of every file, keyed by the path as given."""
    env = dict(os.environ, PYTHONPATH=str(converter_dir(samples)))
    proc = subprocess.run([sys.executable, "-c", ORACLE_SCRIPT, *map(str, paths)], env=env,
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        pytest.fail(f"notesconverter oracle failed:\n{proc.stderr}")
    return json.loads(proc.stdout)
