from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.index.v2.supervisor as supervisor_module
import app.index.v2.worker as worker_module
import app.index.v2.validator as validator_module
from app.index.v2.canonical import canonical_hash, write_json_atomic
from app.index.v2.protocol import BuildRequest, ProtocolError
from app.index.v2.supervisor import (
    WorkerProcessError,
    run_shadow_build,
    worker_command,
)
from app.index.v2.worker import run_worker


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _request(
    job_dir: Path,
    content_dir: Path,
    pageindex_dir: Path,
    *,
    mode: str,
    base_generation: str | None = None,
) -> Path:
    job_dir.mkdir(parents=True)
    path = job_dir / "request.json"
    write_json_atomic(
        path,
        {
            "schema_version": 1,
            "job_id": job_dir.name,
            "mode": mode,
            "content_dir": str(content_dir.resolve()),
            "pageindex_dir": str(pageindex_dir.resolve()),
            "base_generation": base_generation,
        },
    )
    return path


def _install_success_worker_mock(
    monkeypatch: pytest.MonkeyPatch,
    pageindex: Path,
    *,
    result_overrides: dict[str, object] | None = None,
    manifest_overrides: dict[str, object] | None = None,
    noncanonical_manifest: bool = False,
) -> tuple[str, str]:
    job_hex = "1" * 32
    job_id = f"idx_{job_hex}"
    generation = "a" * 20
    monkeypatch.setattr(
        supervisor_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=job_hex),
    )

    def fake_run(command, **_kwargs):
        request_path = Path(command[-1])
        request = _read_json(request_path)
        generation_dir = pageindex.resolve() / "generations" / generation
        generation_dir.mkdir(parents=True)
        manifest: dict[str, object] = {
            "schema_version": 2,
            "generation": generation,
            "documents": {},
            "files": {},
        }
        if manifest_overrides:
            manifest.update(manifest_overrides)
        manifest_path = generation_dir / "manifest.json"
        write_json_atomic(manifest_path, manifest)
        if noncanonical_manifest:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        result: dict[str, object] = {
            "schema_version": 1,
            "status": "ready_to_publish",
            "job_id": request["job_id"],
            "mode": request["mode"],
            "base_generation": request["base_generation"],
            "generation": generation,
            "generation_dir": str(generation_dir),
            "manifest_sha256": canonical_hash(manifest),
            "warnings": [],
            "stats": {},
        }
        if result_overrides:
            result.update(result_overrides)
        write_json_atomic(request_path.parent / "result.json", result)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(supervisor_module.subprocess, "run", fake_run)
    return job_id, generation


def test_build_request_rejects_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(ProtocolError, match="absolute path"):
        BuildRequest.from_dict(
            {
                "schema_version": 1,
                "job_id": "idx_abc",
                "mode": "full",
                "content_dir": "content",
                "pageindex_dir": str(tmp_path.resolve()),
                "base_generation": None,
            }
        )


