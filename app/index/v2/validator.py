"""Materialize and validate complete PageIndex v2 Generation candidates."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from app.retrieval.tokenizer import tokenize

from .artifacts import ArtifactRef, CandidateReceipt
from .canonical import canonical_bytes, canonical_hash, sha256_bytes, write_json_atomic
from .compiler import CompiledGeneration, compile_generation
from .ids import normalize_relative_path
from .input_proof import (
    INPUT_PROOF_PATH,
    proof_from_segments,
    validate_input_proof,
)
from .models import (
    COMPILER_SCHEMA_VERSION,
    CompilerRecipe,
    SegmentRecipe,
)
from .object_store import load_segment
from .streaming_json import (
    BoundedJsonError,
    CanonicalJsonStream,
    iter_canonical_array_items,
    load_bounded_canonical_json,
    stream_file_digest,
)

__all__ = [
    "ValidationMode",
    "ValidationReport",
    "materialize_candidate",
    "validate_candidate",
    "validate_candidate_deep",
    "validate_candidate_normal",
]


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_TYPE_ORDER = {"book": 0, "paper": 1, "note": 2}
_DOCUMENT_TREE_FOLDERS = {"book": "books", "paper": "papers", "note": "notes"}
_REQUIRED_RUNTIME_ARTIFACTS = {
    "global-index.json",
    "node-index.json",
    "chunks.json",
    "inverted-index.json",
    INPUT_PROOF_PATH,
}


class ValidationMode(str, Enum):
    """Validation cost/assurance levels exposed to build orchestration."""

    NORMAL = "normal"
    SAMPLED = "sampled"
    DEEP = "deep"




@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Complete candidate validation outcome."""

    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def error_codes(self) -> tuple[str, ...]:
        return tuple(error.split(":", 1)[0] for error in self.errors)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _safe_relative_path(value: str) -> Path:
    if not isinstance(value, str) or not value or chr(92) in value:
        raise ValueError(f"invalid generation path: {value!r}")
    pure = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        pure.is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"invalid generation path: {value!r}")
    return Path(*pure.parts)


def materialize_candidate(
    candidate_dir: Path,
    compiled: CompiledGeneration,
) -> Path:
    """Write a compiled Generation into a fresh candidate directory."""

    target = Path(candidate_dir)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"candidate directory is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    for relative, payload in sorted(compiled.payloads.items()):
        write_json_atomic(target / _safe_relative_path(relative), payload)
    write_json_atomic(target / "manifest.json", compiled.manifest)
    return target


def _load_json_file(path: Path) -> tuple[object | None, bytes | None, str | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None, "file_missing"
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, raw, "json_invalid"
    return value, raw, None


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: object) -> Sequence[Any] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return None


def _add(errors: list[str], code: str, detail: str) -> None:
    message = f"{code}: {detail}"
    if message not in errors:
        errors.append(message)


def _validate_segment_metadata(
    segment: Mapping[str, object],
    doc_key: str,
    errors: list[str],
) -> None:
    """Validate the recipe/fingerprint facts used for safe Segment reuse."""

    recipe_value = _mapping(segment.get("segment_recipe"))
    if recipe_value is None:
        _add(errors, "segment_recipe_invalid", f"{doc_key}: missing recipe")
        return
    try:
        recipe = SegmentRecipe(**dict(recipe_value))
    except (TypeError, ValueError) as exc:
        _add(errors, "segment_recipe_invalid", f"{doc_key}: {exc}")
        return
    if dict(recipe_value) != recipe.as_dict():
        _add(errors, "segment_recipe_invalid", f"{doc_key}: non-canonical recipe")

    fingerprint = _mapping(segment.get("fingerprint"))
    if fingerprint is None:
        _add(errors, "segment_fingerprint_invalid", f"{doc_key}: missing fingerprint")
        return

    recipe_hash = fingerprint.get("recipe_hash")
    expected_recipe_hash = canonical_hash(recipe.as_dict())
    if recipe_hash != expected_recipe_hash:
        _add(
            errors,
            "segment_recipe_hash_mismatch",
            f"{doc_key}: expected {expected_recipe_hash}, got {recipe_hash!r}",
        )

    raw_files = _sequence(fingerprint.get("source_files"))
    if raw_files is None:
        _add(
            errors,
            "segment_source_files_invalid",
            f"{doc_key}: source_files must be an array",
        )
        return

    records: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for position, raw_record in enumerate(raw_files):
        record = _mapping(raw_record)
        if record is None or set(record) != {"path", "sha256"}:
            _add(
                errors,
                "segment_source_files_invalid",
                f"{doc_key}: source_files[{position}] must contain path/sha256",
            )
            continue
        path = record.get("path")
        digest = record.get("sha256")
        try:
            normalized = normalize_relative_path(path) if isinstance(path, str) else ""
        except (TypeError, ValueError):
            normalized = ""
        if not normalized or normalized != path or path in seen_paths:
            _add(
                errors,
                "segment_source_files_invalid",
                f"{doc_key}: invalid or duplicate path {path!r}",
            )
            continue
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            _add(
                errors,
                "segment_source_files_invalid",
                f"{doc_key}: invalid sha256 for {path!r}",
            )
            continue
        seen_paths.add(path)
        records.append({"path": path, "sha256": digest})

    content_hash = fingerprint.get("content_hash")
    expected_content_hash = canonical_hash(records)
    if (
        len(records) != len(raw_files)
        or not isinstance(content_hash, str)
        or not _SHA256_RE.fullmatch(content_hash)
        or content_hash != expected_content_hash
    ):
        _add(
            errors,
            "segment_content_hash_mismatch",
            f"{doc_key}: expected {expected_content_hash}, got {content_hash!r}",
        )


def _field_counts(chunk: Mapping[str, object]) -> tuple[Counter[str], Counter[str], Counter[str]]:
    breadcrumb = _sequence(chunk.get("breadcrumb"))
    if breadcrumb is None or not all(isinstance(part, str) for part in breadcrumb):
        raise ValueError("chunk breadcrumb must be an array of strings")
    return (
        Counter(tokenize(str(chunk.get("title") or ""))),
        Counter(tokenize(" ".join(breadcrumb))),
        Counter(tokenize(str(chunk.get("body") or ""))),
    )


