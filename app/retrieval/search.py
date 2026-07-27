"""Search functions (port of retrieval.js L228-587).

严格对齐 retrieval.js：

- ``search`` (L228-272)：node-index BM25F 线性检索。
- ``search_inverted`` (L359-442)：倒排 postings 检索。
- ``search_title_phrase`` (L453-488)：路 A，title/breadcrumb phrase 匹配。
- ``search_doc_route`` (L492-536)：路 E，doc title/TOC 路由。
- ``search_multi_path`` (L565-587)：三路 B+A+E → RRF → per-doc 截断。

对齐 pitfalls（已处理）：

- ``Math.round`` vs Python ``round``：JS ``Math.round`` 向 +∞ 舍入
  （``Math.round(0.5)=1``），Python ``round`` 是 banker's rounding
  （``round(0.5)=0``）。分数为正数，用 ``_js_round`` 实现 JS 风格
  （``int(x*100 + 0.5) / 100`` 等）。
- per-doc 截断：``docCount`` Map，每 ``doc_id`` 最多 3 条，
  ``results.length >= topK*2`` 时 break，最后 ``slice(0, topK)``。
- ``search_inverted`` 的 cidMap 构建：``chunk_id`` 形如 ``c000001``，
  ``cid_num = int(cid[1:])``。不以 ``c`` 开头或非数字则跳过。
  cidMap 缓存在 ``chunkStats._cid_map``（首次调用时构建，后续复用）。
- ``search_inverted`` 合成 node：``summary = chunk.body[:200]``，
  positions 的 body 位置写入 ``positions[qt]['summary']`` 复用 summary 字段位。
- ``search_title_phrase`` 的 phraseScore 阈值 >0.3 才保留。
  phraseTotal 包含 CJK 2-gram 数 + 英文 bigram 数 + 英文单 token 数。
- ``search_doc_route`` 取前 5 个文档，每个命中文档的所有 chunk 都给 score=0.1。
- ``search_multi_path`` 三路顺序 A,B,E（RRF 对顺序不敏感因每路独立 rank）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.retrieval.bm25 import (
    BM25Stats,
    ChunkStats,
    bm25_score,
    bm25_score_chunk,
    build_bm25_stats,
)
from app.retrieval.tokenizer import (
    expand_query,
    expand_query_weighted,
    tokenize,
    tokenize_unique,
)

__all__ = [
    "Hit",
    "search",
    "search_inverted",
    "search_title_phrase",
    "search_doc_route",
    "search_multi_path",
]

# CJK 基本区正则（与 tokenizer 一致）
_CJK_RE = re.compile(r"[一-鿿]+")
_EN_LOWER_RE = re.compile(r"[a-z][a-z0-9]{1,}")


def _js_round(x: float, ndigits: int = 0) -> float:
    """JS ``Math.round`` 风格舍入（向 +∞）。

    JS ``Math.round(x)`` = ``floor(x + 0.5)``。Python ``round`` 是 banker's
    rounding（``round(0.5)=0``）。分数为正数，用此函数严格对齐。

    ``ndigits``：``Math.round(s*100)/100`` → ``_js_round(s, 2)``；
    ``Math.round(rrf*1000)/1000`` → ``_js_round(rrf, 3)``。
    """
    factor = 10 ** ndigits
    return math_floor(x * factor + 0.5) / factor


def math_floor(x: float) -> int:
    """``math.floor`` but returns int (avoid float repr issues)."""
    import math

    return math.floor(x)


@dataclass
class Hit:
    """检索命中（与 retrieval.js ``{node, score, tokens, positions, chunk?}`` 对齐）。

    - ``node``：节点元数据（``search`` 用原 node，``search_inverted`` 合成）。
    - ``score``：BM25 分（两位小数）或 RRF 融合分（三位小数，``rrf_fuse`` 覆写）。
    - ``tokens``：查询 token（含同义词扩展）。
    - ``positions``：``{qt: {field: idx}}``，供 ``lexical_rerank`` 算 proximity。
    - ``chunk``：可选，chunk 原始数据（``search_inverted`` / ``search_title_phrase``
      / ``search_doc_route`` 有）。
    - ``rrf_score``：RRF 融合分（浮点，``rrf_fuse`` 注入）。
    - ``rerank_score``：词法精排分（``lexical_rerank`` 注入）。
    """

    node: dict[str, Any]
    score: float
    tokens: list[str]
    positions: dict[str, dict[str, int]] = field(default_factory=dict)
    chunk: Mapping[str, Any] | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


def _per_doc_truncate(scored: list[Hit], top_k: int) -> list[Hit]:
    """per-doc 截断：每 doc_id 最多 3 条，topK*2 break，slice topK。

    与 retrieval.js ``search`` / ``search_inverted`` / ``search_multi_path`` 一致。
    """
    doc_count: dict[str, int] = {}
    results: list[Hit] = []
    for item in scored:
        c = doc_count.get(item.node["doc_id"], 0)
        if c < 3:
            results.append(item)
            doc_count[item.node["doc_id"]] = c + 1
        if len(results) >= top_k * 2:
            break
    return results[:top_k]


def _build_positions_node(tokens: list[str], node: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """构造 positions：query token 在各字段首次 index（node-index 版）。

    与 retrieval.js ``search`` (L241-256) 对齐。
    """
    positions: dict[str, dict[str, int]] = {}
    breadcrumb = node.get("breadcrumb") or []
    terms = node.get("terms") or []
    field_tokens = {
        "title": tokenize(node.get("title") or ""),
        "breadcrumb": tokenize(
            " ".join(breadcrumb) if isinstance(breadcrumb, list) else str(breadcrumb)
        ),
        "terms": tokenize(" ".join(terms) if isinstance(terms, list) else str(terms)),
        "summary": tokenize(node.get("summary") or node.get("excerpt") or ""),
    }
    for qt in tokens:
        for f, ftoks in field_tokens.items():
            try:
                idx = ftoks.index(qt)
            except ValueError:
                idx = -1
            if idx >= 0:
                positions.setdefault(qt, {})[f] = idx
    return positions


def search(
    query: str,
    node_index: Mapping[str, Any] | None,
    stats: BM25Stats | None = None,
    top_k: int = 50,
) -> list[Hit]:
    """node-index BM25F 线性检索（retrieval.js L228-272）。

    - 候选收集用 ``tokenize_unique``；``bm25_score`` 内部用带频次 tokenize。
    - ``score = Math.round(s*100)/100``（两位小数）。
    - per-doc 截断（每 doc 最多 3，topK*2 break，slice topK）。
    """
    if node_index is None:
        return []
    if stats is None:
        stats = build_bm25_stats(node_index)
    if stats is None:
        return []
    tokens = tokenize_unique(query)
    if not tokens:
        return []
    tokens = expand_query(tokens, query)
    scored: list[Hit] = []
    for node in node_index.get("nodes", []):
        s = bm25_score(tokens, node, stats)
        if s > 0:
            score = _js_round(s, 2)
            positions = _build_positions_node(tokens, node)
            scored.append(Hit(node=dict(node), score=score, tokens=tokens, positions=positions))
    # 降序（Python sorted 稳定，同分时保持原顺序——与 JS Array.sort 一致）
    scored.sort(key=lambda h: h.score, reverse=True)
    return _per_doc_truncate(scored, top_k)


def _ensure_cid_map(chunk_stats: ChunkStats) -> dict[int, Mapping[str, Any]]:
    """惰性构建 cid_num → chunk 索引表（retrieval.js L379-389）。

    chunk_id 形如 ``c000001``，``cid_num = int(cid[1:])``。
    不以 ``c`` 开头或非数字则跳过。
    """
    if chunk_stats._cid_map is not None:
        return chunk_stats._cid_map
    m: dict[int, Mapping[str, Any]] = {}
    for ch in chunk_stats.chunks:
        cid = ch.get("chunk_id") or ""
        if cid.startswith("c"):
            try:
                num = int(cid[1:])
            except ValueError:
                continue
            m[num] = ch
    chunk_stats._cid_map = m
    return m


def _build_positions_chunk(
    tokens: list[str], chunk: Mapping[str, Any]
) -> dict[str, dict[str, int]]:
    """构造 positions：token 在 title/breadcrumb/body 中的首次位置（chunk 版）。

    与 retrieval.js ``searchInverted`` (L411-425) 对齐。
    body 的位置写入 ``positions[qt]['summary']`` 复用 summary 字段位。
    """
    positions: dict[str, dict[str, int]] = {}
    title_toks = tokenize(chunk.get("title") or "")
    bc = chunk.get("breadcrumb") or []
    bc_toks = tokenize(" ".join(bc) if isinstance(bc, list) else str(bc))
    body_toks = tokenize(chunk.get("body") or "")
    for qt in tokens:
        try:
            ti = title_toks.index(qt)
        except ValueError:
            ti = -1
        try:
            bi = bc_toks.index(qt)
        except ValueError:
            bi = -1
        try:
            si = body_toks.index(qt)
        except ValueError:
            si = -1
        if ti >= 0 or bi >= 0 or si >= 0:
            positions[qt] = {}
            if ti >= 0:
                positions[qt]["title"] = ti
            if bi >= 0:
                positions[qt]["breadcrumb"] = bi
            if si >= 0:
                positions[qt]["summary"] = si  # 复用 summary 字段位
    return positions


def _synth_node_from_chunk(chunk: Mapping[str, Any]) -> dict[str, Any]:
    """从 chunk 元数据合成 node（retrieval.js L400-409）。

    ``summary = chunk.body[:200]``（chunk body 首段作 summary 兜底）。
    """
    body = chunk.get("body") or ""
    return {
        "doc_id": chunk.get("doc_id"),
        "node_id": chunk.get("node_id"),
        "title": chunk.get("title"),
        "breadcrumb": chunk.get("breadcrumb"),
        "url": "",
        "terms": [],
        "summary": body[:200],
        "line_num": chunk.get("line_num"),
    }


def search_inverted(
    query: str,
    postings: Mapping[str, list[list[int]]] | None,
    chunk_stats: ChunkStats | None,
    top_k: int = 50,
) -> list[Hit]:
    """倒排 postings 检索（retrieval.js L359-442）。

    - postings: ``{token: [[cid_num, tf], ...]}``。
    - 候选收集用 ``tokenize_unique``；打分用带频次 ``bm25_score_chunk``。
    - cidMap 惰性构建（缓存到 ``chunk_stats._cid_map``）。
    - 合成 node（供上层 ``lexical_rerank`` / doc_id 聚合复用）。
    - per-doc 截断同 ``search``。
    """
    if not postings or chunk_stats is None:
        return []
    orig_tokens = tokenize_unique(query)
    if not orig_tokens:
        return []
    tokens, weights = expand_query_weighted(orig_tokens, query)

    # 收集候选 chunk_id → 命中 token 数
    cand_ids: dict[int, int] = {}
    for qt in tokens:
        plist = postings.get(qt)
        if not plist:
            continue
        for entry in plist:
            cid_num = entry[0]
            cand_ids[cid_num] = cand_ids.get(cid_num, 0) + 1
    if not cand_ids:
        return []

    cid_map = _ensure_cid_map(chunk_stats)

    scored: list[Hit] = []
    for cid_num in cand_ids:
        chunk = cid_map.get(cid_num)
        if chunk is None:
            continue
        s = bm25_score_chunk(tokens, chunk, chunk_stats, weights)
        if s > 0:
            score = _js_round(s, 2)
            node = _synth_node_from_chunk(chunk)
            positions = _build_positions_chunk(tokens, chunk)
            scored.append(
                Hit(node=node, score=score, tokens=tokens, positions=positions, chunk=chunk)
            )
    scored.sort(key=lambda h: h.score, reverse=True)
    return _per_doc_truncate(scored, top_k)


def search_title_phrase(
    query: str,
    postings: Mapping[str, list[list[int]]] | None,
    chunk_stats: ChunkStats | None,
    top_k: int = 20,
) -> list[Hit]:
    """路 A：title exact / phrase 匹配（retrieval.js L453-488）。

    - 复用 ``search_inverted`` 候选（topK*3）。
    - CJK 2 字滑窗 + 英文 bigram + 英文单 token 算 phrase 覆盖率。
    - phraseScore > 0.3 才保留，score = ``Math.round(phraseScore*100)/100``。
    """
    if not postings or chunk_stats is None:
        return []
    all_hits = search_inverted(query, postings, chunk_stats, top_k * 3)
    raw = (query or "").lower()
    cjk_part = "".join(_CJK_RE.findall(raw))
    en_tokens = _EN_LOWER_RE.findall(raw)
    out: list[Hit] = []
    for h in all_hits:
        chunk = h.chunk
        if chunk is None:
            continue
        title = chunk.get("title") or ""
        bc = chunk.get("breadcrumb") or []
        bc_str = " ".join(bc) if isinstance(bc, list) else str(bc)
        title_text = (title + " " + bc_str).lower()
        phrase_hits = 0
        phrase_total = 0
        # CJK 2 字滑窗：i 从 0 到 len-2（含）
        for i in range(len(cjk_part) - 1):
            phrase_total += 1
            if cjk_part[i : i + 2] in title_text:
                phrase_hits += 1
        # 英文 token bigram：i 从 0 到 len-2（注意 length-1 时不执行）
        for i in range(len(en_tokens) - 1):
            phrase_total += 1
            if (en_tokens[i] + " " + en_tokens[i + 1]) in title_text:
                phrase_hits += 1
        # 单 token 也算（英文术语）
        for t in en_tokens:
            phrase_total += 1
            if t in title_text:
                phrase_hits += 1
        phrase_score = (phrase_hits / phrase_total) if phrase_total else 0
        if phrase_score > 0.3:
            # 保留原 BM25 分作次序参考，score 覆写为 phraseScore
            out.append(
                Hit(
                    node=h.node,
                    score=_js_round(phrase_score, 2),
                    tokens=h.tokens,
                    positions=h.positions,
                    chunk=chunk,
                )
            )
    out.sort(key=lambda h: h.score, reverse=True)
    return out[:top_k]


def search_doc_route(
    query: str,
    global_index: Mapping[str, Any] | None,
    postings: Mapping[str, list[list[int]]] | None,
    chunk_stats: ChunkStats | None,
    top_k: int = 20,
) -> list[Hit]:
    """路 E：doc title / TOC 路由（retrieval.js L492-536）。

    - 匹配 globalIndex 的文档标题（整书/整篇层面定位）。
    - score = overlap + (cjkHit?2:0) + (enHit?2:0) + (descText.includes(raw)?1:0)。
    - 取 top5 文档的所有 chunk（score=0.1）。
    """
    if not global_index or not global_index.get("docs") or chunk_stats is None:
        return []
    raw = (query or "").lower()
    q_toks = tokenize_unique(query)
    doc_scores: list[tuple[Mapping[str, Any], float]] = []
    for doc in global_index["docs"]:
        title_text = (doc.get("title") or "").lower()
        desc_text = (doc.get("description") or "").lower()
        title_toks = tokenize_unique(doc.get("title") or "")
        # token 重叠
        overlap = 0
        for t in q_toks:
            if t in title_toks:
                overlap += 1
        cjk_hit = any(seg in title_text for seg in _CJK_RE.findall(raw))
        en_hit = any(t in title_text for t in _EN_LOWER_RE.findall(raw))
        score = overlap + (2 if cjk_hit else 0) + (2 if en_hit else 0) + (1 if raw in desc_text else 0)
        if score > 0:
            doc_scores.append((doc, score))
    doc_scores.sort(key=lambda x: x[1], reverse=True)
    # 取 top5 文档
    top_docs = {doc["id"] for doc, _ in doc_scores[:5]}
    if not top_docs:
        return []
    out: list[Hit] = []
    for ch in chunk_stats.chunks:
        if ch.get("doc_id") in top_docs:
            body = ch.get("body") or ""
            node = {
                "doc_id": ch.get("doc_id"),
                "node_id": ch.get("node_id"),
                "title": ch.get("title"),
                "breadcrumb": ch.get("breadcrumb"),
                "url": "",
                "terms": [],
                "summary": body[:200],
                "line_num": ch.get("line_num"),
            }
            out.append(
                Hit(
                    node=node,
                    score=0.1,
                    tokens=q_toks,
                    positions={},
                    chunk=ch,
                )
            )
        if len(out) >= top_k * 3:
            break
    return out[:top_k]


def search_multi_path(
    query: str,
    postings: Mapping[str, list[list[int]]] | None,
    chunk_stats: ChunkStats | None,
    global_index: Mapping[str, Any] | None,
    top_k: int = 50,
) -> list[Hit]:
    """三路召回 + RRF 融合（retrieval.js L565-587）。

    - 路 B：``search_inverted`` topK=max(topK,50)（主路 BM25F body）。
    - 路 A：``search_title_phrase`` topK=20。
    - 路 E：``search_doc_route`` topK=20。
    - RRF 融合（顺序 A,B,E，但 RRF 对顺序不敏感）。
    - per-doc 截断同 ``search``。
    """
    if not postings or chunk_stats is None:
        return []
    # 延迟导入避免循环依赖
    from app.retrieval.fuse import rrf_fuse

    path_b = search_inverted(query, postings, chunk_stats, max(top_k, 50))
    path_a = search_title_phrase(query, postings, chunk_stats, 20)
    path_e = search_doc_route(query, global_index, postings, chunk_stats, 20)
    fused = rrf_fuse([path_a, path_b, path_e])
    return _per_doc_truncate(fused, top_k)
