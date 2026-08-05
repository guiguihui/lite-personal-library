"""Fresh-process PageIndex v3 benchmark contract coverage."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import app.index.v3.benchmark as benchmark_module
from app.index.v2.canonical import canonical_bytes
from app.index.v2.process_metrics import OsProcessMetrics
from app.index.v3.protocol import ViewAttestation
from app.index.v3.benchmark import (
    BenchmarkError,
    SyntheticCorpusSpec,
    _authenticate_query_view,
    _performance_gates,
    _require_os_metrics,
    _run_measured_process,
    main,
    run_deep_incremental_benchmark,
)


def _small_spec() -> SyntheticCorpusSpec:
    return SyntheticCorpusSpec(
        documents=3,
        sections_per_document=2,
        words_per_section=8,
        vocabulary_size=7,
        seed=19,
        expected_chunks=6,
    )


def test_small_corpus_proves_full_incremental_lifecycle(tmp_path: Path) -> None:
    report = run_deep_incremental_benchmark(
        tmp_path / "content",
        tmp_path / "pageindex",
        synthetic=_small_spec(),
        bootstrap_runs=1,
        noop_runs=1,
        edit_runs=1,
        delete_runs=1,
        optimize_runs=1,
        query_runs=1,
        queries=("term00000", "synthetic", "missingtoken", "mutationprobe"),
        query_top_k=5,
        sample_interval_ms=2,
    )

    assert report["schema_version"] == 1
    assert report["benchmark"] == "pageindex-v3-deep-incremental"
    rounds = report["rounds"]
    assert [item["scenario"] for item in rounds] == [
        "bootstrap",
        "noop",
        "edit",
        "delete",
        "optimize",
    ]
    assert [item["state"] for item in rounds] == [
        "ready_to_publish",
        "no_op",
        "ready_to_publish",
        "ready_to_publish",
        "ready_to_publish",
    ]
    assert len({item["job_id"] for item in rounds}) == len(rounds)
    assert all(item["worker_exit_code"] == 0 for item in rounds)
    assert all(item["wall_time_ms"] >= item["worker_process_ms"] for item in rounds)
    assert all(item["strict_verification_ms"] >= 0 for item in rounds)
    assert all(item["process_metrics"]["samples"] > 0 for item in rounds)

    bootstrap, noop, edit, delete, optimize = rounds
    assert bootstrap["parent_generation"] is None
    assert noop["generation"] == bootstrap["generation"]
    assert noop["view_id"] == bootstrap["view_id"]
    for field in (
        "segments_loaded",
        "segments_loaded_peak",
        "postings_visited",
        "bytes_written",
        "normal_validation_runs",
        "legacy_compile_runs",
    ):
        assert noop["metrics"][field] == 0

    assert edit["metrics"]["segments_rebuilt"] == 1
    assert edit["metrics"]["segments_loaded"] <= 2
    assert edit["metrics"]["segments_loaded_peak"] <= 1
    bound = edit["changed_document_work_bound"]
    assert bound["changed_documents"] == 1
    assert bound["logical_postings"] > 0
    assert edit["metrics"]["postings_visited"] <= bound[
        "postings_visited_bound"
    ]
    assert edit["metrics"]["bytes_written"] < bound["bytes_written_bound"]

    assert delete["metrics"]["segments_deleted"] == 1
    assert delete["mutation"]["relative_path"] != edit["mutation"]["relative_path"]
    assert delete["metrics"]["segments_loaded"] == 0
    assert delete["metrics"]["postings_visited"] == 0
    assert delete["metrics"]["bytes_written"] < 8 * 1024 * 1024
    assert optimize["generation"] == delete["generation"]
    assert optimize["view_id"] != delete["view_id"]
    assert optimize["metrics"]["segments_loaded"] == 2
    assert bootstrap["view_layout"]["chunks"] == 6
    assert bootstrap["view_layout"]["layer_depth"] == 1
    assert edit["view_layout"]["layer_depth"] == 2
    assert delete["view_layout"]["chunks"] == 4
    assert delete["view_layout"]["layer_depth"] == 3
    assert optimize["view_layout"]["chunks"] == 4
    assert optimize["view_layout"]["layer_depth"] == 1
    assert all(item["metrics"]["base_postings_scanned"] == 0 for item in rounds)
    assert all(item["metrics"]["legacy_compile_runs"] == 0 for item in rounds)

    query = report["query"]
    assert query["parity"] is True
    assert query["compared_fields"] == [
        "generation",
        "doc_key",
        "doc_uid",
        "segment_hash",
        "local_id",
        "node_key",
        "score",
        "rrf_score",
    ]
    assert [item["kind"] for item in query["samples"]] == [
        "incremental",
        "clean",
    ]
    assert query["samples"][0]["result_sha256"] == query["samples"][1][
        "result_sha256"
    ]
    assert query["mutation_probe"]["required"] is True
    assert query["expectations"]["passed"] is True
    assert query["expectations"]["required_empty"] == ["missingtoken"]
    assert query["expectations"]["mutationprobe_required_hits"] == 1
    assert query["mutation_probe"]["hit_doc_keys"]
    assert set(query["mutation_probe"]["hit_doc_keys"]).issubset(
        query["mutation_probe"]["edited_doc_keys"]
    )
    assert all(item["process_metrics"]["samples"] > 0 for item in query["samples"])
    assert report["configuration"]["parent_resolution"] == (
        "explicit_generation_view_attestation_only"
    )
    assert report["configuration"]["mutable_pointer_resolution"] is False
    assert not (tmp_path / "pageindex" / "current.json").exists()
    assert not (tmp_path / "pageindex" / "latest.json").exists()
    assert report["artifact_storage"]["bases"]["files"] > 0
    assert report["artifact_storage"]["deltas"]["files"] > 0
    assert report["mechanism_gates"]["passed"] is True
    gate_names = {
        item["name"] for item in report["performance_gates"]["gates"]
    }
    assert "performance_scenario_coverage" in gate_names
    assert "edit_bytes_written_p95" in gate_names
    assert "delete_bytes_written_p95" in gate_names
    assert isinstance(report["overall_passed"], bool)
    assert report["corpus"]["observed_initial_chunks"] == 6
    assert report["corpus"]["final_observed_chunks"] == 4
    assert report["corpus"]["exact_initial_chunk_count"] is True
    assert report["corpus"]["exact_final_chunk_count"] is True
    assert report["corpus"]["final_source_proof"]["verified"] is True
    assert report["corpus"]["final_source_proof"]["documents"] == 2
    assert report["view_layout"]["incremental"]["layer_depth"] == 3
    assert report["view_layout"]["clean"]["layer_depth"] == 1

    clean_layout = report["view_layout"]["clean"]
    view_dir = tmp_path / "pageindex" / "views" / clean_layout["view_id"]
    manifest = json.loads((view_dir / "manifest.json").read_text(encoding="utf-8"))
    bad_attestation = ViewAttestation(
        clean_layout["view_id"],
        view_dir,
        "0" * 64,
        manifest["generation"],
        manifest["generation_manifest_sha256"],
    )
    with pytest.raises(BenchmarkError, match="View manifest attestation mismatch"):
        _authenticate_query_view(bad_attestation)
    assert canonical_bytes(json.loads(canonical_bytes(report))) == canonical_bytes(
        report
    )


def test_observed_chunk_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        benchmark_module,
        "_view_layout",
        lambda _pair: {
            "generation": "1" * 64,
            "view_id": "2" * 64,
            "base_id": "3" * 64,
            "delta_ids": [],
            "layer_depth": 1,
            "documents": 1,
            "chunks": 999,
        },
    )

    with pytest.raises(BenchmarkError, match="View chunks differ"):
        run_deep_incremental_benchmark(
            tmp_path / "content",
            tmp_path / "pageindex",
            synthetic=SyntheticCorpusSpec(
                documents=1,
                sections_per_document=1,
                words_per_section=4,
                vocabulary_size=3,
                seed=7,
                expected_chunks=1,
            ),
            bootstrap_runs=1,
            noop_runs=0,
            edit_runs=0,
            delete_runs=0,
            optimize_runs=0,
            query_runs=0,
            sample_interval_ms=2,
        )

def test_exact_50k_requires_the_evidence_sample_counts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exact-50k evidence requires"):
        run_deep_incremental_benchmark(
            tmp_path / "content",
            tmp_path / "pageindex",
            synthetic=SyntheticCorpusSpec.exact_50k(),
            bootstrap_runs=1,
            noop_runs=1,
            edit_runs=1,
            delete_runs=1,
            optimize_runs=1,
            query_runs=1,
        )


def test_os_metric_gate_requires_memory_and_io_fields() -> None:
    unavailable = OsProcessMetrics(
        backend="test",
        status="measured",
        scope="worker_process",
        sample_interval_ms=1,
        samples=1,
        peak_working_set_bytes=1,
        peak_private_bytes_observed=1,
        peak_pagefile_usage_bytes=None,
        io_read_operations=1,
        io_write_operations=1,
        io_read_transfer_bytes=1,
        io_write_transfer_bytes=1,
        warnings=(),
    )

    with pytest.raises(BenchmarkError, match="peak_pagefile_usage_bytes"):
        _require_os_metrics(unavailable, "test round")


def test_query_regression_gate_is_computed_per_query() -> None:
    query_summary = {
        "clean": {
            "runs": 20,
            "queries": {
                "broad": {"p95": 100.0},
                "rare": {"p95": 1.0},
            }
        },
        "incremental": {
            "runs": 20,
            "queries": {
                "broad": {"p95": 105.0},
                "rare": {"p95": 1.2},
            }
        },
    }

    gates = _performance_gates([], query_summary, ("broad", "rare"))
    by_name = {item["name"]: item for item in gates}

    assert by_name["query_regression:broad"]["passed"] is True
    assert by_name["query_regression:broad"]["observed"] == 0.05
    assert by_name["query_regression:rare"]["passed"] is False
    assert by_name["query_regression:rare"]["observed"] == 0.2


def test_exact_50k_requires_os_metrics_before_touching_the_corpus(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="requires --require-os-metrics"):
        run_deep_incremental_benchmark(
            tmp_path / "content",
            tmp_path / "pageindex",
            synthetic=SyntheticCorpusSpec.exact_50k(),
            bootstrap_runs=1,
            noop_runs=20,
            edit_runs=20,
            delete_runs=20,
            optimize_runs=1,
            query_runs=20,
            require_os_metrics=False,
        )
    assert not (tmp_path / "content").exists()
    assert not (tmp_path / "pageindex").exists()


@pytest.mark.parametrize(
    "overrides",
    (
        {"queries": ("onlyeasy",)},
        {"query_top_k": 1},
        {"sample_interval_ms": 5},
    ),
)
def test_exact_50k_rejects_nonstandard_query_evidence_configuration(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    arguments: dict[str, object] = {
        "synthetic": SyntheticCorpusSpec.exact_50k(),
        "bootstrap_runs": 1,
        "noop_runs": 20,
        "edit_runs": 20,
        "delete_runs": 20,
        "optimize_runs": 1,
        "query_runs": 20,
        "require_os_metrics": True,
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match="mutationprobe|DEFAULT_QUERIES"):
        run_deep_incremental_benchmark(
            tmp_path / "content",
            tmp_path / "pageindex",
            **arguments,  # type: ignore[arg-type]
        )

    assert not (tmp_path / "content").exists()

def test_noisy_child_is_terminated_and_logs_are_trimmed(tmp_path: Path) -> None:
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * (2 * 1024 * 1024)); sys.stdout.flush()",
    ]

    with pytest.raises(BenchmarkError, match="child log exceeded"):
        _run_measured_process(
            command,
            cwd=tmp_path,
            stdout_path=stdout,
            stderr_path=stderr,
            sample_interval_ms=1,
        )

    assert stdout.stat().st_size <= 64 * 1024
    assert stderr.stat().st_size <= 64 * 1024


def test_orchestrator_import_does_not_load_worker_or_reader() -> None:
    project_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.index.v3.benchmark; "
                "assert 'app.index.v3.worker' not in sys.modules; "
                "assert 'app.index.v3.reader' not in sys.modules"
            ),
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr

def test_cli_writes_canonical_report_even_when_optional_perf_gate_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "report.json"
    exit_code = main(
        [
            "--content",
            str(tmp_path / "content"),
            "--pageindex",
            str(tmp_path / "pageindex"),
            "--output",
            str(output),
            "--synthetic-profile",
            "custom",
            "--synthetic-documents",
            "1",
            "--synthetic-sections",
            "1",
            "--synthetic-words",
            "4",
            "--synthetic-vocabulary",
            "3",
            "--synthetic-seed",
            "1",
            "--expected-chunks",
            "1",
            "--bootstrap-runs",
            "1",
            "--noop-runs",
            "0",
            "--edit-runs",
            "0",
            "--delete-runs",
            "0",
            "--optimize-runs",
            "0",
            "--query-runs",
            "0",
        ]
    )

    assert exit_code == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert output.read_bytes() == canonical_bytes(report)
    assert json.loads(capsys.readouterr().out) == report
    assert [item["scenario"] for item in report["rounds"]] == ["bootstrap"]
    assert report["mechanism_gates"]["passed"] is False
    assert report["mechanism_gates"]["full_p3_coverage"] is False
