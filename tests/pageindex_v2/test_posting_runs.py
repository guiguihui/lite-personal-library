"""Tests for bounded PageIndex posting runs."""

from __future__ import annotations

from pathlib import Path

import pytest

import app.index.v2.posting_runs as posting_runs
from app.index.v2.posting_runs import (
    PostingRecord,
    PostingRunBuilder,
    PostingRunError,
    PostingRunReader,
    iter_posting_run,
    merge_posting_runs,
)


def _record(token: str, chunk_id: int, body_tf: int = 1) -> PostingRecord:
    return PostingRecord(token, chunk_id, 0, 0, body_tf)


def _single_record_runs(tmp_path: Path, count: int) -> list[Path]:
    paths: list[Path] = []
    for index in range(count):
        builder = PostingRunBuilder(tmp_path / f"runs-{index}", max_run_bytes=1024)
        builder.add(_record(f"token-{index:04d}", index + 1))
        paths.extend(builder.finish().paths)
    return paths


def _track_live_readers(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    original_enter = PostingRunReader.__enter__
    original_close = PostingRunReader.close
    state: dict[str, object] = {"live": set(), "opened": [], "peak": 0}

    def tracked_enter(reader: PostingRunReader) -> PostingRunReader:
        result = original_enter(reader)
        live = state["live"]
        opened = state["opened"]
        assert isinstance(live, set)
        assert isinstance(opened, list)
        live.add(id(reader))
        opened.append(reader.path)
        state["peak"] = max(int(state["peak"]), len(live))
        return result

    def tracked_close(reader: PostingRunReader) -> None:
        live = state["live"]
        assert isinstance(live, set)
        try:
            original_close(reader)
        finally:
            live.discard(id(reader))

    monkeypatch.setattr(PostingRunReader, "__enter__", tracked_enter)
    monkeypatch.setattr(PostingRunReader, "close", tracked_close)
    return state


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


def test_builder_sorts_its_bounded_buffer_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = PostingRunBuilder(tmp_path / "runs", max_run_bytes=1024)
    builder.add(_record("zulu", 2))
    builder.add(_record("alpha", 1))
    original_write = posting_runs._write_sorted_run
    observed_buffer: list[bool] = []

    def capture_buffer(path: Path, records: object) -> int:
        observed_buffer.append(records is builder._buffer)
        return original_write(path, records)  # type: ignore[arg-type]

    monkeypatch.setattr(posting_runs, "_write_sorted_run", capture_buffer)
    builder.finish()

    assert observed_buffer == [True]


def test_reader_mark_replays_group_across_readers_of_same_unchanged_file(
    tmp_path: Path,
) -> None:
    records = [
        _record("alpha", 1),
        _record("alpha", 2),
        _record("beta", 3),
    ]
    builder = PostingRunBuilder(tmp_path / "runs", max_run_bytes=1024)
    for record in records:
        builder.add(record)
    path = builder.finish().paths[0]

    with PostingRunReader(path) as scanner, PostingRunReader(path) as replay:
        group_start = scanner.mark()
        assert next(scanner) == records[0]
        assert next(scanner) == records[1]
        next_group = scanner.mark()
        assert next(scanner) == records[2]

        replay.rewind(group_start)
        assert [next(replay), next(replay)] == records[:2]
        assert replay.mark() == next_group
        assert next(replay) == records[2]

        with pytest.raises(ValueError, match="same unchanged file"):
            replay.rewind(4)


def test_reader_mark_rejects_a_different_run(tmp_path: Path) -> None:
    first = PostingRunBuilder(tmp_path / "first", max_run_bytes=1024)
    first.add(_record("alpha", 1))
    first_path = first.finish().paths[0]
    second = PostingRunBuilder(tmp_path / "second", max_run_bytes=1024)
    second.add(_record("beta", 2))
    second_path = second.finish().paths[0]

    with PostingRunReader(first_path) as source:
        mark = source.mark()
    with PostingRunReader(second_path) as target:
        with pytest.raises(ValueError, match="same unchanged file"):
            target.rewind(mark)


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


def test_merge_observes_fan_in_carries_singletons_and_does_not_rescan_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _track_live_readers(monkeypatch)
    original_merge_group = posting_runs._merge_group
    group_sizes: list[int] = []
    scratch_parents_by_call: list[set[Path]] = []

    def tracked_merge_group(
        paths: object,
        destination: Path,
        *,
        check_cancelled: object,
    ) -> int:
        group = list(paths)  # type: ignore[arg-type]
        group_sizes.append(len(group))
        scratch_parents_by_call[-1].add(destination.parent)
        return original_merge_group(
            group,
            destination,
            check_cancelled=check_cancelled,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(posting_runs, "_merge_group", tracked_merge_group)

    first_paths = _single_record_runs(tmp_path / "first", 5)
    first_destination = tmp_path / "first-merged.pir"
    scratch_parents_by_call.append(set())
    first = merge_posting_runs(first_paths, first_destination, fan_in=2)

    second_paths = _single_record_runs(tmp_path / "second", 2)
    second_destination = tmp_path / "second-merged.pir"
    scratch_parents_by_call.append(set())
    second = merge_posting_runs(second_paths, second_destination, fan_in=2)

    assert first.records == 5
    assert second.records == 2
    assert first.passes == 3
    assert group_sizes == [2, 2, 2, 2, 2]
    assert state["peak"] == 2
    assert state["live"] == set()
    opened = state["opened"]
    assert isinstance(opened, list)
    assert first_destination not in opened
    assert second_destination not in opened
    assert all(len(parents) == 1 for parents in scratch_parents_by_call)
    assert scratch_parents_by_call[0].isdisjoint(scratch_parents_by_call[1])
    assert all(
        not parent.exists()
        for parents in scratch_parents_by_call
        for parent in parents
    )


def test_cancelled_merge_closes_inputs_and_removes_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _single_record_runs(tmp_path, 4)
    state = _track_live_readers(monkeypatch)

    class Cancelled(RuntimeError):
        pass

    cancellation_checks = 0

    def cancel() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 2:
            raise Cancelled("stop")

    original_unlink = Path.unlink
    injected_cleanup_failure = False

    def flaky_unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal injected_cleanup_failure
        if (
            not injected_cleanup_failure
            and path.suffix == ".tmp"
            and ".merge." in path.parent.name
        ):
            injected_cleanup_failure = True
            raise PermissionError("injected Windows cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    destination = tmp_path / "merged.pir"
    with pytest.raises(Cancelled) as raised:
        merge_posting_runs(
            paths,
            destination,
            fan_in=2,
            check_cancelled=cancel,
        )

    assert injected_cleanup_failure
    assert any(
        "injected Windows cleanup failure" in note
        for note in getattr(raised.value, "__notes__", ())
    )
    assert not destination.exists()
    assert state["peak"] == 2
    assert state["live"] == set()
    assert list(tmp_path.glob(f".{destination.name}.merge.*")) == []
    # Windows will reject these unlinks if merge readers leaked handles.
    for path in paths:
        if path.exists():
            path.unlink()
