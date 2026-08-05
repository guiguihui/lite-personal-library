"""本机文件检索 API。

端点:
  POST /api/filesearch/build          — 启动索引构建(full/incremental)
  GET  /api/filesearch/status/{job_id} — 查询构建任务状态
  GET  /api/filesearch/info           — 索引概况(文件数/切片数/token 数)
  GET  /api/filesearch/search         — 关键词检索(BM25 + 倒排索引)

检索流程:
  1. 加载索引(files.json + chunks.json + inverted-index.json)
  2. 对 query 分词(tokenize_unique + expand_query_weighted)
  3. 从倒排索引收集候选 chunk_id
  4. 对每个候选 chunk 用 BM25 打分
  5. 排序 + 截取 top-N + 构建高亮片段

索引数据按 mtime 缓存于 request.app.state.fileindex_cache。
"""

from __future__ import annotations

import json
import logging
import math
import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.http.schemas import (
    FileIndexBuildRequest,
    FileIndexBuildResponse,
    FileIndexInfoResponse,
    FileIndexStatusResponse,
    FileSearchResponse,
    FileSearchResultItem,
)
from app.fileindex.store import FileIndexStore, FileIndexData
from app.fileindex.status import start_build, get_status
from app.retrieval.tokenizer import tokenize_unique, expand_query_weighted

logger = logging.getLogger("lqd.filesearch")

router = APIRouter(prefix="/api/filesearch", tags=["filesearch"])

# 第一阶段硬编码扫描路径(第二阶段改为配置化)
_DEFAULT_SCAN_DIR = r"E:\文档"

# BM25 参数(与 app.retrieval.bm25 保持一致)
_BM25_K = 1.5
_BM25_B = 0.75


def _get_paths(request: Request) -> tuple[str, str]:
    """获取扫描目录和索引目录路径。

    索引目录固定为 data/fileindex(相对于项目根)。
    """
    # 索引目录:项目根/data/fileindex
    project_root = Path(__file__).resolve().parent.parent.parent
    index_dir = str(project_root / "data" / "fileindex")
    return _DEFAULT_SCAN_DIR, index_dir


def _load_index(request: Request, index_dir: str) -> FileIndexData:
    """加载索引并缓存(以 index_dir + 文件 mtime 为失效键)。"""
    store = FileIndexStore(index_dir)
    index_path = Path(index_dir)

    # 收集索引文件 mtime 作为缓存键
    mtimes: dict[str, float | None] = {}
    for fname in ("files.json", "chunks.json", "inverted-index.json"):
        fpath = index_path / fname
        mtimes[fname] = fpath.stat().st_mtime if fpath.is_file() else None

    cache = getattr(request.app.state, "fileindex_cache", None)
    if cache is not None and cache.get("key") == (index_dir, mtimes):
        return cache["data"]

    data = store.load()
    request.app.state.fileindex_cache = {"key": (index_dir, mtimes), "data": data}
    return data


