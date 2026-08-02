"""Bounded external-sort runs for PageIndex field-aware postings.

The compatibility compiler cannot retain all postings in Python objects at
large corpus sizes.  This module stores sorted, length-prefixed binary runs
and merges them with a bounded number of open inputs.
"""

from __future__ import annotations

import heapq
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
import struct
import tempfile
from typing import BinaryIO


_MAGIC = b"PIR1"
_TOKEN_LENGTH = struct.Struct(">I")
_NUMBERS = struct.Struct(">QQQQ")
_MAX_TOKEN_BYTES = 16 * 1024 * 1024


class PostingRunError(ValueError):
    """Raised when a posting run is invalid or cannot be merged safely."""


@dataclass(frozen=True, slots=True)
class PostingRecord:
    """One field-aware posting before compatibility-field aggregation."""

    token: str
    chunk_id: int
    title_tf: int
    breadcrumb_tf: int
    body_tf: int

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("posting token must be a non-empty string")
        for name in ("chunk_id", "title_tf", "breadcrumb_tf", "body_tf"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            if value > 0xFFFFFFFFFFFFFFFF:
                raise ValueError(f"{name} exceeds the posting run integer range")
        if self.chunk_id == 0:
            raise ValueError("chunk_id must be positive")
        if self.title_tf + self.breadcrumb_tf + self.body_tf == 0:
            raise ValueError("posting must contain a positive field TF")

    @property
    def key(self) -> tuple[str, int]:
        return (self.token, self.chunk_id)

    @property
    def encoded_size(self) -> int:
        return _TOKEN_LENGTH.size + len(self.token.encode("utf-8")) + _NUMBERS.size


@dataclass(frozen=True, slots=True)
class PostingRunBuildResult:
    """Run paths and bounded-buffer instrumentation."""

    paths: tuple[Path, ...]
    records: int
    run_buffer_peak_bytes: int
    largest_record_bytes: int


@dataclass(frozen=True, slots=True)
class PostingMergeResult:
    """Result of a bounded fan-in merge."""

    path: Path
    records: int
    passes: int
    peak_open_inputs: int


def _read_exact(stream: BinaryIO, size: int, description: str) -> bytes:
    payload = stream.read(size)
    if len(payload) != size:
        raise PostingRunError(f"truncated posting run {description}")
    return payload


def _write_record(stream: BinaryIO, record: PostingRecord) -> None:
    token = record.token.encode("utf-8")
    if len(token) > _MAX_TOKEN_BYTES:
        raise PostingRunError("posting token exceeds the run size limit")
    stream.write(_TOKEN_LENGTH.pack(len(token)))
    stream.write(token)
    stream.write(
        _NUMBERS.pack(
            record.chunk_id,
            record.title_tf,
            record.breadcrumb_tf,
            record.body_tf,
        )
    )


class PostingRunReader(Iterator[PostingRecord]):
    """Strict streaming reader for one posting run."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._stream: BinaryIO | None = None
        self._last_key: tuple[str, int] | None = None

    def __enter__(self) -> PostingRunReader:
        if self._stream is not None:
            raise RuntimeError("posting run reader is already open")
        stream = self.path.open("rb")
        try:
            if _read_exact(stream, len(_MAGIC), "header") != _MAGIC:
                raise PostingRunError(f"invalid posting run header: {self.path}")
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def __iter__(self) -> PostingRunReader:
        return self

    def __next__(self) -> PostingRecord:
        stream = self._stream
        if stream is None:
            raise RuntimeError("posting run reader is not open")
        length_raw = stream.read(_TOKEN_LENGTH.size)
        if not length_raw:
            raise StopIteration
        if len(length_raw) != _TOKEN_LENGTH.size:
            raise PostingRunError("truncated posting run token length")
        (token_size,) = _TOKEN_LENGTH.unpack(length_raw)
        if token_size == 0 or token_size > _MAX_TOKEN_BYTES:
            raise PostingRunError("invalid posting run token length")
        raw_token = _read_exact(stream, token_size, "token")
        try:
            token = raw_token.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PostingRunError("posting run token is not valid UTF-8") from exc
        numbers = _NUMBERS.unpack(
            _read_exact(stream, _NUMBERS.size, "numeric fields")
        )
        try:
            record = PostingRecord(token, *numbers)
        except ValueError as exc:
            raise PostingRunError(str(exc)) from exc
        if self._last_key is not None and record.key <= self._last_key:
            reason = "duplicate" if record.key == self._last_key else "non-monotonic"
            raise PostingRunError(f"{reason} posting run key: {record.key!r}")
        self._last_key = record.key
        return record


def iter_posting_run(path: Path) -> Iterator[PostingRecord]:
    """Yield records from *path* and always close its handle."""

    with PostingRunReader(path) as reader:
        yield from reader


def _atomic_run_path(destination: Path) -> tuple[int, Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    return descriptor, Path(name)


def _write_sorted_run(path: Path, records: Iterable[PostingRecord]) -> int:
    descriptor, temporary = _atomic_run_path(path)
    count = 0
    last_key: tuple[str, int] | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(_MAGIC)
            for record in records:
                if last_key is not None and record.key <= last_key:
                    reason = "duplicate" if record.key == last_key else "non-monotonic"
                    raise PostingRunError(f"{reason} posting key: {record.key!r}")
                _write_record(stream, record)
                last_key = record.key
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return count
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


class PostingRunBuilder:
    """Collect postings in an encoded-byte-bounded buffer and flush runs."""

    def __init__(self, directory: Path, *, max_run_bytes: int) -> None:
        if isinstance(max_run_bytes, bool) or not isinstance(max_run_bytes, int):
            raise TypeError("max_run_bytes must be an integer")
        if max_run_bytes < 1:
            raise ValueError("max_run_bytes must be positive")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_run_bytes = max_run_bytes
        self._buffer: list[PostingRecord] = []
        self._buffer_bytes = 0
        self._peak_bytes = 0
        self._largest_record = 0
        self._paths: list[Path] = []
        self._records = 0
        self._finished = False

    def add(self, record: PostingRecord) -> None:
        if self._finished:
            raise RuntimeError("posting run builder is already finished")
        if not isinstance(record, PostingRecord):
            raise TypeError("record must be a PostingRecord")
        encoded_size = record.encoded_size
        if self._buffer and self._buffer_bytes + encoded_size > self.max_run_bytes:
            self._flush()
        self._buffer.append(record)
        self._buffer_bytes += encoded_size
        self._records += 1
        self._largest_record = max(self._largest_record, encoded_size)
        self._peak_bytes = max(self._peak_bytes, self._buffer_bytes)

    def _flush(self) -> None:
        if not self._buffer:
            return
        records = sorted(self._buffer, key=lambda item: item.key)
        path = self.directory / f"run-{len(self._paths):08d}.pir"
        _write_sorted_run(path, records)
        self._paths.append(path)
        self._buffer.clear()
        self._buffer_bytes = 0

    def finish(self) -> PostingRunBuildResult:
        if not self._finished:
            self._flush()
            self._finished = True
        return PostingRunBuildResult(
            paths=tuple(self._paths),
            records=self._records,
            run_buffer_peak_bytes=self._peak_bytes,
            largest_record_bytes=self._largest_record,
        )


def _merge_group(
    paths: Sequence[Path],
    destination: Path,
    *,
    check_cancelled: Callable[[], None] | None,
) -> int:
    readers: list[PostingRunReader] = []
    descriptor = -1
    temporary: Path | None = None
    try:
        for path in paths:
            reader = PostingRunReader(path)
            reader.__enter__()
            readers.append(reader)

        descriptor, temporary = _atomic_run_path(destination)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(_MAGIC)
            heap: list[tuple[tuple[str, int], int, PostingRecord]] = []
            for index, reader in enumerate(readers):
                try:
                    record = next(reader)
                except StopIteration:
                    continue
                heapq.heappush(heap, (record.key, index, record))

            count = 0
            last_key: tuple[str, int] | None = None
            while heap:
                if check_cancelled is not None and count % 8192 == 0:
                    check_cancelled()
                _, index, record = heapq.heappop(heap)
                if last_key is not None and record.key <= last_key:
                    reason = "duplicate" if record.key == last_key else "non-monotonic"
                    raise PostingRunError(f"{reason} merged posting key: {record.key!r}")
                _write_record(output, record)
                last_key = record.key
                count += 1
                try:
                    following = next(readers[index])
                except StopIteration:
                    continue
                heapq.heappush(heap, (following.key, index, following))
            output.flush()
            os.fsync(output.fileno())
        for reader in readers:
            reader.close()
        readers.clear()
        os.replace(temporary, destination)
        temporary = None
        return count
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    finally:
        for reader in readers:
            reader.close()
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def merge_posting_runs(
    paths: Sequence[Path],
    destination: Path,
    *,
    fan_in: int = 32,
    check_cancelled: Callable[[], None] | None = None,
) -> PostingMergeResult:
    """Merge sorted *paths* into *destination* with bounded fan-in.

    Input paths are scratch artifacts owned by this operation and are removed
    after their merge group has been durably written.
    """

    if isinstance(fan_in, bool) or not isinstance(fan_in, int):
        raise TypeError("fan_in must be an integer")
    if fan_in < 2:
        raise ValueError("fan_in must be at least two")
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    current = [Path(path) for path in paths]
    passes = 0
    peak_open = 0
    total_records = 0

    if not current:
        total_records = _write_sorted_run(output, ())
        return PostingMergeResult(output, total_records, 0, 0)

    scratch_outputs: set[Path] = set()
    try:
        while len(current) > 1:
            passes += 1
            following: list[Path] = []
            for group_number, start in enumerate(range(0, len(current), fan_in)):
                group = current[start : start + fan_in]
                peak_open = max(peak_open, len(group))
                merged = output.parent / (
                    f"merge-{passes:04d}-{group_number:08d}.pir"
                )
                _merge_group(
                    group,
                    merged,
                    check_cancelled=check_cancelled,
                )
                scratch_outputs.add(merged)
                following.append(merged)
                for path in group:
                    if path != merged:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                        scratch_outputs.discard(path)
            current = following

        final = current[0]
        if final != output:
            os.replace(final, output)
            scratch_outputs.discard(final)
        with PostingRunReader(output) as reader:
            total_records = sum(1 for _ in reader)
        return PostingMergeResult(output, total_records, passes, peak_open)
    except BaseException:
        for path in scratch_outputs:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


__all__ = [
    "PostingMergeResult",
    "PostingRecord",
    "PostingRunBuildResult",
    "PostingRunBuilder",
    "PostingRunError",
    "PostingRunReader",
    "iter_posting_run",
    "merge_posting_runs",
]
