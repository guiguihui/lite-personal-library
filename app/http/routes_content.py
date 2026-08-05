"""GET /api/content/docs | /read | /section | DELETE /api/content/doc — Library 文档浏览 + 删除。

端点(查询参数风格,对齐前端 fetch 调用):
  GET  /api/content/docs?type=books        列该 type 的文档(从 global-index.json 过滤)
  GET  /api/content/read?type=books&slug=x 读 pageindex/{type}s/{slug}.json 完整文档树
  GET  /api/content/section?source_md=...&line_num=0&line_end=10  读正文片段
  DELETE /api/content/doc?type=books&slug=x  删除文档(内容+索引+PDF)

路径安全:source_md 过 resolve_content_path(剥 content/ 前缀后校验 .. 越界)。
type 单复数:global-index.json 用单数(book/paper/note),pageindex 目录用复数(books/papers/notes)。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.storage.content_io import (
    delete_content_dir,
    delete_pdf_dir,
    read_markdown_section,
)
from app.storage.pageindex_io import (
    delete_pageindex_file,
    delete_pageindex_dir,
    read_index,
    remove_doc_from_global_index,
    remove_doc_from_node_index,
)
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
        idx = read_index("global-index.json", cfg)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="global-index.json not found")
    docs = [d for d in idx.get("docs", []) if d.get("type") == t]
    return JSONResponse({"type": t, "docs": docs})


@router.get("/read")
async def read_doc(type: str, slug: str, request: Request) -> JSONResponse:
    """读 pageindex/{type}s/{slug}.json,返回完整文档树(structure)。

    返回 {doc_name, type, title, author, description, tags, structure}。
    """
    cfg = request.app.state.app_config
    t = _normalize_type(type)
    dir_name = _TYPE_TO_DIR[t]
    rel = f"{dir_name}/{slug}.json"
    try:
        data = read_index(rel, cfg)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"not found: {type}/{slug}")
    except PathTraversalError:
        raise HTTPException(status_code=403, detail="path escapes root")
    return JSONResponse(data)


# 必须声明 PlainTextResponse:裸 str 返回值会被 FastAPI 包成 JSON 字符串
# (首尾引号 + \n 转义),前端 res.text() 拿到后直接渲染成一行乱文本
@router.get("/section", response_class=PlainTextResponse)
async def read_section(
    source_md: str,
    line_num: int = 0,
    line_end: int | None = None,
    request: Request = None,
) -> str:
    """读正文片段(按行号区间)。

    source_md 形如 'content/books/foo/ch01.md',剥 'content/' 前缀后过 resolve_content_path。
    返回纯文本 markdown 片段(前端用 render.js 渲染)。
    """
    cfg = request.app.state.app_config
    # 剥 content/ 前缀(索引里的 source_md 都带这个前缀)
    rel = source_md
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


@router.delete("/doc")
async def delete_doc(type: str, slug: str, request: Request) -> JSONResponse:
    """删除文档:内容 + 索引 + PDF 原始档。

    操作顺序:global-index → node-index → pageindex structure → content dir → pdf dir。
    任一步失败都不影响后续步骤(尽力删除),最后返回摘要。
    slug 安全校验:过 _normalize_type 不允许未知 type;禁止 .. 等路径特殊字符。
    """
    import re as _re

    cfg = request.app.state.app_config
    t = _normalize_type(type)
    dir_name = _TYPE_TO_DIR[t]

    # slug 安全校验:禁止路径遍历和特殊字符
    if not _re.match(r"^[a-zA-Z0-9._\-]+$", slug) or ".." in slug:
        raise HTTPException(status_code=400, detail="invalid slug")

    result = {"type": t, "slug": slug, "deleted": [], "errors": []}

    # 1. global-index
    try:
        if remove_doc_from_global_index(slug, cfg):
            result["deleted"].append("global-index")
    except Exception as exc:
        result["errors"].append(f"global-index: {exc}")

    # 2. node-index
    try:
        n = remove_doc_from_node_index(slug, cfg)
        if n > 0:
            result["deleted"].append(f"node-index({n} nodes)")
    except Exception as exc:
        result["errors"].append(f"node-index: {exc}")

    # 3. pageindex structure(books/foo.json)
    try:
        if delete_pageindex_file(f"{dir_name}/{slug}.json", cfg):
            result["deleted"].append("pageindex-structure")
    except Exception as exc:
        result["errors"].append(f"pageindex: {exc}")

    # 4. content 目录
    try:
        if delete_content_dir(t, slug, cfg):
            result["deleted"].append("content")
    except Exception as exc:
        result["errors"].append(f"content: {exc}")

    # 5. PDF 原始档
    try:
        if delete_pdf_dir(slug, cfg):
            result["deleted"].append("pdf")
    except Exception as exc:
        result["errors"].append(f"pdf: {exc}")

    # 同时清理倒排索引缓存(chunks.json + inverted-index.json),下次搜索时会 re-index
    try:
        delete_pageindex_file("chunks.json", cfg)
        delete_pageindex_file("inverted-index.json", cfg)
    except Exception:
        pass

    if not result["deleted"]:
        raise HTTPException(status_code=404, detail=f"document not found: {type}/{slug}")

    return JSONResponse(result)
