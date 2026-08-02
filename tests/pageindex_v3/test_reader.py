from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping

import pytest

from app.index.v2.canonical import canonical_hash
from app.index.v2.models import SegmentRecipe
from app.index.v2.object_store import StoredSegmentRef, put_segment
from app.index.v3.base_builder import build_base_view
from app.index.v3.delta_builder import DeltaBuildResult, build_delta_view
from app.index.v3.generation import (
    LogicalGenerationReceipt,
    build_logical_generation,
)
from app.index.v3.layer_codec import CHUNKS_MAGIC, PostingLayerReceipt
from app.index.v3.models import (
    ChunkRef,
    CompactionPolicy,
    GenerationRecipe,
    SearchViewRecipe,
    TokenSummary,
    ViewPin,
)
from app.index.v3.reader import PinnedSearchView
from app.index.v3.segment_projection import ChunkMetric, SegmentProjector
from app.index.v3.source_diff import SegmentChangeSet
from app.index.v3.statistics import CorpusTotals
from app.index.v3.view_store import (
    BaseObjectReceipt,
    SearchViewReceipt,
    ViewDocumentOwner,
)
from app.retrieval.tokenizer import tokenize


ReadEvent = tuple[str, str, int, int]


@dataclass(frozen=True, slots=True)
class ReaderCorpus:
    pageindex: Path
    generation_recipe: GenerationRecipe
    search_view_recipe: SearchViewRecipe
    initial_generation: LogicalGenerationReceipt
    final_generation: LogicalGenerationReceipt
    initial_base: BaseObjectReceipt
    initial_view: SearchViewReceipt
    delta_results: tuple[DeltaBuildResult, ...]
    final_view: SearchViewReceipt
    clean_base: BaseObjectReceipt
    clean_view: SearchViewReceipt
    final_refs: tuple[StoredSegmentRef, ...]
    layer_receipts: Mapping[str, PostingLayerReceipt]

    @property
    def layer_ids(self) -> tuple[str, ...]:
        return (
            self.initial_base.base_id,
            *(result.delta.delta_id for result in self.delta_results),
        )


def _document_path(doc_type: str, slug: str) -> str:
    if doc_type == "note":
        return f"notes/{slug}.md"
    return f"{doc_type}s/{slug}/_index.md"


def _segment(
    doc_key: str,
    fields: tuple[tuple[str, tuple[str, ...], str], ...],
    *,
    revision: str,
) -> dict[str, object]:
    doc_type, slug = doc_key.split(":", 1)
    chunks: list[dict[str, object]] = []
    postings: dict[str, list[list[int]]] = {}
    for local_id, (title, breadcrumb, body) in enumerate(fields):
        title_tf = Counter(tokenize(title))
        breadcrumb_tf = Counter(tokenize(" ".join(breadcrumb)))
        body_tf = Counter(tokenize(body))
        chunks.append(
            {
                "local_id": local_id,
                "node_key": "root",
                "title": title,
                "breadcrumb": list(breadcrumb),
                "body": body,
                "lengths": {
                    "title": sum(title_tf.values()),
                    "breadcrumb": sum(breadcrumb_tf.values()),
                    "body": sum(body_tf.values()),
                },
            }
        )
        for token in sorted(
            set(title_tf) | set(breadcrumb_tf) | set(body_tf),
            key=lambda value: value.encode("utf-8"),
        ):
            postings.setdefault(token, []).append(
                [
                    local_id,
                    int(title_tf.get(token, 0)),
                    int(breadcrumb_tf.get(token, 0)),
                    int(body_tf.get(token, 0)),
                ]
            )

    segment_recipe = SegmentRecipe().as_dict()
    source_files = [
        {
            "path": _document_path(doc_type, slug),
            "sha256": hashlib.sha256(
                f"{doc_key}:{revision}".encode("utf-8")
            ).hexdigest(),
        }
    ]
    return {
        "schema_version": 2,
        "segment_recipe": segment_recipe,
        "document": {"doc_key": doc_key, "type": doc_type, "id": slug},
        "fingerprint": {
            "content_hash": canonical_hash(source_files),
            "recipe_hash": canonical_hash(segment_recipe),
            "source_files": source_files,
        },
        "nodes": [{"node_key": "root", "legacy_node_id": "1"}],
        "chunks": chunks,
        "postings": {
            token: postings[token]
            for token in sorted(postings, key=lambda value: value.encode("utf-8"))
        },
    }


def _put(
    pageindex: Path,
    doc_key: str,
    fields: tuple[tuple[str, tuple[str, ...], str], ...],
    revision: str,
) -> StoredSegmentRef:
    return put_segment(
        pageindex,
        _segment(doc_key, fields, revision=revision),
    )


