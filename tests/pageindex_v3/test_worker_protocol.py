from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.index.v3.protocol import (
    MAX_JSON_LINE_BYTES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    BuildRequest,
    BuildResult,
    GenerationAttestation,
    LegacyExportAttestation,
    ParentAttestation,
    ProtocolError,
    ViewAttestation,
    WorkerError,
    WorkerMetrics,
    decode_request_line,
    decode_result_line,
    encode_request_line,
    encode_result_line,
)


GENERATION = "1" * 64
GENERATION_MANIFEST = "2" * 64
VIEW = "3" * 64
VIEW_MANIFEST = "4" * 64
NEXT_GENERATION = "5" * 64
NEXT_GENERATION_MANIFEST = "6" * 64
NEXT_VIEW = "7" * 64
NEXT_VIEW_MANIFEST = "8" * 64


def _generation(pageindex: Path, *, next_: bool = False) -> GenerationAttestation:
    generation = NEXT_GENERATION if next_ else GENERATION
    return GenerationAttestation(
        generation=generation,
        generation_dir=pageindex / "generations" / generation,
        manifest_sha256=(
            NEXT_GENERATION_MANIFEST if next_ else GENERATION_MANIFEST
        ),
    )


def _view(pageindex: Path, *, next_: bool = False) -> ViewAttestation:
    return ViewAttestation(
        view_id=NEXT_VIEW if next_ else VIEW,
        view_dir=pageindex / "views" / (NEXT_VIEW if next_ else VIEW),
        manifest_sha256=NEXT_VIEW_MANIFEST if next_ else VIEW_MANIFEST,
        generation=NEXT_GENERATION if next_ else GENERATION,
        generation_manifest_sha256=(
            NEXT_GENERATION_MANIFEST if next_ else GENERATION_MANIFEST
        ),
    )


def _parent(pageindex: Path) -> ParentAttestation:
    return ParentAttestation(
        generation=_generation(pageindex),
        view=_view(pageindex),
    )


def _request(
    tmp_path: Path,
    *,
    mode: str = "incremental",
    parent: ParentAttestation | None = None,
    legacy_export: str = "none",
) -> BuildRequest:
    return BuildRequest(
        protocol=PROTOCOL_NAME,
        protocol_version=PROTOCOL_VERSION,
        job_id="idx_test-1",
        mode=mode,  # type: ignore[arg-type]
        content_dir=tmp_path / "content",
        pageindex_dir=tmp_path / "pageindex",
        parent=parent,
        legacy_export=legacy_export,  # type: ignore[arg-type]
    )


def _ready_result(
    request: BuildRequest,
    *,
    generation: GenerationAttestation | None = None,
    view: ViewAttestation | None = None,
    legacy_artifact: LegacyExportAttestation | None = None,
    metrics: WorkerMetrics | None = None,
) -> BuildResult:
    pageindex = request.pageindex_dir
    return BuildResult(
        protocol=PROTOCOL_NAME,
        protocol_version=PROTOCOL_VERSION,
        job_id=request.job_id,
        mode=request.mode,
        legacy_export=request.legacy_export,
        state="ready_to_publish",
        parent=request.parent,
        generation=generation or _generation(pageindex, next_=True),
        view=view or _view(pageindex, next_=True),
        legacy_export_artifact=legacy_artifact,
        metrics=metrics
        or WorkerMetrics.empty(
            normal_validation_runs=1,
            segments_rebuilt=1,
            segments_loaded=1,
            segments_loaded_peak=1,
            bytes_written=42,
        ),
        error=None,
    )


