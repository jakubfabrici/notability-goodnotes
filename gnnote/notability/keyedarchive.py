"""Read helpers for the NSKeyedArchiver / GLKeyedArchiver object graphs inside a ``.note``.

Byte layout (see ``docs/notability-format.md`` Part 2).  ``Session.plist`` and
``metadata.plist`` are binary plists (``bplist00``) holding a keyed archive::

    {'$version': 100000,
     '$archiver': 'GLKeyedArchiver' | 'NSKeyedArchiver',
     '$top': {'$0': UID(1)} | {'root': UID(1)},
     '$objects': ['$null', <root dict>, ...]}

* Any value may be a ``plistlib.UID`` pointing into ``$objects`` or an inline scalar
  (GLKeyedArchiver inlines ints, floats, bools, bytes and short strings).  :meth:`Archive.deref`
  follows UID chains until a non-UID value appears.
* ``$objects[0]`` is the string ``'$null'`` and means nil.
* Class tags: ``'$class': UID -> {'$classname': str, '$classes': [...]}``; ``$classes`` may be
  missing and Swift classes carry a module prefix (``Notability.MathMediaObject``).
* ``NSArray`` is either ``{'NS.objects': [...]}`` or ``{'NS.object.0': .., 'NS.object.1': ..}``
  (indexed keys are not necessarily stored in order).  ``NSDictionary`` is either the parallel
  ``NS.keys`` / ``NS.objects`` lists or ``NS.key.N`` / ``NS.object.N`` pairs.  Both encodings occur
  in files of the same format version, so every container access goes through
  :meth:`Archive.array` / :meth:`Archive.dictionary`.
* Strings are plain ``str``, ``NSMutableString`` ``{'NS.bytes': b'utf-8'}`` or ``{'NS.string': ..}``.
* Geometry is stored as strings: ``'{x, y}'`` points / sizes, ``'{{x, y}, {w, h}}'`` rects,
  ``'{location, length}'`` ranges; colours also as ``'r,g,b,a'`` strings.
* ``NSDate`` = ``{'NS.time': seconds since 2001-01-01 UTC}``, ``NSUUID`` = ``{'NS.uuidbytes': 16 bytes}``,
  ``UIColor`` = ``{UIColorComponentCount: 4, UIRed, UIGreen, UIBlue, UIAlpha, ...}`` (or the two
  component greyscale form with ``UIWhite``).
* Grouped ink (Notability 16) stores complete nested keyed archives as ``bytes`` values inside
  plain (non-keyed) plists; :func:`load_archive` works for those too.

Everything here is tolerant: malformed containers yield empty results instead of raising.
"""
from __future__ import annotations

import plistlib
import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "Archive", "load_archive", "is_null", "parse_point", "parse_rect", "parse_range",
    "parse_color_string", "color_from_uicolor", "APPLE_EPOCH_OFFSET",
]

# Seconds between the Unix epoch and 2001-01-01T00:00:00Z (NSDate reference date).
APPLE_EPOCH_OFFSET = 978307200.0

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_POINT_RE = re.compile(r"^\s*\{\s*(" + _NUMBER + r")\s*,\s*(" + _NUMBER + r")\s*\}\s*$")
_RECT_RE = re.compile(
    r"^\s*\{\s*\{\s*(" + _NUMBER + r")\s*,\s*(" + _NUMBER + r")\s*\}\s*,\s*\{\s*("
    + _NUMBER + r")\s*,\s*(" + _NUMBER + r")\s*\}\s*\}\s*$"
)
_INDEXED_RE = re.compile(r"^NS\.(object|key)\.(\d+)$")


def is_null(value: Any) -> bool:
    """True for ``None`` and the archive's ``'$null'`` marker."""
    return value is None or (isinstance(value, str) and value == "$null")


