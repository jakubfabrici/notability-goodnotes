"""PDF codec: ``reader.read_pdf(data) -> Document`` and ``writer.write_pdf(doc, options) -> bytes``.

Registered in :mod:`gnnote.formats` as ``pdf``; ``docs/pdf.md`` documents what is written and
read.  Modules (imported lazily by the registry, so importing this package is cheap):
``objects`` (PDF objects, reading on top of :mod:`gnnote.pdfutil`, writing, incremental
updates), ``images`` (JPEG / PNG), ``text`` (fonts and layout), ``ttf`` (TrueType subsetting),
``writer`` and ``reader``.
"""
