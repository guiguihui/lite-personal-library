"""Immutable, content-addressed PageIndex v3 Base and Search View objects.

The logical Generation remains independent of this module.  A Base binds one
physical posting layer to an exact logical Generation, while a Search View
binds an ordered layer chain to its small statistics and document-owner
artifacts.  Candidate writers trust receipts produced in the same build pass;
loading an already-published object performs full artifact authentication.

Pre-existing symlinks/reparse points and observed directory-identity drift fail
closed. Hostile same-user directory replacement between individual syscalls is
outside this local-store threat boundary; atomic no-replace still prevents an
ordinary concurrent publisher from clobbering an existing identity.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.index.v2.artifacts import ArtifactRef, AtomicHashingSink, write_canonical_object
from app.index.v2.canonical import canonical_hash, iter_canonical_json

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


BASE_SCHEMA_VERSION = 1
VIEW_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1

BASE_MANIFEST_PATH = "manifest.json"
VIEW_MANIFEST_PATH = "manifest.json"
STATISTICS_PATH = "statistics.json"
DOCUMENTS_PATH = "documents.json"

_LAYER_PATHS = frozenset(
    {
        "layer-documents.json",
        "postings.piv",
        "chunks.pcv",
        "terms.jsonl",
        "terms.sidx.json",
    }
)
_BASE_FILES = _LAYER_PATHS | {BASE_MANIFEST_PATH}
_VIEW_FILES = frozenset({VIEW_MANIFEST_PATH, STATISTICS_PATH, DOCUMENTS_PATH})
_ARTIFACT_KEYS = {"relative_path", "sha256", "byte_size", "records"}
_STATISTICS_KEYS = {
    "documents",
    "total_chunks",
    "token_count",
    "title_length_sum",
    "breadcrumb_length_sum",
    "body_length_sum",
    "posting_count",
}
_OWNER_KEYS = {
    "doc_key",
    "segment_hash",
    "summary_sha256",
    "summary_bytes",
    "owner_layer_kind",
    "owner_layer_id",
    "doc_ordinal",
}
_RECIPE_KEYS = set(SearchViewRecipe().as_dict())
_BASE_MANIFEST_KEYS = {
    "artifact_kind",
    "schema_version",
    "base_id",
    "generation",
    "generation_manifest_sha256",
    "search_view_recipe",
    "search_view_recipe_hash",
    "layer",
    "statistics",
}
_VIEW_MANIFEST_KEYS = {
    "artifact_kind",
    "schema_version",
    "view_id",
    "generation",
    "generation_manifest_sha256",
    "search_view_recipe",
    "search_view_recipe_hash",
    "base_id",
    "delta_ids",
    "statistics_sha256",
    "documents_sha256",
    "artifacts",
}


class ViewStoreError(ValueError):
    """An immutable Base/View object violates its closed-world contract."""


class ViewStoreConflictError(ViewStoreError):
    """A final identity is occupied by different or invalid bytes."""

    candidate_retained = True


def _u64(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_U64
    ):
        raise ViewStoreError(f"{field} must be a u64 in [0, {MAX_U64}]")
    return value


def _strict_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ViewStoreError(f"{field} must be an object")
    return value


def _strict_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    keys = tuple(value)
    if not all(isinstance(key, str) for key in keys):
        raise ViewStoreError(f"{field} keys must be strings")
    if len(keys) != len(set(keys)):
        raise ViewStoreError(f"{field} contains duplicate keys")
    if set(keys) != expected:
        raise ViewStoreError(
            f"{field} must contain exactly {', '.join(sorted(expected))}"
        )


def _artifact_dict(reference: ArtifactRef) -> dict[str, object]:
    return {
        "relative_path": reference.relative_path,
        "sha256": reference.sha256,
        "byte_size": reference.byte_size,
        "records": reference.records,
    }


def _validate_artifact(
    reference: object,
    expected_path: str,
    field: str,
    *,
    expected_records: int | None = None,
) -> ArtifactRef:
    if not isinstance(reference, ArtifactRef):
        raise TypeError(f"{field} must be an ArtifactRef")
    if reference.relative_path != expected_path:
        raise ViewStoreError(f"{field} path must be {expected_path!r}")
    _u64(reference.byte_size, f"{field}.byte_size")
    if reference.records is None:
        raise ViewStoreError(f"{field}.records must be attested")
    records = _u64(reference.records, f"{field}.records")
    if expected_records is not None and records != expected_records:
        raise ViewStoreError(
            f"{field}.records must equal {expected_records}, got {records}"
        )
    return reference


def _artifact_from_dict(value: object, expected_path: str, field: str) -> ArtifactRef:
    raw = _strict_mapping(value, field)
    _strict_keys(raw, _ARTIFACT_KEYS, field)
    try:
        reference = ArtifactRef(
            relative_path=raw["relative_path"],  # type: ignore[arg-type]
            sha256=raw["sha256"],  # type: ignore[arg-type]
            byte_size=raw["byte_size"],  # type: ignore[arg-type]
            records=raw["records"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise ViewStoreError(f"invalid {field}: {exc}") from exc
    return _validate_artifact(reference, expected_path, field)


def _recipe_from_dict(value: object) -> SearchViewRecipe:
    raw = _strict_mapping(value, "search_view_recipe")
    _strict_keys(raw, _RECIPE_KEYS, "search_view_recipe")
    copied = dict(raw)
    try:
        recipe = SearchViewRecipe(**copied)
    except (TypeError, ValueError) as exc:
        raise ViewStoreError(f"invalid search_view_recipe: {exc}") from exc
    if recipe.as_dict() != copied:
        raise ViewStoreError("search_view_recipe is not normalized")
    return recipe


def _totals_from_dict(value: object) -> CorpusTotals:
    raw = _strict_mapping(value, "statistics")
    _strict_keys(raw, _STATISTICS_KEYS, "statistics")
    try:
        return CorpusTotals(**dict(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ViewStoreError(f"invalid statistics: {exc}") from exc


def _base_core(
    *,
    generation: str,
    generation_manifest_sha256: str,
    search_view_recipe_hash: str,
    layer: PostingLayerReceipt,
    statistics: CorpusTotals,
) -> dict[str, object]:
    return {
        "artifact_kind": "search_base",
        "schema_version": BASE_SCHEMA_VERSION,
        "generation": generation,
        "generation_manifest_sha256": generation_manifest_sha256,
        "search_view_recipe_hash": search_view_recipe_hash,
        "layer": layer.as_dict(),
        "statistics": statistics.as_dict(),
    }


def _view_core(
    *,
    generation: str,
    generation_manifest_sha256: str,
    search_view_recipe_hash: str,
    base_id: str,
    delta_ids: tuple[str, ...],
    statistics_sha256: str,
    documents_sha256: str,
) -> dict[str, object]:
    return {
        "artifact_kind": "search_view",
        "schema_version": VIEW_SCHEMA_VERSION,
        "generation": generation,
        "generation_manifest_sha256": generation_manifest_sha256,
        "search_view_recipe_hash": search_view_recipe_hash,
        "base_id": base_id,
        "delta_ids": list(delta_ids),
        "statistics_sha256": statistics_sha256,
        "documents_sha256": documents_sha256,
    }


def _validate_delta_ids(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("delta_ids must be an iterable of full SHA-256 digests")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError("delta_ids must be iterable") from exc
    for position, value in enumerate(result):
        validate_sha256(value, f"delta_ids[{position}]")
    if len(result) != len(set(result)):
        raise ViewStoreError("delta_ids must be unique and chronologically ordered")
    return result


@dataclass(frozen=True, slots=True)
class ViewDocumentOwner:
    """One active document's newest physical owner."""

    doc_key: str
    segment_hash: str
    summary_sha256: str
    summary_bytes: int
    owner_layer_kind: Literal["base", "delta"]
    owner_layer_id: str
    doc_ordinal: int

    def __post_init__(self) -> None:
        validate_doc_key(self.doc_key)
        validate_sha256(self.segment_hash, "segment_hash")
        validate_sha256(self.summary_sha256, "summary_sha256")
        _u64(self.summary_bytes, "summary_bytes")
        if not isinstance(self.owner_layer_kind, str) or self.owner_layer_kind not in {
            "base",
            "delta",
        }:
            raise ValueError("owner_layer_kind must be 'base' or 'delta'")
        validate_sha256(self.owner_layer_id, "owner_layer_id")
        _u64(self.doc_ordinal, "doc_ordinal")

    def as_dict(self) -> dict[str, object]:
        return {
            "doc_key": self.doc_key,
            "segment_hash": self.segment_hash,
            "summary_sha256": self.summary_sha256,
            "summary_bytes": self.summary_bytes,
            "owner_layer_kind": self.owner_layer_kind,
            "owner_layer_id": self.owner_layer_id,
            "doc_ordinal": self.doc_ordinal,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ViewDocumentOwner":
        raw = _strict_mapping(value, "document owner")
        _strict_keys(raw, _OWNER_KEYS, "document owner")
        try:
            return cls(**dict(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ViewStoreError(f"invalid document owner: {exc}") from exc


@dataclass(frozen=True, slots=True)
class BaseObjectReceipt:
    """Compact receipt for one immutable full-base posting object."""

    root: Path
    base_id: str
    generation: str
    generation_manifest_sha256: str
    search_view_recipe_hash: str
    manifest_ref: ArtifactRef
    layer: PostingLayerReceipt
    statistics: CorpusTotals
    schema_version: int = RECEIPT_SCHEMA_VERSION
    artifact_kind: str = "search_base_receipt"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported Base receipt schema_version")
        if self.artifact_kind != "search_base_receipt":
            raise ValueError("unsupported Base receipt artifact_kind")
        validate_sha256(self.base_id, "base_id")
        validate_sha256(self.generation, "generation")
        validate_sha256(
            self.generation_manifest_sha256, "generation_manifest_sha256"
        )
        validate_sha256(self.search_view_recipe_hash, "search_view_recipe_hash")
        _validate_artifact(
            self.manifest_ref,
            BASE_MANIFEST_PATH,
            "manifest_ref",
            expected_records=1,
        )
        if not isinstance(self.layer, PostingLayerReceipt):
            raise TypeError("layer must be a PostingLayerReceipt")
        if self.layer.root != self.root:
            raise ValueError("layer.root must equal Base receipt root")
        if self.layer.layer_kind != "base":
            raise ValueError("Base receipt requires a base posting layer")
        if self.layer.search_view_recipe_hash != self.search_view_recipe_hash:
            raise ValueError("Base/layer SearchViewRecipe hashes differ")
        if not isinstance(self.statistics, CorpusTotals):
            raise TypeError("statistics must be CorpusTotals")
        if self.layer.document_count != self.statistics.documents:
            raise ValueError("Base layer/statistics document counts differ")
        if self.layer.chunk_count != self.statistics.total_chunks:
            raise ValueError("Base layer/statistics chunk counts differ")
        if self.layer.term_count != self.statistics.token_count:
            raise ValueError("Base layer/statistics token counts differ")
        expected = canonical_hash(
            _base_core(
                generation=self.generation,
                generation_manifest_sha256=self.generation_manifest_sha256,
                search_view_recipe_hash=self.search_view_recipe_hash,
                layer=self.layer,
                statistics=self.statistics,
            )
        )
        if self.base_id != expected:
            raise ValueError("base_id does not match the Base identity core")

    def attestation_dict(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "schema_version": self.schema_version,
            "base_id": self.base_id,
            "generation": self.generation,
            "generation_manifest_sha256": self.generation_manifest_sha256,
            "search_view_recipe_hash": self.search_view_recipe_hash,
            "manifest": _artifact_dict(self.manifest_ref),
            "layer": self.layer.as_dict(),
            "statistics": self.statistics.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class SearchViewReceipt:
    """Compact receipt for a Search View; the owner map remains on disk."""

    root: Path
    view_id: str
    generation: str
    generation_manifest_sha256: str
    search_view_recipe_hash: str
    base_id: str
    delta_ids: tuple[str, ...]
    manifest_ref: ArtifactRef
    statistics_ref: ArtifactRef
    documents_ref: ArtifactRef
    schema_version: int = RECEIPT_SCHEMA_VERSION
    artifact_kind: str = "search_view_receipt"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported Search View receipt schema_version")
        if self.artifact_kind != "search_view_receipt":
            raise ValueError("unsupported Search View receipt artifact_kind")
        validate_sha256(self.view_id, "view_id")
        validate_sha256(self.generation, "generation")
        validate_sha256(
            self.generation_manifest_sha256, "generation_manifest_sha256"
        )
        validate_sha256(self.search_view_recipe_hash, "search_view_recipe_hash")
        validate_sha256(self.base_id, "base_id")
        deltas = _validate_delta_ids(self.delta_ids)
        object.__setattr__(self, "delta_ids", deltas)
        _validate_artifact(
            self.manifest_ref,
            VIEW_MANIFEST_PATH,
            "manifest_ref",
            expected_records=1,
        )
        _validate_artifact(
            self.statistics_ref,
            STATISTICS_PATH,
            "statistics_ref",
            expected_records=1,
        )
        _validate_artifact(self.documents_ref, DOCUMENTS_PATH, "documents_ref")
        expected = canonical_hash(
            _view_core(
                generation=self.generation,
                generation_manifest_sha256=self.generation_manifest_sha256,
                search_view_recipe_hash=self.search_view_recipe_hash,
                base_id=self.base_id,
                delta_ids=deltas,
                statistics_sha256=self.statistics_ref.sha256,
                documents_sha256=self.documents_ref.sha256,
            )
        )
        if self.view_id != expected:
            raise ValueError("view_id does not match the Search View identity core")

    def attestation_dict(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "schema_version": self.schema_version,
            "view_id": self.view_id,
            "generation": self.generation,
            "generation_manifest_sha256": self.generation_manifest_sha256,
            "search_view_recipe_hash": self.search_view_recipe_hash,
            "base_id": self.base_id,
            "delta_ids": list(self.delta_ids),
            "manifest": _artifact_dict(self.manifest_ref),
            "statistics": _artifact_dict(self.statistics_ref),
            "documents": _artifact_dict(self.documents_ref),
        }


def _metadata_is_link(metadata: os.stat_result) -> bool:
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_mask)


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ViewStoreError(f"cannot inspect directory {path}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or _metadata_is_link(metadata):
        raise ViewStoreError(f"object root must be a plain directory: {path}")
    return metadata.st_dev, metadata.st_ino


def _assert_directory_identity(path: Path, identity: tuple[int, int]) -> None:
    if _directory_identity(path) != identity:
        raise ViewStoreError(f"directory identity changed while operating: {path}")


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _require_plain_directory_chain(path: Path) -> Path:
    """Reject every symlink/reparse point from path through its root."""

    absolute = _absolute_path(path)
    for component in (absolute, *absolute.parents):
        try:
            metadata = component.lstat()
        except OSError as exc:
            raise ViewStoreError(
                f"cannot inspect object-store directory ancestor: {component}"
            ) from exc
        if _metadata_is_link(metadata):
            raise ViewStoreError(
                f"object-store path must not traverse a symlink or junction: "
                f"{component}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise ViewStoreError(
                f"object-store path ancestor is not a directory: {component}"
            )
    return absolute


def _ensure_plain_directory(path: Path) -> tuple[Path, tuple[int, int]]:
    """Create missing directories one level at a time without trusting links."""

    absolute = _absolute_path(path)
    missing: list[Path] = []
    current = absolute
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            raise ViewStoreError("cannot find an existing object-store ancestor")
        missing.append(current)
        current = parent

    current = _require_plain_directory_chain(current)
    identity = _directory_identity(current)
    for directory in reversed(missing):
        _assert_directory_identity(current, identity)
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise ViewStoreError(
                f"cannot create object-store directory: {directory}"
            ) from exc
        created_identity = _directory_identity(directory)
        _assert_directory_identity(current, identity)
        current = directory
        identity = created_identity

    _require_plain_directory_chain(absolute)
    return absolute, identity


def _file_set(root: Path) -> set[str]:
    _directory_identity(root)
    result: set[str] = set()
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if _metadata_is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
                    raise ViewStoreError(
                        f"object contains a non-regular entry: {entry.name}"
                    )
                result.add(entry.name)
    except OSError as exc:
        raise ViewStoreError(f"cannot enumerate object root {root}: {exc}") from exc
    return result


def _require_file_set(root: Path, expected: frozenset[str]) -> None:
    observed = _file_set(root)
    if observed != set(expected):
        raise ViewStoreError(
            f"object file set mismatch: missing={sorted(expected - observed)!r}, "
            f"extra={sorted(observed - expected)!r}"
        )


def _open_regular(root: Path, relative_path: str):
    path = root / relative_path
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ViewStoreError(f"cannot inspect artifact {relative_path}: {exc}") from exc
    if _metadata_is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ViewStoreError(f"artifact must be a regular file: {relative_path}")
    try:
        if path.resolve(strict=True).parent != root.resolve(strict=True):
            raise ViewStoreError(f"artifact escapes object root: {relative_path}")
        return path.open("rb", buffering=0)
    except OSError as exc:
        raise ViewStoreError(f"cannot open artifact {relative_path}: {exc}") from exc


def _file_identity(stream: Any) -> tuple[int, int, int, int, int]:
    metadata = os.fstat(stream.fileno())
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_and_authenticate(
    root: Path,
    relative_path: str,
    expected: ArtifactRef | None = None,
) -> tuple[bytearray, ArtifactRef]:
    with _open_regular(root, relative_path) as stream:
        before = _file_identity(stream)
        if expected is not None and before[2] != expected.byte_size:
            raise ViewStoreError(f"{relative_path} byte size does not match receipt")
        digest = hashlib.sha256()
        captured = bytearray()
        while True:
            payload = stream.read(1024 * 1024)
            if not payload:
                break
            captured.extend(payload)
            digest.update(payload)
        after = _file_identity(stream)
    if before != after:
        raise ViewStoreError(f"{relative_path} changed while authenticating")
    actual = ArtifactRef(
        relative_path=relative_path,
        sha256=digest.hexdigest(),
        byte_size=after[2],
        records=None if expected is None else expected.records,
    )
    if expected is not None and (
        actual.sha256 != expected.sha256 or actual.byte_size != expected.byte_size
    ):
        raise ViewStoreError(f"{relative_path} digest does not match receipt")
    return captured, actual


def _strict_json(raw: bytes | bytearray, field: str) -> object:
    duplicates: list[str] = []

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ViewStoreError(f"invalid {field} JSON") from exc
    if duplicates:
        raise ViewStoreError(f"duplicate key in {field} JSON")
    position = 0
    view = memoryview(raw)
    try:
        for fragment in iter_canonical_json(value):
            payload = fragment.encode("utf-8")
            end = position + len(payload)
            if end > len(view) or view[position:end] != payload:
                raise ViewStoreError(f"noncanonical {field} JSON")
            position = end
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ViewStoreError):
            raise
        raise ViewStoreError(f"invalid canonical value in {field}") from exc
    if position != len(view):
        raise ViewStoreError(f"noncanonical {field} JSON")
    return value


def _read_canonical(
    root: Path,
    relative_path: str,
    expected: ArtifactRef | None = None,
) -> tuple[object, ArtifactRef]:
    raw, actual = _read_and_authenticate(root, relative_path, expected)
    return _strict_json(raw, relative_path), actual


def _check_recipe_hash(recipe: SearchViewRecipe, expected: str) -> None:
    actual = canonical_hash(recipe.as_dict())
    if actual != expected:
        raise ViewStoreError("search_view_recipe_hash mismatch")


def _check_base_relations(
    generation: LogicalGenerationReceipt,
    recipe: SearchViewRecipe,
    layer: PostingLayerReceipt,
    statistics: CorpusTotals,
    candidate: Path,
) -> str:
    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    if not isinstance(recipe, SearchViewRecipe):
        raise TypeError("recipe must be a SearchViewRecipe")
    if not isinstance(layer, PostingLayerReceipt):
        raise TypeError("layer must be a PostingLayerReceipt")
    if not isinstance(statistics, CorpusTotals):
        raise TypeError("statistics must be CorpusTotals")
    if layer.root != candidate:
        raise ViewStoreError("layer.root must equal candidate_dir")
    if layer.layer_kind != "base":
        raise ViewStoreError("write_base_candidate requires a base layer")
    recipe_hash = canonical_hash(recipe.as_dict())
    if layer.search_view_recipe_hash != recipe_hash:
        raise ViewStoreError("layer SearchViewRecipe does not match recipe")
    if generation.document_count != statistics.documents:
        raise ViewStoreError("Generation/statistics document counts differ")
    if layer.document_count != statistics.documents:
        raise ViewStoreError("layer/statistics document counts differ")
    if layer.chunk_count != statistics.total_chunks:
        raise ViewStoreError("layer/statistics chunk counts differ")
    if layer.term_count != statistics.token_count:
        raise ViewStoreError("layer/statistics token counts differ")
    return recipe_hash


def _verify_shallow_artifacts(root: Path, references: Iterable[ArtifactRef]) -> None:
    for reference in references:
        path = root / reference.relative_path
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ViewStoreError(
                f"cannot inspect candidate artifact {reference.relative_path}: {exc}"
            ) from exc
        if _metadata_is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ViewStoreError(
                f"candidate artifact must be regular: {reference.relative_path}"
            )
        if metadata.st_size != reference.byte_size:
            raise ViewStoreError(
                f"candidate artifact size differs: {reference.relative_path}"
            )


def write_base_candidate(
    candidate_dir: Path,
    *,
    generation: LogicalGenerationReceipt,
    recipe: SearchViewRecipe,
    layer: PostingLayerReceipt,
    statistics: CorpusTotals,
) -> BaseObjectReceipt:
    """Seal an already-streamed base layer without rereading its large files."""

    candidate = Path(candidate_dir)
    recipe_hash = _check_base_relations(
        generation, recipe, layer, statistics, candidate
    )
    if _file_set(candidate) != set(_LAYER_PATHS):
        raise ViewStoreError("base candidate must contain exactly the five layer files")
    _verify_shallow_artifacts(
        candidate,
        (
            layer.documents,
            layer.postings,
            layer.chunks,
            layer.terms,
            layer.sparse_index,
        ),
    )
    core = _base_core(
        generation=generation.generation_id,
        generation_manifest_sha256=generation.manifest_ref.sha256,
        search_view_recipe_hash=recipe_hash,
        layer=layer,
        statistics=statistics,
    )
    base_id = canonical_hash(core)
    manifest = {
        **core,
        "base_id": base_id,
        "search_view_recipe": recipe.as_dict(),
    }
    manifest_ref = write_canonical_object(
        candidate / BASE_MANIFEST_PATH,
        manifest,
        relative_path=BASE_MANIFEST_PATH,
        records=1,
    )
    return BaseObjectReceipt(
        root=candidate,
        base_id=base_id,
        generation=generation.generation_id,
        generation_manifest_sha256=generation.manifest_ref.sha256,
        search_view_recipe_hash=recipe_hash,
        manifest_ref=manifest_ref,
        layer=layer,
        statistics=statistics,
    )


def _write_json_value(sink: AtomicHashingSink, value: object) -> None:
    for fragment in iter_canonical_json(value):
        sink.write_text(fragment)


def _write_documents(
    path: Path,
    documents: Iterable[tuple[str, ViewDocumentOwner]],
    *,
    base_id: str,
    delta_ids: tuple[str, ...],
) -> ArtifactRef:
    if isinstance(documents, (str, bytes, bytearray)):
        raise TypeError("documents must be an iterable of owner pairs")
    try:
        iterator = iter(documents)
    except TypeError as exc:
        raise TypeError("documents must be iterable") from exc
    sink = AtomicHashingSink(path)
    records = 0
    previous: str | None = None
    seen_doc_keys: set[str] = set()
    seen_segments: set[str] = set()
    base_ordinals: set[int] = set()
    seen_routes: set[tuple[str, str, int]] = set()
    allowed_deltas = set(delta_ids)
    with sink:
        sink.write(b"{")
        for item in iterator:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ViewStoreError("documents items must be (doc_uid, owner) pairs")
            doc_uid, owner = item
            try:
                validate_sha256(doc_uid, "doc_uid")
            except (TypeError, ValueError) as exc:
                raise ViewStoreError(str(exc)) from exc
            if previous is not None and doc_uid <= previous:
                raise ViewStoreError(
                    "document owner keys must be strictly sorted and unique"
                )
            if not isinstance(owner, ViewDocumentOwner):
                raise TypeError("documents values must be ViewDocumentOwner values")
            if doc_uid != make_doc_uid(owner.doc_key):
                raise ViewStoreError("document owner key does not match doc_key")
            if owner.doc_key in seen_doc_keys:
                raise ViewStoreError("document owners contain duplicate doc_key")
            if owner.segment_hash in seen_segments:
                raise ViewStoreError("document owners contain duplicate segment_hash")
            route = (
                owner.owner_layer_kind,
                owner.owner_layer_id,
                owner.doc_ordinal,
            )
            if route in seen_routes:
                raise ViewStoreError("document owners contain duplicate physical route")
            if owner.owner_layer_kind == "base":
                if owner.owner_layer_id != base_id:
                    raise ViewStoreError("base owner_layer_id does not match base_id")
                if owner.doc_ordinal in base_ordinals:
                    raise ViewStoreError("base owner ordinals must be unique")
                base_ordinals.add(owner.doc_ordinal)
            elif owner.owner_layer_id not in allowed_deltas:
                raise ViewStoreError("delta owner_layer_id is absent from delta_ids")
            if records:
                sink.write(b",")
            _write_json_value(sink, doc_uid)
            sink.write(b":")
            _write_json_value(sink, owner.as_dict())
            previous = doc_uid
            seen_doc_keys.add(owner.doc_key)
            seen_segments.add(owner.segment_hash)
            seen_routes.add(route)
            records += 1
        sink.write(b"}")
    if not delta_ids and base_ordinals != set(range(records)):
        raise ViewStoreError("initial Base View owner ordinals must be compact")
    return ArtifactRef(DOCUMENTS_PATH, sink.sha256, sink.byte_size, records)


def _safe_cleanup_new_view(candidate: Path, primary: BaseException) -> None:
    try:
        if not os.path.lexists(candidate):
            return
        identity = _directory_identity(candidate)
        observed = _file_set(candidate)
        if not observed <= set(_VIEW_FILES):
            raise ViewStoreError("refusing to clean unexpected View candidate files")
        _assert_directory_identity(candidate, identity)
        for name in sorted(observed):
            (candidate / name).unlink()
        _assert_directory_identity(candidate, identity)
        candidate.rmdir()
    except BaseException as cleanup_error:
        add_note = getattr(primary, "add_note", None)
        if callable(add_note):
            add_note(f"View candidate cleanup also failed: {cleanup_error!r}")
        raise ViewStoreError("failed to safely clean View candidate") from cleanup_error


def _validate_incremental_parent(
    parent: SearchViewReceipt,
    *,
    base: BaseObjectReceipt,
    recipe_hash: str,
    generation: LogicalGenerationReceipt,
    delta_ids: tuple[str, ...],
) -> None:
    """Bind one incremental View to an authenticated finalized parent."""

    if not isinstance(parent, SearchViewReceipt):
        raise TypeError("parent must be a SearchViewReceipt or None")
    parent_root = _absolute_path(parent.root)
    if parent_root.name != parent.view_id or parent_root.parent.name != "views":
        raise ViewStoreError("incremental parent must be a finalized local View")
    pageindex_dir = parent_root.parent.parent
    authenticated = load_search_view_metadata(pageindex_dir, parent.view_id)
    if authenticated.root != parent_root:
        raise ViewStoreError("incremental parent root does not match finalized View")
    if authenticated.attestation_dict() != parent.attestation_dict():
        raise ViewStoreError("incremental parent receipt does not match local View")
    load_view_statistics(authenticated)
    if parent.base_id != base.base_id:
        raise ViewStoreError("incremental parent and Base IDs differ")
    if parent.search_view_recipe_hash != recipe_hash:
        raise ViewStoreError("incremental parent and View recipes differ")
    if generation.generation_id == parent.generation:
        raise ViewStoreError("incremental target Generation must advance")
    if generation.manifest_ref.sha256 == parent.generation_manifest_sha256:
        raise ViewStoreError("incremental target Generation manifest must advance")
    expected_length = len(parent.delta_ids) + 1
    if len(delta_ids) != expected_length or delta_ids[:-1] != parent.delta_ids:
        raise ViewStoreError(
            "incremental delta_ids must append exactly one ID to the parent chain"
        )


def write_search_view_candidate(
    candidate_dir: Path,
    *,
    generation: LogicalGenerationReceipt,
    recipe: SearchViewRecipe,
    base: BaseObjectReceipt,
    statistics: CorpusTotals,
    documents: Iterable[tuple[str, ViewDocumentOwner]],
    delta_ids: Iterable[str] = (),
    parent: SearchViewReceipt | None = None,
) -> SearchViewReceipt:
    """Stream the two View artifacts and seal their content-addressed manifest."""

    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    if not isinstance(recipe, SearchViewRecipe):
        raise TypeError("recipe must be a SearchViewRecipe")
    if not isinstance(base, BaseObjectReceipt):
        raise TypeError("base must be a BaseObjectReceipt")
    if not isinstance(statistics, CorpusTotals):
        raise TypeError("statistics must be CorpusTotals")
    recipe_hash = canonical_hash(recipe.as_dict())
    if base.search_view_recipe_hash != recipe_hash:
        raise ViewStoreError("Base and View SearchViewRecipe hashes differ")
    if statistics.documents != generation.document_count:
        raise ViewStoreError("View statistics/Generation document counts differ")
    deltas = _validate_delta_ids(delta_ids)
    if base.base_id in deltas:
        raise ViewStoreError("base_id cannot also appear in delta_ids")
    if parent is None:
        if deltas:
            raise ViewStoreError("incremental View requires a parent")
        if base.generation != generation.generation_id:
            raise ViewStoreError("initial Base and logical Generation IDs differ")
        if base.generation_manifest_sha256 != generation.manifest_ref.sha256:
            raise ViewStoreError("initial Base and logical Generation manifests differ")
        if statistics != base.statistics:
            raise ViewStoreError("initial Base View statistics must equal Base statistics")
    else:
        _validate_incremental_parent(
            parent,
            base=base,
            recipe_hash=recipe_hash,
            generation=generation,
            delta_ids=deltas,
        )

    candidate = Path(candidate_dir)
    if os.path.lexists(candidate):
        raise ViewStoreError("View candidate_dir must not already exist")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.mkdir()
    try:
        statistics_ref = write_canonical_object(
            candidate / STATISTICS_PATH,
            statistics.as_dict(),
            relative_path=STATISTICS_PATH,
            records=1,
        )
        documents_ref = _write_documents(
            candidate / DOCUMENTS_PATH,
            documents,
            base_id=base.base_id,
            delta_ids=deltas,
        )
        if documents_ref.records != statistics.documents:
            raise ViewStoreError("View documents/statistics counts differ")
        core = _view_core(
            generation=generation.generation_id,
            generation_manifest_sha256=generation.manifest_ref.sha256,
            search_view_recipe_hash=recipe_hash,
            base_id=base.base_id,
            delta_ids=deltas,
            statistics_sha256=statistics_ref.sha256,
            documents_sha256=documents_ref.sha256,
        )
        view_id = canonical_hash(core)
        manifest = {
            **core,
            "view_id": view_id,
            "search_view_recipe": recipe.as_dict(),
            "artifacts": {
                "statistics": _artifact_dict(statistics_ref),
                "documents": _artifact_dict(documents_ref),
            },
        }
        manifest_ref = write_canonical_object(
            candidate / VIEW_MANIFEST_PATH,
            manifest,
            relative_path=VIEW_MANIFEST_PATH,
            records=1,
        )
        return SearchViewReceipt(
            root=candidate,
            view_id=view_id,
            generation=generation.generation_id,
            generation_manifest_sha256=generation.manifest_ref.sha256,
            search_view_recipe_hash=recipe_hash,
            base_id=base.base_id,
            delta_ids=deltas,
            manifest_ref=manifest_ref,
            statistics_ref=statistics_ref,
            documents_ref=documents_ref,
        )
    except BaseException as exc:
        _safe_cleanup_new_view(candidate, exc)
        raise


def _parse_base_manifest(root: Path, value: object, manifest_ref: ArtifactRef) -> tuple[BaseObjectReceipt, SearchViewRecipe]:
    manifest = _strict_mapping(value, "Base manifest")
    _strict_keys(manifest, _BASE_MANIFEST_KEYS, "Base manifest")
    if manifest["artifact_kind"] != "search_base":
        raise ViewStoreError("unsupported Base artifact_kind")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ViewStoreError("unsupported Base schema_version")
    try:
        base_id = validate_sha256(manifest["base_id"], "base_id")
        generation = validate_sha256(manifest["generation"], "generation")
        generation_manifest_sha256 = validate_sha256(
            manifest["generation_manifest_sha256"], "generation_manifest_sha256"
        )
        recipe_hash = validate_sha256(
            manifest["search_view_recipe_hash"], "search_view_recipe_hash"
        )
    except (TypeError, ValueError) as exc:
        raise ViewStoreError(str(exc)) from exc
    recipe = _recipe_from_dict(manifest["search_view_recipe"])
    _check_recipe_hash(recipe, recipe_hash)
    try:
        layer = PostingLayerReceipt.from_dict(root, manifest["layer"])
    except (TypeError, ValueError) as exc:
        raise ViewStoreError(f"invalid Base layer receipt: {exc}") from exc
    if layer.as_dict() != manifest["layer"]:
        raise ViewStoreError("Base layer receipt is not normalized")
    statistics = _totals_from_dict(manifest["statistics"])
    receipt = BaseObjectReceipt(
        root=root,
        base_id=base_id,
        generation=generation,
        generation_manifest_sha256=generation_manifest_sha256,
        search_view_recipe_hash=recipe_hash,
        manifest_ref=manifest_ref,
        layer=layer,
        statistics=statistics,
    )
    return receipt, recipe


def load_base_object_metadata(
    pageindex_dir: Path,
    base_id: str,
) -> BaseObjectReceipt:
    """Authenticate a finalized Base contract without opening posting artifacts."""

    try:
        digest = validate_sha256(base_id, "base_id")
    except (TypeError, ValueError) as exc:
        raise ViewStoreError(str(exc)) from exc
    store = _absolute_path(Path(pageindex_dir))
    root = store / "objects" / "search" / "bases" / digest
    _require_plain_directory_chain(root)
    _require_file_set(root, _BASE_FILES)
    value, actual = _read_canonical(root, BASE_MANIFEST_PATH)
    manifest_ref = ArtifactRef(
        BASE_MANIFEST_PATH, actual.sha256, actual.byte_size, 1
    )
    receipt, _recipe = _parse_base_manifest(root, value, manifest_ref)
    if receipt.base_id != digest:
        raise ViewStoreError("Base directory name does not match manifest identity")
    return receipt


def load_base_object(pageindex_dir: Path, base_id: str) -> BaseObjectReceipt:
    """Load Base metadata, then explicitly deep-audit its posting layer."""

    receipt = load_base_object_metadata(pageindex_dir, base_id)
    manifest_value, _actual = _read_canonical(
        receipt.root, BASE_MANIFEST_PATH, receipt.manifest_ref
    )
    manifest = _strict_mapping(manifest_value, "Base manifest")
    recipe = _recipe_from_dict(manifest["search_view_recipe"])
    _check_recipe_hash(recipe, receipt.search_view_recipe_hash)
    try:
        with PostingLayerReader(receipt.layer, recipe=recipe) as reader:
            reader.audit()
    except (TypeError, ValueError, OSError) as exc:
        raise ViewStoreError(f"Base posting layer audit failed: {exc}") from exc
    return receipt


def _parse_owner_map(
    value: object,
    receipt: SearchViewReceipt,
) -> dict[str, ViewDocumentOwner]:
    raw = _strict_mapping(value, "documents")
    result: dict[str, ViewDocumentOwner] = {}
    seen_doc_keys: set[str] = set()
    seen_segments: set[str] = set()
    seen_routes: set[tuple[str, str, int]] = set()
    allowed_deltas = set(receipt.delta_ids)
    for doc_uid, raw_owner in raw.items():
        try:
            validate_sha256(doc_uid, "doc_uid")
        except (TypeError, ValueError) as exc:
            raise ViewStoreError(str(exc)) from exc
        owner = ViewDocumentOwner.from_dict(raw_owner)
        if doc_uid != make_doc_uid(owner.doc_key):
            raise ViewStoreError("document owner key does not match doc_key")
        if owner.doc_key in seen_doc_keys:
            raise ViewStoreError("document owners contain duplicate doc_key")
        if owner.segment_hash in seen_segments:
            raise ViewStoreError("document owners contain duplicate segment_hash")
        route = (owner.owner_layer_kind, owner.owner_layer_id, owner.doc_ordinal)
        if route in seen_routes:
            raise ViewStoreError("document owners contain duplicate physical route")
        if owner.owner_layer_kind == "base":
            if owner.owner_layer_id != receipt.base_id:
                raise ViewStoreError("base owner_layer_id does not match base_id")
        elif owner.owner_layer_id not in allowed_deltas:
            raise ViewStoreError("delta owner_layer_id is absent from delta_ids")
        result[doc_uid] = owner
        seen_doc_keys.add(owner.doc_key)
        seen_segments.add(owner.segment_hash)
        seen_routes.add(route)
    if receipt.documents_ref.records != len(result):
        raise ViewStoreError("documents record count does not match receipt")
    if not receipt.delta_ids:
        ordinals = {
            owner.doc_ordinal
            for owner in result.values()
            if owner.owner_layer_kind == "base"
        }
        if len(ordinals) != len(result) or ordinals != set(range(len(result))):
            raise ViewStoreError("initial Base View owner ordinals are not compact")
    return result


def load_view_documents(
    receipt: SearchViewReceipt,
) -> dict[str, ViewDocumentOwner]:
    """Authenticate and materialize the active owner map on explicit request."""

    if not isinstance(receipt, SearchViewReceipt):
        raise TypeError("receipt must be a SearchViewReceipt")
    value, _actual = _read_canonical(
        receipt.root, DOCUMENTS_PATH, receipt.documents_ref
    )
    return _parse_owner_map(value, receipt)


def load_view_statistics(receipt: SearchViewReceipt) -> CorpusTotals:
    """Authenticate only one View's canonical scalar-statistics artifact."""

    if not isinstance(receipt, SearchViewReceipt):
        raise TypeError("receipt must be a SearchViewReceipt")
    _require_plain_directory_chain(receipt.root)
    value, _actual = _read_canonical(
        receipt.root, STATISTICS_PATH, receipt.statistics_ref
    )
    statistics = _totals_from_dict(value)
    if statistics.documents != receipt.documents_ref.records:
        raise ViewStoreError("View statistics/documents counts differ")
    return statistics


def _parse_view_manifest(
    root: Path,
    value: object,
    manifest_ref: ArtifactRef,
) -> tuple[SearchViewReceipt, SearchViewRecipe]:
    manifest = _strict_mapping(value, "Search View manifest")
    _strict_keys(manifest, _VIEW_MANIFEST_KEYS, "Search View manifest")
    if manifest["artifact_kind"] != "search_view":
        raise ViewStoreError("unsupported Search View artifact_kind")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ViewStoreError("unsupported Search View schema_version")
    try:
        view_id = validate_sha256(manifest["view_id"], "view_id")
        generation = validate_sha256(manifest["generation"], "generation")
        generation_manifest_sha256 = validate_sha256(
            manifest["generation_manifest_sha256"], "generation_manifest_sha256"
        )
        recipe_hash = validate_sha256(
            manifest["search_view_recipe_hash"], "search_view_recipe_hash"
        )
        base_id = validate_sha256(manifest["base_id"], "base_id")
        statistics_sha256 = validate_sha256(
            manifest["statistics_sha256"], "statistics_sha256"
        )
        documents_sha256 = validate_sha256(
            manifest["documents_sha256"], "documents_sha256"
        )
    except (TypeError, ValueError) as exc:
        raise ViewStoreError(str(exc)) from exc
    raw_deltas = manifest["delta_ids"]
    if not isinstance(raw_deltas, list):
        raise ViewStoreError("delta_ids must be an array")
    deltas = _validate_delta_ids(raw_deltas)
    if base_id in deltas:
        raise ViewStoreError("base_id cannot also appear in delta_ids")
    recipe = _recipe_from_dict(manifest["search_view_recipe"])
    _check_recipe_hash(recipe, recipe_hash)
    artifacts = _strict_mapping(manifest["artifacts"], "artifacts")
    _strict_keys(artifacts, {"statistics", "documents"}, "artifacts")
    statistics_ref = _artifact_from_dict(
        artifacts["statistics"], STATISTICS_PATH, "statistics artifact"
    )
    documents_ref = _artifact_from_dict(
        artifacts["documents"], DOCUMENTS_PATH, "documents artifact"
    )
    _validate_artifact(statistics_ref, STATISTICS_PATH, "statistics artifact", expected_records=1)
    if statistics_ref.sha256 != statistics_sha256:
        raise ViewStoreError("statistics artifact is rebound from View core")
    if documents_ref.sha256 != documents_sha256:
        raise ViewStoreError("documents artifact is rebound from View core")
    receipt = SearchViewReceipt(
        root=root,
        view_id=view_id,
        generation=generation,
        generation_manifest_sha256=generation_manifest_sha256,
        search_view_recipe_hash=recipe_hash,
        base_id=base_id,
        delta_ids=deltas,
        manifest_ref=manifest_ref,
        statistics_ref=statistics_ref,
        documents_ref=documents_ref,
    )
    return receipt, recipe


def load_search_view_metadata(
    pageindex_dir: Path,
    view_id: str,
) -> SearchViewReceipt:
    """Authenticate the finalized View manifest and its compact receipt."""

    try:
        digest = validate_sha256(view_id, "view_id")
    except (TypeError, ValueError) as exc:
        raise ViewStoreError(str(exc)) from exc
    store = _absolute_path(Path(pageindex_dir))
    root = store / "views" / digest
    _require_plain_directory_chain(root)
    _require_file_set(root, _VIEW_FILES)
    manifest_value, actual = _read_canonical(root, VIEW_MANIFEST_PATH)
    manifest_ref = ArtifactRef(
        VIEW_MANIFEST_PATH, actual.sha256, actual.byte_size, 1
    )
    receipt, _recipe = _parse_view_manifest(root, manifest_value, manifest_ref)
    if receipt.view_id != digest:
        raise ViewStoreError("View directory name does not match manifest identity")
    return receipt


def load_search_view(pageindex_dir: Path, view_id: str) -> SearchViewReceipt:
    """Load and deeply authenticate one finalized Search View."""

    receipt = load_search_view_metadata(pageindex_dir, view_id)
    statistics = load_view_statistics(receipt)
    owners = load_view_documents(receipt)
    if statistics.documents != len(owners):
        raise ViewStoreError("View statistics/documents counts differ")
    return receipt


def _rename_no_replace(source: Path, target: Path) -> None:
    if os.name == "nt":
        source.rename(target)
        return
    if sys.platform != "linux":
        raise ViewStoreError(
            f"atomic no-replace directory publication is unsupported on {sys.platform}"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ViewStoreError("renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), target)
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise ViewStoreError("filesystem lacks atomic no-replace publication")
    raise OSError(error, os.strerror(error), target)


def _discard_candidate(
    candidate: Path,
    expected_files: frozenset[str],
    identity: tuple[int, int],
) -> None:
    _assert_directory_identity(candidate, identity)
    _require_file_set(candidate, expected_files)
    for name in sorted(expected_files):
        _assert_directory_identity(candidate, identity)
        path = candidate / name
        metadata = path.lstat()
        if _metadata_is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ViewStoreError(f"refusing to remove unsafe candidate file: {name}")
        path.unlink()
    _assert_directory_identity(candidate, identity)
    candidate.rmdir()


def _relocate_base(receipt: BaseObjectReceipt, root: Path) -> BaseObjectReceipt:
    layer = PostingLayerReceipt.from_dict(root, receipt.layer.as_dict())
    return BaseObjectReceipt(
        root=root,
        base_id=receipt.base_id,
        generation=receipt.generation,
        generation_manifest_sha256=receipt.generation_manifest_sha256,
        search_view_recipe_hash=receipt.search_view_recipe_hash,
        manifest_ref=receipt.manifest_ref,
        layer=layer,
        statistics=receipt.statistics,
    )


def _relocate_view(receipt: SearchViewReceipt, root: Path) -> SearchViewReceipt:
    return SearchViewReceipt(
        root=root,
        view_id=receipt.view_id,
        generation=receipt.generation,
        generation_manifest_sha256=receipt.generation_manifest_sha256,
        search_view_recipe_hash=receipt.search_view_recipe_hash,
        base_id=receipt.base_id,
        delta_ids=receipt.delta_ids,
        manifest_ref=receipt.manifest_ref,
        statistics_ref=receipt.statistics_ref,
        documents_ref=receipt.documents_ref,
    )


def _finalize(
    *,
    pageindex_dir: Path,
    receipt: BaseObjectReceipt | SearchViewReceipt,
    destination: Path,
    expected_files: frozenset[str],
    loader: Any,
) -> BaseObjectReceipt | SearchViewReceipt:
    candidate = receipt.root
    _require_plain_directory_chain(candidate)
    identity = _directory_identity(candidate)
    _require_file_set(candidate, expected_files)
    if candidate.resolve() == destination.resolve():
        raise ViewStoreError("candidate and final object directories must differ")
    destination_parent, parent_identity = _ensure_plain_directory(
        destination.parent
    )

    def assert_destination_parent() -> None:
        _require_plain_directory_chain(destination_parent)
        _assert_directory_identity(destination_parent, parent_identity)

    def reuse_existing() -> BaseObjectReceipt | SearchViewReceipt:
        assert_destination_parent()
        try:
            existing = loader(pageindex_dir, destination.name)
        except BaseException as exc:
            raise ViewStoreConflictError(
                "existing content-addressed object failed deep validation; "
                "candidate was retained"
            ) from exc
        if existing.attestation_dict() != receipt.attestation_dict():
            raise ViewStoreConflictError(
                "existing content-addressed object differs; candidate was retained"
            )
        _discard_candidate(candidate, expected_files, identity)
        return existing

    if os.path.lexists(destination):
        return reuse_existing()
    assert_destination_parent()
    try:
        _rename_no_replace(candidate, destination)
    except FileExistsError:
        return reuse_existing()
    if isinstance(receipt, BaseObjectReceipt):
        return _relocate_base(receipt, destination)
    return _relocate_view(receipt, destination)


def finalize_base_object(
    pageindex_dir: Path,
    receipt: BaseObjectReceipt,
) -> BaseObjectReceipt:
    """Publish a Base with no-clobber semantics or reuse an identical object."""

    if not isinstance(receipt, BaseObjectReceipt):
        raise TypeError("receipt must be a BaseObjectReceipt")
    store = _absolute_path(Path(pageindex_dir))
    destination = (
        store
        / "objects"
        / "search"
        / "bases"
        / receipt.base_id
    )
    result = _finalize(
        pageindex_dir=store,
        receipt=receipt,
        destination=destination,
        expected_files=_BASE_FILES,
        loader=load_base_object,
    )
    assert isinstance(result, BaseObjectReceipt)
    return result


def finalize_search_view(
    pageindex_dir: Path,
    receipt: SearchViewReceipt,
) -> SearchViewReceipt:
    """Publish a View with no-clobber semantics or reuse an identical object."""

    if not isinstance(receipt, SearchViewReceipt):
        raise TypeError("receipt must be a SearchViewReceipt")
    store = _absolute_path(Path(pageindex_dir))
    destination = store / "views" / receipt.view_id
    result = _finalize(
        pageindex_dir=store,
        receipt=receipt,
        destination=destination,
        expected_files=_VIEW_FILES,
        loader=load_search_view,
    )
    assert isinstance(result, SearchViewReceipt)
    return result


__all__ = [
    "BaseObjectReceipt",
    "SearchViewReceipt",
    "ViewDocumentOwner",
    "ViewStoreConflictError",
    "ViewStoreError",
    "finalize_base_object",
    "finalize_search_view",
    "load_base_object",
    "load_base_object_metadata",
    "load_search_view",
    "load_search_view_metadata",
    "load_view_documents",
    "load_view_statistics",
    "write_base_candidate",
    "write_search_view_candidate",
]
