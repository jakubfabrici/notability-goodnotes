"""Noteastic (Windows) library backups (``.ntcbak``): experimental read-only support.

:mod:`gnnote.noteastic.reader` maps a backup -- a ZIP holding the SQLite database
``noteastic.db`` plus the imported PDFs and images -- onto :class:`gnnote.model.Document`
(``read_ntcbak``).  The layout follows the one public description of the format and has not
been checked against a backup made by the app; see ``docs/noteastic.md``.
"""
