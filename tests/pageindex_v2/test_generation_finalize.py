from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

import app.index.v2.worker as worker_module
from app.index.v2.artifacts import ArtifactRef, CandidateReceipt


_GENERATION_ID = "a" * 20
_DIGEST = "b" * 64


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _candidate_receipt(candidate: Path) -> CandidateReceipt:
    candidate.mkdir()
    manifest = (
        '{"generation":"' + _GENERATION_ID + '","schema_version":3}'
    ).encode("utf-8")
    payloads = {
        "chunks.json": b"x" * (1024 * 1024 + 17),
        "trees/note.json": b'{"root":null}',
    }
    (candidate / "manifest.json").write_bytes(manifest)

    artifacts: dict[str, ArtifactRef] = {}
    for relative, payload in payloads.items():
        path = candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        artifacts[relative] = ArtifactRef(
            relative_path=relative,
            sha256=_sha256(payload),
            byte_size=len(payload),
            records=None,
        )

    return CandidateReceipt(
        candidate_dir=candidate,
        generation_id=_GENERATION_ID,
        revision_sha256=_DIGEST,
        compiler_recipe_hash="c" * 64,
        input_proof_sha256="d" * 64,
        manifest_sha256=_sha256(manifest),
        artifacts=artifacts,
        segment_refs={},
        invariants={},
    )


def _copy_generation(receipt: CandidateReceipt, generation: Path) -> None:
    shutil.copytree(receipt.candidate_dir, generation)


def test_existing_identical_generation_is_verified_without_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _candidate_receipt(tmp_path / "candidate")
    generation = tmp_path / "generations" / receipt.generation_id
    _copy_generation(receipt, generation)

    def forbidden_read_bytes(_path: Path) -> bytes:
        raise AssertionError("finalization materialized a complete file")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    result = worker_module._finalize_generation(receipt, generation)

    assert result == generation
    assert not receipt.candidate_dir.exists()
    assert (generation / "chunks.json").stat().st_size == 1024 * 1024 + 17


def test_existing_generation_with_different_manifest_preserves_candidate(
    tmp_path: Path,
) -> None:
    receipt = _candidate_receipt(tmp_path / "candidate")
    generation = tmp_path / "generations" / receipt.generation_id
    _copy_generation(receipt, generation)
    different = (
        '{"generation":"' + ("e" * 20) + '","schema_version":3}'
    ).encode("utf-8")
    (generation / "manifest.json").write_bytes(different)

    with pytest.raises(RuntimeError, match="manifest hash mismatch"):
        worker_module._finalize_generation(receipt, generation)

    assert receipt.candidate_dir.is_dir()


def test_existing_generation_with_missing_artifact_preserves_candidate(
    tmp_path: Path,
) -> None:
    receipt = _candidate_receipt(tmp_path / "candidate")
    generation = tmp_path / "generations" / receipt.generation_id
    _copy_generation(receipt, generation)
    (generation / "trees" / "note.json").unlink()

    with pytest.raises(RuntimeError, match="file set mismatch"):
        worker_module._finalize_generation(receipt, generation)

    assert receipt.candidate_dir.is_dir()


def test_existing_generation_with_truncated_artifact_preserves_candidate(
    tmp_path: Path,
) -> None:
    receipt = _candidate_receipt(tmp_path / "candidate")
    generation = tmp_path / "generations" / receipt.generation_id
    _copy_generation(receipt, generation)
    (generation / "chunks.json").write_bytes(b"truncated")

    with pytest.raises(RuntimeError, match="artifact size mismatch"):
        worker_module._finalize_generation(receipt, generation)

    assert receipt.candidate_dir.is_dir()


def test_existing_generation_with_same_size_corruption_preserves_candidate(
    tmp_path: Path,
) -> None:
    receipt = _candidate_receipt(tmp_path / "candidate")
    generation = tmp_path / "generations" / receipt.generation_id
    _copy_generation(receipt, generation)
    artifact = generation / "chunks.json"
    artifact.write_bytes(b"z" * artifact.stat().st_size)

    with pytest.raises(RuntimeError, match="artifact hash mismatch"):
        worker_module._finalize_generation(receipt, generation)

    assert receipt.candidate_dir.is_dir()


def test_concurrent_identical_generation_wins_and_candidate_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _candidate_receipt(tmp_path / "candidate")
    generation = tmp_path / "generations" / receipt.generation_id

    def concurrent_install(source: Path, destination: Path) -> None:
        shutil.copytree(source, destination)
        raise FileExistsError("concurrent winner")

    monkeypatch.setattr(worker_module.os, "replace", concurrent_install)

    result = worker_module._finalize_generation(receipt, generation)

    assert result == generation
    assert generation.is_dir()
    assert not receipt.candidate_dir.exists()


def test_concurrent_different_generation_preserves_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _candidate_receipt(tmp_path / "candidate")
    generation = tmp_path / "generations" / receipt.generation_id

    def concurrent_install(source: Path, destination: Path) -> None:
        shutil.copytree(source, destination)
        (destination / "manifest.json").write_bytes(b'{"different":true}')
        raise FileExistsError("concurrent winner")

    monkeypatch.setattr(worker_module.os, "replace", concurrent_install)

    with pytest.raises(RuntimeError, match="concurrent generation differs"):
        worker_module._finalize_generation(receipt, generation)

    assert generation.is_dir()
    assert receipt.candidate_dir.is_dir()


def test_unsafe_receipt_path_is_rejected_before_candidate_move(
    tmp_path: Path,
) -> None:
    receipt = _candidate_receipt(tmp_path / "candidate")
    artifacts = receipt.artifacts
    assert isinstance(artifacts, dict)
    reference = artifacts.pop("chunks.json")
    artifacts["../escape.json"] = reference
    generation = tmp_path / "generations" / receipt.generation_id

    with pytest.raises(RuntimeError, match="unsafe Generation path"):
        worker_module._finalize_generation(receipt, generation)

    assert receipt.candidate_dir.is_dir()
    assert not generation.exists()
