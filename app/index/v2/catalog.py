"""Deterministic PageIndex v2 source discovery and content fingerprints."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .canonical import canonical_hash
from .ids import make_doc_key, normalize_relative_path
from .models import DocumentSource

__all__ = [
    "CatalogError",
    "DocumentSource",
    "discover_documents",
    "fingerprint_document",
    "source_file_records",
]


class CatalogError(ValueError):
    """The content tree cannot be represented deterministically."""


_TYPE_ORDER = {"book": 0, "paper": 1, "note": 2}


def _normalized_name(path: Path) -> str:
    return normalize_relative_path(path.name)


def _is_markdown_file(path: Path) -> bool:
    return path.is_file() and path.name.endswith(".md")


def _assert_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CatalogError(f"source file escapes content directory: {path}") from exc
    return resolved


def _book_sources(content_root: Path) -> list[DocumentSource]:
    books_root = content_root / "books"
    if not books_root.is_dir():
        return []

    result: list[DocumentSource] = []
    directories = sorted(
        (path for path in books_root.iterdir() if path.is_dir()),
        key=_normalized_name,
    )
    for directory in directories:
        if directory.name.startswith("_"):
            continue
        index = directory / "_index.md"
        if not index.is_file():
            continue
        chapters = sorted(
            (
                path
                for path in directory.iterdir()
                if _is_markdown_file(path) and path.name != "_index.md"
            ),
            key=_normalized_name,
        )
        files = (
            Path("books") / directory.name / "_index.md",
            *(
                Path("books") / directory.name / chapter.name
                for chapter in chapters
            ),
        )
        result.append(
            DocumentSource(
                doc_type="book",
                slug=directory.name,
                doc_key=make_doc_key("book", directory.name),
                root=content_root,
                files=files,
            )
        )
    return result


def _paper_sources(content_root: Path) -> list[DocumentSource]:
    papers_root = content_root / "papers"
    if not papers_root.is_dir():
        return []

    result: list[DocumentSource] = []
    directories = sorted(
        (path for path in papers_root.iterdir() if path.is_dir()),
        key=_normalized_name,
    )
    for directory in directories:
        if directory.name.startswith("_"):
            continue
        index = directory / "_index.md"
        if not index.is_file():
            continue
        result.append(
            DocumentSource(
                doc_type="paper",
                slug=directory.name,
                doc_key=make_doc_key("paper", directory.name),
                root=content_root,
                files=(Path("papers") / directory.name / "_index.md",),
            )
        )
    return result


def _note_sources(content_root: Path) -> list[DocumentSource]:
    notes_root = content_root / "notes"
    if not notes_root.is_dir():
        return []

    result: list[DocumentSource] = []
    files = sorted(
        (
            path
            for path in notes_root.iterdir()
            if _is_markdown_file(path) and path.name != "_index.md"
        ),
        key=_normalized_name,
    )
    for path in files:
        slug = path.stem
        result.append(
            DocumentSource(
                doc_type="note",
                slug=slug,
                doc_key=make_doc_key("note", slug),
                root=content_root,
                files=(Path("notes") / path.name,),
            )
        )
    return result


def discover_documents(content_dir: Path) -> tuple[DocumentSource, ...]:
    """Discover every supported logical document under *content_dir*."""
    content_root = Path(content_dir).resolve()
    if not content_root.exists():
        return ()
    if not content_root.is_dir():
        raise NotADirectoryError(content_root)

    documents = [
        *_book_sources(content_root),
        *_paper_sources(content_root),
        *_note_sources(content_root),
    ]
    documents.sort(key=lambda source: (_TYPE_ORDER[source.doc_type], source.slug))

    seen: set[str] = set()
    for source in documents:
        if source.doc_key in seen:
            raise CatalogError(f"duplicate normalized document key: {source.doc_key}")
        seen.add(source.doc_key)
    return tuple(documents)


def source_file_records(source: DocumentSource) -> tuple[dict[str, str], ...]:
    """Return portable path/content records for all ordered Markdown inputs."""
    content_root = source.root.resolve()
    records: list[dict[str, str]] = []
    for raw_relative in source.files:
        relative = normalize_relative_path(Path(raw_relative).as_posix())
        path = _assert_within(content_root / Path(relative), content_root)
        if not path.is_file():
            raise FileNotFoundError(path)
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return tuple(records)


def fingerprint_document(source: DocumentSource) -> str:
    """Hash the document's complete ordered Markdown path/content records."""
    return canonical_hash(list(source_file_records(source)))
