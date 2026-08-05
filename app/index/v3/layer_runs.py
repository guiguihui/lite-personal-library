"""Encoded-byte-bounded external sorting for PageIndex v3 layer postings."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
import hashlib
import heapq
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
from types import TracebackType
from typing import BinaryIO, Literal

from app.index.v2.artifacts import AtomicHashingSink

from .layer_codec import (
    MAX_TOKEN_BYTES,
    LayerDocument,
    PostingLayerReceipt,
    TokenContribution,
    _StagedDocumentArtifacts,
    _finalize_posting_layer,
)
from .models import (
    MAX_U64,
    LayerPosting,
    SearchPosting,
    SearchViewRecipe,
    make_doc_uid,
    validate_doc_key,
    validate_sha256,
)
from .varint import TruncatedVarintError, VarintError, encode_uvarint, read_uvarint


_RUN_MAGIC = b"PIV3RUN1"
_U32 = struct.Struct(">I")
_U64 = struct.Struct(">Q")
_RUN_DIGEST_BYTES = 32


class LayerRunError(ValueError):
    """An external posting run is invalid or cannot be merged safely."""


def _posting_key(row: LayerPosting) -> tuple[bytes, int, int]:
    try:
        token = row.token.encode("utf-8")
    except UnicodeEncodeError as exc:  # LayerPosting already rejects this.
        raise LayerRunError("posting token is not valid UTF-8") from exc
    return token, row.doc_ordinal, row.local_id


def _encoded_record(row: LayerPosting) -> bytes:
    token = row.token.encode("utf-8")
    if not token or len(token) > MAX_TOKEN_BYTES:
        raise LayerRunError("posting token length is outside the run limit")
    return b"".join(
        (
            _U32.pack(len(token)),
            token,
            encode_uvarint(row.doc_ordinal),
            encode_uvarint(row.local_id),
            encode_uvarint(row.title_tf),
            encode_uvarint(row.breadcrumb_tf),
            encode_uvarint(row.body_tf),
        )
    )


def _write_sorted_run(
    path: Path,
    rows: Iterable[LayerPosting],
    *,
    expected_records: int,
) -> int:
    if (
        isinstance(expected_records, bool)
        or not isinstance(expected_records, int)
        or expected_records < 0
        or expected_records > (1 << 64) - 1
    ):
        raise ValueError("expected_records must be a u64")
    sink = AtomicHashingSink(path)
    count = 0
    previous: tuple[bytes, int, int] | None = None
    digest = hashlib.sha256()
    with sink:
        sink.write(_RUN_MAGIC)
        sink.write(_U64.pack(expected_records))
        for row in rows:
            if not isinstance(row, LayerPosting):
                raise TypeError("layer run rows must be LayerPosting values")
            key = _posting_key(row)
            if previous is not None and key <= previous:
                reason = "duplicate" if key == previous else "non-monotonic"
                raise LayerRunError(f"{reason} posting key in layer run")
            encoded = _encoded_record(row)
            sink.write(encoded)
            digest.update(encoded)
            previous = key
            count += 1
        if count != expected_records:
            raise LayerRunError("layer run record count changed while writing")
        sink.write(digest.digest())
    return count

@dataclass(frozen=True, slots=True)
class LayerRunBuildResult:
    paths: tuple[Path, ...]
    records: int
    run_buffer_peak_bytes: int
    largest_record_bytes: int
    run_resident_peak_bytes: int


@dataclass(frozen=True, slots=True)
class LayerRunMergeResult:
    path: Path
    records: int
    passes: int
    peak_open_inputs: int


class LayerRunReader(Iterator[LayerPosting]):
    """Strict unbuffered reader for one counted and digested scratch run."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._stream: BinaryIO | None = None
        self._last_key: tuple[bytes, int, int] | None = None
        self._records = 0
        self._yielded = 0
        self._digest = hashlib.sha256()
        self._finished = False

    def __enter__(self) -> "LayerRunReader":
        if self._stream is not None:
            raise RuntimeError("layer run reader is already open")
        try:
            stream = self.path.open("rb", buffering=256 * 1024)
        except OSError as exc:
            raise LayerRunError(f"cannot open layer run: {self.path}") from exc
        try:
            magic = stream.read(len(_RUN_MAGIC))
            if magic != _RUN_MAGIC:
                raise LayerRunError("invalid or truncated layer run magic")
            count_raw = stream.read(_U64.size)
            if len(count_raw) != _U64.size:
                raise LayerRunError("truncated layer run record count")
            (self._records,) = _U64.unpack(count_raw)
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        self._last_key = None
        self._yielded = 0
        self._digest = hashlib.sha256()
        self._finished = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.close()
        return False

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        self._last_key = None
        self._records = 0
        self._yielded = 0
        self._digest = hashlib.sha256()
        self._finished = False
        if stream is not None:
            stream.close()

    def __iter__(self) -> "LayerRunReader":
        return self

    @property
    def records(self) -> int:
        return self._records

    def _exact(self, size: int, field: str) -> bytes:
        stream = self._stream
        if stream is None:
            raise RuntimeError("layer run reader is not open")
        payload = stream.read(size)
        if len(payload) != size:
            raise LayerRunError(f"truncated layer run {field}")
        return payload

    def _varint(self, field: str) -> int:
        stream = self._stream
        if stream is None:
            raise RuntimeError("layer run reader is not open")
        try:
            return read_uvarint(stream)
        except (EOFError, TruncatedVarintError, VarintError, OSError) as exc:
            raise LayerRunError(
                f"truncated or invalid layer run {field}: {exc}"
            ) from exc

    def __next__(self) -> LayerPosting:
        stream = self._stream
        if stream is None:
            raise RuntimeError("layer run reader is not open")
        if self._finished:
            raise StopIteration
        if self._yielded == self._records:
            footer = self._exact(_RUN_DIGEST_BYTES, "digest footer")
            if footer != self._digest.digest():
                raise LayerRunError("layer run digest footer does not match records")
            if stream.read(1):
                raise LayerRunError("layer run has trailing bytes")
            self._finished = True
            raise StopIteration

        length_raw = stream.read(_U32.size)
        if not length_raw:
            raise LayerRunError("truncated layer run before attested record count")
        if len(length_raw) != _U32.size:
            raise LayerRunError("truncated layer run token length")
        (token_size,) = _U32.unpack(length_raw)
        if token_size == 0 or token_size > MAX_TOKEN_BYTES:
            raise LayerRunError("invalid layer run token length")
        raw_token = self._exact(token_size, "token")
        try:
            token = raw_token.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LayerRunError("layer run token is not valid UTF-8") from exc
        try:
            row = LayerPosting(
                token,
                self._varint("doc_ordinal"),
                self._varint("local_id"),
                self._varint("title_tf"),
                self._varint("breadcrumb_tf"),
                self._varint("body_tf"),
            )
        except LayerRunError:
            raise
        except (TypeError, ValueError) as exc:
            raise LayerRunError(f"invalid layer run posting: {exc}") from exc
        key = _posting_key(row)
        if self._last_key is not None and key <= self._last_key:
            reason = "duplicate" if key == self._last_key else "non-monotonic"
            raise LayerRunError(f"{reason} posting key in layer run")
        self._digest.update(_encoded_record(row))
        self._last_key = key
        self._yielded += 1
        return row

