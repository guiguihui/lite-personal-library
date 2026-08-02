"""Bounded-memory canonical JSON artifact writers and build receipts."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import TracebackType
from typing import BinaryIO

from .canonical import iter_canonical_json

__all__ = [
    "ArtifactRef",
    "AtomicHashingSink",
    "CandidateReceipt",
    "write_canonical_object",
    "write_canonical_object_with_array",
    "write_canonical_object_with_mapping",
]


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"invalid artifact relative path: {value!r}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.as_posix() != value
    ):
        raise ValueError(f"invalid artifact relative path: {value!r}")
    return value


def _sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_int(value: int | None, field: str, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        suffix = " or None" if optional else ""
        raise ValueError(f"{field} must be an integer >= 0{suffix}")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Compact attestation for one canonical artifact on disk."""

    relative_path: str
    sha256: str
    byte_size: int
    records: int | None = None

    def __post_init__(self) -> None:
        _relative_path(self.relative_path)
        _sha256(self.sha256, "sha256")
        _nonnegative_int(self.byte_size, "byte_size", optional=False)
        _nonnegative_int(self.records, "records", optional=True)


@dataclass(frozen=True, slots=True)
class CandidateReceipt:
    """Lightweight output of a candidate compiler/materializer pass."""

    candidate_dir: Path
    generation_id: str
    revision_sha256: str
    compiler_recipe_hash: str
    input_proof_sha256: str
    manifest_sha256: str
    artifacts: Mapping[str, ArtifactRef]
    segment_refs: Mapping[str, object]
    invariants: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_dir", Path(self.candidate_dir))
        if (
            not isinstance(self.generation_id, str)
            or not re.fullmatch(r"[0-9a-f]{20}", self.generation_id)
        ):
            raise ValueError("generation_id must be 20 lowercase hexadecimal characters")
        _sha256(self.revision_sha256, "revision_sha256")
        _sha256(self.compiler_recipe_hash, "compiler_recipe_hash")
        _sha256(self.input_proof_sha256, "input_proof_sha256")
        _sha256(self.manifest_sha256, "manifest_sha256")
        if not isinstance(self.artifacts, Mapping):
            raise TypeError("artifacts must be a mapping")
        if not isinstance(self.segment_refs, Mapping):
            raise TypeError("segment_refs must be a mapping")
        if not isinstance(self.invariants, Mapping):
            raise TypeError("invariants must be a mapping")
        for relative_path, reference in self.artifacts.items():
            _relative_path(relative_path)
            if not isinstance(reference, ArtifactRef):
                raise TypeError("artifacts values must be ArtifactRef instances")
            if reference.relative_path != relative_path:
                raise ValueError(
                    "artifact mapping key must equal ArtifactRef.relative_path"
                )


