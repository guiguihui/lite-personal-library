from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.index.v2.canonical import canonical_hash
from app.index.v2.object_store import StoredSegmentRef
import app.index.v3.generation as generation_module
from app.index.v3.generation import (
    LogicalGenerationError,
    LogicalGenerationReceipt,
    build_logical_generation,
    validate_logical_generation_manifest,
)
from app.index.v3.models import MAX_U64, GenerationRecipe, logical_generation_id


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _ref(
    doc_key: str,
    *,
    segment_seed: str | None = None,
    content_seed: str | None = None,
    segment_recipe_hash: str | None = None,
) -> StoredSegmentRef:
    doc_type, slug = doc_key.split(":", 1)
    return StoredSegmentRef(
        segment_hash=_digest(segment_seed or f"segment:{doc_key}"),
        path=Path("objects") / f"{slug}.json",
        byte_size=123,
        doc_key=doc_key,
        doc_type=doc_type,
        slug=slug,
        content_hash=_digest(content_seed or f"content:{doc_key}"),
        segment_recipe_hash=segment_recipe_hash or _digest("segment-recipe"),
    )


def _proof(
    refs: tuple[StoredSegmentRef, ...],
    recipe: GenerationRecipe | None = None,
) -> dict[str, object]:
    selected_recipe = recipe or GenerationRecipe()
    return {
        "schema_version": 1,
        "compiler_recipe_hash": canonical_hash(selected_recipe.as_dict()),
        "documents": {
            ref.doc_key: {
                "content_hash": ref.content_hash,
                "segment_recipe_hash": ref.segment_recipe_hash,
            }
            for ref in reversed(refs)
        },
    }


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _assert_canonical(path: Path) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert path.read_bytes() == json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def test_build_is_order_independent_and_writes_exact_schema4_artifacts(
    tmp_path: Path,
) -> None:
    refs = (
        _ref("note:zeta"),
        _ref("book:alpha"),
        _ref("note:中文"),
    )
    recipe = GenerationRecipe(body_df_min=17)

    first = build_logical_generation(
        refs,
        _proof(refs, recipe),
        recipe,
        tmp_path / "first",
    )
    second = build_logical_generation(
        iter(reversed(refs)),
        _proof(refs, recipe),
        recipe,
        tmp_path / "second",
    )

    assert first.generation_id == second.generation_id
    assert first.generation_id == logical_generation_id(refs, recipe)
    assert len(first.generation_id) == 64
    assert first.manifest_ref.sha256 == second.manifest_ref.sha256
    assert first.input_proof_ref.sha256 == second.input_proof_ref.sha256
    assert first.document_count == 3

    manifest = _load(first.candidate_dir / "manifest.json")
    assert manifest == {
        "artifact_kind": "logical_generation",
        "document_count": 3,
        "documents": {
            ref.doc_key: ref.segment_hash
            for ref in sorted(refs, key=lambda item: item.doc_key)
        },
        "generation": first.generation_id,
        "generation_recipe": recipe.as_dict(),
        "generation_recipe_hash": canonical_hash(recipe.as_dict()),
        "input_proof": {
            "byte_size": first.input_proof_ref.byte_size,
            "records": 3,
            "relative_path": "input-proof.json",
            "sha256": first.input_proof_ref.sha256,
        },
        "schema_version": 4,
    }
    validate_logical_generation_manifest(manifest)
    _assert_canonical(first.candidate_dir / "manifest.json")
    _assert_canonical(first.candidate_dir / "input-proof.json")
    assert first.manifest_ref.sha256 == hashlib.sha256(
        (first.candidate_dir / "manifest.json").read_bytes()
    ).hexdigest()
    assert first.input_proof_ref.sha256 == hashlib.sha256(
        (first.candidate_dir / "input-proof.json").read_bytes()
    ).hexdigest()
    assert set(path.name for path in first.candidate_dir.iterdir()) == {
        "manifest.json",
        "input-proof.json",
    }


def test_receipt_is_small_strict_and_round_trips(tmp_path: Path) -> None:
    refs = (_ref("note:a"), _ref("note:b"))
    receipt = build_logical_generation(
        refs, _proof(refs), GenerationRecipe(), tmp_path / "candidate"
    )

    assert not hasattr(receipt, "manifest")
    assert not hasattr(receipt, "documents")
    round_trip = LogicalGenerationReceipt.from_dict(
        receipt.candidate_dir, receipt.as_dict()
    )
    assert round_trip == receipt

    invalid = receipt.as_dict()
    invalid["extra"] = True
    with pytest.raises(LogicalGenerationError, match="exactly"):
        LogicalGenerationReceipt.from_dict(receipt.candidate_dir, invalid)

    invalid = receipt.as_dict()
    invalid["generation"] = receipt.generation_id[:20]
    with pytest.raises(ValueError, match="64-character"):
        LogicalGenerationReceipt.from_dict(receipt.candidate_dir, invalid)


