"""入库任务状态管理(进程内内存态,线程安全)。

供 HTTP 路由层(app.http.routes_ingest)调用:
  create_job(req) -> job_id
  get_job(job_id) -> IngestJob | None
  list_jobs() -> list[IngestJob]
  update_job(job_id, **fields) -> None (线程安全)

实现:threading.Lock 保护 _jobs dict。单进程内存态,uvicorn 重启即丢失
(够用于桌面端)。与 app.index.status 同模式。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.http.schemas import IngestExtractRequest


@dataclass
class IngestJob:
    """单个入库任务的状态(可变,后台线程更新)。

    stages: 要执行的阶段列表,如 ["extract","clean","translate","validate"]
    log: 逐步追加的日志行
    result: 完成后的汇总 dict(各阶段产物路径 + stats)
    """

    job_id: str
    status: str  # "running" | "done" | "failed"
    started_at: float = 0.0
    input_pdf: str = ""
    doc_type: str = ""  # "book" | "paper" | "note"
    slug: str = ""
    current_stage: str = "queued"
    pages: str | None = None
    strategy: str | None = None
    stages: tuple[str, ...] = field(default_factory=tuple)
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    # 可选元数据(publish 阶段写 _index.md front matter)
    title: str = ""
    author: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


# 进程内 job 注册表
_jobs: dict[str, IngestJob] = {}
_jobs_lock = threading.Lock()


def _default_stages(doc_type: str) -> tuple[str, ...]:
    """按 doc_type 返回默认阶段序列。"""
    if doc_type == "book":
        return ("extract", "clean", "translate", "validate")
    if doc_type == "paper":
        return ("extract", "clean", "translate", "validate", "note")
    if doc_type == "note":
        return ("extract", "clean", "validate")
    return ("extract", "clean", "validate")


def create_job(req: IngestExtractRequest) -> str:
    """创建入库任务,返回 job_id。

    req.stages 为空时按 doc_type 选默认阶段序列。
    """
    job_id = f"ing_{uuid.uuid4().hex[:12]}"
    stages = tuple(req.stages) if req.stages else _default_stages(req.doc_type)
    job = IngestJob(
        job_id=job_id,
        status="running",
        current_stage="queued",
        started_at=time.time(),
        input_pdf=req.input_pdf,
        doc_type=req.doc_type,
        slug=req.slug,
        pages=req.pages,
        strategy=req.strategy,
        stages=stages,
        log=[],
        result=None,
        title=req.title or "",
        author=req.author or "",
        tags=tuple(req.tags) if req.tags else (),
    )
    with _jobs_lock:
        _jobs[job_id] = job
    return job_id


def get_job(job_id: str) -> IngestJob | None:
    """读任务。返回 IngestJob(调用方负责序列化)。"""
    with _jobs_lock:
        return _jobs.get(job_id)


def list_jobs() -> list[IngestJob]:
    """列所有任务,按 started_at 倒序。"""
    with _jobs_lock:
        items = list(_jobs.values())
    items.sort(key=lambda j: j.started_at, reverse=True)
    return items


def update_job(job_id: str, **fields: Any) -> None:
    """线程安全更新任务字段。

    常用:status, current_stage, log(append 用 log=job.log + [line]),
    result。
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        for k, v in fields.items():
            if k == "log":
                # log 特殊处理:追加而非替换
                job.log.extend(v if isinstance(v, list) else [v])
            else:
                setattr(job, k, v)


def append_log(job_id: str, line: str) -> None:
    """追加一行日志(便捷方法)。"""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.log.append(line)


def cleanup_done(max_keep: int = 20) -> int:
    """清理已完成的任务,保留最近 max_keep 条。返回清理数。"""
    with _jobs_lock:
        done_ids = [jid for jid, j in _jobs.items() if j.status in ("done", "failed")]
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
