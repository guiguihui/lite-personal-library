"""Strict one-Segment-at-a-time projection into raw search facts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.index.v2.canonical import canonical_hash
from app.index.v2.ids import normalize_relative_path
from app.index.v2.models import SegmentRecipe
from app.index.v2.object_store import StoredSegmentRef, load_segment
from app.retrieval.tokenizer import tokenize

from .models import (
    MAX_U64,
    ChunkRef,
    SearchPosting,
    SegmentSummary,
    TokenSummary,
    make_doc_uid,
    validate_doc_key,
    validate_sha256,
)


_FIELD_NAMES = ("title", "breadcrumb", "body")


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise ValueError(f"{name} must be a sequence")
    return value


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _u64(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_U64
    ):
        raise ValueError(f"{name} must be an integer in [0, {MAX_U64}]")
    return value


def _token(value: object) -> str:
    token = _nonempty_string(value, "posting token")
    try:
        token.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("posting token must be valid UTF-8 text") from exc
    return token


def _token_key(value: str) -> bytes:
    return value.encode("utf-8")


def _posting_key(row: SearchPosting) -> tuple[bytes, str, str, int]:
    ref = row.chunk_ref
    return (_token_key(row.token), ref.doc_uid, ref.segment_hash, ref.local_id)


def _validate_ref(ref: StoredSegmentRef) -> str:
    if not isinstance(ref, StoredSegmentRef):
        raise TypeError("ref must be a StoredSegmentRef")
    doc_key = validate_doc_key(ref.doc_key)
    doc_type, slug = doc_key.split(":", 1)
    if ref.doc_type != doc_type or ref.slug != slug:
        raise ValueError(f"Segment ref document attestation mismatch for {doc_key}")
    validate_sha256(ref.segment_hash, "segment_hash digest")
    validate_sha256(ref.content_hash, "content_hash digest")
    validate_sha256(ref.segment_recipe_hash, "segment_recipe_hash digest")
    _u64(ref.byte_size, "ref.byte_size")
    if not isinstance(ref.path, Path):
        raise TypeError("ref.path must be a pathlib.Path")
    return make_doc_uid(doc_key)


def _validate_segment_metadata(
    ref: StoredSegmentRef,
    segment: Mapping[str, object],
) -> None:
    schema = segment.get("schema_version")
    if isinstance(schema, bool) or schema != 2:
        raise ValueError("segment schema_version must be 2")

    document = _mapping(segment.get("document"), "segment.document")
    if (
        document.get("doc_key") != ref.doc_key
        or document.get("type") != ref.doc_type
        or document.get("id") != ref.slug
    ):
        raise ValueError(f"segment document does not match ref for {ref.doc_key}")

    raw_recipe = _mapping(segment.get("segment_recipe"), "segment recipe")
    try:
        recipe = SegmentRecipe(**dict(raw_recipe))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid segment recipe: {exc}") from exc
    if dict(raw_recipe) != recipe.as_dict():
        raise ValueError("segment recipe must contain exactly its canonical fields")

    fingerprint = _mapping(segment.get("fingerprint"), "segment fingerprint")
    if set(fingerprint) != {"content_hash", "recipe_hash", "source_files"}:
        raise ValueError("segment fingerprint must contain exact canonical fields")
    expected_recipe_hash = canonical_hash(recipe.as_dict())
    recipe_hash = validate_sha256(
        fingerprint.get("recipe_hash"),
        "fingerprint recipe_hash digest",
    )
    if recipe_hash != expected_recipe_hash or recipe_hash != ref.segment_recipe_hash:
        raise ValueError("segment recipe fingerprint does not match ref")

    raw_files = _sequence(
        fingerprint.get("source_files"),
        "segment fingerprint source_files",
    )
    records: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for position, raw_record in enumerate(raw_files):
        record = _mapping(
            raw_record,
            f"segment fingerprint source_files[{position}]",
        )
        if set(record) != {"path", "sha256"}:
            raise ValueError("source fingerprint records require exact path/sha256 fields")
        path = _nonempty_string(record.get("path"), "source fingerprint path")
        try:
            normalized = normalize_relative_path(path)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid source fingerprint path: {path!r}") from exc
        if normalized != path or path in seen_paths:
            raise ValueError(f"invalid or duplicate source fingerprint path: {path!r}")
        seen_paths.add(path)
        digest = validate_sha256(
            record.get("sha256"),
            f"source fingerprint sha256 at position {position}",
        )
        records.append({"path": path, "sha256": digest})

    content_hash = validate_sha256(
        fingerprint.get("content_hash"),
        "fingerprint content_hash digest",
    )
    if content_hash != canonical_hash(records) or content_hash != ref.content_hash:
        raise ValueError("segment content fingerprint does not match source files/ref")


def _node_keys(segment: Mapping[str, object]) -> set[str]:
    raw_nodes = _sequence(segment.get("nodes"), "segment.nodes")
    result: set[str] = set()
    for position, raw_node in enumerate(raw_nodes):
        node = _mapping(raw_node, f"segment.nodes[{position}]")
        node_key = _nonempty_string(node.get("node_key"), "node.node_key")
        if node_key in result:
            raise ValueError(f"duplicate node_key in segment: {node_key}")
        result.add(node_key)
    return result


def _node_legacy_ids(segment: Mapping[str, object]) -> dict[str, str]:
    """Return the authenticated stable-node to legacy-node projection."""

    raw_nodes = _sequence(segment.get("nodes"), "segment.nodes")
    result: dict[str, str] = {}
    for position, raw_node in enumerate(raw_nodes):
        node = _mapping(raw_node, f"segment.nodes[{position}]")
        node_key = _nonempty_string(node.get("node_key"), "node.node_key")
        legacy_node_id = _nonempty_string(
            node.get("legacy_node_id") or node.get("node_id"),
            "node.legacy_node_id",
        )
        if node_key in result:
            raise ValueError(f"duplicate node_key in segment: {node_key}")
        result[node_key] = legacy_node_id
    return result


def _field_text_and_lengths(
    chunk: Mapping[str, Any],
    local_id: int,
) -> tuple[str, list[str], str, tuple[int, int, int]]:
    title = chunk.get("title")
    body = chunk.get("body")
    breadcrumb = chunk.get("breadcrumb")
    if not isinstance(title, str):
        raise ValueError(f"chunk {local_id} title must be a string")
    if not isinstance(body, str):
        raise ValueError(f"chunk {local_id} body must be a string")
    if not isinstance(breadcrumb, list) or not all(
        isinstance(part, str) for part in breadcrumb
    ):
        raise ValueError(f"chunk {local_id} breadcrumb must be a list of strings")

    raw_lengths = _mapping(chunk.get("lengths"), f"chunk {local_id} lengths")
    if set(raw_lengths) != set(_FIELD_NAMES):
        raise ValueError(f"chunk {local_id} lengths require exact field names")
    lengths = tuple(
        _u64(raw_lengths[name], f"chunk {local_id} {name} length")
        for name in _FIELD_NAMES
    )
    return title, breadcrumb, body, lengths


def _validated_chunks(
    segment: Mapping[str, object],
    node_keys: set[str],
) -> tuple[dict[int, Mapping[str, Any]], tuple[ChunkMetric, ...]]:
    raw_chunks = _sequence(segment.get("chunks"), "segment.chunks")
    by_id: dict[int, Mapping[str, Any]] = {}
    metrics: list[ChunkMetric] = []
    for position, raw_chunk in enumerate(raw_chunks):
        chunk = _mapping(raw_chunk, f"segment.chunks[{position}]")
        local_id = _u64(chunk.get("local_id"), "chunk.local_id")
        if local_id in by_id:
            raise ValueError(f"duplicate chunk local_id: {local_id}")
        node_key = _nonempty_string(chunk.get("node_key"), "chunk.node_key")
        if node_key not in node_keys:
            raise ValueError(f"chunk {local_id} references unknown node {node_key}")
        _title, _breadcrumb, _body, lengths = _field_text_and_lengths(
            chunk,
            local_id,
        )
        by_id[local_id] = chunk
        metrics.append(ChunkMetric(local_id, *lengths))

    ordered_ids = sorted(by_id)
    if ordered_ids != list(range(len(raw_chunks))):
        raise ValueError("chunk local IDs must be unique and compact from zero")
    metrics.sort(key=lambda item: item.local_id)
    return by_id, tuple(metrics)


def _project_postings(
    raw_value: object,
    ref: StoredSegmentRef,
    doc_uid: str,
    chunk_count: int,
    *,
    materialize_postings: bool,
) -> tuple[
    tuple[SearchPosting, ...] | None,
    tuple[TokenSummary, ...],
    dict[str, Sequence[Any]],
    int,
]:
    if not isinstance(materialize_postings, bool):
        raise TypeError("materialize_postings must be a bool")
    raw_postings = _mapping(raw_value, "segment.postings")
    rows_by_token: dict[str, Sequence[Any]] = {}
    previous_token_key: bytes | None = None
    for raw_token, raw_rows in raw_postings.items():
        token = _token(raw_token)
        token_key = _token_key(token)
        if previous_token_key is not None and token_key <= previous_token_key:
            raise ValueError(
                "posting tokens must be strictly sorted by UTF-8 bytes"
            )
        previous_token_key = token_key
        rows_by_token[token] = _sequence(raw_rows, f"postings[{token!r}]")

    postings: list[SearchPosting] | None = (
        [] if materialize_postings else None
    )
    summaries: list[TokenSummary] = []
    posting_count = 0
    for token, rows in rows_by_token.items():
        if not rows:
            raise ValueError(f"postings[{token!r}] must not be empty")
        previous_local_id: int | None = None
        df_nonbody = 0
        df_body = 0
        for position, raw_row in enumerate(rows):
            row = _sequence(raw_row, f"postings[{token!r}][{position}]")
            if len(row) != 4:
                raise ValueError(
                    f"posting for {token!r} must contain "
                    "[local_id, title_tf, breadcrumb_tf, body_tf]"
                )
            local_id = _u64(row[0], "posting.local_id")
            if local_id >= chunk_count:
                raise ValueError(
                    f"posting for {token!r} references unknown chunk {local_id}"
                )
            if previous_local_id is not None and local_id <= previous_local_id:
                raise ValueError(
                    f"postings for {token!r} must have strictly increasing local IDs"
                )
            previous_local_id = local_id
            title_tf = _u64(row[1], "posting.title_tf")
            breadcrumb_tf = _u64(row[2], "posting.breadcrumb_tf")
            body_tf = _u64(row[3], "posting.body_tf")
            if title_tf + breadcrumb_tf + body_tf == 0:
                raise ValueError(f"posting for {token!r}:{local_id} has zero TF")
            posting_count += 1
            if postings is not None:
                postings.append(
                    SearchPosting(
                        token=token,
                        chunk_ref=ChunkRef(
                            doc_uid,
                            ref.segment_hash,
                            local_id,
                        ),
                        title_tf=title_tf,
                        breadcrumb_tf=breadcrumb_tf,
                        body_tf=body_tf,
                    )
                )
            if title_tf or breadcrumb_tf:
                df_nonbody += 1
            if body_tf:
                df_body += 1
        summaries.append(
            TokenSummary(
                token=token,
                df_any=len(rows),
                df_nonbody=df_nonbody,
                df_body=df_body,
            )
        )
    materialized = None if postings is None else tuple(postings)
    return materialized, tuple(summaries), rows_by_token, posting_count


def _find_posting_row(
    rows: Sequence[Any],
    local_id: int,
) -> Sequence[Any] | None:
    low = 0
    high = len(rows)
    while low < high:
        middle = (low + high) // 2
        row = rows[middle]
        row_local_id = row[0]
        if row_local_id < local_id:
            low = middle + 1
        else:
            high = middle
    if low < len(rows) and rows[low][0] == local_id:
        return rows[low]
    return None


def _validate_chunk_posting_oracle(
    chunks: Mapping[int, Mapping[str, Any]],
    metrics: tuple[ChunkMetric, ...],
    rows_by_token: Mapping[str, Sequence[Any]],
    posting_count: int,
) -> None:
    """Re-tokenize one chunk at a time without cloning the posting table."""

    expected_rows = 0
    for local_id in sorted(chunks):
        chunk = chunks[local_id]
        title, breadcrumb, body, declared_lengths = _field_text_and_lengths(
            chunk,
            local_id,
        )
        title_tf = Counter(tokenize(title))
        breadcrumb_tf = Counter(tokenize(" ".join(breadcrumb)))
        body_tf = Counter(tokenize(body))
        actual_lengths = (
            sum(title_tf.values()),
            sum(breadcrumb_tf.values()),
            sum(body_tf.values()),
        )
        metric = metrics[local_id]
        metric_lengths = (
            metric.title_length,
            metric.breadcrumb_length,
            metric.body_length,
        )
        if declared_lengths != actual_lengths or metric_lengths != actual_lengths:
            raise ValueError(
                f"chunk {local_id} field length mismatch: "
                f"expected {actual_lengths}, got {declared_lengths}"
            )

        tokens = set(title_tf) | set(breadcrumb_tf) | set(body_tf)
        expected_rows += len(tokens)
        for token in sorted(tokens, key=_token_key):
            rows = rows_by_token.get(token)
            row = None if rows is None else _find_posting_row(rows, local_id)
            expected = (
                local_id,
                int(title_tf.get(token, 0)),
                int(breadcrumb_tf.get(token, 0)),
                int(body_tf.get(token, 0)),
            )
            if row is None or tuple(row) != expected:
                raise ValueError(
                    "segment postings do not match tokenized chunk fields"
                )
    if expected_rows != posting_count:
        raise ValueError("segment postings do not match tokenized chunk fields")


@dataclass(frozen=True, slots=True, order=True)
class ChunkMetric:
    """The candidate-seekable length facts for one Segment-local chunk."""

    local_id: int
    title_length: int
    breadcrumb_length: int
    body_length: int

    def __post_init__(self) -> None:
        _u64(self.local_id, "local_id")
        _u64(self.title_length, "title_length")
        _u64(self.breadcrumb_length, "breadcrumb_length")
        _u64(self.body_length, "body_length")

    def as_dict(self) -> dict[str, int]:
        return {
            "local_id": self.local_id,
            "title_length": self.title_length,
            "breadcrumb_length": self.breadcrumb_length,
            "body_length": self.body_length,
        }


@dataclass(frozen=True, slots=True)
class SegmentProjection:
    """Detached raw search facts; no decoded Segment containers escape."""

    ref: StoredSegmentRef
    summary: SegmentSummary
    postings: tuple[SearchPosting, ...]
    chunk_metrics: tuple[ChunkMetric, ...]

    def __post_init__(self) -> None:
        doc_uid = _validate_ref(self.ref)
        if not isinstance(self.summary, SegmentSummary):
            raise TypeError("summary must be a SegmentSummary")
        expected_identity = {
            "segment_hash": self.ref.segment_hash,
            "doc_key": self.ref.doc_key,
            "doc_uid": doc_uid,
            "content_hash": self.ref.content_hash,
            "segment_recipe_hash": self.ref.segment_recipe_hash,
        }
        for field, expected in expected_identity.items():
            if getattr(self.summary, field) != expected:
                raise ValueError(f"summary {field} does not match Segment ref")

        if isinstance(self.chunk_metrics, (str, bytes, bytearray)):
            raise TypeError("chunk_metrics must be an iterable of ChunkMetric values")
        try:
            metrics = tuple(self.chunk_metrics)
        except TypeError as exc:
            raise TypeError(
                "chunk_metrics must be an iterable of ChunkMetric values"
            ) from exc
        if not all(isinstance(metric, ChunkMetric) for metric in metrics):
            raise TypeError("chunk_metrics must contain only ChunkMetric values")
        if tuple(metric.local_id for metric in metrics) != tuple(range(len(metrics))):
            raise ValueError("chunk metrics must have compact sorted local IDs")
        if len(metrics) != self.summary.chunk_count:
            raise ValueError("chunk metric count does not match summary chunk_count")
        if sum(metric.title_length for metric in metrics) != self.summary.title_length_sum:
            raise ValueError("chunk title lengths do not match summary")
        if (
            sum(metric.breadcrumb_length for metric in metrics)
            != self.summary.breadcrumb_length_sum
        ):
            raise ValueError("chunk breadcrumb lengths do not match summary")
        if sum(metric.body_length for metric in metrics) != self.summary.body_length_sum:
            raise ValueError("chunk body lengths do not match summary")

        if isinstance(self.postings, (str, bytes, bytearray)):
            raise TypeError("postings must be an iterable of SearchPosting values")
        try:
            postings = tuple(self.postings)
        except TypeError as exc:
            raise TypeError("postings must be an iterable of SearchPosting values") from exc
        if not all(isinstance(row, SearchPosting) for row in postings):
            raise TypeError("postings must contain only SearchPosting values")
        for row in postings:
            chunk_ref = row.chunk_ref
            if chunk_ref.doc_uid != doc_uid:
                raise ValueError("posting doc_uid does not match Segment ref")
            if chunk_ref.segment_hash != self.ref.segment_hash:
                raise ValueError("posting segment_hash does not match Segment ref")
            if chunk_ref.local_id >= len(metrics):
                raise ValueError("posting local_id is outside Segment chunk metrics")
        previous_key: tuple[bytes, str, str, int] | None = None
        for row in postings:
            key = _posting_key(row)
            if previous_key is not None and key <= previous_key:
                raise ValueError("postings must be strictly sorted and unique")
            previous_key = key

        observed: list[TokenSummary] = []
        position = 0
        while position < len(postings):
            token = postings[position].token
            any_count = nonbody_count = body_count = 0
            while position < len(postings) and postings[position].token == token:
                row = postings[position]
                any_count += 1
                if row.title_tf or row.breadcrumb_tf:
                    nonbody_count += 1
                if row.body_tf:
                    body_count += 1
                position += 1
            observed.append(
                TokenSummary(token, any_count, nonbody_count, body_count)
            )
        if tuple(observed) != self.summary.tokens:
            raise ValueError("summary token statistics do not match postings")
        if len(postings) != self.summary.posting_count:
            raise ValueError("summary posting_count does not match postings")

        object.__setattr__(self, "postings", postings)
        object.__setattr__(self, "chunk_metrics", metrics)


class SegmentProjector:
    """Load, validate, project, and release one immutable Segment per call."""

    __slots__ = ("pageindex_dir",)

    def __init__(self, pageindex_dir: Path) -> None:
        self.pageindex_dir = Path(pageindex_dir)

    def project(self, ref: StoredSegmentRef) -> SegmentProjection:
        doc_uid = _validate_ref(ref)
        segment = load_segment(self.pageindex_dir, ref)
        return self._project_mapping(ref, doc_uid, segment)

    def _analyze_mapping(
        self,
        ref: StoredSegmentRef,
        doc_uid: str,
        segment: Mapping[str, object],
        *,
        materialize_postings: bool,
    ) -> tuple[
        SegmentSummary,
        tuple[ChunkMetric, ...],
        tuple[SearchPosting, ...] | None,
        dict[str, Sequence[Any]],
    ]:
        if not isinstance(segment, Mapping):
            raise TypeError("loaded Segment must be a mapping")
        _validate_segment_metadata(ref, segment)
        node_keys = _node_keys(segment)
        chunks, metrics = _validated_chunks(segment, node_keys)
        (
            postings,
            token_summaries,
            rows_by_token,
            posting_count,
        ) = _project_postings(
            segment.get("postings"),
            ref,
            doc_uid,
            len(metrics),
            materialize_postings=materialize_postings,
        )
        _validate_chunk_posting_oracle(
            chunks,
            metrics,
            rows_by_token,
            posting_count,
        )
        summary = SegmentSummary(
            segment_hash=ref.segment_hash,
            doc_key=ref.doc_key,
            doc_uid=doc_uid,
            content_hash=ref.content_hash,
            segment_recipe_hash=ref.segment_recipe_hash,
            chunk_count=len(metrics),
            title_length_sum=sum(metric.title_length for metric in metrics),
            breadcrumb_length_sum=sum(
                metric.breadcrumb_length for metric in metrics
            ),
            body_length_sum=sum(metric.body_length for metric in metrics),
            posting_count=posting_count,
            tokens=token_summaries,
        )
        return summary, metrics, postings, rows_by_token

    def _project_mapping(
        self,
        ref: StoredSegmentRef,
        doc_uid: str,
        segment: Mapping[str, object],
    ) -> SegmentProjection:
        summary, metrics, postings, _rows = self._analyze_mapping(
            ref,
            doc_uid,
            segment,
            materialize_postings=True,
        )
        if postings is None:
            raise AssertionError("project() must materialize postings")
        return SegmentProjection(ref, summary, postings, metrics)

    def summarize(self, ref: StoredSegmentRef) -> SegmentSummary:
        doc_uid = _validate_ref(ref)
        segment = load_segment(self.pageindex_dir, ref)
        summary, _metrics, postings, _rows = self._analyze_mapping(
            ref,
            doc_uid,
            segment,
            materialize_postings=False,
        )
        if postings is not None:
            raise AssertionError("summarize() must not materialize postings")
        return summary

    def project_to_sink(
        self,
        ref: StoredSegmentRef,
        consume_posting: Callable[[SearchPosting], object],
    ) -> tuple[SegmentSummary, tuple[ChunkMetric, ...]]:
        """Validate once and feed postings without retaining a posting tuple."""

        if not callable(consume_posting):
            raise TypeError("consume_posting must be callable")
        doc_uid = _validate_ref(ref)
        segment = load_segment(self.pageindex_dir, ref)
        summary, metrics, postings, rows_by_token = self._analyze_mapping(
            ref,
            doc_uid,
            segment,
            materialize_postings=False,
        )
        if postings is not None:
            raise AssertionError("project_to_sink() must stream postings")
        del segment, postings
        for token, rows in rows_by_token.items():
            for row in rows:
                consume_posting(
                    SearchPosting(
                        token=token,
                        chunk_ref=ChunkRef(
                            doc_uid,
                            ref.segment_hash,
                            row[0],
                        ),
                        title_tf=row[1],
                        breadcrumb_tf=row[2],
                        body_tf=row[3],
                    )
                )
        return summary, metrics
    def iter_postings(self, ref: StoredSegmentRef) -> Iterator[SearchPosting]:
        doc_uid = _validate_ref(ref)
        return self._iter_postings(ref, doc_uid)

    def _iter_postings(
        self,
        ref: StoredSegmentRef,
        doc_uid: str,
    ) -> Iterator[SearchPosting]:
        segment = load_segment(self.pageindex_dir, ref)
        summary, metrics, postings, rows_by_token = self._analyze_mapping(
            ref,
            doc_uid,
            segment,
            materialize_postings=False,
        )
        if postings is not None:
            raise AssertionError("iter_postings() must stream postings")
        # Drop text/chunk containers before exposing the first row. Only the
        # already-validated raw posting lists remain live during iteration.
        del segment, summary, metrics, postings
        for token, rows in rows_by_token.items():
            for row in rows:
                yield SearchPosting(
                    token=token,
                    chunk_ref=ChunkRef(
                        doc_uid,
                        ref.segment_hash,
                        row[0],
                    ),
                    title_tf=row[1],
                    breadcrumb_tf=row[2],
                    body_tf=row[3],
                )
    def load_chunks(
        self,
        ref: StoredSegmentRef,
        local_ids: Iterable[int],
    ) -> dict[int, dict[str, object]]:
        _validate_ref(ref)
        if isinstance(local_ids, (str, bytes, bytearray)):
            raise TypeError("local_ids must be an iterable of integers")
        try:
            requested = tuple(
                _u64(value, "requested local_id") for value in local_ids
            )
        except TypeError as exc:
            raise TypeError("local_ids must be an iterable of integers") from exc
        if len(requested) != len(set(requested)):
            raise ValueError("requested local_ids must be unique")
        if not requested:
            return {}

        segment = load_segment(self.pageindex_dir, ref)
        if not isinstance(segment, Mapping):
            raise TypeError("loaded Segment must be a mapping")
        _validate_segment_metadata(ref, segment)
        node_keys = _node_keys(segment)
        legacy_ids = _node_legacy_ids(segment)
        if set(legacy_ids) != node_keys:
            raise ValueError("Segment node identity projections differ")
        chunks, _metrics = _validated_chunks(segment, node_keys)
        missing = sorted(set(requested) - set(chunks))
        if missing:
            raise KeyError(f"unknown Segment local IDs: {missing}")
        result: dict[int, dict[str, object]] = {}
        for local_id in sorted(requested):
            chunk = copy.deepcopy(dict(chunks[local_id]))
            node_key = _nonempty_string(
                chunk.get("node_key"),
                f"chunk {local_id} node_key",
            )
            legacy_node_id = legacy_ids[node_key]
            raw_legacy_node_id = chunk.get("legacy_node_id")
            if (
                raw_legacy_node_id is not None
                and raw_legacy_node_id != legacy_node_id
            ):
                raise ValueError(
                    f"chunk {local_id} legacy_node_id differs from its node"
                )
            chunk["legacy_node_id"] = legacy_node_id
            result[local_id] = chunk
        return result


__all__ = [
    "ChunkMetric",
    "SegmentProjection",
    "SegmentProjector",
]
