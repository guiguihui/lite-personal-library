from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from app.index.v2.artifacts import ArtifactRef
from app.index.v2.canonical import canonical_hash
from app.index.v3.delta_store import (
    DeltaStoreConflictError,
    DeltaStoreError,
    DocumentReplacement,
    StatisticsDelta,
    finalize_delta_object,
    load_delta_object,
    load_delta_object_metadata,
    write_delta_candidate,
)
import app.index.v3.delta_store as delta_store_module
from app.index.v3.generation import LogicalGenerationReceipt
from app.index.v3.layer_codec import (
    LayerDocument,
    PostingLayerReader,
    TokenContribution,
)
from app.index.v3.layer_runs import build_sorted_layer
from app.index.v3.models import (
    MAX_U64,
    LayerPosting,
    SearchViewRecipe,
    make_doc_uid,
)
from app.index.v3.segment_projection import ChunkMetric
from app.index.v3.statistics import CorpusTotals
from app.index.v3.view_store import SearchViewReceipt


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


def _artifact(path: str, seed: str, records: int) -> ArtifactRef:
    return ArtifactRef(path, _digest(seed), 97, records)


def _parent(
    root: Path,
    recipe: SearchViewRecipe,
    *,
    document_count: int = 0,
) -> SearchViewReceipt:
    generation = _digest(f"parent-generation:{document_count}")
    generation_manifest = _digest(f"parent-generation-manifest:{document_count}")
    recipe_hash = canonical_hash(recipe.as_dict())
    base_id = _digest(f"base:{document_count}")
    statistics_ref = _artifact("statistics.json", f"statistics:{document_count}", 1)
    documents_ref = _artifact("documents.json", f"documents:{document_count}", document_count)
    core = {
        "artifact_kind": "search_view",
        "schema_version": 1,
        "generation": generation,
        "generation_manifest_sha256": generation_manifest,
        "search_view_recipe_hash": recipe_hash,
        "base_id": base_id,
        "delta_ids": [],
        "statistics_sha256": statistics_ref.sha256,
        "documents_sha256": documents_ref.sha256,
    }
    view_id = canonical_hash(core)
    return SearchViewReceipt(
        root=root / "parents" / "views" / view_id,
        view_id=view_id,
        generation=generation,
        generation_manifest_sha256=generation_manifest,
        search_view_recipe_hash=recipe_hash,
        base_id=base_id,
        delta_ids=(),
        manifest_ref=_artifact("manifest.json", f"view-manifest:{document_count}", 1),
        statistics_ref=statistics_ref,
        documents_ref=documents_ref,
    )


def _generation(
    root: Path,
    *,
    document_count: int,
    revision: str = "target",
) -> LogicalGenerationReceipt:
    return LogicalGenerationReceipt(
        candidate_dir=root / f"generation-{revision}",
        generation_id=_digest(f"generation:{revision}:{document_count}"),
        generation_recipe_hash=_digest("generation-recipe"),
        manifest_ref=_artifact(
            "manifest.json",
            f"generation-manifest:{revision}:{document_count}",
            document_count,
        ),
        input_proof_ref=_artifact(
            "input-proof.json",
            f"generation-proof:{revision}:{document_count}",
            document_count,
        ),
        document_count=document_count,
    )


def _replacement(
    doc_key: str = "note:alpha",
    *,
    old: bool = False,
    new: bool = True,
    ordinal: int | None = 0,
    old_seed: str = "old-alpha",
    new_seed: str = "new-alpha",
) -> DocumentReplacement:
    return DocumentReplacement(
        doc_key=doc_key,
        doc_uid=make_doc_uid(doc_key),
        old_segment_hash=_digest(f"segment:{old_seed}") if old else None,
        old_summary_sha256=_digest(f"summary:{old_seed}") if old else None,
        old_summary_bytes=211 if old else None,
        new_segment_hash=_digest(f"segment:{new_seed}") if new else None,
        new_summary_sha256=_digest(f"summary:{new_seed}") if new else None,
        new_summary_bytes=313 if new else None,
        new_doc_ordinal=ordinal if new else None,
    )


