from __future__ import annotations

from pathlib import Path

import pytest

import app.index.v3.worker as worker_module
from app.index.v3.protocol import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    BuildRequest,
    BuildResult,
    GenerationAttestation,
    ParentAttestation,
    ViewAttestation,
    decode_result_line,
    encode_result_line,
)
from app.index.v3.models import CompactionPolicy
from app.index.v3.view_store import load_search_view_metadata
from app.index.v3.worker import BuildCancelled, execute_request


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _corpus(root: Path) -> Path:
    content = root / "content"
    _write(
        content / "books" / "alpha" / "_index.md",
        "---\ntitle: Alpha\n---\n",
    )
    _write(
        content / "books" / "alpha" / "chapter.md",
        "---\ntitle: Chapter\n---\n# Chapter\nalpha body token\n",
    )
    _write(
        content / "papers" / "beta" / "_index.md",
        "---\ntitle: Beta\n---\n# Beta\nbeta body token\n",
    )
    _write(
        content / "notes" / "welcome.md",
        "---\ntitle: Welcome\n---\n# Welcome\nwelcome body token\n",
    )
    return content


def _request(
    content: Path,
    pageindex: Path,
    job_id: str,
    *,
    parent: ParentAttestation | None = None,
    mode: str = "incremental",
) -> BuildRequest:
    return BuildRequest(
        protocol=PROTOCOL_NAME,
        protocol_version=PROTOCOL_VERSION,
        job_id=job_id,
        mode=mode,  # type: ignore[arg-type]
        content_dir=content,
        pageindex_dir=pageindex,
        parent=parent,
        legacy_export="none",
    )


def _parent(result) -> ParentAttestation:
    assert result.generation is not None
    assert result.view is not None
    return ParentAttestation(result.generation, result.view)


def _assert_strict_result(
    result: BuildResult, request: BuildRequest
) -> BuildResult:
    assert decode_result_line(encode_result_line(result), request=request) == result
    return result


def test_bootstrap_then_no_op_returns_before_segment_ref_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    bootstrap = execute_request(
        _request(content, pageindex, "idx_bootstrap")
    )
    assert bootstrap.state == "ready_to_publish"
    assert bootstrap.metrics.segments_rebuilt == 3
    assert bootstrap.metrics.segments_loaded == 3
    assert bootstrap.metrics.legacy_compile_runs == 0
    assert bootstrap.metrics.normal_validation_runs == 1

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no-op crossed the Segment/ref boundary")

    monkeypatch.setattr(worker_module, "validate_generation_stream", forbidden)
    monkeypatch.setattr(worker_module, "build_segment", forbidden)
    monkeypatch.setattr(worker_module, "build_delta_view", forbidden)
    no_op = execute_request(
        _request(
            content,
            pageindex,
            "idx_no_op",
            parent=_parent(bootstrap),
        )
    )
    assert no_op.state == "no_op"
    assert no_op.generation == bootstrap.generation
    assert no_op.view == bootstrap.view
    assert no_op.metrics.segments_loaded == 0
    assert no_op.metrics.segments_rebuilt == 0
    assert no_op.metrics.postings_visited == 0
    assert no_op.metrics.bytes_written == 0
    assert no_op.metrics.normal_validation_runs == 0


