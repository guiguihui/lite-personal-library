from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Iterable, Mapping

import pytest

from app.index.v3.models import (
    ChunkRef,
    GenerationRecipe,
    SearchPosting,
    TokenSummary,
    make_doc_uid,
)
from app.index.v3.reader import PinnedSearchViewError
from app.index.v3.segment_projection import ChunkMetric
from app.index.v3.statistics import CorpusTotals
from app.retrieval.bm25 import build_chunk_stats
from app.retrieval.search import Hit, search_multi_path
from app.retrieval.search_view import search_pinned_view
from app.retrieval.tokenizer import tokenize

# Importing the decorated fixture makes it available when this file is run in
# isolation while also exercising the real Base-plus-three-Delta reader.
from .test_reader import (  # noqa: E402,F401
    ReaderCorpus,
    _open_clean,
    _open_incremental,
    reader_corpus,
)


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


@dataclass(frozen=True)
class _Doc:
    doc_key: str
    chunks: tuple[dict[str, object], ...]


class _FakePinnedView:
    """Small semantic implementation of the PinnedSearchView query surface."""

    def __init__(self, docs: tuple[_Doc, ...], recipe: GenerationRecipe) -> None:
        self.generation_recipe = recipe
        self.pin = SimpleNamespace(generation=_DIGEST_A, view_id=_DIGEST_B)
        self.metric_requests: list[tuple[ChunkRef, ...]] = []
        self.chunk_requests: list[tuple[ChunkRef, ...]] = []
        self.document_ref_requests: list[tuple[str, ...]] = []

        owners: dict[str, SimpleNamespace] = {}
        chunks: dict[ChunkRef, dict[str, object]] = {}
        metrics: dict[ChunkRef, ChunkMetric] = {}
        postings: dict[str, list[SearchPosting]] = {}
        refs_by_doc: dict[str, tuple[ChunkRef, ...]] = {}
        field_sums = {"title": 0, "breadcrumb": 0, "body": 0}

        for doc in sorted(docs, key=lambda item: item.doc_key):
            doc_uid = make_doc_uid(doc.doc_key)
            segment_hash = __import__("hashlib").sha256(
                f"segment:{doc.doc_key}".encode()
            ).hexdigest()
            owners[doc_uid] = SimpleNamespace(
                doc_key=doc.doc_key,
                segment_hash=segment_hash,
            )
            doc_refs: list[ChunkRef] = []
            for local_id, source in enumerate(doc.chunks):
                chunk = deepcopy(source)
                chunk["local_id"] = local_id
                chunk.setdefault("node_key", f"node-{local_id}")
                chunk.setdefault("legacy_node_id", str(local_id + 1))
                title_tokens = tokenize(str(chunk.get("title") or ""))
                breadcrumb = chunk.get("breadcrumb") or []
                breadcrumb_tokens = tokenize(" ".join(breadcrumb))
                body_tokens = tokenize(str(chunk.get("body") or ""))
                chunk["lengths"] = {
                    "title": len(title_tokens),
                    "breadcrumb": len(breadcrumb_tokens),
                    "body": len(body_tokens),
                }
                ref = ChunkRef(doc_uid, segment_hash, local_id)
                doc_refs.append(ref)
                chunks[ref] = chunk
                metric = ChunkMetric(
                    local_id,
                    len(title_tokens),
                    len(breadcrumb_tokens),
                    len(body_tokens),
                )
                metrics[ref] = metric
                field_sums["title"] += metric.title_length
                field_sums["breadcrumb"] += metric.breadcrumb_length
                field_sums["body"] += metric.body_length
                field_maps = (
                    Counter(title_tokens),
                    Counter(breadcrumb_tokens),
                    Counter(body_tokens),
                )
                for token in set().union(*field_maps):
                    postings.setdefault(token, []).append(
                        SearchPosting(
                            token,
                            ref,
                            field_maps[0].get(token, 0),
                            field_maps[1].get(token, 0),
                            field_maps[2].get(token, 0),
                        )
                    )
            refs_by_doc[doc_uid] = tuple(doc_refs)

        summaries: dict[str, TokenSummary] = {}
        for token, rows in postings.items():
            summaries[token] = TokenSummary(
                token,
                len(rows),
                sum(bool(row.title_tf or row.breadcrumb_tf) for row in rows),
                sum(bool(row.body_tf) for row in rows),
            )
        posting_count = sum(summary.df_any for summary in summaries.values())
        self._totals = CorpusTotals(
            documents=len(docs),
            total_chunks=len(chunks),
            token_count=len(summaries),
            title_length_sum=field_sums["title"],
            breadcrumb_length_sum=field_sums["breadcrumb"],
            body_length_sum=field_sums["body"],
            posting_count=posting_count,
        )
        self._owners = owners
        self._chunks = chunks
        self._metrics = metrics
        self._postings = postings
        self._summaries = summaries
        self._refs_by_doc = refs_by_doc

    def corpus_stats(self) -> CorpusTotals:
        return self._totals

    def documents(self) -> Mapping[str, SimpleNamespace]:
        return self._owners

    def token_stats(
        self, tokens: Iterable[str]
    ) -> dict[str, TokenSummary | None]:
        return {token: self._summaries.get(token) for token in dict.fromkeys(tokens)}

    def _pruned(self, token: str) -> bool:
        summary = self._summaries[token]
        recipe = self.generation_recipe
        return (
            summary.df_body >= recipe.body_df_min
            and summary.df_body * recipe.body_df_ratio_denominator
            >= self._totals.total_chunks * recipe.body_df_ratio_numerator
        )

    def iter_effective_postings(self, token: str) -> Iterable[SearchPosting]:
        for row in self._postings.get(token, ()):
            if not self._pruned(token):
                yield row
            elif row.title_tf or row.breadcrumb_tf:
                yield SearchPosting(
                    row.token,
                    row.chunk_ref,
                    row.title_tf,
                    row.breadcrumb_tf,
                    0,
                )

    def get_chunk_metrics(
        self, refs: Iterable[ChunkRef]
    ) -> dict[ChunkRef, ChunkMetric]:
        requested = tuple(refs)
        self.metric_requests.append(requested)
        return {ref: self._metrics[ref] for ref in requested}

    def get_chunks(
        self, refs: Iterable[ChunkRef]
    ) -> dict[ChunkRef, dict[str, object]]:
        requested = tuple(refs)
        self.chunk_requests.append(requested)
        return {ref: deepcopy(self._chunks[ref]) for ref in requested}

    def document_chunk_refs(
        self, doc_uids: Iterable[str]
    ) -> tuple[ChunkRef, ...]:
        requested = tuple(doc_uids)
        self.document_ref_requests.append(requested)
        return tuple(
            ref
            for doc_uid in requested
            for ref in self._refs_by_doc[doc_uid]
        )


