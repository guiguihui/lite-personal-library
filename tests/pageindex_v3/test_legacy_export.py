from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path

import pytest

from app.index.v2.canonical import canonical_hash
from app.index.v2.models import CompilerRecipe, SegmentRecipe
from app.index.v2.object_store import StoredSegmentRef, put_segment
from app.index.v2.streaming_compiler import compile_generation_to_candidate
from app.index.v2.validator import ValidationReport
import app.index.v3.legacy_export as legacy_export_module
from app.index.v3.generation import LogicalGenerationReceipt, build_logical_generation
from app.index.v3.legacy_export import (
    LegacyExportConflictError,
    LegacyExportValidationError,
    export_legacy_generation,
)
from app.index.v3.models import GenerationRecipe
from app.retrieval.tokenizer import tokenize


def _document_path(doc_type: str, slug: str) -> str:
    if doc_type == "note":
        return f"notes/{slug}.md"
    return f"{doc_type}s/{slug}/_index.md"


def _segment(
    doc_key: str,
    fields: tuple[tuple[str, tuple[str, ...], str], ...],
) -> dict[str, object]:
    doc_type, slug = doc_key.split(":", 1)
    chunks: list[dict[str, object]] = []
    postings: dict[str, list[list[int]]] = {}
    nodes: list[dict[str, object]] = []
    structure: list[dict[str, object]] = []
    for local_id, (title, breadcrumb, body) in enumerate(fields):
        node_key = f"node-{local_id}"
        title_tf = Counter(tokenize(title))
        breadcrumb_tf = Counter(tokenize(" ".join(breadcrumb)))
        body_tf = Counter(tokenize(body))
        nodes.append(
            {
                "node_key": node_key,
                "legacy_node_id": f"{local_id + 1:04d}",
                "title": title,
                "breadcrumb": list(breadcrumb),
                "summary": body[:40],
                "source_md": _document_path(doc_type, slug),
                "line_num": local_id + 1,
            }
        )
        structure.append(
            {
                "node_key": node_key,
                "legacy_node_id": f"{local_id + 1:04d}",
                "title": title,
                "children": [],
            }
        )
        chunks.append(
            {
                "local_id": local_id,
                "node_key": node_key,
                "title": title,
                "breadcrumb": list(breadcrumb),
                "body": body,
                "source_md": _document_path(doc_type, slug),
                "line_num": local_id + 1,
                "lengths": {
                    "title": sum(title_tf.values()),
                    "breadcrumb": sum(breadcrumb_tf.values()),
                    "body": sum(body_tf.values()),
                },
            }
        )
        for token in sorted(
            set(title_tf) | set(breadcrumb_tf) | set(body_tf),
            key=lambda value: value.encode("utf-8"),
        ):
            postings.setdefault(token, []).append(
                [
                    local_id,
                    int(title_tf.get(token, 0)),
                    int(breadcrumb_tf.get(token, 0)),
                    int(body_tf.get(token, 0)),
                ]
            )

    segment_recipe = SegmentRecipe().as_dict()
    source_files = [
        {
            "path": _document_path(doc_type, slug),
            "sha256": hashlib.sha256(doc_key.encode("utf-8")).hexdigest(),
        }
    ]
    return {
        "schema_version": 2,
        "segment_recipe": segment_recipe,
        "document": {
            "doc_key": doc_key,
            "id": slug,
            "type": doc_type,
            "title": slug.title(),
            "author": "",
            "description": "",
            "tags": [],
        },
        "fingerprint": {
            "content_hash": canonical_hash(source_files),
            "recipe_hash": canonical_hash(segment_recipe),
            "source_files": source_files,
        },
        "nodes": nodes,
        "chunks": chunks,
        "postings": {
            token: postings[token]
            for token in sorted(postings, key=lambda value: value.encode("utf-8"))
        },
        "document_tree": {
            "doc_name": slug,
            "type": doc_type,
            "title": slug.title(),
            "structure": structure,
        },
    }


def _proof(
    refs: tuple[StoredSegmentRef, ...], recipe: GenerationRecipe
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "compiler_recipe_hash": canonical_hash(recipe.as_dict()),
        "documents": {
            ref.doc_key: {
                "content_hash": ref.content_hash,
                "segment_recipe_hash": ref.segment_recipe_hash,
            }
            for ref in reversed(refs)
        },
    }


