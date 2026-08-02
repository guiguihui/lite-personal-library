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
from .ids import make_doc_key


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SegmentStoreError(ValueError):
    """Raised when a Segment object is malformed, corrupt, or ambiguous."""


@dataclass(frozen=True, slots=True)
class StoredSegmentRef:
    """A lightweight attestation for one persisted immutable Segment."""

    segment_hash: str
    path: Path
    byte_size: int
    doc_key: str
    doc_type: str
    slug: str
    content_hash: str
    segment_recipe_hash: str

    @property
    def hash(self) -> str:
        """Compatibility alias for callers that refer to the object hash."""

        return self.segment_hash

    @property
    def sha256(self) -> str:
        """The object's SHA-256 digest."""

        return self.segment_hash


# Backward-compatible import name. New code uses the explicit reference name
# so Segment ownership remains visible at API boundaries.
StoredSegment = StoredSegmentRef


def _validate_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(
            f"{field} must be 64 lowercase hexadecimal characters"
        )
    return value


def _validate_segment_hash(segment_hash: object) -> str:
    return _validate_sha256(segment_hash, "segment_hash")


def _document_identity(doc_key: object) -> tuple[str, str, str]:
    if not isinstance(doc_key, str):
        raise ValueError("doc_key must be a valid type-namespaced document key")
    doc_type, separator, slug = doc_key.partition(":")
    if not separator:
        raise ValueError("doc_key must be a valid type-namespaced document key")
    try:
        expected = make_doc_key(doc_type, slug)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "doc_key must be a valid type-namespaced document key"
        ) from exc
    if expected != doc_key:
        raise ValueError("doc_key must be a valid type-namespaced document key")
    return doc_key, doc_type, slug


def _segment_attestation(
    segment: Mapping[str, object],
) -> tuple[str, str, str, str, str]:
    document = segment.get("document")
    fingerprint = segment.get("fingerprint")
    if not isinstance(document, Mapping) or not isinstance(
        fingerprint, Mapping
    ):
        raise SegmentStoreError(
            "segment object is missing document/fingerprint"
        )

    doc_key, doc_type, slug = _document_identity(document.get("doc_key"))
    if document.get("type") != doc_type or document.get("id") != slug:
        raise SegmentStoreError(
            f"segment document identity does not match doc_key: {doc_key}"
        )
    content_hash = _validate_sha256(
        fingerprint.get("content_hash"),
        "content_hash",
    )
    segment_recipe_hash = _validate_sha256(
        fingerprint.get("recipe_hash"),
        "segment_recipe_hash",
    )
    return (
        doc_key,
        doc_type,
        slug,
        content_hash,
        segment_recipe_hash,
    )


def _segment_path(pageindex_dir: Path, segment_hash: str) -> Path:
    digest = _validate_segment_hash(segment_hash)
    return (
        Path(pageindex_dir)
        / "objects"
        / "segments"
        / digest[:2]
        / f"{digest}.json"
    )


def _new_segment_ref(
    segment_hash: str,
    path: Path,
    byte_size: int,
    attestation: tuple[str, str, str, str, str],
) -> StoredSegmentRef:
    doc_key, doc_type, slug, content_hash, segment_recipe_hash = attestation
    return StoredSegmentRef(
        segment_hash=segment_hash,
        path=path,
        byte_size=byte_size,
        doc_key=doc_key,
        doc_type=doc_type,
        slug=slug,
        content_hash=content_hash,
        segment_recipe_hash=segment_recipe_hash,
    )


def segment_ref_from_attestation(
    pageindex_dir: Path,
    doc_key: str,
    segment_hash: str,
    content_hash: str,
    segment_recipe_hash: str,
) -> StoredSegmentRef:
    """Construct a Segment ref from manifest/proof facts without decoding it."""

    identity = _document_identity(doc_key)
    digest = _validate_segment_hash(segment_hash)
    content_digest = _validate_sha256(content_hash, "content_hash")
    recipe_digest = _validate_sha256(
        segment_recipe_hash,
        "segment_recipe_hash",
    )
    destination = _segment_path(Path(pageindex_dir), digest)
    if not destination.exists():
        raise FileNotFoundError(f"segment object not found: {digest}")
    if not destination.is_file():
        raise SegmentStoreError(
            f"segment object path is not a file: {destination}"
        )
    return _new_segment_ref(
        digest,
        destination,
        destination.stat().st_size,
        (*identity, content_digest, recipe_digest),
    )


