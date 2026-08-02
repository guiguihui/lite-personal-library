from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.index.v3.delta_store import load_delta_object_metadata
from app.index.v3.protocol import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    BuildRequest,
    BuildResult,
    GenerationAttestation,
    ParentAttestation,
    ViewAttestation,
    WorkerError,
    WorkerMetrics,
    decode_request_line,
    decode_result_line,
    encode_result_line,
)
from app.index.v3.supervisor import (
    WorkerProcessError,
    run_build,
    verify_worker_completion,
    worker_command,
)
from app.index.v3.view_store import load_search_view_metadata
from app.index.v3.worker import execute_request, run_worker


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _corpus(root: Path, *, token: str = "alpha") -> Path:
    content = root / "content"
    _write(
        content / "notes" / "welcome.md",
        "---\ntitle: Welcome\n---\n# Welcome\n"
        f"{token} body token\n",
    )
    return content


def _parent(result: BuildResult) -> ParentAttestation:
    assert result.generation is not None
    assert result.view is not None
    return ParentAttestation(result.generation, result.view)


def _request(
    content: Path,
    pageindex: Path,
    job_id: str,
) -> BuildRequest:
    return BuildRequest(
        protocol=PROTOCOL_NAME,
        protocol_version=PROTOCOL_VERSION,
        job_id=job_id,
        mode="incremental",
        content_dir=content,
        pageindex_dir=pageindex,
        parent=None,
        legacy_export="none",
    )


def _completed(command: list[str], returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")


def test_worker_command_selects_development_and_frozen_entrypoints(
    tmp_path: Path,
) -> None:
    request = tmp_path / "request.json"

    assert worker_command(
        request, executable="python-test", frozen=False
    ) == [
        "python-test",
        "-m",
        "app.pageindex_v3_worker",
        str(request.resolve()),
    ]
    assert worker_command(
        request, executable="library.exe", frozen=True
    ) == [
        "library.exe",
        "--pageindex-v3-worker",
        str(request.resolve()),
    ]


def test_run_app_routes_p3_worker_before_importing_desktop_main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    source = (project_root / "run_app.py").read_text(encoding="utf-8")
    worker_branch = 'sys.argv[1] == "--pageindex-v3-worker"'
    assert source.index(worker_branch) < source.index("from app.main import")

    sys.modules.pop("run_app", None)
    sys.modules.pop("app.main", None)
    __import__("run_app")
    assert "app.main" not in sys.modules


@pytest.mark.parametrize("field", ["job_id", "mode", "legacy_export", "parent"])
def test_public_verifier_binds_every_result_identity_field(
    tmp_path: Path,
    field: str,
) -> None:
    pageindex = (tmp_path / "pageindex").resolve()
    request = _request(tmp_path / "content", pageindex, "idx_trusted")
    fake_parent = ParentAttestation(
        GenerationAttestation(
            "1" * 64,
            pageindex / "generations" / ("1" * 64),
            "2" * 64,
        ),
        ViewAttestation(
            "3" * 64,
            pageindex / "views" / ("3" * 64),
            "4" * 64,
            "1" * 64,
            "2" * 64,
        ),
    )
    values: dict[str, object] = {
        "protocol": PROTOCOL_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "job_id": request.job_id,
        "mode": request.mode,
        "legacy_export": request.legacy_export,
        "state": "failed",
        "parent": request.parent,
        "generation": None,
        "view": None,
        "legacy_export_artifact": None,
        "metrics": WorkerMetrics.empty(),
        "error": WorkerError("build_failed", "expected test failure"),
    }
    values[field] = {
        "job_id": "idx_other",
        "mode": "optimize",
        "legacy_export": "full",
        "parent": fake_parent,
    }[field]
    result = BuildResult(**values)  # type: ignore[arg-type]

    with pytest.raises(WorkerProcessError, match=field):
        verify_worker_completion(result, request, 1)


def test_real_fresh_process_bootstrap_and_explicit_parent_no_op(
    tmp_path: Path,
) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    _write(pageindex / "current.json", "not a P3 parent pointer")

    bootstrap = run_build(content, pageindex, "incremental")
    no_op = run_build(
        content,
        pageindex,
        "incremental",
        parent=_parent(bootstrap),
    )

    assert bootstrap.state == "ready_to_publish"
    assert no_op.state == "no_op"
    assert no_op.generation == bootstrap.generation
    assert no_op.view == bootstrap.view
    requests = sorted((pageindex / "build").glob("*/request.json"))
    assert len(requests) == 2
    parsed = [decode_request_line(path.read_bytes()) for path in requests]
    assert sum(request.parent is None for request in parsed) == 1
    assert sum(request.parent is not None for request in parsed) == 1


def test_real_fresh_process_incremental_and_optimize_lineage(
    tmp_path: Path,
) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    bootstrap = run_build(content, pageindex, "incremental")
    _write(
        content / "notes" / "welcome.md",
        "---\ntitle: Welcome\n---\n# Welcome\nchanged body\n",
    )

    incremental = run_build(
        content,
        pageindex,
        "incremental",
        parent=_parent(bootstrap),
    )
    optimized = run_build(
        content,
        pageindex,
        "optimize",
        parent=_parent(incremental),
    )

    assert incremental.state == "ready_to_publish"
    assert optimized.state == "ready_to_publish"
    assert optimized.generation == incremental.generation
    assert optimized.view != incremental.view


def test_missing_result_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)

    def fake_run(command: list[str], **_kwargs: object):
        return _completed(command, 0)

    monkeypatch.setattr("app.index.v3.supervisor.subprocess.run", fake_run)

    with pytest.raises(WorkerProcessError, match="without result.json"):
        run_build(content, tmp_path / "pageindex", "incremental")