def _corpus(
    tmp_path: Path,
) -> tuple[
    Path,
    tuple[StoredSegmentRef, ...],
    GenerationRecipe,
    LogicalGenerationReceipt,
]:
    pageindex = tmp_path / "pageindex"
    refs = tuple(
        put_segment(pageindex, segment)
        for segment in (
            _segment(
                "note:zeta",
                (
                    ("Shared title", ("Notes",), "common shared body"),
                    ("Second", (), "common tail"),
                ),
            ),
            _segment(
                "book:alpha",
                (("Alpha common", ("Books", "Alpha"), "common body"),),
            ),
        )
    )
    recipe = GenerationRecipe(
        body_df_min=1,
        body_df_ratio_numerator=1,
        body_df_ratio_denominator=1,
    )
    generation = build_logical_generation(
        reversed(refs),
        _proof(refs, recipe),
        recipe,
        tmp_path / "logical-generation",
    )
    return pageindex, refs, recipe, generation


def _artifact_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _staging_paths(pageindex: Path, generation_id: str) -> tuple[Path, ...]:
    root = pageindex / "exports" / "legacy" / generation_id
    if not root.is_dir():
        return ()
    return tuple(root.glob(".legacy-export-*"))


def test_export_is_explicit_and_matches_schema3_streaming_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, refs, recipe, generation = _corpus(tmp_path)
    compile_calls = 0
    validation_calls = 0
    real_compile = legacy_export_module.compile_generation_to_candidate
    real_validate = legacy_export_module.validate_candidate_normal

    def observed_compile(*args: object, **kwargs: object):
        nonlocal compile_calls
        compile_calls += 1
        return real_compile(*args, **kwargs)

    def observed_validate(*args: object, **kwargs: object):
        nonlocal validation_calls
        validation_calls += 1
        report = real_validate(*args, **kwargs)
        assert report.ok, report.errors
        return report

    monkeypatch.setattr(
        legacy_export_module, "compile_generation_to_candidate", observed_compile
    )
    monkeypatch.setattr(
        legacy_export_module, "validate_candidate_normal", observed_validate
    )

    assert compile_calls == 0
    assert not (pageindex / "exports").exists()
    exported = export_legacy_generation(
        generation,
        pageindex,
        trusted_generation=generation.generation_id,
        check_cancelled=lambda: None,
    )

    assert compile_calls == 1
    assert validation_calls == 1
    assert exported.legacy_compile_runs == 1
    assert exported.logical_generation == generation.generation_id
    assert exported.export_dir == (
        pageindex
        / "exports"
        / "legacy"
        / generation.generation_id
        / exported.export_id
    ).absolute()
    assert exported.bytes_written == sum(
        path.stat().st_size for path in exported.export_dir.rglob("*") if path.is_file()
    )
    assert exported.counters == {
        "legacy_compile_runs": 1,
        "legacy_postings_visited": exported.postings_visited,
        "legacy_bytes_written": exported.bytes_written,
    }
    assert not hasattr(exported, "artifacts")
    assert not hasattr(exported, "segment_refs")
    assert _staging_paths(pageindex, generation.generation_id) == ()

    oracle_recipe = CompilerRecipe(
        body_df_min=recipe.body_df_min,
        body_df_ratio=(
            recipe.body_df_ratio_numerator / recipe.body_df_ratio_denominator
        ),
    )
    oracle = compile_generation_to_candidate(
        refs,
        pageindex,
        tmp_path / "schema3-oracle",
        oracle_recipe,
    )
    assert exported.export_id == oracle.generation_id
    assert exported.postings_visited == oracle.invariants["postings_visited"]
    assert _artifact_bytes(exported.export_dir) == _artifact_bytes(
        oracle.candidate_dir
    )


def test_identical_existing_export_is_validated_and_reused_without_clobber(
    tmp_path: Path,
) -> None:
    pageindex, _refs, _recipe, generation = _corpus(tmp_path)
    first = export_legacy_generation(
        generation,
        pageindex,
        trusted_generation=generation.generation_id,
        check_cancelled=lambda: None,
    )
    before = _artifact_bytes(first.export_dir)

    second = export_legacy_generation(
        generation,
        pageindex,
        trusted_generation=generation.generation_id,
        check_cancelled=lambda: None,
    )

    assert second.export_dir == first.export_dir
    assert second.reused_existing is True
    assert _artifact_bytes(second.export_dir) == before
    assert _staging_paths(pageindex, generation.generation_id) == ()


