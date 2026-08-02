from __future__ import annotations

from collections import Counter, defaultdict
import gc
import hashlib
from pathlib import Path
import weakref

import pytest

from app.index.v2.canonical import canonical_hash
from app.index.v2.models import SegmentRecipe
from app.index.v2.object_store import StoredSegmentRef, put_segment
from app.index.v3.base_builder import build_base_view
import app.index.v3.delta_builder as delta_builder_module
from app.index.v3.delta_builder import build_delta_view
from app.index.v3.generation import build_logical_generation
from app.index.v3.layer_codec import PostingLayerReader
import app.index.v3.segment_projection as projection_module
from app.index.v3.models import (
    ChunkRef,
    CompactionPolicy,
    GenerationRecipe,
    SearchViewRecipe,
)
from app.index.v3.segment_projection import SegmentProjector
from app.index.v3.source_diff import SegmentChangeSet
from app.index.v3.view_store import (
    ViewStoreConflictError,
    load_view_documents,
    load_view_statistics,
)
from app.retrieval.tokenizer import tokenize


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

    recipe = SegmentRecipe().as_dict()
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


def _generation(
    tmp_path: Path,
    name: str,
    refs: tuple[StoredSegmentRef, ...],
    recipe: GenerationRecipe,
):
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
    return build_logical_generation(
        refs,
        proof,
        recipe,
        tmp_path / name,
    )


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
        added=tuple(new_keys - old_keys),
        changed=tuple(changed),
        deleted=tuple(old_keys - new_keys),
        unchanged=tuple((old_keys & new_keys) - changed),
    )


def _new_refs(
    changes: SegmentChangeSet,
    after: tuple[StoredSegmentRef, ...],
) -> tuple[StoredSegmentRef, ...]:
    dirty = set(changes.added) | set(changes.changed)
    return tuple(ref for ref in after if ref.doc_key in dirty)


def _initial_and_target(pageindex: Path):
    initial = (
        put_segment(
            pageindex,
            _segment(
                "note:alpha",
                (("migrate stable", (), "gone common"),),
                revision="a",
            ),
        ),
        put_segment(
            pageindex,
            _segment(
                "book:shared",
                (("", (), "unchanged"),),
                revision="a",
            ),
        ),
        put_segment(
            pageindex,
            _segment(
                "note:delete",
                (("delete", (), ""),),
                revision="a",
            ),
        ),
    )
    by_key = {ref.doc_key: ref for ref in initial}
    target = (
        put_segment(
            pageindex,
            _segment(
                "note:alpha",
                (("stable fresh", (), "migrate common"),),
                revision="b",
            ),
        ),
        by_key["book:shared"],
        put_segment(
            pageindex,
            _segment(
                "note:shared",
                (("added", (), ""),),
                revision="a",
            ),
        ),
        put_segment(
            pageindex,
            _segment("note:empty", (), revision="a"),
        ),
    )
    return initial, target