def test_worker_rejects_unknown_mode(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    write_json_atomic(request, {"schema_version": 1, "mode": "patch"})

    assert run_worker(request) == 2
    result = _read_json(tmp_path / "result.json")
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_request"


def test_recompile_requires_base_generation(tmp_path: Path) -> None:
    with pytest.raises(ProtocolError, match="requires base_generation"):
        BuildRequest.from_dict(
            {
                "schema_version": 1,
                "job_id": "idx_abc",
                "mode": "recompile",
                "content_dir": str(tmp_path.resolve()),
                "pageindex_dir": str(tmp_path.resolve()),
                "base_generation": None,
            }
        )


def test_worker_honors_preexisting_cancel_request(
    tmp_path: Path, sample_content: Path
) -> None:
    pageindex = tmp_path / "pageindex"
    job_dir = pageindex / "build" / "idx_cancel"
    request = _request(job_dir, sample_content, pageindex, mode="full")
    (job_dir / "cancel.request").touch()

    assert run_worker(request) == 3
    assert _read_json(job_dir / "result.json")["status"] == "cancelled"
    assert _read_json(job_dir / "progress.json")["status"] == "cancelled"


def test_worker_parses_the_same_immutable_bytes_it_fingerprints(
    tmp_path: Path,
    sample_content: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    job_dir = pageindex / "build" / "idx_source_snapshot"
    live_note = sample_content / "notes" / "welcome.md"
    original_bytes = live_note.read_bytes()
    transient_text = (
        "---\ntitle: Welcome\n---\n# Welcome\n"
        "TRANSIENT_RACE_CONTENT must never enter the Segment.\n"
    )
    real_build_segment = worker_module.build_segment

    def build_during_aba_save(source, recipe):
        live_note.write_text(transient_text, encoding="utf-8")
        try:
            return real_build_segment(source, recipe)
        finally:
            live_note.write_bytes(original_bytes)

    monkeypatch.setattr(worker_module, "build_segment", build_during_aba_save)

    assert run_worker(
        _request(job_dir, sample_content, pageindex, mode="full")
    ) == 0
    result = _read_json(job_dir / "result.json")
    assert result["stats"]["stabilization_attempts"] == 1
    chunks = _read_json(
        pageindex
        / "generations"
        / str(result["generation"])
        / "chunks.json"
    )["chunks"]
    assert all(
        "TRANSIENT_RACE_CONTENT" not in str(chunk["body"])
        for chunk in chunks
    )


def test_worker_honors_cancel_requested_during_segment_build(
    tmp_path: Path,
    sample_content: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    job_dir = pageindex / "build" / "idx_cancel_during_build"
    legacy = pageindex / "global-index.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-remains-active")
    real_build_segment = worker_module.build_segment
    calls = 0

    def build_then_cancel(source, recipe):
        nonlocal calls
        segment = real_build_segment(source, recipe)
        calls += 1
        if calls == 1:
            (job_dir / "cancel.request").touch()
        return segment

    monkeypatch.setattr(worker_module, "build_segment", build_then_cancel)

    assert run_worker(
        _request(job_dir, sample_content, pageindex, mode="full")
    ) == 3
    assert _read_json(job_dir / "result.json")["status"] == "cancelled"
    assert legacy.read_bytes() == b"legacy-remains-active"
    generations = pageindex / "generations"
    assert not generations.exists() or not any(generations.iterdir())


def test_worker_full_incremental_and_recompile_round_trip(
    tmp_path: Path, sample_content: Path
) -> None:
    pageindex = tmp_path / "pageindex"
    legacy = pageindex / "global-index.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-must-not-change")

    full_dir = pageindex / "build" / "idx_full"
    assert run_worker(
        _request(full_dir, sample_content, pageindex, mode="full")
    ) == 0
    full = _read_json(full_dir / "result.json")
    generation = str(full["generation"])
    assert full["status"] == "ready_to_publish"
    assert full["stats"]["segments_rebuilt"] == 3
    assert full["stats"]["segments_reused"] == 0
    assert (pageindex / "generations" / generation / "manifest.json").is_file()
    assert legacy.read_bytes() == b"legacy-must-not-change"

    incremental_dir = pageindex / "build" / "idx_incremental"
    assert run_worker(
        _request(
            incremental_dir,
            sample_content,
            pageindex,
            mode="incremental",
            base_generation=generation,
        )
    ) == 0
    incremental = _read_json(incremental_dir / "result.json")
    assert incremental["generation"] == generation
    assert incremental["stats"]["segments_rebuilt"] == 0
    assert incremental["stats"]["segments_reused"] == 3

    recompile_dir = pageindex / "build" / "idx_recompile"
    assert run_worker(
        _request(
            recompile_dir,
            sample_content,
            pageindex,
            mode="recompile",
            base_generation=generation,
        )
    ) == 0
    recompile = _read_json(recompile_dir / "result.json")
    assert recompile["generation"] == generation
    assert recompile["stats"]["segments_rebuilt"] == 0
    assert recompile["stats"]["segments_reused"] == 3


def test_worker_persists_compact_and_full_shadow_reports(
    tmp_path: Path,
    sample_content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    full_dir = pageindex / "build" / "idx_shadow_seed"
    assert run_worker(
        _request(full_dir, sample_content, pageindex, mode="full")
    ) == 0
    generation = str(_read_json(full_dir / "result.json")["generation"])
    generation_dir = pageindex / "generations" / generation
    manifest = _read_json(generation_dir / "manifest.json")
    files = manifest["files"]
    assert isinstance(files, dict)
    for relative in files:
        source = generation_dir / relative
        destination = pageindex / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    report_dir = pageindex / "build" / "idx_shadow_report"
    assert run_worker(
        _request(
            report_dir,
            sample_content,
            pageindex,
            mode="recompile",
            base_generation=generation,
        )
    ) == 0

    result = _read_json(report_dir / "result.json")
    summary = result["shadow_report"]
    assert summary["status"] == "complete"
    assert summary["report_file"] == "shadow-report.json"
    assert summary["ok"] is True
    assert summary["publish_blocking_errors"] == 0
    full_report = _read_json(report_dir / "shadow-report.json")
    assert full_report["generation"] == generation
    assert full_report["structural_ok"] is True
    assert full_report["unexplained_semantic_mismatch"] == 0


def test_corrupt_base_fails_without_touching_legacy_and_full_recovers(
    tmp_path: Path,
    sample_content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    seed_dir = pageindex / "build" / "idx_recovery_seed"
    assert run_worker(
        _request(seed_dir, sample_content, pageindex, mode="full")
    ) == 0
    generation = str(_read_json(seed_dir / "result.json")["generation"])
    generation_dir = pageindex / "generations" / generation
    manifest = _read_json(generation_dir / "manifest.json")
    files = manifest["files"]
    assert isinstance(files, dict)
    for relative in files:
        source = generation_dir / relative
        destination = pageindex / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    write_json_atomic(
        pageindex / "current.json",
        {"generation": "legacy-pointer-must-not-change"},
    )
    protected_paths = [pageindex / relative for relative in files]
    protected_paths.append(pageindex / "current.json")
    before = {path: path.read_bytes() for path in protected_paths}

    segment_hash = next(iter(manifest["documents"].values()))
    object_path = (
        pageindex
        / "objects"
        / "segments"
        / segment_hash[:2]
        / f"{segment_hash}.json"
    )
    object_path.write_text('{"corrupt":true}', encoding="utf-8")

    failed_dir = pageindex / "build" / "idx_corrupt_base"
    assert run_worker(
        _request(
            failed_dir,
            sample_content,
            pageindex,
            mode="incremental",
            base_generation=generation,
        )
    ) == 1
    failed = _read_json(failed_dir / "result.json")
    assert failed["status"] == "failed"
    assert failed["error_code"] == "build_failed"
    assert {path: path.read_bytes() for path in protected_paths} == before

    recovery_dir = pageindex / "build" / "idx_full_recovery"
    assert run_worker(
        _request(recovery_dir, sample_content, pageindex, mode="full")
    ) == 0
    recovered = _read_json(recovery_dir / "result.json")
    assert recovered["generation"] == generation
    assert {path: path.read_bytes() for path in protected_paths} == before


def test_corrupt_candidate_fails_and_same_request_recovers(
    tmp_path: Path,
    sample_content: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    job_dir = pageindex / "build" / "idx_candidate_recovery"
    request = _request(job_dir, sample_content, pageindex, mode="full")
    legacy = pageindex / "global-index.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"legacy-remains-active")
    real_validate = validator_module.validate_candidate

    def corrupt_then_validate(candidate, pageindex_dir):
        (Path(candidate) / "chunks.json").unlink()
        return real_validate(candidate, pageindex_dir)

    monkeypatch.setattr(
        validator_module,
        "validate_candidate",
        corrupt_then_validate,
    )
    assert run_worker(request) == 1
    failed = _read_json(job_dir / "result.json")
    assert failed["error_code"] == "validation_failed"
    assert "file_missing" in failed["validation_errors"]
    assert legacy.read_bytes() == b"legacy-remains-active"
    generations = pageindex / "generations"
    assert not generations.exists() or not any(generations.iterdir())

    monkeypatch.setattr(validator_module, "validate_candidate", real_validate)
    assert run_worker(request) == 0
    recovered = _read_json(job_dir / "result.json")
    assert recovered["status"] == "ready_to_publish"
    assert legacy.read_bytes() == b"legacy-remains-active"


def test_incremental_with_base_ignores_unreferenced_corrupt_segment(
    tmp_path: Path, sample_content: Path
) -> None:
    pageindex = tmp_path / "pageindex"
    full_dir = pageindex / "build" / "idx_full"
    assert run_worker(
        _request(full_dir, sample_content, pageindex, mode="full")
    ) == 0
    generation = str(_read_json(full_dir / "result.json")["generation"])

    corrupt_hash = "0" * 64
    corrupt = (
        pageindex
        / "objects"
        / "segments"
        / corrupt_hash[:2]
        / f"{corrupt_hash}.json"
    )
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"unreferenced-corrupt-object")

    incremental_dir = pageindex / "build" / "idx_incremental"
    assert run_worker(
        _request(
            incremental_dir,
            sample_content,
            pageindex,
            mode="incremental",
            base_generation=generation,
        )
    ) == 0
    incremental = _read_json(incremental_dir / "result.json")
    assert incremental["base_generation"] == generation
    assert incremental["generation"] == generation
    assert incremental["stats"]["segments_rebuilt"] == 0
    assert incremental["stats"]["segments_reused"] == 3


def test_supervisor_runs_real_development_subprocess(
    tmp_path: Path, sample_content: Path
) -> None:
    result = run_shadow_build(sample_content, tmp_path / "pageindex", "full")

    assert result["status"] == "ready_to_publish"
    assert result["worker_exit_code"] == 0


def test_supervisor_accepts_verified_mock_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    job_id, generation = _install_success_worker_mock(monkeypatch, pageindex)

    result = run_shadow_build(tmp_path / "content", pageindex, "full")

    assert result["job_id"] == job_id
    assert result["generation"] == generation
    assert result["worker_exit_code"] == 0


def test_supervisor_rejects_forged_generation_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    _install_success_worker_mock(
        monkeypatch,
        pageindex,
        result_overrides={
            "generation_dir": str((tmp_path / "forged-generation").resolve())
        },
    )

    with pytest.raises(WorkerProcessError, match="generation_dir"):
        run_shadow_build(tmp_path / "content", pageindex, "full")


def test_supervisor_rejects_forged_manifest_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    _install_success_worker_mock(
        monkeypatch,
        pageindex,
        result_overrides={"manifest_sha256": "0" * 64},
    )

    with pytest.raises(WorkerProcessError, match="manifest hash"):
        run_shadow_build(tmp_path / "content", pageindex, "full")


def test_supervisor_rejects_forged_job_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    _install_success_worker_mock(
        monkeypatch,
        pageindex,
        result_overrides={"job_id": "idx_forged"},
    )

    with pytest.raises(WorkerProcessError, match="job_id"):
        run_shadow_build(tmp_path / "content", pageindex, "full")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 999),
        ("mode", "incremental"),
        ("base_generation", "a" * 20),
    ],
)
def test_supervisor_rejects_mismatched_result_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    pageindex = tmp_path / "pageindex"
    _install_success_worker_mock(
        monkeypatch,
        pageindex,
        result_overrides={field: value},
    )

    with pytest.raises(WorkerProcessError, match=field):
        run_shadow_build(tmp_path / "content", pageindex, "full")


