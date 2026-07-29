"""Semantic legacy/PageIndex v2 shadow-generation comparison.

The compatibility payloads contain generation-scoped numeric chunk IDs and
legacy node IDs.  Comparing JSON bytes would therefore report harmless
renumbering as data loss.  This module resolves runtime records through the
Generation's Segment manifest and compares stable document, node, and chunk
identities instead.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.retrieval.tokenizer import tokenize

from .compiler import should_prune_body
from .models import CompilerRecipe
from .object_store import load_segment

ChunkKey = tuple[str, str, int]
NodeKey = tuple[str, str]

_CORE_FILES = (
    "global-index.json",
    "node-index.json",
    "chunks.json",
    "inverted-index.json",
)
_TREE_FOLDERS = {"book": "books", "paper": "papers", "note": "notes"}
_LEGACY_STOPWORD_DF_RATIO = 0.35


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"PageIndex file not found: {path}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid PageIndex JSON: {path}: {exc}") from exc


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _sequence(value: object, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise ValueError(f"{field} must be an array")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _body_hash(body: object) -> str:
    return hashlib.sha256(str(body or "").encode("utf-8")).hexdigest()


def _normalize_path(value: object) -> str:
    return str(value or "").replace("\\", "/")


def _normalize_value(value: object) -> object:
    """Return a deterministic, JSON-like comparison value."""

    if isinstance(value, Mapping):
        return {
            str(key): _normalize_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_normalize_value(item) for item in value]
    return value


def _document_projection(document: Mapping[str, Any]) -> dict[str, object]:
    return {
        str(key): _normalize_value(value)
        for key, value in sorted(document.items())
        if key not in {"id", "type"}
    }


def _load_documents(root: Path) -> tuple[
    dict[str, dict[str, object]], dict[str, tuple[str, ...]]
]:
    global_index = _mapping(
        _read_json(root / "global-index.json"), "global-index.json"
    )
    raw_documents = _sequence(global_index.get("docs"), "global-index.docs")
    documents: dict[str, dict[str, object]] = {}
    by_slug: dict[str, list[str]] = defaultdict(list)
    for position, raw in enumerate(raw_documents):
        document = _mapping(raw, f"global-index.docs[{position}]")
        doc_type = _string(document.get("type"), "document.type")
        slug = _string(document.get("id"), "document.id")
        doc_key = f"{doc_type}:{slug}"
        if doc_key in documents:
            raise ValueError(f"duplicate document in {root}: {doc_key}")
        documents[doc_key] = _document_projection(document)
        by_slug[slug].append(doc_key)
    return documents, {
        slug: tuple(sorted(keys)) for slug, keys in sorted(by_slug.items())
    }


def _segment_store_roots(
    legacy_dir: Path, generation_dir: Path
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for start in (generation_dir, legacy_dir):
        candidates.append(start)
        candidates.extend(start.parents)
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "objects" / "segments").is_dir():
            roots.append(resolved)
    return tuple(roots)


def _segment_hash(value: object, doc_key: str) -> str:
    if isinstance(value, str):
        digest = value
    elif isinstance(value, Mapping):
        digest = value.get("segment_hash") or value.get("hash")
    else:
        digest = None
    if not isinstance(digest, str) or not digest:
        raise ValueError(
            f"manifest.documents[{doc_key!r}] must reference a Segment hash"
        )
    return digest


def _load_segments(
    legacy_dir: Path,
    generation_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, object]]:
    documents = _mapping(manifest.get("documents"), "manifest.documents")
    roots = _segment_store_roots(legacy_dir, generation_dir)
    if documents and not roots:
        raise FileNotFoundError(
            "could not locate objects/segments for Generation manifest"
        )

    segments: dict[str, dict[str, object]] = {}
    for doc_key, reference in sorted(documents.items()):
        if not isinstance(doc_key, str) or not doc_key:
            raise ValueError("manifest document keys must be non-empty strings")
        digest = _segment_hash(reference, doc_key)
        last_error: Exception | None = None
        for root in roots:
            try:
                segment = load_segment(root, digest)
            except FileNotFoundError as exc:
                last_error = exc
                continue
            document = _mapping(segment.get("document"), "segment.document")
            if document.get("doc_key") != doc_key:
                raise ValueError(
                    f"Segment {digest} belongs to {document.get('doc_key')!r}, "
                    f"not {doc_key!r}"
                )
            segments[doc_key] = segment
            break
        else:
            raise FileNotFoundError(
                f"Segment object not found for {doc_key}: {digest}"
            ) from last_error
    return segments


def _node_signature(raw: Mapping[str, Any]) -> tuple[str, tuple[str, ...], int]:
    breadcrumb_value = raw.get("breadcrumb") or []
    breadcrumb = tuple(
        str(part)
        for part in _sequence(breadcrumb_value, "node.breadcrumb")
    )
    line_num = raw.get("line_num", 0)
    if isinstance(line_num, bool) or not isinstance(line_num, int):
        line_num = 0
    return (str(raw.get("title") or ""), breadcrumb, line_num)


def _segment_identity_maps(
    segments: Mapping[str, Mapping[str, object]],
) -> tuple[
    dict[tuple[str, str], str],
    dict[tuple[str, tuple[str, tuple[str, ...], int]], tuple[str, ...]],
    dict[ChunkKey, dict[str, object]],
    dict[tuple[str, int], ChunkKey],
    dict[tuple[str, ChunkKey], tuple[int, int, int]],
]:
    node_by_legacy_id: dict[tuple[str, str], str] = {}
    signature_keys: dict[
        tuple[str, tuple[str, tuple[str, ...], int]], set[str]
    ] = defaultdict(set)
    segment_chunks: dict[ChunkKey, dict[str, object]] = {}
    local_chunk_keys: dict[tuple[str, int], ChunkKey] = {}
    field_postings: dict[
        tuple[str, ChunkKey], tuple[int, int, int]
    ] = {}

    for doc_key, segment in sorted(segments.items()):
        raw_nodes = _sequence(segment.get("nodes"), "segment.nodes")
        for position, raw in enumerate(raw_nodes):
            node = _mapping(raw, f"segment.nodes[{position}]")
            node_key = _string(node.get("node_key"), "segment node.node_key")
            legacy_id = _string(
                node.get("legacy_node_id") or node.get("node_id"),
                "segment node.legacy_node_id",
            )
            id_key = (doc_key, legacy_id)
            if id_key in node_by_legacy_id:
                raise ValueError(f"duplicate Segment legacy node ID: {id_key}")
            node_by_legacy_id[id_key] = node_key
            signature_keys[(doc_key, _node_signature(node))].add(node_key)

        raw_chunks = [
            _mapping(raw, f"segment.chunks[{position}]")
            for position, raw in enumerate(
                _sequence(segment.get("chunks"), "segment.chunks")
            )
        ]
        raw_chunks.sort(
            key=lambda chunk: _nonnegative_int(
                chunk.get("local_id"), "segment chunk.local_id"
            )
        )
        next_ordinal: Counter[str] = Counter()
        for chunk in raw_chunks:
            local_id = _nonnegative_int(
                chunk.get("local_id"), "segment chunk.local_id"
            )
            node_key = _string(
                chunk.get("node_key"), "segment chunk.node_key"
            )
            raw_ordinal = chunk.get("node_local_ordinal")
            if (
                isinstance(raw_ordinal, int)
                and not isinstance(raw_ordinal, bool)
                and raw_ordinal >= 0
            ):
                ordinal = raw_ordinal
                next_ordinal[node_key] = max(
                    next_ordinal[node_key], ordinal + 1
                )
            else:
                ordinal = next_ordinal[node_key]
                next_ordinal[node_key] += 1
            key = (doc_key, node_key, ordinal)
            if key in segment_chunks:
                raise ValueError(f"duplicate Segment chunk identity: {key}")
            segment_chunks[key] = {
                "body_sha256": _body_hash(chunk.get("body")),
                "local_id": local_id,
            }
            local_key = (doc_key, local_id)
            if local_key in local_chunk_keys:
                raise ValueError(f"duplicate Segment local chunk ID: {local_key}")
            local_chunk_keys[local_key] = key

        postings = _mapping(segment.get("postings"), "segment.postings")
        for token, raw_rows in sorted(postings.items()):
            if not isinstance(token, str) or not token:
                raise ValueError("Segment posting tokens must be non-empty strings")
            for raw_row in _sequence(raw_rows, f"segment.postings[{token!r}]"):
                row = _sequence(raw_row, f"segment.postings[{token!r}][]")
                if len(row) != 4:
                    raise ValueError(
                        f"Segment posting {token!r} must have four fields"
                    )
                local_id = _nonnegative_int(row[0], "posting.local_id")
                chunk_key = local_chunk_keys.get((doc_key, local_id))
                if chunk_key is None:
                    raise ValueError(
                        f"Segment posting {token!r} references unknown "
                        f"chunk {doc_key}:{local_id}"
                    )
                fields = (
                    _nonnegative_int(row[1], "posting.title_tf"),
                    _nonnegative_int(row[2], "posting.breadcrumb_tf"),
                    _nonnegative_int(row[3], "posting.body_tf"),
                )
                posting_key = (token, chunk_key)
                if posting_key in field_postings:
                    raise ValueError(
                        f"duplicate Segment posting: {token!r} {chunk_key}"
                    )
                field_postings[posting_key] = fields

    signatures = {
        key: tuple(sorted(node_keys))
        for key, node_keys in sorted(signature_keys.items())
    }
    return (
        node_by_legacy_id,
        signatures,
        segment_chunks,
        local_chunk_keys,
        field_postings,
    )


def _resolve_doc_key(
    slug: object,
    by_slug: Mapping[str, tuple[str, ...]],
    node_id: str | None,
    node_by_legacy_id: Mapping[tuple[str, str], str],
) -> str:
    doc_id = _string(slug, "runtime doc_id")
    candidates = by_slug.get(doc_id, ())
    if len(candidates) == 1:
        return candidates[0]
    if node_id:
        resolved = [
            key
            for key in candidates
            if (key, node_id) in node_by_legacy_id
        ]
        if len(resolved) == 1:
            return resolved[0]
    if not candidates:
        return f"unknown:{doc_id}"
    return f"ambiguous:{doc_id}"


def _resolve_node_key(
    doc_key: str,
    node_id: str,
    raw: Mapping[str, Any],
    node_by_legacy_id: Mapping[tuple[str, str], str],
    nodes_by_signature: Mapping[
        tuple[str, tuple[str, tuple[str, ...], int]], tuple[str, ...]
    ],
) -> str:
    mapped = node_by_legacy_id.get((doc_key, node_id))
    if mapped is not None:
        return mapped
    candidates = nodes_by_signature.get((doc_key, _node_signature(raw)), ())
    if len(candidates) == 1:
        return candidates[0]
    return f"legacy:{node_id}"


def _node_projection(raw: Mapping[str, Any]) -> dict[str, object]:
    url = str(raw.get("url") or "").split("#", 1)[0]
    return {
        "title": str(raw.get("title") or ""),
        "breadcrumb": [
            str(part)
            for part in _sequence(
                raw.get("breadcrumb") or [], "node.breadcrumb"
            )
        ],
        "url": url,
        "terms": [
            str(term)
            for term in _sequence(raw.get("terms") or [], "node.terms")
        ],
        "summary": str(raw.get("summary") or ""),
        "line_num": (
            raw.get("line_num")
            if isinstance(raw.get("line_num"), int)
            and not isinstance(raw.get("line_num"), bool)
            else 0
        ),
    }


def _load_nodes(
    root: Path,
    by_slug: Mapping[str, tuple[str, ...]],
    node_by_legacy_id: Mapping[tuple[str, str], str],
    nodes_by_signature: Mapping[
        tuple[str, tuple[str, tuple[str, ...], int]], tuple[str, ...]
    ],
) -> dict[NodeKey, dict[str, object]]:
    node_index = _mapping(
        _read_json(root / "node-index.json"), "node-index.json"
    )
    raw_nodes = _sequence(node_index.get("nodes"), "node-index.nodes")
    nodes: dict[NodeKey, dict[str, object]] = {}
    for position, raw_value in enumerate(raw_nodes):
        raw = _mapping(raw_value, f"node-index.nodes[{position}]")
        node_id = _string(raw.get("node_id"), "node.node_id")
        doc_key = _resolve_doc_key(
            raw.get("doc_id"), by_slug, node_id, node_by_legacy_id
        )
        stable_node_key = _resolve_node_key(
            doc_key,
            node_id,
            raw,
            node_by_legacy_id,
            nodes_by_signature,
        )
        key = (doc_key, stable_node_key)
        if key in nodes:
            raise ValueError(f"duplicate normalized node in {root}: {key}")
        nodes[key] = {
            "projection": _node_projection(raw),
            "legacy_node_id": node_id,
        }
    return nodes


def _numeric_chunk_id(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str):
        digits = value[1:] if value.startswith("c") else value
        if digits.isdigit():
            return int(digits)
    raise ValueError(f"invalid runtime chunk ID: {value!r}")


def _chunk_projection(raw: Mapping[str, Any]) -> dict[str, object]:
    return {
        "title": str(raw.get("title") or ""),
        "breadcrumb": [
            str(part)
            for part in _sequence(
                raw.get("breadcrumb") or [], "chunk.breadcrumb"
            )
        ],
        "body": str(raw.get("body") or ""),
        "source_md": _normalize_path(raw.get("source_md")),
        "line_num": (
            raw.get("line_num")
            if isinstance(raw.get("line_num"), int)
            and not isinstance(raw.get("line_num"), bool)
            else 0
        ),
    }


def _match_runtime_chunk_keys(
    grouped: Mapping[NodeKey, Sequence[Mapping[str, Any]]],
    segment_chunks: Mapping[ChunkKey, Mapping[str, object]],
) -> list[tuple[ChunkKey, Mapping[str, Any]]]:
    segment_by_group: dict[NodeKey, list[ChunkKey]] = defaultdict(list)
    for key in segment_chunks:
        segment_by_group[(key[0], key[1])].append(key)
    for keys in segment_by_group.values():
        keys.sort(key=lambda key: key[2])

    matched: list[tuple[ChunkKey, Mapping[str, Any]]] = []
    for group, values in grouped.items():
        candidates = segment_by_group.get(group, [])
        used: set[ChunkKey] = set()
        for provisional_ordinal, raw in enumerate(values):
            body_sha256 = _body_hash(raw.get("body"))
            exact = (group[0], group[1], provisional_ordinal)
            if (
                exact in segment_chunks
                and exact not in used
                and segment_chunks[exact].get("body_sha256") == body_sha256
            ):
                key = exact
            else:
                body_matches = [
                    candidate
                    for candidate in candidates
                    if candidate not in used
                    and segment_chunks[candidate].get("body_sha256")
                    == body_sha256
                ]
                if len(body_matches) == 1:
                    key = body_matches[0]
                elif exact not in used:
                    key = exact
                else:
                    ordinal = provisional_ordinal
                    while (group[0], group[1], ordinal) in used:
                        ordinal += 1
                    key = (group[0], group[1], ordinal)
            used.add(key)
            matched.append((key, raw))
    return matched


def _field_tf(raw: Mapping[str, Any]) -> dict[str, tuple[int, int, int]]:
    title = Counter(tokenize(str(raw.get("title") or "")))
    breadcrumb = Counter(
        tokenize(
            " ".join(
                str(part)
                for part in _sequence(
                    raw.get("breadcrumb") or [], "chunk.breadcrumb"
                )
            )
        )
    )
    body = Counter(tokenize(str(raw.get("body") or "")))
    return {
        token: (
            int(title.get(token, 0)),
            int(breadcrumb.get(token, 0)),
            int(body.get(token, 0)),
        )
        for token in sorted(set(title) | set(breadcrumb) | set(body))
    }


def _load_chunks(
    root: Path,
    by_slug: Mapping[str, tuple[str, ...]],
    node_by_legacy_id: Mapping[tuple[str, str], str],
    nodes_by_signature: Mapping[
        tuple[str, tuple[str, tuple[str, ...], int]], tuple[str, ...]
    ],
    segment_chunks: Mapping[ChunkKey, Mapping[str, object]],
) -> tuple[
    dict[ChunkKey, dict[str, object]],
    dict[int, ChunkKey],
    dict[tuple[str, ChunkKey], tuple[int, int, int]],
]:
    chunks_index = _mapping(
        _read_json(root / "chunks.json"), "chunks.json"
    )
    raw_chunks = _sequence(chunks_index.get("chunks"), "chunks.chunks")
    grouped: dict[NodeKey, list[Mapping[str, Any]]] = defaultdict(list)
    for position, raw_value in enumerate(raw_chunks):
        raw = _mapping(raw_value, f"chunks.chunks[{position}]")
        node_id = _string(raw.get("node_id"), "chunk.node_id")
        doc_key = _resolve_doc_key(
            raw.get("doc_id"), by_slug, node_id, node_by_legacy_id
        )
        stable_node_key = _resolve_node_key(
            doc_key,
            node_id,
            raw,
            node_by_legacy_id,
            nodes_by_signature,
        )
        grouped[(doc_key, stable_node_key)].append(raw)

    chunks: dict[ChunkKey, dict[str, object]] = {}
    id_to_key: dict[int, ChunkKey] = {}
    reconstructed_fields: dict[
        tuple[str, ChunkKey], tuple[int, int, int]
    ] = {}
    for key, raw in _match_runtime_chunk_keys(grouped, segment_chunks):
        numeric_id = _numeric_chunk_id(raw.get("chunk_id"))
        if key in chunks:
            raise ValueError(f"duplicate normalized chunk in {root}: {key}")
        if numeric_id in id_to_key:
            raise ValueError(
                f"duplicate numeric runtime chunk ID in {root}: {numeric_id}"
            )
        chunks[key] = {
            "projection": _chunk_projection(raw),
            "chunk_id": str(raw.get("chunk_id")),
            "numeric_id": numeric_id,
            "body_sha256": _body_hash(raw.get("body")),
        }
        id_to_key[numeric_id] = key
        for token, fields in _field_tf(raw).items():
            reconstructed_fields[(token, key)] = fields
    return chunks, id_to_key, reconstructed_fields


def _load_postings(
    root: Path,
    id_to_key: Mapping[int, ChunkKey],
) -> tuple[
    dict[str, dict[ChunkKey, int]],
    dict[tuple[str, ChunkKey], int],
]:
    inverted = _mapping(
        _read_json(root / "inverted-index.json"), "inverted-index.json"
    )
    raw_postings = _mapping(
        inverted.get("postings"), "inverted-index.postings"
    )
    postings: dict[str, dict[ChunkKey, int]] = {}
    raw_ids: dict[tuple[str, ChunkKey], int] = {}
    for token, raw_rows in sorted(raw_postings.items()):
        if not isinstance(token, str) or not token:
            raise ValueError("runtime posting tokens must be non-empty strings")
        normalized_rows: dict[ChunkKey, int] = {}
        for raw_row in _sequence(raw_rows, f"postings[{token!r}]"):
            row = _sequence(raw_row, f"postings[{token!r}][]")
            if len(row) != 2:
                raise ValueError(
                    f"runtime posting {token!r} must be [chunk_id, tf]"
                )
            numeric_id = _numeric_chunk_id(row[0])
            tf = _nonnegative_int(row[1], "posting.tf")
            if tf == 0:
                raise ValueError("runtime posting TF must be positive")
            chunk_key = id_to_key.get(numeric_id)
            if chunk_key is None:
                chunk_key = ("@unknown", str(numeric_id), 0)
            if chunk_key in normalized_rows:
                raise ValueError(
                    f"duplicate semantic posting {token!r}: {chunk_key}"
                )
            normalized_rows[chunk_key] = tf
            raw_ids[(token, chunk_key)] = numeric_id
        postings[token] = normalized_rows
    return postings, raw_ids


def _key_detail(key: ChunkKey) -> dict[str, object]:
    return {
        "doc_key": key[0],
        "node_key": key[1],
        "node_local_ordinal": key[2],
    }


def _node_key_label(key: NodeKey) -> str:
    return f"{key[0]}#{key[1]}"


def _chunk_key_label(key: ChunkKey) -> str:
    return f"{key[0]}#{key[1]}@{key[2]}"


def _collection_diff(
    legacy: Mapping[Any, Mapping[str, object]],
    generation: Mapping[Any, Mapping[str, object]],
    *,
    label,
) -> dict[str, object]:
    legacy_keys = set(legacy)
    generation_keys = set(generation)
    missing = sorted(legacy_keys - generation_keys)
    added = sorted(generation_keys - legacy_keys)
    changed: list[dict[str, object]] = []
    id_only_changes = 0
    for key in sorted(legacy_keys & generation_keys):
        left = legacy[key]
        right = generation[key]
        if left.get("projection") != right.get("projection"):
            changed.append(
                {
                    "key": label(key),
                    "legacy": left.get("projection"),
                    "generation": right.get("projection"),
                }
            )
        elif (
            left.get("legacy_node_id") != right.get("legacy_node_id")
            or left.get("chunk_id") != right.get("chunk_id")
        ):
            id_only_changes += 1
    return {
        "legacy_count": len(legacy),
        "generation_count": len(generation),
        "semantic_mismatch": len(missing) + len(added) + len(changed),
        "missing_in_generation": [label(key) for key in missing],
        "added_in_generation": [label(key) for key in added],
        "changed": changed,
        "id_only_changes": id_only_changes,
    }


def _document_diff(
    legacy: Mapping[str, Mapping[str, object]],
    generation: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    legacy_keys = set(legacy)
    generation_keys = set(generation)
    missing = sorted(legacy_keys - generation_keys)
    added = sorted(generation_keys - legacy_keys)
    changed = [
        {
            "doc_key": key,
            "legacy": legacy[key],
            "generation": generation[key],
        }
        for key in sorted(legacy_keys & generation_keys)
        if legacy[key] != generation[key]
    ]
    return {
        "legacy_count": len(legacy),
        "generation_count": len(generation),
        "semantic_mismatch": len(missing) + len(added) + len(changed),
        "missing_in_generation": missing,
        "added_in_generation": added,
        "changed": changed,
    }


def _chunk_diff(
    legacy: Mapping[ChunkKey, Mapping[str, object]],
    generation: Mapping[ChunkKey, Mapping[str, object]],
) -> dict[str, object]:
    report = _collection_diff(
        legacy, generation, label=_chunk_key_label
    )
    changed: list[dict[str, object]] = []
    for key in sorted(set(legacy) & set(generation)):
        left = legacy[key]
        right = generation[key]
        if left.get("projection") == right.get("projection"):
            continue
        detail = {
            **_key_detail(key),
            "legacy_body_sha256": left.get("body_sha256"),
            "generation_body_sha256": right.get("body_sha256"),
            "legacy": left.get("projection"),
            "generation": right.get("projection"),
        }
        changed.append(detail)
    report["changed"] = changed
    return report


def _field_label(title_tf: int, breadcrumb_tf: int) -> str:
    if title_tf and breadcrumb_tf:
        return "title+breadcrumb"
    if title_tf:
        return "title"
    return "breadcrumb"


def _legacy_policy_dropped_tokens(
    segment_fields: Mapping[
        tuple[str, ChunkKey], tuple[int, int, int]
    ],
    *,
    total_chunks: int,
    total_documents: int,
) -> set[str]:
    """Reconstruct the legacy builder's all-field 35% DF filter.

    The legacy builder drops a whole token, including title and breadcrumb
    contributions.  Multi-document libraries use document DF; a one-document
    library falls back to chunk DF.  This reconstruction lets the shadow
    report distinguish an intentional v2 policy change from an unexplained
    Generation mismatch.
    """

    token_documents: dict[str, set[str]] = defaultdict(set)
    token_chunk_df: Counter[str] = Counter()
    for (token, chunk_key), fields in segment_fields.items():
        if sum(fields) <= 0:
            continue
        token_documents[token].add(chunk_key[0])
        token_chunk_df[token] += 1

    if total_documents <= 1:
        cap = int(total_chunks * _LEGACY_STOPWORD_DF_RATIO)
        return {
            token
            for token, chunk_df in token_chunk_df.items()
            if chunk_df > cap
        }
    return {
        token
        for token, documents in token_documents.items()
        if len(documents) / total_documents > _LEGACY_STOPWORD_DF_RATIO
    }


def _postings_diff(
    legacy: Mapping[str, Mapping[ChunkKey, int]],
    generation: Mapping[str, Mapping[ChunkKey, int]],
    legacy_raw_ids: Mapping[tuple[str, ChunkKey], int],
    generation_raw_ids: Mapping[tuple[str, ChunkKey], int],
    segment_fields: Mapping[
        tuple[str, ChunkKey], tuple[int, int, int]
    ],
    legacy_reconstructed_fields: Mapping[
        tuple[str, ChunkKey], tuple[int, int, int]
    ],
    total_chunks: int,
    total_documents: int,
    compiler_recipe: CompilerRecipe,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    body_df: Counter[str] = Counter()
    for (token, _chunk_key), (_title, _breadcrumb, body) in (
        segment_fields.items()
    ):
        if body > 0:
            body_df[token] += 1

    raw_only_legacy = sorted(set(legacy) - set(generation))
    raw_only_generation = sorted(set(generation) - set(legacy))
    details: list[dict[str, object]] = []
    structural_errors: list[dict[str, object]] = []
    structural_pairs: set[tuple[str, ChunkKey]] = set()
    unexpected_tokens: set[str] = set()
    expected_pruned = 0
    expected_legacy_df_policy_delta = 0
    expected_policy_delta = 0
    semantic_mismatch = 0
    unexplained_semantic_mismatch = 0
    observed_differences = 0
    candidate_mismatch = 0
    tf_mismatch = 0
    id_only_changes = 0

    legacy_policy_dropped = _legacy_policy_dropped_tokens(
        segment_fields,
        total_chunks=total_chunks,
        total_documents=total_documents,
    )

    # Validate Generation postings against immutable Segment facts first.
    # This must not depend on the legacy side: legacy and Generation may both
    # omit the same title/breadcrumb posting, which is still a v2 structural
    # error.  Generation compatibility payloads aggregate field TF, so exact
    # expected aggregate TF is the strongest independently observable check.
    generation_pairs = {
        (token, chunk_key)
        for token, rows in generation.items()
        for chunk_key in rows
    }
    for token, chunk_key in sorted(set(segment_fields) | generation_pairs):
        fields = segment_fields.get((token, chunk_key))
        generation_tf = generation.get(token, {}).get(chunk_key)
        legacy_tf = legacy.get(token, {}).get(chunk_key)
        if fields is None:
            detail = {
                "token": token,
                **_key_detail(chunk_key),
                "legacy_tf": legacy_tf,
                "generation_tf": generation_tf,
                "expected_generation_tf": None,
                "field_tf": {"title": 0, "breadcrumb": 0, "body": 0},
                "classification": "structural_error",
                "code": "generation_posting_not_in_segment",
            }
            structural_errors.append(detail)
            structural_pairs.add((token, chunk_key))
            details.append(detail)
            continue

        title_tf, breadcrumb_tf, body_tf = fields
        retained_tf = title_tf + breadcrumb_tf
        prune_body = should_prune_body(
            body_df[token],
            total_chunks,
            min_df=compiler_recipe.body_df_min,
            min_coverage=float(compiler_recipe.body_df_ratio),
        )
        expected_tf = retained_tf + (0 if prune_body else body_tf)
        expected_generation_tf = expected_tf if expected_tf > 0 else None
        if generation_tf == expected_generation_tf:
            continue

        if retained_tf > 0 and (
            generation_tf is None or generation_tf < retained_tf
        ):
            code = "field_posting_lost"
            field = _field_label(title_tf, breadcrumb_tf)
        elif (
            not prune_body
            and body_tf > 0
            and (generation_tf is None or generation_tf < expected_tf)
        ):
            code = "body_posting_pruned_outside_policy"
            field = "body"
        elif prune_body and body_tf > 0 and generation_tf is not None:
            code = "body_pruning_mismatch"
            field = "body"
        else:
            code = "generation_posting_tf_mismatch"
            field = "aggregate"
        detail = {
            "token": token,
            **_key_detail(chunk_key),
            "legacy_tf": legacy_tf,
            "generation_tf": generation_tf,
            "expected_generation_tf": expected_generation_tf,
            "field_tf": {
                "title": title_tf,
                "breadcrumb": breadcrumb_tf,
                "body": body_tf,
            },
            "classification": "structural_error",
            "code": code,
            "field": field,
            "body_df": body_df[token],
            "total_chunks": total_chunks,
            "body_df_min": compiler_recipe.body_df_min,
            "body_df_ratio": float(compiler_recipe.body_df_ratio),
        }
        structural_errors.append(detail)
        structural_pairs.add((token, chunk_key))
        details.append(detail)

    for token in sorted(set(legacy) | set(generation)):
        legacy_rows = legacy.get(token, {})
        generation_rows = generation.get(token, {})
        for chunk_key in sorted(set(legacy_rows) | set(generation_rows)):
            legacy_tf = legacy_rows.get(chunk_key)
            generation_tf = generation_rows.get(chunk_key)
            if legacy_tf == generation_tf:
                if (
                    legacy_tf is not None
                    and legacy_raw_ids.get((token, chunk_key))
                    != generation_raw_ids.get((token, chunk_key))
                ):
                    id_only_changes += 1
                continue

            observed_differences += 1

            fields = segment_fields.get((token, chunk_key))
            if fields is None:
                fields = legacy_reconstructed_fields.get(
                    (token, chunk_key), (0, 0, 0)
                )
            title_tf, breadcrumb_tf, body_tf = fields
            retained_tf = title_tf + breadcrumb_tf
            unfiltered_tf = retained_tf + body_tf
            prune_body = should_prune_body(
                body_df[token],
                total_chunks,
                min_df=compiler_recipe.body_df_min,
                min_coverage=float(compiler_recipe.body_df_ratio),
            )
            expected_generation_tf = retained_tf + (
                0 if prune_body else body_tf
            )
            base_detail: dict[str, object] = {
                "token": token,
                **_key_detail(chunk_key),
                "legacy_tf": legacy_tf,
                "generation_tf": generation_tf,
                "field_tf": {
                    "title": title_tf,
                    "breadcrumb": breadcrumb_tf,
                    "body": body_tf,
                },
            }

            if (token, chunk_key) in structural_pairs:
                semantic_mismatch += 1
                unexplained_semantic_mismatch += 1
                unexpected_tokens.add(token)
                if legacy_tf is None or generation_tf is None:
                    candidate_mismatch += 1
                else:
                    tf_mismatch += 1
                continue

            expected_generation_value = (
                expected_generation_tf
                if expected_generation_tf > 0
                else None
            )
            if (
                token in legacy_policy_dropped
                and token not in legacy
                and generation_tf == expected_generation_value
            ):
                expected_legacy_df_policy_delta += 1
                expected_policy_delta += 1
                semantic_mismatch += 1
                unexpected_tokens.add(token)
                candidate_mismatch += 1
                details.append(
                    {
                        **base_detail,
                        "classification": "expected_legacy_df_policy_delta",
                        "legacy_df_ratio": _LEGACY_STOPWORD_DF_RATIO,
                        "total_documents": total_documents,
                    }
                )
                continue

            if (
                prune_body
                and body_tf > 0
                and legacy_tf == unfiltered_tf
                and (
                    generation_tf == expected_generation_tf
                    or (
                        expected_generation_tf == 0
                        and generation_tf is None
                    )
                )
            ):
                expected_pruned += 1
                expected_policy_delta += 1
                details.append(
                    {
                        **base_detail,
                        "classification": "expected_pruned",
                        "body_df": body_df[token],
                        "total_chunks": total_chunks,
                    }
                )
                continue

            classification = (
                "semantic_candidate_mismatch"
                if legacy_tf is None or generation_tf is None
                else "tf_mismatch"
            )
            details.append(
                {**base_detail, "classification": classification}
            )
            semantic_mismatch += 1
            unexplained_semantic_mismatch += 1
            unexpected_tokens.add(token)
            if classification == "semantic_candidate_mismatch":
                candidate_mismatch += 1
            else:
                tf_mismatch += 1

    report: dict[str, object] = {
        "legacy_tokens": len(legacy),
        "generation_tokens": len(generation),
        "token_mismatch": len(unexpected_tokens),
        "tokens_only_legacy": raw_only_legacy,
        "tokens_only_generation": raw_only_generation,
        "semantic_candidate_mismatch": candidate_mismatch,
        "candidate_mismatch": candidate_mismatch,
        "tf_mismatch": tf_mismatch,
        "semantic_mismatch": semantic_mismatch,
        "semantic_equal": observed_differences == 0,
        "structural_ok": not structural_errors,
        "expected_policy_delta": expected_policy_delta,
        "expected_legacy_df_policy_delta": expected_legacy_df_policy_delta,
        "unexplained_semantic_mismatch": unexplained_semantic_mismatch,
        "publish_blocking_errors": len(structural_errors),
        "id_only_changes": id_only_changes,
        "expected_pruned": expected_pruned,
        "structural_errors": len(structural_errors),
        "details": details,
    }
    return report, structural_errors


def _tree_paths(documents: Mapping[str, Mapping[str, object]]) -> set[str]:
    paths: set[str] = set()
    for doc_key in documents:
        doc_type, separator, slug = doc_key.partition(":")
        if not separator or doc_type not in _TREE_FOLDERS:
            raise ValueError(f"unsupported document key: {doc_key!r}")
        if not slug or "/" in slug or "\\" in slug or slug in {".", ".."}:
            raise ValueError(f"unsafe document slug: {slug!r}")
        paths.add(f"{_TREE_FOLDERS[doc_type]}/{slug}.json")
    return paths


def _actual_tree_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for folder in _TREE_FOLDERS.values():
        directory = root / folder
        if not directory.is_dir():
            continue
        paths.update(
            path.relative_to(root).as_posix()
            for path in directory.glob("*.json")
            if path.is_file()
        )
    return paths


def _tree_diff(
    legacy_dir: Path,
    generation_dir: Path,
    legacy_documents: Mapping[str, Mapping[str, object]],
    generation_documents: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    legacy_referenced = _tree_paths(legacy_documents)
    generation_referenced = _tree_paths(generation_documents)
    all_referenced = legacy_referenced | generation_referenced
    missing: list[str] = []
    added: list[str] = []
    changed: list[str] = []
    for relative in sorted(all_referenced):
        legacy_path = legacy_dir / relative
        generation_path = generation_dir / relative
        if not legacy_path.is_file() and generation_path.is_file():
            added.append(relative)
            continue
        if legacy_path.is_file() and not generation_path.is_file():
            missing.append(relative)
            continue
        if not legacy_path.is_file() and not generation_path.is_file():
            # Both sides reference a missing tree. Keep it visible as one
            # semantic mismatch instead of silently treating it as equal.
            changed.append(relative)
            continue
        if _normalize_value(_read_json(legacy_path)) != _normalize_value(
            _read_json(generation_path)
        ):
            changed.append(relative)

    actual_legacy = _actual_tree_paths(legacy_dir)
    stale = sorted(actual_legacy - legacy_referenced)
    stale_bytes = sum((legacy_dir / path).stat().st_size for path in stale)
    actual_generation = _actual_tree_paths(generation_dir)
    generation_stale = sorted(actual_generation - generation_referenced)
    return {
        "legacy_referenced": len(legacy_referenced),
        "generation_referenced": len(generation_referenced),
        "semantic_mismatch": len(missing) + len(added) + len(changed),
        "missing_in_generation": missing,
        "added_in_generation": added,
        "changed": changed,
        "stale_legacy_files": len(stale),
        "stale_legacy_paths": stale,
        "stale_legacy_bytes": stale_bytes,
        "stale_generation_files": len(generation_stale),
        "stale_generation_paths": generation_stale,
    }


def _index_bytes(root: Path, referenced_trees: set[str]) -> int:
    paths = [root / name for name in _CORE_FILES]
    paths.extend(root / relative for relative in sorted(referenced_trees))
    return sum(path.stat().st_size for path in paths if path.is_file())


def _all_runtime_json_bytes(root: Path) -> int:
    paths = [root / name for name in _CORE_FILES]
    paths.extend(
        root / relative for relative in sorted(_actual_tree_paths(root))
    )
    manifest = root / "manifest.json"
    if manifest.is_file():
        paths.append(manifest)
    return sum(path.stat().st_size for path in paths if path.is_file())


def _duration_ms(manifest: Mapping[str, Any]) -> object:
    candidates: list[object] = [manifest.get("duration_ms")]
    for key in ("stats", "build", "metrics"):
        value = manifest.get(key)
        if isinstance(value, Mapping):
            candidates.append(value.get("duration_ms"))
            candidates.append(value.get("build_duration_ms"))
    for value in candidates:
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
        ):
            return value
    return None


def compare_legacy_to_generation(
    legacy_dir: Path, generation_dir: Path
) -> dict[str, object]:
    """Compare one legacy PageIndex directory with a v2 Generation.

    Global chunk IDs are diagnostic only.  Documents use ``type:id``;
    Segment-backed nodes use stable ``node_key``; chunks use
    ``doc_key + node_key + node-local ordinal`` with a body SHA-256
    diagnostic.  Generation postings are independently checked against
    Segment facts using the compiler recipe recorded in the manifest.  Known
    legacy/v2 policy changes remain visible but never become publish blockers.
    """

    legacy_root = Path(legacy_dir).resolve()
    generation_root = Path(generation_dir).resolve()
    manifest = _mapping(
        _read_json(generation_root / "manifest.json"), "manifest.json"
    )
    raw_compiler_recipe = manifest.get("compiler_recipe")
    if raw_compiler_recipe is None:
        compiler_recipe = CompilerRecipe()
    else:
        recipe_payload = _mapping(
            raw_compiler_recipe, "manifest.compiler_recipe"
        )
        try:
            compiler_recipe = CompilerRecipe(**dict(recipe_payload))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid manifest compiler recipe: {exc}") from exc

    legacy_documents, legacy_by_slug = _load_documents(legacy_root)
    generation_documents, generation_by_slug = _load_documents(
        generation_root
    )
    combined_by_slug: dict[str, tuple[str, ...]] = {}
    for slug in sorted(set(legacy_by_slug) | set(generation_by_slug)):
        combined_by_slug[slug] = tuple(
            sorted(
                set(legacy_by_slug.get(slug, ()))
                | set(generation_by_slug.get(slug, ()))
            )
        )

    segments = _load_segments(legacy_root, generation_root, manifest)
    (
        node_by_legacy_id,
        nodes_by_signature,
        segment_chunks,
        _local_chunk_keys,
        segment_fields,
    ) = _segment_identity_maps(segments)

    legacy_nodes = _load_nodes(
        legacy_root,
        combined_by_slug,
        node_by_legacy_id,
        nodes_by_signature,
    )
    generation_nodes = _load_nodes(
        generation_root,
        combined_by_slug,
        node_by_legacy_id,
        nodes_by_signature,
    )
    legacy_chunks, legacy_chunk_ids, legacy_reconstructed_fields = (
        _load_chunks(
            legacy_root,
            combined_by_slug,
            node_by_legacy_id,
            nodes_by_signature,
            segment_chunks,
        )
    )
    generation_chunks, generation_chunk_ids, _generation_fields = (
        _load_chunks(
            generation_root,
            combined_by_slug,
            node_by_legacy_id,
            nodes_by_signature,
            segment_chunks,
        )
    )
    legacy_postings, legacy_posting_ids = _load_postings(
        legacy_root, legacy_chunk_ids
    )
    generation_postings, generation_posting_ids = _load_postings(
        generation_root, generation_chunk_ids
    )

    documents_report = _document_diff(
        legacy_documents, generation_documents
    )
    nodes_report = _collection_diff(
        legacy_nodes, generation_nodes, label=_node_key_label
    )
    chunks_report = _chunk_diff(legacy_chunks, generation_chunks)
    postings_report, structural_errors = _postings_diff(
        legacy_postings,
        generation_postings,
        legacy_posting_ids,
        generation_posting_ids,
        segment_fields,
        legacy_reconstructed_fields,
        len(segment_chunks),
        len(segments),
        compiler_recipe,
    )
    trees_report = _tree_diff(
        legacy_root,
        generation_root,
        legacy_documents,
        generation_documents,
    )

    generation_warnings = manifest.get("warnings")
    if not isinstance(generation_warnings, list):
        generation_warnings = []
    legacy_tree_paths = _tree_paths(legacy_documents)
    generation_tree_paths = _tree_paths(generation_documents)
    legacy_index_bytes = _index_bytes(legacy_root, legacy_tree_paths)
    generation_index_bytes = _index_bytes(
        generation_root, generation_tree_paths
    )
    metrics: dict[str, object] = {
        "index_bytes": {
            "legacy": legacy_index_bytes,
            "generation": generation_index_bytes,
            "delta": generation_index_bytes - legacy_index_bytes,
            "legacy_including_stale": _all_runtime_json_bytes(legacy_root),
            "generation_all": _all_runtime_json_bytes(generation_root),
        },
        "counts": {
            "legacy": {
                "documents": len(legacy_documents),
                "nodes": len(legacy_nodes),
                "chunks": len(legacy_chunks),
                "tokens": len(legacy_postings),
                "postings": sum(
                    len(rows) for rows in legacy_postings.values()
                ),
            },
            "generation": {
                "documents": len(generation_documents),
                "nodes": len(generation_nodes),
                "chunks": len(generation_chunks),
                "tokens": len(generation_postings),
                "postings": sum(
                    len(rows) for rows in generation_postings.values()
                ),
            },
        },
        "build_duration_ms": {
            "legacy": None,
            "generation": _duration_ms(manifest),
        },
        "warning_totals": {
            "legacy": 0,
            "generation": len(generation_warnings),
        },
    }

    semantic_mismatch = sum(
        int(section["semantic_mismatch"])
        for section in (
            documents_report,
            nodes_report,
            chunks_report,
            postings_report,
            trees_report,
        )
    )
    non_posting_mismatch = sum(
        int(section["semantic_mismatch"])
        for section in (
            documents_report,
            nodes_report,
            chunks_report,
            trees_report,
        )
    )
    expected_policy_delta = int(
        postings_report["expected_policy_delta"]
    )
    unexplained_semantic_mismatch = non_posting_mismatch + int(
        postings_report["unexplained_semantic_mismatch"]
    )
    structural_ok = not structural_errors
    semantic_equal = (
        non_posting_mismatch == 0
        and bool(postings_report["semantic_equal"])
    )
    publish_blocking_errors = len(structural_errors)
    return {
        "schema_version": 1,
        "generation": manifest.get("generation"),
        # Compatibility field: expected policy changes do not make a shadow
        # Generation unsafe.  Callers needing byte-for-byte semantics should
        # use ``semantic_equal`` instead.
        "ok": structural_ok and unexplained_semantic_mismatch == 0,
        "semantic_mismatch": semantic_mismatch,
        "semantic_equal": semantic_equal,
        "structural_ok": structural_ok,
        "expected_policy_delta": expected_policy_delta,
        "unexplained_semantic_mismatch": unexplained_semantic_mismatch,
        "publish_blocking_errors": publish_blocking_errors,
        "documents": documents_report,
        "nodes": nodes_report,
        "chunks": chunks_report,
        "postings": postings_report,
        "document_trees": trees_report,
        "metrics": metrics,
        "structural_errors": structural_errors,
        "warnings": [str(value) for value in generation_warnings],
    }


__all__ = ["compare_legacy_to_generation"]
