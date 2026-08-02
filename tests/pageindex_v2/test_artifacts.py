"""One-pass canonical artifact writer and receipt coverage."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path

import pytest

from app.index.v2.artifacts import (
    ArtifactRef,
    AtomicHashingSink,
    CandidateReceipt,
    write_canonical_object,
    write_canonical_object_with_array,
    write_canonical_object_with_mapping,
)
from app.index.v2.canonical import canonical_bytes, iter_canonical_json


def test_receipts_are_frozen_slotted_lightweight_values(tmp_path: Path) -> None:
    artifact = ArtifactRef(
        relative_path="nested/payload.json",
        sha256="a" * 64,
        byte_size=17,
        records=3,
    )
    receipt = CandidateReceipt(
        candidate_dir=tmp_path / "candidate",
        generation_id="b" * 20,
        revision_sha256="c" * 64,
        compiler_recipe_hash="d" * 64,
        input_proof_sha256="e" * 64,
        manifest_sha256="f" * 64,
        artifacts={artifact.relative_path: artifact},
        segment_refs={"note:alpha": object()},
        invariants={"chunks": 3},
    )

    assert artifact.records == 3
    assert receipt.artifacts[artifact.relative_path] is artifact
    assert not hasattr(artifact, "__dict__")
    assert not hasattr(receipt, "__dict__")
    with pytest.raises(FrozenInstanceError):
        artifact.byte_size = 99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        receipt.generation_id = "changed"  # type: ignore[misc]


def test_iterencode_and_object_writer_match_canonical_bytes(tmp_path: Path) -> None:
    payload = {
        "z-empty": [],
        "ascii": "plain",
        "float": 0.9,
        "integer": 42,
        "nested": {"z": None, "a": [True, False, {"quote": '"\\\n'}]},
        "unicode": "中文 café Ω",
        "empty-object": {},
    }
    expected = canonical_bytes(payload)

    assert "".join(iter_canonical_json(payload)).encode("utf-8") == expected

    target = tmp_path / "candidate" / "payload.json"
    reference = write_canonical_object(
        target,
        payload,
        relative_path="payload.json",
        records=7,
    )

    assert target.read_bytes() == expected
    assert reference == ArtifactRef(
        relative_path="payload.json",
        sha256=hashlib.sha256(expected).hexdigest(),
        byte_size=len(expected),
        records=7,
    )


def test_object_with_array_streams_records_in_exact_canonical_shape(
    tmp_path: Path,
) -> None:
    yielded: list[int] = []

    def records():
        for index, value in enumerate(("一", "escaped\nvalue", {"b": 2, "a": 1})):
            yielded.append(index)
            yield value

    target = tmp_path / "chunks.json"
    reference = write_canonical_object_with_array(
        target,
        fields={"z": {}, "a": 2},
        array_key="chunks",
        items=records(),
        relative_path="chunks.json",
    )
    expected = canonical_bytes(
        {
            "a": 2,
            "chunks": ["一", "escaped\nvalue", {"a": 1, "b": 2}],
            "z": {},
        }
    )

    assert yielded == [0, 1, 2]
    assert target.read_bytes() == expected
    assert reference.records == 3
    assert reference.byte_size == len(expected)
    assert reference.sha256 == hashlib.sha256(expected).hexdigest()


def test_object_with_mapping_sorts_mapping_and_validates_stream_order(
    tmp_path: Path,
) -> None:
    target = tmp_path / "inverted-index.json"
    reference = write_canonical_object_with_mapping(
        target,
        fields={"num_chunks": 2},
        mapping_key="postings",
        items={"中": [[2, 1]], "alpha": [[1, 3]], "empty": []},
        relative_path="inverted-index.json",
    )
    expected = canonical_bytes(
        {
            "num_chunks": 2,
            "postings": {
                "alpha": [[1, 3]],
                "empty": [],
                "中": [[2, 1]],
            },
        }
    )

    assert target.read_bytes() == expected
    assert reference.records == 3

    old = b"old-destination"
    target.write_bytes(old)
    with pytest.raises(ValueError, match="strictly increasing"):
        write_canonical_object_with_mapping(
            target,
            fields={},
            mapping_key="postings",
            items=iter((('z', 1), ('a', 2))),
            relative_path="inverted-index.json",
        )
    assert target.read_bytes() == old


def test_atomic_sink_hashes_each_write_and_replaces_only_on_success(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.json"
    target.write_bytes(b"old")

    sink = AtomicHashingSink(target)
    with sink:
        sink.write(b'{"a":')
        sink.write(b"1}")

    assert target.read_bytes() == b'{"a":1}'
    assert sink.byte_size == 7
    assert sink.sha256 == hashlib.sha256(b'{"a":1}').hexdigest()


def test_stream_failure_leaves_old_destination_and_no_temporary_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "payload.json"
    target.write_bytes(b"old")

    def failing_records():
        yield {"first": True}
        raise OSError("injected write failure")

    with pytest.raises(OSError, match="injected write failure"):
        write_canonical_object_with_array(
            target,
            fields={"kind": "test"},
            array_key="records",
            items=failing_records(),
            relative_path="payload.json",
        )

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".payload.json.*.tmp")) == []


def test_non_finite_number_failure_is_atomic(tmp_path: Path) -> None:
    target = tmp_path / "payload.json"
    target.write_bytes(b"old")

    with pytest.raises(ValueError, match="JSON compliant"):
        write_canonical_object(
            target,
            {"invalid": float("nan")},
            relative_path="payload.json",
        )

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".payload.json.*.tmp")) == []


def test_empty_streamed_containers_match_canonical_bytes(tmp_path: Path) -> None:
    array_path = tmp_path / "array.json"
    mapping_path = tmp_path / "mapping.json"

    array_ref = write_canonical_object_with_array(
        array_path,
        fields={},
        array_key="items",
        items=iter(()),
        relative_path="array.json",
    )
    mapping_ref = write_canonical_object_with_mapping(
        mapping_path,
        fields={},
        mapping_key="items",
        items=iter(()),
        relative_path="mapping.json",
    )

    assert array_path.read_bytes() == canonical_bytes({"items": []})
    assert mapping_path.read_bytes() == canonical_bytes({"items": {}})
    assert array_ref.records == 0
    assert mapping_ref.records == 0


def test_sink_write_exception_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "payload.json"
    target.write_bytes(b"old")
    real_write = AtomicHashingSink.write
    calls = 0

    def fail_second_write(self: AtomicHashingSink, payload) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("sink write failed")
        return real_write(self, payload)

    monkeypatch.setattr(AtomicHashingSink, "write", fail_second_write)
    with pytest.raises(OSError, match="sink write failed"):
        write_canonical_object(
            target,
            {"a": 1},
            relative_path="payload.json",
        )

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".payload.json.*.tmp")) == []


def test_invalid_artifact_metadata_does_not_replace_destination(
    tmp_path: Path,
) -> None:
    target = tmp_path / "payload.json"
    target.write_bytes(b"old")

    with pytest.raises(ValueError, match="relative path"):
        write_canonical_object(
            target,
            {"a": 1},
            relative_path="../payload.json",
        )

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".payload.json.*.tmp")) == []
