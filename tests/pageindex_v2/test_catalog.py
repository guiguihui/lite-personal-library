"""Tests for deterministic document discovery and content fingerprints."""

from __future__ import annotations

from pathlib import Path

from app.index.v2.catalog import discover_documents, fingerprint_document


def test_discover_documents_ignores_section_indexes(sample_content: Path) -> None:
    docs = discover_documents(sample_content)
    assert [d.doc_key for d in docs] == [
        "book:alpha",
        "paper:beta",
        "note:welcome",
    ]


def test_discovered_files_are_relative_and_deterministically_ordered(
    sample_content: Path,
) -> None:
    book = discover_documents(sample_content)[0]
    assert book.root == sample_content.resolve()
    assert [path.as_posix() for path in book.files] == [
        "books/alpha/_index.md",
        "books/alpha/ch01.md",
        "books/alpha/ch02.md",
    ]


def test_fingerprint_changes_when_a_chapter_is_deleted(sample_content: Path) -> None:
    source = discover_documents(sample_content)[0]
    before = fingerprint_document(source)
    (sample_content / "books" / "alpha" / "ch02.md").unlink()
    source_after = discover_documents(sample_content)[0]
    assert fingerprint_document(source_after) != before


def test_fingerprint_changes_for_content_addition_edit_and_rename(
    sample_content: Path,
) -> None:
    source = discover_documents(sample_content)[0]
    initial = fingerprint_document(source)

    ch01 = sample_content / "books" / "alpha" / "ch01.md"
    ch01.write_text(ch01.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
    edited = fingerprint_document(discover_documents(sample_content)[0])
    assert edited != initial

    extra = sample_content / "books" / "alpha" / "appendix.md"
    extra.write_text("# Appendix\n", encoding="utf-8")
    added = fingerprint_document(discover_documents(sample_content)[0])
    assert added != edited

    extra.rename(extra.with_name("afterword.md"))
    renamed = fingerprint_document(discover_documents(sample_content)[0])
    assert renamed != added


def test_fingerprint_is_independent_of_absolute_content_root(
    sample_content: Path,
    tmp_path: Path,
) -> None:
    first = discover_documents(sample_content)[0]
    second_root = tmp_path / "relocated"
    for relative in first.files:
        target = second_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((first.root / relative).read_bytes())
    second = discover_documents(second_root)[0]
    assert fingerprint_document(first) == fingerprint_document(second)
