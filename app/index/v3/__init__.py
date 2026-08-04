"""Incremental base-and-delta search index primitives.

The package root is intentionally import-light. Public names remain
backward-compatible, but their defining modules are imported only when the
name is first requested (PEP 562). This keeps fresh no-op workers from
loading builders, the reader, or the validator they never execute.
"""

from __future__ import annotations

from importlib import import_module
from typing import Final


_EXPORTS: Final[dict[str, str]] = {}


def _exports(module: str, *names: str) -> None:
    for name in names:
        if name in _EXPORTS:  # pragma: no cover - import-time invariant
            raise RuntimeError(f"duplicate PageIndex v3 export: {name}")
        _EXPORTS[name] = module


_exports(
    ".models",
    "ChunkRef", "CompactionPolicy", "GenerationRecipe", "LayerPosting",
    "LegacyExportRecipe", "SearchPosting", "SearchViewRecipe",
    "SegmentSummary", "TokenSummary", "ViewPin", "logical_generation_core",
    "logical_generation_id", "make_doc_uid", "validate_doc_key",
    "validate_sha256",
)
_exports(".source_diff", "SegmentChangeSet", "diff_segment_inputs")
_exports(
    ".segment_projection", "ChunkMetric", "DocumentProjection", "SegmentProjection",
    "SegmentProjector",
)
_exports(
    ".summary_store", "StoredSummaryRef", "SummaryStoreError",
    "load_summary", "put_summary",
)
_exports(
    ".layer_codec", "LayerCodecError", "LayerDocument", "PostingLayerReader",
    "PostingLayerReceipt", "TermRecord", "TokenContribution",
    "write_posting_layer",
)
_exports(
    ".layer_runs", "LayerRunError", "StagedLayerBuilder",
    "build_sorted_layer",
)
_exports(
    ".generation", "LogicalGenerationError", "LogicalGenerationReceipt",
    "build_logical_generation", "validate_logical_generation_inputs",
    "validate_logical_generation_manifest",
)
_exports(".statistics", "CorpusTotals", "TokenDfDelta", "token_df_deltas")
_exports(
    ".view_store", "BaseObjectReceipt", "SearchViewReceipt",
    "ViewDocumentOwner", "ViewStoreConflictError", "ViewStoreError",
    "finalize_base_object", "finalize_search_view", "load_base_object",
    "load_search_view", "load_search_view_metadata", "load_view_documents",
    "write_base_candidate", "write_search_view_candidate",
)
_exports(".base_builder", "build_base_view")
_exports(
    ".delta_store", "DeltaObjectReceipt", "DeltaStoreConflictError",
    "DeltaStoreError", "DocumentReplacement", "StatisticsDelta",
    "finalize_delta_object", "load_delta_object",
    "load_delta_object_metadata", "write_delta_candidate",
)
_exports(
    ".delta_builder", "CompactionRecommendation", "DeltaBuildResult",
    "DeltaBuildWork", "build_delta_view",
)
_exports(
    ".validator", "validate_base_normal", "validate_delta_normal",
    "validate_generation_normal", "validate_view_normal",
)
_exports(
    ".reader", "DEFAULT_CHUNK_CACHE_BYTES", "PinnedSearchView",
    "PinnedSearchViewError",
)
_exports(
    ".protocol", "MAX_JSON_LINE_BYTES", "PROTOCOL_NAME", "PROTOCOL_VERSION",
    "BuildRequest", "BuildResult", "GenerationAttestation",
    "LegacyExportAttestation", "ParentAttestation", "ProtocolError",
    "ViewAttestation", "WorkerError", "WorkerMetrics", "decode_request_line",
    "decode_result_line", "encode_request_line", "encode_result_line",
)


__all__ = [
    "ChunkMetric",
    "DocumentProjection",
    "ChunkRef",
    "BaseObjectReceipt",
    "CompactionPolicy",
    "CorpusTotals",
    "GenerationRecipe",
    "LayerPosting",
    "LayerCodecError",
    "LayerDocument",
    "LayerRunError",
    "LogicalGenerationError",
    "LogicalGenerationReceipt",
    "LegacyExportRecipe",
    "SearchPosting",
    "SearchViewReceipt",
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
    "TokenDfDelta",
    "ViewPin",
    "ViewDocumentOwner",
    "ViewStoreConflictError",
    "ViewStoreError",
    "logical_generation_core",
    "logical_generation_id",
    "load_summary",
    "make_doc_uid",
    "put_summary",
    "write_posting_layer",
    "validate_doc_key",
    "diff_segment_inputs",
    "build_sorted_layer",
    "build_logical_generation",
    "build_base_view",
    "finalize_base_object",
    "finalize_search_view",
    "load_base_object",
    "load_search_view",
    "load_view_documents",
    "token_df_deltas",
    "validate_logical_generation_manifest",
    "validate_logical_generation_inputs",
    "validate_sha256",
    "write_base_candidate",
    "write_search_view_candidate",
    "CompactionRecommendation",
    "DeltaBuildResult",
    "DeltaBuildWork",
    "DeltaObjectReceipt",
    "DeltaStoreConflictError",
    "DeltaStoreError",
    "DocumentReplacement",
    "StatisticsDelta",
    "build_delta_view",
    "finalize_delta_object",
    "load_delta_object",
    "load_delta_object_metadata",
    "load_search_view_metadata",
    "write_delta_candidate",
    "validate_base_normal",
    "validate_delta_normal",
    "validate_generation_normal",
    "validate_view_normal",
    "DEFAULT_CHUNK_CACHE_BYTES",
    "PinnedSearchView",
    "PinnedSearchViewError",
    "MAX_JSON_LINE_BYTES",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "BuildRequest",
    "BuildResult",
    "GenerationAttestation",
    "LegacyExportAttestation",
    "ParentAttestation",
    "ProtocolError",
    "ViewAttestation",
    "WorkerError",
    "WorkerMetrics",
    "decode_request_line",
    "decode_result_line",
    "encode_request_line",
    "encode_result_line",
]


if set(__all__) != set(_EXPORTS):  # pragma: no cover - import-time invariant
    raise RuntimeError("PageIndex v3 lazy export table differs from __all__")


def __getattr__(name: str) -> object:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))