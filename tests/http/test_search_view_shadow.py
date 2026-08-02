from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.schema import AppConfig
from app.http.routes_search import SearchViewShadowTarget, _ShadowRun
from app.http.server import create_app
from app.index.v2.artifacts import ArtifactRef
from app.index.v3.generation import LogicalGenerationReceipt
from app.index.v3.models import ViewPin
from app.retrieval.search import Hit


def _target(tmp_path: Path) -> SearchViewShadowTarget:
    generation_id = "a" * 64
    manifest = ArtifactRef("manifest.json", "b" * 64, 1, 0)
    proof = ArtifactRef("input-proof.json", "c" * 64, 1, 0)
    generation = LogicalGenerationReceipt(
        candidate_dir=tmp_path / "generation",
        generation_id=generation_id,
        generation_recipe_hash="d" * 64,
        manifest_ref=manifest,
        input_proof_ref=proof,
        document_count=0,
    )
    return SearchViewShadowTarget(
        ViewPin(generation_id, "e" * 64),
        generation,
    )


def _hit(*, stable: bool) -> Hit:
    kwargs = {}
    if stable:
        kwargs = {
            "generation": "a" * 64,
            "view_id": "e" * 64,
            "doc_key": "book:alpha",
            "doc_uid": "f" * 64,
            "segment_hash": "1" * 64,
            "local_id": 0,
            "node_key": "root",
        }
    return Hit(
        node={
            "doc_id": "alpha",
            "node_id": "0001",
            "title": "Alpha",
            "breadcrumb": ["Library", "Alpha"],
            "summary": "legacy response text",
        },
        score=0.016,
        tokens=["alpha"],
        **kwargs,
    )


@pytest.fixture
def shadow_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("content", "pageindex", "config", "pdfs"):
        (tmp_path / name).mkdir()
    config = AppConfig(
        content_dir=str(tmp_path / "content"),
        pageindex_dir=str(tmp_path / "pageindex"),
        config_dir=str(tmp_path / "config"),
        pdfs_dir=str(tmp_path / "pdfs"),
    )
    app = create_app(config)

    import app.http.routes_search as route

    monkeypatch.setattr(
        route,
        "_load_index",
        lambda _request, _config: {
            "global_index": {"docs": []},
            "postings": {"alpha": [[1, 1]]},
            "chunk_stats": object(),
            "doc_types": {"alpha": "books"},
        },
    )
    monkeypatch.setattr(
        route,
        "search_multi_path",
        lambda *_args, **_kwargs: [_hit(stable=False)],
    )
    return app, TestClient(app), route


def test_shadow_match_records_diagnostics_but_serves_legacy(
    shadow_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, route = shadow_client
    app.state.search_view_shadow_pin = _target(tmp_path)
    monkeypatch.setattr(
        route,
        "_run_shadow_search",
        lambda *_args: _ShadowRun((_hit(stable=True),), ()),
    )

    response = client.get("/api/search", params={"q": "alpha", "limit": 10})

    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item == {
        "type": "chunk",
        "doc_type": "books",
        "slug": "alpha",
        "node_id": "0001",
        "title": "Alpha",
        "breadcrumb": "Library > Alpha",
        "text": "legacy response text",
        "score": 0.016,
    }
    diagnostic = app.state.search_view_shadow_diagnostics
    assert diagnostic["classification"] == "match"
    assert diagnostic["identity_match"] is True
    assert diagnostic["score_match"] is True
    assert diagnostic["error"] is None
    assert diagnostic["p3_references"] == (
        {
            "doc_key": "book:alpha",
            "doc_uid": "f" * 64,
            "segment_hash": "1" * 64,
            "local_id": 0,
            "node_key": "root",
        },
    )


def test_shadow_pruned_mismatch_is_classified_as_expected_policy_delta(
    shadow_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, route = shadow_client
    app.state.search_view_shadow_pin = _target(tmp_path)
    changed = _hit(stable=True)
    changed.score = 0.032
    monkeypatch.setattr(
        route,
        "_run_shadow_search",
        lambda *_args: _ShadowRun((changed,), ("bodyhot",)),
    )

    response = client.get("/api/search", params={"q": "alpha"})

    assert response.status_code == 200
    assert response.json()["results"][0]["score"] == 0.016
    diagnostic = app.state.search_view_shadow_diagnostics
    assert diagnostic["classification"] == "expected_policy_delta"
    assert diagnostic["expected_semantic_delta"] is True
    assert diagnostic["pruned_tokens"] == ("bodyhot",)


def test_shadow_failure_is_fail_open_and_does_not_change_legacy_response(
    shadow_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, route = shadow_client
    app.state.search_view_shadow_pin = _target(tmp_path)

    def fail(*_args):
        raise RuntimeError("shadow unavailable")

    monkeypatch.setattr(route, "_run_shadow_search", fail)

    response = client.get("/api/search", params={"q": "alpha"})

    assert response.status_code == 200
    assert response.json()["results"][0]["slug"] == "alpha"
    diagnostic = app.state.search_view_shadow_diagnostics
    assert diagnostic["classification"] == "shadow_error"
    assert diagnostic["error"] == "RuntimeError: shadow unavailable"