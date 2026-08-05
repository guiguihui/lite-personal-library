"""Strict unsigned LEB128 primitives for PageIndex v3 binary artifacts."""

from __future__ import annotations

from typing import BinaryIO


MAX_U64 = (1 << 64) - 1
_MAX_ENCODED_BYTES = 10


class VarintError(ValueError):
    """An encoded unsigned integer is invalid or non-canonical."""


class TruncatedVarintError(EOFError):
    """EOF occurred after at least one byte of a varint was consumed."""


def _require_u64(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("unsigned varint value must be an integer")
    if value < 0 or value > MAX_U64:
        raise ValueError(f"unsigned varint value must be in [0, {MAX_U64}]")
    return value


def encode_uvarint(value: int) -> bytes:
    """Return the unique minimal unsigned LEB128 encoding of one u64."""

    remaining = _require_u64(value)
    encoded = bytearray()
    while remaining >= 0x80:
        encoded.append((remaining & 0x7F) | 0x80)
        remaining >>= 7
    encoded.append(remaining)
    return bytes(encoded)


def write_uvarint(stream: BinaryIO, value: int) -> int:
    """Write one minimal u64 and return its encoded byte count."""

    encoded = encode_uvarint(value)
    written = stream.write(encoded)
    if written is not None and written != len(encoded):
        raise OSError(
            f"short unsigned varint write: expected {len(encoded)}, got {written}"
        )
    return len(encoded)


def _read_uvarint(stream: BinaryIO, *, allow_clean_eof: bool) -> int | None:
    encoded = bytearray()
    value = 0

    for position in range(_MAX_ENCODED_BYTES):
        raw = stream.read(1)
        if raw == b"":
            if not encoded and allow_clean_eof:
                return None
            if not encoded:
                raise EOFError("expected unsigned varint")
            raise TruncatedVarintError("truncated unsigned varint")
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != 1:
            raise OSError("binary stream read(1) must return exactly one byte or EOF")

        byte = raw[0]
        encoded.append(byte)
        payload = byte & 0x7F
        if position == _MAX_ENCODED_BYTES - 1 and (
            byte & 0x80 or payload > 1
        ):
            raise VarintError("unsigned varint overflows u64")
        value |= payload << (position * 7)

        if not byte & 0x80:
            if encode_uvarint(value) != bytes(encoded):
                raise VarintError("unsigned varint is not minimally encoded")
            return value

    raise VarintError("unsigned varint overflows u64")


def read_uvarint(stream: BinaryIO) -> int:
    """Read one required minimal u64; clean or partial EOF is an error."""

    value = _read_uvarint(stream, allow_clean_eof=False)
    assert value is not None
    return value


def read_uvarint_or_eof(stream: BinaryIO) -> int | None:
    """Read one minimal u64, returning ``None`` only for a clean EOF."""

    return _read_uvarint(stream, allow_clean_eof=True)


def decode_uvarint(encoded: bytes | bytearray | memoryview) -> int:
    """Decode exactly one minimal u64 from an in-memory byte sequence."""

    if not isinstance(encoded, (bytes, bytearray, memoryview)):
        raise TypeError("encoded unsigned varint must be bytes-like")
    from io import BytesIO

    stream = BytesIO(bytes(encoded))
    value = read_uvarint(stream)
    if stream.read(1) != b"":
        raise VarintError("trailing bytes after unsigned varint")
    return value


__all__ = [
    "MAX_U64",
    "TruncatedVarintError",
    "VarintError",
    "decode_uvarint",
    "encode_uvarint",
    "read_uvarint",
    "read_uvarint_or_eof",
    "write_uvarint",
]
