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
    index_running: bool
    ingest_running: bool
    active_provider: str
    model: str
    has_key: bool
    keyring: bool


class SearchResultItem(BaseModel):
    """/api/search 单条检索结果。"""

    type: str = "chunk"
    doc_type: str
    slug: str
    node_id: str
    title: str
    breadcrumb: str
    text: str
    score: float


class SearchResponse(BaseModel):
    """GET /api/search 响应。"""

    query: str
    results: list[SearchResultItem] = Field(default_factory=list)
