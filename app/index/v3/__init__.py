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
from .segment_projection import ChunkMetric, SegmentProjection, SegmentProjector
from .summary_store import (
    StoredSummaryRef,
    SummaryStoreError,
    load_summary,
    put_summary,
)

__all__ = [
    "ChunkMetric",
    "ChunkRef",
    "CompactionPolicy",
    "GenerationRecipe",
    "LayerPosting",
    "LegacyExportRecipe",
    "SearchPosting",
    "SearchViewRecipe",
    "SegmentChangeSet",
    "SegmentProjection",
    "SegmentProjector",
    "SegmentSummary",
    "StoredSummaryRef",
    "SummaryStoreError",
    "TokenSummary",
    "ViewPin",
    "logical_generation_core",
    "logical_generation_id",
    "load_summary",
    "make_doc_uid",
    "put_summary",
    "validate_doc_key",
    "diff_segment_inputs",
    "validate_sha256",
]
