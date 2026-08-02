from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from app.index.v2.artifacts import ArtifactRef
from app.index.v3.generation import LogicalGenerationReceipt
from app.index.v3.layer_codec import LayerDocument
from app.index.v3.layer_runs import build_sorted_layer
from app.index.v3.models import ChunkRef, SearchPosting, SearchViewRecipe, make_doc_uid
from app.index.v3.segment_projection import ChunkMetric
from app.index.v3.statistics import CorpusTotals
import app.index.v3.view_store as view_store_module
from app.index.v3.view_store import (
    BaseObjectReceipt,
    SearchViewReceipt,
    ViewDocumentOwner,
    ViewStoreConflictError,
    ViewStoreError,
    finalize_base_object,
    finalize_search_view,
    load_base_object,
    load_base_object_metadata,
    load_search_view,
    load_view_documents,
    load_view_statistics,
    write_base_candidate,
    write_search_view_candidate,
)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _generation(
    root: Path,
    *,
    document_count: int,
    revision: str = "",
) -> LogicalGenerationReceipt:
    suffix = f":{revision}" if revision else ""
    return LogicalGenerationReceipt(
        candidate_dir=root / "logical-generation",
        generation_id=_digest(f"generation:{document_count}{suffix}"),
        generation_recipe_hash=_digest("generation-recipe"),
        manifest_ref=ArtifactRef(
            "manifest.json",
            _digest(f"generation-manifest:{document_count}{suffix}"),
            313,
            document_count,
        ),
        input_proof_ref=ArtifactRef(
            "input-proof.json",
            _digest(f"input-proof:{document_count}{suffix}"),
            211,
            document_count,
        ),
        document_count=document_count,
    )


def _single_layer(root: Path, recipe: SearchViewRecipe):
    doc_key = "note:alpha"
    doc_uid = make_doc_uid(doc_key)
    segment_hash = _digest("segment:alpha")
    metric = ChunkMetric(0, 2, 1, 3)
    document = LayerDocument(doc_key, doc_uid, segment_hash, (metric,))
    postings = (
        SearchPosting("alpha", ChunkRef(doc_uid, segment_hash, 0), 1, 1, 0),
        SearchPosting("body", ChunkRef(doc_uid, segment_hash, 0), 0, 0, 2),
    )
    layer = build_sorted_layer(
        root,
        documents=(document,),
        postings=postings,
        layer_kind="base",
        recipe=recipe,
        max_run_bytes=96,
        merge_fan_in=2,
    )
    totals = CorpusTotals(
        documents=1,
        total_chunks=1,
        token_count=2,
        title_length_sum=2,
        breadcrumb_length_sum=1,
        body_length_sum=3,
        posting_count=2,
    )
    return layer, totals, doc_key, doc_uid, segment_hash


def _build_pair(tmp_path: Path, suffix: str = "one"):
    recipe = SearchViewRecipe()
    generation = _generation(tmp_path, document_count=1)
    base_root = tmp_path / f"base-{suffix}"
    layer, totals, doc_key, doc_uid, segment_hash = _single_layer(
        base_root, recipe
    )
    base = write_base_candidate(
        base_root,
        generation=generation,
        recipe=recipe,
        layer=layer,
        statistics=totals,
    )
    owner = ViewDocumentOwner(
        doc_key=doc_key,
        segment_hash=segment_hash,
        summary_sha256=_digest("summary:alpha"),
        summary_bytes=987,
        owner_layer_kind="base",
        owner_layer_id=base.base_id,
        doc_ordinal=0,
    )
    view = write_search_view_candidate(
        tmp_path / f"view-{suffix}",
        generation=generation,
        recipe=recipe,
        base=base,
        statistics=totals,
        documents=((doc_uid, owner),),
    )
    return generation, recipe, totals, base, view, doc_uid, owner


