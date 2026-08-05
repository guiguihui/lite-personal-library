"""GET /api/status — 应用状态聚合(双轨合并版)。

返回应用名称、版本、索引就绪/运行状态、入库运行状态、当前 LLM provider 等。
索引就绪语义(合并 LQ-D-desktop + norag-dev):
- legacy 轨道就绪:data/pageindex/global-index.json 存在
- V3 轨道就绪:data/pageindex/current-v3.json 存在且校验通过
- index_ready = 任一轨道就绪;index_version = "legacy"/"v3"/"both"
"""

from __future__ import annotations

from fastapi import APIRouter, Request

import app
from app.config.store import has_keyring, load_llm_config
from app.http.schemas import StatusResponse
from app.ingest import jobs as ingest_jobs
from app.index import status as index_status
from app.index.v3.runtime import CurrentViewError, load_current
from app.storage.pageindex_io import index_exists

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status", response_model=StatusResponse)
async def get_status(request: Request) -> StatusResponse:
    """聚合应用状态供前端启动页/状态栏展示。"""
    cfg = request.app.state.app_config
    llm = load_llm_config(cfg.config_dir)
    active = llm.get_active()

    index_jobs = index_status.list_jobs()
    ingest_job_list = ingest_jobs.list_jobs()

    legacy_ready = index_exists("global-index.json", cfg)
    try:
        current = load_current(cfg.pageindex_dir)
    except CurrentViewError:
        current = None
    v3_ready = current is not None

    if v3_ready and legacy_ready:
        index_version = "both"
    elif v3_ready:
        index_version = "v3"
    elif legacy_ready:
        index_version = "legacy"
    else:
        index_version = "legacy"

    return StatusResponse(
        app_name="LQ-D",
        version=app.__version__,
        index_ready=legacy_ready or v3_ready,
        index_version=index_version,
        generation=current.pin.generation if current else None,
        view_id=current.pin.view_id if current else None,
        index_running=any(j.get("status") == "running" for j in index_jobs),
        ingest_running=any(j.status == "running" for j in ingest_job_list),
        active_provider=llm.active_provider,
        model=active.model if active else "",
        has_key=bool(active.has_key) if active else False,
        keyring=has_keyring(),
    )
