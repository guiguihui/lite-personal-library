"""Application-level publication and lookup for the current PageIndex v3 view.

PageIndex v3 artifacts are immutable and deliberately do not contain a mutable
"latest" pointer.  The desktop application owns that final concern: after the
supervisor authenticates a successful build, it atomically publishes the exact
Generation/View pair in ``current-v3.json``.  Readers always open that pin.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.index.v2.artifacts import ArtifactRef
from app.index.v2.canonical import write_json_atomic

from .generation import (
    INPUT_PROOF_PATH,
    MANIFEST_PATH,
    LogicalGenerationReceipt,
    validate_logical_generation_manifest,
)
from .models import ViewPin
from .protocol import BuildResult, ParentAttestation, ProtocolError
from .reader import PinnedSearchView
from .view_store import load_search_view


CURRENT_POINTER = "current-v3.json"
CURRENT_SCHEMA_VERSION = 1


class CurrentViewError(ValueError):
    """The application publication pointer is absent, invalid, or stale."""


@dataclass(frozen=True, slots=True)
class CurrentView:
    parent: ParentAttestation
    generation: LogicalGenerationReceipt

    @property
    def pin(self) -> ViewPin:
        return ViewPin(
            self.parent.generation.generation,
            self.parent.view.view_id,
        )


def _artifact_ref(value: object, expected_path: str) -> ArtifactRef:
    if not isinstance(value, dict):
        raise CurrentViewError(f"{expected_path} receipt must be an object")
    expected = {"relative_path", "sha256", "byte_size", "records"}
    if set(value) != expected or value.get("relative_path") != expected_path:
        raise CurrentViewError(f"invalid {expected_path} receipt")
    try:
        return ArtifactRef(
            relative_path=value["relative_path"],
            sha256=value["sha256"],
            byte_size=value["byte_size"],
            records=value["records"],
        )
    except (TypeError, ValueError) as exc:
        raise CurrentViewError(f"invalid {expected_path} receipt: {exc}") from exc


def _receipt_from_attestation(parent: ParentAttestation) -> LogicalGenerationReceipt:
    root = parent.generation.generation_dir
    manifest_path = root / MANIFEST_PATH
    try:
        payload = manifest_path.read_bytes()
        manifest = json.loads(payload.decode("utf-8"))
        validate_logical_generation_manifest(manifest)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CurrentViewError(f"cannot load V3 Generation manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CurrentViewError("V3 Generation manifest must be an object")
    if manifest.get("generation") != parent.generation.generation:
        raise CurrentViewError("Generation manifest differs from build attestation")
    manifest_ref = ArtifactRef(
        relative_path=MANIFEST_PATH,
        sha256=parent.generation.manifest_sha256,
        byte_size=len(payload),
        records=manifest.get("document_count"),
    )
    input_proof_ref = _artifact_ref(manifest.get("input_proof"), INPUT_PROOF_PATH)
    return LogicalGenerationReceipt(
        candidate_dir=root,
        generation_id=parent.generation.generation,
        generation_recipe_hash=manifest.get("generation_recipe_hash"),
        manifest_ref=manifest_ref,
        input_proof_ref=input_proof_ref,
        document_count=manifest.get("document_count"),
    )


def _verify_artifact(root: Path, reference: ArtifactRef) -> None:
    path = root / reference.relative_path
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise CurrentViewError(
            f"cannot read published V3 artifact {reference.relative_path}: {exc}"
        ) from exc
    if size != reference.byte_size or digest.hexdigest() != reference.sha256:
        raise CurrentViewError(
            f"published V3 artifact changed: {reference.relative_path}"
        )

def _pointer_path(pageindex_dir: Path | str) -> Path:
    return Path(pageindex_dir) / CURRENT_POINTER


def publish_current(pageindex_dir: Path | str, result: BuildResult) -> CurrentView:
    """Atomically publish one supervisor-authenticated successful result."""

    if result.state not in {"ready_to_publish", "no_op"}:
        raise CurrentViewError(f"cannot publish V3 result in state {result.state!r}")
    if result.generation is None or result.view is None:
        raise CurrentViewError("successful V3 result has no Generation/View pair")
    parent = ParentAttestation(result.generation, result.view)
    receipt = _receipt_from_attestation(parent)
    current = CurrentView(parent, receipt)
    write_json_atomic(
        _pointer_path(pageindex_dir),
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "parent": parent.as_dict(),
            "generation_receipt": receipt.as_dict(),
        },
    )
    return current


def load_current(pageindex_dir: Path | str) -> CurrentView:
    """Load the exact published pair without resolving any implicit latest state."""

    root = Path(pageindex_dir).absolute()
    path = _pointer_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CurrentViewError("PageIndex v3 has not been published") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentViewError(f"cannot read PageIndex v3 publication: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "parent",
        "generation_receipt",
    }:
        raise CurrentViewError("invalid PageIndex v3 publication envelope")
    if value["schema_version"] != CURRENT_SCHEMA_VERSION:
        raise CurrentViewError("unsupported PageIndex v3 publication schema")
    try:
        parent = ParentAttestation.from_dict(value["parent"], pageindex_dir=root)
        receipt = LogicalGenerationReceipt.from_dict(
            parent.generation.generation_dir,
            value["generation_receipt"],
        )
    except (ProtocolError, TypeError, ValueError) as exc:
        raise CurrentViewError(f"invalid PageIndex v3 publication: {exc}") from exc
    if (
        receipt.generation_id != parent.generation.generation
        or receipt.manifest_ref.sha256 != parent.generation.manifest_sha256
        or parent.view.generation != receipt.generation_id
        or parent.view.generation_manifest_sha256 != receipt.manifest_ref.sha256
    ):
        raise CurrentViewError("PageIndex v3 publication attestations differ")
    try:
        _verify_artifact(receipt.candidate_dir, receipt.manifest_ref)
        _verify_artifact(receipt.candidate_dir, receipt.input_proof_ref)
        view = load_search_view(root, parent.view.view_id)
    except Exception as exc:
        raise CurrentViewError(f"published V3 Search View is invalid: {exc}") from exc
    if (
        view.generation != receipt.generation_id
        or view.generation_manifest_sha256 != receipt.manifest_ref.sha256
        or view.manifest_ref.sha256 != parent.view.manifest_sha256
    ):
        raise CurrentViewError("published V3 Search View differs from pointer")
    return CurrentView(parent, receipt)


def open_current_view(pageindex_dir: Path | str) -> PinnedSearchView:
    current = load_current(pageindex_dir)
    return PinnedSearchView.open(
        Path(pageindex_dir),
        current.pin,
        current.generation,
    )


def is_ready(pageindex_dir: Path | str) -> bool:
    try:
        load_current(pageindex_dir)
    except CurrentViewError:
        return False
    return True


def current_parent(pageindex_dir: Path | str) -> ParentAttestation | None:
    try:
        return load_current(pageindex_dir).parent
    except CurrentViewError:
        return None
