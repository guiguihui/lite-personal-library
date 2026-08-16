from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib

import pytest

from app.index.v2.canonical import canonical_hash
from app.index.v2.compiler import compile_generation, should_prune_body
from app.index.v2.models import CompilerRecipe


def _segment(
    doc_type: str,
    slug: str,
    *,
    token: str,
    title_tf: int = 0,
    breadcrumb_tf: int = 0,
    body_tf: int = 1,
) -> dict[str, object]:
    doc_key = f"{doc_type}:{slug}"
    node_key = f"n_{slug}"
    return {
        "schema_version": 2,
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
            "content_hash": hashlib.sha256(slug.encode("utf-8")).hexdigest(),
            "recipe_hash": "a" * 64,
            "source_files": [],
        },
        "nodes": [
            {
                "node_key": node_key,
                "legacy_node_id": "0001",
                "title": f"{slug} title",
                "breadcrumb": [slug.title(), f"{slug} title"],
                "summary": "summary",
                "source_md": f"/raw/{doc_type}s/{slug}/_index.md",
                "line_num": 1,
            }
        ],
        "chunks": [
            {
                "local_id": 0,
                "node_key": node_key,
                "title": f"{slug} title",
                "breadcrumb": [slug.title(), f"{slug} title"],
                "body": f"{slug} body",
                "source_md": f"/raw/{doc_type}s/{slug}/_index.md",
                "line_num": 1,
                "lengths": {"title": 1, "breadcrumb": 2, "body": 2},
            }
        ],
        "postings": {token: [[0, title_tf, breadcrumb_tf, body_tf]]},
        "document_tree": {
            "doc_name": slug,
            "type": doc_type,
            "title": slug.title(),
            "structure": [],
        },
    }


@pytest.mark.parametrize(
    ("df", "chunks", "expected"),
    [(255, 255, False), (256, 1000, False), (900, 1000, True), (256, 256, True)],
)
def test_body_pruning_uses_both_thresholds(
    df: int, chunks: int, expected: bool
) -> None:
    assert should_prune_body(df, chunks) is expected


def test_compilation_is_deterministic_across_segment_order() -> None:
    forward = [
        _segment("note", "zeta", token="zeta"),
        _segment("book", "alpha", token="alpha"),
    ]
    recipe = CompilerRecipe()

    left = compile_generation(forward, recipe)
    right = compile_generation(list(reversed(forward)), recipe)

    assert left.generation_id == right.generation_id
    assert left.revision_sha256 == right.revision_sha256
    assert left.manifest == right.manifest
    assert left.payloads == right.payloads
    assert [doc["id"] for doc in left.payloads["global-index.json"]["docs"]] == [
        "alpha",
        "zeta",
    ]
    assert list(left.payloads["inverted-index.json"]["postings"]) == [
        "alpha",
        "zeta",
    ]
    with pytest.raises(FrozenInstanceError):
        left.generation_id = "different"  # type: ignore[misc]


def test_compiler_exports_legacy_compatible_payloads() -> None:
    compiled = compile_generation(
        [_segment("book", "alpha", token="search", body_tf=3)],
        CompilerRecipe(),
    )

    assert set(compiled.payloads) == {
        "input-proof.json",
        "global-index.json",
        "node-index.json",
        "chunks.json",
        "inverted-index.json",
        "books/alpha.json",
    }
    assert compiled.payloads["chunks.json"]["chunks"][0]["chunk_id"] == "c000001"
    assert compiled.payloads["node-index.json"]["nodes"][0]["node_id"] == "0001"
    assert compiled.payloads["inverted-index.json"] == {
        "postings": {"search": [[1, 3]]},
        "num_chunks": 1,
    }
    assert compiled.manifest["generation"] == compiled.generation_id
    assert compiled.manifest["revision_sha256"] == compiled.revision_sha256
    assert set(compiled.manifest["files"]) == set(compiled.payloads)
    assert compiled.manifest["schema_version"] == 3
    input_proof = compiled.payloads["input-proof.json"]
    assert input_proof == {
        "schema_version": 1,
        "compiler_recipe_hash": compiled.compiler_recipe_hash,
        "documents": {
            "book:alpha": {
                "content_hash": hashlib.sha256(b"alpha").hexdigest(),
                "segment_recipe_hash": "a" * 64,
            }
        },
    }
    assert compiled.manifest["input_proof_sha256"] == canonical_hash(input_proof)
    assert compiled.revision_sha256 == canonical_hash(
        {
            "schema_version": 3,
            "compiler_recipe_hash": compiled.compiler_recipe_hash,
            "input_proof_sha256": canonical_hash(input_proof),
            "documents": compiled.manifest["documents"],
        }
    )


def test_pruned_body_keeps_title_and_breadcrumb_tf() -> None:
    segment = _segment(
        "book",
        "large",
        token="common",
        title_tf=2,
        breadcrumb_tf=3,
        body_tf=4,
    )
    chunks = segment["chunks"]
    postings = segment["postings"]["common"]
    node_key = segment["nodes"][0]["node_key"]
    for local_id in range(1, 256):
        chunks.append(
            {
                "local_id": local_id,
                "node_key": node_key,
                "title": "",
                "breadcrumb": [],
                "body": "common",
                "source_md": "/raw/books/large/_index.md",
                "line_num": local_id + 1,
                "lengths": {"title": 0, "breadcrumb": 0, "body": 1},
            }
        )
        postings.append([local_id, 0, 0, 1])

    compiled = compile_generation([segment], CompilerRecipe())

    assert compiled.payloads["inverted-index.json"]["postings"]["common"] == [[1, 5]]
    assert compiled.manifest["pruning"]["body_tokens_pruned"] == 1
    assert compiled.manifest["pruning"]["body_postings_pruned"] == 256


def test_compiler_recipe_controls_pruning_and_is_recorded() -> None:
    segment = _segment("note", "small", token="common", title_tf=1, body_tf=2)
    recipe = CompilerRecipe(body_df_min=1, body_df_ratio=1.0)

    compiled = compile_generation([segment], recipe)

    assert compiled.payloads["inverted-index.json"]["postings"]["common"] == [[1, 1]]
    assert compiled.manifest["compiler_recipe"] == recipe.as_dict()
    assert compiled.manifest["pruning"]["body_min_df"] == 1
    assert compiled.manifest["pruning"]["body_min_coverage"] == 1.0


def test_compiler_rejects_duplicate_documents() -> None:
    segment = _segment("book", "alpha", token="alpha")
    with pytest.raises(ValueError, match="duplicate document"):
        compile_generation([segment, segment], CompilerRecipe())
