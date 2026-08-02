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
from .layer_codec import (
    LayerCodecError,
    LayerDocument,
    PostingLayerReader,
    PostingLayerReceipt,
    TermRecord,
    TokenContribution,
    write_posting_layer,
)
from .layer_runs import LayerRunError, StagedLayerBuilder, build_sorted_layer

__all__ = [
    "ChunkMetric",
    "ChunkRef",
    "CompactionPolicy",
    "GenerationRecipe",
    "LayerPosting",
    "LayerCodecError",
    "LayerDocument",
    "LayerRunError",
    "LegacyExportRecipe",
    "SearchPosting",
    "SearchViewRecipe",
    "SegmentChangeSet",
    "SegmentProjection",
    "SegmentProjector",
    "SegmentSummary",
    "StoredSummaryRef",
    "StagedLayerBuilder",
    "PostingLayerReader",
    "PostingLayerReceipt",
    "SummaryStoreError",
    "TokenSummary",
    "TermRecord",
    "TokenContribution",
    "ViewPin",
    "logical_generation_core",
    "logical_generation_id",
    "load_summary",
    "make_doc_uid",
    "put_summary",
    "write_posting_layer",
    "validate_doc_key",
    "diff_segment_inputs",
    "build_sorted_layer",
    "validate_sha256",
]