def _generation(
    root: Path,
    name: str,
    refs: tuple[StoredSegmentRef, ...],
    recipe: GenerationRecipe,
) -> LogicalGenerationReceipt:
    proof = {
        "schema_version": 1,
        "compiler_recipe_hash": canonical_hash(recipe.as_dict()),
        "documents": {
            ref.doc_key: {
                "content_hash": ref.content_hash,
                "segment_recipe_hash": ref.segment_recipe_hash,
            }
            for ref in reversed(refs)
        },
    }
    return build_logical_generation(refs, proof, recipe, root / name)


def _changes(
    before: tuple[StoredSegmentRef, ...],
    after: tuple[StoredSegmentRef, ...],
) -> SegmentChangeSet:
    old = {ref.doc_key: ref for ref in before}
    new = {ref.doc_key: ref for ref in after}
    old_keys = set(old)
    new_keys = set(new)
    changed = {
        key
        for key in old_keys & new_keys
        if old[key].segment_hash != new[key].segment_hash
    }
    return SegmentChangeSet(
        base_by_doc=old,
        current_fingerprints={
            key: new[key].content_hash for key in sorted(new)
        },
        added=tuple(sorted(new_keys - old_keys)),
        changed=tuple(sorted(changed)),
        deleted=tuple(sorted(old_keys - new_keys)),
        unchanged=tuple(sorted((old_keys & new_keys) - changed)),
    )


def _new_refs(
    changes: SegmentChangeSet,
    after: tuple[StoredSegmentRef, ...],
) -> tuple[StoredSegmentRef, ...]:
    dirty = set(changes.added) | set(changes.changed)
    return tuple(ref for ref in after if ref.doc_key in dirty)


def _advance(
    pageindex: Path,
    generation_root: Path,
    name: str,
    parent: SearchViewReceipt,
    before: tuple[StoredSegmentRef, ...],
    after: tuple[StoredSegmentRef, ...],
    generation_recipe: GenerationRecipe,
    search_view_recipe: SearchViewRecipe,
) -> tuple[LogicalGenerationReceipt, DeltaBuildResult]:
    generation = _generation(generation_root, name, after, generation_recipe)
    changes = _changes(before, after)
    result = build_delta_view(
        pageindex,
        parent,
        generation,
        generation_recipe,
        changes,
        _new_refs(changes, after),
        search_view_recipe,
        CompactionPolicy(max_delta_layers=99),
        max_run_bytes=64,
        merge_fan_in=2,
    )
    return generation, result


