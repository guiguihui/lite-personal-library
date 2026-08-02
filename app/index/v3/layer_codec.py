"""Seekable, authenticated PageIndex v3 posting-layer artifacts.

The codec deliberately separates the large binary postings/chunk facts from a
sparse canonical term index.  A reader pins exact handles for its lifetime,
authenticates small routing metadata at open, and authenticates large artifacts
locally as blocks are read.  Hot lookups therefore touch one sparse window and
one token/document block rather than rehashing or materializing the layer.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import threading
from types import TracebackType
from typing import BinaryIO, Literal

from app.index.v2.artifacts import (
    ArtifactRef,
    AtomicHashingSink,
    write_canonical_object_with_array,
)
from app.index.v2.canonical import canonical_hash, iter_canonical_json

from .models import (
    MAX_U64,
    ChunkRef,
    LayerPosting,
    SearchPosting,
    SearchViewRecipe,
    make_doc_uid,
    validate_doc_key,
    validate_sha256,
)
from .segment_projection import ChunkMetric
from .varint import TruncatedVarintError, VarintError, encode_uvarint, read_uvarint


POSTINGS_MAGIC = b"PIV3PST1"
CHUNKS_MAGIC = b"PIV3CHK1"
TERM_INDEX_STRIDE = 128
MAX_TOKEN_BYTES = 1 << 20
MAX_JSONL_LINE_BYTES = (MAX_TOKEN_BYTES * 6) + 4096

_DOCUMENTS_NAME = "layer-documents.json"
_POSTINGS_NAME = "postings.piv"
_CHUNKS_NAME = "chunks.pcv"
_TERMS_NAME = "terms.jsonl"
_SPARSE_NAME = "terms.sidx.json"
_U32 = struct.Struct(">I")
_U64 = struct.Struct(">Q")


class LayerCodecError(ValueError):
    """A layer artifact is invalid, corrupt, or inconsistent with its receipt."""


def _u64(value: object, field: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > MAX_U64
    ):
        raise ValueError(f"{field} must be a u64 in [{minimum}, {MAX_U64}]")
    return value


def _signed_u64(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < -MAX_U64
        or value > MAX_U64
    ):
        raise ValueError(f"{field} must be an integer in [-{MAX_U64}, {MAX_U64}]")
    return value


def _token_bytes(value: object) -> bytes:
    if not isinstance(value, str):
        raise TypeError("token must be a string")
    if not value:
        raise ValueError("token must be non-empty")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("token must be valid UTF-8 text") from exc
    if len(encoded) > MAX_TOKEN_BYTES:
        raise ValueError(f"token exceeds {MAX_TOKEN_BYTES} UTF-8 bytes")
    return encoded


def _layer_kind(value: object) -> Literal["base", "delta"]:
    if value not in {"base", "delta"} or not isinstance(value, str):
        raise ValueError("layer_kind must be 'base' or 'delta'")
    return value  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class LayerDocument:
    """One layer-local document and its text-free candidate metrics."""

    doc_key: str
    doc_uid: str
    segment_hash: str
    chunk_metrics: tuple[ChunkMetric, ...]

    def __post_init__(self) -> None:
        key = validate_doc_key(self.doc_key)
        validate_sha256(self.doc_uid, "doc_uid digest")
        if self.doc_uid != make_doc_uid(key):
            raise ValueError("document doc_uid does not match doc_key")
        validate_sha256(self.segment_hash, "segment_hash digest")
        if isinstance(self.chunk_metrics, (str, bytes, bytearray)):
            raise TypeError("document chunk_metrics must be an iterable")
        try:
            metrics = tuple(self.chunk_metrics)
        except TypeError as exc:
            raise TypeError("document chunk_metrics must be an iterable") from exc
        if not all(isinstance(item, ChunkMetric) for item in metrics):
            raise TypeError("document chunk_metrics must contain ChunkMetric values")
        for expected, metric in enumerate(metrics):
            if metric.local_id != expected:
                raise ValueError("document chunk metrics must have compact local IDs")
        object.__setattr__(self, "chunk_metrics", metrics)

    @property
    def chunk_count(self) -> int:
        return len(self.chunk_metrics)


@dataclass(frozen=True, slots=True, order=True)
class TokenContribution:
    """One base-positive or delta-signed token DF contribution."""

    token: str
    df_any_delta: int
    df_nonbody_delta: int
    df_body_delta: int

    def __post_init__(self) -> None:
        _token_bytes(self.token)
        _signed_u64(self.df_any_delta, "df_any_delta")
        _signed_u64(self.df_nonbody_delta, "df_nonbody_delta")
        _signed_u64(self.df_body_delta, "df_body_delta")

    @property
    def triple(self) -> tuple[int, int, int]:
        return self.df_any_delta, self.df_nonbody_delta, self.df_body_delta


@dataclass(frozen=True, slots=True)
class TermRecord:
    """Canonical random-seek metadata and local PIV authentication."""

    token: str
    block_offset: int | None
    block_bytes: int
    nonbody_rows: int
    body_rows: int
    df_any_delta: int
    df_nonbody_delta: int
    df_body_delta: int
    prefix_bytes: int
    prefix_sha256: str | None
    body_offset: int | None
    body_bytes: int
    body_sha256: str | None

    def __post_init__(self) -> None:
        _token_bytes(self.token)
        block_bytes = _u64(self.block_bytes, "block_bytes")
        nonbody = _u64(self.nonbody_rows, "nonbody_rows")
        body = _u64(self.body_rows, "body_rows")
        prefix_bytes = _u64(self.prefix_bytes, "prefix_bytes")
        body_bytes = _u64(self.body_bytes, "body_bytes")
        _signed_u64(self.df_any_delta, "df_any_delta")
        _signed_u64(self.df_nonbody_delta, "df_nonbody_delta")
        _signed_u64(self.df_body_delta, "df_body_delta")
        has_postings = nonbody + body > 0
        if has_postings:
            block_offset = _u64(self.block_offset, "block_offset")
            if block_bytes == 0 or prefix_bytes == 0:
                raise ValueError("posting term block and prefix bytes must be positive")
            validate_sha256(self.prefix_sha256, "prefix_sha256")
            if body:
                body_offset = _u64(self.body_offset, "body_offset")
                if body_offset != block_offset + prefix_bytes:
                    raise ValueError("body_offset must immediately follow the PIV prefix")
                if body_bytes == 0:
                    raise ValueError("body postings require positive body_bytes")
                validate_sha256(self.body_sha256, "body_sha256")
            elif (
                self.body_offset is not None
                or body_bytes != 0
                or self.body_sha256 is not None
            ):
                raise ValueError("body-free term must use null/zero body attestation")
            if prefix_bytes + body_bytes != block_bytes:
                raise ValueError("PIV prefix and body bytes must exactly cover the block")
        elif (
            self.block_offset is not None
            or block_bytes != 0
            or prefix_bytes != 0
            or self.prefix_sha256 is not None
            or self.body_offset is not None
            or body_bytes != 0
            or self.body_sha256 is not None
        ):
            raise ValueError("posting-free term must use null/zero PIV attestations")
        if not has_postings and self.delta == (0, 0, 0):
            raise ValueError("posting-free term cannot have an all-zero DF delta")

    @property
    def delta(self) -> tuple[int, int, int]:
        return self.df_any_delta, self.df_nonbody_delta, self.df_body_delta

    @property
    def has_postings(self) -> bool:
        return self.nonbody_rows + self.body_rows > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "block_offset": self.block_offset,
            "block_bytes": self.block_bytes,
            "nonbody_rows": self.nonbody_rows,
            "body_rows": self.body_rows,
            "df_any_delta": self.df_any_delta,
            "df_nonbody_delta": self.df_nonbody_delta,
            "df_body_delta": self.df_body_delta,
            "prefix_bytes": self.prefix_bytes,
            "prefix_sha256": self.prefix_sha256,
            "body_offset": self.body_offset,
            "body_bytes": self.body_bytes,
            "body_sha256": self.body_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "TermRecord":
        if not isinstance(value, Mapping):
            raise LayerCodecError("term JSONL record must be an object")
        expected = {
            "token",
            "block_offset",
            "block_bytes",
            "nonbody_rows",
            "body_rows",
            "df_any_delta",
            "df_nonbody_delta",
            "df_body_delta",
            "prefix_bytes",
            "prefix_sha256",
            "body_offset",
            "body_bytes",
            "body_sha256",
        }
        if set(value) != expected:
            raise LayerCodecError("term JSONL record has invalid keys")
        try:
            return cls(**{key: value[key] for key in expected})  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise LayerCodecError(str(exc)) from exc

def _artifact_dict(reference: ArtifactRef) -> dict[str, object]:
    return {
        "relative_path": reference.relative_path,
        "sha256": reference.sha256,
        "byte_size": reference.byte_size,
        "records": reference.records,
    }


def _artifact_from_dict(value: object, role: str) -> ArtifactRef:
    if not isinstance(value, Mapping):
        raise ValueError(f"{role} artifact receipt must be an object")
    expected = {"relative_path", "sha256", "byte_size", "records"}
    if set(value) != expected:
        raise ValueError(f"{role} artifact receipt has invalid keys")
    try:
        return ArtifactRef(
            relative_path=value["relative_path"],  # type: ignore[arg-type]
            sha256=value["sha256"],  # type: ignore[arg-type]
            byte_size=value["byte_size"],  # type: ignore[arg-type]
            records=value["records"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {role} artifact receipt: {exc}") from exc


@dataclass(frozen=True, slots=True)
class PostingLayerReceipt:
    """Trusted attestation and physical locator for exactly one posting layer."""

    root: Path
    layer_kind: Literal["base", "delta"]
    search_view_recipe_hash: str
    documents: ArtifactRef
    postings: ArtifactRef
    chunks: ArtifactRef
    terms: ArtifactRef
    sparse_index: ArtifactRef
    document_count: int
    chunk_count: int
    term_count: int
    nonbody_rows: int
    body_rows: int
    schema_version: int = 1
    artifact_kind: str = "piv3_posting_layer_receipt"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported posting layer receipt schema_version")
        if self.artifact_kind != "piv3_posting_layer_receipt":
            raise ValueError("unsupported posting layer receipt artifact_kind")
        _layer_kind(self.layer_kind)
        validate_sha256(self.search_view_recipe_hash, "search_view_recipe_hash")
        expected_paths = (
            ("documents", self.documents, _DOCUMENTS_NAME),
            ("postings", self.postings, _POSTINGS_NAME),
            ("chunks", self.chunks, _CHUNKS_NAME),
            ("terms", self.terms, _TERMS_NAME),
            ("sparse_index", self.sparse_index, _SPARSE_NAME),
        )
        for role, reference, path in expected_paths:
            if not isinstance(reference, ArtifactRef):
                raise TypeError(f"{role} must be an ArtifactRef")
            if reference.relative_path != path:
                raise ValueError(f"{role} artifact path must be {path!r}")
            _u64(reference.byte_size, f"{role}.byte_size")
            if reference.records is None:
                raise ValueError(f"{role}.records must be attested")
            _u64(reference.records, f"{role}.records")
        if self.postings.byte_size < len(POSTINGS_MAGIC):
            raise ValueError("postings.piv is smaller than its magic")
        if self.chunks.byte_size < len(CHUNKS_MAGIC):
            raise ValueError("chunks.pcv is smaller than its magic")
        document_count = _u64(self.document_count, "document_count")
        chunk_count = _u64(self.chunk_count, "chunk_count")
        term_count = _u64(self.term_count, "term_count")
        nonbody_rows = _u64(self.nonbody_rows, "nonbody_rows")
        body_rows = _u64(self.body_rows, "body_rows")
        if self.documents.records != document_count:
            raise ValueError("documents.records does not match document_count")
        if self.chunks.records != chunk_count:
            raise ValueError("chunks.records does not match chunk_count")
        if self.terms.records != term_count:
            raise ValueError("terms.records does not match term_count")
        if self.postings.records != nonbody_rows + body_rows:
            raise ValueError("postings.records does not match partition row counts")
        expected_anchors = (term_count + TERM_INDEX_STRIDE - 1) // TERM_INDEX_STRIDE
        if self.sparse_index.records != expected_anchors:
            raise ValueError("sparse_index.records does not match term_count")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "schema_version": self.schema_version,
            "layer_kind": self.layer_kind,
            "search_view_recipe_hash": self.search_view_recipe_hash,
            "counts": {
                "documents": self.document_count,
                "chunks": self.chunk_count,
                "terms": self.term_count,
                "nonbody_rows": self.nonbody_rows,
                "body_rows": self.body_rows,
            },
            "artifacts": {
                "documents": _artifact_dict(self.documents),
                "postings": _artifact_dict(self.postings),
                "chunks": _artifact_dict(self.chunks),
                "terms": _artifact_dict(self.terms),
                "sparse_index": _artifact_dict(self.sparse_index),
            },
        }

    @classmethod
    def from_dict(cls, root: Path, value: object) -> "PostingLayerReceipt":
        if not isinstance(value, Mapping):
            raise ValueError("posting layer receipt must be an object")
        expected = {
            "artifact_kind",
            "schema_version",
            "layer_kind",
            "search_view_recipe_hash",
            "counts",
            "artifacts",
        }
        if set(value) != expected:
            raise ValueError("posting layer receipt has invalid keys")
        counts = value["counts"]
        artifacts = value["artifacts"]
        if not isinstance(counts, Mapping) or set(counts) != {
            "documents", "chunks", "terms", "nonbody_rows", "body_rows"
        }:
            raise ValueError("posting layer receipt counts are invalid")
        if not isinstance(artifacts, Mapping) or set(artifacts) != {
            "documents", "postings", "chunks", "terms", "sparse_index"
        }:
            raise ValueError("posting layer receipt artifacts are invalid")
        return cls(
            root=Path(root),
            layer_kind=value["layer_kind"],  # type: ignore[arg-type]
            search_view_recipe_hash=value["search_view_recipe_hash"],  # type: ignore[arg-type]
            documents=_artifact_from_dict(artifacts["documents"], "documents"),
            postings=_artifact_from_dict(artifacts["postings"], "postings"),
            chunks=_artifact_from_dict(artifacts["chunks"], "chunks"),
            terms=_artifact_from_dict(artifacts["terms"], "terms"),
            sparse_index=_artifact_from_dict(artifacts["sparse_index"], "sparse_index"),
            document_count=counts["documents"],  # type: ignore[arg-type]
            chunk_count=counts["chunks"],  # type: ignore[arg-type]
            term_count=counts["terms"],  # type: ignore[arg-type]
            nonbody_rows=counts["nonbody_rows"],  # type: ignore[arg-type]
            body_rows=counts["body_rows"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
            artifact_kind=value["artifact_kind"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class _DocumentRecord:
    doc_key: str
    doc_uid: str
    segment_hash: str
    chunk_count: int
    chunk_block_offset: int
    chunk_block_bytes: int
    chunk_block_sha256: str


@dataclass(frozen=True, slots=True)
class _SparseWindow:
    first_token: str
    offset: int
    byte_size: int
    sha256: str
    lines: int

    @property
    def end(self) -> int:
        return self.offset + self.byte_size

def _ref_from_sink(
    name: str,
    sink: AtomicHashingSink,
    records: int,
) -> ArtifactRef:
    return ArtifactRef(name, sink.sha256, sink.byte_size, records)


def _write_jsonl(
    sink: AtomicHashingSink,
    record: TermRecord,
    digest: "hashlib._Hash",
) -> int:
    line_bytes = 1
    for fragment in iter_canonical_json(record.as_dict()):
        line_bytes += len(fragment.encode("utf-8"))
    if line_bytes > MAX_JSONL_LINE_BYTES:
        raise LayerCodecError("term JSONL record exceeds the safety limit")
    for fragment in iter_canonical_json(record.as_dict()):
        payload = fragment.encode("utf-8")
        sink.write(payload)
        digest.update(payload)
    sink.write(b"\n")
    digest.update(b"\n")
    return line_bytes

def _encoded_nonbody(row: LayerPosting) -> bytes:
    return b"".join(
        (
            encode_uvarint(row.doc_ordinal),
            encode_uvarint(row.local_id),
            encode_uvarint(row.title_tf),
            encode_uvarint(row.breadcrumb_tf),
        )
    )


def _encoded_body(row: LayerPosting) -> bytes:
    return b"".join(
        (
            encode_uvarint(row.doc_ordinal),
            encode_uvarint(row.local_id),
            encode_uvarint(row.body_tf),
        )
    )


class _Peekable:
    __slots__ = ("_iterator", "_value")

    def __init__(self, values: Iterable[object]) -> None:
        self._iterator = iter(values)
        self._value = next(self._iterator, None)

    @property
    def value(self) -> object | None:
        return self._value

    def pop(self) -> object:
        if self._value is None:
            raise StopIteration
        value = self._value
        self._value = next(self._iterator, None)
        return value


def _validated_postings(
    values: Iterable[LayerPosting],
    document_chunk_counts: tuple[int, ...],
) -> Iterator[LayerPosting]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("postings must be an iterable of LayerPosting values")
    previous: tuple[bytes, int, int] | None = None
    for value in values:
        if not isinstance(value, LayerPosting):
            raise TypeError("postings must contain only LayerPosting values")
        token = _token_bytes(value.token)
        key = token, value.doc_ordinal, value.local_id
        if previous is not None and key <= previous:
            reason = "duplicate" if key == previous else "non-monotonic"
            raise LayerCodecError(f"{reason} posting key")
        if value.doc_ordinal >= len(document_chunk_counts):
            raise LayerCodecError("posting references an unknown document ordinal")
        if value.local_id >= document_chunk_counts[value.doc_ordinal]:
            raise LayerCodecError("posting local_id is outside the document")
        previous = key
        yield value


def _validated_contributions(
    values: Iterable[TokenContribution],
) -> Iterator[TokenContribution]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("token_contributions must be an iterable")
    previous: bytes | None = None
    for value in values:
        if not isinstance(value, TokenContribution):
            raise TypeError("token_contributions must contain TokenContribution values")
        encoded = _token_bytes(value.token)
        if previous is not None and encoded <= previous:
            reason = "duplicate" if encoded == previous else "non-monotonic"
            raise LayerCodecError(f"{reason} token contribution")
        previous = encoded
        yield value


def _copy_spool(
    spool: BinaryIO,
    sink: AtomicHashingSink,
    digest: "hashlib._Hash",
    check_cancelled: Callable[[], None] | None = None,
) -> int:
    spool.seek(0)
    written = 0
    while True:
        if check_cancelled is not None:
            check_cancelled()
        payload = spool.read(64 * 1024)
        if not payload:
            return written
        sink.write(payload)
        digest.update(payload)
        written += len(payload)


def _reset_spool(spool: BinaryIO) -> None:
    spool.seek(0)
    spool.truncate(0)


def _write_posting_group(
    rows: _Peekable,
    token: str,
    token_bytes: bytes,
    sink: AtomicHashingSink,
    nonbody_spool: BinaryIO,
    body_spool: BinaryIO,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[int, int, int, int, int, int, str, int | None, int, str | None]:
    _reset_spool(nonbody_spool)
    _reset_spool(body_spool)
    any_rows = nonbody_rows = body_rows = 0
    while isinstance(rows.value, LayerPosting) and rows.value.token == token:
        if check_cancelled is not None and any_rows % 8192 == 0:
            check_cancelled()
        row = rows.pop()
        assert isinstance(row, LayerPosting)
        any_rows += 1
        if row.title_tf or row.breadcrumb_tf:
            nonbody_spool.write(_encoded_nonbody(row))
            nonbody_rows += 1
        if row.body_tf:
            body_spool.write(_encoded_body(row))
            body_rows += 1

    block_offset = sink.byte_size
    prefix_digest = hashlib.sha256()

    def write_prefix(payload: bytes) -> None:
        sink.write(payload)
        prefix_digest.update(payload)

    write_prefix(_U32.pack(len(token_bytes)))
    write_prefix(token_bytes)
    write_prefix(_U64.pack(nonbody_rows))
    prefix_spool_bytes = _copy_spool(nonbody_spool, sink, prefix_digest, check_cancelled)
    write_prefix(_U64.pack(body_rows))
    prefix_bytes = (
        _U32.size
        + len(token_bytes)
        + _U64.size
        + prefix_spool_bytes
        + _U64.size
    )
    if sink.byte_size != block_offset + prefix_bytes:
        raise AssertionError("PIV prefix byte accounting drifted")

    body_offset: int | None = None
    body_bytes = 0
    body_sha256: str | None = None
    if body_rows:
        body_offset = sink.byte_size
        body_digest = hashlib.sha256()
        body_bytes = _copy_spool(body_spool, sink, body_digest, check_cancelled)
        body_sha256 = body_digest.hexdigest()
    block_bytes = sink.byte_size - block_offset
    return (
        block_offset,
        block_bytes,
        any_rows,
        nonbody_rows,
        body_rows,
        prefix_bytes,
        prefix_digest.hexdigest(),
        body_offset,
        body_bytes,
        body_sha256,
    )

@dataclass(frozen=True, slots=True)
class _SealedDocuments:
    """Closed document artifacts plus lean ordinal bounds for PIV validation."""

    documents_ref: ArtifactRef
    chunks_ref: ArtifactRef
    document_chunk_counts: tuple[int, ...]
    document_count: int
    chunk_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.documents_ref, ArtifactRef):
            raise TypeError("documents_ref must be an ArtifactRef")
        if not isinstance(self.chunks_ref, ArtifactRef):
            raise TypeError("chunks_ref must be an ArtifactRef")
        if self.documents_ref.relative_path != _DOCUMENTS_NAME:
            raise ValueError("documents_ref has an invalid relative path")
        if self.chunks_ref.relative_path != _CHUNKS_NAME:
            raise ValueError("chunks_ref has an invalid relative path")
        if not isinstance(self.document_chunk_counts, tuple):
            raise TypeError("document_chunk_counts must be a tuple")
        document_count = _u64(self.document_count, "document_count")
        chunk_count = _u64(self.chunk_count, "chunk_count")
        if len(self.document_chunk_counts) != document_count:
            raise ValueError("document chunk-count table has an invalid length")
        observed_chunks = 0
        for count in self.document_chunk_counts:
            observed_chunks += _u64(count, "document chunk_count")
            _u64(observed_chunks, "chunk_count")
        if observed_chunks != chunk_count:
            raise ValueError("document chunk-count table has an invalid total")
        if self.documents_ref.records != document_count:
            raise ValueError("documents_ref.records does not match document_count")
        if self.chunks_ref.records != chunk_count:
            raise ValueError("chunks_ref.records does not match chunk_count")
        if self.chunks_ref.byte_size < len(CHUNKS_MAGIC):
            raise ValueError("chunks_ref is smaller than its magic")


def _document_record_dict(record: _DocumentRecord) -> dict[str, object]:
    return {
        "doc_key": record.doc_key,
        "doc_uid": record.doc_uid,
        "segment_hash": record.segment_hash,
        "chunk_count": record.chunk_count,
        "chunk_block_offset": record.chunk_block_offset,
        "chunk_block_bytes": record.chunk_block_bytes,
        "chunk_block_sha256": record.chunk_block_sha256,
    }


def _write_spooled_document_record(
    spool: BinaryIO,
    record: _DocumentRecord,
) -> None:
    for fragment in iter_canonical_json(_document_record_dict(record)):
        payload = fragment.encode("utf-8")
        if spool.write(payload) != len(payload):
            raise OSError("short write to staged document record spool")
    if spool.write(b"\n") != 1:
        raise OSError("short write to staged document record spool")


def _iter_spooled_document_records(
    spool: BinaryIO,
    expected_records: int,
    check_cancelled: Callable[[], None] | None,
) -> Iterator[object]:
    spool.flush()
    spool.seek(0)
    observed = 0
    while True:
        raw = spool.readline()
        if not raw:
            break
        if check_cancelled is not None:
            check_cancelled()
        if not raw.endswith(b"\n"):
            raise LayerCodecError("truncated staged document record")
        parsed = _strict_json(raw[:-1], "staged document record")
        if not isinstance(parsed, Mapping):
            raise LayerCodecError("staged document record must be an object")
        if set(parsed) != {
            "doc_key",
            "doc_uid",
            "segment_hash",
            "chunk_count",
            "chunk_block_offset",
            "chunk_block_bytes",
            "chunk_block_sha256",
        }:
            raise LayerCodecError("staged document record has invalid keys")
        observed += 1
        if observed > expected_records:
            raise LayerCodecError("staged document record count is too large")
        yield parsed
    if observed != expected_records:
        raise LayerCodecError("staged document record count is invalid")


class _StagedDocumentArtifacts:
    """Append PCV blocks and spool canonical document records one at a time."""

    def __init__(
        self,
        root: Path,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        if check_cancelled is not None and not callable(check_cancelled):
            raise TypeError("check_cancelled must be callable")
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(
                f"posting layer staging root does not exist: {self.root}"
            )
        self._check_cancelled = check_cancelled
        self._chunk_sink: AtomicHashingSink | None = None
        self._record_spool: BinaryIO | None = None
        self._document_chunk_counts: list[int] = []
        self._chunk_count = 0
        self._previous_doc_uid: bytes | None = None
        self._segment_hashes: set[str] = set()
        self._state = "open"
        try:
            self._record_spool = tempfile.TemporaryFile(
                mode="w+b",
                buffering=256 * 1024,
                dir=self.root,
                prefix=".layer-documents.",
                suffix=".spool",
            )
            chunk_sink = AtomicHashingSink(self.root / _CHUNKS_NAME)
            self._chunk_sink = chunk_sink
            chunk_sink.__enter__()
            chunk_sink.write(CHUNKS_MAGIC)
        except BaseException as exc:
            self._state = "failed"
            self.abort(exc)
            raise

    @property
    def document_count(self) -> int:
        return len(self._document_chunk_counts)

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    def _ensure_open(self) -> tuple[AtomicHashingSink, BinaryIO]:
        if self._state != "open":
            raise RuntimeError("staged document artifacts are not open")
        sink = self._chunk_sink
        spool = self._record_spool
        if sink is None or spool is None:
            raise RuntimeError("staged document artifact handles are closed")
        return sink, spool

    def append_document(
        self,
        *,
        ordinal: int,
        doc_key: str,
        doc_uid: str,
        segment_hash: str,
        chunk_count: int,
        chunk_metrics: Iterable[ChunkMetric],
    ) -> None:
        """Append one document without retaining any of its ChunkMetric values."""

        sink, spool = self._ensure_open()
        try:
            encoded_ordinal = _u64(ordinal, "document ordinal")
            if encoded_ordinal != self.document_count:
                raise ValueError("document ordinal is not the next dense ordinal")
            key = validate_doc_key(doc_key)
            validate_sha256(doc_uid, "doc_uid digest")
            if doc_uid != make_doc_uid(key):
                raise ValueError("document doc_uid does not match doc_key")
            validate_sha256(segment_hash, "segment_hash digest")
            encoded_uid = doc_uid.encode("utf-8")
            if (
                self._previous_doc_uid is not None
                and encoded_uid <= self._previous_doc_uid
            ):
                reason = (
                    "duplicate"
                    if encoded_uid == self._previous_doc_uid
                    else "noncanonical order"
                )
                raise ValueError(f"{reason} document in layer document table")
            if segment_hash in self._segment_hashes:
                raise ValueError("duplicate segment_hash in layer document table")
            expected_chunks = _u64(chunk_count, "document chunk_count")
            new_chunk_total = _u64(
                self._chunk_count + expected_chunks,
                "chunk_count",
            )
            if isinstance(chunk_metrics, (str, bytes, bytearray)):
                raise TypeError("chunk_metrics must be an iterable")
            try:
                metrics = iter(chunk_metrics)
            except TypeError as exc:
                raise TypeError("chunk_metrics must be an iterable") from exc

            callback = self._check_cancelled
            if callback is not None:
                callback()
            offset = sink.byte_size
            digest = hashlib.sha256()

            def write_block(payload: bytes) -> None:
                sink.write(payload)
                digest.update(payload)

            write_block(encode_uvarint(encoded_ordinal))
            write_block(encode_uvarint(expected_chunks))
            observed_chunks = 0
            for metric in metrics:
                if not isinstance(metric, ChunkMetric):
                    raise TypeError(
                        "chunk_metrics must contain only ChunkMetric values"
                    )
                if observed_chunks >= expected_chunks:
                    raise ValueError("chunk_metrics contains more than chunk_count")
                if metric.local_id != observed_chunks:
                    raise ValueError(
                        "document chunk metrics must have compact local IDs"
                    )
                if callback is not None and metric.local_id % 8192 == 0:
                    callback()
                write_block(encode_uvarint(metric.local_id))
                write_block(encode_uvarint(metric.title_length))
                write_block(encode_uvarint(metric.breadcrumb_length))
                write_block(encode_uvarint(metric.body_length))
                observed_chunks += 1
            if observed_chunks != expected_chunks:
                raise ValueError("chunk_metrics contains fewer than chunk_count")

            block_bytes = sink.byte_size - offset
            _write_spooled_document_record(
                spool,
                _DocumentRecord(
                    key,
                    doc_uid,
                    segment_hash,
                    expected_chunks,
                    offset,
                    block_bytes,
                    digest.hexdigest(),
                ),
            )
            self._document_chunk_counts.append(expected_chunks)
            self._chunk_count = new_chunk_total
            self._previous_doc_uid = encoded_uid
            self._segment_hashes.add(segment_hash)
        except BaseException:
            self._state = "failed"
            raise

    def seal(self) -> _SealedDocuments:
        """Close PCV/record handles and materialize canonical documents metadata."""

        sink, spool = self._ensure_open()
        try:
            callback = self._check_cancelled
            if callback is not None:
                callback()
            sink.commit()
            chunks_ref = _ref_from_sink(
                _CHUNKS_NAME,
                sink,
                self._chunk_count,
            )
            self._chunk_sink = None
            documents_ref = write_canonical_object_with_array(
                self.root / _DOCUMENTS_NAME,
                fields={
                    "artifact_kind": "piv3_layer_documents",
                    "schema_version": 1,
                },
                array_key="documents",
                items=_iter_spooled_document_records(
                    spool,
                    self.document_count,
                    callback,
                ),
                relative_path=_DOCUMENTS_NAME,
            )
            spool.close()
            self._record_spool = None
            sealed = _SealedDocuments(
                documents_ref=documents_ref,
                chunks_ref=chunks_ref,
                document_chunk_counts=tuple(self._document_chunk_counts),
                document_count=self.document_count,
                chunk_count=self._chunk_count,
            )
            self._document_chunk_counts.clear()
            self._segment_hashes.clear()
            self._previous_doc_uid = None
            self._state = "sealed"
            return sealed
        except BaseException:
            self._state = "failed"
            raise

    def abort(self, primary_error: BaseException | None = None) -> None:
        """Close all Windows-sensitive handles, preserving a primary failure."""

        failures: list[BaseException] = []
        spool = self._record_spool
        self._record_spool = None
        if spool is not None:
            try:
                spool.close()
            except BaseException as exc:
                failures.append(exc)
        sink = self._chunk_sink
        self._chunk_sink = None
        if sink is not None:
            try:
                marker = RuntimeError("staged document artifacts aborted")
                sink.__exit__(RuntimeError, marker, None)
            except BaseException as exc:
                failures.append(exc)
        self._document_chunk_counts.clear()
        self._segment_hashes.clear()
        self._previous_doc_uid = None
        self._state = "aborted"
        if not failures:
            return
        if primary_error is not None:
            add_note = getattr(primary_error, "add_note", None)
            if callable(add_note):
                for failure in failures:
                    add_note(
                        "closing staged document artifacts also failed: "
                        f"{failure}"
                    )
            return
        first = failures[0]
        add_note = getattr(first, "add_note", None)
        if callable(add_note):
            for failure in failures[1:]:
                add_note(
                    "another staged document cleanup also failed: "
                    f"{failure}"
                )
        raise first

def _finalize_posting_layer(
    root: Path,
    *,
    sealed_documents: _SealedDocuments,
    postings: Iterable[LayerPosting],
    token_contributions: Iterable[TokenContribution] | None = None,
    layer_kind: Literal["base", "delta"],
    recipe: SearchViewRecipe | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> PostingLayerReceipt:
    """Finish PIV/term artifacts from sealed, bounded document metadata."""

    if not isinstance(sealed_documents, _SealedDocuments):
        raise TypeError("sealed_documents must be a _SealedDocuments value")
    kind = _layer_kind(layer_kind)
    physical_recipe = SearchViewRecipe() if recipe is None else recipe
    if not isinstance(physical_recipe, SearchViewRecipe):
        raise TypeError("recipe must be a SearchViewRecipe")
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("check_cancelled must be callable")
    if kind == "delta" and token_contributions is None:
        raise ValueError("delta layers require token_contributions")
    base_stats_supplied = kind == "base" and token_contributions is not None
    contribution_values: Iterable[TokenContribution] = (
        () if token_contributions is None else token_contributions
    )
    target = Path(root)
    if not target.is_dir():
        raise FileNotFoundError(f"posting layer root does not exist: {target}")
    posting_rows = _Peekable(
        _validated_postings(
            postings,
            sealed_documents.document_chunk_counts,
        )
    )
    contribution_rows = _Peekable(
        _validated_contributions(contribution_values)
    )
    piv_sink = AtomicHashingSink(target / _POSTINGS_NAME)
    terms_sink = AtomicHashingSink(target / _TERMS_NAME)
    anchors: list[list[object]] = []
    term_count = nonbody_total = body_total = 0
    window_start = 0
    window_first: str | None = None
    window_lines = 0
    window_digest = hashlib.sha256()

    with tempfile.SpooledTemporaryFile(max_size=64 * 1024, mode="w+b") as nonbody_spool, tempfile.SpooledTemporaryFile(max_size=64 * 1024, mode="w+b") as body_spool, piv_sink, terms_sink:
        piv_sink.write(POSTINGS_MAGIC)

        def finish_window() -> None:
            nonlocal window_first, window_lines, window_digest
            if window_lines == 0:
                return
            assert window_first is not None
            anchors.append(
                [
                    window_first,
                    window_start,
                    terms_sink.byte_size - window_start,
                    window_digest.hexdigest(),
                    window_lines,
                ]
            )
            window_first = None
            window_lines = 0
            window_digest = hashlib.sha256()

        def emit(record: TermRecord) -> None:
            nonlocal term_count, window_start, window_first, window_lines
            if window_lines == 0:
                window_start = terms_sink.byte_size
                window_first = record.token
            _write_jsonl(terms_sink, record, window_digest)
            window_lines += 1
            term_count += 1
            _u64(term_count, "term_count")
            if window_lines == TERM_INDEX_STRIDE:
                finish_window()

        while posting_rows.value is not None or contribution_rows.value is not None:
            if check_cancelled is not None and term_count % 128 == 0:
                check_cancelled()
            posting = posting_rows.value
            contribution = contribution_rows.value
            posting_token_bytes = (
                _token_bytes(posting.token)
                if isinstance(posting, LayerPosting)
                else None
            )
            contribution_token_bytes = (
                _token_bytes(contribution.token)
                if isinstance(contribution, TokenContribution)
                else None
            )

            if contribution_token_bytes is not None and (
                posting_token_bytes is None
                or contribution_token_bytes < posting_token_bytes
            ):
                contribution = contribution_rows.pop()
                assert isinstance(contribution, TokenContribution)
                if kind == "base":
                    raise LayerCodecError(
                        "base token contribution has no matching postings"
                    )
                emit(
                    TermRecord(
                        token=contribution.token,
                        block_offset=None,
                        block_bytes=0,
                        nonbody_rows=0,
                        body_rows=0,
                        df_any_delta=contribution.df_any_delta,
                        df_nonbody_delta=contribution.df_nonbody_delta,
                        df_body_delta=contribution.df_body_delta,
                        prefix_bytes=0,
                        prefix_sha256=None,
                        body_offset=None,
                        body_bytes=0,
                        body_sha256=None,
                    )
                )
                continue

            if not isinstance(posting, LayerPosting):
                raise AssertionError("posting cursor unexpectedly exhausted")
            token = posting.token
            token_bytes = _token_bytes(token)
            (
                block_offset,
                block_bytes,
                any_rows,
                nonbody_rows,
                body_rows,
                prefix_bytes,
                prefix_sha256,
                body_offset,
                body_bytes,
                body_sha256,
            ) = _write_posting_group(
                posting_rows,
                token,
                token_bytes,
                piv_sink,
                nonbody_spool,
                body_spool,
                check_cancelled,
            )
            contribution = contribution_rows.value
            if (
                isinstance(contribution, TokenContribution)
                and _token_bytes(contribution.token) == token_bytes
            ):
                contribution = contribution_rows.pop()
                assert isinstance(contribution, TokenContribution)
                delta = contribution.triple
            elif kind == "base":
                if base_stats_supplied:
                    raise LayerCodecError(
                        "base postings are missing an exact token contribution"
                    )
                delta = (any_rows, nonbody_rows, body_rows)
            else:
                delta = (0, 0, 0)

            if kind == "base":
                expected = (any_rows, nonbody_rows, body_rows)
                if delta != expected:
                    raise LayerCodecError(
                        "base token contribution does not match posting rows"
                    )
            emit(
                TermRecord(
                    token=token,
                    block_offset=block_offset,
                    block_bytes=block_bytes,
                    nonbody_rows=nonbody_rows,
                    body_rows=body_rows,
                    df_any_delta=delta[0],
                    df_nonbody_delta=delta[1],
                    df_body_delta=delta[2],
                    prefix_bytes=prefix_bytes,
                    prefix_sha256=prefix_sha256,
                    body_offset=body_offset,
                    body_bytes=body_bytes,
                    body_sha256=body_sha256,
                )
            )
            nonbody_total += nonbody_rows
            body_total += body_rows
            _u64(nonbody_total, "nonbody_rows")
            _u64(body_total, "body_rows")
        finish_window()

    postings_ref = _ref_from_sink(
        _POSTINGS_NAME, piv_sink, nonbody_total + body_total
    )
    terms_ref = _ref_from_sink(_TERMS_NAME, terms_sink, term_count)
    sparse_ref = write_canonical_object_with_array(
        target / _SPARSE_NAME,
        fields={
            "artifact_kind": "piv3_sparse_term_index",
            "schema_version": 1,
            "stride": TERM_INDEX_STRIDE,
            "terms_sha256": terms_ref.sha256,
            "terms_bytes": terms_ref.byte_size,
            "line_count": term_count,
        },
        array_key="anchors",
        items=anchors,
        relative_path=_SPARSE_NAME,
    )
    receipt = PostingLayerReceipt(
        root=target,
        layer_kind=kind,
        search_view_recipe_hash=canonical_hash(physical_recipe.as_dict()),
        documents=sealed_documents.documents_ref,
        postings=postings_ref,
        chunks=sealed_documents.chunks_ref,
        terms=terms_ref,
        sparse_index=sparse_ref,
        document_count=sealed_documents.document_count,
        chunk_count=sealed_documents.chunk_count,
        term_count=term_count,
        nonbody_rows=nonbody_total,
        body_rows=body_total,
    )
    return receipt

def _cleanup_incomplete_posting_layer(
    target: Path,
    primary_error: BaseException | None,
) -> None:
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        return
    except OSError as cleanup_error:
        if primary_error is None:
            raise
        add_note = getattr(primary_error, "add_note", None)
        if callable(add_note):
            add_note(
                f"cleaning incomplete posting layer {target} also "
                f"failed: {cleanup_error}"
            )


def write_posting_layer(
    root: Path,
    *,
    documents: Iterable[LayerDocument],
    postings: Iterable[LayerPosting],
    token_contributions: Iterable[TokenContribution] | None = None,
    layer_kind: Literal["base", "delta"],
    recipe: SearchViewRecipe | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> PostingLayerReceipt:
    """Write a sorted layer while retaining at most one document's metrics."""

    kind = _layer_kind(layer_kind)
    physical_recipe = SearchViewRecipe() if recipe is None else recipe
    if not isinstance(physical_recipe, SearchViewRecipe):
        raise TypeError("recipe must be a SearchViewRecipe")
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("check_cancelled must be callable")
    if kind == "delta" and token_contributions is None:
        raise ValueError("delta layers require token_contributions")
    if isinstance(documents, (str, bytes, bytearray)):
        raise TypeError("documents must be an iterable of LayerDocument values")
    try:
        document_iterator = iter(documents)
    except TypeError as exc:
        raise TypeError(
            "documents must be an iterable of LayerDocument values"
        ) from exc

    target = Path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    stage: _StagedDocumentArtifacts | None = None
    complete = False
    primary_error: BaseException | None = None
    try:
        stage = _StagedDocumentArtifacts(
            target,
            check_cancelled=check_cancelled,
        )
        for ordinal, document in enumerate(document_iterator):
            if not isinstance(document, LayerDocument):
                raise TypeError(
                    "documents must contain only LayerDocument values"
                )
            stage.append_document(
                ordinal=ordinal,
                doc_key=document.doc_key,
                doc_uid=document.doc_uid,
                segment_hash=document.segment_hash,
                chunk_count=document.chunk_count,
                chunk_metrics=document.chunk_metrics,
            )
            del document
        del document_iterator
        sealed_documents = stage.seal()
        receipt = _finalize_posting_layer(
            target,
            sealed_documents=sealed_documents,
            postings=postings,
            token_contributions=token_contributions,
            layer_kind=kind,
            recipe=physical_recipe,
            check_cancelled=check_cancelled,
        )
        complete = True
        return receipt
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if not complete:
            if stage is not None:
                stage.abort(primary_error)
            _cleanup_incomplete_posting_layer(target, primary_error)

