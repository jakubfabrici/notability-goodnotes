"""Work around two parsing heuristics of the third-party oracles when they read gnnote's output.

The writer tests run parser-for-goodnotes and goodparse on files gnnote generates.  Every
generated file carries fresh random UUIDs, clocks and a device id, and both parsers have a
heuristic that misreads a small share of such files although the files are framed and laid
out like GoodNotes' own:

* parser-for-goodnotes guesses how each archive member is framed.  It first decodes the
  whole member as one protobuf message and falls back to varint-length-delimited records only
  when that fails.  GoodNotes and gnnote frame every index and note member as delimited
  records, but about 3 % of generated files hold a member whose bytes also form one valid
  message; the guess then drops pages or strokes.
* goodparse probes the first field of every record for a placement matrix.  When that field
  is an element UUID, the probe reads the UUID's ASCII as protobuf and raises ``IndexError``
  if a varint starts on its last character (about 0.25 % of files).

The oracle scripts run in a subprocess with this directory on ``PYTHONPATH`` and call the
matching function before parsing.  Nothing changes on disk, and the oracle runs on GoodNotes'
own sample files (``test_goodnotes_reader.py``) do not use these shims.
"""
from __future__ import annotations


def frame_parser_for_goodnotes() -> None:
    """Make parser-for-goodnotes read a member as delimited records when it is a complete run
    of them, and keep the parser's own guess for anything else."""
    from goodnotes_re.archive import GoodNotesDocument
    from goodnotes_re.wire import DecodeError, Message, decode_delimited_messages

    guessing_decode = GoodNotesDocument.decode
    guessing_decode_records = GoodNotesDocument.decode_records

    def delimited(doc, member):
        try:
            return decode_delimited_messages(doc.read(member))
        except DecodeError:
            return None

    def decode(self, member):
        records = delimited(self, member)
        if records is None:
            return guessing_decode(self, member)
        return Message(tuple(field for record in records for field in record.fields), self.read(member))

    def decode_records(self, member):
        records = delimited(self, member)
        return records if records is not None else guessing_decode_records(self, member)

    GoodNotesDocument.decode = decode
    GoodNotesDocument.decode_records = decode_records


def guard_goodparse() -> None:
    """Make goodparse's field probe answer "no such field" for bytes that end mid-varint,
    as it already does for bytes with an unknown wire type."""
    from goodparse import goodnotes

    unguarded = goodnotes._get_blob

    def get_blob(b, field_no):
        try:
            return unguarded(b, field_no)
        except IndexError:
            return None

    goodnotes._get_blob = get_blob
