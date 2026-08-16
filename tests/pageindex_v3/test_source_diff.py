"""Stable-catalog and explicit Segment change-set contracts."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

import app.index.v2.source_snapshot as snapshot_module
from app.index.v2.canonical import canonical_hash
from app.index.v2.models import SegmentRecipe
from app.index.v2.object_store import StoredSegmentRef
from app.index.v2.source_snapshot import (
    StableCatalogSnapshot,
    capture_stable_catalog,
    capture_stable_input_proof,
)
from app.index.v3.models import GenerationRecipe
from app.index.v3.source_diff import SegmentChangeSet, diff_segment_inputs


SEGMENT_RECIPE_HASH = canonical_hash(SegmentRecipe().as_dict())
GENERATION_RECIPE_HASH = canonical_hash(GenerationRecipe().as_dict())


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


def _capture(content: Path, *, check_cancel=lambda: None) -> StableCatalogSnapshot:
    snapshot = capture_stable_catalog(
        content,
        segment_recipe_hash=SEGMENT_RECIPE_HASH,
        compiler_recipe_hash=GENERATION_RECIPE_HASH,
        check_cancel=check_cancel,
        max_workers=2,
    )
    assert isinstance(snapshot, StableCatalogSnapshot)
    return snapshot


def _base_refs(snapshot: StableCatalogSnapshot) -> tuple[StoredSegmentRef, ...]:
    documents = snapshot.proof["documents"]
    assert isinstance(documents, dict)
    refs: list[StoredSegmentRef] = []
    for doc_key in sorted(documents):
        entry = documents[doc_key]
        assert isinstance(entry, dict)
        doc_type, slug = doc_key.split(":", 1)
        segment_hash = hashlib.sha256(f"segment:{doc_key}".encode("utf-8")).hexdigest()
        refs.append(
            StoredSegmentRef(
                segment_hash=segment_hash,
                path=Path("objects") / f"{segment_hash}.json",
                byte_size=123,
                doc_key=doc_key,
                doc_type=doc_type,
                slug=slug,
                content_hash=str(entry["content_hash"]),
                segment_recipe_hash=str(entry["segment_recipe_hash"]),
            )
        )
    return tuple(refs)


def test_capture_returns_the_existing_exact_proof_and_sorted_sources(content: Path) -> None:
    snapshot = _capture(content)
    wrapper_proof = capture_stable_input_proof(
        content,
        segment_recipe_hash=SEGMENT_RECIPE_HASH,
        compiler_recipe_hash=GENERATION_RECIPE_HASH,
        check_cancel=lambda: None,
        max_workers=2,
    )

    assert snapshot.proof == wrapper_proof
    assert tuple(source.doc_key for source in snapshot.sources) == tuple(
        sorted(source.doc_key for source in snapshot.sources)
    )
    assert snapshot.content_dir == content.resolve()
    assert snapshot.verify_unchanged()


def test_capture_hashes_each_file_once_and_verify_never_rehashes(
    content: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_hash = snapshot_module._hash_open_file
    calls: Counter[Path] = Counter()

    def counting_hash(path: Path, check_cancel):
        calls[path] += 1
        return real_hash(path, check_cancel)

    monkeypatch.setattr(snapshot_module, "_hash_open_file", counting_hash)
    snapshot = _capture(content)
    assert calls
    assert set(calls.values()) == {1}
    before = calls.copy()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verify_unchanged performed a content hash")

    monkeypatch.setattr(snapshot_module, "_hash_open_file", forbidden)
    monkeypatch.setattr(snapshot_module, "_fingerprint_source", forbidden)
    assert snapshot.verify_unchanged()
    assert calls == before


def test_unchanged_change_set_contains_refs_without_loading_segments(content: Path) -> None:
    snapshot = _capture(content)
    refs = _base_refs(snapshot)
    changes = diff_segment_inputs(snapshot, tuple(reversed(refs)))

    assert isinstance(changes, SegmentChangeSet)
    assert changes.added == ()
    assert changes.changed == ()
    assert changes.deleted == ()
    assert changes.unchanged == tuple(sorted(ref.doc_key for ref in refs))
    assert tuple(changes.base_by_doc) == changes.unchanged
    assert tuple(changes.current_fingerprints) == changes.unchanged


def test_change_set_distinguishes_add_edit_delete_and_unchanged(content: Path) -> None:
    before = _capture(content)
    refs = _base_refs(before)
    (content / "notes" / "a.md").write_text("# A\nchanged\n", encoding="utf-8")
    (content / "notes" / "b.md").unlink()
    _write(content / "notes" / "c.md", "# C\nadded\n")

    after = _capture(content)
    changes = diff_segment_inputs(after, refs)

    assert changes.added == ("note:c",)
    assert changes.changed == ("note:a",)
    assert changes.deleted == ("note:b",)
    assert changes.unchanged == ("book:book",)


def test_segment_recipe_mismatch_is_a_changed_document(content: Path) -> None:
    snapshot = _capture(content)
    refs = list(_base_refs(snapshot))
    target = next(index for index, ref in enumerate(refs) if ref.doc_key == "note:a")
    refs[target] = replace(refs[target], segment_recipe_hash="f" * 64)

    changes = diff_segment_inputs(snapshot, refs)
    assert changes.changed == ("note:a",)
    assert "note:a" not in changes.unchanged


@pytest.mark.parametrize("mutation", ["edit", "add", "delete"])
def test_verify_unchanged_detects_file_and_topology_changes(
    content: Path,
    mutation: str,
) -> None:
    snapshot = _capture(content)
    if mutation == "edit":
        (content / "notes" / "a.md").write_text("# A\nomega\n", encoding="utf-8")
    elif mutation == "add":
        _write(content / "notes" / "new.md", "# New\n")
    else:
        (content / "notes" / "b.md").unlink()
    assert snapshot.verify_unchanged() is False


def test_diff_rejects_duplicate_refs_and_missing_or_extra_proof(content: Path) -> None:
    snapshot = _capture(content)
    refs = _base_refs(snapshot)
    with pytest.raises(ValueError, match="duplicate"):
        diff_segment_inputs(snapshot, (refs[0], refs[0]))

    proof = dict(snapshot.proof)
    raw_documents = proof["documents"]
    assert isinstance(raw_documents, dict)
    documents = dict(raw_documents)
    documents.pop("note:a")
    proof["documents"] = documents
    malformed = replace(snapshot, proof=proof)
    with pytest.raises(ValueError, match="proof.*sources|sources.*proof"):
        diff_segment_inputs(malformed, refs)

    rebound_source = replace(snapshot.sources[0], slug="wrong")
    rebound = replace(snapshot, sources=(rebound_source, *snapshot.sources[1:]))
    with pytest.raises(ValueError, match="source document attestation"):
        diff_segment_inputs(rebound, refs)


def test_capture_rejects_same_content_path_identity_aba(
    content: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_fingerprint = snapshot_module._fingerprint_source
    replaced = False

    def fingerprint_then_replace(source, check_cancel=lambda: None):
        nonlocal replaced
        result = real_fingerprint(source, check_cancel)
        if source.doc_key == "note:a" and not replaced:
            replaced = True
            path = content / "notes" / "a.md"
            file_state = path.stat()
            directory_state = path.parent.stat()
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            path.touch()
            import os

            os.utime(
                path,
                ns=(file_state.st_atime_ns, file_state.st_mtime_ns),
            )
            os.utime(
                path.parent,
                ns=(directory_state.st_atime_ns, directory_state.st_mtime_ns),
            )
        return result

    monkeypatch.setattr(
        snapshot_module,
        "_fingerprint_source",
        fingerprint_then_replace,
    )
    assert capture_stable_catalog(
        content,
        segment_recipe_hash=SEGMENT_RECIPE_HASH,
        compiler_recipe_hash=GENERATION_RECIPE_HASH,
        check_cancel=lambda: None,
    ) is None
    assert replaced


def test_capture_rejects_final_file_symlink_escape(
    content: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    note = content / "notes" / "a.md"
    note.unlink()
    try:
        note.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="escapes content root"):
        _capture(content)


def test_snapshot_detects_proof_mutation_after_capture(content: Path) -> None:
    snapshot = _capture(content)
    documents = snapshot.proof["documents"]
    assert isinstance(documents, dict)
    documents.pop("note:a")
    with pytest.raises(RuntimeError, match="proof was mutated"):
        diff_segment_inputs(snapshot, _base_refs(_capture(content)))


def test_capture_and_verify_propagate_cancellation(content: Path) -> None:
    class Cancelled(RuntimeError):
        pass

    def cancel() -> None:
        raise Cancelled("stop")

    with pytest.raises(Cancelled, match="stop"):
        _capture(content, check_cancel=cancel)

    snapshot = _capture(content)
    with pytest.raises(Cancelled, match="stop"):
        snapshot.verify_unchanged(cancel)


def test_change_set_mappings_are_read_only_and_order_is_deterministic(
    content: Path,
) -> None:
    snapshot = _capture(content)
    changes = diff_segment_inputs(snapshot, reversed(_base_refs(snapshot)))
    with pytest.raises(TypeError):
        changes.base_by_doc["note:new"] = _base_refs(snapshot)[0]  # type: ignore[index]
    with pytest.raises(TypeError):
        changes.current_fingerprints["note:new"] = "0" * 64  # type: ignore[index]
    assert tuple(changes.base_by_doc) == tuple(sorted(changes.base_by_doc))
    assert tuple(changes.current_fingerprints) == tuple(
        sorted(changes.current_fingerprints)
    )


def test_empty_catalog_has_a_stable_empty_change_set(tmp_path: Path) -> None:
    content = tmp_path / "empty"
    content.mkdir()
    snapshot = _capture(content)
    changes = diff_segment_inputs(snapshot, ())
    assert snapshot.sources == ()
    assert snapshot.verify_unchanged()
    assert changes.added == changes.changed == changes.deleted == changes.unchanged == ()
