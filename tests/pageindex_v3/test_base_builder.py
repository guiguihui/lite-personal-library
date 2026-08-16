from __future__ import annotations

from collections import Counter, defaultdict
import gc
import hashlib
import json
from pathlib import Path
import weakref

import pytest

import app.index.v2.compiler as compatibility_compiler
from app.index.v2.canonical import canonical_hash
from app.index.v2.models import SegmentRecipe
from app.index.v2.object_store import StoredSegmentRef, put_segment
import app.index.v3.base_builder as base_builder_module
from app.index.v3.base_builder import build_base_view
from app.index.v3.generation import build_logical_generation
from app.index.v3.layer_codec import PostingLayerReader
import app.index.v3.layer_runs as layer_runs_module
from app.index.v3.models import (
    ChunkRef,
    GenerationRecipe,
    SearchPosting,
    SearchViewRecipe,
    make_doc_uid,
)
import app.index.v3.segment_projection as projection_module
from app.index.v3.segment_projection import SegmentProjector
from app.index.v3.statistics import CorpusTotals
from app.index.v3.view_store import ViewStoreConflictError, load_view_documents
from app.retrieval.tokenizer import tokenize


_BASE_FILES = {
    "chunks.pcv",
    "layer-documents.json",
    "manifest.json",
    "postings.piv",
    "terms.jsonl",
    "terms.sidx.json",
}
_VIEW_FILES = {"documents.json", "manifest.json", "statistics.json"}


def _document_path(doc_type: str, slug: str) -> str:
    if doc_type == "note":
        return f"notes/{slug}.md"
    return f"{doc_type}s/{slug}/_index.md"


