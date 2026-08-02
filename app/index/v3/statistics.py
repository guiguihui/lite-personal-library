"""Bounded scalar statistics and sparse token deltas for PageIndex v3."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .models import MAX_U64, SegmentSummary


_SCALAR_FIELDS = (
    "documents",
    "total_chunks",
    "title_length_sum",
    "breadcrumb_length_sum",
    "body_length_sum",
    "posting_count",
)


def _require_u64(name: str, value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_U64
    ):
        raise ValueError(f"{name} must be an integer in [0, {MAX_U64}]")
    return value


def _require_signed_u64(name: str, value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < -MAX_U64
        or value > MAX_U64
    ):
        raise ValueError(
            f"{name} must be an integer in [-{MAX_U64}, {MAX_U64}]"
        )
    return value


def _checked_add_u64(name: str, left: int, right: int) -> int:
    result = left + right
    if result > MAX_U64:
        raise ValueError(f"{name} exceeds the unsigned 64-bit range")
    return result


def _checked_add_signed_u64(name: str, left: int, right: int) -> int:
    result = left + right
    if result < -MAX_U64 or result > MAX_U64:
        raise ValueError(f"{name} exceeds the signed PageIndex delta range")
    return result


def _validate_token(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("token must be a string")
    if not value:
        raise ValueError("token must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("token must be valid UTF-8 text") from exc
    return value


def _iter_validated_summaries(
    summaries: Iterable[SegmentSummary],
    *,
    side: str,
) -> Iterator[SegmentSummary]:
    if isinstance(summaries, (str, bytes, bytearray)):
        raise TypeError(f"{side} must be an iterable of SegmentSummary values")
    try:
        iterator = iter(summaries)
    except TypeError as exc:
        raise TypeError(
            f"{side} must be an iterable of SegmentSummary values"
        ) from exc

    seen_documents: set[str] = set()
    for summary in iterator:
        if not isinstance(summary, SegmentSummary):
            raise TypeError(f"{side} must contain only SegmentSummary values")
        if summary.doc_uid in seen_documents:
            raise ValueError(
                f"{side} contains duplicate document summary: {summary.doc_key}"
            )
        seen_documents.add(summary.doc_uid)
        yield summary


def _summary_scalar_totals(
    summaries: Iterable[SegmentSummary],
) -> dict[str, int]:
    totals = {field: 0 for field in _SCALAR_FIELDS}
    for summary in summaries:
        totals["documents"] = _checked_add_u64(
            "documents", totals["documents"], 1
        )
        totals["total_chunks"] = _checked_add_u64(
            "total_chunks", totals["total_chunks"], summary.chunk_count
        )
        totals["title_length_sum"] = _checked_add_u64(
            "title_length_sum",
            totals["title_length_sum"],
            summary.title_length_sum,
        )
        totals["breadcrumb_length_sum"] = _checked_add_u64(
            "breadcrumb_length_sum",
            totals["breadcrumb_length_sum"],
            summary.breadcrumb_length_sum,
        )
        totals["body_length_sum"] = _checked_add_u64(
            "body_length_sum",
            totals["body_length_sum"],
            summary.body_length_sum,
        )
        # This is the logical posting count recorded by the authenticated
        # summary.  It is deliberately independent of any physical layer rows.
        totals["posting_count"] = _checked_add_u64(
            "posting_count", totals["posting_count"], summary.posting_count
        )
    return totals


@dataclass(frozen=True, slots=True)
class CorpusTotals:
    """O(1) corpus-wide values; no vocabulary is retained here."""

    documents: int
    total_chunks: int
    token_count: int
    title_length_sum: int
    breadcrumb_length_sum: int
    body_length_sum: int
    posting_count: int

    def __post_init__(self) -> None:
        for name in (
            "documents",
            "total_chunks",
            "token_count",
            "title_length_sum",
            "breadcrumb_length_sum",
            "body_length_sum",
            "posting_count",
        ):
            _require_u64(name, getattr(self, name))

        non_document_values = (
            self.total_chunks,
            self.token_count,
            self.title_length_sum,
            self.breadcrumb_length_sum,
            self.body_length_sum,
            self.posting_count,
        )
        if self.documents == 0 and any(non_document_values):
            raise ValueError("an empty corpus requires every aggregate to be zero")

        searchable_values = (
            self.token_count,
            self.title_length_sum,
            self.breadcrumb_length_sum,
            self.body_length_sum,
            self.posting_count,
        )
        if self.total_chunks == 0 and any(searchable_values):
            raise ValueError(
                "a corpus with zero chunks cannot contain searchable statistics"
            )

        field_length_sum = (
            self.title_length_sum
            + self.breadcrumb_length_sum
            + self.body_length_sum
        )
        if (self.posting_count == 0) != (field_length_sum == 0):
            raise ValueError(
                "posting_count must equal zero exactly when all field lengths are zero"
            )
        if (self.token_count == 0) != (self.posting_count == 0):
            raise ValueError(
                "token_count must equal zero exactly when posting_count is zero"
            )
        if self.token_count > self.posting_count:
            raise ValueError("token_count must not exceed posting_count")
        if self.posting_count > field_length_sum:
            raise ValueError("posting_count must not exceed total field length")

    @classmethod
    def from_summaries(
        cls,
        summaries: Iterable[SegmentSummary],
        token_count: int,
    ) -> CorpusTotals:
        """Aggregate summary scalars without inspecting ``summary.tokens``."""

        validated_token_count = _require_u64("token_count", token_count)
        totals = _summary_scalar_totals(
            _iter_validated_summaries(summaries, side="summaries")
        )
        return cls(token_count=validated_token_count, **totals)

    def apply(
        self,
        removed: Iterable[SegmentSummary],
        added: Iterable[SegmentSummary],
        token_count_delta: int,
    ) -> CorpusTotals:
        """Return ``self - removed + added`` with a strict subtraction barrier."""

        validated_token_delta = _require_signed_u64(
            "token_count_delta", token_count_delta
        )

        removed_totals = _summary_scalar_totals(
            _iter_validated_summaries(removed, side="removed")
        )
        intermediate: dict[str, int] = {}
        for field in _SCALAR_FIELDS:
            value = getattr(self, field) - removed_totals[field]
            if value < 0:
                raise ValueError(
                    f"cannot remove {removed_totals[field]} from "
                    f"{field}={getattr(self, field)}"
                )
            intermediate[field] = value

        # Validate and add the other side only after every subtraction above
        # succeeded.  This prevents an unrelated addition from masking an
        # invalid removal.
        added_totals = _summary_scalar_totals(
            _iter_validated_summaries(added, side="added")
        )
        final: dict[str, int] = {}
        for field in _SCALAR_FIELDS:
            final[field] = _checked_add_u64(
                field, intermediate[field], added_totals[field]
            )

        final_token_count = self.token_count + validated_token_delta
        _require_u64("token_count after delta", final_token_count)
        return CorpusTotals(token_count=final_token_count, **final)

    def as_dict(self) -> dict[str, int]:
        return {
            "documents": self.documents,
            "total_chunks": self.total_chunks,
            "token_count": self.token_count,
            "title_length_sum": self.title_length_sum,
            "breadcrumb_length_sum": self.breadcrumb_length_sum,
            "body_length_sum": self.body_length_sum,
            "posting_count": self.posting_count,
        }


@dataclass(frozen=True, slots=True)
class TokenDfDelta:
    """A signed raw DF change for one touched token."""

    token: str
    df_any: int
    df_nonbody: int
    df_body: int

    def __post_init__(self) -> None:
        _validate_token(self.token)
        for name in ("df_any", "df_nonbody", "df_body"):
            _require_signed_u64(name, getattr(self, name))
        if self.df_any == self.df_nonbody == self.df_body == 0:
            raise ValueError("a TokenDfDelta must contain at least one change")

    @property
    def triple(self) -> tuple[int, int, int]:
        return (self.df_any, self.df_nonbody, self.df_body)

    def as_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "df_any": self.df_any,
            "df_nonbody": self.df_nonbody,
            "df_body": self.df_body,
        }


def token_df_deltas(
    removed: Iterable[SegmentSummary],
    added: Iterable[SegmentSummary],
) -> Iterator[TokenDfDelta]:
    """Yield UTF-8-sorted net DF changes from changed summaries only."""

    by_token: dict[str, list[int]] = {}

    def accumulate(summary: SegmentSummary, sign: int) -> None:
        for token_summary in summary.tokens:
            values = by_token.setdefault(token_summary.token, [0, 0, 0])
            values[0] = _checked_add_signed_u64(
                f"{token_summary.token!r} df_any",
                values[0],
                sign * token_summary.df_any,
            )
            values[1] = _checked_add_signed_u64(
                f"{token_summary.token!r} df_nonbody",
                values[1],
                sign * token_summary.df_nonbody,
            )
            values[2] = _checked_add_signed_u64(
                f"{token_summary.token!r} df_body",
                values[2],
                sign * token_summary.df_body,
            )

    for summary in _iter_validated_summaries(removed, side="removed"):
        accumulate(summary, -1)
    for summary in _iter_validated_summaries(added, side="added"):
        accumulate(summary, 1)

    for token in sorted(by_token, key=lambda value: value.encode("utf-8")):
        df_any, df_nonbody, df_body = by_token[token]
        if df_any == df_nonbody == df_body == 0:
            continue
        yield TokenDfDelta(token, df_any, df_nonbody, df_body)


__all__ = ["CorpusTotals", "TokenDfDelta", "token_df_deltas"]
