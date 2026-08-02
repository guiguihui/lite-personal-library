from __future__ import annotations

from io import BytesIO

import pytest

from app.index.v3.varint import (
    MAX_U64,
    TruncatedVarintError,
    VarintError,
    decode_uvarint,
    encode_uvarint,
    read_uvarint,
    read_uvarint_or_eof,
    write_uvarint,
)


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (255, b"\xff\x01"),
        (300, b"\xac\x02"),
        (2**63, b"\x80\x80\x80\x80\x80\x80\x80\x80\x80\x01"),
        (MAX_U64, b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01"),
    ],
)
def test_minimal_uvarint_vectors(value: int, encoded: bytes) -> None:
    assert encode_uvarint(value) == encoded
    assert decode_uvarint(encoded) == value
    assert read_uvarint(BytesIO(encoded)) == value


@pytest.mark.parametrize("value", [True, False, -1, MAX_U64 + 1, 1.0, "1"])
def test_encoder_rejects_values_outside_u64(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        encode_uvarint(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "encoded",
    [
        b"\x80\x00",
        b"\x81\x00",
        b"\xff\x81\x00",
        b"\x80\x80\x80\x80\x80\x80\x80\x80\x80\x00",
    ],
)
def test_decoder_rejects_nonminimal_encodings(encoded: bytes) -> None:
    with pytest.raises(VarintError, match="minimally"):
        decode_uvarint(encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        b"\x80\x80\x80\x80\x80\x80\x80\x80\x80\x02",
        b"\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80",
    ],
)
def test_decoder_rejects_u64_overflow(encoded: bytes) -> None:
    with pytest.raises(VarintError, match="overflows"):
        decode_uvarint(encoded)


def test_clean_eof_is_distinct_from_a_truncated_varint() -> None:
    assert read_uvarint_or_eof(BytesIO(b"")) is None
    with pytest.raises(EOFError, match="expected"):
        read_uvarint(BytesIO(b""))
    with pytest.raises(TruncatedVarintError, match="truncated"):
        read_uvarint_or_eof(BytesIO(b"\x80"))


def test_stream_reader_consumes_exactly_one_value() -> None:
    stream = BytesIO(encode_uvarint(128) + encode_uvarint(7))
    assert read_uvarint(stream) == 128
    assert read_uvarint(stream) == 7
    assert read_uvarint_or_eof(stream) is None


def test_in_memory_decoder_rejects_trailing_bytes() -> None:
    with pytest.raises(VarintError, match="trailing"):
        decode_uvarint(b"\x01\x00")


def test_writer_returns_exact_encoded_length() -> None:
    stream = BytesIO()
    assert write_uvarint(stream, 300) == 2
    assert stream.getvalue() == b"\xac\x02"
