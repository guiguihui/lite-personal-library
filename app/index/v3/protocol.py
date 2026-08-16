"""Strict JSON-line protocol for short-lived PageIndex v3 workers.

The protocol deliberately carries the trusted parent manifest digests into the
child request.  A worker result therefore cannot replace a self-consistent
Generation or Search View and ask the supervisor to trust the replacement.
Neither this module nor the worker is a publisher; publication remains a
separate compare-and-swap concern.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.index.v2.canonical import canonical_bytes

from .models import MAX_U64, validate_sha256


PROTOCOL_NAME = "pageindex-v3-worker"
PROTOCOL_VERSION = 1
PROTOCOL_SCHEMA_VERSION = PROTOCOL_VERSION
MAX_JSON_LINE_BYTES = 1024 * 1024

EXIT_SUCCESS = 0
EXIT_BUILD_FAILED = 1
EXIT_INVALID_REQUEST = 2
EXIT_CANCELLED = 3

VALID_BUILD_MODES = frozenset({"incremental", "optimize"})
VALID_LEGACY_EXPORTS = frozenset({"none", "full"})
VALID_RESULT_STATES = frozenset(
    {"no_op", "ready_to_publish", "failed", "cancelled"}
)

BuildMode = Literal["incremental", "optimize"]
LegacyExportMode = Literal["none", "full"]
ResultState = Literal["no_op", "ready_to_publish", "failed", "cancelled"]

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_ERROR_MESSAGE_CHARS = 4000


class ProtocolError(ValueError):
    """A PageIndex v3 worker request or result violates protocol v1."""


def _strict_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{field} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise ProtocolError(f"{field} keys must be strings")
    return value


def _strict_keys(
    value: Mapping[str, object], expected: set[str], field: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProtocolError(
            f"{field} fields do not match protocol schema "
            f"(missing={missing}, extra={extra})"
        )


def _protocol_header(value: Mapping[str, object], field: str) -> None:
    if value.get("protocol") != PROTOCOL_NAME:
        raise ProtocolError(
            f"{field}.protocol must equal {PROTOCOL_NAME!r}"
        )
    version = value.get("protocol_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProtocolError(f"{field}.protocol_version must be integer 1")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"{field}.protocol_version must equal {PROTOCOL_VERSION}"
        )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{field} must be a non-empty string")
    return value


def _safe_id(value: object, field: str) -> str:
    identifier = _required_string(value, field)
    if not _SAFE_ID_RE.fullmatch(identifier) or identifier in {".", ".."}:
        raise ProtocolError(f"{field} contains unsafe characters")
    return identifier


def _digest(value: object, field: str) -> str:
    try:
        return validate_sha256(value, field)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{field} must be a lowercase SHA-256 digest") from exc


def _u64(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_U64
    ):
        raise ProtocolError(f"{field} must be an integer in [0, {MAX_U64}]")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{field} must be a boolean")
    return value


def _absolute_path(value: object, field: str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value:
        path = Path(value)
    else:
        raise ProtocolError(f"{field} must be a non-empty absolute path")
    if not path.is_absolute():
        raise ProtocolError(f"{field} must be an absolute path")
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtocolError(f"cannot resolve {field}: {exc}") from exc


def _enum(value: object, choices: frozenset[str], field: str) -> str:
    parsed = _required_string(value, field)
    if parsed not in choices:
        raise ProtocolError(f"{field} must be one of {sorted(choices)}")
    return parsed


def _expected_object_dir(pageindex_dir: Path, kind: str, identifier: str) -> Path:
    return (pageindex_dir / kind / identifier).resolve()


@dataclass(frozen=True, slots=True)
class GenerationAttestation:
    """Trusted identity, location, and manifest digest for one Generation."""

    generation: str
    generation_dir: Path
    manifest_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation", _digest(self.generation, "generation"))
        object.__setattr__(
            self,
            "generation_dir",
            _absolute_path(self.generation_dir, "generation_dir"),
        )
        object.__setattr__(
            self,
            "manifest_sha256",
            _digest(self.manifest_sha256, "generation manifest_sha256"),
        )

    @classmethod
    def from_dict(
        cls, value: object, *, pageindex_dir: Path
    ) -> "GenerationAttestation":
        raw = _strict_mapping(value, "generation attestation")
        _strict_keys(
            raw,
            {"generation", "generation_dir", "manifest_sha256"},
            "generation attestation",
        )
        result = cls(
            generation=raw["generation"],  # type: ignore[arg-type]
            generation_dir=_serialized_absolute_path(
                raw["generation_dir"], "generation_dir"
            ),
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
        )
        expected = _expected_object_dir(
            _absolute_path(pageindex_dir, "pageindex_dir"),
            "generations",
            result.generation,
        )
        if result.generation_dir != expected:
            raise ProtocolError(
                "generation_dir must equal "
                "pageindex_dir/generations/<generation>"
            )
        return result

    def as_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "generation_dir": str(self.generation_dir),
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class ViewAttestation:
    """Trusted Search View manifest digest bound to a logical Generation."""

    view_id: str
    view_dir: Path
    manifest_sha256: str
    generation: str
    generation_manifest_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "view_id", _digest(self.view_id, "view_id"))
        object.__setattr__(
            self, "view_dir", _absolute_path(self.view_dir, "view_dir")
        )
        object.__setattr__(
            self,
            "manifest_sha256",
            _digest(self.manifest_sha256, "view manifest_sha256"),
        )
        object.__setattr__(self, "generation", _digest(self.generation, "generation"))
        object.__setattr__(
            self,
            "generation_manifest_sha256",
            _digest(
                self.generation_manifest_sha256,
                "view generation_manifest_sha256",
            ),
        )

    @classmethod
    def from_dict(
        cls, value: object, *, pageindex_dir: Path
    ) -> "ViewAttestation":
        raw = _strict_mapping(value, "view attestation")
        _strict_keys(
            raw,
            {
                "view_id",
                "view_dir",
                "manifest_sha256",
                "generation",
                "generation_manifest_sha256",
            },
            "view attestation",
        )
        result = cls(
            view_id=raw["view_id"],  # type: ignore[arg-type]
            view_dir=_serialized_absolute_path(raw["view_dir"], "view_dir"),
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            generation=raw["generation"],  # type: ignore[arg-type]
            generation_manifest_sha256=raw[  # type: ignore[arg-type]
                "generation_manifest_sha256"
            ],
        )
        expected = _expected_object_dir(
            _absolute_path(pageindex_dir, "pageindex_dir"),
            "views",
            result.view_id,
        )
        if result.view_dir != expected:
            raise ProtocolError(
                "view_dir must equal pageindex_dir/views/<view_id>"
            )
        return result

    def as_dict(self) -> dict[str, object]:
        return {
            "view_id": self.view_id,
            "view_dir": str(self.view_dir),
            "manifest_sha256": self.manifest_sha256,
            "generation": self.generation,
            "generation_manifest_sha256": self.generation_manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class ParentAttestation:
    """Complete externally trusted Generation/View parent pair."""

    generation: GenerationAttestation
    view: ViewAttestation

    def __post_init__(self) -> None:
        if not isinstance(self.generation, GenerationAttestation):
            raise ProtocolError("parent.generation must be a GenerationAttestation")
        if not isinstance(self.view, ViewAttestation):
            raise ProtocolError("parent.view must be a ViewAttestation")
        if self.view.generation != self.generation.generation:
            raise ProtocolError("parent View and Generation IDs do not match")
        if (
            self.view.generation_manifest_sha256
            != self.generation.manifest_sha256
        ):
            raise ProtocolError(
                "parent View and Generation manifest attestations do not match"
            )

    @classmethod
    def from_dict(
        cls, value: object, *, pageindex_dir: Path
    ) -> "ParentAttestation":
        raw = _strict_mapping(value, "parent")
        _strict_keys(raw, {"generation", "view"}, "parent")
        return cls(
            generation=GenerationAttestation.from_dict(
                raw["generation"], pageindex_dir=pageindex_dir
            ),
            view=ViewAttestation.from_dict(
                raw["view"], pageindex_dir=pageindex_dir
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation.as_dict(),
            "view": self.view.as_dict(),
        }


def _serialized_absolute_path(value: object, field: str) -> Path:
    if not isinstance(value, str):
        raise ProtocolError(f"{field} must be a JSON string")
    return _absolute_path(value, field)


@dataclass(frozen=True, slots=True)
class BuildRequest:
    """Validated input for exactly one fresh PageIndex v3 worker process."""

    protocol: str
    protocol_version: int
    job_id: str
    mode: BuildMode
    content_dir: Path
    pageindex_dir: Path
    parent: ParentAttestation | None
    legacy_export: LegacyExportMode = "none"

    def __post_init__(self) -> None:
        if self.protocol != PROTOCOL_NAME:
            raise ProtocolError(f"protocol must equal {PROTOCOL_NAME!r}")
        if (
            isinstance(self.protocol_version, bool)
            or not isinstance(self.protocol_version, int)
            or self.protocol_version != PROTOCOL_VERSION
        ):
            raise ProtocolError(f"protocol_version must equal {PROTOCOL_VERSION}")
        object.__setattr__(self, "job_id", _safe_id(self.job_id, "job_id"))
        object.__setattr__(
            self,
            "mode",
            _enum(self.mode, VALID_BUILD_MODES, "mode"),
        )
        object.__setattr__(
            self, "content_dir", _absolute_path(self.content_dir, "content_dir")
        )
        object.__setattr__(
            self,
            "pageindex_dir",
            _absolute_path(self.pageindex_dir, "pageindex_dir"),
        )
        if self.parent is not None and not isinstance(
            self.parent, ParentAttestation
        ):
            raise ProtocolError("parent must be a ParentAttestation or null")
        object.__setattr__(
            self,
            "legacy_export",
            _enum(
                self.legacy_export,
                VALID_LEGACY_EXPORTS,
                "legacy_export",
            ),
        )
        if self.mode == "optimize" and self.parent is None:
            raise ProtocolError("optimize mode requires a complete parent pair")

        if self.parent is not None:
            expected_generation_dir = _expected_object_dir(
                self.pageindex_dir,
                "generations",
                self.parent.generation.generation,
            )
            expected_view_dir = _expected_object_dir(
                self.pageindex_dir, "views", self.parent.view.view_id
            )
            if self.parent.generation.generation_dir != expected_generation_dir:
                raise ProtocolError("parent generation_dir escapes pageindex_dir")
            if self.parent.view.view_dir != expected_view_dir:
                raise ProtocolError("parent view_dir escapes pageindex_dir")

    @classmethod
    def from_dict(cls, value: object) -> "BuildRequest":
        raw = _strict_mapping(value, "request")
        _strict_keys(
            raw,
            {
                "protocol",
                "protocol_version",
                "job_id",
                "mode",
                "content_dir",
                "pageindex_dir",
                "parent",
                "legacy_export",
            },
            "request",
        )
        _protocol_header(raw, "request")
        pageindex_dir = _serialized_absolute_path(
            raw["pageindex_dir"], "pageindex_dir"
        )
        parent_value = raw["parent"]
        parent = (
            None
            if parent_value is None
            else ParentAttestation.from_dict(
                parent_value, pageindex_dir=pageindex_dir
            )
        )
        return cls(
            protocol=raw["protocol"],  # type: ignore[arg-type]
            protocol_version=raw["protocol_version"],  # type: ignore[arg-type]
            job_id=raw["job_id"],  # type: ignore[arg-type]
            mode=raw["mode"],  # type: ignore[arg-type]
            content_dir=_serialized_absolute_path(
                raw["content_dir"], "content_dir"
            ),
            pageindex_dir=pageindex_dir,
            parent=parent,
            legacy_export=raw["legacy_export"],  # type: ignore[arg-type]
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "job_id": self.job_id,
            "mode": self.mode,
            "content_dir": str(self.content_dir),
            "pageindex_dir": str(self.pageindex_dir),
            "parent": None if self.parent is None else self.parent.as_dict(),
            "legacy_export": self.legacy_export,
        }


_METRIC_FIELDS = (
    "source_hash_ms",
    "dirty_segment_ms",
    "generation_ms",
    "delta_ms",
    "normal_validation_ms",
    "legacy_export_ms",
    "segments_rebuilt",
    "segments_deleted",
    "segments_loaded",
    "segments_loaded_peak",
    "postings_visited",
    "base_postings_scanned",
    "bytes_written",
    "legacy_compile_runs",
    "legacy_postings_visited",
    "legacy_bytes_written",
    "normal_validation_runs",
)


@dataclass(frozen=True, slots=True)
class WorkerMetrics:
    """Fixed, bounded metrics used by the P3 mechanism/performance gates."""

    source_hash_ms: int
    dirty_segment_ms: int
    generation_ms: int
    delta_ms: int
    normal_validation_ms: int
    legacy_export_ms: int
    segments_rebuilt: int
    segments_deleted: int
    segments_loaded: int
    segments_loaded_peak: int
    postings_visited: int
    base_postings_scanned: int
    bytes_written: int
    legacy_compile_runs: int
    legacy_postings_visited: int
    legacy_bytes_written: int
    normal_validation_runs: int
    compaction_recommended: bool

    def __post_init__(self) -> None:
        for field in _METRIC_FIELDS:
            object.__setattr__(self, field, _u64(getattr(self, field), field))
        object.__setattr__(
            self,
            "compaction_recommended",
            _boolean(self.compaction_recommended, "compaction_recommended"),
        )
        if self.segments_loaded_peak > self.segments_loaded:
            raise ProtocolError(
                "segments_loaded_peak must not exceed segments_loaded"
            )

    @classmethod
    def empty(cls, **overrides: object) -> "WorkerMetrics":
        """Return zero metrics, with explicit validated overrides."""

        unknown = set(overrides) - (set(_METRIC_FIELDS) | {"compaction_recommended"})
        if unknown:
            raise ProtocolError(f"unknown metric overrides: {sorted(unknown)}")
        values: dict[str, object] = {field: 0 for field in _METRIC_FIELDS}
        values["compaction_recommended"] = False
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    @classmethod
    def from_dict(cls, value: object) -> "WorkerMetrics":
        raw = _strict_mapping(value, "metrics")
        expected = set(_METRIC_FIELDS) | {"compaction_recommended"}
        _strict_keys(raw, expected, "metrics")
        return cls(**dict(raw))  # type: ignore[arg-type]

    def as_dict(self) -> dict[str, object]:
        return {
            **{field: getattr(self, field) for field in _METRIC_FIELDS},
            "compaction_recommended": self.compaction_recommended,
        }


@dataclass(frozen=True, slots=True)
class LegacyExportAttestation:
    """Attestation for an explicitly requested full legacy compatibility export."""

    generation: str
    export_id: str
    export_dir: Path
    manifest_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation", _digest(self.generation, "generation"))
        object.__setattr__(self, "export_id", _safe_id(self.export_id, "export_id"))
        object.__setattr__(
            self, "export_dir", _absolute_path(self.export_dir, "export_dir")
        )
        object.__setattr__(
            self,
            "manifest_sha256",
            _digest(self.manifest_sha256, "legacy manifest_sha256"),
        )

    @classmethod
    def from_dict(
        cls, value: object, *, pageindex_dir: Path
    ) -> "LegacyExportAttestation":
        raw = _strict_mapping(value, "legacy export attestation")
        _strict_keys(
            raw,
            {"generation", "export_id", "export_dir", "manifest_sha256"},
            "legacy export attestation",
        )
        result = cls(
            generation=raw["generation"],  # type: ignore[arg-type]
            export_id=raw["export_id"],  # type: ignore[arg-type]
            export_dir=_serialized_absolute_path(raw["export_dir"], "export_dir"),
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
        )
        expected = (
            _absolute_path(pageindex_dir, "pageindex_dir")
            / "exports"
            / "legacy"
            / result.generation
            / result.export_id
        ).resolve()
        if result.export_dir != expected:
            raise ProtocolError(
                "export_dir must equal "
                "pageindex_dir/exports/legacy/<generation>/<export_id>"
            )
        return result

    def as_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "export_id": self.export_id,
            "export_dir": str(self.export_dir),
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class WorkerError:
    code: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _safe_id(self.code, "error.code"))
        message = _required_string(self.message, "error.message")
        if len(message) > _MAX_ERROR_MESSAGE_CHARS:
            raise ProtocolError("error.message is too long")
        object.__setattr__(self, "message", message)

    @classmethod
    def from_dict(cls, value: object) -> "WorkerError":
        raw = _strict_mapping(value, "error")
        _strict_keys(raw, {"code", "message"}, "error")
        return cls(
            code=raw["code"],  # type: ignore[arg-type]
            message=raw["message"],  # type: ignore[arg-type]
        )

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Strict terminal worker result, authenticated against its request."""

    protocol: str
    protocol_version: int
    job_id: str
    mode: BuildMode
    legacy_export: LegacyExportMode
    state: ResultState
    parent: ParentAttestation | None
    generation: GenerationAttestation | None
    view: ViewAttestation | None
    legacy_export_artifact: LegacyExportAttestation | None
    metrics: WorkerMetrics
    error: WorkerError | None

    def __post_init__(self) -> None:
        if self.protocol != PROTOCOL_NAME:
            raise ProtocolError(f"protocol must equal {PROTOCOL_NAME!r}")
        if (
            isinstance(self.protocol_version, bool)
            or not isinstance(self.protocol_version, int)
            or self.protocol_version != PROTOCOL_VERSION
        ):
            raise ProtocolError(f"protocol_version must equal {PROTOCOL_VERSION}")
        object.__setattr__(self, "job_id", _safe_id(self.job_id, "job_id"))
        object.__setattr__(
            self, "mode", _enum(self.mode, VALID_BUILD_MODES, "mode")
        )
        object.__setattr__(
            self,
            "legacy_export",
            _enum(self.legacy_export, VALID_LEGACY_EXPORTS, "legacy_export"),
        )
        object.__setattr__(
            self, "state", _enum(self.state, VALID_RESULT_STATES, "state")
        )
        if self.parent is not None and not isinstance(
            self.parent, ParentAttestation
        ):
            raise ProtocolError("parent must be a ParentAttestation or null")
        if (self.generation is None) != (self.view is None):
            raise ProtocolError(
                "result must contain both generation and view attestations or neither"
            )
        if self.generation is not None and not isinstance(
            self.generation, GenerationAttestation
        ):
            raise ProtocolError("generation must be a GenerationAttestation")
        if self.view is not None and not isinstance(self.view, ViewAttestation):
            raise ProtocolError("view must be a ViewAttestation")
        if self.generation is not None and self.view is not None:
            if self.view.generation != self.generation.generation:
                raise ProtocolError("result View and Generation IDs do not match")
            if (
                self.view.generation_manifest_sha256
                != self.generation.manifest_sha256
            ):
                raise ProtocolError(
                    "result View and Generation manifest attestations do not match"
                )
        if self.legacy_export_artifact is not None and not isinstance(
            self.legacy_export_artifact, LegacyExportAttestation
        ):
            raise ProtocolError(
                "legacy_export_artifact must be a LegacyExportAttestation or null"
            )
        if not isinstance(self.metrics, WorkerMetrics):
            raise ProtocolError("metrics must be WorkerMetrics")
        if self.error is not None and not isinstance(self.error, WorkerError):
            raise ProtocolError("error must be WorkerError or null")

        successful = self.state in {"no_op", "ready_to_publish"}
        if successful:
            if self.generation is None or self.view is None:
                raise ProtocolError(
                    "successful result requires Generation and View attestations"
                )
            if self.error is not None:
                raise ProtocolError("successful result must not contain error")
            if self.metrics.base_postings_scanned != 0:
                raise ProtocolError("P3 success must not scan base postings")
        else:
            if self.generation is not None or self.view is not None:
                raise ProtocolError(
                    "failed/cancelled result must not attest publishable artifacts"
                )
            if self.legacy_export_artifact is not None:
                raise ProtocolError(
                    "failed/cancelled result must not attest a legacy export"
                )

        if self.state == "failed":
            if self.error is None:
                raise ProtocolError("failed result requires error")
        elif self.state == "cancelled":
            if self.error is not None:
                raise ProtocolError("cancelled result must not contain error")

        if self.state == "no_op":
            if self.mode != "incremental" or self.parent is None:
                raise ProtocolError(
                    "no_op requires incremental mode and a complete parent"
                )
            if (
                self.generation != self.parent.generation
                or self.view != self.parent.view
            ):
                raise ProtocolError(
                    "no_op result attestations must equal the trusted parent"
                )
            logical_work = (
                self.metrics.dirty_segment_ms,
                self.metrics.generation_ms,
                self.metrics.delta_ms,
                self.metrics.normal_validation_ms,
                self.metrics.segments_rebuilt,
                self.metrics.segments_deleted,
                self.metrics.segments_loaded,
                self.metrics.segments_loaded_peak,
                self.metrics.postings_visited,
                self.metrics.base_postings_scanned,
                self.metrics.bytes_written,
                self.metrics.normal_validation_runs,
            )
            if any(logical_work):
                raise ProtocolError("no_op result reports logical build work")

        if self.state == "ready_to_publish":
            if self.metrics.normal_validation_runs != 1:
                raise ProtocolError(
                    "ready_to_publish requires exactly one Normal validation"
                )
            if self.mode == "optimize":
                if self.parent is None:
                    raise ProtocolError("optimize requires a complete parent")
                assert self.generation is not None and self.view is not None
                if self.generation != self.parent.generation:
                    raise ProtocolError(
                        "optimize must preserve the trusted logical Generation"
                    )
                if self.view.view_id == self.parent.view.view_id:
                    raise ProtocolError("optimize must produce a new view_id")
            elif (
                self.parent is not None
                and self.generation == self.parent.generation
                and self.view == self.parent.view
            ):
                raise ProtocolError(
                    "an unchanged incremental result must use state='no_op'"
                )

        if self.legacy_export == "none":
            if self.legacy_export_artifact is not None:
                raise ProtocolError(
                    "legacy_export='none' must not return a legacy artifact"
                )
            if any(
                (
                    self.metrics.legacy_export_ms,
                    self.metrics.legacy_compile_runs,
                    self.metrics.legacy_postings_visited,
                    self.metrics.legacy_bytes_written,
                )
            ):
                raise ProtocolError(
                    "legacy_export='none' reports legacy export work"
                )
        elif successful:
            if self.legacy_export_artifact is None:
                raise ProtocolError(
                    "legacy_export='full' success requires an export attestation"
                )
            assert self.generation is not None
            if (
                self.legacy_export_artifact.generation
                != self.generation.generation
            ):
                raise ProtocolError(
                    "legacy export is not bound to the result Generation"
                )
            if self.metrics.legacy_compile_runs != 1:
                raise ProtocolError(
                    "legacy_export='full' success requires one legacy compile"
                )

    @classmethod
    def from_dict(cls, value: object, *, request: BuildRequest) -> "BuildResult":
        if not isinstance(request, BuildRequest):
            raise TypeError("request must be a BuildRequest")
        raw = _strict_mapping(value, "result")
        _strict_keys(
            raw,
            {
                "protocol",
                "protocol_version",
                "job_id",
                "mode",
                "legacy_export",
                "state",
                "parent",
                "generation",
                "view",
                "legacy_export_artifact",
                "metrics",
                "error",
            },
            "result",
        )
        _protocol_header(raw, "result")

        parent_value = raw["parent"]
        parent = (
            None
            if parent_value is None
            else ParentAttestation.from_dict(
                parent_value, pageindex_dir=request.pageindex_dir
            )
        )
        generation_value = raw["generation"]
        generation = (
            None
            if generation_value is None
            else GenerationAttestation.from_dict(
                generation_value, pageindex_dir=request.pageindex_dir
            )
        )
        view_value = raw["view"]
        view = (
            None
            if view_value is None
            else ViewAttestation.from_dict(
                view_value, pageindex_dir=request.pageindex_dir
            )
        )
        legacy_value = raw["legacy_export_artifact"]
        legacy_artifact = (
            None
            if legacy_value is None
            else LegacyExportAttestation.from_dict(
                legacy_value, pageindex_dir=request.pageindex_dir
            )
        )
        error_value = raw["error"]
        error = None if error_value is None else WorkerError.from_dict(error_value)

        result = cls(
            protocol=raw["protocol"],  # type: ignore[arg-type]
            protocol_version=raw["protocol_version"],  # type: ignore[arg-type]
            job_id=raw["job_id"],  # type: ignore[arg-type]
            mode=raw["mode"],  # type: ignore[arg-type]
            legacy_export=raw["legacy_export"],  # type: ignore[arg-type]
            state=raw["state"],  # type: ignore[arg-type]
            parent=parent,
            generation=generation,
            view=view,
            legacy_export_artifact=legacy_artifact,
            metrics=WorkerMetrics.from_dict(raw["metrics"]),
            error=error,
        )
        if result.job_id != request.job_id:
            raise ProtocolError("result job_id does not match request")
        if result.mode != request.mode:
            raise ProtocolError("result mode does not match request")
        if result.legacy_export != request.legacy_export:
            raise ProtocolError("result legacy_export does not match request")
        if result.parent != request.parent:
            raise ProtocolError("result parent does not match trusted request parent")
        return result

    def as_dict(self) -> dict[str, object]:
        return {
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "job_id": self.job_id,
            "mode": self.mode,
            "legacy_export": self.legacy_export,
            "state": self.state,
            "parent": None if self.parent is None else self.parent.as_dict(),
            "generation": (
                None if self.generation is None else self.generation.as_dict()
            ),
            "view": None if self.view is None else self.view.as_dict(),
            "legacy_export_artifact": (
                None
                if self.legacy_export_artifact is None
                else self.legacy_export_artifact.as_dict()
            ),
            "metrics": self.metrics.as_dict(),
            "error": None if self.error is None else self.error.as_dict(),
        }


