"""GET /api/search?q=...&limit=20 — 检索 API。

优先复用 app.retrieval 中的 Python 检索函数：
- 读 global-index / inverted-index / chunks
- build_chunk_stats -> search_multi_path（三路 B+A+E + RRF 融合）
- 返回统一 SearchResponse

索引文件会按 mtime 缓存于 request.app.state.search_index_cache，避免每次请求都重新解析大文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.config.schema import AppConfig
from app.http.schemas import SearchResponse, SearchResultItem
from app.index.v3.generation import LogicalGenerationReceipt
from app.index.v3.models import ViewPin
from app.index.v3.reader import PinnedSearchView
from app.retrieval.bm25 import build_chunk_stats
from app.retrieval.search import Hit, search_multi_path
from app.retrieval.search_view import search_pinned_view
from app.retrieval.tokenizer import expand_query_weighted, tokenize_unique
from app.storage.pageindex_io import read_index

router = APIRouter(prefix="/api", tags=["search"])

# global-index.json 中 type 是单数（book/paper/note），响应要求复数（books/papers/notes）
_DOC_TYPE_MAP: dict[str, str] = {
    "book": "books",
    "paper": "papers",
    "note": "notes",
}

@dataclass(frozen=True, slots=True)
class SearchViewShadowTarget:
    """One externally trusted immutable P3 pin used only for shadow reads."""

    pin: ViewPin
    generation: LogicalGenerationReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.pin, ViewPin):
            raise TypeError("pin must be a ViewPin")
        if not isinstance(self.generation, LogicalGenerationReceipt):
            raise TypeError("generation must be a LogicalGenerationReceipt")
        if self.pin.generation != self.generation.generation_id:
            raise ValueError("shadow pin and Generation receipt differ")


@dataclass(frozen=True, slots=True)
class _ShadowRun:
    hits: tuple[Hit, ...]
    pruned_tokens: tuple[str, ...]


def _query_pruned_tokens(query: str, view: PinnedSearchView) -> tuple[str, ...]:
    original = tokenize_unique(query)
    if not original:
        return ()
    tokens, _weights = expand_query_weighted(original, query)
    summaries = view.token_stats(tokens)
    total_chunks = view.corpus_stats().total_chunks
    recipe = view.generation_recipe
    return tuple(
        token
        for token in tokens
        if summaries[token] is not None
        and summaries[token].df_body >= recipe.body_df_min
        and summaries[token].df_body * recipe.body_df_ratio_denominator
        >= total_chunks * recipe.body_df_ratio_numerator
    )


def _run_shadow_search(
    target: SearchViewShadowTarget,
    pageindex_dir: str,
    query: str,
    limit: int,
) -> _ShadowRun:
    with PinnedSearchView.open(
        Path(pageindex_dir),
        target.pin,
        target.generation,
    ) as view:
        pruned_tokens = _query_pruned_tokens(query, view)
        hits = search_pinned_view(query, view, top_k=limit)
    return _ShadowRun(tuple(hits), pruned_tokens)


def _compatibility_key(
    hit: Hit,
    legacy_doc_types: dict[str, str],
) -> tuple[str, str, str]:
    node_id = str(hit.node.get("node_id") or "")
    if hit.doc_key is not None and ":" in hit.doc_key:
        doc_type, slug = hit.doc_key.split(":", 1)
        return (_DOC_TYPE_MAP.get(doc_type, doc_type), slug, node_id)
    slug = str(hit.node.get("doc_id") or "")
    return (legacy_doc_types.get(slug, ""), slug, node_id)


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


@router.get(
    "/search",
    response_model=SearchResponse,
    response_model_exclude_none=True,
)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100, description="返回结果数上限"),
) -> SearchResponse:
    """多路检索 API。

    索引未就绪时返回空结果（不报错），前端可先用 /api/status 检查 index_ready。
    """
    cfg = request.app.state.app_config
    shadow_target = getattr(request.app.state, "search_view_shadow_pin", None)
    try:
        data = _load_index(request, cfg)
    except FileNotFoundError:
        return SearchResponse(query=q, results=[])

    if not data["postings"] or data["chunk_stats"] is None:
        return SearchResponse(query=q, results=[])

    legacy_started = perf_counter()
    hits = search_multi_path(
        q,
        data["postings"],
        data["chunk_stats"],
        data["global_index"],
        top_k=limit,
    )
    legacy_ms = (perf_counter() - legacy_started) * 1000

    if shadow_target is not None:
        pin = getattr(shadow_target, "pin", None)
        diagnostic: dict[str, Any] = {
            "generation": getattr(pin, "generation", None),
            "view_id": getattr(pin, "view_id", None),
            "legacy_ms": legacy_ms,
        }
        p3_started = perf_counter()
        try:
            shadow = await run_in_threadpool(
                _run_shadow_search,
                shadow_target,
                cfg.pageindex_dir,
                q,
                limit,
            )
            diagnostic["p3_ms"] = (perf_counter() - p3_started) * 1000
            legacy_keys = tuple(
                _compatibility_key(hit, data["doc_types"])
                for hit in hits
            )
            p3_keys = tuple(
                _compatibility_key(hit, data["doc_types"])
                for hit in shadow.hits
            )
            legacy_scores = tuple(hit.score for hit in hits)
            p3_scores = tuple(hit.score for hit in shadow.hits)
            identity_match = legacy_keys == p3_keys
            score_match = legacy_scores == p3_scores
            mismatch = not identity_match or not score_match
            expected_policy_delta = mismatch and bool(shadow.pruned_tokens)
            diagnostic.update(
                {
                    "legacy_keys": legacy_keys,
                    "p3_keys": p3_keys,
                    "p3_references": tuple(
                        {
                            "doc_key": hit.doc_key,
                            "doc_uid": hit.doc_uid,
                            "segment_hash": hit.segment_hash,
                            "local_id": hit.local_id,
                            "node_key": hit.node_key,
                        }
                        for hit in shadow.hits
                    ),
                    "identity_match": identity_match,
                    "score_match": score_match,
                    "pruned_tokens": shadow.pruned_tokens,
                    "expected_semantic_delta": expected_policy_delta,
                    "classification": (
                        "match"
                        if not mismatch
                        else (
                            "expected_policy_delta"
                            if expected_policy_delta
                            else "regression"
                        )
                    ),
                    "error": None,
                }
            )
        except Exception as exc:
            diagnostic.update(
                {
                    "p3_ms": (perf_counter() - p3_started) * 1000,
                    "classification": "shadow_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        request.app.state.search_view_shadow_diagnostics = diagnostic
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
