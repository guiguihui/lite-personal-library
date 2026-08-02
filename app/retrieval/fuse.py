"""RRF fusion + RM3 pseudo-relevance feedback (port of retrieval.js L541-614).

严格对齐 retrieval.js：

- ``rrf_fuse`` (L541-561)：Reciprocal Rank Fusion，k=60。
- ``rm3_expand`` (L595-614)：RM3 伪相关反馈，用 BM25 top-M 加权扩展 query term。

对齐 pitfalls（已处理）：

- ``rrf_fuse`` 的 key = ``f"{doc_id}:{node_id}"``（同节点不同 chunk 视为同一证据合并）。
- ``rrf = 1 / (k + i + 1)``，i 是 0-based rank。
- 输出 ``score`` 字段覆写为 ``Math.round(rrf*1000)/1000``（3 位小数），
  ``rrf_score`` 保留原 rrf 浮点。用 ``_js_round`` 实现 JS 风格舍入。
- item 保留最高 ``score`` 的那条（不是最高 rrf 贡献的那条）——
  ``if (h.score||0) > (entry.item.score||0): entry.item = h``。
- 输出未截断（截断在调用方 ``search_multi_path``）。
- ``rm3_expand`` 的 ``term_scores`` 用 ``h.score``（BM25 分，可能已被 round 过）加权
  ——不是 ``rrf_score``。
- ``expanded`` 取前 ``top_expansions=15`` 个 term。
- 返回 ``[...new Set([...query_tokens, ...expanded])]``——原始在前，去重。
- 注释提到 RM3 插值 α=0.4 但实现只是去重合并，未做插值权重
  （注释与代码不符，以代码为准）。
"""

from __future__ import annotations

from dataclasses import replace

from app.retrieval.search import Hit
from app.retrieval.search import _js_round  # 复用 JS 风格舍入
from app.retrieval.tokenizer import tokenize

__all__ = ["rrf_fuse", "rm3_expand"]


def rrf_fuse(paths: list[list[Hit] | None], k: int = 60) -> list[Hit]:
    """Reciprocal Rank Fusion（retrieval.js L541-561）。

    ``score(d) = Σ 1/(k + rank_i(d))``，k=60。
    hits 识别键：``doc_id:node_id``（同节点不同 chunk 视为同一证据）。

    - 输出 ``score`` 覆写为 ``Math.round(rrf*1000)/1000``（3 位小数）。
    - ``rrf_score`` 保留原 rrf 浮点。
    - item 保留最高 BM25 分的那条。
    - 输出未截断（按 rrf_score 降序）。
    """
    scores: dict[tuple[str, str, str], dict[str, Hit | float]] = {}
    for hits in paths:
        if not hits:
            continue
        for i, h in enumerate(hits):
            key = (
                ("stable", h.doc_uid, h.node_key)
                if h.doc_uid is not None and h.node_key is not None
                else (
                    "legacy",
                    str(h.node.get("doc_id")),
                    str(h.node.get("node_id")),
                )
            )
            rrf = 1 / (k + i + 1)
            entry = scores.get(key)
            if entry is not None:
                entry["rrf"] = entry["rrf"] + rrf  # type: ignore[operator]
                # 保留最高 BM25 分 + chunk 信息（来自任意一路）
                if (h.score or 0) > (entry["item"].score or 0):  # type: ignore[union-attr]
                    entry["item"] = h
            else:
                scores[key] = {"item": h, "rrf": rrf}
    out: list[Hit] = []
    for e in scores.values():
        item: Hit = e["item"]  # type: ignore[assignment]
        rrf: float = e["rrf"]  # type: ignore[assignment]
        # 复制并覆写 score / rrf_score（不修改原 Hit，保持不可变）
        fused = replace(
            item,
            score=_js_round(rrf, 3),
            rrf_score=rrf,
        )
        out.append(fused)
    out.sort(key=lambda h: h.rrf_score or 0, reverse=True)
    return out


def rm3_expand(
    query_tokens: list[str],
    hits: list[Hit],
    M: int = 10,
    top_expansions: int = 15,
) -> list[str]:
    """RM3 伪相关反馈（retrieval.js L595-614）。

    - 用 BM25 top-M 当反馈集扩展 query term。
    - ``term_scores`` 用 ``h.score``（BM25 分）加权频次。
    - ``expanded`` 取前 ``top_expansions`` 个 term（按 term_scores 降序）。
    - 返回 ``[...new Set([...query_tokens, ...expanded])]``（原始在前，去重）。
    - topM 空 → 返回 query_tokens 原样。
    """
    top_m = hits[:M]
    if not top_m:
        return list(query_tokens)
    term_scores: dict[str, float] = {}
    for h in top_m:
        n = h.node
        title = n.get("title") or ""
        terms = n.get("terms") or []
        terms_str = " ".join(terms) if isinstance(terms, list) else str(terms)
        summary = n.get("summary") or n.get("excerpt") or ""
        text = f"{title} {terms_str} {summary}"
        tks = tokenize(text)
        for t in tks:
            term_scores[t] = term_scores.get(t, 0) + (h.score or 0)
    expanded = [
        t for t, _ in sorted(term_scores.items(), key=lambda x: x[1], reverse=True)[:top_expansions]
    ]
    # 原始 tokens 优先（去重，保序）
    seen: set[str] = set()
    out: list[str] = []
    for t in [*query_tokens, *expanded]:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out