def test_supervisor_rejects_manifest_generation_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    _install_success_worker_mock(
        monkeypatch,
        pageindex,
        manifest_overrides={"generation": "b" * 20},
    )

    with pytest.raises(WorkerProcessError, match="manifest ID"):
        run_shadow_build(tmp_path / "content", pageindex, "full")


def test_supervisor_rejects_noncanonical_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    _install_success_worker_mock(
        monkeypatch,
        pageindex,
        noncanonical_manifest=True,
    )

    with pytest.raises(WorkerProcessError, match="not canonical"):
        run_shadow_build(tmp_path / "content", pageindex, "full")


def test_supervisor_uses_latest_shadow_as_default_incremental_base(
    tmp_path: Path, sample_content: Path
) -> None:
    pageindex = tmp_path / "pageindex"
    full = run_shadow_build(sample_content, pageindex, "full")
    generation = str(full["generation"])
    write_json_atomic(
        pageindex / "current.json",
        {"generation": "legacy-pointer-not-a-v2-generation"},
    )

    (sample_content / "papers" / "beta" / "_index.md").unlink()
    incremental = run_shadow_build(sample_content, pageindex, "incremental")

    assert incremental["base_generation"] == generation
    assert incremental["stats"]["segments_deleted"] == 1
    assert incremental["stats"]["segments_rebuilt"] == 0
    assert incremental["stats"]["segments_reused"] == 2