@pytest.fixture
def reader_corpus(tmp_path: Path) -> ReaderCorpus:
    pageindex = tmp_path / "pageindex"
    generations = tmp_path / "generations"
    generations.mkdir()
    generation_recipe = GenerationRecipe(
        body_df_min=2,
        body_df_ratio_numerator=3,
        body_df_ratio_denominator=4,
    )
    search_view_recipe = SearchViewRecipe()

    initial = (
        _put(
            pageindex,
            "note:chain",
            (("titlehot chaina", ("crumbhot",), "bodyhot titlehot crumbhot obsoletea"),),
            "a",
        ),
        _put(
            pageindex,
            "book:delete",
            (("titlehot delete", ("crumbhot",), "bodyhot titlehot crumbhot deletedtoken"),),
            "a",
        ),
        _put(
            pageindex,
            "book:shared",
            (("titlehot booktoken", ("crumbhot",), "bodyhot titlehot crumbhot booktoken"),),
            "a",
        ),
        _put(
            pageindex,
            "note:shared",
            (("titlehot notetoken", ("crumbhot",), "bodyhot titlehot crumbhot notetoken"),),
            "a",
        ),
    )
    initial_by_key = {ref.doc_key: ref for ref in initial}
    generation0 = _generation(
        generations, "generation-0", initial, generation_recipe
    )
    initial_base, initial_view = build_base_view(
        pageindex,
        initial,
        generation0,
        generation_recipe,
        search_view_recipe,
        max_run_bytes=64,
        merge_fan_in=2,
    )

    added = _put(
        pageindex,
        "paper:added",
        (
            (
                "titlehot addedtoken",
                ("crumbhot",),
                "bodyhot titlehot crumbhot bodyrare addedtoken",
            ),
            (
                "titlehot addedsecond",
                ("crumbhot",),
                "bodyhot titlehot crumbhot addedsecond",
            ),
        ),
        "a",
    )
    chain_b = _put(
        pageindex,
        "note:chain",
        (("titlehot chainb", ("crumbhot",), "bodyhot titlehot crumbhot obsoleteb"),),
        "b",
    )
    after1 = (
        chain_b,
        initial_by_key["book:shared"],
        initial_by_key["note:shared"],
        added,
    )
    _generation1, delta1 = _advance(
        pageindex,
        generations,
        "generation-1",
        initial_view,
        initial,
        after1,
        generation_recipe,
        search_view_recipe,
    )

    chain_c = _put(
        pageindex,
        "note:chain",
        (("titlehot chainc", ("crumbhot",), "bodyhot titlehot crumbhot chainc"),),
        "c",
    )
    after2 = (
        chain_c,
        initial_by_key["book:shared"],
        initial_by_key["note:shared"],
        added,
    )
    _generation2, delta2 = _advance(
        pageindex,
        generations,
        "generation-2",
        delta1.view,
        after1,
        after2,
        generation_recipe,
        search_view_recipe,
    )

    final_refs = (
        initial_by_key["book:shared"],
        initial_by_key["note:shared"],
        added,
    )
    generation3, delta3 = _advance(
        pageindex,
        generations,
        "generation-3",
        delta2.view,
        after2,
        final_refs,
        generation_recipe,
        search_view_recipe,
    )
    clean_base, clean_view = build_base_view(
        pageindex,
        final_refs,
        generation3,
        generation_recipe,
        search_view_recipe,
        max_run_bytes=64,
        merge_fan_in=2,
    )

    delta_results = (delta1, delta2, delta3)
    layer_receipts = {
        initial_base.base_id: initial_base.layer,
        **{
            result.delta.delta_id: result.delta.layer
            for result in delta_results
        },
    }
    return ReaderCorpus(
        pageindex=pageindex,
        generation_recipe=generation_recipe,
        search_view_recipe=search_view_recipe,
        initial_generation=generation0,
        final_generation=generation3,
        initial_base=initial_base,
        initial_view=initial_view,
        delta_results=delta_results,
        final_view=delta3.view,
        clean_base=clean_base,
        clean_view=clean_view,
        final_refs=final_refs,
        layer_receipts=layer_receipts,
    )


def _open_incremental(
    corpus: ReaderCorpus,
    *,
    read_observer: Callable[[str, str, int, int], None] | None = None,
    chunk_cache_bytes: int = 1024 * 1024,
) -> PinnedSearchView:
    return PinnedSearchView.open(
        corpus.pageindex,
        ViewPin(corpus.final_generation.generation_id, corpus.final_view.view_id),
        corpus.final_generation,
        read_observer=read_observer,
        chunk_cache_bytes=chunk_cache_bytes,
    )


def _open_clean(corpus: ReaderCorpus) -> PinnedSearchView:
    return PinnedSearchView.open(
        corpus.pageindex,
        ViewPin(corpus.final_generation.generation_id, corpus.clean_view.view_id),
        corpus.final_generation,
    )


def _logical_documents(
    documents: Mapping[str, ViewDocumentOwner],
) -> dict[str, str]:
    return {
        owner.doc_key: owner.segment_hash
        for owner in documents.values()
    }


