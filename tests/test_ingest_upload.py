from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.schema import AppConfig
from app.http.server import create_app
from app.ingest import jobs
from app.ingest.preflight import PreflightError, safe_filename, validate_slug


@pytest.fixture(autouse=True)
def _clear_jobs():
    jobs._jobs.clear()
    yield
    jobs._jobs.clear()


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    for name in ("content", "pageindex", "config", "pdfs"):
        (tmp_path / name).mkdir()
    return AppConfig(
        content_dir=str(tmp_path / "content"),
        pageindex_dir=str(tmp_path / "pageindex"),
        config_dir=str(tmp_path / "config"),
        pdfs_dir=str(tmp_path / "pdfs"),
        pdf_strategy="local",
        http_host="127.0.0.1",
        http_port=8765,
        use_llm_proxy=False,
    )


@pytest.fixture
def client(cfg: AppConfig) -> TestClient:
    return TestClient(create_app(cfg))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("../../book.pdf", "book.pdf"), ("中文 书.epub", "中文 书.epub"), ("A.PDF", "A.PDF")],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


@pytest.mark.parametrize("raw", ["", ".", "..", "CON.pdf", "bad\x00.pdf"])
def test_safe_filename_rejects_unsafe_names(raw: str) -> None:
    with pytest.raises(PreflightError):
        safe_filename(raw)


@pytest.mark.parametrize("slug", ["", ".", "..", "../escape", "a/b", "a\\b", "bad.", "bad "])
def test_validate_slug_rejects_unsafe_values(slug: str, cfg: AppConfig) -> None:
    with pytest.raises(PreflightError):
        validate_slug(slug, cfg.pdfs_dir)


def test_capabilities_do_not_advertise_docx(client: TestClient) -> None:
    response = client.get("/api/ingest/capabilities")
    assert response.status_code == 200
    data = response.json()
    assert data["formats"]["pdf"]["available"] is True
    assert data["formats"]["epub"]["available"] is True
    assert data["formats"]["docx"]["available"] is False


def test_browser_upload_stages_real_bytes_before_job(client: TestClient, cfg: AppConfig) -> None:
    response = client.post(
        "/api/ingest/upload",
        data={"request": json.dumps({"doc_type": "book", "slug": "browser-upload", "extract_strategy": "local", "network_policy": "offline", "stages": ["extract"]})},
        files={"file": ("Browser Book.pdf", b"%PDF-1.4\n% test\n", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    job = jobs.get_job(response.json()["job_id"])
    assert job is not None
    source = Path(job.input_pdf)
    assert source.is_file()
    assert source.read_bytes().startswith(b"%PDF-")
    assert Path(cfg.pdfs_dir) / "_uploads" in source.parents


def test_upload_rejects_signature_mismatch_without_job(client: TestClient) -> None:
    response = client.post(
        "/api/ingest/upload",
        data={"request": json.dumps({"doc_type": "book", "slug": "bad-signature", "network_policy": "offline", "stages": ["extract"]})},
        files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "SIGNATURE_MISMATCH"
    assert jobs.list_jobs() == []


def test_full_missing_source_fails_synchronously_without_job(client: TestClient) -> None:
    response = client.post(
        "/api/ingest/full",
        json={"input_pdf": "missing.pdf", "doc_type": "book", "slug": "missing-source", "stages": ["extract"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SOURCE_NOT_FOUND"
    assert jobs.list_jobs() == []