def _make_snippet(text: str, query_tokens: list[str], max_len: int = 200) -> str:
    """从 text 中提取包含查询词的片段(高亮用)。

    找到第一个匹配 token 的位置,前后各扩展 max_len/2 字符。
    """
    if not text or not query_tokens:
        return text[:max_len] + ("..." if len(text) > max_len else "")

    lower_text = text.lower()
    best_pos = -1
    for tok in query_tokens:
        pos = lower_text.find(tok.lower())
        if pos >= 0:
            best_pos = pos
            break

    if best_pos < 0:
        return text[:max_len] + ("..." if len(text) > max_len else "")

    half = max_len // 2
    start = max(0, best_pos - half)
    end = min(len(text), best_pos + half)
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def _bm25_score_chunk(
    query_tokens: list[str],
    chunk_text: str,
    df_map: dict[str, int],
    total_chunks: int,
    avg_len: float,
    weights: dict[str, float] | None = None,
) -> float:
    """对单个 chunk 的文本做 BM25 打分。

    简化版 BM25(单字段 body),与 app.retrieval.bm25 的 chunk 打分对齐。
    """
    doc_tokens = tokenize_unique(chunk_text)
    doc_len = len(doc_tokens)
    if doc_len == 0:
        return 0.0

    # TF map
    tf_map: dict[str, int] = {}
    raw_tokens = chunk_text.lower()  # 粗略,实际用 tokenize 重复
    # 用 tokenize_unique 的结果做 TF(已去重,tf=1)
    # 但 BM25 需要 TF,所以重新 tokenize
    from app.retrieval.tokenizer import tokenize
    all_toks = tokenize(chunk_text)
    for t in all_toks:
        tf_map[t] = tf_map.get(t, 0) + 1

    score = 0.0
    n = total_chunks or 1
    for qt in query_tokens:
        tf = tf_map.get(qt) or 0
        if not tf:
            continue
        df = df_map.get(qt) or 0
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        norm = 1 - _BM25_B + _BM25_B * (doc_len / (avg_len or 1))
        w = (weights.get(qt) or 0) if weights is not None else 1
        score += idf * ((tf * (_BM25_K + 1)) / (tf + _BM25_K * norm)) * w
    return score


@router.post("/build", response_model=FileIndexBuildResponse)
async def build_index(
    request: Request,
    body: FileIndexBuildRequest,
) -> FileIndexBuildResponse:
    """启动索引构建任务。立即返回 job_id,后台异步执行。"""
    try:
        scan_dir, index_dir = _get_paths(request)
        if body.scan_dir:
            scan_dir = body.scan_dir

        logger.info("启动索引构建: mode=%s, scan_dir=%s", body.mode, scan_dir)
        job_id = start_build(body.mode, scan_dir, index_dir)
        return FileIndexBuildResponse(job_id=job_id, status="running")
    except Exception as exc:
        tb_str = traceback.format_exc()
        err_detail = (
            f"[POST /api/filesearch/build] 启动索引构建失败 — "
            f"{type(exc).__name__}: {exc}\n{tb_str}"
        )
        logger.error(err_detail)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "BUILD_START_FAILED",
                "message": f"启动索引构建失败: {type(exc).__name__}: {exc}",
                "endpoint": "POST /api/filesearch/build",
                "traceback": tb_str,
            },
        ) from exc


@router.get("/status/{job_id}", response_model=FileIndexStatusResponse)
async def get_build_status(job_id: str) -> FileIndexStatusResponse:
    """查询构建任务状态。"""
    try:
        status = get_status(job_id)
        if status is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "JOB_NOT_FOUND",
                    "message": f"任务不存在: job_id={job_id}",
                    "endpoint": f"GET /api/filesearch/status/{job_id}",
                },
            )
        return FileIndexStatusResponse(**status)
    except HTTPException:
        raise
    except Exception as exc:
        tb_str = traceback.format_exc()
        logger.error(
            "[GET /api/filesearch/status/%s] 查询状态失败 — %s: %s\n%s",
            job_id, type(exc).__name__, exc, tb_str,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "STATUS_QUERY_FAILED",
                "message": f"查询构建状态失败: {type(exc).__name__}: {exc}",
                "endpoint": f"GET /api/filesearch/status/{job_id}",
                "traceback": tb_str,
            },
        ) from exc


@router.get("/info", response_model=FileIndexInfoResponse)
async def get_index_info(request: Request) -> FileIndexInfoResponse:
    """获取索引概况。"""
    try:
        scan_dir, index_dir = _get_paths(request)
        data = _load_index(request, index_dir)
        return FileIndexInfoResponse(
            files_count=len(data.files),
            chunks_count=len(data.chunks),
            tokens_count=len(data.inverted_index),
            scan_dir=scan_dir,
            index_dir=index_dir,
        )
    except Exception as exc:
        tb_str = traceback.format_exc()
        logger.error(
            "[GET /api/filesearch/info] 获取索引概况失败 — %s: %s\n%s",
            type(exc).__name__, exc, tb_str,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "INDEX_INFO_FAILED",
                "message": f"获取索引概况失败: {type(exc).__name__}: {exc}",
                "endpoint": "GET /api/filesearch/info",
                "traceback": tb_str,
            },
        ) from exc