def _strict_json(raw: bytes | bytearray, description: str) -> object:
    duplicates: list[str] = []

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LayerCodecError(f"invalid {description} JSON") from exc
    if duplicates:
        raise LayerCodecError(f"duplicate key in {description} JSON")
    position = 0
    view = memoryview(raw)
    try:
        for fragment in iter_canonical_json(parsed):
            payload = fragment.encode("utf-8")
            end = position + len(payload)
            if end > len(view) or view[position:end] != payload:
                raise LayerCodecError(f"noncanonical {description} JSON")
            position = end
    except (TypeError, ValueError) as exc:
        raise LayerCodecError(
            f"invalid {description} canonical JSON value"
        ) from exc
    if position != len(view):
        raise LayerCodecError(f"noncanonical {description} JSON")
    return parsed


def _file_identity(stream: BinaryIO) -> tuple[int, int, int, int, int]:
    stat = os.fstat(stream.fileno())
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _open_session_file(
    root: Path,
    reference: ArtifactRef,
    role: str,
) -> tuple[BinaryIO, tuple[int, int, int, int, int]]:
    if root.is_symlink():
        raise LayerCodecError("posting layer root must not be a symlink")
    path = root / reference.relative_path
    if path.is_symlink():
        raise LayerCodecError(f"{role} artifact must not be a symlink")
    try:
        if path.resolve(strict=True).parent != root.resolve(strict=True):
            raise LayerCodecError(f"{role} artifact escapes the posting layer root")
        stream = path.open("rb", buffering=0)
    except OSError as exc:
        raise LayerCodecError(f"cannot open {role} artifact") from exc
    try:
        identity = _file_identity(stream)
        if identity[2] != reference.byte_size:
            raise LayerCodecError(f"{role} artifact byte size does not match receipt")
        return stream, identity
    except BaseException:
        stream.close()
        raise


