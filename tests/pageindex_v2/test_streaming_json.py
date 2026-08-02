from __future__ import annotations

from pathlib import Path

import pytest

from app.index.v2.streaming_json import CanonicalJsonStream


def test_canonical_json_stream_observes_each_nonempty_chunk_once(
    tmp_path: Path,
) -> None:
    payload = b'{"value":"abcdef"}'
    path = tmp_path / "value.json"
    path.write_bytes(payload)
    observed: list[bytes] = []

    with CanonicalJsonStream(
        path,
        chunk_size=4,
        read_observer=observed.append,
    ) as reader:
        assert reader.read_value() == {"value": "abcdef"}
        reader.finish()

    assert observed == [
        payload[offset : offset + 4]
        for offset in range(0, len(payload), 4)
    ]
    assert all(observed)


def test_canonical_json_stream_does_not_observe_eof(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_bytes(b"")
    observed: list[bytes] = []

    with CanonicalJsonStream(path, chunk_size=1, read_observer=observed.append) as reader:
        reader.finish()

    assert observed == []


def test_canonical_json_stream_propagates_observer_exception_unchanged(
    tmp_path: Path,
) -> None:
    payload = b'{"value":"abcdef"}'
    path = tmp_path / "value.json"
    path.write_bytes(payload)
    raised = RuntimeError("stop observing")
    observed: list[bytes] = []

    def observe(chunk: bytes) -> None:
        observed.append(chunk)
        if len(observed) == 2:
            raise raised

    with pytest.raises(RuntimeError) as caught:
        with CanonicalJsonStream(
            path,
            chunk_size=4,
            read_observer=observe,
        ) as reader:
            reader.read_value()

    assert caught.value is raised
    assert observed == [payload[:4], payload[4:8]]


def test_canonical_json_stream_rejects_noncallable_observer(tmp_path: Path) -> None:
    path = tmp_path / "value.json"
    path.write_bytes(b"null")

    with pytest.raises(TypeError, match="read_observer must be callable"):
        CanonicalJsonStream(path, read_observer=object())  # type: ignore[arg-type]


def test_canonical_json_stream_default_behavior_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "value.json"
    path.write_bytes(b'[1,{"two":2}]')

    with CanonicalJsonStream(path, chunk_size=2) as reader:
        assert reader.read_value() == [1, {"two": 2}]
        reader.finish()
