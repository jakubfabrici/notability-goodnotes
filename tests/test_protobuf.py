"""Unit tests for gnnote.protobuf: hand vectors from docs/goodnotes-stroke.md, round trips,
malformed input, and a walk over every record of every sample notebook."""
from __future__ import annotations

import struct
import zipfile

import pytest

from gnnote import protobuf as pb

UUID = "6C008FF2-04F1-4455-B2F5-5AE0C232CB8A"


# --------------------------------------------------------------------------- varints


@pytest.mark.parametrize("n, expected", [
    (0, b"\x00"),
    (1, b"\x01"),
    (127, b"\x7f"),
    (128, b"\x80\x01"),
    (300, b"\xac\x02"),
    (535, b"\x97\x04"),            # the pen stroke's #2 length (doc section 2)
    (5381, b"\x85\x2a"),           # metadata #14
    (3639689413, b"\xc5\x81\xc5\xc7\x0d"),
    (4216713141, b"\xb5\xdf\xd7\xda\x0f"),
    (2 ** 64 - 1, b"\xff" * 9 + b"\x01"),
])
def test_varint_vectors(n, expected):
    assert pb.varint(n) == expected
    assert pb.read_varint(expected) == (n, len(expected))


def test_varint_negative_is_two_complement_64():
    data = pb.varint(-1)
    assert data == b"\xff" * 9 + b"\x01"
    assert pb.read_varint(data)[0] == 2 ** 64 - 1


def test_read_varint_errors():
    with pytest.raises(ValueError):
        pb.read_varint(b"")
    with pytest.raises(ValueError):
        pb.read_varint(b"\x80")
    with pytest.raises(ValueError):
        pb.read_varint(b"\x80" * 10 + b"\x01")  # 11 bytes


# --------------------------------------------------------------------------- decoding


def test_colour_message_from_doc():
    # section 2: G 0.478431, B 1.0, A 1.0 (R omitted because it is 0.0)
    data = bytes.fromhex("15f5f4f43e1d0000803f250000803f")
    fields = pb.decode_message(data)
    assert [f.number for f in fields] == [2, 3, 4]
    assert all(f.wire_type == pb.WIRE_FIXED32 for f in fields)
    assert pb.fixed32_float(fields[0]) == pytest.approx(0.478431, abs=1e-6)
    assert pb.fixed32_float(fields[1]) == 1.0
    assert pb.fixed32_float(pb.get(fields, 4)) == 1.0
    assert pb.get(fields, 1) is None
    assert pb.fixed32_uint(fields[1]) == 0x3F800000


def test_nested_clock_register_from_doc():
    data = bytes.fromhex("0a080803 10c581c5c70d".replace(" ", ""))
    outer = pb.decode_message(data)
    assert len(outer) == 1 and outer[0].number == 1 and outer[0].wire_type == pb.WIRE_LEN
    inner = pb.message_value(outer[0])
    assert inner == [pb.Field(1, 0, 3), pb.Field(2, 0, 3639689413)]
    assert pb.varint_value(inner[1]) == 3639689413


def test_stroke_message_trailer_from_doc():
    tail = bytes.fromhex("3200 3a0a0a080803 10c581c5c70d 4a00 7a08080c10b5dfd7da0f a20100 a80118".replace(" ", ""))
    fields = pb.decode_message(tail)
    assert [(f.number, f.wire_type) for f in fields] == [(6, 2), (7, 2), (9, 2), (15, 2), (20, 2), (21, 0)]
    assert pb.bytes_value(pb.get(fields, 6)) == b""
    assert pb.varint_value(pb.get(fields, 21)) == 24
    clock = pb.message_value(pb.get(fields, 15))
    assert clock == [pb.Field(1, 0, 12), pb.Field(2, 0, 4216713141)]
    assert pb.encode_message(fields) == tail


def test_fixed64_and_string_helpers():
    # container doc section 2.4: 0x4279373138b9d67f = 1732802481053.4 ms = 2024-11-28T14:01:21Z
    raw = bytes.fromhex("7fd6b93831377942")
    ts = struct.unpack("<d", raw)[0]
    assert ts == pytest.approx(1732802481053.4, abs=0.01)
    data = pb.field_fixed64(10, ts) + pb.field_fixed64(11, 2 ** 63 + 5) + pb.field_bytes(1, UUID)
    assert data[:9] == b"\x51" + raw
    fields = pb.decode_message(data)
    assert pb.fixed64_float(fields[0]) == ts
    assert fields[0].value == raw
    assert pb.fixed64_uint(fields[1]) == 2 ** 63 + 5
    assert pb.string_value(fields[2]) == UUID


def test_get_all_and_get():
    fields = pb.decode_message(pb.field_varint(1, 1) + pb.field_varint(2, 2) + pb.field_varint(1, 3))
    assert [f.value for f in pb.get_all(fields, 1)] == [1, 3]
    assert pb.get(fields, 1).value == 1
    assert pb.get_all(fields, 9) == []


def test_try_decode_message():
    assert pb.try_decode_message(b"\x80") is None
    assert pb.try_decode_message(b"\x08\x01") == [pb.Field(1, 0, 1)]
    assert pb.try_decode_message(b"") == []


