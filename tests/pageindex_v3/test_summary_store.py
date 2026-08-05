from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import app.index.v3.summary_store as summary_store
from app.index.v2.canonical import canonical_bytes
from app.index.v2.object_store import StoredSegmentRef
from app.index.v3.models import SegmentSummary, TokenSummary, make_doc_uid
from app.index.v3.summary_store import (
    StoredSummaryRef,
    SummaryStoreError,
    load_summary,
    put_summary,
)


def _ref(tmp_path: Path, *, doc_key: str = "note:alpha") -> StoredSegmentRef:
    doc_type, slug = doc_key.split(":", 1)
    return StoredSegmentRef(
        segment_hash="a" * 64,
        path=tmp_path / "unused-segment.json",
        byte_size=1,
        doc_key=doc_key,
        doc_type=doc_type,
        slug=slug,
        content_hash="b" * 64,
        segment_recipe_hash="c" * 64,
    )


def _summary(ref: StoredSegmentRef) -> SegmentSummary:
    return SegmentSummary(
        segment_hash=ref.segment_hash,
        doc_key=ref.doc_key,
        doc_uid=make_doc_uid(ref.doc_key),
        content_hash=ref.content_hash,
        segment_recipe_hash=ref.segment_recipe_hash,
        chunk_count=1,
        title_length_sum=1,
        breadcrumb_length_sum=0,
        body_length_sum=0,
        posting_count=1,
        tokens=(TokenSummary("alpha", 1, 1, 0),),
    )


def _path(pageindex: Path, ref: StoredSegmentRef) -> Path:
    return (
        pageindex
        / "objects"
        / "search"
        / "summaries"
        / ref.segment_hash[:2]
        / f"{ref.segment_hash}.json"
    )


def _receipt_for_payload(
    ref: StoredSegmentRef,
    payload: bytes,
) -> StoredSummaryRef:
    return StoredSummaryRef(
        segment_hash=ref.segment_hash,
        summary_sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        doc_key=ref.doc_key,
        doc_uid=make_doc_uid(ref.doc_key),
        content_hash=ref.content_hash,
        segment_recipe_hash=ref.segment_recipe_hash,
    )


def _load_with_current_receipt(
    pageindex: Path,
    ref: StoredSegmentRef,
) -> SegmentSummary:
    payload = _path(pageindex, ref).read_bytes()
    return load_summary(
        pageindex,
        ref,
        _receipt_for_payload(ref, payload),
    )


def test_summary_round_trip_uses_content_addressed_canonical_path(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)

    receipt = put_summary(pageindex, summary)
    path = receipt.path_for(pageindex)

    assert path == _path(pageindex, ref)
    assert path.read_bytes() == canonical_bytes(summary.as_dict())
    assert receipt.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert receipt.byte_size == path.stat().st_size
    assert load_summary(pageindex, ref, receipt) == summary


def test_put_summary_is_idempotent_without_replacing_existing_file(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)
    receipt = put_summary(pageindex, summary)
    path = receipt.path_for(pageindex)
    before = path.stat()

    assert put_summary(pageindex, summary) == receipt

    after = path.stat()
    assert path.read_bytes() == canonical_bytes(summary.as_dict())
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_put_summary_streams_without_materializing_canonical_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)

    def forbidden(_value: object) -> bytes:
        raise AssertionError("summary serialization must be streamed")

    def forbidden_as_dict(_value: SegmentSummary) -> dict[str, object]:
        raise AssertionError("put_summary must not copy the complete token table")

    monkeypatch.setattr(summary_store, "canonical_bytes", forbidden, raising=False)
    monkeypatch.setattr(SegmentSummary, "as_dict", forbidden_as_dict)

    receipt = put_summary(pageindex, summary)
    assert receipt.path_for(pageindex).is_file()
    assert load_summary(pageindex, ref, receipt) == summary


@pytest.mark.parametrize("corrupt", [b"{}", b"not-json", b"\xff"])
def test_put_refuses_existing_corruption_without_repair(
    tmp_path: Path,
    corrupt: bytes,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)
    path = _path(pageindex, ref)
    path.parent.mkdir(parents=True)
    path.write_bytes(corrupt)

    with pytest.raises(SummaryStoreError, match="differs|corrupt"):
        put_summary(pageindex, summary)

    assert path.read_bytes() == corrupt


def test_put_rejects_directory_destination(tmp_path: Path) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    path = _path(pageindex, ref)
    path.mkdir(parents=True)

    with pytest.raises(SummaryStoreError, match="not a regular file"):
        put_summary(pageindex, _summary(ref))


def test_concurrent_identical_publish_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)
    destination = _path(pageindex, ref)

    def concurrent_winner(_source: Path, target: Path) -> None:
        target.write_bytes(canonical_bytes(summary.as_dict()))
        raise FileExistsError("winner")

    monkeypatch.setattr(summary_store.os, "link", concurrent_winner)

    receipt = put_summary(pageindex, summary)
    assert receipt.path_for(pageindex) == destination
    assert load_summary(pageindex, ref, receipt) == summary
    assert not list(destination.parent.glob(".*.tmp"))


