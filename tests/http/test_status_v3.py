from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config.schema import AppConfig
from app.http.server import create_app


@pytest.fixture
def status_client(tmp_path: Path) -> TestClient:
    for name in ("content", "pageindex", "config", "pdfs"):
        (tmp_path / name).mkdir()
    cfg = AppConfig(
        content_dir=str(tmp_path / "content"),
        pageindex_dir=str(tmp_path / "pageindex"),
        config_dir=str(tmp_path / "config"),
        pdfs_dir=str(tmp_path / "pdfs"),
    )
    return TestClient(create_app(cfg))


def test_status_reports_the_published_v3_identity(
    status_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.http.routes_status as route

    monkeypatch.setattr(
        route,
        "load_current",
        lambda _path: SimpleNamespace(
            pin=SimpleNamespace(generation="a" * 64, view_id="b" * 64)
        ),
    )

    response = status_client.get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert body["index_ready"] is True
    assert body["index_version"] == "v3"
    assert body["generation"] == "a" * 64
    assert body["view_id"] == "b" * 64