def test_delta_add_edit_delete_matches_clean_base_and_never_reads_parent_postings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    initial, target = _initial_and_target(pageindex)
    generation_recipe = GenerationRecipe()
    view_recipe = SearchViewRecipe()
    initial_generation = _generation(
        tmp_path, "initial-generation", initial, generation_recipe
    )
    base, parent = build_base_view(
        pageindex,
        initial,
        initial_generation,
        generation_recipe,
        view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    target_generation = _generation(
        tmp_path, "target-generation", target, generation_recipe
    )
    changes = _changes(initial, target)
    new_refs = _new_refs(changes, target)

    projected: list[str] = []
    control_reads: Counter[str] = Counter()
    live_segments: list[weakref.ReferenceType[TrackedDict]] = []
    peak_segments = 0

    class TrackedDict(dict):
        pass

    original_load_segment = projection_module.load_segment
    original_project = SegmentProjector.project_to_sink
    original_load_metadata = delta_builder_module.load_search_view_metadata
    original_load_documents = delta_builder_module.load_view_documents
    original_load_statistics = delta_builder_module.load_view_statistics

    def counted_metadata(*args, **kwargs):
        control_reads["metadata"] += 1
        return original_load_metadata(*args, **kwargs)

    def counted_documents(*args, **kwargs):
        control_reads["documents"] += 1
        return original_load_documents(*args, **kwargs)

    def counted_statistics(*args, **kwargs):
        control_reads["statistics"] += 1
        return original_load_statistics(*args, **kwargs)

    def tracked_load_segment(*args, **kwargs):
        nonlocal peak_segments
        gc.collect()
        assert not any(reference() is not None for reference in live_segments)
        value = TrackedDict(original_load_segment(*args, **kwargs))
        live_segments.append(weakref.ref(value))
        peak_segments = max(
            peak_segments,
            sum(reference() is not None for reference in live_segments),
        )
        return value

    def tracked_project(self, ref, consume_posting):
        projected.append(ref.doc_key)
        return original_project(self, ref, consume_posting)

    original_reader_init = PostingLayerReader.__init__
    original_read_at = PostingLayerReader._read_at
    original_audit = PostingLayerReader.audit
    parent_layer_reads: Counter[str] = Counter()
    parent_reader_flags: list[bool] = []

    def tracked_reader_init(self, *args, **kwargs):
        observer = kwargs.get("read_observer")

        def combined_observer(name, offset, size):
            parent_layer_reads[name] += size
            if observer is not None:
                observer(name, offset, size)

        kwargs["read_observer"] = combined_observer
        parent_reader_flags.append(kwargs.get("load_documents", True))
        original_reader_init(self, *args, **kwargs)

    def forbid_parent_postings(self, name, offset, size):
        if name == "postings.piv":
            raise AssertionError("dirty build must not read parent postings.piv")
        return original_read_at(self, name, offset, size)

    def forbid_audit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dirty build must not deep-audit parent layers")

    monkeypatch.setattr(projection_module, "load_segment", tracked_load_segment)
    monkeypatch.setattr(SegmentProjector, "project_to_sink", tracked_project)
    monkeypatch.setattr(
        delta_builder_module, "load_search_view_metadata", counted_metadata
    )
    monkeypatch.setattr(
        delta_builder_module, "load_view_documents", counted_documents
    )
    monkeypatch.setattr(
        delta_builder_module, "load_view_statistics", counted_statistics
    )
    monkeypatch.setattr(PostingLayerReader, "__init__", tracked_reader_init)
    monkeypatch.setattr(PostingLayerReader, "_read_at", forbid_parent_postings)
    monkeypatch.setattr(PostingLayerReader, "audit", forbid_audit)

    result = build_delta_view(
        pageindex,
        parent,
        target_generation,
        generation_recipe,
        changes,
        reversed(new_refs),
        view_recipe,
        CompactionPolicy(max_delta_layers=1),
        max_run_bytes=1,
        merge_fan_in=2,
    )

    gc.collect()
    assert peak_segments <= 1
    assert not any(reference() is not None for reference in live_segments)
    assert sorted(projected) == sorted(ref.doc_key for ref in new_refs)
    assert control_reads == Counter(
        {"metadata": 1, "documents": 1, "statistics": 1}
    )
    assert result.view.delta_ids == (result.delta.delta_id,)
    assert result.view.base_id == base.base_id
    assert result.work.old_summaries_loaded == 2
    assert result.work.old_segments_loaded == 0
    assert result.work.new_segments_loaded == 3
    assert result.work.new_summaries_built == 3
    assert result.work.segments_loaded_peak == 1
    assert result.work.base_posting_bytes_read == 0
    assert result.work.touched_tokens == 7
    assert result.work.parent_term_windows_read > 0
    assert parent_reader_flags == [False]
    assert parent_layer_reads["layer-documents.json"] == 0
    assert parent_layer_reads["postings.piv"] == 0
    assert parent_layer_reads["terms.sidx.json"] > 0
    assert parent_layer_reads["terms.jsonl"] > 0
    assert result.compaction.layer_limit_reached

    records = PostingLayerReader.lookup_terms
    # Restore reader hooks only for explicit post-build assertions.
    monkeypatch.setattr(PostingLayerReader, "__init__", original_reader_init)
    monkeypatch.setattr(PostingLayerReader, "_read_at", original_read_at)
    with PostingLayerReader(result.delta.layer, recipe=view_recipe) as reader:
        terms = records(reader, ["common", "gone", "migrate", "stable"])
    assert terms["common"] is not None
    assert terms["common"].delta == (0, 0, 0)
    assert terms["common"].has_postings
    assert terms["stable"] is not None
    assert terms["stable"].delta == (0, 0, 0)
    assert terms["gone"] is not None
    assert terms["gone"].delta == (-1, 0, -1)
    assert not terms["gone"].has_postings
    assert terms["migrate"] is not None
    assert terms["migrate"].delta == (0, -1, 1)

    owners = load_view_documents(result.view)
    target_by_key = {ref.doc_key: ref for ref in target}
    assert {owner.doc_key for owner in owners.values()} == set(target_by_key)
    assert {"book:shared", "note:shared"} <= {
        owner.doc_key for owner in owners.values()
    }
    for owner in owners.values():
        assert owner.segment_hash == target_by_key[owner.doc_key].segment_hash
    assert next(
        owner for owner in owners.values() if owner.doc_key == "book:shared"
    ).owner_layer_kind == "base"
    assert next(
        owner for owner in owners.values() if owner.doc_key == "note:alpha"
    ).owner_layer_id == result.delta.delta_id

    expected_rows = defaultdict(list)
    expected_metrics = {}
    projector = SegmentProjector(pageindex)
    for ref in target:
        projection = projector.project(ref)
        for row in projection.postings:
            expected_rows[row.token].append(row)
        for metric in projection.chunk_metrics:
            expected_metrics[
                (
                    projection.summary.doc_uid,
                    projection.summary.segment_hash,
                    metric.local_id,
                )
            ] = metric
    for token in expected_rows:
        expected_rows[token].sort()

    effective_rows = defaultdict(list)
    effective_metrics = {}
    for layer_id, layer in (
        (base.base_id, base.layer),
        (result.delta.delta_id, result.delta.layer),
    ):
        with PostingLayerReader(layer, recipe=view_recipe) as reader:
            for token in expected_rows:
                for row in reader.iter_token(token):
                    owner = owners.get(row.chunk_ref.doc_uid)
                    if (
                        owner is not None
                        and owner.owner_layer_id == layer_id
                        and owner.segment_hash == row.chunk_ref.segment_hash
                    ):
                        effective_rows[token].append(row)
            layer_refs = {
                ChunkRef(doc_uid, segment_hash, local_id): None
                for doc_uid, segment_hash, local_id in expected_metrics
                if owners[doc_uid].owner_layer_id == layer_id
            }
            for ref, metric in reader.get_chunk_metrics(layer_refs).items():
                effective_metrics[(ref.doc_uid, ref.segment_hash, ref.local_id)] = metric
    assert {
        token: sorted(rows) for token, rows in effective_rows.items()
    } == dict(expected_rows)
    assert effective_metrics == expected_metrics

    clean_base, clean_view = build_base_view(
        pageindex,
        target,
        target_generation,
        generation_recipe,
        view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    assert load_view_statistics(result.view) == clean_base.statistics
    assert load_view_statistics(result.view) == load_view_statistics(clean_view)

    monkeypatch.setattr(PostingLayerReader, "audit", original_audit)
    rebuilt = build_delta_view(
        pageindex,
        parent,
        target_generation,
        generation_recipe,
        changes,
        new_refs,
        view_recipe,
        CompactionPolicy(max_delta_layers=99, max_delta_bytes_numerator=1),
        max_run_bytes=1,
        merge_fan_in=2,
    )
    assert rebuilt.delta.delta_id == result.delta.delta_id
    assert rebuilt.view.view_id == result.view.view_id
    assert not rebuilt.compaction.layer_limit_reached


def test_repeated_replacements_a_to_b_to_c_to_delete(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    recipe = GenerationRecipe()
    view_recipe = SearchViewRecipe()
    key = "note:chain"
    ref_a = put_segment(
        pageindex,
        _segment(key, (("alpha", (), ""),), revision="a"),
    )
    generation_a = _generation(tmp_path, "generation-a", (ref_a,), recipe)
    _base, parent = build_base_view(
        pageindex,
        (ref_a,),
        generation_a,
        recipe,
        view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )

    previous = (ref_a,)
    for revision, token in (("b", "beta"), ("c", "gamma")):
        current = (
            put_segment(
                pageindex,
                _segment(key, ((token, (), ""),), revision=revision),
            ),
        )
        generation = _generation(
            tmp_path, f"generation-{revision}", current, recipe
        )
        changes = _changes(previous, current)
        result = build_delta_view(
            pageindex,
            parent,
            generation,
            recipe,
            changes,
            current,
            view_recipe,
            max_run_bytes=1,
            merge_fan_in=2,
        )
        assert result.work.old_segments_loaded == 0
        assert result.work.old_summaries_loaded == 1
        assert result.work.new_segments_loaded == 1
        parent = result.view
        previous = current

    empty: tuple[StoredSegmentRef, ...] = ()
    generation_empty = _generation(
        tmp_path, "generation-empty", empty, recipe
    )
    delete_result = build_delta_view(
        pageindex,
        parent,
        generation_empty,
        recipe,
        _changes(previous, empty),
        (),
        view_recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    assert len(delete_result.view.delta_ids) == 3
    assert load_view_documents(delete_result.view) == {}
    assert load_view_statistics(delete_result.view).as_dict() == {
        "documents": 0,
        "total_chunks": 0,
        "token_count": 0,
        "title_length_sum": 0,
        "breadcrumb_length_sum": 0,
        "body_length_sum": 0,
        "posting_count": 0,
    }
    assert delete_result.delta.layer.document_count == 0
    assert delete_result.work.new_segments_loaded == 0
    assert delete_result.work.segments_loaded_peak == 0
    assert delete_result.work.old_segments_loaded == 0


def test_view_conflict_retains_candidate_and_published_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    initial, target = _initial_and_target(pageindex)
    recipe = GenerationRecipe()
    initial_generation = _generation(
        tmp_path, "generation-initial", initial, recipe
    )
    _base, parent = build_base_view(
        pageindex,
        initial,
        initial_generation,
        recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    target_generation = _generation(
        tmp_path, "generation-target", target, recipe
    )
    changes = _changes(initial, target)
    retained: Path | None = None

    def conflict(_pageindex_dir: Path, receipt):
        nonlocal retained
        retained = receipt.root
        raise ViewStoreConflictError("injected View identity conflict")

    monkeypatch.setattr(delta_builder_module, "finalize_search_view", conflict)
    with pytest.raises(ViewStoreConflictError, match="injected"):
        build_delta_view(
            pageindex,
            parent,
            target_generation,
            recipe,
            changes,
            _new_refs(changes, target),
            max_run_bytes=1,
            merge_fan_in=2,
        )

    assert retained is not None and retained.is_dir()
    assert {path.name for path in retained.iterdir()} == {
        "documents.json",
        "manifest.json",
        "statistics.json",
    }
    scratch = retained.parent
    assert scratch.name.startswith(".piv3-delta-build.")
    finalized_deltas = tuple(
        (pageindex / "objects" / "search" / "deltas").iterdir()
    )
    assert len(finalized_deltas) == 1
    assert finalized_deltas[0].is_dir()


@pytest.mark.parametrize(
    "cancel_phase", ("owner_rewrite", "before_view_finalize")
)
def test_cancellation_after_delta_publication_leaves_only_orphan_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_phase: str,
) -> None:
    pageindex = tmp_path / cancel_phase / "pageindex"
    initial, target = _initial_and_target(pageindex)
    recipe = GenerationRecipe()
    initial_generation = _generation(
        tmp_path / cancel_phase,
        "generation-initial",
        initial,
        recipe,
    )
    _base, parent = build_base_view(
        pageindex,
        initial,
        initial_generation,
        recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    target_generation = _generation(
        tmp_path / cancel_phase,
        "generation-target",
        target,
        recipe,
    )
    changes = _changes(initial, target)
    original_finalize_delta = delta_builder_module.finalize_delta_object
    original_write_view = delta_builder_module.write_search_view_candidate
    published_deltas: list[Path] = []
    delta_published = False
    view_candidate_written = False
    post_publish_checks = 0

    def finalize_delta_then_arm(pageindex_dir: Path, receipt):
        nonlocal delta_published
        result = original_finalize_delta(pageindex_dir, receipt)
        published_deltas.append(result.root)
        delta_published = True
        return result

    def write_view_then_arm(*args, **kwargs):
        nonlocal view_candidate_written
        result = original_write_view(*args, **kwargs)
        view_candidate_written = True
        return result

    def cancel() -> None:
        nonlocal post_publish_checks
        if not delta_published:
            return
        post_publish_checks += 1
        if cancel_phase == "owner_rewrite" and post_publish_checks == 3:
            raise RuntimeError("cancel during owner rewrite")
        if cancel_phase == "before_view_finalize" and view_candidate_written:
            raise RuntimeError("cancel before View finalize")

    monkeypatch.setattr(
        delta_builder_module, "finalize_delta_object", finalize_delta_then_arm
    )
    monkeypatch.setattr(
        delta_builder_module, "write_search_view_candidate", write_view_then_arm
    )
    views_before = set(parent.root.parent.iterdir())

    with pytest.raises(RuntimeError, match="cancel"):
        build_delta_view(
            pageindex,
            parent,
            target_generation,
            recipe,
            changes,
            _new_refs(changes, target),
            max_run_bytes=1,
            merge_fan_in=2,
            check_cancelled=cancel,
        )

    assert delta_published
    assert len(published_deltas) == 1
    assert published_deltas[0].is_dir()
    assert set(parent.root.parent.iterdir()) == views_before
    assert parent.root.is_dir()
    assert not tuple(pageindex.glob(".piv3-delta-build.*"))


def test_successful_view_publication_has_no_late_cancellation_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    initial, target = _initial_and_target(pageindex)
    recipe = GenerationRecipe()
    initial_generation = _generation(
        tmp_path, "generation-initial", initial, recipe
    )
    _base, parent = build_base_view(
        pageindex,
        initial,
        initial_generation,
        recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    target_generation = _generation(
        tmp_path, "generation-target", target, recipe
    )
    changes = _changes(initial, target)
    published = False
    original_finalize = delta_builder_module.finalize_search_view

    def finalize_then_arm(pageindex_dir: Path, receipt):
        nonlocal published
        result = original_finalize(pageindex_dir, receipt)
        published = True
        return result

    def cancel() -> None:
        if published:
            raise RuntimeError("late cancellation after published View")

    monkeypatch.setattr(
        delta_builder_module, "finalize_search_view", finalize_then_arm
    )
    result = build_delta_view(
        pageindex,
        parent,
        target_generation,
        recipe,
        changes,
        _new_refs(changes, target),
        max_run_bytes=1,
        merge_fan_in=2,
        check_cancelled=cancel,
    )

    assert published
    assert result.view.root.is_dir()
    assert not tuple(pageindex.glob(".piv3-delta-build.*"))

def test_noop_rejected_before_scratch_and_cancellation_cleans_private_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    initial, target = _initial_and_target(pageindex)
    recipe = GenerationRecipe()
    generation = _generation(tmp_path, "generation-initial", initial, recipe)
    _base, parent = build_base_view(
        pageindex,
        initial,
        generation,
        recipe,
        max_run_bytes=1,
        merge_fan_in=2,
    )
    noop = _changes(initial, initial)
    with pytest.raises(ValueError, match="at least one changed"):
        build_delta_view(
            pageindex, parent, generation, recipe, noop, ()
        )
    assert not tuple(pageindex.glob(".piv3-delta-build.*"))

    target_generation = _generation(
        tmp_path, "generation-target", target, recipe
    )
    changes = _changes(initial, target)
    armed = False
    original = SegmentProjector.project_to_sink

    def arm_after_projection(self, ref, consume_posting):
        nonlocal armed
        result = original(self, ref, consume_posting)
        armed = True
        return result

    def cancel() -> None:
        if armed:
            raise RuntimeError("cancel after projection")

    monkeypatch.setattr(
        SegmentProjector, "project_to_sink", arm_after_projection
    )
    with pytest.raises(RuntimeError, match="cancel after projection"):
        build_delta_view(
            pageindex,
            parent,
            target_generation,
            recipe,
            changes,
            _new_refs(changes, target),
            max_run_bytes=1,
            merge_fan_in=2,
            check_cancelled=cancel,
        )
    assert not tuple(pageindex.glob(".piv3-delta-build.*"))
    assert parent.root.is_dir()