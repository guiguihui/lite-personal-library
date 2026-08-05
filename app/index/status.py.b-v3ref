"""In-process job manager for PageIndex v3 builds."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from app.config.schema import BuildResult
from app.index.v3.runtime import current_parent, publish_current
from app.index.v3.supervisor import run_build
from app.knowledge.build_hook import finish_with_links


@dataclass
class BuildStatus:
    job_id: str
    status: str  # "running" | "done" | "failed"
    mode: str  # external compatibility: "full" | "incremental"
    started_at: float
    current_stage: str = "queued"
    log: list[str] = field(default_factory=list)
    result: BuildResult | None = None


_jobs: dict[str, BuildStatus] = {}
_jobs_lock = threading.Lock()


def _stage(job_id: str, stage: str, message: str | None = None) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        job.current_stage = stage
        if message:
            job.log.append(message)


def _run_build(
    job_id: str,
    mode: str,
    content_dir: str,
    pageindex_dir: str,
    _llm_model: str,
) -> None:
    started = perf_counter()
    try:
        _stage(job_id, "building", "PageIndex V3: preparing immutable build")
        parent = None if mode == "full" else current_parent(pageindex_dir)
        v3_result = run_build(
            Path(content_dir),
            Path(pageindex_dir),
            "incremental",
            parent=parent,
            legacy_export="none",
        )
        if v3_result.state not in {"ready_to_publish", "no_op"}:
            error = (
                v3_result.error.message
                if v3_result.error is not None
                else f"V3 build ended in state {v3_result.state}"
            )
            result = BuildResult(
                ok=False,
                docs_built=0,
                duration_sec=perf_counter() - started,
                error=error,
                log=(f"PageIndex V3: {v3_result.state}",),
            )
        else:
            _stage(job_id, "publishing", "PageIndex V3: publishing current view")
            current = publish_current(pageindex_dir, v3_result)
            raw = {
                "ok": True,
                "docs_built": v3_result.metrics.segments_rebuilt,
                "duration_sec": perf_counter() - started,
                "error": None,
                "log": [
                    f"PageIndex V3: {v3_result.state}",
                    f"Generation: {current.pin.generation}",
                    f"View: {current.pin.view_id}",
                    "Legacy export: disabled",
                ],
            }
            _stage(job_id, "knowledge_links", "Knowledge links: rebuilding from V3")
            result = finish_with_links(raw, content_dir, pageindex_dir)

        with _jobs_lock:
            job = _jobs[job_id]
            job.status = "done" if result.ok else "failed"
            job.current_stage = "done" if result.ok else "failed"
            job.result = result
            job.log = list(result.log)
    except Exception as exc:  # noqa: BLE001 - surfaced through job API
        result = BuildResult(
            ok=False,
            docs_built=0,
            duration_sec=perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
            log=(f"PageIndex V3 error: {type(exc).__name__}: {exc}",),
        )
        with _jobs_lock:
            job = _jobs[job_id]
            job.status = "failed"
            job.current_stage = "failed"
            job.result = result
            job.log = list(result.log)


def start_build(
    mode: str,
    content_dir: str,
    pageindex_dir: str,
    llm_model: str = "",
) -> str:
    if mode not in {"full", "incremental"}:
        raise ValueError("mode must be 'full' or 'incremental'")
    job_id = f"idx_{uuid.uuid4().hex[:12]}"
    with _jobs_lock:
        _jobs[job_id] = BuildStatus(
            job_id=job_id,
            status="running",
            mode=mode,
            started_at=time.time(),
        )
    threading.Thread(
        target=_run_build,
        args=(job_id, mode, content_dir, pageindex_dir, llm_model),
        daemon=True,
        name=f"index-build-{job_id}",
    ).start()
    return job_id


def get_status(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        status = _jobs.get(job_id)
        if status is None:
            return None
        result_dict: dict[str, Any] | None = None
        if status.result is not None:
            result_dict = {
                "ok": status.result.ok,
                "docs_built": status.result.docs_built,
                "duration_sec": status.result.duration_sec,
                "error": status.result.error,
                "mode": status.mode,
            }
        return {
            "job_id": status.job_id,
            "status": status.status,
            "current_stage": status.current_stage,
            "log": list(status.log),
            "result": result_dict,
        }


def list_jobs() -> list[dict[str, Any]]:
    with _jobs_lock:
        items = list(_jobs.values())
    items.sort(key=lambda item: item.started_at, reverse=True)
    return [
        {
            "job_id": item.job_id,
            "status": item.status,
            "mode": item.mode,
            "started_at": item.started_at,
            "current_stage": item.current_stage,
        }
        for item in items
    ]


def cleanup_done(max_keep: int = 20) -> int:
    with _jobs_lock:
        done_ids = [
            job_id
            for job_id, status in _jobs.items()
            if status.status in ("done", "failed")
        ]
        if len(done_ids) <= max_keep:
            return 0
        done_ids.sort(key=lambda job_id: _jobs[job_id].started_at, reverse=True)
        for job_id in done_ids[max_keep:]:
            del _jobs[job_id]
        return len(done_ids) - max_keep