def _authenticate_handle(
    stream: BinaryIO,
    reference: ArtifactRef,
    role: str,
    observer: Callable[[str, int, int], None] | None = None,
    *,
    capture: bool = False,
) -> bytes | bytearray | None:
    before = _file_identity(stream)
    if before[2] != reference.byte_size:
        raise LayerCodecError(f"{role} artifact byte size does not match receipt")
    stream.seek(0)
    digest = hashlib.sha256()
    captured = bytearray() if capture else None
    offset = 0
    while offset < reference.byte_size:
        size = min(1024 * 1024, reference.byte_size - offset)
        payload = stream.read(size)
        if len(payload) != size:
            raise LayerCodecError(f"{role} artifact changed during authentication")
        digest.update(payload)
        if captured is not None:
            captured.extend(payload)
        if observer is not None:
            observer(role, offset, size)
        offset += size
    after = _file_identity(stream)
    if before != after:
        raise LayerCodecError(f"{role} artifact changed during authentication")
    if digest.hexdigest() != reference.sha256:
        raise LayerCodecError(f"{role} artifact SHA-256 does not match receipt")
    stream.seek(0)
    return captured

class _RangeCursor:
    __slots__ = (
        "_buffer",
        "_buffer_start",
        "_reader",
        "end",
        "name",
        "position",
    )

    def __init__(
        self,
        reader: "PostingLayerReader",
        name: str,
        start: int,
        end: int,
    ) -> None:
        self._reader = reader
        self.name = name
        self.position = start
        self.end = end
        self._buffer = b""
        self._buffer_start = start

    @property
    def remaining(self) -> int:
        return self.end - self.position

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.remaining
        size = min(size, self.remaining)
        if size == 0:
            return b""
        output = bytearray()
        while len(output) < size:
            buffer_end = self._buffer_start + len(self._buffer)
            if not (self._buffer_start <= self.position < buffer_end):
                physical_size = min(64 * 1024, self.end - self.position)
                self._buffer_start = self.position
                self._buffer = self._reader._read_at(
                    self.name, self.position, physical_size
                )
                buffer_end = self._buffer_start + len(self._buffer)
            available = min(size - len(output), buffer_end - self.position)
            start = self.position - self._buffer_start
            output.extend(self._buffer[start : start + available])
            self.position += available
        return bytes(output)

