"""Local source snapshot cache binding and invalidation tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from app.index.v2.artifacts import ArtifactRef
from app.index.v2.canonical import canonical_bytes, canonical_hash
from app.index.v2.models import SegmentRecipe
from app.index.v2.source_snapshot import StableCatalogSnapshot, capture_stable_catalog
from app.index.v3.generation import LogicalGenerationReceipt
from app.index.v3.models import GenerationRecipe
import app.index.v3.source_snapshot_cache as cache_module
from app.index.v3.source_snapshot_cache import (
    load_source_snapshot_cache,
    source_snapshot_cache_path,
    store_source_snapshot_cache,
)


SEGMENT_RECIPE_HASH = canonical_hash(SegmentRecipe().as_dict())
GENERATION_RECIPE_HASH = canonical_hash(GenerationRecipe().as_dict())


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def content(tmp_path: Path) -> Path:
    root = tmp_path / "content"
    _write(root / "notes" / "a.md", "# A\nalpha\n")
    _write(root / "notes" / "b.md", "# B\nbeta\n")
    _write(root / "books" / "book" / "_index.md", "# Book\n")
    _write(root / "books" / "book" / "chapter.md", "# Chapter\ngamma\n")
    return root


def _capture(content: Path) -> StableCatalogSnapshot:
    snapshot = capture_stable_catalog(
        content,
        segment_recipe_hash=SEGMENT_RECIPE_HASH,
        compiler_recipe_hash=GENERATION_RECIPE_HASH,
        check_cancel=lambda: None,
        max_workers=2,
    )
    assert isinstance(snapshot, StableCatalogSnapshot)
    return snapshot


def _generation(
    snapshot: StableCatalogSnapshot,
    root: Path,
    *,
    generation_id: str | None = None,
    manifest_sha256: str | None = None,
) -> LogicalGenerationReceipt:
    documents = snapshot.proof["documents"]
    assert isinstance(documents, dict)
    count = len(documents)
    return LogicalGenerationReceipt(
        candidate_dir=root / "generations" / "candidate",
        generation_id=generation_id or _digest("generation"),
        generation_recipe_hash=GENERATION_RECIPE_HASH,
        manifest_ref=ArtifactRef(
            "manifest.json",
            manifest_sha256 or _digest("manifest"),
            321,
            count,
        ),
        input_proof_ref=ArtifactRef(
            "input-proof.json",
            snapshot.proof_sha256,
            123,
            count,
        ),
        document_count=count,
    )


def test_round_trip_reconstructs_exact_unchanged_snapshot(
    tmp_path: Path,
    content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    snapshot = _capture(content)
    generation = _generation(snapshot, pageindex)

    path = store_source_snapshot_cache(pageindex, snapshot, generation)
    loaded = load_source_snapshot_cache(pageindex, content, generation)

    assert path == source_snapshot_cache_path(
        pageindex, content, generation.generation_id
    )
    assert path is not None and path.is_file()
    assert isinstance(loaded, StableCatalogSnapshot)
    assert loaded.content_dir == snapshot.content_dir
    assert loaded.sources == snapshot.sources
    assert loaded.proof == snapshot.proof
    assert loaded.proof_sha256 == snapshot.proof_sha256
    assert loaded.directory_state == snapshot.directory_state
    assert loaded.topology == snapshot.topology
    assert loaded.file_state == snapshot.file_state
    assert loaded.verify_unchanged()


@pytest.mark.parametrize("mutation", ["edit", "rename", "add", "delete"])
def test_source_change_invalidates_cache(
    tmp_path: Path,
    content: Path,
    mutation: str,
) -> None:
    pageindex = tmp_path / "pageindex"
    snapshot = _capture(content)
    generation = _generation(snapshot, pageindex)
    assert store_source_snapshot_cache(pageindex, snapshot, generation) is not None

    if mutation == "edit":
        _write(content / "notes" / "a.md", "# A\ncontent changed and grew\n")
    elif mutation == "rename":
        (content / "notes" / "a.md").rename(content / "notes" / "renamed.md")
    elif mutation == "add":
        _write(content / "notes" / "added.md", "# Added\n")
    else:
        (content / "notes" / "b.md").unlink()

    assert load_source_snapshot_cache(pageindex, content, generation) is None


def test_corruption_and_generation_binding_mismatch_are_rejected(
    tmp_path: Path,
    content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    snapshot = _capture(content)
    generation = _generation(snapshot, pageindex)
    path = store_source_snapshot_cache(pageindex, snapshot, generation)
    assert path is not None

    other_parent = replace(
        generation,
        manifest_ref=replace(
            generation.manifest_ref,
            sha256=_digest("different-parent-manifest"),
        ),
    )
    assert load_source_snapshot_cache(pageindex, content, other_parent) is None

    raw = bytearray(path.read_bytes())
    marker = generation.manifest_ref.sha256.encode("ascii")
    position = raw.index(marker)
    raw[position] = ord("0") if raw[position] != ord("0") else ord("1")
    path.write_bytes(raw)
    assert load_source_snapshot_cache(pageindex, content, generation) is None


def test_cache_copied_to_another_content_root_is_rejected(
    tmp_path: Path,
    content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    snapshot = _capture(content)
    generation = _generation(snapshot, pageindex)
    original = store_source_snapshot_cache(pageindex, snapshot, generation)
    assert original is not None

    other_content = tmp_path / "other-content"
    shutil.copytree(content, other_content)
    rebound_path = source_snapshot_cache_path(
        pageindex, other_content, generation.generation_id
    )
    rebound_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(original, rebound_path)

    assert load_source_snapshot_cache(pageindex, other_content, generation) is None


def test_cache_hit_opens_only_cache_bytes_not_source_bytes(
    tmp_path: Path,
    content: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    snapshot = _capture(content)
    generation = _generation(snapshot, pageindex)
    assert store_source_snapshot_cache(pageindex, snapshot, generation) is not None

    root = content.resolve()
    real_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return real_open(path, *args, **kwargs)
        raise AssertionError(f"cache hit opened source file bytes: {resolved}")

    monkeypatch.setattr(Path, "open", guarded_open)
    loaded = load_source_snapshot_cache(pageindex, content, generation)
    assert isinstance(loaded, StableCatalogSnapshot)


def test_store_uses_canonical_atomic_install(
    tmp_path: Path,
    content: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    snapshot = _capture(content)
    generation = _generation(snapshot, pageindex)
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source, target):
        replacements.append((Path(source), Path(target)))
        return real_replace(source, target)

    monkeypatch.setattr(cache_module.os, "replace", recording_replace)
    path = store_source_snapshot_cache(pageindex, snapshot, generation)

    assert path is not None
    assert replacements == [(replacements[0][0], path)]
    assert replacements[0][0].parent == path.parent
    assert replacements[0][0].name.startswith(f".{path.name}.")
    assert replacements[0][0].suffix == ".tmp"
    raw = path.read_bytes()
    assert raw == canonical_bytes(json.loads(raw))
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))


def test_oversized_entry_and_noncanonical_schema_are_rejected(
    tmp_path: Path,
    content: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    snapshot = _capture(content)
    generation = _generation(snapshot, pageindex)
    path = store_source_snapshot_cache(pageindex, snapshot, generation)
    assert path is not None

    monkeypatch.setattr(cache_module, "SOURCE_SNAPSHOT_CACHE_MAX_BYTES", 32)
    assert load_source_snapshot_cache(pageindex, content, generation) is None

    monkeypatch.setattr(
        cache_module,
        "SOURCE_SNAPSHOT_CACHE_MAX_BYTES",
        256 * 1024 * 1024,
    )
    decoded = json.loads(path.read_bytes())
    decoded["unexpected"] = True
    path.write_bytes(canonical_bytes(decoded))
    assert load_source_snapshot_cache(pageindex, content, generation) is None


def test_store_refuses_snapshot_not_bound_to_generation(
    tmp_path: Path,
    content: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    snapshot = _capture(content)
    generation = _generation(snapshot, pageindex)
    mismatched = replace(
        generation,
        input_proof_ref=replace(
            generation.input_proof_ref,
            sha256=_digest("different-proof"),
        ),
    )

    assert store_source_snapshot_cache(pageindex, snapshot, mismatched) is None
    assert not source_snapshot_cache_path(
        pageindex, content, mismatched.generation_id
    ).exists()