@pytest.mark.parametrize("data", [
    b"\x00",                     # field number 0
    b"\x0b",                     # wire type 3 (start group)
    b"\x0c",                     # wire type 4 (end group)
    b"\x0e",                     # wire type 6
    b"\x0f",                     # wire type 7
    b"\x08",                     # varint field without a value
    b"\x08\x80",                 # truncated varint value
    b"\x0a\x05ab",               # LEN runs past the end
    b"\x0a",                     # LEN without length
    b"\x0d\x00\x00",             # truncated fixed32
    b"\x09\x00",                 # truncated fixed64
    b"\x80",                     # truncated key
])
def test_decode_message_rejects_malformed(data):
    with pytest.raises(ValueError):
        pb.decode_message(data)


def test_records_round_trip_and_errors():
    recs = [b"", b"a", bytes(range(200)), b"x" * 300]
    stream = pb.encode_records(recs)
    assert stream.startswith(b"\x00\x01a\xc8\x01")
    assert pb.decode_records(stream) == recs
    assert pb.decode_records(b"") == []
    with pytest.raises(ValueError):
        pb.decode_records(b"\x05ab")
    with pytest.raises(ValueError):
        pb.decode_records(b"\x80")


def test_index_notes_prefix_from_container_doc():
    # index.notes.pb begins `52 0a 24 ...` = record length 82 then field 1 LEN 36
    rec = pb.field_bytes(1, UUID) + b"\x00" * (82 - 38)
    stream = pb.encode_records([rec])
    assert stream[:3] == b"\x52\x0a\x24"


# --------------------------------------------------------------------------- encoding


def test_metadata_record_recipe():
    rec = (pb.field_bytes(1, UUID) + pb.field_message(2, pb.field_varint(1, 2) + pb.field_varint(2, 0x12345678))
           + pb.field_varint(8, 1606161793633522096) + pb.field_varint(9, 141448)
           + pb.field_varint(14, 5381) + pb.field_varint(16, 24))
    assert rec[:2] == b"\x0a\x24"
    assert rec[38:42] == b"\x12\x08\x08\x02"
    assert rec.endswith(b"\x70\x85\x2a\x80\x01\x18")
    fields = pb.decode_message(rec)
    assert [f.number for f in fields] == [1, 2, 8, 9, 14, 16]
    assert pb.varint_value(pb.get(fields, 8)) == 1606161793633522096


def test_fixed32_encoders():
    assert pb.field_fixed32(4, 1.0) == b"\x25\x00\x00\x80\x3f"
    assert pb.field_fixed32(4, 0.5) == b"\x25\x00\x00\x00\x3f"
    assert pb.field_fixed32_bits(2, 0x3EF4F4F5) == b"\x15\xf5\xf4\xf4\x3e"
    with pytest.raises(ValueError):
        pb.field_varint(0, 1)


def test_encode_field_rejects_wrong_shapes():
    with pytest.raises(ValueError):
        pb.encode_field(pb.Field(1, pb.WIRE_FIXED32, b"abc"))
    with pytest.raises(ValueError):
        pb.encode_field(pb.Field(1, pb.WIRE_VARINT, b"abc"))
    with pytest.raises(ValueError):
        pb.encode_field(pb.Field(1, pb.WIRE_LEN, 5))
    with pytest.raises(ValueError):
        pb.encode_field(pb.Field(1, 3, b""))
    with pytest.raises(ValueError):
        pb.fixed32_float(pb.Field(1, 0, 7))


def test_encode_decode_identity_random_message():
    msg = b"".join([
        pb.field_varint(1, 0), pb.field_varint(2, 2 ** 40), pb.field_bytes(3, b"\x00\xff" * 100),
        pb.field_fixed32(4, -2.5), pb.field_fixed64(5, 123456789.0), pb.field_fixed64(6, 7),
        pb.field_message(200, pb.field_varint(1, 1)),
    ])
    fields = pb.decode_message(msg)
    assert pb.encode_message(fields) == msg
    assert pb.get(fields, 200).number == 200


# --------------------------------------------------------------------------- samples


def _walk_elements(path):
    """Yield (member, metadata fields, stroke fields) for every (metadata, content) pair."""
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.startswith("notes/"):
                continue
            records = pb.decode_records(z.read(name))
            previous = None
            for rec in records:
                fields = pb.decode_message(rec)
                assert pb.encode_message(fields) == rec
                content = pb.get(fields, 7)
                if content is not None:
                    assert len(fields) == 1
                    yield name, previous, pb.decode_message(content.value)
                previous = fields


def test_every_sample_record_decodes_and_reencodes(samples):
    total = 0
    pairs_ok = 0
    for path in samples.goodnotes_files():
        for _name, meta, stroke in _walk_elements(path):
            geo = pb.get(stroke, 2)
            if geo is None or geo.value[:4] != b"bv41":
                continue
            total += 1
            assert pb.varint_value(pb.get(stroke, 21)) in (24, 25)
            assert pb.string_value(pb.get(stroke, 1)) == pb.string_value(pb.get(meta, 1))
            assert pb.varint_value(pb.get(meta, 16)) == pb.varint_value(pb.get(stroke, 21))
            # clocks: metadata #2 == stroke #15 (doc section 1.1)
            assert pb.bytes_value(pb.get(meta, 2)) == pb.bytes_value(pb.get(stroke, 15))
            pairs_ok += 1
    assert total == 5677
    assert pairs_ok == total