def test_worker_exit_code_must_match_result_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)

    def fake_run(command: list[str], **_kwargs: object):
        assert run_worker(Path(command[-1])) == 0
        return _completed(command, 1)

    monkeypatch.setattr("app.index.v3.supervisor.subprocess.run", fake_run)

    with pytest.raises(WorkerProcessError, match="exit code"):
        run_build(content, tmp_path / "pageindex", "incremental")


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("generation/input-proof.json", "Generation input proof"),
        ("view/statistics.json", "View statistics"),
        ("view/documents.json", "View documents"),
    ],
)
def test_control_artifact_hashes_are_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    message: str,
) -> None:
    content = _corpus(tmp_path)

    def fake_run(command: list[str], **_kwargs: object):
        request_path = Path(command[-1])
        assert run_worker(request_path) == 0
        request = decode_request_line(request_path.read_bytes())
        result = decode_result_line(
            (request_path.parent / "result.json").read_bytes(),
            request=request,
        )
        assert result.generation is not None
        assert result.view is not None
        kind, name = relative_path.split("/", 1)
        root = (
            result.generation.generation_dir
            if kind == "generation"
            else result.view.view_dir
        )
        artifact = root / name
        artifact.write_bytes(artifact.read_bytes() + b"\n")
        return _completed(command, 0)

    monkeypatch.setattr("app.index.v3.supervisor.subprocess.run", fake_run)

    with pytest.raises(WorkerProcessError, match=message):
        run_build(content, tmp_path / "pageindex", "incremental")


