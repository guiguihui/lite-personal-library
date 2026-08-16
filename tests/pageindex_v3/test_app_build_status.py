from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.schema import BuildResult
from app.index import status


def test_application_build_uses_v3_without_legacy_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}
    job_id = "idx_test"
    status._jobs[job_id] = status.BuildStatus(job_id, "running", "incremental", 0)
    parent = object()
    monkeypatch.setattr(status, "current_parent", lambda _path: parent)

    def fake_run(content, pageindex, mode, **kwargs):
        observed.update(
            content=content,
            pageindex=pageindex,
            mode=mode,
            **kwargs,
        )
        return SimpleNamespace(
            state="ready_to_publish",
            metrics=SimpleNamespace(segments_rebuilt=1),
        )

    monkeypatch.setattr(status, "run_build", fake_run)
    monkeypatch.setattr(
        status,
        "publish_current",
        lambda *_args: SimpleNamespace(
            pin=SimpleNamespace(generation="generation", view_id="view")
        ),
    )
    monkeypatch.setattr(
        status,
        "finish_with_links",
        lambda raw, *_args: BuildResult(
            ok=True,
            docs_built=raw["docs_built"],
            duration_sec=raw["duration_sec"],
            log=tuple(raw["log"]),
        ),
    )

    status._run_build(job_id, "incremental", "content", "pageindex", "ignored")

    assert observed["mode"] == "incremental"
    assert observed["parent"] is parent
    assert observed["legacy_export"] == "none"
    assert status.get_status(job_id)["status"] == "done"
    status._jobs.pop(job_id, None)
