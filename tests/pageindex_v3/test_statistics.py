"""Contracts for bounded PageIndex v3 corpus statistics."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
import hashlib

import pytest

from app.index.v3.models import MAX_U64, SegmentSummary, TokenSummary, make_doc_uid
from app.index.v3.statistics import CorpusTotals, TokenDfDelta, token_df_deltas


def _summary(
    doc_key: str,
    tokens: tuple[TokenSummary, ...] = (),
    *,
    salt: str = "",
    chunk_count: int | None = None,
    title_length_sum: int | None = None,
    breadcrumb_length_sum: int = 0,
    body_length_sum: int | None = None,
) -> SegmentSummary:
    ordered_tokens = tuple(sorted(tokens, key=lambda item: item.token.encode("utf-8")))
    inferred_chunks = max(
        (
            max(item.df_any, item.df_nonbody, item.df_body)
            for item in ordered_tokens
        ),
        default=0,
    )
    title = (
        sum(item.df_nonbody for item in ordered_tokens)
        if title_length_sum is None
        else title_length_sum
    )
    body = (
        sum(item.df_body for item in ordered_tokens)
        if body_length_sum is None
        else body_length_sum
    )
    digest = hashlib.sha256(f"{doc_key}|{salt}".encode("utf-8")).hexdigest()
    return SegmentSummary(
        segment_hash=digest,
        doc_key=doc_key,
        doc_uid=make_doc_uid(doc_key),
        content_hash=hashlib.sha256(f"content|{doc_key}|{salt}".encode()).hexdigest(),
        segment_recipe_hash="f" * 64,
        chunk_count=inferred_chunks if chunk_count is None else chunk_count,
        title_length_sum=title,
        breadcrumb_length_sum=breadcrumb_length_sum,
        body_length_sum=body,
        posting_count=sum(item.df_any for item in ordered_tokens),
        tokens=ordered_tokens,
    )


def _vocabulary_size(summaries: tuple[SegmentSummary, ...]) -> int:
    return len({item.token for summary in summaries for item in summary.tokens})


def _valid_totals() -> CorpusTotals:
    return CorpusTotals(
        documents=1,
        total_chunks=1,
        token_count=1,
        title_length_sum=1,
        breadcrumb_length_sum=0,
        body_length_sum=0,
        posting_count=1,
    )


def test_from_summaries_builds_exact_frozen_o1_totals() -> None:
    summaries = (
        _summary(
            "note:a",
            (
                TokenSummary("alpha", 2, 1, 2),
                TokenSummary("beta", 1, 1, 0),
            ),
        ),
        _summary(
            "note:b",
            (
                TokenSummary("beta", 1, 0, 1),
                TokenSummary("gamma", 1, 1, 0),
            ),
        ),
    )

    totals = CorpusTotals.from_summaries(summaries, token_count=3)

    assert totals == CorpusTotals(
        documents=2,
        total_chunks=3,
        token_count=3,
        title_length_sum=3,
        breadcrumb_length_sum=0,
        body_length_sum=3,
        posting_count=5,
    )
    assert totals.as_dict() == {
        "documents": 2,
        "total_chunks": 3,
        "token_count": 3,
        "title_length_sum": 3,
        "breadcrumb_length_sum": 0,
        "body_length_sum": 3,
        "posting_count": 5,
    }
    with pytest.raises(FrozenInstanceError):
        totals.documents = 3  # type: ignore[misc]


def test_corpus_totals_never_reads_summary_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    summaries = (
        _summary("note:a", (TokenSummary("alpha", 2, 1, 2),)),
        _summary("note:b", (TokenSummary("beta", 1, 1, 0),)),
    )
    original_getattribute = SegmentSummary.__getattribute__

    def reject_tokens(self: SegmentSummary, name: str) -> object:
        if name == "tokens":
            raise AssertionError("CorpusTotals must not inspect summary.tokens")
        return original_getattribute(self, name)

    monkeypatch.setattr(SegmentSummary, "__getattribute__", reject_tokens)

    totals = CorpusTotals.from_summaries(summaries, token_count=2)
    patched = totals.apply((summaries[0],), (summaries[0],), token_count_delta=0)

    assert totals.posting_count == 3
    assert patched == totals


def test_posting_count_comes_from_summary_not_token_record_count() -> None:
    summary = _summary(
        "note:a",
        (
            TokenSummary("alpha", 3, 3, 0),
            TokenSummary("beta", 2, 0, 2),
        ),
    )

    totals = CorpusTotals.from_summaries((summary,), token_count=2)

    assert len(summary.tokens) == 2
    assert summary.posting_count == 5
    assert totals.posting_count == 5


def test_token_count_is_a_mandatory_external_full_base_result() -> None:
    with pytest.raises(TypeError):
        CorpusTotals.from_summaries(())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        _valid_totals().apply((), ())  # type: ignore[call-arg]


@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", -1, MAX_U64 + 1])
@pytest.mark.parametrize(
    "field",
    [
        "documents",
        "total_chunks",
        "token_count",
        "title_length_sum",
        "breadcrumb_length_sum",
        "body_length_sum",
        "posting_count",
    ],
)
def test_corpus_totals_rejects_non_u64_fields(field: str, invalid: object) -> None:
    with pytest.raises(ValueError):
        replace(_valid_totals(), **{field: invalid})


def test_empty_corpus_and_searchable_scalar_conservation() -> None:
    assert CorpusTotals(0, 0, 0, 0, 0, 0, 0)
    assert CorpusTotals(1, 0, 0, 0, 0, 0, 0)  # one empty document

    invalid_values = (
        (0, 1, 0, 0, 0, 0, 0),
        (1, 0, 1, 1, 0, 0, 1),
        (1, 1, 0, 1, 0, 0, 1),
        (1, 1, 1, 0, 0, 0, 1),
        (1, 1, 2, 1, 0, 0, 1),
        (1, 1, 1, 1, 0, 0, 2),
    )
    for values in invalid_values:
        with pytest.raises(ValueError):
            CorpusTotals(*values)


def test_from_summaries_rejects_duplicate_documents_per_side() -> None:
    old = _summary("note:a", (TokenSummary("old", 1, 1, 0),), salt="old")
    new = _summary("note:a", (TokenSummary("new", 1, 0, 1),), salt="new")

    with pytest.raises(ValueError, match="duplicate document"):
        CorpusTotals.from_summaries((old, new), token_count=2)


def test_from_summaries_rejects_scalar_overflow() -> None:
    left = _summary(
        "note:a",
        (TokenSummary("a", 1, 1, 0),),
        title_length_sum=MAX_U64,
    )
    right = _summary(
        "note:b",
        (TokenSummary("b", 1, 1, 0),),
        title_length_sum=1,
    )

    with pytest.raises(ValueError, match="title_length_sum exceeds"):
        CorpusTotals.from_summaries((left, right), token_count=2)


@pytest.mark.parametrize("bad", ["summaries", b"summaries", 42, (object(),)])
def test_summary_inputs_are_strict_iterables(bad: object) -> None:
    with pytest.raises(TypeError):
        CorpusTotals.from_summaries(bad, token_count=0)  # type: ignore[arg-type]


def test_apply_matches_clean_recomputation_for_replace_delete_and_add() -> None:
    a = _summary(
        "note:a",
        (
            TokenSummary("alpha", 1, 1, 0),
            TokenSummary("common", 1, 0, 1),
        ),
    )
    b_old = _summary(
        "note:b",
        (
            TokenSummary("common", 1, 1, 0),
            TokenSummary("old-only", 1, 0, 1),
        ),
        salt="old",
    )
    c = _summary("note:c", (TokenSummary("deleted", 1, 1, 0),))
    b_new = _summary(
        "note:b",
        (
            TokenSummary("common", 1, 0, 1),
            TokenSummary("fresh", 1, 1, 0),
        ),
        salt="new",
    )
    d = _summary("note:d", (TokenSummary("新", 1, 0, 1),))
    before = (a, b_old, c)
    after = (a, b_new, d)
    before_tokens = _vocabulary_size(before)
    after_tokens = _vocabulary_size(after)

    base = CorpusTotals.from_summaries(before, token_count=before_tokens)
    patched = base.apply(
        (b_old, c),
        (b_new, d),
        token_count_delta=after_tokens - before_tokens,
    )
    clean = CorpusTotals.from_summaries(after, token_count=after_tokens)

    assert patched == clean


def test_apply_allows_one_document_on_both_sides_of_replacement() -> None:
    old = _summary("note:a", (TokenSummary("old", 1, 1, 0),), salt="old")
    new = _summary("note:a", (TokenSummary("new", 1, 0, 1),), salt="new")
    base = CorpusTotals.from_summaries((old,), token_count=1)

    assert base.apply((old,), (new,), token_count_delta=0) == (
        CorpusTotals.from_summaries((new,), token_count=1)
    )


def test_apply_rejects_removal_before_consuming_additions() -> None:
    base_summary = _summary("note:a", (TokenSummary("base", 1, 1, 0),))
    impossible_removal = _summary(
        "note:x",
        (TokenSummary("x", 1, 1, 0),),
        title_length_sum=5,
    )
    base = CorpusTotals.from_summaries((base_summary,), token_count=1)

    def additions_must_not_be_read() -> Iterator[SegmentSummary]:
        raise AssertionError("addition side was consumed before subtraction failed")
        yield impossible_removal

    with pytest.raises(ValueError, match="cannot remove"):
        base.apply(
            (impossible_removal,),
            additions_must_not_be_read(),
            token_count_delta=0,
        )


def test_apply_rejects_duplicate_documents_independently_on_each_side() -> None:
    old = _summary("note:a", (TokenSummary("old", 1, 1, 0),), salt="old")
    duplicate = _summary(
        "note:a", (TokenSummary("other", 1, 0, 1),), salt="other"
    )
    base = CorpusTotals.from_summaries((old,), token_count=1)

    with pytest.raises(ValueError, match="removed contains duplicate"):
        base.apply((old, duplicate), (), token_count_delta=-1)
    with pytest.raises(ValueError, match="added contains duplicate"):
        base.apply((), (old, duplicate), token_count_delta=1)


@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", -MAX_U64 - 1, MAX_U64 + 1])
def test_apply_rejects_invalid_token_count_delta(invalid: object) -> None:
    with pytest.raises(ValueError):
        _valid_totals().apply((), (), token_count_delta=invalid)  # type: ignore[arg-type]


def test_apply_rejects_negative_and_overflowed_final_token_count() -> None:
    with pytest.raises(ValueError, match="token_count after delta"):
        _valid_totals().apply((), (), token_count_delta=-2)

    maximum = CorpusTotals(
        documents=1,
        total_chunks=MAX_U64,
        token_count=MAX_U64,
        title_length_sum=MAX_U64,
        breadcrumb_length_sum=0,
        body_length_sum=0,
        posting_count=MAX_U64,
    )
    with pytest.raises(ValueError, match="token_count after delta"):
        maximum.apply((), (), token_count_delta=1)


def test_apply_rejects_added_scalar_overflow() -> None:
    maximum = CorpusTotals(
        documents=1,
        total_chunks=1,
        token_count=1,
        title_length_sum=MAX_U64,
        breadcrumb_length_sum=0,
        body_length_sum=0,
        posting_count=1,
    )
    addition = _summary("note:b", (TokenSummary("b", 1, 1, 0),))

    with pytest.raises(ValueError, match="title_length_sum exceeds"):
        maximum.apply((), (addition,), token_count_delta=0)


def test_token_df_deltas_preserves_disappearance_and_field_migration() -> None:
    old = _summary(
        "note:a",
        (
            TokenSummary("gone", 2, 1, 2),
            TokenSummary("migrate", 1, 1, 0),
            TokenSummary("same", 1, 0, 1),
        ),
        salt="old",
    )
    new = _summary(
        "note:a",
        (
            TokenSummary("migrate", 1, 0, 1),
            TokenSummary("new", 1, 1, 0),
            TokenSummary("same", 1, 0, 1),
        ),
        salt="new",
    )

    result = token_df_deltas((old,), (new,))

    assert isinstance(result, Iterator)
    assert list(result) == [
        TokenDfDelta("gone", -2, -1, -2),
        TokenDfDelta("migrate", 0, -1, 1),
        TokenDfDelta("new", 1, 1, 0),
    ]


def test_token_df_deltas_uses_utf8_order_and_omits_zero_net_tokens() -> None:
    old = _summary("note:a", (TokenSummary("same", 1, 1, 0),), salt="old")
    added = _summary(
        "note:b",
        tuple(TokenSummary(token, 1, 1, 0) for token in ("z", "é", "中", "a")),
    )
    same = _summary("note:a", (TokenSummary("same", 1, 1, 0),), salt="new")

    deltas = list(token_df_deltas((old,), (added, same)))

    assert [item.token for item in deltas] == sorted(
        ("z", "é", "中", "a"), key=lambda token: token.encode("utf-8")
    )
    assert "same" not in {item.token for item in deltas}


def test_token_df_deltas_rejects_duplicate_documents_per_side() -> None:
    old = _summary("note:a", (TokenSummary("old", 1, 1, 0),), salt="old")
    duplicate = _summary("note:a", (TokenSummary("new", 1, 0, 1),), salt="new")

    with pytest.raises(ValueError, match="removed contains duplicate"):
        list(token_df_deltas((old, duplicate), ()))
    with pytest.raises(ValueError, match="added contains duplicate"):
        list(token_df_deltas((), (old, duplicate)))


def test_token_df_deltas_rejects_signed_accumulator_overflow() -> None:
    left = _summary(
        "note:a",
        (TokenSummary("huge", MAX_U64, MAX_U64, 0),),
        chunk_count=MAX_U64,
        title_length_sum=MAX_U64,
    )
    right = _summary(
        "note:b",
        (TokenSummary("huge", 1, 1, 0),),
    )

    with pytest.raises(ValueError, match="delta range"):
        list(token_df_deltas((left, right), ()))


@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", -MAX_U64 - 1, MAX_U64 + 1])
@pytest.mark.parametrize("field", ["df_any", "df_nonbody", "df_body"])
def test_token_df_delta_rejects_invalid_signed_fields(
    field: str, invalid: object
) -> None:
    values: dict[str, object] = {"df_any": 1, "df_nonbody": 0, "df_body": 0}
    values[field] = invalid
    with pytest.raises(ValueError):
        TokenDfDelta("token", **values)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", ["", 1, "\ud800"])
def test_token_df_delta_rejects_invalid_tokens(invalid: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        TokenDfDelta(invalid, 1, 0, 0)  # type: ignore[arg-type]


def test_token_df_delta_is_nonzero_frozen_and_serializable() -> None:
    with pytest.raises(ValueError, match="at least one change"):
        TokenDfDelta("token", 0, 0, 0)

    value = TokenDfDelta("token", 0, -1, 1)
    assert value.triple == (0, -1, 1)
    assert value.as_dict() == {
        "token": "token",
        "df_any": 0,
        "df_nonbody": -1,
        "df_body": 1,
    }
    with pytest.raises(FrozenInstanceError):
        value.df_any = 1  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["summaries", b"summaries", 42, (object(),)])
def test_token_delta_summary_inputs_are_strict(bad: object) -> None:
    with pytest.raises(TypeError):
        list(token_df_deltas(bad, ()))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        list(token_df_deltas((), bad))  # type: ignore[arg-type]
