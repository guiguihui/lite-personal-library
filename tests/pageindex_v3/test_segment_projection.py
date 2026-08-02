from __future__ import annotations

from collections import Counter
import copy
from dataclasses import replace
import gc
from pathlib import Path
import weakref

import pytest

import app.index.v3.segment_projection as projection_module
from app.index.v2.canonical import canonical_hash
from app.index.v2.models import SegmentRecipe
from app.index.v2.object_store import StoredSegmentRef, put_segment
from app.index.v3.models import ChunkRef, SearchPosting, make_doc_uid
from app.index.v3.segment_projection import (
    ChunkMetric,
    SegmentProjection,
    SegmentProjector,
)
from app.index.v3.summary_store import load_summary, put_summary
from app.retrieval.tokenizer import tokenize


def _document_path(doc_type: str, slug: str) -> str:
    if doc_type == "note":
        return f"notes/{slug}.md"
    return f"{doc_type}s/{slug}/_index.md"


def _valid_segment(
    doc_key: str = "note:alpha",
    fields: list[tuple[str, list[str], str]] | None = None,
) -> dict[str, object]:
    doc_type, slug = doc_key.split(":", 1)
    actual_fields = fields
    if actual_fields is None:
        actual_fields = [
            ("Title title shared", ["Path"], "Body body shared"),
            ("", [], "Body"),
        ]

    chunks: list[dict[str, object]] = []
    postings: dict[str, list[list[int]]] = {}
    for local_id, (title, breadcrumb, body) in enumerate(actual_fields):
        title_tf = Counter(tokenize(title))
        breadcrumb_tf = Counter(tokenize(" ".join(breadcrumb)))
        body_tf = Counter(tokenize(body))
        chunks.append(
            {
                "local_id": local_id,
                "node_key": "node-1",
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

    recipe = SegmentRecipe().as_dict()
    source_files = [
        {"path": _document_path(doc_type, slug), "sha256": "a" * 64}
    ]
    return {
        "schema_version": 2,
        "segment_recipe": recipe,
        "document": {
            "doc_key": doc_key,
            "type": doc_type,
            "id": slug,
        },
        "fingerprint": {
            "content_hash": canonical_hash(source_files),
            "recipe_hash": canonical_hash(recipe),
            "source_files": source_files,
        },
        "nodes": [{"node_key": "node-1", "legacy_node_id": "1"}],
        "chunks": chunks,
        "postings": {
            token: postings[token]
            for token in sorted(postings, key=lambda value: value.encode("utf-8"))
        },
    }


def _store(
    tmp_path: Path,
    segment: dict[str, object] | None = None,
) -> tuple[Path, StoredSegmentRef]:
    pageindex = tmp_path / "pageindex"
    return pageindex, put_segment(pageindex, segment or _valid_segment())


def test_project_preserves_raw_field_tfs_and_stable_chunk_refs(
    tmp_path: Path,
) -> None:
    pageindex, ref = _store(tmp_path)

    projection = SegmentProjector(pageindex).project(ref)
    doc_uid = make_doc_uid(ref.doc_key)

    expected = (
        SearchPosting("body", ChunkRef(doc_uid, ref.segment_hash, 0), 0, 0, 2),
        SearchPosting("body", ChunkRef(doc_uid, ref.segment_hash, 1), 0, 0, 1),
        SearchPosting("path", ChunkRef(doc_uid, ref.segment_hash, 0), 0, 1, 0),
        SearchPosting("shared", ChunkRef(doc_uid, ref.segment_hash, 0), 1, 0, 1),
        SearchPosting("title", ChunkRef(doc_uid, ref.segment_hash, 0), 2, 0, 0),
    )
    assert projection.ref == ref
    assert projection.postings == expected
    assert projection.postings == tuple(sorted(projection.postings))
    assert projection.chunk_metrics == (
        ChunkMetric(0, 3, 1, 3),
        ChunkMetric(1, 0, 0, 1),
    )


def test_summary_uses_union_df_and_exact_scalar_sums(tmp_path: Path) -> None:
    pageindex, ref = _store(tmp_path)

    summary = SegmentProjector(pageindex).summarize(ref)

    assert summary.segment_hash == ref.segment_hash
    assert summary.doc_uid == make_doc_uid(ref.doc_key)
    assert summary.chunk_count == 2
    assert (
        summary.title_length_sum,
        summary.breadcrumb_length_sum,
        summary.body_length_sum,
    ) == (3, 1, 4)
    assert summary.posting_count == 5
    assert {
        token.token: (token.df_any, token.df_nonbody, token.df_body)
        for token in summary.tokens
    } == {
        "body": (2, 0, 2),
        "path": (1, 1, 0),
        "shared": (1, 1, 1),
        "title": (1, 1, 0),
    }


def test_projection_summary_sidecar_round_trip(tmp_path: Path) -> None:
    pageindex, ref = _store(tmp_path)
    summary = SegmentProjector(pageindex).summarize(ref)

    summary_ref = put_summary(pageindex, summary)

    assert load_summary(pageindex, ref, summary_ref) == summary


def test_project_keeps_body_tf_above_pruning_threshold(tmp_path: Path) -> None:
    segment = _valid_segment(
        fields=[("", [], "common") for _ in range(256)],
    )
    pageindex, ref = _store(tmp_path, segment)

    projection = SegmentProjector(pageindex).project(ref)

    assert len(projection.postings) == 256
    assert all(row.token == "common" and row.body_tf == 1 for row in projection.postings)
    assert projection.summary.tokens[0].df_body == 256
    assert projection.summary.tokens[0].df_any == 256


@pytest.mark.parametrize("fields", [[], [("", [], "")]])
def test_project_supports_empty_segments_and_fields(
    tmp_path: Path,
    fields: list[tuple[str, list[str], str]],
) -> None:
    pageindex, ref = _store(tmp_path, _valid_segment(fields=fields))

    projection = SegmentProjector(pageindex).project(ref)

    assert projection.summary.chunk_count == len(fields)
    assert projection.summary.posting_count == 0
    assert projection.summary.tokens == ()
    assert projection.postings == ()


def test_project_sorts_unicode_tokens_by_utf8_bytes(tmp_path: Path) -> None:
    segment = _valid_segment(fields=[("知识知识", ["路径"], "正文内容")])
    pageindex, ref = _store(tmp_path, segment)

    projection = SegmentProjector(pageindex).project(ref)
    tokens = tuple(token.token for token in projection.summary.tokens)

    assert tokens
    assert tokens == tuple(sorted(tokens, key=lambda value: value.encode("utf-8")))
    assert tuple(row.token for row in projection.postings) == tuple(
        sorted((row.token for row in projection.postings), key=lambda value: value.encode("utf-8"))
    )


def test_same_slug_across_types_has_distinct_document_identity(tmp_path: Path) -> None:
    pageindex = tmp_path / "pageindex"
    note_ref = put_segment(pageindex, _valid_segment("note:shared"))
    paper_ref = put_segment(pageindex, _valid_segment("paper:shared"))
    projector = SegmentProjector(pageindex)

    note = projector.project(note_ref)
    paper = projector.project(paper_ref)

    assert note.summary.doc_uid != paper.summary.doc_uid
    assert {row.chunk_ref.doc_uid for row in note.postings} == {note.summary.doc_uid}
    assert {row.chunk_ref.doc_uid for row in paper.postings} == {paper.summary.doc_uid}


def _invalid_variants() -> list[tuple[str, callable]]:
    def schema(segment: dict[str, object]) -> None:
        segment["schema_version"] = 3

    def recipe(segment: dict[str, object]) -> None:
        cast = segment["segment_recipe"]
        assert isinstance(cast, dict)
        cast["unknown"] = "value"

    def fingerprint(segment: dict[str, object]) -> None:
        cast = segment["fingerprint"]
        assert isinstance(cast, dict)
        cast["content_hash"] = "f" * 64

    def duplicate_node(segment: dict[str, object]) -> None:
        cast = segment["nodes"]
        assert isinstance(cast, list)
        cast.append(copy.deepcopy(cast[0]))

    def local_id_gap(segment: dict[str, object]) -> None:
        chunks = segment["chunks"]
        postings = segment["postings"]
        assert isinstance(chunks, list) and isinstance(postings, dict)
        chunks[0]["local_id"] = 2
        for rows in postings.values():
            for row in rows:
                if row[0] == 0:
                    row[0] = 2

    def unknown_node(segment: dict[str, object]) -> None:
        chunks = segment["chunks"]
        assert isinstance(chunks, list)
        chunks[0]["node_key"] = "missing"

    def bool_length(segment: dict[str, object]) -> None:
        chunks = segment["chunks"]
        assert isinstance(chunks, list)
        chunks[0]["lengths"]["title"] = True

    def wrong_length(segment: dict[str, object]) -> None:
        chunks = segment["chunks"]
        assert isinstance(chunks, list)
        chunks[0]["lengths"]["body"] += 1

    def wrong_tf(segment: dict[str, object]) -> None:
        postings = segment["postings"]
        assert isinstance(postings, dict)
        postings["body"][0][3] += 1

    def duplicate_posting(segment: dict[str, object]) -> None:
        postings = segment["postings"]
        assert isinstance(postings, dict)
        postings["body"].append(copy.deepcopy(postings["body"][0]))

    def zero_tf(segment: dict[str, object]) -> None:
        postings = segment["postings"]
        assert isinstance(postings, dict)
        postings["body"][0][1:] = [0, 0, 0]

    return [
        ("schema", schema),
        ("recipe", recipe),
        ("fingerprint", fingerprint),
        ("duplicate node", duplicate_node),
        ("compact", local_id_gap),
        ("unknown node", unknown_node),
        ("length", bool_length),
        ("length", wrong_length),
        ("postings", wrong_tf),
        ("postings", duplicate_posting),
        ("posting", zero_tf),
    ]


@pytest.mark.parametrize(("match", "mutate"), _invalid_variants())
def test_project_rejects_invalid_segment_facts(
    tmp_path: Path,
    match: str,
    mutate: callable,
) -> None:
    segment = _valid_segment()
    mutate(segment)
    pageindex, ref = _store(tmp_path, segment)

    with pytest.raises((TypeError, ValueError), match=match):
        SegmentProjector(pageindex).project(ref)


def test_project_rejects_noncanonical_posting_row_order(tmp_path: Path) -> None:
    segment = _valid_segment()
    postings = segment["postings"]
    assert isinstance(postings, dict)
    postings["body"] = list(reversed(postings["body"]))
    pageindex, ref = _store(tmp_path, segment)

    with pytest.raises(ValueError, match="postings"):
        SegmentProjector(pageindex).project(ref)


def test_load_chunks_returns_sorted_requested_deep_copies(tmp_path: Path) -> None:
    pageindex, ref = _store(tmp_path)
    projector = SegmentProjector(pageindex)

    first = projector.load_chunks(ref, [1, 0])
    assert list(first) == [0, 1]
    assert set(first) == {0, 1}
    first[0]["breadcrumb"].append("mutated")
    first[0]["lengths"]["body"] = 99

    second = projector.load_chunks(ref, [0])
    assert second[0]["breadcrumb"] == ["Path"]
    assert second[0]["lengths"]["body"] == 3
    assert second[0]["legacy_node_id"] == "1"


def test_load_chunks_rejects_chunk_node_legacy_identity_drift(
    tmp_path: Path,
) -> None:
    segment = _valid_segment()
    chunks = segment["chunks"]
    assert isinstance(chunks, list)
    chunks[0]["legacy_node_id"] = "999"
    pageindex, ref = _store(tmp_path, segment)

    with pytest.raises(ValueError, match="legacy_node_id differs"):
        SegmentProjector(pageindex).load_chunks(ref, [0])


@pytest.mark.parametrize("local_ids", [[True], [-1], [0, 0], [99], "0"])
def test_load_chunks_rejects_invalid_or_missing_ids(
    tmp_path: Path,
    local_ids: object,
) -> None:
    pageindex, ref = _store(tmp_path)

    with pytest.raises((TypeError, ValueError, KeyError)):
        SegmentProjector(pageindex).load_chunks(ref, local_ids)


def test_load_chunks_empty_request_does_not_decode_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, ref = _store(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("empty hydration must not decode a Segment")

    monkeypatch.setattr(projection_module, "load_segment", forbidden)
    assert SegmentProjector(pageindex).load_chunks(ref, []) == {}


def test_projection_value_rejects_inconsistent_bindings(tmp_path: Path) -> None:
    pageindex, ref = _store(tmp_path)
    projection = SegmentProjector(pageindex).project(ref)

    wrong_summary = replace(projection.summary, content_hash="f" * 64)
    with pytest.raises(ValueError, match="content_hash"):
        SegmentProjection(
            ref=ref,
            summary=wrong_summary,
            postings=projection.postings,
            chunk_metrics=projection.chunk_metrics,
        )

    wrong_posting = replace(
        projection.postings[0],
        chunk_ref=ChunkRef("e" * 64, ref.segment_hash, 0),
    )
    with pytest.raises(ValueError, match="doc_uid"):
        SegmentProjection(
            ref=ref,
            summary=projection.summary,
            postings=(wrong_posting, *projection.postings[1:]),
            chunk_metrics=projection.chunk_metrics,
        )


def test_project_to_sink_streams_one_row_at_a_time_with_summary_and_metrics(
    tmp_path: Path,
) -> None:
    pageindex, ref = _store(tmp_path)
    projector = SegmentProjector(pageindex)
    observed: list[SearchPosting] = []

    summary, metrics = projector.project_to_sink(ref, observed.append)
    materialized = projector.project(ref)

    assert tuple(observed) == materialized.postings
    assert summary == materialized.summary
    assert metrics == materialized.chunk_metrics


def test_summarize_does_not_materialize_search_postings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, ref = _store(tmp_path)

    def forbidden_posting(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("summarize must not construct SearchPosting rows")

    monkeypatch.setattr(
        projection_module,
        "SearchPosting",
        forbidden_posting,
    )

    summary = SegmentProjector(pageindex).summarize(ref)
    assert summary.posting_count == 5


def test_iter_postings_streams_and_releases_nonposting_segment_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, ref = _store(tmp_path)
    source = _valid_segment()

    class TrackedDict(dict):
        pass

    loaded: weakref.ReferenceType[TrackedDict] | None = None

    def tracked_loader(*_args: object, **_kwargs: object) -> TrackedDict:
        nonlocal loaded
        value = TrackedDict(copy.deepcopy(source))
        loaded = weakref.ref(value)
        return value

    monkeypatch.setattr(projection_module, "load_segment", tracked_loader)
    rows = SegmentProjector(pageindex).iter_postings(ref)
    assert loaded is None

    first = next(rows)
    gc.collect()
    assert first.token == "body"
    assert loaded is not None and loaded() is None
    assert tuple(rows)


def test_consecutive_projects_do_not_retain_decoded_segment_graphs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, ref = _store(tmp_path)
    source = _valid_segment()

    class TrackedDict(dict):
        pass

    previous: weakref.ReferenceType[TrackedDict] | None = None

    def tracked_loader(*_args: object, **_kwargs: object) -> TrackedDict:
        nonlocal previous
        gc.collect()
        assert previous is None or previous() is None
        value = TrackedDict(copy.deepcopy(source))
        previous = weakref.ref(value)
        return value

    monkeypatch.setattr(projection_module, "load_segment", tracked_loader)
    projector = SegmentProjector(pageindex)
    first = projector.project(ref)
    second = projector.project(ref)

    assert first == second
    gc.collect()
    assert previous is not None and previous() is None

