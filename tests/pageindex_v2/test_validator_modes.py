"""Contracts for bounded Normal validation and legacy Deep compatibility."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import app.index.v2.validator as validator_module
from app.index.v2.artifacts import (
    ArtifactRef,
    CandidateReceipt,
    write_canonical_object,
)
from app.index.v2.canonical import canonical_hash
from app.index.v2.streaming_compiler import compile_generation_to_candidate
from app.index.v2.input_proof import INPUT_PROOF_PATH, proof_from_fingerprints
from app.index.v2.models import COMPILER_SCHEMA_VERSION, CompilerRecipe
from app.index.v2.object_store import put_segment
from app.index.v2.streaming_json import (
    BoundedJsonError,
    iter_canonical_array_items,
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
        "global-index.json": (
            {
                "docs": [
                    {
                        "author": "",
                        "date": "",
                        "description": "",
                        "id": "alpha",
                        "path": "/notes/",
                        "source_title": "",
                        "source_type": "",
                        "tags": [],
                        "title": "alpha",
                        "type": "note",
                        "url": "/notes/alpha.html",
                    }
                ]
            },
            1,
        ),
        "node-index.json": ({"nodes": []}, 0),
        "chunks.json": ({"chunks": []}, 0),
        "inverted-index.json": ({"num_chunks": 0, "postings": {}}, 0),
        "notes/alpha.json": (
            {"doc_name": "alpha", "structure": [], "type": "note"},
            None,
        ),
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
        "tokens_before": 0,
        "tokens_after": 0,
        "postings_before": 0,
        "postings_after": 0,
        "body_tokens_pruned": 0,
        "body_postings_pruned": 0,
        "body_tf_pruned": 0,
        "estimated_bytes_saved": 0,
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


def _runtime_segment(
    doc_type: str,
    slug: str,
    *,
    node_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    doc_key = f"{doc_type}:{slug}"
    return {
        "schema_version": 2,
        "document": {
            "doc_key": doc_key,
            "type": doc_type,
            "id": slug,
        },
        "fingerprint": {
            "content_hash": canonical_hash([doc_key]),
            "recipe_hash": "b" * 64,
        },
        "nodes": [
            {
                "node_key": f"{doc_key}:{node_id}",
                "legacy_node_id": node_id,
                "title": node_id,
                "breadcrumb": [],
                "terms": [],
                "summary": "",
                "line_num": 0,
            }
            for node_id in node_ids
        ],
        "chunks": [],
        "postings": {},
        "document_tree": {},
    }


def _compiled_receipt(
    tmp_path: Path,
    *segments: dict[str, object],
) -> tuple[Path, CandidateReceipt]:
    pageindex = tmp_path / "pageindex"
    refs = [put_segment(pageindex, segment) for segment in segments]
    receipt = compile_generation_to_candidate(
        refs,
        pageindex,
        tmp_path / "candidate",
        CompilerRecipe(),
        max_run_bytes=128,
        merge_fan_in=2,
    )
    return pageindex, receipt

def _rebind_candidate(
    receipt: CandidateReceipt,
    *,
    replacements: dict[str, tuple[object | bytes | None, int | None]],
    stats: dict[str, object] | None = None,
    pruning: dict[str, object] | None = None,
) -> CandidateReceipt:
    """Rewrite artifacts and both attestations like a consistently buggy compiler."""

    artifacts = dict(receipt.artifacts)
    for relative, (value, records) in replacements.items():
        path = receipt.candidate_dir / Path(*relative.split("/"))
        if value is None:
            path.unlink()
            artifacts.pop(relative)
            continue
        if isinstance(value, bytes):
            path.write_bytes(value)
            artifacts[relative] = ArtifactRef(
                relative_path=relative,
                sha256=hashlib.sha256(value).hexdigest(),
                byte_size=len(value),
                records=records,
            )
            continue
        artifacts[relative] = write_canonical_object(
            path,
            value,
            relative_path=relative,
            records=records,
        )

    manifest_path = receipt.candidate_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = {
        relative: {
            "sha256": reference.sha256,
            "bytes": reference.byte_size,
        }
        for relative, reference in sorted(artifacts.items())
    }
    if stats is not None:
        manifest["stats"] = stats
    if pruning is not None:
        manifest["pruning"] = pruning
    manifest_ref = write_canonical_object(
        manifest_path,
        manifest,
        relative_path="manifest.json",
        records=len(artifacts),
    )
    invariants = dict(receipt.invariants)
    invariants["stats"] = manifest["stats"]
    invariants["pruning"] = manifest["pruning"]
    invariants["generation_bytes_written"] = manifest_ref.byte_size + sum(
        reference.byte_size for reference in artifacts.values()
    )
    return replace(
        receipt,
        manifest_sha256=manifest_ref.sha256,
        artifacts=artifacts,
        invariants=invariants,
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


@pytest.mark.parametrize(
    "missing_relative",
    ["inverted-index.json", "notes/alpha.json", INPUT_PROOF_PATH],
)
def test_normal_validation_requires_independently_derived_artifacts(
    tmp_path: Path,
    missing_relative: str,
) -> None:
    pageindex, receipt = _receipt(tmp_path)
    rebound = _rebind_candidate(
        receipt,
        replacements={missing_relative: (None, None)},
    )

    report = validate_candidate_normal(rebound, pageindex)

    assert not report.ok
    assert "required_artifact_missing" in report.error_codes


def test_normal_validation_binds_optional_tree_identity_to_manifest(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _receipt(tmp_path)
    rebound = _rebind_candidate(
        receipt,
        replacements={
            "notes/alpha.json": (
                {"doc_name": "other", "structure": [], "type": "book"},
                None,
            )
        },
    )

    report = validate_candidate_normal(rebound, pageindex)

    assert not report.ok
    assert "tree_document_mismatch" in report.error_codes


def test_normal_validation_accepts_legacy_empty_tree_mapping(tmp_path: Path) -> None:
    pageindex, receipt = _receipt(tmp_path)
    rebound = _rebind_candidate(
        receipt,
        replacements={"notes/alpha.json": ({}, None)},
    )

    report = validate_candidate_normal(rebound, pageindex)

    assert report.ok, report.errors

def test_normal_validation_rejects_rebound_document_order(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _compiled_receipt(
        tmp_path,
        _runtime_segment("book", "alpha"),
        _runtime_segment("note", "zeta"),
    )
    global_index = json.loads(
        (receipt.candidate_dir / "global-index.json").read_text(encoding="utf-8")
    )
    global_index["docs"].reverse()
    rebound = _rebind_candidate(
        receipt,
        replacements={"global-index.json": (global_index, 2)},
    )

    report = validate_candidate_normal(rebound, pageindex)

    assert not report.ok
    assert "document_order_invalid" in report.error_codes


def test_normal_validation_rejects_rebound_legacy_node_order(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _compiled_receipt(
        tmp_path,
        _runtime_segment("note", "alpha", node_ids=("0002", "0001")),
    )
    node_index = json.loads(
        (receipt.candidate_dir / "node-index.json").read_text(encoding="utf-8")
    )
    node_index["nodes"].reverse()
    rebound = _rebind_candidate(
        receipt,
        replacements={"node-index.json": (node_index, 2)},
    )

    report = validate_candidate_normal(rebound, pageindex)

    assert not report.ok
    assert "node_order_invalid" in report.error_codes


def test_normal_validation_accepts_same_slug_across_document_types(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _compiled_receipt(
        tmp_path,
        _runtime_segment("book", "shared", node_ids=("0001",)),
        _runtime_segment("note", "shared", node_ids=("0001",)),
    )

    report = validate_candidate_normal(receipt, pageindex)

    assert report.ok, report.errors

def test_normal_validation_rejects_rebound_dangling_chunk_reference(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _receipt(tmp_path)
    stats = {
        "documents": 1,
        "nodes": 0,
        "chunks": 1,
        "tokens": 0,
        "postings": 0,
    }
    rebound = _rebind_candidate(
        receipt,
        replacements={
            "chunks.json": (
                {
                    "chunks": [
                        {
                            "body": "",
                            "breadcrumb": [],
                            "chunk_id": "c000001",
                            "doc_id": "alpha",
                            "line_num": 0,
                            "node_id": "missing",
                            "source_md": "",
                            "title": "",
                        }
                    ]
                },
                1,
            ),
            "inverted-index.json": (
                {"num_chunks": 1, "postings": {}},
                0,
            ),
        },
        stats=stats,
    )

    report = validate_candidate_normal(rebound, pageindex)

    assert not report.ok
    assert "chunk_unknown_node" in report.error_codes


def test_normal_validation_rejects_rebound_noncanonical_token_order(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _receipt(tmp_path)
    stats = {
        "documents": 1,
        "nodes": 0,
        "chunks": 0,
        "tokens": 2,
        "postings": 0,
    }
    pruning = {
        "body_min_df": CompilerRecipe().body_df_min,
        "body_min_coverage": CompilerRecipe().body_df_ratio,
        "tokens_before": 2,
        "tokens_after": 2,
        "postings_before": 0,
        "postings_after": 0,
        "body_tokens_pruned": 0,
        "body_postings_pruned": 0,
        "body_tf_pruned": 0,
        "estimated_bytes_saved": 0,
    }
    rebound = _rebind_candidate(
        receipt,
        replacements={
            "inverted-index.json": (
                b'{"num_chunks":0,"postings":{"z":[],"a":[]}}',
                2,
            )
        },
        stats=stats,
        pruning=pruning,
    )

    report = validate_candidate_normal(rebound, pageindex)

    assert not report.ok
    assert "file_not_canonical" in report.error_codes

def test_normal_validation_binds_pruning_policy_to_compiler_recipe(
    tmp_path: Path,
) -> None:
    pageindex, receipt = _receipt(tmp_path)
    pruning = dict(receipt.invariants["pruning"])
    pruning["body_min_df"] = int(pruning["body_min_df"]) + 1
    rebound = _rebind_candidate(
        receipt,
        replacements={},
        pruning=pruning,
    )

    report = validate_candidate_normal(rebound, pageindex)

    assert not report.ok
    assert "pruning_recipe_mismatch" in report.error_codes

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

def test_bounded_control_reader_wraps_nonfinite_number(tmp_path: Path) -> None:
    path = tmp_path / "nonfinite.json"
    path.write_bytes(b'{"value":NaN}')

    with pytest.raises(BoundedJsonError, match="cannot be encoded canonically"):
        load_bounded_canonical_json(path)


def test_streamed_array_reader_wraps_lone_surrogate(tmp_path: Path) -> None:
    path = tmp_path / "surrogate.json"
    path.write_bytes(b'{"docs":[{"value":"\\ud800"}]}')

    with pytest.raises(BoundedJsonError, match="cannot be encoded canonically"):
        list(
            iter_canonical_array_items(
                path,
                object_key="docs",
                chunk_size=2,
            )
        )