def test_artifact_receipts_enforce_p3_u64_bounds(tmp_path: Path) -> None:
    refs = (_ref("note:a"),)
    receipt = build_logical_generation(
        refs, _proof(refs), GenerationRecipe(), tmp_path / "candidate"
    )

    oversized_receipt = receipt.as_dict()
    oversized_receipt["artifacts"]["manifest"]["byte_size"] = MAX_U64 + 1
    with pytest.raises(LogicalGenerationError, match="range"):
        LogicalGenerationReceipt.from_dict(
            receipt.candidate_dir, oversized_receipt
        )

    oversized_receipt = receipt.as_dict()
    oversized_receipt["artifacts"]["input_proof"]["records"] = MAX_U64 + 1
    with pytest.raises(LogicalGenerationError, match="range"):
        LogicalGenerationReceipt.from_dict(
            receipt.candidate_dir, oversized_receipt
        )

    oversized_manifest = _load(receipt.candidate_dir / "manifest.json")
    oversized_manifest["input_proof"]["byte_size"] = MAX_U64 + 1
    with pytest.raises(LogicalGenerationError, match="range"):
        validate_logical_generation_manifest(oversized_manifest)

def test_proof_is_authenticated_but_excluded_from_logical_identity(
    tmp_path: Path,
) -> None:
    original = _ref("note:a", segment_seed="same", content_seed="content-a")
    reattested = replace(original, content_hash=_digest("content-b"))
    recipe = GenerationRecipe()

    first = build_logical_generation(
        (original,), _proof((original,), recipe), recipe, tmp_path / "first"
    )
    second = build_logical_generation(
        (reattested,),
        _proof((reattested,), recipe),
        recipe,
        tmp_path / "second",
    )

    assert first.generation_id == second.generation_id
    assert first.input_proof_ref.sha256 != second.input_proof_ref.sha256
    assert first.manifest_ref.sha256 != second.manifest_ref.sha256


