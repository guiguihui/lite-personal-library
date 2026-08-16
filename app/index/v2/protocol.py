"""Versioned task-file protocol for PageIndex v2 worker processes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, write_json_atomic


PROTOCOL_SCHEMA_VERSION = 1
VALID_BUILD_MODES = frozenset({"incremental", "full", "recompile"})
VALID_BUILD_OUTCOMES = frozenset({"built", "no_change"})

EXIT_SUCCESS = 0
EXIT_BUILD_FAILED = 1
EXIT_INVALID_REQUEST = 2
EXIT_CANCELLED = 3

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TERMINAL_STATES = frozenset({"ready_to_publish", "failed", "cancelled"})


class ProtocolError(ValueError):
    """A request or task-file payload violates the worker protocol."""


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{field} must be a non-empty string")
    return value


def _safe_id(value: object, field: str) -> str:
    identifier = _required_string(value, field)
    if not _SAFE_ID_RE.fullmatch(identifier) or identifier in {".", ".."}:
        raise ProtocolError(f"{field} contains unsafe characters")
    return identifier


def _absolute_path(value: object, field: str) -> Path:
    raw = _required_string(value, field)
    path = Path(raw)
    if not path.is_absolute():
        raise ProtocolError(f"{field} must be an absolute path")
    return path.resolve()


@dataclass(frozen=True, slots=True)
class BuildRequest:
    """Validated input for one short-lived PageIndex worker."""

    schema_version: int
    job_id: str
    mode: str
    content_dir: Path
    pageindex_dir: Path
    base_generation: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BuildRequest":
        """Parse and strictly validate a protocol-v1 request."""

        if not isinstance(value, Mapping):
            raise ProtocolError("request must be a JSON object")

        schema_version = value.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != PROTOCOL_SCHEMA_VERSION
        ):
            raise ProtocolError(
                f"schema_version must equal {PROTOCOL_SCHEMA_VERSION}"
            )

        mode = _required_string(value.get("mode"), "mode")
        if mode not in VALID_BUILD_MODES:
            raise ProtocolError(
                f"mode must be one of {sorted(VALID_BUILD_MODES)}, got {mode!r}"
            )

        base_value = value.get("base_generation")
        if base_value is None:
            base_generation = None
        else:
            base_generation = _safe_id(base_value, "base_generation")
        if mode == "recompile" and base_generation is None:
            raise ProtocolError("recompile mode requires base_generation")

        return cls(
            schema_version=schema_version,
            job_id=_safe_id(value.get("job_id"), "job_id"),
            mode=mode,
            content_dir=_absolute_path(value.get("content_dir"), "content_dir"),
            pageindex_dir=_absolute_path(
                value.get("pageindex_dir"), "pageindex_dir"
            ),
            base_generation=base_generation,
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable request payload."""

        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "mode": self.mode,
            "content_dir": str(self.content_dir),
            "pageindex_dir": str(self.pageindex_dir),
            "base_generation": self.base_generation,
        }


def read_json_object(path: Path) -> dict[str, object]:
    """Read a UTF-8 JSON object from *path*."""

    try:
        value: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"{path} must contain a JSON object")
    return value


def load_request(path: Path) -> BuildRequest:
    """Load and validate a request file."""

    return BuildRequest.from_dict(read_json_object(Path(path)))


def utc_now() -> str:
    """Return a compact UTC timestamp for mutable task diagnostics."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class TaskReporter:
    """Maintain progress, append-only events, and one final result file."""

    def __init__(self, job_dir: Path, job_id: str) -> None:
        self.job_dir = Path(job_dir)
        self.job_id = job_id
        self.sequence = 0
        self.state: str | None = None

    @property
    def cancel_path(self) -> Path:
        return self.job_dir / "cancel.request"

    def is_cancelled(self) -> bool:
        return self.cancel_path.is_file()

    def transition(self, status: str, **details: object) -> None:
        """Record the latest state atomically and append the matching event."""

        if self.state in _TERMINAL_STATES:
            raise ProtocolError(f"cannot transition from terminal state {self.state}")
        if not isinstance(status, str) or not status:
            raise ProtocolError("status must be a non-empty string")

        self.sequence += 1
        timestamp = utc_now()
        payload: dict[str, object] = {
            "schema_version": PROTOCOL_SCHEMA_VERSION,
            "job_id": self.job_id,
            "sequence": self.sequence,
            "status": status,
            "updated_at": timestamp,
            **details,
        }
        write_json_atomic(self.job_dir / "progress.json", payload)
        self._append_event(
            {
                "schema_version": PROTOCOL_SCHEMA_VERSION,
                "job_id": self.job_id,
                "sequence": self.sequence,
                "event": "state_changed",
                "status": status,
                "at": timestamp,
                **details,
            }
        )
        self.state = status

    def event(self, event: str, **details: object) -> None:
        """Append a diagnostic event without changing the current state."""

        if not isinstance(event, str) or not event:
            raise ProtocolError("event must be a non-empty string")
        self.sequence += 1
        self._append_event(
            {
                "schema_version": PROTOCOL_SCHEMA_VERSION,
                "job_id": self.job_id,
                "sequence": self.sequence,
                "event": event,
                "status": self.state,
                "at": utc_now(),
                **details,
            }
        )

    def finish(self, result: Mapping[str, object]) -> None:
        """Atomically write the authoritative terminal result."""

        status = result.get("status")
        if status not in _TERMINAL_STATES:
            raise ProtocolError(f"invalid terminal result status: {status!r}")
        if (
            status == "ready_to_publish"
            and result.get("outcome") not in VALID_BUILD_OUTCOMES
        ):
            raise ProtocolError(
                f"invalid successful build outcome: {result.get('outcome')!r}"
            )
        write_json_atomic(self.job_dir / "result.json", dict(result))

    def _append_event(self, payload: Mapping[str, object]) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        with (self.job_dir / "events.jsonl").open("ab") as stream:
            stream.write(canonical_bytes(payload))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())


__all__ = [
    "BuildRequest",
    "EXIT_BUILD_FAILED",
    "EXIT_CANCELLED",
    "EXIT_INVALID_REQUEST",
    "EXIT_SUCCESS",
    "PROTOCOL_SCHEMA_VERSION",
    "ProtocolError",
    "TaskReporter",
    "VALID_BUILD_MODES",
    "VALID_BUILD_OUTCOMES",
    "load_request",
    "read_json_object",
]
