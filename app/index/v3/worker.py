"""Fresh-process PageIndex v3 build orchestration.

The default path is deliberately proof-first.  An unchanged incremental
request returns before collecting or decoding any Segment.  A dirty request
builds one replacement Delta and runs one scoped P3 Normal validation; full
Base construction is reserved for bootstrap and explicit ``optimize``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from typing import Any

from app.index.v2.artifacts import ArtifactRef
from app.index.v2.canonical import (
    canonical_bytes,
    canonical_hash,
    iter_canonical_json,
)
from app.index.v2.ids import normalize_relative_path
from app.index.v2.models import DocumentSource, SegmentRecipe
from app.index.v2.object_store import StoredSegmentRef
from app.index.v2.source_snapshot import (
    StableCatalogSnapshot,
    capture_stable_catalog,
)

from .delta_store import load_delta_object_metadata
from .generation import (
    INPUT_PROOF_PATH,
    MANIFEST_PATH,
    LogicalGenerationReceipt,
    _rename_no_replace,
    build_logical_generation,
    validate_logical_generation_manifest,
)
from .models import (
    CompactionPolicy,
    GenerationRecipe,
    SearchViewRecipe,
    ViewPin,
)
from .protocol import (
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
    encode_result_line,
)
from .source_diff import SegmentChangeSet, diff_segment_inputs
from .view_store import (
    SearchViewReceipt,
    load_base_object_metadata,
    load_search_view_metadata,
)


EXIT_SUCCESS = 0
EXIT_BUILD_FAILED = 1
EXIT_INVALID_REQUEST = 2
EXIT_CANCELLED = 3

_MAX_STABILIZATION_ATTEMPTS = 3
_READ_BUFFER_BYTES = 1024 * 1024
_GENERATION_FILES = frozenset({MANIFEST_PATH, INPUT_PROOF_PATH})


# These indirections preserve the existing monkeypatch surface while keeping
# build-only modules out of fresh worker and no-op imports.
def build_segment(*args: Any, **kwargs: Any) -> Any:
    from app.index.v2.segment_builder import build_segment as implementation

    return implementation(*args, **kwargs)


def put_segment(*args: Any, **kwargs: Any) -> Any:
    from app.index.v2.object_store import put_segment as implementation

    return implementation(*args, **kwargs)


def validate_generation_stream(*args: Any, **kwargs: Any) -> Any:
    from .generation_stream import validate_generation_stream as implementation

    return implementation(*args, **kwargs)


def build_base_view(*args: Any, **kwargs: Any) -> Any:
    from .base_builder import build_base_view as implementation

    return implementation(*args, **kwargs)


def build_delta_view(*args: Any, **kwargs: Any) -> Any:
    from .delta_builder import build_delta_view as implementation

    return implementation(*args, **kwargs)


def validate_base_normal(*args: Any, **kwargs: Any) -> Any:
    from .validator import validate_base_normal as implementation

    return implementation(*args, **kwargs)


def validate_delta_normal(*args: Any, **kwargs: Any) -> Any:
    from .validator import validate_delta_normal as implementation

    return implementation(*args, **kwargs)


def validate_view_normal(*args: Any, **kwargs: Any) -> Any:
    from .validator import validate_view_normal as implementation

    return implementation(*args, **kwargs)

class BuildCancelled(RuntimeError):
    """The job's cancellation marker was observed at a safe boundary."""


class BuildValidationError(RuntimeError):
    """A freshly built immutable P3 artifact failed Normal validation."""


class _DirtySourceChanged(RuntimeError):
    """A dirty document no longer matches the stable catalog proof."""


@dataclass(frozen=True, slots=True)
class _ParentState:
    attestation: ParentAttestation
    generation: LogicalGenerationReceipt
    generation_recipe: GenerationRecipe
    view: SearchViewReceipt


@dataclass(slots=True)
class _MetricState:
    source_hash_ms: int = 0
    dirty_segment_ms: int = 0
    generation_ms: int = 0
    delta_ms: int = 0
    normal_validation_ms: int = 0
    legacy_export_ms: int = 0
    segments_rebuilt: int = 0
    segments_deleted: int = 0
    segments_loaded: int = 0
    segments_loaded_peak: int = 0
    postings_visited: int = 0
    base_postings_scanned: int = 0
    bytes_written: int = 0
    legacy_compile_runs: int = 0
    legacy_postings_visited: int = 0
    legacy_bytes_written: int = 0
    normal_validation_runs: int = 0
    compaction_recommended: bool = False

    def receipt(self) -> WorkerMetrics:
        return WorkerMetrics(**{
            field: getattr(self, field)
            for field in WorkerMetrics.__dataclass_fields__
        })


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def _streaming_canonical_receipt(value: object) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    for fragment in iter_canonical_json(value):
        encoded = fragment.encode("utf-8")
        digest.update(encoded)
        byte_size += len(encoded)
    return digest.hexdigest(), byte_size


