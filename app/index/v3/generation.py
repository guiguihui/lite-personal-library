"""Streaming logical Generation identities and canonical attestations.

Logical identity deliberately contains only the Generation recipe and the
sorted document-to-Segment mapping.  Source input proof remains a separately
authenticated attestation: it is bound strictly to the refs and recipe, but it
does not make two otherwise identical Generations acquire different IDs.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.index.v2.artifacts import ArtifactRef, AtomicHashingSink
from app.index.v2.canonical import iter_canonical_json
from app.index.v2.input_proof import INPUT_PROOF_SCHEMA_VERSION
from app.index.v2.object_store import StoredSegmentRef

from .models import (
    LOGICAL_GENERATION_SCHEMA_VERSION,
    MAX_U64,
    GenerationRecipe,
    validate_doc_key,
    validate_sha256,
)


MANIFEST_PATH = "manifest.json"
INPUT_PROOF_PATH = "input-proof.json"
RECEIPT_SCHEMA_VERSION = 1

_MANIFEST_KEYS = {
    "artifact_kind",
    "document_count",
    "documents",
    "generation",
    "generation_recipe",
    "generation_recipe_hash",
    "input_proof",
    "schema_version",
}
_PROOF_KEYS = {"compiler_recipe_hash", "documents", "schema_version"}
_PROOF_DOCUMENT_KEYS = {"content_hash", "segment_recipe_hash"}
_ARTIFACT_KEYS = {"byte_size", "records", "relative_path", "sha256"}
_RECIPE_KEYS = set(GenerationRecipe().as_dict())


class LogicalGenerationError(ValueError):
    """A logical Generation request or artifact violates its contract."""


@dataclass(frozen=True, slots=True)
class _ValidatedRef:
    doc_key: str
    segment_hash: str
    content_hash: str
    segment_recipe_hash: str


@dataclass(frozen=True, slots=True)
class _OwnedDirectory:
    path: Path
    device: int
    inode: int


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


def _strict_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LogicalGenerationError(f"{field} must be an object")
    return value


def _strict_keys(
    value: Mapping[str, Any],
    expected: set[str],
    field: str,
) -> tuple[str, ...]:
    """Consume a mapping's key iterator exactly once and reject anomalies."""

    keys = tuple(value)
    if not all(isinstance(key, str) for key in keys):
        raise LogicalGenerationError(f"{field} keys must be strings")
    if len(keys) != len(set(keys)):
        raise LogicalGenerationError(f"{field} contains duplicate keys")
    if set(keys) != expected:
        raise LogicalGenerationError(
            f"{field} must contain exactly {', '.join(sorted(expected))}"
        )
    return keys


def _has_adjacent_duplicate(values: list[str]) -> bool:
    return any(
        values[position - 1] == values[position]
        for position in range(1, len(values))
    )

def _write_json_value(sink: AtomicHashingSink, value: object) -> None:
    for fragment in iter_canonical_json(value):
        sink.write_text(fragment)


def _update_json_value(digest: Any, value: object) -> None:
    for fragment in iter_canonical_json(value):
        digest.update(fragment.encode("utf-8"))


def _canonical_digest(value: object) -> str:
    digest = hashlib.sha256()
    _update_json_value(digest, value)
    return digest.hexdigest()


def _write_key(sink: AtomicHashingSink, key: str) -> None:
    _write_json_value(sink, key)
    sink.write(b":")