def _validate_segment_payload(
    segment: Mapping[str, object],
    doc_key: str,
    errors: list[str],
) -> None:
    """Recompute field postings and lengths from immutable chunk facts."""

    raw_nodes = _sequence(segment.get("nodes"))
    raw_chunks = _sequence(segment.get("chunks"))
    raw_postings = _mapping(segment.get("postings"))
    if raw_nodes is None or raw_chunks is None or raw_postings is None:
        _add(errors, "segment_schema_invalid", f"{doc_key}: nodes/chunks/postings")
        return

    node_keys: set[str] = set()
    for position, raw_node in enumerate(raw_nodes):
        node = _mapping(raw_node)
        node_key = node.get("node_key") if node is not None else None
        if not isinstance(node_key, str) or not node_key or node_key in node_keys:
            _add(
                errors,
                "segment_node_invalid",
                f"{doc_key}: nodes[{position}] has invalid/duplicate node_key",
            )
            continue
        node_keys.add(node_key)

    expected: dict[str, list[list[int]]] = {}
    local_ids: set[int] = set()
    for position, raw_chunk in enumerate(raw_chunks):
        chunk = _mapping(raw_chunk)
        if chunk is None:
            _add(errors, "segment_chunk_invalid", f"{doc_key}: chunks[{position}]")
            continue
        local_id = chunk.get("local_id")
        node_key = chunk.get("node_key")
        if (
            isinstance(local_id, bool)
            or not isinstance(local_id, int)
            or local_id < 0
            or local_id in local_ids
        ):
            _add(
                errors,
                "segment_chunk_invalid",
                f"{doc_key}: chunks[{position}] has invalid/duplicate local_id",
            )
            continue
        local_ids.add(local_id)
        if node_key not in node_keys:
            _add(
                errors,
                "segment_chunk_unknown_node",
                f"{doc_key}:{local_id} -> {node_key!r}",
            )
        try:
            title, breadcrumb, body = _field_counts(chunk)
        except ValueError as exc:
            _add(errors, "segment_chunk_invalid", f"{doc_key}:{local_id}: {exc}")
            continue

        lengths = _mapping(chunk.get("lengths"))
        expected_lengths = {
            "title": sum(title.values()),
            "breadcrumb": sum(breadcrumb.values()),
            "body": sum(body.values()),
        }
        if lengths is None or dict(lengths) != expected_lengths:
            _add(
                errors,
                "segment_field_lengths_mismatch",
                f"{doc_key}:{local_id}",
            )

        for token in sorted(set(title) | set(breadcrumb) | set(body)):
            expected.setdefault(token, []).append(
                [
                    local_id,
                    int(title.get(token, 0)),
                    int(breadcrumb.get(token, 0)),
                    int(body.get(token, 0)),
                ]
            )

    if sorted(local_ids) != list(range(len(raw_chunks))):
        _add(errors, "segment_local_ids_not_compact", doc_key)
    expected = {
        token: sorted(rows, key=lambda row: row[0])
        for token, rows in sorted(expected.items())
    }
    if dict(raw_postings) != expected:
        _add(errors, "segment_postings_mismatch", doc_key)


def _validate_runtime_references(
    payloads: Mapping[str, object],
    errors: list[str],
) -> None:
    global_index = _mapping(payloads.get("global-index.json"))
    node_index = _mapping(payloads.get("node-index.json"))
    chunks_index = _mapping(payloads.get("chunks.json"))
    inverted_index = _mapping(payloads.get("inverted-index.json"))
    if not all((global_index, node_index, chunks_index, inverted_index)):
        _add(errors, "runtime_schema_invalid", "one or more global payloads are not objects")
        return

    docs = _sequence(global_index.get("docs"))
    nodes = _sequence(node_index.get("nodes"))
    chunks = _sequence(chunks_index.get("chunks"))
    postings = _mapping(inverted_index.get("postings"))
    if docs is None or nodes is None or chunks is None or postings is None:
        _add(errors, "runtime_schema_invalid", "global payload collections have invalid types")
        return

    doc_ids: set[str] = set()
    doc_keys: set[str] = set()
    for value in docs:
        doc = _mapping(value)
        if doc is None:
            _add(errors, "document_invalid", "global document is not an object")
            continue
        slug = doc.get("id")
        doc_type = doc.get("type")
        if not isinstance(slug, str) or not isinstance(doc_type, str):
            _add(errors, "document_invalid", "document id/type must be strings")
            continue
        key = f"{doc_type}:{slug}"
        if key in doc_keys:
            _add(errors, "document_duplicate", key)
        doc_keys.add(key)
        doc_ids.add(slug)

    node_refs: set[tuple[str, str]] = set()
    for value in nodes:
        node = _mapping(value)
        if node is None:
            _add(errors, "node_invalid", "node entry is not an object")
            continue
        doc_id = node.get("doc_id")
        node_id = node.get("node_id")
        if not isinstance(doc_id, str) or not isinstance(node_id, str):
            _add(errors, "node_invalid", "node doc_id/node_id must be strings")
            continue
        if doc_id not in doc_ids:
            _add(errors, "node_unknown_document", f"{doc_id}:{node_id}")
        ref = (doc_id, node_id)
        if ref in node_refs:
            _add(errors, "node_duplicate", f"{doc_id}:{node_id}")
        node_refs.add(ref)

    numeric_chunks: set[int] = set()
    for value in chunks:
        chunk = _mapping(value)
        if chunk is None:
            _add(errors, "chunk_invalid", "chunk entry is not an object")
            continue
        chunk_id = chunk.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id.startswith("c"):
            _add(errors, "chunk_invalid", f"invalid chunk id {chunk_id!r}")
            continue
        try:
            numeric_id = int(chunk_id[1:])
        except ValueError:
            _add(errors, "chunk_invalid", f"invalid chunk id {chunk_id!r}")
            continue
        if numeric_id in numeric_chunks:
            _add(errors, "chunk_duplicate", chunk_id)
        numeric_chunks.add(numeric_id)
        ref = (chunk.get("doc_id"), chunk.get("node_id"))
        if ref not in node_refs:
            _add(errors, "chunk_unknown_node", f"{ref[0]}:{ref[1]}")

    if inverted_index.get("num_chunks") != len(chunks):
        _add(errors, "chunk_count_mismatch", "inverted num_chunks differs from chunks length")

    for token, raw_rows in postings.items():
        if not isinstance(token, str) or not token:
            _add(errors, "posting_invalid", "posting token must be a non-empty string")
            continue
        rows = _sequence(raw_rows)
        if rows is None:
            _add(errors, "posting_invalid", f"{token!r} posting list is not an array")
            continue
        seen_for_token: set[int] = set()
        for raw_row in rows:
            row = _sequence(raw_row)
            if row is None or len(row) != 2:
                _add(errors, "posting_invalid", f"{token!r} row must be [chunk, tf]")
                continue
            chunk_id, tf = row
            if isinstance(chunk_id, bool) or not isinstance(chunk_id, int):
                _add(errors, "posting_invalid", f"{token!r} chunk id is not an integer")
                continue
            if chunk_id not in numeric_chunks:
                _add(errors, "posting_unknown_chunk", f"{token!r}:{chunk_id}")
            if chunk_id in seen_for_token:
                _add(errors, "posting_duplicate_chunk", f"{token!r}:{chunk_id}")
            seen_for_token.add(chunk_id)
            if isinstance(tf, bool) or not isinstance(tf, int) or tf <= 0:
                _add(errors, "posting_invalid_tf", f"{token!r}:{chunk_id}")


_MISSING = object()


def _ref_field(reference: object, field: str) -> object:
    if isinstance(reference, Mapping):
        return reference.get(field, _MISSING)
    return getattr(reference, field, _MISSING)


