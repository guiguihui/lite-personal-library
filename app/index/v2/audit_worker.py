"""Run an explicit PageIndex Deep audit in one isolated child process.

This module deliberately provides orchestration primitives only.  Callers may
start, wait for, or cancel one audit; no timer, scheduler, publish operation, or
``current.json`` update lives here.  Supported triggers are therefore explicit
ones such as CI, a recipe/schema migration, a manual audit, or a future
deterministic sampling escalation.

Semantic validation failures are returned as the original ``ValidationReport``
from :func:`validate_candidate_deep`.  Failures to launch, communicate with, or
trust the child process instead use the separate ``audit_error`` result channel
and never synthesize index-corruption errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, Any

from .canonical import canonical_bytes
from .validator import ValidationReport, validate_candidate_deep


AUDIT_PROTOCOL_SCHEMA_VERSION = 1
_AUDIT_OPERATION = "validate_candidate_deep"
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_DIAGNOSTIC_CHARS = 2000
_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class AuditProtocolError(ValueError):
    """A Deep-audit request or result violates the subprocess protocol."""


class AuditStatus(str, Enum):
    """Terminal states exposed by the parent-side audit API."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    AUDIT_ERROR = "audit_error"


@dataclass(frozen=True, slots=True)
class AuditError:
    """An orchestration failure, separate from semantic validation errors."""

    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class AuditRequest:
    """Strict JSON request for one Deep audit child."""

    schema_version: int
    request_id: str
    operation: str
    candidate_dir: Path
    pageindex_dir: Path

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "AuditRequest":
        if not isinstance(value, Mapping):
            raise AuditProtocolError("request must be a JSON object")
        expected_fields = {
            "schema_version",
            "request_id",
            "operation",
            "candidate_dir",
            "pageindex_dir",
        }
        if set(value) != expected_fields:
            raise AuditProtocolError("request fields do not match protocol schema")
        schema_version = value.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != AUDIT_PROTOCOL_SCHEMA_VERSION
        ):
            raise AuditProtocolError(
                f"schema_version must equal {AUDIT_PROTOCOL_SCHEMA_VERSION}"
            )
        request_id = value.get("request_id")
        if (
            not isinstance(request_id, str)
            or not _SAFE_REQUEST_ID_RE.fullmatch(request_id)
        ):
            raise AuditProtocolError("request_id contains unsafe characters")
        operation = value.get("operation")
        if operation != _AUDIT_OPERATION:
            raise AuditProtocolError(f"operation must equal {_AUDIT_OPERATION!r}")
        candidate_dir = _absolute_path(value.get("candidate_dir"), "candidate_dir")
        pageindex_dir = _absolute_path(value.get("pageindex_dir"), "pageindex_dir")
        return cls(
            schema_version=schema_version,
            request_id=request_id,
            operation=operation,
            candidate_dir=candidate_dir,
            pageindex_dir=pageindex_dir,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "operation": self.operation,
            "candidate_dir": str(self.candidate_dir),
            "pageindex_dir": str(self.pageindex_dir),
        }


@dataclass(frozen=True, slots=True)
class AuditResult:
    """One terminal audit result, including exactly one failure channel."""

    schema_version: int
    request_id: str
    status: AuditStatus
    audit_pid: int | None
    validation: ValidationReport | None
    audit_error: AuditError | None

    @property
    def ok(self) -> bool:
        """Whether the child completed and the candidate passed Deep validation."""

        return bool(
            self.status is AuditStatus.COMPLETED
            and self.validation is not None
            and self.validation.ok
        )

    @property
    def error_codes(self) -> tuple[str, ...]:
        """Expose only validator codes; orchestration errors never appear here."""

        if self.validation is None:
            return ()
        return self.validation.error_codes

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "status": self.status.value,
            "audit_pid": self.audit_pid,
            "validation": (
                None if self.validation is None else self.validation.as_dict()
            ),
            "audit_error": (
                None if self.audit_error is None else self.audit_error.as_dict()
            ),
        }