def test_supervisor_rejects_new_delta_layer_modified_after_worker_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    content = _corpus(tmp_path)
    bootstrap = run_build(content, pageindex, "incremental")
    parent = _parent(bootstrap)
    _write(
        content / "notes" / "welcome.md",
        "# Welcome\nchanged after bootstrap\n",
    )

    def fake_run(command: list[str], **_kwargs: object):
        request_path = Path(command[-1])
        assert run_worker(request_path) == 0
        request = decode_request_line(request_path.read_bytes())
        result = decode_result_line(
            (request_path.parent / "result.json").read_bytes(),
            request=request,
        )
        assert result.view is not None
        view = load_search_view_metadata(
            request.pageindex_dir,
            result.view.view_id,
        )
        delta = load_delta_object_metadata(
            request.pageindex_dir,
            view.delta_ids[-1],
        )
        postings = delta.root / delta.layer.postings.relative_path
        payload = bytearray(postings.read_bytes())
        payload[-1] ^= 1
        postings.write_bytes(payload)
        return _completed(command, 0)

    monkeypatch.setattr(
        "app.index.v3.supervisor.subprocess.run",
        fake_run,
    )
    with pytest.raises(WorkerProcessError, match="Delta layer artifacts"):
        run_build(
            content,
            pageindex,
            "incremental",
            parent=parent,
        )

def test_forged_generation_manifest_hash_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)

    def fake_run(command: list[str], **_kwargs: object):
        request_path = Path(command[-1])
        assert run_worker(request_path) == 0
        request = decode_request_line(request_path.read_bytes())
        result = decode_result_line(
            (request_path.parent / "result.json").read_bytes(),
            request=request,
        )
        assert result.generation is not None
        manifest = result.generation.generation_dir / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        return _completed(command, 0)

    monkeypatch.setattr("app.index.v3.supervisor.subprocess.run", fake_run)

    with pytest.raises(WorkerProcessError, match="Generation manifest hash"):
        run_build(content, tmp_path / "pageindex", "incremental")


def test_self_consistent_result_that_does_not_extend_parent_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    content = _corpus(tmp_path / "parent", token="parent")
    parent_result = run_build(content, pageindex, "incremental")
    parent = _parent(parent_result)

    alternate_content = _corpus(tmp_path / "alternate", token="alternate")
    alternate = execute_request(
        _request(alternate_content, pageindex, "idx_alternate_bootstrap")
    )
    assert alternate.state == "ready_to_publish"
    assert alternate.generation is not None
    assert alternate.view is not None

    def fake_run(command: list[str], **_kwargs: object):
        request_path = Path(command[-1])
        request = decode_request_line(request_path.read_bytes())
        forged = BuildResult(
            protocol=PROTOCOL_NAME,
            protocol_version=PROTOCOL_VERSION,
            job_id=request.job_id,
            mode=request.mode,
            legacy_export=request.legacy_export,
            state="ready_to_publish",
            parent=request.parent,
            generation=alternate.generation,
            view=alternate.view,
            legacy_export_artifact=None,
            metrics=alternate.metrics,
            error=None,
        )
        (request_path.parent / "result.json").write_bytes(
            encode_result_line(forged)
        )
        return _completed(command, 0)

    monkeypatch.setattr("app.index.v3.supervisor.subprocess.run", fake_run)
    _write(content / "notes" / "welcome.md", "# changed\nchanged body\n")

    with pytest.raises(WorkerProcessError, match="incremental lineage"):
        run_build(
            content,
            pageindex,
            "incremental",
            parent=parent,
        )


def test_explicit_legacy_export_manifest_hash_is_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _corpus(tmp_path)

    def fake_run(command: list[str], **_kwargs: object):
        request_path = Path(command[-1])
        assert run_worker(request_path) == 0
        request = decode_request_line(request_path.read_bytes())
        result = decode_result_line(
            (request_path.parent / "result.json").read_bytes(),
            request=request,
        )
        assert result.legacy_export_artifact is not None
        manifest = result.legacy_export_artifact.export_dir / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        return _completed(command, 0)

    monkeypatch.setattr("app.index.v3.supervisor.subprocess.run", fake_run)

    with pytest.raises(WorkerProcessError, match="legacy manifest hash"):
        run_build(
            content,
            tmp_path / "pageindex",
            "incremental",
            legacy_export="full",
        )
