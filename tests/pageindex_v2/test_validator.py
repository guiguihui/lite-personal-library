"""Tests for candidate materialization and structural validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.index.v2.canonical import canonical_hash, write_json_atomic
from app.index.v2.compiler import compile_generation
from app.index.v2.models import CompilerRecipe, SegmentRecipe
from app.index.v2.object_store import put_segment
from app.index.v2.validator import materialize_candidate, validate_candidate


def _segment() -> dict[str, object]:
    recipe = SegmentRecipe().as_dict()
    source_files = [
        {"path": "notes/alpha.md", "sha256": "0" * 64},
    ]
    return {
        "schema_version": 2,
        "segment_recipe": recipe,
        "document": {
            "doc_key": "note:alpha",
            "id": "alpha",
            "type": "note",
            "title": "Alpha",
            "author": "",
            "description": "",
            "tags": [],
        },
        "fingerprint": {
            "content_hash": canonical_hash(source_files),
            "recipe_hash": canonical_hash(recipe),
            "source_files": source_files,
        },
        "nodes": [{
            "node_key": "n_alpha",
            "legacy_node_id": "0001",
            "title": "Alpha",
            "breadcrumb": ["Alpha"],
            "summary": "summary",
            "source_md": "content/notes/alpha.md",
            "line_num": 0,
            "line_end": 2,
        }],
        "chunks": [{
            "local_id": 0,
            "node_key": "n_alpha",
            "title": "Alpha",
            "breadcrumb": ["Alpha"],
            "body": "searchable body",
            "source_md": "content/notes/alpha.md",
            "line_num": 0,
            "line_end": 2,
            "lengths": {"title": 1, "breadcrumb": 1, "body": 2},
        }],
        "postings": {
            "alpha": [[0, 1, 1, 0]],
            "body": [[0, 0, 0, 1]],
            "searchable": [[0, 0, 0, 1]],
        },
        "document_tree": {
            "doc_name": "alpha",
            "type": "note",
            "title": "Alpha",
            "structure": [],
        },
    }


def _candidate(
    tmp_path: Path,
    segment: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    pageindex = tmp_path / "pageindex"
    segment = segment or _segment()
    put_segment(pageindex, segment)
    compiled = compile_generation([segment], CompilerRecipe())
    candidate = materialize_candidate(tmp_path / "candidate", compiled)
    return pageindex, candidate


def test_materialized_candidate_validates(tmp_path: Path) -> None:
    pageindex, candidate = _candidate(tmp_path)
    report = validate_candidate(candidate, pageindex)
    assert report.ok, report.errors


def test_validator_rejects_dangling_posting_even_with_updated_file_hash(tmp_path: Path) -> None:
    pageindex, candidate = _candidate(tmp_path)
    inverted_path = candidate / "inverted-index.json"
    inverted = json.loads(inverted_path.read_text(encoding="utf-8"))
    inverted["postings"]["broken"] = [[999999, 1]]
    write_json_atomic(inverted_path, inverted)

    manifest_path = candidate / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = inverted_path.read_bytes()
    import hashlib

    manifest["files"]["inverted-index.json"] = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    write_json_atomic(manifest_path, manifest)

    report = validate_candidate(candidate, pageindex)
    assert not report.ok
    assert "posting_unknown_chunk" in report.error_codes
    assert "compiled_payload_mismatch" in report.error_codes


def test_validator_rejects_corrupt_segment_object(tmp_path: Path) -> None:
    pageindex, candidate = _candidate(tmp_path)
    manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
    segment_hash = next(iter(manifest["documents"].values()))
    object_path = pageindex / "objects" / "segments" / segment_hash[:2] / f"{segment_hash}.json"
    object_path.write_text('{"broken":true}', encoding="utf-8")

    report = validate_candidate(candidate, pageindex)
    assert not report.ok
    assert "segment_object_invalid" in report.error_codes


def test_validator_rejects_missing_generation_file(tmp_path: Path) -> None:
    pageindex, candidate = _candidate(tmp_path)
    (candidate / "chunks.json").unlink()

    report = validate_candidate(candidate, pageindex)
    assert not report.ok
    assert "file_missing" in report.error_codes


def test_validator_rejects_segment_recipe_hash_mismatch(tmp_path: Path) -> None:
    segment = _segment()
    fingerprint = segment["fingerprint"]
    assert isinstance(fingerprint, dict)
    fingerprint["recipe_hash"] = "0" * 64
    pageindex, candidate = _candidate(tmp_path, segment)

    report = validate_candidate(candidate, pageindex)

    assert not report.ok
    assert "segment_recipe_hash_mismatch" in report.error_codes


def test_validator_rejects_unsupported_segment_recipe(tmp_path: Path) -> None:
    segment = _segment()
    recipe = segment["segment_recipe"]
    fingerprint = segment["fingerprint"]
    assert isinstance(recipe, dict)
    assert isinstance(fingerprint, dict)
    recipe["tokenizer_version"] = "unknown-tokenizer-v99"
    fingerprint["recipe_hash"] = canonical_hash(recipe)
    pageindex, candidate = _candidate(tmp_path, segment)

    report = validate_candidate(candidate, pageindex)

    assert not report.ok
    assert "segment_recipe_invalid" in report.error_codes


def test_validator_rejects_segment_content_hash_mismatch(tmp_path: Path) -> None:
    segment = _segment()
    fingerprint = segment["fingerprint"]
    assert isinstance(fingerprint, dict)
    fingerprint["content_hash"] = "f" * 64
    pageindex, candidate = _candidate(tmp_path, segment)

    report = validate_candidate(candidate, pageindex)

    assert not report.ok
    assert "segment_content_hash_mismatch" in report.error_codes


def test_validator_recomputes_segment_field_postings(tmp_path: Path) -> None:
    segment = _segment()
    postings = segment["postings"]
    assert isinstance(postings, dict)
    del postings["alpha"]
    pageindex, candidate = _candidate(tmp_path, segment)

    report = validate_candidate(candidate, pageindex)

    assert not report.ok
    assert "segment_postings_mismatch" in report.error_codes


def test_materializer_rejects_windows_drive_relative_payload_path(
    tmp_path: Path,
) -> None:
    segment = _segment()
    compiled = compile_generation([segment], CompilerRecipe())
    compiled.payloads["C:/outside.json"] = {"unsafe": True}

    with pytest.raises(ValueError, match="invalid generation path"):
        materialize_candidate(tmp_path / "candidate", compiled)