def _absolute_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AuditProtocolError(f"{field} must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        raise AuditProtocolError(f"{field} must be an absolute path")
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise AuditProtocolError(f"cannot resolve {field}: {exc}") from exc


def _diagnostic(value: object) -> str:
    text = str(value).strip()
    if len(text) > _MAX_DIAGNOSTIC_CHARS:
        return text[-_MAX_DIAGNOSTIC_CHARS:]
    return text


def _audit_error_result(
    request_id: str,
    audit_pid: int | None,
    code: str,
    message: object,
) -> AuditResult:
    return AuditResult(
        schema_version=AUDIT_PROTOCOL_SCHEMA_VERSION,
        request_id=request_id,
        status=AuditStatus.AUDIT_ERROR,
        audit_pid=audit_pid,
        validation=None,
        audit_error=AuditError(code=code, message=_diagnostic(message)),
    )


def _cancelled_result(request_id: str, audit_pid: int | None) -> AuditResult:
    return AuditResult(
        schema_version=AUDIT_PROTOCOL_SCHEMA_VERSION,
        request_id=request_id,
        status=AuditStatus.CANCELLED,
        audit_pid=audit_pid,
        validation=None,
        audit_error=None,
    )


def _validation_from_dict(value: object) -> ValidationReport:
    if not isinstance(value, Mapping):
        raise AuditProtocolError("validation must be an object")
    if set(value) != {"ok", "errors", "warnings"}:
        raise AuditProtocolError("validation fields do not match protocol schema")
    ok = value.get("ok")
    errors = value.get("errors")
    warnings = value.get("warnings")
    if not isinstance(ok, bool):
        raise AuditProtocolError("validation.ok must be a boolean")
    if (
        not isinstance(errors, list)
        or not all(isinstance(item, str) for item in errors)
    ):
        raise AuditProtocolError("validation.errors must be an array of strings")
    if (
        not isinstance(warnings, list)
        or not all(isinstance(item, str) for item in warnings)
    ):
        raise AuditProtocolError("validation.warnings must be an array of strings")
    if ok != (not errors):
        raise AuditProtocolError("validation.ok disagrees with validation.errors")
    return ValidationReport(ok=ok, errors=tuple(errors), warnings=tuple(warnings))


def _audit_error_from_dict(value: object) -> AuditError:
    if not isinstance(value, Mapping) or set(value) != {"code", "message"}:
        raise AuditProtocolError("audit_error fields do not match protocol schema")
    code = value.get("code")
    message = value.get("message")
    if not isinstance(code, str) or not code:
        raise AuditProtocolError("audit_error.code must be a non-empty string")
    if not isinstance(message, str):
        raise AuditProtocolError("audit_error.message must be a string")
    return AuditError(code=code, message=message)


def _result_from_dict(
    value: object,
    *,
    expected_request_id: str,
    expected_pid: int,
) -> AuditResult:
    if not isinstance(value, Mapping):
        raise AuditProtocolError("result must be a JSON object")
    expected_fields = {
        "schema_version",
        "request_id",
        "status",
        "audit_pid",
        "validation",
        "audit_error",
    }
    if set(value) != expected_fields:
        raise AuditProtocolError("result fields do not match protocol schema")
    schema_version = value.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != AUDIT_PROTOCOL_SCHEMA_VERSION
    ):
        raise AuditProtocolError(
            f"result schema_version must equal {AUDIT_PROTOCOL_SCHEMA_VERSION}"
        )
    if value.get("request_id") != expected_request_id:
        raise AuditProtocolError("result request_id does not match request")
    audit_pid = value.get("audit_pid")
    if (
        isinstance(audit_pid, bool)
        or not isinstance(audit_pid, int)
        or audit_pid <= 0
        or audit_pid != expected_pid
    ):
        raise AuditProtocolError("result audit_pid does not match child process")
    status_value = value.get("status")
    try:
        status = AuditStatus(status_value)
    except (TypeError, ValueError) as exc:
        raise AuditProtocolError(f"unknown result status {status_value!r}") from exc
    if status is AuditStatus.CANCELLED:
        raise AuditProtocolError("child cannot claim parent-side cancellation")
    if status is AuditStatus.COMPLETED:
        if value.get("audit_error") is not None:
            raise AuditProtocolError("completed result contains audit_error")
        validation = _validation_from_dict(value.get("validation"))
        audit_error = None
    else:
        if value.get("validation") is not None:
            raise AuditProtocolError("audit_error result contains validation")
        validation = None
        audit_error = _audit_error_from_dict(value.get("audit_error"))
    return AuditResult(
        schema_version=schema_version,
        request_id=expected_request_id,
        status=status,
        audit_pid=audit_pid,
        validation=validation,
        audit_error=audit_error,
    )


