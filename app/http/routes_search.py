"""GET /api/search?q=...&limit=20 — 检索 API(双轨合并版)。

合并 LQ-D-desktop 与 norag-dev 的检索入口:
- V3 优先:若 data/pageindex/current-v3.json 存在,打开精确的 PinnedSearchView,
  通过 app.retrieval.search_view:search_pinned_view 执行稀疏候选检索,返回
  UI + 上下文 + 可重复性三类字段(source_md/line_num/generation/view_id/doc_key/
  doc_uid/segment_hash/local_id/node_key)。
- Legacy 回退:V3 指针缺失时,回退到 app.retrieval 的 Python multi-path 检索
  (读 global-index / inverted-index / chunks,三路 B+A+E + RRF 融合),保证
  兼容面可用(与 LQ-D-desktop 行为一致)。
- 两种模式都不静默失败:V3 指针存在但校验失败时返回 503(禁止回退)。

排序权威:V3 与 legacy 各维护自己的排序(双轨设计),聊天走 V3,阅读页走 legacy。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.config.schema import AppConfig
from app.http.schemas import SearchResponse, SearchResultItem
from app.retrieval.bm25 import build_chunk_stats
from app.retrieval.search import Hit, search_multi_path
from app.index.v3.runtime import CURRENT_POINTER, open_current_view
from app.retrieval.search_view import search_pinned_view
from app.storage.pageindex_io import read_index

router = APIRouter(prefix="/api", tags=["search"])

# global-index.json 中 type 是单数(book/paper/note),响应要求复数(books/papers/notes)
_DOC_TYPE_MAP: dict[str, str] = {
    "book": "books",
    "paper": "papers",
    "note": "notes",
}


# ── V3 检索路径 ──────────────────────────────────────────────────────────
def _search_v3(pageindex_dir: str, query: str, limit: int) -> list[Hit]:
    with open_current_view(pageindex_dir) as view:
        return search_pinned_view(query, view, top_k=limit)


def _hit_to_result(hit: Hit) -> SearchResultItem:
    """把 V3 命中转成 SearchResultItem(含稳定引用与上下文字段)。"""
    node = hit.node
    chunk = hit.chunk or {}
    doc_key = hit.doc_key or str(node.get("doc_key") or "")
    if ":" in doc_key:
        doc_type, slug = doc_key.split(":", 1)
    else:
        doc_type = str(node.get("doc_type") or "")
        slug = str(node.get("slug") or node.get("doc_id") or "")
    breadcrumb_raw = chunk.get("breadcrumb") or node.get("breadcrumb") or []
    breadcrumb = (
        " > ".join(str(part) for part in breadcrumb_raw)
        if isinstance(breadcrumb_raw, list)
        else str(breadcrumb_raw)
    )
    return SearchResultItem(
        type="chunk",
        doc_type=doc_type,
        slug=slug,
        node_id=str(
            chunk.get("legacy_node_id")
            or chunk.get("node_id")
            or node.get("node_id")
            or ""
        ),
        title=str(chunk.get("title") or node.get("title") or ""),
        breadcrumb=breadcrumb,
        text=str(chunk.get("body") or node.get("summary") or ""),
        score=hit.score,
        generation=hit.generation,
        view_id=hit.view_id,
        doc_key=hit.doc_key,
        doc_uid=hit.doc_uid,
        segment_hash=hit.segment_hash,
        local_id=hit.local_id,
        node_key=hit.node_key,
        source_md=(
            str(chunk.get("source_md"))
            if chunk.get("source_md") is not None
            else None
        ),
        line_num=(
            chunk.get("line_num")
            if isinstance(chunk.get("line_num"), int)
            else None
        ),
        line_end=(
            chunk.get("line_end")
            if isinstance(chunk.get("line_end"), int)
            else None
        ),
    )


# ── Legacy 检索路径(兼容回退) ─────────────────────────────────────────────
def _load_index(request: Request, cfg: AppConfig) -> dict[str, Any]:
    """加载索引并缓存(以 pageindex_dir + 文件 mtime 为失效键)。"""
    pageindex_dir = cfg.pageindex_dir
    files = {
        "global": Path(pageindex_dir) / "global-index.json",
        "inverted": Path(pageindex_dir) / "inverted-index.json",
        "chunks": Path(pageindex_dir) / "chunks.json",
    }

    mtimes: dict[str, float | None] = {}
    for key, path in files.items():
        mtimes[key] = path.stat().st_mtime if path.is_file() else None

    cache = getattr(request.app.state, "search_index_cache", None)
    if cache is not None and cache.get("key") == (pageindex_dir, mtimes):
        return cache["data"]

    global_index = read_index("global-index.json", cfg)
    inverted_index = read_index("inverted-index.json", cfg)
    chunks_data = read_index("chunks.json", cfg)

    chunks = chunks_data.get("chunks", []) if isinstance(chunks_data, dict) else chunks_data
    chunk_stats = build_chunk_stats(chunks)
    postings = (
        inverted_index.get("postings", {})
        if isinstance(inverted_index, dict)
        else {}
    )

    doc_types = {
        d.get("id", ""): _DOC_TYPE_MAP.get(d.get("type", ""), d.get("type", ""))
        for d in global_index.get("docs", [])
    }

    data: dict[str, Any] = {
        "global_index": global_index,
        "postings": postings,
        "chunk_stats": chunk_stats,
        "doc_types": doc_types,
    }
    request.app.state.search_index_cache = {"key": (pageindex_dir, mtimes), "data": data}
    return data


def _search_legacy(request: Request, cfg: AppConfig, query: str, limit: int) -> list[SearchResultItem]:
    """Legacy 多路检索(原 LQ-D-desktop 实现,双轨兼容回退)。"""
    try:
        data = _load_index(request, cfg)
    except FileNotFoundError:
        return []

    if not data["postings"] or data["chunk_stats"] is None:
        return []

    hits = search_multi_path(
        query,
        data["postings"],
        data["chunk_stats"],
        data["global_index"],
        top_k=limit,
    )

    results: list[SearchResultItem] = []
    for h in hits:
        node = h.node
        doc_id = node.get("doc_id") or ""
        breadcrumb_raw = node.get("breadcrumb") or []
        breadcrumb = (
            " > ".join(breadcrumb_raw)
            if isinstance(breadcrumb_raw, list)
            else str(breadcrumb_raw)
        )
        results.append(
            SearchResultItem(
                type="chunk",
                doc_type=data["doc_types"].get(doc_id, ""),
                slug=doc_id,
                node_id=node.get("node_id") or "",
                title=node.get("title") or "",
                breadcrumb=breadcrumb,
                text=node.get("summary") or "",
                score=h.score,
            )
        )
    return results


@router.get("/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100, description="返回结果数上限"),
) -> SearchResponse:
    """多路检索 API(双轨)。

    V3 优先;V3 指针存在但校验/打开失败时返回 HTTP 503;V3 指针缺失时
    回退 legacy Python 检索。
    """
    cfg = request.app.state.app_config
    pageindex_dir = cfg.pageindex_dir

    if not (Path(pageindex_dir) / CURRENT_POINTER).is_file():
        # V3 未发布 → legacy 回退(兼容面,行为与 LQ-D-desktop 一致)。
        # 用 run_in_threadpool 避免同步读取 chunks.json(~26MB)阻塞事件循环。
        return SearchResponse(
            query=q,
            results=await run_in_threadpool(_search_legacy, request, cfg, q, limit),
        )

    try:
        hits = await run_in_threadpool(_search_v3, pageindex_dir, q, limit)
    except Exception as exc:  # noqa: BLE001 - convert corrupt runtime state to 503
        raise HTTPException(
            status_code=503,
            detail=f"PageIndex V3 search unavailable: {type(exc).__name__}: {exc}",
        ) from exc

    results = [_hit_to_result(h) for h in hits]
    return SearchResponse(query=q, results=results)