def _validate_refs(
    refs: Iterable[StoredSegmentRef],
    check_cancelled: Callable[[], None],
) -> list[_ValidatedRef]:
    if isinstance(refs, (str, bytes, bytearray)):
        raise TypeError("refs must be an iterable of StoredSegmentRef values")
    try:
        iterator = iter(refs)
    except TypeError as exc:
        raise TypeError(
            "refs must be an iterable of StoredSegmentRef values"
        ) from exc

    result: list[_ValidatedRef] = []
    seen_segment_hashes: set[str] = set()
    for position, ref in enumerate(iterator):
        check_cancelled()
        if not isinstance(ref, StoredSegmentRef):
            raise TypeError("refs must contain only StoredSegmentRef values")
        try:
            doc_key = validate_doc_key(ref.doc_key)
            segment_hash = validate_sha256(
                ref.segment_hash, f"refs[{position}].segment_hash"
            )
            content_hash = validate_sha256(
                ref.content_hash, f"refs[{position}].content_hash"
            )
            segment_recipe_hash = validate_sha256(
                ref.segment_recipe_hash,
                f"refs[{position}].segment_recipe_hash",
            )
        except (TypeError, ValueError) as exc:
            raise LogicalGenerationError(str(exc)) from exc
        doc_type, slug = doc_key.split(":", 1)
        if ref.doc_type != doc_type or ref.slug != slug:
            raise LogicalGenerationError(
                f"Segment ref document attestation mismatch for {doc_key}"
            )
        _u64(ref.byte_size, f"refs[{position}].byte_size")
        if not isinstance(ref.path, Path):
            raise TypeError("StoredSegmentRef.path must be a pathlib.Path")
        if segment_hash in seen_segment_hashes:
            raise LogicalGenerationError(
                "segment_hash is attested to more than one document: "
                f"{segment_hash}"
            )
        seen_segment_hashes.add(segment_hash)
        result.append(
            _ValidatedRef(
                doc_key=doc_key,
                segment_hash=segment_hash,
                content_hash=content_hash,
                segment_recipe_hash=segment_recipe_hash,
            )
        )
    result.sort(key=lambda item: item.doc_key)
    for position in range(1, len(result)):
        if result[position - 1].doc_key == result[position].doc_key:
            raise LogicalGenerationError(
                f"duplicate document ref: {result[position].doc_key}"
            )
    return result


def _generation_id(refs: list[_ValidatedRef], recipe_hash: str) -> str:
    """Hash the canonical identity core without constructing it in memory."""

    digest = hashlib.sha256()
    digest.update(b'{"artifact_kind":"logical_generation","documents":{')
    for position, ref in enumerate(refs):
        if position:
            digest.update(b",")
        _update_json_value(digest, ref.doc_key)
        digest.update(b":")
        _update_json_value(digest, ref.segment_hash)
    digest.update(b'},"generation_recipe_hash":')
    _update_json_value(digest, recipe_hash)
    digest.update(
        b',"schema_version":'
        + str(LOGICAL_GENERATION_SCHEMA_VERSION).encode("ascii")
        + b"}"
    )
    return digest.hexdigest()


def _artifact_dict(reference: ArtifactRef) -> dict[str, object]:
    return {
        "byte_size": reference.byte_size,
        "records": reference.records,
        "relative_path": reference.relative_path,
        "sha256": reference.sha256,
    }


