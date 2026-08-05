"""Lexical rerank + MMR (port of retrieval.js L620-749).

严格对齐 retrieval.js：

- ``lexical_rerank`` (L620-696)：proximity + phrase + coverage + BM25 归一化加权。
- ``shingle`` (L700-706)：4-gram shingle。
- ``jaccard`` (L708-713)：标准 Jaccard。
- ``mmr_select`` (L715-740)：MMR 去冗余，λ=0.6，maxChunks=8。
- ``estimate_tokens`` (L744-749)：token 估算（中文 chars/1.5，英文 chars/4）。

对齐 pitfalls（已处理）：

- ``RERANK_WEIGHTS = {bm25:0.5, prox:0.2, phrase:0.2, cov:0.1}``。
- proximity：按字段分别算 query tokens 的最小跨度，``score = 1/(span+1)``，
  取最优字段。``bestProx===0`` 时若 anyHit 给 0.2。
- phrase：中文用 2 字滑窗子串匹配原文（不是 2-gram token 拼接），
  英文用 token bigram。``phraseScore = min(phraseHits/phraseTotal, 1)``。
- coverage：``hitTk.size / queryTokens.length``（已是 [0,1]，不归一化）。
- min-max 归一化：``range = mx - mn || 1``（mx==mn 时 range=1，所有归一化值为 0）。
  对 bm25/prox/phrase 三项分别算，coverage 不归一化。
- ``rerankScore`` 写回 ``h.rerank_score``（retrieval.js 原地修改 hits）。
  Python 移植保持不可变：返回新 Hit 列表（带 rerank_score）。
- ``shingle``：``tokens.length < k`` 时返回 ``Set(tokens)``（不是空集，是单 token 集合）。
- ``jaccard``：``inter / (sizeA + sizeB - inter)``，空集返回 0。
- ``mmr_select``：第一个（最高分）直接选，之后每轮选
  ``mmr = λ*rerankScore - (1-λ)*maxSim`` 最大的。``maxChunks=8``。
  原地修改 contexts（``c._shingle`` 注入）——Python 移植用独立 dict 避免污染输入。
"""

from __future__ import annotations

import math
import re
from typing import Any

from app.retrieval.search import Hit
from app.retrieval.tokenizer import tokenize, tokenize_unique

__all__ = [
    "lexical_rerank",
    "shingle",
    "jaccard",
    "mmr_select",
    "estimate_tokens",
    "RERANK_WEIGHTS",
]

RERANK_WEIGHTS: dict[str, float] = {"bm25": 0.5, "prox": 0.2, "phrase": 0.2, "cov": 0.1}

_CJK_RE = re.compile(r"[一-鿿]+")
_EN_LOWER_RE = re.compile(r"[a-z][a-z0-9]{1,}")