def iter_layer_run(path: Path) -> Iterator[LayerPosting]:
    with LayerRunReader(path) as reader:
        yield from reader


class LayerRunBuilder:
    """Sort and spill before either encoded or charged resident bytes overflow."""

    def __init__(self, directory: Path, *, max_run_bytes: int) -> None:
        if isinstance(max_run_bytes, bool) or not isinstance(max_run_bytes, int):
            raise TypeError("max_run_bytes must be an integer")
        if max_run_bytes < 1:
            raise ValueError("max_run_bytes must be positive")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_run_bytes = max_run_bytes
        self._buffer: list[LayerPosting] = []
        self._buffer_bytes = 0
        self._buffer_resident = 0
        self._peak_bytes = 0
        self._peak_resident = 0
        self._largest_record = 0
        self._records = 0
        self._paths: list[Path] = []
        self._finished = False

    def add(self, row: LayerPosting) -> None:
        if self._finished:
            raise RuntimeError("layer run builder is already finished")
        if not isinstance(row, LayerPosting):
            raise TypeError("row must be a LayerPosting")
        encoded_size = len(_encoded_record(row))
        token_key = row.token.encode("utf-8")
        # Charge the retained object graph plus the transient key objects made
        # by list.sort(key=...).  Using getsizeof(token) is essential: one
        # non-BMP codepoint can switch an otherwise ASCII string to PEP 393's
        # four-byte representation even when its UTF-8 form remains small.
        resident_size = (
            sys.getsizeof(row)
            + sys.getsizeof(row.token)
            + sum(
                sys.getsizeof(value)
                for value in (
                    row.doc_ordinal,
                    row.local_id,
                    row.title_tf,
                    row.breadcrumb_tf,
                    row.body_tf,
                )
            )
            + sys.getsizeof((token_key, row.doc_ordinal, row.local_id))
            + sys.getsizeof(token_key)
            + 64  # list/Timsort pointer arrays and allocator slack per row
        )
        if self._buffer and (
            self._buffer_bytes + encoded_size > self.max_run_bytes
            or self._buffer_resident + resident_size > self.max_run_bytes
        ):
            self._flush()
        self._buffer.append(row)
        self._buffer_bytes += encoded_size
        self._buffer_resident += resident_size
        self._records += 1
        self._largest_record = max(self._largest_record, encoded_size)
        self._peak_bytes = max(self._peak_bytes, self._buffer_bytes)
        self._peak_resident = max(
            self._peak_resident, self._buffer_resident
        )

    def _flush(self) -> None:
        if not self._buffer:
            return
        self._buffer.sort(key=_posting_key)
        path = self.directory / f"run-{len(self._paths):08d}.p3r"
        _write_sorted_run(path, self._buffer, expected_records=len(self._buffer))
        self._paths.append(path)
        self._buffer.clear()
        self._buffer_bytes = 0
        self._buffer_resident = 0

    def finish(self) -> LayerRunBuildResult:
        if not self._finished:
            self._flush()
            self._finished = True
        return LayerRunBuildResult(
            tuple(self._paths),
            self._records,
            self._peak_bytes,
            self._largest_record,
            self._peak_resident,
        )


