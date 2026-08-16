from __future__ import annotations

from app.retrieval.search import Hit
from app.retrieval.search_view import search_pinned_view

from .test_reader import (
    ReaderCorpus,
    _open_clean,
    _open_incremental,
    reader_corpus,
)


def _stable_results(hits: list[Hit]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            hit.generation,
            hit.doc_key,
            hit.doc_uid,
            hit.segment_hash,
            hit.local_id,
            hit.node_key,
            hit.node.get("node_id"),
            hit.score,
            hit.rrf_score,
        )
        for hit in hits
    )


def test_incremental_and_clean_base_searches_match_on_real_reader(
    reader_corpus: ReaderCorpus,
) -> None:
    queries = (
        "titlehot",
        "crumbhot",
        "bodyrare",
        "addedtoken",
        "booktoken",
        "bodyhot",
        "missingtoken",
        "!!!",
    )

    with _open_incremental(reader_corpus) as incremental:
        with _open_clean(reader_corpus) as clean:
            for query in queries:
                incremental_hits = search_pinned_view(query, incremental, top_k=10)
                clean_hits = search_pinned_view(query, clean, top_k=10)
                assert _stable_results(incremental_hits) == _stable_results(
                    clean_hits
                )


def test_real_reader_search_uses_node_authenticated_legacy_identity(
    reader_corpus: ReaderCorpus,
) -> None:
    with _open_incremental(reader_corpus) as reader:
        hit = search_pinned_view("addedtoken", reader, top_k=5)[0]

    assert hit.node.get("node_id") == "1"
    assert hit.node_key == "root"
    assert hit.doc_key == "paper:added"