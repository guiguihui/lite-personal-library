from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.index.v2.canonical import canonical_bytes
from app.index.v2.object_store import (
    SegmentStoreError,
    find_reusable_segments,
    load_segment,
    put_segment,
)


def _segment(doc_key: str = "book:alpha") -> dict[str, object]:
    return {
        "schema_version": 2,
        "document": {
            "doc_key": doc_key,
            "id": doc_key.split(":", 1)[1],
            "type": doc_key.split(":", 1)[0],
            "title": "Alpha",
        },
        "fingerprint": {
            "content_hash": "content-alpha",
            "recipe_hash": "recipe-v1",
            "source_files": [],
        },
        "nodes": [],
        "chunks": [],
        "postings": {},
        "document_tree": {},
    }


def test_put_segment_is_content_addressed_and_immutable(tmp_path: Path) -> None:
    segment = _segment()

    first = put_segment(tmp_path, segment)
    before_mtime = first.path.stat().st_mtime_ns
    second = put_segment(tmp_path, segment)

    assert first == second
    assert first.path == (
        tmp_path
        / "objects"
        / "segments"
        / first.segment_hash[:2]
        / f"{first.segment_hash}.json"
    )
    assert first.path.read_bytes() == canonical_bytes(segment)
    assert second.path.stat().st_mtime_ns == before_mtime
    with pytest.raises(FrozenInstanceError):
        first.segment_hash = "different"  # type: ignore[misc]


def test_load_segment_rejects_malformed_hashes(tmp_path: Path) -> None:
    for bad_hash in ("../escape", "abc", "A" * 64, "g" * 64):
        with pytest.raises(ValueError):
            load_segment(tmp_path, bad_hash)


def test_load_segment_detects_corrupt_object(tmp_path: Path) -> None:
    stored = put_segment(tmp_path, _segment())
    stored.path.write_text('{"corrupt":true}', encoding="utf-8")

    with pytest.raises(SegmentStoreError, match="hash mismatch"):
        load_segment(tmp_path, stored.segment_hash)


def test_put_segment_repairs_an_object_with_the_wrong_digest(tmp_path: Path) -> None:
    segment = _segment()
    stored = put_segment(tmp_path, segment)
    stored.path.write_text('{"corrupt":true}', encoding="utf-8")

    repaired = put_segment(tmp_path, segment)

    assert repaired.segment_hash == stored.segment_hash
    assert repaired.path.read_bytes() == canonical_bytes(segment)
    assert load_segment(tmp_path, stored.segment_hash) == segment


def test_find_reusable_segments_indexes_fingerprints(tmp_path: Path) -> None:
    segment = _segment()
    stored = put_segment(tmp_path, segment)

    reusable = find_reusable_segments(tmp_path)

    assert reusable == {
        ("book:alpha", "content-alpha", "recipe-v1"): stored.segment_hash
    }
