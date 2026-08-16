"""Authenticated best-effort cache for reusable source metadata snapshots.

This cache is only an optimization hint inside PageIndex's existing local,
same-user trust boundary.  Its Generation bindings and canonical payload hash
detect stale or accidentally corrupted entries; they are not a cryptographic
proof against a hostile local user who can rewrite both the cache and source
metadata.  Callers must preserve the full source-hashing fallback whenever a
cache entry is absent or rejected.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.index.v2.canonical import canonical_bytes, canonical_hash, write_json_atomic
from app.index.v2.ids import normalize_relative_path
from app.index.v2.source_snapshot import (
    StableCatalogSnapshot,
    _prepare_sources,
    _sources_from_topology,
)

from .generation import LogicalGenerationReceipt
from .models import MAX_U64, validate_doc_key, validate_sha256


SOURCE_SNAPSHOT_CACHE_SCHEMA_VERSION = 1
SOURCE_SNAPSHOT_CACHE_MAX_BYTES = 32 * 1024 * 1024

_CACHE_DIRECTORY = Path("cache") / "source-snapshots"
_ARTIFACT_KIND = "local_source_snapshot_cache"
_MAX_ENTRIES = 1_000_000
_MAX_FILES_PER_SOURCE = 1_000_000
_MAX_TEXT_BYTES = 1024 * 1024
_MAX_SIGNED_64 = (1 << 63) - 1
_MIN_SIGNED_64 = -(1 << 63)

_ROOT_KEYS = {
    "artifact_kind",
    "schema_version",
    "binding",
    "snapshot",
    "payload_sha256",
}
_BINDING_KEYS = {
    "cache_key",
    "content_root",
    "generation",
    "generation_manifest_sha256",
    "input_proof_sha256",
    "generation_recipe_hash",
}
_SNAPSHOT_KEYS = {
    "proof",
    "directory_state",
    "topology",
    "file_state",
}
_DIRECTORY_KEYS = {"relative", "mtime_ns", "ctime_ns", "ino", "dev"}
_TOPOLOGY_KEYS = {"doc_key", "files"}
_FILE_KEYS = {
    "doc_key",
    "path",
    "size",
    "mtime_ns",
    "ctime_ns",
    "ino",
    "dev",
}


class _CacheFormatError(ValueError):
    """A local cache entry does not satisfy its closed schema."""


def _strict_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _CacheFormatError(f"{field} must be an object")
    return value


def _strict_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if not all(isinstance(key, str) for key in value):
        raise _CacheFormatError(f"{field} keys must be strings")
    if set(value) != expected:
        raise _CacheFormatError(
            f"{field} must contain exactly {', '.join(sorted(expected))}"
        )


def _strict_pairs(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for raw_key, value in pairs:
        if not isinstance(raw_key, str):
            raise _CacheFormatError("JSON object keys must be strings")
        if raw_key in result:
            raise _CacheFormatError(f"duplicate JSON object key: {raw_key!r}")
        result[raw_key] = value
    return result


def _text(value: object, field: str, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_dot):
        raise _CacheFormatError(f"{field} must be a non-empty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _CacheFormatError(f"{field} must be valid UTF-8 text") from exc
    if size > _MAX_TEXT_BYTES:
        raise _CacheFormatError(f"{field} exceeds {_MAX_TEXT_BYTES} UTF-8 bytes")
    return value


def _u64(value: object, field: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_U64:
        raise _CacheFormatError(f"{field} must be a u64")
    return value


def _signed_64(value: object, field: str) -> int:
    if (
        type(value) is not int
        or value < _MIN_SIGNED_64
        or value > _MAX_SIGNED_64
    ):
        raise _CacheFormatError(f"{field} must be a signed 64-bit integer")
    return value


def _sequence(value: object, field: str, *, maximum: int) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _CacheFormatError(f"{field} must be an array")
    if len(value) > maximum:
        raise _CacheFormatError(f"{field} exceeds {maximum} entries")
    return value


def _content_root_binding(content_dir: Path) -> str:
    resolved = Path(content_dir).resolve()
    normalized = os.path.normcase(str(resolved)).replace("\\", "/")
    return _text(normalized, "content_root")


def _cache_key(content_root: str, generation_id: str) -> str:
    return canonical_hash(
        {"content_root": content_root, "generation": generation_id}
    )


def source_snapshot_cache_path(
    pageindex_dir: Path,
    content_dir: Path,
    generation_id: str,
) -> Path:
    """Return the deterministic cache path for one root/Generation pair."""

    generation = validate_sha256(generation_id, "generation_id")
    root = _content_root_binding(content_dir)
    return (
        Path(pageindex_dir).resolve()
        / _CACHE_DIRECTORY
        / f"{_cache_key(root, generation)}.json"
    )


def _binding(
    content_dir: Path,
    generation: LogicalGenerationReceipt,
) -> dict[str, str]:
    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    root = _content_root_binding(content_dir)
    return {
        "cache_key": _cache_key(root, generation.generation_id),
        "content_root": root,
        "generation": generation.generation_id,
        "generation_manifest_sha256": generation.manifest_ref.sha256,
        "input_proof_sha256": generation.input_proof_ref.sha256,
        "generation_recipe_hash": generation.generation_recipe_hash,
    }


def _snapshot_payload(
    snapshot: StableCatalogSnapshot,
    proof: dict[str, object],
) -> dict[str, object]:
    return {
        "proof": proof,
        "directory_state": [
            {
                "relative": relative,
                "mtime_ns": mtime_ns,
                "ctime_ns": ctime_ns,
                "ino": ino,
                "dev": dev,
            }
            for relative, mtime_ns, ctime_ns, ino, dev in snapshot.directory_state
        ],
        "topology": [
            {"doc_key": doc_key, "files": list(files)}
            for doc_key, files in snapshot.topology
        ],
        "file_state": [
            {
                "doc_key": doc_key,
                "path": relative,
                "size": size,
                "mtime_ns": mtime_ns,
                "ctime_ns": ctime_ns,
                "ino": ino,
                "dev": dev,
            }
            for (
                doc_key,
                relative,
                size,
                mtime_ns,
                ctime_ns,
                ino,
                dev,
            ) in snapshot.file_state
        ],
    }


def _cache_core(
    snapshot: StableCatalogSnapshot,
    generation: LogicalGenerationReceipt,
) -> dict[str, object] | None:
    if not isinstance(snapshot, StableCatalogSnapshot):
        raise TypeError("snapshot must be a StableCatalogSnapshot")
    binding = _binding(snapshot.content_dir, generation)
    proof = snapshot.validated_proof()
    documents = proof.get("documents")
    if (
        snapshot.proof_sha256 != generation.input_proof_ref.sha256
        or proof.get("compiler_recipe_hash") != generation.generation_recipe_hash
        or not isinstance(documents, Mapping)
        or len(documents) != generation.document_count
    ):
        return None
    return {
        "artifact_kind": _ARTIFACT_KIND,
        "schema_version": SOURCE_SNAPSHOT_CACHE_SCHEMA_VERSION,
        "binding": binding,
        "snapshot": _snapshot_payload(snapshot, proof),
    }


def store_source_snapshot_cache(
    pageindex_dir: Path,
    snapshot: StableCatalogSnapshot,
    generation: LogicalGenerationReceipt,
) -> Path | None:
    """Atomically store a bound cache hint; return ``None`` on best-effort failure."""

    core = _cache_core(snapshot, generation)
    if core is None:
        return None
    payload = {**core, "payload_sha256": canonical_hash(core)}
    try:
        encoded = canonical_bytes(payload)
        if len(encoded) > SOURCE_SNAPSHOT_CACHE_MAX_BYTES:
            return None
        path = source_snapshot_cache_path(
            pageindex_dir, snapshot.content_dir, generation.generation_id
        )
        write_json_atomic(path, payload)
    except OSError:
        return None
    return path


def _plain_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISREG(metadata.st_mode) and not (
        stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)
    )


def _read_bounded(path: Path) -> bytes | None:
    if not _plain_regular_file(path):
        return None
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > SOURCE_SNAPSHOT_CACHE_MAX_BYTES
            ):
                return None
            raw = stream.read(SOURCE_SNAPSHOT_CACHE_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
    except OSError:
        return None
    if len(raw) > SOURCE_SNAPSHOT_CACHE_MAX_BYTES:
        return None
    identity = lambda value: (
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_ino,
        value.st_dev,
    )
    if identity(before) != identity(after) or len(raw) != before.st_size:
        return None
    return raw


def _parse_directory_state(value: object) -> tuple[tuple[str, int, int, int, int], ...]:
    entries = _sequence(value, "snapshot.directory_state", maximum=_MAX_ENTRIES)
    result: list[tuple[str, int, int, int, int]] = []
    for index, raw_entry in enumerate(entries):
        field = f"snapshot.directory_state[{index}]"
        entry = _strict_mapping(raw_entry, field)
        _strict_keys(entry, _DIRECTORY_KEYS, field)
        relative = _text(entry["relative"], f"{field}.relative", allow_dot=True)
        if relative != ".":
            relative = normalize_relative_path(relative)
        result.append(
            (
                relative,
                _signed_64(entry["mtime_ns"], f"{field}.mtime_ns"),
                _signed_64(entry["ctime_ns"], f"{field}.ctime_ns"),
                _u64(entry["ino"], f"{field}.ino"),
                _u64(entry["dev"], f"{field}.dev"),
            )
        )
    if not result or result[0][0] != ".":
        raise _CacheFormatError("directory_state must begin with the content root")
    relatives = tuple(entry[0] for entry in result)
    if len(set(relatives)) != len(relatives):
        raise _CacheFormatError("directory_state contains duplicate paths")
    return tuple(result)


def _parse_topology(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    entries = _sequence(value, "snapshot.topology", maximum=_MAX_ENTRIES)
    result: list[tuple[str, tuple[str, ...]]] = []
    for index, raw_entry in enumerate(entries):
        field = f"snapshot.topology[{index}]"
        entry = _strict_mapping(raw_entry, field)
        _strict_keys(entry, _TOPOLOGY_KEYS, field)
        try:
            doc_key = validate_doc_key(entry["doc_key"])
        except (TypeError, ValueError) as exc:
            raise _CacheFormatError(f"invalid {field}.doc_key: {exc}") from exc
        raw_files = _sequence(
            entry["files"], f"{field}.files", maximum=_MAX_FILES_PER_SOURCE
        )
        if not raw_files:
            raise _CacheFormatError(f"{field}.files must not be empty")
        files: list[str] = []
        for file_index, raw_file in enumerate(raw_files):
            file_field = f"{field}.files[{file_index}]"
            normalized = normalize_relative_path(_text(raw_file, file_field))
            if normalized != raw_file:
                raise _CacheFormatError(f"{file_field} must be normalized")
            files.append(normalized)
        if len(set(files)) != len(files):
            raise _CacheFormatError(f"{field}.files contains duplicates")
        result.append((doc_key, tuple(files)))
    doc_keys = tuple(entry[0] for entry in result)
    if doc_keys != tuple(sorted(doc_keys)) or len(set(doc_keys)) != len(doc_keys):
        raise _CacheFormatError("topology document keys must be unique and sorted")
    return tuple(result)


def _parse_file_state(
    value: object,
) -> tuple[tuple[str, str, int, int, int, int, int], ...]:
    entries = _sequence(value, "snapshot.file_state", maximum=_MAX_ENTRIES)
    result: list[tuple[str, str, int, int, int, int, int]] = []
    for index, raw_entry in enumerate(entries):
        field = f"snapshot.file_state[{index}]"
        entry = _strict_mapping(raw_entry, field)
        _strict_keys(entry, _FILE_KEYS, field)
        try:
            doc_key = validate_doc_key(entry["doc_key"])
        except (TypeError, ValueError) as exc:
            raise _CacheFormatError(f"invalid {field}.doc_key: {exc}") from exc
        relative = normalize_relative_path(_text(entry["path"], f"{field}.path"))
        if relative != entry["path"]:
            raise _CacheFormatError(f"{field}.path must be normalized")
        result.append(
            (
                doc_key,
                relative,
                _u64(entry["size"], f"{field}.size"),
                _signed_64(entry["mtime_ns"], f"{field}.mtime_ns"),
                _signed_64(entry["ctime_ns"], f"{field}.ctime_ns"),
                _u64(entry["ino"], f"{field}.ino"),
                _u64(entry["dev"], f"{field}.dev"),
            )
        )
    return tuple(result)


def _snapshot_from_payload(
    root: Path,
    value: object,
) -> StableCatalogSnapshot:
    payload = _strict_mapping(value, "snapshot")
    _strict_keys(payload, _SNAPSHOT_KEYS, "snapshot")
    topology = _parse_topology(payload["topology"])
    directory_state = _parse_directory_state(payload["directory_state"])
    file_state = _parse_file_state(payload["file_state"])
    sources = _sources_from_topology(root, topology)
    prepared_sources = _prepare_sources(root, sources)

    expected_pairs = tuple(
        (doc_key, relative)
        for doc_key, files in topology
        for relative in files
    )
    if tuple((entry[0], entry[1]) for entry in file_state) != expected_pairs:
        raise _CacheFormatError("file_state does not exactly cover topology files")

    snapshot = StableCatalogSnapshot(
        content_dir=root,
        sources=sources,
        prepared_sources=prepared_sources,
        proof=payload["proof"],  # type: ignore[arg-type]
        directory_state=directory_state,
        topology=topology,
        file_state=file_state,
    )
    documents = snapshot.proof.get("documents")
    if not isinstance(documents, Mapping) or tuple(documents) != tuple(
        doc_key for doc_key, _files in topology
    ):
        raise _CacheFormatError("input proof does not exactly cover topology documents")
    return snapshot


def load_source_snapshot_cache(
    pageindex_dir: Path,
    content_dir: Path,
    generation: LogicalGenerationReceipt,
    *,
    check_cancelled: Callable[[], None] = lambda: None,
) -> StableCatalogSnapshot | None:
    """Load a valid unchanged snapshot, or return ``None`` for every cache miss."""

    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    if not callable(check_cancelled):
        raise TypeError("check_cancelled must be callable")
    root = Path(content_dir).resolve()
    expected_binding = _binding(root, generation)
    path = source_snapshot_cache_path(pageindex_dir, root, generation.generation_id)
    raw = _read_bounded(path)
    if raw is None:
        return None

    check_cancelled()
    try:
        decoded = json.loads(raw, object_pairs_hook=_strict_pairs)
        envelope = _strict_mapping(decoded, "source snapshot cache")
        _strict_keys(envelope, _ROOT_KEYS, "source snapshot cache")
        if canonical_bytes(envelope) != raw:
            raise _CacheFormatError("source snapshot cache must be canonical JSON")
        if envelope["artifact_kind"] != _ARTIFACT_KIND:
            raise _CacheFormatError("unsupported source snapshot cache artifact_kind")
        if (
            type(envelope["schema_version"]) is not int
            or envelope["schema_version"] != SOURCE_SNAPSHOT_CACHE_SCHEMA_VERSION
        ):
            raise _CacheFormatError("unsupported source snapshot cache schema_version")
        binding = _strict_mapping(envelope["binding"], "binding")
        _strict_keys(binding, _BINDING_KEYS, "binding")
        for field, expected in expected_binding.items():
            if binding[field] != expected:
                raise _CacheFormatError(f"cache binding mismatch: {field}")
        supplied_hash = validate_sha256(envelope["payload_sha256"], "payload_sha256")
        core = {key: envelope[key] for key in envelope if key != "payload_sha256"}
        if canonical_hash(core) != supplied_hash:
            raise _CacheFormatError("source snapshot cache payload hash mismatch")
        snapshot = _snapshot_from_payload(root, envelope["snapshot"])
        # Construction already detached, validated and hashed the proof.  This
        # freshly reconstructed object cannot have been mutated between those
        # operations, so avoid a second O(N) validation pass on the hot load.
        proof = snapshot.proof
        documents = proof.get("documents")
        if (
            snapshot.proof_sha256 != generation.input_proof_ref.sha256
            or proof.get("compiler_recipe_hash") != generation.generation_recipe_hash
            or not isinstance(documents, Mapping)
            or len(documents) != generation.document_count
        ):
            raise _CacheFormatError("cached source proof is not bound to Generation")
    except (
        json.JSONDecodeError,
        UnicodeError,
        TypeError,
        ValueError,
        OSError,
        RuntimeError,
        OverflowError,
    ):
        return None

    check_cancelled()
    try:
        unchanged = snapshot.verify_unchanged(check_cancelled)
    except (OSError, TypeError, ValueError):
        return None
    return snapshot if unchanged else None


__all__ = [
    "SOURCE_SNAPSHOT_CACHE_MAX_BYTES",
    "SOURCE_SNAPSHOT_CACHE_SCHEMA_VERSION",
    "load_source_snapshot_cache",
    "source_snapshot_cache_path",
    "store_source_snapshot_cache",
]
