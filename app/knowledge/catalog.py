"""Build a catalog of books, papers, and notes from Markdown content."""

from __future__ import annotations

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


def _v3_headings(pageindex_dir: Path) -> dict[str, tuple[tuple[str, str], ...]]:
    """Read heading identities from the published V3 view, never legacy JSON."""

    from app.index.v3.runtime import CURRENT_POINTER, open_current_view

    if not (pageindex_dir / CURRENT_POINTER).is_file():
        return {}

    try:
        with open_current_view(pageindex_dir) as view:
            owners = view.documents()
            refs = view.document_chunk_refs(owners)
            chunks = view.get_chunks(refs)
            found: dict[str, dict[str, str]] = {}
            for ref in refs:
                owner = owners[ref.doc_uid]
                chunk = chunks[ref]
                title = str(chunk.get("title") or "").strip()
                node_id = str(
                    chunk.get("legacy_node_id") or chunk.get("node_id") or ""
                )
                if title and node_id:
                    found.setdefault(owner.doc_key, {}).setdefault(
                        normalize_lookup_key(title), node_id
                    )
    except Exception as exc:
        raise ValueError(f"cannot read published V3 headings: {exc}") from exc
    return {
        doc_key: tuple(sorted(headings.items()))
        for doc_key, headings in found.items()
    }


def _make_ref(
    doc_type: str,
    slug: str,
    files: list[Path],
    headings: dict[str, tuple[tuple[str, str], ...]],
) -> DocumentRef:
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
        headings=headings.get(f"{doc_type}:{slug}", ()),
        preview=_preview(body),
    )


def build_catalog(content_dir: Path, pageindex_dir: Path) -> dict[str, DocumentRef]:
    refs: dict[str, DocumentRef] = {}
    headings = _v3_headings(pageindex_dir)
    for doc_type in ("book", "paper"):
        root = content_dir / f"{doc_type}s"
        if not root.is_dir():
            continue
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            files = sorted(directory.glob("*.md"))
            if files:
                ref = _make_ref(doc_type, directory.name, files, headings)
                if ref.doc_id in refs:
                    raise ValueError(f"duplicate document id: {ref.doc_id}")
                refs[ref.doc_id] = ref
        if doc_type == "paper":
            for path in sorted(root.glob("*.md")):
                if path.name != "_index.md":
                    ref = _make_ref(doc_type, path.stem, [path], headings)
                    if ref.doc_id in refs:
                        raise ValueError(f"duplicate document id: {ref.doc_id}")
                    refs[ref.doc_id] = ref
    notes = content_dir / "notes"
    if notes.is_dir():
        for path in sorted(notes.glob("*.md")):
            if path.name == "_index.md":
                continue
            ref = _make_ref("note", path.stem, [path], headings)
            if ref.doc_id in refs:
                raise ValueError(f"duplicate document id: {ref.doc_id}")
            refs[ref.doc_id] = ref
    return refs
