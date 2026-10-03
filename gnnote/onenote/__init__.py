"""Microsoft OneNote reader (OneNote -> any other format; writing OneNote is not supported).

* :mod:`.common`  -- bounded byte access, extended GUIDs, property sets ([MS-ONESTORE] 2.6)
* :mod:`.native`  -- the desktop revision store ([MS-ONESTORE] 2.1 - 2.6)
* :mod:`.package` -- the OneDrive "alternative packaging" ([MS-ONESTORE] 2.7 - 2.8, [MS-FSSHTTPB])
* :mod:`.schema`  -- object types and property IDs ([MS-ONE] plus the ink objects)
* :mod:`.ink`     -- ink strokes
* :mod:`.reader`  -- pages, text, pictures, notebook ZIPs: :func:`read_onenote`

See ``docs/onenote.md`` for what is converted and how to get the files from an iPad.
"""
from .reader import read_onenote

__all__ = ["read_onenote"]
