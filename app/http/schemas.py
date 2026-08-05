"""Pydantic 请求/响应模型。

所有 HTTP 端点的请求体/响应体 schema 集中在此,供 routes_* 引用。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class IndexBuildRequest(BaseModel):
    mode: str = Field(default="incremental", pattern="^(full|incremental)$")
    llm_model: str = ""


class IndexBuildResponse(BaseModel):
    job_id: str
    status: str


class SettingsResponse(BaseModel):
    active_provider: str
    providers: dict[str, dict[str, Any]]  # {name: {model, base_url, has_key}}
    remember_key: bool
    use_llm_proxy: bool
    has_keyring: bool


class SettingsUpdate(BaseModel):
    key: str  # "api_key" | "active_provider" | "model" | "base_url" | "remember_key" | "use_llm_proxy"
    value: Any
    provider: str | None = None  # api_key/model/base_url 操作的目标 provider


class SettingsUpdateResponse(BaseModel):
    ok: bool


class IngestExtractRequest(BaseModel):
    input_pdf: str
    doc_type: str = Field(pattern="^(book|paper|note)$")
    slug: str
    pages: str | None = None
    strategy: str | None = None  # "local" | "mineru",None 用 AppConfig.pdf_strategy
    stages: list[str] | None = None  # 自定义阶段序列;None 按 doc_type 选默认
    # 可选元数据(由 /full 透传,publish 阶段写 _index.md front matter)
    title: str | None = None
    author: str | None = None
    tags: list[str] | None = None


class IngestFullRequest(BaseModel):
    """完整入库流水线请求(POST /api/ingest/full)。

    title/author/tags 为可选自定义元数据(向后兼容,老请求不带这些字段)。
    提取完成后由 extract_adapter 写入 front matter,覆盖提取器返回值。
    """

    input_pdf: str
    doc_type: str = Field(pattern="^(book|paper|note)$")
    slug: str
    pages: str | None = None
    strategy: str | None = None
    stages: list[str] | None = None
    title: str | None = None
    author: str | None = None
    tags: list[str] | None = None


class AppConfigUpdate(BaseModel):
    """应用配置更新请求(PUT /api/app/config)。

    所有字段 Optional,只更新传入的字段(部分更新语义)。
    路径字段(content_dir/pageindex_dir/pdfs_dir)变更需后端校验安全性。
    http_host/http_port 变更需要重启应用才能生效。
    """

    content_dir: str | None = None
    pageindex_dir: str | None = None
    pdfs_dir: str | None = None
    pdf_strategy: str | None = Field(default=None, pattern="^(local|mineru)$")
    http_host: str | None = None
    http_port: int | None = None
    use_llm_proxy: bool | None = None


class AppConfigResponse(BaseModel):
    """应用配置响应(GET /api/app/config)。不返回敏感信息。"""

    content_dir: str
    pageindex_dir: str
    pdfs_dir: str
    pdf_strategy: str
    http_host: str
    http_port: int
    use_llm_proxy: bool


class AppConfigUpdateResponse(BaseModel):
    """应用配置更新响应。requires_restart 表示 host/port 变更需重启。"""

    ok: bool
    requires_restart: bool


class IngestTranslateRequest(BaseModel):
    doc_type: str = Field(pattern="^(book|paper)$")
    slug: str
    tier: str = "strong"


class IngestValidateRequest(BaseModel):
    doc_type: str = Field(pattern="^(book|paper)$")
    slug: str


class IngestRecleanRequest(BaseModel):
    """对已入库书重跑 clean 阶段(修复伪标题等)。"""
    slug: str
    doc_type: str = Field(pattern="^(book|paper|note)$")


class IngestResponse(BaseModel):
    job_id: str
    status: str


class JobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    current_stage: str = ""
    log: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None


class StatusResponse(BaseModel):
    """GET /api/status 应用状态聚合响应。"""

    app_name: str
    version: str
    index_ready: bool
    index_version: str = "legacy"  # "legacy" | "v3" | "both"
    generation: str | None = None
    view_id: str | None = None
    index_running: bool
    ingest_running: bool
    active_provider: str
    model: str
    has_key: bool
    keyring: bool


class SearchResultItem(BaseModel):
    """/api/search 单条检索结果。

    UI 字段: type/doc_type/slug/node_id/title/breadcrumb/text/score
    上下文字段(V3): source_md/line_num/line_end
    可重复性字段(V3): generation/view_id/doc_key/doc_uid/segment_hash/local_id/node_key
    """

    type: str = "chunk"
    doc_type: str
    slug: str
    node_id: str
    title: str
    breadcrumb: str
    text: str
    score: float
    # V3 上下文与可重复性字段(legacy 回退时为空)
    source_md: str | None = None
    line_num: int | None = None
    line_end: int | None = None
    generation: str | None = None
    view_id: str | None = None
    doc_key: str | None = None
    doc_uid: str | None = None
    segment_hash: str | None = None
    local_id: int | None = None
    node_key: str | None = None


class SearchResponse(BaseModel):
    """GET /api/search 响应。"""

    query: str
    results: list[SearchResultItem] = Field(default_factory=list)


# ── 本机文件检索 ──────────────────────────────────────────────────────


class FileSearchResultItem(BaseModel):
    """/api/filesearch/search 单条检索结果。"""

    file_name: str
    file_path: str
    page: int
    page_label: str
    line_start: int
    line_end: int
    text: str
    score: float
    snippet: str = ""  # 关键词高亮片段


class FileSearchResponse(BaseModel):
    """GET /api/filesearch/search 响应。"""

    query: str
    total: int
    results: list[FileSearchResultItem] = Field(default_factory=list)


class FileIndexBuildRequest(BaseModel):
    """POST /api/filesearch/build 请求。"""

    mode: str = Field(default="incremental", pattern="^(full|incremental)$")
    scan_dir: str = ""  # 空串用默认路径


class FileIndexBuildResponse(BaseModel):
    """POST /api/filesearch/build 响应。"""

    job_id: str
    status: str


class FileIndexStatusResponse(BaseModel):
    """GET /api/filesearch/status/{job_id} 响应。"""

    job_id: str
    status: str
    mode: str = ""  # "full" | "incremental"
    current_stage: str = ""
    progress: float = 0.0  # 0.0 ~ 1.0
    current_file: str = ""  # 当前正在处理的文件名
    log: list[str] = Field(default_factory=list)
    error: str = ""  # 详细错误信息(含 traceback)
    result: dict[str, Any] | None = None


class FileIndexInfoResponse(BaseModel):
    """GET /api/filesearch/info 响应 — 索引概况。"""

    files_count: int
    chunks_count: int
    tokens_count: int
    scan_dir: str
    index_dir: str