def _close_readers(readers: list[LayerRunReader]) -> None:
    first: OSError | None = None
    for reader in readers:
        try:
            reader.close()
        except OSError as exc:
            if first is None:
                first = exc
    readers.clear()
    if first is not None:
        raise first


def _add_cleanup_note(primary: BaseException, message: str) -> None:
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(message)


def _cleanup_owned_tree(
    directory: Path,
    primary: BaseException | None,
) -> None:
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        return
    except OSError as exc:
        if primary is not None:
            _add_cleanup_note(
                primary,
                f"cleaning owned layer scratch {directory} also failed: {exc}",
            )
            return
        raise

def _merge_group(
    paths: Sequence[Path],
    destination: Path,
    *,
    check_cancelled: Callable[[], None] | None,
) -> int:
    readers: list[LayerRunReader] = []
    sink = AtomicHashingSink(destination)
    try:
        for path in paths:
            reader = LayerRunReader(path)
            reader.__enter__()
            readers.append(reader)
        expected_records = sum(reader.records for reader in readers)
        if expected_records > (1 << 64) - 1:
            raise LayerRunError("merged layer run record count exceeds u64")
        count = 0
        previous: tuple[bytes, int, int] | None = None
        digest = hashlib.sha256()
        with sink:
            sink.write(_RUN_MAGIC)
            sink.write(_U64.pack(expected_records))
            heap: list[tuple[tuple[bytes, int, int], int, LayerPosting]] = []
            for index, reader in enumerate(readers):
                row = next(reader, None)
                if row is not None:
                    heapq.heappush(heap, (_posting_key(row), index, row))
            while heap:
                if check_cancelled is not None and count % 8192 == 0:
                    check_cancelled()
                key, index, row = heapq.heappop(heap)
                if previous is not None and key <= previous:
                    reason = "duplicate" if key == previous else "non-monotonic"
                    raise LayerRunError(f"{reason} merged posting key")
                encoded = _encoded_record(row)
                sink.write(encoded)
                digest.update(encoded)
                previous = key
                count += 1
                following = next(readers[index], None)
                if following is not None:
                    heapq.heappush(
                        heap, (_posting_key(following), index, following)
                    )
            if count != expected_records:
                raise LayerRunError("merged layer run count is inconsistent")
            sink.write(digest.digest())
        _close_readers(readers)
        return count
    except BaseException as exc:
        try:
            _close_readers(readers)
        except OSError as close_error:
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(f"closing layer run readers also failed: {close_error}")
        raise

