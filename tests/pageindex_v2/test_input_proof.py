"""Tests for deterministic, content-bound Generation input proofs."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.index.v2.canonical import canonical_bytes, canonical_hash
from app.index.v2.input_proof import (
    proof_from_fingerprints,
    validate_input_proof,
)


def test_input_proof_ignores_document_insertion_order() -> None:
    left = proof_from_fingerprints(
        {"note:b": "b" * 64, "note:a": "a" * 64},
        "c" * 64,
        "d" * 64,
    )
    right = proof_from_fingerprints(
        {"note:a": "a" * 64, "note:b": "b" * 64},
        "c" * 64,
        "d" * 64,
    )

    assert canonical_bytes(left) == canonical_bytes(right)
    assert list(left["documents"]) == ["note:a", "note:b"]


def test_input_proof_changes_with_content_or_recipe() -> None:
    base = proof_from_fingerprints(
        {"note:a": "a" * 64},
        "b" * 64,
        "c" * 64,
    )
    changed_content = proof_from_fingerprints(
        {"note:a": "d" * 64},
        "b" * 64,
        "c" * 64,
    )
    changed_segment_recipe = proof_from_fingerprints(
        {"note:a": "a" * 64},
        "e" * 64,
        "c" * 64,
    )
    changed_compiler_recipe = proof_from_fingerprints(
        {"note:a": "a" * 64},
        "b" * 64,
        "f" * 64,
    )

    assert canonical_hash(base) != canonical_hash(changed_content)
    assert canonical_hash(base) != canonical_hash(changed_segment_recipe)
    assert canonical_hash(base) != canonical_hash(changed_compiler_recipe)


def test_validate_input_proof_returns_a_sorted_detached_copy() -> None:
    source = {
        "schema_version": 1,
        "compiler_recipe_hash": "d" * 64,
        "documents": {
            "note:b": {
                "content_hash": "b" * 64,
                "segment_recipe_hash": "c" * 64,
            },
            "note:a": {
                "segment_recipe_hash": "c" * 64,
                "content_hash": "a" * 64,
            },
        },
    }

    validated = validate_input_proof(source)
    source["documents"]["note:a"]["content_hash"] = "f" * 64

    assert list(validated["documents"]) == ["note:a", "note:b"]
    assert validated["documents"]["note:a"]["content_hash"] == "a" * 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda proof: proof.update(schema_version=True),
        lambda proof: proof.update(extra="not-allowed"),
        lambda proof: proof["documents"].update(
            {
                "": {
                    "content_hash": "a" * 64,
                    "segment_recipe_hash": "b" * 64,
                }
            }
        ),
        lambda proof: proof["documents"]["note:a"].update(
            content_hash="not-a-sha256"
        ),
        lambda proof: proof["documents"]["note:a"].update(extra="not-allowed"),
    ],
)
def test_validate_input_proof_rejects_noncanonical_shapes(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    proof = proof_from_fingerprints(
        {"note:a": "a" * 64},
        "b" * 64,
        "c" * 64,
    )
    mutate(proof)

    with pytest.raises(ValueError):
        validate_input_proof(proof)
