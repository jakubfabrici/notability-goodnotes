"""Builder for NSKeyedArchiver / GLKeyedArchiver object graphs (the write side).

Byte layout produced (``docs/notability-format.md`` Part 2).  The result of
:meth:`ArchiveBuilder.dumps` is a binary plist (``bplist00``) of the shape::

    {'$version': 100000,
     '$archiver': 'GLKeyedArchiver' | 'NSKeyedArchiver',
     '$top': {'$0': UID(1)} | {'root': UID(1)},
     '$objects': ['$null', <root dict>, ...]}

* ``$objects[0]`` is always the string ``'$null'``; :attr:`ArchiveBuilder.NULL` (``UID(0)``)
  encodes nil.
* Every object added through :meth:`add` occupies one slot and is referenced by a
  ``plistlib.UID``.  Scalars (ints, floats, bools, bytes) may be stored inline in a dict
  instead; both forms are accepted by the apps and the writer mirrors what the Notability
  10.4 template does (strings and container objects by UID, numbers inline except where the
  template uses UIDs).
* Plain ``str`` objects are interned: equal strings share one UID, as NSKeyedArchiver does.
* Class registration: ``{'$classname': name, '$classes': [name, ..., 'NSObject']}`` stored
  once per class and referenced from each instance's ``'$class'`` key.  ``$classes`` may be
  omitted (the template writes ``InkedSpatialHash`` that way).
* ``NSArray`` / ``NSMutableArray``: ``{'NS.objects': [...], '$class': ...}``;
  ``NSDictionary`` / ``NSMutableDictionary``: parallel ``NS.keys`` / ``NS.objects`` lists
  (keys are UIDs of interned strings or ints).
* ``NSMutableString``: ``{'NS.bytes': b'utf-8'}`` (Session.plist) or ``{'NS.string': str}``
  (metadata.plist); ``NSDate``: ``{'NS.time': seconds since 2001-01-01 UTC}``;
  ``UIColor``: ``{UIColorComponentCount: 4, UIRed, UIGreen, UIBlue, UIAlpha, NSColorSpace: 2,
  NSRGB: b'r g b[ a]'}``.
* Geometry values are strings: ``'{x, y}'``, ``'{{x, y}, {w, h}}'``, ``'r,g,b,a'``.

Only the standard library is used (``plistlib``).
"""
from __future__ import annotations

import plistlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "ArchiveBuilder", "APPLE_EPOCH_OFFSET", "INT64_MAX",
    "point_string", "size_string", "rect_string", "range_string", "color_string", "nsrgb_bytes",
]

# Seconds between the Unix epoch and 2001-01-01T00:00:00Z (NSDate reference date).
APPLE_EPOCH_OFFSET = 978307200.0
# Marker of a blank (non-PDF) page in ``pageLayoutArray``.
INT64_MAX = 9223372036854775807

UID = plistlib.UID

# Inheritance chains of every class the writer emits, as app-written files register them.
CLASS_CHAINS: Dict[str, Optional[List[str]]] = {
    "NoteTakingSession": ["NoteTakingSession", "NSObject"],
    "FormattedString": ["FormattedString", "NSObject"],
    "HandwritingObject": ["HandwritingObject", "NSObject"],
    "InkedSpatialHash": None,  # the 10.4 template registers it without $classes
    "NBReflowStateLocked": ["NBReflowStateLocked", "NBReflowState", "NSObject"],
    "NBReflowStateReflowable": ["NBReflowStateReflowable", "NBReflowState", "NSObject"],
    "NBAttributedString": ["NBAttributedString", "NSObject"],
    "NBCPEventManager": ["NBCPEventManager", "NSObject"],
    "PDFFile": ["PDFFile", "NSObject"],
    "ImageMediaObject": ["ImageMediaObject", "GenericMediaObject", "NSObject"],
    "TextBlockMediaObject": ["TextBlockMediaObject", "CanvasMediaObject", "GenericMediaObject", "NSObject"],
    "Figure": ["Figure", "NSObject"],
    "ImageObject": ["ImageObject", "DrawObject", "NSObject"],
    "GLSnapshot": ["GLSnapshot", "NSObject"],
    "Notability.NBPaperStyle": ["Notability.NBPaperStyle", "NSObject"],
    "SessionInfo": ["SessionInfo", "NSObject"],
    "UIColor": ["UIColor", "NSObject"],
    "NSDate": ["NSDate", "NSObject"],
    "NSArray": ["NSArray", "NSObject"],
    "NSMutableArray": ["NSMutableArray", "NSArray", "NSObject"],
    "NSDictionary": ["NSDictionary", "NSObject"],
    "NSMutableDictionary": ["NSMutableDictionary", "NSDictionary", "NSObject"],
    "NSMutableString": ["NSMutableString", "NSString", "NSObject"],
}


