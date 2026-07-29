"""Canonical JSON and hashing helpers for immutable PageIndex v2 artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

__all__ = [
    "canonical_bytes",
    "canonical_hash",
    "sha256_bytes",
    "write_json_atomic",
]


def canonical_bytes(value: object) -> bytes:
    """Serialize *value* using the PageIndex v2 canonical JSON contract."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """Return a lowercase, unprefixed SHA-256 hexadecimal digest."""
    return hashlib.sha256(value).hexdigest()


def canonical_hash(value: object) -> str:
    """Hash the canonical JSON representation of *value*."""
    return sha256_bytes(canonical_bytes(value))


def write_json_atomic(path: Path, value: object) -> None:
    """Atomically replace *path* with canonical JSON bytes."""
    target = Path(path)
    payload = canonical_bytes(value)
    target.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        os.replace(temporary, target)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
