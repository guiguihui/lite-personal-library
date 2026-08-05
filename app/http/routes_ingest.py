"""POST /api/ingest/* + GET /api/ingest/{job_id} — 入库流水线任务管理。

端点:
  POST /extract  — 仅提取阶段(create_job + 后台跑 extract)
  POST /translate — 仅翻译阶段
  POST /validate — 仅验证阶段
  POST /full     — 完整流水线(extract→clean→translate→validate→note)
  GET  /{job_id}  — 轮询任务状态
  GET  /jobs     — 列所有任务(调试用)

后台线程跑 app.ingest.pipeline.run_pipeline,job_id 立即返回,
前端轮询 GET /{job_id}。
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Request

from app.http.schemas import (
    IngestExtractRequest,
    IngestFullRequest,
    IngestRecleanRequest,
    IngestResponse,
    IngestTranslateRequest,
    IngestValidateRequest,
    JobStatus,
)
from app.ingest.jobs import create_job, get_job, list_jobs, update_job
from app.ingest.pipeline import run_pipeline

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


def _start_pipeline_thread(job_id: str, request: Request) -> None:
    """启动后台线程跑 run_pipeline。"""
    cfg = request.app.state.app_config
    t = threading.Thread(
        target=run_pipeline,
        args=(job_id, cfg),
        daemon=True,
        name=f"ingest-{job_id}",
    )
    t.start()


def _run_reclean_thread(job_id: str, cfg) -> None:
    """后台线程:对已入库书重跑 clean + 触发增量 build。

    不走 run_pipeline(reclean 不是 stage 流水线),直接调 run_reclean。
    build 用 start_build 后台触发(不阻塞),与 pipeline 收尾一致。
    """
    from app.ingest.clean_adapter import run_reclean
    from app.ingest.jobs import get_job as _get_job
    from app.index.status import start_build

    job = _get_job(job_id)
    if job is None:
        return
    try:
        result = run_reclean(job, cfg)
        # 触发增量 build(content/ 文件 MD5 变 → 增量检测 → 重建索引)
        try:
            start_build("incremental", cfg.content_dir, cfg.pageindex_dir, "")
        except Exception as exc:  # noqa: BLE001 — build 失败不影响 reclean 结果
            from app.ingest.jobs import append_log
            append_log(job_id, f"[reclean] build trigger failed: {exc}")
        update_job(job_id, status="done", result=result)
    except Exception as exc:  # noqa: BLE001
        from app.ingest.jobs import append_log
        append_log(job_id, f"[reclean] failed: {exc}")
        update_job(job_id, status="failed", result={"error": str(exc)})


def _start_reclean_thread(job_id: str, request: Request) -> None:
    """启动后台线程跑 reclean。"""
    cfg = request.app.state.app_config
    t = threading.Thread(
        target=_run_reclean_thread,
        args=(job_id, cfg),
        daemon=True,
        name=f"reclean-{job_id}",
    )
    t.start()


def _job_to_status(job) -> JobStatus:
    """IngestJob → JobStatus 响应模型。"""
    return JobStatus(
        job_id=job.job_id,
        status=job.status,
        current_stage=job.current_stage,
        log=list(job.log),
        result=job.result,
    )


@router.post("/extract", response_model=IngestResponse)
async def extract_endpoint(body: IngestExtractRequest, request: Request) -> IngestResponse:
    """触发提取阶段(仅 extract)。"""
    # 强制 stages 只含 extract
    body.stages = ["extract"]
    job_id = create_job(body)
    _start_pipeline_thread(job_id, request)
    return IngestResponse(job_id=job_id, status="running")


@router.post("/translate", response_model=IngestResponse)
async def translate_endpoint(body: IngestTranslateRequest, request: Request) -> IngestResponse:
    """触发翻译阶段(translate only)。

    需要 slug 对应的 merged/book.md 已存在(由 extract 阶段产出)。
    """
    # 构造 extract request 复用 create_job,但 stages 只含 translate
    extract_req = IngestExtractRequest(
        input_pdf="",  # translate 不需要 input_pdf
        doc_type=body.doc_type,
        slug=body.slug,
        pages=None,
        strategy=None,
        stages=["translate"],
    )
    job_id = create_job(extract_req)
    # translate 阶段需要 prev_result(merged_path),由 adapter 从 pdfs_dir/slug/ 读
    _start_pipeline_thread(job_id, request)
    return IngestResponse(job_id=job_id, status="running")


@router.post("/validate", response_model=IngestResponse)
async def validate_endpoint(body: IngestValidateRequest, request: Request) -> IngestResponse:
    """触发验证阶段(validate only)。"""
    extract_req = IngestExtractRequest(
        input_pdf="",
        doc_type=body.doc_type,
        slug=body.slug,
        pages=None,
        strategy=None,
        stages=["validate"],
    )
    job_id = create_job(extract_req)
    _start_pipeline_thread(job_id, request)
    return IngestResponse(job_id=job_id, status="running")


@router.post("/full", response_model=IngestResponse)
async def full_endpoint(body: IngestFullRequest, request: Request) -> IngestResponse:
    """触发完整入库流水线(extract→clean→translate→validate→note)。

    body.stages 为空时按 doc_type 选默认阶段序列。
    """
    extract_req = IngestExtractRequest(
        input_pdf=body.input_pdf,
        doc_type=body.doc_type,
        slug=body.slug,
        pages=body.pages,
        strategy=body.strategy,
        stages=body.stages,
        title=body.title,
        author=body.author,
        tags=body.tags,
    )
    job_id = create_job(extract_req)
    _start_pipeline_thread(job_id, request)
    return IngestResponse(job_id=job_id, status="running")


@router.post("/reclean", response_model=IngestResponse)
async def reclean_endpoint(body: IngestRecleanRequest, request: Request) -> IngestResponse:
    """对已入库书重跑 clean 阶段(修复伪标题等)。

    直接对 content/ 下的正文文件跑 clean_markdown.clean(),写回,
    触发增量 build(MD5 变 → 重建索引)。用于已入库但标题层级有问题的书。
    """
    extract_req = IngestExtractRequest(
        input_pdf="",
        doc_type=body.doc_type,
        slug=body.slug,
        pages=None,
        strategy=None,
        stages=("reclean",),  # 标记为 reclean 任务(不走 pipeline stage 路由)
    )
    job_id = create_job(extract_req)
    _start_reclean_thread(job_id, request)
    return IngestResponse(job_id=job_id, status="running")


@router.get("/jobs", response_model=list[dict])
async def list_jobs_endpoint(request: Request) -> list[dict]:
    """列所有入库任务(调试用)。"""
    jobs = list_jobs()
    return [
        {
            "job_id": j.job_id,
            "status": j.status,
            "current_stage": j.current_stage,
            "doc_type": j.doc_type,
            "slug": j.slug,
            "started_at": j.started_at,
            "stages": list(j.stages),
        }
        for j in jobs
    ]


@router.get("/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str, request: Request) -> JobStatus:
    """轮询入库任务状态。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return _job_to_status(job)