def test_recipe_semantics_change_generation_identity(tmp_path: Path) -> None:
    refs = (_ref("note:a"),)
    default = GenerationRecipe()
    changed = GenerationRecipe(body_df_min=default.body_df_min + 1)

    first = build_logical_generation(
        refs, _proof(refs, default), default, tmp_path / "default"
    )
    second = build_logical_generation(
        refs, _proof(refs, changed), changed, tmp_path / "changed"
    )
    assert first.generation_id != second.generation_id


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda proof: proof.update(schema_version=2),
            "schema_version",
        ),
        (
            lambda proof: proof.update(schema_version=True),
            "schema_version",
        ),
        (
            lambda proof: proof.update(extra=True),
            "exactly",
        ),
        (
            lambda proof: proof.update(compiler_recipe_hash=_digest("wrong")),
            "does not match Generation recipe",
        ),
        (
            lambda proof: proof["documents"]["note:a"].update(extra=True),
            "exactly",
        ),
        (
            lambda proof: proof["documents"]["note:a"].update(
                content_hash=_digest("wrong")
            ),
            "attestation does not match",
        ),
        (
            lambda proof: proof["documents"]["note:a"].update(
                segment_recipe_hash=_digest("wrong")
            ),
            "attestation does not match",
        ),
        (
            lambda proof: proof["documents"].pop("note:a"),
            "do not match",
        ),
        (
            lambda proof: proof["documents"].update(
                {
                    "note:extra": {
                        "content_hash": _digest("extra-content"),
                        "segment_recipe_hash": _digest("segment-recipe"),
                    }
                }
            ),
            "do not match",
        ),
    ],
)
def test_input_proof_is_strictly_bound_and_failure_removes_candidate(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    refs = (_ref("note:a"),)
    proof = _proof(refs)
    mutation(proof)
    candidate = tmp_path / "candidate"

    with pytest.raises(LogicalGenerationError, match=message):
        build_logical_generation(refs, proof, GenerationRecipe(), candidate)

    assert not candidate.exists()


def test_ref_validation_rejects_duplicates_and_manual_attestation_drift(
    tmp_path: Path,
) -> None:
    first = _ref("note:a")
    duplicate_doc = replace(first, segment_hash=_digest("other"))
    with pytest.raises(LogicalGenerationError, match="duplicate document"):
        build_logical_generation(
            (first, duplicate_doc),
            _proof((first,)),
            GenerationRecipe(),
            tmp_path / "duplicate-doc",
        )

    second = _ref("note:b")
    duplicate_hash = replace(second, segment_hash=first.segment_hash)
    with pytest.raises(LogicalGenerationError, match="more than one document"):
        build_logical_generation(
            (first, duplicate_hash),
            _proof((first, second)),
            GenerationRecipe(),
            tmp_path / "duplicate-hash",
        )

    drifted = replace(first, doc_type="book")
    with pytest.raises(LogicalGenerationError, match="attestation mismatch"):
        build_logical_generation(
            (drifted,),
            _proof((first,)),
            GenerationRecipe(),
            tmp_path / "drifted",
        )


class _OneShotRefs:
    def __init__(self, refs: tuple[StoredSegmentRef, ...]) -> None:
        self.refs = refs
        self.iterations = 0

    def __iter__(self) -> Iterator[StoredSegmentRef]:
        self.iterations += 1
        if self.iterations != 1:
            raise AssertionError("refs were consumed more than once")
        yield from self.refs


class _OnePassMapping(Mapping[str, object]):
    def __init__(self, value: Mapping[str, object]) -> None:
        self.value = dict(value)
        self.iterations = 0

    def __getitem__(self, key: str) -> object:
        return self.value[key]

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        if self.iterations != 1:
            raise AssertionError("mapping was iterated more than once")
        return iter(self.value)

    def __len__(self) -> int:
        return len(self.value)


def test_refs_and_proof_mappings_are_consumed_once(tmp_path: Path) -> None:
    refs = (_ref("note:a"), _ref("note:b"))
    one_shot_refs = _OneShotRefs(refs)
    raw = _proof(refs)
    raw_documents = raw["documents"]
    assert isinstance(raw_documents, dict)
    wrapped_entries = {
        key: _OnePassMapping(value)
        for key, value in raw_documents.items()
        if isinstance(value, dict)
    }
    documents = _OnePassMapping(wrapped_entries)
    proof = _OnePassMapping(
        {
            "schema_version": raw["schema_version"],
            "compiler_recipe_hash": raw["compiler_recipe_hash"],
            "documents": documents,
        }
    )

    receipt = build_logical_generation(
        one_shot_refs,
        proof,
        GenerationRecipe(),
        tmp_path / "candidate",
    )

    assert receipt.document_count == 2
    assert one_shot_refs.iterations == 1
    assert proof.iterations == 1
    assert documents.iterations == 1
    assert all(entry.iterations == 1 for entry in wrapped_entries.values())


def test_large_identity_never_materializes_the_core_as_one_json_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = tuple(_ref(f"note:doc-{position:04d}") for position in range(500))
    recipe = GenerationRecipe()
    proof = _proof(refs, recipe)
    expected = logical_generation_id(refs, recipe)
    original = generation_module.iter_canonical_json

    def forbidden(_value: object) -> object:
        raise AssertionError("whole-value canonical helper was called")

    def guarded(value: object) -> Iterator[str]:
        if isinstance(value, Mapping) and "documents" in value:
            raise AssertionError("O(N) identity core was materialized")
        yield from original(value)

    monkeypatch.setattr(generation_module, "iter_canonical_json", guarded)
    monkeypatch.setattr("app.index.v2.canonical.canonical_bytes", forbidden)
    monkeypatch.setattr("app.index.v2.canonical.canonical_hash", forbidden)
    monkeypatch.setattr("app.index.v3.models.canonical_hash", forbidden)
    receipt = build_logical_generation(
        iter(refs),
        proof,
        recipe,
        tmp_path / "candidate",
    )
    assert receipt.document_count == 500
    assert receipt.generation_id == expected


class _Cancelled(RuntimeError):
    pass


def test_cancellation_during_streaming_strictly_removes_candidate(
    tmp_path: Path,
) -> None:
    refs = tuple(_ref(f"note:doc-{position}") for position in range(8))
    candidate = tmp_path / "candidate"
    calls = 0

    def check_cancelled() -> None:
        nonlocal calls
        calls += 1
        if calls == 14:
            raise _Cancelled("stop")

    with pytest.raises(_Cancelled, match="stop"):
        build_logical_generation(
            refs,
            _proof(refs),
            GenerationRecipe(),
            candidate,
            check_cancelled,
        )

    assert not candidate.exists()
    assert not tuple(tmp_path.glob("candidate/*"))
    assert not tuple(tmp_path.glob(".candidate.logical-generation.*"))


def test_public_candidate_appears_only_after_private_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = (_ref("note:a"),)
    candidate = tmp_path / "candidate"
    original = generation_module._write_manifest

    def guarded_manifest(path: Path, *args: object, **kwargs: object):
        assert not os.path.lexists(candidate)
        assert path.parent.parent == tmp_path
        assert path.parent.name.startswith(".candidate.logical-generation.")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(generation_module, "_write_manifest", guarded_manifest)
    receipt = build_logical_generation(
        refs, _proof(refs), GenerationRecipe(), candidate
    )

    assert receipt.candidate_dir == candidate
    assert candidate.is_dir()
    assert not tuple(tmp_path.glob(".candidate.logical-generation.*"))

def test_atomic_publish_never_replaces_a_concurrent_empty_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = (_ref("note:a"),)
    candidate = tmp_path / "candidate"
    original = generation_module._rename_no_replace
    winner_identity: tuple[int, int] | None = None

    def concurrent_target(source: Path, target: Path) -> None:
        nonlocal winner_identity
        target.mkdir()
        metadata = target.stat()
        winner_identity = (metadata.st_dev, metadata.st_ino)
        original(source, target)

    monkeypatch.setattr(
        generation_module, "_rename_no_replace", concurrent_target
    )
    with pytest.raises(LogicalGenerationError, match="must not already exist"):
        build_logical_generation(
            refs, _proof(refs), GenerationRecipe(), candidate
        )

    assert winner_identity is not None
    metadata = candidate.stat()
    assert (metadata.st_dev, metadata.st_ino) == winner_identity
    assert not tuple(candidate.iterdir())
    assert not tuple(tmp_path.glob(".candidate.logical-generation.*"))

def test_staging_inspection_failure_removes_private_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"

    def fail_inspection(path: Path):
        raise LogicalGenerationError(f"injected inspection failure: {path}")

    monkeypatch.setattr(
        generation_module, "_capture_owned_directory", fail_inspection
    )
    with pytest.raises(LogicalGenerationError, match="injected inspection failure"):
        build_logical_generation(
            (), _proof(()), GenerationRecipe(), candidate
        )

    assert not candidate.exists()
    assert not tuple(tmp_path.glob(".candidate.logical-generation.*"))

def test_staging_identity_swap_is_detected_without_recursive_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = (_ref("note:a"),)
    candidate = tmp_path / "candidate"
    original = generation_module._write_input_proof
    replacement: Path | None = None

    def swap_after_proof(path: Path, *args: object, **kwargs: object):
        nonlocal replacement
        receipt = original(path, *args, **kwargs)
        stolen = tmp_path / "stolen-owned-staging"
        path.parent.rename(stolen)
        path.parent.mkdir()
        replacement = path.parent
        (replacement / "sentinel.txt").write_text("do not delete", encoding="utf-8")
        return receipt

    monkeypatch.setattr(generation_module, "_write_input_proof", swap_after_proof)
    with pytest.raises(LogicalGenerationError, match="failed to safely clean"):
        build_logical_generation(
            refs, _proof(refs), GenerationRecipe(), candidate
        )

    assert not candidate.exists()
    assert replacement is not None
    assert (replacement / "sentinel.txt").read_text(encoding="utf-8") == "do not delete"
    assert (tmp_path / "stolen-owned-staging" / "input-proof.json").is_file()

def test_empty_generation_is_supported(tmp_path: Path) -> None:
    recipe = GenerationRecipe()
    receipt = build_logical_generation(
        (), _proof((), recipe), recipe, tmp_path / "empty"
    )
    assert receipt.document_count == 0
    assert receipt.manifest_ref.records == 0
    assert receipt.input_proof_ref.records == 0
    assert receipt.generation_id == logical_generation_id((), recipe)
    validate_logical_generation_manifest(
        _load(receipt.candidate_dir / "manifest.json")
    )


def test_existing_candidate_file_or_directory_is_never_modified(
    tmp_path: Path,
) -> None:
    refs = (_ref("note:a"),)
    existing_dir = tmp_path / "existing-dir"
    existing_dir.mkdir()
    sentinel = existing_dir / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(LogicalGenerationError, match="must not already exist"):
        build_logical_generation(
            refs, _proof(refs), GenerationRecipe(), existing_dir
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"

    existing_file = tmp_path / "existing-file"
    existing_file.write_text("keep", encoding="utf-8")
    with pytest.raises(LogicalGenerationError, match="must not already exist"):
        build_logical_generation(
            refs, _proof(refs), GenerationRecipe(), existing_file
        )
    assert existing_file.read_text(encoding="utf-8") == "keep"


def test_symlink_candidate_and_symlink_ancestor_are_rejected(tmp_path: Path) -> None:
    refs = (_ref("note:a"),)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(LogicalGenerationError, match="already exist"):
        build_logical_generation(
            refs, _proof(refs), GenerationRecipe(), link
        )
    with pytest.raises(LogicalGenerationError, match="symlink or junction"):
        build_logical_generation(
            refs, _proof(refs), GenerationRecipe(), link / "candidate"
        )
    assert not (real / "candidate").exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction contract is Windows-only")
def test_windows_junction_ancestor_is_rejected(tmp_path: Path) -> None:
    refs = (_ref("note:a"),)
    real = tmp_path / "real"
    real.mkdir()
    junction = tmp_path / "junction"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"cannot create NTFS junction: {completed.stderr}")
    try:
        assert generation_module._path_is_link(junction)
        with pytest.raises(LogicalGenerationError, match="symlink or junction"):
            build_logical_generation(
                refs,
                _proof(refs),
                GenerationRecipe(),
                junction / "candidate",
            )
        assert not (real / "candidate").exists()
    finally:
        os.rmdir(junction)

def test_manifest_parser_discriminates_schema_and_exact_shape(
    tmp_path: Path,
) -> None:
    refs = (_ref("note:a"),)
    receipt = build_logical_generation(
        refs, _proof(refs), GenerationRecipe(), tmp_path / "candidate"
    )
    manifest = _load(receipt.candidate_dir / "manifest.json")

    old = dict(manifest)
    old["schema_version"] = 3
    with pytest.raises(LogicalGenerationError, match="schema_version"):
        validate_logical_generation_manifest(old)

    compatibility = {
        "schema_version": 3,
        "generation": receipt.generation_id,
    }
    with pytest.raises(LogicalGenerationError, match="exactly"):
        validate_logical_generation_manifest(compatibility)

    extra = dict(manifest)
    extra["legacy_export"] = {}
    with pytest.raises(LogicalGenerationError, match="exactly"):
        validate_logical_generation_manifest(extra)

    short = dict(manifest)
    short["generation"] = receipt.generation_id[:20]
    with pytest.raises(LogicalGenerationError, match="64-character"):
        validate_logical_generation_manifest(short)


def test_manifest_parser_rejects_rebound_semantics(tmp_path: Path) -> None:
    refs = (_ref("note:a"), _ref("note:b"))
    receipt = build_logical_generation(
        refs, _proof(refs), GenerationRecipe(), tmp_path / "candidate"
    )
    original = _load(receipt.candidate_dir / "manifest.json")

    wrong_generation = json.loads(json.dumps(original))
    wrong_generation["generation"] = _digest("wrong")
    with pytest.raises(LogicalGenerationError, match="identity mismatch"):
        validate_logical_generation_manifest(wrong_generation)

    wrong_recipe = json.loads(json.dumps(original))
    wrong_recipe["generation_recipe"]["body_df_min"] += 1
    with pytest.raises(LogicalGenerationError, match="recipe_hash mismatch"):
        validate_logical_generation_manifest(wrong_recipe)

    noncanonical_recipe = json.loads(json.dumps(original))
    noncanonical_recipe["generation_recipe"]["body_df_ratio_numerator"] = 18
    noncanonical_recipe["generation_recipe"]["body_df_ratio_denominator"] = 20
    with pytest.raises(LogicalGenerationError, match="canonical, normalized"):
        validate_logical_generation_manifest(noncanonical_recipe)

    wrong_count = json.loads(json.dumps(original))
    wrong_count["document_count"] += 1
    with pytest.raises(LogicalGenerationError, match="document_count"):
        validate_logical_generation_manifest(wrong_count)

    wrong_proof_count = json.loads(json.dumps(original))
    wrong_proof_count["input_proof"]["records"] = 0
    with pytest.raises(LogicalGenerationError, match="records"):
        validate_logical_generation_manifest(wrong_proof_count)


def test_argument_validation_happens_before_candidate_creation(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    with pytest.raises(TypeError, match="recipe"):
        build_logical_generation((), {}, object(), candidate)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="proof"):
        build_logical_generation(
            (), object(), GenerationRecipe(), candidate  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="check_cancelled"):
        build_logical_generation(
            (),
            _proof(()),
            GenerationRecipe(),
            candidate,
            check_cancelled=object(),  # type: ignore[arg-type]
        )
    assert not os.path.lexists(candidate)