def _docs(*, common_body: bool = False) -> tuple[_Doc, ...]:
    common = " commonbody bodyhot" if common_body else ""
    return (
        _Doc(
            "book:alpha",
            (
                {
                    "node_key": "alpha-root",
                    "legacy_node_id": "10",
                    "title": (
                        "Quantum Search Guide Overview 量子搜索"
                        + (" commonbody" if common_body else "")
                    ),
                    "breadcrumb": ["Quantum Search Guide", "Overview"],
                    "body": f"rarebody signal signal introduction{common}",
                    "line_num": 10,
                },
                {
                    "node_key": "alpha-detail",
                    "legacy_node_id": "20",
                    "title": "Advanced Quantum Search",
                    "breadcrumb": ["Quantum Search Guide", "Advanced"],
                    "body": f"secondary material{common}",
                    "line_num": 20,
                },
            ),
        ),
        _Doc(
            "note:beta",
            (
                {
                    "node_key": "beta-root",
                    "legacy_node_id": "1",
                    "title": "Vector Notes 向量笔记",
                    "breadcrumb": [
                        "Vector Notes",
                        *(("commonbody",) if common_body else ()),
                    ],
                    "body": f"rarebody vector algebra{common}",
                    "line_num": 1,
                },
            ),
        ),
        _Doc(
            "paper:gamma",
            (
                {
                    "node_key": "gamma-root",
                    "legacy_node_id": "7",
                    "title": "Thermal Paper 热力论文",
                    "breadcrumb": ["Thermal Paper"],
                    "body": f"thermal system{common}",
                    "line_num": 7,
                },
            ),
        ),
        _Doc(
            "note:unrelated",
            (
                {
                    "node_key": "unrelated-root",
                    "legacy_node_id": "99",
                    "title": "Cooking Notes",
                    "breadcrumb": ["Cooking"],
                    "body": f"kitchen recipe{common}",
                    "line_num": 99,
                },
            ),
        ),
    )


