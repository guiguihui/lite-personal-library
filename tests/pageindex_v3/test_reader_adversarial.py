from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Callable

import pytest

from app.index.v2.artifacts import ArtifactRef
from app.index.v2.canonical import canonical_bytes
from app.index.v3.generation import LogicalGenerationReceipt
from app.index.v3.layer_codec import PostingLayerReader
from app.index.v3.models import ChunkRef, ViewPin, make_doc_uid
from app.index.v3.reader import PinnedSearchView, PinnedSearchViewError
from app.index.v3.segment_projection import SegmentProjector
from app.index.v3.view_store import load_view_documents

# Pytest's default import mode places this test directory on sys.path.  Importing
# only the shared fixture/helpers avoids rebuilding the deliberately multi-layer
# corpus while keeping this adversarial suite independent of its test functions.
from test_reader import ReaderCorpus, _open_incremental, reader_corpus


def _ref(corpus: ReaderCorpus, position: int = 0) -> ChunkRef:
    stored = corpus.final_refs[position]
    return ChunkRef(make_doc_uid(stored.doc_key), stored.segment_hash, 0)


def _generation_receipt(root: Path) -> LogicalGenerationReceipt:
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    input_proof = manifest["input_proof"]
    document_count = manifest["document_count"]
    return LogicalGenerationReceipt(
        candidate_dir=root,
        generation_id=manifest["generation"],
        generation_recipe_hash=manifest["generation_recipe_hash"],
        manifest_ref=ArtifactRef(
            relative_path="manifest.json",
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            byte_size=len(manifest_bytes),
            records=document_count,
        ),
        input_proof_ref=ArtifactRef(
            relative_path=input_proof["relative_path"],
            sha256=input_proof["sha256"],
            byte_size=input_proof["byte_size"],
            records=input_proof["records"],
        ),
        document_count=document_count,
    )


