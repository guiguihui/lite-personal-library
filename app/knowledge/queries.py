"""Pure queries over a validated link index."""

from __future__ import annotations

from typing import Any


def _edge(index: dict[str, Any], reference: int | dict[str, Any]) -> dict[str, Any]:
    return index["edges"][reference] if isinstance(reference, int) else reference


def get_backlinks(index: dict[str, Any], doc_id: str) -> dict[str, Any]:
    documents = index["documents"]
    if doc_id not in documents:
        raise KeyError(doc_id)
    grouped: dict[str, dict[str, Any]] = {}
    archived: list[dict[str, Any]] = []
    for reference in index["incoming"].get(doc_id, []):
        edge = _edge(index, reference)
        if edge.get("relation_type") != "explicit":
            continue
        source_id = edge["source_id"]
        item = grouped.setdefault(source_id, {"source": documents[source_id], "occurrences": []})
        item["occurrences"].append(edge)
    active: list[dict[str, Any]] = []
    for source_id in sorted(grouped):
        item = grouped[source_id]
        item["count"] = len(item["occurrences"])
        if item["source"].get("governance", {}).get("status") == "archived":
            archived.append(item)
        else:
            active.append(item)
    return {"doc": documents[doc_id], "backlinks": active, "archived_backlinks": archived, "total": len(grouped)}


def get_neighborhood(index: dict[str, Any], doc_id: str, limit: int = 40, include_provenance: bool = False) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    documents = index["documents"]
    if doc_id not in documents:
        raise KeyError(doc_id)
    outgoing = [_edge(index, reference) for reference in index["outgoing"].get(doc_id, [])]
    incoming = [_edge(index, reference) for reference in index["incoming"].get(doc_id, [])]
    outgoing = [edge for edge in outgoing if edge["relation_type"] == "explicit" or include_provenance]
    incoming = [edge for edge in incoming if edge["relation_type"] == "explicit" or include_provenance]
    outgoing_ids = {edge["target_id"] for edge in outgoing}
    incoming_ids = {edge["source_id"] for edge in incoming}
    neighbors = outgoing_ids | incoming_ids

    def priority(neighbor: str) -> tuple[int, str]:
        if neighbor in outgoing_ids and neighbor in incoming_ids:
            return (0, neighbor)
        return (1 if neighbor in outgoing_ids else 2, neighbor)

    ordered = sorted(neighbors, key=priority)
    visible = [item for item in ordered if documents[item].get("governance", {}).get("status") != "archived"]
    hidden = len(ordered) - len(visible)
    chosen = set(visible[:limit])
    edges = [edge for edge in outgoing + incoming if (edge.get("target_id") in chosen or edge.get("source_id") in chosen)]
    deduped = {(edge["source_id"], edge["target_id"], edge["relation_type"]): edge for edge in edges}
    return {
        "center": documents[doc_id],
        "nodes": [documents[item] for item in [doc_id, *visible[:limit]]],
        "edges": [deduped[key] for key in sorted(deduped)],
        "total_neighbors": len(visible), "truncated": len(visible) > limit,
        "hidden_by_status": hidden,
    }


def get_preview(index: dict[str, Any], doc_id: str, node_id: str | None = None) -> dict[str, Any]:
    try:
        doc = index["documents"][doc_id]
    except KeyError as exc:
        raise KeyError(doc_id) from exc
    return {
        "id": doc_id, "title": doc["title"], "type": doc["type"],
        "governance": doc.get("governance", {}), "excerpt_markdown": doc.get("preview", ""),
        "node_id": node_id,
    }