def _resolves_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def _candidate_file_set(
    candidate: Path,
    errors: list[str],
) -> tuple[set[str], set[str]]:
    """Return regular candidate files and paths that must never be opened."""

    actual: set[str] = set()
    unsafe: set[str] = set()
    if candidate.is_symlink():
        _add(errors, "unsafe_file", "candidate directory is a symbolic link")
        return actual, unsafe
    if not candidate.exists():
        _add(errors, "candidate_missing", str(candidate))
        return actual, unsafe
    if not candidate.is_dir():
        _add(errors, "candidate_invalid", f"not a directory: {candidate}")
        return actual, unsafe

    def walk_error(exc: OSError) -> None:
        _add(errors, "candidate_scan_failed", str(exc))

    for current, directories, filenames in os.walk(
        candidate,
        followlinks=False,
        onerror=walk_error,
    ):
        current_path = Path(current)
        for name in tuple(directories):
            path = current_path / name
            relative = path.relative_to(candidate).as_posix()
            if path.is_symlink() or not _resolves_within(path, candidate):
                unsafe.add(relative)
                _add(errors, "unsafe_file", relative)
                directories.remove(name)

        for name in filenames:
            path = current_path / name
            relative = path.relative_to(candidate).as_posix()
            actual.add(relative)
            try:
                _safe_relative_path(relative)
            except ValueError:
                unsafe.add(relative)
                _add(errors, "unsafe_file", relative)
                continue
            if (
                path.is_symlink()
                or not path.is_file()
                or not _resolves_within(path, candidate)
            ):
                unsafe.add(relative)
                _add(errors, "unsafe_file", relative)
    return actual, unsafe


def _load_control_document(
    path: Path,
    relative: str,
    errors: list[str],
) -> object | None:
    try:
        return load_bounded_canonical_json(path)
    except FileNotFoundError:
        _add(errors, "file_missing", relative)
    except BoundedJsonError as exc:
        code = (
            "manifest_not_canonical"
            if relative == "manifest.json" and "not canonical" in str(exc)
            else "file_not_canonical"
            if "not canonical" in str(exc)
            else "json_invalid"
        )
        _add(errors, code, f"{relative}: {exc}")
    except OSError as exc:
        _add(errors, "file_read_failed", f"{relative}: {exc}")
    return None


def _valid_receipt_artifacts(
    receipt: CandidateReceipt,
    errors: list[str],
) -> dict[str, ArtifactRef]:
    artifacts: dict[str, ArtifactRef] = {}
    for relative, reference in receipt.artifacts.items():
        try:
            _safe_relative_path(relative)
        except (TypeError, ValueError) as exc:
            _add(errors, "receipt_path_invalid", str(exc))
            continue
        if relative == "manifest.json":
            _add(
                errors,
                "receipt_path_invalid",
                "manifest.json is attested by manifest_sha256",
            )
            continue
        if not isinstance(reference, ArtifactRef):
            _add(errors, "receipt_invalid", f"{relative}: invalid ArtifactRef")
            continue
        if reference.relative_path != relative:
            _add(
                errors,
                "receipt_invalid",
                f"{relative}: ArtifactRef path differs",
            )
            continue
        if (
            not _SHA256_RE.fullmatch(reference.sha256)
            or isinstance(reference.byte_size, bool)
            or not isinstance(reference.byte_size, int)
            or reference.byte_size < 0
        ):
            _add(errors, "receipt_invalid", f"{relative}: invalid attestation")
            continue
        artifacts[relative] = reference
    return artifacts


def _validate_artifact_receipts(
    candidate: Path,
    receipt: CandidateReceipt,
    artifacts: Mapping[str, ArtifactRef],
    unsafe: set[str],
    errors: list[str],
) -> object | None:
    for relative, reference in sorted(artifacts.items()):
        if relative in unsafe:
            continue
        try:
            actual = stream_file_digest(candidate / _safe_relative_path(relative))
        except FileNotFoundError:
            _add(errors, "file_missing", relative)
            continue
        except OSError as exc:
            _add(errors, "file_read_failed", f"{relative}: {exc}")
            continue
        if actual.sha256 != reference.sha256:
            _add(errors, "file_hash_mismatch", relative)
        if actual.byte_size != reference.byte_size:
            _add(errors, "file_size_mismatch", relative)

    if "manifest.json" in unsafe:
        return None
    try:
        manifest_digest = stream_file_digest(candidate / "manifest.json")
    except FileNotFoundError:
        _add(errors, "file_missing", "manifest.json")
        return None
    except OSError as exc:
        _add(errors, "file_read_failed", f"manifest.json: {exc}")
        return None
    if manifest_digest.sha256 != receipt.manifest_sha256:
        _add(errors, "manifest_hash_mismatch", "manifest.json")
    return manifest_digest


def _validate_segment_ref_bindings(
    segment_refs: Mapping[str, object],
    documents: Mapping[str, object],
    proof_documents: Mapping[str, object],
    pageindex_dir: Path,
    errors: list[str],
) -> None:
    valid_ref_keys: set[str] = set()
    for raw_doc_key in segment_refs:
        if not isinstance(raw_doc_key, str) or not raw_doc_key:
            _add(errors, "segment_reference_invalid", repr(raw_doc_key))
            continue
        valid_ref_keys.add(raw_doc_key)

    document_keys = set(documents)
    proof_keys = set(proof_documents)
    if valid_ref_keys != document_keys:
        _add(
            errors,
            "segment_reference_mismatch",
            "receipt Segment refs differ from manifest documents",
        )
    if proof_keys != document_keys:
        _add(
            errors,
            "input_proof_documents_mismatch",
            "proof documents differ from manifest documents",
        )

    for doc_key in sorted(valid_ref_keys & document_keys & proof_keys):
        reference = segment_refs[doc_key]
        segment_hash = _ref_field(reference, "segment_hash")
        reference_doc_key = _ref_field(reference, "doc_key")
        doc_type = _ref_field(reference, "doc_type")
        slug = _ref_field(reference, "slug")
        content_hash = _ref_field(reference, "content_hash")
        segment_recipe_hash = _ref_field(reference, "segment_recipe_hash")
        byte_size = _ref_field(reference, "byte_size")
        reference_path = _ref_field(reference, "path")
        proof_entry = _mapping(proof_documents[doc_key])

        invalid = False
        if (
            not isinstance(segment_hash, str)
            or not _SHA256_RE.fullmatch(segment_hash)
            or documents[doc_key] != segment_hash
        ):
            invalid = True
        if (
            reference_doc_key != doc_key
            or not isinstance(doc_type, str)
            or not isinstance(slug, str)
            or f"{doc_type}:{slug}" != doc_key
        ):
            invalid = True
        if proof_entry is None:
            invalid = True
        elif (
            proof_entry.get("content_hash") != content_hash
            or proof_entry.get("segment_recipe_hash") != segment_recipe_hash
        ):
            _add(errors, "input_proof_segment_mismatch", doc_key)
        if (
            not isinstance(content_hash, str)
            or not _SHA256_RE.fullmatch(content_hash)
            or not isinstance(segment_recipe_hash, str)
            or not _SHA256_RE.fullmatch(segment_recipe_hash)
            or isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or byte_size < 0
        ):
            invalid = True
        if invalid:
            _add(errors, "segment_reference_invalid", doc_key)
            continue

        expected_path = (
            Path(pageindex_dir)
            / "objects"
            / "segments"
            / segment_hash[:2]
            / f"{segment_hash}.json"
        )
        try:
            supplied_path = Path(reference_path)
        except TypeError:
            _add(errors, "segment_reference_invalid", f"{doc_key}: invalid path")
            continue
        if (
            supplied_path.resolve() != expected_path.resolve()
            or supplied_path.is_symlink()
            or not _resolves_within(supplied_path, Path(pageindex_dir))
        ):
            _add(errors, "segment_reference_invalid", f"{doc_key}: unsafe path")
            continue
        if not supplied_path.exists():
            _add(errors, "segment_object_missing", doc_key)
            continue
        if not supplied_path.is_file():
            _add(errors, "segment_reference_invalid", f"{doc_key}: not a file")
            continue
        try:
            actual_size = supplied_path.stat().st_size
        except OSError as exc:
            _add(errors, "segment_object_invalid", f"{doc_key}: {exc}")
            continue
        if actual_size != byte_size:
            _add(errors, "segment_object_size_mismatch", doc_key)