def test_windows_publish_falls_back_when_hard_links_are_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows no-clobber rename fallback")

    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)

    def unsupported_link(_source: Path, _target: Path) -> None:
        raise OSError("hard links unsupported")

    monkeypatch.setattr(summary_store.os, "link", unsupported_link)

    receipt = put_summary(pageindex, summary)
    assert load_summary(pageindex, ref, receipt) == summary
    assert not list(receipt.path_for(pageindex).parent.glob(".*.tmp"))


def test_concurrent_conflicting_publish_never_overwrites_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)
    destination = _path(pageindex, ref)
    winner = b"{}"

    def concurrent_winner(_source: Path, target: Path) -> None:
        target.write_bytes(winner)
        raise FileExistsError("winner")

    monkeypatch.setattr(summary_store.os, "link", concurrent_winner)

    with pytest.raises(SummaryStoreError, match="differs|corrupt"):
        put_summary(pageindex, summary)

    assert destination.read_bytes() == winner
    assert not list(destination.parent.glob(".*.tmp"))


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"not-json",
        b"[]",
        json.dumps({"schema_version": 1}, indent=2).encode("utf-8"),
    ],
)
def test_load_rejects_invalid_or_incomplete_payloads(
    tmp_path: Path,
    payload: bytes,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    path = _path(pageindex, ref)
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    with pytest.raises(SummaryStoreError):
        _load_with_current_receipt(pageindex, ref)


def test_load_rejects_canonical_but_wrong_facts_under_trusted_receipt(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)
    receipt = put_summary(pageindex, summary)
    path = receipt.path_for(pageindex)
    payload = summary.as_dict()
    payload["field_length_sums"]["title"] = 999
    path.write_bytes(canonical_bytes(payload))

    with pytest.raises(SummaryStoreError, match="receipt hash/size mismatch"):
        load_summary(pageindex, ref, receipt)


def test_load_binds_parsed_semantics_across_hash_parse_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)
    receipt = put_summary(pageindex, summary)
    payload = summary.as_dict()
    payload["field_length_sums"]["title"] = 999
    replacement = canonical_bytes(payload)
    real_hash_file = summary_store._hash_file

    def swap_after_hash(path: Path) -> tuple[str, int]:
        result = real_hash_file(path)
        path.write_bytes(replacement)
        return result

    monkeypatch.setattr(summary_store, "_hash_file", swap_after_hash)

    with pytest.raises(SummaryStoreError, match="semantic receipt mismatch"):
        load_summary(pageindex, ref, receipt)

def test_load_rejects_noncanonical_semantically_valid_json(tmp_path: Path) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    summary = _summary(ref)
    path = _path(pageindex, ref)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(summary.as_dict(), indent=2), encoding="utf-8")

    with pytest.raises(SummaryStoreError, match="canonical"):
        _load_with_current_receipt(pageindex, ref)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"extra": True}),
        lambda payload: payload["field_length_sums"].update({"extra": 0}),
        lambda payload: payload["tokens"][0].update({"extra": 0}),
        lambda payload: payload.pop("posting_count"),
    ],
)
def test_load_rejects_unknown_or_missing_fields(
    tmp_path: Path,
    mutate: object,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    payload = _summary(ref).as_dict()
    mutate(payload)
    path = _path(pageindex, ref)
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_bytes(payload))

    with pytest.raises(SummaryStoreError, match="field|keys|payload"):
        _load_with_current_receipt(pageindex, ref)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("segment_hash", "d" * 64),
        ("doc_key", "note:other"),
        ("doc_uid", make_doc_uid("note:other")),
        ("content_hash", "d" * 64),
        ("segment_recipe_hash", "d" * 64),
    ],
)
def test_load_rejects_summary_attestation_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    payload = _summary(ref).as_dict()
    payload[field] = value
    if field == "doc_key":
        payload["doc_uid"] = make_doc_uid(value)
    path = _path(pageindex, ref)
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_bytes(payload))

    with pytest.raises(SummaryStoreError, match=field):
        _load_with_current_receipt(pageindex, ref)


def test_load_missing_summary_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="summary object not found"):
        ref = _ref(tmp_path)
        load_summary(
            tmp_path / "pageindex",
            ref,
            _receipt_for_payload(ref, b""),
        )


def test_put_rejects_symlink_destination_when_supported(tmp_path: Path) -> None:
    pageindex = tmp_path / "pageindex"
    ref = _ref(tmp_path)
    destination = _path(pageindex, ref)
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    try:
        os.symlink(outside, destination)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(SummaryStoreError, match="symlink"):
        put_summary(pageindex, _summary(ref))
    assert outside.read_bytes() == b"outside"

