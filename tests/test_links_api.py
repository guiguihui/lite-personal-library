from pathlib import Path

from fastapi.testclient import TestClient

from app.config.schema import AppConfig
from app.http.server import create_app
from app.knowledge.indexer import build_link_index


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_link_api_contract(tmp_path: Path) -> None:
    data = tmp_path / "data"
    content, pageindex = data / "content", data / "pageindex"
    _write(content / "notes" / "a.md", "---\nid: note:a\ntitle: A\n---\n[[paper:p]]\n")
    _write(content / "papers" / "p" / "_index.md", "---\nid: paper:p\ntitle: P\nstatus: reviewed\n---\nText\n")
    pageindex.mkdir(parents=True)
    build_link_index(content, pageindex)
    cfg = AppConfig(str(content), str(pageindex), str(data / "config"), str(data / "pdfs"))
    client = TestClient(create_app(cfg))

    resolved = client.post("/api/links/resolve", json={"current_id": "note:a", "targets": [{"target": "paper:p"}]})
    assert resolved.status_code == 200
    assert resolved.json()["results"][0]["id"] == "paper:p"
    assert client.get("/api/links/backlinks", params={"id": "paper:p"}).json()["total"] == 1
    graph = client.get("/api/links/neighborhood", params={"id": "paper:p"}).json()
    assert {node["id"] for node in graph["nodes"]} == {"note:a", "paper:p"}
    assert client.get("/api/links/preview", params={"id": "paper:p"}).json()["governance"]["status"] == "reviewed"
    assert client.get("/api/links/diagnostics").json()["summary"]["broken"] == 0


def test_link_api_degrades_when_index_missing(tmp_path: Path) -> None:
    cfg = AppConfig(str(tmp_path / "content"), str(tmp_path / "pageindex"), str(tmp_path / "config"), str(tmp_path / "pdfs"))
    response = TestClient(create_app(cfg)).get("/api/links/backlinks", params={"id": "note:none"})
    assert response.status_code == 503
