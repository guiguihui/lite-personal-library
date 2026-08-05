"""Launch and authenticate one fresh PageIndex v3 worker process per build."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.index.v2.canonical import canonical_bytes

from .delta_store import DeltaObjectReceipt, load_delta_object_metadata
from .layer_codec import PostingLayerReader
from .protocol import (
    EXIT_BUILD_FAILED,
    EXIT_CANCELLED,
    EXIT_SUCCESS,
    MAX_JSON_LINE_BYTES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    BuildMode,
    BuildRequest,
    BuildResult,
    GenerationAttestation,
    LegacyExportAttestation,
    LegacyExportMode,
    ParentAttestation,
    ProtocolError,
    encode_request_line,
    decode_result_line,
)
from .view_store import (
    BaseObjectReceipt,
    SearchViewReceipt,
    load_base_object_metadata,
    load_search_view_metadata,
)


_READ_BUFFER_BYTES = 1024 * 1024
_GENERATION_FILES = frozenset({"manifest.json", "input-proof.json"})
_LEGACY_SCHEMA_VERSIONS = frozenset({2, 3})
_LEGACY_ARTIFACT_KIND = "legacy_generation"
_LEGACY_EXPORT_ID_RE = re.compile(r"^[0-9a-f]{20}$")
_GENERATION_PREFIX_RE = re.compile(
    rb'^\{"artifact_kind":"logical_generation","document_count":'
    rb"(?P<count>0|[1-9][0-9]*),"
    rb'"documents":\{'
)
_GENERATION_TAIL_RE = re.compile(
    rb'\},"generation":"(?P<generation>[0-9a-f]{64})",'
    rb'"generation_recipe":\{.*\},'
    rb'"generation_recipe_hash":"[0-9a-f]{64}",'
    rb'"input_proof":\{"byte_size":(?P<proof_size>0|[1-9][0-9]*),'
    rb'"records":(?P<proof_records>0|[1-9][0-9]*),'
    rb'"relative_path":"input-proof\.json",'
    rb'"sha256":"(?P<proof_sha256>[0-9a-f]{64})"\},'
    rb'"schema_version":4\}$'
)
_GENERATION_PREFIX_BYTES = 256
_GENERATION_TAIL_BYTES = 64 * 1024


class WorkerProcessError(RuntimeError):
    """The worker failed to produce a result that is safe to consume."""


@dataclass(frozen=True, slots=True)
class _VerifiedPair:
    generation: GenerationAttestation
    view: SearchViewReceipt


def _path_is_link(metadata: os.stat_result) -> bool:
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_mask)


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Cross-platform stable file identity for anti-drift checks.

    On Windows/NTFS, ``st_ctime_ns`` is unreliable: opening a just-written file
    for read can refresh the ctime, so an ``lstat()`` before open and an
    ``fstat()`` after open can differ by a few ms even though the file is
    untouched. Comparing ctime therefore produces false "changed while reading"
    failures on the desktop (Windows) target. We omit ctime on Windows and keep
    dev/ino/size/mtime_ns; on other platforms ctime is kept.
    """

    if os.name == "nt":
        return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_plain_directory(path: Path, field: str) -> Path:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise WorkerProcessError(f"cannot inspect {field}: {candidate}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or _path_is_link(metadata):
        raise WorkerProcessError(f"{field} must be a plain directory: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkerProcessError(f"cannot resolve {field}: {candidate}: {exc}") from exc
    if resolved != candidate.resolve():
        raise WorkerProcessError(f"{field} changed while being resolved: {candidate}")
    return resolved


def _require_scoped_directory(
    actual: Path,
    pageindex_dir: Path,
    components: tuple[str, ...],
    field: str,
) -> Path:
    cursor = _require_plain_directory(Path(pageindex_dir), "PageIndex root")
    for component in components:
        child = cursor / component
        resolved = _require_plain_directory(child, field)
        if resolved.parent != cursor or resolved.name != component:
            raise WorkerProcessError(f"{field} escapes PageIndex: {actual}")
        cursor = resolved
    if Path(actual) != cursor:
        raise WorkerProcessError(
            f"{field} {actual} does not equal the scoped object path {cursor}"
        )
    return cursor


def _plain_file_metadata(path: Path, field: str) -> os.stat_result:
    try:
        metadata = Path(path).lstat()
    except OSError as exc:
        raise WorkerProcessError(f"cannot inspect {field}: {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or _path_is_link(metadata):
        raise WorkerProcessError(f"{field} must be a plain file: {path}")
    return metadata


def _read_stable_bytes(
    path: Path,
    field: str,
    *,
    max_bytes: int | None = None,
) -> bytes:
    metadata = _plain_file_metadata(path, field)
    if max_bytes is not None and metadata.st_size > max_bytes:
        raise WorkerProcessError(f"{field} exceeds the byte limit")
    try:
        with Path(path).open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(metadata):
                raise WorkerProcessError(f"{field} changed before it was read")
            payload = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
    except WorkerProcessError:
        raise
    except OSError as exc:
        raise WorkerProcessError(f"cannot read {field}: {path}: {exc}") from exc
    if max_bytes is not None and len(payload) > max_bytes:
        raise WorkerProcessError(f"{field} exceeds the byte limit")
    if _identity(opened) != _identity(after) or len(payload) != opened.st_size:
        raise WorkerProcessError(f"{field} changed while it was read")
    current = _plain_file_metadata(path, field)
    if _identity(current) != _identity(after):
        raise WorkerProcessError(f"{field} changed after it was read")
    return payload


def _hash_stable_file(
    path: Path,
    field: str,
    *,
    prefix_bytes: int = 0,
    tail_bytes: int = 0,
) -> tuple[str, int, bytes, bytes]:
    if prefix_bytes < 0 or tail_bytes < 0:
        raise ValueError("capture byte counts must be non-negative")
    metadata = _plain_file_metadata(path, field)
    digest = hashlib.sha256()
    observed = 0
    prefix = bytearray()
    tail = bytearray()
    try:
        with Path(path).open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(metadata):
                raise WorkerProcessError(f"{field} changed before hashing")
            while True:
                payload = stream.read(_READ_BUFFER_BYTES)
                if not payload:
                    break
                digest.update(payload)
                observed += len(payload)
                if len(prefix) < prefix_bytes:
                    prefix.extend(payload[: prefix_bytes - len(prefix)])
                if tail_bytes:
                    tail.extend(payload)
                    if len(tail) > tail_bytes:
                        del tail[:-tail_bytes]
            after = os.fstat(stream.fileno())
    except WorkerProcessError:
        raise
    except OSError as exc:
        raise WorkerProcessError(f"cannot hash {field}: {path}: {exc}") from exc
    if _identity(opened) != _identity(after) or observed != opened.st_size:
        raise WorkerProcessError(f"{field} changed while hashing")
    current = _plain_file_metadata(path, field)
    if _identity(current) != _identity(after):
        raise WorkerProcessError(f"{field} changed after hashing")
    return digest.hexdigest(), observed, bytes(prefix), bytes(tail)


def _decode_canonical_object(payload: bytes, field: str) -> Mapping[str, Any]:
    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise WorkerProcessError(f"{field} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise WorkerProcessError(f"{field} contains non-finite number {value!r}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except WorkerProcessError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProcessError(f"{field} is not valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise WorkerProcessError(f"{field} must be a JSON object")
    try:
        encoded = canonical_bytes(value)
    except (TypeError, ValueError) as exc:
        raise WorkerProcessError(f"{field} cannot be canonicalized") from exc
    if encoded != payload:
        raise WorkerProcessError(f"{field} is not canonical JSON")
    return value


def _directory_names(root: Path, field: str) -> frozenset[str]:
    try:
        return frozenset(path.name for path in root.iterdir())
    except OSError as exc:
        raise WorkerProcessError(f"cannot enumerate {field}: {root}: {exc}") from exc


def _verify_generation(
    attestation: GenerationAttestation,
    pageindex_dir: Path,
) -> None:
    root = _require_scoped_directory(
        attestation.generation_dir,
        pageindex_dir,
        ("generations", attestation.generation),
        "Generation directory",
    )
    if _directory_names(root, "Generation directory") != _GENERATION_FILES:
        raise WorkerProcessError("Generation directory has an invalid file set")
    actual_hash, _, prefix, tail = _hash_stable_file(
        root / "manifest.json",
        "Generation manifest",
        prefix_bytes=_GENERATION_PREFIX_BYTES,
        tail_bytes=_GENERATION_TAIL_BYTES,
    )
    if actual_hash != attestation.manifest_sha256:
        raise WorkerProcessError(
            "Generation manifest hash does not match the worker attestation"
        )
    prefix_match = _GENERATION_PREFIX_RE.match(prefix)
    tail_match = _GENERATION_TAIL_RE.search(tail)
    if prefix_match is None or tail_match is None:
        raise WorkerProcessError(
            "Generation manifest control envelope is invalid or unbounded"
        )
    generation = tail_match.group("generation").decode("ascii")
    if generation != attestation.generation:
        raise WorkerProcessError(
            "Generation manifest identity does not match the worker attestation"
        )
    document_count = int(prefix_match.group("count"))
    proof_records = int(tail_match.group("proof_records"))
    if proof_records != document_count:
        raise WorkerProcessError(
            "Generation input proof record count does not match the manifest"
        )
    proof_hash, proof_size, _, _ = _hash_stable_file(
        root / "input-proof.json",
        "Generation input proof",
    )
    if (
        proof_hash != tail_match.group("proof_sha256").decode("ascii")
        or proof_size != int(tail_match.group("proof_size"))
    ):
        raise WorkerProcessError(
            "Generation input proof does not match its manifest receipt"
        )


def _verify_view(
    attestation: Any,
    pageindex_dir: Path,
) -> SearchViewReceipt:
    root = _require_scoped_directory(
        attestation.view_dir,
        pageindex_dir,
        ("views", attestation.view_id),
        "View directory",
    )
    actual_hash, _, _, _ = _hash_stable_file(
        root / "manifest.json", "View manifest"
    )
    if actual_hash != attestation.manifest_sha256:
        raise WorkerProcessError(
            "View manifest hash does not match the worker attestation"
        )
    try:
        receipt = load_search_view_metadata(pageindex_dir, attestation.view_id)
    except Exception as exc:
        raise WorkerProcessError(f"View metadata is invalid: {exc}") from exc
    try:
        receipt_root = receipt.root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkerProcessError("cannot resolve authenticated View root") from exc
    if receipt_root != root or receipt.manifest_ref.sha256 != actual_hash:
        raise WorkerProcessError("View receipt was rebound after authentication")
    if (
        receipt.view_id != attestation.view_id
        or receipt.generation != attestation.generation
        or receipt.generation_manifest_sha256
        != attestation.generation_manifest_sha256
    ):
        raise WorkerProcessError("View receipt does not match its attestation")
    for name, reference in (
        ("statistics", receipt.statistics_ref),
        ("documents", receipt.documents_ref),
    ):
        digest, byte_size, _, _ = _hash_stable_file(
            root / reference.relative_path,
            f"View {name}",
        )
        if digest != reference.sha256 or byte_size != reference.byte_size:
            raise WorkerProcessError(
                f"View {name} does not match its manifest receipt"
            )
    return receipt


def _verify_pair(
    pair: ParentAttestation,
    pageindex_dir: Path,
) -> _VerifiedPair:
    _verify_generation(pair.generation, pageindex_dir)
    view = _verify_view(pair.view, pageindex_dir)
    if (
        view.generation != pair.generation.generation
        or view.generation_manifest_sha256 != pair.generation.manifest_sha256
    ):
        raise WorkerProcessError("Generation/View pair has inconsistent lineage")
    return _VerifiedPair(pair.generation, view)


def _load_base(pageindex_dir: Path, view: SearchViewReceipt) -> BaseObjectReceipt:
    try:
        receipt = load_base_object_metadata(pageindex_dir, view.base_id)
    except Exception as exc:
        raise WorkerProcessError(f"Base metadata is invalid: {exc}") from exc
    if receipt.base_id != view.base_id:
        raise WorkerProcessError("Base receipt identity does not match the View")
    if receipt.search_view_recipe_hash != view.search_view_recipe_hash:
        raise WorkerProcessError("Base/View recipe lineage does not match")
    return receipt


def _load_delta(pageindex_dir: Path, delta_id: str) -> DeltaObjectReceipt:
    try:
        return load_delta_object_metadata(pageindex_dir, delta_id)
    except Exception as exc:
        raise WorkerProcessError(f"Delta metadata is invalid: {exc}") from exc


def _verify_ready_lineage(
    request: BuildRequest,
    result: BuildResult,
    target: _VerifiedPair,
    parent: _VerifiedPair | None,
) -> None:
    assert result.generation is not None
    if request.mode == "optimize":
        if parent is None:
            raise WorkerProcessError("optimize lineage requires an explicit parent")
        if target.view.delta_ids:
            raise WorkerProcessError("optimize lineage must end in a clean Base View")
        if target.view.search_view_recipe_hash != parent.view.search_view_recipe_hash:
            raise WorkerProcessError("optimize lineage changed the Search View recipe")
        base = _load_base(request.pageindex_dir, target.view)
        if (
            base.generation != result.generation.generation
            or base.generation_manifest_sha256 != result.generation.manifest_sha256
        ):
            raise WorkerProcessError("optimize Base is not bound to the Generation")
        return

    if parent is None:
        if target.view.delta_ids:
            raise WorkerProcessError("bootstrap lineage must end in a clean Base View")
        base = _load_base(request.pageindex_dir, target.view)
        if (
            base.generation != result.generation.generation
            or base.generation_manifest_sha256 != result.generation.manifest_sha256
        ):
            raise WorkerProcessError("bootstrap Base is not bound to the Generation")
        return

    if result.generation.generation == parent.generation.generation:
        raise WorkerProcessError(
            "incremental lineage must create a new logical Generation"
        )
    if target.view.search_view_recipe_hash != parent.view.search_view_recipe_hash:
        raise WorkerProcessError("incremental lineage changed the Search View recipe")
    if target.view.base_id != parent.view.base_id:
        raise WorkerProcessError("incremental lineage replaced the trusted Base")
    if (
        len(target.view.delta_ids) != len(parent.view.delta_ids) + 1
        or target.view.delta_ids[:-1] != parent.view.delta_ids
    ):
        raise WorkerProcessError(
            "incremental lineage must append exactly one Delta to the parent View"
        )
    delta = _load_delta(request.pageindex_dir, target.view.delta_ids[-1])
    if (
        delta.parent_view_id != request.parent.view.view_id
        or delta.parent_view_manifest_sha256
        != request.parent.view.manifest_sha256
    ):
        raise WorkerProcessError("incremental Delta is not bound to the parent View")
    if (
        delta.generation != result.generation.generation
        or delta.generation_manifest_sha256 != result.generation.manifest_sha256
        or delta.search_view_recipe_hash != target.view.search_view_recipe_hash
    ):
        raise WorkerProcessError("incremental Delta is not bound to the result View")
    try:
        with PostingLayerReader(delta.layer, load_documents=False) as reader:
            reader.authenticate_artifacts()
    except Exception as exc:
        raise WorkerProcessError(
            f"incremental Delta layer artifacts are invalid: {exc}"
        ) from exc


def _verify_legacy_export(
    attestation: LegacyExportAttestation,
    pageindex_dir: Path,
) -> None:
    if not _LEGACY_EXPORT_ID_RE.fullmatch(attestation.export_id):
        raise WorkerProcessError(
            "legacy export_id must be 20 lowercase hexadecimal characters"
        )
    root = _require_scoped_directory(
        attestation.export_dir,
        pageindex_dir,
        (
            "exports",
            "legacy",
            attestation.generation,
            attestation.export_id,
        ),
        "legacy export directory",
    )
    payload = _read_stable_bytes(root / "manifest.json", "legacy manifest")
    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != attestation.manifest_sha256:
        raise WorkerProcessError(
            "legacy manifest hash does not match the worker attestation"
        )
    manifest = _decode_canonical_object(payload, "legacy manifest")
    schema = manifest.get("schema_version")
    if type(schema) is not int or schema not in _LEGACY_SCHEMA_VERSIONS:
        raise WorkerProcessError("legacy manifest has an unsupported schema")
    if manifest.get("generation") != attestation.export_id:
        raise WorkerProcessError("legacy manifest identity does not match export_id")
    if (
        "artifact_kind" in manifest
        and manifest.get("artifact_kind") != _LEGACY_ARTIFACT_KIND
    ):
        raise WorkerProcessError("legacy manifest declares a non-legacy artifact")


def _verify_worker_completion(
    result: BuildResult,
    request: BuildRequest,
    returncode: int,
    *,
    verified_parent: _VerifiedPair | None,
) -> None:
    if not isinstance(result, BuildResult):
        raise TypeError("result must be a BuildResult")
    if not isinstance(request, BuildRequest):
        raise TypeError("request must be a BuildRequest")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise WorkerProcessError("worker return code must be an integer")
    expected_identity = (
        ("job_id", result.job_id, request.job_id),
        ("mode", result.mode, request.mode),
        ("legacy_export", result.legacy_export, request.legacy_export),
        ("parent", result.parent, request.parent),
    )
    for field, actual, trusted in expected_identity:
        if actual != trusted:
            raise WorkerProcessError(
                f"worker result {field} does not match the trusted request"
            )
    expected = {
        "no_op": EXIT_SUCCESS,
        "ready_to_publish": EXIT_SUCCESS,
        "failed": EXIT_BUILD_FAILED,
        "cancelled": EXIT_CANCELLED,
    }[result.state]
    if returncode != expected:
        raise WorkerProcessError(
            f"worker exit code {returncode} disagrees with result state {result.state!r}"
        )
    if result.state not in {"no_op", "ready_to_publish"}:
        return

    if verified_parent is not None:
        if request.parent is None or verified_parent.generation != request.parent.generation:
            raise WorkerProcessError("preverified parent does not match the request")
        parent = verified_parent
    else:
        parent = (
            None
            if request.parent is None
            else _verify_pair(request.parent, request.pageindex_dir)
        )

    assert result.generation is not None and result.view is not None
    if result.state == "no_op":
        if parent is None:
            raise WorkerProcessError("no-op result is missing its verified parent")
        target = parent
    else:
        target = _verify_pair(
            ParentAttestation(result.generation, result.view),
            request.pageindex_dir,
        )
        _verify_ready_lineage(request, result, target, parent)
    if result.legacy_export_artifact is not None:
        _verify_legacy_export(
            result.legacy_export_artifact,
            request.pageindex_dir,
        )


def verify_worker_completion(
    result: BuildResult,
    request: BuildRequest,
    returncode: int,
) -> None:
    """Authenticate process status, immutable artifacts, and P3 lineage."""

    _verify_worker_completion(
        result,
        request,
        returncode,
        verified_parent=None,
    )


def worker_command(
    request_path: Path,
    *,
    executable: str | None = None,
    frozen: bool | None = None,
) -> list[str]:
    """Return the development or frozen one-job worker command."""

    actual_executable = executable or sys.executable
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    request = str(Path(request_path).resolve())
    if is_frozen:
        return [actual_executable, "--pageindex-v3-worker", request]
    return [actual_executable, "-m", "app.pageindex_v3_worker", request]


def _create_job_directory(pageindex_dir: Path, job_id: str) -> Path:
    try:
        pageindex_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkerProcessError(f"cannot create PageIndex root: {exc}") from exc
    root = _require_plain_directory(pageindex_dir, "PageIndex root")
    build = root / "build"
    try:
        build.mkdir(exist_ok=True)
    except OSError as exc:
        raise WorkerProcessError(f"cannot create worker build root: {exc}") from exc
    build = _require_plain_directory(build, "worker build root")
    job = build / job_id
    try:
        job.mkdir(exist_ok=False)
    except OSError as exc:
        raise WorkerProcessError(f"cannot create fresh worker job directory: {exc}") from exc
    return _require_plain_directory(job, "worker job directory")


def _write_request(path: Path, request: BuildRequest) -> None:
    payload = encode_request_line(request)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise WorkerProcessError(f"cannot write worker request: {exc}") from exc


def _diagnostic(completed: subprocess.CompletedProcess[str]) -> str:
    for value in (completed.stderr, completed.stdout):
        if isinstance(value, bytes):
            candidate = value.decode("utf-8", errors="replace").strip()
        elif isinstance(value, str):
            candidate = value.strip()
        else:
            candidate = ""
        if candidate:
            return candidate[:2000]
    return ""


def run_build(
    content_dir: Path,
    pageindex_dir: Path,
    mode: BuildMode,
    *,
    parent: ParentAttestation | None = None,
    legacy_export: LegacyExportMode = "none",
) -> BuildResult:
    """Run exactly one new job in exactly one fresh worker subprocess."""

    content = Path(content_dir).resolve()
    pageindex = Path(pageindex_dir).resolve()
    job_id = f"p3_{uuid.uuid4().hex}"
    request = BuildRequest(
        protocol=PROTOCOL_NAME,
        protocol_version=PROTOCOL_VERSION,
        job_id=job_id,
        mode=mode,
        content_dir=content,
        pageindex_dir=pageindex,
        parent=parent,
        legacy_export=legacy_export,
    )
    verified_parent = (
        None
        if request.parent is None
        else _verify_pair(request.parent, pageindex)
    )

    job_dir = _create_job_directory(pageindex, job_id)
    request_path = job_dir / "request.json"
    _write_request(request_path, request)
    project_root = Path(__file__).resolve().parents[3]
    command = worker_command(request_path)
    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise WorkerProcessError(f"cannot launch PageIndex v3 worker: {exc}") from exc

    result_path = job_dir / "result.json"
    if not os.path.lexists(result_path):
        diagnostic = _diagnostic(completed)
        raise WorkerProcessError(
            f"worker exited {completed.returncode} without result.json"
            + (f": {diagnostic}" if diagnostic else "")
        )
    payload = _read_stable_bytes(
        result_path,
        "worker result.json",
        max_bytes=MAX_JSON_LINE_BYTES,
    )
    try:
        result = decode_result_line(payload, request=request)
    except ProtocolError as exc:
        raise WorkerProcessError(f"worker result.json is not trustworthy: {exc}") from exc
    _verify_worker_completion(
        result,
        request,
        completed.returncode,
        verified_parent=verified_parent,
    )
    return result


__all__ = [
    "WorkerProcessError",
    "run_build",
    "verify_worker_completion",
    "worker_command",
]