def test_edit_delete_and_explicit_optimize_have_bounded_mechanisms(
    tmp_path: Path,
) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    bootstrap = execute_request(
        _request(content, pageindex, "idx_seed")
    )
    assert bootstrap.state == "ready_to_publish"

    note = content / "notes" / "welcome.md"
    note.write_text(
        note.read_text(encoding="utf-8") + "\nsingle document edit\n",
        encoding="utf-8",
    )
    edited = execute_request(
        _request(
            content,
            pageindex,
            "idx_edit",
            parent=_parent(bootstrap),
        )
    )
    assert edited.state == "ready_to_publish"
    assert edited.generation != bootstrap.generation
    assert edited.metrics.segments_rebuilt == 1
    assert edited.metrics.segments_deleted == 0
    assert edited.metrics.segments_loaded <= 2
    assert edited.metrics.segments_loaded_peak <= 1
    assert edited.metrics.base_postings_scanned == 0
    assert edited.metrics.legacy_compile_runs == 0
    assert edited.metrics.normal_validation_runs == 1
    assert edited.view is not None
    edited_view = load_search_view_metadata(pageindex, edited.view.view_id)
    assert len(edited_view.delta_ids) == 1

    note.unlink()
    deleted = execute_request(
        _request(
            content,
            pageindex,
            "idx_delete",
            parent=_parent(edited),
        )
    )
    assert deleted.state == "ready_to_publish"
    assert deleted.metrics.segments_rebuilt == 0
    assert deleted.metrics.segments_deleted == 1
    assert deleted.metrics.segments_loaded == 0
    assert deleted.metrics.segments_loaded_peak == 0
    assert deleted.metrics.legacy_compile_runs == 0
    assert deleted.view is not None
    deleted_view = load_search_view_metadata(pageindex, deleted.view.view_id)
    assert len(deleted_view.delta_ids) == 2

    optimized = execute_request(
        _request(
            content,
            pageindex,
            "idx_optimize",
            parent=_parent(deleted),
            mode="optimize",
        )
    )
    assert optimized.state == "ready_to_publish"
    assert optimized.generation == deleted.generation
    assert optimized.view != deleted.view
    assert optimized.metrics.legacy_compile_runs == 0
    assert optimized.metrics.segments_loaded == 2
    assert optimized.metrics.normal_validation_runs == 1
    assert optimized.view is not None
    optimized_view = load_search_view_metadata(pageindex, optimized.view.view_id)
    assert optimized_view.delta_ids == ()


def test_pre_cancelled_request_returns_strict_cancelled_result(
    tmp_path: Path,
) -> None:
    content = _corpus(tmp_path)
    request = _request(content, tmp_path / "pageindex", "idx_cancelled")

    def cancelled() -> None:
        raise BuildCancelled("cancel before parent authentication")

    result = _assert_strict_result(
        execute_request(request, check_cancelled=cancelled), request
    )
    assert result.state == "cancelled"
    assert result.error is None
    assert result.generation is None
    assert result.view is None


def test_valid_request_execution_failure_returns_strict_failed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)
    request = _request(content, tmp_path / "pageindex", "idx_failed")

    def fail_capture(*_args, **_kwargs):
        raise RuntimeError("injected capture failure")

    monkeypatch.setattr(worker_module, "_capture_snapshot", fail_capture)
    result = _assert_strict_result(execute_request(request), request)
    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == "build_failed"
    assert "RuntimeError: injected capture failure" in result.error.message
    assert result.generation is None
    assert result.view is None


@pytest.mark.parametrize("tamper", ("manifest_bytes", "attested_hash"))
def test_parent_generation_manifest_or_hash_tamper_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    bootstrap = execute_request(_request(content, pageindex, "idx_parent_seed"))
    assert bootstrap.state == "ready_to_publish"
    parent = _parent(bootstrap)

    if tamper == "manifest_bytes":
        manifest = parent.generation.generation_dir / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
    else:
        parent = ParentAttestation(
            GenerationAttestation(
                generation=parent.generation.generation,
                generation_dir=parent.generation.generation_dir,
                manifest_sha256="f" * 64,
            ),
            ViewAttestation(
                view_id=parent.view.view_id,
                view_dir=parent.view.view_dir,
                manifest_sha256=parent.view.manifest_sha256,
                generation=parent.view.generation,
                generation_manifest_sha256="f" * 64,
            ),
        )

    request = _request(
        content,
        pageindex,
        f"idx_parent_tamper_{tamper}",
        parent=parent,
    )
    result = _assert_strict_result(execute_request(request), request)
    assert result.state == "failed"
    assert result.error is not None
    assert "parent Generation manifest attestation mismatch" in result.error.message


