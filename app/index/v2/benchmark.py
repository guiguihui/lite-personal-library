"""Repeatable OS-level capacity benchmarks for PageIndex.

Every measured round launches the production worker command in a fresh process.
The benchmark writes only shadow ``build``, ``objects`` and ``generations`` data;
existing legacy runtime files are fingerprinted and must remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import random
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

from .canonical import canonical_bytes, canonical_hash, write_json_atomic
from .process_metrics import ProcessMonitor
from .protocol import BuildRequest, read_json_object
from .supervisor import (
    WorkerProcessError,
    verify_worker_completion,
    worker_command,
)


class BenchmarkError(RuntimeError):
    """A benchmark request is unsafe or a measured worker round failed."""


_SYNTHETIC_MARKER = ".pageindex-v2-benchmark-synthetic.json"
_LEGACY_CORE_FILES = (
    ".fingerprints.json",
    "global-index.json",
    "node-index.json",
    "chunks.json",
    "inverted-index.json",
    "current.json",
)
_LEGACY_TREE_FOLDERS = ("books", "papers", "notes")
_EXACT_50K_SHAPE = (1000, 50, 48, 4096, 42, 50000)
_EDITABLE_TOKEN_RE = re.compile(rb"(?:term[0-9]{5}|mut[0-9a-f]{6})")


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1")


@dataclass(frozen=True, slots=True)
class SyntheticCorpusSpec:
    """Parameters that fully determine a generated Markdown corpus."""

    documents: int = 10
    sections_per_document: int = 4
    words_per_section: int = 128
    vocabulary_size: int = 256
    seed: int = 0
    profile: str | None = None
    expected_chunks: int | None = None

    def __post_init__(self) -> None:
        _positive_int("documents", self.documents)
        _positive_int("sections_per_document", self.sections_per_document)
        _positive_int("words_per_section", self.words_per_section)
        _positive_int("vocabulary_size", self.vocabulary_size)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        if self.profile is not None and (
            not isinstance(self.profile, str) or not self.profile
        ):
            raise ValueError("profile must be a non-empty string or None")
        if self.expected_chunks is not None:
            _positive_int("expected_chunks", self.expected_chunks)
        if self.profile == "exact-50k":
            actual = (
                self.documents,
                self.sections_per_document,
                self.words_per_section,
                self.vocabulary_size,
                self.seed,
                self.expected_chunks,
            )
            if actual != _EXACT_50K_SHAPE:
                raise ValueError(
                    "exact-50k profile parameters are fixed; use "
                    "SyntheticCorpusSpec.exact_50k()"
                )

    @classmethod
    def exact_50k(cls) -> "SyntheticCorpusSpec":
        (
            documents,
            sections_per_document,
            words_per_section,
            vocabulary_size,
            seed,
            expected_chunks,
        ) = _EXACT_50K_SHAPE
        return cls(
            documents=documents,
            sections_per_document=sections_per_document,
            words_per_section=words_per_section,
            vocabulary_size=vocabulary_size,
            seed=seed,
            profile="exact-50k",
            expected_chunks=expected_chunks,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "documents": self.documents,
            "sections_per_document": self.sections_per_document,
            "words_per_section": self.words_per_section,
            "vocabulary_size": self.vocabulary_size,
            "seed": self.seed,
            "profile": self.profile,
            "expected_chunks": self.expected_chunks,
        }


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
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
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


def _synthetic_document(
    document_index: int,
    spec: SyntheticCorpusSpec,
    generator: random.Random,
) -> bytes:
    title = f"Synthetic Document {document_index:05d}"
    vocabulary = [f"term{index:05d}" for index in range(spec.vocabulary_size)]
    lines = [
        "---",
        f"title: {title}",
        "description: Deterministic PageIndex v2 benchmark fixture",
        "---",
        f"# {title}",
        "",
    ]
    for section_index in range(spec.sections_per_document):
        lines.extend(
            [
                f"## Section {section_index:05d}",
                " ".join(
                    vocabulary[generator.randrange(spec.vocabulary_size)]
                    for _ in range(spec.words_per_section)
                ),
                "",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def generate_synthetic_corpus(
    content_dir: Path,
    spec: SyntheticCorpusSpec,
) -> dict[str, object]:
    """Create or verify one deterministic, marker-owned synthetic corpus.

    A non-empty directory without the benchmark marker is rejected.  Existing
    marker-owned files are only replaced when their deterministic bytes differ;
    no file is deleted, making interrupted runs recoverable and foreign content
    impossible to overwrite silently.
    """

    root = Path(content_dir).resolve()
    if root.exists() and not root.is_dir():
        raise BenchmarkError(f"content path is not a directory: {root}")
    marker_payload: dict[str, object] = {
        "schema_version": 2,
        "generator": "pageindex-v2-synthetic-v2",
        "spec": spec.as_dict(),
    }
    marker = root / _SYNTHETIC_MARKER
    if root.is_dir() and any(root.iterdir()):
        if not marker.is_file():
            raise BenchmarkError(
                f"refusing non-empty content directory without benchmark "
                f"marker: {root}"
            )
        existing_marker = read_json_object(marker)
        if existing_marker != marker_payload:
            raise BenchmarkError(
                "synthetic corpus marker does not match requested parameters"
            )

    expected_paths = {
        Path("notes") / f"synthetic-{index:05d}.md"
        for index in range(spec.documents)
    }
    if root.is_dir():
        existing_markdown = {
            path.relative_to(root)
            for path in root.rglob("*.md")
            if path.is_file()
        }
        unexpected = sorted(
            existing_markdown - expected_paths,
            key=lambda path: path.as_posix(),
        )
        if unexpected:
            raise BenchmarkError(
                "synthetic corpus contains unexpected Markdown files: "
                + ", ".join(path.as_posix() for path in unexpected)
            )

    root.mkdir(parents=True, exist_ok=True)
    generator = random.Random(spec.seed)
    records: list[dict[str, object]] = []
    total_bytes = 0
    for index, relative in enumerate(
        sorted(expected_paths, key=lambda path: path.as_posix())
    ):
        payload = _synthetic_document(index, spec, generator)
        destination = root / relative
        if not destination.is_file() or destination.read_bytes() != payload:
            _write_bytes_atomic(destination, payload)
        total_bytes += len(payload)
        records.append(
            {
                "path": relative.as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    write_json_atomic(marker, marker_payload)
    return {
        **spec.as_dict(),
        "sections": spec.documents * spec.sections_per_document,
        "words": (
            spec.documents
            * spec.sections_per_document
            * spec.words_per_section
        ),
        "markdown_bytes": total_bytes,
        "corpus_sha256": canonical_hash(records),
    }


@dataclass(slots=True)
class _SyntheticMutationState:
    """Track and mutate only files attested by one synthetic marker."""

    root: Path
    spec: SyntheticCorpusSpec
    active_hashes: dict[str, str]

    @classmethod
    def capture(
        cls,
        content_dir: Path,
        spec: SyntheticCorpusSpec,
    ) -> "_SyntheticMutationState":
        root = Path(content_dir).resolve()
        state = cls(root=root, spec=spec, active_hashes={})
        expected = {
            f"notes/synthetic-{index:05d}.md"
            for index in range(spec.documents)
        }
        state._verify_marker()
        actual = state._markdown_paths()
        if actual != expected:
            raise BenchmarkError(
                "synthetic corpus file set changed before mutation"
            )
        for relative in sorted(expected):
            target = state._owned_file(relative)
            state.active_hashes[relative] = hashlib.sha256(
                target.read_bytes()
            ).hexdigest()
        state.verify()
        return state

    def _verify_marker(self) -> None:
        expected = {
            "schema_version": 2,
            "generator": "pageindex-v2-synthetic-v2",
            "spec": self.spec.as_dict(),
        }
        marker = self.root / _SYNTHETIC_MARKER
        try:
            actual = read_json_object(marker)
        except Exception as exc:
            raise BenchmarkError(
                "synthetic marker is unavailable during mutation"
            ) from exc
        if actual != expected:
            raise BenchmarkError(
                "synthetic marker changed during benchmark"
            )

    def _markdown_paths(self) -> set[str]:
        return {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*.md")
            if path.is_file()
        }

    def _owned_file(self, relative: str) -> Path:
        target = self.root / Path(relative)
        if target.is_symlink():
            raise BenchmarkError(
                f"synthetic mutation target must not be a symlink: {relative}"
            )
        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(self.root)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise BenchmarkError(
                f"synthetic mutation target is unsafe: {relative}"
            ) from exc
        return target

    def verify(self) -> None:
        """Reject external additions, deletions, and edits before touching a file."""

        self._verify_marker()
        if self._markdown_paths() != set(self.active_hashes):
            raise BenchmarkError(
                "synthetic corpus file set changed during benchmark"
            )
        for relative, expected_hash in sorted(self.active_hashes.items()):
            actual_hash = hashlib.sha256(
                self._owned_file(relative).read_bytes()
            ).hexdigest()
            if actual_hash != expected_hash:
                raise BenchmarkError(
                    f"synthetic corpus file changed outside benchmark: {relative}"
                )

    def edit_one(self, ordinal: int) -> dict[str, object]:
        self.verify()
        paths = sorted(self.active_hashes)
        if not paths:
            raise BenchmarkError("cannot edit an empty synthetic corpus")
        relative = paths[(ordinal - 1) % len(paths)]
        target = self._owned_file(relative)
        payload = target.read_bytes()
        match = _EDITABLE_TOKEN_RE.search(payload)
        if match is None:
            raise BenchmarkError(
                f"synthetic document has no editable body token: {relative}"
            )
        salt = 0
        while True:
            digest = hashlib.sha256(
                f"{self.spec.seed}:{ordinal}:{relative}:{salt}".encode("utf-8")
            ).hexdigest()[:6]
            replacement = f"mut{digest}".encode("ascii")
            if replacement != match.group(0):
                break
            salt += 1
        mutated = payload[:match.start()] + replacement + payload[match.end():]
        before_hash = self.active_hashes[relative]
        after_hash = hashlib.sha256(mutated).hexdigest()
        _write_bytes_atomic(target, mutated)
        self.active_hashes[relative] = after_hash
        self.verify()
        return {
            "kind": "one_document_edit",
            "ordinal": ordinal,
            "relative_path": relative,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "expected_chunk_delta": 0,
        }

    def delete_one(self, ordinal: int) -> dict[str, object]:
        self.verify()
        paths = sorted(self.active_hashes)
        if not paths:
            raise BenchmarkError("cannot delete from an empty synthetic corpus")
        relative = paths[0]
        target = self._owned_file(relative)
        before_hash = self.active_hashes[relative]
        target.unlink()
        del self.active_hashes[relative]
        self.verify()
        return {
            "kind": "one_document_delete",
            "ordinal": ordinal,
            "relative_path": relative,
            "before_sha256": before_hash,
            "after_sha256": None,
            "expected_chunk_delta": -self.spec.sections_per_document,
        }


def _legacy_snapshot(pageindex_dir: Path) -> dict[str, dict[str, object]]:
    root = Path(pageindex_dir)
    paths = [root / name for name in _LEGACY_CORE_FILES]
    for folder in _LEGACY_TREE_FOLDERS:
        directory = root / folder
        if directory.is_dir():
            paths.extend(path for path in directory.glob("*.json") if path.is_file())
    snapshot: dict[str, dict[str, object]] = {}
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        snapshot[path.relative_to(root).as_posix()] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return snapshot


def _latest_generation(pageindex_dir: Path) -> str | None:
    root = Path(pageindex_dir) / "generations"
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    for directory in root.iterdir():
        manifest_path = directory / "manifest.json"
        if not directory.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = read_json_object(manifest_path)
        except Exception:
            continue
        if manifest.get("generation") == directory.name:
            candidates.append(directory)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda directory: (
            (directory / "manifest.json").stat().st_mtime_ns,
            directory.name,
        ),
    ).name


def _directory_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
    )


def _generation_sizes(
    pageindex_dir: Path,
    generation: str,
) -> dict[str, int]:
    generation_dir = Path(pageindex_dir) / "generations" / generation
    manifest = read_json_object(generation_dir / "manifest.json")
    documents = manifest.get("documents")
    if not isinstance(documents, Mapping):
        raise BenchmarkError("generation manifest documents must be an object")
    hashes = sorted(set(documents.values()))
    if not all(isinstance(value, str) and value for value in hashes):
        raise BenchmarkError("generation manifest has invalid Segment hashes")

    segment_bytes = 0
    for raw_hash in hashes:
        segment_hash = str(raw_hash)
        segment_path = (
            Path(pageindex_dir)
            / "objects"
            / "segments"
            / segment_hash[:2]
            / f"{segment_hash}.json"
        )
        if not segment_path.is_file():
            raise BenchmarkError(f"referenced Segment is missing: {segment_hash}")
        segment_bytes += segment_path.stat().st_size

    store_root = Path(pageindex_dir) / "objects" / "segments"
    segment_store_bytes = (
        _directory_bytes(store_root) if store_root.is_dir() else 0
    )
    return {
        "generation_bytes": _directory_bytes(generation_dir),
        "segment_bytes": segment_bytes,
        "segment_count": len(hashes),
        "segment_store_bytes": segment_store_bytes,
    }


def _terminate_worker(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _read_log_tail(path: Path, limit: int = 4096) -> str:
    if not path.is_file():
        return ""
    payload = path.read_bytes()
    return payload[-limit:].decode("utf-8", errors="replace").strip()


def _run_round(
    content_dir: Path,
    pageindex_dir: Path,
    mode: str,
    *,
    base_generation: str | None,
    ordinal: int,
    mode_ordinal: int,
    scenario: str,
    scenario_ordinal: int,
    mutation: Mapping[str, object],
    sample_interval_ms: int,
    require_os_metrics: bool,
) -> dict[str, object]:
    job_id = f"idx_benchmark_{uuid.uuid4().hex}"
    job_dir = pageindex_dir / "build" / job_id
    request_path = job_dir / "request.json"
    request = BuildRequest.from_dict(
        {
            "schema_version": 1,
            "job_id": job_id,
            "mode": mode,
            "content_dir": str(content_dir),
            "pageindex_dir": str(pageindex_dir),
            "base_generation": base_generation,
        }
    )
    write_json_atomic(request_path, request.as_dict())

    stdout_path = job_dir / "worker.stdout.log"
    stderr_path = job_dir / "worker.stderr.log"
    project_root = Path(__file__).resolve().parents[3]
    process: subprocess.Popen | None = None
    monitor: ProcessMonitor | None = None
    metrics = None
    returncode: int | None = None
    started = time.perf_counter()
    try:
        with stdout_path.open("wb") as stdout_stream, stderr_path.open(
            "wb"
        ) as stderr_stream:
            process = subprocess.Popen(
                worker_command(request_path),
                cwd=project_root,
                stdout=stdout_stream,
                stderr=stderr_stream,
                shell=False,
            )
            try:
                monitor = ProcessMonitor.attach(
                    process.pid,
                    sample_interval_ms=sample_interval_ms,
                )
                while process.poll() is None:
                    monitor.sample()
                    time.sleep(sample_interval_ms / 1000)
                monitor.sample()
                returncode = process.wait()
            except BaseException:
                _terminate_worker(process)
                raise
    finally:
        wall_time_ms = round((time.perf_counter() - started) * 1000, 3)
        if monitor is not None:
            metrics = monitor.finish()
            monitor.close()

    if process is None or returncode is None or metrics is None:
        raise BenchmarkError(f"{mode} round {mode_ordinal} did not start")
    if require_os_metrics and (
        metrics.status != "measured"
        or metrics.peak_working_set_bytes is None
    ):
        raise BenchmarkError(
            f"{mode} round {mode_ordinal} required OS metrics are unavailable: "
            + ", ".join(metrics.warnings)
        )

    result_path = job_dir / "result.json"
    if not result_path.is_file():
        diagnostic = _read_log_tail(stderr_path) or _read_log_tail(stdout_path)
        raise BenchmarkError(
            f"{mode} round {mode_ordinal} worker exited {returncode} "
            "without result.json"
            + (f": {diagnostic}" if diagnostic else "")
        )
    result = read_json_object(result_path)
    try:
        verify_worker_completion(
            result,
            request,
            pageindex_dir,
            returncode,
        )
    except WorkerProcessError as exc:
        raise BenchmarkError(
            f"{mode} round {mode_ordinal} returned an untrusted result: {exc}"
        ) from exc
    if result.get("status") != "ready_to_publish":
        raise BenchmarkError(
            f"{mode} round {mode_ordinal} failed with exit {returncode}: "
            f"{result.get('message', result.get('status'))}"
        )

    generation = result.get("generation")
    if not isinstance(generation, str) or not generation:
        raise BenchmarkError("worker result is missing generation")
    worker_stats = result.get("stats")
    if not isinstance(worker_stats, Mapping):
        raise BenchmarkError("worker result stats must be an object")
    sizes = _generation_sizes(pageindex_dir, generation)
    return {
        "ordinal": ordinal,
        "mode_ordinal": mode_ordinal,
        "mode": mode,
        "scenario": scenario,
        "scenario_ordinal": scenario_ordinal,
        "mutation": dict(mutation),
        "outcome": result.get("outcome"),
        "base_generation": result.get("base_generation"),
        "generation": generation,
        "worker_pid": process.pid,
        "worker_exit_code": returncode,
        "wall_time_ms": wall_time_ms,
        "process_metrics": metrics.as_dict(),
        "worker_stats": dict(worker_stats),
        "warnings": list(result.get("warnings", [])),
        **sizes,
    }

def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _distribution(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": round(min(values), 3),
        "median": round(float(statistics.median(values)), 3),
        "p95": round(_nearest_rank(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def _mode_summary(
    rounds: Sequence[Mapping[str, object]],
    mode: str,
    *,
    field: str = "mode",
) -> dict[str, object]:
    selected = [item for item in rounds if item.get(field) == mode]
    if not selected:
        return {
            "runs": 0,
            "outcomes": {},
            "wall_time_ms": None,
            "peak_working_set_bytes": None,
            "peak_private_bytes_observed": None,
            "io_read_transfer_bytes": None,
            "io_write_transfer_bytes": None,
            "generation_bytes": None,
            "segment_bytes": None,
        }

    def process_values(name: str) -> list[float]:
        values: list[float] = []
        for item in selected:
            metrics = item.get("process_metrics")
            if not isinstance(metrics, Mapping):
                continue
            value = metrics.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
        return values

    generations = [int(item["generation_bytes"]) for item in selected]
    segments = [int(item["segment_bytes"]) for item in selected]
    outcomes: dict[str, int] = {}
    for item in selected:
        outcome = str(item.get("outcome"))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    return {
        "runs": len(selected),
        "outcomes": dict(sorted(outcomes.items())),
        "wall_time_ms": _distribution(
            [float(item["wall_time_ms"]) for item in selected]
        ),
        "peak_working_set_bytes": _distribution(
            process_values("peak_working_set_bytes")
        ),
        "peak_private_bytes_observed": _distribution(
            process_values("peak_private_bytes_observed")
        ),
        "io_read_transfer_bytes": _distribution(
            process_values("io_read_transfer_bytes")
        ),
        "io_write_transfer_bytes": _distribution(
            process_values("io_write_transfer_bytes")
        ),
        "generation_bytes": {
            "last": generations[-1],
            "max": max(generations),
        },
        "segment_bytes": {
            "last": segments[-1],
            "max": max(segments),
        },
    }

def _nonnegative_runs(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be an integer >= 0")


def _initial_pageindex_state(pageindex_dir: Path) -> dict[str, object]:
    generation_root = Path(pageindex_dir) / "generations"
    segment_root = Path(pageindex_dir) / "objects" / "segments"
    generation_count = (
        sum(
            1
            for path in generation_root.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
        if generation_root.is_dir()
        else 0
    )
    segment_count = (
        sum(1 for _ in segment_root.glob("*/*.json"))
        if segment_root.is_dir()
        else 0
    )
    return {
        "generation_count": generation_count,
        "segment_count": segment_count,
        "latest_generation": _latest_generation(pageindex_dir),
        "derived_state": (
            "empty"
            if generation_count == 0 and segment_count == 0
            else "warm"
        ),
    }


def _generation_chunk_count(
    pageindex_dir: Path,
    generation: str | None,
) -> int | None:
    if generation is None:
        return None
    manifest = read_json_object(
        Path(pageindex_dir) / "generations" / generation / "manifest.json"
    )
    stats = manifest.get("stats")
    value = stats.get("chunks") if isinstance(stats, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BenchmarkError(
            f"base generation {generation} has no valid chunk count"
        )
    return value


def _worker_stat(
    round_result: Mapping[str, object],
    name: str,
) -> int:
    stats = round_result.get("worker_stats")
    value = stats.get(name) if isinstance(stats, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BenchmarkError(
            f"{round_result.get('scenario')} round has no valid {name}"
        )
    return value


def _validate_scenario_round(
    round_result: Mapping[str, object],
    scenario: str,
    *,
    previous_chunks: int | None,
    synthetic: SyntheticCorpusSpec | None,
) -> int:
    """Validate outcome, mechanism counters, and scenario-specific chunk facts."""

    outcome = round_result.get("outcome")
    base_generation = round_result.get("base_generation")
    if scenario == "unchanged" and base_generation is None:
        expected_outcome = "built"
    elif scenario == "unchanged":
        expected_outcome = "no_change"
    else:
        expected_outcome = "built"
    if outcome != expected_outcome:
        raise BenchmarkError(
            f"{scenario} round expected {expected_outcome}, observed {outcome}"
        )

    if outcome == "no_change":
        for name in (
            "segments_loaded",
            "segments_loaded_peak",
            "run_buffer_peak_bytes",
            "postings_visited",
            "generation_bytes_written",
            "full_compile_runs",
            "normal_validation_runs",
            "deep_validation_runs",
        ):
            if _worker_stat(round_result, name) != 0:
                raise BenchmarkError(
                    f"unchanged no-op round reported non-zero {name}"
                )
    else:
        if _worker_stat(round_result, "full_compile_runs") != 1:
            raise BenchmarkError(
                f"{scenario} build did not compile exactly once"
            )
        if _worker_stat(round_result, "normal_validation_runs") != 1:
            raise BenchmarkError(
                f"{scenario} build did not run Normal validation exactly once"
            )
        if _worker_stat(round_result, "deep_validation_runs") != 0:
            raise BenchmarkError(
                f"{scenario} build unexpectedly ran Deep validation"
            )
        if _worker_stat(round_result, "segments_loaded_peak") > 1:
            raise BenchmarkError(
                f"{scenario} build loaded more than one Segment at once"
            )

    if scenario == "edit":
        if _worker_stat(round_result, "segments_rebuilt") != 1:
            raise BenchmarkError("edit round did not rebuild exactly one Segment")
        if _worker_stat(round_result, "segments_deleted") != 0:
            raise BenchmarkError("edit round unexpectedly deleted a Segment")
    elif scenario == "delete":
        if _worker_stat(round_result, "segments_deleted") != 1:
            raise BenchmarkError("delete round did not delete exactly one Segment")
        if _worker_stat(round_result, "segments_rebuilt") != 0:
            raise BenchmarkError("delete round unexpectedly rebuilt a Segment")
    elif scenario == "recompile":
        if _worker_stat(round_result, "segments_rebuilt") != 0:
            raise BenchmarkError("recompile round rebuilt a Segment")
        if round_result.get("generation") != base_generation:
            raise BenchmarkError(
                "recompile round changed Generation without changing inputs"
            )

    chunks = _worker_stat(round_result, "chunks")
    if scenario == "full":
        expected_chunks = (
            synthetic.expected_chunks if synthetic is not None else None
        )
    elif scenario == "delete" and previous_chunks is not None:
        expected_chunks = previous_chunks - (
            synthetic.sections_per_document if synthetic is not None else 0
        )
    else:
        expected_chunks = previous_chunks
    if expected_chunks is not None and chunks != expected_chunks:
        raise BenchmarkError(
            f"{scenario} round expected {expected_chunks} chunks, observed {chunks}"
        )
    return chunks


def run_capacity_benchmark(
    content_dir: Path,
    pageindex_dir: Path,
    *,
    full_runs: int = 1,
    incremental_runs: int = 3,
    edit_runs: int = 0,
    delete_runs: int = 0,
    recompile_runs: int = 0,
    synthetic: SyntheticCorpusSpec | None = None,
    sample_interval_ms: int = 10,
    require_os_metrics: bool = False,
) -> dict[str, object]:
    """Run isolated full, unchanged, dirty, delete, and recompile rounds."""

    _nonnegative_runs("full_runs", full_runs)
    _nonnegative_runs("incremental_runs", incremental_runs)
    _nonnegative_runs("edit_runs", edit_runs)
    _nonnegative_runs("delete_runs", delete_runs)
    _nonnegative_runs("recompile_runs", recompile_runs)
    _positive_int("sample_interval_ms", sample_interval_ms)
    if not isinstance(require_os_metrics, bool):
        raise ValueError("require_os_metrics must be a boolean")
    if (
        full_runs
        + incremental_runs
        + edit_runs
        + delete_runs
        + recompile_runs
        == 0
    ):
        raise ValueError("at least one benchmark round is required")
    if (edit_runs > 0 or delete_runs > 0) and synthetic is None:
        raise ValueError(
            "edit/delete benchmark rounds require a synthetic marker-owned corpus"
        )
    if (
        synthetic is not None
        and delete_runs > synthetic.documents
    ):
        raise ValueError(
            "delete_runs cannot exceed synthetic document count"
        )

    content = Path(content_dir).resolve()
    pageindex = Path(pageindex_dir).resolve()
    if synthetic is None and not content.is_dir():
        raise BenchmarkError(f"content directory does not exist: {content}")
    pageindex.mkdir(parents=True, exist_ok=True)

    initial_state = _initial_pageindex_state(pageindex)
    if (
        synthetic is not None
        and synthetic.profile == "exact-50k"
        and full_runs > 0
        and initial_state["derived_state"] != "empty"
    ):
        raise BenchmarkError(
            "exact-50k full benchmark requires an empty derived PageIndex state"
        )

    corpus = (
        generate_synthetic_corpus(content, synthetic)
        if synthetic is not None
        else None
    )
    if not content.is_dir():
        raise BenchmarkError(f"content directory does not exist: {content}")

    legacy_before = _legacy_snapshot(pageindex)
    rounds: list[dict[str, object]] = []
    base_generation = _latest_generation(pageindex) if full_runs == 0 else None
    previous_chunks = _generation_chunk_count(pageindex, base_generation)
    baseline_chunks = (
        synthetic.expected_chunks
        if synthetic is not None and synthetic.expected_chunks is not None
        else previous_chunks
    )
    mutation_state = (
        _SyntheticMutationState.capture(content, synthetic)
        if synthetic is not None
        else None
    )
    mode_ordinals = {"full": 0, "incremental": 0, "recompile": 0}
    try:
        for scenario, mode, count in (
            ("full", "full", full_runs),
            ("unchanged", "incremental", incremental_runs),
            ("edit", "incremental", edit_runs),
            ("delete", "incremental", delete_runs),
            ("recompile", "recompile", recompile_runs),
        ):
            for scenario_ordinal in range(1, count + 1):
                if scenario in {"edit", "delete", "recompile"} and base_generation is None:
                    raise BenchmarkError(
                        f"{scenario} benchmark requires a base Generation"
                    )
                mutation: Mapping[str, object] = {"kind": "none"}
                if mutation_state is not None:
                    if scenario == "edit":
                        mutation = mutation_state.edit_one(scenario_ordinal)
                    elif scenario == "delete":
                        mutation = mutation_state.delete_one(scenario_ordinal)
                    else:
                        mutation_state.verify()
                mode_ordinals[mode] += 1
                round_result = _run_round(
                    content,
                    pageindex,
                    mode,
                    base_generation=(
                        None if mode == "full" else base_generation
                    ),
                    ordinal=len(rounds) + 1,
                    mode_ordinal=mode_ordinals[mode],
                    scenario=scenario,
                    scenario_ordinal=scenario_ordinal,
                    mutation=mutation,
                    sample_interval_ms=sample_interval_ms,
                    require_os_metrics=require_os_metrics,
                )
                if (
                    synthetic is not None
                    and synthetic.profile == "exact-50k"
                    and scenario == "unchanged"
                    and round_result.get("outcome") != "no_change"
                ):
                    raise BenchmarkError(
                        "exact-50k incremental round did not take the no-change path"
                    )
                observed = _validate_scenario_round(
                    round_result,
                    scenario,
                    previous_chunks=previous_chunks,
                    synthetic=synthetic,
                )
                if mutation_state is not None:
                    mutation_state.verify()
                if corpus is not None:
                    if baseline_chunks is None:
                        baseline_chunks = observed
                    corpus["observed_chunks"] = baseline_chunks
                    corpus["final_observed_chunks"] = observed
                    expected = synthetic.expected_chunks
                    corpus["exact_chunk_count"] = (
                        baseline_chunks == expected
                        if expected is not None
                        else None
                    )
                rounds.append(round_result)
                base_generation = str(round_result["generation"])
                previous_chunks = observed
    except BaseException as exc:
        legacy_after_failure = _legacy_snapshot(pageindex)
        if legacy_after_failure != legacy_before:
            raise BenchmarkError(
                "legacy active files changed during failed benchmark"
            ) from exc
        raise

    legacy_after = _legacy_snapshot(pageindex)
    if legacy_after != legacy_before:
        raise BenchmarkError("legacy active files changed during benchmark")
    backends = sorted(
        {
            str(metrics.get("backend"))
            for item in rounds
            for metrics in (item.get("process_metrics"),)
            if isinstance(metrics, Mapping)
        }
    )
    report: dict[str, object] = {
        "schema_version": 2,
        "configuration": {
            "content_dir": str(content),
            "pageindex_dir": str(pageindex),
            "full_runs": full_runs,
            "incremental_runs": incremental_runs,
            "edit_runs": edit_runs,
            "delete_runs": delete_runs,
            "recompile_runs": recompile_runs,
            "synthetic": synthetic.as_dict() if synthetic is not None else None,
            "worker_execution": "fresh_subprocess_per_round",
            "process_sample_interval_ms": sample_interval_ms,
            "require_os_metrics": require_os_metrics,
            "metric_backends": backends,
        },
        "initial_pageindex_state": initial_state,
        "corpus": corpus,
        "rounds": rounds,
        "summary": {
            "full": _mode_summary(rounds, "full"),
            "incremental": _mode_summary(rounds, "incremental"),
            "recompile": _mode_summary(rounds, "recompile"),
            "scenarios": {
                scenario: _mode_summary(
                    rounds,
                    scenario,
                    field="scenario",
                )
                for scenario in (
                    "full",
                    "unchanged",
                    "edit",
                    "delete",
                    "recompile",
                )
            },
        },
        "legacy_active_files": {
            "checked": len(legacy_before),
            "unchanged": True,
        },
    }
    canonical_bytes(report)
    return report

def _integer_at_least(minimum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer") from exc
        if parsed < minimum:
            raise argparse.ArgumentTypeError(f"must be >= {minimum}")
        return parsed

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run isolated PageIndex full/incremental benchmarks."
    )
    parser.add_argument("--content", required=True, type=Path)
    parser.add_argument("--pageindex", required=True, type=Path)
    parser.add_argument("--full-runs", type=_integer_at_least(0), default=1)
    parser.add_argument(
        "--incremental-runs",
        type=_integer_at_least(0),
        default=3,
    )
    parser.add_argument(
        "--edit-runs",
        type=_integer_at_least(0),
        default=0,
        help="Run this many deterministic one-document edit rounds.",
    )
    parser.add_argument(
        "--delete-runs",
        type=_integer_at_least(0),
        default=0,
        help="Run this many deterministic one-document delete rounds.",
    )
    parser.add_argument(
        "--recompile-runs",
        type=_integer_at_least(0),
        default=0,
        help="Recompile the current Generation without changing source files.",
    )
    synthetic_group = parser.add_mutually_exclusive_group()
    synthetic_group.add_argument(
        "--synthetic-documents",
        type=_integer_at_least(0),
        default=0,
        help="Generate this many deterministic note documents before running.",
    )
    synthetic_group.add_argument(
        "--synthetic-profile",
        choices=("exact-50k",),
        help="Use a fixed, postcondition-checked capacity corpus.",
    )
    parser.add_argument(
        "--synthetic-sections",
        type=_integer_at_least(1),
        default=4,
    )
    parser.add_argument(
        "--synthetic-words",
        type=_integer_at_least(1),
        default=128,
    )
    parser.add_argument(
        "--synthetic-vocabulary",
        type=_integer_at_least(1),
        default=256,
    )
    parser.add_argument("--synthetic-seed", type=int, default=0)
    parser.add_argument(
        "--process-sample-interval-ms",
        type=_integer_at_least(1),
        default=10,
    )
    parser.add_argument(
        "--require-os-metrics",
        action="store_true",
        help="Fail the benchmark if OS process metrics cannot be measured.",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; stdout and optional output use canonical schema 2 JSON."""

    arguments = _parser().parse_args(argv)
    synthetic = None
    if arguments.synthetic_profile == "exact-50k":
        synthetic = SyntheticCorpusSpec.exact_50k()
    elif arguments.synthetic_documents > 0:
        synthetic = SyntheticCorpusSpec(
            documents=arguments.synthetic_documents,
            sections_per_document=arguments.synthetic_sections,
            words_per_section=arguments.synthetic_words,
            vocabulary_size=arguments.synthetic_vocabulary,
            seed=arguments.synthetic_seed,
        )
    try:
        report = run_capacity_benchmark(
            arguments.content,
            arguments.pageindex,
            full_runs=arguments.full_runs,
            incremental_runs=arguments.incremental_runs,
            edit_runs=arguments.edit_runs,
            delete_runs=arguments.delete_runs,
            recompile_runs=arguments.recompile_runs,
            synthetic=synthetic,
            sample_interval_ms=arguments.process_sample_interval_ms,
            require_os_metrics=arguments.require_os_metrics,
        )
    except (BenchmarkError, OSError, ValueError) as exc:
        failure = {
            "schema_version": 2,
            "status": "failed",
            "message": str(exc),
        }
        sys.stdout.buffer.write(canonical_bytes(failure) + b"\n")
        return 1

    if arguments.output is not None:
        write_json_atomic(arguments.output.resolve(), report)
    sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BenchmarkError",
    "SyntheticCorpusSpec",
    "generate_synthetic_corpus",
    "main",
    "run_capacity_benchmark",
]
