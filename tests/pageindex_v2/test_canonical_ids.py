"""Tests for deterministic serialization, recipes, and stable identifiers."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import re

import pytest

from app.index.v2.canonical import canonical_bytes, canonical_hash, write_json_atomic
from app.index.v2.ids import make_doc_key, make_node_key, normalize_relative_path
from app.index.v2.models import CompilerRecipe, SegmentRecipe


def test_canonical_hash_ignores_mapping_insertion_order() -> None:
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})


def test_canonical_bytes_follow_the_versioned_json_rules() -> None:
    value = {"z": "中文", "nested": {"b": 2, "a": 1}}
    assert canonical_bytes(value) == (
        '{"nested":{"a":1,"b":2},"z":"中文"}'.encode("utf-8")
    )
    with pytest.raises(ValueError):
        canonical_bytes({"invalid": float("nan")})


def test_write_json_atomic_writes_only_canonical_bytes(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "value.json"
    value = {"b": 2, "a": 1}
    write_json_atomic(target, value)
    assert target.read_bytes() == canonical_bytes(value)
    assert json.loads(target.read_text(encoding="utf-8")) == value
    assert list(target.parent.iterdir()) == [target]


def test_normalize_relative_path_is_unicode_and_cross_platform_stable() -> None:
    decomposed = "books/cafe\u0301/./ch01.md"
    assert normalize_relative_path(decomposed) == "books/café/ch01.md"
    assert normalize_relative_path(r"books\café\ch01.md") == "books/café/ch01.md"


@pytest.mark.parametrize("path", ["", ".", "../outside.md", "/root.md", "C:/root.md"])
def test_normalize_relative_path_rejects_non_relative_inputs(path: str) -> None:
    with pytest.raises(ValueError):
        normalize_relative_path(path)


def test_doc_key_is_namespaced_and_validated() -> None:
    assert make_doc_key("book", "demo") == "book:demo"
    with pytest.raises(ValueError):
        make_doc_key("collection", "demo")
    with pytest.raises(ValueError):
        make_doc_key("book", "../demo")


def test_node_key_is_cross_platform_stable() -> None:
    left = make_node_key(
        "book:demo",
        r"books\demo\ch01.md",
        [" Demo ", "A  B"],
        0,
    )
    right = make_node_key(
        "book:demo",
        "books/demo/ch01.md",
        ["Demo", "A B"],
        0,
    )
    assert left == right
    assert re.fullmatch(r"n_[0-9a-f]{24}", left)


def test_node_key_distinguishes_duplicates_and_source_moves() -> None:
    base = make_node_key("book:demo", "books/demo/ch01.md", ["Demo"], 0)
    duplicate = make_node_key("book:demo", "books/demo/ch01.md", ["Demo"], 1)
    moved = make_node_key("book:demo", "books/demo/ch02.md", ["Demo"], 0)
    assert len({base, duplicate, moved}) == 3


def test_recipe_dicts_are_versioned_and_recipe_instances_are_immutable() -> None:
    segment = SegmentRecipe()
    compiler = CompilerRecipe()
    assert segment.as_dict()["schema_version"] == 2
    assert segment.as_dict()["chunk_target_chars"] == 500
    assert compiler.as_dict()["schema_version"] == 3
    assert compiler.as_dict()["generation_layout_version"] == "manifest-input-proof-v1"
    assert compiler.as_dict()["body_df_min"] == 256
    assert compiler.as_dict()["body_df_ratio"] == 0.90
    with pytest.raises(FrozenInstanceError):
        segment.chunk_target_chars = 1000  # type: ignore[misc]


def test_recipes_reject_unimplemented_schema_and_algorithm_versions() -> None:
    with pytest.raises(ValueError, match="unsupported Segment schema_version"):
        SegmentRecipe(schema_version=99)
    with pytest.raises(ValueError, match="unsupported tokenizer_version"):
        SegmentRecipe(tokenizer_version="retrieval-tokenizer-v99")
    with pytest.raises(ValueError, match="unsupported Compiler schema_version"):
        CompilerRecipe(schema_version=99)
    with pytest.raises(ValueError, match="unsupported ordering_version"):
        CompilerRecipe(ordering_version="unknown-order-v99")
