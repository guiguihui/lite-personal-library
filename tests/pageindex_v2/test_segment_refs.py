from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import app.index.v2.object_store as object_store_module
from app.index.v2.canonical import canonical_bytes
from app.index.v2.object_store import (
    SegmentStoreError,
    StoredSegmentRef,
    load_segment,
    put_segment,
    segment_ref_from_attestation,
)


_CONTENT_HASH = "1" * 64
_SEGMENT_RECIPE_HASH = "2" * 64


def _segment() -> dict[str, object]:
    return {
        "schema_version": 2,
        "document": {
            "doc_key": "book:alpha",
            "id": "alpha",
            "type": "book",
            "title": "Alpha",
        },
        "fingerprint": {
            "content_hash": _CONTENT_HASH,
            "recipe_hash": _SEGMENT_RECIPE_HASH,
            "source_files": [],
        },
        "nodes": [],
        "chunks": [],
        "postings": {},
        "document_tree": {},
    }


def test_put_segment_returns_complete_ref_without_changing_object_bytes(
    tmp_path: Path,
) -> None:
    segment = _segment()
    expected = canonical_bytes(segment)

    stored = put_segment(tmp_path, segment)

    assert isinstance(stored, StoredSegmentRef)
    assert stored.path.read_bytes() == expected
    assert stored.byte_size == len(expected)
    assert stored.doc_key == "book:alpha"
    assert stored.doc_type == "book"
    assert stored.slug == "alpha"
    assert stored.content_hash == _CONTENT_HASH
    assert stored.segment_recipe_hash == _SEGMENT_RECIPE_HASH
    assert stored.hash == stored.segment_hash
    assert stored.sha256 == stored.segment_hash
    assert load_segment(tmp_path, stored) == segment
    assert load_segment(tmp_path, stored.segment_hash) == segment


def test_ref_from_attestation_does_not_decode_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = put_segment(tmp_path, _segment())

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("attestation construction decoded the Segment")

    monkeypatch.setattr(object_store_module.json, "loads", forbidden)

    attested = segment_ref_from_attestation(
        tmp_path,
        stored.doc_key,
        stored.segment_hash,
        stored.content_hash,
        stored.segment_recipe_hash,
    )

    assert attested == stored


@pytest.mark.parametrize(
    "doc_key",
    [
        "alpha",
        "unknown:alpha",
        "book:",
        "book:../alpha",
        "book:alpha:beta",
    ],
)
def test_ref_from_attestation_rejects_malformed_doc_key(
    tmp_path: Path,
    doc_key: str,
) -> None:
    stored = put_segment(tmp_path, _segment())

    with pytest.raises(ValueError, match="doc_key"):
        segment_ref_from_attestation(
            tmp_path,
            doc_key,
            stored.segment_hash,
            stored.content_hash,
            stored.segment_recipe_hash,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("segment_hash", "../escape"),
        ("content_hash", "not-a-digest"),
        ("segment_recipe_hash", "A" * 64),
    ],
)
def test_ref_from_attestation_rejects_malformed_hashes(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    stored = put_segment(tmp_path, _segment())
    values = {
        "segment_hash": stored.segment_hash,
        "content_hash": stored.content_hash,
        "segment_recipe_hash": stored.segment_recipe_hash,
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        segment_ref_from_attestation(
            tmp_path,
            stored.doc_key,
            values["segment_hash"],
            values["content_hash"],
            values["segment_recipe_hash"],
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("doc_key", "note:alpha"),
        ("content_hash", "f" * 64),
        ("segment_recipe_hash", "e" * 64),
    ],
)
def test_load_segment_rejects_ref_attestation_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    stored = put_segment(tmp_path, _segment())
    attested = segment_ref_from_attestation(
        tmp_path,
        value if field == "doc_key" else stored.doc_key,
        stored.segment_hash,
        value if field == "content_hash" else stored.content_hash,
        value if field == "segment_recipe_hash" else stored.segment_recipe_hash,
    )

    with pytest.raises(SegmentStoreError, match="attestation mismatch"):
        load_segment(tmp_path, attested)


def test_load_segment_rejects_ref_path_or_size_mismatch(tmp_path: Path) -> None:
    stored = put_segment(tmp_path, _segment())

    escaped = replace(stored, path=tmp_path / "outside.json")
    with pytest.raises(SegmentStoreError, match="path mismatch"):
        load_segment(tmp_path, escaped)

    wrong_size = replace(stored, byte_size=stored.byte_size + 1)
    with pytest.raises(SegmentStoreError, match="byte size mismatch"):
        load_segment(tmp_path, wrong_size)


def test_ref_from_attestation_requires_existing_object(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="segment object not found"):
        segment_ref_from_attestation(
            tmp_path,
            "book:alpha",
            "a" * 64,
            _CONTENT_HASH,
            _SEGMENT_RECIPE_HASH,
        )
