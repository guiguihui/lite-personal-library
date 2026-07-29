"""Repeatable capacity benchmark coverage for PageIndex v2."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.index.v2.benchmark as benchmark_module
from app.index.v2.benchmark import (
    BenchmarkError,
    SyntheticCorpusSpec,
    generate_synthetic_corpus,
    main,
    run_capacity_benchmark,
)
from app.index.v2.canonical import canonical_bytes


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    }


def test_synthetic_corpus_is_repeatable_and_refuses_foreign_content(
    tmp_path: Path,
) -> None:
    spec = SyntheticCorpusSpec(
        documents=2,
        sections_per_document=2,
        words_per_section=12,
        vocabulary_size=7,
        seed=42,
    )
    left = tmp_path / "left"
    right = tmp_path / "right"

    left_result = generate_synthetic_corpus(left, spec)
    right_result = generate_synthetic_corpus(right, spec)

    assert left_result == right_result
    assert left_result["documents"] == 2
    assert left_result["sections"] == 4
    assert left_result["words"] == 48
    assert _snapshot(left) == _snapshot(right)

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.md").write_text("must stay", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="non-empty content directory"):
        generate_synthetic_corpus(foreign, spec)
    assert (foreign / "keep.md").read_text(encoding="utf-8") == "must stay"


def test_capacity_benchmark_records_round_metrics_and_preserves_legacy(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content"
    pageindex = tmp_path / "pageindex"
    pageindex.mkdir()
    legacy = pageindex / "global-index.json"
    legacy.write_bytes(b"legacy-active-sentinel")

    report = run_capacity_benchmark(
        content,
        pageindex,
        full_runs=1,
        incremental_runs=1,
        synthetic=SyntheticCorpusSpec(
            documents=2,
            sections_per_document=1,
            words_per_section=8,
            vocabulary_size=5,
            seed=7,
        ),
    )

    assert report["schema_version"] == 1
    assert [item["mode"] for item in report["rounds"]] == [
        "full",
        "incremental",
    ]
    full, incremental = report["rounds"]
    assert full["worker_stats"]["segments_rebuilt"] == 2
    assert incremental["worker_stats"]["segments_reused"] == 2
    assert incremental["generation"] == full["generation"]
    assert all(item["wall_time_ms"] >= 0 for item in report["rounds"])
    assert all(
        item["tracemalloc_peak_bytes"] is None for item in report["rounds"]
    )
    assert all(item["generation_bytes"] > 0 for item in report["rounds"])
    assert all(item["segment_bytes"] > 0 for item in report["rounds"])
    assert all(item["segment_count"] == 2 for item in report["rounds"])
    assert report["summary"]["full"]["runs"] == 1
    assert report["summary"]["incremental"]["runs"] == 1
    assert report["summary"]["full"]["tracemalloc_peak_bytes"] is None
    assert report["configuration"]["memory_metric"] == {
        "name": "python_tracemalloc_peak_bytes",
        "scope": "in_process_worker",
        "status": "disabled",
    }
    assert report["legacy_active_files"] == {
        "checked": 1,
        "unchanged": True,
    }
    assert legacy.read_bytes() == b"legacy-active-sentinel"
    assert canonical_bytes(json.loads(canonical_bytes(report))) == canonical_bytes(
        report
    )


def test_benchmark_cli_writes_canonical_json(tmp_path: Path, capsys) -> None:
    output = tmp_path / "benchmark.json"
    exit_code = main(
        [
            "--content",
            str(tmp_path / "content"),
            "--pageindex",
            str(tmp_path / "pageindex"),
            "--full-runs",
            "1",
            "--incremental-runs",
            "0",
            "--synthetic-documents",
            "1",
            "--synthetic-sections",
            "1",
            "--synthetic-words",
            "6",
            "--synthetic-vocabulary",
            "4",
            "--synthetic-seed",
            "11",
            "--trace-memory",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert output.read_bytes() == canonical_bytes(report)
    assert json.loads(capsys.readouterr().out) == report
    assert report["configuration"]["synthetic"]["seed"] == 11
    assert report["configuration"]["trace_memory"] is True
    assert report["configuration"]["memory_metric"]["status"] == "measured"
    assert report["rounds"][0]["tracemalloc_peak_bytes"] > 0
    assert len(report["rounds"]) == 1


def test_worker_exception_is_not_masked_by_memory_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WorkerBoom(RuntimeError):
        pass

    def fail_worker(_request_path: Path) -> int:
        raise WorkerBoom("worker exploded")

    monkeypatch.setattr(benchmark_module, "run_worker", fail_worker)

    with pytest.raises(WorkerBoom, match="worker exploded"):
        run_capacity_benchmark(
            tmp_path / "content",
            tmp_path / "pageindex",
            full_runs=1,
            incremental_runs=0,
            synthetic=SyntheticCorpusSpec(
                documents=1,
                sections_per_document=1,
                words_per_section=4,
                vocabulary_size=3,
                seed=1,
            ),
            trace_memory=True,
        )