def _count_run(
    path: Path,
    check_cancelled: Callable[[], None] | None,
) -> int:
    count = 0
    with LayerRunReader(path) as reader:
        for _row in reader:
            if check_cancelled is not None and count % 8192 == 0:
                check_cancelled()
            count += 1
    return count


def merge_layer_runs(
    paths: Sequence[Path],
    destination: Path,
    *,
    fan_in: int = 32,
    check_cancelled: Callable[[], None] | None = None,
) -> LayerRunMergeResult:
    """Merge owned scratch runs with at most ``fan_in`` simultaneous readers."""

    if isinstance(fan_in, bool) or not isinstance(fan_in, int):
        raise TypeError("fan_in must be an integer")
    if fan_in < 2:
        raise ValueError("fan_in must be at least two")
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    current: list[tuple[Path, int | None]] = [
        (Path(path), None) for path in paths
    ]
    if not current:
        count = _write_sorted_run(output, (), expected_records=0)
        return LayerRunMergeResult(output, count, 0, 0)
    if len(current) == 1:
        path, _known_records = current[0]
        count = _count_run(path, check_cancelled)
        if path != output:
            os.replace(path, output)
        return LayerRunMergeResult(output, count, 0, 1)

    scratch = Path(
        tempfile.mkdtemp(dir=output.parent, prefix=f".{output.name}.merge.")
    )
    passes = 0
    peak_open = 0
    primary_error: BaseException | None = None
    try:
        while len(current) > 1:
            passes += 1
            following: list[tuple[Path, int | None]] = []
            for group_number, start in enumerate(range(0, len(current), fan_in)):
                group = current[start : start + fan_in]
                if len(group) == 1:
                    following.append(group[0])
                    continue
                peak_open = max(peak_open, len(group))
                merged = scratch / f"pass-{passes:04d}-{group_number:08d}.p3r"
                merged_records = _merge_group(
                    [path for path, _records in group],
                    merged,
                    check_cancelled=check_cancelled,
                )
                following.append((merged, merged_records))
                for path, _records in group:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            current = following
        final, known_records = current[0]
        records = (
            _count_run(final, check_cancelled)
            if known_records is None
            else known_records
        )
        if final != output:
            os.replace(final, output)
        return LayerRunMergeResult(output, records, passes, peak_open)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _cleanup_owned_tree(scratch, primary_error)


@dataclass(slots=True)
class _DocumentRoute:
    ordinal: int
    doc_key: str
    doc_uid: str
    segment_hash: str
    chunk_count: int | None = None


def _required_u64(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_U64
    ):
        raise LayerRunError(f"{field} must be a u64")
    return value


class _StagedDocumentTicket:
    """One active document whose postings precede its streamed PCV metrics."""

    def __init__(
        self,
        builder: "StagedLayerBuilder",
        route: _DocumentRoute,
    ) -> None:
        self._builder = builder
        self._route = route
        self._closed = False
        self._posting_count = 0
        self._max_local_id = -1

    @property
    def ordinal(self) -> int:
        return self._route.ordinal

    def add_posting(self, row: SearchPosting) -> None:
        if self._closed:
            raise RuntimeError("staged document ticket is already closed")
        if not isinstance(row, SearchPosting):
            error = TypeError("document ticket postings must be SearchPosting values")
            self._builder._abort_with_primary(error)
            raise error
        ref = row.chunk_ref
        if ref.doc_uid != self._route.doc_uid:
            error = LayerRunError(
                "ticket posting ChunkRef doc_uid does not match the active document"
            )
            self._builder._abort_with_primary(error)
            raise error
        if ref.segment_hash != self._route.segment_hash:
            error = LayerRunError(
                "ticket posting ChunkRef segment_hash does not match the active document"
            )
            self._builder._abort_with_primary(error)
            raise error
        try:
            self._builder._accept_physical(
                LayerPosting(
                    row.token,
                    self._route.ordinal,
                    ref.local_id,
                    row.title_tf,
                    row.breadcrumb_tf,
                    row.body_tf,
                ),
                SearchPosting,
            )
        except BaseException as exc:
            self._builder._abort_with_primary(exc)
            raise
        self._posting_count += 1
        self._max_local_id = max(self._max_local_id, ref.local_id)

    def commit(
        self,
        chunk_count: int,
        chunk_metrics: Iterable[object],
    ) -> None:
        if self._closed:
            raise RuntimeError("staged document ticket is already closed")
        count = _required_u64(chunk_count, "chunk_count")
        if self._max_local_id >= count:
            error = LayerRunError(
                "ticket posting local_id is outside committed chunk metrics"
            )
            self._builder._abort_with_primary(error)
            raise error
        try:
            self._builder._commit_document(self, count, chunk_metrics)
        except BaseException as exc:
            self._builder._abort_with_primary(exc)
            raise
        self._closed = True