class _RuntimeArtifactError(ValueError):
    __slots__ = ("code", "detail")

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _runtime_fail(code: str, detail: str) -> None:
    raise _RuntimeArtifactError(code, detail)


def _runtime_mapping(value: object, label: str) -> Mapping[str, Any]:
    mapping = _mapping(value)
    if mapping is None:
        _runtime_fail("runtime_schema_invalid", f"{label} must be an object")
    return mapping


def _runtime_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _runtime_fail("runtime_schema_invalid", f"{label} must be a non-empty string")
    return value


def _runtime_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        _runtime_fail("runtime_schema_invalid", f"{label} must be a string")
    return value


def _runtime_string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _runtime_fail(
            "runtime_schema_invalid",
            f"{label} must be an array of strings",
        )
    return value


def _runtime_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        _runtime_fail(
            "runtime_schema_invalid",
            f"{label} fields differ from schema",
        )

def _runtime_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _runtime_fail(
            "runtime_schema_invalid",
            f"{label} must be a non-negative integer",
        )
    return value


def _required_artifact_paths(
    documents: Mapping[str, object],
    errors: list[str],
) -> set[str]:
    required = set(_REQUIRED_RUNTIME_ARTIFACTS)
    for doc_key in documents:
        doc_type, separator, slug = doc_key.partition(":")
        folder = _DOCUMENT_TREE_FOLDERS.get(doc_type)
        if (
            separator != ":"
            or folder is None
            or not slug
            or "/" in slug
            or "\\" in slug
            or slug in {".", ".."}
        ):
            _add(errors, "segment_reference_invalid", f"unsafe doc key {doc_key!r}")
            continue
        relative = f"{folder}/{slug}.json"
        try:
            _safe_relative_path(relative)
        except ValueError:
            _add(errors, "segment_reference_invalid", f"unsafe doc key {doc_key!r}")
            continue
        required.add(relative)
    return required


def _iter_runtime_array(
    candidate: Path,
    relative: str,
    object_key: str,
) -> Any:
    try:
        yield from iter_canonical_array_items(
            candidate / _safe_relative_path(relative),
            object_key=object_key,
        )
    except BoundedJsonError as exc:
        raise _RuntimeArtifactError(
            "file_not_canonical",
            f"{relative}: {exc}",
        ) from exc
    except OSError as exc:
        raise _RuntimeArtifactError(
            "file_read_failed",
            f"{relative}: {exc}",
        ) from exc


def _scan_inverted_index(
    path: Path,
    *,
    chunks_count: int,
) -> tuple[int, int, int]:
    try:
        with CanonicalJsonStream(path) as reader:
            reader.expect(b'{"num_chunks":')
            num_chunks = _runtime_nonnegative_int(
                reader.read_value(max_bytes=64),
                "inverted-index.num_chunks",
            )
            reader.expect(b',"postings":{')
            tokens_count = 0
            postings_count = 0
            previous_token: str | None = None
            first_empty_token: str | None = None
            first_token = True
            while reader.peek_byte() != ord("}"):
                if not first_token:
                    reader.expect(b",")
                first_token = False
                token = _runtime_string(
                    reader.read_value(max_bytes=1024 * 1024),
                    "inverted-index token",
                )
                if previous_token is not None and token <= previous_token:
                    _runtime_fail(
                        "file_not_canonical",
                        "inverted-index.json: posting tokens are not strictly increasing",
                    )
                previous_token = token
                tokens_count += 1
                reader.expect(b":[")
                first_posting = True
                previous_chunk = 0
                token_postings = 0
                while reader.peek_byte() != ord("]"):
                    if not first_posting:
                        reader.expect(b",")
                    first_posting = False
                    chunk_id, tf = reader.read_nonnegative_int_pair(max_bytes=256)
                    if chunk_id < 1 or chunk_id > chunks_count:
                        _runtime_fail(
                            "posting_unknown_chunk",
                            f"{token!r}:{chunk_id}",
                        )
                    if chunk_id <= previous_chunk:
                        _runtime_fail(
                            "posting_order_invalid",
                            f"{token!r} chunk ids must be strictly increasing",
                        )
                    if tf <= 0:
                        _runtime_fail("posting_invalid_tf", f"{token!r}:{chunk_id}")
                    previous_chunk = chunk_id
                    token_postings += 1
                    postings_count += 1
                reader.expect(b"]")
                if token_postings == 0 and first_empty_token is None:
                    first_empty_token = token
            reader.expect(b"}}")
            reader.finish()
            if first_empty_token is not None:
                _runtime_fail(
                    "posting_invalid",
                    f"{first_empty_token!r} posting list must not be empty",
                )
    except BoundedJsonError as exc:
        raise _RuntimeArtifactError(
            "file_not_canonical",
            f"inverted-index.json: {exc}",
        ) from exc
    except OSError as exc:
        raise _RuntimeArtifactError(
            "file_read_failed",
            f"inverted-index.json: {exc}",
        ) from exc

    if num_chunks != chunks_count:
        _runtime_fail(
            "chunk_count_mismatch",
            "inverted num_chunks differs from streamed chunks count",
        )
    return num_chunks, tokens_count, postings_count


def _validate_tree_artifacts(
    candidate: Path,
    artifacts: Mapping[str, ArtifactRef],
    unsafe: set[str],
    documents: Mapping[str, object],
    errors: list[str],
) -> None:
    for doc_key in sorted(documents):
        doc_type, _, slug = doc_key.partition(":")
        folder = _DOCUMENT_TREE_FOLDERS.get(doc_type)
        if folder is None or not slug:
            continue
        relative = f"{folder}/{slug}.json"
        if relative not in artifacts or relative in unsafe:
            continue
        try:
            value = load_bounded_canonical_json(
                candidate / _safe_relative_path(relative)
            )
        except FileNotFoundError:
            _add(errors, "file_missing", relative)
            continue
        except BoundedJsonError as exc:
            code = (
                "file_not_canonical"
                if "not canonical" in str(exc)
                else "json_invalid"
            )
            _add(errors, code, f"{relative}: {exc}")
            continue
        except OSError as exc:
            _add(errors, "file_read_failed", f"{relative}: {exc}")
            continue
        tree = _mapping(value)
        if tree is None:
            _add(errors, "tree_schema_invalid", f"{relative}: expected object")
            continue
        if "doc_name" in tree and tree.get("doc_name") != slug:
            _add(errors, "tree_document_mismatch", f"{relative}: doc_name")
        if "type" in tree and tree.get("type") != doc_type:
            _add(errors, "tree_document_mismatch", f"{relative}: type")