def test_conflicting_existing_export_is_never_clobbered(
    tmp_path: Path,
) -> None:
    pageindex, _refs, _recipe, generation = _corpus(tmp_path)
    first = export_legacy_generation(
        generation,
        pageindex,
        trusted_generation=generation.generation_id,
        check_cancelled=lambda: None,
    )
    sentinel = first.export_dir / "do-not-clobber.txt"
    sentinel.write_bytes(b"owned by another publisher")

    with pytest.raises(LegacyExportConflictError, match="existing legacy export"):
        export_legacy_generation(
            generation,
            pageindex,
            trusted_generation=generation.generation_id,
            check_cancelled=lambda: None,
        )

    assert sentinel.read_bytes() == b"owned by another publisher"
    assert _staging_paths(pageindex, generation.generation_id) == ()


def test_invalid_trusted_generation_cannot_escape_export_root(
    tmp_path: Path,
) -> None:
    pageindex, _refs, _recipe, generation = _corpus(tmp_path)

    with pytest.raises(ValueError, match="trusted_generation"):
        export_legacy_generation(
            generation,
            pageindex,
            trusted_generation="../outside",
            check_cancelled=lambda: None,
        )
    with pytest.raises(ValueError, match="trusted_generation"):
        export_legacy_generation(
            generation,
            pageindex,
            trusted_generation="0" * 64,
            check_cancelled=lambda: None,
        )

    assert not (pageindex / "exports").exists()
    assert not (tmp_path / "outside").exists()


def test_validation_failure_removes_private_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, _refs, _recipe, generation = _corpus(tmp_path)
    monkeypatch.setattr(
        legacy_export_module,
        "validate_candidate_normal",
        lambda *_args, **_kwargs: ValidationReport(
            False, ("forced_invalid: test",), ("forced warning",)
        ),
    )

    with pytest.raises(LegacyExportValidationError, match="forced_invalid"):
        export_legacy_generation(
            generation,
            pageindex,
            trusted_generation=generation.generation_id,
            check_cancelled=lambda: None,
        )

    generation_root = pageindex / "exports" / "legacy" / generation.generation_id
    assert tuple(generation_root.iterdir()) == ()


def test_partial_compiler_failure_removes_private_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, _refs, _recipe, generation = _corpus(tmp_path)

    def failing_compile(
        _refs: object,
        _pageindex: object,
        candidate: Path,
        _recipe: object,
        **_kwargs: object,
    ) -> object:
        Path(candidate, "partial.bin").write_bytes(b"partial")
        raise RuntimeError("compiler failed")

    monkeypatch.setattr(
        legacy_export_module, "compile_generation_to_candidate", failing_compile
    )

    with pytest.raises(RuntimeError, match="compiler failed"):
        export_legacy_generation(
            generation,
            pageindex,
            trusted_generation=generation.generation_id,
            check_cancelled=lambda: None,
        )

    generation_root = pageindex / "exports" / "legacy" / generation.generation_id
    assert tuple(generation_root.iterdir()) == ()

def test_cancellation_after_compile_is_preserved_and_cleans_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, _refs, _recipe, generation = _corpus(tmp_path)
    real_compile = legacy_export_module.compile_generation_to_candidate
    compiled = False

    class Cancelled(BaseException):
        pass

    def observed_compile(*args: object, **kwargs: object):
        nonlocal compiled
        receipt = real_compile(*args, **kwargs)
        compiled = True
        return receipt

    def check_cancelled() -> None:
        if compiled:
            raise Cancelled("stop after full compile")

    monkeypatch.setattr(
        legacy_export_module, "compile_generation_to_candidate", observed_compile
    )

    with pytest.raises(Cancelled, match="stop after full compile"):
        export_legacy_generation(
            generation,
            pageindex,
            trusted_generation=generation.generation_id,
            check_cancelled=check_cancelled,
        )

    generation_root = pageindex / "exports" / "legacy" / generation.generation_id
    staging_root = pageindex / ".legacy-export-staging"
    assert tuple(generation_root.iterdir()) == ()
    assert tuple(staging_root.iterdir()) == ()
