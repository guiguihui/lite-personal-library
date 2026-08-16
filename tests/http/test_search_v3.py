from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.schema import AppConfig
from app.http.server import create_app
from app.retrieval.search import Hit


@pytest.fixture
def v3_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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

    return app, TestClient(app), route, tmp_path / "pageindex"


def _hit() -> Hit:
    return Hit(
        node={
            "doc_id": "alpha",
            "node_id": "0001",
            "title": "Alpha section",
            "breadcrumb": ["Alpha", "Section"],
            "summary": "short",
        },
        chunk={
            "legacy_node_id": "0001",
            "title": "Alpha section",
            "breadcrumb": ["Alpha", "Section"],
            "body": "complete V3 body",
            "source_md": "content/notes/alpha.md",
            "line_num": 4,
            "line_end": 9,
        },
        score=0.125,
        tokens=["alpha"],
        generation="a" * 64,
        view_id="b" * 64,
        doc_key="note:alpha",
        doc_uid="c" * 64,
        segment_hash="d" * 64,
        local_id=2,
        node_key="section",
    )


def test_search_without_v3_publication_returns_empty(v3_client) -> None:
    _app, client, _route, _pageindex = v3_client

    response = client.get("/api/search", params={"q": "alpha"})

    assert response.status_code == 200
    assert response.json() == {"query": "alpha", "results": []}


def test_search_serves_v3_hit_and_source_metadata(
    v3_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client, route, pageindex = v3_client
    (pageindex / route.CURRENT_POINTER).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(route, "_search_v3", lambda *_args: [_hit()])

    response = client.get("/api/search", params={"q": "alpha", "limit": 10})

    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item == {
        "type": "chunk",
        "doc_type": "note",
        "slug": "alpha",
        "node_id": "0001",
        "title": "Alpha section",
        "breadcrumb": "Alpha > Section",
        "text": "complete V3 body",
        "score": 0.125,
        "generation": "a" * 64,
        "view_id": "b" * 64,
        "doc_key": "note:alpha",
        "doc_uid": "c" * 64,
        "segment_hash": "d" * 64,
        "local_id": 2,
        "node_key": "section",
        "source_md": "content/notes/alpha.md",
        "line_num": 4,
        "line_end": 9,
    }


def test_search_v3_failure_is_reported_as_unavailable(
    v3_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client, route, pageindex = v3_client
    (pageindex / route.CURRENT_POINTER).write_text("{}", encoding="utf-8")

    def fail(*_args):
        raise RuntimeError("invalid pin")

    monkeypatch.setattr(route, "_search_v3", fail)
    response = client.get("/api/search", params={"q": "alpha"})

    assert response.status_code == 503
    assert "PageIndex V3 search unavailable" in response.json()["detail"]