"""索引构建任务状态管理(进程内内存态)。

供 HTTP 路由层(app.http.routes_index)调用:
  start_build(mode, content_dir, pageindex_dir, llm_model) -> job_id
  get_status(job_id) -> dict

实现:threading.Thread 后台跑 builder.build_full/build_incremental,
完成后写 _jobs[job_id]["result"]。单进程内存态,uvicorn 重启即丢失(够用于桌面端)。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config.schema import BuildResult
from app.index.builder import build_full, build_incremental


@dataclass
class BuildStatus:
    """单个构建任务的状态(可变,后台线程更新)。"""

    job_id: str
    status: str  # "running" | "done" | "failed"
    mode: str  # "full" | "incremental"
    started_at: float
    current_stage: str = "queued"
    log: list[str] = field(default_factory=list)
    result: BuildResult | None = None


# 进程内 job 注册表(job_id -> BuildStatus)
_jobs: dict[str, BuildStatus] = {}
_jobs_lock = threading.Lock()


def _run_build(job_id: str, mode: str, content_dir: str, pageindex_dir: str, llm_model: str) -> None:
    """后台线程:执行索引构建,逐步更新 _jobs[job_id]。"""
    try:
        with _jobs_lock:
            _jobs[job_id].status = "running"
            _jobs[job_id].current_stage = "building"

        if mode == "incremental":
            result = build_incremental(content_dir, pageindex_dir, llm_model)
        else:
            result = build_full(content_dir, pageindex_dir, llm_model)

        with _jobs_lock:
            _jobs[job_id].status = "done" if result.ok else "failed"
            _jobs[job_id].current_stage = "done" if result.ok else "failed"
            _jobs[job_id].result = result
            _jobs[job_id].log = list(result.log)
    except Exception as exc:  # noqa: BLE001
        with _jobs_lock:
            _jobs[job_id].status = "failed"
            _jobs[job_id].current_stage = "failed"
            _jobs[job_id].log.append(f"[status error] {type(exc).__name__}: {exc}")
            _jobs[job_id].result = BuildResult(
                ok=False,
                docs_built=0,
                duration_sec=0.0,
                error=f"{type(exc).__name__}: {exc}",
                log=(),
            )


def start_build(
    mode: str,
    content_dir: str,
    pageindex_dir: str,
    llm_model: str = "",
) -> str:
    """启动后台构建任务,立即返回 job_id。

    mode:           "full" | "incremental"
    content_dir:    内容根(data/content)
    pageindex_dir:  输出根(data/pageindex)
    llm_model:       litellm 模型串;"" = 本地退化
    """
    job_id = f"idx_{uuid.uuid4().hex[:12]}"
    with _jobs_lock:
        _jobs[job_id] = BuildStatus(
            job_id=job_id,
            status="running",
            mode=mode,
            started_at=time.time(),
            current_stage="queued",
            log=[],
            result=None,
        )
    t = threading.Thread(
        target=_run_build,
        args=(job_id, mode, content_dir, pageindex_dir, llm_model),
        daemon=True,
        name=f"index-build-{job_id}",
    )
    t.start()
    return job_id


def get_status(job_id: str) -> dict[str, Any] | None:
    """读任务状态。返回 dict(供 JobStatus 响应模型序列化)。"""
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
    """列所有任务(供调试/管理 UI)。按 started_at 倒序。"""
    with _jobs_lock:
        items = list(_jobs.values())
    items.sort(key=lambda s: s.started_at, reverse=True)
    return [
        {
            "job_id": s.job_id,
            "status": s.status,
            "mode": s.mode,
            "started_at": s.started_at,
            "current_stage": s.current_stage,
        }
        for s in items
    ]


def cleanup_done(max_keep: int = 20) -> int:
    """清理已完成的任务,保留最近 max_keep 条。返回清理数。"""
    with _jobs_lock:
        done_ids = [jid for jid, s in _jobs.items() if s.status in ("done", "failed")]
        # 保留最近 max_keep 条已完成的,其余删除
        if len(done_ids) <= max_keep:
            return 0
        done_ids_sorted = sorted(
            done_ids,
            key=lambda jid: _jobs[jid].started_at,
            reverse=True,
        )
        to_remove = done_ids_sorted[max_keep:]
        for jid in to_remove:
            del _jobs[jid]
        return len(to_remove)
