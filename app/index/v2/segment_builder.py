"""Build immutable, field-aware PageIndex v2 document Segments.

The legacy builder remains the compatibility parser for Stage A.  This module
adapts its document tree and chunk splitting behavior into a deterministic
Segment without reusing its mutable global postings or sequential global IDs.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.retrieval.tokenizer import tokenize
from app.vendor import build_pageindex as legacy

from .canonical import canonical_hash
from .catalog import DocumentSource, source_file_records
from .ids import make_node_key, normalize_relative_path
from .models import SegmentRecipe

__all__ = ["SegmentBuildError", "build_segment"]


class SegmentBuildError(RuntimeError):
    """The source document could not be converted into a valid Segment."""


def _process_legacy_document(
    source: DocumentSource,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run only the legacy parser/tree adapter with deterministic summaries."""

    saved_model = legacy.LLM_MODEL
    legacy.LLM_MODEL = ""
    try:
        if source.doc_type == "book":
            result = legacy.process_book(
                source.slug,
                str(Path(source.root) / "books" / source.slug),
            )
        elif source.doc_type == "paper":
            result = legacy.process_paper(
                source.slug,
                str(Path(source.root) / "papers" / source.slug),
            )
        elif source.doc_type == "note":
            note_path = Path(source.root) / "notes" / f"{source.slug}.md"
            result = legacy.process_note(source.slug, str(note_path))
        else:
            raise SegmentBuildError(f"unsupported document type: {source.doc_type}")
    finally:
        legacy.LLM_MODEL = saved_model

    if len(result) != 3:
        raise SegmentBuildError(
            f"legacy parser returned {len(result)} values for {source.doc_key}; expected 3"
        )
    tree, flat_nodes, chunk_nodes = result
    if tree is None:
        raise SegmentBuildError(f"document has no indexable headings: {source.doc_key}")
    return tree, list(flat_nodes), list(chunk_nodes)


def _walk_tree(
    nodes: Iterable[Mapping[str, Any]],
    breadcrumb: list[str],
) -> Iterable[tuple[Mapping[str, Any], list[str]]]:
    for node in nodes:
        crumb = breadcrumb + [str(node.get("title") or "")]
        yield node, crumb
        children = node.get("nodes") or []
        yield from _walk_tree(children, crumb)