@router.get("/search", response_model=FileSearchResponse)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, description="搜索关键词"),
    limit: int = Query(default=20, ge=1, le=200),
) -> FileSearchResponse:
    """关键词检索本机文件内容。

    流程:
    1. 对 q 分词 + 同义词扩展
    2. 从倒排索引收集候选 chunk_id
    3. 对候选 chunk 做 BM25 打分
    4. 排序 + 截取 top-N
    """
    try:
        if not q.strip():
            return FileSearchResponse(query=q, total=0, results=[])

        scan_dir, index_dir = _get_paths(request)
        data = _load_index(request, index_dir)

        if not data.chunks:
            return FileSearchResponse(query=q, total=0, results=[])

        # 分词 + 同义词扩展
        raw_tokens = tokenize_unique(q)
        query_tokens, weights = expand_query_weighted(raw_tokens, q)

        if not query_tokens:
            return FileSearchResponse(query=q, total=0, results=[])

        logger.info("检索: q=%s, tokens=%s, weights=%s", q, query_tokens, weights)

        # 从倒排索引收集候选 chunk_id
        candidate_ids: set[str] = set()
        for tok in query_tokens:
            ids = data.inverted_index.get(tok)
            if ids:
                candidate_ids.update(ids)

        if not candidate_ids:
            return FileSearchResponse(query=q, total=0, results=[])

        # 构建 chunk_id → ChunkEntry 映射
        chunk_map: dict[str, Any] = {}
        for ch in data.chunks:
            if ch.chunk_id in candidate_ids:
                chunk_map[ch.chunk_id] = ch

        # 构建 DF map(从倒排索引)
        df_map: dict[str, int] = {
            tok: len(ids) for tok, ids in data.inverted_index.items()
        }

        # 计算平均文档长度
        total_len = 0
        for ch in chunk_map.values():
            from app.retrieval.tokenizer import tokenize
            total_len += len(tokenize(ch.text))
        avg_len = total_len / len(chunk_map) if chunk_map else 1

        # BM25 打分
        scored: list[tuple[float, Any]] = []
        for cid, ch in chunk_map.items():
            score = _bm25_score_chunk(
                query_tokens, ch.text, df_map, len(data.chunks), avg_len, weights,
            )
            if score > 0:
                scored.append((score, ch))

        # 排序(降序)
        scored.sort(key=lambda x: x[0], reverse=True)

        # 截取 top-N
        top = scored[:limit]

        # 构建响应
        results = [
            FileSearchResultItem(
                file_name=ch.file_name,
                file_path=ch.file_path,
                page=ch.page,
                page_label=ch.page_label,
                line_start=ch.line_start,
                line_end=ch.line_end,
                text=ch.text,
                score=score,
                snippet=_make_snippet(ch.text, query_tokens),
            )
            for score, ch in top
        ]

        logger.info("检索完成: q=%s, 候选=%d, 命中=%d, 返回=%d",
                    q, len(candidate_ids), len(scored), len(results))

        return FileSearchResponse(
            query=q,
            total=len(scored),
            results=results,
        )
    except Exception as exc:
        tb_str = traceback.format_exc()
        logger.error(
            "[GET /api/filesearch/search] 检索失败 q=%s — %s: %s\n%s",
            q, type(exc).__name__, exc, tb_str,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "SEARCH_FAILED",
                "message": f"检索失败: {type(exc).__name__}: {exc}",
                "endpoint": "GET /api/filesearch/search",
                "query": q,
                "traceback": tb_str,
            },
        ) from exc