def _validate_runtime_artifacts(
    candidate: Path,
    artifacts: Mapping[str, ArtifactRef],
    unsafe: set[str],
    documents: Mapping[str, object],
    stats: Mapping[str, object] | None,
    pruning: Mapping[str, object] | None,
    compiler_recipe: Mapping[str, object] | None,
    errors: list[str],
) -> None:
    _validate_tree_artifacts(candidate, artifacts, unsafe, documents, errors)
    runtime_paths = {
        "global-index.json",
        "node-index.json",
        "chunks.json",
        "inverted-index.json",
    }
    if not runtime_paths.issubset(artifacts) or runtime_paths & unsafe:
        return

    try:
        document_keys: set[str] = set()
        document_keys_by_slug: dict[str, set[str]] = {}
        previous_document_order: tuple[int, str, str] | None = None
        documents_count = 0
        for value in _iter_runtime_array(candidate, "global-index.json", "docs"):
            doc = _runtime_mapping(value, "global-index.docs[]")
            doc_type = _runtime_string(doc.get("type"), "document.type")
            slug = _runtime_string(doc.get("id"), "document.id")
            if doc_type not in _DOCUMENT_TYPE_ORDER:
                _runtime_fail("document_invalid", f"unsupported type {doc_type!r}")
            document_fields = {
                "author",
                "description",
                "id",
                "path",
                "tags",
                "title",
                "type",
                "url",
            }
            if doc_type == "paper":
                document_fields.add("year")
            elif doc_type == "note":
                document_fields.update({"date", "source_title", "source_type"})
            _runtime_exact_keys(doc, document_fields, "global-index.docs[]")
            for field in ("author", "description", "path", "title", "url"):
                _runtime_text(doc.get(field), f"document.{field}")
            if not isinstance(doc.get("tags"), list):
                _runtime_fail(
                    "runtime_schema_invalid",
                    "document.tags must be an array",
                )
            if doc_type == "note":
                for field in ("date", "source_title", "source_type"):
                    _runtime_text(doc.get(field), f"document.{field}")
            doc_key = f"{doc_type}:{slug}"
            order = (_DOCUMENT_TYPE_ORDER[doc_type], slug, doc_key)
            if previous_document_order is not None and order <= previous_document_order:
                _runtime_fail(
                    "document_order_invalid",
                    "global documents are not strictly ordered",
                )
            previous_document_order = order
            if doc_key in document_keys:
                _runtime_fail("document_duplicate", doc_key)
            document_keys.add(doc_key)
            document_keys_by_slug.setdefault(slug, set()).add(doc_key)
            documents_count += 1
        if document_keys != set(documents):
            _runtime_fail(
                "runtime_document_mismatch",
                "global documents differ from manifest documents",
            )

        ambiguous_slugs = {
            slug
            for slug, keys in document_keys_by_slug.items()
            if len(keys) > 1
        }
        node_refs: set[tuple[str, str]] = set()
        previous_node_order: tuple[object, ...] | None = None
        nodes_count = 0
        for value in _iter_runtime_array(candidate, "node-index.json", "nodes"):
            node = _runtime_mapping(value, "node-index.nodes[]")
            _runtime_exact_keys(
                node,
                {
                    "breadcrumb",
                    "doc_id",
                    "line_num",
                    "node_id",
                    "summary",
                    "terms",
                    "title",
                    "url",
                },
                "node-index.nodes[]",
            )
            doc_id = _runtime_string(node.get("doc_id"), "node.doc_id")
            node_id = _runtime_string(node.get("node_id"), "node.node_id")
            for field in ("summary", "title"):
                _runtime_text(node.get(field), f"node.{field}")
            _runtime_string(node.get("url"), "node.url")
            _runtime_string_list(node.get("breadcrumb"), "node.breadcrumb")
            _runtime_string_list(node.get("terms"), "node.terms")
            _runtime_nonnegative_int(node.get("line_num"), "node.line_num")
            matching_doc_keys = document_keys_by_slug.get(doc_id)
            if not matching_doc_keys:
                _runtime_fail("node_unknown_document", f"{doc_id}:{node_id}")
            reference = (doc_id, node_id)
            if reference in node_refs and doc_id not in ambiguous_slugs:
                _runtime_fail("node_duplicate", f"{doc_id}:{node_id}")
            node_refs.add(reference)
            if not ambiguous_slugs:
                (matching_doc_key,) = matching_doc_keys
                doc_type, _, slug = matching_doc_key.partition(":")
                if node_id.isdigit():
                    legacy_order: tuple[object, ...] = (0, int(node_id), node_id)
                else:
                    legacy_order = (1, node_id, node_id)
                order = (
                    _DOCUMENT_TYPE_ORDER[doc_type],
                    slug,
                    matching_doc_key,
                    *legacy_order,
                )
                if previous_node_order is not None and order <= previous_node_order:
                    _runtime_fail(
                        "node_order_invalid",
                        "global nodes are not in legacy document/node order",
                    )
                previous_node_order = order
            nodes_count += 1

        chunks_count = 0
        for value in _iter_runtime_array(candidate, "chunks.json", "chunks"):
            chunk = _runtime_mapping(value, "chunks.chunks[]")
            _runtime_exact_keys(
                chunk,
                {
                    "body",
                    "breadcrumb",
                    "chunk_id",
                    "doc_id",
                    "line_num",
                    "node_id",
                    "source_md",
                    "title",
                },
                "chunks.chunks[]",
            )
            chunks_count += 1
            chunk_id = _runtime_string(chunk.get("chunk_id"), "chunk.chunk_id")
            expected_chunk_id = f"c{chunks_count:06d}"
            if chunk_id != expected_chunk_id:
                _runtime_fail(
                    "chunk_order_invalid",
                    f"expected {expected_chunk_id}, got {chunk_id!r}",
                )
            doc_id = _runtime_string(chunk.get("doc_id"), "chunk.doc_id")
            node_id = _runtime_string(chunk.get("node_id"), "chunk.node_id")
            for field in ("body", "source_md", "title"):
                _runtime_text(chunk.get(field), f"chunk.{field}")
            _runtime_string_list(chunk.get("breadcrumb"), "chunk.breadcrumb")
            _runtime_nonnegative_int(chunk.get("line_num"), "chunk.line_num")
            if (doc_id, node_id) not in node_refs:
                _runtime_fail("chunk_unknown_node", f"{doc_id}:{node_id}")

        _, tokens_count, postings_count = _scan_inverted_index(
            candidate / "inverted-index.json",
            chunks_count=chunks_count,
        )
    except _RuntimeArtifactError as exc:
        _add(errors, exc.code, exc.detail)
        return

    counts = {
        "global-index.json": documents_count,
        "node-index.json": nodes_count,
        "chunks.json": chunks_count,
        "inverted-index.json": tokens_count,
    }
    for relative, actual in counts.items():
        if artifacts[relative].records != actual:
            _add(errors, "aggregate_mismatch", f"{relative} records")
    proof_reference = artifacts.get(INPUT_PROOF_PATH)
    if proof_reference is not None and proof_reference.records != len(documents):
        _add(errors, "aggregate_mismatch", "input proof records")

    actual_stats = {
        "documents": documents_count,
        "nodes": nodes_count,
        "chunks": chunks_count,
        "tokens": tokens_count,
        "postings": postings_count,
    }
    if stats is None:
        _add(errors, "manifest_invalid", "stats must be an object")
    else:
        for key, actual in actual_stats.items():
            value = stats.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value != actual
            ):
                _add(errors, "aggregate_mismatch", f"stats.{key}")

    if pruning is None:
        _add(errors, "manifest_invalid", "pruning must be an object")
        return
    expected_pruning_fields = {
        "body_min_coverage",
        "body_min_df",
        "body_postings_pruned",
        "body_tf_pruned",
        "body_tokens_pruned",
        "estimated_bytes_saved",
        "postings_after",
        "postings_before",
        "tokens_after",
        "tokens_before",
    }
    if set(pruning) != expected_pruning_fields:
        _add(errors, "pruning_invalid", "fields differ from schema")
    pruning_values: dict[str, int] = {}
    for key in (
        "tokens_before",
        "tokens_after",
        "postings_before",
        "postings_after",
        "body_tokens_pruned",
        "body_postings_pruned",
        "body_tf_pruned",
        "estimated_bytes_saved",
    ):
        value = pruning.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _add(errors, "pruning_invalid", key)
            continue
        pruning_values[key] = value
    body_min_df = pruning.get("body_min_df")
    if isinstance(body_min_df, bool) or not isinstance(body_min_df, int) or body_min_df < 1:
        _add(errors, "pruning_invalid", "body_min_df")
    body_min_coverage = pruning.get("body_min_coverage")
    if (
        isinstance(body_min_coverage, bool)
        or not isinstance(body_min_coverage, (int, float))
        or not 0.0 <= float(body_min_coverage) <= 1.0
    ):
        _add(errors, "pruning_invalid", "body_min_coverage")
    if compiler_recipe is None:
        _add(errors, "pruning_invalid", "compiler recipe unavailable")
    else:
        if body_min_df != compiler_recipe.get("body_df_min"):
            _add(errors, "pruning_recipe_mismatch", "body_min_df")
        if body_min_coverage != compiler_recipe.get("body_df_ratio"):
            _add(errors, "pruning_recipe_mismatch", "body_min_coverage")
    if len(pruning_values) != 8:
        return
    if pruning_values["tokens_before"] < pruning_values["tokens_after"]:
        _add(errors, "pruning_invalid", "tokens_before < tokens_after")
    if pruning_values["postings_before"] < pruning_values["postings_after"]:
        _add(errors, "pruning_invalid", "postings_before < postings_after")
    if pruning_values["body_tokens_pruned"] > pruning_values["tokens_before"]:
        _add(errors, "pruning_invalid", "body_tokens_pruned > tokens_before")
    if pruning_values["body_postings_pruned"] > pruning_values["postings_before"]:
        _add(errors, "pruning_invalid", "body_postings_pruned > postings_before")
    if pruning_values["tokens_after"] != tokens_count:
        _add(errors, "aggregate_mismatch", "pruning.tokens_after")
    if pruning_values["postings_after"] != postings_count:
        _add(errors, "aggregate_mismatch", "pruning.postings_after")