def test_open_requires_exact_view_pin_and_generation_receipt(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    with _open_incremental(corpus) as reader:
        assert _logical_documents(reader.documents()) == {
            ref.doc_key: ref.segment_hash for ref in corpus.final_refs
        }

    stale_generation_pin = ViewPin(
        corpus.initial_generation.generation_id,
        corpus.final_view.view_id,
    )
    with pytest.raises(ValueError):
        PinnedSearchView.open(
            corpus.pageindex,
            stale_generation_pin,
            corpus.final_generation,
        )

    stale_view_pin = ViewPin(
        corpus.final_generation.generation_id,
        corpus.initial_view.view_id,
    )
    with pytest.raises(ValueError):
        PinnedSearchView.open(
            corpus.pageindex,
            stale_view_pin,
            corpus.final_generation,
        )

    exact_pin = ViewPin(
        corpus.final_generation.generation_id,
        corpus.final_view.view_id,
    )
    with pytest.raises(ValueError):
        PinnedSearchView.open(
            corpus.pageindex,
            exact_pin,
            corpus.initial_generation,
        )

    forged_manifest_ref = replace(
        corpus.final_generation.manifest_ref,
        sha256="f" * 64,
    )
    forged_receipt = replace(
        corpus.final_generation,
        manifest_ref=forged_manifest_ref,
    )
    with pytest.raises(ValueError):
        PinnedSearchView.open(
            corpus.pageindex,
            exact_pin,
            forged_receipt,
        )


def test_multidelta_newest_wins_matches_clean_base(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    tokens = (
        "titlehot",
        "crumbhot",
        "bodyhot",
        "bodyrare",
        "booktoken",
        "notetoken",
        "addedtoken",
        "addedsecond",
        "obsoletea",
        "obsoleteb",
        "chainc",
        "deletedtoken",
        "missingtoken",
    )
    with _open_incremental(corpus) as incremental:
        with _open_clean(corpus) as clean:
            assert incremental.corpus_stats() == clean.corpus_stats()
            assert _logical_documents(incremental.documents()) == _logical_documents(
                clean.documents()
            )
            for token in tokens:
                assert list(incremental.iter_raw_postings(token)) == list(
                    clean.iter_raw_postings(token)
                )
                assert list(incremental.iter_effective_postings(token)) == list(
                    clean.iter_effective_postings(token)
                )
            assert incremental.token_stats(tokens) == clean.token_stats(tokens)

        final_keys = {
            owner.doc_key for owner in incremental.documents().values()
        }
        assert {"book:shared", "note:shared"} <= final_keys
        assert "book:delete" not in final_keys
        assert "paper:added" in final_keys
        for disappeared in (
            "obsoletea",
            "obsoleteb",
            "chainc",
            "deletedtoken",
        ):
            assert list(incremental.iter_raw_postings(disappeared)) == []
            assert list(incremental.iter_effective_postings(disappeared)) == []
        assert list(incremental.iter_raw_postings("addedtoken"))


def test_effective_postings_prune_only_extreme_body_field(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    with _open_incremental(corpus) as reader:
        statistics = reader.token_stats(
            ("bodyhot", "titlehot", "crumbhot", "bodyrare", "missingtoken")
        )
        assert statistics["bodyhot"] == TokenSummary("bodyhot", 4, 0, 4)
        assert statistics["titlehot"] == TokenSummary("titlehot", 4, 4, 4)
        assert statistics["crumbhot"] == TokenSummary("crumbhot", 4, 4, 4)
        assert statistics["bodyrare"] == TokenSummary("bodyrare", 1, 0, 1)
        assert statistics["missingtoken"] is None

        assert list(reader.iter_raw_postings("bodyhot"))
        assert list(reader.iter_effective_postings("bodyhot")) == []

        raw_title = list(reader.iter_raw_postings("titlehot"))
        effective_title = list(reader.iter_effective_postings("titlehot"))
        assert raw_title and all(row.body_tf > 0 for row in raw_title)
        assert effective_title and all(
            row.title_tf > 0 and row.body_tf == 0 for row in effective_title
        )

        raw_breadcrumb = list(reader.iter_raw_postings("crumbhot"))
        effective_breadcrumb = list(reader.iter_effective_postings("crumbhot"))
        assert raw_breadcrumb and all(row.body_tf > 0 for row in raw_breadcrumb)
        assert effective_breadcrumb and all(
            row.breadcrumb_tf > 0 and row.body_tf == 0
            for row in effective_breadcrumb
        )

        assert list(reader.iter_effective_postings("bodyrare")) == list(
            reader.iter_raw_postings("bodyrare")
        )


def test_effective_postings_do_not_read_pruned_body_partitions(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    reads: list[ReadEvent] = []

    def observe(layer_id: str, name: str, offset: int, size: int) -> None:
        reads.append((layer_id, name, offset, size))

    with _open_incremental(corpus, read_observer=observe) as reader:
        body_ranges: dict[str, tuple[int, int]] = {}
        for layer in reader._layers_newest:
            record = layer.reader.lookup_term("titlehot")
            assert record is not None
            if record.body_offset is not None:
                body_ranges[layer.layer_id] = (
                    record.body_offset,
                    record.body_offset + record.body_bytes,
                )

        reads.clear()
        assert list(reader.iter_effective_postings("titlehot"))
        effective_reads = tuple(reads)
        assert not any(
            name == "postings.piv"
            and layer_id in body_ranges
            and offset < body_ranges[layer_id][1]
            and offset + size > body_ranges[layer_id][0]
            for layer_id, name, offset, size in effective_reads
        )

        reads.clear()
        assert list(reader.iter_raw_postings("titlehot"))
        assert any(
            name == "postings.piv"
            and layer_id in body_ranges
            and offset < body_ranges[layer_id][1]
            and offset + size > body_ranges[layer_id][0]
            for layer_id, name, offset, size in reads
        )


def test_token_stats_reads_at_most_one_sparse_window_per_layer(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    reads: list[ReadEvent] = []

    def observe(layer_id: str, name: str, offset: int, size: int) -> None:
        reads.append((layer_id, name, offset, size))

    with _open_incremental(corpus, read_observer=observe) as reader:
        reads.clear()
        assert reader.token_stats(("bodyhot",))["bodyhot"] == TokenSummary(
            "bodyhot", 4, 0, 4
        )

    assert reads
    assert {name for _layer, name, _offset, _size in reads} == {"terms.jsonl"}
    by_layer: dict[str, list[ReadEvent]] = defaultdict(list)
    for event in reads:
        by_layer[event[0]].append(event)
    assert set(by_layer) == set(corpus.layer_ids)
    assert all(len(events) <= 1 for events in by_layer.values())


def _document_blocks(
    receipt: PostingLayerReceipt,
) -> dict[str, int]:
    payload = json.loads(
        (receipt.root / receipt.documents.relative_path).read_bytes()
    )
    return {
        row["doc_uid"]: row["chunk_block_bytes"]
        for row in payload["documents"]
    }


def test_candidate_metrics_and_chunks_read_only_addressed_documents(
    reader_corpus: ReaderCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = reader_corpus
    with _open_incremental(corpus) as reader:
        added_refs = tuple(
            row.chunk_ref for row in reader.iter_raw_postings("addedtoken")
        ) + tuple(
            row.chunk_ref for row in reader.iter_raw_postings("addedsecond")
        )
        book_ref = next(
            row.chunk_ref
            for row in reader.iter_raw_postings("booktoken")
            if row.chunk_ref.doc_uid
            != added_refs[0].doc_uid
        )
        candidates = tuple(sorted((*added_refs, book_ref)))
        owners = reader.documents()
        candidate_layer_ids = {
            owners[ref.doc_uid].owner_layer_id for ref in candidates
        }

    with _open_clean(corpus) as clean:
        expected_metrics = clean.get_chunk_metrics(candidates)
        expected_chunks = clean.get_chunks(candidates)

    reads: list[ReadEvent] = []

    def observe(layer_id: str, name: str, offset: int, size: int) -> None:
        reads.append((layer_id, name, offset, size))

    with _open_incremental(corpus, read_observer=observe) as reader:
        reads.clear()
        assert reader.get_chunk_metrics(candidates) == expected_metrics

    assert reads
    assert {layer_id for layer_id, _name, _offset, _size in reads} == (
        candidate_layer_ids
    )
    assert {name for _layer, name, _offset, _size in reads} <= {
        "layer-documents.json",
        "chunks.pcv",
    }
    bytes_by_layer_and_file: Counter[tuple[str, str]] = Counter()
    for layer_id, name, _offset, size in reads:
        bytes_by_layer_and_file[(layer_id, name)] += size
    candidate_uids_by_layer: dict[str, set[str]] = defaultdict(set)
    for ref in candidates:
        candidate_uids_by_layer[owners[ref.doc_uid].owner_layer_id].add(
            ref.doc_uid
        )
    for layer_id in candidate_layer_ids:
        receipt = corpus.layer_receipts[layer_id]
        block_bytes = _document_blocks(receipt)
        expected_pcv_bound = len(CHUNKS_MAGIC) + 2 * sum(
            block_bytes[doc_uid]
            for doc_uid in candidate_uids_by_layer[layer_id]
        )
        assert bytes_by_layer_and_file[(layer_id, "chunks.pcv")] <= (
            expected_pcv_bound
        )
        assert bytes_by_layer_and_file[(layer_id, "layer-documents.json")] <= (
            receipt.documents.byte_size
        )

    load_calls: list[tuple[str, tuple[int, ...]]] = []
    original_load_chunks = SegmentProjector.load_chunks

    def observed_load_chunks(
        projector: SegmentProjector,
        ref: StoredSegmentRef,
        local_ids: object,
    ) -> dict[int, dict[str, object]]:
        requested = tuple(local_ids)  # type: ignore[arg-type]
        load_calls.append((ref.segment_hash, requested))
        return original_load_chunks(projector, ref, requested)

    monkeypatch.setattr(SegmentProjector, "load_chunks", observed_load_chunks)
    with _open_incremental(corpus, chunk_cache_bytes=1024 * 1024) as reader:
        assert reader.get_chunks(candidates) == expected_chunks
        first_calls = tuple(load_calls)
        assert reader.get_chunks(candidates) == expected_chunks
        assert tuple(load_calls) == first_calls

    expected_ids_by_segment: dict[str, set[int]] = defaultdict(set)
    for ref in candidates:
        expected_ids_by_segment[ref.segment_hash].add(ref.local_id)
    assert len(load_calls) == len(expected_ids_by_segment)
    assert {
        segment_hash: set(local_ids)
        for segment_hash, local_ids in load_calls
    } == expected_ids_by_segment
