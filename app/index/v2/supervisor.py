"""Launch and supervise short-lived PageIndex v2 worker processes."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Sequence

from .canonical import canonical_bytes, canonical_hash, write_json_atomic
from .protocol import (
    EXIT_BUILD_FAILED,
    EXIT_CANCELLED,
    EXIT_INVALID_REQUEST,
    EXIT_SUCCESS,
    BuildRequest,
    ProtocolError,
    VALID_BUILD_MODES,
    VALID_BUILD_OUTCOMES,
    read_json_object,
)


class WorkerProcessError(RuntimeError):
    """The worker process failed to produce a trustworthy result."""


_GENERATION_ID_RE = re.compile(r"^[0-9a-f]{20}$")


def _verify_success_result(
    result: dict[str, object],
    request: BuildRequest,
    pageindex_dir: Path,
) -> None:
    """Verify the worker identity and immutable Generation on disk."""

    expected_identity = {
        "schema_version": request.schema_version,
        "job_id": request.job_id,
        "mode": request.mode,
        "base_generation": request.base_generation,
    }
    for field, expected in expected_identity.items():
        if field not in result or result[field] != expected:
            raise WorkerProcessError(
                f"worker result {field} {result.get(field)!r} "
                f"does not match request {expected!r}"
            )

    outcome = result.get("outcome")
    if outcome not in VALID_BUILD_OUTCOMES:
        raise WorkerProcessError(
            f"worker returned unknown build outcome {outcome!r}"
        )

    generation = result.get("generation")
    if not isinstance(generation, str) or not _GENERATION_ID_RE.fullmatch(
        generation
    ):
        raise WorkerProcessError(
            f"worker returned unsafe generation ID {generation!r}"
        )

    if outcome == "no_change" and (
        request.mode != "incremental"
        or request.base_generation is None
        or generation != request.base_generation
    ):
        raise WorkerProcessError(
            "no_change requires incremental mode and the unchanged base Generation"
        )

    generation_dir_value = result.get("generation_dir")
    if not isinstance(generation_dir_value, str) or not generation_dir_value:
        raise WorkerProcessError("worker result generation_dir is missing")
    generation_dir = Path(generation_dir_value)
    expected_dir = pageindex_dir / "generations" / generation
    if not generation_dir.is_absolute() or generation_dir != expected_dir:
        raise WorkerProcessError(
            f"worker result generation_dir {generation_dir} does not equal "
            f"{expected_dir}"
        )
    if not generation_dir.is_dir():
        raise WorkerProcessError(
            f"worker generation_dir does not exist: {generation_dir}"
        )

    try:
        generation_root = (pageindex_dir / "generations").resolve(strict=True)
        resolved_generation = generation_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkerProcessError(
            f"cannot resolve worker generation_dir: {exc}"
        ) from exc
    if (
        resolved_generation.parent != generation_root
        or resolved_generation.name != generation
    ):
        raise WorkerProcessError(
            f"worker generation_dir escapes generation root: {generation_dir}"
        )

    manifest_path = generation_dir / "manifest.json"
    try:
        manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise WorkerProcessError(
            f"worker generation manifest is unavailable: {manifest_path}: {exc}"
        ) from exc
    try:
        manifest_value = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProcessError(
            f"worker generation manifest is invalid JSON: {manifest_path}"
        ) from exc
    if not isinstance(manifest_value, dict):
        raise WorkerProcessError(
            f"worker generation manifest must be an object: {manifest_path}"
        )
    try:
        canonical_manifest = canonical_bytes(manifest_value)
    except (TypeError, ValueError) as exc:
        raise WorkerProcessError(
            f"worker generation manifest cannot be canonicalized: {manifest_path}"
        ) from exc
    if manifest_raw != canonical_manifest:
        raise WorkerProcessError(
            f"worker generation manifest is not canonical JSON: {manifest_path}"
        )
    if manifest_value.get("generation") != generation:
        raise WorkerProcessError(
            "worker generation manifest ID does not match result generation"
        )

    manifest_sha256 = result.get("manifest_sha256")
    actual_sha256 = canonical_hash(manifest_value)
    if manifest_sha256 != actual_sha256:
        raise WorkerProcessError(
            f"worker manifest hash {manifest_sha256!r} does not match "
            f"{actual_sha256}"
        )


def verify_worker_completion(
    result: dict[str, object],
    request: BuildRequest,
    pageindex_dir: Path,
    returncode: int,
) -> None:
    """Verify the process exit status and any successful Generation result."""

    expected_codes = {
        "ready_to_publish": EXIT_SUCCESS,
        "failed": (
            EXIT_INVALID_REQUEST
            if result.get("error_code") == "invalid_request"
            else EXIT_BUILD_FAILED
        ),
        "cancelled": EXIT_CANCELLED,
    }
    expected = expected_codes.get(result.get("status"))
    if expected is None:
        raise WorkerProcessError(
            f"worker returned unknown result status {result.get('status')!r}"
        )
    if returncode != expected:
        raise WorkerProcessError(
            f"worker exit code {returncode} disagrees with "
            f"result status {result.get('status')!r}"
        )
    if expected == EXIT_SUCCESS:
        _verify_success_result(result, request, Path(pageindex_dir))


def worker_command(
    request_path: Path,
    *,
    executable: str | None = None,
    frozen: bool | None = None,
) -> list[str]:
    """Return the development or PyInstaller worker launch command."""

    actual_executable = executable or sys.executable
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    request = str(Path(request_path).resolve())
    if is_frozen:
        return [actual_executable, "--pageindex-worker", request]
    return [actual_executable, "-m", "app.pageindex_worker", request]


def _current_generation(pageindex_dir: Path) -> str | None:
    pointer = pageindex_dir / "current.json"
    if not pointer.is_file():
        return None
    try:
        value = read_json_object(pointer)
    except ProtocolError:
        return None
    generation = value.get("generation")
    if not isinstance(generation, str) or not _GENERATION_ID_RE.fullmatch(generation):
        return None
    manifest = pageindex_dir / "generations" / generation / "manifest.json"
    return generation if manifest.is_file() else None


def _latest_shadow_generation(pageindex_dir: Path) -> str | None:
    current = _current_generation(pageindex_dir)
    if current is not None:
        return current

    root = pageindex_dir / "generations"
    if not root.is_dir():
        return None
    candidates = [
        directory
        for directory in root.iterdir()
        if (
            directory.is_dir()
            and _GENERATION_ID_RE.fullmatch(directory.name)
            and (directory / "manifest.json").is_file()
        )
    ]
    if not candidates:
        return None
    latest = max(
        candidates,
        key=lambda directory: (
            (directory / "manifest.json").stat().st_mtime_ns,
            directory.name,
        ),
    )
    return latest.name


def run_shadow_build(
    content_dir: Path,
    pageindex_dir: Path,
    mode: str,
    *,
    base_generation: str | None = None,
) -> dict[str, object]:
    """Run one worker subprocess and return its final result payload."""

    content = Path(content_dir).resolve()
    pageindex = Path(pageindex_dir).resolve()
    if mode in {"incremental", "recompile"} and base_generation is None:
        latest_generation = _latest_shadow_generation(pageindex)
        if latest_generation is not None:
            base_generation = latest_generation
        elif mode == "recompile":
            raise ProtocolError(
                "recompile mode requires base_generation and no generation exists"
            )

    job_id = f"idx_{uuid.uuid4().hex}"
    job_dir = pageindex / "build" / job_id
    request_path = job_dir / "request.json"
    request = BuildRequest.from_dict(
        {
            "schema_version": 1,
            "job_id": job_id,
            "mode": mode,
            "content_dir": str(content),
            "pageindex_dir": str(pageindex),
            "base_generation": base_generation,
        }
    )
    write_json_atomic(request_path, request.as_dict())

    project_root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        worker_command(request_path),
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    result_path = job_dir / "result.json"
    if not result_path.is_file():
        diagnostic = completed.stderr.strip() or completed.stdout.strip()
        raise WorkerProcessError(
            f"worker exited {completed.returncode} without result.json"
            + (f": {diagnostic}" if diagnostic else "")
        )

    result = read_json_object(result_path)
    verify_worker_completion(result, request, pageindex, completed.returncode)

    return {**result, "worker_exit_code": completed.returncode}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a PageIndex v2 shadow generation in a worker process."
    )
    parser.add_argument("mode", choices=sorted(VALID_BUILD_MODES))
    parser.add_argument("--content", required=True, type=Path)
    parser.add_argument("--pageindex", required=True, type=Path)
    parser.add_argument("--base-generation")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point used by development and diagnostics."""

    arguments = _parser().parse_args(argv)
    try:
        result = run_shadow_build(
            arguments.content,
            arguments.pageindex,
            arguments.mode,
            base_generation=arguments.base_generation,
        )
    except (ProtocolError, WorkerProcessError) as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False))
        return EXIT_INVALID_REQUEST

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(result["worker_exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "WorkerProcessError",
    "main",
    "run_shadow_build",
    "worker_command",
    "verify_worker_completion",
]
