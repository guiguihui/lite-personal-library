"""Build a catalog of books, papers, and notes from Markdown content."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .frontmatter import canonical_id, parse_aliases, parse_governance, split_frontmatter
from .models import DocumentRef
from .wikilinks import normalize_lookup_key


def _preview(body: str, limit: int = 320) -> str:
    paragraphs = re.split(r"\n\s*\n", body)
    for paragraph in paragraphs:
        text = " ".join(line.strip(" #>\t") for line in paragraph.splitlines()).strip()
        if text and not text.startswith(("{{<", "<section", "</")):
            return text[:limit]
    return ""


def _pageindex_headings(pageindex_dir: Path, doc_type: str, slug: str) -> tuple[tuple[str, str], ...]:
    path = pageindex_dir / f"{doc_type}s" / f"{slug}.json"
    if not path.is_file():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    found: dict[str, str] = {}

    def walk(nodes: object) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title, node_id = str(node.get("title", "")), str(node.get("node_id", ""))
            if title and node_id:
                found.setdefault(normalize_lookup_key(title), node_id)
            walk(node.get("nodes"))

    walk(data.get("structure", []))
    return tuple(sorted(found.items()))


def _make_ref(doc_type: str, slug: str, files: list[Path], pageindex_dir: Path) -> DocumentRef:
    metadata_file = next((path for path in files if path.name == "_index.md"), files[0])
    data, body, _ = split_frontmatter(metadata_file.read_text(encoding="utf-8"))
    return DocumentRef(
        doc_id=canonical_id(doc_type, slug, data.get("id")),
        doc_type=doc_type,  # type: ignore[arg-type]
        slug=slug,
        title=str(data.get("title") or slug).strip(),
        aliases=parse_aliases(data.get("aliases")),
        governance=parse_governance(data),
        source_files=tuple(path.as_posix() for path in sorted(files)),
        headings=_pageindex_headings(pageindex_dir, doc_type, slug),
        preview=_preview(body),
    )


def build_catalog(content_dir: Path, pageindex_dir: Path) -> dict[str, DocumentRef]:
    refs: dict[str, DocumentRef] = {}
    for doc_type in ("book", "paper"):
        root = content_dir / f"{doc_type}s"
        if not root.is_dir():
            continue
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            files = sorted(directory.glob("*.md"))
            if files:
                ref = _make_ref(doc_type, directory.name, files, pageindex_dir)
                if ref.doc_id in refs:
                    raise ValueError(f"duplicate document id: {ref.doc_id}")
                refs[ref.doc_id] = ref
        if doc_type == "paper":
            for path in sorted(root.glob("*.md")):
                if path.name != "_index.md":
                    ref = _make_ref(doc_type, path.stem, [path], pageindex_dir)
                    if ref.doc_id in refs:
                        raise ValueError(f"duplicate document id: {ref.doc_id}")
                    refs[ref.doc_id] = ref
    notes = content_dir / "notes"
    if notes.is_dir():
        for path in sorted(notes.glob("*.md")):
            if path.name == "_index.md":
                continue
            ref = _make_ref("note", path.stem, [path], pageindex_dir)
            if ref.doc_id in refs:
                raise ValueError(f"duplicate document id: {ref.doc_id}")
            refs[ref.doc_id] = ref
    return refs
