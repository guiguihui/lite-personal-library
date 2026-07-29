"""Repeatable, standard-library-only capacity benchmarks for PageIndex v2.

The benchmark invokes the worker core in-process. Optional ``tracemalloc``
measurement therefore observes allocations made by the real build path, but
is disabled by default because tracing materially slows large corpora. It writes only v2
``build``, ``objects`` and ``generations`` data; existing legacy runtime files
are fingerprinted before and after every benchmark and must remain unchanged.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import math
import os
import random
import statistics
import sys
import tempfile
import time
import tracemalloc
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .canonical import canonical_bytes, canonical_hash, write_json_atomic
from .protocol import EXIT_SUCCESS, read_json_object
from .worker import run_worker


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

    def __post_init__(self) -> None:
        _positive_int("documents", self.documents)
        _positive_int("sections_per_document", self.sections_per_document)
        _positive_int("words_per_section", self.words_per_section)
        _positive_int("vocabulary_size", self.vocabulary_size)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")

    def as_dict(self) -> dict[str, int]:
        return {
            "documents": self.documents,
            "sections_per_document": self.sections_per_document,
            "words_per_section": self.words_per_section,
            "vocabulary_size": self.vocabulary_size,
            "seed": self.seed,
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
        "schema_version": 1,
        "generator": "pageindex-v2-synthetic-v1",
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


def _run_round(
    content_dir: Path,
    pageindex_dir: Path,
    mode: str,
    *,
    base_generation: str | None,
    ordinal: int,
    mode_ordinal: int,
    trace_memory: bool,
) -> dict[str, object]:
    job_id = f"idx_benchmark_{uuid.uuid4().hex}"
    request_path = pageindex_dir / "build" / job_id / "request.json"
    write_json_atomic(
        request_path,
        {
            "schema_version": 1,
            "job_id": job_id,
            "mode": mode,
            "content_dir": str(content_dir),
            "pageindex_dir": str(pageindex_dir),
            "base_generation": base_generation,
        },
    )

    gc.collect()
    tracing_was_active = False
    baseline_bytes = 0
    tracemalloc_peak_bytes: int | None = None
    if trace_memory:
        tracing_was_active = tracemalloc.is_tracing()
        if not tracing_was_active:
            tracemalloc.start()
        baseline_bytes = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
    started = time.perf_counter()
    try:
        worker_exit_code = run_worker(request_path)
    finally:
        wall_time_ms = round((time.perf_counter() - started) * 1000, 3)
        if trace_memory:
            try:
                _current, peak_bytes = tracemalloc.get_traced_memory()
                tracemalloc_peak_bytes = max(0, peak_bytes - baseline_bytes)
            finally:
                if not tracing_was_active and tracemalloc.is_tracing():
                    tracemalloc.stop()

    result = read_json_object(request_path.parent / "result.json")
    if worker_exit_code != EXIT_SUCCESS or result.get("status") != "ready_to_publish":
        raise BenchmarkError(
            f"{mode} round {mode_ordinal} failed with exit "
            f"{worker_exit_code}: {result.get('message', result.get('status'))}"
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
        "base_generation": result.get("base_generation"),
        "generation": generation,
        "worker_exit_code": worker_exit_code,
        "wall_time_ms": wall_time_ms,
        "tracemalloc_peak_bytes": tracemalloc_peak_bytes,
        "worker_stats": dict(worker_stats),
        "warnings": list(result.get("warnings", [])),
        **sizes,
    }


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _mode_summary(
    rounds: Sequence[Mapping[str, object]],
    mode: str,
) -> dict[str, object]:
    selected = [item for item in rounds if item.get("mode") == mode]
    if not selected:
        return {
            "runs": 0,
            "wall_time_ms": None,
            "tracemalloc_peak_bytes": None,
            "generation_bytes": None,
            "segment_bytes": None,
        }
    wall = [float(item["wall_time_ms"]) for item in selected]
    peaks = [
        int(value)
        for item in selected
        for value in (item.get("tracemalloc_peak_bytes"),)
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    generations = [int(item["generation_bytes"]) for item in selected]
    segments = [int(item["segment_bytes"]) for item in selected]
    return {
        "runs": len(selected),
        "wall_time_ms": {
            "min": round(min(wall), 3),
            "median": round(float(statistics.median(wall)), 3),
            "p95": round(_nearest_rank(wall, 0.95), 3),
            "max": round(max(wall), 3),
        },
        "tracemalloc_peak_bytes": (
            {
                "max": max(peaks),
                "median": int(statistics.median(peaks)),
            }
            if peaks
            else None
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


def run_capacity_benchmark(
    content_dir: Path,
    pageindex_dir: Path,
    *,
    full_runs: int = 1,
    incremental_runs: int = 3,
    synthetic: SyntheticCorpusSpec | None = None,
    trace_memory: bool = False,
) -> dict[str, object]:
    """Run consecutive full and incremental rounds and return one report."""

    _nonnegative_runs("full_runs", full_runs)
    _nonnegative_runs("incremental_runs", incremental_runs)
    if full_runs + incremental_runs == 0:
        raise ValueError("at least one benchmark round is required")

    content = Path(content_dir).resolve()
    pageindex = Path(pageindex_dir).resolve()
    corpus = (
        generate_synthetic_corpus(content, synthetic)
        if synthetic is not None
        else None
    )
    if not content.is_dir():
        raise BenchmarkError(f"content directory does not exist: {content}")
    pageindex.mkdir(parents=True, exist_ok=True)

    legacy_before = _legacy_snapshot(pageindex)
    rounds: list[dict[str, object]] = []
    base_generation = (
        _latest_generation(pageindex) if full_runs == 0 else None
    )
    try:
        for mode, count in (
            ("full", full_runs),
            ("incremental", incremental_runs),
        ):
            for mode_ordinal in range(1, count + 1):
                round_result = _run_round(
                    content,
                    pageindex,
                    mode,
                    base_generation=(
                        base_generation if mode == "incremental" else None
                    ),
                    ordinal=len(rounds) + 1,
                    mode_ordinal=mode_ordinal,
                    trace_memory=trace_memory,
                )
                rounds.append(round_result)
                base_generation = str(round_result["generation"])
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
    report: dict[str, object] = {
        "schema_version": 1,
        "configuration": {
            "content_dir": str(content),
            "pageindex_dir": str(pageindex),
            "full_runs": full_runs,
            "incremental_runs": incremental_runs,
            "synthetic": synthetic.as_dict() if synthetic is not None else None,
            "trace_memory": trace_memory,
            "tracemalloc_scope": (
                "in_process_worker" if trace_memory else None
            ),
            "memory_metric": {
                "name": "python_tracemalloc_peak_bytes",
                "scope": "in_process_worker",
                "status": "measured" if trace_memory else "disabled",
            },
        },
        "corpus": corpus,
        "rounds": rounds,
        "summary": {
            "full": _mode_summary(rounds, "full"),
            "incremental": _mode_summary(rounds, "incremental"),
        },
        "legacy_active_files": {
            "checked": len(legacy_before),
            "unchanged": True,
        },
    }
    # Exercise the canonical serializer before returning so callers never get
    # a report containing NaN, unsupported values, or non-string object keys.
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
        description="Run repeatable PageIndex v2 full/incremental benchmarks."
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
        "--synthetic-documents",
        type=_integer_at_least(0),
        default=0,
        help="Generate this many deterministic note documents before running.",
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
        "--trace-memory",
        action="store_true",
        help=(
            "Measure in-process worker allocations with tracemalloc; "
            "disabled by default because it can be very slow."
        ),
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; stdout and optional output file use canonical JSON."""

    arguments = _parser().parse_args(argv)
    synthetic = None
    if arguments.synthetic_documents > 0:
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
            synthetic=synthetic,
            trace_memory=arguments.trace_memory,
        )
    except (BenchmarkError, OSError, ValueError) as exc:
        failure = {
            "schema_version": 1,
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
