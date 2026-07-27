"""BM25 / BM25F scoring (port of retrieval.js L155-352).

严格对齐 retrieval.js：

- node-index BM25F (L155-272)：``build_bm25_stats`` / ``bm25_score``。
- chunk BM25F (L283-352)：``build_chunk_stats`` / ``bm25_score_chunk``。

常量：

- ``FIELD_BOOST = {title:6, breadcrumb:3, terms:2, summary:2}``
- ``CHUNK_FIELD_BOOST = {title:6, breadcrumb:3, body:1}``
- ``BM25_K = 1.5``, ``BM25_B = 0.75``
- ``FIELDS = ["title", "breadcrumb", "terms", "summary"]``

对齐 pitfalls（已处理）：

- IDF 公式 ``log(1+(N-df+0.5)/(df+0.5))`` 是 BM25+ 风格（+1 外层平滑），
  df=0 时 idf>0（不会负）。Python 用 ``math.log``（自然对数）。
- BM25 公式逐字段独立算，不是合并。
- ``build_bm25_stats`` 的 DF 是 per-field 去重累加（同一 token 在同 node 的
  title 和 terms 都出现会被计 df+=2）。
- ``build_bm25_stats`` 的 totalLen 用 ``tokenize(title+breadcrumb+terms+summary
  拼接).length``——拼接后 CJK 2-gram 会跨字段边界生成新的 2-gram，
  avgLen 因此略大于各 fieldAvgLen 之和。移植时保留拼接行为以严格对齐。
- ``build_chunk_stats`` 的 DF 按 chunk 合并去重（一个 chunk 内某 token 只算 1 次，
  无论在几个字段出现）——与 node-index 的 per-field 去重不同，不要统一两者语义。
- ``build_chunk_stats`` 预计算 ``ch._tf``（查询期直接查表，避免重复 tokenize 42k
  chunk——性能关键）。Python 移植用独立 dict 而非注入到 chunk dict
  （保持 chunk 输入不可变），但仍通过 ``ChunkStats._tf`` 暴露。
- ``ChunkStats._cid_map`` 惰性构建（首次 ``search_inverted`` 调用时构建，后续复用）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.retrieval.tokenizer import tokenize, tokenize_unique

__all__ = [
    "BM25Stats",
    "ChunkStats",
    "build_bm25_stats",
    "build_chunk_stats",
    "bm25_score",
    "bm25_score_chunk",
    "FIELD_BOOST",
    "CHUNK_FIELD_BOOST",
    "BM25_K",
    "BM25_B",
    "FIELDS",
]

# 字段权重：title 最高，breadcrumb 次之，terms/summary 正常
FIELD_BOOST: dict[str, int] = {"title": 6, "breadcrumb": 3, "terms": 2, "summary": 2}
BM25_K = 1.5
BM25_B = 0.75
# summary 字段（LLM 摘要）权重高于旧 excerpt；兼容旧索引（fallback 到 excerpt）
FIELDS: list[str] = ["title", "breadcrumb", "terms", "summary"]

# chunk 字段权重：title 6 / breadcrumb 3 / body 1（与 node-index BM25F 对齐）
CHUNK_FIELD_BOOST: dict[str, int] = {"title": 6, "breadcrumb": 3, "body": 1}


@dataclass(frozen=True)
class BM25Stats:
    """node-index BM25 统计（``build_bm25_stats`` 返回）。

    与 retrieval.js 返回对象字段对齐：``df`` / ``N`` / ``avgLen`` / ``fieldAvgLen``。
    """

    df: dict[str, int]
    N: int
    avgLen: float
    fieldAvgLen: dict[str, float]


def _node_field_text(node: Mapping[str, Any]) -> dict[str, str]:
    """构造 node 的字段文本（summary fallback excerpt，兼容旧索引）。"""
    breadcrumb = node.get("breadcrumb") or []
    terms = node.get("terms") or []
    return {
        "title": node.get("title") or "",
        "breadcrumb": " ".join(breadcrumb) if isinstance(breadcrumb, list) else str(breadcrumb),
        "terms": " ".join(terms) if isinstance(terms, list) else str(terms),
        "summary": node.get("summary") or node.get("excerpt") or "",
    }


def build_bm25_stats(node_index: Mapping[str, Any] | None) -> BM25Stats | None:
    """从 nodeIndex 构建 BM25 统计（retrieval.js L162-196）。

    - DF：per-field 去重累加（同一 token 在同 node 的多 field 出现会被计多次）。
    - totalLen：``tokenize(title+breadcrumb+terms+summary 拼接).length`` 累加
      （拼接会改变 CJK 2-gram 边界，但 JS 实现就是这样）。
    - N = ``len(nodes) or 1``。
    """
    if node_index is None:
        return None
    nodes = node_index.get("nodes") or []
    df: dict[str, int] = {}
    total_len = 0
    field_len: dict[str, int] = {"title": 0, "breadcrumb": 0, "terms": 0, "summary": 0}
    for node in nodes:
        field_text = _node_field_text(node)
        for f in FIELDS:
            toks = tokenize(field_text[f])
            field_len[f] += len(toks)
            # per-field 去重累加 df（new Set(toks)）
            for t in dict.fromkeys(toks):
                df[t] = df.get(t, 0) + 1
        # totalLen 用拼接后的 tokenize（跨字段边界 2-gram）
        total_len += len(
            tokenize(
                field_text["title"]
                + field_text["breadcrumb"]
                + field_text["terms"]
                + field_text["summary"]
            )
        )
    n = len(nodes) or 1
    return BM25Stats(
        df=df,
        N=n,
        avgLen=total_len / n,
        fieldAvgLen={
            "title": field_len["title"] / n,
            "breadcrumb": field_len["breadcrumb"] / n,
            "terms": field_len["terms"] / n,
            "summary": field_len["summary"] / n,
        },
    )


def bm25_score(
    query_tokens: list[str],
    node: Mapping[str, Any],
    stats: BM25Stats,
    weights: Mapping[str, float] | None = None,
) -> float:
    """BM25F 打分（retrieval.js L198-224）。

    - 逐字段独立算 TF/len，IDF 用 BM25+ 风格 ``log(1+(N-df+0.5)/(df+0.5))``。
    - ``norm = 1 - BM25_B + BM25_B * (docLen / (avgLen or 1))``。
    - ``score = idf * (tf*(k+1) / (tf + k*norm)) * FIELD_BOOST[f] * w``。
    - ``weights`` 可选（同义词扩展用），None 时 w=1。
    - 返回未归一化的 total。
    """
    total = 0.0
    field_text = _node_field_text(node)
    for f in FIELDS:
        doc_tokens = tokenize(field_text[f])
        doc_len = len(doc_tokens)
        avg_len = stats.fieldAvgLen.get(f) or 1
        # tfMap: 频次 Map
        tf_map: dict[str, int] = {}
        for t in doc_tokens:
            tf_map[t] = tf_map.get(t, 0) + 1
        for qt in query_tokens:
            tf = tf_map.get(qt) or 0
            if not tf:
                continue
            df = stats.df.get(qt) or 0
            idf = math.log(1 + (stats.N - df + 0.5) / (df + 0.5))
            norm = 1 - BM25_B + BM25_B * (doc_len / (avg_len or 1))
            score = idf * ((tf * (BM25_K + 1)) / (tf + BM25_K * norm))
            w = (weights.get(qt) or 0) if weights is not None else 1
            total += score * FIELD_BOOST[f] * w
    return total


# ══════════════════════════════════════════════════════════════════════════
# 正文 chunk 倒排检索（阶段 1）
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class ChunkStats:
    """chunk BM25F 统计（``build_chunk_stats`` 返回）。

    与 retrieval.js 返回对象字段对齐：``df`` / ``N`` / ``avgLen`` /
    ``fieldAvgLen`` / ``chunks`` / ``_cidMap``（惰性构建）。
    ``_tf`` 是预计算的每 chunk 每字段 TF map（``dict[id_num, dict]``）。

    非 frozen：``_cid_map`` 惰性注入（与 retrieval.js 一致）。
    """

    df: dict[str, int]
    N: int
    avgLen: float
    fieldAvgLen: dict[str, float]
    chunks: list[Mapping[str, Any]]
    # 预计算 TF：chunk_id_str → {field: {token: tf}, "_len": {field: len}}
    _tf: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 惰性构建：cid_num(int) → chunk
    _cid_map: dict[int, Mapping[str, Any]] | None = field(default=None)


def _chunk_id(ch: Mapping[str, Any]) -> str:
    """获取 chunk_id（兼容缺失）。"""
    return ch.get("chunk_id") or ""


def build_chunk_stats(chunks: Any | None) -> ChunkStats | None:
    """从 chunks 构建统计 + 预计算每 chunk 的字段 TF map（retrieval.js L283-323）。

    - DF 按 chunk 合并去重（一个 chunk 内某 token 只算 1 次，无论在几个字段出现）。
    - avgLen 是三字段长度之和 / N（不是单字段）。
    - 预计算 ``_tf``（查询期 ``bm25_score_chunk`` 直接查表，不再 tokenize）。
    """
    if chunks is None:
        return None
    lst = chunks.get("chunks") if isinstance(chunks, Mapping) else chunks
    if lst is None:
        return None
    df: dict[str, int] = {}
    field_len: dict[str, int] = {"title": 0, "breadcrumb": 0, "body": 0}
    _tf: dict[str, dict[str, Any]] = {}
    for ch in lst:
        cid = _chunk_id(ch)
        title_toks = tokenize(ch.get("title") or "")
        bc = ch.get("breadcrumb") or []
        bc_toks = tokenize(" ".join(bc) if isinstance(bc, list) else str(bc))
        body_toks = tokenize(ch.get("body") or "")
        field_len["title"] += len(title_toks)
        field_len["breadcrumb"] += len(bc_toks)
        field_len["body"] += len(body_toks)
        # 预计算每字段的 TF map
        _tf[cid] = {
            "title": _build_tf_map(title_toks),
            "breadcrumb": _build_tf_map(bc_toks),
            "body": _build_tf_map(body_toks),
            "_len": {
                "title": len(title_toks),
                "breadcrumb": len(bc_toks),
                "body": len(body_toks),
            },
        }
        # 合并去重后算 DF（一个 chunk 内某 token 只算 1 次）
        all_toks = list(dict.fromkeys([*title_toks, *bc_toks, *body_toks]))
        for t in all_toks:
            df[t] = df.get(t, 0) + 1
    n = len(lst) or 1
    return ChunkStats(
        df=df,
        N=n,
        avgLen=(field_len["title"] + field_len["breadcrumb"] + field_len["body"]) / n,
        fieldAvgLen={
            "title": field_len["title"] / n,
            "breadcrumb": field_len["breadcrumb"] / n,
            "body": field_len["body"] / n,
        },
        chunks=list(lst),
        _tf=_tf,
        _cid_map=None,
    )


def _build_tf_map(toks: list[str]) -> dict[str, int]:
    """从 token 列表构建频次 map。"""
    m: dict[str, int] = {}
    for t in toks:
        m[t] = m.get(t, 0) + 1
    return m


def bm25_score_chunk(
    query_tokens: list[str],
    chunk: Mapping[str, Any],
    stats: ChunkStats,
    weights: Mapping[str, float] | None = None,
    _tf: Mapping[str, Any] | None = None,
) -> float:
    """chunk 多字段 BM25F 打分（retrieval.js L331-352）。

    - 用预计算的 ``_tf``（通过 ``stats._tf[chunk_id]`` 查表，避免重复 tokenize）。
    - ``CHUNK_FIELD_BOOST = {title:6, breadcrumb:3, body:1}``。
    - ``weights`` 可选，None 时 w=1。

    ``_tf`` 参数：可选，传入预计算 TF（供 ``search_inverted`` 复用，避免重复查
    ``stats._tf``）；None 时从 ``stats._tf[chunk_id]`` 查。
    """
    tf = _tf if _tf is not None else stats._tf.get(_chunk_id(chunk))
    if tf is None:
        return 0.0
    total = 0.0
    for f in ("title", "breadcrumb", "body"):
        tf_map = tf.get(f)
        if not tf_map:
            continue  # 无预计算时跳过（不应发生）
        doc_len = tf["_len"][f]
        avg_len = (stats.fieldAvgLen.get(f) if stats.fieldAvgLen else None) or stats.avgLen or 1
        for qt in query_tokens:
            tfreq = tf_map.get(qt) or 0
            if not tfreq:
                continue
            df = stats.df.get(qt) or 0
            idf = math.log(1 + (stats.N - df + 0.5) / (df + 0.5))
            norm = 1 - BM25_B + BM25_B * (doc_len / (avg_len or 1))
            w = (weights.get(qt) or 0) if weights is not None else 1
            total += (
                idf
                * ((tfreq * (BM25_K + 1)) / (tfreq + BM25_K * norm))
                * CHUNK_FIELD_BOOST[f]
                * w
            )
    return total
