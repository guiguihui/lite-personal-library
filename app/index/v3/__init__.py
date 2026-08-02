"""Incremental base-and-delta search index primitives."""

from .models import (
    ChunkRef,
    CompactionPolicy,
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
from .source_diff import SegmentChangeSet, diff_segment_inputs

__all__ = [
    "ChunkRef",
    "CompactionPolicy",
    "GenerationRecipe",
    "LayerPosting",
    "LegacyExportRecipe",
    "SearchPosting",
    "SearchViewRecipe",
    "SegmentChangeSet",
    "SegmentSummary",
    "TokenSummary",
    "ViewPin",
    "logical_generation_core",
    "logical_generation_id",
    "make_doc_uid",
    "validate_doc_key",
    "diff_segment_inputs",
    "validate_sha256",
]
