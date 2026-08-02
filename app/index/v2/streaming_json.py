"""Bounded-memory primitives for validating JSON artifacts on disk.

Large runtime artifacts are intentionally treated as byte streams here.  Only
small control-plane documents (for example, a manifest or input proof) should
be decoded with :func:`load_bounded_canonical_json`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .canonical import canonical_bytes

__all__ = [
    "BoundedJsonError",
    "FileDigest",
    "load_bounded_canonical_json",
    "stream_file_digest",
]


DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_CONTROL_DOCUMENT_LIMIT = 64 * 1024 * 1024


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
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoundedJsonError("invalid UTF-8 JSON control document") from exc
    if canonical_bytes(value) != encoded:
        raise BoundedJsonError("JSON control document is not canonical")
    return value
