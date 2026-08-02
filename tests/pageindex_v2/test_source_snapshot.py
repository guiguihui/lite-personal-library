from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import app.index.v2.source_snapshot as snapshot_module
from app.index.v2.canonical import canonical_hash
from app.index.v2.catalog import discover_documents, fingerprint_document
from app.index.v2.input_proof import proof_from_fingerprints
from app.index.v2.models import CompilerRecipe, SegmentRecipe
from app.index.v2.source_snapshot import capture_stable_input_proof


def _recipe_hashes() -> tuple[str, str]:
    return (
        canonical_hash(SegmentRecipe().as_dict()),
        canonical_hash(CompilerRecipe().as_dict()),
    )


def test_stable_capture_matches_sequential_exact_content_proof(
    sample_content: Path,
) -> None:
    segment_hash, compiler_hash = _recipe_hashes()
    sources = discover_documents(sample_content)
    expected = proof_from_fingerprints(
        {
            source.doc_key: fingerprint_document(source)
            for source in sources
        },
        segment_hash,
        compiler_hash,
    )

    actual = capture_stable_input_proof(
        sample_content,
        segment_recipe_hash=segment_hash,
        compiler_recipe_hash=compiler_hash,
        check_cancel=lambda: None,
    )

    assert actual == expected


def test_capture_rejects_source_change_inside_hash_envelope(
    sample_content: Path,
    monkeypatch,
) -> None:
    segment_hash, compiler_hash = _recipe_hashes()
    real_fingerprint = snapshot_module._fingerprint_source
    changed = False

    def fingerprint_then_change(source, check_cancel=lambda: None):
        nonlocal changed
        result = real_fingerprint(source, check_cancel)
        if source.doc_key == "note:welcome" and not changed:
            changed = True
            note = sample_content / "notes" / "welcome.md"
            note.write_text(
                note.read_text(encoding="utf-8") + "\nChanged during capture.\n",
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(
        snapshot_module,
        "_fingerprint_source",
        fingerprint_then_change,
    )

    assert (
        capture_stable_input_proof(
            sample_content,
            segment_recipe_hash=segment_hash,
            compiler_recipe_hash=compiler_hash,
            check_cancel=lambda: None,
        )
        is None
    )
    assert changed is True


def test_capture_rejects_catalog_topology_change_inside_hash_envelope(
    sample_content: Path,
    monkeypatch,
) -> None:
    segment_hash, compiler_hash = _recipe_hashes()
    real_fingerprint = snapshot_module._fingerprint_source
    changed = False

    def fingerprint_then_add(source, check_cancel=lambda: None):
        nonlocal changed
        result = real_fingerprint(source, check_cancel)
        if source.doc_key == "note:welcome" and not changed:
            changed = True
            (sample_content / "notes" / "added-during-capture.md").write_text(
                "# Added during capture\n",
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(
        snapshot_module,
        "_fingerprint_source",
        fingerprint_then_add,
    )

    assert (
        capture_stable_input_proof(
            sample_content,
            segment_recipe_hash=segment_hash,
            compiler_recipe_hash=compiler_hash,
            check_cancel=lambda: None,
        )
        is None
    )
    assert changed is True


def test_capture_rejects_insert_in_discovery_boundary_gap(
    sample_content: Path,
    monkeypatch,
) -> None:
    segment_hash, compiler_hash = _recipe_hashes()
    real_discover = snapshot_module.discover_documents
    calls = 0

    def discover_then_insert(root: Path):
        nonlocal calls
        sources = real_discover(root)
        calls += 1
        if calls == 1:
            (sample_content / "notes" / "inserted-after-discovery.md").write_text(
                "# Inserted after discovery\n",
                encoding="utf-8",
            )
        return sources

    monkeypatch.setattr(
        snapshot_module,
        "discover_documents",
        discover_then_insert,
    )

    assert (
        capture_stable_input_proof(
            sample_content,
            segment_recipe_hash=segment_hash,
            compiler_recipe_hash=compiler_hash,
            check_cancel=lambda: None,
        )
        is None
    )
    assert calls == 1


def test_capture_rejects_ancestor_symlink_escape(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.md").write_text(
        "# Outside content root\n",
        encoding="utf-8",
    )
    notes_link = content / "notes"
    try:
        notes_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    segment_hash, compiler_hash = _recipe_hashes()
    with pytest.raises(ValueError, match="escapes content root"):
        capture_stable_input_proof(
            content,
            segment_recipe_hash=segment_hash,
            compiler_recipe_hash=compiler_hash,
            check_cancel=lambda: None,
        )


def test_fast_topology_rescan_matches_catalog_discovery(
    sample_content: Path,
) -> None:
    for name in ("a\u0301.md", "b.md"):
        (sample_content / "notes" / name).write_text(
            f"# {name}\n",
            encoding="utf-8",
        )

    assert snapshot_module._rescan_catalog_topology(
        sample_content
    ) == snapshot_module._source_topology(
        discover_documents(sample_content)
    )


def test_fingerprint_normalizes_record_path_but_opens_raw_path(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content"
    note = content / "notes" / "a\u0301.md"
    note.parent.mkdir(parents=True)
    payload = b"# Decomposed Unicode path\n"
    note.write_bytes(payload)

    sources = discover_documents(content)
    prepared = snapshot_module._prepare_sources(content.resolve(), sources)
    fingerprint, _states = snapshot_module._fingerprint_source(prepared[0])

    assert fingerprint == canonical_hash(
        [
            {
                "path": "notes/\u00e1.md",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ]
    )