class AuditHandle:
    """Parent-side handle for one running or failed-to-launch audit."""

    def __init__(
        self,
        request_id: str,
        process: subprocess.Popen[bytes] | None,
        initial_result: AuditResult | None = None,
    ) -> None:
        self.request_id = request_id
        self._process = process
        self._pid = None if process is None else process.pid
        self._result = initial_result

    @property
    def pid(self) -> int | None:
        return self._pid

    def poll(self) -> AuditResult | None:
        """Return the terminal result if available without blocking."""

        if self._result is not None:
            return self._result
        if self._process is None or self._process.poll() is None:
            return None
        return self.wait(timeout=0)

    def wait(self, timeout: float | None = None) -> AuditResult:
        """Wait for completion, leaving the child running if timeout expires."""

        if self._result is not None:
            return self._result
        process = self._process
        if process is None:  # defensive; launch failures always install a result
            self._result = _audit_error_result(
                self.request_id,
                self._pid,
                "launch_failed",
                "audit process was not created",
            )
            return self._result
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"Deep audit process {process.pid} did not finish in time"
            ) from exc
        self._result = self._interpret_process_result(stdout, stderr)
        return self._result

    def cancel(self, timeout: float = 2.0) -> AuditResult:
        """Terminate a running audit and reap it, escalating to kill on timeout."""

        if self._result is not None:
            return self._result
        process = self._process
        if process is None:
            return self.wait(timeout=0)
        if process.poll() is not None:
            return self.wait(timeout=0)
        try:
            process.terminate()
            try:
                process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        except OSError as exc:
            try:
                if process.poll() is None:
                    process.kill()
                process.communicate()
            except OSError:
                pass
            self._result = _audit_error_result(
                self.request_id,
                self._pid,
                "cancel_failed",
                exc,
            )
            return self._result
        self._result = _cancelled_result(self.request_id, self._pid)
        return self._result

    def _interpret_process_result(
        self,
        stdout: bytes,
        stderr: bytes,
    ) -> AuditResult:
        process = self._process
        assert process is not None
        if process.returncode != 0:
            diagnostic = _diagnostic(
                stderr.decode("utf-8", errors="replace")
                or stdout.decode("utf-8", errors="replace")
                or f"child exited with code {process.returncode}"
            )
            return _audit_error_result(
                self.request_id,
                self._pid,
                "process_failed",
                f"child exited with code {process.returncode}: {diagnostic}",
            )
        try:
            payload = json.loads(stdout.decode("utf-8"))
            return _result_from_dict(
                payload,
                expected_request_id=self.request_id,
                expected_pid=process.pid,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, AuditProtocolError) as exc:
            diagnostic = _diagnostic(
                stderr.decode("utf-8", errors="replace")
                or stdout.decode("utf-8", errors="replace")
            )
            message = str(exc)
            if diagnostic:
                message = f"{message}: {diagnostic}"
            return _audit_error_result(
                self.request_id,
                self._pid,
                "protocol_error",
                message,
            )


def audit_worker_command(*, executable: str | None = None) -> list[str]:
    """Return the development command for one Deep audit child."""

    return [
        executable or sys.executable,
        "-m",
        "app.index.v2.audit_worker",
        "--child",
    ]