def test_closed_session_rejects_every_query_surface(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    reader = _open_incremental(corpus)
    candidate = _ref(corpus)
    reader.get_chunks((candidate,))  # Populate state that close must discard.

    reader.close()
    reader.close()  # Closing an already closed immutable session is idempotent.
    assert not reader._owners
    assert not reader._refs_by_uid
    assert not reader._layers_chronological
    assert not reader._layers_by_id
    assert not reader._active_ordinals_by_layer
    assert not reader._chunk_cache

    operations: tuple[Callable[[], object], ...] = (
        reader.corpus_stats,
        reader.documents,
        lambda: reader.token_stats(("booktoken",)),
        lambda: reader.iter_raw_postings("booktoken"),
        lambda: reader.iter_effective_postings("booktoken"),
        lambda: reader.get_chunk_metrics((candidate,)),
        lambda: reader.get_chunks((candidate,)),
        reader.__enter__,
    )
    for operation in operations:
        with pytest.raises(RuntimeError, match="PinnedSearchView is closed"):
            operation()


@pytest.mark.parametrize("method_name", ("get_chunk_metrics", "get_chunks"))
def test_chunk_apis_reject_duplicate_and_invalid_refs_before_io(
    reader_corpus: ReaderCorpus,
    method_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = reader_corpus
    valid = _ref(corpus)
    inactive_segment = ChunkRef(valid.doc_uid, "f" * 64, valid.local_id)
    if inactive_segment.segment_hash == valid.segment_hash:
        inactive_segment = ChunkRef(valid.doc_uid, "0" * 64, valid.local_id)
    unknown_uid = "0" * 64
    if unknown_uid == valid.doc_uid:
        unknown_uid = "f" * 64
    unknown_document = ChunkRef(unknown_uid, valid.segment_hash, valid.local_id)
    outside_document = ChunkRef(valid.doc_uid, valid.segment_hash, 2**32)

    projector_calls: list[tuple[str, tuple[int, ...]]] = []
    original = SegmentProjector.load_chunks

    def observed_load(
        projector: SegmentProjector,
        stored: object,
        local_ids: object,
    ) -> dict[int, dict[str, object]]:
        requested = tuple(local_ids)  # type: ignore[arg-type]
        projector_calls.append((stored.segment_hash, requested))  # type: ignore[attr-defined]
        return original(projector, stored, requested)  # type: ignore[arg-type]

    monkeypatch.setattr(SegmentProjector, "load_chunks", observed_load)
    with _open_incremental(corpus) as reader:
        method = getattr(reader, method_name)
        with pytest.raises(ValueError, match="duplicate ChunkRef"):
            method((valid, valid))
        with pytest.raises(PinnedSearchViewError, match="not active"):
            method((inactive_segment,))
        with pytest.raises(PinnedSearchViewError, match="not active"):
            method((unknown_document,))
        with pytest.raises(PinnedSearchViewError, match="outside the document"):
            method((outside_document,))
        with pytest.raises(TypeError, match="only ChunkRef"):
            method((object(),))

    assert projector_calls == []


def test_open_rejects_authenticated_owner_with_wrong_physical_route(
    reader_corpus: ReaderCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = reader_corpus
    owners = load_view_documents(corpus.final_view)
    doc_uid, owner = next(iter(owners.items()))
    forged = dict(owners)
    forged[doc_uid] = replace(owner, doc_ordinal=owner.doc_ordinal + 2**16)

    import app.index.v3.reader as reader_module

    monkeypatch.setattr(reader_module, "load_view_documents", lambda _view: forged)
    with pytest.raises(PinnedSearchViewError, match="owner route differs"):
        PinnedSearchView.open(
            corpus.pageindex,
            ViewPin(
                corpus.final_generation.generation_id,
                corpus.final_view.view_id,
            ),
            corpus.final_generation,
        )


def test_open_rejects_static_owner_artifact_tampering(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    owner_path = corpus.final_view.root / corpus.final_view.documents_ref.relative_path
    owner_path.write_bytes(owner_path.read_bytes() + b" ")

    with pytest.raises(ValueError, match="byte size does not match receipt"):
        PinnedSearchView.open(
            corpus.pageindex,
            ViewPin(
                corpus.final_generation.generation_id,
                corpus.final_view.view_id,
            ),
            corpus.final_generation,
        )


def test_zero_cache_reloads_and_small_cache_evicts_lru_segment(
    reader_corpus: ReaderCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = reader_corpus
    first = _ref(corpus, 0)
    second = _ref(corpus, 1)
    with _open_incremental(corpus, chunk_cache_bytes=0) as reader:
        first_chunk = reader.get_chunks((first,))[first]
        second_chunk = reader.get_chunks((second,))[second]
    single_entry_budget = max(
        len(canonical_bytes(first_chunk)),
        len(canonical_bytes(second_chunk)),
    )

    calls: list[str] = []
    original = SegmentProjector.load_chunks

    def observed_load(
        projector: SegmentProjector,
        stored: object,
        local_ids: object,
    ) -> dict[int, dict[str, object]]:
        calls.append(stored.segment_hash)  # type: ignore[attr-defined]
        return original(projector, stored, local_ids)  # type: ignore[arg-type]

    monkeypatch.setattr(SegmentProjector, "load_chunks", observed_load)

    with _open_incremental(corpus, chunk_cache_bytes=0) as reader:
        reader.get_chunks((first,))
        reader.get_chunks((first,))
    assert calls == [first.segment_hash, first.segment_hash]

    calls.clear()
    with _open_incremental(
        corpus, chunk_cache_bytes=single_entry_budget
    ) as reader:
        reader.get_chunks((first,))
        reader.get_chunks((second,))
        reader.get_chunks((first,))
    assert calls == [first.segment_hash, second.segment_hash, first.segment_hash]


def test_mutating_returned_chunk_does_not_pollute_cached_value(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    candidate = _ref(corpus)
    with _open_incremental(corpus) as reader:
        returned = reader.get_chunks((candidate,))[candidate]
        expected = copy.deepcopy(returned)
        returned["title"] = "poisoned"
        returned["breadcrumb"] = ["poisoned"]
        returned["lengths"] = {"title": 999, "breadcrumb": 999, "body": 999}

        assert reader.get_chunks((candidate,))[candidate] == expected


def test_observer_exception_closes_current_and_previously_opened_layers(
    reader_corpus: ReaderCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = reader_corpus
    failing_layer = corpus.delta_results[0].delta.delta_id
    failure = RuntimeError("observer failed")
    closed_roots: list[Path] = []
    original_close = PostingLayerReader.close

    def tracked_close(reader: PostingLayerReader) -> None:
        closed_roots.append(reader.receipt.root)
        original_close(reader)

    def fail_on_second_layer(
        layer_id: str,
        _name: str,
        _offset: int,
        _size: int,
    ) -> None:
        if layer_id == failing_layer:
            raise failure

    monkeypatch.setattr(PostingLayerReader, "close", tracked_close)
    with pytest.raises(RuntimeError) as raised:
        _open_incremental(corpus, read_observer=fail_on_second_layer)

    assert raised.value is failure
    assert corpus.initial_base.root in closed_roots
    assert corpus.delta_results[0].delta.root in closed_roots


def test_empty_token_batch_is_a_no_io_noop(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    reads: list[tuple[str, str, int, int]] = []
    with _open_incremental(corpus, read_observer=lambda *event: reads.append(event)) as reader:
        reads.clear()
        assert reader.token_stats(()) == {}
        assert reader.token_stats(iter(())) == {}

    assert reads == []


def test_layer_construction_failure_preserves_primary_and_closes_all(
    reader_corpus: ReaderCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = reader_corpus
    primary = RuntimeError("third layer construction failed")
    cleanup = RuntimeError("earlier layer cleanup failed")
    opened: list[PostingLayerReader] = []
    close_attempts: list[Path] = []

    import app.index.v3.reader as reader_module

    original_constructor = reader_module.PostingLayerReader
    original_close = PostingLayerReader.close

    def construct(*args: object, **kwargs: object) -> PostingLayerReader:
        if len(opened) == 2:
            raise primary
        reader = original_constructor(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(reader)
        return reader

    def close(reader: PostingLayerReader) -> None:
        close_attempts.append(reader.receipt.root)
        original_close(reader)
        if len(close_attempts) == 1:
            raise cleanup

    monkeypatch.setattr(reader_module, "PostingLayerReader", construct)
    monkeypatch.setattr(PostingLayerReader, "close", close)

    with pytest.raises(RuntimeError) as raised:
        _open_incremental(corpus)

    assert raised.value is primary
    assert len(opened) == 2
    assert close_attempts == [
        opened[1].receipt.root,
        opened[0].receipt.root,
    ]


def test_mutable_current_pointer_cannot_change_open_pinned_results(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    current = corpus.pageindex / "current.json"
    assert not current.exists()

    with _open_incremental(corpus) as reader:
        expected_postings = list(reader.iter_raw_postings("addedtoken"))
        expected_documents = dict(reader.documents())
        assert expected_postings

        current.write_text(
            json.dumps(
                {
                    "generation": corpus.initial_generation.generation_id,
                    "view": corpus.initial_view.view_id,
                }
            ),
            encoding="utf-8",
        )
        assert list(reader.iter_raw_postings("addedtoken")) == expected_postings
        assert dict(reader.documents()) == expected_documents

        current.write_text("not-json", encoding="utf-8")
        assert list(reader.iter_raw_postings("addedtoken")) == expected_postings
        assert dict(reader.documents()) == expected_documents


def test_intermediate_a_to_b_view_is_newest_wins(
    reader_corpus: ReaderCorpus,
) -> None:
    corpus = reader_corpus
    delta = corpus.delta_results[0]
    generation = _generation_receipt(
        corpus.final_generation.candidate_dir.parent / "generation-1"
    )
    assert generation.generation_id == delta.view.generation

    pin = ViewPin(generation.generation_id, delta.view.view_id)
    with PinnedSearchView.open(corpus.pageindex, pin, generation) as reader:
        chain_uid = make_doc_uid("note:chain")
        assert reader.documents()[chain_uid].owner_layer_id == delta.delta.delta_id
        assert list(reader.iter_raw_postings("chainb"))
        assert list(reader.iter_raw_postings("obsoleteb"))
        assert list(reader.iter_raw_postings("chaina")) == []
        assert list(reader.iter_raw_postings("obsoletea")) == []
        assert list(reader.iter_raw_postings("deletedtoken")) == []
        assert list(reader.iter_raw_postings("addedtoken"))


def test_open_rejects_reordered_delta_chain(
    reader_corpus: ReaderCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = reader_corpus

    import app.index.v3.reader as reader_module

    original_load = reader_module.load_search_view_metadata
    forged = copy.copy(corpus.final_view)
    object.__setattr__(forged, "delta_ids", tuple(reversed(forged.delta_ids)))

    def load_view(pageindex: Path, view_id: str):
        if view_id == corpus.final_view.view_id:
            return forged
        return original_load(pageindex, view_id)

    monkeypatch.setattr(reader_module, "load_search_view_metadata", load_view)
    with pytest.raises(PinnedSearchViewError, match="reordered or spliced"):
        PinnedSearchView.open(
            corpus.pageindex,
            ViewPin(
                corpus.final_generation.generation_id,
                corpus.final_view.view_id,
            ),
            corpus.final_generation,
        )


def test_relative_pageindex_path_survives_cwd_change(
    reader_corpus: ReaderCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = reader_corpus
    candidate = _ref(corpus)
    original_parent = corpus.pageindex.parent
    other_cwd = original_parent / "other-cwd"
    other_cwd.mkdir()

    monkeypatch.chdir(original_parent)
    reader = PinnedSearchView.open(
        Path(corpus.pageindex.name),
        ViewPin(corpus.final_generation.generation_id, corpus.final_view.view_id),
        corpus.final_generation,
    )
    with reader:
        monkeypatch.chdir(other_cwd)
        chunk = reader.get_chunks((candidate,))[candidate]
        assert chunk["title"]