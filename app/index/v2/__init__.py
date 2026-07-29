"""Deterministic PageIndex v2 shadow-generation primitives."""

from .canonical import canonical_bytes, canonical_hash, write_json_atomic
from .catalog import DocumentSource, discover_documents, fingerprint_document
from .ids import make_doc_key, make_node_key, normalize_relative_path
from .models import CompilerRecipe, SegmentRecipe

__all__ = [
    "CompilerRecipe",
    "DocumentSource",
    "SegmentRecipe",
    "canonical_bytes",
    "canonical_hash",
    "discover_documents",
    "fingerprint_document",
    "make_doc_key",
    "make_node_key",
    "normalize_relative_path",
    "write_json_atomic",
]