def test_parent_search_view_recipe_mismatch_fails_before_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    bootstrap = execute_request(_request(content, pageindex, "idx_recipe_seed"))
    assert bootstrap.state == "ready_to_publish"

    original_recipe = worker_module.SearchViewRecipe

    class DifferentSearchViewRecipe:
        def as_dict(self) -> dict[str, object]:
            return {
                **original_recipe().as_dict(),
                "posting_codec_version": "future-incompatible-codec",
            }

    monkeypatch.setattr(
        worker_module, "SearchViewRecipe", DifferentSearchViewRecipe
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recipe mismatch must fail before source capture")

    monkeypatch.setattr(worker_module, "_capture_snapshot", forbidden)
    request = _request(
        content,
        pageindex,
        "idx_recipe_mismatch",
        parent=_parent(bootstrap),
    )
    result = _assert_strict_result(execute_request(request), request)
    assert result.state == "failed"
    assert result.error is not None
    assert "parent SearchViewRecipe differs" in result.error.message


def test_generation_destination_conflict_fails_without_clobbering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    occupied: list[Path] = []

    def inject_conflict(_source: Path, destination: Path) -> None:
        destination.mkdir(parents=True)
        marker = destination / "occupied-by-other-writer"
        marker.write_text("do not clobber", encoding="utf-8")
        occupied.append(marker)
        raise FileExistsError(str(destination))

    monkeypatch.setattr(worker_module, "_rename_no_replace", inject_conflict)
    request = _request(content, pageindex, "idx_generation_conflict")
    result = _assert_strict_result(execute_request(request), request)
    assert result.state == "failed"
    assert result.error is not None
    assert "logical Generation directory has an invalid file set" in result.error.message
    assert len(occupied) == 1
    assert occupied[0].read_text(encoding="utf-8") == "do not clobber"


def test_dirty_retry_that_returns_to_parent_proof_fails_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    bootstrap = execute_request(_request(content, pageindex, "idx_retry_seed"))
    assert bootstrap.state == "ready_to_publish"
    note = content / "notes" / "welcome.md"
    parent_bytes = note.read_bytes()
    note.write_bytes(parent_bytes + b"\ntransient edit\n")

    attempts = 0

    def revert_then_retry(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        note.write_bytes(parent_bytes)
        raise worker_module._DirtySourceChanged("injected dirty race")

    monkeypatch.setattr(worker_module, "_build_dirty_segments", revert_then_retry)
    request = _request(
        content,
        pageindex,
        "idx_retry_reverted",
        parent=_parent(bootstrap),
    )
    result = _assert_strict_result(execute_request(request), request)
    assert attempts == 1
    assert result.state == "failed"
    assert result.error is not None
    assert (
        "source returned to the parent proof after a dirty retry; "
        "rerun the incremental request"
    ) in result.error.message


def test_delta_chain_no_op_preserves_compaction_recommendation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    bootstrap = execute_request(_request(content, pageindex, "idx_compact_seed"))
    assert bootstrap.state == "ready_to_publish"
    note = content / "notes" / "welcome.md"
    note.write_text(
        note.read_text(encoding="utf-8") + "\ncreate one Delta\n",
        encoding="utf-8",
    )
    edited = execute_request(
        _request(
            content,
            pageindex,
            "idx_compact_edit",
            parent=_parent(bootstrap),
        )
    )
    assert edited.state == "ready_to_publish"
    assert edited.view is not None
    assert load_search_view_metadata(pageindex, edited.view.view_id).delta_ids

    monkeypatch.setattr(
        worker_module,
        "CompactionPolicy",
        lambda: CompactionPolicy(max_delta_layers=1),
    )
    request = _request(
        content,
        pageindex,
        "idx_compact_no_op",
        parent=_parent(edited),
    )
    result = _assert_strict_result(execute_request(request), request)
    assert result.state == "no_op"
    assert result.metrics.compaction_recommended is True
