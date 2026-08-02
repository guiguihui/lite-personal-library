"""Deterministic link target resolution."""

from __future__ import annotations

from pathlib import PurePosixPath

from .models import DocumentRef, ParsedLink, ResolvedEdge
from .wikilinks import normalize_lookup_key


def _legacy_target(target: str) -> str:
    value = target.replace("\\", "/").split("?", 1)[0].strip()
    value = value.removeprefix("content/").lstrip("/")
    if value.endswith(".md"):
        value = value[:-3]
    path = PurePosixPath(value)
    parts = [part for part in path.parts if part not in (".", "..")]
    if len(parts) >= 2 and parts[0] in ("books", "papers", "notes"):
        kind = parts[0][:-1]
        slug = parts[1] if parts[1] != "_index" else ""
        if slug:
            return f"{kind}:{slug}"
    return value


def _matches(ref: DocumentRef, key: str) -> bool:
    values = (ref.title, *ref.aliases)
    return any(normalize_lookup_key(value) == key for value in values)


def resolve_link(source: DocumentRef, link: ParsedLink, catalog: dict[str, DocumentRef]) -> ResolvedEdge:
    target = _legacy_target(link.target)
    candidates: list[DocumentRef]
    if target in catalog:
        candidates = [catalog[target]]
    else:
        key = normalize_lookup_key(target)
        same_type = [ref for ref in catalog.values() if ref.doc_type == source.doc_type and _matches(ref, key)]
        candidates = same_type if len(same_type) == 1 else [ref for ref in catalog.values() if _matches(ref, key)]
    unique = sorted({ref.doc_id: ref for ref in candidates}.values(), key=lambda ref: ref.doc_id)
    if len(unique) != 1:
        return ResolvedEdge(
            source_id=source.doc_id, target_id=None, relation_type="explicit", syntax=link.syntax,
            status="ambiguous" if unique else "broken", candidates=tuple(ref.doc_id for ref in unique),
            target_anchor=link.anchor, alias=link.alias, raw=link.raw,
        )
    target_ref = unique[0]
    node_id = None
    if link.anchor:
        node_id = target_ref.heading_map().get(normalize_lookup_key(link.anchor))
    return ResolvedEdge(
        source_id=source.doc_id, target_id=target_ref.doc_id, relation_type="explicit", syntax=link.syntax,
        status="resolved", target_anchor=link.anchor, target_node_id=node_id,
        alias=link.alias, raw=link.raw,
    )
