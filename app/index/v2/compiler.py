"""Deterministic compiler from PageIndex v2 Segments to runtime JSON."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_bytes, canonical_hash
from .models import CompilerRecipe


BODY_DF_MIN = 256
BODY_COVERAGE_MIN = 0.90
_DOC_TYPE_ORDER = {"book": 0, "paper": 1, "note": 2}
_TERM_SPLIT_RE = re.compile(r"[\s·\-\—\.,;:!?()\[\]{}]+")


@dataclass(frozen=True)
class CompiledGeneration:
    """A deterministic Generation ready to be materialized and validated."""

    generation_id: str
    revision_sha256: str
    compiler_recipe_hash: str
    manifest: dict[str, object]
    payloads: dict[str, object]

    @property
    def files(self) -> dict[str, object]:
        """Compatibility alias for the runtime payload mapping."""

        return self.payloads

    def all_payloads(self) -> dict[str, object]:
        """Return runtime payloads plus ``manifest.json``."""

        return {"manifest.json": self.manifest, **self.payloads}


def should_prune_body(
    body_df: int,
    total_chunks: int,
    *,
    min_df: int = BODY_DF_MIN,
    min_coverage: float = BODY_COVERAGE_MIN,
) -> bool:
    """Return whether a token's body field meets both pruning thresholds."""

    if body_df < 0 or total_chunks < 0:
        raise ValueError("body_df and total_chunks must be non-negative")
    if total_chunks == 0:
        return False
    if min_df < 1 or not 0.0 <= min_coverage <= 1.0:
        raise ValueError("invalid body pruning thresholds")
    return body_df >= min_df and body_df / total_chunks >= min_coverage