def _node_records(
    source: DocumentSource,
    document_tree: Mapping[str, Any],
    flat_nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    title = str(document_tree.get("title") or source.slug)
    tree_by_legacy_id: dict[str, tuple[Mapping[str, Any], list[str]]] = {}
    for tree_node, breadcrumb in _walk_tree(document_tree.get("structure") or [], [title]):
        legacy_id = str(tree_node.get("node_id") or "")
        if legacy_id:
            tree_by_legacy_id[legacy_id] = (tree_node, breadcrumb)

    duplicate_counts: Counter[tuple[str, tuple[str, ...]]] = Counter()
    records: list[dict[str, Any]] = []
    by_legacy_id: dict[str, dict[str, Any]] = {}
    for flat_node in flat_nodes:
        legacy_id = str(flat_node.get("node_id") or "")
        tree_info = tree_by_legacy_id.get(legacy_id)
        tree_node = tree_info[0] if tree_info else {}
        breadcrumb = list(
            (tree_info[1] if tree_info else None)
            or flat_node.get("breadcrumb")
            or [title, str(flat_node.get("title") or "")]
        )
        source_md = str(tree_node.get("source_md") or "")
        if not source_md:
            source_md = _infer_source_path(source, str(flat_node.get("url") or ""))
        normalized_source = normalize_relative_path(source_md)
        duplicate_key = (normalized_source, tuple(str(part) for part in breadcrumb))
        ordinal = duplicate_counts[duplicate_key]
        duplicate_counts[duplicate_key] += 1
        node_key = make_node_key(
            source.doc_key,
            normalized_source,
            breadcrumb,
            ordinal,
        )
        record = {
            "node_key": node_key,
            "legacy_node_id": legacy_id,
            "title": str(flat_node.get("title") or tree_node.get("title") or ""),
            "breadcrumb": breadcrumb,
            "url": str(flat_node.get("url") or ""),
            "terms": list(flat_node.get("terms") or []),
            "summary": str(
                flat_node.get("summary") or tree_node.get("summary") or ""
            ),
            "source_md": source_md,
            "line_num": int(tree_node.get("line_num") or flat_node.get("line_num") or 0),
            "line_end": int(tree_node.get("line_end") or 0),
        }
        if legacy_id in by_legacy_id:
            raise SegmentBuildError(
                f"duplicate legacy node id {legacy_id!r} in {source.doc_key}"
            )
        records.append(record)
        by_legacy_id[legacy_id] = record

    records.sort(key=lambda item: (item["node_key"], item["legacy_node_id"]))
    return records, by_legacy_id


def _infer_source_path(source: DocumentSource, url: str) -> str:
    clean_url = url.split("#", 1)[0].lstrip("/")
    if source.doc_type == "book" and clean_url.endswith(".html"):
        relative = clean_url[: -len(".html")] + ".md"
        return f"content/{relative}"
    if source.doc_type == "paper":
        return f"content/papers/{source.slug}/_index.md"
    if source.doc_type == "note":
        return f"content/notes/{source.slug}.md"
    return ""


def _tf(tokens: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts


def _chunk_pieces(text: str, target: int, overlap: int) -> list[str]:
    if not text or not text.strip():
        return []
    if len(text) <= target:
        return [text]
    return [piece for piece, _start, _end in legacy.split_into_chunks(text, target, overlap)]


def _chunk_records(
    source: DocumentSource,
    chunk_nodes: list[dict[str, Any]],
    nodes_by_legacy_id: Mapping[str, Mapping[str, Any]],
    recipe: SegmentRecipe,
) -> tuple[list[dict[str, Any]], dict[str, list[list[int]]]]:
    pending: list[dict[str, Any]] = []
    for chunk_node in chunk_nodes:
        legacy_id = str(chunk_node.get("node_id") or "")
        node = nodes_by_legacy_id.get(legacy_id)
        if node is None:
            raise SegmentBuildError(
                f"chunk references unknown node {legacy_id!r} in {source.doc_key}"
            )
        body = str(chunk_node.get("text") or "")
        for node_ordinal, piece in enumerate(
            _chunk_pieces(body, recipe.chunk_target_chars, recipe.chunk_overlap_chars)
        ):
            title = str(chunk_node.get("title") or node.get("title") or "")
            breadcrumb = list(chunk_node.get("breadcrumb") or node.get("breadcrumb") or [])
            title_tf = _tf(tokenize(title))
            breadcrumb_tf = _tf(tokenize(" ".join(str(part) for part in breadcrumb)))
            body_tf = _tf(tokenize(piece))
            pending.append(
                {
                    "node_key": node["node_key"],
                    "legacy_node_id": legacy_id,
                    "node_local_ordinal": node_ordinal,
                    "title": title,
                    "breadcrumb": breadcrumb,
                    "body": piece,
                    "source_md": str(
                        chunk_node.get("source_md") or node.get("source_md") or ""
                    ),
                    "line_num": int(
                        chunk_node.get("line_num") or node.get("line_num") or 0
                    ),
                    "line_end": int(node.get("line_end") or 0),
                    "lengths": {
                        "title": sum(title_tf.values()),
                        "breadcrumb": sum(breadcrumb_tf.values()),
                        "body": sum(body_tf.values()),
                    },
                    "_field_tf": (title_tf, breadcrumb_tf, body_tf),
                }
            )

    pending.sort(
        key=lambda item: (
            str(item["node_key"]),
            int(item["node_local_ordinal"]),
            hashlib.sha256(str(item["body"]).encode("utf-8")).hexdigest(),
        )
    )
    postings: dict[str, list[list[int]]] = {}
    chunks: list[dict[str, Any]] = []
    for local_id, item in enumerate(pending):
        title_tf, breadcrumb_tf, body_tf = item.pop("_field_tf")
        item["local_id"] = local_id
        chunks.append(item)
        for token in sorted(set(title_tf) | set(breadcrumb_tf) | set(body_tf)):
            postings.setdefault(token, []).append(
                [
                    local_id,
                    int(title_tf.get(token, 0)),
                    int(breadcrumb_tf.get(token, 0)),
                    int(body_tf.get(token, 0)),
                ]
            )
    return chunks, {token: postings[token] for token in sorted(postings)}


def _document_metadata(
    source: DocumentSource,
    document_tree: Mapping[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "doc_key": source.doc_key,
        "id": source.slug,
        "type": source.doc_type,
        "title": str(document_tree.get("title") or source.slug),
        "author": str(document_tree.get("author") or ""),
        "description": str(document_tree.get("description") or ""),
        "tags": list(document_tree.get("tags") or []),
    }
    if source.doc_type == "book":
        result.update(
            {
                "path": f"/books/{source.slug}/",
                "url": f"/books/{source.slug}.html",
            }
        )
    elif source.doc_type == "paper":
        result.update(
            {
                "path": f"/papers/{source.slug}/",
                "url": f"/papers/{source.slug}.html",
                "year": document_tree.get("year", ""),
            }
        )
    else:
        result.update(
            {
                "path": "/notes/",
                "url": f"/notes/{source.slug}.html",
                "date": str(document_tree.get("date") or ""),
                "source_type": str(document_tree.get("source_type") or ""),
                "source_title": str(document_tree.get("source_title") or ""),
            }
        )
    return result


def build_segment(
    source: DocumentSource,
    recipe: SegmentRecipe | None = None,
) -> dict[str, Any]:
    """Build one deterministic, unfiltered v2 Segment."""

    actual_recipe = recipe or SegmentRecipe()
    document_tree, flat_nodes, chunk_nodes = _process_legacy_document(source)
    nodes, nodes_by_legacy_id = _node_records(source, document_tree, flat_nodes)
    chunks, postings = _chunk_records(
        source,
        chunk_nodes,
        nodes_by_legacy_id,
        actual_recipe,
    )
    recipe_dict = actual_recipe.as_dict()
    source_records = [dict(record) for record in source_file_records(source)]
    return {
        "schema_version": actual_recipe.schema_version,
        "segment_recipe": recipe_dict,
        "document": _document_metadata(source, document_tree),
        "fingerprint": {
            "content_hash": canonical_hash(source_records),
            "recipe_hash": canonical_hash(recipe_dict),
            "source_files": source_records,
        },
        "nodes": nodes,
        "chunks": chunks,
        "postings": postings,
        "document_tree": document_tree,
    }
