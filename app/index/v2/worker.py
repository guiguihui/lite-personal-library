"""Short-lived PageIndex v2 build worker."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import canonical_hash, write_json_atomic
from .catalog import DocumentSource, discover_documents, fingerprint_document
from .compiler import CompiledGeneration, compile_generation
from .ids import normalize_relative_path
from .models import CompilerRecipe, SegmentRecipe
from .object_store import (
    find_reusable_segments,
    load_segment,
    put_segment,
)
from .protocol import (
    EXIT_BUILD_FAILED,
    EXIT_CANCELLED,
    EXIT_INVALID_REQUEST,
    EXIT_SUCCESS,
    PROTOCOL_SCHEMA_VERSION,
    BuildRequest,
    ProtocolError,
    TaskReporter,
    read_json_object,
    utc_now,
)
from .segment_builder import build_segment


class BuildCancelled(RuntimeError):
    """Cooperative cancellation was requested at a safe stage boundary."""


class CandidateValidationError(RuntimeError):
    """The materialized candidate failed structural validation."""

    def __init__(self, error_codes: Sequence[str], warnings: Sequence[str]) -> None:
        self.error_codes = tuple(str(code) for code in error_codes)
        self.warnings = tuple(str(warning) for warning in warnings)
        detail = ", ".join(self.error_codes) or "unknown structural error"
        super().__init__(f"candidate validation failed: {detail}")


class ShadowComparisonError(RuntimeError):
    """A persisted Shadow report contains publish-blocking differences."""

    def __init__(
        self,
        summary: Mapping[str, object],
        warnings: Sequence[str],
    ) -> None:
        self.summary = dict(summary)
        self.warnings = tuple(str(warning) for warning in warnings)
        super().__init__("shadow comparison found unexplained or structural differences")


_LEGACY_CORE_FILES = (
    "global-index.json",
    "node-index.json",
    "chunks.json",
    "inverted-index.json",
)


def _check_cancel(reporter: TaskReporter) -> None:
    if reporter.is_cancelled():
        raise BuildCancelled("cancel.request was observed")


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _read_base_segments(
    request: BuildRequest,
    reporter: TaskReporter,
) -> tuple[list[dict[str, object]], set[str]]:
    generation = request.base_generation
    if generation is None:
        return [], set()

    manifest_path = (
        request.pageindex_dir
        / "generations"
        / generation
        / "manifest.json"
    )
    manifest = read_json_object(manifest_path)
    if manifest.get("generation") != generation:
        raise ValueError(
            f"base manifest generation mismatch at {manifest_path}"
        )
    documents = _mapping(manifest.get("documents"), "manifest.documents")

    segments: list[dict[str, object]] = []
    for doc_key in sorted(documents):
        _check_cancel(reporter)
        segment_hash = _string(
            documents[doc_key], f"manifest.documents[{doc_key!r}]"
        )
        segment = load_segment(request.pageindex_dir, segment_hash)
        document = _mapping(segment.get("document"), "segment.document")
        if document.get("doc_key") != doc_key:
            raise ValueError(
                f"segment {segment_hash} does not belong to {doc_key}"
            )
        segments.append(segment)
    return segments, set(str(key) for key in documents)


def _snapshot_sources(
    content_dir: Path,
) -> tuple[tuple[DocumentSource, ...], dict[str, str]]:
    sources = discover_documents(content_dir)
    fingerprints = {
        source.doc_key: fingerprint_document(source) for source in sources
    }
    return sources, fingerprints


def _capture_source(
    source: DocumentSource,
) -> tuple[str, tuple[tuple[Path, bytes], ...]]:
    """Read one logical document once and hash the exact captured bytes."""

    content_root = source.root.resolve()
    captured: list[tuple[Path, bytes]] = []
    records: list[dict[str, str]] = []
    for raw_relative in source.files:
        relative_text = normalize_relative_path(Path(raw_relative).as_posix())
        relative = Path(relative_text)
        live_path = (content_root / relative).resolve()
        try:
            live_path.relative_to(content_root)
        except ValueError as exc:
            raise ValueError(f"source file escapes content root: {live_path}") from exc
        payload = live_path.read_bytes()
        captured.append((relative, payload))
        records.append(
            {
                "path": relative_text,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return canonical_hash(records), tuple(captured)


def _materialize_source_snapshot(
    source: DocumentSource,
    captured: Sequence[tuple[Path, bytes]],
    snapshot_root: Path,
) -> DocumentSource:
    """Create the immutable parser input corresponding to a capture."""

    root = Path(snapshot_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative, payload in captured:
        destination = (root / relative).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"snapshot path escapes root: {relative}") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    return DocumentSource(
        doc_type=source.doc_type,
        slug=source.slug,
        doc_key=source.doc_key,
        root=root,
        files=source.files,
    )


def _base_reusable_segments(
    segments: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str, str], str]:
    """Index only Segments referenced by the selected base Generation."""

    reusable: dict[tuple[str, str, str], str] = {}
    for segment in segments:
        document = _mapping(segment.get("document"), "segment.document")
        fingerprint = _mapping(
            segment.get("fingerprint"), "segment.fingerprint"
        )
        key = (
            _string(document.get("doc_key"), "document.doc_key"),
            _string(
                fingerprint.get("content_hash"),
                "fingerprint.content_hash",
            ),
            _string(
                fingerprint.get("recipe_hash"),
                "fingerprint.recipe_hash",
            ),
        )
        segment_hash = canonical_hash(segment)
        previous = reusable.get(key)
        if previous is not None and previous != segment_hash:
            raise ValueError(
                f"base generation has conflicting reusable segments for {key!r}"
            )
        reusable[key] = segment_hash
    return reusable


def _source_segments(
    request: BuildRequest,
    reporter: TaskReporter,
    base_segments: Sequence[Mapping[str, object]] = (),
) -> tuple[list[dict[str, object]], dict[str, int], set[str]]:
    if not request.content_dir.is_dir():
        raise NotADirectoryError(request.content_dir)

    recipe = SegmentRecipe()
    recipe_hash = canonical_hash(recipe.as_dict())
    if request.mode == "full":
        reusable: dict[tuple[str, str, str], str] = {}
    elif request.base_generation is not None:
        reusable = _base_reusable_segments(base_segments)
    else:
        # Bootstrap incremental builds have no Generation lineage yet. Retain
        # the existing object-store fallback only for that first-build case.
        reusable = find_reusable_segments(request.pageindex_dir)

    for attempt in range(1, 4):
        _check_cancel(reporter)
        reporter.transition(
            "discovering",
            attempt=attempt,
            mode=request.mode,
        )
        sources = discover_documents(request.content_dir)
        before: dict[str, str] = {}
        reporter.transition(
            "building_segments",
            attempt=attempt,
            documents_total=len(sources),
            documents_complete=0,
        )

        segments: list[dict[str, object]] = []
        rebuilt = 0
        reused = 0
        job_dir = request.pageindex_dir / "build" / request.job_id
        with tempfile.TemporaryDirectory(
            dir=job_dir,
            prefix=f"source-snapshot-{attempt}-",
        ) as snapshot_name:
            snapshot_root = Path(snapshot_name)
            for position, source in enumerate(sources, start=1):
                _check_cancel(reporter)
                try:
                    content_hash, captured = _capture_source(source)
                except FileNotFoundError:
                    reporter.event(
                        "source_changed_during_capture",
                        doc_key=source.doc_key,
                        attempt=attempt,
                    )
                    break
                before[source.doc_key] = content_hash
                reuse_key = (source.doc_key, content_hash, recipe_hash)
                segment_hash = reusable.get(reuse_key)
                if segment_hash is not None:
                    segment = load_segment(request.pageindex_dir, segment_hash)
                    reused += 1
                    action = "reused"
                else:
                    immutable_source = _materialize_source_snapshot(
                        source,
                        captured,
                        snapshot_root,
                    )
                    segment = build_segment(immutable_source, recipe)
                    fingerprint = _mapping(
                        segment.get("fingerprint"), "segment.fingerprint"
                    )
                    if fingerprint.get("content_hash") != content_hash:
                        raise RuntimeError(
                            f"snapshot fingerprint mismatch for {source.doc_key}"
                        )
                    put_segment(request.pageindex_dir, segment)
                    rebuilt += 1
                    action = "rebuilt"
                segments.append(segment)
                reporter.transition(
                    "building_segments",
                    attempt=attempt,
                    documents_total=len(sources),
                    documents_complete=position,
                    doc_key=source.doc_key,
                    action=action,
                )
            else:
                _check_cancel(reporter)
                _, after = _snapshot_sources(request.content_dir)
                if before == after:
                    return (
                        segments,
                        {
                            "segments_rebuilt": rebuilt,
                            "segments_reused": reused,
                            "stabilization_attempts": attempt,
                        },
                        set(before),
                    )

        reporter.event("content_snapshot_changed", attempt=attempt)

    raise RuntimeError("content did not stabilize after 3 build attempts")


def _validation_details(report: object) -> tuple[bool, list[str], list[str]]:
    ok = bool(getattr(report, "ok", False))
    raw_errors = getattr(report, "error_codes", ())
    raw_warnings = getattr(report, "warnings", ())
    errors = [str(value) for value in raw_errors]
    warnings = [str(value) for value in raw_warnings]
    return ok, errors, warnings


def _write_shadow_report(
    request: BuildRequest,
    reporter: TaskReporter,
    generation_dir: Path,
    *,
    generation_build_duration_ms: float,
) -> tuple[dict[str, object], list[str], float]:
    """Persist a full legacy/v2 report and return its compact result summary."""

    _check_cancel(reporter)
    missing = [
        name
        for name in _LEGACY_CORE_FILES
        if not (request.pageindex_dir / name).is_file()
    ]
    if missing:
        summary: dict[str, object] = {
            "status": "not_available",
            "missing_legacy_files": missing,
        }
        return summary, ["shadow_report_skipped_missing_legacy"], 0.0

    reporter.transition(
        "comparing_shadow",
        generation=generation_dir.name,
    )
    started = time.perf_counter()
    from .shadow_diff import compare_legacy_to_generation

    report = compare_legacy_to_generation(
        request.pageindex_dir,
        generation_dir,
    )
    metrics = report.get("metrics")
    if isinstance(metrics, dict):
        durations = metrics.get("build_duration_ms")
        if isinstance(durations, dict):
            durations["generation"] = generation_build_duration_ms
    shadow_duration_ms = round((time.perf_counter() - started) * 1000, 3)
    report["shadow_duration_ms"] = shadow_duration_ms
    report_path = request.pageindex_dir / "build" / request.job_id / "shadow-report.json"
    write_json_atomic(report_path, report)
    _check_cancel(reporter)

    trees = report.get("document_trees")
    stale_legacy_files = (
        trees.get("stale_legacy_files") if isinstance(trees, Mapping) else None
    )
    summary = {
        "status": "complete",
        "report_file": "shadow-report.json",
        "ok": bool(report.get("ok")),
        "semantic_equal": bool(report.get("semantic_equal")),
        "structural_ok": bool(report.get("structural_ok")),
        "expected_policy_delta": int(report.get("expected_policy_delta", 0)),
        "unexplained_semantic_mismatch": int(
            report.get("unexplained_semantic_mismatch", 0)
        ),
        "publish_blocking_errors": int(
            report.get("publish_blocking_errors", 0)
        ),
        "stale_legacy_files": stale_legacy_files,
    }
    warnings: list[str] = []
    if not summary["semantic_equal"]:
        warnings.append(
            f"shadow_expected_policy_delta:{summary['expected_policy_delta']}"
        )
    if not summary["ok"]:
        warnings.append("shadow_report_not_publishable")
    return summary, warnings, shadow_duration_ms


def _directory_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    }


def _finalize_generation(
    candidate_dir: Path,
    generation_dir: Path,
) -> Path:
    generation_dir.parent.mkdir(parents=True, exist_ok=True)
    if generation_dir.exists():
        if not generation_dir.is_dir():
            raise RuntimeError(
                f"generation path is not a directory: {generation_dir}"
            )
        if _directory_files(candidate_dir) != _directory_files(generation_dir):
            raise RuntimeError(
                f"existing generation differs from candidate: {generation_dir.name}"
            )
        shutil.rmtree(candidate_dir)
        return generation_dir

    try:
        os.replace(candidate_dir, generation_dir)
    except OSError:
        if not generation_dir.is_dir():
            raise
        if _directory_files(candidate_dir) != _directory_files(generation_dir):
            raise RuntimeError(
                f"concurrent generation differs from candidate: "
                f"{generation_dir.name}"
            )
        shutil.rmtree(candidate_dir)
    return generation_dir


def _compile_and_validate(
    request: BuildRequest,
    reporter: TaskReporter,
    segments: Sequence[Mapping[str, object]],
) -> tuple[CompiledGeneration, Path, list[str]]:
    _check_cancel(reporter)
    reporter.transition("compiling_global", segments=len(segments))
    compiled = compile_generation(tuple(segments), CompilerRecipe())

    _check_cancel(reporter)
    reporter.transition(
        "materializing",
        generation=compiled.generation_id,
    )
    # Imported here so invalid requests remain diagnosable even if a packaged
    # build is missing the validator module.
    from .validator import materialize_candidate, validate_candidate

    candidate_dir = request.pageindex_dir / "build" / request.job_id / "candidate"
    if candidate_dir.exists():
        if not candidate_dir.is_dir():
            raise RuntimeError(f'candidate path is not a directory: {candidate_dir}')
        shutil.rmtree(candidate_dir)
    materialized = Path(materialize_candidate(candidate_dir, compiled))

    _check_cancel(reporter)
    reporter.transition(
        "validating",
        generation=compiled.generation_id,
    )
    report = validate_candidate(materialized, request.pageindex_dir)
    ok, errors, warnings = _validation_details(report)
    if not ok:
        raise CandidateValidationError(errors, warnings)

    _check_cancel(reporter)
    reporter.transition(
        "finalizing_generation",
        generation=compiled.generation_id,
    )
    generation_dir = _finalize_generation(
        materialized,
        request.pageindex_dir
        / "generations"
        / compiled.generation_id,
    )
    return compiled, generation_dir, warnings


def _failed_result(
    *,
    job_id: str,
    mode: str | None,
    base_generation: str | None,
    error_code: str,
    message: str,
    warnings: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "status": "failed",
        "job_id": job_id,
        "mode": mode,
        "base_generation": base_generation,
        "error_code": error_code,
        "message": message,
        "warnings": list(warnings),
        "finished_at": utc_now(),
    }


def run_worker(request_path: Path) -> int:
    """Execute one request and return its stable process exit code."""

    path = Path(request_path).resolve()
    job_dir = path.parent
    fallback_job_id = job_dir.name or "unknown"

    try:
        raw = read_json_object(path)
        request = BuildRequest.from_dict(raw)
        expected_request_path = (
            request.pageindex_dir
            / "build"
            / request.job_id
            / "request.json"
        ).resolve()
        if path != expected_request_path:
            raise ProtocolError(
                f"request path must equal {expected_request_path}"
            )
    except (ProtocolError, OSError, ValueError) as exc:
        reporter = TaskReporter(job_dir, fallback_job_id)
        result = _failed_result(
            job_id=fallback_job_id,
            mode=None,
            base_generation=None,
            error_code="invalid_request",
            message=str(exc),
        )
        reporter.transition(
            "failed",
            error_code="invalid_request",
            message=str(exc),
        )
        reporter.finish(result)
        return EXIT_INVALID_REQUEST

    reporter = TaskReporter(job_dir, request.job_id)
    started = time.perf_counter()
    try:
        reporter.transition("accepted", mode=request.mode)
        _check_cancel(reporter)

        base_segments: list[dict[str, object]] = []
        base_doc_keys: set[str] = set()
        if request.base_generation is not None:
            reporter.transition(
                "loading_base_generation",
                base_generation=request.base_generation,
            )
            base_segments, base_doc_keys = _read_base_segments(request, reporter)

        if request.mode == "recompile":
            segments = base_segments
            current_doc_keys = base_doc_keys
            build_stats = {
                "segments_rebuilt": 0,
                "segments_reused": len(segments),
                "stabilization_attempts": 0,
            }
        else:
            segments, build_stats, current_doc_keys = _source_segments(
                request, reporter, base_segments
            )

        compiled, generation_dir, warnings = _compile_and_validate(
            request,
            reporter,
            segments,
        )
        generation_build_duration_ms = round(
            (time.perf_counter() - started) * 1000,
            3,
        )
        shadow_summary, shadow_warnings, shadow_duration_ms = _write_shadow_report(
            request,
            reporter,
            generation_dir,
            generation_build_duration_ms=generation_build_duration_ms,
        )
        warnings = [*warnings, *shadow_warnings]
        if (
            shadow_summary.get("status") == "complete"
            and not shadow_summary.get("ok")
        ):
            raise ShadowComparisonError(
                shadow_summary,
                warnings,
            )
        manifest_stats = _mapping(compiled.manifest.get("stats"), "manifest.stats")
        stats: dict[str, object] = {
            **build_stats,
            "segments_deleted": len(base_doc_keys - current_doc_keys),
            **dict(manifest_stats),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "shadow_duration_ms": shadow_duration_ms,
        }
        result: dict[str, object] = {
            "schema_version": PROTOCOL_SCHEMA_VERSION,
            "status": "ready_to_publish",
            "job_id": request.job_id,
            "mode": request.mode,
            "base_generation": request.base_generation,
            "generation": compiled.generation_id,
            "manifest_sha256": canonical_hash(compiled.manifest),
            "generation_dir": str(generation_dir),
            "warnings": warnings,
            "shadow_report": shadow_summary,
            "stats": stats,
            "finished_at": utc_now(),
        }
        reporter.transition(
            "ready_to_publish",
            generation=compiled.generation_id,
            warnings=len(warnings),
        )
        reporter.finish(result)
        return EXIT_SUCCESS
    except BuildCancelled as exc:
        result = {
            "schema_version": PROTOCOL_SCHEMA_VERSION,
            "status": "cancelled",
            "job_id": request.job_id,
            "mode": request.mode,
            "base_generation": request.base_generation,
            "message": str(exc),
            "warnings": [],
            "finished_at": utc_now(),
        }
        reporter.transition("cancelled", message=str(exc))
        reporter.finish(result)
        return EXIT_CANCELLED
    except CandidateValidationError as exc:
        result = _failed_result(
            job_id=request.job_id,
            mode=request.mode,
            base_generation=request.base_generation,
            error_code="validation_failed",
            message=str(exc),
            warnings=exc.warnings,
        )
        result["validation_errors"] = list(exc.error_codes)
        reporter.transition(
            "failed",
            error_code="validation_failed",
            validation_errors=list(exc.error_codes),
        )
        reporter.finish(result)
        return EXIT_BUILD_FAILED
    except ShadowComparisonError as exc:
        result = _failed_result(
            job_id=request.job_id,
            mode=request.mode,
            base_generation=request.base_generation,
            error_code="shadow_comparison_failed",
            message=str(exc),
            warnings=exc.warnings,
        )
        result["shadow_report"] = exc.summary
        reporter.transition(
            "failed",
            error_code="shadow_comparison_failed",
            publish_blocking_errors=exc.summary.get("publish_blocking_errors", 0),
        )
        reporter.finish(result)
        return EXIT_BUILD_FAILED
    except Exception as exc:
        result = _failed_result(
            job_id=request.job_id,
            mode=request.mode,
            base_generation=request.base_generation,
            error_code="build_failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        reporter.transition(
            "failed",
            error_code="build_failed",
            message=str(exc),
        )
        reporter.finish(result)
        return EXIT_BUILD_FAILED


__all__ = ["run_worker"]
