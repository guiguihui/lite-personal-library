"""LQ-D — Retrieval core (Python port of retrieval.js).

阶段 7：Python 检索重写。与前端 ``frontend/chat/retrieval.js`` 行为对齐，
用于：

1. 对拍防回归（``benchmark.py`` 跑 golden.json，对照 JS harness.js 结果）
2. 未来迁移基础（运行时检索仍在前端 retrieval.js，此包为参考实现）
3. 交叉验证算法正确性

设计原则（与 retrieval.js 一致）：

- 纯函数 / 有显式入参；index/stats 作为参数传入，不持有闭包状态。
- 行为与 retrieval.js 逐行对齐，包括正则、常量、截断、舍入。
- 不用 ``print``（用 ``logging`` 或返回值）。

模块映射（retrieval.js 区段 → Python 模块）：

- tokenizer (L21-42)        → :mod:`app.retrieval.tokenizer`
- synonyms/expand (L48-149) → :mod:`app.retrieval.tokenizer`（同义词表与扩展）
- BM25 node-index (L155-272)→ :mod:`app.retrieval.bm25` (build_bm25_stats/bm25_score)
- BM25F chunk (L283-352)    → :mod:`app.retrieval.bm25` (build_chunk_stats/bm25_score_chunk)
- search (L228-272, L359-536)→ :mod:`app.retrieval.search`
- RRF + RM3 (L541-614)      → :mod:`app.retrieval.fuse`
- rerank + MMR (L620-740)   → :mod:`app.retrieval.rerank`
- confidence (L767-807)     → :mod:`app.retrieval.confidence`
- benchmark harness         → :mod:`app.retrieval.benchmark`
"""

from app.retrieval.tokenizer import (
    tokenize,
    tokenize_raw,
    tokenize_unique,
    expand_query,
    expand_query_weighted,
    SYNONYMS,
    SYNONYM_WEIGHT,
)
from app.retrieval.bm25 import (
    BM25Stats,
    ChunkStats,
    build_bm25_stats,
    build_chunk_stats,
    bm25_score,
    bm25_score_chunk,
    FIELD_BOOST,
    CHUNK_FIELD_BOOST,
    BM25_K,
    BM25_B,
    FIELDS,
)
from app.retrieval.search import (
    Hit,
    search,
    search_inverted,
    search_title_phrase,
    search_doc_route,
    search_multi_path,
)
from app.retrieval.fuse import rrf_fuse, rm3_expand
from app.retrieval.rerank import (
    lexical_rerank,
    shingle,
    jaccard,
    mmr_select,
    estimate_tokens,
    RERANK_WEIGHTS,
)
from app.retrieval.confidence import (
    classify_confidence,
    classify_confidence_multi,
    compute_confidence_signals,
)

__all__ = [
    # tokenizer
    "tokenize",
    "tokenize_raw",
    "tokenize_unique",
    "expand_query",
    "expand_query_weighted",
    "SYNONYMS",
    "SYNONYM_WEIGHT",
    # bm25
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
    # search
    "Hit",
    "search",
    "search_inverted",
    "search_title_phrase",
    "search_doc_route",
    "search_multi_path",
    # fuse
    "rrf_fuse",
    "rm3_expand",
    # rerank
    "lexical_rerank",
    "shingle",
    "jaccard",
    "mmr_select",
    "estimate_tokens",
    "RERANK_WEIGHTS",
    # confidence
    "classify_confidence",
    "classify_confidence_multi",
    "compute_confidence_signals",
]
