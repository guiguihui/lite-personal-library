"""Canonical, immutable sidecars for per-Segment search summaries."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from app.index.v2.canonical import iter_canonical_json
from app.index.v2.object_store import StoredSegmentRef

from .models import (
    MAX_U64,
    SegmentSummary,
    TokenSummary,
    make_doc_uid,
    validate_doc_key,
    validate_sha256,
)


_TOP_LEVEL_KEYS = {
    "artifact_kind",
    "schema_version",
    "segment_hash",
    "doc_key",
    "doc_uid",
    "content_hash",
    "segment_recipe_hash",
    "chunk_count",
    "field_length_sums",
    "posting_count",
    "tokens",
}
_FIELD_LENGTH_KEYS = {"title", "breadcrumb", "body"}
_TOKEN_KEYS = {"token", "df_any", "df_nonbody", "df_body"}


class SummaryStoreError(ValueError):
    """A summary sidecar is malformed, corrupt, or ambiguously published."""


@dataclass(frozen=True, slots=True)
class StoredSummaryRef:
    """Trusted digest/size attestation for one derived Segment summary."""

    segment_hash: str
    summary_sha256: str
    byte_size: int
    doc_key: str
    doc_uid: str
    content_hash: str
    segment_recipe_hash: str
    schema_version: int = 1
    artifact_kind: str = "segment_search_summary_ref"

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("StoredSummaryRef schema_version must be 1")
        if self.artifact_kind != "segment_search_summary_ref":
            raise ValueError(
                "StoredSummaryRef artifact_kind must be "
                "'segment_search_summary_ref'"
            )
        validate_sha256(self.segment_hash, "segment_hash digest")
        validate_sha256(self.summary_sha256, "summary_sha256 digest")
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 0
            or self.byte_size > MAX_U64
        ):
            raise ValueError(
                f"summary byte_size must be an integer in [0, {MAX_U64}]"
            )
        doc_key = validate_doc_key(self.doc_key)
        validate_sha256(self.doc_uid, "doc_uid digest")
        if self.doc_uid != make_doc_uid(doc_key):
            raise ValueError("summary ref doc_uid does not match doc_key")
        validate_sha256(self.content_hash, "content_hash digest")
        validate_sha256(
            self.segment_recipe_hash,
            "segment_recipe_hash digest",
        )

    @property
    def sha256(self) -> str:
        return self.summary_sha256

    @property
    def relative_path(self) -> str:
        digest = self.segment_hash
        return (
            f"objects/search/summaries/{digest[:2]}/{digest}.json"
        )

    def path_for(self, pageindex_dir: Path) -> Path:
        return Path(pageindex_dir) / Path(self.relative_path)

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "schema_version": self.schema_version,
            "segment_hash": self.segment_hash,
            "summary_sha256": self.summary_sha256,
            "byte_size": self.byte_size,
            "doc_key": self.doc_key,
            "doc_uid": self.doc_uid,
            "content_hash": self.content_hash,
            "segment_recipe_hash": self.segment_recipe_hash,
        }


def _summary_path(pageindex_dir: Path, segment_hash: object) -> Path:
    digest = validate_sha256(segment_hash, "segment_hash digest")
    return (
        Path(pageindex_dir)
        / "objects"
        / "search"
        / "summaries"
        / digest[:2]
        / f"{digest}.json"
    )


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SummaryStoreError(f"{name} must be a mapping")
    return value


def _require_sequence(value: object, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise SummaryStoreError(f"{name} must be a sequence")
    return value


def _strict_keys(
    value: Mapping[str, Any],
    expected: set[str],
    name: str,
) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise SummaryStoreError(
            f"{name} keys differ: missing={missing}, extra={extra}"
        )


def _summary_from_payload(payload: object) -> SegmentSummary:
    root = _require_mapping(payload, "summary payload")
    _strict_keys(root, _TOP_LEVEL_KEYS, "summary payload")
    lengths = _require_mapping(root["field_length_sums"], "field_length_sums")
    _strict_keys(lengths, _FIELD_LENGTH_KEYS, "field_length_sums")
    raw_tokens = _require_sequence(root["tokens"], "summary tokens")
    tokens: list[TokenSummary] = []
    for position, raw_token in enumerate(raw_tokens):
        token = _require_mapping(raw_token, f"summary tokens[{position}]")
        _strict_keys(token, _TOKEN_KEYS, f"summary tokens[{position}]")
        try:
            tokens.append(
                TokenSummary(
                    token=token["token"],
                    df_any=token["df_any"],
                    df_nonbody=token["df_nonbody"],
                    df_body=token["df_body"],
                )
            )
        except (TypeError, ValueError) as exc:
            raise SummaryStoreError(
                f"invalid summary token at position {position}: {exc}"
            ) from exc
    try:
        return SegmentSummary(
            artifact_kind=root["artifact_kind"],
            schema_version=root["schema_version"],
            segment_hash=root["segment_hash"],
            doc_key=root["doc_key"],
            doc_uid=root["doc_uid"],
            content_hash=root["content_hash"],
            segment_recipe_hash=root["segment_recipe_hash"],
            chunk_count=root["chunk_count"],
            title_length_sum=lengths["title"],
            breadcrumb_length_sum=lengths["breadcrumb"],
            body_length_sum=lengths["body"],
            posting_count=root["posting_count"],
            tokens=tuple(tokens),
        )
    except (TypeError, ValueError) as exc:
        raise SummaryStoreError(f"invalid summary payload: {exc}") from exc


def _validate_ref(ref: StoredSegmentRef) -> None:
    if not isinstance(ref, StoredSegmentRef):
        raise TypeError("ref must be a StoredSegmentRef")
    doc_key = validate_doc_key(ref.doc_key)
    doc_type, slug = doc_key.split(":", 1)
    if ref.doc_type != doc_type or ref.slug != slug:
        raise ValueError(f"Segment ref document attestation mismatch for {doc_key}")
    validate_sha256(ref.segment_hash, "segment_hash digest")
    validate_sha256(ref.content_hash, "content_hash digest")
    validate_sha256(ref.segment_recipe_hash, "segment_recipe_hash digest")
    if (
        isinstance(ref.byte_size, bool)
        or not isinstance(ref.byte_size, int)
        or ref.byte_size < 0
        or ref.byte_size > MAX_U64
    ):
        raise ValueError(f"ref.byte_size must be an integer in [0, {MAX_U64}]")
    if not isinstance(ref.path, Path):
        raise TypeError("ref.path must be a pathlib.Path")


def _bind_summary(summary: SegmentSummary, ref: StoredSegmentRef) -> None:
    expected = {
        "segment_hash": ref.segment_hash,
        "doc_key": ref.doc_key,
        "doc_uid": make_doc_uid(ref.doc_key),
        "content_hash": ref.content_hash,
        "segment_recipe_hash": ref.segment_recipe_hash,
    }
    for field, value in expected.items():
        if getattr(summary, field) != value:
            raise SummaryStoreError(f"summary {field} does not match Segment ref")


def _new_summary_ref(
    summary: SegmentSummary,
    summary_sha256: str,
    byte_size: int,
) -> StoredSummaryRef:
    return StoredSummaryRef(
        segment_hash=summary.segment_hash,
        summary_sha256=summary_sha256,
        byte_size=byte_size,
        doc_key=summary.doc_key,
        doc_uid=summary.doc_uid,
        content_hash=summary.content_hash,
        segment_recipe_hash=summary.segment_recipe_hash,
    )


def _bind_summary_ref(
    summary_ref: StoredSummaryRef,
    ref: StoredSegmentRef,
) -> None:
    if not isinstance(summary_ref, StoredSummaryRef):
        raise TypeError("summary_ref must be a StoredSummaryRef")
    expected = {
        "segment_hash": ref.segment_hash,
        "doc_key": ref.doc_key,
        "doc_uid": make_doc_uid(ref.doc_key),
        "content_hash": ref.content_hash,
        "segment_recipe_hash": ref.segment_recipe_hash,
    }
    for field, value in expected.items():
        if getattr(summary_ref, field) != value:
            raise SummaryStoreError(
                f"summary_ref {field} does not match Segment ref"
            )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as stream:
        while True:
            payload = stream.read(1024 * 1024)
            if not payload:
                break
            digest.update(payload)
            byte_size += len(payload)
    return digest.hexdigest(), byte_size


def _encoded_file_matches(path: Path, fragments: Iterator[str]) -> bool:
    with path.open("rb") as stream:
        for fragment in fragments:
            encoded = fragment.encode("utf-8")
            if stream.read(len(encoded)) != encoded:
                return False
        return stream.read(1) == b""


def _canonical_file_matches(path: Path, payload: object) -> bool:
    """Compare canonical encoding without allocating the complete byte string."""

    return _encoded_file_matches(path, iter_canonical_json(payload))


def _iter_summary_json(summary: SegmentSummary) -> Iterator[str]:
    """Encode a summary incrementally without copying its complete token table."""

    scalar_fields: tuple[tuple[str, object], ...] = (
        ("artifact_kind", summary.artifact_kind),
        ("chunk_count", summary.chunk_count),
        ("content_hash", summary.content_hash),
        ("doc_key", summary.doc_key),
        ("doc_uid", summary.doc_uid),
        (
            "field_length_sums",
            {
                "title": summary.title_length_sum,
                "breadcrumb": summary.breadcrumb_length_sum,
                "body": summary.body_length_sum,
            },
        ),
        ("posting_count", summary.posting_count),
        ("schema_version", summary.schema_version),
        ("segment_hash", summary.segment_hash),
        ("segment_recipe_hash", summary.segment_recipe_hash),
    )
    yield "{"
    for position, (name, value) in enumerate(scalar_fields):
        if position:
            yield ","
        yield from iter_canonical_json(name)
        yield ":"
        yield from iter_canonical_json(value)
    yield ',"tokens":['
    for position, token in enumerate(summary.tokens):
        if position:
            yield ","
        yield from iter_canonical_json(token.as_dict())
    yield "]}"


def _summary_encoding_receipt(summary: SegmentSummary) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    for fragment in _iter_summary_json(summary):
        encoded = fragment.encode("utf-8")
        digest.update(encoded)
        byte_size += len(encoded)
    return digest.hexdigest(), byte_size

def _summary_file_matches(path: Path, summary: SegmentSummary) -> bool:
    return _encoded_file_matches(path, _iter_summary_json(summary))


def _reject_symlink_components(root: Path, destination: Path) -> None:
    current = root
    if current.is_symlink():
        raise SummaryStoreError(f"summary root must not be a symlink: {current}")
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise SummaryStoreError("summary destination escapes PageIndex root") from exc
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SummaryStoreError(
                f"summary path component must not be a symlink: {current}"
            )


def _ensure_parent(pageindex_dir: Path, destination: Path) -> None:
    root = Path(pageindex_dir)
    if root.is_symlink():
        raise SummaryStoreError(f"summary root must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise SummaryStoreError(f"summary root is not a directory: {root}")

    current = root
    relative_parent = destination.parent.relative_to(root)
    for part in relative_parent.parts:
        current = current / part
        if current.is_symlink():
            raise SummaryStoreError(
                f"summary parent must not be a symlink: {current}"
            )
        try:
            current.mkdir()
        except FileExistsError:
            if current.is_symlink():
                raise SummaryStoreError(
                    f"summary parent must not be a symlink: {current}"
                )
            if not current.is_dir():
                raise SummaryStoreError(
                    f"summary parent is not a directory: {current}"
                )
    _reject_symlink_components(root, destination.parent)
    try:
        destination.parent.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise SummaryStoreError("summary parent escapes PageIndex root") from exc


def _summary_file_receipt(
    path: Path,
    summary: SegmentSummary,
) -> tuple[bool, str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as stream:
        for fragment in _iter_summary_json(summary):
            expected = fragment.encode("utf-8")
            actual = stream.read(len(expected))
            digest.update(actual)
            byte_size += len(actual)
            if actual != expected:
                return False, digest.hexdigest(), byte_size
        if stream.read(1):
            return False, digest.hexdigest(), byte_size
    return True, digest.hexdigest(), byte_size


def _verify_existing(
    destination: Path,
    summary: SegmentSummary,
) -> tuple[str, int]:
    if destination.is_symlink():
        raise SummaryStoreError(
            f"summary destination must not be a symlink: {destination}"
        )
    if not destination.is_file():
        raise SummaryStoreError(
            f"summary destination is not a regular file: {destination}"
        )
    try:
        matches, sha256, byte_size = _summary_file_receipt(
            destination,
            summary,
        )
    except OSError as exc:
        raise SummaryStoreError(
            f"cannot verify existing summary: {destination}"
        ) from exc
    if not matches:
        raise SummaryStoreError(
            f"existing summary differs or is corrupt: {destination}"
        )
    return sha256, byte_size


def _write_temporary(
    parent: Path,
    summary: SegmentSummary,
    name: str,
) -> tuple[Path, str, int]:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            for fragment in _iter_summary_json(summary):
                encoded = fragment.encode("utf-8")
                written = stream.write(encoded)
                if written != len(encoded):
                    raise OSError(
                        f"short summary write: expected {len(encoded)}, got {written}"
                    )
                digest.update(encoded)
                byte_size += written
            stream.flush()
            os.fsync(stream.fileno())
        return temporary, digest.hexdigest(), byte_size
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


def _publish_no_replace(temporary: Path, destination: Path) -> bool:
    """Install without clobber; use Windows rename if hard links are unavailable."""

    try:
        os.link(temporary, destination)
    except FileExistsError:
        return False
    except OSError as link_error:
        if os.name != "nt":
            raise
        try:
            # On Windows os.rename is atomic and fails if destination exists.
            os.rename(temporary, destination)
        except OSError as rename_error:
            if destination.exists() or destination.is_symlink():
                return False
            raise rename_error from link_error
    return True


def put_summary(
    pageindex_dir: Path,
    summary: SegmentSummary,
) -> StoredSummaryRef:
    """Publish once and return the receipt a trusted manifest must retain."""

    if not isinstance(summary, SegmentSummary):
        raise TypeError("summary must be a SegmentSummary")

    destination = _summary_path(Path(pageindex_dir), summary.segment_hash)
    _ensure_parent(Path(pageindex_dir), destination)
    if destination.exists() or destination.is_symlink():
        sha256, byte_size = _verify_existing(destination, summary)
        return _new_summary_ref(summary, sha256, byte_size)

    temporary, expected_sha256, expected_size = _write_temporary(
        destination.parent,
        summary,
        destination.name,
    )
    try:
        _publish_no_replace(temporary, destination)
        actual_sha256, actual_size = _verify_existing(destination, summary)
        if (
            actual_sha256 != expected_sha256
            or actual_size != expected_size
        ):
            raise SummaryStoreError(
                f"published summary receipt mismatch: {destination}"
            )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return _new_summary_ref(summary, actual_sha256, actual_size)


def load_summary(
    pageindex_dir: Path,
    ref: StoredSegmentRef,
    summary_ref: StoredSummaryRef,
) -> SegmentSummary:
    """Load a canonical sidecar only under a trusted SHA-256/size receipt."""

    _validate_ref(ref)
    _bind_summary_ref(summary_ref, ref)
    root = Path(pageindex_dir)
    destination = _summary_path(root, ref.segment_hash)
    if not destination.exists() and not destination.is_symlink():
        raise FileNotFoundError(f"summary object not found: {ref.segment_hash}")
    _reject_symlink_components(root, destination)
    if not destination.is_file():
        raise SummaryStoreError(
            f"summary destination is not a regular file: {destination}"
        )
    try:
        destination.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise SummaryStoreError("summary destination escapes PageIndex root") from exc

    try:
        actual_sha256, actual_size = _hash_file(destination)
    except OSError as exc:
        raise SummaryStoreError(
            f"cannot hash summary object: {destination}"
        ) from exc
    if (
        actual_sha256 != summary_ref.summary_sha256
        or actual_size != summary_ref.byte_size
    ):
        raise SummaryStoreError(
            f"summary receipt hash/size mismatch: {destination}"
        )

    try:
        with destination.open("r", encoding="utf-8", newline="") as stream:
            payload = json.load(stream)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SummaryStoreError(f"invalid summary JSON: {destination}") from exc
    try:
        canonical = _canonical_file_matches(destination, payload)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SummaryStoreError(f"invalid summary payload: {destination}") from exc
    if not canonical:
        raise SummaryStoreError(f"summary JSON is not canonical: {destination}")
    summary = _summary_from_payload(payload)
    semantic_sha256, semantic_size = _summary_encoding_receipt(summary)
    if (
        semantic_sha256 != summary_ref.summary_sha256
        or semantic_size != summary_ref.byte_size
    ):
        raise SummaryStoreError(
            f"summary semantic receipt mismatch: {destination}"
        )
    _bind_summary(summary, ref)
    return summary


__all__ = [
    "StoredSummaryRef",
    "SummaryStoreError",
    "load_summary",
    "put_summary",
]
