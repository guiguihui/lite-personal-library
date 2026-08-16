"""Deterministic source-input proofs bound into PageIndex Generations."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


INPUT_PROOF_SCHEMA_VERSION = 1
INPUT_PROOF_PATH = "input-proof.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROOF_KEYS = {"schema_version", "compiler_recipe_hash", "documents"}
_DOCUMENT_PROOF_KEYS = {"content_hash", "segment_recipe_hash"}


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _doc_key(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _build_proof(
    documents: Mapping[str, tuple[str, str]],
    compiler_recipe_hash: str,
) -> dict[str, object]:
    compiler_hash = _sha256(
        compiler_recipe_hash,
        "compiler_recipe_hash",
    )
    normalized: dict[str, dict[str, str]] = {}
    for raw_doc_key in sorted(documents):
        doc_key = _doc_key(raw_doc_key, "documents key")
        pair = documents[raw_doc_key]
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
        ):
            raise ValueError(
                f"documents[{doc_key!r}] must contain content and recipe hashes"
            )
        content_hash, segment_recipe_hash = pair
        normalized[doc_key] = {
            "content_hash": _sha256(
                content_hash,
                f"documents[{doc_key!r}].content_hash",
            ),
            "segment_recipe_hash": _sha256(
                segment_recipe_hash,
                f"documents[{doc_key!r}].segment_recipe_hash",
            ),
        }
    return {
        "schema_version": INPUT_PROOF_SCHEMA_VERSION,
        "compiler_recipe_hash": compiler_hash,
        "documents": normalized,
    }


def proof_from_fingerprints(
    fingerprints: Mapping[str, str],
    segment_recipe_hash: str,
    compiler_recipe_hash: str,
) -> dict[str, object]:
    """Build a proof for live document fingerprints under one Segment recipe."""

    if not isinstance(fingerprints, Mapping):
        raise ValueError("fingerprints must be an object")
    recipe_hash = _sha256(segment_recipe_hash, "segment_recipe_hash")
    documents = {
        _doc_key(doc_key, "fingerprints key"): (
            _sha256(content_hash, f"fingerprints[{doc_key!r}]"),
            recipe_hash,
        )
        for doc_key, content_hash in fingerprints.items()
    }
    return _build_proof(documents, compiler_recipe_hash)


def proof_from_segments(
    segments: Sequence[Mapping[str, object]],
    compiler_recipe_hash: str,
) -> dict[str, object]:
    """Derive the source proof recorded by a compiled Segment collection."""

    if not isinstance(segments, Sequence) or isinstance(
        segments,
        (str, bytes, bytearray),
    ):
        raise ValueError("segments must be an array")
    documents: dict[str, tuple[str, str]] = {}
    for position, raw_segment in enumerate(segments):
        segment = _mapping(raw_segment, f"segments[{position}]")
        document = _mapping(
            segment.get("document"),
            f"segments[{position}].document",
        )
        fingerprint = _mapping(
            segment.get("fingerprint"),
            f"segments[{position}].fingerprint",
        )
        doc_key = _doc_key(
            document.get("doc_key"),
            f"segments[{position}].document.doc_key",
        )
        if doc_key in documents:
            raise ValueError(f"duplicate document in input proof: {doc_key}")
        documents[doc_key] = (
            _sha256(
                fingerprint.get("content_hash"),
                f"segments[{position}].fingerprint.content_hash",
            ),
            _sha256(
                fingerprint.get("recipe_hash"),
                f"segments[{position}].fingerprint.recipe_hash",
            ),
        )
    return _build_proof(documents, compiler_recipe_hash)


def validate_input_proof(value: object) -> dict[str, object]:
    """Validate and return a sorted, detached canonical proof value."""

    proof = _mapping(value, "input proof")
    if set(proof) != _PROOF_KEYS:
        raise ValueError(
            "input proof must contain exactly "
            "schema_version, compiler_recipe_hash and documents"
        )
    schema_version = proof.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != INPUT_PROOF_SCHEMA_VERSION
    ):
        raise ValueError(
            f"input proof schema_version must equal {INPUT_PROOF_SCHEMA_VERSION}"
        )
    compiler_recipe_hash = _sha256(
        proof.get("compiler_recipe_hash"),
        "compiler_recipe_hash",
    )
    raw_documents = _mapping(proof.get("documents"), "documents")
    documents: dict[str, tuple[str, str]] = {}
    for raw_doc_key, raw_entry in raw_documents.items():
        doc_key = _doc_key(raw_doc_key, "documents key")
        entry = _mapping(raw_entry, f"documents[{doc_key!r}]")
        if set(entry) != _DOCUMENT_PROOF_KEYS:
            raise ValueError(
                f"documents[{doc_key!r}] must contain exactly "
                "content_hash and segment_recipe_hash"
            )
        documents[doc_key] = (
            _sha256(
                entry.get("content_hash"),
                f"documents[{doc_key!r}].content_hash",
            ),
            _sha256(
                entry.get("segment_recipe_hash"),
                f"documents[{doc_key!r}].segment_recipe_hash",
            ),
        )
    return _build_proof(documents, compiler_recipe_hash)


__all__ = [
    "INPUT_PROOF_PATH",
    "INPUT_PROOF_SCHEMA_VERSION",
    "proof_from_fingerprints",
    "proof_from_segments",
    "validate_input_proof",
]
