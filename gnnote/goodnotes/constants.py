"""Constants shared by the GoodNotes codec: field numbers, fixed UUIDs, byte constants.

Everything here is taken from ``docs/goodnotes-container.md``, ``docs/goodnotes-stroke.md``
and ``docs/goodnotes-elements.md`` (sample files written by GoodNotes 5/6, 2024-2026).

Layout facts encoded as constants:

* ``schema.pb`` is one varint field ``{#1 24}`` = bytes ``08 18`` (container doc section 3).
* Canvas units are PDF points times 132/72 (section 9.1); flat stroke widths are
  points times 2 (stroke doc, Critic addition 4).
* ``#30.#3`` / ``#30.#7`` of the document-created event carry one constant UUID in every
  sample (Critic addition 6 of the container doc); it is copied verbatim.
* ``#54.#17.#1`` of every page-created event is one 48-byte colour block (light grey +
  white RGBA) that is byte-identical in all 2025/2026 samples (section 10.3).
* ``thumbnail.jpg`` is any baseline JPEG; GoodNotes Mac 6 accepted a 4x4 white JPEG
  (Critic addition 7).  :data:`THUMBNAIL_JPEG` is a 24x31 white baseline JPEG (JFIF,
  three components 4:4:4, standard Annex K Huffman tables, DC-only blocks) built once by a
  stand-alone encoder and verified with an independent decoder.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- ZIP members

MEMBER_SEARCH_INDEX = "index.search.pb"
MEMBER_NOTES_INDEX = "index.notes.pb"
MEMBER_EVENTS = "index.events.pb"
MEMBER_THUMBNAIL = "thumbnail.jpg"
MEMBER_ATTACHMENTS_INDEX = "index.attachments.pb"
MEMBER_SCHEMA = "schema.pb"
NOTES_PREFIX = "notes/"
ATTACHMENTS_PREFIX = "attachments/"

SCHEMA_VERSION = 24
SCHEMA_PB = b"\x08\x18"  # {#1 24}

# --------------------------------------------------------------------------- units

CANVAS_PER_POINT = 132.0 / 72.0  # canvas units per PDF point (11/6)
WIDTH_PER_POINT = 2.0  # flat stroke W per point of rendered width (1/144 in)
HIGHLIGHTER_ALPHA = 0.5
TEXT_PADDING = 10.0  # canvas units between a text box's outer rect and its text frame
ELEMENT_MAGIC = 5381  # metadata #14, constant in every 2025/2026 file
ELEMENT_CLOCK_VERSION = 2  # minimum clock version GoodNotes writes on untouched elements
FILL_CLOCK_VERSION = 1  # ``#9.#3.#1`` edit clock of a fill record as first written (1, bumped on resize)
FILL_ALPHA = 0.1  # alpha GoodNotes gives a shape fill (the model carries it; this is the fallback)
EVENT_CLOCK_VERSION = 1  # clock version of the registers inside events (Test4)

# --------------------------------------------------------------------------- fixed UUIDs

DOCUMENT_CONSTANT_UUID = "5A53E89E-F4C2-4548-8DD3-E9DF9FB4592E"  # #30.#3.#1 and #30.#7.#1
ORIENTATION_PORTRAIT = "P"  # #30.#6.#1
RECOGNITION_LANGUAGE = "auto"  # #30.#9 and #105.#6
PAGING_PREFIX = "PagingViewServiceUpdater:"  # #10.#3

# --------------------------------------------------------------------------- event types

EVENT_DOCUMENT_CREATED = 30
EVENT_ATTACHMENT_ADDED = 6
EVENT_TEMPLATE_CREATED = 2
EVENT_PAGE_CREATED = 54
EVENT_SEARCH_UPDATED = 105
EVENT_CURRENT_PAGE = 10
EVENT_NOTES_WRITTEN = 102

# --------------------------------------------------------------------------- element kinds

CONTENT_IMAGE = 1
CONTENT_STROKE = 7
CONTENT_TEXT = 8
CONTENT_FILL = 9  # shape fill of a closed auto-shape (goodnotes-v35-elements.md section 1)

# --------------------------------------------------------------------------- image attachment kinds

# ``#1.#6`` of an image record: absent on PNGs, 1 on JPEG photos, 3 on vector (PDF) stickers
# (goodnotes-v35-elements.md section 5.1; the enum meaning is inferred from those samples).
IMAGE_KIND_PHOTO = 1
IMAGE_KIND_PDF = 3

# --------------------------------------------------------------------------- page event colours

# #54.#17.#1 as bytes, including its own tag (0a 2e): {#2 {#1 RGBA(0.8667 x3, 1), #2 RGBA(1,1,1,1)}}
PAGE_COLOUR_BLOCK = bytes.fromhex(
    "0a2e122c0a140ddedd5d3f15dedd5d3f1ddedd5d3f250000803f"
    "12140d0000803f150000803f1d0000803f250000803f"
)

# --------------------------------------------------------------------------- order keys

ORDER_KEY_PREFIX = "43"  # page order keys "43" + base36(index).rjust(4, "0")
BASE36_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# --------------------------------------------------------------------------- paper names

PAPER_NAME_SUFFIX = " - White"  # "<UUID>_<size>_1_1 - White" for generated paper
A4_SIZE = (595.28, 841.89)

# --------------------------------------------------------------------------- thumbnail

THUMBNAIL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb00430001010101010101"
    "0101010101010101010101010101010101010101010101010101010101010101"
    "01010101010101010101010101010101010101010101010101ffc0001108001f"
    "001803011100021100031100ffc4001f00000105010101010101000000000000"
    "00000102030405060708090a0bffc400b5100002010303020403050504040000"
    "017d01020300041105122131410613516107227114328191a1082342b1c11552"
    "d1f02433627282090a161718191a25262728292a3435363738393a4344454647"
    "48494a535455565758595a636465666768696a737475767778797a8384858687"
    "88898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3"
    "c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6"
    "f7f8f9faffda000c03010002000300003f00fefe28a28a28a28a28a28a28a28a"
    "28a28a28a28a28a28a28a28a28a28affd9"
)
THUMBNAIL_SIZE = (24, 31)