def validate_candidate_normal(
    receipt: CandidateReceipt,
    pageindex_dir: Path,
) -> ValidationReport:
    """Validate a compiler receipt without loading or recompiling all data."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(receipt, CandidateReceipt):
        _add(errors, "receipt_invalid", "expected CandidateReceipt")
        return ValidationReport(False, tuple(errors), tuple(warnings))

    for field in (
        "revision_sha256",
        "compiler_recipe_hash",
        "input_proof_sha256",
        "manifest_sha256",
    ):
        value = getattr(receipt, field)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            _add(errors, "receipt_invalid", field)
    if (
        not isinstance(receipt.generation_id, str)
        or not re.fullmatch(r"[0-9a-f]{20}", receipt.generation_id)
    ):
        _add(errors, "receipt_invalid", "generation_id")

    candidate = Path(receipt.candidate_dir)
    artifacts = _valid_receipt_artifacts(receipt, errors)
    actual_files, unsafe = _candidate_file_set(candidate, errors)
    expected_files = {"manifest.json", *artifacts}
    for extra in sorted(actual_files - expected_files):
        _add(errors, "unexpected_file", extra)
    for missing in sorted(expected_files - actual_files):
        _add(errors, "file_missing", missing)

    manifest_digest = _validate_artifact_receipts(
        candidate,
        receipt,
        artifacts,
        unsafe,
        errors,
    )
    manifest_value = _load_control_document(
        candidate / "manifest.json",
        "manifest.json",
        errors,
    )
    manifest = _mapping(manifest_value)
    if manifest is None:
        if manifest_value is not None:
            _add(errors, "manifest_invalid", "manifest must be an object")
        return ValidationReport(False, tuple(errors), tuple(warnings))

    if manifest.get("schema_version") != COMPILER_SCHEMA_VERSION:
        _add(
            errors,
            "schema_unknown",
            f"manifest schema {manifest.get('schema_version')!r}",
        )
    if manifest.get("generation") != receipt.generation_id:
        _add(errors, "generation_id_mismatch", "receipt differs from manifest")
    if manifest.get("revision_sha256") != receipt.revision_sha256:
        _add(errors, "revision_hash_mismatch", "receipt differs from manifest")
    if manifest.get("compiler_recipe_hash") != receipt.compiler_recipe_hash:
        _add(
            errors,
            "compiler_recipe_hash_mismatch",
            "receipt differs from manifest",
        )
    if manifest.get("input_proof_sha256") != receipt.input_proof_sha256:
        _add(
            errors,
            "input_proof_hash_mismatch",
            "receipt differs from manifest",
        )

    recipe_value = _mapping(manifest.get("compiler_recipe"))
    if recipe_value is None:
        _add(errors, "compiler_recipe_invalid", "compiler_recipe must be an object")
    else:
        try:
            recipe = CompilerRecipe(**dict(recipe_value))
        except (TypeError, ValueError) as exc:
            _add(errors, "compiler_recipe_invalid", str(exc))
        else:
            if canonical_bytes(dict(recipe_value)) != canonical_bytes(recipe.as_dict()):
                _add(errors, "compiler_recipe_invalid", "non-canonical recipe value")
            recipe_hash = canonical_hash(recipe.as_dict())
            if recipe_hash != receipt.compiler_recipe_hash:
                _add(
                    errors,
                    "compiler_recipe_hash_mismatch",
                    "compiler recipe differs from receipt",
                )

    raw_documents = _mapping(manifest.get("documents"))
    documents: dict[str, object] = {}
    if raw_documents is None:
        _add(errors, "manifest_invalid", "documents must be an object")
    else:
        for doc_key, segment_hash in raw_documents.items():
            if (
                not isinstance(doc_key, str)
                or not doc_key
                or not isinstance(segment_hash, str)
                or not _SHA256_RE.fullmatch(segment_hash)
            ):
                _add(
                    errors,
                    "segment_reference_invalid",
                    repr((doc_key, segment_hash)),
                )
                continue
            documents[doc_key] = segment_hash

        core_manifest = {
            "schema_version": COMPILER_SCHEMA_VERSION,
            "compiler_recipe_hash": manifest.get("compiler_recipe_hash"),
            "input_proof_sha256": manifest.get("input_proof_sha256"),
            "documents": dict(raw_documents),
        }
        revision = canonical_hash(core_manifest)
        if manifest.get("revision_sha256") != revision:
            _add(errors, "revision_hash_mismatch", revision)
        if receipt.revision_sha256 != revision:
            _add(errors, "revision_hash_mismatch", "receipt core manifest")
        if manifest.get("generation") != revision[:20]:
            _add(errors, "generation_id_mismatch", revision[:20])
        if receipt.generation_id != revision[:20]:
            _add(errors, "generation_id_mismatch", "receipt core manifest")

    required_artifacts = _required_artifact_paths(documents, errors)
    for missing in sorted(required_artifacts - set(artifacts)):
        _add(errors, "required_artifact_missing", missing)
    for extra in sorted(set(artifacts) - required_artifacts):
        _add(errors, "unexpected_artifact", extra)
    manifest_files = _mapping(manifest.get("files"))
    if manifest_files is None:
        _add(errors, "manifest_invalid", "files must be an object")
        manifest_files = {}
    if set(manifest_files) != set(artifacts):
        _add(
            errors,
            "receipt_file_set_mismatch",
            "manifest files differ from receipt artifacts",
        )
    for relative, reference in sorted(artifacts.items()):
        metadata = _mapping(manifest_files.get(relative))
        if metadata is None:
            _add(errors, "file_metadata_invalid", relative)
            continue
        if set(metadata) != {"sha256", "bytes"}:
            _add(errors, "file_metadata_invalid", relative)
        if metadata.get("sha256") != reference.sha256:
            _add(errors, "file_hash_mismatch", f"{relative}: manifest/receipt")
        if metadata.get("bytes") != reference.byte_size:
            _add(errors, "file_size_mismatch", f"{relative}: manifest/receipt")

    proof_documents: Mapping[str, object] = {}
    if INPUT_PROOF_PATH not in artifacts:
        _add(errors, "input_proof_missing", "receipt artifacts")
    else:
        proof_value = _load_control_document(
            candidate / INPUT_PROOF_PATH,
            INPUT_PROOF_PATH,
            errors,
        )
        if proof_value is not None:
            try:
                proof = validate_input_proof(proof_value)
            except ValueError as exc:
                _add(errors, "input_proof_invalid", str(exc))
            else:
                proof_hash = canonical_hash(proof)
                if proof_hash != receipt.input_proof_sha256:
                    _add(
                        errors,
                        "input_proof_hash_mismatch",
                        "proof differs from receipt",
                    )
                if proof_hash != manifest.get("input_proof_sha256"):
                    _add(
                        errors,
                        "input_proof_hash_mismatch",
                        "proof differs from manifest",
                    )
                if proof.get("compiler_recipe_hash") != receipt.compiler_recipe_hash:
                    _add(
                        errors,
                        "input_proof_compiler_recipe_mismatch",
                        "proof differs from receipt",
                    )
                proof_documents = _mapping(proof.get("documents")) or {}

    _validate_segment_ref_bindings(
        receipt.segment_refs,
        documents,
        proof_documents,
        Path(pageindex_dir),
        errors,
    )

    stats = _mapping(manifest.get("stats"))
    pruning = _mapping(manifest.get("pruning"))
    _validate_runtime_artifacts(
        candidate,
        artifacts,
        unsafe,
        documents,
        stats,
        pruning,
        recipe_value,
        errors,
    )

    for key in ("stats", "pruning"):
        if key in receipt.invariants and receipt.invariants[key] != manifest.get(key):
            _add(errors, "aggregate_mismatch", f"receipt invariant {key}")
    if (
        manifest_digest is not None
        and "generation_bytes_written" in receipt.invariants
    ):
        expected_bytes = manifest_digest.byte_size + sum(
            reference.byte_size for reference in artifacts.values()
        )
        if receipt.invariants["generation_bytes_written"] != expected_bytes:
            _add(errors, "aggregate_mismatch", "generation_bytes_written")

    raw_warnings = manifest.get("warnings")
    if isinstance(raw_warnings, list):
        warnings.extend(str(item) for item in raw_warnings)
    else:
        _add(errors, "manifest_invalid", "warnings must be an array")
    return ValidationReport(not errors, tuple(errors), tuple(warnings))


def validate_candidate_deep(candidate_dir: Path, pageindex_dir: Path) -> ValidationReport:
    """Validate hashes, references, Segment facts, and exact pruning output."""

    candidate = Path(candidate_dir)
    errors: list[str] = []
    warnings: list[str] = []
    manifest_value, manifest_raw, manifest_error = _load_json_file(candidate / "manifest.json")
    if manifest_error:
        _add(errors, manifest_error, "manifest.json")
        return ValidationReport(False, tuple(errors), tuple(warnings))
    manifest = _mapping(manifest_value)
    if manifest is None:
        _add(errors, "manifest_invalid", "manifest must be an object")
        return ValidationReport(False, tuple(errors), tuple(warnings))
    if manifest_raw != canonical_bytes(manifest):
        _add(errors, "manifest_not_canonical", "manifest.json")
    if manifest.get("schema_version") != COMPILER_SCHEMA_VERSION:
        _add(
            errors,
            "schema_unknown",
            f"manifest schema {manifest.get('schema_version')!r}",
        )

    documents = _mapping(manifest.get("documents"))
    files = _mapping(manifest.get("files"))
    if documents is None:
        _add(errors, "manifest_invalid", "documents must be an object")
        documents = {}
    if files is None:
        _add(errors, "manifest_invalid", "files must be an object")
        files = {}
    if INPUT_PROOF_PATH not in files:
        _add(errors, "input_proof_missing", "manifest files")

    recipe_value = _mapping(manifest.get("compiler_recipe"))
    if recipe_value is None:
        _add(errors, "compiler_recipe_invalid", "compiler_recipe must be an object")
        compiler_recipe = CompilerRecipe()
    else:
        try:
            compiler_recipe = CompilerRecipe(**dict(recipe_value))
        except (TypeError, ValueError) as exc:
            _add(errors, "compiler_recipe_invalid", str(exc))
            compiler_recipe = CompilerRecipe()
    if canonical_hash(compiler_recipe.as_dict()) != manifest.get("compiler_recipe_hash"):
        _add(errors, "compiler_recipe_hash_mismatch", "manifest compiler recipe")

    core_manifest = {
        "schema_version": COMPILER_SCHEMA_VERSION,
        "compiler_recipe_hash": manifest.get("compiler_recipe_hash"),
        "input_proof_sha256": manifest.get("input_proof_sha256"),
        "documents": dict(documents),
    }
    revision = canonical_hash(core_manifest)
    if manifest.get("revision_sha256") != revision:
        _add(errors, "revision_hash_mismatch", revision)
    if manifest.get("generation") != revision[:20]:
        _add(errors, "generation_id_mismatch", revision[:20])

    payloads: dict[str, object] = {}
    for relative, metadata_value in sorted(files.items()):
        try:
            relative_path = _safe_relative_path(relative)
        except ValueError as exc:
            _add(errors, "manifest_path_invalid", str(exc))
            continue
        metadata = _mapping(metadata_value)
        if metadata is None:
            _add(errors, "file_metadata_invalid", relative)
            continue
        value, raw, file_error = _load_json_file(candidate / relative_path)
        if file_error:
            _add(errors, file_error, relative)
            continue
        assert raw is not None
        if metadata.get("sha256") != sha256_bytes(raw):
            _add(errors, "file_hash_mismatch", relative)
        if metadata.get("bytes") != len(raw):
            _add(errors, "file_size_mismatch", relative)
        if value is not None and raw != canonical_bytes(value):
            _add(errors, "file_not_canonical", relative)
        payloads[relative] = value

    expected_files = {"manifest.json", *files.keys()}
    actual_files = {
        path.relative_to(candidate).as_posix()
        for path in candidate.rglob("*")
        if path.is_file()
    }
    for extra in sorted(actual_files - expected_files):
        _add(errors, "unexpected_file", extra)
    for missing in sorted(expected_files - actual_files):
        _add(errors, "file_missing", missing)

    input_proof: dict[str, object] | None = None
    input_proof_value = payloads.get(INPUT_PROOF_PATH)
    if INPUT_PROOF_PATH not in payloads:
        _add(errors, "input_proof_missing", INPUT_PROOF_PATH)
    else:
        try:
            input_proof = validate_input_proof(input_proof_value)
        except ValueError as exc:
            _add(errors, "input_proof_invalid", str(exc))
        else:
            proof_hash = canonical_hash(input_proof)
            if proof_hash != manifest.get("input_proof_sha256"):
                _add(
                    errors,
                    "input_proof_hash_mismatch",
                    f"expected {manifest.get('input_proof_sha256')!r}, "
                    f"got {proof_hash}",
                )
            proof_documents = _mapping(input_proof.get("documents"))
            if (
                proof_documents is None
                or set(proof_documents) != set(documents)
            ):
                _add(
                    errors,
                    "input_proof_documents_mismatch",
                    "proof documents differ from manifest documents",
                )
            if input_proof.get("compiler_recipe_hash") != manifest.get(
                "compiler_recipe_hash"
            ):
                _add(
                    errors,
                    "input_proof_compiler_recipe_mismatch",
                    "proof compiler recipe differs from manifest",
                )

    segments: list[Mapping[str, object]] = []
    for doc_key, segment_hash in sorted(documents.items()):
        if not isinstance(doc_key, str) or not isinstance(segment_hash, str):
            _add(errors, "segment_reference_invalid", repr((doc_key, segment_hash)))
            continue
        try:
            segment = load_segment(Path(pageindex_dir), segment_hash)
        except Exception as exc:  # validation converts object-store failures to codes
            _add(errors, "segment_object_invalid", f"{doc_key}: {type(exc).__name__}: {exc}")
            continue
        document = _mapping(segment.get("document"))
        if document is None or document.get("doc_key") != doc_key:
            _add(errors, "segment_document_mismatch", doc_key)
        if segment.get("schema_version") != 2:
            _add(errors, "schema_unknown", f"segment {doc_key}")
        _validate_segment_metadata(segment, doc_key, errors)
        _validate_segment_payload(segment, doc_key, errors)
        segments.append(segment)

    if input_proof is not None and len(segments) == len(documents):
        compiler_hash = input_proof.get("compiler_recipe_hash")
        if not isinstance(compiler_hash, str):
            _add(
                errors,
                "input_proof_segment_mismatch",
                "validated proof has no compiler recipe hash",
            )
        else:
            try:
                segment_input_proof = proof_from_segments(
                    segments,
                    compiler_hash,
                )
            except ValueError as exc:
                _add(
                    errors,
                    "input_proof_segment_mismatch",
                    f"cannot derive proof from loaded Segments: {exc}",
                )
            else:
                if segment_input_proof != input_proof:
                    proof_documents = _mapping(input_proof.get("documents")) or {}
                    segment_documents = (
                        _mapping(segment_input_proof.get("documents")) or {}
                    )
                    mismatched = sorted(
                        doc_key
                        for doc_key in set(proof_documents) | set(segment_documents)
                        if proof_documents.get(doc_key)
                        != segment_documents.get(doc_key)
                    )
                    detail = ", ".join(mismatched) or "proof metadata"
                    _add(errors, "input_proof_segment_mismatch", detail)

    try:
        expected = compile_generation(segments, compiler_recipe)
    except Exception as exc:
        _add(errors, "segment_compile_failed", f"{type(exc).__name__}: {exc}")
    else:
        if expected.compiler_recipe_hash != manifest.get("compiler_recipe_hash"):
            _add(errors, "compiler_recipe_mismatch", expected.compiler_recipe_hash)
        if expected.generation_id != manifest.get("generation"):
            _add(errors, "compiled_generation_mismatch", expected.generation_id)
        if expected.manifest.get("input_proof_sha256") != manifest.get(
            "input_proof_sha256"
        ):
            _add(
                errors,
                "input_proof_hash_mismatch",
                "deep recompilation produced a different input proof",
            )
        for relative, expected_payload in expected.payloads.items():
            actual_payload = payloads.get(relative)
            if actual_payload != expected_payload:
                _add(errors, "compiled_payload_mismatch", relative)
        if set(expected.payloads) != set(files):
            _add(errors, "compiled_file_set_mismatch", "manifest files differ from compiled files")
        if manifest.get("stats") != expected.manifest.get("stats"):
            _add(errors, "stats_mismatch", "manifest stats differ from compiled stats")
        if manifest.get("pruning") != expected.manifest.get("pruning"):
            _add(errors, "pruning_mismatch", "field pruning differs from Segment facts")

    _validate_runtime_references(payloads, errors)
    raw_warnings = manifest.get("warnings")
    if isinstance(raw_warnings, list):
        warnings.extend(str(item) for item in raw_warnings)
    return ValidationReport(not errors, tuple(errors), tuple(warnings))


def validate_candidate(
    candidate_dir: Path,
    pageindex_dir: Path,
) -> ValidationReport:
    """Backward-compatible entry point for the legacy Deep validator."""

    return validate_candidate_deep(candidate_dir, pageindex_dir)