def _build_empty_pair(tmp_path: Path, suffix: str = "empty"):
    recipe = SearchViewRecipe()
    generation = _generation(tmp_path, document_count=0)
    base_root = tmp_path / f"base-{suffix}"
    layer = build_sorted_layer(
        base_root,
        documents=(),
        postings=(),
        layer_kind="base",
        recipe=recipe,
    )
    totals = CorpusTotals(0, 0, 0, 0, 0, 0, 0)
    base = write_base_candidate(
        base_root,
        generation=generation,
        recipe=recipe,
        layer=layer,
        statistics=totals,
    )
    view = write_search_view_candidate(
        tmp_path / f"view-{suffix}",
        generation=generation,
        recipe=recipe,
        base=base,
        statistics=totals,
        documents=(),
    )
    return generation, recipe, totals, base, view


def test_writers_freeze_exact_canonical_base_and_view_contracts(
    tmp_path: Path,
) -> None:
    generation, recipe, totals, base, view, doc_uid, owner = _build_pair(
        tmp_path
    )

    base_manifest_raw = (base.root / "manifest.json").read_bytes()
    base_manifest = json.loads(base_manifest_raw)
    assert base_manifest_raw == _canonical(base_manifest)
    assert set(base_manifest) == {
        "artifact_kind",
        "schema_version",
        "base_id",
        "generation",
        "generation_manifest_sha256",
        "search_view_recipe",
        "search_view_recipe_hash",
        "layer",
        "statistics",
    }
    assert base_manifest["artifact_kind"] == "search_base"
    assert base_manifest["base_id"] == base.base_id
    assert base_manifest["generation"] == generation.generation_id
    assert base_manifest["search_view_recipe"] == recipe.as_dict()
    assert base_manifest["layer"] == base.layer.as_dict()
    assert base_manifest["statistics"] == totals.as_dict()

    statistics_raw = (view.root / "statistics.json").read_bytes()
    assert json.loads(statistics_raw) == totals.as_dict()
    assert statistics_raw == _canonical(totals.as_dict())
    assert set(json.loads(statistics_raw)) == set(totals.as_dict())

    documents_raw = (view.root / "documents.json").read_bytes()
    assert documents_raw == _canonical({doc_uid: owner.as_dict()})
    assert json.loads(documents_raw) == {doc_uid: owner.as_dict()}

    view_manifest_raw = (view.root / "manifest.json").read_bytes()
    view_manifest = json.loads(view_manifest_raw)
    assert view_manifest_raw == _canonical(view_manifest)
    assert set(view_manifest) == {
        "artifact_kind",
        "schema_version",
        "view_id",
        "generation",
        "generation_manifest_sha256",
        "search_view_recipe",
        "search_view_recipe_hash",
        "base_id",
        "delta_ids",
        "statistics_sha256",
        "documents_sha256",
        "artifacts",
    }
    assert view_manifest["artifact_kind"] == "search_view"
    assert view_manifest["delta_ids"] == []
    assert view_manifest["statistics_sha256"] == view.statistics_ref.sha256
    assert view_manifest["documents_sha256"] == view.documents_ref.sha256
    assert not hasattr(view, "documents")
    assert len(base.base_id) == len(view.view_id) == 64


def test_finalize_and_load_deeply_authenticate_both_objects(tmp_path: Path) -> None:
    _generation_receipt, _recipe, totals, base, view, doc_uid, owner = _build_pair(
        tmp_path
    )
    store = tmp_path / "pageindex"

    finalized_base = finalize_base_object(store, base)
    finalized_view = finalize_search_view(store, view)

    assert finalized_base.root == store / "objects" / "search" / "bases" / base.base_id
    assert finalized_base.layer.root == finalized_base.root
    assert finalized_view.root == store / "views" / view.view_id
    assert load_base_object(store, base.base_id).attestation_dict() == (
        finalized_base.attestation_dict()
    )
    assert load_search_view(store, view.view_id).attestation_dict() == (
        finalized_view.attestation_dict()
    )
    assert load_view_documents(finalized_view) == {doc_uid: owner}
    assert json.loads((finalized_view.root / "statistics.json").read_bytes()) == (
        totals.as_dict()
    )


def test_empty_base_and_view_are_valid_content_addressed_objects(
    tmp_path: Path,
) -> None:
    _generation_receipt, _recipe, _totals, base, view = _build_empty_pair(
        tmp_path
    )
    store = tmp_path / "pageindex"
    base = finalize_base_object(store, base)
    view = finalize_search_view(store, view)
    assert load_base_object(store, base.base_id).statistics.documents == 0
    assert load_search_view(store, view.view_id).documents_ref.records == 0
    assert load_view_documents(view) == {}


