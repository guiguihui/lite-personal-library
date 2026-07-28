"""pytest 集成测试:HTTP API 端点(app/http/routes_*)。

用 FastAPI TestClient + tmp_path fixture 构造隔离的 data 目录,
不依赖真实 data/content 或 data/pageindex。

覆盖端点:
  GET  /                    根重定向
  GET  /frontend/index.html 前端静态
  GET  /raw/content/<path>  md 原文 + 路径越界 403
  GET  /pageindex/<path>    索引 JSON + 路径越界 403
  GET  /api/settings        BYOK 配置(不返回 key)
  PUT  /api/settings        更新配置(active_provider/use_llm_proxy/model)
  GET  /api/settings/key    读 key(明文降级路径)
  GET  /api/content/docs    Library 文档列表
  GET  /api/content/read    文档树
  GET  /api/content/section 正文片段
  POST /api/index/build     触发构建(后台线程,验证 job_id 返回)
  GET  /api/index/build/{id} 轮询状态
  GET  /api/index/jobs      列任务
  POST /api/ingest/extract  触发提取(验证 job_id)
  GET  /api/ingest/{id}     轮询
  GET  /api/ingest/jobs     列任务
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config.schema import AppConfig
from app.http.server import create_app


# ══════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════


def _make_cfg(data_dir: Path) -> AppConfig:
    """构造指向 tmp data/ 的 AppConfig。"""
    for sub in ("content", "pageindex", "config", "pdfs"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    return AppConfig(
        content_dir=str(data_dir / "content"),
        pageindex_dir=str(data_dir / "pageindex"),
        config_dir=str(data_dir / "config"),
        pdfs_dir=str(data_dir / "pdfs"),
        pdf_strategy="local",
        http_host="127.0.0.1",
        http_port=8765,
        use_llm_proxy=False,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    """隔离的 AppConfig(tmp_path/data)。"""
    return _make_cfg(tmp_path / "data")


@pytest.fixture
def client(cfg: AppConfig) -> TestClient:
    """TestClient(挂载真实 FastAPI app,不走网络)。"""
    app = create_app(cfg)
    return TestClient(app)


@pytest.fixture
def cfg_with_content(tmp_path: Path) -> AppConfig:
    """带最小 content + pageindex 的 cfg(从真实 data 复制 about.md + global-index)。"""
    cfg = _make_cfg(tmp_path / "data")
    # __file__ = .../yuulibrary-desktop/tests/test_http_api.py → parent.parent = 项目根
    root = Path(__file__).resolve().parent.parent
    src_content = root / "data" / "content"
    src_pageindex = root / "data" / "pageindex"
    # 复制 about.md + _index.md
    for name in ("about.md", "_index.md"):
        src = src_content / name
        if src.exists():
            shutil.copy2(src, Path(cfg.content_dir) / name)
    # 复制 global-index.json + node-index.json(供 content/docs + pageindex 端点)
    if src_pageindex.exists():
        for name in ("global-index.json", "node-index.json"):
            src = src_pageindex / name
            if src.exists():
                shutil.copy2(src, Path(cfg.pageindex_dir) / name)
        # 复制所有 book 的 structure json 供 /api/content/read(global-index 引用)
        books_src = src_pageindex / "books"
        books_dst = Path(cfg.pageindex_dir) / "books"
        books_dst.mkdir(exist_ok=True)
        if books_src.exists():
            for p in books_src.glob("*.json"):
                shutil.copy2(p, books_dst / p.name)
    return cfg


@pytest.fixture
def client_with_content(cfg_with_content: AppConfig) -> TestClient:
    """带内容的 TestClient。"""
    return TestClient(create_app(cfg_with_content))


# ══════════════════════════════════════════════════════════════════════════
# 根 + 前端静态
# ══════════════════════════════════════════════════════════════════════════


class TestRootAndStatic:
    def test_root_redirects_to_frontend(self, client: TestClient) -> None:
        # 根路径 307 重定向到 /frontend/index.html
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (301, 302, 303, 307, 308)
        assert "/frontend/index.html" in r.headers.get("location", "")

    def test_frontend_index_html_served(self, client: TestClient) -> None:
        r = client.get("/frontend/index.html")
        assert r.status_code == 200
        assert "<html" in r.text.lower()

    def test_openapi_available(self, client: TestClient) -> None:
        r = client.get("/api/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert "paths" in spec
        # 关键端点都在
        paths = spec["paths"]
        assert "/api/settings" in paths
        assert "/raw/content/{path}" in paths


# ══════════════════════════════════════════════════════════════════════════
# /raw/content/<path>
# ══════════════════════════════════════════════════════════════════════════


class TestRawContent:
    def test_read_markdown(self, client_with_content: TestClient) -> None:
        r = client_with_content.get("/raw/content/about.md")
        assert r.status_code == 200
        # 返回 md 原文(含 front matter)
        assert "---" in r.text or len(r.text) > 0

    def test_not_found(self, client: TestClient) -> None:
        r = client.get("/raw/content/nonexistent.md")
        assert r.status_code == 404

    def test_path_traversal_blocked(self, client: TestClient) -> None:
        # 字面 ../ 会被 Starlette 规范化(→ 404,不在 /raw/content/ 前缀下)
        # URL 编码的 ..%2F 才到达路由层触发 resolve_content_path 的 403
        r1 = client.get("/raw/content/../../../etc/passwd")
        assert r1.status_code in (403, 404)
        r2 = client.get("/raw/content/..%2F..%2Fetc%2Fpasswd")
        assert r2.status_code == 403
        # 两种情况都不应返回系统文件内容
        assert "root:" not in r1.text and "root:" not in r2.text


# ══════════════════════════════════════════════════════════════════════════
# /pageindex/<path>
# ══════════════════════════════════════════════════════════════════════════


class TestPageindex:
    def test_read_global_index(self, client_with_content: TestClient) -> None:
        r = client_with_content.get("/pageindex/global-index.json")
        assert r.status_code == 200
        data = r.json()
        assert "docs" in data

    def test_not_found(self, client: TestClient) -> None:
        r = client.get("/pageindex/nonexistent.json")
        assert r.status_code == 404

    def test_path_traversal_blocked(self, client: TestClient) -> None:
        # 字面 ../ 被 Starlette 规范化(→ 404);编码 ..%2F 到达路由层 → 403
        r1 = client.get("/pageindex/../../../etc/passwd")
        assert r1.status_code in (403, 404)
        r2 = client.get("/pageindex/..%2F..%2Fetc%2Fpasswd")
        assert r2.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# /api/settings
# ══════════════════════════════════════════════════════════════════════════


class TestSettings:
    def test_get_settings_shape(self, client: TestClient) -> None:
        r = client.get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        # 必填字段
        assert "active_provider" in data
        assert "providers" in data
        assert "remember_key" in data
        assert "use_llm_proxy" in data
        assert "has_keyring" in data
        # providers 是 dict,每个含 model/base_url/has_key
        for name, p in data["providers"].items():
            assert "model" in p
            assert "base_url" in p
            assert "has_key" in p
            # has_key 是 bool,不返回 key 本身
            assert isinstance(p["has_key"], bool)

    def test_settings_no_key_leaked(self, client: TestClient) -> None:
        # /api/settings 响应里不应出现任何 key 字段(只有 has_key)
        r = client.get("/api/settings")
        text = r.text
        # 不应含 sk- / api_key 字样(key 本身)
        assert "sk-" not in text
        assert '"api_key"' not in text

    def test_update_active_provider(self, client: TestClient, cfg: AppConfig) -> None:
        # 切换 active_provider 到 deepseek(默认 providers 含 deepseek)
        r = client.put(
            "/api/settings",
            json={"key": "active_provider", "value": "deepseek"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # 验证已写盘 + 读回
        r2 = client.get("/api/settings")
        assert r2.json()["active_provider"] == "deepseek"

    def test_update_unknown_provider_rejected(self, client: TestClient) -> None:
        r = client.put(
            "/api/settings",
            json={"key": "active_provider", "value": "nonexistent_provider"},
        )
        assert r.status_code == 400

    def test_update_use_llm_proxy(self, client: TestClient) -> None:
        r = client.put(
            "/api/settings",
            json={"key": "use_llm_proxy", "value": True},
        )
        assert r.status_code == 200
        r2 = client.get("/api/settings")
        assert r2.json()["use_llm_proxy"] is True

    def test_update_model(self, client: TestClient) -> None:
        r = client.put(
            "/api/settings",
            json={"key": "model", "value": "test-model", "provider": "deepseek"},
        )
        assert r.status_code == 200
        r2 = client.get("/api/settings")
        assert r2.json()["providers"]["deepseek"]["model"] == "test-model"

    def test_update_model_requires_provider(self, client: TestClient) -> None:
        # model/base_url 必须带 provider
        r = client.put(
            "/api/settings",
            json={"key": "model", "value": "x"},
        )
        assert r.status_code == 400

    def test_update_unknown_key_rejected(self, client: TestClient) -> None:
        r = client.put(
            "/api/settings",
            json={"key": "unknown_key", "value": "x"},
        )
        assert r.status_code == 400

    def test_set_and_get_api_key(self, client: TestClient) -> None:
        # set api_key(走 keyring 或明文降级)
        r = client.put(
            "/api/settings",
            json={"key": "api_key", "value": "sk-test-123", "provider": "deepseek"},
        )
        assert r.status_code == 200
        # /api/settings/key 读回
        r2 = client.get("/api/settings/key", params={"provider": "deepseek"})
        assert r2.status_code == 200
        assert r2.json()["api_key"] == "sk-test-123"
        # /api/settings 的 has_key 应变 True
        r3 = client.get("/api/settings")
        assert r3.json()["providers"]["deepseek"]["has_key"] is True

    def test_set_api_key_requires_provider(self, client: TestClient) -> None:
        r = client.put(
            "/api/settings",
            json={"key": "api_key", "value": "sk-test"},
        )
        assert r.status_code == 400

    def test_api_key_survives_subsequent_setting_updates(
        self, client: TestClient
    ) -> None:
        """回归:set api_key 后再 PUT 其他字段(remember_key/model 等)不应丢 key。

        根因:save_llm_config 重写 llm.yaml 时曾丢弃 _plain_keys 明文降级区。
        前端 saveLLM 序列里 api_key 之后还有 remember_key,后者走 save_llm_config
        重写,把刚写的 _plain_keys 冲掉 → get_api_key 返回空 → 401。
        """
        # 1. set api_key
        r = client.put(
            "/api/settings",
            json={"key": "api_key", "value": "sk-survive-123", "provider": "custom"},
        )
        assert r.status_code == 200
        # 2. 模拟前端 saveLLM 后续步骤:remember_key + model + path_mode
        for key, value in [
            ("remember_key", True),
            ("model", "EB-GLM-5.2"),
            ("path_mode", "suffix"),
        ]:
            r = client.put(
                "/api/settings",
                json={
                    "key": key,
                    "value": value,
                    "provider": "custom" if key != "remember_key" else None,
                },
            )
            assert r.status_code == 200, (key, r.text)
        # 3. key 必须仍在
        r = client.get("/api/settings/key", params={"provider": "custom"})
        assert r.status_code == 200
        assert r.json()["api_key"] == "sk-survive-123", "key 被 save_llm_config 冲掉了"
        # 4. has_key 仍为 True
        r = client.get("/api/settings")
        assert r.json()["providers"]["custom"]["has_key"] is True

    def test_get_providers_shape(self, client: TestClient) -> None:
        r = client.get("/api/settings/providers")
        assert r.status_code == 200
        data = r.json()
        assert "names" in data
        assert "defaults" in data
        # 10 个 provider(含 custom)
        assert len(data["names"]) == 10
        assert "anthropic" in data["names"]
        assert "custom" in data["names"]
        # 每个 default 含 model + base_url
        for name in data["names"]:
            d = data["defaults"][name]
            assert "model" in d
            assert "base_url" in d


# ══════════════════════════════════════════════════════════════════════════
# /api/app/config
# ══════════════════════════════════════════════════════════════════════════


class TestAppConfig:
    def test_get_app_config_shape(self, client: TestClient) -> None:
        r = client.get("/api/app/config")
        assert r.status_code == 200
        data = r.json()
        for key in (
            "content_dir",
            "pageindex_dir",
            "pdfs_dir",
            "pdf_strategy",
            "http_host",
            "http_port",
            "use_llm_proxy",
        ):
            assert key in data

    def test_update_pdf_strategy(self, client: TestClient) -> None:
        r = client.put(
            "/api/app/config",
            json={"pdf_strategy": "mineru"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "requires_restart": False}
        # 读回验证
        r2 = client.get("/api/app/config")
        assert r2.json()["pdf_strategy"] == "mineru"

    def test_update_invalid_pdf_strategy_rejected(self, client: TestClient) -> None:
        r = client.put(
            "/api/app/config",
            json={"pdf_strategy": "invalid_strategy"},
        )
        assert r.status_code == 422  # Pydantic pattern 校验失败

    def test_update_use_llm_proxy(self, client: TestClient) -> None:
        r = client.put(
            "/api/app/config",
            json={"use_llm_proxy": True},
        )
        assert r.status_code == 200
        assert r.json()["requires_restart"] is False
        r2 = client.get("/api/app/config")
        assert r2.json()["use_llm_proxy"] is True

    def test_update_port_requires_restart(self, client: TestClient) -> None:
        r = client.put(
            "/api/app/config",
            json={"http_port": 9999},
        )
        assert r.status_code == 200
        assert r.json()["requires_restart"] is True

    def test_update_unsafe_path_rejected(self, client: TestClient) -> None:
        # 系统敏感目录应被拒
        r = client.put(
            "/api/app/config",
            json={"content_dir": "C:\\Windows\\System32"},
        )
        assert r.status_code == 400
        assert "unsafe" in r.json()["detail"].lower()

    def test_update_safe_path_accepted(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        safe_dir = str(tmp_path / "custom_content")
        r = client.put(
            "/api/app/config",
            json={"content_dir": safe_dir},
        )
        assert r.status_code == 200
        # 新目录应被创建
        from pathlib import Path as P

        assert P(safe_dir).is_dir()
        # 读回验证
        r2 = client.get("/api/app/config")
        assert r2.json()["content_dir"] == safe_dir


# ══════════════════════════════════════════════════════════════════════════
# /api/content/*
# ══════════════════════════════════════════════════════════════════════════


class TestContentApi:
    def test_list_docs_books(self, client_with_content: TestClient) -> None:
        r = client_with_content.get("/api/content/docs", params={"type": "books"})
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "book"
        assert isinstance(data["docs"], list)

    def test_list_docs_type_normalize(self, client_with_content: TestClient) -> None:
        # 复数 books 和单数 book 都接受
        r1 = client_with_content.get("/api/content/docs", params={"type": "books"})
        r2 = client_with_content.get("/api/content/docs", params={"type": "book"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["docs"] == r2.json()["docs"]

    def test_list_docs_unknown_type(self, client: TestClient) -> None:
        r = client.get("/api/content/docs", params={"type": "unknown"})
        assert r.status_code == 400

    def test_list_docs_no_global_index(self, client: TestClient) -> None:
        # 空 pageindex → 404
        r = client.get("/api/content/docs", params={"type": "books"})
        assert r.status_code == 404

    def test_read_doc(self, client_with_content: TestClient) -> None:
        # 先列 docs 拿一个 slug
        r = client_with_content.get("/api/content/docs", params={"type": "books"})
        docs = r.json()["docs"]
        if not docs:
            pytest.skip("no books in fixture")
        slug = docs[0]["id"]
        r2 = client_with_content.get(
            "/api/content/read", params={"type": "books", "slug": slug}
        )
        assert r2.status_code == 200
        data = r2.json()
        assert "structure" in data or "doc_name" in data

    def test_read_doc_not_found(self, client_with_content: TestClient) -> None:
        r = client_with_content.get(
            "/api/content/read", params={"type": "books", "slug": "nonexistent"}
        )
        assert r.status_code == 404

    def test_read_section(self, client_with_content: TestClient) -> None:
        # 读 about.md 的前 10 行
        r = client_with_content.get(
            "/api/content/section",
            params={"source_md": "content/about.md", "line_num": 0, "line_end": 10},
        )
        assert r.status_code == 200
        # 返回纯文本片段
        assert isinstance(r.text, str)

    def test_read_section_path_traversal(self, client: TestClient) -> None:
        r = client.get(
            "/api/content/section",
            params={"source_md": "content/../../../etc/passwd"},
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# /api/index/build
# ══════════════════════════════════════════════════════════════════════════


class TestIndexBuild:
    def test_build_returns_job_id(self, client: TestClient) -> None:
        r = client.post("/api/index/build", json={"mode": "incremental"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "running"
        assert data["job_id"].startswith("idx_")

    def test_build_invalid_mode_rejected(self, client: TestClient) -> None:
        r = client.post("/api/index/build", json={"mode": "invalid"})
        assert r.status_code == 422  # pydantic pattern 校验

    def test_get_build_status_unknown_job(self, client: TestClient) -> None:
        r = client.get("/api/index/build/idx_nonexistent")
        assert r.status_code == 404

    def test_build_lifecycle(self, client_with_content: TestClient) -> None:
        # 触发增量构建 → 轮询到 done/failed
        r = client_with_content.post("/api/index/build", json={"mode": "incremental"})
        job_id = r.json()["job_id"]
        # 轮询(最多 30 秒)
        for _ in range(60):
            s = client_with_content.get(f"/api/index/build/{job_id}").json()
            if s["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        assert s["status"] in ("done", "failed")
        assert s["job_id"] == job_id

    def test_list_jobs(self, client: TestClient) -> None:
        r = client.get("/api/index/jobs")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ══════════════════════════════════════════════════════════════════════════
# /api/ingest/*
# ══════════════════════════════════════════════════════════════════════════


class TestIngest:
    def test_extract_returns_job_id(self, client: TestClient) -> None:
        r = client.post(
            "/api/ingest/extract",
            json={
                "input_pdf": "fake.pdf",
                "doc_type": "book",
                "slug": "test-slug",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "running"
        assert data["job_id"].startswith("ing_")

    def test_extract_invalid_doc_type(self, client: TestClient) -> None:
        r = client.post(
            "/api/ingest/extract",
            json={"input_pdf": "x.pdf", "doc_type": "invalid", "slug": "s"},
        )
        assert r.status_code == 422

    def test_get_ingest_status_unknown(self, client: TestClient) -> None:
        r = client.get("/api/ingest/ing_nonexistent")
        assert r.status_code == 404

    def test_list_jobs(self, client: TestClient) -> None:
        r = client.get("/api/ingest/jobs")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_extract_missing_input_fails(self, client: TestClient) -> None:
        # input_pdf 不存在 → 后台线程 failed
        r = client.post(
            "/api/ingest/extract",
            json={"input_pdf": "nonexistent.pdf", "doc_type": "book", "slug": "fail-slug"},
        )
        job_id = r.json()["job_id"]
        for _ in range(40):
            s = client.get(f"/api/ingest/{job_id}").json()
            if s["status"] in ("done", "failed"):
                break
            time.sleep(0.25)
        assert s["status"] == "failed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
