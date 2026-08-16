"""Bounded-memory validation for a persisted logical Generation.

The manifest and input proof are parsed as a synchronized pair.  This proves
their canonical, internal consistency and the presence of the addressed
Segment files without decoding any Segment.  It deliberately does *not*
provide an external source anchor: a self-consistent replacement Generation
must still be rejected by comparing its receipt to trusted caller state.

Memory is O(stream chunk + largest document key + distinct Segment hashes).
Document keys have no schema-level length limit, so each streamed key is
subject to a generous per-value safety bound rather than whole-file loading.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.index.v2.artifacts import ArtifactRef
from app.index.v2.canonical import canonical_bytes, canonical_hash
from app.index.v2.input_proof import INPUT_PROOF_SCHEMA_VERSION
from app.index.v2.object_store import StoredSegmentRef
from app.index.v2.streaming_json import BoundedJsonError, CanonicalJsonStream

from .generation import (
    INPUT_PROOF_PATH,
    MANIFEST_PATH,
    LogicalGenerationError,
    LogicalGenerationReceipt,
)
from .models import (
    LOGICAL_GENERATION_SCHEMA_VERSION,
    MAX_U64,
    GenerationRecipe,
    validate_doc_key,
    validate_sha256,
)


_STREAM_CHUNK_SIZE = 1024 * 1024
_MAX_SCALAR_BYTES = 1024
_MAX_DOCUMENT_KEY_BYTES = 16 * 1024 * 1024
_MAX_PROOF_RECORD_BYTES = 1024
_MAX_RECIPE_BYTES = 64 * 1024
_MAX_ARTIFACT_BYTES = 4096

_EXPECTED_FILES = frozenset({MANIFEST_PATH, INPUT_PROOF_PATH})
_RECIPE_KEYS = frozenset(GenerationRecipe().as_dict())
_PROOF_RECORD_KEYS = frozenset({"content_hash", "segment_recipe_hash"})
_ARTIFACT_KEYS = frozenset({"byte_size", "records", "relative_path", "sha256"})


class _DigestObserver:
    __slots__ = ("_check_cancelled", "_digest", "byte_size")

    def __init__(self, check_cancelled: Callable[[], None]) -> None:
        self._check_cancelled = check_cancelled
        self._digest = hashlib.sha256()
        self.byte_size = 0

    def __call__(self, chunk: bytes) -> None:
        try:
            self._check_cancelled()
        except BaseException as exc:
            # Keep cancellation distinct even when it happens to use the same
            # exception type as the JSON parser.
            raise _CancellationSignal(exc) from exc
        self._digest.update(chunk)
        self.byte_size += len(chunk)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


class _CancellationSignal(BaseException):
    __slots__ = ("cause",)

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class _CandidateSnapshot:
    root: _PathIdentity
    files: Mapping[str, _PathIdentity]


def _metadata_is_link(metadata: os.stat_result) -> bool:
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_mask)


def _identity(metadata: os.stat_result) -> _PathIdentity:
    return _PathIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _plain_directory(path: Path, field: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LogicalGenerationError(f"cannot inspect {field}: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or _metadata_is_link(metadata):
        raise LogicalGenerationError(f"{field} must be a plain directory")
    return metadata


def _plain_file(path: Path, field: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LogicalGenerationError(f"cannot inspect {field}: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or _metadata_is_link(metadata):
        raise LogicalGenerationError(f"{field} must be a plain regular file")
    return metadata


def _capture_candidate(root: Path) -> _CandidateSnapshot:
    root_metadata = _plain_directory(root, "logical Generation receipt root")
    # Reject junction/symlink traversal above the leaf as well as at the leaf.
    for parent in root.parents:
        parent_metadata = _plain_directory(
            parent, "logical Generation receipt root ancestor"
        )
        if _metadata_is_link(parent_metadata):
            raise LogicalGenerationError(
                "logical Generation receipt root must not traverse a link"
            )
    try:
        with os.scandir(root) as entries:
            names = {entry.name for entry in entries}
    except OSError as exc:
        raise LogicalGenerationError(
            "cannot enumerate logical Generation receipt root"
        ) from exc
    if names != _EXPECTED_FILES:
        raise LogicalGenerationError(
            "logical Generation receipt root has an invalid file set"
        )
    files = {
        name: _identity(_plain_file(root / name, f"Generation artifact {name}"))
        for name in sorted(_EXPECTED_FILES)
    }
    return _CandidateSnapshot(_identity(root_metadata), files)


def _assert_candidate_unchanged(root: Path, before: _CandidateSnapshot) -> None:
    after = _capture_candidate(root)
    if after != before:
        raise LogicalGenerationError(
            "logical Generation receipt root changed while validating"
        )


def _u64(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_U64
    ):
        raise LogicalGenerationError(
            f"{field} must be an integer in the range [0, {MAX_U64}]"
        )
    return value


def _validated_sha(value: object, field: str) -> str:
    try:
        return validate_sha256(value, field)
    except (TypeError, ValueError) as exc:
        raise LogicalGenerationError(str(exc)) from exc


def _validated_doc_key(value: object, field: str) -> str:
    try:
        return validate_doc_key(value)
    except (TypeError, ValueError) as exc:
        raise LogicalGenerationError(f"invalid {field}: {exc}") from exc


def _strict_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LogicalGenerationError(f"{field} must be an object")
    return value


def _strict_keys(
    value: Mapping[str, Any], expected: frozenset[str], field: str
) -> None:
    keys = tuple(value)
    if not all(isinstance(key, str) for key in keys) or set(keys) != expected:
        raise LogicalGenerationError(
            f"{field} must contain exactly {', '.join(sorted(expected))}"
        )


def _artifact_from_value(value: object) -> ArtifactRef:
    mapping = _strict_mapping(value, "manifest input_proof")
    _strict_keys(mapping, _ARTIFACT_KEYS, "manifest input_proof")
    try:
        reference = ArtifactRef(
            relative_path=mapping["relative_path"],  # type: ignore[arg-type]
            sha256=mapping["sha256"],  # type: ignore[arg-type]
            byte_size=mapping["byte_size"],  # type: ignore[arg-type]
            records=mapping["records"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise LogicalGenerationError(f"invalid manifest input_proof: {exc}") from exc
    if reference.relative_path != INPUT_PROOF_PATH:
        raise LogicalGenerationError(
            f"manifest input_proof path must be {INPUT_PROOF_PATH!r}"
        )
    _u64(reference.byte_size, "manifest input_proof.byte_size")
    if reference.records is None:
        raise LogicalGenerationError("manifest input_proof.records must be attested")
    _u64(reference.records, "manifest input_proof.records")
    return reference


class _SegmentStoreInspector:
    __slots__ = ("_ready", "_root", "_segments", "_validated_prefixes")

    def __init__(self, pageindex_dir: Path) -> None:
        self._root = Path(pageindex_dir).absolute()
        self._segments = self._root / "objects" / "segments"
        self._ready = False
        self._validated_prefixes: set[str] = set()

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        _plain_directory(self._root, "PageIndex root")
        for parent in self._root.parents:
            _plain_directory(parent, "PageIndex root ancestor")
        _plain_directory(self._root / "objects", "PageIndex objects directory")
        _plain_directory(self._segments, "PageIndex Segment directory")
        self._ready = True

    def inspect(
        self,
        *,
        doc_key: str,
        segment_hash: str,
        content_hash: str,
        segment_recipe_hash: str,
    ) -> StoredSegmentRef:
        self._ensure_ready()
        prefix = segment_hash[:2]
        prefix_dir = self._segments / prefix
        if prefix not in self._validated_prefixes:
            _plain_directory(prefix_dir, f"Segment prefix directory {prefix}")
            self._validated_prefixes.add(prefix)
        path = prefix_dir / f"{segment_hash}.json"
        metadata = _plain_file(path, f"Segment object {segment_hash}")
        byte_size = _u64(metadata.st_size, f"Segment object {segment_hash} size")
        doc_type, _, slug = doc_key.partition(":")
        return StoredSegmentRef(
            segment_hash=segment_hash,
            path=path,
            byte_size=byte_size,
            doc_key=doc_key,
            doc_type=doc_type,
            slug=slug,
            content_hash=content_hash,
            segment_recipe_hash=segment_recipe_hash,
        )


def _read_value(
    reader: CanonicalJsonStream, *, max_bytes: int, field: str
) -> object:
    try:
        return reader.read_value(max_bytes=max_bytes)
    except BoundedJsonError as exc:
        raise LogicalGenerationError(f"invalid {field}: {exc}") from exc


def _expect(reader: CanonicalJsonStream, literal: bytes, field: str) -> None:
    try:
        reader.expect(literal)
    except BoundedJsonError as exc:
        raise LogicalGenerationError(f"invalid {field}: {exc}") from exc


def _peek(reader: CanonicalJsonStream, field: str) -> int | None:
    try:
        return reader.peek_byte()
    except BoundedJsonError as exc:
        raise LogicalGenerationError(f"invalid {field}: {exc}") from exc


def _finish(reader: CanonicalJsonStream, field: str) -> None:
    try:
        reader.finish()
    except BoundedJsonError as exc:
        raise LogicalGenerationError(f"invalid {field}: {exc}") from exc


def validate_generation_stream(
    receipt: LogicalGenerationReceipt,
    pageindex_dir: Path,
    *,
    check_cancelled: Callable[[], None],
    collect_refs: bool = False,
    recipe_observer: Callable[[GenerationRecipe], None] | None = None,
) -> dict[str, StoredSegmentRef]:
    """Validate one Generation in one bounded parse pass per control file.

    With ``collect_refs=False`` no document-to-ref collection is retained.  A
    compact Segment-hash set remains necessary to reject one object being
    attested as multiple documents.  See the module docstring for the external
    source-anchor limitation.
    """

    if not isinstance(receipt, LogicalGenerationReceipt):
        raise TypeError("receipt must be a LogicalGenerationReceipt")
    if not callable(check_cancelled):
        raise TypeError("check_cancelled must be callable")
    if type(collect_refs) is not bool:
        raise TypeError("collect_refs must be a bool")
    if recipe_observer is not None and not callable(recipe_observer):
        raise TypeError("recipe_observer must be callable")

    check_cancelled()
    root = Path(receipt.candidate_dir).absolute()
    before = _capture_candidate(root)
    if before.files[MANIFEST_PATH].size != receipt.manifest_ref.byte_size:
        raise LogicalGenerationError("manifest.json size does not match receipt")
    if before.files[INPUT_PROOF_PATH].size != receipt.input_proof_ref.byte_size:
        raise LogicalGenerationError("input-proof.json size does not match receipt")

    segment_store = _SegmentStoreInspector(Path(pageindex_dir))
    manifest_observer = _DigestObserver(check_cancelled)
    proof_observer = _DigestObserver(check_cancelled)
    generation_digest = hashlib.sha256()
    generation_digest.update(
        b'{"artifact_kind":"logical_generation","documents":{'
    )
    refs: dict[str, StoredSegmentRef] = {}
    seen_segment_hashes: set[str] = set()
    count = 0
    previous_key: str | None = None

    try:
        with CanonicalJsonStream(
            root / MANIFEST_PATH,
            chunk_size=_STREAM_CHUNK_SIZE,
            read_observer=manifest_observer,
        ) as manifest, CanonicalJsonStream(
            root / INPUT_PROOF_PATH,
            chunk_size=_STREAM_CHUNK_SIZE,
            read_observer=proof_observer,
        ) as proof:
            _expect(
                manifest,
                b'{"artifact_kind":"logical_generation","document_count":',
                MANIFEST_PATH,
            )
            manifest_count = _u64(
                _read_value(
                    manifest,
                    max_bytes=_MAX_SCALAR_BYTES,
                    field="manifest document_count",
                ),
                "manifest document_count",
            )
            _expect(manifest, b',"documents":{', MANIFEST_PATH)

            _expect(proof, b'{"compiler_recipe_hash":', INPUT_PROOF_PATH)
            proof_recipe_hash = _validated_sha(
                _read_value(
                    proof,
                    max_bytes=_MAX_SCALAR_BYTES,
                    field="input proof compiler_recipe_hash",
                ),
                "input proof compiler_recipe_hash",
            )
            _expect(proof, b',"documents":{', INPUT_PROOF_PATH)

            while True:
                manifest_done = _peek(manifest, MANIFEST_PATH) == ord("}")
                proof_done = _peek(proof, INPUT_PROOF_PATH) == ord("}")
                if manifest_done or proof_done:
                    if manifest_done != proof_done:
                        raise LogicalGenerationError(
                            "manifest and input proof document sets differ"
                        )
                    break
                check_cancelled()
                if count:
                    _expect(manifest, b",", MANIFEST_PATH)
                    _expect(proof, b",", INPUT_PROOF_PATH)

                doc_key = _validated_doc_key(
                    _read_value(
                        manifest,
                        max_bytes=_MAX_DOCUMENT_KEY_BYTES,
                        field="manifest document key",
                    ),
                    "manifest document key",
                )
                _expect(manifest, b":", MANIFEST_PATH)
                segment_hash = _validated_sha(
                    _read_value(
                        manifest,
                        max_bytes=_MAX_SCALAR_BYTES,
                        field=f"manifest documents[{doc_key!r}]",
                    ),
                    f"manifest documents[{doc_key!r}]",
                )

                proof_key = _validated_doc_key(
                    _read_value(
                        proof,
                        max_bytes=_MAX_DOCUMENT_KEY_BYTES,
                        field="input proof document key",
                    ),
                    "input proof document key",
                )
                if proof_key != doc_key:
                    raise LogicalGenerationError(
                        "manifest and input proof document keys differ"
                    )
                _expect(proof, b":", INPUT_PROOF_PATH)
                proof_record = _strict_mapping(
                    _read_value(
                        proof,
                        max_bytes=_MAX_PROOF_RECORD_BYTES,
                        field=f"input proof documents[{doc_key!r}]",
                    ),
                    f"input proof documents[{doc_key!r}]",
                )
                _strict_keys(
                    proof_record,
                    _PROOF_RECORD_KEYS,
                    f"input proof documents[{doc_key!r}]",
                )
                content_hash = _validated_sha(
                    proof_record["content_hash"],
                    f"input proof documents[{doc_key!r}].content_hash",
                )
                segment_recipe_hash = _validated_sha(
                    proof_record["segment_recipe_hash"],
                    f"input proof documents[{doc_key!r}].segment_recipe_hash",
                )

                if previous_key is not None and doc_key <= previous_key:
                    raise LogicalGenerationError(
                        "Generation document keys must be strictly increasing"
                    )
                if segment_hash in seen_segment_hashes:
                    raise LogicalGenerationError(
                        "segment_hash is attested to more than one document: "
                        f"{segment_hash}"
                    )
                seen_segment_hashes.add(segment_hash)

                if count:
                    generation_digest.update(b",")
                generation_digest.update(canonical_bytes(doc_key))
                generation_digest.update(b":")
                generation_digest.update(canonical_bytes(segment_hash))

                reference = segment_store.inspect(
                    doc_key=doc_key,
                    segment_hash=segment_hash,
                    content_hash=content_hash,
                    segment_recipe_hash=segment_recipe_hash,
                )
                if collect_refs:
                    refs[doc_key] = reference
                previous_key = doc_key
                count += 1

            _expect(manifest, b'},"generation":', MANIFEST_PATH)
            manifest_generation = _validated_sha(
                _read_value(
                    manifest,
                    max_bytes=_MAX_SCALAR_BYTES,
                    field="manifest generation",
                ),
                "manifest generation",
            )
            _expect(manifest, b',"generation_recipe":', MANIFEST_PATH)
            recipe_value = _strict_mapping(
                _read_value(
                    manifest,
                    max_bytes=_MAX_RECIPE_BYTES,
                    field="manifest generation_recipe",
                ),
                "manifest generation_recipe",
            )
            _strict_keys(recipe_value, _RECIPE_KEYS, "manifest generation_recipe")
            raw_recipe = dict(recipe_value)
            try:
                recipe = GenerationRecipe(**raw_recipe)
            except (TypeError, ValueError) as exc:
                raise LogicalGenerationError(
                    f"invalid manifest generation_recipe: {exc}"
                ) from exc
            if recipe.as_dict() != raw_recipe:
                raise LogicalGenerationError(
                    "manifest generation_recipe is not normalized"
                )

            _expect(manifest, b',"generation_recipe_hash":', MANIFEST_PATH)
            manifest_recipe_hash = _validated_sha(
                _read_value(
                    manifest,
                    max_bytes=_MAX_SCALAR_BYTES,
                    field="manifest generation_recipe_hash",
                ),
                "manifest generation_recipe_hash",
            )
            _expect(manifest, b',"input_proof":', MANIFEST_PATH)
            embedded_proof = _artifact_from_value(
                _read_value(
                    manifest,
                    max_bytes=_MAX_ARTIFACT_BYTES,
                    field="manifest input_proof",
                )
            )
            _expect(manifest, b',"schema_version":', MANIFEST_PATH)
            manifest_schema = _read_value(
                manifest,
                max_bytes=_MAX_SCALAR_BYTES,
                field="manifest schema_version",
            )
            if (
                type(manifest_schema) is not int
                or manifest_schema != LOGICAL_GENERATION_SCHEMA_VERSION
            ):
                raise LogicalGenerationError(
                    "unsupported logical Generation schema_version"
                )
            _expect(manifest, b"}", MANIFEST_PATH)
            _finish(manifest, MANIFEST_PATH)

            _expect(proof, b'},"schema_version":', INPUT_PROOF_PATH)
            proof_schema = _read_value(
                proof,
                max_bytes=_MAX_SCALAR_BYTES,
                field="input proof schema_version",
            )
            if (
                type(proof_schema) is not int
                or proof_schema != INPUT_PROOF_SCHEMA_VERSION
            ):
                raise LogicalGenerationError("unsupported input proof schema_version")
            _expect(proof, b"}", INPUT_PROOF_PATH)
            _finish(proof, INPUT_PROOF_PATH)
    except _CancellationSignal as signal:
        raise signal.cause
    except BoundedJsonError as exc:
        # Defensive fallback for parser operations added above without wrappers.
        raise LogicalGenerationError(f"invalid logical Generation JSON: {exc}") from exc

    _assert_candidate_unchanged(root, before)

    actual_manifest = ArtifactRef(
        relative_path=MANIFEST_PATH,
        sha256=manifest_observer.sha256,
        byte_size=manifest_observer.byte_size,
        records=count,
    )
    actual_proof = ArtifactRef(
        relative_path=INPUT_PROOF_PATH,
        sha256=proof_observer.sha256,
        byte_size=proof_observer.byte_size,
        records=count,
    )
    if actual_manifest != receipt.manifest_ref:
        raise LogicalGenerationError("manifest.json hash, size, or records mismatch")
    if actual_proof != receipt.input_proof_ref:
        raise LogicalGenerationError("input-proof.json hash, size, or records mismatch")
    if embedded_proof != actual_proof:
        raise LogicalGenerationError("manifest input_proof attestation mismatch")
    if count != manifest_count or count != receipt.document_count:
        raise LogicalGenerationError("logical Generation document_count mismatch")
    if embedded_proof.records != count:
        raise LogicalGenerationError("manifest input_proof records mismatch")

    recipe_hash = canonical_hash(raw_recipe)
    if not (
        recipe_hash
        == manifest_recipe_hash
        == proof_recipe_hash
        == receipt.generation_recipe_hash
    ):
        raise LogicalGenerationError("logical Generation recipe hash mismatch")

    generation_digest.update(b'},"generation_recipe_hash":')
    generation_digest.update(canonical_bytes(recipe_hash))
    generation_digest.update(
        b',"schema_version":'
        + str(LOGICAL_GENERATION_SCHEMA_VERSION).encode("ascii")
        + b"}"
    )
    generation_id = generation_digest.hexdigest()
    if generation_id != manifest_generation or generation_id != receipt.generation_id:
        raise LogicalGenerationError("logical Generation identity mismatch")
    if recipe_observer is not None:
        recipe_observer(recipe)
    return refs


__all__ = ["validate_generation_stream"]
