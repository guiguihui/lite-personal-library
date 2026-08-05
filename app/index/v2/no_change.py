"""Generation-bound no-change matching before any Segment is loaded."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, canonical_hash, sha256_bytes
from .source_snapshot import capture_stable_input_proof
from .input_proof import (
    INPUT_PROOF_PATH,
    validate_input_proof,
)
from .models import COMPILER_SCHEMA_VERSION, CompilerRecipe, SegmentRecipe
from .protocol import BuildRequest


@dataclass(frozen=True, slots=True)
class NoChangeMatch:
    """A validated Generation matching one stable live source capture."""

    generation_dir: Path
    manifest: dict[str, object]
    manifest_sha256: str
    document_count: int
    stabilization_attempts: int


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _read_canonical_object(
    path: Path, field: str
) -> tuple[dict[str, object], bytes]:
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {field} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    try:
        encoded = canonical_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} cannot be canonicalized: {exc}") from exc
    if raw != encoded:
        raise ValueError(f"{field} is not canonical JSON")
    return value, raw


def _live_proof(
    request: BuildRequest,
    *,
    segment_recipe_hash: str,
    compiler_recipe_hash: str,
    check_cancel: Callable[[], None],
) -> dict[str, object] | None:
    check_cancel()
    return capture_stable_input_proof(
        request.content_dir,
        segment_recipe_hash=segment_recipe_hash,
        compiler_recipe_hash=compiler_recipe_hash,
        check_cancel=check_cancel,
    )


def try_no_change(
    request: BuildRequest,
    *,
    check_cancel: Callable[[], None],
) -> NoChangeMatch | None:
    """Return a trusted match or ``None`` when a real build is required.

    Schema-2 Generations deliberately fall through to the existing build path.
    A schema-3 Generation is proof-bound, so corruption is a hard failure rather
    than a reason to silently compile from an untrusted base.
    """

    if request.mode != "incremental" or request.base_generation is None:
        return None

    generation = request.base_generation
    generation_dir = request.pageindex_dir / "generations" / generation
    manifest, manifest_raw = _read_canonical_object(
        generation_dir / "manifest.json",
        "base manifest",
    )
    schema_version = manifest.get("schema_version")
    if schema_version == 2:
        return None
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != COMPILER_SCHEMA_VERSION
    ):
        raise ValueError(
            f"unsupported base manifest schema_version: {schema_version!r}"
        )
    if manifest.get("generation") != generation:
        raise ValueError("base manifest generation does not match its directory")

    manifest_recipe = _mapping(
        manifest.get("compiler_recipe"), "base manifest compiler recipe"
    )
    manifest_recipe_hash = canonical_hash(dict(manifest_recipe))
    if manifest.get("compiler_recipe_hash") != manifest_recipe_hash:
        raise ValueError("base manifest compiler recipe hash is invalid")
    current_recipe = CompilerRecipe().as_dict()
    current_recipe_hash = canonical_hash(current_recipe)

    documents = _mapping(manifest.get("documents"), "base manifest documents")
    files = _mapping(manifest.get("files"), "base manifest files")
    proof_metadata = _mapping(
        files.get(INPUT_PROOF_PATH),
        f"base manifest files[{INPUT_PROOF_PATH!r}]",
    )
    proof, proof_raw = _read_canonical_object(
        generation_dir / INPUT_PROOF_PATH,
        "base input proof",
    )
    proof = validate_input_proof(proof)
    proof_sha256 = sha256_bytes(proof_raw)
    if proof_metadata.get("sha256") != proof_sha256:
        raise ValueError("base input proof file hash does not match manifest")
    if proof_metadata.get("bytes") != len(proof_raw):
        raise ValueError("base input proof byte count does not match manifest")
    if manifest.get("input_proof_sha256") != proof_sha256:
        raise ValueError("base input proof is not bound by the manifest")
    if proof.get("compiler_recipe_hash") != manifest_recipe_hash:
        raise ValueError("base input proof compiler recipe does not match manifest")

    proof_documents = _mapping(proof.get("documents"), "input proof documents")
    if set(proof_documents) != set(documents):
        raise ValueError("base input proof document set does not match manifest")

    core_manifest = {
        "schema_version": schema_version,
        "compiler_recipe_hash": manifest_recipe_hash,
        "input_proof_sha256": proof_sha256,
        "documents": dict(documents),
    }
    revision_sha256 = canonical_hash(core_manifest)
    if manifest.get("revision_sha256") != revision_sha256:
        raise ValueError("base manifest revision hash is invalid")
    if generation != revision_sha256[:20]:
        raise ValueError("base Generation ID is invalid")
    if manifest_recipe_hash != current_recipe_hash:
        return None

    segment_recipe_hash = canonical_hash(SegmentRecipe().as_dict())
    live = _live_proof(
        request,
        segment_recipe_hash=segment_recipe_hash,
        compiler_recipe_hash=current_recipe_hash,
        check_cancel=check_cancel,
    )
    if live is None or live != proof:
        return None

    return NoChangeMatch(
        generation_dir=generation_dir,
        manifest=manifest,
        manifest_sha256=sha256_bytes(manifest_raw),
        document_count=len(documents),
        stabilization_attempts=1,
    )


__all__ = ["NoChangeMatch", "try_no_change"]