def test_new_candidate_sealing_and_first_finalize_do_not_reaudit_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = SearchViewRecipe()
    generation = _generation(tmp_path, document_count=1)
    base_root = tmp_path / "base"
    layer, totals, *_rest = _single_layer(base_root, recipe)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("new candidate must not perform a second layer audit")

    monkeypatch.setattr(view_store_module.PostingLayerReader, "audit", forbidden)
    base = write_base_candidate(
        base_root,
        generation=generation,
        recipe=recipe,
        layer=layer,
        statistics=totals,
    )
    finalized = finalize_base_object(tmp_path / "store", base)
    assert finalized.root.is_dir()


def test_metadata_and_statistics_loaders_touch_only_control_plane_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generation_receipt, _recipe, totals, base, view, *_rest = _build_pair(
        tmp_path
    )
    store = tmp_path / "store"
    base = finalize_base_object(store, base)
    view = finalize_search_view(store, view)
    original_open = view_store_module._open_regular
    opened: list[str] = []

    def control_plane_only(root: Path, relative_path: str):
        opened.append(relative_path)
        if relative_path in view_store_module._LAYER_PATHS:
            raise AssertionError("metadata loader opened a posting-layer artifact")
        return original_open(root, relative_path)

    def forbidden_audit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("metadata loader invoked the deep layer audit")

    monkeypatch.setattr(view_store_module, "_open_regular", control_plane_only)
    monkeypatch.setattr(
        view_store_module.PostingLayerReader,
        "audit",
        forbidden_audit,
    )

    metadata = load_base_object_metadata(store, base.base_id)
    assert metadata.attestation_dict() == base.attestation_dict()
    assert opened == ["manifest.json"]

    opened.clear()
    assert load_view_statistics(view) == totals
    assert opened == ["statistics.json"]


def test_incremental_view_appends_one_delta_over_an_older_base(
    tmp_path: Path,
) -> None:
    _old_generation, recipe, totals, base, parent, doc_uid, owner = _build_pair(
        tmp_path
    )
    store = tmp_path / "store"
    base = finalize_base_object(store, base)
    parent = finalize_search_view(store, parent)
    target = _generation(
        tmp_path / "target",
        document_count=1,
        revision="next",
    )
    delta_id = _digest("delta:next")

    incremental = write_search_view_candidate(
        tmp_path / "view-incremental",
        generation=target,
        recipe=recipe,
        base=base,
        statistics=totals,
        documents=((doc_uid, owner),),
        delta_ids=parent.delta_ids + (delta_id,),
        parent=parent,
    )

    assert base.generation != target.generation_id
    assert incremental.generation == target.generation_id
    assert incremental.base_id == base.base_id
    assert incremental.delta_ids == (delta_id,)


