"""Explicit schema-3 compatibility export for one logical Generation.

This module is deliberately absent from the incremental build path.  Calling
``export_legacy_generation`` performs exactly one full P2 streaming compile,
Normal-validates its candidate, and atomically publishes it below the logical
Generation namespace.  No function in this module interprets a ``none`` mode;
the worker owns that policy decision and simply does not call this module.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from app.index.v2.artifacts import CandidateReceipt
from app.index.v2.models import CompilerRecipe
from app.index.v2.streaming_compiler import compile_generation_to_candidate
from app.index.v2.validator import ValidationReport, validate_candidate_normal

from .generation import (
    LogicalGenerationReceipt,
    _rename_no_replace,
)
from .generation_stream import validate_generation_stream
from .models import GenerationRecipe, LegacyExportRecipe, validate_sha256


_EXPORT_ID_RE = re.compile(r"^[0-9a-f]{20}$")
_STAGING_DIRECTORY = ".legacy-export-staging"
_STAGING_PREFIX = ".job-"


class LegacyExportError(RuntimeError):
    """An explicit compatibility export could not be completed safely."""


class LegacyExportValidationError(LegacyExportError):
    """The P2 Normal validator rejected a freshly compiled candidate."""

    def __init__(self, report: ValidationReport) -> None:
        self.errors = tuple(report.errors)
        self.warnings = tuple(report.warnings)
        details = "; ".join(self.errors) or "unknown validation failure"
        super().__init__(f"legacy export Normal validation failed: {details}")


class LegacyExportConflictError(LegacyExportError):
    """A deterministic export ID is occupied by different or invalid bytes."""


@dataclass(frozen=True, slots=True)
class LegacyExportReceipt:
    """Small published-export attestation and worker-facing counters."""

    logical_generation: str
    export_id: str
    export_dir: Path
    revision_sha256: str
    manifest_sha256: str
    bytes_written: int
    postings_visited: int
    reused_existing: bool = False
    warnings: tuple[str, ...] = ()
    legacy_compile_runs: int = 1

    def __post_init__(self) -> None:
        validate_sha256(self.logical_generation, "logical_generation")
        if not isinstance(self.export_id, str) or not _EXPORT_ID_RE.fullmatch(
            self.export_id
        ):
            raise ValueError(
                "export_id must be 20 lowercase hexadecimal characters"
            )
        object.__setattr__(self, "export_dir", Path(self.export_dir).absolute())
        validate_sha256(self.revision_sha256, "revision_sha256")
        validate_sha256(self.manifest_sha256, "manifest_sha256")
        if (
            isinstance(self.bytes_written, bool)
            or not isinstance(self.bytes_written, int)
            or self.bytes_written < 0
        ):
            raise ValueError("bytes_written must be an integer >= 0")
        if (
            isinstance(self.postings_visited, bool)
            or not isinstance(self.postings_visited, int)
            or self.postings_visited < 0
        ):
            raise ValueError("postings_visited must be an integer >= 0")
        if type(self.reused_existing) is not bool:
            raise TypeError("reused_existing must be a bool")
        normalized_warnings = tuple(self.warnings)
        if not all(isinstance(value, str) for value in normalized_warnings):
            raise TypeError("warnings must contain strings")
        object.__setattr__(self, "warnings", normalized_warnings)
        if type(self.legacy_compile_runs) is not int or self.legacy_compile_runs != 1:
            raise ValueError("legacy_compile_runs must equal 1")

    @property
    def counters(self) -> dict[str, int]:
        return {
            "legacy_compile_runs": self.legacy_compile_runs,
            "legacy_postings_visited": self.postings_visited,
            "legacy_bytes_written": self.bytes_written,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_generation": self.logical_generation,
            "export_id": self.export_id,
            "export_dir": str(self.export_dir),
            "revision_sha256": self.revision_sha256,
            "manifest_sha256": self.manifest_sha256,
            "bytes_written": self.bytes_written,
            "postings_visited": self.postings_visited,
            "reused_existing": self.reused_existing,
            "warnings": list(self.warnings),
            "legacy_compile_runs": self.legacy_compile_runs,
        }


def _path_is_link(metadata: os.stat_result) -> bool:
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_mask)


def _require_plain_directory(path: Path, field: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LegacyExportError(f"cannot inspect {field}: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or _path_is_link(metadata):
        raise LegacyExportError(f"{field} must be a plain directory: {path}")


def _ensure_plain_child(parent: Path, name: str, field: str) -> Path:
    _require_plain_directory(parent, f"{field} parent")
    child = parent / name
    try:
        child.mkdir(exist_ok=True)
    except OSError as exc:
        raise LegacyExportError(f"cannot create {field}: {child}") from exc
    _require_plain_directory(child, field)
    return child


def _export_parent(pageindex_dir: Path, logical_generation: str) -> Path:
    root = Path(pageindex_dir).absolute()
    _require_plain_directory(root, "PageIndex root")
    exports = _ensure_plain_child(root, "exports", "legacy export root")
    legacy = _ensure_plain_child(exports, "legacy", "legacy export namespace")
    return _ensure_plain_child(
        legacy,
        logical_generation,
        "logical Generation export namespace",
    )


def _staging_parent(pageindex_dir: Path) -> Path:
    root = Path(pageindex_dir).absolute()
    _require_plain_directory(root, "PageIndex root")
    return _ensure_plain_child(
        root,
        _STAGING_DIRECTORY,
        "legacy export staging namespace",
    )


def _remove_owned_candidate(candidate: Path, primary: BaseException | None) -> None:
    if not os.path.lexists(candidate):
        return
    try:
        metadata = candidate.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or _path_is_link(metadata):
            raise LegacyExportError(
                "refusing to clean a replaced legacy export staging path"
            )
        shutil.rmtree(candidate)
        if os.path.lexists(candidate):
            raise LegacyExportError(
                "legacy export staging directory survived cleanup"
            )
    except BaseException as cleanup_error:
        if primary is not None and hasattr(primary, "add_note"):
            primary.add_note(
                "failed to clean legacy export staging directory "
                f"{candidate}: {cleanup_error!r}"
            )
        if isinstance(cleanup_error, LegacyExportError):
            raise
        raise LegacyExportError(
            "failed to clean legacy export staging directory"
        ) from cleanup_error


def _compiler_recipe(recipe: GenerationRecipe) -> CompilerRecipe:
    layout = LegacyExportRecipe()
    return CompilerRecipe(
        body_df_min=recipe.body_df_min,
        body_df_ratio=(
            recipe.body_df_ratio_numerator / recipe.body_df_ratio_denominator
        ),
        compatibility_format_version=layout.compatibility_format_version,
        ordering_version=layout.ordering_version,
        generation_layout_version=layout.generation_layout_version,
    )


def _candidate_metric(receipt: CandidateReceipt, name: str) -> int:
    value = receipt.invariants.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LegacyExportError(f"legacy compiler did not attest {name}")
    return value


def _validation_or_raise(
    receipt: CandidateReceipt,
    pageindex_dir: Path,
) -> ValidationReport:
    report = validate_candidate_normal(receipt, pageindex_dir)
    if not isinstance(report, ValidationReport):
        raise LegacyExportError(
            "legacy Normal validator returned an invalid report"
        )
    if not report.ok:
        raise LegacyExportValidationError(report)
    return report


def _existing_export_warnings(
    destination: Path,
    receipt: CandidateReceipt,
    pageindex_dir: Path,
) -> tuple[str, ...]:
    try:
        _require_plain_directory(destination, "existing legacy export")
        existing = replace(receipt, candidate_dir=destination)
        report = validate_candidate_normal(existing, pageindex_dir)
    except BaseException as exc:
        raise LegacyExportConflictError(
            "existing legacy export is not the validated candidate"
        ) from exc
    if not isinstance(report, ValidationReport) or not report.ok:
        details = (
            "; ".join(report.errors)
            if isinstance(report, ValidationReport)
            else "invalid validator report"
        )
        raise LegacyExportConflictError(
            "existing legacy export is not the validated candidate: " + details
        )
    return tuple(report.warnings)


def _publish_or_reuse(
    candidate: Path,
    destination: Path,
    receipt: CandidateReceipt,
    pageindex_dir: Path,
) -> tuple[bool, tuple[str, ...]]:
    if os.path.lexists(destination):
        return True, _existing_export_warnings(
            destination, receipt, pageindex_dir
        )
    try:
        _rename_no_replace(candidate, destination)
    except FileExistsError:
        return True, _existing_export_warnings(
            destination, receipt, pageindex_dir
        )
    _require_plain_directory(destination, "published legacy export")
    return False, ()


def _merge_warnings(*groups: tuple[str, ...]) -> tuple[str, ...]:
    ordered: dict[str, None] = {}
    for group in groups:
        for warning in group:
            ordered.setdefault(warning, None)
    return tuple(ordered)


def export_legacy_generation(
    generation: LogicalGenerationReceipt,
    pageindex_dir: Path,
    *,
    trusted_generation: str,
    check_cancelled: Callable[[], None],
    max_run_bytes: int = 32 * 1024 * 1024,
    merge_fan_in: int = 32,
) -> LegacyExportReceipt:
    """Compile, Normal-validate, and publish one full schema-3 export.

    ``trusted_generation`` is supplied by worker state outside the Generation
    receipt being validated.  Requiring this anchor prevents a self-consistent
    replacement receipt from selecting an arbitrary export namespace.
    """

    if not isinstance(generation, LogicalGenerationReceipt):
        raise TypeError("generation must be a LogicalGenerationReceipt")
    if not callable(check_cancelled):
        raise TypeError("check_cancelled must be callable")
    trusted = validate_sha256(trusted_generation, "trusted_generation")
    if generation.generation_id != trusted:
        raise ValueError(
            "trusted_generation does not match the logical Generation receipt"
        )

    check_cancelled()
    observed_recipes: list[GenerationRecipe] = []
    refs_by_key = validate_generation_stream(
        generation,
        pageindex_dir,
        check_cancelled=check_cancelled,
        collect_refs=True,
        recipe_observer=observed_recipes.append,
    )
    if len(observed_recipes) != 1:
        raise LegacyExportError(
            "logical Generation validation did not yield exactly one recipe"
        )
    recipe = _compiler_recipe(observed_recipes[0])
    ordered_refs = tuple(refs_by_key.values())
    parent = _export_parent(Path(pageindex_dir), trusted)
    staging_parent = _staging_parent(Path(pageindex_dir))
    candidate = Path(
        tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=staging_parent)
    ).absolute()
    _require_plain_directory(candidate, "legacy export staging directory")
    candidate_owned = True

    try:
        check_cancelled()
        receipt = compile_generation_to_candidate(
            ordered_refs,
            Path(pageindex_dir),
            candidate,
            recipe,
            max_run_bytes=max_run_bytes,
            merge_fan_in=merge_fan_in,
        )
        if Path(receipt.candidate_dir).absolute() != candidate:
            raise LegacyExportError(
                "legacy compiler returned an unexpected candidate directory"
            )
        bytes_written = _candidate_metric(receipt, "generation_bytes_written")
        postings_visited = _candidate_metric(receipt, "postings_visited")
        check_cancelled()
        report = _validation_or_raise(receipt, Path(pageindex_dir))
        check_cancelled()

        destination = parent / receipt.generation_id
        reused, reuse_warnings = _publish_or_reuse(
            candidate,
            destination,
            receipt,
            Path(pageindex_dir),
        )
        if reused:
            _remove_owned_candidate(candidate, None)
        candidate_owned = False
        return LegacyExportReceipt(
            logical_generation=trusted,
            export_id=receipt.generation_id,
            export_dir=destination,
            revision_sha256=receipt.revision_sha256,
            manifest_sha256=receipt.manifest_sha256,
            bytes_written=bytes_written,
            postings_visited=postings_visited,
            reused_existing=reused,
            warnings=_merge_warnings(tuple(report.warnings), reuse_warnings),
        )
    except BaseException as exc:
        if candidate_owned:
            _remove_owned_candidate(candidate, exc)
        raise


__all__ = [
    "LegacyExportConflictError",
    "LegacyExportError",
    "LegacyExportReceipt",
    "LegacyExportValidationError",
    "export_legacy_generation",
]