class Archive:
    """UID resolver over the output of :func:`plistlib.loads` for one keyed archive."""

    def __init__(self, plist: Dict[str, Any]):
        objs = plist.get("$objects") if isinstance(plist, dict) else None
        self.objs: List[Any] = list(objs) if isinstance(objs, list) else []
        self.archiver: str = str(plist.get("$archiver", "")) if isinstance(plist, dict) else ""
        top = plist.get("$top") if isinstance(plist, dict) else None
        root_ref: Any = None
        if isinstance(top, dict):
            if "root" in top:
                root_ref = top["root"]
            elif "$0" in top:
                root_ref = top["$0"]
            elif top:
                root_ref = next(iter(top.values()))
        self.root: Any = self.deref(root_ref)

    # -- resolution -------------------------------------------------------------------

    def deref(self, value: Any) -> Any:
        """Follow UID references; returns the stored object (may be ``'$null'``)."""
        seen = 0
        while isinstance(value, plistlib.UID):
            index = value.data
            if index < 0 or index >= len(self.objs) or seen > 64:
                return None
            value = self.objs[index]
            seen += 1
        return value

    def classname(self, obj: Any) -> Optional[str]:
        obj = self.deref(obj)
        if not isinstance(obj, dict):
            return None
        cls = self.deref(obj.get("$class"))
        if isinstance(cls, dict):
            name = self.deref(cls.get("$classname"))
            return name if isinstance(name, str) else None
        return None

    def classes(self, obj: Any) -> List[str]:
        """The ``$classes`` inheritance chain (may be empty when the archive omits it)."""
        obj = self.deref(obj)
        if not isinstance(obj, dict):
            return []
        cls = self.deref(obj.get("$class"))
        if isinstance(cls, dict):
            chain = self.deref(cls.get("$classes"))
            if isinstance(chain, list):
                return [c for c in (self.deref(x) for x in chain) if isinstance(c, str)]
            name = self.deref(cls.get("$classname"))
            return [name] if isinstance(name, str) else []
        return []

    def get(self, obj: Any, key: str, default: Any = None) -> Any:
        """``obj[key]`` dereferenced, ``default`` when absent or nil."""
        obj = self.deref(obj)
        if not isinstance(obj, dict) or key not in obj:
            return default
        value = self.deref(obj[key])
        return default if is_null(value) else value

    # -- scalars ----------------------------------------------------------------------

    def string(self, value: Any) -> Optional[str]:
        value = self.deref(value)
        if isinstance(value, dict):
            if "NS.bytes" in value:
                raw = self.deref(value["NS.bytes"])
                if isinstance(raw, (bytes, bytearray)):
                    return bytes(raw).decode("utf-8", "replace")
                if isinstance(raw, str):
                    return raw
                return None
            if "NS.string" in value:
                return self.string(value["NS.string"])
            return None
        if isinstance(value, str):
            return None if value == "$null" else value
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", "replace")
        return None

    def number(self, value: Any, default: float = 0.0) -> float:
        value = self.deref(value)
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return default
        return default

    def integer(self, value: Any, default: int = 0) -> int:
        value = self.deref(value)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value == value:
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                return default
        return default

    def boolean(self, value: Any, default: bool = False) -> bool:
        value = self.deref(value)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    def data(self, value: Any) -> bytes:
        """``bytes`` content or ``b''`` for anything else."""
        value = self.deref(value)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return b""

    def date(self, value: Any) -> Optional[float]:
        """NSDate as Unix seconds."""
        value = self.deref(value)
        if isinstance(value, dict) and "NS.time" in value:
            return self.number(value["NS.time"]) + APPLE_EPOCH_OFFSET
        return None

    # -- containers -------------------------------------------------------------------

    def array(self, value: Any) -> List[Any]:
        """Elements of an NSArray in either encoding, each dereferenced."""
        value = self.deref(value)
        if is_null(value):
            return []
        if isinstance(value, list):
            return [self.deref(x) for x in value]
        if not isinstance(value, dict):
            return []
        if "NS.objects" in value:
            items = self.deref(value["NS.objects"])
            return [self.deref(x) for x in items] if isinstance(items, list) else []
        return [self.deref(x) for x in self._indexed(value, "object")]

    def dictionary(self, value: Any) -> Dict[str, Any]:
        """Entries of an NSDictionary in either encoding, keys as ``str``, values dereferenced."""
        value = self.deref(value)
        if is_null(value) or not isinstance(value, dict):
            return {}
        out: Dict[str, Any] = {}
        if "NS.keys" in value:
            keys = self.deref(value["NS.keys"])
            vals = self.deref(value.get("NS.objects"))
            if isinstance(keys, list) and isinstance(vals, list):
                for k, v in zip(keys, vals):
                    name = self._key_name(k)
                    if name is not None:
                        out[name] = self.deref(v)
            return out
        keys = self._indexed(value, "key")
        vals = self._indexed(value, "object")
        if keys:
            for k, v in zip(keys, vals):
                name = self._key_name(k)
                if name is not None:
                    out[name] = self.deref(v)
            return out
        # A plain dict without NS.* wrapping (plistlib output of a non-keyed plist).
        if "$class" not in value:
            for k, v in value.items():
                if isinstance(k, str):
                    out[k] = self.deref(v)
        return out

    def _indexed(self, value: Dict[str, Any], kind: str) -> List[Any]:
        found: List[Tuple[int, Any]] = []
        for k, v in value.items():
            if isinstance(k, str):
                m = _INDEXED_RE.match(k)
                if m and m.group(1) == kind:
                    found.append((int(m.group(2)), v))
        found.sort(key=lambda kv: kv[0])
        return [v for _, v in found]

    def _key_name(self, key: Any) -> Optional[str]:
        name = self.string(key)
        if name is not None:
            return name
        key = self.deref(key)
        if isinstance(key, (int, float)) and not isinstance(key, bool):
            return str(key)
        return None


