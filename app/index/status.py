"""索引构建任务状态管理(进程内内存态)——双轨合并版。

合并 LQ-D-desktop(legacy 单体索引)与 norag-dev(PageIndex V3)两套构建:
- legacy 轨道:  app.index.builder.build_full/build_incremental,产出
  global-index/node-index/inverted-index/chunks.json,供 library 阅读与
  /pageindex 兼容读取面使用(行为与 LQ-D-desktop 完全一致)。
- V3 轨道:  app.index.v3.supervisor.run_build → publish_current →
  finish_with_links,产出 data/pageindex/current-v3.json + 不可变
  Generation/View + link-index.json,供 /api/search(聊天)与知识链接使用。

两条轨道在一个 job 内并行执行;结果合并为一个 BuildResult:
- legacy ok 且 V3 ok            → done(带 V3 generation/view 信息)
- 任一失败                     → failed(另一轨结果记录在 log)
- 状态通过 current_stage 区分轨道进度。

实现:threading.Thread 后台并行跑两条轨道,完成后写 _jobs[job_id]["result"]。
单进程内存态,uvicorn 重启即丢失(够用于桌面端)。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config.schema import BuildResult
from app.index.builder import build_full, build_incremental
from app.index.v3.runtime import current_parent, publish_current
from app.index.v3.supervisor import run_build
from app.knowledge.build_hook import finish_with_links


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


def _append_log(job_id: str, message: str) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].log.append(message)


def _set_stage(job_id: str, stage: str, message: str | None = None) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].current_stage = stage
            if message:
                _jobs[job_id].log.append(message)


def _run_legacy_track(
    job_id: str,
    mode: str,
    content_dir: str,
    pageindex_dir: str,
    llm_model: str,
) -> BuildResult:
    """legacy 轨道:现有 4 文件索引构建,行为与 LQ-D-desktop 完全一致。"""
    _set_stage(job_id, "legacy", "Legacy: building monolith index (global/node/inverted/chunks)")
    if mode == "incremental":
        return build_incremental(content_dir, pageindex_dir, llm_model)
    return build_full(content_dir, pageindex_dir, llm_model)


def _run_v3_track(
    mode: str,
    content_dir: str,
    pageindex_dir: str,
    started: float,
    job_id: str,
) -> BuildResult:
    """V3 轨道:不可变 Generation/View 构建 + 原子发布 + 知识链接。"""
    _set_stage(job_id, "v3", "PageIndex V3: preparing immutable build")
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
        return BuildResult(
            ok=False,
            docs_built=0,
            duration_sec=time.perf_counter() - started,
            error=error,
            log=(f"PageIndex V3: {v3_result.state}",),
        )
    _set_stage(job_id, "v3_publish", "PageIndex V3: publishing current view")
    current = publish_current(pageindex_dir, v3_result)
    raw = {
        "ok": True,
        "docs_built": v3_result.metrics.segments_rebuilt,
        "duration_sec": time.perf_counter() - started,
        "error": None,
        "log": [
            f"PageIndex V3: {v3_result.state}",
            f"Generation: {current.pin.generation}",
            f"View: {current.pin.view_id}",
            "Legacy export: disabled (V3 native)",
        ],
    }
    _set_stage(job_id, "v3_links", "Knowledge links: rebuilding from V3")
    return finish_with_links(raw, content_dir, pageindex_dir)


def _merge_results(
    legacy: BuildResult,
    v3: BuildResult,
    duration_sec: float,
    mode: str,
) -> BuildResult:
    """合并两条轨道结果。任一失败 → failed;否则 done。"""
    combined_log = list(legacy.log) + list(v3.log)
    if not legacy.ok and not v3.ok:
        return BuildResult(
            ok=False,
            docs_built=legacy.docs_built,
            duration_sec=duration_sec,
            error=f"both tracks failed: legacy={legacy.error}; v3={v3.error}",
            log=tuple(combined_log),
        )
    if not legacy.ok:
        return BuildResult(
            ok=False,
            docs_built=v3.docs_built,
            duration_sec=duration_sec,
            error=f"legacy track failed: {legacy.error}",
            log=tuple(combined_log),
        )
    if not v3.ok:
        return BuildResult(
            ok=False,
            docs_built=legacy.docs_built,
            duration_sec=duration_sec,
            error=f"v3 track failed: {v3.error}",
            log=tuple(combined_log),
        )
    return BuildResult(
        ok=True,
        docs_built=max(legacy.docs_built, v3.docs_built),
        duration_sec=duration_sec,
        error=None,
        log=tuple(combined_log),
    )


def _run_build(
    job_id: str,
    mode: str,
    content_dir: str,
    pageindex_dir: str,
    llm_model: str,
) -> None:
    """后台线程:并行执行 legacy + V3 两条轨道,合并结果。"""
    started = time.perf_counter()
    try:
        with _jobs_lock:
            _jobs[job_id].status = "running"
            _jobs[job_id].current_stage = "building"

        # 并行执行两条轨道(线程隔离,互不干扰)
        legacy_result: BuildResult | None = None
        v3_result: BuildResult | None = None
        errors: list[str] = []

        def _legacy_worker() -> None:
            nonlocal legacy_result
            try:
                legacy_result = _run_legacy_track(job_id, mode, content_dir, pageindex_dir, llm_model)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"legacy track raised: {type(exc).__name__}: {exc}")

        def _v3_worker() -> None:
            nonlocal v3_result
            try:
                v3_result = _run_v3_track(mode, content_dir, pageindex_dir, started, job_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"v3 track raised: {type(exc).__name__}: {exc}")

        legacy_thread = threading.Thread(target=_legacy_worker, daemon=True, name=f"legacy-{job_id}")
        v3_thread = threading.Thread(target=_v3_worker, daemon=True, name=f"v3-{job_id}")
        legacy_thread.start()
        v3_thread.start()
        legacy_thread.join()
        v3_thread.join()

        # 轨道线程内异常(未吞)补成失败结果
        if legacy_result is None:
            legacy_result = BuildResult(
                ok=False,
                docs_built=0,
                duration_sec=0.0,
                error=errors[0] if errors else "legacy track produced no result",
                log=(),
            )
        if v3_result is None:
            v3_result = BuildResult(
                ok=False,
                docs_built=0,
                duration_sec=0.0,
                error=errors[-1] if errors else "v3 track produced no result",
                log=(),
            )

        result = _merge_results(legacy_result, v3_result, time.perf_counter() - started, mode)

        with _jobs_lock:
            _jobs[job_id].status = "done" if result.ok else "failed"
            _jobs[job_id].current_stage = "done" if result.ok else "failed"
            _jobs[job_id].result = result
            # 追加而非覆盖:保留构建期间 _set_stage 写入的进度日志(legacy/v3/v3_publish/
            # v3_links),再把合并结果摘要追加在后,供事后审计。
            _jobs[job_id].log.extend(list(result.log))
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
    cleanup_done()  # 有界保留已完成任务,防止 _jobs 长期无界增长
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
        # 锁内读取可变字段并构建完整快照,避免后台线程并发修改读到中间态。
        items = [
            {
                "job_id": s.job_id,
                "status": s.status,
                "mode": s.mode,
                "started_at": s.started_at,
                "current_stage": s.current_stage,
            }
            for s in _jobs.values()
        ]
    items.sort(key=lambda s: s["started_at"], reverse=True)
    return items


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
