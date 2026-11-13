"""Samsung Notes ``.sdocx`` notes (Samsung Notes for Windows and for Galaxy phones and tablets):
read-only support.

* :mod:`.records` -- bounded little-endian reading of the S-Pen SDK records: frames,
  bitfields, strings, the delta-compressed stroke events, objects, pages, ``note.note``,
  ``pageIdInfo.dat``, ``media/mediaInfo.dat`` and ``end_tag.bin``
* :mod:`.reader`  -- maps a note onto :class:`gnnote.model.Document`: :func:`read_sdocx`

See ``docs/samsungnotes.md`` for the format, the mapping and what is not converted.
"""
from .reader import read_sdocx

__all__ = ["read_sdocx"]
