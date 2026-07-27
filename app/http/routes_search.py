"""GET /api/search?q=...&limit=20 — 检索 API。

优先复用 app.retrieval 中的 Python 检索函数：
- 读 global-index / inverted-index / chunks
- build_chunk_stats -> search_multi_path（三路 B+A+E + RRF 融合）
- 返回统一 SearchResponse

索引文件会按 mtime 缓存于 request.app.state.search_index_cache，避免每次请求都重新解析大文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.config.schema import AppConfig
from app.http.schemas import SearchResponse, SearchResultItem
from app.retrieval.bm25 import build_chunk_stats
from app.retrieval.search import search_multi_path
from app.storage.pageindex_io import read_index

router = APIRouter(prefix="/api", tags=["search"])

# global-index.json 中 type 是单数（book/paper/note），响应要求复数（books/papers/notes）
_DOC_TYPE_MAP: dict[str, str] = {
    "book": "books",
    "paper": "papers",
    "note": "notes",
}


def _load_index(request: Request, cfg: AppConfig) -> dict[str, Any]:
    """加载索引并缓存（以 pageindex_dir + 文件 mtime 为失效键）。"""
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


@router.get("/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100, description="返回结果数上限"),
) -> SearchResponse:
    """多路检索 API。

    索引未就绪时返回空结果（不报错），前端可先用 /api/status 检查 index_ready。
    """
    cfg = request.app.state.app_config
    try:
        data = _load_index(request, cfg)
    except FileNotFoundError:
        return SearchResponse(query=q, results=[])

    if not data["postings"] or data["chunk_stats"] is None:
        return SearchResponse(query=q, results=[])

    hits = search_multi_path(
        q,
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

    return SearchResponse(query=q, results=results)