def _close_stream(stream: IO[bytes] | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        pass


def _reap_failed_request_write(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if process.poll() is None:
                process.kill()
            process.communicate()
        except OSError:
            pass


def start_deep_audit(
    candidate_dir: Path,
    pageindex_dir: Path,
    *,
    executable: str | None = None,
) -> AuditHandle:
    """Start one Deep audit and immediately return its parent-side handle."""

    request_id = f"audit_{uuid.uuid4().hex}"
    try:
        request = AuditRequest.from_dict(
            {
                "schema_version": AUDIT_PROTOCOL_SCHEMA_VERSION,
                "request_id": request_id,
                "operation": _AUDIT_OPERATION,
                "candidate_dir": str(Path(candidate_dir).resolve()),
                "pageindex_dir": str(Path(pageindex_dir).resolve()),
            }
        )
        request_bytes = canonical_bytes(request.as_dict()) + b"\n"
        project_root = Path(__file__).resolve().parents[3]
        process = subprocess.Popen(
            audit_worker_command(executable=executable),
            cwd=project_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        result = _audit_error_result(
            request_id,
            None,
            "launch_failed",
            exc,
        )
        return AuditHandle(request_id, None, result)

    try:
        assert process.stdin is not None
        process.stdin.write(request_bytes)
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
    except OSError as exc:
        _close_stream(process.stdin)
        process.stdin = None
        _reap_failed_request_write(process)
        result = _audit_error_result(
            request_id,
            process.pid,
            "request_write_failed",
            exc,
        )
        return AuditHandle(request_id, process, result)
    return AuditHandle(request_id, process)


def run_deep_audit(
    candidate_dir: Path,
    pageindex_dir: Path,
    *,
    executable: str | None = None,
    timeout: float | None = None,
) -> AuditResult:
    """Start and synchronously wait for one explicit Deep audit."""

    return start_deep_audit(
        candidate_dir,
        pageindex_dir,
        executable=executable,
    ).wait(timeout=timeout)


def _read_request(stream: IO[bytes]) -> AuditRequest:
    raw = stream.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        raise AuditProtocolError("request exceeds byte limit")
    try:
        value: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditProtocolError(f"request is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise AuditProtocolError("request must be a JSON object")
    return AuditRequest.from_dict(value)


def _child_result(request: AuditRequest) -> AuditResult:
    try:
        validation = validate_candidate_deep(
            request.candidate_dir,
            request.pageindex_dir,
        )
    except BaseException as exc:
        return _audit_error_result(
            request.request_id,
            os.getpid(),
            "audit_failed",
            f"{type(exc).__name__}: {exc}",
        )
    return AuditResult(
        schema_version=AUDIT_PROTOCOL_SCHEMA_VERSION,
        request_id=request.request_id,
        status=AuditStatus.COMPLETED,
        audit_pid=os.getpid(),
        validation=validation,
        audit_error=None,
    )


def _best_effort_request_id(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unknown"
    if isinstance(value, Mapping):
        request_id = value.get("request_id")
        if isinstance(request_id, str) and _SAFE_REQUEST_ID_RE.fullmatch(request_id):
            return request_id
    return "unknown"


def _run_child() -> int:
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    try:
        if len(raw) > _MAX_REQUEST_BYTES:
            raise AuditProtocolError("request exceeds byte limit")
        try:
            value: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuditProtocolError(
                f"request is not valid UTF-8 JSON: {exc}"
            ) from exc
        if not isinstance(value, Mapping):
            raise AuditProtocolError("request must be a JSON object")
        request = AuditRequest.from_dict(value)
    except AuditProtocolError as exc:
        result = _audit_error_result(
            _best_effort_request_id(raw),
            os.getpid(),
            "protocol_error",
            exc,
        )
    else:
        result = _child_result(request)
    sys.stdout.buffer.write(canonical_bytes(result.as_dict()) + b"\n")
    sys.stdout.buffer.flush()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one explicit PageIndex v2 Deep audit child."
    )
    parser.add_argument("--child", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point used only by :func:`start_deep_audit`."""

    _parser().parse_args(argv)
    return _run_child()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUDIT_PROTOCOL_SCHEMA_VERSION",
    "AuditError",
    "AuditHandle",
    "AuditProtocolError",
    "AuditRequest",
    "AuditResult",
    "AuditStatus",
    "audit_worker_command",
    "main",
    "run_deep_audit",
    "start_deep_audit",
]
