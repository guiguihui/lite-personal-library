"""Semantic comparison tests for legacy and PageIndex v2 generations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.index.v2.compiler import compile_generation
from app.index.v2.models import CompilerRecipe
from app.index.v2.object_store import put_segment
from app.index.v2.shadow_diff import compare_legacy_to_generation
from app.index.v2.validator import materialize_candidate


def _segment(
    *,
    slug: str = "alpha",
    doc_type: str = "note",
    chunk_count: int = 2,
    token: str = "search",
    title_tf: int = 0,
    breadcrumb_tf: int = 0,
    body_tf: int = 1,
) -> dict[str, object]:
    node_key = f"n_{slug}"
    folder = f"{doc_type}s"
    chunks = []
    postings = []
    for local_id in range(chunk_count):
        chunks.append(
            {
                "local_id": local_id,
                "node_key": node_key,
                "node_local_ordinal": local_id,
                "title": f"{slug.title()} title",
                "breadcrumb": [slug.title(), f"{slug.title()} title"],
                "body": f"body {local_id}",
                "source_md": f"content/{folder}/{slug}.md",
                "line_num": local_id,
                "line_end": local_id + 1,
                "lengths": {"title": 2, "breadcrumb": 3, "body": 2},
            }
        )
        postings.append(
            [local_id, title_tf, breadcrumb_tf, body_tf]
        )
    return {
        "schema_version": 2,
        "segment_recipe": {"schema_version": 2},
        "document": {
            "doc_key": f"{doc_type}:{slug}",
            "id": slug,
            "type": doc_type,
            "title": slug.title(),
            "author": "",
            "description": "",
            "tags": [],
            "date": "",
            "source_type": "",
            "source_title": "",
            "path": f"/{folder}/",
            "url": f"/{folder}/{slug}.html",
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
                "title": f"{slug.title()} title",
                "breadcrumb": [slug.title(), f"{slug.title()} title"],
                "summary": "",
                "source_md": f"content/{folder}/{slug}.md",
                "line_num": 0,
                "line_end": chunk_count,
            }
        ],
        "chunks": chunks,
        "postings": {token: postings},
        "document_tree": {
            "doc_name": slug,
            "type": doc_type,
            "title": slug.title(),
            "structure": [],
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _materialize(
    pageindex: Path,
    segment: dict[str, object] | list[dict[str, object]],
    recipe: CompilerRecipe | None = None,
) -> tuple[Path, dict[str, object]]:
    segments = segment if isinstance(segment, list) else [segment]
    for value in segments:
        put_segment(pageindex, value)
    compiled = compile_generation(segments, recipe or CompilerRecipe())
    generation = pageindex / "generations" / compiled.generation_id
    materialize_candidate(generation, compiled)
    return generation, compiled.payloads


def _write_legacy(
    legacy: Path,
    payloads: dict[str, object],
    *,
    chunk_id_offset: int = 100,
) -> None:
    _write_json(legacy / "global-index.json", payloads["global-index.json"])
    _write_json(legacy / "node-index.json", payloads["node-index.json"])

    chunks_payload = json.loads(json.dumps(payloads["chunks.json"]))
    id_map: dict[int, int] = {}
    for ordinal, chunk in enumerate(chunks_payload["chunks"], start=1):
        legacy_id = chunk_id_offset + ordinal
        id_map[ordinal] = legacy_id
        chunk["chunk_id"] = f"c{legacy_id:06d}"
    _write_json(legacy / "chunks.json", chunks_payload)

    inverted_payload = json.loads(json.dumps(payloads["inverted-index.json"]))
    for rows in inverted_payload["postings"].values():
        for row in rows:
            row[0] = id_map[row[0]]
    _write_json(legacy / "inverted-index.json", inverted_payload)

    for relative, payload in payloads.items():
        if "/" in relative:
            _write_json(legacy / relative, payload)


def test_diff_ignores_global_chunk_renumbering_and_reports_stale_trees(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    generation, payloads = _materialize(pageindex, _segment())
    _write_legacy(pageindex, payloads)
    _write_json(pageindex / "books" / "stale.json", {"stale": True})

    report = compare_legacy_to_generation(pageindex, generation)

    assert report["documents"]["semantic_mismatch"] == 0
    assert report["nodes"]["semantic_mismatch"] == 0
    assert report["chunks"]["semantic_mismatch"] == 0
    assert report["chunks"]["id_only_changes"] == 2
    assert report["postings"]["semantic_mismatch"] == 0
    assert report["postings"]["id_only_changes"] == 2
    assert report["document_trees"]["semantic_mismatch"] == 0
    assert report["document_trees"]["stale_legacy_files"] == 1
    assert report["document_trees"]["stale_legacy_paths"] == [
        "books/stale.json"
    ]


def test_diff_classifies_threshold_body_loss_as_expected_pruning(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    segment = _segment(chunk_count=256, token="common", body_tf=1)
    generation, payloads = _materialize(pageindex, segment)

    # Legacy PageIndex contains the unpruned body postings.
    legacy_payloads = dict(payloads)
    legacy_payloads["inverted-index.json"] = {
        "postings": {
            "common": [[chunk_id, 1] for chunk_id in range(1, 257)]
        },
        "num_chunks": 256,
    }
    _write_legacy(pageindex, legacy_payloads, chunk_id_offset=0)

    report = compare_legacy_to_generation(pageindex, generation)

    assert report["postings"]["expected_pruned"] == 256
    assert report["postings"]["structural_errors"] == 0
    assert report["postings"]["semantic_mismatch"] == 0


def test_diff_treats_title_or_breadcrumb_posting_loss_as_structural(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    segment = _segment(
        chunk_count=1,
        token="heading",
        title_tf=1,
        breadcrumb_tf=1,
        body_tf=0,
    )
    generation, payloads = _materialize(pageindex, segment)
    _write_legacy(pageindex, payloads, chunk_id_offset=0)

    # Simulate a broken v2 export. Segment facts still prove that the posting
    # has title and breadcrumb contributions which must never be pruned.
    _write_json(
        generation / "inverted-index.json",
        {"postings": {}, "num_chunks": 1},
    )

    report = compare_legacy_to_generation(pageindex, generation)

    assert report["postings"]["structural_errors"] == 1
    assert report["postings"]["semantic_mismatch"] == 1
    assert report["structural_errors"][0]["code"] == "field_posting_lost"
    assert report["structural_errors"][0]["field"] == "title+breadcrumb"


def test_diff_detects_title_posting_omitted_by_both_sides(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    segment = _segment(
        chunk_count=1,
        token="heading",
        title_tf=1,
        breadcrumb_tf=1,
        body_tf=0,
    )
    generation, payloads = _materialize(pageindex, segment)
    _write_legacy(pageindex, payloads, chunk_id_offset=0)

    empty = {"postings": {}, "num_chunks": 1}
    _write_json(pageindex / "inverted-index.json", empty)
    _write_json(generation / "inverted-index.json", empty)

    report = compare_legacy_to_generation(pageindex, generation)

    assert report["postings"]["semantic_equal"] is True
    assert report["postings"]["structural_errors"] == 1
    assert report["structural_errors"][0]["code"] == "field_posting_lost"
    assert report["structural_ok"] is False
    assert report["publish_blocking_errors"] == 1
    assert report["ok"] is False


def test_diff_detects_title_tf_undercounted_by_both_sides(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    segment = _segment(
        chunk_count=1,
        token="heading",
        title_tf=2,
        breadcrumb_tf=0,
        body_tf=0,
    )
    generation, payloads = _materialize(pageindex, segment)
    _write_legacy(pageindex, payloads, chunk_id_offset=0)

    undercounted = {
        "postings": {"heading": [[1, 1]]},
        "num_chunks": 1,
    }
    _write_json(pageindex / "inverted-index.json", undercounted)
    _write_json(generation / "inverted-index.json", undercounted)

    report = compare_legacy_to_generation(pageindex, generation)

    assert report["postings"]["semantic_equal"] is True
    assert report["postings"]["structural_errors"] == 1
    assert report["structural_errors"][0]["code"] == "field_posting_lost"
    assert report["structural_errors"][0]["expected_generation_tf"] == 2
    assert report["ok"] is False


def test_diff_treats_body_pruned_below_threshold_as_structural(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    segment = _segment(
        chunk_count=1,
        token="bodyterm",
        title_tf=0,
        breadcrumb_tf=0,
        body_tf=1,
    )
    generation, payloads = _materialize(pageindex, segment)
    _write_legacy(pageindex, payloads, chunk_id_offset=0)
    _write_json(
        generation / "inverted-index.json",
        {"postings": {}, "num_chunks": 1},
    )

    report = compare_legacy_to_generation(pageindex, generation)

    assert report["postings"]["structural_errors"] == 1
    assert report["structural_errors"][0]["code"] == (
        "body_posting_pruned_outside_policy"
    )
    assert report["postings"]["unexplained_semantic_mismatch"] == 1
    assert report["ok"] is False


def test_diff_uses_manifest_compiler_recipe_for_expected_pruning(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    segment = _segment(
        chunk_count=1,
        token="common",
        title_tf=0,
        breadcrumb_tf=0,
        body_tf=1,
    )
    recipe = CompilerRecipe(body_df_min=1, body_df_ratio=1.0)
    generation, payloads = _materialize(pageindex, segment, recipe)

    legacy_payloads = json.loads(json.dumps(payloads))
    legacy_payloads["inverted-index.json"] = {
        "postings": {"common": [[1, 1]]},
        "num_chunks": 1,
    }
    _write_legacy(pageindex, legacy_payloads, chunk_id_offset=0)

    report = compare_legacy_to_generation(pageindex, generation)

    assert report["postings"]["expected_pruned"] == 1
    assert report["postings"]["expected_policy_delta"] == 1
    assert report["postings"]["structural_errors"] == 0
    assert report["unexplained_semantic_mismatch"] == 0
    assert report["semantic_equal"] is False
    assert report["ok"] is True


def test_diff_classifies_legacy_document_df_filter_as_expected_policy(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    segments = [
        _segment(slug="alpha", chunk_count=1, token="shared"),
        _segment(slug="beta", chunk_count=1, token="shared"),
        _segment(slug="gamma", chunk_count=1, token="unique"),
    ]
    generation, payloads = _materialize(pageindex, segments)
    legacy_payloads = json.loads(json.dumps(payloads))
    del legacy_payloads["inverted-index.json"]["postings"]["shared"]
    _write_legacy(pageindex, legacy_payloads, chunk_id_offset=0)

    report = compare_legacy_to_generation(pageindex, generation)

    assert report["postings"]["semantic_mismatch"] == 2
    assert report["postings"]["expected_legacy_df_policy_delta"] == 2
    assert report["expected_policy_delta"] == 2
    assert report["unexplained_semantic_mismatch"] == 0
    assert report["semantic_equal"] is False
    assert report["structural_ok"] is True
    assert report["publish_blocking_errors"] == 0
    assert report["ok"] is True
    assert {
        detail["classification"] for detail in report["postings"]["details"]
    } == {"expected_legacy_df_policy_delta"}


def test_diff_reports_changed_chunk_body_as_one_semantic_mismatch(
    tmp_path: Path,
) -> None:
    pageindex = tmp_path / "pageindex"
    generation, payloads = _materialize(pageindex, _segment(chunk_count=1))
    _write_legacy(pageindex, payloads, chunk_id_offset=0)

    chunks = json.loads((pageindex / "chunks.json").read_text(encoding="utf-8"))
    chunks["chunks"][0]["body"] = "different body"
    _write_json(pageindex / "chunks.json", chunks)

    report = compare_legacy_to_generation(pageindex, generation)

    assert report["chunks"]["semantic_mismatch"] == 1
    detail = report["chunks"]["changed"][0]
    assert detail["doc_key"] == "note:alpha"
    assert detail["node_key"] == "n_alpha"
    assert detail["legacy_body_sha256"] != detail["generation_body_sha256"]