def _build_add_parts(tmp_path: Path, suffix: str = "one"):
    recipe = SearchViewRecipe()
    parent = _parent(tmp_path, recipe, document_count=0)
    generation = _generation(tmp_path, document_count=1, revision="add")
    replacement = _replacement()
    assert replacement.new_segment_hash is not None
    root = tmp_path / f"delta-{suffix}"
    document = LayerDocument(
        doc_key=replacement.doc_key,
        doc_uid=replacement.doc_uid,
        segment_hash=replacement.new_segment_hash,
        chunk_metrics=(ChunkMetric(0, 1, 0, 1),),
    )
    layer = build_sorted_layer(
        root,
        documents=(document,),
        postings=(LayerPosting("alpha", 0, 0, 1, 0, 1),),
        token_contributions=(TokenContribution("alpha", 1, 1, 1),),
        layer_kind="delta",
        recipe=recipe,
        max_run_bytes=96,
        merge_fan_in=2,
    )
    statistics_delta = StatisticsDelta(1, 1, 1, 1, 0, 1, 1)
    return recipe, parent, generation, layer, statistics_delta, replacement


def _seal_add(tmp_path: Path, suffix: str = "one"):
    recipe, parent, generation, layer, statistics_delta, replacement = (
        _build_add_parts(tmp_path, suffix)
    )
    receipt = write_delta_candidate(
        layer.root,
        parent=parent,
        generation=generation,
        recipe=recipe,
        layer=layer,
        statistics_delta=statistics_delta,
        replacements=(replacement,),
    )
    return receipt, recipe, parent, generation, statistics_delta, replacement


def test_statistics_delta_signed_domain_and_apply_barriers() -> None:
    parent = CorpusTotals(1, 1, 1, 1, 0, 0, 1)
    remove_all = StatisticsDelta(-1, -1, -1, -1, 0, 0, -1)
    assert remove_all.apply(parent) == CorpusTotals(0, 0, 0, 0, 0, 0, 0)

    values = {name: 0 for name in StatisticsDelta.__dataclass_fields__}
    for field in values:
        with pytest.raises(ValueError):
            StatisticsDelta(**{**values, field: True})
        with pytest.raises(ValueError):
            StatisticsDelta(**{**values, field: MAX_U64 + 1})
        with pytest.raises(ValueError):
            StatisticsDelta(**{**values, field: -MAX_U64 - 1})

    with pytest.raises(DeltaStoreError, match="underflows or overflows"):
        StatisticsDelta(-2, 0, 0, 0, 0, 0, 0).apply(parent)

    maximum = CorpusTotals(MAX_U64, MAX_U64, 1, MAX_U64, 0, 0, 1)
    with pytest.raises(DeltaStoreError, match="underflows or overflows"):
        StatisticsDelta(1, 0, 0, 0, 0, 0, 0).apply(maximum)

    with pytest.raises(DeltaStoreError, match="invalid corpus totals"):
        StatisticsDelta(0, 0, -1, 0, 0, 0, 0).apply(parent)


