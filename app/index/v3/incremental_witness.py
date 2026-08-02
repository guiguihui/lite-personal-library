"""Ephemeral observations shared inside one trusted incremental build."""

from __future__ import annotations

from dataclasses import dataclass, field

from .layer_codec import TokenContribution
from .models import MAX_U64, validate_sha256


@dataclass(frozen=True, slots=True)
class ParentDfWitness:
    """Authenticated parent term observations from the builder's layer reads.

    This is deliberately not a persistent proof. It is accepted only by the
    trusted in-process fast validator and is bound to the exact parent View.
    """

    parent_view_id: str
    parent_view_manifest_sha256: str
    search_view_recipe_hash: str
    parent_total_chunks: int
    contributions: tuple[TokenContribution, ...] = field(repr=False)

    def __post_init__(self) -> None:
        validate_sha256(self.parent_view_id, "parent_view_id")
        validate_sha256(
            self.parent_view_manifest_sha256,
            "parent_view_manifest_sha256",
        )
        validate_sha256(self.search_view_recipe_hash, "search_view_recipe_hash")
        if (
            isinstance(self.parent_total_chunks, bool)
            or not isinstance(self.parent_total_chunks, int)
            or not 0 <= self.parent_total_chunks <= MAX_U64
        ):
            raise ValueError("parent_total_chunks must be a u64")
        if isinstance(self.contributions, (str, bytes, bytearray)):
            raise TypeError("contributions must contain TokenContribution values")
        try:
            values = tuple(self.contributions)
        except TypeError as exc:
            raise TypeError(
                "contributions must contain TokenContribution values"
            ) from exc
        previous: bytes | None = None
        for value in values:
            if not isinstance(value, TokenContribution):
                raise TypeError(
                    "contributions must contain TokenContribution values"
                )
            token_bytes = value.token.encode("utf-8")
            if previous is not None and token_bytes <= previous:
                raise ValueError("contributions must be strictly token-sorted")
            previous = token_bytes
        object.__setattr__(self, "contributions", values)


__all__ = ["ParentDfWitness"]