class AtomicHashingSink:
    """Write bytes to a sibling temporary file and atomically install them."""

    __slots__ = (
        "_byte_size",
        "_committed",
        "_digest",
        "_started",
        "_stream",
        "_temporary",
        "target",
    )

    def __init__(self, target: Path) -> None:
        self.target = Path(target)
        self._digest = hashlib.sha256()
        self._byte_size = 0
        self._stream: BinaryIO | None = None
        self._temporary: Path | None = None
        self._started = False
        self._committed = False

    @property
    def byte_size(self) -> int:
        return self._byte_size

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def __enter__(self) -> "AtomicHashingSink":
        if self._started:
            raise RuntimeError("atomic hashing sink cannot be reused")
        self._started = True
        self.target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.target.parent,
            prefix=f".{self.target.name}.",
            suffix=".tmp",
        )
        self._temporary = Path(temporary_name)
        try:
            self._stream = os.fdopen(descriptor, "wb")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._remove_temporary()
            raise
        return self

    def write(self, payload: bytes | bytearray | memoryview) -> int:
        if self._stream is None or self._committed:
            raise RuntimeError("atomic hashing sink is not open")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")
        data = bytes(payload)
        written = self._stream.write(data)
        if written != len(data):
            raise OSError(
                f"short artifact write: expected {len(data)} bytes, wrote {written}"
            )
        self._digest.update(data)
        self._byte_size += written
        return written

    def write_text(self, payload: str) -> int:
        if not isinstance(payload, str):
            raise TypeError("payload must be text")
        return self.write(payload.encode("utf-8"))

    def commit(self) -> None:
        if self._committed:
            return
        if self._stream is None or self._temporary is None:
            raise RuntimeError("atomic hashing sink is not open")
        stream = self._stream
        try:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            self._stream = None
            os.replace(self._temporary, self.target)
            self._temporary = None
            self._committed = True
        except BaseException:
            self._stream = None if stream.closed else stream
            self._abort()
            raise

    def _remove_temporary(self) -> None:
        temporary = self._temporary
        self._temporary = None
        if temporary is None:
            return
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    def _abort(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        self._remove_temporary()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            self._abort()
            return False
        self.commit()
        return False


def _write_json_value(sink: AtomicHashingSink, value: object) -> None:
    for fragment in iter_canonical_json(value):
        sink.write_text(fragment)


def _prepare_fields(
    fields: Mapping[str, object],
    streamed_key: str,
) -> dict[str, object]:
    if not isinstance(fields, Mapping):
        raise TypeError("fields must be a mapping")
    if not isinstance(streamed_key, str) or not streamed_key:
        raise ValueError("streamed object key must be a non-empty string")
    prepared = dict(fields)
    if not all(isinstance(key, str) for key in prepared):
        raise TypeError("canonical object keys must be strings")
    if streamed_key in prepared:
        raise ValueError(f"streamed object key is duplicated: {streamed_key!r}")
    return prepared


def _write_object_key(sink: AtomicHashingSink, key: str) -> None:
    _write_json_value(sink, key)
    sink.write(b":")


def _artifact_ref(
    target: Path,
    sink: AtomicHashingSink,
    relative_path: str | None,
    records: int | None,
) -> ArtifactRef:
    return ArtifactRef(
        relative_path=_relative_path(relative_path or target.name),
        sha256=sink.sha256,
        byte_size=sink.byte_size,
        records=records,
    )


def write_canonical_object(
    path: Path,
    value: Mapping[str, object],
    *,
    relative_path: str | None = None,
    records: int | None = None,
) -> ArtifactRef:
    """Atomically write one ordinary canonical JSON object."""

    if not isinstance(value, Mapping):
        raise TypeError("value must be a mapping")
    target = Path(path)
    resolved_relative = _relative_path(relative_path or target.name)
    _nonnegative_int(records, "records", optional=True)
    sink = AtomicHashingSink(target)
    with sink:
        _write_json_value(sink, dict(value))
    return _artifact_ref(target, sink, resolved_relative, records)


def write_canonical_object_with_array(
    path: Path,
    *,
    fields: Mapping[str, object],
    array_key: str,
    items: Iterable[object],
    relative_path: str | None = None,
) -> ArtifactRef:
    """Write an object whose selected array is consumed one item at a time."""

    prepared = _prepare_fields(fields, array_key)
    target = Path(path)
    resolved_relative = _relative_path(relative_path or target.name)
    sink = AtomicHashingSink(target)
    records = 0
    with sink:
        sink.write(b"{")
        first_field = True
        for key in sorted((*prepared, array_key)):
            if not first_field:
                sink.write(b",")
            first_field = False
            _write_object_key(sink, key)
            if key != array_key:
                _write_json_value(sink, prepared[key])
                continue
            sink.write(b"[")
            for item in items:
                if records:
                    sink.write(b",")
                _write_json_value(sink, item)
                records += 1
            sink.write(b"]")
        sink.write(b"}")
    return _artifact_ref(target, sink, resolved_relative, records)


def _mapping_items(
    items: Mapping[str, object] | Iterable[tuple[str, object]],
) -> Iterable[tuple[str, object]]:
    if isinstance(items, Mapping):
        if not all(isinstance(key, str) for key in items):
            raise TypeError("canonical mapping keys must be strings")
        return ((key, items[key]) for key in sorted(items))
    return items


def write_canonical_object_with_mapping(
    path: Path,
    *,
    fields: Mapping[str, object],
    mapping_key: str,
    items: Mapping[str, object] | Iterable[tuple[str, object]],
    relative_path: str | None = None,
) -> ArtifactRef:
    """Write an object whose selected mapping is streamed in sorted-key order."""

    prepared = _prepare_fields(fields, mapping_key)
    target = Path(path)
    sink = AtomicHashingSink(target)
    resolved_relative = _relative_path(relative_path or target.name)
    records = 0
    with sink:
        sink.write(b"{")
        first_field = True
        for key in sorted((*prepared, mapping_key)):
            if not first_field:
                sink.write(b",")
            first_field = False
            _write_object_key(sink, key)
            if key != mapping_key:
                _write_json_value(sink, prepared[key])
                continue

            sink.write(b"{")
            previous: str | None = None
            for item in _mapping_items(items):
                if not isinstance(item, (tuple, list)) or len(item) != 2:
                    raise ValueError("mapping items must be (key, value) pairs")
                item_key, item_value = item
                if not isinstance(item_key, str) or not item_key:
                    raise ValueError("mapping keys must be non-empty strings")
                if previous is not None and item_key <= previous:
                    raise ValueError("mapping keys must be strictly increasing")
                if records:
                    sink.write(b",")
                _write_object_key(sink, item_key)
                _write_json_value(sink, item_value)
                previous = item_key
                records += 1
            sink.write(b"}")
        sink.write(b"}")
    return _artifact_ref(target, sink, resolved_relative, records)
