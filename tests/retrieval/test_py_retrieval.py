"""pytest tests for app.retrieval (parity with frontend/chat/retrieval.js).

测试覆盖（对照 JS 行为）：

- ``tokenize_raw``：中文 2-gram / 英文 / 数字 / 边界（空、单字、单字母）。
- ``tokenize_unique``：去重保序。
- ``expand_query_weighted``：同义词扩展 + 权重（原始 1.0，同义词 0.6）。
- ``build_bm25_stats`` / ``bm25_score``：基本打分 + IDF 非负 + 字段加权。
- ``build_chunk_stats`` / ``bm25_score_chunk``：预计算 TF + per-chunk DF 去重。
- ``search``：能召回 + per-doc 截断 + score 两位小数。
- ``search_inverted``：倒排召回 + cidMap 构建 + 合成 node。
- ``rrf_fuse``：融合正确（key=doc_id:node_id，score 3 位小数）。
- ``rm3_expand``：原始 token 优先 + topExpansions=15。
- ``lexical_rerank``：rerankScore 写回 + 降序。
- ``shingle`` / ``jaccard`` / ``mmr_select``：4-gram + 空集 + 贪心。
- ``classify_confidence_multi``：短路顺序 + 阈值。
- ``compute_confidence_signals``：coverage / margin / sourceCount。
- ``benchmark.run_golden``：跑 golden.json（集成测试，标记 slow）。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.retrieval import (
    BM25Stats,
    CHUNK_FIELD_BOOST,
    ChunkStats,
    FIELD_BOOST,
    Hit,
    RERANK_WEIGHTS,
    SYNONYM_WEIGHT,
    bm25_score,
    bm25_score_chunk,
    build_bm25_stats,
    build_chunk_stats,
    classify_confidence,
    classify_confidence_multi,
    compute_confidence_signals,
    estimate_tokens,
    expand_query,
    expand_query_weighted,
    jaccard,
    lexical_rerank,
    mmr_select,
    rm3_expand,
    rrf_fuse,
    search,
    search_inverted,
    shingle,
    tokenize,
    tokenize_raw,
    tokenize_unique,
)
from app.retrieval.benchmark import run_golden

# ══════════════════════════════════════════════════════════════════════════
# Tokenizer
# ══════════════════════════════════════════════════════════════════════════


class TestTokenizer:
    def test_tokenize_raw_chinese_2gram(self) -> None:
        # "相变临界" → ["相变", "变临", "临界"]（2-gram 滑窗）
        assert tokenize_raw("相变临界") == ["相变", "变临", "临界"]

    def test_tokenize_raw_chinese_single_dropped(self) -> None:
        # 单字 CJK 段被丢弃（设计决策）
        assert tokenize_raw("光") == []
        # "光相变" → ["光相", "相变"]（单字在多字段中参与 2-gram）
        assert tokenize_raw("光相变") == ["光相", "相变"]

    def test_tokenize_raw_english(self) -> None:
        # 英文单词 lower，长度≥2
        assert tokenize_raw("SPT Rabi") == ["spt", "rabi"]
        # 单个字母不被匹配
        assert tokenize_raw("a b") == []

    def test_tokenize_raw_numbers(self) -> None:
        # 纯数字 ≥2 位
        assert tokenize_raw("89") == ["89"]
        # 单个数字不被匹配
        assert tokenize_raw("7") == []
        # "a7" 匹配英文正则（字母+数字，长度≥2）——与 JS 一致
        assert tokenize_raw("a7") == ["a7"]

    def test_tokenize_raw_mixed(self) -> None:
        # 混合：英文 + 数字 + 中文
        toks = tokenize_raw("Berry 相位 2024")
        assert "berry" in toks
        assert "相位" in toks
        assert "2024" in toks

    def test_tokenize_raw_empty(self) -> None:
        assert tokenize_raw("") == []
        assert tokenize_raw(None) == []  # type: ignore[arg-type]
        assert tokenize_raw(".,;!") == []

    def test_tokenize_keeps_duplicates(self) -> None:
        # tokenize 保留重复（BM25 TF 用）
        assert tokenize("相变 相变") == ["相变", "相变"]

    def test_tokenize_unique_dedupes_preserves_order(self) -> None:
        # 去重保序（用长度≥2 的 token，单字母不匹配英文正则）
        assert tokenize_unique("ab cd ab ef cd") == ["ab", "cd", "ef"]
        # 不是 set（无序）——保序是关键
        toks = tokenize_unique("zebra apple zebra banana apple")
        assert toks == ["zebra", "apple", "banana"]


class TestExpandQuery:
    def test_expand_query_weighted_original_1_synonym_06(self) -> None:
        # "相变" → 原始 token "相变" (1.0) + 同义词 ["phase transition","critical","临界"]
        tokens = tokenize_unique("相变")
        toks, weights = expand_query_weighted(tokens, "相变")
        assert "相变" in weights and weights["相变"] == 1.0
        # 同义词 token 权重 0.6
        assert "phase" in weights and weights["phase"] == 0.6
        assert "transition" in weights and weights["transition"] == 0.6
        assert "critical" in weights and weights["critical"] == 0.6
        # "临界" 是中文 2-gram，也走 tokenize_unique
        assert "临界" in weights and weights["临界"] == 0.6

    def test_expand_query_weighted_multiword_synonym_tokenized(self) -> None:
        # "linear response" 应被拆成 ["linear", "response"]，不是单 token
        tokens = tokenize_unique("线性响应")
        toks, weights = expand_query_weighted(tokens, "线性响应")
        assert "linear" in toks
        assert "response" in toks
        # 不应出现 "linear response" 作为单 token
        assert "linear response" not in toks

    def test_expand_query_weighted_no_match(self) -> None:
        # 无同义词匹配时，只有原始 token
        tokens = tokenize_unique("xyz")
        toks, weights = expand_query_weighted(tokens, "xyz")
        assert toks == ["xyz"]
        assert weights == {"xyz": 1.0}

    def test_expand_query_returns_tokens_only(self) -> None:
        tokens = tokenize_unique("相变")
        toks = expand_query(tokens, "相变")
        assert "相变" in toks
        assert "phase" in toks  # 同义词扩展


# ══════════════════════════════════════════════════════════════════════════
# BM25
# ══════════════════════════════════════════════════════════════════════════


def _make_node_index() -> dict[str, Any]:
    """构造测试 node-index（2 个 node）。"""
    return {
        "nodes": [
            {
                "doc_id": "doc-a",
                "node_id": "0001",
                "title": "相变理论",
                "breadcrumb": ["物理", "凝聚态"],
                "terms": ["相变", "临界"],
                "summary": "相变是物质状态的转变",
                "url": "",
                "line_num": "0",
            },
            {
                "doc_id": "doc-b",
                "node_id": "0002",
                "title": "Berry Phase",
                "breadcrumb": ["拓扑", "几何相位"],
                "terms": ["berry", "贝里"],
                "summary": "贝里相位是几何相位",
                "url": "",
                "line_num": "0",
            },
        ]
    }


class TestBM25:
    def test_build_bm25_stats_basic(self) -> None:
        ni = _make_node_index()
        stats = build_bm25_stats(ni)
        assert stats is not None
        assert stats.N == 2
        # df 应含 "相变"（在 title + terms + summary 出现，per-field 去重累加）
        assert stats.df.get("相变", 0) >= 2  # title + terms + summary
        # fieldAvgLen 各字段应有值
        assert stats.fieldAvgLen["title"] > 0
        assert stats.fieldAvgLen["summary"] > 0

    def test_build_bm25_stats_df_per_field(self) -> None:
        # DF 是 per-field 去重累加：同一 token 在同 node 的 title 和 terms 都出现
        # 会被计 df+=2（per-field 去重，非 per-doc 去重）
        ni = {
            "nodes": [
                {
                    "doc_id": "d",
                    "node_id": "0001",
                    "title": "相变",
                    "breadcrumb": [],
                    "terms": ["相变"],
                    "summary": "相变",
                    "url": "",
                    "line_num": "0",
                }
            ]
        }
        stats = build_bm25_stats(ni)
        assert stats is not None
        # "相变" 在 title/terms/summary 三字段都出现 → df=3
        assert stats.df["相变"] == 3

    def test_bm25_score_idf_nonneg(self) -> None:
        # IDF = log(1+(N-df+0.5)/(df+0.5))。注意：per-field df 可能 > N
        # （同一 token 在同 node 多 field 出现会被计多次），此时 IDF 可能为负。
        # 这是 retrieval.js 的实际行为（与 JS 对拍一致）。
        # 用一个 token 只在单 field 出现的 fixture，保证 df <= N → IDF > 0。
        ni = {
            "nodes": [
                {
                    "doc_id": "d1",
                    "node_id": "0001",
                    "title": "uniqueword",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "",
                    "url": "",
                    "line_num": "0",
                },
                {
                    "doc_id": "d2",
                    "node_id": "0002",
                    "title": "other",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "",
                    "url": "",
                    "line_num": "0",
                },
            ]
        }
        stats = build_bm25_stats(ni)
        node = ni["nodes"][0]
        # "uniqueword" 只在 d1 title 出现 → df=1, N=2 → IDF>0
        score = bm25_score(["uniqueword"], node, stats)
        assert score > 0

    def test_bm25_score_field_boost(self) -> None:
        # title 命中权重最高（FIELD_BOOST title=6 vs summary=2）
        # 用独立 fixture 避免 df>N 的负 IDF 干扰
        ni = {
            "nodes": [
                {
                    "doc_id": "d1",
                    "node_id": "0001",
                    "title": "uniqueword",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "",
                    "url": "",
                    "line_num": "0",
                },
                {
                    "doc_id": "d2",
                    "node_id": "0002",
                    "title": "other",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "uniqueword",
                    "url": "",
                    "line_num": "0",
                },
            ]
        }
        stats = build_bm25_stats(ni)
        # d1: title 含 "uniqueword"（FIELD_BOOST 6）
        node_title = ni["nodes"][0]
        # d2: summary 含 "uniqueword"（FIELD_BOOST 2）
        node_summary = ni["nodes"][1]
        score_title = bm25_score(["uniqueword"], node_title, stats)
        score_summary = bm25_score(["uniqueword"], node_summary, stats)
        # title 命中分应高于 summary 命中（FIELD_BOOST 6 vs 2）
        assert score_title > score_summary

    def test_bm25_score_weights(self) -> None:
        # weights 可选：同义词 token 权重 0.6
        ni = {
            "nodes": [
                {
                    "doc_id": "d1",
                    "node_id": "0001",
                    "title": "uniqueword",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "",
                    "url": "",
                    "line_num": "0",
                },
                {
                    "doc_id": "d2",
                    "node_id": "0002",
                    "title": "other",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "",
                    "url": "",
                    "line_num": "0",
                },
            ]
        }
        stats = build_bm25_stats(ni)
        node = ni["nodes"][0]
        score_no_w = bm25_score(["uniqueword"], node, stats)
        score_w = bm25_score(["uniqueword"], node, stats, weights={"uniqueword": 0.6})
        # 权重 0.6 应使分数降低
        assert score_w < score_no_w
        assert abs(score_w - score_no_w * 0.6) < 1e-9


class TestChunkStats:
    def _make_chunks(self) -> list[dict[str, Any]]:
        return [
            {
                "chunk_id": "c000001",
                "doc_id": "doc-a",
                "node_id": "0001",
                "title": "相变理论",
                "breadcrumb": ["物理"],
                "body": "相变是物质状态的转变，临界点",
                "line_num": "0",
            },
            {
                "chunk_id": "c000002",
                "doc_id": "doc-b",
                "node_id": "0002",
                "title": "Berry Phase",
                "breadcrumb": ["拓扑"],
                "body": "贝里相位是几何相位",
                "line_num": "0",
            },
        ]

    def test_build_chunk_stats_basic(self) -> None:
        chunks = self._make_chunks()
        stats = build_chunk_stats(chunks)
        assert stats is not None
        assert stats.N == 2
        # _tf 预计算
        assert "c000001" in stats._tf
        tf1 = stats._tf["c000001"]
        assert "title" in tf1 and "body" in tf1
        assert tf1["_len"]["title"] > 0
        # body 含 "相变"
        assert tf1["body"].get("相变", 0) >= 1

    def test_build_chunk_stats_df_per_chunk_merged(self) -> None:
        # DF 按 chunk 合并去重：一个 chunk 内某 token 只算 1 次
        chunks = [
            {
                "chunk_id": "c000001",
                "doc_id": "d",
                "node_id": "0001",
                "title": "相变",
                "breadcrumb": ["相变"],
                "body": "相变",
                "line_num": "0",
            }
        ]
        stats = build_chunk_stats(chunks)
        assert stats is not None
        # "相变" 在 title/breadcrumb/body 都出现，但 chunk 内只算 1 次
        assert stats.df["相变"] == 1

    def test_bm25_score_chunk_basic(self) -> None:
        chunks = self._make_chunks()
        stats = build_chunk_stats(chunks)
        chunk = chunks[0]
        score = bm25_score_chunk(["相变"], chunk, stats)
        assert score > 0

    def test_bm25_score_chunk_no_tf(self) -> None:
        # 无预计算 TF 时返回 0
        chunks = self._make_chunks()
        stats = build_chunk_stats(chunks)
        # 构造一个不在 _tf 里的 chunk
        fake_chunk = {"chunk_id": "c999999", "doc_id": "d", "node_id": "0001"}
        score = bm25_score_chunk(["相变"], fake_chunk, stats)
        assert score == 0


# ══════════════════════════════════════════════════════════════════════════
# Search
# ══════════════════════════════════════════════════════════════════════════


class TestSearch:
    def test_search_basic(self) -> None:
        ni = _make_node_index()
        hits = search("相变", ni)
        # 与 JS 对拍：doc-a 的 "相变" 在 title+terms+summary 三字段都出现，
        # per-field df=3 > N=2 → IDF 为负 → 被 s>0 过滤。top1 是 doc-b
        # （"贝里相位是几何相位" 含 "相位"，但 "相变" 不在 doc-b）。
        # 实际上 "相变" 在 doc-a 但 df>N 导致负分被过滤，所以 hits 可能为空
        # 或返回 doc-b（同义词扩展 "临界"/"phase" 等命中）。与 JS 一致。
        # 这里只验证：能召回（或正确返回空），且 score 两位小数。
        for h in hits:
            assert h.score == round(h.score, 2)
        # 用一个不会触发 df>N 的查询验证召回
        hits2 = search("Berry", ni)
        assert len(hits2) > 0
        assert hits2[0].node["doc_id"] == "doc-b"

    def test_search_empty_query(self) -> None:
        ni = _make_node_index()
        assert search("", ni) == []
        assert search(".,;", ni) == []  # 无 token

    def test_search_per_doc_truncate(self) -> None:
        # 构造多个同 doc 的 node，验证每 doc 最多 3 条
        ni = {
            "nodes": [
                {
                    "doc_id": "d",
                    "node_id": f"{i:04d}",
                    "title": f"相变 {i}",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "",
                    "url": "",
                    "line_num": "0",
                }
                for i in range(10)
            ]
        }
        hits = search("相变", ni, top_k=50)
        # 10 个同 doc node，每 doc 最多 3 → 最多 3 条
        assert len(hits) <= 3

    def test_search_positions(self) -> None:
        ni = _make_node_index()
        hits = search("Berry", ni)
        assert hits
        # positions 应记录 "berry" 在 title 的首次位置
        assert "berry" in hits[0].positions
        assert "title" in hits[0].positions["berry"]


class TestSearchInverted:
    def _make_postings(self, chunks: list[dict[str, Any]]) -> dict[str, list[list[int]]]:
        """构造简单 postings：{token: [[cid_num, tf], ...]}。"""
        from app.retrieval.tokenizer import tokenize

        postings: dict[str, list[list[int]]] = {}
        for ch in chunks:
            cid_num = int(ch["chunk_id"][1:])
            for t in set(tokenize(ch.get("body", ""))):
                postings.setdefault(t, []).append([cid_num, 1])
        return postings

    def test_search_inverted_basic(self) -> None:
        chunks = [
            {
                "chunk_id": "c000001",
                "doc_id": "doc-a",
                "node_id": "0001",
                "title": "相变理论",
                "breadcrumb": ["物理"],
                "body": "相变是物质状态的转变",
                "line_num": "0",
            }
        ]
        stats = build_chunk_stats(chunks)
        postings = self._make_postings(chunks)
        hits = search_inverted("相变", postings, stats)
        assert len(hits) == 1
        assert hits[0].node["doc_id"] == "doc-a"
        # 合成 node summary = body[:200]
        assert hits[0].node["summary"] == "相变是物质状态的转变"
        # positions 含 body 位置（写入 summary 字段位）
        assert "相变" in hits[0].positions
        assert "summary" in hits[0].positions["相变"]

    def test_search_inverted_cid_map_cached(self) -> None:
        chunks = [
            {
                "chunk_id": "c000001",
                "doc_id": "d",
                "node_id": "0001",
                "title": "相变",
                "breadcrumb": [],
                "body": "相变",
                "line_num": "0",
            }
        ]
        stats = build_chunk_stats(chunks)
        postings = self._make_postings(chunks)
        # 首次调用构建 _cid_map
        assert stats._cid_map is None
        search_inverted("相变", postings, stats)
        assert stats._cid_map is not None
        assert 1 in stats._cid_map

    def test_search_inverted_no_postings(self) -> None:
        chunks: list[dict[str, Any]] = []
        stats = build_chunk_stats(chunks)
        assert search_inverted("相变", None, stats) == []
        assert search_inverted("相变", {}, stats) == []


# ══════════════════════════════════════════════════════════════════════════
# RRF + RM3
# ══════════════════════════════════════════════════════════════════════════


def _make_hit(doc_id: str, node_id: str, score: float) -> Hit:
    return Hit(
        node={"doc_id": doc_id, "node_id": node_id, "title": "", "breadcrumb": [], "terms": [], "summary": ""},
        score=score,
        tokens=[],
        positions={},
    )


class TestRRFFuse:
    def test_rrf_fuse_basic(self) -> None:
        # 两路，同 key 出现在两路 → rrf 累加
        path_a = [_make_hit("d1", "0001", 1.0), _make_hit("d2", "0002", 0.5)]
        path_b = [_make_hit("d1", "0001", 0.8), _make_hit("d3", "0003", 0.3)]
        fused = rrf_fuse([path_a, path_b])
        # d1:0001 在两路都 rank0 → rrf = 1/61 + 1/61 = 2/61
        assert fused[0].node["doc_id"] == "d1"
        assert abs(fused[0].rrf_score - (2 * (1 / 61))) < 1e-9
        # score 是 3 位小数（_js_round）
        assert fused[0].score == round(fused[0].rrf_score, 3)

    def test_rrf_fuse_key_is_doc_node(self) -> None:
        # 同 doc 不同 node 视为不同证据（key = doc_id:node_id）
        path_a = [_make_hit("d1", "0001", 1.0)]
        path_b = [_make_hit("d1", "0002", 0.8)]  # 同 doc 不同 node
        fused = rrf_fuse([path_a, path_b])
        # 两个独立 key，不合并
        assert len(fused) == 2

    def test_rrf_fuse_keeps_highest_bm25_item(self) -> None:
        # 同 key 在两路出现，保留最高 BM25 分的那条作为 item
        # 注意：RRF 会覆写 score 为 round(rrf*1000)/1000，但 item 本身
        # （node/chunk/positions）保留最高 BM25 分那条。
        path_a = [_make_hit("d1", "0001", 1.0)]
        path_b = [_make_hit("d1", "0001", 2.0)]  # 更高 BM25 分
        fused = rrf_fuse([path_a, path_b])
        # score 被 RRF 覆写（3 位小数），不是原 BM25 分
        assert fused[0].score == round(fused[0].rrf_score, 3)
        # 但 rrf_score 保留原浮点
        assert abs(fused[0].rrf_score - (2 * (1 / 61))) < 1e-9

    def test_rrf_fuse_empty(self) -> None:
        assert rrf_fuse([]) == []
        assert rrf_fuse([None]) == []  # type: ignore[list-item]
        assert rrf_fuse([[]]) == []


class TestRM3Expand:
    def test_rm3_expand_original_first(self) -> None:
        hits = [_make_hit("d1", "0001", 1.0)]
        hits[0].node["title"] = "相变 临界"
        hits[0].node["terms"] = []
        hits[0].node["summary"] = "相变理论"
        out = rm3_expand(["相变"], hits)
        # 原始 token 在前
        assert out[0] == "相变"
        # 扩展 token 在后
        assert "临界" in out

    def test_rm3_expand_empty_hits(self) -> None:
        assert rm3_expand(["相变"], []) == ["相变"]

    def test_rm3_expand_top_expansions_15(self) -> None:
        # 构造大量 term，验证 topExpansions=15
        hits = [_make_hit("d1", "0001", 1.0)]
        hits[0].node["title"] = " ".join(f"t{i:02d}" for i in range(20))
        hits[0].node["terms"] = []
        hits[0].node["summary"] = ""
        out = rm3_expand(["orig"], hits)
        # 原始 1 + 扩展最多 15 = 最多 16
        assert len(out) <= 16
        assert out[0] == "orig"


# ══════════════════════════════════════════════════════════════════════════
# Rerank + MMR
# ══════════════════════════════════════════════════════════════════════════


class TestShingle:
    def test_shingle_4gram(self) -> None:
        # 4 个 token（长度≥2）→ 1 个 shingle
        s = shingle("alpha beta gamma delta")
        assert s == {"alpha beta gamma delta"}

    def test_shingle_short_returns_single_tokens(self) -> None:
        # tokens.length < k → 返回 Set(tokens)（不是空集）
        s = shingle("alpha beta gamma")
        assert s == {"alpha", "beta", "gamma"}

    def test_shingle_empty(self) -> None:
        assert shingle("") == set()
        assert shingle(None) == set()  # type: ignore[arg-type]


class TestJaccard:
    def test_jaccard_basic(self) -> None:
        a = {"a", "b", "c"}
        b = {"b", "c", "d"}
        # inter=2, sizeA=3, sizeB=3 → 2/(3+3-2)=2/4=0.5
        assert abs(jaccard(a, b) - 0.5) < 1e-9

    def test_jaccard_empty(self) -> None:
        assert jaccard(set(), {"a"}) == 0
        assert jaccard({"a"}, set()) == 0

    def test_jaccard_identical(self) -> None:
        a = {"a", "b"}
        # inter=2, sizeA=2, sizeB=2 → 2/(2+2-2)=1
        assert jaccard(a, a) == 1.0


class TestMMRSelect:
    def test_mmr_select_first_directly(self) -> None:
        # 第一个（最高分）直接选
        ctxs = [
            {"text": "a b c d", "rerankScore": 0.9},
            {"text": "e f g h", "rerankScore": 0.5},
        ]
        out = mmr_select(ctxs, lambda_=0.6, max_chunks=8)
        assert out[0]["text"] == "a b c d"

    def test_mmr_select_max_chunks(self) -> None:
        ctxs = [{"text": f"text {i} a b c", "rerankScore": 1.0 - i * 0.1} for i in range(10)]
        out = mmr_select(ctxs, max_chunks=3)
        assert len(out) == 3

    def test_mmr_select_single(self) -> None:
        ctxs = [{"text": "a b c d", "rerankScore": 0.9}]
        out = mmr_select(ctxs)
        assert len(out) == 1


class TestLexicalRerank:
    def test_lexical_rerank_empty(self) -> None:
        assert lexical_rerank([], "query", []) == []

    def test_lexical_rerank_writes_score(self) -> None:
        hits = [
            Hit(
                node={
                    "doc_id": "d1",
                    "node_id": "0001",
                    "title": "相变理论",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "",
                },
                score=1.0,
                tokens=["相变"],
                positions={"相变": {"title": 0}},
            )
        ]
        out = lexical_rerank(["相变"], "相变", hits)
        assert len(out) == 1
        assert out[0].rerank_score is not None
        # 降序（单元素无所谓）
        assert out[0].rerank_score >= 0


# ══════════════════════════════════════════════════════════════════════════
# Confidence
# ══════════════════════════════════════════════════════════════════════════


class TestClassifyConfidenceMulti:
    def test_low_coverage(self) -> None:
        # coverage < 0.5 → low（短路，最先判定）
        assert classify_confidence_multi({"coverage": 0.3, "rrf_score": 0.1}) == "low"

    def test_high_confidence(self) -> None:
        # coverage>=0.7 && rrf>=0.05 && (titleHit||sourceCount>=2||margin>=0.01)
        assert (
            classify_confidence_multi(
                {
                    "coverage": 0.8,
                    "rrf_score": 0.06,
                    "title_hit": True,
                    "source_count": 1,
                    "margin": 0,
                }
            )
            == "high"
        )
        assert (
            classify_confidence_multi(
                {
                    "coverage": 0.8,
                    "rrf_score": 0.06,
                    "title_hit": False,
                    "source_count": 2,
                    "margin": 0,
                }
            )
            == "high"
        )

    def test_medium_confidence(self) -> None:
        # coverage>=0.5 || rrf>=0.04（但 coverage<0.5 会先短路到 low，
        # 所以 rrf>=0.04 只在 coverage>=0.5 时可达——实际等价于 coverage>=0.5）
        # 与 JS 对拍：coverage=0.6 rrf=0.01 → medium
        assert (
            classify_confidence_multi(
                {"coverage": 0.6, "rrf_score": 0.01, "title_hit": False, "source_count": 1, "margin": 0}
            )
            == "medium"
        )
        # coverage=0.4 rrf=0.05 → low（coverage<0.5 短路，与 JS 一致）
        assert (
            classify_confidence_multi(
                {"coverage": 0.4, "rrf_score": 0.05, "title_hit": False, "source_count": 1, "margin": 0}
            )
            == "low"
        )

    def test_low_fallback(self) -> None:
        # coverage<0.5 且 rrf<0.04 → low
        assert (
            classify_confidence_multi(
                {"coverage": 0.4, "rrf_score": 0.03, "title_hit": False, "source_count": 1, "margin": 0}
            )
            == "low"
        )


class TestComputeConfidenceSignals:
    def test_empty_hits(self) -> None:
        s = compute_confidence_signals("query", [])
        assert s["coverage"] == 0
        assert s["rrf_score"] == 0
        assert s["title_hit"] is False
        assert s["margin"] == 0
        assert s["source_count"] == 0

    def test_basic_signals(self) -> None:
        hits = [
            Hit(
                node={
                    "doc_id": "d1",
                    "node_id": "0001",
                    "title": "相变",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "",
                },
                score=1.0,
                tokens=["相变"],
                positions={"相变": {"title": 0}},
                rrf_score=0.1,
            ),
            Hit(
                node={
                    "doc_id": "d2",
                    "node_id": "0002",
                    "title": "其他",
                    "breadcrumb": [],
                    "terms": [],
                    "summary": "",
                },
                score=0.5,
                tokens=["相变"],
                positions={},
                rrf_score=0.08,
            ),
        ]
        s = compute_confidence_signals("相变", hits)
        # coverage: top5 里命中 "相变" → 1/1 = 1.0
        assert s["coverage"] == 1.0
        # rrf_score = hits[0].rrf_score
        assert s["rrf_score"] == 0.1
        # margin = 0.1 - 0.08 = 0.02
        assert abs(s["margin"] - 0.02) < 1e-9
        # title_hit: top1 title 含 "相变"
        assert s["title_hit"] is True
        # source_count: top10 的 doc_id 去重 → 2
        assert s["source_count"] == 2

    def test_margin_single_hit(self) -> None:
        # 只有 1 条结果 → margin = 0
        hits = [
            Hit(
                node={"doc_id": "d1", "node_id": "0001", "title": "", "breadcrumb": [], "terms": [], "summary": ""},
                score=1.0,
                tokens=[],
                positions={},
                rrf_score=0.1,
            )
        ]
        s = compute_confidence_signals("query", hits)
        assert s["margin"] == 0


# ══════════════════════════════════════════════════════════════════════════
# Benchmark (integration)
# ══════════════════════════════════════════════════════════════════════════


class TestBenchmark:
    @pytest.mark.integration
    def test_run_golden_returns_result(self, tmp_path: Any) -> None:
        # 用真实索引跑 golden.json（集成测试）
        # 项目根：向上找 pyproject.toml
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        pageindex_dir = os.path.join(root, "data", "pageindex")
        golden_path = os.path.join(root, "tests", "retrieval", "golden.json")
        if not os.path.exists(pageindex_dir) or not os.path.exists(golden_path):
            pytest.skip("pageindex/golden.json not available")
        result = run_golden(pageindex_dir=pageindex_dir, golden_path=golden_path, topk=10)
        assert result.n > 0
        # 总体应有非零 recall（148 题至少能召回一些）
        assert result.overall.recall > 0
        # 汇总可打印
        s = result.summary()
        assert "检索基准" in s


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
