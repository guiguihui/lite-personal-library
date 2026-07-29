"""Materialize and validate complete PageIndex v2 Generation candidates."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from app.retrieval.tokenizer import tokenize

from .canonical import canonical_bytes, canonical_hash, sha256_bytes, write_json_atomic
from .compiler import CompiledGeneration, compile_generation
from .ids import normalize_relative_path
from .models import CompilerRecipe, SegmentRecipe
from .object_store import load_segment

__all__ = ["ValidationReport", "materialize_candidate", "validate_candidate"]


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def validate_candidate(candidate_dir: Path, pageindex_dir: Path) -> ValidationReport:
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
    if manifest.get("schema_version") != 2:
        _add(errors, "schema_unknown", f"manifest schema {manifest.get('schema_version')!r}")

    documents = _mapping(manifest.get("documents"))
    files = _mapping(manifest.get("files"))
    if documents is None:
        _add(errors, "manifest_invalid", "documents must be an object")
        documents = {}
    if files is None:
        _add(errors, "manifest_invalid", "files must be an object")
        files = {}

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
        "schema_version": 2,
        "compiler_recipe_hash": manifest.get("compiler_recipe_hash"),
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

    try:
        expected = compile_generation(segments, compiler_recipe)
    except Exception as exc:
        _add(errors, "segment_compile_failed", f"{type(exc).__name__}: {exc}")
    else:
        if expected.compiler_recipe_hash != manifest.get("compiler_recipe_hash"):
            _add(errors, "compiler_recipe_mismatch", expected.compiler_recipe_hash)
        if expected.generation_id != manifest.get("generation"):
            _add(errors, "compiled_generation_mismatch", expected.generation_id)
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