def _legacy_inputs(
    docs: tuple[_Doc, ...],
) -> tuple[dict[str, list[list[int]]], object, dict[str, object]]:
    chunks: list[dict[str, object]] = []
    postings: dict[str, list[list[int]]] = {}
    global_docs: list[dict[str, object]] = []
    for doc in sorted(docs, key=lambda item: item.doc_key):
        _doc_type, slug = doc.doc_key.split(":", 1)
        first = doc.chunks[0]
        global_docs.append(
            {
                "id": slug,
                "title": first["title"],
                "description": "",
            }
        )
        for source in doc.chunks:
            chunk = {
                "chunk_id": f"c{len(chunks) + 1:06d}",
                "doc_id": slug,
                "node_id": source["legacy_node_id"],
                "title": source["title"],
                "breadcrumb": deepcopy(source["breadcrumb"]),
                "body": source["body"],
                "source_md": "",
                "line_num": source["line_num"],
            }
            chunks.append(chunk)
            cid = len(chunks)
            all_tokens = tokenize(str(chunk["title"]))
            all_tokens += tokenize(" ".join(chunk["breadcrumb"]))
            all_tokens += tokenize(str(chunk["body"]))
            for token, tf in Counter(all_tokens).items():
                postings.setdefault(token, []).append([cid, tf])
    return postings, build_chunk_stats(chunks), {"docs": global_docs}


def _core(hits: list[Hit]) -> list[tuple[str, str, float, float | None]]:
    return [
        (
            str(hit.node["doc_id"]),
            str(hit.node["node_id"]),
            hit.score,
            hit.rrf_score,
        )
        for hit in hits
    ]


@pytest.mark.parametrize(
    "query",
    (
        "rarebody",
        "quantum search",
        "overview",
        "Quantum Search Guide Overview",
        "signal signal",
        "向量笔记",
        "thermal",
        "!!!",
    ),
)
def test_unpruned_multi_path_matches_legacy_oracle_exactly(query: str) -> None:
    docs = _docs()
    view = _FakePinnedView(docs, GenerationRecipe(body_df_min=1000))
    postings, stats, global_index = _legacy_inputs(docs)

    expected = search_multi_path(query, postings, stats, global_index, top_k=10)
    actual = search_pinned_view(query, view, top_k=10)  # type: ignore[arg-type]

    assert _core(actual) == _core(expected)


