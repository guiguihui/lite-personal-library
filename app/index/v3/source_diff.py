"""Derive explicit Segment changes from one stable source-catalog snapshot."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from app.index.v2.object_store import StoredSegmentRef
from app.index.v2.source_snapshot import StableCatalogSnapshot

from .models import MAX_U64, validate_doc_key, validate_sha256


def _sorted_tuple(name: str, values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(sorted(values))
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must contain unique document keys")
    for value in result:
        validate_doc_key(value)
    return result


@dataclass(frozen=True, slots=True)
class SegmentChangeSet:
    """A deterministic partition of current and base document identities."""

    base_by_doc: Mapping[str, StoredSegmentRef]
    current_fingerprints: Mapping[str, str]
    added: tuple[str, ...]
    changed: tuple[str, ...]
    deleted: tuple[str, ...]
    unchanged: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.base_by_doc, Mapping):
            raise TypeError("base_by_doc must be a mapping")
        if not isinstance(self.current_fingerprints, Mapping):
            raise TypeError("current_fingerprints must be a mapping")

        base: dict[str, StoredSegmentRef] = {}
        for raw_doc_key in sorted(self.base_by_doc):
            doc_key = validate_doc_key(raw_doc_key)
            ref = self.base_by_doc[raw_doc_key]
            if not isinstance(ref, StoredSegmentRef):
                raise TypeError("base_by_doc values must be StoredSegmentRef instances")
            if ref.doc_key != doc_key:
                raise ValueError(f"base ref key mismatch for {doc_key}")
            base[doc_key] = ref

        fingerprints: dict[str, str] = {}
        for raw_doc_key in sorted(self.current_fingerprints):
            doc_key = validate_doc_key(raw_doc_key)
            fingerprints[doc_key] = validate_sha256(
                self.current_fingerprints[raw_doc_key],
                f"current_fingerprints[{doc_key!r}] digest",
            )

        added = _sorted_tuple("added", self.added)
        changed = _sorted_tuple("changed", self.changed)
        deleted = _sorted_tuple("deleted", self.deleted)
        unchanged = _sorted_tuple("unchanged", self.unchanged)
        partitions = (set(added), set(changed), set(deleted), set(unchanged))
        for position, left in enumerate(partitions):
            for right in partitions[position + 1 :]:
                if left & right:
                    raise ValueError("change-set partitions must be disjoint")

        base_keys = set(base)
        current_keys = set(fingerprints)
        if set(deleted) | set(changed) | set(unchanged) != base_keys:
            raise ValueError("base documents are not exactly partitioned")
        if set(added) | set(changed) | set(unchanged) != current_keys:
            raise ValueError("current documents are not exactly partitioned")

        object.__setattr__(self, "base_by_doc", MappingProxyType(base))
        object.__setattr__(
            self,
            "current_fingerprints",
            MappingProxyType(fingerprints),
        )
        object.__setattr__(self, "added", added)
        object.__setattr__(self, "changed", changed)
        object.__setattr__(self, "deleted", deleted)
        object.__setattr__(self, "unchanged", unchanged)


def _validated_base_refs(
    refs: Iterable[StoredSegmentRef],
) -> dict[str, StoredSegmentRef]:
    if isinstance(refs, (str, bytes, bytearray)):
        raise TypeError("base_refs must be an iterable of StoredSegmentRef values")
    try:
        values = tuple(refs)
    except TypeError as exc:
        raise TypeError(
            "base_refs must be an iterable of StoredSegmentRef values"
        ) from exc

    by_doc: dict[str, StoredSegmentRef] = {}
    seen_hashes: set[str] = set()
    for ref in values:
        if not isinstance(ref, StoredSegmentRef):
            raise TypeError("base_refs must contain only StoredSegmentRef values")
        doc_key = validate_doc_key(ref.doc_key)
        doc_type, slug = doc_key.split(":", 1)
        if ref.doc_type != doc_type or ref.slug != slug:
            raise ValueError(f"base ref document attestation mismatch for {doc_key}")
        segment_hash = validate_sha256(ref.segment_hash, "segment_hash digest")
        validate_sha256(ref.content_hash, "content_hash digest")
        validate_sha256(ref.segment_recipe_hash, "segment_recipe_hash digest")
        if (
            isinstance(ref.byte_size, bool)
            or not isinstance(ref.byte_size, int)
            or ref.byte_size < 0
            or ref.byte_size > MAX_U64
        ):
            raise ValueError("base ref byte_size must be a non-negative integer")
        if not isinstance(ref.path, Path):
            raise TypeError("base ref path must be a pathlib.Path")
        if doc_key in by_doc:
            raise ValueError(f"duplicate base document ref: {doc_key}")
        if segment_hash in seen_hashes:
            raise ValueError(
                f"segment_hash is attested to more than one base document: "
                f"{segment_hash}"
            )
        seen_hashes.add(segment_hash)
        by_doc[doc_key] = ref
    return {doc_key: by_doc[doc_key] for doc_key in sorted(by_doc)}


def diff_segment_inputs(
    snapshot: StableCatalogSnapshot,
    base_refs: Iterable[StoredSegmentRef],
) -> SegmentChangeSet:
    """Compare proof attestations without loading any Segment object."""

    if not isinstance(snapshot, StableCatalogSnapshot):
        raise TypeError("snapshot must be a StableCatalogSnapshot")
    proof = snapshot.validated_proof()
    raw_documents = proof["documents"]
    if not isinstance(raw_documents, Mapping):
        raise ValueError("snapshot proof documents must be a mapping")

    source_keys: set[str] = set()
    for source in snapshot.sources:
        doc_key = validate_doc_key(source.doc_key)
        if doc_key in source_keys:
            raise ValueError(f"duplicate source document: {doc_key}")
        doc_type, slug = doc_key.split(":", 1)
        if source.doc_type != doc_type or source.slug != slug:
            raise ValueError(f"source document attestation mismatch for {doc_key}")
        if source.root.resolve() != snapshot.content_dir:
            raise ValueError(f"source root does not match snapshot for {doc_key}")
        source_keys.add(doc_key)

    proof_keys = {validate_doc_key(key) for key in raw_documents}
    if proof_keys != source_keys:
        raise ValueError("snapshot proof documents do not match snapshot sources")

    current_content: dict[str, str] = {}
    current_recipe: dict[str, str] = {}
    for doc_key in sorted(proof_keys):
        entry = raw_documents[doc_key]
        if not isinstance(entry, Mapping):
            raise ValueError(f"snapshot proof entry is invalid for {doc_key}")
        current_content[doc_key] = validate_sha256(
            entry.get("content_hash"),
            f"proof[{doc_key!r}].content_hash digest",
        )
        current_recipe[doc_key] = validate_sha256(
            entry.get("segment_recipe_hash"),
            f"proof[{doc_key!r}].segment_recipe_hash digest",
        )

    base = _validated_base_refs(base_refs)
    base_keys = set(base)
    current_keys = set(current_content)
    added = current_keys - base_keys
    deleted = base_keys - current_keys
    changed: set[str] = set()
    unchanged: set[str] = set()
    for doc_key in current_keys & base_keys:
        ref = base[doc_key]
        target = changed if (
            ref.content_hash != current_content[doc_key]
            or ref.segment_recipe_hash != current_recipe[doc_key]
        ) else unchanged
        target.add(doc_key)

    return SegmentChangeSet(
        base_by_doc=base,
        current_fingerprints=current_content,
        added=tuple(added),
        changed=tuple(changed),
        deleted=tuple(deleted),
        unchanged=tuple(unchanged),
    )


__all__ = ["SegmentChangeSet", "diff_segment_inputs"]
