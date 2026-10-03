// Generated from gnnote/formats.py by scripts/build_web.py; do not edit by hand.
// tests/test_formats.py checks that this file matches the Python registry.
export const FORMATS = [
  {
    "id": "goodnotes",
    "name": "GoodNotes",
    "extension": ".goodnotes",
    "inputExtensions": [
      ".goodnotes"
    ],
    "readable": true,
    "writable": true,
    "defaultTarget": "notability"
  },
  {
    "id": "notability",
    "name": "Notability",
    "extension": ".note",
    "inputExtensions": [
      ".note"
    ],
    "readable": true,
    "writable": true,
    "defaultTarget": "goodnotes"
  },
  {
    "id": "noteful",
    "name": "Noteful",
    "extension": ".noteful",
    "inputExtensions": [
      ".noteful"
    ],
    "readable": true,
    "writable": true,
    "defaultTarget": "notability"
  },
  {
    "id": "collanote",
    "name": "CollaNote",
    "extension": ".cnote",
    "inputExtensions": [
      ".cnote"
    ],
    "readable": true,
    "writable": false,
    "defaultTarget": "notability"
  },
  {
    "id": "pdf",
    "name": "PDF",
    "extension": ".pdf",
    "inputExtensions": [
      ".pdf"
    ],
    "readable": true,
    "writable": true,
    "defaultTarget": "notability"
  }
];
