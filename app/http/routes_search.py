"""The single retrieval boundary for library search and chat."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.http.schemas import SearchResponse, SearchResultItem
from app.index.v3.runtime import CURRENT_POINTER, open_current_view
from app.retrieval.search import Hit
from app.retrieval.search_view import search_pinned_view

router = APIRouter(prefix="/api", tags=["search"])


def _search_v3(pageindex_dir: str, query: str, limit: int) -> list[Hit]:
    with open_current_view(pageindex_dir) as view:
        return search_pinned_view(query, view, top_k=limit)


@router.get(
    "/search",
    response_model=SearchResponse,
    response_model_exclude_none=True,
)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100, description="返回结果上限"),
) -> SearchResponse:
    """Search exactly the currently published immutable PageIndex V3 view."""

    cfg = request.app.state.app_config
    if not (Path(cfg.pageindex_dir) / CURRENT_POINTER).is_file():
        return SearchResponse(query=q, results=[])
    try:
        hits = await run_in_threadpool(_search_v3, cfg.pageindex_dir, q, limit)
    except Exception as exc:  # noqa: BLE001 - convert corrupt runtime state to 503
        raise HTTPException(
            status_code=503,
            detail=f"PageIndex V3 search unavailable: {type(exc).__name__}: {exc}",
        ) from exc

    results: list[SearchResultItem] = []
    for hit in hits:
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
        results.append(
            SearchResultItem(
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
        )
    return SearchResponse(query=q, results=results)