def test_effective_posting_count_must_match_effective_df(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _FakePinnedView(_docs(), GenerationRecipe(body_df_min=1000))
    original = view.iter_effective_postings

    def omit_one(token: str):
        rows = tuple(original(token))
        return rows[:-1] if token == "rarebody" else rows

    monkeypatch.setattr(view, "iter_effective_postings", omit_one)

    with pytest.raises(PinnedSearchViewError, match="differs from DF"):
        search_pinned_view("rarebody", view, top_k=10)  # type: ignore[arg-type]


def test_extreme_body_df_is_intentionally_not_legacy_equivalent() -> None:
    docs = _docs(common_body=True)
    postings, stats, global_index = _legacy_inputs(docs)
    legacy = search_multi_path("bodyhot", postings, stats, global_index, top_k=10)
    view = _FakePinnedView(
        docs,
        GenerationRecipe(
            body_df_min=2,
            body_df_ratio_numerator=1,
            body_df_ratio_denominator=2,
        ),
    )

    assert legacy
    assert search_pinned_view("bodyhot", view, top_k=10) == []  # type: ignore[arg-type]


def test_extreme_body_pruning_never_removes_title_or_breadcrumb() -> None:
    docs = _docs(common_body=True)
    view = _FakePinnedView(
        docs,
        GenerationRecipe(
            body_df_min=2,
            body_df_ratio_numerator=1,
            body_df_ratio_denominator=2,
        ),
    )

    hits = search_pinned_view("commonbody", view, top_k=10)  # type: ignore[arg-type]

    assert hits
    assert {hit.node["doc_id"] for hit in hits} == {"alpha", "beta"}
    assert all(
        "summary" not in hit.positions.get("commonbody", {})
        for hit in hits
    )
    assert any("title" in hit.positions["commonbody"] for hit in hits)
    assert any("breadcrumb" in hit.positions["commonbody"] for hit in hits)


def test_candidate_io_and_document_routing_never_load_global_chunks() -> None:
    docs = _docs()
    view = _FakePinnedView(docs, GenerationRecipe(body_df_min=1000))

    hits = search_pinned_view("rarebody", view, top_k=10)  # type: ignore[arg-type]

    assert hits
    assert view.metric_requests
    assert view.chunk_requests
    loaded_doc_uids = {ref.doc_uid for ref in view.chunk_requests[-1]}
    expected = {make_doc_uid("book:alpha"), make_doc_uid("note:beta")}
    assert loaded_doc_uids == expected
    assert make_doc_uid("note:unrelated") not in loaded_doc_uids
    assert view.document_ref_requests == []

    view.chunk_requests.clear()
    routed = search_pinned_view("quantum search", view, top_k=10)  # type: ignore[arg-type]
    assert routed
    assert view.document_ref_requests == [(make_doc_uid("book:alpha"),)]
    assert {ref.doc_uid for ref in view.chunk_requests[-1]} == {
        make_doc_uid("book:alpha")
    }


def test_broad_posting_hydrates_only_ranked_shortlists() -> None:
    docs = tuple(
        _Doc(
            f"note:doc-{index:03d}",
            (
                {
                    "node_key": "root",
                    "legacy_node_id": "1",
                    "title": f"Document {index:03d}",
                    "breadcrumb": [],
                    "body": "needle " + "padding " * index,
                    "line_num": 1,
                },
            ),
        )
        for index in range(100)
    )
    view = _FakePinnedView(docs, GenerationRecipe(body_df_min=1000))

    assert search_pinned_view("needle", view, top_k=5)  # type: ignore[arg-type]

    assert len(view.metric_requests[-1]) == 100
    # Path A's legacy 60-candidate window is the widest hydrated shortlist;
    # the 40 losing candidates never load Segment body text.
    assert len(view.chunk_requests[-1]) == 60
    assert set(view.chunk_requests[-1]) < set(view.metric_requests[-1])


def test_hits_keep_legacy_rrf_identity_and_carry_immutable_references() -> None:
    view = _FakePinnedView(_docs(), GenerationRecipe(body_df_min=1000))

    hit = search_pinned_view("signal", view, top_k=5)[0]  # type: ignore[arg-type]

    assert hit.node["doc_id"] == "alpha"
    assert hit.node["node_id"] == "10"
    assert hit.chunk is not None
    for field in (
        "generation",
        "view_id",
        "doc_key",
        "doc_uid",
        "segment_hash",
        "local_id",
        "node_key",
    ):
        assert hit.node[field] == hit.chunk[field]
    assert hit.node["generation"] == _DIGEST_A
    assert hit.node["view_id"] == _DIGEST_B
    assert hit.node["doc_key"] == "book:alpha"
    assert hit.node["node_key"] == "alpha-root"


def test_real_incremental_and_clean_views_score_identically(
    reader_corpus: ReaderCorpus,
) -> None:
    with _open_incremental(reader_corpus) as incremental:
        with _open_clean(reader_corpus) as clean:
            for query in (
                "addedtoken",
                "bodyrare",
                "titlehot",
                "crumbhot",
                "bodyhot",
                "missingtoken",
            ):
                incremental_hits = search_pinned_view(query, incremental, top_k=10)
                clean_hits = search_pinned_view(query, clean, top_k=10)
                assert [
                    (
                        hit.doc_key,
                        hit.node_key,
                        hit.local_id,
                        hit.score,
                        hit.rrf_score,
                    )
                    for hit in incremental_hits
                ] == [
                    (
                        hit.doc_key,
                        hit.node_key,
                        hit.local_id,
                        hit.score,
                        hit.rrf_score,
                    )
                    for hit in clean_hits
                ]
                assert all(
                    hit.generation == incremental.pin.generation
                    and hit.view_id == incremental.pin.view_id
                    for hit in incremental_hits
                )