def load_archive(data: bytes) -> Archive:
    """Parse a binary/XML plist that holds a keyed archive (``Session.plist``, nested group archives)."""
    plist = plistlib.loads(bytes(data))
    if not isinstance(plist, dict):
        raise ValueError("keyed archive root is not a dictionary")
    return Archive(plist)


# -- geometry strings ---------------------------------------------------------------------


def parse_point(text: Any) -> Optional[Tuple[float, float]]:
    """``'{x, y}'`` (also used for sizes) -> ``(x, y)``; ``None`` when unparsable."""
    if not isinstance(text, str):
        return None
    m = _POINT_RE.match(text)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def parse_rect(text: Any) -> Optional[Tuple[float, float, float, float]]:
    """``'{{x, y}, {w, h}}'`` -> ``(x, y, w, h)``."""
    if not isinstance(text, str):
        return None
    m = _RECT_RE.match(text)
    if not m:
        return None
    try:
        return tuple(float(m.group(i)) for i in range(1, 5))  # type: ignore[return-value]
    except ValueError:
        return None


def parse_range(text: Any) -> Optional[Tuple[int, int]]:
    """``'{location, length}'`` -> ``(location, length)``."""
    point = parse_point(text)
    if point is None:
        return None
    return int(point[0]), int(point[1])


def parse_color_string(text: Any) -> Optional[Tuple[float, float, float, float]]:
    """``'r,g,b,a'`` cross-platform colour string -> RGBA floats."""
    if not isinstance(text, str):
        return None
    parts = text.split(",")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    if len(values) == 4:
        return tuple(max(0.0, min(1.0, v)) for v in values)  # type: ignore[return-value]
    if len(values) == 3:
        return (values[0], values[1], values[2], 1.0)
    if len(values) == 2:
        return (values[0], values[0], values[0], values[1])
    return None


def color_from_uicolor(archive: Archive, value: Any) -> Optional[Tuple[float, float, float, float]]:
    """``UIColor`` / ``NSColor`` archive dict -> RGBA floats (``None`` when unknown)."""
    value = archive.deref(value)
    if not isinstance(value, dict):
        return None
    if "UIRed" in value or "UIGreen" in value or "UIBlue" in value:
        return (
            archive.number(value.get("UIRed")),
            archive.number(value.get("UIGreen")),
            archive.number(value.get("UIBlue")),
            archive.number(value.get("UIAlpha"), 1.0),
        )
    if "UIWhite" in value:
        w = archive.number(value.get("UIWhite"))
        return (w, w, w, archive.number(value.get("UIAlpha"), 1.0))
    rgb = archive.deref(value.get("NSRGB"))
    if isinstance(rgb, (bytes, bytearray)):
        parts = bytes(rgb).rstrip(b"\x00").decode("ascii", "replace").split()
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            nums = []
        if len(nums) >= 3:
            alpha = nums[3] if len(nums) > 3 else 1.0
            return (nums[0], nums[1], nums[2], alpha)
    white = archive.deref(value.get("NSWhite"))
    if isinstance(white, (bytes, bytearray)):
        parts = bytes(white).rstrip(b"\x00").decode("ascii", "replace").split()
        try:
            w = float(parts[0])
            alpha = float(parts[1]) if len(parts) > 1 else 1.0
            return (w, w, w, alpha)
        except (ValueError, IndexError):
            return None
    return None
