"""Immutable PageIndex v3 document-replacement Delta objects.

A Delta owns postings and chunk metrics only for added or replaced documents.
Its signed statistics and replacement table advance one exact Search View.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from app.index.v2.artifacts import ArtifactRef, write_canonical_object
from app.index.v2.canonical import canonical_hash

from .generation import LogicalGenerationReceipt
from .layer_codec import PostingLayerReader, PostingLayerReceipt
from .models import (
    MAX_U64,
    SearchViewRecipe,
    make_doc_uid,
    validate_doc_key,
    validate_sha256,
)
from .statistics import CorpusTotals
from .view_store import (
    SearchViewReceipt,
    ViewStoreError,
    _absolute_path,
    _directory_identity,
    _discard_candidate,
    _ensure_plain_directory,
    _file_set,
    _read_canonical,
    _rename_no_replace,
    _require_file_set,
    _require_plain_directory_chain,
    _verify_shallow_artifacts,
)


DELTA_MANIFEST_PATH = "manifest.json"
_LAYER_PATHS = frozenset(
    {
        "layer-documents.json",
        "postings.piv",
        "chunks.pcv",
        "terms.jsonl",
        "terms.sidx.json",
    }
)
_DELTA_FILES = _LAYER_PATHS | {DELTA_MANIFEST_PATH}
_STAT_KEYS = {
    "documents",
    "total_chunks",
    "token_count",
    "title_length_sum",
    "breadcrumb_length_sum",
    "body_length_sum",
    "posting_count",
}
_REPLACEMENT_KEYS = {
    "doc_key",
    "doc_uid",
    "old_segment_hash",
    "old_summary_sha256",
    "old_summary_bytes",
    "new_segment_hash",
    "new_summary_sha256",
    "new_summary_bytes",
    "new_doc_ordinal",
}
_MANIFEST_KEYS = {
    "artifact_kind",
    "schema_version",
    "delta_id",
    "parent_view_id",
    "parent_view_manifest_sha256",
    "generation",
    "generation_manifest_sha256",
    "search_view_recipe",
    "search_view_recipe_hash",
    "statistics_delta",
    "layer",
    "replacements",
}
_LAYER_DOCUMENT_KEYS = {
    "doc_key",
    "doc_uid",
    "segment_hash",
    "chunk_count",
    "chunk_block_offset",
    "chunk_block_bytes",
    "chunk_block_sha256",
}


class DeltaStoreError(ValueError):
    """A Delta violates its closed-world identity or replacement contract."""


class DeltaStoreConflictError(DeltaStoreError):
    """A final Delta identity contains different or invalid bytes."""

    candidate_retained = True


def _signed(value: object, field: str) -> int:
    if (
        type(value) is not int
        or value < -MAX_U64
        or value > MAX_U64
    ):
        raise ValueError(f"{field} must be in [-{MAX_U64}, {MAX_U64}]")
    return value


def _u64(value: object, field: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_U64:
        raise ValueError(f"{field} must be a u64")
    return value


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeltaStoreError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise DeltaStoreError(f"{field} keys must be strings")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise DeltaStoreError(f"{field} has invalid keys")


@dataclass(frozen=True, slots=True)
class StatisticsDelta:
    documents: int
    total_chunks: int
    token_count: int
    title_length_sum: int
    breadcrumb_length_sum: int
    body_length_sum: int
    posting_count: int

    def __post_init__(self) -> None:
        for field in _STAT_KEYS:
            _signed(getattr(self, field), field)

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

    @classmethod
    def from_dict(cls, value: object) -> "StatisticsDelta":
        raw = _mapping(value, "statistics_delta")
        _keys(raw, _STAT_KEYS, "statistics_delta")
        try:
            return cls(**dict(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise DeltaStoreError(f"invalid statistics_delta: {exc}") from exc

    def apply(self, parent: CorpusTotals) -> CorpusTotals:
        if not isinstance(parent, CorpusTotals):
            raise TypeError("parent must be CorpusTotals")
        values: dict[str, int] = {}
        for field in _STAT_KEYS:
            after = getattr(parent, field) + getattr(self, field)
            if after < 0 or after > MAX_U64:
                raise DeltaStoreError(
                    f"statistics_delta.{field} underflows or overflows u64"
                )
            values[field] = after
        try:
            return CorpusTotals(**values)
        except (TypeError, ValueError) as exc:
            raise DeltaStoreError(
                f"statistics_delta produces invalid corpus totals: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class DocumentReplacement:
    doc_key: str
    doc_uid: str
    old_segment_hash: str | None
    old_summary_sha256: str | None
    old_summary_bytes: int | None
    new_segment_hash: str | None
    new_summary_sha256: str | None
    new_summary_bytes: int | None
    new_doc_ordinal: int | None

    def __post_init__(self) -> None:
        key = validate_doc_key(self.doc_key)
        validate_sha256(self.doc_uid, "doc_uid")
        if self.doc_uid != make_doc_uid(key):
            raise ValueError("replacement doc_uid does not match doc_key")
        old = (
            self.old_segment_hash,
            self.old_summary_sha256,
            self.old_summary_bytes,
        )
        new = (
            self.new_segment_hash,
            self.new_summary_sha256,
            self.new_summary_bytes,
            self.new_doc_ordinal,
        )
        old_full, old_null = all(x is not None for x in old), all(x is None for x in old)
        new_full, new_null = all(x is not None for x in new), all(x is None for x in new)
        if not (old_full or old_null):
            raise ValueError("replacement old fields must be complete or null")
        if not (new_full or new_null):
            raise ValueError("replacement new fields must be complete or null")
        if old_null and new_null:
            raise ValueError("replacement cannot have both sides null")
        if old_full:
            validate_sha256(self.old_segment_hash, "old_segment_hash")
            validate_sha256(self.old_summary_sha256, "old_summary_sha256")
            _u64(self.old_summary_bytes, "old_summary_bytes")
        if new_full:
            validate_sha256(self.new_segment_hash, "new_segment_hash")
            validate_sha256(self.new_summary_sha256, "new_summary_sha256")
            _u64(self.new_summary_bytes, "new_summary_bytes")
            _u64(self.new_doc_ordinal, "new_doc_ordinal")
        if old_full and new_full:
            if self.old_segment_hash == self.new_segment_hash:
                raise ValueError("edit replacement must change segment_hash")
            if self.old_summary_sha256 == self.new_summary_sha256:
                raise ValueError("edit replacement must change summary_sha256")

    @property
    def operation(self) -> str:
        if self.old_segment_hash is None:
            return "add"
        if self.new_segment_hash is None:
            return "delete"
        return "edit"

    def as_dict(self) -> dict[str, object]:
        return {
            "doc_key": self.doc_key,
            "doc_uid": self.doc_uid,
            "old_segment_hash": self.old_segment_hash,
            "old_summary_sha256": self.old_summary_sha256,
            "old_summary_bytes": self.old_summary_bytes,
            "new_segment_hash": self.new_segment_hash,
            "new_summary_sha256": self.new_summary_sha256,
            "new_summary_bytes": self.new_summary_bytes,
            "new_doc_ordinal": self.new_doc_ordinal,
        }

    @classmethod
    def from_dict(cls, value: object) -> "DocumentReplacement":
        raw = _mapping(value, "document replacement")
        _keys(raw, _REPLACEMENT_KEYS, "document replacement")
        try:
            return cls(**dict(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise DeltaStoreError(f"invalid document replacement: {exc}") from exc


def _replacements(
    source: Iterable[DocumentReplacement],
) -> tuple[DocumentReplacement, ...]:
    if isinstance(source, (str, bytes, bytearray)):
        raise TypeError("replacements must be an iterable")
    try:
        values = tuple(source)
    except TypeError as exc:
        raise TypeError("replacements must be an iterable") from exc
    if not values:
        raise DeltaStoreError("a Delta must contain at least one replacement")
    if not all(isinstance(item, DocumentReplacement) for item in values):
        raise TypeError("replacements must contain DocumentReplacement values")
    previous: bytes | None = None
    keys: set[str] = set()
    old_segments: set[str] = set()
    new_segments: set[str] = set()
    ordinal = 0
    for item in values:
        encoded = item.doc_uid.encode("utf-8")
        if previous is not None and encoded <= previous:
            raise DeltaStoreError("replacements must be sorted uniquely by doc_uid")
        if item.doc_key in keys:
            raise DeltaStoreError("duplicate replacement doc_key")
        if item.old_segment_hash is not None:
            if item.old_segment_hash in old_segments:
                raise DeltaStoreError("duplicate old replacement segment_hash")
            old_segments.add(item.old_segment_hash)
        if item.new_segment_hash is not None:
            if item.new_segment_hash in new_segments:
                raise DeltaStoreError("duplicate new replacement segment_hash")
            new_segments.add(item.new_segment_hash)
            if item.new_doc_ordinal != ordinal:
                raise DeltaStoreError("new replacement ordinals must be compact")
            ordinal += 1
        previous = encoded
        keys.add(item.doc_key)
    return values


def _core(
    *,
    parent_view_id: str,
    parent_view_manifest_sha256: str,
    generation: str,
    generation_manifest_sha256: str,
    search_view_recipe_hash: str,
    statistics_delta: StatisticsDelta,
    layer: PostingLayerReceipt,
    replacements: tuple[DocumentReplacement, ...],
) -> dict[str, object]:
    return {
        "artifact_kind": "search_delta",
        "schema_version": 1,
        "parent_view_id": parent_view_id,
        "parent_view_manifest_sha256": parent_view_manifest_sha256,
        "generation": generation,
        "generation_manifest_sha256": generation_manifest_sha256,
        "search_view_recipe_hash": search_view_recipe_hash,
        "statistics_delta": statistics_delta.as_dict(),
        "layer": layer.as_dict(),
        "replacements": [item.as_dict() for item in replacements],
    }


@dataclass(frozen=True, slots=True)
class DeltaObjectReceipt:
    root: Path
    delta_id: str
    parent_view_id: str
    parent_view_manifest_sha256: str
    generation: str
    generation_manifest_sha256: str
    search_view_recipe_hash: str
    manifest_ref: ArtifactRef
    layer: PostingLayerReceipt
    statistics_delta: StatisticsDelta
    replacements: tuple[DocumentReplacement, ...]
    schema_version: int = 1
    artifact_kind: str = "search_delta_receipt"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported Delta receipt schema_version")
        if self.artifact_kind != "search_delta_receipt":
            raise ValueError("unsupported Delta receipt artifact_kind")
        for field in (
            "delta_id",
            "parent_view_id",
            "parent_view_manifest_sha256",
            "generation",
            "generation_manifest_sha256",
            "search_view_recipe_hash",
        ):
            validate_sha256(getattr(self, field), field)
        if not isinstance(self.manifest_ref, ArtifactRef):
            raise TypeError("manifest_ref must be an ArtifactRef")
        if self.manifest_ref.relative_path != DELTA_MANIFEST_PATH:
            raise ValueError("Delta manifest path must be manifest.json")
        if self.manifest_ref.records != 1:
            raise ValueError("Delta manifest must attest one record")
        _u64(self.manifest_ref.byte_size, "manifest_ref.byte_size")
        if not isinstance(self.layer, PostingLayerReceipt):
            raise TypeError("layer must be a PostingLayerReceipt")
        if self.layer.root != self.root or self.layer.layer_kind != "delta":
            raise ValueError("Delta receipt requires its local delta layer")
        if self.layer.search_view_recipe_hash != self.search_view_recipe_hash:
            raise ValueError("Delta/layer recipe hashes differ")
        if not isinstance(self.statistics_delta, StatisticsDelta):
            raise TypeError("statistics_delta must be StatisticsDelta")
        values = _replacements(self.replacements)
        object.__setattr__(self, "replacements", values)
        adds = sum(item.operation == "add" for item in values)
        deletes = sum(item.operation == "delete" for item in values)
        new_count = sum(item.new_segment_hash is not None for item in values)
        if self.statistics_delta.documents != adds - deletes:
            raise ValueError("document statistics delta differs from replacements")
        if self.layer.document_count != new_count:
            raise ValueError("Delta layer/replacement document counts differ")
        expected = canonical_hash(
            _core(
                parent_view_id=self.parent_view_id,
                parent_view_manifest_sha256=self.parent_view_manifest_sha256,
                generation=self.generation,
                generation_manifest_sha256=self.generation_manifest_sha256,
                search_view_recipe_hash=self.search_view_recipe_hash,
                statistics_delta=self.statistics_delta,
                layer=self.layer,
                replacements=values,
            )
        )
        if self.delta_id != expected:
            raise ValueError("delta_id does not match the Delta identity core")

    def attestation_dict(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "schema_version": self.schema_version,
            "delta_id": self.delta_id,
            "parent_view_id": self.parent_view_id,
            "parent_view_manifest_sha256": self.parent_view_manifest_sha256,
            "generation": self.generation,
            "generation_manifest_sha256": self.generation_manifest_sha256,
            "search_view_recipe_hash": self.search_view_recipe_hash,
            "manifest": {
                "relative_path": self.manifest_ref.relative_path,
                "sha256": self.manifest_ref.sha256,
                "byte_size": self.manifest_ref.byte_size,
                "records": self.manifest_ref.records,
            },
            "layer": self.layer.as_dict(),
            "statistics_delta": self.statistics_delta.as_dict(),
            "replacements": [item.as_dict() for item in self.replacements],
        }


def _recipe(value: object) -> SearchViewRecipe:
    raw = _mapping(value, "search_view_recipe")
    _keys(raw, set(SearchViewRecipe().as_dict()), "search_view_recipe")
    copied = dict(raw)
    try:
        recipe = SearchViewRecipe(**copied)
    except (TypeError, ValueError) as exc:
        raise DeltaStoreError(f"invalid search_view_recipe: {exc}") from exc
    if recipe.as_dict() != copied:
        raise DeltaStoreError("search_view_recipe is not normalized")
    return recipe


def _bind_documents(
    root: Path,
    layer: PostingLayerReceipt,
    replacements: tuple[DocumentReplacement, ...],
) -> None:
    try:
        value, _ = _read_canonical(root, layer.documents.relative_path, layer.documents)
    except ViewStoreError as exc:
        raise DeltaStoreError(f"invalid Delta document table: {exc}") from exc
    raw = _mapping(value, "Delta document table")
    _keys(raw, {"artifact_kind", "schema_version", "documents"}, "Delta document table")
    if (
        raw["artifact_kind"] != "piv3_layer_documents"
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
    ):
        raise DeltaStoreError("unsupported Delta document table schema")
    documents = raw["documents"]
    if not isinstance(documents, list):
        raise DeltaStoreError("Delta documents must be an array")
    expected = [item for item in replacements if item.new_segment_hash is not None]
    if len(documents) != len(expected):
        raise DeltaStoreError("Delta document table/replacement counts differ")
    chunks = 0
    for ordinal, (raw_document, replacement) in enumerate(zip(documents, expected, strict=True)):
        document = _mapping(raw_document, "Delta layer document")
        _keys(document, _LAYER_DOCUMENT_KEYS, "Delta layer document")
        if replacement.new_doc_ordinal != ordinal:
            raise DeltaStoreError("replacement ordinal does not match document table")
        if (
            document["doc_key"] != replacement.doc_key
            or document["doc_uid"] != replacement.doc_uid
            or document["segment_hash"] != replacement.new_segment_hash
        ):
            raise DeltaStoreError("replacement does not match Delta document table")
        try:
            chunk_count = _u64(
                document["chunk_count"], "layer document chunk_count"
            )
        except (TypeError, ValueError) as exc:
            raise DeltaStoreError(f"invalid Delta layer document: {exc}") from exc
        chunks += chunk_count
        if chunks > MAX_U64:
            raise DeltaStoreError("Delta layer chunk count overflows u64")
    if chunks != layer.chunk_count:
        raise DeltaStoreError("Delta document table chunk count differs from layer")


def write_delta_candidate(
    candidate_dir: Path,
    *,
    parent: SearchViewReceipt,
    generation: LogicalGenerationReceipt,
    recipe: SearchViewRecipe,
    layer: PostingLayerReceipt,
    statistics_delta: StatisticsDelta,
    replacements: Iterable[DocumentReplacement],
) -> DeltaObjectReceipt:
    """Seal a streamed Delta without rereading postings or chunk blocks."""

    candidate = Path(candidate_dir)
    if not isinstance(parent, SearchViewReceipt):
        raise TypeError("parent must be a SearchViewReceipt")
    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    if not isinstance(recipe, SearchViewRecipe):
        raise TypeError("recipe must be a SearchViewRecipe")
    if not isinstance(layer, PostingLayerReceipt):
        raise TypeError("layer must be a PostingLayerReceipt")
    if not isinstance(statistics_delta, StatisticsDelta):
        raise TypeError("statistics_delta must be StatisticsDelta")
    values = _replacements(replacements)
    recipe_hash = canonical_hash(recipe.as_dict())
    if parent.search_view_recipe_hash != recipe_hash:
        raise DeltaStoreError("parent SearchViewRecipe does not match recipe")
    if generation.generation_id == parent.generation:
        raise DeltaStoreError("Delta target Generation must advance parent")
    if layer.root != candidate or layer.layer_kind != "delta":
        raise DeltaStoreError("candidate must own a delta posting layer")
    if layer.search_view_recipe_hash != recipe_hash:
        raise DeltaStoreError("layer SearchViewRecipe does not match recipe")
    adds = sum(item.operation == "add" for item in values)
    deletes = sum(item.operation == "delete" for item in values)
    new_count = sum(item.new_segment_hash is not None for item in values)
    if statistics_delta.documents != adds - deletes:
        raise DeltaStoreError("document statistics delta differs from replacements")
    if layer.document_count != new_count:
        raise DeltaStoreError("Delta layer/replacement document counts differ")
    expected_documents = parent.documents_ref.records + adds - deletes
    if expected_documents < 0 or generation.document_count != expected_documents:
        raise DeltaStoreError("target Generation count differs from replacements")
    try:
        if _file_set(candidate) != set(_LAYER_PATHS):
            raise DeltaStoreError("Delta candidate must contain exactly five layer files")
        _verify_shallow_artifacts(
            candidate,
            (layer.documents, layer.postings, layer.chunks, layer.terms, layer.sparse_index),
        )
    except ViewStoreError as exc:
        raise DeltaStoreError(str(exc)) from exc
    _bind_documents(candidate, layer, values)
    core = _core(
        parent_view_id=parent.view_id,
        parent_view_manifest_sha256=parent.manifest_ref.sha256,
        generation=generation.generation_id,
        generation_manifest_sha256=generation.manifest_ref.sha256,
        search_view_recipe_hash=recipe_hash,
        statistics_delta=statistics_delta,
        layer=layer,
        replacements=values,
    )
    delta_id = canonical_hash(core)
    manifest_ref = write_canonical_object(
        candidate / DELTA_MANIFEST_PATH,
        {**core, "delta_id": delta_id, "search_view_recipe": recipe.as_dict()},
        relative_path=DELTA_MANIFEST_PATH,
        records=1,
    )
    return DeltaObjectReceipt(
        candidate,
        delta_id,
        parent.view_id,
        parent.manifest_ref.sha256,
        generation.generation_id,
        generation.manifest_ref.sha256,
        recipe_hash,
        manifest_ref,
        layer,
        statistics_delta,
        values,
    )


def _parse_manifest(
    root: Path,
    value: object,
    manifest_ref: ArtifactRef,
) -> tuple[DeltaObjectReceipt, SearchViewRecipe]:
    manifest = _mapping(value, "Delta manifest")
    _keys(manifest, _MANIFEST_KEYS, "Delta manifest")
    if (
        manifest["artifact_kind"] != "search_delta"
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
    ):
        raise DeltaStoreError("unsupported Delta manifest schema")
    try:
        delta_id = validate_sha256(manifest["delta_id"], "delta_id")
        parent_view_id = validate_sha256(manifest["parent_view_id"], "parent_view_id")
        parent_manifest = validate_sha256(
            manifest["parent_view_manifest_sha256"], "parent_view_manifest_sha256"
        )
        generation = validate_sha256(manifest["generation"], "generation")
        generation_manifest = validate_sha256(
            manifest["generation_manifest_sha256"], "generation_manifest_sha256"
        )
        recipe_hash = validate_sha256(
            manifest["search_view_recipe_hash"], "search_view_recipe_hash"
        )
    except (TypeError, ValueError) as exc:
        raise DeltaStoreError(str(exc)) from exc
    recipe = _recipe(manifest["search_view_recipe"])
    if canonical_hash(recipe.as_dict()) != recipe_hash:
        raise DeltaStoreError("search_view_recipe_hash mismatch")
    try:
        layer = PostingLayerReceipt.from_dict(root, manifest["layer"])
    except (TypeError, ValueError) as exc:
        raise DeltaStoreError(f"invalid Delta layer: {exc}") from exc
    if layer.as_dict() != manifest["layer"]:
        raise DeltaStoreError("Delta layer is not normalized")
    statistics_delta = StatisticsDelta.from_dict(manifest["statistics_delta"])
    raw_replacements = manifest["replacements"]
    if not isinstance(raw_replacements, list):
        raise DeltaStoreError("Delta replacements must be an array")
    replacements = _replacements(
        DocumentReplacement.from_dict(item) for item in raw_replacements
    )
    try:
        receipt = DeltaObjectReceipt(
            root,
            delta_id,
            parent_view_id,
            parent_manifest,
            generation,
            generation_manifest,
            recipe_hash,
            manifest_ref,
            layer,
            statistics_delta,
            replacements,
        )
    except (TypeError, ValueError) as exc:
        raise DeltaStoreError(f"invalid Delta manifest identity: {exc}") from exc
    return receipt, recipe


def _load_metadata(
    pageindex_dir: Path,
    delta_id: str,
) -> tuple[DeltaObjectReceipt, SearchViewRecipe]:
    try:
        digest = validate_sha256(delta_id, "delta_id")
    except (TypeError, ValueError) as exc:
        raise DeltaStoreError(str(exc)) from exc
    root = _absolute_path(Path(pageindex_dir)) / "objects" / "search" / "deltas" / digest
    try:
        _require_plain_directory_chain(root)
        _require_file_set(root, _DELTA_FILES)
        value, actual = _read_canonical(root, DELTA_MANIFEST_PATH)
    except ViewStoreError as exc:
        raise DeltaStoreError(str(exc)) from exc
    receipt, recipe = _parse_manifest(
        root,
        value,
        ArtifactRef(DELTA_MANIFEST_PATH, actual.sha256, actual.byte_size, 1),
    )
    if receipt.delta_id != digest:
        raise DeltaStoreError("Delta directory name differs from identity")
    _bind_documents(root, receipt.layer, receipt.replacements)
    return receipt, recipe


def load_delta_object_metadata(pageindex_dir: Path, delta_id: str) -> DeltaObjectReceipt:
    """Authenticate metadata without scanning terms, postings, or chunks."""

    receipt, _ = _load_metadata(pageindex_dir, delta_id)
    return receipt


def load_delta_object(pageindex_dir: Path, delta_id: str) -> DeltaObjectReceipt:
    """Authenticate metadata and then explicitly deep-audit the layer."""

    receipt, recipe = _load_metadata(pageindex_dir, delta_id)
    try:
        with PostingLayerReader(receipt.layer, recipe=recipe) as reader:
            reader.audit()
    except (TypeError, ValueError, OSError) as exc:
        raise DeltaStoreError(f"Delta posting layer audit failed: {exc}") from exc
    return receipt


def _relocate(receipt: DeltaObjectReceipt, root: Path) -> DeltaObjectReceipt:
    return DeltaObjectReceipt(
        root,
        receipt.delta_id,
        receipt.parent_view_id,
        receipt.parent_view_manifest_sha256,
        receipt.generation,
        receipt.generation_manifest_sha256,
        receipt.search_view_recipe_hash,
        receipt.manifest_ref,
        PostingLayerReceipt.from_dict(root, receipt.layer.as_dict()),
        receipt.statistics_delta,
        receipt.replacements,
    )


def finalize_delta_object(
    pageindex_dir: Path,
    receipt: DeltaObjectReceipt,
) -> DeltaObjectReceipt:
    """Publish with atomic no-clobber semantics; first publish never audits."""

    if not isinstance(receipt, DeltaObjectReceipt):
        raise TypeError("receipt must be a DeltaObjectReceipt")
    candidate = receipt.root
    try:
        _require_plain_directory_chain(candidate)
        identity = _directory_identity(candidate)
        _require_file_set(candidate, _DELTA_FILES)
    except ViewStoreError as exc:
        raise DeltaStoreError(str(exc)) from exc
    store = _absolute_path(Path(pageindex_dir))
    destination = store / "objects" / "search" / "deltas" / receipt.delta_id
    if candidate.resolve() == destination.resolve():
        raise DeltaStoreError("candidate and final Delta directories must differ")
    try:
        destination_parent, parent_identity = _ensure_plain_directory(destination.parent)
    except ViewStoreError as exc:
        raise DeltaStoreError(str(exc)) from exc

    def assert_parent() -> None:
        try:
            _require_plain_directory_chain(destination_parent)
            if _directory_identity(destination_parent) != parent_identity:
                raise DeltaStoreError("Delta destination parent identity changed")
        except ViewStoreError as exc:
            raise DeltaStoreError(str(exc)) from exc

    def reuse() -> DeltaObjectReceipt:
        assert_parent()
        try:
            existing = load_delta_object(store, receipt.delta_id)
        except BaseException as exc:
            raise DeltaStoreConflictError(
                "existing Delta failed deep validation; candidate retained"
            ) from exc
        if existing.attestation_dict() != receipt.attestation_dict():
            raise DeltaStoreConflictError("existing Delta differs; candidate retained")
        try:
            _discard_candidate(candidate, _DELTA_FILES, identity)
        except ViewStoreError as exc:
            raise DeltaStoreError("failed to discard identical candidate") from exc
        return existing

    if os.path.lexists(destination):
        return reuse()
    assert_parent()
    try:
        _rename_no_replace(candidate, destination)
    except FileExistsError:
        return reuse()
    except ViewStoreError as exc:
        raise DeltaStoreError(str(exc)) from exc
    return _relocate(receipt, destination)


__all__ = [
    "DeltaObjectReceipt",
    "DeltaStoreConflictError",
    "DeltaStoreError",
    "DocumentReplacement",
    "StatisticsDelta",
    "finalize_delta_object",
    "load_delta_object",
    "load_delta_object_metadata",
    "write_delta_candidate",
]
