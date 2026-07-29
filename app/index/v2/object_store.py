"""Immutable, content-addressed storage for PageIndex v2 Segments."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, canonical_hash, write_json_atomic


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SegmentStoreError(ValueError):
    """Raised when a Segment object is malformed, corrupt, or ambiguous."""


@dataclass(frozen=True)
class StoredSegment:
    """A persisted immutable Segment object."""

    segment_hash: str
    path: Path
    byte_size: int

    @property
    def hash(self) -> str:
        """Compatibility alias for callers that refer to the object hash."""

        return self.segment_hash

    @property
    def sha256(self) -> str:
        """The object's SHA-256 digest."""

        return self.segment_hash


def _validate_segment_hash(segment_hash: str) -> str:
    if not isinstance(segment_hash, str) or not _SHA256_RE.fullmatch(segment_hash):
        raise ValueError("segment_hash must be 64 lowercase hexadecimal characters")
    return segment_hash


def _segment_path(pageindex_dir: Path, segment_hash: str) -> Path:
    digest = _validate_segment_hash(segment_hash)
    return (
        Path(pageindex_dir)
        / "objects"
        / "segments"
        / digest[:2]
        / f"{digest}.json"
    )


def put_segment(
    pageindex_dir: Path, segment: Mapping[str, object]
) -> StoredSegment:
    """Persist ``segment`` once and return its content-addressed location.

    Existing valid objects are never rewritten. If the path contains bytes
    whose digest does not match its content address, a full rebuild may repair
    that objectively corrupt derived object with the canonical Segment bytes.
    """

    if not isinstance(segment, Mapping):
        raise TypeError("segment must be a mapping")

    encoded = canonical_bytes(segment)
    segment_hash = canonical_hash(segment)
    _validate_segment_hash(segment_hash)
    destination = _segment_path(Path(pageindex_dir), segment_hash)

    if destination.exists():
        if not destination.is_file():
            raise SegmentStoreError(
                f"segment object path is not a file: {destination}"
            )
        existing = destination.read_bytes()
        if existing != encoded:
            if hashlib.sha256(existing).hexdigest() == segment_hash:
                raise SegmentStoreError(
                    f"segment object collision at {destination}"
                )
            write_json_atomic(destination, segment)
            repaired = destination.read_bytes()
            if repaired != encoded:
                raise SegmentStoreError(
                    f"segment object repair failed at {destination}"
                )
            return StoredSegment(segment_hash, destination, len(repaired))
        return StoredSegment(segment_hash, destination, len(existing))

    write_json_atomic(destination, segment)
    written = destination.read_bytes()
    if written != encoded:
        raise SegmentStoreError(
            f"segment object was not written canonically at {destination}"
        )
    return StoredSegment(segment_hash, destination, len(written))


def load_segment(pageindex_dir: Path, segment_hash: str) -> dict[str, object]:
    """Load and verify one canonical Segment object.

    Hash validation happens before path construction, so traversal strings can
    never escape the object-store root.
    """

    digest = _validate_segment_hash(segment_hash)
    path = _segment_path(Path(pageindex_dir), digest)
    try:
        encoded = path.read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(f"segment object not found: {digest}") from None

    actual = hashlib.sha256(encoded).hexdigest()
    if actual != digest:
        raise SegmentStoreError(
            f"segment object hash mismatch: expected {digest}, got {actual}"
        )

    try:
        value: Any = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SegmentStoreError(f"invalid segment JSON: {digest}") from exc
    if not isinstance(value, dict):
        raise SegmentStoreError(f"segment object must contain a JSON object: {digest}")
    if canonical_bytes(value) != encoded:
        raise SegmentStoreError(f"segment object is not canonical JSON: {digest}")
    return value


def find_reusable_segments(
    pageindex_dir: Path,
) -> dict[tuple[str, str, str], str]:
    """Index stored Segments by ``(doc_key, content_hash, recipe_hash)``.

    Conflicting objects for the same reuse key are rejected instead of picking
    one based on filesystem iteration order.
    """

    root = Path(pageindex_dir) / "objects" / "segments"
    if not root.is_dir():
        return {}

    reusable: dict[tuple[str, str, str], str] = {}
    for path in sorted(root.glob("*/*.json"), key=lambda item: item.as_posix()):
        segment_hash = path.stem
        _validate_segment_hash(segment_hash)
        if path.parent.name != segment_hash[:2]:
            raise SegmentStoreError(
                f"segment object is stored under the wrong prefix: {path}"
            )
        segment = load_segment(Path(pageindex_dir), segment_hash)
        document = segment.get("document")
        fingerprint = segment.get("fingerprint")
        if not isinstance(document, Mapping) or not isinstance(
            fingerprint, Mapping
        ):
            raise SegmentStoreError(
                f"segment object is missing document/fingerprint: {segment_hash}"
            )

        doc_key = document.get("doc_key")
        content_hash = fingerprint.get("content_hash")
        recipe_hash = fingerprint.get("recipe_hash")
        if not all(
            isinstance(value, str) and value
            for value in (doc_key, content_hash, recipe_hash)
        ):
            raise SegmentStoreError(
                f"segment object has an invalid reuse fingerprint: {segment_hash}"
            )

        key = (doc_key, content_hash, recipe_hash)
        previous = reusable.get(key)
        if previous is not None and previous != segment_hash:
            raise SegmentStoreError(
                f"conflicting segment objects for reuse key {key!r}: "
                f"{previous} and {segment_hash}"
            )
        reusable[key] = segment_hash

    return reusable


__all__ = [
    "SegmentStoreError",
    "StoredSegment",
    "find_reusable_segments",
    "load_segment",
    "put_segment",
]
