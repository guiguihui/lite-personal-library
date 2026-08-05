from __future__ import annotations

import gc
import hashlib
from pathlib import Path
import weakref

import pytest

from app.index.v3.layer_codec import LayerDocument, PostingLayerReader
from app.index.v3.layer_runs import StagedLayerBuilder, build_sorted_layer
from app.index.v3.models import ChunkRef, SearchPosting, make_doc_uid
from app.index.v3.segment_projection import ChunkMetric


_LAYER_ARTIFACTS = (
    "layer-documents.json",
    "postings.piv",
    "chunks.pcv",
    "terms.jsonl",
    "terms.sidx.json",
)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _header(key: str) -> tuple[str, str, str]:
    return key, make_doc_uid(key), _digest(f"segment:{key}")


def _ordered_headers(*keys: str) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted((_header(key) for key in keys), key=lambda value: value[1].encode("utf-8"))
    )


def _documents() -> tuple[LayerDocument, ...]:
    values = (
        LayerDocument(
            *_header("note:alpha"),
            chunk_metrics=(
                ChunkMetric(0, 2, 1, 7),
                ChunkMetric(1, 1, 0, 4),
            ),
        ),
        LayerDocument(
            *_header("note:beta"),
            chunk_metrics=(ChunkMetric(0, 3, 2, 5),),
        ),
    )
    return tuple(sorted(values, key=lambda item: item.doc_uid.encode("utf-8")))


def _postings(documents: tuple[LayerDocument, ...]) -> tuple[SearchPosting, ...]:
    by_key = {document.doc_key: document for document in documents}

    def row(
        token: str,
        key: str,
        local_id: int,
        title_tf: int,
        breadcrumb_tf: int,
        body_tf: int,
    ) -> SearchPosting:
        document = by_key[key]
        return SearchPosting(
            token,
            ChunkRef(document.doc_uid, document.segment_hash, local_id),
            title_tf,
            breadcrumb_tf,
            body_tf,
        )

    # Deliberately neither token- nor document-sorted. Both builder paths must
    # produce the same canonical physical layer independently of input order.
    return (
        row("标题", "note:beta", 0, 1, 2, 1),
        row("apple", "note:alpha", 1, 1, 0, 0),
        row("body-only", "note:alpha", 1, 0, 0, 2),
        row("apple", "note:beta", 0, 0, 0, 3),
        row("apple", "note:alpha", 0, 2, 1, 4),
    )


def _build_staged(
    root: Path,
    documents: tuple[LayerDocument, ...],
    postings: tuple[SearchPosting, ...],
    *,
    max_run_bytes: int = 1,
    merge_fan_in: int = 2,
):
    by_uid: dict[str, list[SearchPosting]] = {
        document.doc_uid: [] for document in documents
    }
    for posting in postings:
        by_uid[posting.chunk_ref.doc_uid].append(posting)

    with StagedLayerBuilder(
        root,
        layer_kind="base",
        max_run_bytes=max_run_bytes,
        merge_fan_in=merge_fan_in,
    ) as stage:
        for document in documents:
            ticket = stage.begin_document(
                document.doc_key,
                document.doc_uid,
                document.segment_hash,
            )
            for posting in reversed(by_uid[document.doc_uid]):
                ticket.add_posting(posting)
            ticket.commit(document.chunk_count, iter(document.chunk_metrics))
        receipt = stage.finish()
    return receipt


def test_staged_builder_matches_build_sorted_layer_artifact_bytes(
    tmp_path: Path,
) -> None:
    documents = _documents()
    postings = _postings(documents)
    expected = build_sorted_layer(
        tmp_path / "materialized",
        documents=documents,
        postings=postings,
        layer_kind="base",
        max_run_bytes=1,
        merge_fan_in=2,
    )

    observed = _build_staged(tmp_path / "staged", documents, postings)

    for name in _LAYER_ARTIFACTS:
        assert (observed.root / name).read_bytes() == (
            expected.root / name
        ).read_bytes()


