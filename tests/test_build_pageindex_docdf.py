"""pytest 单元测试:build_pageindex 文档级 DF 停用词过滤。

覆盖 _build_cid_doc_map / _doc_level_df / _filter_stopwords:
  - 单文档库退化为 chunk 级 DF（避免全杀）
  - 多文档库用文档级 DF（单文档高频领域词保留，跨文档高频真停用词丢弃）
  - 全量与增量过滤行为一致
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# vendor 脚本非 app 包子模块，需加 sys.path
_VENDOR = Path(__file__).resolve().parent.parent / "app" / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from build_pageindex import (  # type: ignore  # noqa: E402
    _build_cid_doc_map,
    _doc_level_df,
    _filter_stopwords,
    STOPWORD_DF_RATIO,
)


# ══════════════════════════════════════════════════════════════════════════
# _build_cid_doc_map
# ══════════════════════════════════════════════════════════════════════════


class TestBuildCidDocMap:
    def test_basic_mapping(self) -> None:
        chunks = [
            {"chunk_id": "c000001", "doc_id": "docA"},
            {"chunk_id": "c000002", "doc_id": "docA"},
            {"chunk_id": "c000003", "doc_id": "docB"},
        ]
        m = _build_cid_doc_map(chunks)
        assert m == {1: "docA", 2: "docA", 3: "docB"}

    def test_skips_invalid_ids(self) -> None:
        """非 c 前缀或非数字的 chunk_id 被跳过。"""
        chunks = [
            {"chunk_id": "c000001", "doc_id": "docA"},
            {"chunk_id": "bad", "doc_id": "docB"},
            {"chunk_id": "cXYZ", "doc_id": "docC"},
            {"chunk_id": "", "doc_id": "docD"},
        ]
        m = _build_cid_doc_map(chunks)
        assert m == {1: "docA"}


# ══════════════════════════════════════════════════════════════════════════
# _doc_level_df
# ══════════════════════════════════════════════════════════════════════════


class TestDocLevelDf:
    def test_single_doc_high_freq(self) -> None:
        """token 在 1 个 doc 的多个 chunk 出现 → doc_df=1。"""
        cid_doc_map = {1: "A", 2: "A", 3: "A", 4: "B"}
        posting_list = [[1, 2], [2, 1], [3, 1]]  # 全在 docA
        assert _doc_level_df(posting_list, cid_doc_map) == 1

    def test_multi_doc(self) -> None:
        """token 跨 3 个 doc → doc_df=3。"""
        cid_doc_map = {1: "A", 2: "B", 3: "C", 4: "A"}
        posting_list = [[1, 1], [2, 1], [3, 1], [4, 1]]
        assert _doc_level_df(posting_list, cid_doc_map) == 3

    def test_empty_postings(self) -> None:
        assert _doc_level_df([], {}) == 0

    def test_unknown_cid_ignored(self) -> None:
        """cid 不在 map 里（不应发生，但容错）→ 不计入 DF。"""
        cid_doc_map = {1: "A"}
        posting_list = [[1, 1], [999, 1]]
        assert _doc_level_df(posting_list, cid_doc_map) == 1


# ══════════════════════════════════════════════════════════════════════════
# _filter_stopwords
# ══════════════════════════════════════════════════════════════════════════


def _chunks(n_docs: int, chunks_per_doc: int = 10) -> list[dict]:
    """构造 n_docs 个文档、每文档 chunks_per_doc 个 chunk 的测试数据。"""
    docs = [chr(ord("A") + i) for i in range(n_docs)]
    out = []
    cid = 1
    for d in docs:
        for _ in range(chunks_per_doc):
            out.append({"chunk_id": f"c{cid:06d}", "doc_id": d})
            cid += 1
    return out


class TestFilterStopwordsMultiDoc:
    def test_single_doc_high_freq_preserved(self) -> None:
        """4 文档库：token 只在 1 个 doc 高频 → doc_df=1, 25% < 35% 保留。

        这是核心修复场景：infovision 的"车位"只在这 1 本书高频，不该被误杀。
        """
        chunks = _chunks(n_docs=4, chunks_per_doc=10)
        # 车位只在 docA 的 9/10 chunk（高频但单文档）
        cid_doc_map = _build_cid_doc_map(chunks)
        docA_cids = [cid for cid, d in cid_doc_map.items() if d == "A"]
        车位_postings = [[cid, 1] for cid in docA_cids[:9]]
        postings = {"车位": 车位_postings}
        filtered, kept, dropped = _filter_stopwords(postings, chunks)
        assert "车位" in filtered
        assert kept == 1 and dropped == 0

    def test_cross_doc_high_freq_dropped(self) -> None:
        """4 文档库：token 在 3 个 doc 高频 → 75% > 35% 丢弃（真停用词）。"""
        chunks = _chunks(n_docs=4, chunks_per_doc=10)
        cid_doc_map = _build_cid_doc_map(chunks)
        # 取 docA/B/C 各 1 个 chunk（每 doc 10 chunk，第 1 个 cid 分别是 1/11/21）
        first_cid_per_doc: dict[str, int] = {}
        for cid, d in cid_doc_map.items():
            if d not in first_cid_per_doc:
                first_cid_per_doc[d] = cid
        cids_abc = [first_cid_per_doc[d] for d in ("A", "B", "C")]
        postings = {"跨文档词": [[cid, 1] for cid in cids_abc]}
        filtered, kept, dropped = _filter_stopwords(postings, chunks)
        assert "跨文档词" not in filtered
        assert dropped == 1 and kept == 0

    def test_all_docs_token_dropped(self) -> None:
        """4 文档库：token 在所有 4 个 doc → 100% > 35% 丢弃。"""
        chunks = _chunks(n_docs=4, chunks_per_doc=10)
        cid_doc_map = _build_cid_doc_map(chunks)
        # 每 doc 取 1 个 cid（4 个不同 doc）
        first_cid_per_doc: dict[str, int] = {}
        for cid, d in cid_doc_map.items():
            if d not in first_cid_per_doc:
                first_cid_per_doc[d] = cid
        all_doc_cids = list(first_cid_per_doc.values())
        postings = {"的": [[cid, 1] for cid in all_doc_cids]}
        filtered, _, _ = _filter_stopwords(postings, chunks)
        assert "的" not in filtered


class TestFilterStopwordsSingleDoc:
    def test_single_doc_degrades_to_chunk_level(self) -> None:
        """单文档库：退化为 chunk 级 DF（避免文档级全杀）。"""
        chunks = _chunks(n_docs=1, chunks_per_doc=100)
        # 高频 token：95% chunk 出现 → chunk 级 cap=int(100*0.35)=35, 95>35 丢
        cid_doc_map = _build_cid_doc_map(chunks)
        all_cids = list(cid_doc_map.keys())
        postings = {
            "高频": [[cid, 1] for cid in all_cids[:95]],
            "低频": [[cid, 1] for cid in all_cids[:2]],
        }
        filtered, kept, dropped = _filter_stopwords(postings, chunks)
        assert "高频" not in filtered
        assert "低频" in filtered
        assert kept == 1 and dropped == 1

    def test_single_doc_low_freq_preserved(self) -> None:
        """单文档库：低频 token 保留。"""
        chunks = _chunks(n_docs=1, chunks_per_doc=100)
        cid_doc_map = _build_cid_doc_map(chunks)
        cids = list(cid_doc_map.keys())
        postings = {"稀有": [[cids[0], 1], [cids[1], 1]]}
        filtered, _, _ = _filter_stopwords(postings, chunks)
        assert "稀有" in filtered


class TestFilterStopwordsEdgeCases:
    def test_two_doc_library(self) -> None:
        """2 文档库：跨 2 doc 的 token = 100% > 35% 丢弃（合理，2 文档不区分）。"""
        chunks = _chunks(n_docs=2, chunks_per_doc=5)
        cid_doc_map = _build_cid_doc_map(chunks)
        docA_cid = next(cid for cid, d in cid_doc_map.items() if d == "A")
        docB_cid = next(cid for cid, d in cid_doc_map.items() if d == "B")
        postings = {"跨文档": [[docA_cid, 1], [docB_cid, 1]]}
        filtered, _, dropped = _filter_stopwords(postings, chunks)
        assert "跨文档" not in filtered
        assert dropped == 1

    def test_empty_postings(self) -> None:
        chunks = _chunks(n_docs=3)
        filtered, kept, dropped = _filter_stopwords({}, chunks)
        assert filtered == {} and kept == 0 and dropped == 0

    def test_empty_chunks(self) -> None:
        """无 chunk：n_docs=0 退化为单文档分支，cap=0。"""
        filtered, kept, dropped = _filter_stopwords({"x": [[1, 1]]}, [])
        assert filtered == {} and kept == 0 and dropped == 1
