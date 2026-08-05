"""Benchmark harness (port of tests/retrieval/harness.js).

加载索引 + golden.json，跑检索管线（search → RM3 → lexicalRerank，
与 chat.js retrieveContext 前段一致），输出 Recall@10 / MRR@10 / 无答案准确率，
按 category 与 confidence 分桶。

对齐 pitfalls（已处理）：

- harness.js 用 ``static/pageindex`` 路径，但实际数据在 ``data/pageindex``。
  本模块接受 ``pageindex_dir`` 参数，默认 ``data/pageindex``（相对项目根）。
- ``run_pipeline``：倒排就绪→``search_multi_path``；否则回退 ``search``。
  RM3 重算用 ``bm25_score``（node-index 版，FIELD_BOOST title6/breadcrumb3/
  terms2/summary2），不是 chunk 的 ``bm25_score_chunk``。
- ``evaluate`` 对 no_answer 题（``expect_doc_ids`` 空）：
  ``correct = (not hits) or confidence == "low"``。
- MRR：第一个命中的期望 doc 的倒数排名（top-K 内），未命中→0。
  Recall 是二值（matched.length>0?1:0）。
- 分桶：每题记 3 个桶（all, cat:{category}, conf:{confidence}），
  每题只调一次 record per key（n 不重复累加）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.retrieval.bm25 import BM25Stats, ChunkStats, build_bm25_stats, build_chunk_stats, bm25_score
from app.retrieval.confidence import classify_confidence_multi, compute_confidence_signals
from app.retrieval.fuse import rm3_expand
from app.retrieval.rerank import lexical_rerank
from app.retrieval.search import Hit, search, search_multi_path
from app.retrieval.tokenizer import tokenize

__all__ = ["BenchmarkResult", "Bucket", "run_golden", "run_pipeline", "evaluate"]

_LOG = logging.getLogger(__name__)

# 默认 pageindex 目录（相对项目根）。harness.js 用 static/pageindex，
# 但实际数据在 data/pageindex。
_DEFAULT_PAGEINDEX_DIR = "data/pageindex"


def _to_fixed(x: float, digits: int = 3) -> str:
    """JS ``Number.toFixed`` 风格格式化（半数向上，与 harness.js 一致）。

    JS ``toFixed`` 用 round-half-up（0.0625 → "0.063"），
    Python ``:.3f`` 用 banker's rounding（0.0625 → "0.062"）。
    用此函数保证显示与 harness.js 完全一致。
    """
    factor = 10 ** digits
    # 半数向上：floor(x*factor + 0.5) / factor
    import math

    rounded = math.floor(x * factor + 0.5) / factor
    return f"{rounded:.{digits}f}"


@dataclass
class Bucket:
    """分桶统计（与 harness.js ``byBucket[key]`` 对齐）。"""

    n: int = 0
    recall: float = 0.0
    mrr: float = 0.0
    correct: int = 0

    def record(self, recall: float, mrr: float, correct: bool) -> None:
        self.n += 1
        self.recall += recall
        self.mrr += mrr
        self.correct += 1 if correct else 0

    def fmt(self, topk: int) -> str:
        if self.n == 0:
            return "n/a"
        return (
            f"R@{topk}={_to_fixed(self.recall / self.n)} "
            f"MRR={_to_fixed(self.mrr / self.n)} "
            f"acc={_to_fixed(self.correct / self.n)} (n={self.n})"
        )


@dataclass
class BenchmarkResult:
    """基准测试结果（与 harness.js 输出对齐）。"""

    n: int
    topk: int
    overall: Bucket
    by_category: dict[str, Bucket] = field(default_factory=dict)
    by_confidence: dict[str, Bucket] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    inverted_ready: bool = False

    def summary(self) -> str:
        """格式化汇总（与 harness.js console.log 输出对齐）。

        注意：标题行严格对齐 harness.js（``检索基准 (n=N, TOPK=K)``），
        不附加 ``inverted=yes``——``inverted_ready`` 仅作为字段保留供程序访问。
        """
        lines = [
            "\n═══════════════════════════════════════════════════════",
            f"  检索基准 (n={self.n}, TOPK={self.topk})",
            "═══════════════════════════════════════════════════════",
            f"  总体:        {self.overall.fmt(self.topk)}",
            "\n  按 category:",
        ]
        for k, b in self.by_category.items():
            lines.append(f"    {k:<20} {b.fmt(self.topk)}")
        lines.append("\n  按 confidence:")
        for k, b in self.by_confidence.items():
            lines.append(f"    {k:<20} {b.fmt(self.topk)}")
        hard_fails = [f for f in self.failures if f["q"].get("confidence") == "hard"]
        if hard_fails:
            n_hard = self.by_confidence.get("hard", Bucket()).n
            lines.append(
                f"\n  ⚠ hard 题失败 ({len(hard_fails)}/{n_hard}):"
            )
            for f in hard_fails[:20]:
                top1 = f["res"]["hits"][0] if f["res"]["hits"] else None
                got = top1.node["doc_id"] if top1 else "(empty)"
                expect = ",".join(f["q"].get("expect_doc_ids", []))
                lines.append(
                    f"    ✗ [{f['q'].get('category')}] \"{f['q'].get('query')}\" "
                    f"expect={expect} got={got} conf={f['res']['confidence']}"
                )
        lines.append("")
        return "\n".join(lines)


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_indices(
    pageindex_dir: str,
    use_inverted: bool = True,
) -> tuple[dict, dict, BM25Stats, dict | None, ChunkStats | None, bool]:
    """加载索引（与 harness.js L26-46 对齐）。

    返回 ``(node_index, global_index, stats, inverted_index, chunk_stats, inverted_ready)``。
    """
    node_index = _load_json(os.path.join(pageindex_dir, "node-index.json"))
    global_index = _load_json(os.path.join(pageindex_dir, "global-index.json"))
    stats = build_bm25_stats(node_index)
    if stats is None:
        stats = BM25Stats(df={}, N=1, avgLen=0.0, fieldAvgLen={})
    inverted_index = None
    chunk_stats = None
    inverted_ready = False
    if use_inverted:
        inv_path = os.path.join(pageindex_dir, "inverted-index.json")
        chunks_path = os.path.join(pageindex_dir, "chunks.json")
        if os.path.exists(inv_path) and os.path.exists(chunks_path):
            inv_data = _load_json(inv_path)
            chunks_data = _load_json(chunks_path)
            inverted_index = inv_data.get("postings") if isinstance(inv_data, Mapping) else None
            chunk_stats = build_chunk_stats(chunks_data)
            inverted_ready = inverted_index is not None and chunk_stats is not None
    return node_index, global_index, stats, inverted_index, chunk_stats, inverted_ready


def run_pipeline(
    query: str,
    node_index: dict,
    stats: BM25Stats,
    global_index: dict | None,
    inverted_index: dict | None,
    chunk_stats: ChunkStats | None,
) -> list[Hit]:
    """检索管线（与 harness.js L57-74 对齐）。

    - 倒排就绪→``search_multi_path``；否则回退 ``search``。
    - RM3 重算用 ``bm25_score``（node-index 版）。
    - ``lexical_rerank`` 用原始 ``orig_tokens``（带频次 tokenize，不是 unique）。
    """
    if inverted_index is not None and chunk_stats is not None:
        hits = search_multi_path(query, inverted_index, chunk_stats, global_index, 50)
    else:
        hits = search(query, node_index, stats, 50)
    if not hits:
        return []
    orig_tokens = tokenize(query)
    expanded_tokens = rm3_expand(orig_tokens, hits)
    if len(expanded_tokens) > len(orig_tokens):
        for h in hits:
            h.score = round(bm25_score(expanded_tokens, h.node, stats) * 100) / 100
        hits = [h for h in hits if h.score > 0]
        hits.sort(key=lambda h: h.score, reverse=True)
    hits = lexical_rerank(orig_tokens, query, hits)
    return hits


def evaluate(
    q: Mapping[str, Any],
    node_index: dict,
    stats: BM25Stats,
    global_index: dict | None,
    inverted_index: dict | None,
    chunk_stats: ChunkStats | None,
    topk: int = 10,
) -> dict[str, Any]:
    """评测单题（与 harness.js L79-114 对齐）。

    - Recall@K：期望 doc_id 是否出现在 top-K 命中里。
    - no_answer 题（expect_doc_ids 空）：期望检索结果为空或 confidence=low。
    """
    hits = run_pipeline(query=q["query"], node_index=node_index, stats=stats,
                        global_index=global_index, inverted_index=inverted_index,
                        chunk_stats=chunk_stats)
    top = hits[:topk]
    hit_doc_ids = {h.node["doc_id"] for h in top}
    signals = compute_confidence_signals(q["query"], hits)
    confidence = classify_confidence_multi(signals)

    expect_doc_ids = q.get("expect_doc_ids", [])
    if len(expect_doc_ids) == 0:
        # 无答案题：无命中（或仅 confidence=low）算正确
        correct = (not hits) or confidence == "low"
        return {
            "correct": correct,
            "recall": 1 if correct else 0,
            "mrr": 0,
            "hits": top,
            "confidence": confidence,
            "kind": "no_answer",
        }

    # 期望至少一个期望 doc 出现在 top-K
    matched = [doc_id for doc_id in expect_doc_ids if doc_id in hit_doc_ids]
    recall = 1 if matched else 0

    # MRR：第一个命中的期望 doc 的倒数排名
    mrr = 0
    for i, h in enumerate(top):
        if h.node["doc_id"] in expect_doc_ids:
            mrr = 1 / (i + 1)
            break

    return {
        "correct": recall == 1,
        "recall": recall,
        "mrr": mrr,
        "hits": top,
        "confidence": confidence,
        "kind": "answerable",
    }


def run_golden(
    pageindex_dir: str | None = None,
    golden_path: str | None = None,
    topk: int = 10,
    use_inverted: bool = True,
    filter_category: str | None = None,
) -> BenchmarkResult:
    """跑 golden.json 评测（与 harness.js 主流程对齐）。

    Args:
        pageindex_dir: pageindex 目录。None → 项目根 ``data/pageindex``。
        golden_path: golden.json 路径。None → ``tests/retrieval/golden.json``。
        topk: Recall 截断（默认 10）。
        use_inverted: 是否使用倒排索引（False 强制回退线性 BM25）。
        filter_category: 只跑某类（None 跑全量）。

    Returns:
        BenchmarkResult。
    """
    # 定位项目根（向上找 pyproject.toml）
    root = os.getcwd()
    if pageindex_dir is None:
        pageindex_dir = os.path.join(root, _DEFAULT_PAGEINDEX_DIR)
    if golden_path is None:
        golden_path = os.path.join(root, "tests", "retrieval", "golden.json")

    node_index, global_index, stats, inverted_index, chunk_stats, inverted_ready = _load_indices(
        pageindex_dir, use_inverted=use_inverted
    )
    golden = _load_json(golden_path)

    questions = (
        [q for q in golden if q.get("category") == filter_category]
        if filter_category
        else list(golden)
    )

    overall = Bucket()
    by_category: dict[str, Bucket] = {}
    by_confidence: dict[str, Bucket] = {}
    failures: list[dict[str, Any]] = []

    def record(bucket: Bucket, recall: float, mrr: float, correct: bool) -> None:
        bucket.record(recall, mrr, correct)

    for q in questions:
        res = evaluate(
            q, node_index, stats, global_index, inverted_index, chunk_stats, topk=topk
        )
        correct = res["correct"]
        # 每题记 3 个桶（all, cat:{category}, conf:{confidence}）
        record(overall, res["recall"], res["mrr"], correct)
        cat = q.get("category", "unknown")
        if cat not in by_category:
            by_category[cat] = Bucket()
        record(by_category[cat], res["recall"], res["mrr"], correct)
        conf = q.get("confidence", "unknown")
        if conf not in by_confidence:
            by_confidence[conf] = Bucket()
        record(by_confidence[conf], res["recall"], res["mrr"], correct)

        if not correct:
            failures.append({"q": q, "res": res})

    return BenchmarkResult(
        n=len(questions),
        topk=topk,
        overall=overall,
        by_category=by_category,
        by_confidence=by_confidence,
        failures=failures,
        inverted_ready=inverted_ready,
    )