def test_worker_command_supports_development_and_frozen_launch(
    tmp_path: Path,
) -> None:
    request = tmp_path / "request.json"

    development = worker_command(request, executable="python", frozen=False)
    frozen = worker_command(request, executable="app.exe", frozen=True)

    assert development == [
        "python",
        "-m",
        "app.pageindex_worker",
        str(request.resolve()),
    ]
    assert frozen == [
        "app.exe",
        "--pageindex-worker",
        str(request.resolve()),
    ]


def test_run_app_routes_worker_before_importing_desktop_main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    source = (project_root / 'run_app.py').read_text(encoding='utf-8')
    worker_branch = 'sys.argv[1] == ' + chr(34) + '--pageindex-worker' + chr(34)
    assert source.index(worker_branch) < source.index('from app.main import')

    sys.modules.pop('run_app', None)
    sys.modules.pop('app.main', None)
    __import__('run_app')
    assert 'app.main' not in sys.modules


def test_setuptools_discovers_nested_app_packages() -> None:
    project_root = Path(__file__).resolve().parents[2]
    pyproject = (project_root / 'pyproject.toml').read_text(encoding='utf-8')

    assert '[tool.setuptools.packages.find]' in pyproject
    assert "include = ['app*']" in pyproject or (
        'include = [' + chr(34) + 'app*' + chr(34) + ']' in pyproject
    )
    assert 'packages = [' + chr(34) + 'app' + chr(34) + ']' not in pyproject
