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
            expected_chunks=2,
        ),
    )

    assert report["schema_version"] == 2
    assert [item["mode"] for item in report["rounds"]] == [
        "full",
        "incremental",
    ]
    full, incremental = report["rounds"]
    assert full["outcome"] == "built"
    assert incremental["outcome"] == "no_change"
    assert full["worker_pid"] != incremental["worker_pid"]
    assert all(item["process_metrics"]["samples"] > 0 for item in report["rounds"])
    assert full["worker_stats"]["segments_rebuilt"] == 2
    assert incremental["worker_stats"]["segments_reused"] == 2
    assert incremental["worker_stats"]["segments_loaded"] == 0
    assert incremental["worker_stats"]["postings_visited"] == 0
    assert incremental["worker_stats"]["generation_bytes_written"] == 0
    assert incremental["worker_stats"]["deep_validation_runs"] == 0
    assert incremental["generation"] == full["generation"]
    assert all(item["wall_time_ms"] >= 0 for item in report["rounds"])
    assert all(item["generation_bytes"] > 0 for item in report["rounds"])
    assert all(item["segment_bytes"] > 0 for item in report["rounds"])
    assert all(item["segment_count"] == 2 for item in report["rounds"])
    assert report["summary"]["full"]["runs"] == 1
    assert report["summary"]["incremental"]["runs"] == 1
    assert report["summary"]["full"]["outcomes"] == {"built": 1}
    assert report["summary"]["incremental"]["outcomes"] == {
        "no_change": 1
    }
    assert report["initial_pageindex_state"] == {
        "generation_count": 0,
        "segment_count": 0,
        "latest_generation": None,
        "derived_state": "empty",
    }
    assert report["summary"]["full"]["peak_working_set_bytes"] is not None
    assert report["configuration"]["worker_execution"] == (
        "fresh_subprocess_per_round"
    )
    assert report["legacy_active_files"] == {
        "checked": 1,
        "unchanged": True,
    }
    assert legacy.read_bytes() == b"legacy-active-sentinel"
    assert canonical_bytes(json.loads(canonical_bytes(report))) == canonical_bytes(
        report
    )
    assert report["corpus"]["observed_chunks"] == 2
    assert report["corpus"]["exact_chunk_count"] is True


def test_exact_50k_profile_has_the_measured_fixture_shape() -> None:
    spec = SyntheticCorpusSpec.exact_50k()

    assert spec.documents == 1000
    assert spec.sections_per_document == 50
    assert spec.words_per_section == 48
    assert spec.vocabulary_size == 4096
    assert spec.seed == 42
    assert spec.profile == "exact-50k"
    assert spec.expected_chunks == 50000


def test_exact_50k_profile_parameters_cannot_be_forged() -> None:
    with pytest.raises(ValueError, match="parameters are fixed"):
        SyntheticCorpusSpec(
            documents=1,
            sections_per_document=1,
            words_per_section=4,
            vocabulary_size=3,
            seed=1,
            profile="exact-50k",
            expected_chunks=1,
        )


def test_exact_profile_full_run_rejects_warm_derived_state(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    generation = pageindex / "generations" / ("a" * 20)
    generation.mkdir(parents=True)
    (generation / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(BenchmarkError, match="empty derived PageIndex"):
        run_capacity_benchmark(
            tmp_path / "content",
            pageindex,
            full_runs=1,
            incremental_runs=0,
            synthetic=SyntheticCorpusSpec.exact_50k(),
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
            "--require-os-metrics",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert output.read_bytes() == canonical_bytes(report)
    assert json.loads(capsys.readouterr().out) == report
    assert report["configuration"]["synthetic"]["seed"] == 11
    assert report["configuration"]["require_os_metrics"] is True
    assert report["rounds"][0]["process_metrics"]["status"] == "measured"
    assert report["rounds"][0]["process_metrics"]["samples"] > 0
    assert len(report["rounds"]) == 1


def test_worker_launch_exception_is_not_masked_by_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WorkerBoom(RuntimeError):
        pass

    def fail_launch(*_args, **_kwargs):
        raise WorkerBoom("worker exploded")

    monkeypatch.setattr(benchmark_module.subprocess, "Popen", fail_launch)

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
        )
