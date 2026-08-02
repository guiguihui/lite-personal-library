"""Contracts for bounded Normal validation and legacy Deep compatibility."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

import app.index.v2.validator as validator_module
from app.index.v2.artifacts import CandidateReceipt, write_canonical_object
from app.index.v2.canonical import canonical_hash
from app.index.v2.input_proof import INPUT_PROOF_PATH, proof_from_fingerprints
from app.index.v2.models import COMPILER_SCHEMA_VERSION, CompilerRecipe
from app.index.v2.object_store import put_segment
from app.index.v2.streaming_json import (
    BoundedJsonError,
    load_bounded_canonical_json,
    stream_file_digest,
)
from app.index.v2.validator import (
    ValidationMode,
    validate_candidate_normal,
)


def _receipt(tmp_path: Path) -> tuple[Path, CandidateReceipt]:
    pageindex = tmp_path / "pageindex"
    candidate = tmp_path / "candidate"
    content_hash = "a" * 64
    segment_recipe_hash = "b" * 64
    segment_ref = put_segment(
        pageindex,
        {
            "schema_version": 2,
            "document": {
                "doc_key": "note:alpha",
                "type": "note",
                "id": "alpha",
            },
            "fingerprint": {
                "content_hash": content_hash,
                "recipe_hash": segment_recipe_hash,
            },
        },
    )
    recipe = CompilerRecipe()
    recipe_payload = recipe.as_dict()
    recipe_hash = canonical_hash(recipe_payload)
    proof = proof_from_fingerprints(
        {"note:alpha": content_hash},
        segment_recipe_hash,
        recipe_hash,
    )
    proof_hash = canonical_hash(proof)
    documents = {"note:alpha": segment_ref.segment_hash}
    revision = canonical_hash(
        {
            "schema_version": COMPILER_SCHEMA_VERSION,
            "compiler_recipe_hash": recipe_hash,
            "input_proof_sha256": proof_hash,
            "documents": documents,
        }
    )

    artifacts = {}
    values = {
        "global-index.json": ({"docs": [{"id": "alpha", "type": "note"}]}, 1),
        "node-index.json": ({"nodes": []}, 0),
        "chunks.json": ({"chunks": []}, 0),
        "inverted-index.json": ({"num_chunks": 0, "postings": {}}, 0),
        "notes/alpha.json": ({"doc_name": "alpha", "structure": []}, None),
        INPUT_PROOF_PATH: (proof, 1),
    }
    for relative, (value, records) in values.items():
        reference = write_canonical_object(
            candidate / Path(*relative.split("/")),
            value,
            relative_path=relative,
            records=records,
        )
        artifacts[relative] = reference

    stats = {
        "documents": 1,
        "nodes": 0,
        "chunks": 0,
        "tokens": 0,
        "postings": 0,
    }
    pruning = {
        "body_min_df": recipe.body_df_min,
        "body_min_coverage": recipe.body_df_ratio,
    }
    manifest = {
        "schema_version": COMPILER_SCHEMA_VERSION,
        "generation": revision[:20],
        "revision_sha256": revision,
        "compiler_recipe_hash": recipe_hash,
        "input_proof_sha256": proof_hash,
        "compiler_recipe": recipe_payload,
        "documents": documents,
        "files": {
            relative: {
                "sha256": reference.sha256,
                "bytes": reference.byte_size,
            }
            for relative, reference in sorted(artifacts.items())
        },
        "stats": stats,
        "pruning": pruning,
        "warnings": [],
    }
    manifest_ref = write_canonical_object(
        candidate / "manifest.json",
        manifest,
        relative_path="manifest.json",
        records=len(artifacts),
    )
    generation_bytes = manifest_ref.byte_size + sum(
        reference.byte_size for reference in artifacts.values()
    )
    return pageindex, CandidateReceipt(
        candidate_dir=candidate,
        generation_id=revision[:20],
        revision_sha256=revision,
        compiler_recipe_hash=recipe_hash,
        input_proof_sha256=proof_hash,
        manifest_sha256=manifest_ref.sha256,
        artifacts=artifacts,
        segment_refs={segment_ref.doc_key: segment_ref},
        invariants={
            "stats": stats,
            "pruning": pruning,
            "generation_bytes_written": generation_bytes,
        },
    )


def test_validation_modes_are_stable_string_values() -> None:
    assert [mode.value for mode in ValidationMode] == [
        "normal",
        "sampled",
        "deep",
    ]


def test_normal_validation_never_deep_compiles_or_loads_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, receipt = _receipt(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Normal validation entered a Deep-only path")

    monkeypatch.setattr(validator_module, "compile_generation", forbidden)
    monkeypatch.setattr(validator_module, "load_segment", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)

    report = validate_candidate_normal(receipt, pageindex)

    assert report.ok, report.errors


def test_normal_validation_streams_every_artifact_hash_and_size(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _receipt(tmp_path)
    damaged = receipt.candidate_dir / "inverted-index.json"
    with damaged.open("ab") as stream:
        stream.write(b" ")

    report = validate_candidate_normal(receipt, pageindex)

    assert not report.ok
    assert "file_hash_mismatch" in report.error_codes
    assert "file_size_mismatch" in report.error_codes


def test_normal_validation_rejects_extra_and_unsafe_receipt_paths(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _receipt(tmp_path)
    write_canonical_object(
        receipt.candidate_dir / "extra.json",
        {"unexpected": True},
        relative_path="extra.json",
    )
    mutated = dict(receipt.artifacts)
    mutated["../escape.json"] = next(iter(receipt.artifacts.values()))
    object.__setattr__(receipt, "artifacts", mutated)

    report = validate_candidate_normal(receipt, pageindex)

    assert not report.ok
    assert "unexpected_file" in report.error_codes
    assert "receipt_path_invalid" in report.error_codes


def test_normal_validation_binds_proof_to_segment_ref(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _receipt(tmp_path)
    original = receipt.segment_refs["note:alpha"]
    object.__setattr__(
        receipt,
        "segment_refs",
        {"note:alpha": replace(original, content_hash="f" * 64)},
    )

    report = validate_candidate_normal(receipt, pageindex)

    assert not report.ok
    assert "input_proof_segment_mismatch" in report.error_codes


def test_streaming_helpers_do_not_require_path_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "control.json"
    payload = b'{"value":"streamed"}'
    path.write_bytes(payload)

    def forbidden(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("read_bytes would materialize the whole file")

    monkeypatch.setattr(Path, "read_bytes", forbidden)

    digest = stream_file_digest(path, chunk_size=3)
    assert digest.sha256 == hashlib.sha256(payload).hexdigest()
    assert digest.byte_size == len(payload)
    assert load_bounded_canonical_json(path, chunk_size=2) == {
        "value": "streamed"
    }


def test_bounded_control_reader_rejects_oversized_input(tmp_path: Path) -> None:
    path = tmp_path / "too-large.json"
    path.write_bytes(b'{"value":"too large"}')

    with pytest.raises(BoundedJsonError, match="exceeds"):
        load_bounded_canonical_json(path, max_bytes=4, chunk_size=2)