class _OneShotMetrics:
    def __init__(self, values: tuple[ChunkMetric, ...]) -> None:
        self._values = values
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations != 1:
            raise AssertionError("chunk metrics were consumed more than once")
        yield from self._values


class _WeakChunkMetric(ChunkMetric):
    """Weak-referenceable ChunkMetric used to prove staged ownership release."""


def test_commit_consumes_one_shot_metrics_once_and_releases_previous_metrics(
    tmp_path: Path,
) -> None:
    first, second = _ordered_headers("note:release-a", "note:release-b")
    target = tmp_path / "release"

    with StagedLayerBuilder(
        target,
        layer_kind="base",
        max_run_bytes=1,
        merge_fan_in=2,
    ) as stage:
        ticket = stage.begin_document(*first)
        ticket.add_posting(
            SearchPosting(
                "first",
                ChunkRef(first[1], first[2], 0),
                1,
                0,
                0,
            )
        )
        metric = _WeakChunkMetric(0, 3, 2, 1)
        metric_ref = weakref.ref(metric)
        source = _OneShotMetrics((metric,))
        ticket.commit(1, source)
        assert source.iterations == 1

        # The active stage may retain the compact document routing record, but
        # it must not retain the consumed per-chunk container or metric objects.
        del ticket, metric, source
        gc.collect()
        assert metric_ref() is None

        following = stage.begin_document(*second)
        following.add_posting(
            SearchPosting(
                "second",
                ChunkRef(second[1], second[2], 0),
                0,
                1,
                0,
            )
        )
        following.commit(1, iter((ChunkMetric(0, 1, 1, 1),)))
        stage.finish()


def test_ticket_accepts_postings_before_document_commit(tmp_path: Path) -> None:
    header = _header("note:post-before-commit")
    target = tmp_path / "post-before-commit"

    with StagedLayerBuilder(
        target,
        layer_kind="base",
        max_run_bytes=1,
        merge_fan_in=2,
    ) as stage:
        ticket = stage.begin_document(*header)
        ticket.add_posting(
            SearchPosting(
                "early",
                ChunkRef(header[1], header[2], 0),
                1,
                0,
                1,
            )
        )
        ticket.commit(1, (ChunkMetric(0, 1, 0, 1),))
        receipt = stage.finish()

    with PostingLayerReader(receipt) as reader:
        rows = tuple(reader.iter_token("early"))
    assert len(rows) == 1
    assert rows[0].chunk_ref == ChunkRef(header[1], header[2], 0)


@pytest.mark.parametrize(
    ("bad_field", "message"),
    (
        ("doc_uid", "doc_uid|document"),
        ("segment_hash", "segment_hash"),
    ),
)
def test_ticket_rejects_posting_owned_by_wrong_document_identity(
    tmp_path: Path,
    bad_field: str,
    message: str,
) -> None:
    header = _header(f"note:wrong-{bad_field}")
    doc_uid = _digest("foreign-document") if bad_field == "doc_uid" else header[1]
    segment_hash = (
        _digest("foreign-segment") if bad_field == "segment_hash" else header[2]
    )

    with pytest.raises(ValueError, match=message):
        with StagedLayerBuilder(
            tmp_path / f"wrong-{bad_field}",
            layer_kind="base",
            max_run_bytes=1,
            merge_fan_in=2,
        ) as stage:
            ticket = stage.begin_document(*header)
            ticket.add_posting(
                SearchPosting(
                    "foreign",
                    ChunkRef(doc_uid, segment_hash, 0),
                    1,
                    0,
                    0,
                )
            )


