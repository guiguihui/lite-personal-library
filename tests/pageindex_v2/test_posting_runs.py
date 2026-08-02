"""Tests for bounded PageIndex posting runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.index.v2.posting_runs import (
    PostingRecord,
    PostingRunBuilder,
    PostingRunError,
    iter_posting_run,
    merge_posting_runs,
)


def _record(token: str, chunk_id: int, body_tf: int = 1) -> PostingRecord:
    return PostingRecord(token, chunk_id, 0, 0, body_tf)


def test_builder_enforces_encoded_byte_bound_and_merges_deterministically(
    tmp_path: Path,
) -> None:
    records = [
        _record("中文", 3),
        PostingRecord("alpha", 2, 1, 0, 0),
        PostingRecord("alpha", 1, 0, 2, 0),
        _record("zulu", 4),
    ]
    largest = max(record.encoded_size for record in records)
    builder = PostingRunBuilder(tmp_path / "runs", max_run_bytes=largest)
    for record in reversed(records):
        builder.add(record)
    built = builder.finish()

    assert len(built.paths) == len(records)
    assert built.run_buffer_peak_bytes <= largest
    assert built.largest_record_bytes == largest

    destination = tmp_path / "merged.pir"
    merged = merge_posting_runs(built.paths, destination, fan_in=2)

    assert merged.records == len(records)
    assert merged.passes == 2
    assert merged.peak_open_inputs == 2
    assert list(iter_posting_run(destination)) == sorted(
        records, key=lambda item: item.key
    )


def test_single_record_larger_than_bound_is_allowed_and_reported(
    tmp_path: Path,
) -> None:
    record = _record("token-that-is-longer-than-the-bound", 1)
    builder = PostingRunBuilder(tmp_path / "runs", max_run_bytes=1)
    builder.add(record)

    result = builder.finish()

    assert result.run_buffer_peak_bytes == record.encoded_size
    assert result.run_buffer_peak_bytes <= 1 + result.largest_record_bytes


def test_merge_rejects_duplicate_keys_across_runs(tmp_path: Path) -> None:
    paths: list[Path] = []
    for index in range(2):
        builder = PostingRunBuilder(tmp_path / f"runs-{index}", max_run_bytes=1024)
        builder.add(_record("duplicate", 1))
        paths.extend(builder.finish().paths)

    with pytest.raises(PostingRunError, match="duplicate merged posting key"):
        merge_posting_runs(paths, tmp_path / "merged.pir", fan_in=2)


@pytest.mark.parametrize("damage", ["header", "truncated", "utf8"])
def test_reader_rejects_corrupt_runs(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "broken.pir"
    if damage == "header":
        path.write_bytes(b"NOPE")
    elif damage == "truncated":
        path.write_bytes(b"PIR1\x00\x00")
    else:
        path.write_bytes(
            b"PIR1" + b"\x00\x00\x00\x01" + b"\xff" + (b"\x00" * 32)
        )

    with pytest.raises(PostingRunError):
        list(iter_posting_run(path))


def test_empty_merge_produces_valid_empty_run(tmp_path: Path) -> None:
    destination = tmp_path / "empty.pir"

    result = merge_posting_runs([], destination)

    assert result.records == 0
    assert result.peak_open_inputs == 0
    assert list(iter_posting_run(destination)) == []


def test_cancelled_merge_closes_inputs_and_removes_partial_output(
    tmp_path: Path,
) -> None:
    paths: list[Path] = []
    for index in range(2):
        builder = PostingRunBuilder(tmp_path / f"runs-{index}", max_run_bytes=1024)
        builder.add(_record(f"token-{index}", index + 1))
        paths.extend(builder.finish().paths)

    class Cancelled(RuntimeError):
        pass

    def cancel() -> None:
        raise Cancelled("stop")

    destination = tmp_path / "merged.pir"
    with pytest.raises(Cancelled):
        merge_posting_runs(
            paths,
            destination,
            fan_in=2,
            check_cancelled=cancel,
        )

    assert not destination.exists()
    # Windows will reject these unlinks if merge readers leaked handles.
    for path in paths:
        if path.exists():
            path.unlink()