def test_incremental_view_rejects_missing_or_invalid_parent_and_chain(
    tmp_path: Path,
) -> None:
    old_generation, recipe, totals, base, parent, doc_uid, owner = _build_pair(
        tmp_path
    )
    store = tmp_path / "store"
    base = finalize_base_object(store, base)
    parent = finalize_search_view(store, parent)
    target = _generation(
        tmp_path / "target",
        document_count=1,
        revision="next",
    )
    first_delta = _digest("delta:first")
    second_delta = _digest("delta:second")

    with pytest.raises(ViewStoreError, match="requires a parent"):
        write_search_view_candidate(
            tmp_path / "missing-parent",
            generation=target,
            recipe=recipe,
            base=base,
            statistics=totals,
            documents=((doc_uid, owner),),
            delta_ids=(first_delta,),
        )

    with pytest.raises(ViewStoreError, match="append exactly one"):
        write_search_view_candidate(
            tmp_path / "chain-jump",
            generation=target,
            recipe=recipe,
            base=base,
            statistics=totals,
            documents=((doc_uid, owner),),
            delta_ids=(first_delta, second_delta),
            parent=parent,
        )

    forged_parent = replace(parent, root=tmp_path / "not-a-finalized-view")
    with pytest.raises(ViewStoreError, match="finalized local View"):
        write_search_view_candidate(
            tmp_path / "forged-parent",
            generation=target,
            recipe=recipe,
            base=base,
            statistics=totals,
            documents=((doc_uid, owner),),
            delta_ids=(first_delta,),
            parent=forged_parent,
        )

    with pytest.raises(ViewStoreError, match="must advance"):
        write_search_view_candidate(
            tmp_path / "same-generation",
            generation=old_generation,
            recipe=recipe,
            base=base,
            statistics=totals,
            documents=((doc_uid, owner),),
            delta_ids=(first_delta,),
            parent=parent,
        )

    _empty_generation, _empty_recipe, _empty_totals, wrong_base, _wrong_view = (
        _build_empty_pair(tmp_path, "wrong-base")
    )
    with pytest.raises(ViewStoreError, match="parent and Base IDs differ"):
        write_search_view_candidate(
            tmp_path / "wrong-base",
            generation=target,
            recipe=recipe,
            base=wrong_base,
            statistics=totals,
            documents=((doc_uid, owner),),
            delta_ids=(first_delta,),
            parent=parent,
        )

    first = write_search_view_candidate(
        tmp_path / "first-incremental",
        generation=target,
        recipe=recipe,
        base=base,
        statistics=totals,
        documents=((doc_uid, owner),),
        delta_ids=(first_delta,),
        parent=parent,
    )
    first = finalize_search_view(store, first)
    later = _generation(
        tmp_path / "later",
        document_count=1,
        revision="later",
    )
    with pytest.raises(ViewStoreError, match="append exactly one"):
        write_search_view_candidate(
            tmp_path / "chain-reordered",
            generation=later,
            recipe=recipe,
            base=base,
            statistics=totals,
            documents=((doc_uid, owner),),
            delta_ids=(second_delta, first_delta),
            parent=first,
        )


def test_receipts_reject_artifact_rebinding_without_changing_identity(
    tmp_path: Path,
) -> None:
    _generation_receipt, _recipe, _totals, base, view, *_rest = _build_pair(
        tmp_path
    )
    rebound_statistics = ArtifactRef(
        "statistics.json",
        _digest("different statistics"),
        view.statistics_ref.byte_size,
        1,
    )
    with pytest.raises(ValueError, match="view_id"):
        replace(view, statistics_ref=rebound_statistics)

    rebound_layer = replace(
        base.layer,
        terms=ArtifactRef(
            "terms.jsonl",
            _digest("different terms"),
            base.layer.terms.byte_size,
            base.layer.terms.records,
        ),
    )
    with pytest.raises(ValueError, match="base_id"):
        replace(base, layer=rebound_layer)


def test_load_rejects_manifest_artifact_rebinding_and_recipe_tampering(
    tmp_path: Path,
) -> None:
    _generation_receipt, _recipe, _totals, base, view, *_rest = _build_pair(
        tmp_path
    )
    store = tmp_path / "store"
    base = finalize_base_object(store, base)
    view = finalize_search_view(store, view)

    view_manifest_path = view.root / "manifest.json"
    view_manifest = json.loads(view_manifest_path.read_bytes())
    view_manifest["artifacts"]["statistics"]["sha256"] = _digest("rebound")
    view_manifest_path.write_bytes(_canonical(view_manifest))
    with pytest.raises(ViewStoreError, match="rebound"):
        load_search_view(store, view.view_id)

    base_manifest_path = base.root / "manifest.json"
    base_manifest = json.loads(base_manifest_path.read_bytes())
    base_manifest["search_view_recipe"]["posting_codec_version"] = "wrong"
    base_manifest_path.write_bytes(_canonical(base_manifest))
    with pytest.raises(ViewStoreError, match="search_view_recipe"):
        load_base_object(store, base.base_id)


@pytest.mark.parametrize(
    "relative_path",
    ["postings.piv", "chunks.pcv", "terms.jsonl", "layer-documents.json"],
)
def test_load_base_rejects_each_tampered_large_or_routing_artifact(
    tmp_path: Path,
    relative_path: str,
) -> None:
    _generation_receipt, _recipe, _totals, base, _view, *_rest = _build_pair(
        tmp_path
    )
    store = tmp_path / "store"
    base = finalize_base_object(store, base)
    path = base.root / relative_path
    raw = path.read_bytes()
    path.write_bytes(raw + b"x")
    with pytest.raises(ViewStoreError, match="audit"):
        load_base_object(store, base.base_id)