def _cursor_exact(cursor: _RangeCursor, size: int, field: str) -> bytes:
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise LayerCodecError(f"invalid {field} read size")
    if size > cursor.remaining:
        raise LayerCodecError(f"truncated {field}")
    payload = cursor.read(size)
    if len(payload) != size:
        raise LayerCodecError(f"truncated {field}")
    return payload


def _cursor_uvarint(cursor: _RangeCursor, field: str) -> int:
    try:
        return read_uvarint(cursor)
    except (EOFError, TruncatedVarintError, VarintError, OSError) as exc:
        raise LayerCodecError(f"invalid {field} varint: {exc}") from exc


def _cursor_u64(cursor: _RangeCursor, field: str) -> int:
    return _U64.unpack(_cursor_exact(cursor, _U64.size, field))[0]



def _parse_documents(raw: bytes, chunks_size: int) -> tuple[_DocumentRecord, ...]:
    parsed = _strict_json(raw, "layer documents")
    if not isinstance(parsed, Mapping) or set(parsed) != {
        "artifact_kind", "schema_version", "documents"
    }:
        raise LayerCodecError("layer documents object has invalid keys")
    if parsed["artifact_kind"] != "piv3_layer_documents" or type(parsed["schema_version"]) is not int or parsed["schema_version"] != 1:
        raise LayerCodecError("unsupported layer documents schema")
    values = parsed["documents"]
    if not isinstance(values, list):
        raise LayerCodecError("layer documents must contain an array")
    result: list[_DocumentRecord] = []
    previous: bytes | None = None
    expected_offset = len(CHUNKS_MAGIC)
    segments: set[str] = set()
    expected_keys = {
        "doc_key", "doc_uid", "segment_hash", "chunk_count",
        "chunk_block_offset", "chunk_block_bytes", "chunk_block_sha256",
    }
    for value in values:
        if not isinstance(value, Mapping) or set(value) != expected_keys:
            raise LayerCodecError("layer document record has invalid keys")
        try:
            key = validate_doc_key(value["doc_key"])
            uid = validate_sha256(value["doc_uid"], "doc_uid digest")
            segment = validate_sha256(value["segment_hash"], "segment_hash digest")
            count = _u64(value["chunk_count"], "chunk_count")
            offset = _u64(value["chunk_block_offset"], "chunk_block_offset")
            block_bytes = _u64(value["chunk_block_bytes"], "chunk_block_bytes", minimum=2)
            block_sha256 = validate_sha256(
                value["chunk_block_sha256"], "chunk_block_sha256"
            )
        except (TypeError, ValueError) as exc:
            raise LayerCodecError(f"invalid layer document record: {exc}") from exc
        if uid != make_doc_uid(key):
            raise LayerCodecError("layer document doc_uid does not match doc_key")
        encoded = uid.encode("utf-8")
        if previous is not None and encoded <= previous:
            raise LayerCodecError("layer documents are not strictly sorted")
        if segment in segments:
            raise LayerCodecError("duplicate layer document segment_hash")
        if offset != expected_offset:
            raise LayerCodecError("chunk blocks are not contiguous")
        expected_offset += block_bytes
        if expected_offset > chunks_size:
            raise LayerCodecError("chunk block exceeds chunks.pcv")
        result.append(_DocumentRecord(key, uid, segment, count, offset, block_bytes, block_sha256))
        previous = encoded
        segments.add(segment)
    if expected_offset != chunks_size:
        raise LayerCodecError("chunks.pcv has a gap or trailing bytes")
    return tuple(result)


