"""End-to-end equivalence tests for full and incremental v2 builds."""

from __future__ import annotations

import json
from pathlib import Path

from app.index.v2.canonical import write_json_atomic
from app.index.v2.worker import run_worker


def _run(
    pageindex: Path,
    content: Path,
    job_id: str,
    mode: str,
    base_generation: str | None = None,
) -> dict[str, object]:
    job_dir = pageindex / "build" / job_id
    job_dir.mkdir(parents=True)
    request = job_dir / "request.json"
    write_json_atomic(
        request,
        {
            "schema_version": 1,
            "job_id": job_id,
            "mode": mode,
            "content_dir": str(content.resolve()),
            "pageindex_dir": str(pageindex.resolve()),
            "base_generation": base_generation,
        },
    )
    assert run_worker(request) == 0
    value = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_incremental_and_full_match_after_edit_add_and_delete(
    tmp_path: Path,
    sample_content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    initial = _run(pageindex, sample_content, "idx_initial", "full")
    initial_generation = str(initial["generation"])

    note = sample_content / "notes" / "welcome.md"
    note.write_text(
        note.read_text(encoding="utf-8") + "\nMore changed searchable content.\n",
        encoding="utf-8",
    )
    edited_incremental = _run(
        pageindex,
        sample_content,
        "idx_edit_incremental",
        "incremental",
        initial_generation,
    )
    assert edited_incremental["stats"]["segments_rebuilt"] == 1
    assert edited_incremental["stats"]["segments_reused"] == 2
    edited_full = _run(pageindex, sample_content, "idx_edit_full", "full")
    assert edited_incremental["generation"] == edited_full["generation"]

    added_note = sample_content / "notes" / "second.md"
    added_note.write_text(
        "---\ntitle: Second\n---\n# Second\n" + ("added searchable text " * 8),
        encoding="utf-8",
    )
    added_incremental = _run(
        pageindex,
        sample_content,
        "idx_add_incremental",
        "incremental",
        str(edited_incremental["generation"]),
    )
    assert added_incremental["stats"]["segments_rebuilt"] == 1
    assert added_incremental["stats"]["segments_reused"] == 3
    added_full = _run(pageindex, sample_content, "idx_add_full", "full")
    assert added_incremental["generation"] == added_full["generation"]

    (sample_content / "papers" / "beta" / "_index.md").unlink()
    deleted_incremental = _run(
        pageindex,
        sample_content,
        "idx_delete_incremental",
        "incremental",
        str(added_incremental["generation"]),
    )
    assert (
        deleted_incremental["base_generation"]
        == added_incremental["generation"]
    )
    assert deleted_incremental["stats"]["segments_deleted"] == 1
    assert deleted_incremental["stats"]["segments_rebuilt"] == 0
    deleted_full = _run(pageindex, sample_content, "idx_delete_full", "full")
    assert deleted_incremental["generation"] == deleted_full["generation"]


def test_repeated_full_build_is_content_deterministic(
    tmp_path: Path,
    sample_content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    first = _run(pageindex, sample_content, "idx_first", "full")
    second = _run(pageindex, sample_content, "idx_second", "full")

    assert first["generation"] == second["generation"]
    assert first["manifest_sha256"] == second["manifest_sha256"]
