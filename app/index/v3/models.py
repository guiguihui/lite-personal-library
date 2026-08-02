"""Strict immutable identities and value objects for PageIndex v3."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Iterable

from app.index.v2.canonical import canonical_hash
from app.index.v2.ids import make_doc_key
from app.index.v2.object_store import StoredSegmentRef


LOGICAL_GENERATION_SCHEMA_VERSION = 4
MODEL_SCHEMA_VERSION = 1
MAX_U64 = (1 << 64) - 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_exact(name: str, value: object, expected: str) -> None:
    if value != expected:
        raise ValueError(f"unsupported {name}: {value!r}; expected {expected!r}")


def _require_u64(name: str, value: object, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > MAX_U64
    ):
        raise ValueError(
            f"{name} must be an integer in the range [{minimum}, {MAX_U64}]"
        )
    return value


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


def validate_sha256(value: object, field: str = "sha256 digest") -> str:
    """Return a complete lowercase SHA-256 digest or fail closed."""

    if not isinstance(field, str) or not field:
        raise ValueError("field must be a non-empty string")
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a 64-character lowercase SHA-256 digest")
    return value


def validate_doc_key(value: object) -> str:
    """Validate an already-canonical, type-namespaced v2 document key."""

    if not isinstance(value, str):
        raise TypeError("doc_key must be a string")
    doc_type, separator, slug = value.partition(":")
    if not separator:
        raise ValueError("doc_key must be a canonical type-namespaced document key")
    try:
        expected = make_doc_key(doc_type, slug)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "doc_key must be a canonical type-namespaced document key"
        ) from exc
    if expected != value:
        raise ValueError("doc_key must be a canonical type-namespaced document key")
    return value


def make_doc_uid(doc_key: str) -> str:
    """Hash a canonical document key into a portable full-width identity."""

    validated = validate_doc_key(doc_key)
    return hashlib.sha256(validated.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GenerationRecipe:
    """Every setting that changes logical search semantics."""

    schema_version: int = MODEL_SCHEMA_VERSION
    artifact_kind: str = "logical_generation_recipe"
    field_postings_version: str = "raw-field-tf-v1"
    chunk_ref_version: str = "doc-uid-segment-local-v1"
    idf_policy_version: str = "effective-df-v1"
    body_df_min: int = 256
    body_df_ratio_numerator: int = 9
    body_df_ratio_denominator: int = 10

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_SCHEMA_VERSION or isinstance(
            self.schema_version, bool
        ):
            raise ValueError(
                f"unsupported schema_version: {self.schema_version!r}; "
                f"expected {MODEL_SCHEMA_VERSION}"
            )
        _require_exact("artifact_kind", self.artifact_kind, "logical_generation_recipe")
        _require_exact(
            "field_postings_version", self.field_postings_version, "raw-field-tf-v1"
        )
        _require_exact(
            "chunk_ref_version", self.chunk_ref_version, "doc-uid-segment-local-v1"
        )
        _require_exact("idf_policy_version", self.idf_policy_version, "effective-df-v1")
        _require_u64("body_df_min", self.body_df_min, minimum=1)
        numerator = _require_u64(
            "body_df_ratio_numerator", self.body_df_ratio_numerator
        )
        denominator = _require_u64(
            "body_df_ratio_denominator",
            self.body_df_ratio_denominator,
            minimum=1,
        )
        if numerator > denominator:
            raise ValueError(
                "body_df_ratio_numerator must not exceed "
                "body_df_ratio_denominator"
            )
        divisor = math.gcd(numerator, denominator)
        object.__setattr__(self, "body_df_ratio_numerator", numerator // divisor)
        object.__setattr__(self, "body_df_ratio_denominator", denominator // divisor)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_kind": self.artifact_kind,
            "field_postings_version": self.field_postings_version,
            "chunk_ref_version": self.chunk_ref_version,
            "idf_policy_version": self.idf_policy_version,
            "body_df_min": self.body_df_min,
            "body_df_ratio_numerator": self.body_df_ratio_numerator,
            "body_df_ratio_denominator": self.body_df_ratio_denominator,
        }


@dataclass(frozen=True, slots=True)
class SearchViewRecipe:
    """Byte and interpretation versions for immutable physical Search Views."""

    schema_version: int = MODEL_SCHEMA_VERSION
    artifact_kind: str = "search_view_recipe"
    posting_codec_version: str = "piv3-split-field-uvarint-v1"
    chunk_lengths_codec_version: str = "piv3-document-block-uvarint-v1"
    term_index_version: str = "canonical-jsonl-sparse-v1"
    replacement_version: str = "document-newest-wins-v1"
    owner_map_version: str = "layer-owner-map-v1"
    statistics_version: str = "scalar-plus-layer-delta-v1"

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_SCHEMA_VERSION or isinstance(
            self.schema_version, bool
        ):
            raise ValueError(
                f"unsupported schema_version: {self.schema_version!r}; "
                f"expected {MODEL_SCHEMA_VERSION}"
            )
        supported = {
            "artifact_kind": "search_view_recipe",
            "posting_codec_version": "piv3-split-field-uvarint-v1",
            "chunk_lengths_codec_version": "piv3-document-block-uvarint-v1",
            "term_index_version": "canonical-jsonl-sparse-v1",
            "replacement_version": "document-newest-wins-v1",
            "owner_map_version": "layer-owner-map-v1",
            "statistics_version": "scalar-plus-layer-delta-v1",
        }
        for name, expected in supported.items():
            _require_exact(name, getattr(self, name), expected)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_kind": self.artifact_kind,
            "posting_codec_version": self.posting_codec_version,
            "chunk_lengths_codec_version": self.chunk_lengths_codec_version,
            "term_index_version": self.term_index_version,
            "replacement_version": self.replacement_version,
            "owner_map_version": self.owner_map_version,
            "statistics_version": self.statistics_version,
        }


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """Operational recommendation thresholds excluded from content identity."""

    max_delta_layers: int = 32
    max_delta_bytes_numerator: int = 1
    max_delta_bytes_denominator: int = 5

    def __post_init__(self) -> None:
        _require_u64("max_delta_layers", self.max_delta_layers, minimum=1)
        numerator = _require_u64(
            "max_delta_bytes_numerator",
            self.max_delta_bytes_numerator,
            minimum=1,
        )
        denominator = _require_u64(
            "max_delta_bytes_denominator",
            self.max_delta_bytes_denominator,
            minimum=1,
        )
        if numerator > denominator:
            raise ValueError(
                "max_delta_bytes_numerator must not exceed "
                "max_delta_bytes_denominator"
            )
        divisor = math.gcd(numerator, denominator)
        object.__setattr__(self, "max_delta_bytes_numerator", numerator // divisor)
        object.__setattr__(self, "max_delta_bytes_denominator", denominator // divisor)

    def as_dict(self) -> dict[str, int]:
        return {
            "max_delta_layers": self.max_delta_layers,
            "max_delta_bytes_numerator": self.max_delta_bytes_numerator,
            "max_delta_bytes_denominator": self.max_delta_bytes_denominator,
        }


@dataclass(frozen=True, slots=True)
class LegacyExportRecipe:
    """Compatibility layout values isolated from logical and physical identity."""

    schema_version: int = MODEL_SCHEMA_VERSION
    artifact_kind: str = "legacy_export_recipe"
    compatibility_format_version: str = "legacy-pageindex-v1"
    ordering_version: str = "doc-node-chunk-v1"
    generation_layout_version: str = "manifest-input-proof-v1"

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_SCHEMA_VERSION or isinstance(
            self.schema_version, bool
        ):
            raise ValueError(
                f"unsupported schema_version: {self.schema_version!r}; "
                f"expected {MODEL_SCHEMA_VERSION}"
            )
        supported = {
            "artifact_kind": "legacy_export_recipe",
            "compatibility_format_version": "legacy-pageindex-v1",
            "ordering_version": "doc-node-chunk-v1",
            "generation_layout_version": "manifest-input-proof-v1",
        }
        for name, expected in supported.items():
            _require_exact(name, getattr(self, name), expected)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_kind": self.artifact_kind,
            "compatibility_format_version": self.compatibility_format_version,
            "ordering_version": self.ordering_version,
            "generation_layout_version": self.generation_layout_version,
        }


@dataclass(frozen=True, slots=True)
class ViewPin:
    generation: str
    view_id: str

    def __post_init__(self) -> None:
        validate_sha256(self.generation, "generation digest")
        validate_sha256(self.view_id, "view_id digest")

    def as_dict(self) -> dict[str, str]:
        return {"generation": self.generation, "view_id": self.view_id}


@dataclass(frozen=True, slots=True, order=True)
class ChunkRef:
    doc_uid: str
    segment_hash: str
    local_id: int

    def __post_init__(self) -> None:
        validate_sha256(self.doc_uid, "doc_uid digest")
        validate_sha256(self.segment_hash, "segment_hash digest")
        _require_u64("local_id", self.local_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "doc_uid": self.doc_uid,
            "segment_hash": self.segment_hash,
            "local_id": self.local_id,
        }


def _validate_field_tfs(
    title_tf: object,
    breadcrumb_tf: object,
    body_tf: object,
) -> None:
    title = _require_u64("title_tf", title_tf)
    breadcrumb = _require_u64("breadcrumb_tf", breadcrumb_tf)
    body = _require_u64("body_tf", body_tf)
    if title + breadcrumb + body == 0:
        raise ValueError("a posting must contain at least one positive field TF")


@dataclass(frozen=True, slots=True, order=True)
class SearchPosting:
    """A logical posting bound to a stable cross-layer ChunkRef."""

    token: str
    chunk_ref: ChunkRef
    title_tf: int
    breadcrumb_tf: int
    body_tf: int

    def __post_init__(self) -> None:
        _validate_token(self.token)
        if not isinstance(self.chunk_ref, ChunkRef):
            raise TypeError("chunk_ref must be a ChunkRef")
        _validate_field_tfs(self.title_tf, self.breadcrumb_tf, self.body_tf)

    @property
    def total_tf(self) -> int:
        return self.title_tf + self.breadcrumb_tf + self.body_tf

    @property
    def key(self) -> tuple[str, ChunkRef]:
        return (self.token, self.chunk_ref)

    def as_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "chunk_ref": self.chunk_ref.as_dict(),
            "title_tf": self.title_tf,
            "breadcrumb_tf": self.breadcrumb_tf,
            "body_tf": self.body_tf,
        }


@dataclass(frozen=True, slots=True, order=True)
class LayerPosting:
    """A compact physical posting whose document identity is layer-local."""

    token: str
    doc_ordinal: int
    local_id: int
    title_tf: int
    breadcrumb_tf: int
    body_tf: int

    def __post_init__(self) -> None:
        _validate_token(self.token)
        _require_u64("doc_ordinal", self.doc_ordinal)
        _require_u64("local_id", self.local_id)
        _validate_field_tfs(self.title_tf, self.breadcrumb_tf, self.body_tf)

    @property
    def total_tf(self) -> int:
        return self.title_tf + self.breadcrumb_tf + self.body_tf

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.token, self.doc_ordinal, self.local_id)


@dataclass(frozen=True, slots=True, order=True)
class TokenSummary:
    token: str
    df_any: int
    df_nonbody: int
    df_body: int

    def __post_init__(self) -> None:
        _validate_token(self.token)
        any_count = _require_u64("df_any", self.df_any, minimum=1)
        nonbody_count = _require_u64("df_nonbody", self.df_nonbody)
        body_count = _require_u64("df_body", self.df_body)
        if max(nonbody_count, body_count) > any_count:
            raise ValueError("df_nonbody and df_body must not exceed df_any")
        if any_count > nonbody_count + body_count:
            raise ValueError("df_any must be the union of nonbody and body rows")

    def as_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "df_any": self.df_any,
            "df_nonbody": self.df_nonbody,
            "df_body": self.df_body,
        }


@dataclass(frozen=True, slots=True)
class SegmentSummary:
    segment_hash: str
    doc_key: str
    doc_uid: str
    content_hash: str
    segment_recipe_hash: str
    chunk_count: int
    title_length_sum: int
    breadcrumb_length_sum: int
    body_length_sum: int
    posting_count: int
    tokens: tuple[TokenSummary, ...]
    schema_version: int = MODEL_SCHEMA_VERSION
    artifact_kind: str = "segment_search_summary"

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_SCHEMA_VERSION or isinstance(
            self.schema_version, bool
        ):
            raise ValueError(
                f"unsupported schema_version: {self.schema_version!r}; "
                f"expected {MODEL_SCHEMA_VERSION}"
            )
        _require_exact("artifact_kind", self.artifact_kind, "segment_search_summary")
        validate_sha256(self.segment_hash, "segment_hash digest")
        doc_key = validate_doc_key(self.doc_key)
        validate_sha256(self.doc_uid, "doc_uid digest")
        if self.doc_uid != make_doc_uid(doc_key):
            raise ValueError("doc_uid does not match doc_key")
        validate_sha256(self.content_hash, "content_hash digest")
        validate_sha256(self.segment_recipe_hash, "segment_recipe_hash digest")
        chunk_count = _require_u64("chunk_count", self.chunk_count)
        _require_u64("title_length_sum", self.title_length_sum)
        _require_u64("breadcrumb_length_sum", self.breadcrumb_length_sum)
        _require_u64("body_length_sum", self.body_length_sum)
        posting_count = _require_u64("posting_count", self.posting_count)

        if isinstance(self.tokens, (str, bytes, bytearray)):
            raise TypeError("tokens must be an iterable of TokenSummary values")
        try:
            tokens = tuple(self.tokens)
        except TypeError as exc:
            raise TypeError("tokens must be an iterable of TokenSummary values") from exc
        object.__setattr__(self, "tokens", tokens)

        previous: bytes | None = None
        observed_postings = 0
        observed_nonbody = 0
        observed_body = 0
        for token_summary in tokens:
            if not isinstance(token_summary, TokenSummary):
                raise TypeError("tokens must contain only TokenSummary values")
            encoded = token_summary.token.encode("utf-8")
            if previous is not None and encoded <= previous:
                raise ValueError("token summaries must be strictly sorted and unique")
            previous = encoded
            if (
                token_summary.df_any > chunk_count
                or token_summary.df_nonbody > chunk_count
                or token_summary.df_body > chunk_count
            ):
                raise ValueError("token document frequencies must not exceed chunk_count")
            observed_postings += token_summary.df_any
            observed_nonbody += token_summary.df_nonbody
            observed_body += token_summary.df_body
        if observed_postings != posting_count:
            raise ValueError("posting_count must equal the sum of token df_any values")
        if observed_nonbody > self.title_length_sum + self.breadcrumb_length_sum:
            raise ValueError(
                "nonbody document frequencies exceed title and breadcrumb lengths"
            )
        if observed_body > self.body_length_sum:
            raise ValueError("body document frequencies exceed body length")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "schema_version": self.schema_version,
            "segment_hash": self.segment_hash,
            "doc_key": self.doc_key,
            "doc_uid": self.doc_uid,
            "content_hash": self.content_hash,
            "segment_recipe_hash": self.segment_recipe_hash,
            "chunk_count": self.chunk_count,
            "field_length_sums": {
                "title": self.title_length_sum,
                "breadcrumb": self.breadcrumb_length_sum,
                "body": self.body_length_sum,
            },
            "posting_count": self.posting_count,
            "tokens": [token.as_dict() for token in self.tokens],
        }


def _validated_documents(refs: Iterable[StoredSegmentRef]) -> dict[str, str]:
    if isinstance(refs, (str, bytes, bytearray)):
        raise TypeError("refs must be an iterable of StoredSegmentRef values")
    try:
        values = tuple(refs)
    except TypeError as exc:
        raise TypeError("refs must be an iterable of StoredSegmentRef values") from exc

    validated: list[tuple[str, str]] = []
    seen: set[str] = set()
    seen_hashes: set[str] = set()
    for ref in values:
        if not isinstance(ref, StoredSegmentRef):
            raise TypeError("refs must contain only StoredSegmentRef values")
        doc_key = validate_doc_key(ref.doc_key)
        doc_type, slug = doc_key.split(":", 1)
        if ref.doc_type != doc_type or ref.slug != slug:
            raise ValueError(f"Segment ref document attestation mismatch for {doc_key}")
        segment_hash = validate_sha256(ref.segment_hash, "segment_hash digest")
        validate_sha256(ref.content_hash, "content_hash digest")
        validate_sha256(ref.segment_recipe_hash, "segment_recipe_hash digest")
        _require_u64("byte_size", ref.byte_size)
        if not isinstance(ref.path, Path):
            raise TypeError("StoredSegmentRef.path must be a pathlib.Path")
        if doc_key in seen:
            raise ValueError(f"duplicate document ref: {doc_key}")
        if segment_hash in seen_hashes:
            raise ValueError(
                f"segment_hash is attested to more than one document: {segment_hash}"
            )
        seen.add(doc_key)
        seen_hashes.add(segment_hash)
        validated.append((doc_key, segment_hash))
    return {doc_key: segment_hash for doc_key, segment_hash in sorted(validated)}


def logical_generation_core(
    refs: Iterable[StoredSegmentRef],
    recipe: GenerationRecipe,
) -> dict[str, object]:
    """Build the complete semantic identity core without touching the filesystem."""

    if not isinstance(recipe, GenerationRecipe):
        raise TypeError("recipe must be a GenerationRecipe")
    return {
        "artifact_kind": "logical_generation",
        "schema_version": LOGICAL_GENERATION_SCHEMA_VERSION,
        "generation_recipe_hash": canonical_hash(recipe.as_dict()),
        "documents": _validated_documents(refs),
    }


def logical_generation_id(
    refs: Iterable[StoredSegmentRef],
    recipe: GenerationRecipe,
) -> str:
    """Return the full SHA-256 identity for a logical Generation."""

    return canonical_hash(logical_generation_core(refs, recipe))


__all__ = [
    "LOGICAL_GENERATION_SCHEMA_VERSION",
    "MAX_U64",
    "MODEL_SCHEMA_VERSION",
    "ChunkRef",
    "CompactionPolicy",
    "GenerationRecipe",
    "LayerPosting",
    "LegacyExportRecipe",
    "SearchPosting",
    "SearchViewRecipe",
    "SegmentSummary",
    "TokenSummary",
    "ViewPin",
    "logical_generation_core",
    "logical_generation_id",
    "make_doc_uid",
    "validate_doc_key",
    "validate_sha256",
]