def _check_plain_file(path: Path, field: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"cannot inspect {field}: {path}") from exc
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(attributes & reparse_mask)
    ):
        raise ValueError(f"{field} must be a plain regular file")
    return metadata


def _check_plain_directory(path: Path, field: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"cannot inspect {field}: {path}") from exc
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(attributes & reparse_mask)
    ):
        raise ValueError(f"{field} must be a plain directory")
    return metadata


def _hash_file(path: Path, field: str) -> tuple[str, int]:
    metadata = _check_plain_file(path, field)
    digest = hashlib.sha256()
    observed = 0
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != metadata.st_size:
            raise ValueError(f"{field} changed before hashing")
        while True:
            payload = stream.read(_READ_BUFFER_BYTES)
            if not payload:
                break
            digest.update(payload)
            observed += len(payload)
        after = os.fstat(stream.fileno())
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after) or observed != before.st_size:
        raise ValueError(f"{field} changed while hashing")
    return digest.hexdigest(), observed


def _strict_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _load_generation_receipt(
    attestation: GenerationAttestation,
) -> tuple[LogicalGenerationReceipt, GenerationRecipe]:
    """Authenticate compact control artifacts without loading a Segment."""

    root = attestation.generation_dir
    if not root.is_dir():
        raise ValueError(f"logical Generation directory is missing: {root}")
    observed = frozenset(path.name for path in root.iterdir())
    if observed != _GENERATION_FILES:
        raise ValueError("logical Generation directory has an invalid file set")

    manifest_path = root / MANIFEST_PATH
    manifest_sha256, manifest_bytes = _hash_file(
        manifest_path, "parent Generation manifest"
    )
    if manifest_sha256 != attestation.manifest_sha256:
        raise ValueError("parent Generation manifest attestation mismatch")
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            manifest_value = json.load(stream)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("parent Generation manifest is invalid JSON") from exc
    if not isinstance(manifest_value, dict):
        raise ValueError("parent Generation manifest must be an object")
    canonical_sha256, canonical_size = _streaming_canonical_receipt(
        manifest_value
    )
    if canonical_sha256 != manifest_sha256 or canonical_size != manifest_bytes:
        raise ValueError("parent Generation manifest is not canonical JSON")
    validate_logical_generation_manifest(manifest_value)
    if manifest_value.get("generation") != attestation.generation:
        raise ValueError("parent Generation ID differs from its manifest")

    count = manifest_value.get("document_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("parent Generation document_count is invalid")
    proof_value = _strict_mapping(
        manifest_value.get("input_proof"), "manifest input_proof"
    )
    try:
        proof_ref = ArtifactRef(
            relative_path=proof_value["relative_path"],
            sha256=proof_value["sha256"],
            byte_size=proof_value["byte_size"],
            records=proof_value["records"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("parent Generation input-proof receipt is invalid") from exc
    if proof_ref.relative_path != INPUT_PROOF_PATH or proof_ref.records != count:
        raise ValueError("parent Generation input-proof receipt is rebound")
    proof_sha256, proof_bytes = _hash_file(
        root / INPUT_PROOF_PATH, "parent Generation input proof"
    )
    if proof_sha256 != proof_ref.sha256 or proof_bytes != proof_ref.byte_size:
        raise ValueError("parent Generation input proof attestation mismatch")

    recipe_value = _strict_mapping(
        manifest_value.get("generation_recipe"), "generation_recipe"
    )
    try:
        recipe = GenerationRecipe(**dict(recipe_value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"parent Generation recipe is invalid: {exc}") from exc
    receipt = LogicalGenerationReceipt(
        candidate_dir=root,
        generation_id=attestation.generation,
        generation_recipe_hash=manifest_value["generation_recipe_hash"],
        manifest_ref=ArtifactRef(MANIFEST_PATH, manifest_sha256, manifest_bytes, count),
        input_proof_ref=proof_ref,
        document_count=count,
    )
    return receipt, recipe


def _load_parent(request: BuildRequest) -> _ParentState | None:
    if request.parent is None:
        return None
    expected_generation_dir = (
        request.pageindex_dir
        / "generations"
        / request.parent.generation.generation
    ).absolute()
    expected_view_dir = (
        request.pageindex_dir / "views" / request.parent.view.view_id
    ).absolute()
    if request.parent.generation.generation_dir != expected_generation_dir:
        raise ValueError(
            "parent Generation path traverses a link or escapes its store"
        )
    if request.parent.view.view_dir != expected_view_dir:
        raise ValueError("parent View path traverses a link or escapes its store")
    generation, recipe = _load_generation_receipt(request.parent.generation)
    view = load_search_view_metadata(
        request.pageindex_dir, request.parent.view.view_id
    )
    if view.root.resolve() != request.parent.view.view_dir:
        raise ValueError("parent View directory attestation mismatch")
    if view.manifest_ref.sha256 != request.parent.view.manifest_sha256:
        raise ValueError("parent View manifest attestation mismatch")
    if (
        view.generation != generation.generation_id
        or view.generation_manifest_sha256 != generation.manifest_ref.sha256
    ):
        raise ValueError("parent View is not bound to the parent Generation")
    return _ParentState(request.parent, generation, recipe, view)


def _capture_snapshot(
    request: BuildRequest,
    generation_recipe: GenerationRecipe,
    segment_recipe: SegmentRecipe,
    check_cancelled: Callable[[], None],
) -> StableCatalogSnapshot | None:
    return capture_stable_catalog(
        request.content_dir,
        segment_recipe_hash=canonical_hash(segment_recipe.as_dict()),
        compiler_recipe_hash=canonical_hash(generation_recipe.as_dict()),
        check_cancel=check_cancelled,
    )


def _collect_generation_refs(
    generation: LogicalGenerationReceipt,
    pageindex_dir: Path,
    expected_recipe: GenerationRecipe,
    check_cancelled: Callable[[], None],
) -> dict[str, StoredSegmentRef]:
    observed: list[GenerationRecipe] = []
    refs = validate_generation_stream(
        generation,
        pageindex_dir,
        check_cancelled=check_cancelled,
        collect_refs=True,
        recipe_observer=observed.append,
    )
    if observed != [expected_recipe]:
        raise ValueError("logical Generation recipe changed while authenticating")
    return refs


def _build_dirty_segments(
    snapshot: StableCatalogSnapshot,
    changes: SegmentChangeSet,
    pageindex_dir: Path,
    segment_recipe: SegmentRecipe,
    snapshot_root: Path,
    check_cancelled: Callable[[], None],
) -> tuple[StoredSegmentRef, ...]:
    sources = {source.doc_key: source for source in snapshot.sources}
    refs: list[StoredSegmentRef] = []
    for doc_key in sorted((*changes.added, *changes.changed)):
        check_cancelled()
        source = sources.get(doc_key)
        if source is None:
            raise ValueError(f"stable source catalog omits dirty document {doc_key}")
        immutable_source = _materialize_dirty_source(
            source,
            snapshot_root,
            changes.current_fingerprints[doc_key],
            check_cancelled,
        )
        segment = build_segment(immutable_source, segment_recipe)
        ref = put_segment(pageindex_dir, segment)
        if (
            ref.doc_key != doc_key
            or ref.content_hash != changes.current_fingerprints[doc_key]
            or ref.segment_recipe_hash != canonical_hash(segment_recipe.as_dict())
        ):
            raise ValueError(f"dirty Segment attestation differs from proof: {doc_key}")
        refs.append(ref)
        del segment
    return tuple(refs)


def _materialize_dirty_source(
    source: DocumentSource,
    snapshot_root: Path,
    expected_content_hash: str,
    check_cancelled: Callable[[], None],
) -> DocumentSource:
    """Stream one dirty document into immutable parser input and bind its hash."""

    live_root = source.root.resolve()
    target_root = Path(snapshot_root).resolve()
    records: list[dict[str, str]] = []
    for raw_relative in source.files:
        check_cancelled()
        relative_text = normalize_relative_path(Path(raw_relative).as_posix())
        relative = Path(relative_text)
        live = (live_root / relative).resolve()
        target = (target_root / relative).resolve()
        try:
            live.relative_to(live_root)
            target.relative_to(target_root)
        except ValueError as exc:
            raise ValueError(
                f"dirty source path escapes its root: {relative}"
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        try:
            with live.open("rb") as source_stream, target.open("xb") as sink:
                before = os.fstat(source_stream.fileno())
                remaining = int(before.st_size)
                while remaining:
                    payload = source_stream.read(
                        min(_READ_BUFFER_BYTES, remaining)
                    )
                    if not payload:
                        raise _DirtySourceChanged(str(live))
                    sink.write(payload)
                    digest.update(payload)
                    remaining -= len(payload)
                    check_cancelled()
                after = os.fstat(source_stream.fileno())
                sink.flush()
                os.fsync(sink.fileno())
        except (FileNotFoundError, FileExistsError) as exc:
            raise _DirtySourceChanged(str(live)) from exc
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise _DirtySourceChanged(str(live))
        records.append(
            {"path": relative_text, "sha256": digest.hexdigest()}
        )
    if canonical_hash(records) != expected_content_hash:
        raise _DirtySourceChanged(source.doc_key)
    return DocumentSource(
        doc_type=source.doc_type,
        slug=source.slug,
        doc_key=source.doc_key,
        root=target_root,
        files=source.files,
    )


def _target_refs(
    changes: SegmentChangeSet,
    new_refs: tuple[StoredSegmentRef, ...],
) -> tuple[StoredSegmentRef, ...]:
    new_by_doc = {ref.doc_key: ref for ref in new_refs}
    result: list[StoredSegmentRef] = []
    for doc_key in sorted(changes.current_fingerprints):
        if doc_key in new_by_doc:
            result.append(new_by_doc[doc_key])
        else:
            result.append(changes.base_by_doc[doc_key])
    return tuple(result)


def _remove_owned_candidate(path: Path, owner: Path) -> None:
    candidate = path.resolve()
    expected_parent = owner.resolve()
    if candidate.parent != expected_parent:
        raise RuntimeError("refusing to remove a candidate outside its job directory")
    shutil.rmtree(candidate)


def _finalize_generation(
    pageindex_dir: Path,
    receipt: LogicalGenerationReceipt,
    job_dir: Path,
) -> tuple[LogicalGenerationReceipt, bool]:
    root = pageindex_dir / "generations"
    root.mkdir(parents=True, exist_ok=True)
    _check_plain_directory(root, "logical Generation store")
    if root.resolve() != root.absolute():
        raise ValueError("logical Generation store must not traverse a link")
    destination = root / receipt.generation_id
    if os.path.lexists(destination):
        _check_plain_directory(
            destination, "logical Generation destination"
        )
        if destination.resolve() != destination.absolute():
            raise ValueError(
                "logical Generation destination traverses a link"
            )
        expected = GenerationAttestation(
            receipt.generation_id,
            destination,
            receipt.manifest_ref.sha256,
        )
        existing, _recipe = _load_generation_receipt(expected)
        if existing.as_dict() != receipt.as_dict():
            raise ValueError("logical Generation identity is occupied by different bytes")
        _remove_owned_candidate(receipt.candidate_dir, job_dir)
        return existing, False
    try:
        _rename_no_replace(receipt.candidate_dir, destination)
    except OSError:
        if not os.path.lexists(destination):
            raise
        _check_plain_directory(
            destination, "logical Generation destination"
        )
        if destination.resolve() != destination.absolute():
            raise ValueError(
                "logical Generation destination traverses a link"
            )
        expected = GenerationAttestation(
            receipt.generation_id,
            destination,
            receipt.manifest_ref.sha256,
        )
        existing, _recipe = _load_generation_receipt(expected)
        if existing.as_dict() != receipt.as_dict():
            raise ValueError("logical Generation publication conflict")
        _remove_owned_candidate(receipt.candidate_dir, job_dir)
        return existing, False
    return replace(receipt, candidate_dir=destination), True


def _layer_bytes(layer: object) -> int:
    return sum(
        getattr(layer, name).byte_size
        for name in ("documents", "postings", "chunks", "terms", "sparse_index")
    )


def _base_view_bytes(base: object, view: SearchViewReceipt) -> int:
    return (
        base.manifest_ref.byte_size
        + _layer_bytes(base.layer)
        + view.manifest_ref.byte_size
        + view.statistics_ref.byte_size
        + view.documents_ref.byte_size
    )


def _parent_compaction_recommended(
    pageindex_dir: Path, view: SearchViewReceipt
) -> bool:
    """Recompute the policy hint without loading postings or Segments."""

    policy = CompactionPolicy()
    if not view.delta_ids:
        return False
    if len(view.delta_ids) >= policy.max_delta_layers:
        return True
    base = load_base_object_metadata(pageindex_dir, view.base_id)
    base_bytes = base.manifest_ref.byte_size + _layer_bytes(base.layer)
    delta_bytes = 0
    for delta_id in view.delta_ids:
        delta = load_delta_object_metadata(pageindex_dir, delta_id)
        delta_bytes += delta.manifest_ref.byte_size + _layer_bytes(delta.layer)
    return (
        delta_bytes * policy.max_delta_bytes_denominator
        >= base_bytes * policy.max_delta_bytes_numerator
    )


def _attest_generation(receipt: LogicalGenerationReceipt) -> GenerationAttestation:
    return GenerationAttestation(
        generation=receipt.generation_id,
        generation_dir=receipt.candidate_dir,
        manifest_sha256=receipt.manifest_ref.sha256,
    )


def _attest_view(receipt: SearchViewReceipt) -> ViewAttestation:
    return ViewAttestation(
        view_id=receipt.view_id,
        view_dir=receipt.root,
        manifest_sha256=receipt.manifest_ref.sha256,
        generation=receipt.generation,
        generation_manifest_sha256=receipt.generation_manifest_sha256,
    )


def _require_valid(report: object, field: str) -> None:
    if not getattr(report, "ok", False):
        errors = getattr(report, "errors", ())
        detail = "; ".join(str(value) for value in errors) or "unknown error"
        raise BuildValidationError(f"{field} failed: {detail}")


def _run_legacy_export(
    request: BuildRequest,
    generation: LogicalGenerationReceipt,
    metrics: _MetricState,
    check_cancelled: Callable[[], None],
) -> LegacyExportAttestation | None:
    if request.legacy_export == "none":
        return None
    # Keep the P2 compatibility compiler outside the imported default path.
    from .legacy_export import export_legacy_generation

    started = time.perf_counter()
    receipt = export_legacy_generation(
        generation,
        request.pageindex_dir,
        trusted_generation=generation.generation_id,
        check_cancelled=check_cancelled,
    )
    metrics.legacy_export_ms += _elapsed_ms(started)
    counters = receipt.counters
    metrics.legacy_compile_runs += counters["legacy_compile_runs"]
    metrics.legacy_postings_visited += counters["legacy_postings_visited"]
    metrics.legacy_bytes_written += counters["legacy_bytes_written"]
    return LegacyExportAttestation(
        generation=receipt.logical_generation,
        export_id=receipt.export_id,
        export_dir=receipt.export_dir,
        manifest_sha256=receipt.manifest_sha256,
    )


def _success_result(
    request: BuildRequest,
    state: str,
    generation: GenerationAttestation,
    view: ViewAttestation,
    legacy: LegacyExportAttestation | None,
    metrics: _MetricState,
) -> BuildResult:
    return BuildResult(
        protocol=PROTOCOL_NAME,
        protocol_version=PROTOCOL_VERSION,
        job_id=request.job_id,
        mode=request.mode,
        legacy_export=request.legacy_export,
        state=state,  # type: ignore[arg-type]
        parent=request.parent,
        generation=generation,
        view=view,
        legacy_export_artifact=legacy,
        metrics=metrics.receipt(),
        error=None,
    )


def _execute_incremental(
    request: BuildRequest,
    parent: _ParentState | None,
    metrics: _MetricState,
    check_cancelled: Callable[[], None],
    job_dir: Path,
) -> BuildResult:
    generation_recipe = GenerationRecipe()
    search_recipe = SearchViewRecipe()
    segment_recipe = SegmentRecipe()
    if parent is not None and parent.generation_recipe != generation_recipe:
        raise ValueError(
            "parent Generation recipe differs from the active P3 recipe; "
            "a recipe migration requires an explicit future migration path"
        )
    if (
        parent is not None
        and parent.view.search_view_recipe_hash
        != canonical_hash(search_recipe.as_dict())
    ):
        raise ValueError(
            "parent SearchViewRecipe differs from the active P3 recipe; "
            "physical recipe migration requires explicit optimize support"
        )

    parent_refs: dict[str, StoredSegmentRef] | None = None
    snapshot: StableCatalogSnapshot | None = None
    changes: SegmentChangeSet | None = None
    new_refs: tuple[StoredSegmentRef, ...] = ()
    dirty_attempted = False
    for _attempt in range(_MAX_STABILIZATION_ATTEMPTS):
        check_cancelled()
        started = time.perf_counter()
        snapshot = _capture_snapshot(
            request, generation_recipe, segment_recipe, check_cancelled
        )
        metrics.source_hash_ms += _elapsed_ms(started)
        if snapshot is None:
            continue

        if (
            parent is not None
            and snapshot.proof_sha256 == parent.generation.input_proof_ref.sha256
        ):
            if dirty_attempted:
                raise RuntimeError(
                    "source returned to the parent proof after a dirty retry; "
                    "rerun the incremental request"
                )
            metrics.compaction_recommended = _parent_compaction_recommended(
                request.pageindex_dir, parent.view
            )
            legacy = _run_legacy_export(
                request, parent.generation, metrics, check_cancelled
            )
            return _success_result(
                request,
                "no_op",
                request.parent.generation,
                request.parent.view,
                legacy,
                metrics,
            )

        if parent is not None and parent_refs is None:
            parent_refs = _collect_generation_refs(
                parent.generation,
                request.pageindex_dir,
                generation_recipe,
                check_cancelled,
            )
        base_refs = () if parent_refs is None else tuple(parent_refs.values())
        changes = diff_segment_inputs(snapshot, base_refs)
        if not (changes.added or changes.changed or changes.deleted):
            raise ValueError("source proof changed without a Segment-level change")
        started = time.perf_counter()
        dirty_attempted = True
        try:
            with tempfile.TemporaryDirectory(
                dir=job_dir, prefix="source-snapshot-"
            ) as snapshot_name:
                new_refs = _build_dirty_segments(
                    snapshot,
                    changes,
                    request.pageindex_dir,
                    segment_recipe,
                    Path(snapshot_name),
                    check_cancelled,
                )
        except _DirtySourceChanged:
            metrics.dirty_segment_ms += _elapsed_ms(started)
            continue
        except ValueError:
            if not snapshot.verify_unchanged(check_cancelled):
                metrics.dirty_segment_ms += _elapsed_ms(started)
                continue
            raise
        metrics.dirty_segment_ms += _elapsed_ms(started)
        if snapshot.verify_unchanged(check_cancelled):
            break
    else:
        raise RuntimeError("source catalog did not stabilize after three attempts")

    assert snapshot is not None and changes is not None
    metrics.segments_rebuilt = len(new_refs)
    metrics.segments_deleted = len(changes.deleted)
    new_ref_count = len(new_refs)
    new_segment_bytes = sum(ref.byte_size for ref in new_refs)
    refs = _target_refs(changes, new_refs)
    target_ref_count = len(refs)
    proof = snapshot.validated_proof()
    del snapshot
    parent_refs = None

    generation_candidate = job_dir / "generation-candidate"
    started = time.perf_counter()
    candidate = build_logical_generation(
        refs,
        proof,
        generation_recipe,
        generation_candidate,
        check_cancelled,
    )
    generation, published = _finalize_generation(
        request.pageindex_dir, candidate, job_dir
    )
    del proof
    metrics.generation_ms += _elapsed_ms(started)
    if published:
        metrics.bytes_written += (
            generation.manifest_ref.byte_size + generation.input_proof_ref.byte_size
        )
    metrics.bytes_written += new_segment_bytes
    check_cancelled()

    started = time.perf_counter()
    delta_result: Any | None = None
    if parent is None:
        base, view = build_base_view(
            request.pageindex_dir,
            refs,
            generation,
            generation_recipe,
            search_recipe,
            check_cancelled=check_cancelled,
        )
        metrics.segments_loaded += target_ref_count
        metrics.segments_loaded_peak = min(1, target_ref_count)
        metrics.postings_visited += base.statistics.posting_count
        metrics.bytes_written += _base_view_bytes(base, view)
    else:
        if generation.generation_id == parent.generation.generation_id:
            raise ValueError("dirty inputs did not advance the logical Generation")
        delta_result = build_delta_view(
            request.pageindex_dir,
            parent.view,
            generation,
            generation_recipe,
            changes,
            new_refs,
            search_recipe,
            CompactionPolicy(),
            check_cancelled=check_cancelled,
        )
        view = delta_result.view
        metrics.segments_loaded += delta_result.work.new_segments_loaded
        metrics.segments_loaded_peak = max(
            metrics.segments_loaded_peak,
            delta_result.work.segments_loaded_peak,
        )
        metrics.postings_visited += delta_result.work.projected_postings
        metrics.bytes_written += delta_result.work.bytes_written
        metrics.bytes_written += sum(
            replacement.new_summary_bytes or 0
            for replacement in delta_result.delta.replacements
        )
        metrics.compaction_recommended = delta_result.compaction.recommended
    metrics.delta_ms += _elapsed_ms(started)
    del refs, new_refs, changes
    check_cancelled()

    started = time.perf_counter()
    if parent is None:
        _require_valid(
            validate_base_normal(
                base,
                generation,
                request.pageindex_dir,
                check_cancelled=check_cancelled,
            ),
            "Base Normal validation",
        )
        _require_valid(
            validate_view_normal(
                view,
                generation,
                request.pageindex_dir,
                pin=ViewPin(generation.generation_id, view.view_id),
                check_cancelled=check_cancelled,
            ),
            "View Normal validation",
        )
        metrics.postings_visited += base.layer.postings.records or 0
    else:
        assert delta_result is not None
        _require_valid(
            validate_delta_normal(
                delta_result.delta,
                parent.view,
                view,
                parent.generation,
                generation,
                request.pageindex_dir,
                parent_pin=ViewPin(
                    parent.generation.generation_id, parent.view.view_id
                ),
                target_pin=ViewPin(generation.generation_id, view.view_id),
                check_cancelled=check_cancelled,
            ),
            "Delta Normal validation",
        )
        metrics.segments_loaded += new_ref_count
        metrics.segments_loaded_peak = max(
            metrics.segments_loaded_peak, min(1, new_ref_count)
        )
        metrics.postings_visited += delta_result.work.projected_postings
        metrics.postings_visited += 2 * (
            delta_result.delta.layer.postings.records or 0
        )
    metrics.normal_validation_ms += _elapsed_ms(started)
    metrics.normal_validation_runs = 1

    legacy = _run_legacy_export(request, generation, metrics, check_cancelled)
    return _success_result(
        request,
        "ready_to_publish",
        _attest_generation(generation),
        _attest_view(view),
        legacy,
        metrics,
    )


def _execute_optimize(
    request: BuildRequest,
    parent: _ParentState,
    metrics: _MetricState,
    check_cancelled: Callable[[], None],
) -> BuildResult:
    generation_recipe = GenerationRecipe()
    search_recipe = SearchViewRecipe()
    if parent.generation_recipe != generation_recipe:
        raise ValueError("parent Generation recipe differs from the active P3 recipe")
    refs_by_key = _collect_generation_refs(
        parent.generation,
        request.pageindex_dir,
        generation_recipe,
        check_cancelled,
    )
    refs = tuple(refs_by_key.values())
    ref_count = len(refs)
    del refs_by_key

    started = time.perf_counter()
    base, view = build_base_view(
        request.pageindex_dir,
        refs,
        parent.generation,
        generation_recipe,
        search_recipe,
        check_cancelled=check_cancelled,
    )
    del refs
    metrics.delta_ms += _elapsed_ms(started)
    if view.view_id == parent.view.view_id:
        raise ValueError("the parent View is already an optimized zero-Delta View")
    metrics.segments_loaded += ref_count
    metrics.segments_loaded_peak = min(1, ref_count)
    metrics.postings_visited += base.statistics.posting_count
    metrics.bytes_written += _base_view_bytes(base, view)

    started = time.perf_counter()
    _require_valid(
        validate_base_normal(
            base,
            parent.generation,
            request.pageindex_dir,
            check_cancelled=check_cancelled,
        ),
        "optimized Base Normal validation",
    )
    _require_valid(
        validate_view_normal(
            view,
            parent.generation,
            request.pageindex_dir,
            pin=ViewPin(parent.generation.generation_id, view.view_id),
            check_cancelled=check_cancelled,
        ),
        "optimized View Normal validation",
    )
    metrics.normal_validation_ms += _elapsed_ms(started)
    metrics.normal_validation_runs = 1
    metrics.postings_visited += base.layer.postings.records or 0

    legacy = _run_legacy_export(
        request, parent.generation, metrics, check_cancelled
    )
    return _success_result(
        request,
        "ready_to_publish",
        request.parent.generation,
        _attest_view(view),
        legacy,
        metrics,
    )


def execute_request(
    request: BuildRequest,
    *,
    check_cancelled: Callable[[], None] | None = None,
    job_dir: Path | None = None,
) -> BuildResult:
    """Execute one validated request and always return a strict terminal result."""

    if not isinstance(request, BuildRequest):
        raise TypeError("request must be a BuildRequest")
    metrics = _MetricState()
    cancel = (lambda: None) if check_cancelled is None else check_cancelled
    if not callable(cancel):
        raise TypeError("check_cancelled must be callable")
    work = (
        request.pageindex_dir / "build" / request.job_id
        if job_dir is None
        else Path(job_dir).resolve()
    )
    try:
        work.mkdir(parents=True, exist_ok=True)
        cancel()
        parent = _load_parent(request)
        if request.mode == "incremental":
            return _execute_incremental(request, parent, metrics, cancel, work)
        if parent is None:
            raise ProtocolError("optimize requires a complete parent pair")
        return _execute_optimize(request, parent, metrics, cancel)
    except BuildCancelled:
        return BuildResult(
            PROTOCOL_NAME,
            PROTOCOL_VERSION,
            request.job_id,
            request.mode,
            request.legacy_export,
            "cancelled",
            request.parent,
            None,
            None,
            None,
            metrics.receipt(),
            None,
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:4000] or "build failed"
        return BuildResult(
            PROTOCOL_NAME,
            PROTOCOL_VERSION,
            request.job_id,
            request.mode,
            request.legacy_export,
            "failed",
            request.parent,
            None,
            None,
            None,
            metrics.receipt(),
            WorkerError("build_failed", message),
        )


def _write_atomic(path: Path, payload: bytes) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.{os.getpid()}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_invalid_result(path: Path, message: str) -> None:
    payload = {
        "protocol": PROTOCOL_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "state": "failed",
        "error": {"code": "invalid_request", "message": message[:4000]},
    }
    _write_atomic(path, canonical_bytes(payload) + b"\n")


def run_worker(request_path: Path) -> int:
    """Read one JSON-line task file, execute it, and atomically write result.json."""

    path = Path(request_path).absolute()
    result_path = path.parent / "result.json"
    try:
        metadata = _check_plain_file(path, "worker request")
        if metadata.st_size > MAX_JSON_LINE_BYTES:
            raise ProtocolError("request exceeds JSON-line byte limit")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            payload = stream.read(MAX_JSON_LINE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(payload) > MAX_JSON_LINE_BYTES:
            raise ProtocolError("request exceeds JSON-line byte limit")
        request_identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if (
            request_identity(metadata) != request_identity(before)
            or request_identity(before) != request_identity(after)
            or len(payload) != before.st_size
        ):
            raise ProtocolError("request changed while being read")
        request = decode_request_line(payload)
        expected_job = (
            request.pageindex_dir / "build" / request.job_id
        ).absolute()
        if path.name != "request.json" or path.parent != expected_job:
            raise ProtocolError(
                "request path must equal "
                "pageindex_dir/build/<job_id>/request.json"
            )
        _check_plain_directory(path.parent, "worker job directory")
        if path.parent.resolve() != expected_job:
            raise ProtocolError("worker job directory traverses a link")
    except (OSError, ValueError, ProtocolError) as exc:
        _write_invalid_result(result_path, str(exc))
        return EXIT_INVALID_REQUEST

    cancel_path = path.parent / "cancel.request"

    def check_cancelled() -> None:
        if cancel_path.is_file():
            raise BuildCancelled("build cancelled")

    result = execute_request(
        request,
        check_cancelled=check_cancelled,
        job_dir=path.parent,
    )
    _write_atomic(result_path, encode_result_line(result))
    if result.state in {"no_op", "ready_to_publish"}:
        return EXIT_SUCCESS
    if result.state == "cancelled":
        return EXIT_CANCELLED
    return EXIT_BUILD_FAILED


__all__ = [
    "BuildCancelled",
    "BuildValidationError",
    "EXIT_BUILD_FAILED",
    "EXIT_CANCELLED",
    "EXIT_INVALID_REQUEST",
    "EXIT_SUCCESS",
    "execute_request",
    "run_worker",
]