def _artifact_from_dict(value: object, expected_path: str) -> ArtifactRef:
    artifact = _strict_mapping(value, f"{expected_path} artifact receipt")
    _strict_keys(artifact, _ARTIFACT_KEYS, f"{expected_path} artifact receipt")
    try:
        reference = ArtifactRef(
            relative_path=artifact["relative_path"],  # type: ignore[arg-type]
            sha256=artifact["sha256"],  # type: ignore[arg-type]
            byte_size=artifact["byte_size"],  # type: ignore[arg-type]
            records=artifact["records"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise LogicalGenerationError(
            f"invalid {expected_path} artifact receipt: {exc}"
        ) from exc
    if reference.relative_path != expected_path:
        raise LogicalGenerationError(
            f"artifact path must be {expected_path!r}"
        )
    _u64(reference.byte_size, f"{expected_path}.byte_size")
    if reference.records is None:
        raise LogicalGenerationError(f"{expected_path}.records must be attested")
    _u64(reference.records, f"{expected_path}.records")
    return reference


@dataclass(frozen=True, slots=True)
class LogicalGenerationReceipt:
    """Small attestation for a Generation; no manifest or document map is held."""

    candidate_dir: Path
    generation_id: str
    generation_recipe_hash: str
    manifest_ref: ArtifactRef
    input_proof_ref: ArtifactRef
    document_count: int
    schema_version: int = RECEIPT_SCHEMA_VERSION
    artifact_kind: str = "logical_generation_receipt"

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_dir", Path(self.candidate_dir))
        if (
            type(self.schema_version) is not int
            or self.schema_version != RECEIPT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported logical Generation receipt schema_version")
        if self.artifact_kind != "logical_generation_receipt":
            raise ValueError("unsupported logical Generation receipt artifact_kind")
        validate_sha256(self.generation_id, "generation_id")
        validate_sha256(
            self.generation_recipe_hash, "generation_recipe_hash"
        )
        count = _u64(self.document_count, "document_count")
        for name, reference, expected_path in (
            ("manifest_ref", self.manifest_ref, MANIFEST_PATH),
            ("input_proof_ref", self.input_proof_ref, INPUT_PROOF_PATH),
        ):
            if not isinstance(reference, ArtifactRef):
                raise TypeError(f"{name} must be an ArtifactRef")
            if reference.relative_path != expected_path:
                raise ValueError(f"{name} path must be {expected_path!r}")
            _u64(reference.byte_size, f"{name}.byte_size")
            if reference.records is None:
                raise ValueError(f"{name}.records must be attested")
            _u64(reference.records, f"{name}.records")
            if reference.records != count:
                raise ValueError(f"{name}.records must equal document_count")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "schema_version": self.schema_version,
            "generation": self.generation_id,
            "generation_recipe_hash": self.generation_recipe_hash,
            "document_count": self.document_count,
            "artifacts": {
                "manifest": _artifact_dict(self.manifest_ref),
                "input_proof": _artifact_dict(self.input_proof_ref),
            },
        }

    @classmethod
    def from_dict(
        cls,
        candidate_dir: Path,
        value: object,
    ) -> "LogicalGenerationReceipt":
        receipt = _strict_mapping(value, "logical Generation receipt")
        expected = {
            "artifact_kind",
            "schema_version",
            "generation",
            "generation_recipe_hash",
            "document_count",
            "artifacts",
        }
        _strict_keys(receipt, expected, "logical Generation receipt")
        artifacts = _strict_mapping(receipt["artifacts"], "artifacts")
        _strict_keys(artifacts, {"manifest", "input_proof"}, "artifacts")
        return cls(
            candidate_dir=Path(candidate_dir),
            generation_id=receipt["generation"],  # type: ignore[arg-type]
            generation_recipe_hash=receipt["generation_recipe_hash"],  # type: ignore[arg-type]
            manifest_ref=_artifact_from_dict(
                artifacts["manifest"], MANIFEST_PATH
            ),
            input_proof_ref=_artifact_from_dict(
                artifacts["input_proof"], INPUT_PROOF_PATH
            ),
            document_count=receipt["document_count"],  # type: ignore[arg-type]
            schema_version=receipt["schema_version"],  # type: ignore[arg-type]
            artifact_kind=receipt["artifact_kind"],  # type: ignore[arg-type]
        )


def _metadata_is_link(metadata: os.stat_result) -> bool:
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(file_attributes & reparse_mask)


def _path_is_link(path: Path) -> bool:
    """Recognize POSIX links and every Windows reparse-point directory."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return _metadata_is_link(metadata)


def _capture_owned_directory(path: Path) -> _OwnedDirectory:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LogicalGenerationError(
            f"cannot inspect logical Generation staging directory: {path}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or _metadata_is_link(metadata):
        raise LogicalGenerationError(
            "logical Generation staging path is not an owned plain directory"
        )
    return _OwnedDirectory(path, metadata.st_dev, metadata.st_ino)


def _assert_owned_directory(owned: _OwnedDirectory) -> None:
    current = _capture_owned_directory(owned.path)
    if (current.device, current.inode) != (owned.device, owned.inode):
        raise LogicalGenerationError(
            "logical Generation staging directory ownership changed"
        )


def _validate_candidate_parent(parent: Path) -> None:
    if not parent.is_dir():
        raise LogicalGenerationError(
            "candidate_dir parent must be an existing directory"
        )
    for component in (parent, *parent.parents):
        if _path_is_link(component):
            raise LogicalGenerationError(
                f"candidate_dir must not traverse a symlink or junction: {component}"
            )


def _prepare_candidate(candidate_dir: Path) -> tuple[Path, _OwnedDirectory]:
    """Create an unpredictable private sibling, leaving the public path absent."""

    candidate = Path(candidate_dir)
    if not candidate.name or candidate.parent == candidate:
        raise LogicalGenerationError("candidate_dir must name a new directory")
    if os.path.lexists(candidate):
        raise LogicalGenerationError("candidate_dir must not already exist")
    parent = candidate.parent
    _validate_candidate_parent(parent)
    try:
        staging = Path(
            tempfile.mkdtemp(
                dir=parent,
                prefix=f".{candidate.name}.logical-generation.",
            )
        )
    except FileExistsError as exc:
        raise LogicalGenerationError("candidate_dir must not already exist") from exc
    try:
        owned = _capture_owned_directory(staging)
    except BaseException as primary:
        try:
            staging.rmdir()
        except BaseException as cleanup_error:
            if hasattr(primary, "add_note"):
                primary.add_note(
                    f"failed to remove invalid staging directory {staging}: "
                    f"{cleanup_error!r}"
                )
            raise LogicalGenerationError(
                "failed to clean invalid logical Generation staging directory"
            ) from cleanup_error
        raise
    return candidate, owned


def _cleanup_candidate(owned: _OwnedDirectory, primary: BaseException) -> None:
    """Remove only owned, known files; never recurse through a swapped path."""

    try:
        _assert_owned_directory(owned)
        for relative_path in (MANIFEST_PATH, INPUT_PROOF_PATH):
            artifact = owned.path / relative_path
            try:
                metadata = artifact.lstat()
            except FileNotFoundError:
                continue
            if _path_is_link(artifact) or not stat.S_ISREG(metadata.st_mode):
                raise LogicalGenerationError(
                    f"refusing to clean unexpected artifact type: {artifact}"
                )
            artifact.unlink()
        with os.scandir(owned.path) as entries:
            leftovers = tuple(entry.name for entry in entries)
        if leftovers:
            raise LogicalGenerationError(
                "refusing to recursively clean unexpected staging entries: "
                + ", ".join(sorted(leftovers))
            )
        owned.path.rmdir()
    except BaseException as cleanup_error:
        if hasattr(primary, "add_note"):
            primary.add_note(
                f"failed to clean logical Generation staging directory "
                f"{owned.path}: {cleanup_error!r}"
            )
        raise LogicalGenerationError(
            "failed to safely clean logical Generation staging directory"
        ) from cleanup_error


def _rename_no_replace(source: Path, target: Path) -> None:
    """Atomically publish a directory, never replacing an existing target."""

    if os.name == "nt":
        # Windows directory rename is no-clobber unless replacement is
        # explicitly requested through a different API.
        source.rename(target)
        return
    if sys.platform != "linux":
        raise LogicalGenerationError(
            "atomic no-replace directory publication is unsupported "
            f"on {sys.platform}"
        )

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise LogicalGenerationError(
            "renameat2(RENAME_NOREPLACE) is required for safe publication"
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(target),
        rename_noreplace,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), target)
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise LogicalGenerationError(
            "filesystem does not support atomic no-replace publication"
        )
    raise OSError(error, os.strerror(error), target)

def _write_input_proof(
    path: Path,
    proof: Mapping[str, Any],
    refs: list[_ValidatedRef],
    recipe_hash: str,
    check_cancelled: Callable[[], None],
) -> ArtifactRef:
    _strict_keys(proof, _PROOF_KEYS, "input proof")
    schema_version = proof["schema_version"]
    if (
        type(schema_version) is not int
        or schema_version != INPUT_PROOF_SCHEMA_VERSION
    ):
        raise LogicalGenerationError(
            f"input proof schema_version must equal {INPUT_PROOF_SCHEMA_VERSION}"
        )
    try:
        compiler_recipe_hash = validate_sha256(
            proof["compiler_recipe_hash"], "compiler_recipe_hash"
        )
    except (TypeError, ValueError) as exc:
        raise LogicalGenerationError(str(exc)) from exc
    if compiler_recipe_hash != recipe_hash:
        raise LogicalGenerationError(
            "input proof compiler_recipe_hash does not match Generation recipe"
        )

    raw_documents = _strict_mapping(proof["documents"], "input proof documents")
    ordered_keys = list(raw_documents)
    if not all(isinstance(key, str) for key in ordered_keys):
        raise LogicalGenerationError("input proof document keys must be strings")
    ordered_keys.sort()
    if _has_adjacent_duplicate(ordered_keys):
        raise LogicalGenerationError("input proof contains duplicate document keys")
    if len(ordered_keys) != len(refs):
        raise LogicalGenerationError(
            "input proof documents do not match Segment refs"
        )

    sink = AtomicHashingSink(path)
    with sink:
        sink.write(b'{"compiler_recipe_hash":')
        _write_json_value(sink, compiler_recipe_hash)
        sink.write(b',"documents":{')
        for position, (doc_key, ref) in enumerate(zip(ordered_keys, refs)):
            check_cancelled()
            try:
                validated_key = validate_doc_key(doc_key)
            except (TypeError, ValueError) as exc:
                raise LogicalGenerationError(str(exc)) from exc
            if validated_key != ref.doc_key:
                raise LogicalGenerationError(
                    "input proof documents do not match Segment refs"
                )
            entry = _strict_mapping(
                raw_documents[doc_key], f"input proof documents[{doc_key!r}]"
            )
            _strict_keys(
                entry,
                _PROOF_DOCUMENT_KEYS,
                f"input proof documents[{doc_key!r}]",
            )
            try:
                content_hash = validate_sha256(
                    entry["content_hash"],
                    f"documents[{doc_key!r}].content_hash",
                )
                segment_recipe_hash = validate_sha256(
                    entry["segment_recipe_hash"],
                    f"documents[{doc_key!r}].segment_recipe_hash",
                )
            except (TypeError, ValueError) as exc:
                raise LogicalGenerationError(str(exc)) from exc
            if (
                content_hash != ref.content_hash
                or segment_recipe_hash != ref.segment_recipe_hash
            ):
                raise LogicalGenerationError(
                    f"input proof attestation does not match Segment ref: {doc_key}"
                )
            if position:
                sink.write(b",")
            _write_key(sink, doc_key)
            sink.write(b'{"content_hash":')
            _write_json_value(sink, content_hash)
            sink.write(b',"segment_recipe_hash":')
            _write_json_value(sink, segment_recipe_hash)
            sink.write(b"}")
        sink.write(b'},"schema_version":')
        sink.write(str(INPUT_PROOF_SCHEMA_VERSION).encode("ascii"))
        sink.write(b"}")
    return ArtifactRef(
        relative_path=INPUT_PROOF_PATH,
        sha256=sink.sha256,
        byte_size=sink.byte_size,
        records=len(refs),
    )


def _write_manifest(
    path: Path,
    refs: list[_ValidatedRef],
    recipe: GenerationRecipe,
    recipe_hash: str,
    generation_id: str,
    input_proof_ref: ArtifactRef,
    check_cancelled: Callable[[], None],
) -> ArtifactRef:
    sink = AtomicHashingSink(path)
    with sink:
        sink.write(b'{"artifact_kind":"logical_generation","document_count":')
        sink.write(str(len(refs)).encode("ascii"))
        sink.write(b',"documents":{')
        for position, ref in enumerate(refs):
            check_cancelled()
            if position:
                sink.write(b",")
            _write_key(sink, ref.doc_key)
            _write_json_value(sink, ref.segment_hash)
        sink.write(b'},"generation":')
        _write_json_value(sink, generation_id)
        sink.write(b',"generation_recipe":')
        _write_json_value(sink, recipe.as_dict())
        sink.write(b',"generation_recipe_hash":')
        _write_json_value(sink, recipe_hash)
        sink.write(b',"input_proof":')
        _write_json_value(sink, _artifact_dict(input_proof_ref))
        sink.write(b',"schema_version":')
        sink.write(str(LOGICAL_GENERATION_SCHEMA_VERSION).encode("ascii"))
        sink.write(b"}")
    return ArtifactRef(
        relative_path=MANIFEST_PATH,
        sha256=sink.sha256,
        byte_size=sink.byte_size,
        records=len(refs),
    )


def validate_logical_generation_manifest(value: object) -> None:
    """Fail closed on a decoded P3 manifest, including schema-2/3 artifacts."""

    manifest = _strict_mapping(value, "logical Generation manifest")
    _strict_keys(manifest, _MANIFEST_KEYS, "logical Generation manifest")
    if manifest["artifact_kind"] != "logical_generation":
        raise LogicalGenerationError("unsupported logical Generation artifact_kind")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != LOGICAL_GENERATION_SCHEMA_VERSION
    ):
        raise LogicalGenerationError(
            "unsupported logical Generation schema_version"
        )
    try:
        generation = validate_sha256(manifest["generation"], "generation")
        recipe_hash = validate_sha256(
            manifest["generation_recipe_hash"], "generation_recipe_hash"
        )
    except (TypeError, ValueError) as exc:
        raise LogicalGenerationError(str(exc)) from exc
    recipe_value = _strict_mapping(
        manifest["generation_recipe"], "generation_recipe"
    )
    _strict_keys(recipe_value, _RECIPE_KEYS, "generation_recipe")
    raw_recipe = dict(recipe_value)
    try:
        recipe = GenerationRecipe(**raw_recipe)
    except (TypeError, ValueError) as exc:
        raise LogicalGenerationError(f"invalid generation_recipe: {exc}") from exc
    if recipe.as_dict() != raw_recipe:
        raise LogicalGenerationError(
            "generation_recipe must contain canonical, normalized values"
        )
    if _canonical_digest(raw_recipe) != recipe_hash:
        raise LogicalGenerationError("generation_recipe_hash mismatch")

    raw_documents = _strict_mapping(manifest["documents"], "documents")
    ordered_keys = list(raw_documents)
    if not all(isinstance(key, str) for key in ordered_keys):
        raise LogicalGenerationError("documents keys must be strings")
    ordered_keys.sort()
    if _has_adjacent_duplicate(ordered_keys):
        raise LogicalGenerationError("documents contains duplicate keys")
    count = _u64(manifest["document_count"], "document_count")
    if count != len(ordered_keys):
        raise LogicalGenerationError("document_count does not match documents")
    refs: list[_ValidatedRef] = []
    seen_segment_hashes: set[str] = set()
    for doc_key in ordered_keys:
        try:
            validated_key = validate_doc_key(doc_key)
            segment_hash = validate_sha256(
                raw_documents[doc_key], f"documents[{doc_key!r}]"
            )
        except (TypeError, ValueError) as exc:
            raise LogicalGenerationError(str(exc)) from exc
        if segment_hash in seen_segment_hashes:
            raise LogicalGenerationError(
                "segment_hash is attested to more than one document: "
                f"{segment_hash}"
            )
        seen_segment_hashes.add(segment_hash)
        refs.append(
            _ValidatedRef(validated_key, segment_hash, "0" * 64, "0" * 64)
        )
    if _generation_id(refs, recipe_hash) != generation:
        raise LogicalGenerationError("generation identity mismatch")
    input_proof = _artifact_from_dict(
        manifest["input_proof"], INPUT_PROOF_PATH
    )
    if input_proof.records != count:
        raise LogicalGenerationError(
            "input-proof.json records does not match document_count"
        )


def build_logical_generation(
    refs: Iterable[StoredSegmentRef],
    proof: Mapping[str, object],
    recipe: GenerationRecipe,
    candidate_dir: Path,
    check_cancelled: Callable[[], None] | None = None,
) -> LogicalGenerationReceipt:
    """Build one immutable schema-4 logical Generation candidate.

    ``refs`` and the proof's document mapping are each consumed once.  The
    returned receipt contains only scalar identities and two artifact refs;
    callers may release the O(N) inputs immediately after this function.
    """

    if not isinstance(recipe, GenerationRecipe):
        raise TypeError("recipe must be a GenerationRecipe")
    if not isinstance(proof, Mapping):
        raise TypeError("proof must be a mapping")
    if check_cancelled is None:
        check_cancelled = lambda: None
    if not callable(check_cancelled):
        raise TypeError("check_cancelled must be callable")

    check_cancelled()
    validated_refs = _validate_refs(refs, check_cancelled)
    recipe_hash = _canonical_digest(recipe.as_dict())
    generation_id = _generation_id(validated_refs, recipe_hash)
    check_cancelled()

    candidate, owned = _prepare_candidate(Path(candidate_dir))
    try:
        _assert_owned_directory(owned)
        input_proof_ref = _write_input_proof(
            owned.path / INPUT_PROOF_PATH,
            proof,
            validated_refs,
            recipe_hash,
            check_cancelled,
        )
        _assert_owned_directory(owned)
        check_cancelled()
        manifest_ref = _write_manifest(
            owned.path / MANIFEST_PATH,
            validated_refs,
            recipe,
            recipe_hash,
            generation_id,
            input_proof_ref,
            check_cancelled,
        )
        _assert_owned_directory(owned)
        check_cancelled()
        receipt = LogicalGenerationReceipt(
            candidate_dir=candidate,
            generation_id=generation_id,
            generation_recipe_hash=recipe_hash,
            manifest_ref=manifest_ref,
            input_proof_ref=input_proof_ref,
            document_count=len(validated_refs),
        )
        _validate_candidate_parent(candidate.parent)
        _assert_owned_directory(owned)
        if os.path.lexists(candidate):
            raise LogicalGenerationError("candidate_dir must not already exist")
        try:
            _rename_no_replace(owned.path, candidate)
        except FileExistsError as exc:
            raise LogicalGenerationError(
                "candidate_dir must not already exist"
            ) from exc
        owned = _OwnedDirectory(candidate, owned.device, owned.inode)
        _assert_owned_directory(owned)
        return receipt
    except BaseException as exc:
        _cleanup_candidate(owned, exc)
        raise


__all__ = [
    "INPUT_PROOF_PATH",
    "MANIFEST_PATH",
    "LogicalGenerationError",
    "LogicalGenerationReceipt",
    "build_logical_generation",
    "validate_logical_generation_manifest",
]