def _num(value: float) -> str:
    """Format a number the way Cocoa's ``NSStringFromCGPoint`` does (no trailing zeros)."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def point_string(x: float, y: float) -> str:
    return "{" + _num(x) + ", " + _num(y) + "}"


def size_string(w: float, h: float) -> str:
    return point_string(w, h)


def rect_string(x: float, y: float, w: float, h: float) -> str:
    return "{" + point_string(x, y) + ", " + point_string(w, h) + "}"


def range_string(location: int, length: int) -> str:
    return "{" + str(int(location)) + ", " + str(int(length)) + "}"


def color_string(r: float, g: float, b: float, a: float) -> str:
    """``'r,g,b,a'`` with 15 decimals (the ``...CrossPlatform`` keys)."""
    return ",".join(f"{float(c):.15f}" for c in (r, g, b, a))


def nsrgb_bytes(r: float, g: float, b: float, a: float) -> bytes:
    """``NSRGB`` payload: ``b'r g b'`` with a fourth component only when alpha != 1."""
    def fmt(c: float) -> str:
        text = f"{float(c):.3f}".rstrip("0").rstrip(".")
        return text if text else "0"
    parts = [fmt(r), fmt(g), fmt(b)]
    if abs(a - 1.0) > 1e-9:
        parts.append(fmt(a))
    return " ".join(parts).encode("ascii")


class ArchiveBuilder:
    """Accumulates ``$objects`` and produces the keyed-archive plist."""

    NULL = UID(0)

    def __init__(self, archiver: str = "GLKeyedArchiver", top_key: str = "$0"):
        self.archiver = archiver
        self.top_key = top_key
        self.objects: List[Any] = ["$null"]
        self._classes: Dict[str, UID] = {}
        self._strings: Dict[str, UID] = {}
        self._root: Optional[UID] = None

    # -- primitives ---------------------------------------------------------------------

    def add(self, obj: Any) -> UID:
        """Append ``obj`` to ``$objects`` and return its UID."""
        self.objects.append(obj)
        return UID(len(self.objects) - 1)

    def reserve(self) -> UID:
        """Reserve a slot (filled later with :meth:`fill`) so the root can be ``$objects[1]``."""
        return self.add(None)

    def fill(self, uid: UID, obj: Any) -> UID:
        self.objects[uid.data] = obj
        return uid

    def set_root(self, uid: UID) -> None:
        self._root = uid

    def cls(self, name: str, chain: Optional[Sequence[str]] = None) -> UID:
        """UID of the class registration dict for ``name`` (created on first use)."""
        if name in self._classes:
            return self._classes[name]
        if chain is None and name in CLASS_CHAINS:
            chain = CLASS_CHAINS[name]
        entry: Dict[str, Any] = {"$classname": name}
        if chain is not None:
            entry["$classes"] = list(chain)
        uid = self.add(entry)
        self._classes[name] = uid
        return uid

    def string(self, text: str) -> UID:
        """Interned plain string object."""
        if text in self._strings:
            return self._strings[text]
        uid = self.add(text)
        self._strings[text] = uid
        return uid

    def mutable_string(self, text: str, as_bytes: bool = True) -> UID:
        """``NSMutableString`` in the ``NS.bytes`` (default) or ``NS.string`` encoding."""
        payload: Dict[str, Any] = {"NS.bytes": text.encode("utf-8")} if as_bytes else {"NS.string": text}
        payload["$class"] = self.cls("NSMutableString")
        return self.add(payload)

    def object(self, classname: str, fields: Dict[str, Any], uid: Optional[UID] = None) -> UID:
        """A class instance: ``fields`` plus ``$class``.  ``uid`` fills a reserved slot."""
        payload = dict(fields)
        payload["$class"] = self.cls(classname)
        if uid is not None:
            return self.fill(uid, payload)
        return self.add(payload)

    # -- Foundation containers -----------------------------------------------------------

    def array(self, items: Iterable[Any], mutable: bool = False) -> UID:
        return self.object("NSMutableArray" if mutable else "NSArray", {"NS.objects": list(items)})

    def dictionary(self, pairs: Iterable[Tuple[Any, Any]], mutable: bool = True) -> UID:
        """``NS.keys`` / ``NS.objects`` dictionary.  ``str`` keys are interned, ints added."""
        keys: List[Any] = []
        values: List[Any] = []
        for key, value in pairs:
            if isinstance(key, str):
                keys.append(self.string(key))
            elif isinstance(key, UID):
                keys.append(key)
            else:
                keys.append(self.add(key))
            values.append(value)
        return self.object("NSMutableDictionary" if mutable else "NSDictionary",
                           {"NS.keys": keys, "NS.objects": values})

    def date(self, unix_seconds: float) -> UID:
        return self.object("NSDate", {"NS.time": float(unix_seconds) - APPLE_EPOCH_OFFSET})

    def uicolor(self, r: float, g: float, b: float, a: float = 1.0) -> UID:
        return self.object("UIColor", {
            "UIColorComponentCount": 4,
            "UIRed": float(r), "UIGreen": float(g), "UIBlue": float(b), "UIAlpha": float(a),
            "NSColorSpace": 2,
            "NSRGB": nsrgb_bytes(r, g, b, a),
        })

    # -- output --------------------------------------------------------------------------

    def plist(self) -> Dict[str, Any]:
        if self._root is None:
            raise ValueError("archive root not set")
        return {
            "$version": 100000,
            "$archiver": self.archiver,
            "$top": {self.top_key: self._root},
            "$objects": list(self.objects),
        }

    def dumps(self) -> bytes:
        return plistlib.dumps(self.plist(), fmt=plistlib.FMT_BINARY)