def _parse_sparse(
    raw: bytes,
    receipt: PostingLayerReceipt,
) -> tuple[_SparseWindow, ...]:
    parsed = _strict_json(raw, "sparse term index")
    expected = {
        "artifact_kind", "schema_version", "stride", "terms_sha256",
        "terms_bytes", "line_count", "anchors",
    }
    if not isinstance(parsed, Mapping) or set(parsed) != expected:
        raise LayerCodecError("sparse term index has invalid keys")
    if (
        parsed["artifact_kind"] != "piv3_sparse_term_index"
        or type(parsed["schema_version"]) is not int
        or parsed["schema_version"] != 1
        or type(parsed["stride"]) is not int
        or parsed["stride"] != TERM_INDEX_STRIDE
    ):
        raise LayerCodecError("unsupported sparse term index schema")
    try:
        terms_bytes = _u64(parsed["terms_bytes"], "sparse terms_bytes")
        line_count = _u64(parsed["line_count"], "sparse line_count")
    except (TypeError, ValueError) as exc:
        raise LayerCodecError(f"invalid sparse term index counts: {exc}") from exc
    if (
        parsed["terms_sha256"] != receipt.terms.sha256
        or terms_bytes != receipt.terms.byte_size
        or line_count != receipt.term_count
    ):
        raise LayerCodecError("sparse term index does not bind terms.jsonl receipt")
    values = parsed["anchors"]
    if not isinstance(values, list):
        raise LayerCodecError("sparse term anchors must be an array")
    expected_count = (receipt.term_count + TERM_INDEX_STRIDE - 1) // TERM_INDEX_STRIDE
    if len(values) != expected_count:
        raise LayerCodecError("sparse term anchor count is invalid")
    windows: list[_SparseWindow] = []
    previous_token: bytes | None = None
    expected_offset = 0
    observed_lines = 0
    for index, value in enumerate(values):
        if not isinstance(value, list) or len(value) != 5:
            raise LayerCodecError(
                "sparse term anchor must be [token, offset, bytes, sha256, lines]"
            )
        try:
            token = value[0]
            encoded = _token_bytes(token)
            offset = _u64(value[1], "sparse term offset")
            byte_size = _u64(value[2], "sparse term bytes", minimum=1)
            digest = validate_sha256(value[3], "sparse window sha256")
            lines = _u64(value[4], "sparse window lines", minimum=1)
        except (TypeError, ValueError) as exc:
            raise LayerCodecError(f"invalid sparse term anchor: {exc}") from exc
        expected_lines = (
            TERM_INDEX_STRIDE
            if index + 1 < len(values)
            else receipt.term_count - (index * TERM_INDEX_STRIDE)
        )
        if lines != expected_lines or lines > TERM_INDEX_STRIDE:
            raise LayerCodecError("sparse term window line count is invalid")
        if previous_token is not None and encoded <= previous_token:
            raise LayerCodecError("sparse term anchors are not strictly sorted")
        if offset != expected_offset:
            raise LayerCodecError("sparse term windows have a gap or overlap")
        expected_offset += byte_size
        if expected_offset > receipt.terms.byte_size:
            raise LayerCodecError("sparse term window exceeds terms.jsonl")
        windows.append(_SparseWindow(token, offset, byte_size, digest, lines))
        previous_token = encoded
        observed_lines += lines
    if expected_offset != receipt.terms.byte_size:
        raise LayerCodecError("sparse term windows do not cover terms.jsonl")
    if observed_lines != receipt.term_count:
        raise LayerCodecError("sparse term windows do not cover every term line")
    return tuple(windows)

