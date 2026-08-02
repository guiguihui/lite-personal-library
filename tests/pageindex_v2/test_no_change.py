from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.index.v2.worker as worker_module
from app.index.v2.canonical import canonical_hash, write_json_atomic
from app.index.v2.worker import run_worker


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _request(
    pageindex: Path,
    content: Path,
    job_id: str,
    mode: str,
    *,
    base_generation: str | None = None,
) -> Path:
    job_dir = pageindex / "build" / job_id
    request_path = job_dir / "request.json"
    write_json_atomic(
        request_path,
        {
            "schema_version": 1,
            "job_id": job_id,
            "mode": mode,
            "content_dir": str(content.resolve()),
            "pageindex_dir": str(pageindex.resolve()),
            "base_generation": base_generation,
        },
    )
    return request_path


def _seed_generation(pageindex: Path, content: Path) -> str:
    request = _request(pageindex, content, "idx_no_change_seed", "full")
    assert run_worker(request) == 0
    result = _read_json(request.parent / "result.json")
    assert result["outcome"] == "built"
    return str(result["generation"])


def _snapshot(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda value: value.as_posix())
        if path.is_file()
    }


def test_no_change_returns_before_loading_compiling_or_shadowing(
    tmp_path: Path,
    sample_content: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    generation = _seed_generation(pageindex, sample_content)
    objects_before = _snapshot(pageindex / "objects")
    generations_before = _snapshot(pageindex / "generations")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no-op crossed the deep incremental boundary")

    monkeypatch.setattr(worker_module, "load_segment", forbidden)
    monkeypatch.setattr(worker_module, "_compile_and_validate", forbidden)
    monkeypatch.setattr(worker_module, "_write_shadow_report", forbidden)

    request = _request(
        pageindex,
        sample_content,
        "idx_no_change_match",
        "incremental",
        base_generation=generation,
    )
    assert run_worker(request) == 0
    result = _read_json(request.parent / "result.json")

    assert result["outcome"] == "no_change"
    assert result["generation"] == generation
    assert result["base_generation"] == generation
    assert result["shadow_report"] == {
        "status": "not_run",
        "reason": "no_change",
    }
    assert result["stats"]["no_op"] is True
    assert result["stats"]["segments_loaded"] == 0
    assert result["stats"]["postings_visited"] == 0
    assert result["stats"]["generation_bytes_written"] == 0
    assert result["stats"]["deep_validation_runs"] == 0
    assert _snapshot(pageindex / "objects") == objects_before
    assert _snapshot(pageindex / "generations") == generations_before


@pytest.mark.parametrize("change", ["modify", "add", "delete"])
def test_changed_source_never_uses_no_change(
    tmp_path: Path,
    sample_content: Path,
    change: str,
) -> None:
    pageindex = tmp_path / "pageindex"
    generation = _seed_generation(pageindex, sample_content)

    if change == "modify":
        note = sample_content / "notes" / "welcome.md"
        note.write_text(
            note.read_text(encoding="utf-8") + "\nChanged content.\n",
            encoding="utf-8",
        )
    elif change == "add":
        (sample_content / "notes" / "new-note.md").write_text(
            "---\ntitle: New note\n---\n# New note\nAdded content.\n",
            encoding="utf-8",
        )
    else:
        (sample_content / "papers" / "beta" / "_index.md").unlink()

    request = _request(
        pageindex,
        sample_content,
        f"idx_changed_{change}",
        "incremental",
        base_generation=generation,
    )
    assert run_worker(request) == 0
    result = _read_json(request.parent / "result.json")
    assert result["outcome"] == "built"
    assert result["stats"]["no_op"] is False


def test_schema_2_generation_without_proof_is_upgraded_by_building(
    tmp_path: Path,
    sample_content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    generation = _seed_generation(pageindex, sample_content)
    generation_dir = pageindex / "generations" / generation
    manifest = _read_json(generation_dir / "manifest.json")

    legacy_generation = "b" * 20
    assert legacy_generation != generation
    manifest["schema_version"] = 2
    manifest["generation"] = legacy_generation
    manifest.pop("input_proof_sha256", None)
    files = manifest["files"]
    assert isinstance(files, dict)
    files.pop("input-proof.json", None)
    write_json_atomic(generation_dir / "manifest.json", manifest)
    (generation_dir / "input-proof.json").unlink()
    legacy_dir = generation_dir.with_name(legacy_generation)
    generation_dir.rename(legacy_dir)

    request = _request(
        pageindex,
        sample_content,
        "idx_schema_2_upgrade",
        "incremental",
        base_generation=legacy_generation,
    )
    assert run_worker(request) == 0
    result = _read_json(request.parent / "result.json")

    assert result["outcome"] == "built"
    assert result["generation"] != legacy_generation
    upgraded_manifest = _read_json(
        pageindex / "generations" / str(result["generation"]) / "manifest.json"
    )
    assert upgraded_manifest["schema_version"] == 3
    assert "input_proof_sha256" in upgraded_manifest


def test_self_consistent_old_recipe_falls_through_to_upgrade_build(
    tmp_path: Path,
    sample_content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    current_generation = _seed_generation(pageindex, sample_content)
    generation_dir = pageindex / "generations" / current_generation
    manifest_path = generation_dir / "manifest.json"
    proof_path = generation_dir / "input-proof.json"
    manifest = _read_json(manifest_path)
    proof = _read_json(proof_path)

    recipe = manifest["compiler_recipe"]
    assert isinstance(recipe, dict)
    recipe["body_df_min"] = int(recipe["body_df_min"]) + 1
    old_recipe_hash = canonical_hash(recipe)
    proof["compiler_recipe_hash"] = old_recipe_hash
    write_json_atomic(proof_path, proof)
    proof_sha256 = canonical_hash(proof)

    files = manifest["files"]
    documents = manifest["documents"]
    assert isinstance(files, dict)
    assert isinstance(documents, dict)
    proof_metadata = files["input-proof.json"]
    assert isinstance(proof_metadata, dict)
    proof_metadata["sha256"] = proof_sha256
    proof_metadata["bytes"] = len(proof_path.read_bytes())
    manifest["compiler_recipe_hash"] = old_recipe_hash
    manifest["input_proof_sha256"] = proof_sha256
    core = {
        "schema_version": 3,
        "compiler_recipe_hash": old_recipe_hash,
        "input_proof_sha256": proof_sha256,
        "documents": documents,
    }
    old_revision = canonical_hash(core)
    old_generation = old_revision[:20]
    assert old_generation != current_generation
    manifest["revision_sha256"] = old_revision
    manifest["generation"] = old_generation
    write_json_atomic(manifest_path, manifest)
    old_generation_dir = generation_dir.with_name(old_generation)
    generation_dir.rename(old_generation_dir)

    request = _request(
        pageindex,
        sample_content,
        "idx_old_recipe_upgrade",
        "incremental",
        base_generation=old_generation,
    )
    assert run_worker(request) == 0
    result = _read_json(request.parent / "result.json")

    assert result["outcome"] == "built"
    assert result["generation"] == current_generation


def test_corrupt_bound_input_proof_fails_instead_of_falling_back(
    tmp_path: Path,
    sample_content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    generation = _seed_generation(pageindex, sample_content)
    proof_path = pageindex / "generations" / generation / "input-proof.json"
    proof_path.write_text('{"schema_version":1}', encoding="utf-8")

    request = _request(
        pageindex,
        sample_content,
        "idx_corrupt_input_proof",
        "incremental",
        base_generation=generation,
    )
    assert run_worker(request) == 1
    result = _read_json(request.parent / "result.json")
    assert result["status"] == "failed"
    assert result["error_code"] == "build_failed"