def _required_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _required_sequence(value: object, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise ValueError(f"{name} must be a sequence")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _document_sort_key(
    segment: Mapping[str, object],
) -> tuple[int, str, str]:
    document = _required_mapping(segment.get("document"), "segment.document")
    doc_type = _required_string(document.get("type"), "document.type")
    slug = _required_string(document.get("id"), "document.id")
    doc_key = _required_string(document.get("doc_key"), "document.doc_key")
    return (_DOC_TYPE_ORDER.get(doc_type, len(_DOC_TYPE_ORDER)), slug, doc_key)


def _legacy_node_sort_key(node: Mapping[str, Any]) -> tuple[int, int | str, str]:
    legacy_id = str(node.get("legacy_node_id") or node.get("node_id") or "")
    if legacy_id.isdigit():
        return (0, int(legacy_id), legacy_id)
    return (1, legacy_id, legacy_id)


def _extract_terms(title: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for part in _TERM_SPLIT_RE.split(title):
        part = part.strip()
        if len(part) >= 2 and part not in seen:
            terms.append(part)
            seen.add(part)
    return terms


def _default_document_url(doc_type: str, slug: str) -> tuple[str, str]:
    if doc_type == "book":
        return (f"/books/{slug}/", f"/books/{slug}.html")
    if doc_type == "paper":
        return (f"/papers/{slug}/", f"/papers/{slug}.html")
    if doc_type == "note":
        return ("/notes/", f"/notes/{slug}.html")
    raise ValueError(f"unsupported document type: {doc_type}")


def _global_document(document: Mapping[str, Any]) -> dict[str, object]:
    doc_type = _required_string(document.get("type"), "document.type")
    slug = _required_string(document.get("id"), "document.id")
    default_path, default_url = _default_document_url(doc_type, slug)
    result: dict[str, object] = {
        "id": slug,
        "type": doc_type,
        "title": str(document.get("title") or slug),
        "author": str(document.get("author") or ""),
        "description": str(document.get("description") or ""),
        "tags": copy.deepcopy(document.get("tags") or []),
        "path": str(document.get("path") or default_path),
        "url": str(document.get("url") or default_url),
    }
    if not isinstance(result["tags"], list):
        raise ValueError("document.tags must be a list")
    if doc_type == "paper":
        result["year"] = copy.deepcopy(document.get("year") or "")
    elif doc_type == "note":
        result["date"] = str(document.get("date") or "")
        result["source_type"] = str(document.get("source_type") or "")
        result["source_title"] = str(document.get("source_title") or "")
    return result


def _node_url(
    node: Mapping[str, Any], doc_type: str, slug: str, legacy_node_id: str
) -> str:
    explicit = node.get("url")
    if isinstance(explicit, str) and explicit:
        return explicit
    if doc_type == "book":
        source_md = str(node.get("source_md") or "")
        filename = source_md.replace("\\", "/").rsplit("/", 1)[-1]
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        page = f"{stem}.html" if stem and stem != "_index" else "index.html"
        base = f"/books/{slug}/{page}"
    elif doc_type == "paper":
        base = f"/papers/{slug}/index.html"
    else:
        base = f"/notes/{slug}.html"
    return f"{base}#pi-node-{legacy_node_id}"


def _node_payload(
    node: Mapping[str, Any], doc_type: str, slug: str
) -> dict[str, object]:
    legacy_node_id = _required_string(
        node.get("legacy_node_id") or node.get("node_id"),
        "node.legacy_node_id",
    )
    title = str(node.get("title") or "")
    breadcrumb = copy.deepcopy(node.get("breadcrumb") or [])
    if not isinstance(breadcrumb, list) or not all(
        isinstance(part, str) for part in breadcrumb
    ):
        raise ValueError("node.breadcrumb must be a list of strings")
    terms = copy.deepcopy(node.get("terms"))
    if terms is None:
        terms = _extract_terms(title)
    if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
        raise ValueError("node.terms must be a list of strings")
    return {
        "doc_id": slug,
        "node_id": legacy_node_id,
        "title": title,
        "breadcrumb": breadcrumb,
        "url": _node_url(node, doc_type, slug, legacy_node_id),
        "terms": terms,
        "summary": str(node.get("summary") or ""),
        "line_num": _nonnegative_int(node.get("line_num", 0), "node.line_num"),
    }


def _tree_payload(
    segment: Mapping[str, object], doc_type: str, slug: str
) -> tuple[str, object]:
    tree = segment.get("document_tree")
    if not isinstance(tree, Mapping):
        raise ValueError("segment.document_tree must be a mapping")
    if "/" in slug or "\\" in slug or slug in {".", ".."}:
        raise ValueError(f"unsafe document id: {slug!r}")
    folder = {"book": "books", "paper": "papers", "note": "notes"}[doc_type]
    return f"{folder}/{slug}.json", copy.deepcopy(dict(tree))


def compile_generation(
    segments: Sequence[Mapping[str, object]],
    recipe: CompilerRecipe,
) -> CompiledGeneration:
    """Compile a complete Segment set into deterministic compatibility JSON.

    Segment order is ignored. Global numeric chunk IDs are scoped to the
    returned Generation and allocated from one in deterministic order.
    """

    if not hasattr(recipe, "as_dict"):
        raise TypeError("recipe must provide as_dict()")
    recipe_payload = recipe.as_dict()
    if not isinstance(recipe_payload, Mapping):
        raise TypeError("recipe.as_dict() must return a mapping")
    compiler_recipe_hash = canonical_hash(recipe_payload)

    ordered_segments = sorted(tuple(segments), key=_document_sort_key)
    document_hashes: dict[str, str] = {}
    segment_records: list[
        tuple[Mapping[str, object], Mapping[str, Any], str, str, str]
    ] = []
    for segment in ordered_segments:
        if not isinstance(segment, Mapping):
            raise ValueError("each segment must be a mapping")
        if segment.get("schema_version") != 2:
            raise ValueError("segment.schema_version must be 2")
        document = _required_mapping(segment.get("document"), "segment.document")
        doc_key = _required_string(document.get("doc_key"), "document.doc_key")
        doc_type = _required_string(document.get("type"), "document.type")
        slug = _required_string(document.get("id"), "document.id")
        if doc_type not in _DOC_TYPE_ORDER:
            raise ValueError(f"unsupported document type: {doc_type}")
        if doc_key != f"{doc_type}:{slug}":
            raise ValueError(
                f"document.doc_key must equal '{doc_type}:{slug}', got {doc_key!r}"
            )
        if doc_key in document_hashes:
            raise ValueError(f"duplicate document: {doc_key}")
        document_hashes[doc_key] = canonical_hash(segment)
        segment_records.append((segment, document, doc_key, doc_type, slug))

    core_manifest: dict[str, object] = {
        "schema_version": 2,
        "compiler_recipe_hash": compiler_recipe_hash,
        "documents": document_hashes,
    }
    revision_sha256 = canonical_hash(core_manifest)
    generation_id = revision_sha256[:20]

    global_documents: list[dict[str, object]] = []
    global_nodes: list[dict[str, object]] = []
    global_chunks: list[dict[str, object]] = []
    tree_payloads: dict[str, object] = {}
    global_chunk_ids: dict[tuple[str, int], int] = {}

    # Normalized unfiltered postings: token -> (global id, title, breadcrumb, body).
    normalized_postings: dict[str, list[tuple[int, int, int, int]]] = {}
    seen_token_chunks: dict[str, set[int]] = {}

    for segment, document, doc_key, doc_type, slug in segment_records:
        global_documents.append(_global_document(document))

        nodes_value = _required_sequence(segment.get("nodes"), "segment.nodes")
        nodes: list[Mapping[str, Any]] = []
        node_by_key: dict[str, Mapping[str, Any]] = {}
        for value in nodes_value:
            node = _required_mapping(value, "segment.nodes[]")
            node_key = _required_string(node.get("node_key"), "node.node_key")
            if node_key in node_by_key:
                raise ValueError(f"duplicate node_key in {doc_key}: {node_key}")
            node_by_key[node_key] = node
            nodes.append(node)
        nodes.sort(key=_legacy_node_sort_key)
        global_nodes.extend(
            _node_payload(node, doc_type, slug) for node in nodes
        )

        tree_path, tree = _tree_payload(segment, doc_type, slug)
        tree_payloads[tree_path] = tree

        chunks_value = _required_sequence(segment.get("chunks"), "segment.chunks")
        chunks: list[Mapping[str, Any]] = [
            _required_mapping(value, "segment.chunks[]") for value in chunks_value
        ]
        chunks.sort(
            key=lambda chunk: (
                _required_string(chunk.get("node_key"), "chunk.node_key"),
                _nonnegative_int(chunk.get("local_id"), "chunk.local_id"),
            )
        )
        seen_local_ids: set[int] = set()
        for chunk in chunks:
            local_id = _nonnegative_int(chunk.get("local_id"), "chunk.local_id")
            if local_id in seen_local_ids:
                raise ValueError(f"duplicate chunk local_id in {doc_key}: {local_id}")
            seen_local_ids.add(local_id)
            node_key = _required_string(chunk.get("node_key"), "chunk.node_key")
            node = node_by_key.get(node_key)
            if node is None:
                raise ValueError(
                    f"chunk {doc_key}:{local_id} references unknown node {node_key}"
                )
            global_id = len(global_chunks) + 1
            global_chunk_ids[(doc_key, local_id)] = global_id
            legacy_node_id = _required_string(
                node.get("legacy_node_id") or node.get("node_id"),
                "node.legacy_node_id",
            )
            breadcrumb = copy.deepcopy(chunk.get("breadcrumb") or [])
            if not isinstance(breadcrumb, list) or not all(
                isinstance(part, str) for part in breadcrumb
            ):
                raise ValueError("chunk.breadcrumb must be a list of strings")
            global_chunks.append(
                {
                    "chunk_id": f"c{global_id:06d}",
                    "doc_id": slug,
                    "node_id": legacy_node_id,
                    "title": str(chunk.get("title") or ""),
                    "breadcrumb": breadcrumb,
                    "body": str(chunk.get("body") or ""),
                    "source_md": str(chunk.get("source_md") or ""),
                    "line_num": _nonnegative_int(
                        chunk.get("line_num", 0), "chunk.line_num"
                    ),
                }
            )

        postings = _required_mapping(segment.get("postings"), "segment.postings")
        for token, posting_values in postings.items():
            token = _required_string(token, "posting token")
            posting_sequence = _required_sequence(
                posting_values, f"postings[{token!r}]"
            )
            target = normalized_postings.setdefault(token, [])
            token_chunks = seen_token_chunks.setdefault(token, set())
            for item in posting_sequence:
                fields = _required_sequence(item, f"postings[{token!r}][]")
                if len(fields) != 4:
                    raise ValueError(
                        f"posting for {token!r} must contain "
                        "[local_id, title_tf, breadcrumb_tf, body_tf]"
                    )
                local_id = _nonnegative_int(fields[0], "posting.local_id")
                global_id = global_chunk_ids.get((doc_key, local_id))
                if global_id is None:
                    raise ValueError(
                        f"posting for {token!r} references unknown chunk "
                        f"{doc_key}:{local_id}"
                    )
                if global_id in token_chunks:
                    raise ValueError(
                        f"duplicate posting for {token!r} and chunk {global_id}"
                    )
                title_tf = _nonnegative_int(fields[1], "posting.title_tf")
                breadcrumb_tf = _nonnegative_int(
                    fields[2], "posting.breadcrumb_tf"
                )
                body_tf = _nonnegative_int(fields[3], "posting.body_tf")
                if title_tf + breadcrumb_tf + body_tf == 0:
                    raise ValueError(
                        f"posting for {token!r} and chunk {global_id} has zero TF"
                    )
                target.append((global_id, title_tf, breadcrumb_tf, body_tf))
                token_chunks.add(global_id)

    total_chunks = len(global_chunks)
    unpruned_export: dict[str, list[list[int]]] = {}
    exported_postings: dict[str, list[list[int]]] = {}
    body_tokens_pruned = 0
    body_postings_pruned = 0
    body_tf_pruned = 0
    postings_before = 0

    for token in sorted(normalized_postings):
        rows = sorted(normalized_postings[token], key=lambda row: row[0])
        postings_before += len(rows)
        body_df = sum(1 for _, _, _, body_tf in rows if body_tf > 0)
        prune_body = should_prune_body(
            body_df,
            total_chunks,
            min_df=recipe.body_df_min,
            min_coverage=float(recipe.body_df_ratio),
        )
        if prune_body:
            body_tokens_pruned += 1

        unpruned_rows: list[list[int]] = []
        exported_rows: list[list[int]] = []
        for chunk_id, title_tf, breadcrumb_tf, body_tf in rows:
            unpruned_rows.append(
                [chunk_id, title_tf + breadcrumb_tf + body_tf]
            )
            if prune_body and body_tf:
                body_postings_pruned += 1
                body_tf_pruned += body_tf
            total_tf = title_tf + breadcrumb_tf + (
                0 if prune_body else body_tf
            )
            # Title and breadcrumb contributions always survive DF pruning.
            if total_tf > 0:
                exported_rows.append([chunk_id, total_tf])
        if unpruned_rows:
            unpruned_export[token] = unpruned_rows
        if exported_rows:
            exported_postings[token] = exported_rows

    inverted_payload: dict[str, object] = {
        "postings": exported_postings,
        "num_chunks": total_chunks,
    }
    payloads: dict[str, object] = {
        "global-index.json": {"docs": global_documents},
        "node-index.json": {"nodes": global_nodes},
        "chunks.json": {"chunks": global_chunks},
        "inverted-index.json": inverted_payload,
        **tree_payloads,
    }
    payloads = dict(sorted(payloads.items()))

    file_metadata: dict[str, object] = {}
    for relative_path, payload in payloads.items():
        encoded = canonical_bytes(payload)
        file_metadata[relative_path] = {
            "sha256": canonical_hash(payload),
            "bytes": len(encoded),
        }

    postings_after = sum(len(rows) for rows in exported_postings.values())
    before_bytes = len(
        canonical_bytes({"postings": unpruned_export, "num_chunks": total_chunks})
    )
    after_bytes = len(canonical_bytes(inverted_payload))
    manifest: dict[str, object] = {
        "schema_version": 2,
        "generation": generation_id,
        "revision_sha256": revision_sha256,
        "compiler_recipe_hash": compiler_recipe_hash,
        "compiler_recipe": dict(recipe_payload),
        "documents": document_hashes,
        "files": file_metadata,
        "stats": {
            "documents": len(global_documents),
            "nodes": len(global_nodes),
            "chunks": total_chunks,
            "tokens": len(exported_postings),
            "postings": postings_after,
        },
        "pruning": {
            "body_min_df": recipe.body_df_min,
            "body_min_coverage": float(recipe.body_df_ratio),
            "tokens_before": len(normalized_postings),
            "tokens_after": len(exported_postings),
            "postings_before": postings_before,
            "postings_after": postings_after,
            "body_tokens_pruned": body_tokens_pruned,
            "body_postings_pruned": body_postings_pruned,
            "body_tf_pruned": body_tf_pruned,
            "estimated_bytes_saved": max(0, before_bytes - after_bytes),
        },
        "warnings": [],
    }

    return CompiledGeneration(
        generation_id=generation_id,
        revision_sha256=revision_sha256,
        compiler_recipe_hash=compiler_recipe_hash,
        manifest=manifest,
        payloads=payloads,
    )


__all__ = [
    "BODY_COVERAGE_MIN",
    "BODY_DF_MIN",
    "CompiledGeneration",
    "compile_generation",
    "should_prune_body",
]