def _iter_file_lines(
    stream: BinaryIO,
    byte_size: int,
    observer: Callable[[str, int, int], None] | None = None,
    role: str = _TERMS_NAME,
) -> Iterator[tuple[int, bytes]]:
    """Scan canonical JSONL in fixed chunks without RawIOBase byte reads."""

    stream.seek(0)
    loaded = 0
    line_start = 0
    buffered = bytearray()
    while loaded < byte_size or buffered:
        newline = buffered.find(b"\n")
        if newline >= 0:
            raw = bytes(buffered[: newline + 1])
            del buffered[: newline + 1]
            if len(raw) > MAX_JSONL_LINE_BYTES:
                raise LayerCodecError("terms.jsonl line exceeds the safety limit")
            yield line_start, raw
            line_start += len(raw)
            continue
        if loaded >= byte_size:
            if buffered:
                raise LayerCodecError(
                    "terms.jsonl line is truncated or missing final LF"
                )
            return
        if len(buffered) > MAX_JSONL_LINE_BYTES:
            raise LayerCodecError("terms.jsonl line exceeds the safety limit")
        size = min(64 * 1024, byte_size - loaded)
        payload = stream.read(size)
        if len(payload) != size:
            raise LayerCodecError("terms.jsonl changed during streaming scan")
        buffered.extend(payload)
        if observer is not None:
            observer(role, loaded, size)
        loaded += size


def _term_from_line(raw: bytes) -> TermRecord:
    if not raw.endswith(b"\n") or raw.endswith(b"\r\n"):
        raise LayerCodecError("terms.jsonl must use one canonical LF per line")
    parsed = _strict_json(raw[:-1], "term record")
    return TermRecord.from_dict(parsed)


def _scan_terms(
    stream: BinaryIO,
    receipt: PostingLayerReceipt,
    windows: tuple[_SparseWindow, ...],
    observer: Callable[[str, int, int], None] | None = None,
) -> None:
    line_number = 0
    expected_posting_offset = len(POSTINGS_MAGIC)
    previous: bytes | None = None
    nonbody_total = body_total = 0
    window_index = -1
    window_lines = 0
    window_digest = hashlib.sha256()
    for line_start, raw in _iter_file_lines(
        stream, receipt.terms.byte_size, observer, _TERMS_NAME
    ):
        if line_number % TERM_INDEX_STRIDE == 0:
            window_index += 1
            window_lines = 0
            window_digest = hashlib.sha256()
            if (
                window_index >= len(windows)
                or windows[window_index].offset != line_start
            ):
                raise LayerCodecError(
                    "sparse term anchor does not identify its exact line"
                )
        record = _term_from_line(raw)
        encoded = _token_bytes(record.token)
        if previous is not None and encoded <= previous:
            raise LayerCodecError("term records are not strictly sorted")
        window = windows[window_index]
        if window_lines == 0 and record.token != window.first_token:
            raise LayerCodecError("sparse term anchor token is incorrect")
        window_digest.update(raw)
        window_lines += 1
        if record.has_postings:
            if record.block_offset != expected_posting_offset:
                raise LayerCodecError("posting term blocks are not contiguous")
            expected_posting_offset += record.block_bytes
            if expected_posting_offset > receipt.postings.byte_size:
                raise LayerCodecError("posting term block exceeds postings.piv")
        if receipt.layer_kind == "base":
            any_df, nonbody_df, body_df = record.delta
            if min(record.delta) < 0:
                raise LayerCodecError("base term DF contributions must be non-negative")
            if max(nonbody_df, body_df) > any_df or any_df > nonbody_df + body_df:
                raise LayerCodecError("base term DF union is invalid")
            if max(any_df, nonbody_df, body_df) > receipt.chunk_count:
                raise LayerCodecError("base term DF exceeds the layer chunk count")
            if not record.has_postings:
                raise LayerCodecError("base term contribution has no postings")
        nonbody_total += record.nonbody_rows
        body_total += record.body_rows
        _u64(nonbody_total, "nonbody_rows")
        _u64(body_total, "body_rows")
        previous = encoded
        line_number += 1
        if window_lines == window.lines:
            if line_start + len(raw) != window.end:
                raise LayerCodecError("sparse term window byte boundary is invalid")
            if window_digest.hexdigest() != window.sha256:
                raise LayerCodecError("sparse term window SHA-256 is invalid")
    if line_number != receipt.term_count:
        raise LayerCodecError("terms.jsonl line count does not match receipt")
    if window_index + 1 != len(windows):
        raise LayerCodecError("sparse term window count is invalid")
    if expected_posting_offset != receipt.postings.byte_size:
        raise LayerCodecError("postings.piv has a gap or trailing bytes")
    if nonbody_total != receipt.nonbody_rows or body_total != receipt.body_rows:
        raise LayerCodecError("term partition row totals do not match receipt")

