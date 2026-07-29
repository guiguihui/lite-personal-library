"""Versioned immutable records shared by PageIndex v2 build stages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "COMPILER_SCHEMA_VERSION",
    "SEGMENT_SCHEMA_VERSION",
    "CompilerRecipe",
    "DocumentSource",
    "SegmentRecipe",
]


SEGMENT_SCHEMA_VERSION = 2
COMPILER_SCHEMA_VERSION = 2


def _require_int(name: str, value: int, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SegmentRecipe:
    """Every input that can change the bytes of a per-document Segment."""

    schema_version: int = SEGMENT_SCHEMA_VERSION
    tokenizer_version: str = "retrieval-tokenizer-v1"
    chunk_target_chars: int = 500
    chunk_overlap_chars: int = 100
    markdown_parser_version: str = "legacy-pageindex-markdown-v1"
    heading_split_version: str = "legacy-pageindex-heading-v1"
    summary_policy_version: str = "deterministic-excerpt-v1"
    summary_model_id: str = "none"
    summary_prompt_version: str = "none"

    def __post_init__(self) -> None:
        if self.schema_version != SEGMENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported Segment schema_version: {self.schema_version!r}"
            )
        _require_int("chunk_target_chars", self.chunk_target_chars, minimum=1)
        _require_int("chunk_overlap_chars", self.chunk_overlap_chars, minimum=0)
        if self.chunk_overlap_chars >= self.chunk_target_chars:
            raise ValueError("chunk_overlap_chars must be smaller than chunk_target_chars")
        for name in (
            "tokenizer_version",
            "markdown_parser_version",
            "heading_split_version",
            "summary_policy_version",
            "summary_model_id",
            "summary_prompt_version",
        ):
            _require_nonempty(name, getattr(self, name))
        supported = {
            "tokenizer_version": "retrieval-tokenizer-v1",
            "markdown_parser_version": "legacy-pageindex-markdown-v1",
            "heading_split_version": "legacy-pageindex-heading-v1",
            "summary_policy_version": "deterministic-excerpt-v1",
            "summary_model_id": "none",
            "summary_prompt_version": "none",
        }
        for name, expected in supported.items():
            actual = getattr(self, name)
            if actual != expected:
                raise ValueError(
                    f"unsupported {name}: {actual!r}; expected {expected!r}"
                )

    def as_dict(self) -> dict[str, object]:
        """Return the complete, canonical-hashable recipe payload."""
        return {
            "schema_version": self.schema_version,
            "tokenizer_version": self.tokenizer_version,
            "chunk_target_chars": self.chunk_target_chars,
            "chunk_overlap_chars": self.chunk_overlap_chars,
            "markdown_parser_version": self.markdown_parser_version,
            "heading_split_version": self.heading_split_version,
            "summary_policy_version": self.summary_policy_version,
            "summary_model_id": self.summary_model_id,
            "summary_prompt_version": self.summary_prompt_version,
        }


@dataclass(frozen=True, slots=True)
class CompilerRecipe:
    """Every input that can change compiled Generation artifacts."""

    schema_version: int = COMPILER_SCHEMA_VERSION
    field_postings_version: str = "field-tf-v1"
    body_df_min: int = 256
    body_df_ratio: float = 0.90
    compatibility_format_version: str = "legacy-pageindex-v1"
    ordering_version: str = "doc-node-chunk-v1"

    def __post_init__(self) -> None:
        if self.schema_version != COMPILER_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported Compiler schema_version: {self.schema_version!r}"
            )
        _require_int("body_df_min", self.body_df_min, minimum=1)
        if isinstance(self.body_df_ratio, bool) or not isinstance(
            self.body_df_ratio, (int, float)
        ):
            raise ValueError("body_df_ratio must be a number in the range [0, 1]")
        if not 0.0 <= float(self.body_df_ratio) <= 1.0:
            raise ValueError("body_df_ratio must be a number in the range [0, 1]")
        for name in (
            "field_postings_version",
            "compatibility_format_version",
            "ordering_version",
        ):
            _require_nonempty(name, getattr(self, name))
        supported = {
            "field_postings_version": "field-tf-v1",
            "compatibility_format_version": "legacy-pageindex-v1",
            "ordering_version": "doc-node-chunk-v1",
        }
        for name, expected in supported.items():
            actual = getattr(self, name)
            if actual != expected:
                raise ValueError(
                    f"unsupported {name}: {actual!r}; expected {expected!r}"
                )

    def as_dict(self) -> dict[str, object]:
        """Return the complete, canonical-hashable recipe payload."""
        return {
            "schema_version": self.schema_version,
            "field_postings_version": self.field_postings_version,
            "body_df_min": self.body_df_min,
            "body_df_ratio": float(self.body_df_ratio),
            "compatibility_format_version": self.compatibility_format_version,
            "ordering_version": self.ordering_version,
        }


@dataclass(frozen=True, slots=True)
class DocumentSource:
    """One logical document and its complete ordered Markdown inputs.

    ``root`` is the absolute content directory. ``files`` are normalized paths
    relative to that directory, making the record portable across machines.
    """

    doc_type: str
    slug: str
    doc_key: str
    root: Path
    files: tuple[Path, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "files", tuple(Path(path) for path in self.files))
        if not self.files:
            raise ValueError("a document source must contain at least one Markdown file")
