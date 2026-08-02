"""Contracts for PageIndex v3 logical and physical value objects."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
import hashlib
from pathlib import Path

import pytest

from app.index.v2.canonical import canonical_bytes, canonical_hash
from app.index.v2.object_store import StoredSegmentRef
from app.index.v3.models import (
    CompactionPolicy,
    ChunkRef,
    GenerationRecipe,
    LayerPosting,
    LegacyExportRecipe,
    SearchPosting,
    SearchViewRecipe,
    SegmentSummary,
    TokenSummary,
    ViewPin,
    logical_generation_core,
    logical_generation_id,
    make_doc_uid,
    validate_doc_key,
    validate_sha256,
)


def _stored_ref(
    doc_key: str,
    segment_hash: str,
    *,
    path: Path | None = None,
) -> StoredSegmentRef:
    doc_type, slug = doc_key.split(":", 1)
    return StoredSegmentRef(
        segment_hash=segment_hash,
        path=path or Path("objects") / f"{segment_hash}.json",
        byte_size=123,
        doc_key=doc_key,
        doc_type=doc_type,
        slug=slug,
        content_hash="a" * 64,
        segment_recipe_hash="b" * 64,
    )


def _summary(*, tokens: tuple[TokenSummary, ...] | None = None) -> SegmentSummary:
    values = tokens if tokens is not None else (
        TokenSummary("alpha", df_any=2, df_nonbody=1, df_body=2),
        TokenSummary("beta", df_any=1, df_nonbody=1, df_body=0),
    )
    return SegmentSummary(
        segment_hash="1" * 64,
        doc_key="note:a",
        doc_uid=make_doc_uid("note:a"),
        content_hash="2" * 64,
        segment_recipe_hash="3" * 64,
        chunk_count=2,
        title_length_sum=4,
        breadcrumb_length_sum=5,
        body_length_sum=20,
        posting_count=sum(item.df_any for item in values),
        tokens=values,
    )


def test_recipe_payloads_are_exact_versioned_canonical_values() -> None:
    generation = GenerationRecipe()
    view = SearchViewRecipe()
    compaction = CompactionPolicy()
    legacy = LegacyExportRecipe()

    assert generation.as_dict() == {
        "artifact_kind": "logical_generation_recipe",
        "body_df_min": 256,
        "body_df_ratio_denominator": 10,
        "body_df_ratio_numerator": 9,
        "chunk_ref_version": "doc-uid-segment-local-v1",
        "field_postings_version": "raw-field-tf-v1",
        "idf_policy_version": "effective-df-v1",
        "schema_version": 1,
    }
    assert view.as_dict() == {
        "artifact_kind": "search_view_recipe",
        "chunk_lengths_codec_version": "piv3-document-block-uvarint-v1",
        "owner_map_version": "layer-owner-map-v1",
        "posting_codec_version": "piv3-split-field-uvarint-v1",
        "replacement_version": "document-newest-wins-v1",
        "schema_version": 1,
        "statistics_version": "scalar-plus-layer-delta-v1",
        "term_index_version": "canonical-jsonl-sparse-v1",
    }
    assert compaction.as_dict() == {
        "max_delta_bytes_denominator": 5,
        "max_delta_bytes_numerator": 1,
        "max_delta_layers": 32,
    }
    assert legacy.as_dict() == {
        "artifact_kind": "legacy_export_recipe",
        "compatibility_format_version": "legacy-pageindex-v1",
        "generation_layout_version": "manifest-input-proof-v1",
        "ordering_version": "doc-node-chunk-v1",
        "schema_version": 1,
    }
    canonical_bytes(generation.as_dict())
    canonical_bytes(view.as_dict())
    canonical_bytes(compaction.as_dict())
    canonical_bytes(legacy.as_dict())


def test_recipes_are_frozen_slotted_and_return_detached_values() -> None:
    recipe = GenerationRecipe()
    assert not hasattr(recipe, "__dict__")
    with pytest.raises(FrozenInstanceError):
        recipe.body_df_min = 1  # type: ignore[misc]
    detached = recipe.as_dict()
    detached["body_df_min"] = 1
    assert recipe.body_df_min == 256


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: GenerationRecipe(schema_version=2), "schema_version"),
        (lambda: GenerationRecipe(artifact_kind="other"), "artifact_kind"),
        (lambda: GenerationRecipe(field_postings_version="other"), "field_postings_version"),
        (lambda: GenerationRecipe(chunk_ref_version="other"), "chunk_ref_version"),
        (lambda: GenerationRecipe(idf_policy_version="other"), "idf_policy_version"),
        (lambda: SearchViewRecipe(schema_version=2), "schema_version"),
        (lambda: SearchViewRecipe(posting_codec_version="other"), "posting_codec_version"),
        (lambda: SearchViewRecipe(owner_map_version="other"), "owner_map_version"),
        (lambda: LegacyExportRecipe(ordering_version="other"), "ordering_version"),
    ],
)
def test_recipes_reject_unknown_versions(factory: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: GenerationRecipe(body_df_min=True),
        lambda: GenerationRecipe(body_df_min=0),
        lambda: GenerationRecipe(body_df_ratio_numerator=True),
        lambda: GenerationRecipe(body_df_ratio_numerator=-1),
        lambda: GenerationRecipe(body_df_ratio_denominator=0),
        lambda: GenerationRecipe(
            body_df_ratio_numerator=11,
            body_df_ratio_denominator=10,
        ),
        lambda: CompactionPolicy(max_delta_layers=True),
        lambda: CompactionPolicy(max_delta_layers=0),
        lambda: CompactionPolicy(max_delta_bytes_numerator=0),
        lambda: CompactionPolicy(max_delta_bytes_denominator=0),
        lambda: CompactionPolicy(
            max_delta_bytes_numerator=6,
            max_delta_bytes_denominator=5,
        ),
    ],
)
def test_recipe_numeric_boundaries(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


def test_doc_uid_is_exact_utf8_sha256_and_type_namespaced() -> None:
    assert make_doc_uid("note:a") == hashlib.sha256(b"note:a").hexdigest()
    assert make_doc_uid("note:a") != make_doc_uid("book:a")
    assert make_doc_uid("note:café") == hashlib.sha256(
        "note:café".encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "value",
    ["", "alpha", "unknown:a", "note:", "note:..", "note:a/b", "note:a:b", "note:cafe\u0301"],
)
def test_doc_uid_rejects_malformed_or_noncanonical_keys(value: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        make_doc_uid(value)
    with pytest.raises((TypeError, ValueError)):
        validate_doc_key(value)


def test_doc_uid_rejects_non_strings() -> None:
    with pytest.raises(TypeError):
        make_doc_uid(1)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["a" * 63, "a" * 65, "A" * 64, "g" * 64, 1])
def test_sha256_validator_rejects_malformed_values(value: object) -> None:
    with pytest.raises(ValueError, match="digest"):
        validate_sha256(value, "digest")


def test_logical_generation_core_is_exact_sorted_and_order_independent() -> None:
    refs = (
        _stored_ref("note:z", "2" * 64),
        _stored_ref("book:a", "1" * 64),
    )
    core = logical_generation_core(refs, GenerationRecipe())
    assert core == {
        "artifact_kind": "logical_generation",
        "schema_version": 4,
        "generation_recipe_hash": canonical_hash(GenerationRecipe().as_dict()),
        "documents": {"book:a": "1" * 64, "note:z": "2" * 64},
    }
    identifier = logical_generation_id(refs, GenerationRecipe())
    assert tuple(core["documents"]) == ("book:a", "note:z")
    assert identifier == canonical_hash(core)
    assert len(identifier) == 64
    assert identifier == logical_generation_id(tuple(reversed(refs)), GenerationRecipe())


def test_logical_identity_ignores_nonlogical_ref_attestations() -> None:
    ref = _stored_ref("note:a", "1" * 64)
    changed_attestation = replace(
        ref,
        path=Path("elsewhere") / "segment.json",
        byte_size=999,
        content_hash="c" * 64,
        segment_recipe_hash="d" * 64,
    )
    assert logical_generation_id((ref,), GenerationRecipe()) == logical_generation_id(
        (changed_attestation,), GenerationRecipe()
    )
    assert logical_generation_id((ref,), GenerationRecipe()) != logical_generation_id(
        (replace(ref, segment_hash="2" * 64),), GenerationRecipe()
    )
    assert logical_generation_id((ref,), GenerationRecipe()) != logical_generation_id(
        (ref,), GenerationRecipe(body_df_min=257)
    )


def test_equivalent_policy_ratios_have_one_canonical_identity() -> None:
    ref = _stored_ref("note:a", "1" * 64)
    reduced = GenerationRecipe(
        body_df_ratio_numerator=9,
        body_df_ratio_denominator=10,
    )
    equivalent = GenerationRecipe(
        body_df_ratio_numerator=18,
        body_df_ratio_denominator=20,
    )
    assert equivalent == reduced
    assert equivalent.as_dict() == reduced.as_dict()
    assert logical_generation_id((ref,), equivalent) == logical_generation_id(
        (ref,), reduced
    )
    assert logical_generation_id(
        (ref,),
        GenerationRecipe(body_df_ratio_numerator=4, body_df_ratio_denominator=5),
    ) != logical_generation_id((ref,), reduced)
    assert CompactionPolicy(
        max_delta_bytes_numerator=2,
        max_delta_bytes_denominator=10,
    ) == CompactionPolicy()


def test_physical_compaction_and_legacy_recipes_are_outside_logical_api() -> None:
    ref = _stored_ref("note:a", "1" * 64)
    core = logical_generation_core((ref,), GenerationRecipe())
    encoded = canonical_bytes(core)
    assert b"search_view" not in encoded
    assert b"compaction" not in encoded
    assert b"legacy" not in encoded
    assert SearchViewRecipe() == SearchViewRecipe(owner_map_version="layer-owner-map-v1")
    assert CompactionPolicy(max_delta_layers=8) != CompactionPolicy(max_delta_layers=32)
    with pytest.raises(TypeError, match="GenerationRecipe"):
        logical_generation_core((ref,), SearchViewRecipe())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ref: replace(ref, doc_type="book"),
        lambda ref: replace(ref, slug="other"),
        lambda ref: replace(ref, segment_hash="A" * 64),
        lambda ref: replace(ref, content_hash="bad"),
        lambda ref: replace(ref, segment_recipe_hash="bad"),
        lambda ref: replace(ref, byte_size=True),
        lambda ref: replace(ref, byte_size=-1),
        lambda ref: replace(ref, path="not-a-path"),
    ],
)
def test_logical_generation_revalidates_manual_segment_refs(mutate: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        logical_generation_core((mutate(_stored_ref("note:a", "1" * 64)),), GenerationRecipe())  # type: ignore[operator]


def test_logical_generation_rejects_duplicate_documents_and_wrong_types() -> None:
    ref = _stored_ref("note:a", "1" * 64)
    with pytest.raises(ValueError, match="duplicate"):
        logical_generation_core((ref, replace(ref, segment_hash="2" * 64)), GenerationRecipe())
    with pytest.raises(ValueError, match="segment_hash"):
        logical_generation_core(
            (ref, _stored_ref("book:b", "1" * 64)),
            GenerationRecipe(),
        )
    with pytest.raises(TypeError, match="StoredSegmentRef"):
        logical_generation_core((object(),), GenerationRecipe())  # type: ignore[arg-type]


def test_chunk_and_posting_records_are_strict_frozen_and_orderable() -> None:
    first = ChunkRef("1" * 64, "2" * 64, 0)
    second = ChunkRef("1" * 64, "2" * 64, 1)
    assert first < second
    posting = SearchPosting("alpha", first, title_tf=1, breadcrumb_tf=0, body_tf=2)
    layer = LayerPosting("alpha", 0, 0, title_tf=1, breadcrumb_tf=0, body_tf=2)
    assert posting.total_tf == 3
    assert layer.total_tf == 3
    assert posting.key == ("alpha", first)
    assert layer.key == ("alpha", 0, 0)
    assert replace(posting, body_tf=3).key == posting.key
    assert replace(layer, body_tf=3).key == layer.key
    assert replace(posting, body_tf=3) != posting
    assert replace(layer, body_tf=3) != layer
    assert [item.name for item in fields(SearchPosting)] == [
        "token", "chunk_ref", "title_tf", "breadcrumb_tf", "body_tf"
    ]
    assert [item.name for item in fields(LayerPosting)] == [
        "token", "doc_ordinal", "local_id", "title_tf", "breadcrumb_tf", "body_tf"
    ]
    assert not hasattr(posting, "__dict__")
    with pytest.raises(FrozenInstanceError):
        posting.body_tf = 0  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ChunkRef("bad", "2" * 64, 0),
        lambda: ChunkRef("1" * 64, "bad", 0),
        lambda: ChunkRef("1" * 64, "2" * 64, True),
        lambda: ChunkRef("1" * 64, "2" * 64, -1),
        lambda: ChunkRef("1" * 64, "2" * 64, 2**64),
        lambda: SearchPosting("", ChunkRef("1" * 64, "2" * 64, 0), 1, 0, 0),
        lambda: SearchPosting("\ud800", ChunkRef("1" * 64, "2" * 64, 0), 1, 0, 0),
        lambda: SearchPosting("alpha", ChunkRef("1" * 64, "2" * 64, 0), 0, 0, 0),
        lambda: SearchPosting("alpha", ChunkRef("1" * 64, "2" * 64, 0), True, 0, 0),
        lambda: LayerPosting("alpha", True, 0, 1, 0, 0),
        lambda: LayerPosting("alpha", 0, 0, 2**64, 0, 0),
    ],
)
def test_chunk_and_posting_records_reject_bad_values(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def test_token_summary_allows_field_overlap_but_enforces_union_bounds() -> None:
    assert TokenSummary("alpha", 2, 1, 2).as_dict() == {
        "token": "alpha",
        "df_any": 2,
        "df_nonbody": 1,
        "df_body": 2,
    }
    assert TokenSummary("overlap", 1, 1, 1)
    assert TokenSummary("disjoint", 2, 1, 1)
    assert TokenSummary("body-only", 1, 0, 1)
    assert TokenSummary("nonbody-only", 1, 1, 0)
    assert TokenSummary("maximum", 2**64 - 1, 2**64 - 1, 0)
    with pytest.raises(ValueError):
        TokenSummary("alpha", 1, 2, 0)
    with pytest.raises(ValueError):
        TokenSummary("alpha", 3, 1, 1)
    with pytest.raises(ValueError):
        TokenSummary("alpha", 0, 0, 0)


@pytest.mark.parametrize(
    "values",
    [
        (True, 1, 0),
        (1, True, 0),
        (1, 0, True),
        (-1, 0, 0),
        (1, -1, 1),
        (1, 1, -1),
        (2**64, 2**64, 0),
    ],
)
def test_token_summary_rejects_non_u64_counts(values: tuple[object, object, object]) -> None:
    with pytest.raises(ValueError):
        TokenSummary("alpha", *values)  # type: ignore[arg-type]


def test_segment_summary_is_strict_immutable_and_detached() -> None:
    summary = _summary()
    assert not hasattr(summary, "__dict__")
    assert isinstance(summary.tokens, tuple)
    assert summary.as_dict() == {
        "artifact_kind": "segment_search_summary",
        "schema_version": 1,
        "segment_hash": "1" * 64,
        "doc_key": "note:a",
        "doc_uid": make_doc_uid("note:a"),
        "content_hash": "2" * 64,
        "segment_recipe_hash": "3" * 64,
        "chunk_count": 2,
        "field_length_sums": {"title": 4, "breadcrumb": 5, "body": 20},
        "posting_count": 3,
        "tokens": [item.as_dict() for item in summary.tokens],
    }
    detached = summary.as_dict()
    detached["tokens"].clear()  # type: ignore[union-attr]
    assert len(summary.tokens) == 2


def test_segment_summary_allows_empty_corpus_and_empty_text_chunk() -> None:
    empty = replace(
        _summary(tokens=()),
        chunk_count=0,
        title_length_sum=0,
        breadcrumb_length_sum=0,
        body_length_sum=0,
        posting_count=0,
    )
    empty_chunk = replace(empty, chunk_count=1)
    assert empty.tokens == ()
    assert empty.as_dict()["tokens"] == []
    assert empty_chunk.chunk_count == 1


@pytest.mark.parametrize(
    "factory",
    [
        lambda: replace(_summary(), doc_uid="f" * 64),
        lambda: replace(_summary(), posting_count=2),
        lambda: replace(_summary(), chunk_count=1),
        lambda: replace(_summary(), title_length_sum=0, breadcrumb_length_sum=0),
        lambda: replace(_summary(), body_length_sum=0),
        lambda: replace(_summary(), tokens=tuple(reversed(_summary().tokens))),
        lambda: replace(_summary(), tokens=(_summary().tokens[0],) * 2),
        lambda: replace(_summary(), title_length_sum=True),
    ],
)
def test_segment_summary_rejects_cross_field_inconsistency(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def test_view_pin_requires_complete_hashes() -> None:
    pin = ViewPin("1" * 64, "2" * 64)
    assert pin.as_dict() == {"generation": "1" * 64, "view_id": "2" * 64}
    with pytest.raises(ValueError):
        ViewPin("1" * 20, "2" * 64)
    with pytest.raises(ValueError):
        ViewPin("1" * 64, "2" * 20)