class PostingLayerReader:
    """Authenticated immutable-session reader for one posting layer."""

    def __init__(
        self,
        receipt: PostingLayerReceipt,
        *,
        recipe: SearchViewRecipe | None = None,
        read_observer: Callable[[str, int, int], None] | None = None,
    ) -> None:
        if not isinstance(receipt, PostingLayerReceipt):
            raise TypeError("receipt must be a PostingLayerReceipt")
        expected_recipe = SearchViewRecipe() if recipe is None else recipe
        if not isinstance(expected_recipe, SearchViewRecipe):
            raise TypeError("recipe must be a SearchViewRecipe")
        if canonical_hash(expected_recipe.as_dict()) != receipt.search_view_recipe_hash:
            raise ValueError("posting layer SearchViewRecipe does not match receipt")
        if read_observer is not None and not callable(read_observer):
            raise TypeError("read_observer must be callable")
        self.receipt = receipt
        self.read_observer = read_observer
        self._lookup_state = threading.local()
        self.startup_bytes_read = {
            _DOCUMENTS_NAME: 0,
            _POSTINGS_NAME: 0,
            _CHUNKS_NAME: 0,
            _TERMS_NAME: 0,
            _SPARSE_NAME: 0,
        }
        self._lock = threading.RLock()
        self._handles: dict[str, BinaryIO] = {}
        self._identities: dict[str, tuple[int, int, int, int, int]] = {}
        self._magic_verified: set[str] = set()
        self._documents: tuple[_DocumentRecord, ...] = ()
        self._documents_by_uid: dict[str, tuple[int, _DocumentRecord]] = {}
        self._windows: tuple[_SparseWindow, ...] = ()
        self._window_keys: tuple[bytes, ...] = ()
        try:
            for name, reference in (
                (_DOCUMENTS_NAME, receipt.documents),
                (_POSTINGS_NAME, receipt.postings),
                (_CHUNKS_NAME, receipt.chunks),
                (_TERMS_NAME, receipt.terms),
                (_SPARSE_NAME, receipt.sparse_index),
            ):
                handle, identity = _open_session_file(
                    receipt.root, reference, name
                )
                self._handles[name] = handle
                self._identities[name] = identity

            documents_raw = _authenticate_handle(
                self._handles[_DOCUMENTS_NAME],
                receipt.documents,
                _DOCUMENTS_NAME,
                self.read_observer,
                capture=True,
            )
            assert documents_raw is not None
            self.startup_bytes_read[_DOCUMENTS_NAME] = receipt.documents.byte_size
            self._documents = _parse_documents(
                documents_raw, receipt.chunks.byte_size
            )
            del documents_raw
            if len(self._documents) != receipt.document_count:
                raise LayerCodecError("document count does not match receipt")
            if sum(item.chunk_count for item in self._documents) != receipt.chunk_count:
                raise LayerCodecError("chunk count does not match receipt")
            self._documents_by_uid = {
                item.doc_uid: (ordinal, item)
                for ordinal, item in enumerate(self._documents)
            }

            sparse_raw = _authenticate_handle(
                self._handles[_SPARSE_NAME],
                receipt.sparse_index,
                _SPARSE_NAME,
                self.read_observer,
                capture=True,
            )
            assert sparse_raw is not None
            self.startup_bytes_read[_SPARSE_NAME] = receipt.sparse_index.byte_size
            self._windows = _parse_sparse(sparse_raw, receipt)
            del sparse_raw
            self._window_keys = tuple(
                _token_bytes(window.first_token) for window in self._windows
            )
        except BaseException:
            self.close()
            raise
    def __enter__(self) -> "PostingLayerReader":
        self._ensure_open()
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
        handles = getattr(self, "_handles", None)
        if not handles:
            return
        first: OSError | None = None
        for handle in tuple(handles.values()):
            try:
                handle.close()
            except OSError as exc:
                if first is None:
                    first = exc
        handles.clear()
        self._identities.clear()
        self._magic_verified.clear()
        if first is not None:
            raise first

    def _ensure_open(self) -> None:
        if not self._handles:
            raise RuntimeError("posting layer reader is closed")

    def _reference(self, name: str) -> ArtifactRef:
        references = {
            _DOCUMENTS_NAME: self.receipt.documents,
            _POSTINGS_NAME: self.receipt.postings,
            _CHUNKS_NAME: self.receipt.chunks,
            _TERMS_NAME: self.receipt.terms,
            _SPARSE_NAME: self.receipt.sparse_index,
        }
        try:
            return references[name]
        except KeyError as exc:
            raise LayerCodecError("unsupported layer artifact") from exc

    def _ensure_identity(self, name: str) -> None:
        if _file_identity(self._handles[name]) != self._identities[name]:
            raise LayerCodecError(f"{name} changed during immutable session")

    def _read_raw(self, name: str, offset: int, size: int) -> bytes:
        handle = self._handles[name]
        handle.seek(offset)
        payload = handle.read(size)
        if len(payload) != size:
            raise LayerCodecError(f"truncated {name}")
        return payload

    def _read_at(self, name: str, offset: int, size: int) -> bytes:
        self._ensure_open()
        reference = self._reference(name)
        if (
            isinstance(offset, bool)
            or isinstance(size, bool)
            or not isinstance(offset, int)
            or not isinstance(size, int)
            or offset < 0
            or size < 0
            or offset + size > reference.byte_size
        ):
            raise LayerCodecError(f"out-of-range read for {name}")
        with self._lock:
            self._ensure_identity(name)
            payload = self._read_raw(name, offset, size)
            self._ensure_identity(name)
            observer = self.read_observer
            if observer is not None and size:
                observer(name, offset, size)
        return payload

    def _verify_range(
        self,
        name: str,
        offset: int,
        byte_size: int,
        expected_sha256: str,
    ) -> None:
        validate_sha256(expected_sha256, f"{name} local SHA-256")
        reference = self._reference(name)
        if (
            isinstance(offset, bool)
            or isinstance(byte_size, bool)
            or not isinstance(offset, int)
            or not isinstance(byte_size, int)
            or offset < 0
            or byte_size < 0
            or offset + byte_size > reference.byte_size
        ):
            raise LayerCodecError(f"local block is outside {name}")
        digest = hashlib.sha256()
        consumed = 0
        while consumed < byte_size:
            size = min(64 * 1024, byte_size - consumed)
            digest.update(self._read_at(name, offset + consumed, size))
            consumed += size
        if digest.hexdigest() != expected_sha256:
            raise LayerCodecError(f"{name} local block SHA-256 mismatch")

    def _ensure_magic(self, name: str, expected: bytes) -> None:
        if name in self._magic_verified:
            return
        if self._read_at(name, 0, len(expected)) != expected:
            raise LayerCodecError(f"invalid {name} magic")
        self._magic_verified.add(name)
    def _window_lines(self, start: int, end: int) -> Iterator[tuple[int, bytes]]:
        position = start
        buffered = bytearray()
        line_start = start
        while position < end or buffered:
            newline = buffered.find(b"\n")
            if newline >= 0:
                raw = bytes(buffered[: newline + 1])
                del buffered[: newline + 1]
                if len(raw) > MAX_JSONL_LINE_BYTES:
                    raise LayerCodecError("terms.jsonl line exceeds the safety limit")
                yield line_start, raw
                line_start += len(raw)
                continue
            if position >= end:
                if buffered:
                    raise LayerCodecError("sparse term window ends inside a line")
                return
            if len(buffered) > MAX_JSONL_LINE_BYTES:
                raise LayerCodecError("terms.jsonl line exceeds the safety limit")
            size = min(4096, end - position)
            buffered.extend(self._read_at(_TERMS_NAME, position, size))
            position += size

    @property
    def last_sparse_scan_lines(self) -> int:
        return getattr(self._lookup_state, "scanned_lines", 0)

    def lookup_term(self, token: str) -> TermRecord | None:
        target = _token_bytes(token)
        self._lookup_state.scanned_lines = 0
        if not self._windows:
            return None
        index = bisect_right(self._window_keys, target) - 1
        if index < 0:
            return None
        window = self._windows[index]
        digest = hashlib.sha256()
        scanned = 0
        candidate: TermRecord | None = None
        previous: bytes | None = None
        for offset, raw in self._window_lines(window.offset, window.end):
            digest.update(raw)
            scanned += 1
            if scanned > TERM_INDEX_STRIDE:
                raise LayerCodecError("sparse term lookup exceeded its stride")
            record = _term_from_line(raw)
            encoded = _token_bytes(record.token)
            if previous is not None and encoded <= previous:
                raise LayerCodecError("term window is not strictly sorted")
            if scanned == 1 and (
                record.token != window.first_token or offset != window.offset
            ):
                raise LayerCodecError("term window does not match its sparse anchor")
            if encoded == target:
                candidate = record
            previous = encoded
        self._lookup_state.scanned_lines = scanned
        if scanned != window.lines:
            raise LayerCodecError("term window line count does not match sparse index")
        if digest.hexdigest() != window.sha256:
            raise LayerCodecError("term window SHA-256 does not match sparse index")
        return candidate
    def _posting_header(
        self,
        record: TermRecord,
    ) -> tuple[int, int, int, int, int]:
        if record.block_offset is None:
            raise LayerCodecError("posting-free term has no PIV block")
        start = record.block_offset
        prefix_end = start + record.prefix_bytes
        if start < len(POSTINGS_MAGIC) or prefix_end > self.receipt.postings.byte_size:
            raise LayerCodecError("posting term prefix is out of range")
        if record.prefix_sha256 is None:
            raise LayerCodecError("posting term prefix is not authenticated")
        self._ensure_magic(_POSTINGS_NAME, POSTINGS_MAGIC)
        self._verify_range(
            _POSTINGS_NAME, start, record.prefix_bytes, record.prefix_sha256
        )
        if record.prefix_bytes < _U32.size + _U64.size + _U64.size + 1:
            raise LayerCodecError("posting term prefix is too short")
        token_size = _U32.unpack(
            self._read_at(_POSTINGS_NAME, start, _U32.size)
        )[0]
        if token_size == 0 or token_size > MAX_TOKEN_BYTES:
            raise LayerCodecError("invalid posting token length")
        token_offset = start + _U32.size
        nonbody_count_offset = token_offset + token_size
        nonbody_start = nonbody_count_offset + _U64.size
        nonbody_end = prefix_end - _U64.size
        if nonbody_start > nonbody_end:
            raise LayerCodecError("posting token length exceeds its prefix")
        raw_token = self._read_at(_POSTINGS_NAME, token_offset, token_size)
        try:
            token = raw_token.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LayerCodecError("posting token is not valid UTF-8") from exc
        if token != record.token or raw_token != _token_bytes(record.token):
            raise LayerCodecError("posting token does not match term metadata")
        nonbody_count = _U64.unpack(
            self._read_at(_POSTINGS_NAME, nonbody_count_offset, _U64.size)
        )[0]
        if nonbody_count != record.nonbody_rows:
            raise LayerCodecError("nonbody row count does not match term metadata")
        if nonbody_count > (nonbody_end - nonbody_start) // 4:
            raise LayerCodecError("nonbody row count exceeds its prefix")
        body_count = _U64.unpack(
            self._read_at(_POSTINGS_NAME, nonbody_end, _U64.size)
        )[0]
        if body_count != record.body_rows:
            raise LayerCodecError("body row count does not match term metadata")
        if body_count > record.body_bytes // 3:
            raise LayerCodecError("body row count exceeds its attested bytes")
        return nonbody_start, nonbody_end, nonbody_count, body_count, prefix_end
    def _validate_row_ref(self, ordinal: int, local_id: int) -> _DocumentRecord:
        if ordinal >= len(self._documents):
            raise LayerCodecError("posting references an unknown document ordinal")
        document = self._documents[ordinal]
        if local_id >= document.chunk_count:
            raise LayerCodecError("posting local_id is outside the document")
        return document

    def _nonbody_rows(
        self,
        cursor: _RangeCursor,
        count: int,
    ) -> Iterator[tuple[tuple[int, int], int, int]]:
        previous: tuple[int, int] | None = None
        for _ in range(count):
            ordinal = _cursor_uvarint(cursor, "nonbody doc_ordinal")
            local_id = _cursor_uvarint(cursor, "nonbody local_id")
            title_tf = _cursor_uvarint(cursor, "title_tf")
            breadcrumb_tf = _cursor_uvarint(cursor, "breadcrumb_tf")
            key = ordinal, local_id
            if previous is not None and key <= previous:
                raise LayerCodecError("duplicate or non-monotonic nonbody posting")
            if title_tf + breadcrumb_tf == 0:
                raise LayerCodecError("nonbody posting has zero field TF")
            self._validate_row_ref(ordinal, local_id)
            previous = key
            yield key, title_tf, breadcrumb_tf

    def _body_rows(
        self,
        cursor: _RangeCursor,
        count: int,
    ) -> Iterator[tuple[tuple[int, int], int]]:
        previous: tuple[int, int] | None = None
        for _ in range(count):
            ordinal = _cursor_uvarint(cursor, "body doc_ordinal")
            local_id = _cursor_uvarint(cursor, "body local_id")
            body_tf = _cursor_uvarint(cursor, "body_tf")
            key = ordinal, local_id
            if previous is not None and key <= previous:
                raise LayerCodecError("duplicate or non-monotonic body posting")
            if body_tf == 0:
                raise LayerCodecError("body posting has zero TF")
            self._validate_row_ref(ordinal, local_id)
            previous = key
            yield key, body_tf

    def _search_posting(
        self,
        token: str,
        key: tuple[int, int],
        title_tf: int,
        breadcrumb_tf: int,
        body_tf: int,
    ) -> SearchPosting:
        ordinal, local_id = key
        document = self._documents[ordinal]
        return SearchPosting(
            token,
            ChunkRef(document.doc_uid, document.segment_hash, local_id),
            title_tf,
            breadcrumb_tf,
            body_tf,
        )

    def iter_token(
        self,
        token: str,
        include_body: bool = True,
    ) -> Iterator[SearchPosting]:
        if not isinstance(include_body, bool):
            raise TypeError("include_body must be a bool")
        record = self.lookup_term(token)
        if record is None or not record.has_postings:
            return iter(())
        if not include_body:
            return self._iter_nonbody_only(record)
        return self._iter_complete(record)

    def _iter_nonbody_only(self, record: TermRecord) -> Iterator[SearchPosting]:
        (
            nonbody_start,
            nonbody_end,
            nonbody_count,
            _body_count,
            _prefix_end,
        ) = self._posting_header(record)
        cursor = _RangeCursor(
            self, _POSTINGS_NAME, nonbody_start, nonbody_end
        )
        for key, title_tf, breadcrumb_tf in self._nonbody_rows(
            cursor, nonbody_count
        ):
            yield self._search_posting(
                record.token, key, title_tf, breadcrumb_tf, 0
            )
        if cursor.remaining != 0:
            raise LayerCodecError("posting nonbody partition has trailing bytes")
        # Deliberately do not read body-row bytes.  The locally authenticated
        # prefix already binds body_count; the caller pruned the body field.

    def _iter_complete(self, record: TermRecord) -> Iterator[SearchPosting]:
        (
            nonbody_start,
            nonbody_end,
            nonbody_count,
            body_count,
            prefix_end,
        ) = self._posting_header(record)
        body_start = prefix_end
        if body_count:
            if record.body_offset != body_start or record.body_sha256 is None:
                raise LayerCodecError("body partition attestation is inconsistent")
            self._verify_range(
                _POSTINGS_NAME,
                body_start,
                record.body_bytes,
                record.body_sha256,
            )
        elif record.body_bytes != 0:
            raise LayerCodecError("body-free posting has attested body bytes")

        nonbody_cursor = _RangeCursor(
            self, _POSTINGS_NAME, nonbody_start, nonbody_end
        )
        body_cursor = _RangeCursor(
            self, _POSTINGS_NAME, body_start, body_start + record.body_bytes
        )
        nonbody = self._nonbody_rows(nonbody_cursor, nonbody_count)
        body = self._body_rows(body_cursor, body_count)
        left = next(nonbody, None)
        right = next(body, None)
        while left is not None or right is not None:
            if right is None or (left is not None and left[0] < right[0]):
                assert left is not None
                key, title_tf, breadcrumb_tf = left
                yield self._search_posting(
                    record.token, key, title_tf, breadcrumb_tf, 0
                )
                left = next(nonbody, None)
            elif left is None or right[0] < left[0]:
                key, body_tf = right
                yield self._search_posting(record.token, key, 0, 0, body_tf)
                right = next(body, None)
            else:
                key, title_tf, breadcrumb_tf = left
                _, body_tf = right
                yield self._search_posting(
                    record.token, key, title_tf, breadcrumb_tf, body_tf
                )
                left = next(nonbody, None)
                right = next(body, None)
        if nonbody_cursor.remaining != 0 or body_cursor.remaining != 0:
            raise LayerCodecError("posting token block has trailing bytes")
    def get_chunk_metrics(
        self,
        refs: Iterable[ChunkRef],
    ) -> dict[ChunkRef, ChunkMetric]:
        if isinstance(refs, (str, bytes, bytearray)):
            raise TypeError("refs must be an iterable of ChunkRef values")
        requested: list[ChunkRef] = []
        grouped: dict[int, set[int]] = {}
        seen: set[ChunkRef] = set()
        for ref in refs:
            if not isinstance(ref, ChunkRef):
                raise TypeError("refs must contain only ChunkRef values")
            if ref in seen:
                raise ValueError("duplicate ChunkRef request")
            owner = self._documents_by_uid.get(ref.doc_uid)
            if owner is None:
                raise ValueError("ChunkRef document is not owned by this layer")
            ordinal, document = owner
            if ref.segment_hash != document.segment_hash:
                raise ValueError("ChunkRef segment_hash does not match layer owner")
            if ref.local_id >= document.chunk_count:
                raise ValueError("ChunkRef local_id is outside the document")
            requested.append(ref)
            grouped.setdefault(ordinal, set()).add(ref.local_id)
            seen.add(ref)

        observed: dict[tuple[int, int], ChunkMetric] = {}
        if grouped:
            self._ensure_magic(_CHUNKS_NAME, CHUNKS_MAGIC)
        for ordinal in sorted(grouped):
            document = self._documents[ordinal]
            self._verify_range(
                _CHUNKS_NAME,
                document.chunk_block_offset,
                document.chunk_block_bytes,
                document.chunk_block_sha256,
            )
            cursor = _RangeCursor(
                self,
                _CHUNKS_NAME,
                document.chunk_block_offset,
                document.chunk_block_offset + document.chunk_block_bytes,
            )
            encoded_ordinal = _cursor_uvarint(cursor, "chunk document ordinal")
            count = _cursor_uvarint(cursor, "chunk count")
            if encoded_ordinal != ordinal or count != document.chunk_count:
                raise LayerCodecError("chunk block header does not match document table")
            if count > cursor.remaining // 4:
                raise LayerCodecError("chunk count exceeds its document block")
            wanted = grouped[ordinal]
            for expected_local_id in range(count):
                local_id = _cursor_uvarint(cursor, "chunk local_id")
                title = _cursor_uvarint(cursor, "chunk title_length")
                breadcrumb = _cursor_uvarint(cursor, "chunk breadcrumb_length")
                body = _cursor_uvarint(cursor, "chunk body_length")
                if local_id != expected_local_id:
                    raise LayerCodecError("chunk local IDs are not compact and sorted")
                if local_id in wanted:
                    observed[(ordinal, local_id)] = ChunkMetric(
                        local_id, title, breadcrumb, body
                    )
            if cursor.remaining != 0:
                raise LayerCodecError("chunk document block has trailing bytes")

        return {
            ref: observed[(self._documents_by_uid[ref.doc_uid][0], ref.local_id)]
            for ref in requested
        }

    def audit(self) -> None:
        """Explicitly authenticate and decode all artifacts off the hot path."""

        with self._lock:
            for name, reference in (
                (_DOCUMENTS_NAME, self.receipt.documents),
                (_POSTINGS_NAME, self.receipt.postings),
                (_CHUNKS_NAME, self.receipt.chunks),
                (_TERMS_NAME, self.receipt.terms),
                (_SPARSE_NAME, self.receipt.sparse_index),
            ):
                _authenticate_handle(
                    self._handles[name],
                    reference,
                    name,
                    self.read_observer,
                )
                self._ensure_identity(name)
            self._ensure_magic(_POSTINGS_NAME, POSTINGS_MAGIC)
            self._ensure_magic(_CHUNKS_NAME, CHUNKS_MAGIC)
            terms = self._handles[_TERMS_NAME]
            self._ensure_identity(_TERMS_NAME)
            _scan_terms(
                terms,
                self.receipt,
                self._windows,
                self.read_observer,
            )
            self._ensure_identity(_TERMS_NAME)
            for _offset, raw in _iter_file_lines(
                terms,
                self.receipt.terms.byte_size,
                self.read_observer,
                _TERMS_NAME,
            ):
                record = _term_from_line(raw)
                if not record.has_postings:
                    continue
                any_rows = nonbody_rows = body_rows = 0
                for row in self._iter_complete(record):
                    any_rows += 1
                    if row.title_tf or row.breadcrumb_tf:
                        nonbody_rows += 1
                    if row.body_tf:
                        body_rows += 1
                if self.receipt.layer_kind == "base" and record.delta != (
                    any_rows,
                    nonbody_rows,
                    body_rows,
                ):
                    raise LayerCodecError(
                        "base term DF contribution does not match decoded postings"
                    )
            self._ensure_identity(_TERMS_NAME)
            for ordinal, document in enumerate(self._documents):
                refs = (
                    ChunkRef(document.doc_uid, document.segment_hash, local_id)
                    for local_id in range(document.chunk_count)
                )
                metrics = self.get_chunk_metrics(refs)
                if len(metrics) != document.chunk_count:
                    raise LayerCodecError(
                        f"chunk audit failed for ordinal {ordinal}"
                    )

__all__ = [
    "CHUNKS_MAGIC",
    "MAX_JSONL_LINE_BYTES",
    "MAX_TOKEN_BYTES",
    "POSTINGS_MAGIC",
    "TERM_INDEX_STRIDE",
    "LayerCodecError",
    "LayerDocument",
    "PostingLayerReader",
    "PostingLayerReceipt",
    "TermRecord",
    "TokenContribution",
    "write_posting_layer",
]
