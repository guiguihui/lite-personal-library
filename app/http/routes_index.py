"""POST /api/index/build + GET /api/index/build/{job_id} — 索引构建任务管理。

供前端 manage.js 触发切片/索引重建(full/incremental)并轮询任务状态。
构建在后台线程跑(app.index.status),job_id 立即返回,前端轮询 GET /build/{job_id}。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.http.schemas import IndexBuildRequest, IndexBuildResponse, JobStatus
from app.index import status

router = APIRouter(prefix="/api/index", tags=["index"])


@router.post("/build", response_model=IndexBuildResponse)
async def build_index_endpoint(body: IndexBuildRequest, request: Request) -> IndexBuildResponse:
    """触发索引构建(full/incremental),立即返回 job_id。"""
    cfg = request.app.state.app_config
    job_id = status.start_build(
        mode=body.mode,
        content_dir=cfg.content_dir,
        pageindex_dir=cfg.pageindex_dir,
        llm_model=body.llm_model,
    )
    return IndexBuildResponse(job_id=job_id, status="running")


@router.get("/build/{job_id}", response_model=JobStatus)
async def get_build_status(job_id: str, request: Request) -> JobStatus:
    """轮询索引构建任务状态。"""
    data = status.get_status(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return JobStatus(
        job_id=data["job_id"],
        status=data["status"],
        current_stage=data["current_stage"],
        log=data["log"],
        result=data["result"],
    )


@router.get("/jobs", response_model=list[dict])
async def list_jobs(request: Request) -> list[dict]:
    """列所有构建任务(调试用)。"""
    return status.list_jobs()