def test_load_rejects_noncanonical_statistics_and_extra_files(tmp_path: Path) -> None:
    _generation_receipt, _recipe, totals, base, view, *_rest = _build_pair(
        tmp_path
    )
    store = tmp_path / "store"
    finalize_base_object(store, base)
    view = finalize_search_view(store, view)
    statistics = view.root / "statistics.json"
    statistics.write_text(json.dumps(totals.as_dict(), indent=2), encoding="utf-8")
    with pytest.raises(ViewStoreError, match="byte size|digest|noncanonical"):
        load_search_view(store, view.view_id)

    statistics.write_bytes(_canonical(totals.as_dict()))
    (view.root / "unexpected.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ViewStoreError, match="file set"):
        load_search_view(store, view.view_id)


def test_writer_rejects_owner_key_route_and_count_drift(tmp_path: Path) -> None:
    generation, recipe, totals, base, _view, _doc_uid, owner = _build_pair(
        tmp_path, "source"
    )
    wrong_uid = _digest("wrong-owner-key")
    with pytest.raises(ViewStoreError, match="does not match doc_key"):
        write_search_view_candidate(
            tmp_path / "wrong-key",
            generation=generation,
            recipe=recipe,
            base=base,
            statistics=totals,
            documents=((wrong_uid, owner),),
        )
    assert not (tmp_path / "wrong-key").exists()

    wrong_route = replace(owner, owner_layer_id=_digest("wrong-layer"))
    with pytest.raises(ViewStoreError, match="base owner_layer_id"):
        write_search_view_candidate(
            tmp_path / "wrong-route",
            generation=generation,
            recipe=recipe,
            base=base,
            statistics=totals,
            documents=((make_doc_uid(owner.doc_key), wrong_route),),
        )
    assert not (tmp_path / "wrong-route").exists()

    with pytest.raises(ViewStoreError, match="counts differ"):
        write_search_view_candidate(
            tmp_path / "missing-owner",
            generation=generation,
            recipe=recipe,
            base=base,
            statistics=totals,
            documents=(),
        )
    assert not (tmp_path / "missing-owner").exists()


def test_concurrent_identical_finalization_reuses_deeply_validated_objects(
    tmp_path: Path,
) -> None:
    *_, first_base, first_view, _uid, _owner = _build_pair(tmp_path, "first")
    *_, second_base, second_view, _uid2, _owner2 = _build_pair(tmp_path, "second")
    assert first_base.base_id == second_base.base_id
    assert first_view.view_id == second_view.view_id
    store = tmp_path / "store"

    finalized_base = finalize_base_object(store, first_base)
    finalized_view = finalize_search_view(store, first_view)
    reused_base = finalize_base_object(store, second_base)
    reused_view = finalize_search_view(store, second_view)

    assert reused_base.root == finalized_base.root
    assert reused_view.root == finalized_view.root
    assert not second_base.root.exists()
    assert not second_view.root.exists()


def test_conflicting_existing_object_retains_candidate_for_diagnosis(
    tmp_path: Path,
) -> None:
    *_, first_base, first_view, _uid, _owner = _build_pair(tmp_path, "first")
    *_, second_base, second_view, _uid2, _owner2 = _build_pair(tmp_path, "second")
    store = tmp_path / "store"
    finalized_base = finalize_base_object(store, first_base)
    finalized_view = finalize_search_view(store, first_view)
    (finalized_base.root / "terms.jsonl").write_bytes(b"corrupt")
    (finalized_view.root / "statistics.json").write_bytes(b"corrupt")

    with pytest.raises(ViewStoreConflictError) as base_error:
        finalize_base_object(store, second_base)
    assert base_error.value.candidate_retained is True
    assert second_base.root.is_dir()

    with pytest.raises(ViewStoreConflictError) as view_error:
        finalize_search_view(store, second_view)
    assert view_error.value.candidate_retained is True
    assert second_view.root.is_dir()


def test_target_appearing_during_no_replace_is_reused_only_after_deep_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_, candidate, _view, _uid, _owner = _build_pair(tmp_path, "candidate")
    *_, rival, _rival_view, _uid2, _owner2 = _build_pair(tmp_path, "rival")
    store = tmp_path / "store"

    def race(_source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(rival.root, target)
        raise FileExistsError(target)

    monkeypatch.setattr(view_store_module, "_rename_no_replace", race)
    finalized = finalize_base_object(store, candidate)
    assert finalized.root == store / "objects" / "search" / "bases" / candidate.base_id
    assert not candidate.root.exists()
    assert rival.root.exists()


def test_short_ids_bool_schema_and_unsafe_extra_owner_fields_fail_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises((ValueError, ViewStoreError), match="64-character"):
        load_base_object(tmp_path, "abcd")
    with pytest.raises((ValueError, ViewStoreError), match="schema_version"):
        SearchViewReceipt(
            root=tmp_path,
            view_id=_digest("view"),
            generation=_digest("generation"),
            generation_manifest_sha256=_digest("manifest"),
            search_view_recipe_hash=_digest("recipe"),
            base_id=_digest("base"),
            delta_ids=(),
            manifest_ref=ArtifactRef("manifest.json", _digest("vm"), 1, 1),
            statistics_ref=ArtifactRef("statistics.json", _digest("s"), 1, 1),
            documents_ref=ArtifactRef("documents.json", _digest("d"), 2, 0),
            schema_version=True,
        )
    owner = {
        "doc_key": "note:a",
        "segment_hash": _digest("segment"),
        "summary_sha256": _digest("summary"),
        "summary_bytes": 1,
        "owner_layer_kind": "base",
        "owner_layer_id": _digest("base"),
        "doc_ordinal": 0,
        "extra": True,
    }
    with pytest.raises(ViewStoreError, match="exactly"):
        ViewDocumentOwner.from_dict(owner)


def test_load_and_finalize_reject_symlinked_store_ancestors(
    tmp_path: Path,
) -> None:
    *_, _base, published_view, _uid, _owner = _build_pair(tmp_path, "published")
    outside_store = tmp_path / "outside-store"
    published_view = finalize_search_view(outside_store, published_view)
    linked_store = tmp_path / "linked-store"
    linked_store.mkdir()
    linked_views = linked_store / "views"
    try:
        linked_views.symlink_to(outside_store / "views", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(ViewStoreError, match="symlink or junction"):
        load_search_view(linked_store, published_view.view_id)

    *_, _base2, candidate, _uid2, _owner2 = _build_pair(tmp_path, "candidate")
    blocked_store = tmp_path / "blocked-store"
    blocked_store.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (blocked_store / "views").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ViewStoreError, match="symlink or junction"):
        finalize_search_view(blocked_store, candidate)
    assert candidate.root.is_dir()
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction contract is Windows-only")
def test_finalize_rejects_windows_junction_destination_ancestor(
    tmp_path: Path,
) -> None:
    *_, _base, candidate, _uid, _owner = _build_pair(tmp_path, "junction")
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = store / "views"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"cannot create NTFS junction: {completed.stderr}")
    try:
        with pytest.raises(ViewStoreError, match="symlink or junction"):
            finalize_search_view(store, candidate)
        assert candidate.root.is_dir()
        assert list(outside.iterdir()) == []
    finally:
        os.rmdir(junction)


def test_destination_parent_identity_is_rechecked_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_, _base, candidate, _uid, _owner = _build_pair(tmp_path, "identity")
    store = tmp_path / "store"
    destination_parent = store / "views"
    destination_parent.mkdir(parents=True)
    original_assert = view_store_module._assert_directory_identity

    def changed(path: Path, identity: tuple[int, int]) -> None:
        if path == destination_parent:
            raise ViewStoreError("directory identity changed while operating")
        original_assert(path, identity)

    def rename_must_not_run(_source: Path, _target: Path) -> None:
        raise AssertionError("rename ran before destination-parent identity check")

    monkeypatch.setattr(view_store_module, "_assert_directory_identity", changed)
    monkeypatch.setattr(view_store_module, "_rename_no_replace", rename_must_not_run)
    with pytest.raises(ViewStoreError, match="identity changed"):
        finalize_search_view(store, candidate)
    assert candidate.root.is_dir()
