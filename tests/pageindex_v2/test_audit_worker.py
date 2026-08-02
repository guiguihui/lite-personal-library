"""Tests for explicit, short-lived PageIndex Deep audit processes."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

import app.index.v2.audit_worker as audit_worker
from app.index.v2.audit_worker import AuditStatus, start_deep_audit
from app.index.v2.canonical import canonical_hash, write_json_atomic
from app.index.v2.compiler import compile_generation
from app.index.v2.models import CompilerRecipe, SegmentRecipe
from app.index.v2.object_store import put_segment
from app.index.v2.validator import materialize_candidate, validate_candidate_deep


def _segment() -> dict[str, object]:
    recipe = SegmentRecipe().as_dict()
    source_files = [{"path": "notes/alpha.md", "sha256": "0" * 64}]
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
        "nodes": [
            {
                "node_key": "n_alpha",
                "legacy_node_id": "0001",
                "title": "Alpha",
                "breadcrumb": ["Alpha"],
                "summary": "summary",
                "source_md": "content/notes/alpha.md",
                "line_num": 0,
                "line_end": 2,
            }
        ],
        "chunks": [
            {
                "local_id": 0,
                "node_key": "n_alpha",
                "title": "Alpha",
                "breadcrumb": ["Alpha"],
                "body": "searchable body",
                "source_md": "content/notes/alpha.md",
                "line_num": 0,
                "line_end": 2,
                "lengths": {"title": 1, "breadcrumb": 1, "body": 2},
            }
        ],
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


def _candidate(tmp_path: Path) -> tuple[Path, Path, str]:
    pageindex = tmp_path / "pageindex"
    segment = _segment()
    stored = put_segment(pageindex, segment)
    compiled = compile_generation([segment], CompilerRecipe())
    candidate = materialize_candidate(tmp_path / "candidate", compiled)
    return pageindex, candidate, stored.segment_hash


def _refresh_manifest_file(candidate: Path, relative: str) -> None:
    manifest_path = candidate / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = (candidate / relative).read_bytes()
    manifest["files"][relative] = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    write_json_atomic(manifest_path, manifest)


def test_deep_audit_runs_in_a_distinct_short_lived_process(tmp_path: Path) -> None:
    pageindex, candidate, _segment_hash = _candidate(tmp_path)

    handle = start_deep_audit(candidate, pageindex)

    assert handle.pid is not None
    assert handle.pid != os.getpid()
    result = handle.wait(timeout=20)
    assert result.status is AuditStatus.COMPLETED
    assert result.audit_pid == handle.pid
    assert result.validation is not None
    assert result.validation.ok
    assert result.audit_error is None
    assert handle.poll() is result


def test_semantic_errors_preserve_validator_order_and_protocol_round_trip(
    tmp_path: Path,
) -> None:
    pageindex, candidate, _segment_hash = _candidate(tmp_path)
    inverted_path = candidate / "inverted-index.json"
    inverted = json.loads(inverted_path.read_text(encoding="utf-8"))
    inverted["postings"]["broken"] = [[999999, 1]]
    write_json_atomic(inverted_path, inverted)
    _refresh_manifest_file(candidate, "inverted-index.json")
    expected = validate_candidate_deep(candidate, pageindex)

    first = start_deep_audit(candidate, pageindex).wait(timeout=20)
    second = start_deep_audit(candidate, pageindex).wait(timeout=20)

    assert not expected.ok
    assert first.status is AuditStatus.COMPLETED
    assert first.audit_error is None
    assert first.validation is not None
    assert first.validation.errors == expected.errors
    assert second.validation is not None
    assert second.validation.errors == expected.errors
    assert first.as_dict()["validation"]["errors"] == list(expected.errors)


def test_corrupt_segment_is_a_validation_failure_not_an_audit_error(
    tmp_path: Path,
) -> None:
    pageindex, candidate, segment_hash = _candidate(tmp_path)
    object_path = (
        pageindex
        / "objects"
        / "segments"
        / segment_hash[:2]
        / f"{segment_hash}.json"
    )
    object_path.write_text('{"broken":true}', encoding="utf-8")

    result = start_deep_audit(candidate, pageindex).wait(timeout=20)

    assert result.status is AuditStatus.COMPLETED
    assert result.audit_error is None
    assert result.validation is not None
    assert not result.validation.ok
    assert "segment_object_invalid" in result.validation.error_codes


def test_payload_tamper_is_reported_with_stable_deep_validator_error(
    tmp_path: Path,
) -> None:
    pageindex, candidate, _segment_hash = _candidate(tmp_path)
    chunks_path = candidate / "chunks.json"
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    chunks["chunks"][0]["body"] = "tampered after compilation"
    write_json_atomic(chunks_path, chunks)
    _refresh_manifest_file(candidate, "chunks.json")

    result = start_deep_audit(candidate, pageindex).wait(timeout=20)

    assert result.status is AuditStatus.COMPLETED
    assert result.audit_error is None
    assert result.validation is not None
    assert "compiled_payload_mismatch" in result.validation.error_codes


@pytest.mark.parametrize(
    ("script", "expected_code"),
    [
        ("raise SystemExit(17)", "process_failed"),
        ("print('not-json')", "protocol_error"),
    ],
)
def test_process_and_result_protocol_faults_are_audit_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    expected_code: str,
) -> None:
    pageindex, candidate, _segment_hash = _candidate(tmp_path)
    monkeypatch.setattr(
        audit_worker,
        "audit_worker_command",
        lambda **_kwargs: [sys.executable, "-c", script],
    )

    result = start_deep_audit(candidate, pageindex).wait(timeout=20)

    assert result.status is AuditStatus.AUDIT_ERROR
    assert result.validation is None
    assert result.audit_error is not None
    assert result.audit_error.code == expected_code


def test_launch_fault_is_returned_as_an_audit_error(tmp_path: Path) -> None:
    pageindex, candidate, _segment_hash = _candidate(tmp_path)

    handle = start_deep_audit(
        candidate,
        pageindex,
        executable=str(tmp_path / "missing-python-executable"),
    )
    result = handle.wait(timeout=20)

    assert handle.pid is None
    assert result.status is AuditStatus.AUDIT_ERROR
    assert result.validation is None
    assert result.audit_error is not None
    assert result.audit_error.code == "launch_failed"


def test_running_audit_can_be_cancelled_without_mutating_published_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, candidate, _segment_hash = _candidate(tmp_path)
    current_path = pageindex / "current.json"
    published_path = pageindex / "generations" / "published" / "sentinel.json"
    write_json_atomic(current_path, {"generation": "published"})
    write_json_atomic(published_path, {"unchanged": True})
    current_before = current_path.read_bytes()
    published_before = published_path.read_bytes()
    monkeypatch.setattr(
        audit_worker,
        "audit_worker_command",
        lambda **_kwargs: [
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.read(); time.sleep(60)",
        ],
    )
    handle = start_deep_audit(candidate, pageindex)

    result = handle.cancel(timeout=2)

    assert result.status is AuditStatus.CANCELLED
    assert result.validation is None
    assert result.audit_error is None
    assert current_path.read_bytes() == current_before
    assert published_path.read_bytes() == published_before
    assert handle.wait(timeout=1) is result
