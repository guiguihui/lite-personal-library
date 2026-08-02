from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.index.v3.models import ChunkRef, make_doc_uid
from app.index.v3.reader import PinnedSearchViewError
from app.index.v3.segment_projection import SegmentProjector

# Reuse the Base-plus-three-Delta corpus. Importing the decorated fixture into
# this module also makes it visible to pytest when this file runs in isolation.
from test_reader import ReaderCorpus, _open_incremental, reader_corpus  # noqa: F401


def test_document_chunk_refs_preserve_selected_document_order_without_segments(
    reader_corpus: ReaderCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs_by_key = {ref.doc_key: ref for ref in reader_corpus.final_refs}
    selected = (make_doc_uid("paper:added"), make_doc_uid("book:shared"))

    with _open_incremental(reader_corpus) as reader:
        segment_reads = Mock(side_effect=AssertionError("must not read Segment"))
        monkeypatch.setattr(SegmentProjector, "load_chunks", segment_reads)

        assert reader.document_chunk_refs(selected) == (
            ChunkRef(selected[0], refs_by_key["paper:added"].segment_hash, 0),
            ChunkRef(selected[0], refs_by_key["paper:added"].segment_hash, 1),
            ChunkRef(selected[1], refs_by_key["book:shared"].segment_hash, 0),
        )
        segment_reads.assert_not_called()


def test_document_chunk_refs_are_immutable_and_reject_ambiguous_requests(
    reader_corpus: ReaderCorpus,
) -> None:
    active_uid = make_doc_uid("note:shared")
    unknown_uid = "f" * 64

    with _open_incremental(reader_corpus) as reader:
        refs = reader.document_chunk_refs((active_uid,))
        assert isinstance(refs, tuple)
        assert refs == (
            ChunkRef(
                active_uid,
                next(
                    ref.segment_hash
                    for ref in reader_corpus.final_refs
                    if ref.doc_key == "note:shared"
                ),
                0,
            ),
        )

        with pytest.raises(ValueError, match="duplicate document UID"):
            reader.document_chunk_refs((active_uid, active_uid))
        with pytest.raises(PinnedSearchViewError, match="not active"):
            reader.document_chunk_refs((unknown_uid,))
        with pytest.raises(TypeError, match="iterable"):
            reader.document_chunk_refs(active_uid)
        with pytest.raises(TypeError, match="only strings"):
            reader.document_chunk_refs((active_uid, 3))
