"""Tests for deterministic, field-aware per-document Segments."""

from __future__ import annotations

from pathlib import Path

from app.index.v2.canonical import canonical_bytes
from app.index.v2.catalog import discover_documents
from app.index.v2.models import SegmentRecipe
from app.index.v2.segment_builder import build_segment


def _make_bodies_indexable(content: Path) -> None:
    targets = (
        content / "books" / "alpha" / "ch01.md",
        content / "books" / "alpha" / "ch02.md",
        content / "papers" / "beta" / "_index.md",
        content / "notes" / "welcome.md",
    )
    addition = "\n" + ("common searchable body text " * 8) + "\n"
    for path in targets:
        path.write_text(path.read_text(encoding="utf-8") + addition, encoding="utf-8")


def test_segment_builds_all_supported_document_types(sample_content: Path) -> None:
    _make_bodies_indexable(sample_content)
    segments = [build_segment(source, SegmentRecipe()) for source in discover_documents(sample_content)]

    assert [segment["document"]["doc_key"] for segment in segments] == [
        "book:alpha",
        "paper:beta",
        "note:welcome",
    ]
    assert all(segment["nodes"] for segment in segments)
    assert all(segment["chunks"] for segment in segments)


def test_segment_keeps_stable_and_compatibility_node_ids(sample_content: Path) -> None:
    _make_bodies_indexable(sample_content)
    source = discover_documents(sample_content)[0]
    segment = build_segment(source, SegmentRecipe())

    assert all(node["node_key"].startswith("n_") for node in segment["nodes"])
    assert all(len(node["node_key"]) == 26 for node in segment["nodes"])
    assert all(node["legacy_node_id"] for node in segment["nodes"])
    assert {chunk["node_key"] for chunk in segment["chunks"]} <= {
        node["node_key"] for node in segment["nodes"]
    }


def test_segment_postings_store_unfiltered_field_tf(sample_content: Path) -> None:
    _make_bodies_indexable(sample_content)
    source = discover_documents(sample_content)[0]
    segment = build_segment(source, SegmentRecipe())

    assert segment["postings"]["common"]
    assert all(len(posting) == 4 for posting in segment["postings"]["common"])
    assert any(posting[3] > 0 for posting in segment["postings"]["common"])
    assert any(posting[1] > 0 for posting in segment["postings"]["first"])
    assert all(posting[0] >= 0 for posting in segment["postings"]["common"])


def test_segment_rebuild_is_byte_deterministic(sample_content: Path) -> None:
    _make_bodies_indexable(sample_content)
    source = discover_documents(sample_content)[0]
    recipe = SegmentRecipe()

    first = build_segment(source, recipe)
    second = build_segment(source, recipe)

    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["fingerprint"]["source_files"] == [
        {
            "path": path.as_posix(),
            "sha256": record["sha256"],
        }
        for path, record in zip(source.files, first["fingerprint"]["source_files"])
    ]


def test_chunk_lengths_match_field_token_counts(sample_content: Path) -> None:
    _make_bodies_indexable(sample_content)
    segment = build_segment(discover_documents(sample_content)[1], SegmentRecipe())

    for chunk in segment["chunks"]:
        assert set(chunk["lengths"]) == {"title", "breadcrumb", "body"}
        assert all(value >= 0 for value in chunk["lengths"].values())
        assert chunk["source_md"].startswith("content/")
        assert chunk["line_end"] >= chunk["line_num"]