def _decode_json_line(line: bytes | str, field: str) -> Mapping[str, object]:
    if isinstance(line, bytes):
        if len(line) > MAX_JSON_LINE_BYTES:
            raise ProtocolError(f"{field} exceeds JSON-line byte limit")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"{field} must be UTF-8 JSON") from exc
    elif isinstance(line, str):
        try:
            byte_count = len(line.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ProtocolError(f"{field} must be valid UTF-8 text") from exc
        if byte_count > MAX_JSON_LINE_BYTES:
            raise ProtocolError(f"{field} exceeds JSON-line byte limit")
        text = line
    else:
        raise ProtocolError(f"{field} must be bytes or text")

    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    if not text:
        raise ProtocolError(f"{field} must contain one JSON object")
    if "\n" in text or "\r" in text:
        raise ProtocolError(f"{field} must contain exactly one JSON line")
    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError(f"{field} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ProtocolError(f"{field} contains non-finite number {value!r}")

    try:
        value: Any = json.loads(
            text,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"{field} is not valid JSON: {exc}") from exc
    return _strict_mapping(value, field)


def encode_request_line(request: BuildRequest) -> bytes:
    if not isinstance(request, BuildRequest):
        raise TypeError("request must be a BuildRequest")
    payload = canonical_bytes(request.as_dict()) + b"\n"
    if len(payload) > MAX_JSON_LINE_BYTES:
        raise ProtocolError("request exceeds JSON-line byte limit")
    return payload


def decode_request_line(line: bytes | str) -> BuildRequest:
    return BuildRequest.from_dict(_decode_json_line(line, "request"))


def encode_result_line(result: BuildResult) -> bytes:
    if not isinstance(result, BuildResult):
        raise TypeError("result must be a BuildResult")
    payload = canonical_bytes(result.as_dict()) + b"\n"
    if len(payload) > MAX_JSON_LINE_BYTES:
        raise ProtocolError("result exceeds JSON-line byte limit")
    return payload


def decode_result_line(
    line: bytes | str, *, request: BuildRequest
) -> BuildResult:
    return BuildResult.from_dict(_decode_json_line(line, "result"), request=request)


__all__ = [
    "BuildMode",
    "BuildRequest",
    "BuildResult",
    "EXIT_BUILD_FAILED",
    "EXIT_CANCELLED",
    "EXIT_INVALID_REQUEST",
    "EXIT_SUCCESS",
    "GenerationAttestation",
    "LegacyExportAttestation",
    "LegacyExportMode",
    "MAX_JSON_LINE_BYTES",
    "PROTOCOL_NAME",
    "PROTOCOL_SCHEMA_VERSION",
    "PROTOCOL_VERSION",
    "ParentAttestation",
    "ProtocolError",
    "ResultState",
    "VALID_BUILD_MODES",
    "VALID_LEGACY_EXPORTS",
    "VALID_RESULT_STATES",
    "ViewAttestation",
    "WorkerError",
    "WorkerMetrics",
    "decode_request_line",
    "decode_result_line",
    "encode_request_line",
    "encode_result_line",
]
