"""Build a deterministic, atomically replaced link index."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from .catalog import build_catalog
from .models import ResolvedEdge
from .resolver import resolve_link
from .wikilinks import parse_links


def _fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.as_posix().encode())
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _excerpt(lines: list[str], line: int) -> str:
    start = max(0, line - 2)
    return " ".join(part.strip() for part in lines[start : start + 3] if part.strip())[:240]


def _source_heading(lines: list[str], line: int) -> str | None:
    for item in reversed(lines[:line]):
        if item.lstrip().startswith("#"):
            return item.lstrip("# ").strip() or None
    return None


def _document_dict(ref) -> dict:
    return {
        "id": ref.doc_id, "type": ref.doc_type, "slug": ref.slug, "title": ref.title,
        "aliases": list(ref.aliases), "preview": ref.preview, "governance": ref.governance.to_dict(),
    }


def _edge_dict(edge: ResolvedEdge) -> dict:
    data = edge.to_dict()
    return {
        key: value for key, value in data.items()
        if value not in (None, "", (), []) and not (key == "raw" and edge.status == "resolved")
    }


def build_link_index(content_dir: Path | str, pageindex_dir: Path | str) -> dict:
    content_root, output_root = Path(content_dir), Path(pageindex_dir)
    catalog = build_catalog(content_root, output_root)
    path_to_ref = {Path(path).resolve(): ref for ref in catalog.values() for path in ref.source_files}
    edges: list[ResolvedEdge] = []
    markdown_files = sorted(path_to_ref)
    for path in markdown_files:
        ref = path_to_ref[path]
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for link in parse_links(text):
            edge = resolve_link(ref, link, catalog)
            edges.append(replace(
                edge, source_md=path.relative_to(content_root).as_posix(), source_line=link.line,
                source_heading=_source_heading(lines, link.line), excerpt=_excerpt(lines, link.line),
            ))
        for source in ref.governance.sources:
            if source in catalog:
                edges.append(ResolvedEdge(ref.doc_id, source, "provenance", "frontmatter", "resolved", raw=source))
    edges.sort(key=lambda edge: (edge.source_id, edge.target_id or "", edge.relation_type, edge.source_md, edge.source_line, edge.raw))
    outgoing: dict[str, list[dict]] = {doc_id: [] for doc_id in sorted(catalog)}
    incoming: dict[str, list[dict]] = {doc_id: [] for doc_id in sorted(catalog)}
    diagnostics = {"broken": [], "ambiguous": [], "invalid_frontmatter": []}
    serialized = []
    for edge in edges:
        item = _edge_dict(edge)
        serialized.append(item)
        if edge.status == "resolved" and edge.target_id:
            edge_index = len(serialized) - 1
            outgoing[edge.source_id].append(edge_index)
            incoming[edge.target_id].append(edge_index)
        else:
            diagnostics[edge.status].append(item)
    data = {
        "schema_version": 1,
        "content_fingerprint": _fingerprint(markdown_files),
        "documents": {doc_id: _document_dict(catalog[doc_id]) for doc_id in sorted(catalog)},
        "edges": serialized, "outgoing": outgoing, "incoming": incoming, "diagnostics": diagnostics,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".link-index-", suffix=".tmp", dir=output_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output_root / "link-index.json")
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return data
