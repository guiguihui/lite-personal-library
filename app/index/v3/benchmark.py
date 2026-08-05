"""Fresh-process performance evidence for PageIndex v3.

The orchestrator deliberately does not import the PageIndex worker or reader.
Every build and query sample is a new process, stdout/stderr are redirected to
files with an actively enforced size limit, and every P3 parent is supplied as
an explicit authenticated Generation/View pair.  No mutable ``current`` or
``latest`` pointer participates in a benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.index.v2.benchmark import (
    BenchmarkError as V2BenchmarkError,
    SyntheticCorpusSpec,
    generate_synthetic_corpus,
)
from app.index.v2.canonical import canonical_bytes, canonical_hash
from app.index.v2.process_metrics import OsProcessMetrics, ProcessMonitor

from .protocol import (
    MAX_JSON_LINE_BYTES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    BuildRequest,
    BuildResult,
    GenerationAttestation,
    ParentAttestation,
    ViewAttestation,
    decode_result_line,
    encode_request_line,
)
from .supervisor import (
    WorkerProcessError,
    verify_worker_completion,
    worker_command,
)


class BenchmarkError(RuntimeError):
    """The requested evidence run is unsafe, invalid, or untrustworthy."""


REPORT_SCHEMA_VERSION = 1
QUERY_PROTOCOL_VERSION = 1
DEFAULT_QUERIES = (
    "term00000",
    "term00001",
    "synthetic",
    "section",
    "missingtoken",
    "mutationprobe",
)
MAX_ACTIVE_LOG_BYTES = 1024 * 1024
MAX_RETAINED_LOG_BYTES = 64 * 1024
MAX_QUERY_RESULT_BYTES = 4 * 1024 * 1024
MAX_QUERY_COUNT = 128
MAX_QUERY_CHARS = 4096
MAX_WORKING_SET_BYTES = 512 * 1024 * 1024
MAX_CHILD_WALL_SECONDS = 3600
MAX_NOOP_P95_MS = 500.0
MAX_DIRTY_P95_MS = 5000.0
MAX_DIRTY_BYTES_WRITTEN = 16 * 1024 * 1024
MAX_QUERY_REGRESSION = 0.10
_DIRTY_WRITE_FIXED_BYTES = 8 * 1024 * 1024
_DIRTY_WRITE_SEGMENT_MULTIPLIER = 32
_EDITABLE_TOKEN_RE = re.compile(rb"(?:term[0-9]{5}|mut[0-9a-f]{6})")

_HIT_FIELDS = (
    "generation",
    "doc_key",
    "doc_uid",
    "segment_hash",
    "local_id",
    "node_key",
    "score",
    "rrf_score",
)
_NOOP_ZERO_METRICS = (
    "dirty_segment_ms",
    "generation_ms",
    "delta_ms",
    "normal_validation_ms",
    "legacy_export_ms",
    "segments_rebuilt",
    "segments_deleted",
    "segments_loaded",
    "segments_loaded_peak",
    "postings_visited",
    "base_postings_scanned",
    "bytes_written",
    "legacy_compile_runs",
    "legacy_postings_visited",
    "legacy_bytes_written",
    "normal_validation_runs",
)
_LEGACY_ZERO_METRICS = (
    "legacy_export_ms",
    "legacy_compile_runs",
    "legacy_postings_visited",
    "legacy_bytes_written",
)
_REQUIRED_OS_FIELDS = (
    "peak_working_set_bytes",
    "peak_private_bytes_observed",
    "peak_pagefile_usage_bytes",
    "io_read_transfer_bytes",
    "io_write_transfer_bytes",
)


def _nonnegative(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be an integer >= 0")


def _positive(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1")


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=False)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise BenchmarkError(f"cannot write benchmark request {path}: {exc}") from exc


def _read_bounded_plain(path: Path, *, limit: int, field: str) -> bytes:
    def _identity(m):
        # Windows/NTFS: st_ctime_ns is refreshed by open-for-read, so lstat
        # (before open) vs fstat (after open) differ spuriously. Omit ctime on
        # Windows (same rationale as supervisor._identity / worker._stable_identity).
        if os.name == "nt":
            return (m.st_dev, m.st_ino, m.st_size, m.st_mtime_ns)
        return (m.st_dev, m.st_ino, m.st_size, m.st_mtime_ns, m.st_ctime_ns)

    try:
        before = path.lstat()
    except OSError as exc:
        raise BenchmarkError(f"cannot inspect {field}: {exc}") from exc
    if not path.is_file() or path.is_symlink():
        raise BenchmarkError(f"{field} is not a plain file")
    reparse = getattr(before, "st_file_attributes", 0) & getattr(
        __import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    if reparse:
        raise BenchmarkError(f"{field} must not be a reparse point")
    if before.st_size > limit:
        raise BenchmarkError(f"{field} exceeds {limit} bytes")
    try:
        with path.open("rb") as stream:
            payload = stream.read(limit + 1)
            opened = os.fstat(stream.fileno())
    except OSError as exc:
        raise BenchmarkError(f"cannot read {field}: {exc}") from exc
    try:
        after = path.lstat()
    except OSError as exc:
        raise BenchmarkError(f"cannot re-inspect {field}: {exc}") from exc
    identity_before = _identity(before)
    identity_opened = _identity(opened)
    identity_after = _identity(after)
    if (
        identity_before != identity_opened
        or identity_opened != identity_after
        or len(payload) != before.st_size
    ):
        raise BenchmarkError(f"{field} changed while it was read")
    if len(payload) > limit:
        raise BenchmarkError(f"{field} exceeds {limit} bytes")
    return payload


def _decode_canonical_object(payload: bytes, field: str) -> Mapping[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise BenchmarkError(f"{field} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise BenchmarkError(f"{field} contains non-finite number {value!r}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except BenchmarkError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"{field} is invalid UTF-8 JSON") from exc
    if not isinstance(value, Mapping) or canonical_bytes(value) != payload:
        raise BenchmarkError(f"{field} must be one canonical JSON object")
    return value

def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@dataclass(slots=True)
class _MutationState:
    """Deterministic O(files metadata), O(1 content) synthetic mutations."""

    root: Path
    spec: SyntheticCorpusSpec
    active_hashes: dict[str, str]
    identities: dict[str, tuple[int, int, int, int, int]]
    protected_paths: set[str]

    @classmethod
    def capture(
        cls, content_dir: Path, spec: SyntheticCorpusSpec
    ) -> "_MutationState":
        root = Path(content_dir).resolve()
        expected = {
            f"notes/synthetic-{index:05d}.md"
            for index in range(spec.documents)
        }
        state = cls(root, spec, {}, {}, set())
        state._verify_marker()
        if state._markdown_paths() != expected:
            raise BenchmarkError("synthetic corpus file set differs from its spec")
        for relative in sorted(expected):
            target = state._owned_file(relative)
            payload = target.read_bytes()
            state.active_hashes[relative] = hashlib.sha256(payload).hexdigest()
            state.identities[relative] = _file_identity(target)
        state.verify_catalog()
        return state

    def _verify_marker(self) -> None:
        marker = self.root / ".pageindex-v2-benchmark-synthetic.json"
        payload = _read_bounded_plain(
            marker, limit=64 * 1024, field="synthetic corpus marker"
        )
        actual = _decode_canonical_object(payload, "synthetic corpus marker")
        expected = {
            "schema_version": 2,
            "generator": "pageindex-v2-synthetic-v2",
            "spec": self.spec.as_dict(),
        }
        if actual != expected:
            raise BenchmarkError("synthetic corpus marker changed during benchmark")

    def _markdown_paths(self) -> set[str]:
        return {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*.md")
            if path.is_file()
        }

    def _owned_file(self, relative: str) -> Path:
        target = self.root / Path(relative)
        try:
            metadata = target.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if target.is_symlink() or attributes & reparse or not target.is_file():
                raise BenchmarkError(
                    f"synthetic mutation target is not a plain file: {relative}"
                )
            target.resolve(strict=True).relative_to(self.root)
        except BenchmarkError:
            raise
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise BenchmarkError(
                f"synthetic mutation target is unsafe: {relative}"
            ) from exc
        return target

    def verify_catalog(self) -> None:
        """Verify marker, file set, and metadata without pre-reading all content."""

        self._verify_marker()
        if self._markdown_paths() != set(self.active_hashes):
            raise BenchmarkError("synthetic corpus file set changed during benchmark")
        for relative, identity in self.identities.items():
            if _file_identity(self._owned_file(relative)) != identity:
                raise BenchmarkError(
                    f"synthetic corpus file metadata changed externally: {relative}"
                )

    def _verified_payload(self, relative: str) -> tuple[Path, bytes]:
        target = self._owned_file(relative)
        payload = target.read_bytes()
        if hashlib.sha256(payload).hexdigest() != self.active_hashes[relative]:
            raise BenchmarkError(
                f"synthetic corpus target content changed externally: {relative}"
            )
        return target, payload

    def edit_one(self, ordinal: int) -> dict[str, object]:
        self.verify_catalog()
        paths = sorted(self.active_hashes)
        if not paths:
            raise BenchmarkError("cannot edit an empty synthetic corpus")
        relative = paths[(ordinal - 1) % len(paths)]
        target, payload = self._verified_payload(relative)
        match = _EDITABLE_TOKEN_RE.search(payload)
        if match is None:
            raise BenchmarkError(
                f"synthetic document has no editable token: {relative}"
            )
        replacement = b"mutationprobe"
        mutated = payload[: match.start()] + replacement + payload[match.end() :]
        before_hash = self.active_hashes[relative]
        after_hash = hashlib.sha256(mutated).hexdigest()
        _write_bytes_atomic(target, mutated)
        self.active_hashes[relative] = after_hash
        self.identities[relative] = _file_identity(target)
        self.protected_paths.add(relative)
        self.verify_catalog()
        return {
            "kind": "one_document_edit",
            "ordinal": ordinal,
            "relative_path": relative,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "expected_chunk_delta": 0,
        }

    def delete_one(self, ordinal: int) -> dict[str, object]:
        self.verify_catalog()
        paths = sorted(self.active_hashes)
        if not paths:
            raise BenchmarkError("cannot delete from an empty synthetic corpus")
        candidates = [path for path in reversed(paths) if path not in self.protected_paths]
        if not candidates:
            raise BenchmarkError("delete would remove every mutation probe document")
        relative = candidates[0]
        target, _payload = self._verified_payload(relative)
        before_hash = self.active_hashes[relative]
        target.unlink()
        del self.active_hashes[relative]
        del self.identities[relative]
        self.verify_catalog()
        return {
            "kind": "one_document_delete",
            "ordinal": ordinal,
            "relative_path": relative,
            "before_sha256": before_hash,
            "after_sha256": None,
            "expected_chunk_delta": -self.spec.sections_per_document,
        }

def _log_tail(path: Path, limit: int = 4096) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(max(0, size - limit))
            payload = stream.read(limit)
    except OSError:
        return ""
    return payload.decode("utf-8", errors="replace").strip()


def _trim_log(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= MAX_RETAINED_LOG_BYTES:
        return
    with path.open("rb") as stream:
        stream.seek(size - MAX_RETAINED_LOG_BYTES)
        tail = stream.read(MAX_RETAINED_LOG_BYTES)
    _write_bytes_atomic(path, tail)


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_measured_process(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    sample_interval_ms: int,
) -> tuple[int, int, float, OsProcessMetrics]:
    """Run one process without PIPEs and actively cap both log files."""

    process: subprocess.Popen[Any] | None = None
    monitor: ProcessMonitor | None = None
    metrics: OsProcessMetrics | None = None
    returncode: int | None = None
    started = time.perf_counter()
    try:
        with stdout_path.open("wb") as stdout_stream, stderr_path.open(
            "wb"
        ) as stderr_stream:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdout=stdout_stream,
                stderr=stderr_stream,
                shell=False,
            )
            try:
                monitor = ProcessMonitor.attach(
                    process.pid, sample_interval_ms=sample_interval_ms
                )
                while True:
                    monitor.sample()
                    for log_path in (stdout_path, stderr_path):
                        if log_path.stat().st_size > MAX_ACTIVE_LOG_BYTES:
                            _terminate(process)
                            raise BenchmarkError(
                                f"child log exceeded {MAX_ACTIVE_LOG_BYTES} bytes: "
                                f"{log_path.name}"
                            )
                    if process.poll() is not None:
                        break
                    if time.perf_counter() - started > MAX_CHILD_WALL_SECONDS:
                        _terminate(process)
                        raise BenchmarkError(
                            f"child exceeded {MAX_CHILD_WALL_SECONDS} seconds"
                        )
                    time.sleep(sample_interval_ms / 1000)
                returncode = process.wait()
            except BaseException:
                _terminate(process)
                raise
    finally:
        wall_ms = round((time.perf_counter() - started) * 1000, 3)
        if monitor is not None:
            metrics = monitor.finish()
            monitor.close()
        for log_path in (stdout_path, stderr_path):
            _trim_log(log_path)
    if process is None or metrics is None or returncode is None:
        raise BenchmarkError("child process did not start")
    return process.pid, returncode, wall_ms, metrics


def _require_os_metrics(metrics: OsProcessMetrics, field: str) -> None:
    missing = [
        name for name in _REQUIRED_OS_FIELDS if getattr(metrics, name) is None
    ]
    if metrics.status != "measured" or metrics.samples < 1 or missing:
        details = ", ".join((*missing, *metrics.warnings))
        raise BenchmarkError(
            f"{field} requires complete OS process metrics"
            + (f": {details}" if details else "")
        )


def _parent(result: BuildResult) -> ParentAttestation:
    if result.generation is None or result.view is None:
        raise BenchmarkError("successful build did not return a complete parent pair")
    return ParentAttestation(result.generation, result.view)


def _changed_document_posting_bound(
    pageindex_dir: Path,
    before: ParentAttestation,
    after: ParentAttestation,
) -> dict[str, object]:
    # Read the authenticated O(changes) Delta control plane. Do not decode
    # either O(corpus) Generation manifest in the long-lived orchestrator.
    from app.index.v3.delta_store import load_delta_object_metadata
    from app.index.v3.view_store import load_search_view_metadata

    view = load_search_view_metadata(pageindex_dir, after.view.view_id)
    if not view.delta_ids:
        raise BenchmarkError("edit View does not contain a Delta")
    delta = load_delta_object_metadata(pageindex_dir, view.delta_ids[-1])
    if (
        delta.parent_view_id != before.view.view_id
        or delta.generation != after.generation.generation
        or delta.generation_manifest_sha256 != after.generation.manifest_sha256
        or len(delta.replacements) != 1
    ):
        raise BenchmarkError(
            "one-document edit Delta is not bound to exactly one replacement"
        )
    replacement = delta.replacements[0]
    if (
        replacement.old_segment_hash is None
        or replacement.new_segment_hash is None
    ):
        raise BenchmarkError("one-document edit replacement is not old-to-new")
    digest = replacement.new_segment_hash
    path = pageindex_dir / "objects" / "segments" / digest[:2] / f"{digest}.json"
    payload = _read_bounded_plain(
        path, limit=64 * 1024 * 1024, field="changed Segment"
    )
    if hashlib.sha256(payload).hexdigest() != digest:
        raise BenchmarkError("changed Segment hash mismatch")
    try:
        segment = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError("changed Segment is invalid JSON") from exc
    if canonical_bytes(segment) != payload or not isinstance(segment, Mapping):
        raise BenchmarkError("changed Segment is not canonical")
    postings = segment.get("postings")
    if not isinstance(postings, Mapping):
        raise BenchmarkError("changed Segment postings are invalid")
    logical = 0
    physical_upper = 0
    for rows in postings.values():
        if not isinstance(rows, list):
            raise BenchmarkError("changed Segment posting rows are invalid")
        logical += len(rows)
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) != 4
                or any(isinstance(item, bool) or not isinstance(item, int) for item in row)
            ):
                raise BenchmarkError("changed Segment posting row is invalid")
            physical_upper += sum(1 for tf in row[1:] if tf > 0)
    # Dirty build and Normal validation each visit logical projections; the
    # validator audits and streams each emitted field posting at most twice.
    bound = 2 * logical + 2 * physical_upper
    write_bound = min(
        MAX_DIRTY_BYTES_WRITTEN,
        _DIRTY_WRITE_FIXED_BYTES
        + _DIRTY_WRITE_SEGMENT_MULTIPLIER * len(payload),
    )
    return {
        "changed_documents": 1,
        "doc_key": replacement.doc_key,
        "changed_segment_bytes": len(payload),
        "logical_postings": logical,
        "field_postings_upper_bound": physical_upper,
        "postings_visited_bound": bound,
        "bytes_written_bound": write_bound,
    }

def _validate_mechanism(
    scenario: str,
    result: BuildResult,
    *,
    previous: BuildResult | None,
    posting_bound: Mapping[str, object] | None,
    expected_documents: int,
) -> None:
    metrics = result.metrics.as_dict()
    for name in _LEGACY_ZERO_METRICS:
        if metrics[name] != 0:
            raise BenchmarkError(f"{scenario} unexpectedly reported {name}")
    if metrics["base_postings_scanned"] != 0:
        raise BenchmarkError(f"{scenario} scanned parent Base postings")

    if scenario == "noop":
        if result.state != "no_op":
            raise BenchmarkError(f"no-op round returned state={result.state!r}")
        for name in _NOOP_ZERO_METRICS:
            if metrics[name] != 0:
                raise BenchmarkError(f"no-op round reported non-zero {name}")
        if previous is None or _parent(result) != _parent(previous):
            raise BenchmarkError("no-op round did not preserve its explicit parent")
        return

    if result.state != "ready_to_publish":
        raise BenchmarkError(f"{scenario} round returned state={result.state!r}")
    if metrics["normal_validation_runs"] != 1:
        raise BenchmarkError(f"{scenario} did not run one Normal validation")
    if metrics["segments_loaded_peak"] > 1:
        raise BenchmarkError(f"{scenario} retained more than one Segment at once")

    if scenario == "bootstrap":
        if previous is not None or result.parent is not None:
            raise BenchmarkError("bootstrap must not resolve or carry a parent")
        if metrics["segments_rebuilt"] != expected_documents:
            raise BenchmarkError("bootstrap did not rebuild the complete corpus")
        if metrics["segments_loaded"] != expected_documents:
            raise BenchmarkError("bootstrap Segment load count is inconsistent")
    elif scenario == "edit":
        if previous is None or result.parent != _parent(previous):
            raise BenchmarkError("edit did not use the explicit preceding parent")
        if metrics["segments_rebuilt"] != 1 or metrics["segments_deleted"] != 0:
            raise BenchmarkError("edit did not rebuild exactly one document")
        if metrics["segments_loaded"] > 2:
            raise BenchmarkError("edit loaded more than old+new changed Segments")
        if posting_bound is None:
            raise BenchmarkError("edit has no changed-document posting bound")
        visited_bound = posting_bound["postings_visited_bound"]
        if isinstance(visited_bound, bool) or not isinstance(visited_bound, int):
            raise BenchmarkError("edit posting bound is invalid")
        if metrics["postings_visited"] > visited_bound:
            raise BenchmarkError(
                "edit postings_visited exceeds the changed-document bound"
            )
        write_bound = posting_bound["bytes_written_bound"]
        if isinstance(write_bound, bool) or not isinstance(write_bound, int):
            raise BenchmarkError("edit byte-write bound is invalid")
        if metrics["bytes_written"] >= write_bound:
            raise BenchmarkError(
                "edit bytes_written exceeds the changed-document bound"
            )
    elif scenario == "delete":
        if previous is None or result.parent != _parent(previous):
            raise BenchmarkError("delete did not use the explicit preceding parent")
        if metrics["segments_rebuilt"] != 0 or metrics["segments_deleted"] != 1:
            raise BenchmarkError("delete did not remove exactly one document")
        if (
            metrics["segments_loaded"] != 0
            or metrics["segments_loaded_peak"] != 0
            or metrics["postings_visited"] != 0
        ):
            raise BenchmarkError("delete performed Segment/posting work")
        if metrics["bytes_written"] >= _DIRTY_WRITE_FIXED_BYTES:
            raise BenchmarkError(
                "delete bytes_written exceeds the bounded control-plane allowance"
            )
    elif scenario == "optimize":
        if previous is None or result.parent != _parent(previous):
            raise BenchmarkError("optimize did not use the explicit preceding parent")
        if result.generation != previous.generation or result.view == previous.view:
            raise BenchmarkError("optimize did not preserve Generation and replace View")
        if metrics["segments_rebuilt"] or metrics["segments_deleted"]:
            raise BenchmarkError("optimize rebuilt source Segments")
        if metrics["segments_loaded"] != expected_documents:
            raise BenchmarkError("optimize did not stream each live Segment exactly once")
    else:
        raise ValueError(f"unknown benchmark scenario: {scenario}")


def _run_build_round(
    content_dir: Path,
    pageindex_dir: Path,
    *,
    scenario: str,
    scenario_ordinal: int,
    ordinal: int,
    mode: str,
    parent: ParentAttestation | None,
    mutation: Mapping[str, object],
    sample_interval_ms: int,
    require_os_metrics: bool,
) -> tuple[dict[str, object], BuildResult]:
    job_id = f"p3_bench_{uuid.uuid4().hex}"
    request = BuildRequest(
        protocol=PROTOCOL_NAME,
        protocol_version=PROTOCOL_VERSION,
        job_id=job_id,
        mode=mode,  # type: ignore[arg-type]
        content_dir=content_dir,
        pageindex_dir=pageindex_dir,
        parent=parent,
        legacy_export="none",
    )
    job_dir = pageindex_dir / "build" / job_id
    request_path = job_dir / "request.json"
    _write_exclusive(request_path, encode_request_line(request))
    stdout_path = job_dir / "worker.stdout.log"
    stderr_path = job_dir / "worker.stderr.log"
    project_root = Path(__file__).resolve().parents[3]
    end_to_end_started = time.perf_counter()
    pid, returncode, process_wall_ms, process_metrics = _run_measured_process(
        worker_command(request_path),
        cwd=project_root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        sample_interval_ms=sample_interval_ms,
    )
    if require_os_metrics:
        _require_os_metrics(process_metrics, f"{scenario} round {scenario_ordinal}")
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        diagnostic = _log_tail(stderr_path) or _log_tail(stdout_path)
        raise BenchmarkError(
            f"{scenario} worker exited {returncode} without result.json"
            + (f": {diagnostic}" if diagnostic else "")
        )
    try:
        result = decode_result_line(
            _read_bounded_plain(
                result_path,
                limit=MAX_JSON_LINE_BYTES,
                field="worker result.json",
            ),
            request=request,
        )
        verify_worker_completion(result, request, returncode)
    except (ValueError, WorkerProcessError) as exc:
        raise BenchmarkError(
            f"{scenario} worker returned an untrusted terminal result: {exc}"
        ) from exc
    end_to_end_ms = round((time.perf_counter() - end_to_end_started) * 1000, 3)
    if result.state not in {"no_op", "ready_to_publish"}:
        message = result.error.message if result.error is not None else result.state
        raise BenchmarkError(f"{scenario} worker failed: {message}")
    round_result = {
        "ordinal": ordinal,
        "scenario": scenario,
        "scenario_ordinal": scenario_ordinal,
        "mode": mode,
        "mutation": dict(mutation),
        "job_id": job_id,
        "worker_pid": pid,
        "worker_exit_code": returncode,
        "wall_time_ms": end_to_end_ms,
        "worker_process_ms": process_wall_ms,
        "strict_verification_ms": round(
            max(0.0, end_to_end_ms - process_wall_ms), 3
        ),
        "state": result.state,
        "parent_generation": (
            None if parent is None else parent.generation.generation
        ),
        "parent_view_id": None if parent is None else parent.view.view_id,
        "generation": result.generation.generation if result.generation else None,
        "view_id": result.view.view_id if result.view else None,
        "metrics": result.metrics.as_dict(),
        "process_metrics": process_metrics.as_dict(),
        "logs": {
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "overflow_failure_threshold_bytes": MAX_ACTIVE_LOG_BYTES,
            "hard_retained_bytes": MAX_RETAINED_LOG_BYTES,
        },
    }
    return round_result, result


def _query_worker_command(request: Path, result: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "app.index.v3.benchmark",
        "--query-worker",
        str(request.resolve()),
        str(result.resolve()),
    ]


def _artifact_ref(value: object, expected_path: str):
    from app.index.v2.artifacts import ArtifactRef

    if not isinstance(value, Mapping) or set(value) != {
        "relative_path",
        "sha256",
        "byte_size",
        "records",
    }:
        raise ValueError(f"invalid artifact reference for {expected_path}")
    ref = ArtifactRef(
        relative_path=value["relative_path"],  # type: ignore[arg-type]
        sha256=value["sha256"],  # type: ignore[arg-type]
        byte_size=value["byte_size"],  # type: ignore[arg-type]
        records=value["records"],  # type: ignore[arg-type]
    )
    if ref.relative_path != expected_path:
        raise ValueError(f"artifact path must be {expected_path}")
    return ref


def _open_query_generation(attestation: GenerationAttestation):
    """Rebuild a compact receipt from authenticated immutable artifacts."""

    from app.index.v2.artifacts import ArtifactRef
    from app.index.v3.generation import LogicalGenerationReceipt

    root = attestation.generation_dir
    payload = _read_bounded_plain(
        root / "manifest.json",
        limit=64 * 1024 * 1024,
        field="query Generation manifest",
    )
    if hashlib.sha256(payload).hexdigest() != attestation.manifest_sha256:
        raise ValueError("query Generation manifest attestation mismatch")
    manifest = json.loads(payload.decode("utf-8"))
    if canonical_bytes(manifest) != payload or not isinstance(manifest, Mapping):
        raise ValueError("query Generation manifest is not canonical")
    count = manifest.get("document_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("query Generation document_count is invalid")
    proof = _artifact_ref(manifest.get("input_proof"), "input-proof.json")
    proof_payload = _read_bounded_plain(
        root / proof.relative_path,
        limit=64 * 1024 * 1024,
        field="query Generation input proof",
    )
    if (
        hashlib.sha256(proof_payload).hexdigest() != proof.sha256
        or len(proof_payload) != proof.byte_size
        or proof.records != count
    ):
        raise ValueError("query Generation input proof attestation mismatch")
    return LogicalGenerationReceipt(
        candidate_dir=root,
        generation_id=attestation.generation,
        generation_recipe_hash=manifest["generation_recipe_hash"],
        manifest_ref=ArtifactRef(
            "manifest.json", attestation.manifest_sha256, len(payload), count
        ),
        input_proof_ref=proof,
        document_count=count,
    )


def _authenticate_query_view(attestation: ViewAttestation) -> None:
    payload = _read_bounded_plain(
        attestation.view_dir / "manifest.json",
        limit=MAX_JSON_LINE_BYTES,
        field="query View manifest",
    )
    if hashlib.sha256(payload).hexdigest() != attestation.manifest_sha256:
        raise BenchmarkError("query View manifest attestation mismatch")
    manifest = _decode_canonical_object(payload, "query View manifest")
    if (
        manifest.get("view_id") != attestation.view_id
        or manifest.get("generation") != attestation.generation
        or manifest.get("generation_manifest_sha256")
        != attestation.generation_manifest_sha256
    ):
        raise BenchmarkError("query View manifest control binding mismatch")

def _stable_hit(hit: object) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in _HIT_FIELDS:
        value = getattr(hit, name)
        if name == "local_id" and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError("query hit local_id is invalid")
        if name not in {"local_id", "score", "rrf_score"} and not isinstance(
            value, str
        ):
            raise ValueError(f"query hit {name} is invalid")
        result[name] = value
    return result


def _query_worker(request_path: Path, result_path: Path) -> int:
    """Execute real pinned queries in a short-lived, independently measured process."""

    started_ns = time.perf_counter_ns()
    try:
        request_payload = _read_bounded_plain(
            Path(request_path),
            limit=MAX_JSON_LINE_BYTES,
            field="query request",
        )
        request = _decode_canonical_object(request_payload, "query request")
        expected = {
            "schema_version",
            "job_id",
            "pageindex_dir",
            "generation",
            "view",
            "queries",
            "top_k",
            "orchestrator_started_ns",
        }
        if set(request) != expected or request["schema_version"] != QUERY_PROTOCOL_VERSION:
            raise ValueError("query request schema mismatch")
        job_id = request["job_id"]
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("query job_id is invalid")
        pageindex = Path(request["pageindex_dir"]).resolve()  # type: ignore[arg-type]
        generation = GenerationAttestation.from_dict(
            request["generation"], pageindex_dir=pageindex
        )
        view = ViewAttestation.from_dict(request["view"], pageindex_dir=pageindex)
        if (
            view.generation != generation.generation
            or view.generation_manifest_sha256 != generation.manifest_sha256
        ):
            raise ValueError("query Generation/View pair is inconsistent")
        _authenticate_query_view(view)
        queries = request["queries"]
        if (
            not isinstance(queries, list)
            or not queries
            or len(queries) > MAX_QUERY_COUNT
            or not all(
                isinstance(item, str) and 0 < len(item) <= MAX_QUERY_CHARS
                for item in queries
            )
            or len(set(queries)) != len(queries)
        ):
            raise ValueError("queries must be unique bounded non-empty strings")
        top_k = request["top_k"]
        if (
            isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or top_k < 1
            or top_k > 1000
        ):
            raise ValueError("top_k must be an integer >= 1")
        orchestrator_started_ns = request["orchestrator_started_ns"]
        if (
            isinstance(orchestrator_started_ns, bool)
            or not isinstance(orchestrator_started_ns, int)
            or orchestrator_started_ns < 1
        ):
            raise ValueError("orchestrator_started_ns is invalid")

        # Heavy reader/retrieval imports exist only in this dedicated process.
        from app.index.v3.models import ViewPin
        from app.index.v3.reader import PinnedSearchView
        from app.retrieval.search_view import search_pinned_view

        generation_receipt = _open_query_generation(generation)
        opened_at = time.perf_counter_ns()
        reader = PinnedSearchView.open(
            pageindex,
            ViewPin(generation.generation, view.view_id),
            generation_receipt,
        )
        open_finished = time.perf_counter_ns()
        observations: list[dict[str, object]] = []
        try:
            for query in queries:
                query_started = time.perf_counter_ns()
                hits = search_pinned_view(query, reader, top_k=top_k)
                query_finished = time.perf_counter_ns()
                observations.append(
                    {
                        "query": query,
                        "query_ms": round(
                            (query_finished - query_started) / 1_000_000, 6
                        ),
                        "hits": [_stable_hit(hit) for hit in hits],
                    }
                )
        finally:
            reader.close()
        finished_ns = time.perf_counter_ns()
        result = {
            "schema_version": QUERY_PROTOCOL_VERSION,
            "job_id": job_id,
            "state": "complete",
            "generation": generation.generation,
            "view_id": view.view_id,
            "startup_ms": round(
                max(0, started_ns - orchestrator_started_ns) / 1_000_000, 3
            ),
            "open_ms": round((open_finished - opened_at) / 1_000_000, 3),
            "worker_wall_ms": round((finished_ns - started_ns) / 1_000_000, 3),
            "queries": observations,
            "error": None,
        }
        _write_bytes_atomic(result_path, canonical_bytes(result))
        return 0
    except BaseException as exc:
        failure = {
            "schema_version": QUERY_PROTOCOL_VERSION,
            "job_id": "unknown",
            "state": "failed",
            "generation": None,
            "view_id": None,
            "startup_ms": 0.0,
            "open_ms": 0.0,
            "worker_wall_ms": round(
                (time.perf_counter_ns() - started_ns) / 1_000_000, 3
            ),
            "queries": [],
            "error": f"{type(exc).__name__}: {exc}"[:4000],
        }
        try:
            _write_bytes_atomic(result_path, canonical_bytes(failure))
        except BaseException:
            pass
        return 1


def _validate_query_result(
    value: object,
    *,
    job_id: str,
    pair: ParentAttestation,
    queries: tuple[str, ...],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise BenchmarkError("query result must be an object")
    expected = {
        "schema_version",
        "job_id",
        "state",
        "generation",
        "view_id",
        "startup_ms",
        "open_ms",
        "worker_wall_ms",
        "queries",
        "error",
    }
    if set(value) != expected or value.get("schema_version") != QUERY_PROTOCOL_VERSION:
        raise BenchmarkError("query result schema mismatch")
    if value.get("job_id") != job_id:
        raise BenchmarkError("query result job_id mismatch")
    if value.get("state") != "complete" or value.get("error") is not None:
        raise BenchmarkError(f"query worker failed: {value.get('error')}")
    if (
        value.get("generation") != pair.generation.generation
        or value.get("view_id") != pair.view.view_id
    ):
        raise BenchmarkError("query result pair mismatch")
    for name in ("startup_ms", "open_ms", "worker_wall_ms"):
        elapsed = value[name]
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(float(elapsed))
            or elapsed < 0
        ):
            raise BenchmarkError(f"query result {name} is invalid")

    observations = value.get("queries")
    if not isinstance(observations, list) or [
        item.get("query") if isinstance(item, Mapping) else None
        for item in observations
    ] != list(queries):
        raise BenchmarkError("query result order mismatch")
    string_fields = (
        "generation",
        "doc_key",
        "doc_uid",
        "segment_hash",
        "node_key",
    )
    for item in observations:
        assert isinstance(item, Mapping)
        if set(item) != {"query", "query_ms", "hits"}:
            raise BenchmarkError("query observation schema mismatch")
        query_ms = item["query_ms"]
        if (
            isinstance(query_ms, bool)
            or not isinstance(query_ms, (int, float))
            or not math.isfinite(float(query_ms))
            or query_ms < 0
        ):
            raise BenchmarkError("query duration is invalid")
        hits = item["hits"]
        if not isinstance(hits, list):
            raise BenchmarkError("query hits must be a list")
        for hit in hits:
            if not isinstance(hit, Mapping) or set(hit) != set(_HIT_FIELDS):
                raise BenchmarkError("query hit stable-field schema mismatch")
            if hit["generation"] != pair.generation.generation or not all(
                isinstance(hit[name], str) and hit[name]
                for name in string_fields
            ):
                raise BenchmarkError("query hit identity is invalid")
            local_id = hit["local_id"]
            if (
                isinstance(local_id, bool)
                or not isinstance(local_id, int)
                or local_id < 0
            ):
                raise BenchmarkError("query hit local_id is invalid")
            score = hit["score"]
            rrf_score = hit["rrf_score"]
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
                or (
                    rrf_score is not None
                    and (
                        isinstance(rrf_score, bool)
                        or not isinstance(rrf_score, (int, float))
                        or not math.isfinite(float(rrf_score))
                    )
                )
            ):
                raise BenchmarkError("query hit score is invalid")
    return dict(value)

def _run_query_sample(
    pageindex_dir: Path,
    pair: ParentAttestation,
    *,
    kind: str,
    pair_ordinal: int,
    ordinal: int,
    queries: tuple[str, ...],
    top_k: int,
    sample_interval_ms: int,
    require_os_metrics: bool,
) -> tuple[dict[str, object], dict[str, list[dict[str, object]]]]:
    job_id = f"p3_query_{uuid.uuid4().hex}"
    job_dir = pageindex_dir / "build" / job_id
    request_path = job_dir / "query-request.json"
    result_path = job_dir / "query-result.json"
    orchestrator_started_ns = time.perf_counter_ns()
    request = {
        "schema_version": QUERY_PROTOCOL_VERSION,
        "job_id": job_id,
        "pageindex_dir": str(pageindex_dir),
        "generation": pair.generation.as_dict(),
        "view": pair.view.as_dict(),
        "queries": list(queries),
        "top_k": top_k,
        "orchestrator_started_ns": orchestrator_started_ns,
    }
    _write_exclusive(request_path, canonical_bytes(request))
    stdout_path = job_dir / "query.stdout.log"
    stderr_path = job_dir / "query.stderr.log"
    project_root = Path(__file__).resolve().parents[3]
    pid, returncode, wall_ms, process_metrics = _run_measured_process(
        _query_worker_command(request_path, result_path),
        cwd=project_root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        sample_interval_ms=sample_interval_ms,
    )
    if require_os_metrics:
        _require_os_metrics(process_metrics, f"{kind} query {pair_ordinal}")
    if returncode != 0 or not result_path.is_file():
        diagnostic = _log_tail(stderr_path) or _log_tail(stdout_path)
        raise BenchmarkError(
            f"{kind} query worker exited {returncode}"
            + (f": {diagnostic}" if diagnostic else "")
        )
    payload = _read_bounded_plain(
        result_path, limit=MAX_QUERY_RESULT_BYTES, field="query result"
    )
    parsed = _decode_canonical_object(payload, "query result")

    result = _validate_query_result(
        parsed, job_id=job_id, pair=pair, queries=queries
    )
    by_query: dict[str, list[dict[str, object]]] = {}
    durations: dict[str, float] = {}
    for observation in result["queries"]:  # type: ignore[assignment]
        assert isinstance(observation, Mapping)
        query = str(observation["query"])
        hits = observation["hits"]
        assert isinstance(hits, list)
        by_query[query] = [dict(hit) for hit in hits]
        durations[query] = float(observation["query_ms"])
    sample = {
        "ordinal": ordinal,
        "pair_ordinal": pair_ordinal,
        "kind": kind,
        "job_id": job_id,
        "worker_pid": pid,
        "worker_exit_code": returncode,
        "generation": pair.generation.generation,
        "view_id": pair.view.view_id,
        "wall_time_ms": wall_ms,
        "startup_ms": result["startup_ms"],
        "open_ms": result["open_ms"],
        "worker_wall_ms": result["worker_wall_ms"],
        "query_ms": durations,
        "result_sha256": canonical_hash(by_query),
        "result_counts": {
            query: len(hits) for query, hits in sorted(by_query.items())
        },
        "process_metrics": process_metrics.as_dict(),
        "logs": {
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "overflow_failure_threshold_bytes": MAX_ACTIVE_LOG_BYTES,
            "hard_retained_bytes": MAX_RETAINED_LOG_BYTES,
        },
    }
    return sample, by_query


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _distribution(
    values: Sequence[float], *, precision: int = 3
) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": round(min(values), precision),
        "median": round(float(statistics.median(values)), precision),
        "p95": round(_nearest_rank(values, 0.95), precision),
        "max": round(max(values), precision),
    }


def _scenario_summary(
    rounds: Sequence[Mapping[str, object]], scenario: str
) -> dict[str, object]:
    selected = [item for item in rounds if item.get("scenario") == scenario]
    if not selected:
        return {"runs": 0}

    def process_values(name: str) -> list[float]:
        result: list[float] = []
        for item in selected:
            metrics = item.get("process_metrics")
            value = metrics.get(name) if isinstance(metrics, Mapping) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result.append(float(value))
        return result

    return {
        "runs": len(selected),
        "states": {
            state: sum(1 for item in selected if item.get("state") == state)
            for state in sorted({str(item.get("state")) for item in selected})
        },
        "wall_time_ms": _distribution(
            [float(item["wall_time_ms"]) for item in selected]
        ),
        "peak_working_set_bytes": _distribution(
            process_values("peak_working_set_bytes")
        ),
        "peak_private_bytes_observed": _distribution(
            process_values("peak_private_bytes_observed")
        ),
        "peak_pagefile_usage_bytes": _distribution(
            process_values("peak_pagefile_usage_bytes")
        ),
        "io_read_transfer_bytes": _distribution(
            process_values("io_read_transfer_bytes")
        ),
        "io_write_transfer_bytes": _distribution(
            process_values("io_write_transfer_bytes")
        ),
    }


def _query_summary(
    samples: Sequence[Mapping[str, object]], queries: tuple[str, ...]
) -> dict[str, object]:
    result: dict[str, object] = {}
    for kind in ("incremental", "clean"):
        selected = [sample for sample in samples if sample.get("kind") == kind]

        def process_values(name: str) -> list[float]:
            values: list[float] = []
            for item in selected:
                metrics = item.get("process_metrics")
                value = metrics.get(name) if isinstance(metrics, Mapping) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values.append(float(value))
            return values

        result[kind] = {
            "runs": len(selected),
            "wall_time_ms": _distribution(
                [float(item["wall_time_ms"]) for item in selected]
            ),
            "startup_ms": _distribution(
                [float(item["startup_ms"]) for item in selected]
            ),
            "open_ms": _distribution(
                [float(item["open_ms"]) for item in selected]
            ),
            "peak_working_set_bytes": _distribution(
                process_values("peak_working_set_bytes")
            ),
            "peak_private_bytes_observed": _distribution(
                process_values("peak_private_bytes_observed")
            ),
            "peak_pagefile_usage_bytes": _distribution(
                process_values("peak_pagefile_usage_bytes")
            ),
            "io_read_transfer_bytes": _distribution(
                process_values("io_read_transfer_bytes")
            ),
            "io_write_transfer_bytes": _distribution(
                process_values("io_write_transfer_bytes")
            ),
            "queries": {
                query: _distribution(
                    [
                        float(item["query_ms"][query])
                        for item in selected
                    ],  # type: ignore[index]
                    precision=6,
                )
                for query in queries
            },
        }
    return result

def _artifact_storage(pageindex_dir: Path) -> dict[str, dict[str, object]]:
    roots = {
        "segments": pageindex_dir / "objects" / "segments",
        "summaries": pageindex_dir / "objects" / "search" / "summaries",
        "bases": pageindex_dir / "objects" / "search" / "bases",
        "deltas": pageindex_dir / "objects" / "search" / "deltas",
        "generations": pageindex_dir / "generations",
        "views": pageindex_dir / "views",
    }
    result: dict[str, dict[str, object]] = {}
    directory_kinds = {"bases", "deltas", "generations", "views"}
    for name, root in roots.items():
        file_count = 0
        total_bytes = 0
        object_bytes: list[float] = []
        if root.is_dir() and name in directory_kinds:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                child_bytes = 0
                for path in child.rglob("*"):
                    if path.is_file():
                        size = path.stat().st_size
                        file_count += 1
                        total_bytes += size
                        child_bytes += size
                object_bytes.append(float(child_bytes))
        elif root.is_dir():
            for path in root.rglob("*"):
                if path.is_file():
                    size = path.stat().st_size
                    file_count += 1
                    total_bytes += size
                    object_bytes.append(float(size))
        result[name] = {
            "files": file_count,
            "bytes": total_bytes,
            "objects": len(object_bytes),
            "bytes_per_object": _distribution(object_bytes),
        }
    return result

def _validate_final_source_proof(
    pair: ParentAttestation,
    active_hashes: Mapping[str, str],
) -> dict[str, object]:
    """Independently bind the final Generation proof to every live source file."""

    from app.index.v2.ids import make_doc_key
    from app.index.v2.input_proof import validate_input_proof

    root = pair.generation.generation_dir
    manifest_payload = _read_bounded_plain(
        root / "manifest.json",
        limit=64 * 1024 * 1024,
        field="final Generation manifest",
    )
    if (
        hashlib.sha256(manifest_payload).hexdigest()
        != pair.generation.manifest_sha256
    ):
        raise BenchmarkError("final Generation manifest attestation mismatch")
    manifest = _decode_canonical_object(
        manifest_payload,
        "final Generation manifest",
    )
    if manifest.get("generation") != pair.generation.generation:
        raise BenchmarkError("final Generation manifest identity mismatch")
    try:
        proof_ref = _artifact_ref(
            manifest.get("input_proof"),
            "input-proof.json",
        )
    except (TypeError, ValueError) as exc:
        raise BenchmarkError(f"final input proof receipt is invalid: {exc}") from exc
    proof_payload = _read_bounded_plain(
        root / proof_ref.relative_path,
        limit=64 * 1024 * 1024,
        field="final Generation input proof",
    )
    if (
        hashlib.sha256(proof_payload).hexdigest() != proof_ref.sha256
        or len(proof_payload) != proof_ref.byte_size
        or proof_ref.records != len(active_hashes)
    ):
        raise BenchmarkError("final Generation input proof receipt mismatch")
    proof_value = _decode_canonical_object(
        proof_payload,
        "final Generation input proof",
    )
    try:
        proof = validate_input_proof(proof_value)
    except (TypeError, ValueError) as exc:
        raise BenchmarkError(f"final Generation input proof is invalid: {exc}") from exc
    documents = proof["documents"]
    if not isinstance(documents, Mapping):
        raise BenchmarkError("final Generation input proof documents are invalid")

    expected: dict[str, str] = {}
    for relative, digest in sorted(active_hashes.items()):
        doc_key = make_doc_key("note", Path(relative).stem)
        expected[doc_key] = canonical_hash(
            [{"path": relative, "sha256": digest}]
        )
    if set(documents) != set(expected):
        raise BenchmarkError(
            "final Generation input proof does not cover the live corpus"
        )
    for doc_key, expected_hash in expected.items():
        observed = documents[doc_key]
        if (
            not isinstance(observed, Mapping)
            or observed.get("content_hash") != expected_hash
        ):
            raise BenchmarkError(
                f"final Generation input proof hash mismatch: {doc_key}"
            )
    return {
        "verified": True,
        "documents": len(expected),
        "input_proof_sha256": proof_ref.sha256,
        "expected_document_hashes_sha256": canonical_hash(expected),
    }

def _view_layout(pair: ParentAttestation) -> dict[str, object]:
    from app.index.v3.view_store import (
        load_search_view_metadata,
        load_view_statistics,
    )

    pageindex_dir = pair.view.view_dir.parent.parent
    view = load_search_view_metadata(pageindex_dir, pair.view.view_id)
    totals = load_view_statistics(view)
    return {
        "generation": view.generation,
        "view_id": view.view_id,
        "base_id": view.base_id,
        "delta_ids": list(view.delta_ids),
        "layer_depth": 1 + len(view.delta_ids),
        "documents": totals.documents,
        "chunks": totals.total_chunks,
    }

def _attest_round_layout(
    round_result: dict[str, object],
    result: BuildResult,
    *,
    expected_documents: int,
    expected_chunks: int,
    expected_delta_layers: int,
) -> dict[str, object]:
    layout = _view_layout(_parent(result))
    if layout["documents"] != expected_documents:
        raise BenchmarkError(
            f"{round_result['scenario']} View documents differ from the corpus"
        )
    if layout["chunks"] != expected_chunks:
        raise BenchmarkError(
            f"{round_result['scenario']} View chunks differ from the expected corpus"
        )
    delta_ids = layout["delta_ids"]
    if not isinstance(delta_ids, list) or len(delta_ids) != expected_delta_layers:
        raise BenchmarkError(
            f"{round_result['scenario']} View Delta depth is inconsistent"
        )
    round_result["view_layout"] = layout
    return layout

def _derived_state_is_empty(pageindex_dir: Path) -> bool:
    roots = (
        pageindex_dir / "generations",
        pageindex_dir / "views",
        pageindex_dir / "objects" / "segments",
        pageindex_dir / "objects" / "search",
    )
    return not any(
        any(path.is_file() for path in root.rglob("*"))
        for root in roots
        if root.is_dir()
    )


def _mechanism_report(
    rounds: Sequence[Mapping[str, object]],
    *,
    expected: Mapping[str, int],
) -> dict[str, object]:
    scenarios = ("bootstrap", "noop", "edit", "delete", "optimize")
    observed = {
        scenario: sum(1 for item in rounds if item.get("scenario") == scenario)
        for scenario in scenarios
    }
    coverage_passed = observed == dict(expected)
    full_p3_coverage = all(observed[scenario] > 0 for scenario in scenarios)
    legacy_compile_runs = 0
    base_postings_scanned = 0
    segments_loaded_peak = 0
    noop_logical_work = 0
    for item in rounds:
        metrics = item.get("metrics")
        if not isinstance(metrics, Mapping):
            raise BenchmarkError("round metrics are missing from the report")
        legacy_compile_runs += int(metrics["legacy_compile_runs"])
        base_postings_scanned += int(metrics["base_postings_scanned"])
        segments_loaded_peak = max(
            segments_loaded_peak, int(metrics["segments_loaded_peak"])
        )
        if item.get("scenario") == "noop":
            noop_logical_work += sum(int(metrics[name]) for name in _NOOP_ZERO_METRICS)
    invariants_passed = (
        legacy_compile_runs == 0
        and base_postings_scanned == 0
        and segments_loaded_peak <= 1
        and noop_logical_work == 0
    )
    return {
        "passed": coverage_passed and full_p3_coverage and invariants_passed,
        "configured_coverage_passed": coverage_passed,
        "full_p3_coverage": full_p3_coverage,
        "expected_rounds": dict(expected),
        "observed_rounds": observed,
        "legacy_compile_runs": legacy_compile_runs,
        "base_postings_scanned": base_postings_scanned,
        "segments_loaded_peak_max": segments_loaded_peak,
        "noop_logical_work": noop_logical_work,
        "dirty_postings_bound": "2*logical+2*field_postings_upper_bound",
        "dirty_bytes_written_bound": (
            "min(16MiB,8MiB+32*changed_segment_bytes); delete<8MiB"
        ),
    }

def _performance_gates(
    rounds: Sequence[Mapping[str, object]],
    query_summary: Mapping[str, object],
    queries: tuple[str, ...],
) -> list[dict[str, object]]:
    gates: list[dict[str, object]] = []

    def add(name: str, observed: float | None, limit: float, unit: str) -> None:
        gates.append(
            {
                "name": name,
                "observed": None if observed is None else round(observed, 6),
                "comparator": "<",
                "limit": limit,
                "unit": unit,
                "passed": observed is not None and observed < limit,
            }
        )

    by_scenario = {
        scenario: [item for item in rounds if item.get("scenario") == scenario]
        for scenario in ("noop", "edit", "delete")
    }
    covered = sum(bool(by_scenario[scenario]) for scenario in by_scenario)
    gates.append(
        {
            "name": "performance_scenario_coverage",
            "observed": covered,
            "comparator": "==",
            "limit": 3,
            "unit": "scenarios",
            "passed": covered == 3,
        }
    )
    if by_scenario["noop"]:
        add(
            "noop_wall_p95",
            _nearest_rank(
                [float(item["wall_time_ms"]) for item in by_scenario["noop"]],
                0.95,
            ),
            MAX_NOOP_P95_MS,
            "ms",
        )
    for scenario in ("edit", "delete"):
        if by_scenario[scenario]:
            add(
                f"{scenario}_wall_p95",
                _nearest_rank(
                    [
                        float(item["wall_time_ms"])
                        for item in by_scenario[scenario]
                    ],
                    0.95,
                ),
                MAX_DIRTY_P95_MS,
                "ms",
            )
            write_limit = (
                MAX_DIRTY_BYTES_WRITTEN
                if scenario == "edit"
                else _DIRTY_WRITE_FIXED_BYTES
            )
            add(
                f"{scenario}_bytes_written_p95",
                _nearest_rank(
                    [
                        float(item["metrics"]["bytes_written"])
                        for item in by_scenario[scenario]
                    ],
                    0.95,
                ),
                write_limit,
                "bytes",
            )

    for metric in (
        "peak_working_set_bytes",
        "peak_private_bytes_observed",
        "peak_pagefile_usage_bytes",
    ):
        values: list[float] = []
        for item in rounds:
            process = item.get("process_metrics")
            value = process.get(metric) if isinstance(process, Mapping) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
        for kind in ("incremental", "clean"):
            summary = query_summary.get(kind)
            distribution = summary.get(metric) if isinstance(summary, Mapping) else None
            value = distribution.get("max") if isinstance(distribution, Mapping) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
        add(
            f"all_process_{metric}_max",
            max(values) if values else None,
            MAX_WORKING_SET_BYTES,
            "bytes",
        )

    clean = query_summary.get("clean")
    incremental = query_summary.get("incremental")
    clean_runs = clean.get("runs") if isinstance(clean, Mapping) else 0
    incremental_runs = incremental.get("runs") if isinstance(incremental, Mapping) else 0
    if clean_runs and incremental_runs:
        clean_queries = clean.get("queries") if isinstance(clean, Mapping) else None
        incremental_queries = (
            incremental.get("queries") if isinstance(incremental, Mapping) else None
        )
        for query in queries:
            clean_dist = (
                clean_queries.get(query)
                if isinstance(clean_queries, Mapping)
                else None
            )
            incremental_dist = (
                incremental_queries.get(query)
                if isinstance(incremental_queries, Mapping)
                else None
            )
            clean_p95 = (
                clean_dist.get("p95") if isinstance(clean_dist, Mapping) else None
            )
            incremental_p95 = (
                incremental_dist.get("p95")
                if isinstance(incremental_dist, Mapping)
                else None
            )
            regression = None
            if (
                isinstance(clean_p95, (int, float))
                and not isinstance(clean_p95, bool)
                and clean_p95 > 0
                and isinstance(incremental_p95, (int, float))
                and not isinstance(incremental_p95, bool)
            ):
                regression = float(incremental_p95) / float(clean_p95) - 1.0
            add(
                f"query_regression:{query}",
                regression,
                MAX_QUERY_REGRESSION,
                "ratio",
            )
    return gates

def _exact_profile_requirements(
    spec: SyntheticCorpusSpec,
    *,
    bootstrap_runs: int,
    noop_runs: int,
    edit_runs: int,
    delete_runs: int,
    optimize_runs: int,
    query_runs: int,
) -> None:
    if spec.profile != "exact-50k":
        return
    if (
        bootstrap_runs != 1
        or noop_runs < 20
        or edit_runs < 20
        or delete_runs < 20
        or optimize_runs != 1
        or query_runs < 20
    ):
        raise ValueError(
            "exact-50k evidence requires 1 bootstrap, at least 20 no-op/edit/"
            "delete/query samples, and 1 optimize"
        )


def run_deep_incremental_benchmark(
    content_dir: Path,
    pageindex_dir: Path,
    *,
    synthetic: SyntheticCorpusSpec,
    bootstrap_runs: int = 1,
    noop_runs: int = 20,
    edit_runs: int = 20,
    delete_runs: int = 20,
    optimize_runs: int = 1,
    query_runs: int = 20,
    queries: Sequence[str] = DEFAULT_QUERIES,
    query_top_k: int = 10,
    sample_interval_ms: int = 10,
    require_os_metrics: bool = False,
) -> dict[str, object]:
    """Run the complete P3 bootstrap/no-op/dirty/optimize/query proof."""

    for name, value in (
        ("bootstrap_runs", bootstrap_runs),
        ("noop_runs", noop_runs),
        ("edit_runs", edit_runs),
        ("delete_runs", delete_runs),
        ("optimize_runs", optimize_runs),
        ("query_runs", query_runs),
    ):
        _nonnegative(name, value)
    _positive("sample_interval_ms", sample_interval_ms)
    _positive("query_top_k", query_top_k)
    if query_top_k > 1000:
        raise ValueError("query_top_k must be <= 1000")
    if bootstrap_runs != 1:
        raise ValueError("P3 evidence requires exactly one bootstrap run")
    if optimize_runs not in {0, 1}:
        raise ValueError("optimize_runs must be 0 or 1")
    if edit_runs + delete_runs == 0 and optimize_runs:
        raise ValueError("optimize evidence requires at least one dirty round")
    if delete_runs >= synthetic.documents:
        raise ValueError("delete_runs must leave at least one live document")
    query_values = tuple(queries)
    if (
        not query_values
        or len(query_values) > MAX_QUERY_COUNT
        or not all(
            isinstance(item, str) and 0 < len(item) <= MAX_QUERY_CHARS
            for item in query_values
        )
        or len(set(query_values)) != len(query_values)
    ):
        raise ValueError("queries must be unique bounded non-empty strings")
    if query_runs and optimize_runs != 1:
        raise ValueError("query parity requires one clean optimize round")
    if edit_runs and query_runs and "mutationprobe" not in query_values:
        raise ValueError("edit query parity requires the mutationprobe query")
    _exact_profile_requirements(
        synthetic,
        bootstrap_runs=bootstrap_runs,
        noop_runs=noop_runs,
        edit_runs=edit_runs,
        delete_runs=delete_runs,
        optimize_runs=optimize_runs,
        query_runs=query_runs,
    )
    if synthetic.profile == "exact-50k":
        if not require_os_metrics:
            raise ValueError("exact-50k evidence requires --require-os-metrics")
        if (
            query_values != DEFAULT_QUERIES
            or query_top_k != 10
            or sample_interval_ms != 10
        ):
            raise ValueError(
                "exact-50k evidence requires DEFAULT_QUERIES, query_top_k=10, "
                "and sample_interval_ms=10"
            )

    content = Path(content_dir).resolve()
    pageindex = Path(pageindex_dir).resolve()
    if pageindex.exists() and not pageindex.is_dir():
        raise BenchmarkError(f"PageIndex path is not a directory: {pageindex}")
    if not _derived_state_is_empty(pageindex):
        raise BenchmarkError("P3 benchmark requires an empty derived PageIndex state")
    try:
        corpus = generate_synthetic_corpus(content, synthetic)
        mutations = _MutationState.capture(content, synthetic)
    except V2BenchmarkError as exc:
        raise BenchmarkError(str(exc)) from exc

    initial_expected_chunks = (
        synthetic.expected_chunks
        if synthetic.expected_chunks is not None
        else synthetic.documents * synthetic.sections_per_document
    )
    rounds: list[dict[str, object]] = []
    edited_paths: set[str] = set()
    edited_doc_keys: set[str] = set()
    current: BuildResult | None = None
    ordinal = 0

    ordinal += 1
    bootstrap_round, bootstrap = _run_build_round(
        content,
        pageindex,
        scenario="bootstrap",
        scenario_ordinal=1,
        ordinal=ordinal,
        mode="incremental",
        parent=None,
        mutation={"kind": "none"},
        sample_interval_ms=sample_interval_ms,
        require_os_metrics=require_os_metrics,
    )
    _validate_mechanism(
        "bootstrap",
        bootstrap,
        previous=None,
        posting_bound=None,
        expected_documents=synthetic.documents,
    )
    _attest_round_layout(
        bootstrap_round,
        bootstrap,
        expected_documents=synthetic.documents,
        expected_chunks=initial_expected_chunks,
        expected_delta_layers=0,
    )
    rounds.append(bootstrap_round)
    current = bootstrap

    for scenario_ordinal in range(1, noop_runs + 1):
        ordinal += 1
        round_result, result = _run_build_round(
            content,
            pageindex,
            scenario="noop",
            scenario_ordinal=scenario_ordinal,
            ordinal=ordinal,
            mode="incremental",
            parent=_parent(current),
            mutation={"kind": "none"},
            sample_interval_ms=sample_interval_ms,
            require_os_metrics=require_os_metrics,
        )
        _validate_mechanism(
            "noop",
            result,
            previous=current,
            posting_bound=None,
            expected_documents=synthetic.documents,
        )
        _attest_round_layout(
            round_result,
            result,
            expected_documents=synthetic.documents,
            expected_chunks=initial_expected_chunks,
            expected_delta_layers=0,
        )
        rounds.append(round_result)

    for scenario_ordinal in range(1, edit_runs + 1):
        mutation = mutations.edit_one(scenario_ordinal)
        edited_paths.add(str(mutation["relative_path"]))
        ordinal += 1
        previous = current
        round_result, result = _run_build_round(
            content,
            pageindex,
            scenario="edit",
            scenario_ordinal=scenario_ordinal,
            ordinal=ordinal,
            mode="incremental",
            parent=_parent(previous),
            mutation=mutation,
            sample_interval_ms=sample_interval_ms,
            require_os_metrics=require_os_metrics,
        )
        posting_bound = _changed_document_posting_bound(
            pageindex, _parent(previous), _parent(result)
        )
        edited_doc_keys.add(str(posting_bound["doc_key"]))
        _validate_mechanism(
            "edit",
            result,
            previous=previous,
            posting_bound=posting_bound,
            expected_documents=synthetic.documents,
        )
        round_result["changed_document_work_bound"] = posting_bound
        _attest_round_layout(
            round_result,
            result,
            expected_documents=synthetic.documents,
            expected_chunks=initial_expected_chunks,
            expected_delta_layers=scenario_ordinal,
        )
        rounds.append(round_result)
        current = result

    live_documents = synthetic.documents
    for scenario_ordinal in range(1, delete_runs + 1):
        mutation = mutations.delete_one(scenario_ordinal)
        if str(mutation["relative_path"]) in edited_paths:
            raise BenchmarkError("delete removed a mutation probe document")
        ordinal += 1
        previous = current
        round_result, result = _run_build_round(
            content,
            pageindex,
            scenario="delete",
            scenario_ordinal=scenario_ordinal,
            ordinal=ordinal,
            mode="incremental",
            parent=_parent(previous),
            mutation=mutation,
            sample_interval_ms=sample_interval_ms,
            require_os_metrics=require_os_metrics,
        )
        live_documents -= 1
        _validate_mechanism(
            "delete",
            result,
            previous=previous,
            posting_bound=None,
            expected_documents=live_documents,
        )
        _attest_round_layout(
            round_result,
            result,
            expected_documents=live_documents,
            expected_chunks=(
                initial_expected_chunks
                - scenario_ordinal * synthetic.sections_per_document
            ),
            expected_delta_layers=edit_runs + scenario_ordinal,
        )
        rounds.append(round_result)
        current = result

    if not edited_paths.issubset(mutations.active_hashes):
        raise BenchmarkError("a mutation probe document did not survive")
    incremental_pair = _parent(current)
    source_proof_evidence = _validate_final_source_proof(
        incremental_pair,
        mutations.active_hashes,
    )
    clean_result: BuildResult | None = None
    clean_pair: ParentAttestation | None = None
    if optimize_runs:
        ordinal += 1
        round_result, clean_result = _run_build_round(
            content,
            pageindex,
            scenario="optimize",
            scenario_ordinal=1,
            ordinal=ordinal,
            mode="optimize",
            parent=incremental_pair,
            mutation={"kind": "none"},
            sample_interval_ms=sample_interval_ms,
            require_os_metrics=require_os_metrics,
        )
        _validate_mechanism(
            "optimize",
            clean_result,
            previous=current,
            posting_bound=None,
            expected_documents=live_documents,
        )
        _attest_round_layout(
            round_result,
            clean_result,
            expected_documents=live_documents,
            expected_chunks=(
                initial_expected_chunks
                - delete_runs * synthetic.sections_per_document
            ),
            expected_delta_layers=0,
        )
        rounds.append(round_result)
        clean_pair = _parent(clean_result)

    query_samples: list[dict[str, object]] = []
    baseline_results: dict[str, list[dict[str, object]]] | None = None
    if query_runs:
        assert clean_pair is not None
        query_ordinal = 0
        for pair_ordinal in range(1, query_runs + 1):
            order = (
                (("incremental", incremental_pair), ("clean", clean_pair))
                if pair_ordinal % 2
                else (("clean", clean_pair), ("incremental", incremental_pair))
            )
            pair_results: dict[str, dict[str, list[dict[str, object]]]] = {}
            for kind, pair in order:
                query_ordinal += 1
                sample, results = _run_query_sample(
                    pageindex,
                    pair,
                    kind=kind,
                    pair_ordinal=pair_ordinal,
                    ordinal=query_ordinal,
                    queries=query_values,
                    top_k=query_top_k,
                    sample_interval_ms=sample_interval_ms,
                    require_os_metrics=require_os_metrics,
                )
                query_samples.append(sample)
                pair_results[kind] = results
            if pair_results["incremental"] != pair_results["clean"]:
                raise BenchmarkError(
                    f"query parity failed at paired sample {pair_ordinal}"
                )
            if baseline_results is None:
                baseline_results = pair_results["clean"]
            elif pair_results["clean"] != baseline_results:
                raise BenchmarkError(
                    f"query results changed across sample {pair_ordinal}"
                )

    mutation_probe_hits: list[dict[str, object]] = []
    query_expectations: dict[str, object] = {
        "required_nonempty": [],
        "required_empty": [],
        "mutationprobe_required_hits": 0,
        "passed": not query_runs,
    }
    if query_runs:
        assert baseline_results is not None
        required_nonempty = {"synthetic"} & set(query_values)
        if synthetic.profile == "exact-50k":
            required_nonempty.update(
                {"term00000", "term00001", "section"} & set(query_values)
            )
        for query in sorted(required_nonempty):
            if not baseline_results.get(query):
                raise BenchmarkError(
                    f"required positive query returned no hits: {query}"
                )
        required_empty = {"missingtoken"} & set(query_values)
        for query in sorted(required_empty):
            if baseline_results.get(query):
                raise BenchmarkError(
                    f"required negative query returned hits: {query}"
                )

        required_probe_hits = 0
        if edit_runs:
            mutation_probe_hits = baseline_results.get("mutationprobe", [])
            required_probe_hits = min(edit_runs, query_top_k)
            observed_probe_keys = {
                str(hit["doc_key"]) for hit in mutation_probe_hits
            }
            if (
                len(mutation_probe_hits) != required_probe_hits
                or len(observed_probe_keys) != required_probe_hits
            ):
                raise BenchmarkError(
                    "mutationprobe did not cover the expected edited documents"
                )
            if not observed_probe_keys.issubset(edited_doc_keys):
                raise BenchmarkError(
                    "mutationprobe query escaped edited documents"
                )
        query_expectations = {
            "required_nonempty": sorted(required_nonempty),
            "required_empty": sorted(required_empty),
            "mutationprobe_required_hits": required_probe_hits,
            "passed": True,
        }
    query_summary = _query_summary(query_samples, query_values)
    performance_gates = _performance_gates(rounds, query_summary, query_values)
    incremental_layout = _view_layout(incremental_pair)
    clean_layout = None if clean_pair is None else _view_layout(clean_pair)
    final_active = [
        {"path": path, "sha256": digest}
        for path, digest in sorted(mutations.active_hashes.items())
    ]
    mechanism_gates = _mechanism_report(
        rounds,
        expected={
            "bootstrap": bootstrap_runs,
            "noop": noop_runs,
            "edit": edit_runs,
            "delete": delete_runs,
            "optimize": optimize_runs,
        },
    )
    performance_gate_report = {
        "all_passed": all(
            bool(item["passed"]) for item in performance_gates
        ),
        "gates": performance_gates,
    }
    overall_passed = bool(
        mechanism_gates["passed"]
        and performance_gate_report["all_passed"]
        and source_proof_evidence["verified"]
        and query_expectations["passed"]
    )
    report: dict[str, object] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark": "pageindex-v3-deep-incremental",
        "overall_passed": overall_passed,
        "configuration": {
            "content_dir": str(content),
            "pageindex_dir": str(pageindex),
            "bootstrap_runs": bootstrap_runs,
            "noop_runs": noop_runs,
            "edit_runs": edit_runs,
            "delete_runs": delete_runs,
            "optimize_runs": optimize_runs,
            "query_runs": query_runs,
            "queries": list(query_values),
            "query_top_k": query_top_k,
            "sample_interval_ms": sample_interval_ms,
            "require_os_metrics": require_os_metrics,
            "worker_execution": "fresh_subprocess_per_sample",
            "child_timeout_seconds": MAX_CHILD_WALL_SECONDS,
            "parent_resolution": "explicit_generation_view_attestation_only",
            "mutable_pointer_resolution": False,
            "build_wall_scope": (
                "fresh_child_process_plus_strict_parent_and_result_verification"
            ),
            "process_metric_scope": "direct_child_pid_only",
            "io_metric_scope": (
                "process_lifetime_logical_transfer_including_runtime_protocol_"
                "logs_and_cache"
            ),
            "query_ms_scope": "pinned_reader_search_only",
            "source_snapshot_cache": (
                "bootstrap_warms_cache; dirty cache bytes count in bytes_written"
            ),
            "log_transport": "files_no_pipe_with_overflow_failure_and_hard_retention",
            "synthetic": synthetic.as_dict(),
        },
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
        },
        "corpus": {
            **corpus,
            "observed_initial_chunks": rounds[0]["view_layout"]["chunks"],
            "exact_initial_chunk_count": (
                rounds[0]["view_layout"]["chunks"] == initial_expected_chunks
            ),
            "final_documents": live_documents,
            "final_expected_chunks": (
                initial_expected_chunks
                - delete_runs * synthetic.sections_per_document
            ),
            "final_observed_chunks": incremental_layout["chunks"],
            "exact_final_chunk_count": (
                incremental_layout["chunks"]
                == initial_expected_chunks
                - delete_runs * synthetic.sections_per_document
            ),
            "final_corpus_sha256": canonical_hash(final_active),
            "final_source_proof": source_proof_evidence,
        },
        "rounds": rounds,
        "summary": {
            scenario: _scenario_summary(rounds, scenario)
            for scenario in ("bootstrap", "noop", "edit", "delete", "optimize")
        },
        "query": {
            "compared_fields": list(_HIT_FIELDS),
            "ignored_identity_fields": ["view_id"],
            "paired_order": "alternating_incremental_clean",
            "parity": baseline_results is not None if query_runs else None,
            "correctness_scope": (
                "same Generation layout parity plus independent source-proof "
                "and synthetic query expectations"
            ),
            "expectations": query_expectations,
            "stable_results": baseline_results,
            "samples": query_samples,
            "summary": query_summary,
            "mutation_probe": {
                "required": bool(edit_runs and query_runs),
                "edited_paths": sorted(edited_paths),
                "edited_doc_keys": sorted(edited_doc_keys),
                "hit_doc_keys": sorted(
                    {str(hit["doc_key"]) for hit in mutation_probe_hits}
                ),
            },
        },
        "view_layout": {
            "incremental": incremental_layout,
            "clean": clean_layout,
        },
        "artifact_storage": _artifact_storage(pageindex),
        "mechanism_gates": mechanism_gates,
        "performance_gates": performance_gate_report,
    }
    # Prove report values themselves are canonical-JSON encodable now, not only
    # when the CLI happens to persist them later.
    canonical_bytes(report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run fresh-process PageIndex v3 deep-incremental evidence."
    )
    parser.add_argument("--content", type=Path, required=True)
    parser.add_argument("--pageindex", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--synthetic-profile", choices=("exact-50k", "custom"), default="exact-50k"
    )
    parser.add_argument("--synthetic-documents", type=int, default=10)
    parser.add_argument("--synthetic-sections", type=int, default=4)
    parser.add_argument("--synthetic-words", type=int, default=128)
    parser.add_argument("--synthetic-vocabulary", type=int, default=256)
    parser.add_argument("--synthetic-seed", type=int, default=0)
    parser.add_argument("--expected-chunks", type=int)
    parser.add_argument("--bootstrap-runs", type=int, default=1)
    parser.add_argument("--noop-runs", type=int, default=20)
    parser.add_argument("--edit-runs", type=int, default=20)
    parser.add_argument("--delete-runs", type=int, default=20)
    parser.add_argument("--optimize-runs", type=int, default=1)
    parser.add_argument("--query-runs", type=int, default=20)
    parser.add_argument("--query", action="append", dest="queries")
    parser.add_argument("--query-top-k", type=int, default=10)
    parser.add_argument("--sample-interval-ms", type=int, default=10)
    parser.add_argument("--require-os-metrics", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--query-worker":
        if len(args) != 3:
            return 2
        return _query_worker(Path(args[1]), Path(args[2]))
    parser = _parser()
    parsed = parser.parse_args(args)
    if parsed.synthetic_profile == "exact-50k":
        synthetic = SyntheticCorpusSpec.exact_50k()
    else:
        synthetic = SyntheticCorpusSpec(
            documents=parsed.synthetic_documents,
            sections_per_document=parsed.synthetic_sections,
            words_per_section=parsed.synthetic_words,
            vocabulary_size=parsed.synthetic_vocabulary,
            seed=parsed.synthetic_seed,
            profile=None,
            expected_chunks=parsed.expected_chunks,
        )
    try:
        report = run_deep_incremental_benchmark(
            parsed.content,
            parsed.pageindex,
            synthetic=synthetic,
            bootstrap_runs=parsed.bootstrap_runs,
            noop_runs=parsed.noop_runs,
            edit_runs=parsed.edit_runs,
            delete_runs=parsed.delete_runs,
            optimize_runs=parsed.optimize_runs,
            query_runs=parsed.query_runs,
            queries=parsed.queries or DEFAULT_QUERIES,
            query_top_k=parsed.query_top_k,
            sample_interval_ms=parsed.sample_interval_ms,
            require_os_metrics=parsed.require_os_metrics,
        )
    except (BenchmarkError, ValueError) as exc:
        parser.exit(2, f"benchmark failed: {exc}\n")
    payload = canonical_bytes(report)
    _write_bytes_atomic(parsed.output.resolve(), payload)
    sys.stdout.write(payload.decode("utf-8") + "\n")
    return 0 if report["overall_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BenchmarkError",
    "DEFAULT_QUERIES",
    "SyntheticCorpusSpec",
    "generate_synthetic_corpus",
    "main",
    "run_deep_incremental_benchmark",
]