def test_commit_rejects_posting_local_id_outside_committed_chunk_count(
    tmp_path: Path,
) -> None:
    header = _header("note:bad-local-id")

    with pytest.raises(ValueError, match="local_id|chunk_count|outside"):
        with StagedLayerBuilder(
            tmp_path / "bad-local-id",
            layer_kind="base",
            max_run_bytes=1,
            merge_fan_in=2,
        ) as stage:
            ticket = stage.begin_document(*header)
            ticket.add_posting(
                SearchPosting(
                    "outside",
                    ChunkRef(header[1], header[2], 1),
                    1,
                    0,
                    0,
                )
            )
            ticket.commit(1, (ChunkMetric(0, 1, 0, 0),))


def test_finish_rejects_an_uncommitted_document(tmp_path: Path) -> None:
    header = _header("note:uncommitted")
    target = tmp_path / "uncommitted"

    with pytest.raises((ValueError, RuntimeError), match="commit"):
        with StagedLayerBuilder(
            target,
            layer_kind="base",
            max_run_bytes=1,
            merge_fan_in=2,
        ) as stage:
            ticket = stage.begin_document(*header)
            ticket.add_posting(
                SearchPosting(
                    "pending",
                    ChunkRef(header[1], header[2], 0),
                    1,
                    0,
                    0,
                )
            )
            stage.finish()

    assert not target.exists()


def test_begin_document_rejects_noncanonical_document_order(tmp_path: Path) -> None:
    low, high = _ordered_headers("note:order-a", "note:order-b")

    with pytest.raises(ValueError, match="order|monotonic|sorted"):
        with StagedLayerBuilder(
            tmp_path / "bad-order",
            layer_kind="base",
            max_run_bytes=1,
            merge_fan_in=2,
        ) as stage:
            first = stage.begin_document(*high)
            first.commit(0, ())
            stage.begin_document(*low)


def test_begin_document_rejects_duplicate_document(tmp_path: Path) -> None:
    header = _header("note:duplicate")

    with pytest.raises(ValueError, match="duplicate|already"):
        with StagedLayerBuilder(
            tmp_path / "duplicate",
            layer_kind="base",
            max_run_bytes=1,
            merge_fan_in=2,
        ) as stage:
            first = stage.begin_document(*header)
            first.commit(0, ())
            stage.begin_document(*header)


def test_forced_one_row_runs_and_two_way_merge_pass_deep_audit(
    tmp_path: Path,
) -> None:
    documents = _documents()
    receipt = _build_staged(
        tmp_path / "audited",
        documents,
        _postings(documents),
        max_run_bytes=1,
        merge_fan_in=2,
    )

    with PostingLayerReader(receipt) as reader:
        reader.audit()


def test_exceptional_context_exit_cleans_target_and_owned_scratch(
    tmp_path: Path,
) -> None:
    class Exploded(RuntimeError):
        pass

    target = tmp_path / "exploded"
    before = {path.name for path in tmp_path.iterdir()}
    header = _header("note:exploded")

    with pytest.raises(Exploded, match="projection failed"):
        with StagedLayerBuilder(
            target,
            layer_kind="base",
            max_run_bytes=1,
            merge_fan_in=2,
        ) as stage:
            ticket = stage.begin_document(*header)
            ticket.add_posting(
                SearchPosting(
                    "partial",
                    ChunkRef(header[1], header[2], 0),
                    1,
                    0,
                    0,
                )
            )
            raise Exploded("projection failed")

    assert not target.exists()
    assert {path.name for path in tmp_path.iterdir()} == before

def test_explicit_abort_surfaces_document_handle_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "abort-cleanup-failure"
    stage = StagedLayerBuilder(
        target,
        layer_kind="base",
        max_run_bytes=1,
        merge_fan_in=2,
    )
    documents = stage._documents
    assert documents is not None
    original_abort = documents.abort

    def failing_abort(primary_error=None):
        original_abort(primary_error)
        raise OSError("injected handle cleanup failure")

    monkeypatch.setattr(documents, "abort", failing_abort)
    with pytest.raises(OSError, match="injected handle cleanup failure"):
        stage.abort()

    assert not target.exists()
    assert not tuple(tmp_path.glob(".abort-cleanup-failure.layer-build.*"))