def lexical_rerank(
    query_tokens: list[str], raw_query: str, hits: list[Hit]
) -> list[Hit]:
    """词法精排（retrieval.js L620-696）。

    - 四子分：proximity / phrase / coverage / bm25。
    - bm25/prox/phrase 三者 min-max 归一化，coverage 已 [0,1]。
    - ``rerankScore = 0.5*_bm25 + 0.2*_prox + 0.2*_phrase + 0.1*cov``。
    - 按 rerankScore 降序返回（新 Hit 列表，带 rerank_score）。
    """
    if not hits:
        return hits
    raw = raw_query or ""
    cjk_part = "".join(_CJK_RE.findall(raw))
    en_tokens = _EN_LOWER_RE.findall(raw.lower())

    # 计算各子分
    sub: list[dict[str, Any]] = []
    for h in hits:
        pos_set = h.positions or {}
        # (a) proximity：按字段分别算 query tokens 的最小跨度，取最优字段
        best_prox = 0.0
        for f in ("title", "breadcrumb", "terms", "summary"):
            positions_in_field: list[int] = []
            for qt in query_tokens:
                fp = pos_set.get(qt)
                if fp and f in fp:
                    positions_in_field.append(fp[f])
            if len(positions_in_field) >= 2:
                span = max(positions_in_field) - min(positions_in_field)
                score = 1 / (span + 1)
                if score > best_prox:
                    best_prox = score
        if best_prox == 0:
            # 只命中单个 token，给基础分
            any_hit = any(fp for fp in pos_set.values() if fp)
            best_prox = 0.2 if any_hit else 0
        # (b) phrase：检查 query 的连续片段是否在 title/breadcrumb 精确出现
        title = h.node.get("title") or ""
        bc = h.node.get("breadcrumb") or []
        bc_str = " ".join(bc) if isinstance(bc, list) else str(bc)
        title_text = (title + " " + bc_str).lower()
        phrase_hits = 0
        phrase_total = 0
        # 中文 2 字滑窗
        for i in range(len(cjk_part) - 1):
            phrase_total += 1
            if cjk_part[i : i + 2].lower() in title_text:
                phrase_hits += 1
        # 英文 token bigram
        for i in range(len(en_tokens) - 1):
            phrase_total += 1
            if (en_tokens[i] + " " + en_tokens[i + 1]) in title_text:
                phrase_hits += 1
        phrase_score = min(phrase_hits / phrase_total, 1) if phrase_total else 0
        # (c) coverage：命中 query token 数 / 总 query token 数
        hit_tk = {qt for qt in query_tokens if qt in pos_set}
        cov_score = (len(hit_tk) / len(query_tokens)) if query_tokens else 0
        sub.append({"h": h, "bm25": h.score, "prox": best_prox, "phrase": phrase_score, "cov": cov_score})

    # min-max 归一化各子分
    def norm(key: str) -> None:
        vals = [s[key] for s in sub]
        mn = min(vals)
        mx = max(vals)
        rng = (mx - mn) or 1
        for s in sub:
            s["_" + key] = (s[key] - mn) / rng

    norm("bm25")
    norm("prox")
    norm("phrase")
    # coverage 已经是 [0,1]，无需归一化
    # 加权求和 → rerankScore
    out: list[Hit] = []
    for s in sub:
        rerank_score = (
            RERANK_WEIGHTS["bm25"] * s["_bm25"]
            + RERANK_WEIGHTS["prox"] * s["_prox"]
            + RERANK_WEIGHTS["phrase"] * s["_phrase"]
            + RERANK_WEIGHTS["cov"] * s["cov"]
        )
        h: Hit = s["h"]
        # 返回新 Hit（带 rerank_score），不修改原 Hit
        # 保留 V3 稳定引用字段(doc_key/doc_uid/generation/view_id/...),
        # 供 search_view 溯源使用。
        out.append(
            Hit(
                node=h.node,
                score=h.score,
                tokens=h.tokens,
                positions=h.positions,
                chunk=h.chunk,
                rrf_score=h.rrf_score,
                rerank_score=rerank_score,
                generation=h.generation,
                view_id=h.view_id,
                doc_key=h.doc_key,
                doc_uid=h.doc_uid,
                segment_hash=h.segment_hash,
                local_id=h.local_id,
                node_key=h.node_key,
            )
        )
    out.sort(key=lambda h: h.rerank_score or 0, reverse=True)
    return out


def shingle(text: str | None, k: int = 4) -> set[str]:
    """4-gram shingle（retrieval.js L700-706）。

    - ``tokens.length < k`` 时返回 ``Set(tokens)``（不是空集，是单 token 集合）。
    - shingle 元素是 ``tokens[i:i+k]`` 用空格连接的字符串。
    """
    tokens = tokenize(text)
    if len(tokens) < k:
        return set(tokens)
    shingles: set[str] = set()
    for i in range(len(tokens) - k + 1):
        shingles.add(" ".join(tokens[i : i + k]))
    return shingles


def jaccard(set_a: set[str], set_b: set[str]) -> float:
    """标准 Jaccard（retrieval.js L708-713）。

    ``inter / (sizeA + sizeB - inter)``，任一空集返回 0。
    """
    if not set_a or not set_b:
        return 0
    inter = sum(1 for s in set_a if s in set_b)
    return inter / (len(set_a) + len(set_b) - inter)


def mmr_select(
    contexts: list[dict[str, Any]],
    lambda_: float = 0.6,
    max_chunks: int = 8,
) -> list[dict[str, Any]]:
    """MMR 去冗余（retrieval.js L715-740）。

    - λ=0.6 偏相关但保留多样性。contexts 须已按 rerankScore 降序，且含 ``.text``。
    - 第一个（最高分）直接选。
    - 之后每轮选 ``mmr = λ*rerankScore - (1-λ)*maxSim`` 最大的。
    - ``max_chunks=8``。
    - 原地修改 contexts（``c._shingle`` 注入）——Python 移植用独立 dict 避免污染输入。
    """
    if len(contexts) <= 1:
        return list(contexts)
    # 预计算 shingle（不污染输入）
    shingles = {id(c): shingle(c.get("text") or "") for c in contexts}
    selected = [contexts[0]]
    remaining = list(contexts[1:])
    while len(selected) < max_chunks and remaining:
        best_idx = 0
        best_score = float("-inf")
        for i, cand in enumerate(remaining):
            max_sim = 0.0
            cand_sh = shingles[id(cand)]
            for s in selected:
                sim = jaccard(cand_sh, shingles[id(s)])
                if sim > max_sim:
                    max_sim = sim
            mmr = lambda_ * (cand.get("rerankScore") or 0) - (1 - lambda_) * max_sim
            if mmr > best_score:
                best_score = mmr
                best_idx = i
        selected.append(remaining.pop(best_idx))
    return selected


def estimate_tokens(text: str | None) -> int:
    """Token 估算（retrieval.js L744-749）。

    近似：中文 chars/1.5，英文 chars/4。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return math.ceil(cjk / 1.5 + other / 4)
