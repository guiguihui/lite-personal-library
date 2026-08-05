"""Confidence classification (port of retrieval.js L754-807).

严格对齐 retrieval.js：

- ``classify_confidence`` (L754-759)：旧版（已废弃，保留兼容）。
- ``classify_confidence_multi`` (L767-778)：多信号绝对分（阶段 5 核心）。
- ``compute_confidence_signals`` (L781-807)：从 rerank 后的 hits + query 提取信号。

对齐 pitfalls（已处理）：

- ``classify_confidence_multi`` 的判定顺序是短路的，必须严格按
  coverage<0.5 → high 判定 → medium 判定 → low 顺序，不能调换。
  阈值 0.5/0.7/0.05/0.04/0.01 是经验值，原样保留。
- ``compute_confidence_signals`` 的 ``margin = hits[0].rrf_score - hits[1].rrf_score``：
  hits[1] 可能为 None（只有 1 条结果），用 ``hits[1].rrf_score if len(hits)>1
  and hits[1].rrf_score else 0``。
- ``source_count`` 用 ``hits[:10]`` 的 doc_id 去重计数。
- ``coverage``：top5 里命中 query token 数 / 总 query token 数
  （qToks 空→0）。
- ``title_hit``：top1 的 (title+" "+breadcrumb.join(" ")).toLowerCase()，
  qToks 任一 t in titleText。
"""

from __future__ import annotations

from typing import Any, Mapping

from app.retrieval.search import Hit
from app.retrieval.tokenizer import tokenize_unique

__all__ = [
    "classify_confidence",
    "classify_confidence_multi",
    "compute_confidence_signals",
    "ConfidenceSignals",
]


class ConfidenceSignals(dict):
    """Confidence 信号（与 retrieval.js 返回对象对齐）。

    ``{coverage, rrf_score, title_hit, margin, source_count}``。
    继承 dict 以兼容 ``**signals`` 解包与 ``signals.get(key, default)``。
    """

    __slots__ = ()

    @property
    def coverage(self) -> float:
        return self.get("coverage", 0)  # type: ignore[return-value]

    @property
    def rrf_score(self) -> float:
        return self.get("rrf_score", 0)  # type: ignore[return-value]

    @property
    def title_hit(self) -> bool:
        return self.get("title_hit", False)  # type: ignore[return-value]

    @property
    def margin(self) -> float:
        return self.get("margin", 0)  # type: ignore[return-value]

    @property
    def source_count(self) -> int:
        return self.get("source_count", 1)  # type: ignore[return-value]


def classify_confidence(top_rerank: float, source_count: int) -> str:
    """旧版 confidence（retrieval.js L754-759，已废弃，保留兼容）。

    建议用 :func:`classify_confidence_multi`。
    """
    if top_rerank >= 0.6 and source_count >= 2:
        return "high"
    if top_rerank >= 0.3 or (top_rerank >= 0.15 and source_count >= 2):
        return "medium"
    return "low"


def classify_confidence_multi(signals: Mapping[str, Any]) -> str:
    """多信号 confidence（retrieval.js L767-778，阶段 5 核心）。

    信号（实测鉴别力排序）：
      coverage  最强：good 查询 ~1.0，no_answer 0.25-0.57
      rrf_score 次强：good ≥0.06，no_answer ≤0.047
      title_hit  补充：top1 标题命中 query 核心词
      margin    补充：top1-top2 rrf_score 差（明显领先更可信）

    判定顺序（短路，必须按此顺序）：

    1. ``coverage < 0.5`` → ``"low"``
    2. ``coverage >= 0.7 && rrf_score >= 0.05 && (title_hit || source_count >= 2 || margin >= 0.01)`` → ``"high"``
    3. ``coverage >= 0.5 || rrf_score >= 0.04`` → ``"medium"``
    4. else → ``"low"``
    """
    coverage = signals.get("coverage", 0)
    rrf_score = signals.get("rrf_score", 0)
    title_hit = signals.get("title_hit", False)
    margin = signals.get("margin", 0)
    source_count = signals.get("source_count", 1)
    # 低覆盖直接 low（大量 query 词未命中 → 很可能无答案）
    if coverage < 0.5:
        return "low"
    # 高覆盖 + 强 rrf + (标题命中或多源) → high
    if coverage >= 0.7 and rrf_score >= 0.05 and (title_hit or source_count >= 2 or margin >= 0.01):
        return "high"
    # 中等覆盖 或 中等 rrf → medium
    if coverage >= 0.5 or rrf_score >= 0.04:
        return "medium"
    return "low"


def compute_confidence_signals(query: str, hits: list[Hit]) -> ConfidenceSignals:
    """计算 confidence 信号（retrieval.js L781-807）。

    从 rerank 后的 hits + query 提取。hits 空返回全零信号。
    """
    if not hits:
        return ConfidenceSignals(
            coverage=0, rrf_score=0, title_hit=False, margin=0, source_count=0
        )
    q_toks = tokenize_unique(query)
    top5 = hits[:5]
    # coverage：top5 里命中了多少 query token
    hit_tk: set[str] = set()
    for h in top5:
        for qt in q_toks:
            if qt in (h.positions or {}):
                hit_tk.add(qt)
    coverage = (len(hit_tk) / len(q_toks)) if q_toks else 0
    rrf_score = hits[0].rrf_score or 0
    # margin = hits[0].rrf_score - hits[1].rrf_score（hits[1] 可能为 None）
    if len(hits) > 1 and hits[1].rrf_score is not None:
        margin = (hits[0].rrf_score or 0) - (hits[1].rrf_score or 0)
    else:
        margin = 0.0
    # title_hit：top1 标题/breadcrumb 含 query 的任一核心词
    top1 = hits[0]
    title = top1.node.get("title") or ""
    bc = top1.node.get("breadcrumb") or []
    bc_str = " ".join(bc) if isinstance(bc, list) else str(bc)
    title_text = (title + " " + bc_str).lower()
    title_hit = any(t in title_text for t in q_toks)
    source_count = len({h.node.get("doc_id") for h in hits[:10]})
    return ConfidenceSignals(
        coverage=coverage,
        rrf_score=rrf_score,
        title_hit=title_hit,
        margin=margin,
        source_count=source_count,
    )