def put_segment(
    pageindex_dir: Path, segment: Mapping[str, object]
) -> StoredSegmentRef:
    """Persist ``segment`` once and return its content-addressed location.

    Existing valid objects are never rewritten. If the path contains bytes
    whose digest does not match its content address, a full rebuild may repair
    that objectively corrupt derived object with the canonical Segment bytes.
    """

    if not isinstance(segment, Mapping):
        raise TypeError("segment must be a mapping")

    attestation = _segment_attestation(segment)
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
            return _new_segment_ref(
                segment_hash,
                destination,
                len(repaired),
                attestation,
            )
        return _new_segment_ref(
            segment_hash,
            destination,
            len(existing),
            attestation,
        )

    write_json_atomic(destination, segment)
    written = destination.read_bytes()
    if written != encoded:
        raise SegmentStoreError(
            f"segment object was not written canonically at {destination}"
        )
    return _new_segment_ref(
        segment_hash,
        destination,
        len(written),
        attestation,
    )


def load_segment(
    pageindex_dir: Path, segment_hash: str | StoredSegmentRef
) -> dict[str, object]:
    """Load and verify one canonical Segment object.

    Hash validation happens before path construction, so traversal strings can
    never escape the object-store root.
    """

    ref = segment_hash if isinstance(segment_hash, StoredSegmentRef) else None
    digest = _validate_segment_hash(
        ref.segment_hash if ref is not None else segment_hash
    )
    path = _segment_path(Path(pageindex_dir), digest)
    if ref is not None:
        identity = _document_identity(ref.doc_key)
        if (ref.doc_type, ref.slug) != identity[1:]:
            raise SegmentStoreError(
                f"segment ref attestation mismatch for {ref.doc_key}"
            )
        _validate_sha256(ref.content_hash, "content_hash")
        _validate_sha256(
            ref.segment_recipe_hash,
            "segment_recipe_hash",
        )
        if (
            isinstance(ref.byte_size, bool)
            or not isinstance(ref.byte_size, int)
            or ref.byte_size < 0
        ):
            raise ValueError("byte_size must be a non-negative integer")
        if Path(ref.path).resolve() != path.resolve():
            raise SegmentStoreError(
                f"segment ref path mismatch: expected {path}, got {ref.path}"
            )
    try:
        encoded = path.read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(f"segment object not found: {digest}") from None

    actual = hashlib.sha256(encoded).hexdigest()
    if actual != digest:
        raise SegmentStoreError(
            f"segment object hash mismatch: expected {digest}, got {actual}"
        )
    if ref is not None and len(encoded) != ref.byte_size:
        raise SegmentStoreError(
            "segment ref byte size mismatch: "
            f"expected {ref.byte_size}, got {len(encoded)}"
        )

    try:
        value: Any = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SegmentStoreError(f"invalid segment JSON: {digest}") from exc
    if not isinstance(value, dict):
        raise SegmentStoreError(f"segment object must contain a JSON object: {digest}")
    if canonical_bytes(value) != encoded:
        raise SegmentStoreError(f"segment object is not canonical JSON: {digest}")
    if ref is not None:
        try:
            actual_attestation = _segment_attestation(value)
        except (TypeError, ValueError) as exc:
            raise SegmentStoreError(
                f"segment ref attestation mismatch for {ref.doc_key}"
            ) from exc
        expected_attestation = (
            ref.doc_key,
            ref.doc_type,
            ref.slug,
            ref.content_hash,
            ref.segment_recipe_hash,
        )
        if actual_attestation != expected_attestation:
            raise SegmentStoreError(
                f"segment ref attestation mismatch for {ref.doc_key}"
            )
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
    "StoredSegmentRef",
    "find_reusable_segments",
    "load_segment",
    "put_segment",
    "segment_ref_from_attestation",
]
