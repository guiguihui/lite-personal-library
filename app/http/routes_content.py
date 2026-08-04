"""GET /api/content/docs | /read | /section — Library 文档浏览。

端点(查询参数风格,对齐前端 fetch 调用):
  GET /api/content/docs?type=books        列该 type 的文档(从 global-index.json 过滤)
  GET /api/content/read?type=books&slug=x 读 pageindex/{type}s/{slug}.json 完整文档树
  GET /api/content/section?source_md=...&line_num=0&line_end=10  读正文片段

路径安全:source_md 过 resolve_content_path(剥 content/ 前缀后校验 .. 越界)。
type 单复数:global-index.json 用单数(book/paper/note),pageindex 目录用复数(books/papers/notes)。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.storage.content_io import read_markdown_section
from app.index.v3.runtime import CurrentViewError, open_current_view
from app.library.v3_service import LibraryV3Error, LibraryV3Service
from app.storage.paths import PathTraversalError, resolve_content_path

router = APIRouter(prefix="/api/content", tags=["content"])

# global-index.json 的 type(单数) → pageindex 目录名(复数)
_TYPE_TO_DIR = {
    "book": "books",
    "paper": "papers",
    "note": "notes",
}
# 前端传入的 type 也接受复数形式(books/papers/notes),统一归一化为单数
_TYPE_NORMALIZE = {
    "books": "book",
    "papers": "paper",
    "notes": "note",
    "book": "book",
    "paper": "paper",
    "note": "note",
}


def _normalize_type(raw: str) -> str:
    """归一化 type 为单数形式(book/paper/note)。未知值抛 400。"""
    t = _TYPE_NORMALIZE.get(raw)
    if t is None:
        raise HTTPException(status_code=400, detail=f"unknown type: {raw}")
    return t


@router.get("/docs")
async def list_docs(type: str, request: Request) -> JSONResponse:
    """列该 type 的文档(从 global-index.json 读 docs,过滤 type)。

    返回 {type, docs: [{id, title, author, description, tags, ...}]}。
    """
    cfg = request.app.state.app_config
    t = _normalize_type(type)
    try:
        with open_current_view(cfg.pageindex_dir) as view:
            payload = LibraryV3Service(view).list_documents(t)
    except CurrentViewError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "V3_VIEW_UNAVAILABLE", "message": str(exc), "retryable": True},
        ) from exc
    except LibraryV3Error as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
    return JSONResponse(payload)


@router.get("/read")
async def read_doc(
    type: str,
    slug: str,
    request: Request,
    generation: str | None = None,
    view_id: str | None = None,
) -> JSONResponse:
    """读 pageindex/{type}s/{slug}.json,返回完整文档树(structure)。

    返回 {doc_name, type, title, author, description, tags, structure}。
    """
    cfg = request.app.state.app_config
    t = _normalize_type(type)
    try:
        with open_current_view(cfg.pageindex_dir) as view:
            data = LibraryV3Service(view).read_document(
                t, slug, generation=generation, view_id=view_id
            )
    except CurrentViewError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "V3_VIEW_UNAVAILABLE", "message": str(exc), "retryable": True},
        ) from exc
    except LibraryV3Error as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
    return JSONResponse(data)


@router.get("/section", response_class=PlainTextResponse)
async def read_section(
    source_md: str,
    line_num: int = 0,
    line_end: int | None = None,
    type: str | None = None,
    slug: str | None = None,
    generation: str | None = None,
    view_id: str | None = None,
    request: Request = None,
) -> str:
    """读正文片段(按行号区间)。

    source_md 形如 'content/books/foo/ch01.md',剥 'content/' 前缀后过 resolve_content_path。
    返回纯文本 markdown 片段(前端用 render.js 渲染)。
    """
    cfg = request.app.state.app_config
    security_rel = source_md.replace("\\", "/")
    if security_rel.startswith("content/"):
        security_rel = security_rel[len("content/") :]
    try:
        resolve_content_path(security_rel, cfg)
    except PathTraversalError as exc:
        raise HTTPException(status_code=403, detail="path escapes root") from exc

    probe = source_md.replace("\\", "/")
    if probe.startswith("content/"):
        probe = probe[len("content/") :]
    parts = probe.split("/")
    doc_type = _TYPE_NORMALIZE.get(type) if type else None
    if doc_type is None and parts:
        doc_type = {"books": "book", "papers": "paper", "notes": "note"}.get(parts[0])
    if slug is None and len(parts) > 1:
        slug = parts[1]
        if doc_type == "note":
            slug = slug.rsplit(".", 1)[0]
    if doc_type is None or slug is None:
        raise HTTPException(status_code=422, detail={"code": "DOCUMENT_ID_REQUIRED", "message": "Cannot determine V3 document identity"})
    try:
        with open_current_view(cfg.pageindex_dir) as view:
            authorized_rel = LibraryV3Service(view).authorize_source(
                doc_type,
                slug,
                source_md,
                generation=generation,
                view_id=view_id,
            )
    except CurrentViewError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "V3_VIEW_UNAVAILABLE", "message": str(exc), "retryable": True},
        ) from exc
    except LibraryV3Error as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc

    # 剥 content/ 前缀(索引里的 source_md 都带这个前缀)
    rel = authorized_rel
    prefix = "content/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
    # 安全校验(过 resolve_content_path 会抛 PathTraversalError 若 .. 越界)
    try:
        resolve_content_path(rel, cfg)
    except PathTraversalError:
        raise HTTPException(status_code=403, detail="path escapes root")
    end = line_end if line_end is not None else line_num + 50
    try:
        text = read_markdown_section(rel, line_num, end, cfg)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"not found: {source_md}")
    except PathTraversalError:
        raise HTTPException(status_code=403, detail="path escapes root")
    return text