def _segment(
    doc_key: str,
    fields: tuple[tuple[str, tuple[str, ...], str], ...],
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

    recipe = SegmentRecipe().as_dict()
    source_files = [
        {
            "path": _document_path(doc_type, slug),
            "sha256": hashlib.sha256(doc_key.encode("utf-8")).hexdigest(),
        }
    ]
    return {
        "schema_version": 2,
        "segment_recipe": recipe,
        "document": {"doc_key": doc_key, "type": doc_type, "id": slug},
        "fingerprint": {
            "content_hash": canonical_hash(source_files),
            "recipe_hash": canonical_hash(recipe),
            "source_files": source_files,
        },
        "nodes": [{"node_key": "root", "legacy_node_id": "1"}],
        "chunks": chunks,
        "postings": {
            token: postings[token]
            for token in sorted(postings, key=lambda value: value.encode("utf-8"))
        },
    }


def _stored_corpus(pageindex: Path) -> tuple[StoredSegmentRef, ...]:
    values = (
        _segment(
            "note:zeta",
            (
                ("Shared Zeta", ("Root",), "body common shared"),
                ("", (), "shared body"),
            ),
        ),
        _segment(
            "book:alpha",
            (("Alpha shared", ("Root", "Books"), "common common"),),
        ),
        _segment(
            "note:中文",
            (("中文 shared", ("路径",), "body"),),
        ),
    )
    return tuple(put_segment(pageindex, value) for value in values)


def _proof(
    refs: tuple[StoredSegmentRef, ...],
    recipe: GenerationRecipe,
) -> dict[str, object]:
    return {
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


def _generation(
    tmp_path: Path,
    refs: tuple[StoredSegmentRef, ...],
    recipe: GenerationRecipe,
):
    return build_logical_generation(
        refs,
        _proof(refs, recipe),
        recipe,
        tmp_path / "logical-generation",
    )


def _artifact_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _expected_facts(pageindex: Path, refs: tuple[StoredSegmentRef, ...]):
    projections = tuple(SegmentProjector(pageindex).project(ref) for ref in refs)
    rows: dict[str, list[SearchPosting]] = defaultdict(list)
    metrics = {}
    summaries = []
    for projection in projections:
        summaries.append(projection.summary)
        for posting in projection.postings:
            rows[posting.token].append(posting)
        for metric in projection.chunk_metrics:
            metrics[
                ChunkRef(
                    projection.summary.doc_uid,
                    projection.summary.segment_hash,
                    metric.local_id,
                )
            ] = metric
    for token in rows:
        rows[token].sort(
            key=lambda row: (
                row.chunk_ref.doc_uid.encode("utf-8"),
                row.chunk_ref.local_id,
            )
        )
    totals = CorpusTotals.from_summaries(summaries, token_count=len(rows))
    return rows, metrics, tuple(summaries), totals


def test_full_base_is_order_independent_and_matches_clean_oracle(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    refs = _stored_corpus(pageindex)
    generation_recipe = GenerationRecipe(body_df_min=17)
    view_recipe = SearchViewRecipe()
    generation = _generation(tmp_path, refs, generation_recipe)
    expected_rows, expected_metrics, summaries, expected_totals = _expected_facts(
        pageindex, refs
    )

    base, view = build_base_view(
        pageindex,
        refs,
        generation,
        generation_recipe,
        view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    first_base_bytes = _artifact_bytes(base.root)
    first_view_bytes = _artifact_bytes(view.root)

    rebuilt_base, rebuilt_view = build_base_view(
        pageindex,
        iter(reversed(refs)),
        generation,
        generation_recipe,
        view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )

    assert rebuilt_base == base
    assert rebuilt_view == view
    assert base.base_id == (
        "769b4ab2b723fb9948f95835d1e1d95a"
        "0de756c86edc3871b03ce7f1c2f7eca4"
    )
    assert view.view_id == (
        "c6d927446d4c9b27af09803e54c05a45"
        "fbaf96586f063b43664834a00602c70a"
    )
    assert not tuple(pageindex.glob(".piv3-base-build.*"))
    assert _artifact_bytes(base.root) == first_base_bytes
    assert _artifact_bytes(view.root) == first_view_bytes
    assert {path.name for path in base.root.iterdir()} == _BASE_FILES
    assert {path.name for path in view.root.iterdir()} == _VIEW_FILES
    assert not {
        "global-index.json",
        "inverted-index.json",
        "chunks.json",
        "node-index.json",
    } & {path.name for path in base.root.iterdir()}

    assert base.statistics == expected_totals
    assert base.layer.term_count == expected_totals.token_count
    assert base.layer.document_count == expected_totals.documents
    assert base.layer.chunk_count == expected_totals.total_chunks
    # At least one logical row has both nonbody and body fields, proving that
    # posting_count came from SegmentSummary rather than split physical rows.
    assert base.layer.postings.records > base.statistics.posting_count
    assert json.loads((view.root / "statistics.json").read_text("utf-8")) == (
        expected_totals.as_dict()
    )

    with PostingLayerReader(base.layer, recipe=view_recipe) as reader:
        reader.audit()
        for token, rows in expected_rows.items():
            assert tuple(reader.iter_token(token)) == tuple(rows)
            record = reader.lookup_term(token)
            assert record is not None
            assert record.delta == (
                len(rows),
                sum(bool(row.title_tf or row.breadcrumb_tf) for row in rows),
                sum(bool(row.body_tf) for row in rows),
            )
        assert reader.lookup_term("definitely-absent") is None
        assert reader.get_chunk_metrics(expected_metrics) == expected_metrics

    owners = load_view_documents(view)
    expected_order = sorted(
        ((make_doc_uid(ref.doc_key), ref) for ref in refs),
        key=lambda item: item[0].encode("utf-8"),
    )
    assert tuple(owners) == tuple(doc_uid for doc_uid, _ref in expected_order)
    summaries_by_uid = {summary.doc_uid: summary for summary in summaries}
    for ordinal, (doc_uid, ref) in enumerate(expected_order):
        owner = owners[doc_uid]
        summary = summaries_by_uid[doc_uid]
        summary_path = (
            pageindex
            / "objects"
            / "search"
            / "summaries"
            / ref.segment_hash[:2]
            / f"{ref.segment_hash}.json"
        )
        payload = summary_path.read_bytes()
        assert owner.doc_key == ref.doc_key == summary.doc_key
        assert owner.segment_hash == ref.segment_hash == summary.segment_hash
        assert owner.summary_sha256 == hashlib.sha256(payload).hexdigest()
        assert owner.summary_bytes == len(payload)
        assert owner.owner_layer_kind == "base"
        assert owner.owner_layer_id == base.base_id
        assert owner.doc_ordinal == ordinal


def test_build_uses_exactly_one_streaming_projection_per_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    refs = _stored_corpus(pageindex)
    recipe = GenerationRecipe()
    generation = _generation(tmp_path, refs, recipe)
    calls: Counter[str] = Counter()
    live_segments: list[weakref.ReferenceType[TrackedDict]] = []
    peak = 0

    class TrackedDict(dict):
        pass

    original_load = projection_module.load_segment

    def tracked_load(*args: object, **kwargs: object) -> TrackedDict:
        nonlocal peak
        gc.collect()
        assert not any(reference() is not None for reference in live_segments)
        value = TrackedDict(original_load(*args, **kwargs))
        live_segments.append(weakref.ref(value))
        peak = max(
            peak,
            sum(reference() is not None for reference in live_segments),
        )
        return value

    original_project_to_sink = SegmentProjector.project_to_sink

    def tracked_project_to_sink(self, ref, consume_posting):
        calls[ref.doc_key] += 1
        return original_project_to_sink(self, ref, consume_posting)

    def forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("materializing compatibility path must not be used")

    monkeypatch.setattr(projection_module, "load_segment", tracked_load)
    monkeypatch.setattr(
        SegmentProjector, "project_to_sink", tracked_project_to_sink
    )
    monkeypatch.setattr(SegmentProjector, "project", forbidden)
    monkeypatch.setattr(SegmentProjector, "summarize", forbidden)
    monkeypatch.setattr(SegmentProjector, "iter_postings", forbidden)
    monkeypatch.setattr(layer_runs_module, "build_sorted_layer", forbidden)
    monkeypatch.setattr(
        compatibility_compiler, "compile_generation_to_candidate", forbidden
    )
    monkeypatch.setattr(
        base_builder_module, "canonical_bytes", forbidden, raising=False
    )

    base, view = build_base_view(
        pageindex,
        iter(reversed(refs)),
        generation,
        recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )

    gc.collect()
    assert calls == Counter({ref.doc_key: 1 for ref in refs})
    assert peak <= 1
    assert not any(reference() is not None for reference in live_segments)
    assert base.statistics.documents == len(refs)
    assert view.generation == generation.generation_id


class _Cancelled(RuntimeError):
    pass


def test_cancellation_cleans_private_build_state_without_touching_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    refs = _stored_corpus(pageindex)
    recipe = GenerationRecipe()
    generation = _generation(tmp_path, refs, recipe)
    base, view = build_base_view(
        pageindex,
        refs,
        generation,
        recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    base_bytes = _artifact_bytes(base.root)
    view_bytes = _artifact_bytes(view.root)
    armed = False
    original = SegmentProjector.project_to_sink

    def arm_after_projection(self, ref, consume_posting):
        nonlocal armed
        result = original(self, ref, consume_posting)
        armed = True
        return result

    def check_cancelled() -> None:
        if armed:
            raise _Cancelled("stop after first projected Segment")

    monkeypatch.setattr(SegmentProjector, "project_to_sink", arm_after_projection)
    with pytest.raises(_Cancelled, match="stop after first"):
        build_base_view(
            pageindex,
            refs,
            generation,
            recipe,
            max_run_bytes=1,
            merge_fan_in=2,
            check_cancelled=check_cancelled,
        )

    assert not tuple(pageindex.glob(".piv3-base-build.*"))
    assert _artifact_bytes(base.root) == base_bytes
    assert _artifact_bytes(view.root) == view_bytes


def test_finalize_conflict_retains_the_complete_candidate_for_diagnosis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    refs = _stored_corpus(pageindex)
    recipe = GenerationRecipe()
    generation = _generation(tmp_path, refs, recipe)
    retained: Path | None = None

    def conflict(_pageindex_dir: Path, receipt):
        nonlocal retained
        retained = receipt.root
        raise ViewStoreConflictError("injected content-address conflict")

    monkeypatch.setattr(base_builder_module, "finalize_base_object", conflict)
    with pytest.raises(ViewStoreConflictError, match="injected"):
        build_base_view(
            pageindex,
            refs,
            generation,
            recipe,
            max_run_bytes=1,
            merge_fan_in=2,
        )

    assert retained is not None
    assert retained.is_dir()
    assert {path.name for path in retained.iterdir()} == _BASE_FILES
    assert (retained.parent / "owners.jsonl").is_file()


def test_argument_validation_precedes_scratch_creation(tmp_path: Path) -> None:
    pageindex = tmp_path / "pageindex"
    refs = _stored_corpus(pageindex)
    recipe = GenerationRecipe()
    generation = _generation(tmp_path, refs, recipe)

    with pytest.raises(TypeError, match="generation"):
        build_base_view(pageindex, refs, object(), recipe)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="generation_recipe"):
        build_base_view(
            pageindex, refs, generation, object()  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="search_view_recipe"):
        build_base_view(
            pageindex,
            refs,
            generation,
            recipe,
            object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="check_cancelled"):
        build_base_view(
            pageindex,
            refs,
            generation,
            recipe,
            check_cancelled=object(),  # type: ignore[arg-type]
        )
    assert not tuple(pageindex.glob(".piv3-base-build.*"))
