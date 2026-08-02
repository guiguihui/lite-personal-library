"""Bounded-memory primitives for validating JSON artifacts on disk.

Large runtime artifacts are intentionally treated as byte streams here.  Only
small control-plane documents (for example, a manifest or input proof) should
be decoded with :func:`load_bounded_canonical_json`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .canonical import canonical_bytes

__all__ = [
    "BoundedJsonError",
    "CanonicalJsonStream",
    "FileDigest",
    "iter_canonical_array_items",
    "load_bounded_canonical_json",
    "stream_file_digest",
]


DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_CONTROL_DOCUMENT_LIMIT = 64 * 1024 * 1024
DEFAULT_STREAM_VALUE_LIMIT = 16 * 1024 * 1024


class BoundedJsonError(ValueError):
    """Raised when a bounded control document cannot be decoded safely."""


@dataclass(frozen=True, slots=True)
class FileDigest:
    """Digest and byte count produced by one streaming file pass."""

    sha256: str
    byte_size: int


def _positive_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be an integer > 0")
    return value


class CanonicalJsonStream:
    """Small-buffer reader for one canonical JSON stream.

    ``read_value`` materializes only the next scalar/object/array and enforces
    an explicit byte limit. Callers keep large wrapper collections streaming
    by reading their punctuation separately and consuming one item at a time.
    """

    __slots__ = ("_buffer", "_chunk_size", "_position", "_stream", "path")

    def __init__(
        self,
        path: Path,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.path = Path(path)
        self._chunk_size = _positive_int(chunk_size, "chunk_size")
        self._stream: BinaryIO | None = None
        self._buffer = b""
        self._position = 0

    def __enter__(self) -> "CanonicalJsonStream":
        if self._stream is not None:
            raise RuntimeError("canonical JSON stream cannot be reused")
        self._stream = self.path.open("rb")
        return self

    def __exit__(self, *_exc: object) -> bool:
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.close()
        self._buffer = b""
        self._position = 0
        return False

    def _require_open(self) -> BinaryIO:
        if self._stream is None:
            raise RuntimeError("canonical JSON stream is not open")
        return self._stream

    def _fill(self) -> bool:
        stream = self._require_open()
        chunk = stream.read(self._chunk_size)
        if not chunk:
            self._buffer = b""
            self._position = 0
            return False
        self._buffer = chunk
        self._position = 0
        return True

    def peek_byte(self) -> int | None:
        if self._position >= len(self._buffer) and not self._fill():
            return None
        return self._buffer[self._position]

    def read_byte(self) -> int:
        value = self.peek_byte()
        if value is None:
            raise BoundedJsonError("unexpected end of canonical JSON stream")
        self._position += 1
        return value

    def expect(self, literal: bytes) -> None:
        if not isinstance(literal, bytes) or not literal:
            raise ValueError("literal must be non-empty bytes")
        for expected in literal:
            actual = self.read_byte()
            if actual != expected:
                raise BoundedJsonError(
                    f"unexpected canonical JSON byte: expected {expected:#x}, "
                    f"got {actual:#x}"
                )

    def read_value(
        self,
        *,
        max_bytes: int = DEFAULT_STREAM_VALUE_LIMIT,
    ) -> object:
        """Decode the next complete canonical JSON value under a byte bound."""

        limit = _positive_int(max_bytes, "max_bytes")
        raw = bytearray()
        first = self.peek_byte()
        if first is None:
            raise BoundedJsonError("missing canonical JSON value")

        if first in (ord("{"), ord("[")):
            depth = 0
            in_string = False
            escaped = False
            while True:
                value = self.read_byte()
                raw.append(value)
                if len(raw) > limit:
                    raise BoundedJsonError(
                        f"streamed JSON value exceeds {limit} bytes"
                    )
                if in_string:
                    if escaped:
                        escaped = False
                    elif value == ord("\\"):
                        escaped = True
                    elif value == ord('"'):
                        in_string = False
                    continue
                if value == ord('"'):
                    in_string = True
                elif value in (ord("{"), ord("[")):
                    depth += 1
                elif value in (ord("}"), ord("]")):
                    depth -= 1
                    if depth == 0:
                        break
                    if depth < 0:
                        raise BoundedJsonError("unbalanced canonical JSON value")
        elif first == ord('"'):
            escaped = False
            while True:
                value = self.read_byte()
                raw.append(value)
                if len(raw) > limit:
                    raise BoundedJsonError(
                        f"streamed JSON value exceeds {limit} bytes"
                    )
                if len(raw) == 1:
                    continue
                if escaped:
                    escaped = False
                elif value == ord("\\"):
                    escaped = True
                elif value == ord('"'):
                    break
        else:
            while True:
                value = self.peek_byte()
                if value is None or value in (ord(","), ord("]"), ord("}")):
                    break
                raw.append(self.read_byte())
                if len(raw) > limit:
                    raise BoundedJsonError(
                        f"streamed JSON value exceeds {limit} bytes"
                    )

        encoded = bytes(raw)
        try:
            decoded = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise BoundedJsonError("invalid streamed UTF-8 JSON value") from exc
        try:
            canonical = canonical_bytes(decoded)
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise BoundedJsonError(
                "streamed JSON value cannot be encoded canonically"
            ) from exc
        if canonical != encoded:
            raise BoundedJsonError("streamed JSON value is not canonical")
        return decoded

    def read_nonnegative_int_pair(
        self,
        *,
        max_bytes: int = 256,
    ) -> tuple[int, int]:
        """Read one canonical ``[nonnegative_int,nonnegative_int]`` value."""

        limit = _positive_int(max_bytes, "max_bytes")
        self.expect(b"[")
        parts: list[bytes] = []
        size = 0
        while True:
            if self.peek_byte() is None:
                raise BoundedJsonError("unterminated canonical integer pair")
            end = self._buffer.find(b"]", self._position)
            if end >= 0:
                part = self._buffer[self._position : end]
                self._position = end + 1
                parts.append(part)
                size += len(part)
                break
            part = self._buffer[self._position :]
            self._position = len(self._buffer)
            parts.append(part)
            size += len(part)
            if size > limit:
                raise BoundedJsonError(
                    f"canonical integer pair exceeds {limit} bytes"
                )
        if size > limit:
            raise BoundedJsonError(f"canonical integer pair exceeds {limit} bytes")
        encoded = b"".join(parts)
        left, separator, right = encoded.partition(b",")
        if not separator or b"," in right:
            raise BoundedJsonError(
                "canonical integer pair must contain exactly two values"
            )
        for value in (left, right):
            if (
                not value
                or not value.isdigit()
                or (len(value) > 1 and value.startswith(b"0"))
            ):
                raise BoundedJsonError(
                    "canonical integer pair values must be non-negative integers"
                )
        return int(left), int(right)

    def finish(self) -> None:
        if self.peek_byte() is not None:
            raise BoundedJsonError("trailing bytes after canonical JSON document")


def iter_canonical_array_items(
    path: Path,
    *,
    object_key: str,
    max_item_bytes: int = DEFAULT_STREAM_VALUE_LIMIT,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[object]:
    """Yield one item at a time from ``{object_key: [...]}`` canonical JSON."""

    if not isinstance(object_key, str) or not object_key:
        raise ValueError("object_key must be a non-empty string")
    prefix = b"{" + canonical_bytes(object_key) + b":["
    with CanonicalJsonStream(path, chunk_size=chunk_size) as reader:
        reader.expect(prefix)
        first = True
        while reader.peek_byte() != ord("]"):
            if not first:
                reader.expect(b",")
            first = False
            yield reader.read_value(max_bytes=max_item_bytes)
        reader.expect(b"]}")
        reader.finish()

def stream_file_digest(
    path: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> FileDigest:
    """Hash ``path`` without materializing its contents in memory."""

    block_size = _positive_int(chunk_size, "chunk_size")
    digest = hashlib.sha256()
    byte_size = 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(block_size):
            digest.update(chunk)
            byte_size += len(chunk)
    return FileDigest(sha256=digest.hexdigest(), byte_size=byte_size)


def load_bounded_canonical_json(
    path: Path,
    *,
    max_bytes: int = DEFAULT_CONTROL_DOCUMENT_LIMIT,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> object:
    """Decode one canonical JSON control document under a hard byte limit.

    The bound is checked while reading, so a stale or adversarial file size
    cannot cause an unbounded allocation.  Runtime indexes should not use this
    helper; their validation remains streaming.
    """

    limit = _positive_int(max_bytes, "max_bytes")
    block_size = _positive_int(chunk_size, "chunk_size")
    raw = bytearray()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(min(block_size, limit + 1)):
            raw.extend(chunk)
            if len(raw) > limit:
                raise BoundedJsonError(
                    f"JSON control document exceeds {limit} bytes"
                )
    encoded = bytes(raw)
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BoundedJsonError("invalid UTF-8 JSON control document") from exc
    try:
        canonical = canonical_bytes(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BoundedJsonError(
            "JSON control document cannot be encoded canonically"
        ) from exc
    if canonical != encoded:
        raise BoundedJsonError("JSON control document is not canonical")
    return value