class StagedLayerBuilder:
    """Build one layer while retaining at most one document's chunk metrics."""

    def __init__(
        self,
        root: Path,
        *,
        layer_kind: Literal["base", "delta"],
        recipe: SearchViewRecipe | None = None,
        max_run_bytes: int = 64 * 1024 * 1024,
        merge_fan_in: int = 32,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        if layer_kind not in {"base", "delta"} or not isinstance(layer_kind, str):
            raise ValueError("layer_kind must be 'base' or 'delta'")
        physical_recipe = SearchViewRecipe() if recipe is None else recipe
        if not isinstance(physical_recipe, SearchViewRecipe):
            raise TypeError("recipe must be a SearchViewRecipe")
        if isinstance(merge_fan_in, bool) or not isinstance(merge_fan_in, int):
            raise TypeError("merge_fan_in must be an integer")
        if merge_fan_in < 2:
            raise ValueError("merge_fan_in must be at least two")
        if check_cancelled is not None and not callable(check_cancelled):
            raise TypeError("check_cancelled must be callable")

        self.root = Path(root)
        if self.root.exists():
            raise FileExistsError(
                f"posting layer target already exists: {self.root}"
            )
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir()
        self.layer_kind = layer_kind
        self.recipe = physical_recipe
        self.merge_fan_in = merge_fan_in
        self.check_cancelled = check_cancelled
        self._state = "open"
        self._active: _StagedDocumentTicket | None = None
        self._routes: list[_DocumentRoute] = []
        self._by_uid: dict[str, _DocumentRoute] = {}
        self._segments: set[str] = set()
        self._previous_uid: bytes | None = None
        self._mode: type[SearchPosting] | type[LayerPosting] | None = None
        self._posting_count = 0
        self._scratch: Path | None = None
        self._documents: _StagedDocumentArtifacts | None = None
        try:
            self._scratch = Path(
                tempfile.mkdtemp(
                    dir=self.root.parent,
                    prefix=f".{self.root.name}.layer-build.",
                )
            )
            self._runs = LayerRunBuilder(
                self._scratch / "runs",
                max_run_bytes=max_run_bytes,
            )
            self._documents = _StagedDocumentArtifacts(
                self.root,
                check_cancelled=check_cancelled,
            )
        except BaseException as exc:
            self._state = "aborted"
            documents = self._documents
            if documents is not None:
                try:
                    documents.abort(exc)
                except BaseException as cleanup_error:
                    _add_cleanup_note(
                        exc,
                        f"aborting staged document artifacts also failed: "
                        f"{cleanup_error}",
                    )
            if self._scratch is not None:
                _cleanup_owned_tree(self._scratch, exc)
            _cleanup_owned_tree(self.root, exc)
            raise

    def __enter__(self) -> "StagedLayerBuilder":
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._state == "finished":
            return False
        if exc is not None:
            self._abort_with_primary(exc)
            return False
        if self._state == "aborted":
            return False
        error = LayerRunError("staged layer builder exited without finish()")
        self._abort_with_primary(error)
        raise error

    def _ensure_open(self) -> None:
        if self._state != "open":
            raise RuntimeError(
                f"staged layer builder is not open (state={self._state})"
            )

    def begin_document(
        self,
        doc_key: str,
        doc_uid: str,
        segment_hash: str,
    ) -> _StagedDocumentTicket:
        self._ensure_open()
        if self._active is not None:
            error = LayerRunError(
                "previous staged document must be committed before begin_document"
            )
            self._abort_with_primary(error)
            raise error
        try:
            key = validate_doc_key(doc_key)
            validate_sha256(doc_uid, "doc_uid digest")
            if doc_uid != make_doc_uid(key):
                raise ValueError("doc_uid does not match doc_key")
            validate_sha256(segment_hash, "segment_hash digest")
        except (TypeError, ValueError) as exc:
            error = LayerRunError(f"invalid staged document identity: {exc}")
            self._abort_with_primary(error)
            raise error from exc
        encoded = doc_uid.encode("utf-8")
        if self._previous_uid is not None and encoded <= self._previous_uid:
            reason = (
                "duplicate staged document doc_uid"
                if encoded == self._previous_uid
                else "staged documents must be strictly sorted by doc_uid"
            )
            error = LayerRunError(reason)
            self._abort_with_primary(error)
            raise error
        if segment_hash in self._segments:
            error = LayerRunError("duplicate staged document segment_hash")
            self._abort_with_primary(error)
            raise error
        route = _DocumentRoute(
            len(self._routes),
            key,
            doc_uid,
            segment_hash,
        )
        self._routes.append(route)
        self._by_uid[doc_uid] = route
        self._segments.add(segment_hash)
        self._previous_uid = encoded
        ticket = _StagedDocumentTicket(self, route)
        self._active = ticket
        return ticket

    def _accept_physical(
        self,
        row: LayerPosting,
        source_mode: type[SearchPosting] | type[LayerPosting],
    ) -> None:
        self._ensure_open()
        if self._mode is None:
            self._mode = source_mode
        elif self._mode is not source_mode:
            raise LayerRunError(
                "logical and physical posting rows cannot be mixed"
            )
        if self.check_cancelled is not None and self._posting_count % 8192 == 0:
            self.check_cancelled()
        self._runs.add(row)
        self._posting_count += 1

    def add_posting(self, row: SearchPosting | LayerPosting) -> None:
        self._ensure_open()
        if self._active is not None:
            error = LayerRunError(
                "use the active document ticket to add postings before commit"
            )
            self._abort_with_primary(error)
            raise error
        try:
            if isinstance(row, SearchPosting):
                route = self._by_uid.get(row.chunk_ref.doc_uid)
                if route is None or route.chunk_count is None:
                    raise LayerRunError(
                        "posting ChunkRef document is not committed in this layer"
                    )
                if row.chunk_ref.segment_hash != route.segment_hash:
                    raise LayerRunError(
                        "posting ChunkRef segment_hash does not match owner"
                    )
                if row.chunk_ref.local_id >= route.chunk_count:
                    raise LayerRunError(
                        "posting ChunkRef local_id is outside owner document"
                    )
                physical = LayerPosting(
                    row.token,
                    route.ordinal,
                    row.chunk_ref.local_id,
                    row.title_tf,
                    row.breadcrumb_tf,
                    row.body_tf,
                )
                self._accept_physical(physical, SearchPosting)
                return
            if isinstance(row, LayerPosting):
                if row.doc_ordinal >= len(self._routes):
                    raise LayerRunError(
                        "physical posting document ordinal is not committed"
                    )
                route = self._routes[row.doc_ordinal]
                if route.chunk_count is None:
                    raise LayerRunError(
                        "physical posting document is not committed"
                    )
                if row.local_id >= route.chunk_count:
                    raise LayerRunError(
                        "physical posting local_id is outside owner document"
                    )
                self._accept_physical(row, LayerPosting)
                return
            raise TypeError(
                "postings must contain SearchPosting or LayerPosting values"
            )
        except BaseException as exc:
            self._abort_with_primary(exc)
            raise

    def _commit_document(
        self,
        ticket: _StagedDocumentTicket,
        chunk_count: int,
        chunk_metrics: Iterable[object],
    ) -> None:
        self._ensure_open()
        if self._active is not ticket:
            raise LayerRunError("staged document ticket is not active")
        documents = self._documents
        if documents is None:
            raise RuntimeError("staged document artifacts are unavailable")
        route = ticket._route
        documents.append_document(
            ordinal=route.ordinal,
            doc_key=route.doc_key,
            doc_uid=route.doc_uid,
            segment_hash=route.segment_hash,
            chunk_count=chunk_count,
            chunk_metrics=chunk_metrics,
        )
        route.chunk_count = chunk_count
        self._active = None

    def finish(
        self,
        *,
        token_contributions: Iterable[TokenContribution] | None = None,
    ) -> PostingLayerReceipt:
        self._ensure_open()
        if self._active is not None:
            error = LayerRunError(
                "active staged document must be committed before finish"
            )
            self._abort_with_primary(error)
            raise error
        self._state = "finishing"
        sorted_rows: Iterator[LayerPosting] | None = None
        try:
            documents = self._documents
            scratch = self._scratch
            if documents is None or scratch is None:
                raise RuntimeError("staged layer resources are unavailable")
            sealed = documents.seal()
            built = self._runs.finish()
            merged = merge_layer_runs(
                built.paths,
                scratch / "sorted.p3r",
                fan_in=self.merge_fan_in,
                check_cancelled=self.check_cancelled,
            )
            if merged.records != built.records:
                raise LayerRunError(
                    "merged layer run count does not match input count"
                )
            sorted_rows = iter_layer_run(merged.path)
            receipt = _finalize_posting_layer(
                self.root,
                sealed_documents=sealed,
                postings=sorted_rows,
                token_contributions=token_contributions,
                layer_kind=self.layer_kind,
                recipe=self.recipe,
                check_cancelled=self.check_cancelled,
            )
            sorted_rows.close()  # type: ignore[attr-defined]
            sorted_rows = None
            _cleanup_owned_tree(scratch, None)
        except BaseException as exc:
            if sorted_rows is not None:
                try:
                    sorted_rows.close()  # type: ignore[attr-defined]
                except BaseException as close_error:
                    _add_cleanup_note(
                        exc,
                        f"closing sorted layer iterator also failed: {close_error}",
                    )
            self._abort_with_primary(exc)
            raise
        self._state = "finished"
        self._documents = None
        self._scratch = None
        return receipt

    def _abort_with_primary(self, primary: BaseException) -> None:
        if self._state in {"finished", "aborted"}:
            return
        self._state = "aborted"
        self._active = None
        documents = self._documents
        self._documents = None
        if documents is not None:
            try:
                documents.abort(primary)
            except BaseException as cleanup_error:
                _add_cleanup_note(
                    primary,
                    f"aborting staged document artifacts also failed: "
                    f"{cleanup_error}",
                )
        scratch = self._scratch
        self._scratch = None
        if scratch is not None:
            _cleanup_owned_tree(scratch, primary)
        _cleanup_owned_tree(self.root, primary)

    def abort(self) -> None:
        """Abort explicitly and surface any cleanup failure to the caller."""

        if self._state in {"finished", "aborted"}:
            return
        self._state = "aborted"
        self._active = None
        first: BaseException | None = None
        documents = self._documents
        self._documents = None
        if documents is not None:
            try:
                documents.abort()
            except BaseException as exc:
                first = exc
        scratch = self._scratch
        self._scratch = None
        if scratch is not None:
            try:
                _cleanup_owned_tree(scratch, first)
            except BaseException as exc:
                first = exc
        try:
            _cleanup_owned_tree(self.root, first)
        except BaseException as exc:
            first = exc
        if first is not None:
            raise first


def build_sorted_layer(
    root: Path,
    *,
    documents: Iterable[LayerDocument],
    postings: Iterable[SearchPosting | LayerPosting],
    token_contributions: Iterable[TokenContribution] | None = None,
    layer_kind: Literal["base", "delta"],
    recipe: SearchViewRecipe | None = None,
    max_run_bytes: int = 64 * 1024 * 1024,
    merge_fan_in: int = 32,
    check_cancelled: Callable[[], None] | None = None,
) -> PostingLayerReceipt:
    """Compatibility wrapper over the one-document-at-a-time staged builder."""

    if isinstance(documents, (str, bytes, bytearray)):
        raise TypeError("documents must be an iterable of LayerDocument values")
    if isinstance(postings, (str, bytes, bytearray)):
        raise TypeError("postings must be an iterable")
    with StagedLayerBuilder(
        root,
        layer_kind=layer_kind,
        recipe=recipe,
        max_run_bytes=max_run_bytes,
        merge_fan_in=merge_fan_in,
        check_cancelled=check_cancelled,
    ) as builder:
        for document in documents:
            if not isinstance(document, LayerDocument):
                raise TypeError(
                    "documents must contain LayerDocument values"
                )
            ticket = builder.begin_document(
                doc_key=document.doc_key,
                doc_uid=document.doc_uid,
                segment_hash=document.segment_hash,
            )
            ticket.commit(
                chunk_count=document.chunk_count,
                chunk_metrics=iter(document.chunk_metrics),
            )
        for row in postings:
            builder.add_posting(row)
        return builder.finish(token_contributions=token_contributions)

__all__ = [
    "LayerRunBuildResult",
    "LayerRunBuilder",
    "LayerRunError",
    "LayerRunMergeResult",
    "LayerRunReader",
    "StagedLayerBuilder",
    "build_sorted_layer",
    "iter_layer_run",
    "merge_layer_runs",
]