def test_incremental_request_without_parent_round_trips_as_one_json_line(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    encoded = encode_request_line(request)

    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert decode_request_line(encoded) == request
    assert json.loads(encoded) == request.as_dict()


def test_complete_parent_pair_round_trips_and_is_generation_bound(
    tmp_path: Path,
) -> None:
    pageindex = (tmp_path / "pageindex").resolve()
    request = _request(tmp_path, parent=_parent(pageindex))

    parsed = BuildRequest.from_dict(request.as_dict())

    assert parsed == request
    assert parsed.parent is not None
    assert parsed.parent.view.generation == parsed.parent.generation.generation
    assert (
        parsed.parent.view.generation_manifest_sha256
        == parsed.parent.generation.manifest_sha256
    )


@pytest.mark.parametrize("missing", ["generation", "view"])
def test_request_rejects_mixed_or_missing_parent_pair(
    tmp_path: Path, missing: str
) -> None:
    pageindex = (tmp_path / "pageindex").resolve()
    payload = _request(tmp_path, parent=_parent(pageindex)).as_dict()
    assert isinstance(payload["parent"], dict)
    del payload["parent"][missing]

    with pytest.raises(ProtocolError, match="parent fields"):
        BuildRequest.from_dict(payload)


def test_request_rejects_parent_view_bound_to_another_generation(
    tmp_path: Path,
) -> None:
    pageindex = (tmp_path / "pageindex").resolve()
    payload = _request(tmp_path, parent=_parent(pageindex)).as_dict()
    assert isinstance(payload["parent"], dict)
    assert isinstance(payload["parent"]["view"], dict)
    payload["parent"]["view"]["generation"] = NEXT_GENERATION

    with pytest.raises(ProtocolError, match="IDs do not match"):
        BuildRequest.from_dict(payload)


def test_optimize_requires_a_complete_parent_pair(tmp_path: Path) -> None:
    with pytest.raises(ProtocolError, match="optimize.*parent"):
        _request(tmp_path, mode="optimize")


@pytest.mark.parametrize("legacy_export", ["", "partial", True, 1, None])
def test_request_rejects_malformed_legacy_export(
    tmp_path: Path, legacy_export: object
) -> None:
    payload = _request(tmp_path).as_dict()
    payload["legacy_export"] = legacy_export

    with pytest.raises(ProtocolError, match="legacy_export"):
        BuildRequest.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("protocol_version", True, "integer 1"),
        ("protocol_version", 2, "must equal 1"),
        ("protocol", "pageindex-v2-worker", "pageindex-v3-worker"),
        ("job_id", "../escape", "unsafe"),
        ("mode", "full", "mode"),
    ],
)
def test_request_rejects_version_confusion_unsafe_ids_and_unknown_modes(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    payload = _request(tmp_path).as_dict()
    payload[field] = value

    with pytest.raises(ProtocolError, match=message):
        BuildRequest.from_dict(payload)


def test_v2_schema_one_request_cannot_be_confused_with_p3_protocol(
    tmp_path: Path,
) -> None:
    v2_payload = {
        "schema_version": 1,
        "job_id": "idx_test",
        "mode": "incremental",
        "content_dir": str((tmp_path / "content").resolve()),
        "pageindex_dir": str((tmp_path / "pageindex").resolve()),
        "base_generation": None,
    }

    with pytest.raises(ProtocolError, match="fields do not match"):
        BuildRequest.from_dict(v2_payload)


def test_request_rejects_extra_and_missing_fields(tmp_path: Path) -> None:
    payload = _request(tmp_path).as_dict()
    payload["surprise"] = 1
    with pytest.raises(ProtocolError, match="extra=.*surprise"):
        BuildRequest.from_dict(payload)

    payload = _request(tmp_path).as_dict()
    del payload["legacy_export"]
    with pytest.raises(ProtocolError, match="missing=.*legacy_export"):
        BuildRequest.from_dict(payload)


def test_request_rejects_relative_and_out_of_scope_parent_paths(
    tmp_path: Path,
) -> None:
    payload = _request(tmp_path).as_dict()
    payload["content_dir"] = "relative/content"
    with pytest.raises(ProtocolError, match="absolute"):
        BuildRequest.from_dict(payload)

    pageindex = (tmp_path / "pageindex").resolve()
    payload = _request(tmp_path, parent=_parent(pageindex)).as_dict()
    assert isinstance(payload["parent"], dict)
    assert isinstance(payload["parent"]["generation"], dict)
    payload["parent"]["generation"]["generation_dir"] = str(
        (tmp_path / "elsewhere" / GENERATION).resolve()
    )
    with pytest.raises(ProtocolError, match="pageindex_dir/generations"):
        BuildRequest.from_dict(payload)


def test_request_rejects_unsafe_digest_and_nested_extra_key(tmp_path: Path) -> None:
    pageindex = (tmp_path / "pageindex").resolve()
    payload = _request(tmp_path, parent=_parent(pageindex)).as_dict()
    assert isinstance(payload["parent"], dict)
    assert isinstance(payload["parent"]["view"], dict)
    payload["parent"]["view"]["view_id"] = "../view"
    with pytest.raises(ProtocolError, match="lowercase SHA-256"):
        BuildRequest.from_dict(payload)

    payload = _request(tmp_path, parent=_parent(pageindex)).as_dict()
    assert isinstance(payload["parent"], dict)
    assert isinstance(payload["parent"]["view"], dict)
    payload["parent"]["view"]["extra"] = False
    with pytest.raises(ProtocolError, match="view attestation fields"):
        BuildRequest.from_dict(payload)


def test_ready_result_round_trips_and_attests_both_manifests(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    result = _ready_result(request)

    encoded = encode_result_line(result)
    parsed = decode_result_line(encoded, request=request)

    assert parsed == result
    assert parsed.generation is not None
    assert parsed.view is not None
    assert parsed.view.generation == parsed.generation.generation
    assert (
        parsed.view.generation_manifest_sha256
        == parsed.generation.manifest_sha256
    )


def test_no_op_must_return_the_exact_trusted_parent_and_zero_logical_work(
    tmp_path: Path,
) -> None:
    pageindex = (tmp_path / "pageindex").resolve()
    request = _request(tmp_path, parent=_parent(pageindex))
    assert request.parent is not None
    result = BuildResult(
        protocol=PROTOCOL_NAME,
        protocol_version=PROTOCOL_VERSION,
        job_id=request.job_id,
        mode="incremental",
        legacy_export="none",
        state="no_op",
        parent=request.parent,
        generation=request.parent.generation,
        view=request.parent.view,
        legacy_export_artifact=None,
        metrics=WorkerMetrics.empty(source_hash_ms=2),
        error=None,
    )
    assert decode_result_line(encode_result_line(result), request=request) == result

    payload = result.as_dict()
    payload["generation"] = _generation(pageindex, next_=True).as_dict()
    payload["view"] = _view(pageindex, next_=True).as_dict()
    with pytest.raises(ProtocolError, match="must equal the trusted parent"):
        BuildResult.from_dict(payload, request=request)

    payload = result.as_dict()
    assert isinstance(payload["metrics"], dict)
    payload["metrics"]["bytes_written"] = 1
    with pytest.raises(ProtocolError, match="reports logical build work"):
        BuildResult.from_dict(payload, request=request)


def test_ready_result_requires_both_attestations_and_one_normal_validation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    payload = _ready_result(request).as_dict()
    payload["view"] = None
    with pytest.raises(ProtocolError, match="both generation and view"):
        BuildResult.from_dict(payload, request=request)

    payload = _ready_result(request).as_dict()
    assert isinstance(payload["metrics"], dict)
    payload["metrics"]["normal_validation_runs"] = 0
    with pytest.raises(ProtocolError, match="exactly one Normal"):
        BuildResult.from_dict(payload, request=request)


def test_unchanged_incremental_result_must_use_no_op_state(tmp_path: Path) -> None:
    pageindex = (tmp_path / "pageindex").resolve()
    parent = _parent(pageindex)
    request = _request(tmp_path, parent=parent)

    with pytest.raises(ProtocolError, match="must use state='no_op'"):
        _ready_result(
            request,
            generation=parent.generation,
            view=parent.view,
        )


def test_optimize_preserves_generation_and_changes_view(tmp_path: Path) -> None:
    pageindex = (tmp_path / "pageindex").resolve()
    parent = _parent(pageindex)
    request = _request(tmp_path, mode="optimize", parent=parent)
    new_view = ViewAttestation(
        view_id=NEXT_VIEW,
        view_dir=pageindex / "views" / NEXT_VIEW,
        manifest_sha256=NEXT_VIEW_MANIFEST,
        generation=GENERATION,
        generation_manifest_sha256=GENERATION_MANIFEST,
    )
    result = _ready_result(
        request,
        generation=parent.generation,
        view=new_view,
    )
    assert decode_result_line(encode_result_line(result), request=request) == result

    payload = result.as_dict()
    payload["view"] = parent.view.as_dict()
    with pytest.raises(ProtocolError, match="new view_id"):
        BuildResult.from_dict(payload, request=request)

    payload = result.as_dict()
    payload["generation"] = _generation(pageindex, next_=True).as_dict()
    payload["view"] = _view(pageindex, next_=True).as_dict()
    with pytest.raises(ProtocolError, match="preserve.*Generation"):
        BuildResult.from_dict(payload, request=request)


def test_full_legacy_export_requires_separate_attestation_and_counters(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, legacy_export="full")
    export = LegacyExportAttestation(
        generation=NEXT_GENERATION,
        export_id="export-1",
        export_dir=(
            request.pageindex_dir
            / "exports"
            / "legacy"
            / NEXT_GENERATION
            / "export-1"
        ),
        manifest_sha256="9" * 64,
    )
    result = _ready_result(
        request,
        legacy_artifact=export,
        metrics=WorkerMetrics.empty(
            normal_validation_runs=1,
            legacy_compile_runs=1,
            legacy_postings_visited=10,
            legacy_bytes_written=100,
            legacy_export_ms=5,
        ),
    )
    assert decode_result_line(encode_result_line(result), request=request) == result

    payload = result.as_dict()
    payload["legacy_export_artifact"] = None
    with pytest.raises(ProtocolError, match="requires an export attestation"):
        BuildResult.from_dict(payload, request=request)


@pytest.mark.parametrize("metric", ["source_hash_ms", "segments_loaded", "bytes_written"])
def test_metrics_reject_booleans_as_integers(tmp_path: Path, metric: str) -> None:
    request = _request(tmp_path)
    payload = _ready_result(request).as_dict()
    assert isinstance(payload["metrics"], dict)
    payload["metrics"][metric] = True

    with pytest.raises(ProtocolError, match=metric):
        BuildResult.from_dict(payload, request=request)


def test_metrics_reject_extra_missing_and_impossible_peak(tmp_path: Path) -> None:
    request = _request(tmp_path)
    payload = _ready_result(request).as_dict()
    assert isinstance(payload["metrics"], dict)
    payload["metrics"]["mystery"] = 1
    with pytest.raises(ProtocolError, match="metrics fields"):
        BuildResult.from_dict(payload, request=request)

    payload = _ready_result(request).as_dict()
    assert isinstance(payload["metrics"], dict)
    del payload["metrics"]["postings_visited"]
    with pytest.raises(ProtocolError, match="metrics fields"):
        BuildResult.from_dict(payload, request=request)

    payload = _ready_result(request).as_dict()
    assert isinstance(payload["metrics"], dict)
    payload["metrics"]["segments_loaded_peak"] = 2
    with pytest.raises(ProtocolError, match="must not exceed"):
        BuildResult.from_dict(payload, request=request)


def test_result_rejects_request_rebinding_and_out_of_scope_paths(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    payload = _ready_result(request).as_dict()
    payload["job_id"] = "idx_other"
    with pytest.raises(ProtocolError, match="job_id does not match"):
        BuildResult.from_dict(payload, request=request)

    payload = _ready_result(request).as_dict()
    assert isinstance(payload["view"], dict)
    payload["view"]["view_dir"] = str((tmp_path / "forged" / NEXT_VIEW).resolve())
    with pytest.raises(ProtocolError, match="pageindex_dir/views"):
        BuildResult.from_dict(payload, request=request)


def test_failed_and_cancelled_results_have_one_unambiguous_error_channel(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    failed = BuildResult(
        protocol=PROTOCOL_NAME,
        protocol_version=PROTOCOL_VERSION,
        job_id=request.job_id,
        mode=request.mode,
        legacy_export=request.legacy_export,
        state="failed",
        parent=None,
        generation=None,
        view=None,
        legacy_export_artifact=None,
        metrics=WorkerMetrics.empty(),
        error=WorkerError(code="invalid_request", message="bad request"),
    )
    assert decode_result_line(encode_result_line(failed), request=request) == failed

    cancelled_payload = copy.deepcopy(failed.as_dict())
    cancelled_payload["state"] = "cancelled"
    with pytest.raises(ProtocolError, match="cancelled.*error"):
        BuildResult.from_dict(cancelled_payload, request=request)

    failed_payload = copy.deepcopy(failed.as_dict())
    failed_payload["error"] = None
    with pytest.raises(ProtocolError, match="failed.*requires error"):
        BuildResult.from_dict(failed_payload, request=request)


def test_result_rejects_extra_and_missing_fields(tmp_path: Path) -> None:
    request = _request(tmp_path)
    payload = _ready_result(request).as_dict()
    payload["status"] = "ready_to_publish"
    with pytest.raises(ProtocolError, match="result fields"):
        BuildResult.from_dict(payload, request=request)

    payload = _ready_result(request).as_dict()
    del payload["state"]
    with pytest.raises(ProtocolError, match="result fields"):
        BuildResult.from_dict(payload, request=request)


@pytest.mark.parametrize(
    "line",
    [
        b"{}\n{}\n",
        b"not-json\n",
        b"[]\n",
        b"\xff\n",
        b"\n",
    ],
)
def test_json_line_transport_rejects_multiple_malformed_or_nonobject_lines(
    line: bytes,
) -> None:
    with pytest.raises(ProtocolError):
        decode_request_line(line)


def test_json_line_transport_has_a_bounded_message_size() -> None:
    with pytest.raises(ProtocolError, match="byte limit"):
        decode_request_line(b"{" + b" " * MAX_JSON_LINE_BYTES + b"}\n")


def test_json_line_transport_rejects_duplicate_keys_and_nonfinite_numbers(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    encoded = encode_request_line(request).decode("utf-8")
    duplicate = encoded.replace(
        '"job_id":"idx_test-1"',
        '"job_id":"idx_test-1","job_id":"idx_other"',
    )
    with pytest.raises(ProtocolError, match="duplicate key 'job_id'"):
        decode_request_line(duplicate)

    nonfinite = encoded.replace('"protocol_version":1', '"protocol_version":NaN')
    with pytest.raises(ProtocolError, match="non-finite"):
        decode_request_line(nonfinite)