def test_document_replacement_freezes_add_edit_delete_states() -> None:
    add = _replacement()
    edit = _replacement(old=True, new=True)
    delete = _replacement(old=True, new=False, ordinal=None)
    assert (add.operation, edit.operation, delete.operation) == (
        "add",
        "edit",
        "delete",
    )
    assert DocumentReplacement.from_dict(add.as_dict()) == add

    with pytest.raises(ValueError, match="doc_uid"):
        replace(add, doc_uid=_digest("wrong-document"))
    with pytest.raises(ValueError, match="old fields"):
        replace(add, old_segment_hash=_digest("partial-old"))
    with pytest.raises(ValueError, match="new fields"):
        replace(add, new_summary_sha256=None)
    with pytest.raises(ValueError, match="both sides null"):
        DocumentReplacement(
            "note:none",
            make_doc_uid("note:none"),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    with pytest.raises(ValueError, match="change segment_hash"):
        replace(edit, new_segment_hash=edit.old_segment_hash)
    with pytest.raises(ValueError, match="change summary_sha256"):
        replace(edit, new_summary_sha256=edit.old_summary_sha256)
    with pytest.raises(ValueError, match="u64"):
        replace(add, new_doc_ordinal=True)


def test_replacement_sequence_rejects_sort_ordinal_segment_and_document_drift(
    tmp_path: Path,
) -> None:
    first = _replacement("note:first", new_seed="first")
    second = _replacement("note:second", ordinal=1, new_seed="second")
    ordered = tuple(sorted((first, second), key=lambda item: item.doc_uid.encode("utf-8")))
    ordered = tuple(replace(item, new_doc_ordinal=index) for index, item in enumerate(ordered))

    reversed_order = tuple(
        replace(item, new_doc_ordinal=index)
        for index, item in enumerate(reversed(ordered))
    )
    with pytest.raises(DeltaStoreError, match="sorted uniquely"):
        delta_store_module._replacements(reversed_order)
    with pytest.raises(DeltaStoreError, match="ordinals must be compact"):
        delta_store_module._replacements((replace(ordered[0], new_doc_ordinal=1),))
    with pytest.raises(DeltaStoreError, match="duplicate new"):
        delta_store_module._replacements(
            (ordered[0], replace(ordered[1], new_segment_hash=ordered[0].new_segment_hash))
        )

    old_first = _replacement("note:old-first", old=True, new=False, ordinal=None)
    old_second = _replacement("note:old-second", old=True, new=False, ordinal=None)
    old_ordered = tuple(
        sorted((old_first, old_second), key=lambda item: item.doc_uid.encode("utf-8"))
    )
    with pytest.raises(DeltaStoreError, match="duplicate old"):
        delta_store_module._replacements(
            (
                old_ordered[0],
                replace(
                    old_ordered[1],
                    old_segment_hash=old_ordered[0].old_segment_hash,
                ),
            )
        )

    recipe, parent, generation, layer, _statistics, replacement = _build_add_parts(
        tmp_path,
        "statistics-drift",
    )
    with pytest.raises(DeltaStoreError, match="document statistics"):
        write_delta_candidate(
            layer.root,
            parent=parent,
            generation=generation,
            recipe=recipe,
            layer=layer,
            statistics_delta=StatisticsDelta(0, 1, 1, 1, 0, 1, 1),
            replacements=(replacement,),
        )


def test_writer_freezes_exact_identity_and_authenticates_document_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe, parent, generation, layer, statistics_delta, replacement = (
        _build_add_parts(tmp_path, "identity")
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("candidate writer must not deep-audit the layer")

    monkeypatch.setattr(delta_store_module.PostingLayerReader, "audit", forbidden)
    receipt = write_delta_candidate(
        layer.root,
        parent=parent,
        generation=generation,
        recipe=recipe,
        layer=layer,
        statistics_delta=statistics_delta,
        replacements=(replacement,),
    )
    raw = (receipt.root / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    assert raw == _canonical(manifest)
    assert set(manifest) == {
        "artifact_kind",
        "schema_version",
        "delta_id",
        "parent_view_id",
        "parent_view_manifest_sha256",
        "generation",
        "generation_manifest_sha256",
        "search_view_recipe",
        "search_view_recipe_hash",
        "statistics_delta",
        "layer",
        "replacements",
    }
    core = {
        key: value
        for key, value in manifest.items()
        if key not in {"delta_id", "search_view_recipe"}
    }
    assert receipt.delta_id == manifest["delta_id"] == canonical_hash(core)
    assert manifest["statistics_delta"] == statistics_delta.as_dict()
    assert manifest["replacements"] == [replacement.as_dict()]
    assert manifest["layer"] == layer.as_dict()


def test_writer_rejects_document_table_binding_and_exact_file_set(
    tmp_path: Path,
) -> None:
    recipe, parent, generation, layer, statistics_delta, replacement = (
        _build_add_parts(tmp_path, "binding")
    )
    wrong = replace(replacement, new_segment_hash=_digest("wrong-new-segment"))
    with pytest.raises(DeltaStoreError, match="does not match Delta document table"):
        write_delta_candidate(
            layer.root,
            parent=parent,
            generation=generation,
            recipe=recipe,
            layer=layer,
            statistics_delta=statistics_delta,
            replacements=(wrong,),
        )

    (layer.root / "unexpected.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DeltaStoreError, match="exactly five"):
        write_delta_candidate(
            layer.root,
            parent=parent,
            generation=generation,
            recipe=recipe,
            layer=layer,
            statistics_delta=statistics_delta,
            replacements=(replacement,),
        )

    recipe, parent, generation, layer, statistics_delta, replacement = (
        _build_add_parts(tmp_path, "chunk-binding")
    )
    documents_path = layer.root / "layer-documents.json"
    documents = json.loads(documents_path.read_bytes())
    documents["documents"][0]["chunk_count"] = 2
    rebound = _canonical(documents)
    documents_path.write_bytes(rebound)
    bad_layer = replace(
        layer,
        documents=ArtifactRef(
            "layer-documents.json",
            hashlib.sha256(rebound).hexdigest(),
            len(rebound),
            layer.documents.records,
        ),
    )
    with pytest.raises(DeltaStoreError, match="chunk count differs"):
        write_delta_candidate(
            layer.root,
            parent=parent,
            generation=generation,
            recipe=recipe,
            layer=bad_layer,
            statistics_delta=statistics_delta,
            replacements=(replacement,),
        )


@pytest.mark.parametrize("invalid_chunk_count", [True, -1])
def test_invalid_document_chunk_count_uses_delta_store_error_at_public_boundaries(
    tmp_path: Path,
    invalid_chunk_count: object,
) -> None:
    recipe, parent, generation, layer, statistics_delta, replacement = (
        _build_add_parts(tmp_path, "invalid-writer-chunks")
    )
    documents_path = layer.root / "layer-documents.json"
    documents = json.loads(documents_path.read_bytes())
    documents["documents"][0]["chunk_count"] = invalid_chunk_count
    rebound = _canonical(documents)
    documents_path.write_bytes(rebound)
    rebound_layer = replace(
        layer,
        documents=ArtifactRef(
            "layer-documents.json",
            hashlib.sha256(rebound).hexdigest(),
            len(rebound),
            layer.documents.records,
        ),
    )
    with pytest.raises(DeltaStoreError, match="chunk_count"):
        write_delta_candidate(
            layer.root,
            parent=parent,
            generation=generation,
            recipe=recipe,
            layer=rebound_layer,
            statistics_delta=statistics_delta,
            replacements=(replacement,),
        )

    candidate, recipe, *_rest = _seal_add(tmp_path, "invalid-metadata-chunks")
    store = tmp_path / "store-invalid-chunks"
    finalized = finalize_delta_object(store, candidate)
    old_root = finalized.root
    documents_path = old_root / "layer-documents.json"
    documents = json.loads(documents_path.read_bytes())
    documents["documents"][0]["chunk_count"] = invalid_chunk_count
    rebound = _canonical(documents)
    documents_path.write_bytes(rebound)
    rebound_layer = replace(
        finalized.layer,
        documents=ArtifactRef(
            "layer-documents.json",
            hashlib.sha256(rebound).hexdigest(),
            len(rebound),
            finalized.layer.documents.records,
        ),
    )
    core = delta_store_module._core(
        parent_view_id=finalized.parent_view_id,
        parent_view_manifest_sha256=finalized.parent_view_manifest_sha256,
        generation=finalized.generation,
        generation_manifest_sha256=finalized.generation_manifest_sha256,
        search_view_recipe_hash=finalized.search_view_recipe_hash,
        statistics_delta=finalized.statistics_delta,
        layer=rebound_layer,
        replacements=finalized.replacements,
    )
    rebound_id = canonical_hash(core)
    (old_root / "manifest.json").write_bytes(
        _canonical(
            {
                **core,
                "delta_id": rebound_id,
                "search_view_recipe": recipe.as_dict(),
            }
        )
    )
    rebound_root = old_root.parent / rebound_id
    old_root.rename(rebound_root)
    with pytest.raises(DeltaStoreError, match="chunk_count"):
        load_delta_object_metadata(store, rebound_id)

def test_metadata_skips_audit_deep_load_audits_once_and_first_publish_skips_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, *_rest = _seal_add(tmp_path, "load-boundaries")
    store = tmp_path / "store"
    original = PostingLayerReader.audit

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("first publish and metadata load must not audit")

    monkeypatch.setattr(delta_store_module.PostingLayerReader, "audit", forbidden)
    finalized = finalize_delta_object(store, candidate)
    metadata = load_delta_object_metadata(store, finalized.delta_id)
    assert metadata.attestation_dict() == finalized.attestation_dict()

    calls = 0

    def counted(reader: PostingLayerReader) -> None:
        nonlocal calls
        calls += 1
        original(reader)

    monkeypatch.setattr(delta_store_module.PostingLayerReader, "audit", counted)
    loaded = load_delta_object(store, finalized.delta_id)
    assert loaded.attestation_dict() == finalized.attestation_dict()
    assert calls == 1


def test_postings_tamper_is_metadata_safe_but_deep_load_rejects(
    tmp_path: Path,
) -> None:
    candidate, *_rest = _seal_add(tmp_path, "postings-tamper")
    store = tmp_path / "store"
    finalized = finalize_delta_object(store, candidate)
    postings = finalized.root / "postings.piv"
    payload = bytearray(postings.read_bytes())
    payload[-1] ^= 1
    postings.write_bytes(payload)

    assert load_delta_object_metadata(store, finalized.delta_id).delta_id == finalized.delta_id
    with pytest.raises(DeltaStoreError, match="audit"):
        load_delta_object(store, finalized.delta_id)


def test_manifest_rebinding_and_extra_files_fail_closed(tmp_path: Path) -> None:
    candidate, *_rest = _seal_add(tmp_path, "manifest-rebind")
    store = tmp_path / "store"
    finalized = finalize_delta_object(store, candidate)
    manifest_path = finalized.root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["parent_view_manifest_sha256"] = _digest("rebound-parent-manifest")
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(DeltaStoreError, match="delta_id"):
        load_delta_object_metadata(store, finalized.delta_id)

    manifest["parent_view_manifest_sha256"] = finalized.parent_view_manifest_sha256
    manifest_path.write_bytes(_canonical(manifest))
    (finalized.root / "unexpected.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DeltaStoreError, match="file set"):
        load_delta_object_metadata(store, finalized.delta_id)


def test_identical_finalize_reuses_deep_object_and_discards_candidate(
    tmp_path: Path,
) -> None:
    first, *_ = _seal_add(tmp_path, "first")
    second, *_ = _seal_add(tmp_path, "second")
    assert first.delta_id == second.delta_id
    store = tmp_path / "store"
    finalized = finalize_delta_object(store, first)
    reused = finalize_delta_object(store, second)
    assert reused.attestation_dict() == finalized.attestation_dict()
    assert reused.root == finalized.root
    assert not second.root.exists()


def test_corrupt_existing_delta_is_conflict_and_retains_candidate(
    tmp_path: Path,
) -> None:
    first, *_ = _seal_add(tmp_path, "existing")
    candidate, *_ = _seal_add(tmp_path, "candidate")
    store = tmp_path / "store"
    finalized = finalize_delta_object(store, first)
    (finalized.root / "postings.piv").write_bytes(b"corrupt")

    with pytest.raises(DeltaStoreConflictError) as captured:
        finalize_delta_object(store, candidate)
    assert captured.value.candidate_retained is True
    assert candidate.root.is_dir()


def test_target_appearing_during_no_replace_is_deeply_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, *_ = _seal_add(tmp_path, "race-candidate")
    rival, *_ = _seal_add(tmp_path, "race-rival")
    assert candidate.delta_id == rival.delta_id
    store = tmp_path / "store"

    def race(_source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(rival.root, target)
        raise FileExistsError(target)

    monkeypatch.setattr(delta_store_module, "_rename_no_replace", race)
    finalized = finalize_delta_object(store, candidate)
    assert finalized.root == (
        store / "objects" / "search" / "deltas" / candidate.delta_id
    )
    assert not candidate.root.exists()
    assert rival.root.exists()


@pytest.mark.skipif(os.name == "nt", reason="unprivileged symlink setup varies on Windows")
def test_metadata_rejects_symlinked_artifact(tmp_path: Path) -> None:
    candidate, *_ = _seal_add(tmp_path, "symlink")
    store = tmp_path / "store"
    finalized = finalize_delta_object(store, candidate)
    postings = finalized.root / "postings.piv"
    outside = tmp_path / "outside-postings.piv"
    shutil.copyfile(postings, outside)
    postings.unlink()
    postings.symlink_to(outside)
    with pytest.raises(DeltaStoreError, match="non-regular|symlink|junction"):
        load_delta_object_metadata(store, finalized.delta_id)
