"""Short-lived PageIndex v2 build worker."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from .canonical import canonical_hash, write_json_atomic
from .catalog import DocumentSource, discover_documents, fingerprint_document
from .ids import normalize_relative_path
from .input_proof import INPUT_PROOF_PATH, validate_input_proof
from .models import CompilerRecipe, SegmentRecipe
from .streaming_json import stream_file_digest
from .no_change import NoChangeMatch, try_no_change
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

if TYPE_CHECKING:
    from .artifacts import CandidateReceipt
    from .compiler import CompiledGeneration
    from .object_store import StoredSegmentRef


def compile_generation(
    segments: Sequence[Mapping[str, object]],
    recipe: CompilerRecipe,
) -> CompiledGeneration:
    from .compiler import compile_generation as implementation

    return implementation(segments, recipe)


def compile_generation_to_candidate(
    refs: Sequence[StoredSegmentRef],
    pageindex_dir: Path,
    candidate_dir: Path,
    recipe: CompilerRecipe,
    *,
    max_run_bytes: int = 32 * 1024 * 1024,
    merge_fan_in: int = 32,
) -> CandidateReceipt:
    from .compiler import compile_generation_to_candidate as implementation

    return implementation(
        refs,
        pageindex_dir,
        candidate_dir,
        recipe,
        max_run_bytes=max_run_bytes,
        merge_fan_in=merge_fan_in,
    )


def find_reusable_segments(
    pageindex_dir: Path,
) -> dict[tuple[str, str, str], str]:
    from .object_store import find_reusable_segments as implementation

    return implementation(pageindex_dir)


def segment_ref_from_attestation(
    pageindex_dir: Path,
    doc_key: str,
    segment_hash: str,
    content_hash: str,
    segment_recipe_hash: str,
) -> StoredSegmentRef:
    from .object_store import segment_ref_from_attestation as implementation

    return implementation(
        pageindex_dir,
        doc_key,
        segment_hash,
        content_hash,
        segment_recipe_hash,
    )


def load_segment(
    pageindex_dir: Path,
    segment_hash: str | StoredSegmentRef,
) -> dict[str, object]:
    from .object_store import load_segment as implementation

    return implementation(pageindex_dir, segment_hash)


def put_segment(
    pageindex_dir: Path,
    segment: Mapping[str, object],
) -> StoredSegmentRef:
    from .object_store import put_segment as implementation

    return implementation(pageindex_dir, segment)


def build_segment(
    source: DocumentSource,
    recipe: SegmentRecipe | None = None,
) -> dict[str, Any]:
    from .segment_builder import build_segment as implementation

    return implementation(source, recipe)


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


def _try_no_change(
    request: BuildRequest, reporter: TaskReporter
) -> NoChangeMatch | None:
    if request.mode != "incremental" or request.base_generation is None:
        return None
    reporter.transition(
        "checking_no_change",
        base_generation=request.base_generation,
    )
    return try_no_change(
        request, check_cancel=lambda: _check_cancel(reporter)
    )


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _nonnegative_metric(
    values: Mapping[str, object],
    key: str,
    default: int,
) -> int:
    value = values.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _read_base_segment_refs(
    request: BuildRequest,
    reporter: TaskReporter,
) -> tuple[list[StoredSegmentRef], set[str]]:
    """Read base attestations without decoding any Segment object."""

    generation = request.base_generation
    if generation is None:
        return [], set()

    generation_dir = (
        request.pageindex_dir
        / "generations"
        / generation
    )
    manifest_path = generation_dir / "manifest.json"
    manifest = read_json_object(manifest_path)
    if manifest.get("generation") != generation:
        raise ValueError(
            f"base manifest generation mismatch at {manifest_path}"
        )
    documents = _mapping(manifest.get("documents"), "manifest.documents")
    document_keys = {
        _string(raw_doc_key, "manifest.documents key")
        for raw_doc_key in documents
    }
    files = _mapping(manifest.get("files"), "manifest.files")
    if (
        manifest.get("schema_version") == 2
        and INPUT_PROOF_PATH not in files
    ):
        if request.mode == "recompile":
            raise ValueError(
                "schema-2 base cannot be recompiled without input proof"
            )
        return [], document_keys

    proof = validate_input_proof(
        read_json_object(generation_dir / INPUT_PROOF_PATH)
    )
    proof_documents = _mapping(
        proof.get("documents"),
        "input_proof.documents",
    )
    if document_keys != set(proof_documents):
        raise ValueError(
            "base input proof document set does not match manifest"
        )
    proof_hash = canonical_hash(proof)
    if manifest.get("input_proof_sha256") != proof_hash:
        raise ValueError("base input proof is not bound by the manifest")
    if proof.get("compiler_recipe_hash") != manifest.get(
        "compiler_recipe_hash"
    ):
        raise ValueError(
            "base input proof compiler recipe does not match manifest"
        )
    proof_metadata = _mapping(
        files.get(INPUT_PROOF_PATH),
        f"manifest.files[{INPUT_PROOF_PATH!r}]",
    )
    if proof_metadata.get("sha256") != proof_hash:
        raise ValueError(
            "base input proof file hash does not match manifest"
        )

    refs: list[StoredSegmentRef] = []
    for doc_key in sorted(document_keys):
        _check_cancel(reporter)
        segment_hash = _string(
            documents[doc_key],
            f"manifest.documents[{doc_key!r}]",
        )
        proof_entry = _mapping(
            proof_documents[doc_key],
            f"input_proof.documents[{doc_key!r}]",
        )
        refs.append(
            segment_ref_from_attestation(
                request.pageindex_dir,
                doc_key,
                segment_hash,
                _string(
                    proof_entry.get("content_hash"),
                    f"input_proof.documents[{doc_key!r}].content_hash",
                ),
                _string(
                    proof_entry.get("segment_recipe_hash"),
                    f"input_proof.documents[{doc_key!r}].segment_recipe_hash",
                ),
            )
        )
    return refs, document_keys


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


def _base_reusable_segment_refs(
    refs: Sequence[StoredSegmentRef],
) -> dict[tuple[str, str, str], StoredSegmentRef]:
    """Index refs bound to the selected base Generation."""

    reusable: dict[tuple[str, str, str], StoredSegmentRef] = {}
    for ref in refs:
        key = (
            ref.doc_key,
            ref.content_hash,
            ref.segment_recipe_hash,
        )
        previous = reusable.get(key)
        if (
            previous is not None
            and previous.segment_hash != ref.segment_hash
        ):
            raise ValueError(
                f"base generation has conflicting reusable segments for {key!r}"
            )
        reusable[key] = ref
    return reusable


def _source_segment_refs(
    request: BuildRequest,
    reporter: TaskReporter,
    base_refs: Sequence[StoredSegmentRef] = (),
) -> tuple[list[StoredSegmentRef], dict[str, object], set[str]]:
    """Discover sources while retaining only immutable Segment references."""

    if not request.content_dir.is_dir():
        raise NotADirectoryError(request.content_dir)

    recipe = SegmentRecipe()
    recipe_hash = canonical_hash(recipe.as_dict())
    bootstrap_reuse_scan_ms = 0.0
    if request.mode == "full":
        reusable: dict[
            tuple[str, str, str],
            StoredSegmentRef,
        ] = {}
    elif request.base_generation is not None:
        reusable = _base_reusable_segment_refs(base_refs)
    else:
        # Bootstrap incremental builds have no Generation lineage yet. Keep
        # the object-store scan isolated to this first-build path.
        scan_started = time.perf_counter()
        reusable = {
            key: segment_ref_from_attestation(
                request.pageindex_dir,
                key[0],
                segment_hash,
                key[1],
                key[2],
            )
            for key, segment_hash in find_reusable_segments(
                request.pageindex_dir
            ).items()
        }
        bootstrap_reuse_scan_ms = round(
            (time.perf_counter() - scan_started) * 1000,
            3,
        )

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

        refs: list[StoredSegmentRef] = []
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
                ref = reusable.get(reuse_key)
                if ref is not None:
                    del captured
                    reused += 1
                    action = "reused"
                else:
                    immutable_source = _materialize_source_snapshot(
                        source,
                        captured,
                        snapshot_root,
                    )
                    del captured
                    segment = build_segment(immutable_source, recipe)
                    fingerprint = _mapping(
                        segment.get("fingerprint"), "segment.fingerprint"
                    )
                    if fingerprint.get("content_hash") != content_hash:
                        raise RuntimeError(
                            f"snapshot fingerprint mismatch for {source.doc_key}"
                        )
                    ref = put_segment(request.pageindex_dir, segment)
                    del segment
                    rebuilt += 1
                    action = "rebuilt"

                if (
                    ref.doc_key,
                    ref.content_hash,
                    ref.segment_recipe_hash,
                ) != reuse_key:
                    raise RuntimeError(
                        f"Segment ref attestation mismatch for {source.doc_key}"
                    )
                refs.append(ref)
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
                        refs,
                        {
                            "segments_rebuilt": rebuilt,
                            "segments_reused": reused,
                            "stabilization_attempts": attempt,
                            "bootstrap_reuse_scan_ms": (
                                bootstrap_reuse_scan_ms
                            ),
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


def _safe_generation_path(root: Path, relative: object) -> Path:
    """Resolve one receipt path without permitting traversal or drive changes."""

    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise RuntimeError(f"unsafe Generation path: {relative!r}")
    posix = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    if (
        posix.is_absolute()
        or windows.drive
        or windows.root
        or posix.as_posix() != relative
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise RuntimeError(f"unsafe Generation path: {relative!r}")

    root_resolved = Path(root).resolve()
    path = root_resolved.joinpath(*posix.parts)
    try:
        path.resolve().relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"unsafe Generation path: {relative!r}") from exc
    return path


def _generation_file_set(root: Path) -> set[str]:
    """Return exact regular-file paths while rejecting links and escapes."""

    generation = Path(root)
    if generation.is_symlink():
        raise RuntimeError(f"unsafe Generation root link: {generation}")
    root_resolved = generation.resolve()
    files: set[str] = set()
    try:
        paths = generation.rglob("*")
        for path in paths:
            relative = path.relative_to(generation).as_posix()
            if path.is_symlink():
                raise RuntimeError(
                    f"unsafe Generation symbolic link: {relative}"
                )
            try:
                path.resolve().relative_to(root_resolved)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"unsafe Generation path: {relative}"
                ) from exc
            if path.is_file():
                _safe_generation_path(generation, relative)
                files.add(relative)
            elif not path.is_dir():
                raise RuntimeError(
                    f"unsupported Generation entry: {relative}"
                )
    except OSError as exc:
        raise RuntimeError(
            f"cannot enumerate Generation files: {generation}: {exc}"
        ) from exc
    return files

def _receipt_generation_files(
    generation_dir: Path,
    receipt: CandidateReceipt,
) -> tuple[Mapping[str, Any], set[str]]:
    """Validate receipt paths before any candidate move or deletion."""

    generation = Path(generation_dir)
    artifacts = _mapping(
        receipt.artifacts,
        "candidate_receipt.artifacts",
    )
    expected_files = {"manifest.json"}
    for relative, reference in artifacts.items():
        _safe_generation_path(generation, relative)
        if relative == "manifest.json":
            raise RuntimeError(
                "candidate receipt must not list manifest.json as an artifact"
            )
        if getattr(reference, "relative_path", None) != relative:
            raise RuntimeError(
                f"candidate receipt artifact path mismatch: {relative!r}"
            )
        expected_files.add(relative)
    return artifacts, expected_files


def _verify_generation_receipt(
    generation_dir: Path,
    receipt: CandidateReceipt,
) -> None:
    """Verify one existing Generation using bounded streaming reads."""

    generation = Path(generation_dir)
    artifacts, expected_files = _receipt_generation_files(
        generation, receipt
    )

    actual_files = _generation_file_set(generation)
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise RuntimeError(
            "Generation file set mismatch: "
            f"missing={missing!r}, extra={extra!r}"
        )

    manifest_path = _safe_generation_path(generation, "manifest.json")
    try:
        manifest_digest = stream_file_digest(manifest_path)
    except OSError as exc:
        raise RuntimeError(
            f"cannot hash Generation manifest: {manifest_path}: {exc}"
        ) from exc
    if manifest_digest.sha256 != receipt.manifest_sha256:
        raise RuntimeError(
            "Generation manifest hash mismatch: "
            f"expected {receipt.manifest_sha256}, "
            f"got {manifest_digest.sha256}"
        )

    for relative in sorted(artifacts):
        reference = artifacts[relative]
        path = _safe_generation_path(generation, relative)
        try:
            byte_size = path.stat().st_size
        except OSError as exc:
            raise RuntimeError(
                f"cannot stat Generation artifact {relative}: {exc}"
            ) from exc
        expected_size = getattr(reference, "byte_size", None)
        if byte_size != expected_size:
            raise RuntimeError(
                f"Generation artifact size mismatch for {relative}: "
                f"expected {expected_size}, got {byte_size}"
            )
        try:
            digest = stream_file_digest(path)
        except OSError as exc:
            raise RuntimeError(
                f"cannot hash Generation artifact {relative}: {exc}"
            ) from exc
        if digest.byte_size != expected_size:
            raise RuntimeError(
                f"Generation artifact size changed while hashing {relative}: "
                f"expected {expected_size}, got {digest.byte_size}"
            )
        expected_hash = getattr(reference, "sha256", None)
        if digest.sha256 != expected_hash:
            raise RuntimeError(
                f"Generation artifact hash mismatch for {relative}: "
                f"expected {expected_hash}, got {digest.sha256}"
            )


def _finalize_generation(
    receipt: CandidateReceipt,
    generation_dir: Path,
) -> Path:
    """Install a candidate or prove an existing Generation is identical."""

    candidate_dir = Path(receipt.candidate_dir)
    generation = Path(generation_dir)
    _receipt_generation_files(generation, receipt)
    if candidate_dir.resolve() == generation.resolve():
        raise RuntimeError("candidate and Generation directories must differ")
    if candidate_dir.is_symlink() or not candidate_dir.is_dir():
        raise RuntimeError(
            f"candidate path is not a directory: {candidate_dir}"
        )

    generation.parent.mkdir(parents=True, exist_ok=True)
    if generation.exists() or generation.is_symlink():
        if generation.is_symlink() or not generation.is_dir():
            raise RuntimeError(
                f"generation path is not a directory: {generation}"
            )
        try:
            _verify_generation_receipt(generation, receipt)
        except RuntimeError as exc:
            raise RuntimeError(
                "existing generation differs from candidate: "
                f"{generation.name}: {exc}"
            ) from exc
        shutil.rmtree(candidate_dir)
        return generation

    try:
        os.replace(candidate_dir, generation)
    except OSError:
        if generation.is_symlink() or not generation.is_dir():
            raise
        try:
            _verify_generation_receipt(generation, receipt)
        except RuntimeError as exc:
            raise RuntimeError(
                "concurrent generation differs from candidate: "
                f"{generation.name}: {exc}"
            ) from exc
        shutil.rmtree(candidate_dir)
    return generation


def _compile_and_validate(
    request: BuildRequest,
    reporter: TaskReporter,
    refs: Sequence[StoredSegmentRef],
) -> tuple[CandidateReceipt, Path, list[str]]:
    """Compile refs directly to disk and validate the materialized candidate."""

    candidate_dir = (
        request.pageindex_dir
        / "build"
        / request.job_id
        / "candidate"
    )
    if candidate_dir.exists():
        if not candidate_dir.is_dir():
            raise RuntimeError(
                f"candidate path is not a directory: {candidate_dir}"
            )
        shutil.rmtree(candidate_dir)

    _check_cancel(reporter)
    reporter.transition("compiling_global", segments=len(refs))
    receipt = compile_generation_to_candidate(
        tuple(refs),
        request.pageindex_dir,
        candidate_dir,
        CompilerRecipe(),
    )
    materialized = Path(receipt.candidate_dir)
    if materialized.resolve() != candidate_dir.resolve():
        raise RuntimeError(
            "candidate compiler returned an unexpected directory: "
            f"{materialized}"
        )

    _check_cancel(reporter)
    reporter.transition(
        "materializing",
        generation=receipt.generation_id,
    )
    # Imported locally so invalid requests remain diagnosable in packaged apps.
    from .validator import validate_candidate_normal

    _check_cancel(reporter)
    reporter.transition(
        "validating",
        generation=receipt.generation_id,
    )
    report = validate_candidate_normal(receipt, request.pageindex_dir)
    ok, errors, warnings = _validation_details(report)
    if not ok:
        raise CandidateValidationError(errors, warnings)

    _check_cancel(reporter)
    reporter.transition(
        "finalizing_generation",
        generation=receipt.generation_id,
    )
    generation_dir = _finalize_generation(
        receipt,
        request.pageindex_dir
        / "generations"
        / receipt.generation_id,
    )
    return receipt, generation_dir, warnings


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

        no_change = _try_no_change(request, reporter)
        if no_change is not None:
            manifest_stats = _mapping(
                no_change.manifest.get("stats"),
                "manifest.stats",
            )
            stats: dict[str, object] = {
                **dict(manifest_stats),
                "no_op": True,
                "segments_loaded": 0,
                "segments_loaded_peak": 0,
                "run_buffer_peak_bytes": 0,
                "segments_rebuilt": 0,
                "segments_reused": no_change.document_count,
                "segments_deleted": 0,
                "stabilization_attempts": no_change.stabilization_attempts,
                "bootstrap_reuse_scan_ms": 0.0,
                "postings_visited": 0,
                "generation_bytes_written": 0,
                "full_compile_runs": 0,
                "normal_validation_runs": 0,
                "deep_validation_runs": 0,
                "duration_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
                "shadow_duration_ms": 0.0,
            }
            result: dict[str, object] = {
                "schema_version": PROTOCOL_SCHEMA_VERSION,
                "status": "ready_to_publish",
                "outcome": "no_change",
                "job_id": request.job_id,
                "mode": request.mode,
                "base_generation": request.base_generation,
                "generation": no_change.generation_dir.name,
                "manifest_sha256": no_change.manifest_sha256,
                "generation_dir": str(no_change.generation_dir),
                "warnings": [],
                "shadow_report": {
                    "status": "not_run",
                    "reason": "no_change",
                },
                "stats": stats,
                "finished_at": utc_now(),
            }
            reporter.transition(
                "ready_to_publish",
                generation=no_change.generation_dir.name,
                outcome="no_change",
                warnings=0,
            )
            reporter.finish(result)
            return EXIT_SUCCESS

        base_refs: list[StoredSegmentRef] = []
        base_doc_keys: set[str] = set()
        if request.base_generation is not None:
            reporter.transition(
                "loading_base_generation",
                base_generation=request.base_generation,
            )
            base_refs, base_doc_keys = _read_base_segment_refs(
                request,
                reporter,
            )

        if request.mode == "recompile":
            refs = base_refs
            current_doc_keys = base_doc_keys
            build_stats = {
                "segments_rebuilt": 0,
                "segments_reused": len(refs),
                "stabilization_attempts": 0,
                "bootstrap_reuse_scan_ms": 0.0,
            }
        else:
            refs, build_stats, current_doc_keys = _source_segment_refs(
                request,
                reporter,
                base_refs,
            )

        receipt, generation_dir, warnings = _compile_and_validate(
            request,
            reporter,
            refs,
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
        manifest_path = generation_dir / "manifest.json"
        manifest = read_json_object(manifest_path)
        if manifest.get("generation") != receipt.generation_id:
            raise RuntimeError(
                "materialized manifest generation does not match receipt"
            )
        if canonical_hash(manifest) != receipt.manifest_sha256:
            raise RuntimeError(
                "materialized manifest hash does not match receipt"
            )
        manifest_stats = _mapping(
            manifest.get("stats"),
            "manifest.stats",
        )
        invariants = _mapping(
            receipt.invariants,
            "candidate_receipt.invariants",
        )
        generation_bytes_default = (
            sum(
                artifact.byte_size
                for artifact in receipt.artifacts.values()
            )
            + manifest_path.stat().st_size
        )
        stats: dict[str, object] = {
            **build_stats,
            "no_op": False,
            "segments_deleted": len(base_doc_keys - current_doc_keys),
            **dict(manifest_stats),
            "segments_loaded": _nonnegative_metric(
                invariants,
                "segments_loaded",
                len(refs),
            ),
            "segments_loaded_peak": _nonnegative_metric(
                invariants,
                "segments_loaded_peak",
                min(1, len(refs)),
            ),
            "run_buffer_peak_bytes": _nonnegative_metric(
                invariants,
                "run_buffer_peak_bytes",
                0,
            ),
            "postings_visited": _nonnegative_metric(
                invariants,
                "postings_visited",
                0,
            ),
            "generation_bytes_written": _nonnegative_metric(
                invariants,
                "generation_bytes_written",
                generation_bytes_default,
            ),
            "full_compile_runs": 1,
            "normal_validation_runs": 1,
            "deep_validation_runs": 0,
            "duration_ms": round(
                (time.perf_counter() - started) * 1000,
                3,
            ),
            "shadow_duration_ms": shadow_duration_ms,
        }
        result: dict[str, object] = {
            "schema_version": PROTOCOL_SCHEMA_VERSION,
            "status": "ready_to_publish",
            "outcome": "built",
            "job_id": request.job_id,
            "mode": request.mode,
            "base_generation": request.base_generation,
            "generation": receipt.generation_id,
            "manifest_sha256": receipt.manifest_sha256,
            "generation_dir": str(generation_dir),
            "warnings": warnings,
            "shadow_report": shadow_summary,
            "stats": stats,
            "finished_at": utc_now(),
        }
        reporter.transition(
            "ready_to_publish",
            generation=receipt.generation_id,
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
