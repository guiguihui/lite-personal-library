"""本机文件索引构建任务状态管理(进程内内存态)。

供 HTTP 路由层(app.http.routes_filesearch)调用:
  start_build(mode, scan_dir, index_dir) -> job_id
  get_status(job_id) -> dict
  list_jobs() -> list[dict]

实现:threading.Thread 后台跑 builder.build_full/build_incremental,
完成后写 _jobs[job_id]["result"]。单进程内存态,uvicorn 重启即丢失。
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.fileindex.builder import build_full, build_incremental, FileBuildResult

logger = logging.getLogger(__name__)


@dataclass
class FileBuildStatus:
    """单个构建任务的状态(可变,后台线程更新)。"""

    job_id: str
    status: str  # "queued" | "running" | "done" | "failed"
    mode: str  # "full" | "incremental"
    started_at: float
    scan_dir: str = ""
    index_dir: str = ""
    current_stage: str = "queued"  # "queued" | "scanning" | "building" | "saving" | "done" | "failed"
    progress: float = 0.0  # 0.0 ~ 1.0
    current_file: str = ""  # 当前正在处理的文件名
    log: list[str] = field(default_factory=list)
    error: str = ""  # 详细的错误信息(含 traceback)
    result: FileBuildResult | None = None


# 进程内 job 注册表(job_id -> FileBuildStatus)
_jobs: dict[str, FileBuildStatus] = {}
_jobs_lock = threading.Lock()


def _run_build(job_id: str, mode: str, scan_dir: str, index_dir: str) -> None:
    """后台线程:执行索引构建,逐步更新 _jobs[job_id]。

    异常处理策略:
    - builder 内部异常由 builder 自己捕获并返回 FileBuildResult(error=...)
    - 此处只捕获 builder 外部意外异常(如 import 失败、路径校验异常)
    - 所有异常都记录完整 traceback 到 status.error 和日志
    """
    def _progress_callback(stage: str, msg: str, progress: float = -1.0) -> None:
        """实时进度回调。

        参数:
            stage:     当前阶段标识(building/scanning/saving/done/failed)
            msg:       日志消息
            progress:  0.0~1.0 的进度比例,-1 表示不更新进度
        """
        with _jobs_lock:
            st = _jobs.get(job_id)
            if st is not None:
                st.current_stage = stage
                st.log.append(msg)
                if progress >= 0.0:
                    st.progress = progress
                # 从消息中提取当前文件名(格式: [N/M] 正在解析: filename)
                if "正在解析:" in msg:
                    parts = msg.split("正在解析:", 1)
                    if len(parts) == 2:
                        st.current_file = parts[1].strip()
                elif "新文件:" in msg:
                    parts = msg.split("新文件:", 1)
                    if len(parts) == 2:
                        st.current_file = parts[1].strip()
                elif "文件已变更" in msg:
                    parts = msg.split("重新索引:", 1)
                    if len(parts) == 2:
                        st.current_file = parts[1].strip()

    try:
        with _jobs_lock:
            _jobs[job_id].status = "running"
            _jobs[job_id].current_stage = "building"
            _jobs[job_id].progress = 0.0

        logger.info(
            "构建任务启动: job_id=%s, mode=%s, scan_dir=%s, index_dir=%s",
            job_id, mode, scan_dir, index_dir,
        )

        if mode == "incremental":
            result = build_incremental(scan_dir, index_dir, _progress_callback)
        else:
            result = build_full(scan_dir, index_dir, _progress_callback)

        with _jobs_lock:
            st = _jobs[job_id]
            st.status = "done" if result.ok else "failed"
            st.current_stage = "done" if result.ok else "failed"
            st.progress = 1.0 if result.ok else st.progress
            st.result = result
            st.current_file = ""
            # 用 builder 最终日志覆盖(确保完整性)
            st.log = list(result.log)
            if not result.ok and result.error:
                st.error = result.error
                logger.error(
                    "构建任务失败: job_id=%s, error=%s\n%s",
                    job_id, result.error, "\n".join(result.log),
                )
            else:
                logger.info(
                    "构建任务完成: job_id=%s, 索引=%d, 切片=%d, 耗时=%.2fs",
                    job_id, result.files_indexed, result.chunks_built,
                    result.duration_sec,
                )

    except Exception as exc:
        # 捕获 builder 外部意外异常(不应发生,但需要兜底)
        tb_str = traceback.format_exc()
        err_detail = f"{type(exc).__name__}: {exc}\n{tb_str}"
        logger.error(
            "构建任务意外异常: job_id=%s\n%s", job_id, err_detail,
        )
        with _jobs_lock:
            st = _jobs[job_id]
            st.status = "failed"
            st.current_stage = "failed"
            st.error = err_detail
            st.log.append(f"[FATAL] {err_detail}")
            st.result = FileBuildResult(
                ok=False,
                files_scanned=0,
                files_indexed=0,
                files_skipped=0,
                chunks_built=0,
                duration_sec=0.0,
                error=err_detail,
                log=(),
            )


def start_build(mode: str, scan_dir: str, index_dir: str) -> str:
    """启动后台构建任务,立即返回 job_id。

    参数:
        mode:       "full" | "incremental"
        scan_dir:   要扫描的目录(如 E:\\文档\\iSC-PPT文件)
        index_dir:  索引输出目录(如 data/fileindex)

    返回:
        job_id 字符串(格式 fsidx_xxxxxxxxxxxx)

    异常:
        不抛异常 — 即使参数无效也只记录错误,返回 job_id,
        后续 get_status() 会返回 failed 状态。
    """
    job_id = f"fsidx_{uuid.uuid4().hex[:12]}"
    with _jobs_lock:
        _jobs[job_id] = FileBuildStatus(
            job_id=job_id,
            status="queued",
            mode=mode,
            started_at=time.time(),
            scan_dir=scan_dir,
            index_dir=index_dir,
            current_stage="queued",
            progress=0.0,
            log=[],
            error="",
            result=None,
        )
    t = threading.Thread(
        target=_run_build,
        args=(job_id, mode, scan_dir, index_dir),
        daemon=True,
        name=f"fileindex-build-{job_id}",
    )
    t.start()
    logger.info("创建构建任务: job_id=%s, mode=%s", job_id, mode)
    return job_id


def get_status(job_id: str) -> dict[str, Any] | None:
    """读任务状态。返回 dict(供 HTTP 响应序列化)。

    返回 None 表示 job_id 不存在。
    """
    with _jobs_lock:
        status = _jobs.get(job_id)
        if status is None:
            return None
        result_dict: dict[str, Any] | None = None
        if status.result is not None:
            r = status.result
            result_dict = {
                "ok": r.ok,
                "files_scanned": r.files_scanned,
                "files_indexed": r.files_indexed,
                "files_skipped": r.files_skipped,
                "chunks_built": r.chunks_built,
                "duration_sec": r.duration_sec,
                "error": r.error,
                "mode": status.mode,
            }
        return {
            "job_id": status.job_id,
            "status": status.status,
            "mode": status.mode,
            "current_stage": status.current_stage,
            "progress": round(status.progress, 4),
            "current_file": status.current_file,
            "log": list(status.log),
            "error": status.error,
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
            "progress": round(s.progress, 4),
            "current_file": s.current_file,
            "error": s.error,
        }
        for s in items
    ]


def cleanup_done(max_keep: int = 20) -> int:
    """清理已完成的任务,保留最近 max_keep 条。返回清理数。"""
    with _jobs_lock:
        done_ids = [jid for jid, s in _jobs.items() if s.status in ("done", "failed")]
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
