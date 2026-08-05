"""pytest 单元测试:LLM provider 协议 + 路径适配(app.llm.providers)。

覆盖 resolve_protocol / resolve_endpoint / build_request 的核心矩阵:
  - 自定义端点场景(http://lanz.hikvision.com/v3/anthropic/model + anthropic + full)
  - 内置 provider 向后兼容(anthropic/deepseek/dashscope 等 auto 模式)
  - 已知后缀检测(/v1/messages、/v1/chat/completions)
  - 非法 protocol/path_mode 取值校验(经 routes_settings)
  - llm.yaml 读写 roundtrip + 老 yaml 向后兼容
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.schema import AppConfig, LlmConfig, LlmProviderConfig
from app.config.store import load_llm_config, save_llm_config
from app.http.server import create_app
from app.llm.providers import (
    build_request,
    resolve_endpoint,
    resolve_protocol,
)


# ══════════════════════════════════════════════════════════════════════════
# resolve_protocol
# ══════════════════════════════════════════════════════════════════════════


class TestResolveProtocol:
    """协议判定:auto 按 provider 推断,显式值覆盖。"""

    @pytest.mark.parametrize(
        "provider,protocol,expected",
        [
            ("anthropic", "auto", "anthropic"),
            ("deepseek", "auto", "openai"),
            ("openai", "auto", "openai"),
            ("custom", "auto", "openai"),
            ("custom", "anthropic", "anthropic"),
            ("custom", "openai", "openai"),
            ("anthropic", "openai", "openai"),  # 显式覆盖
            ("deepseek", "anthropic", "anthropic"),  # 显式覆盖
        ],
    )
    def test_resolve(self, provider: str, protocol: str, expected: str) -> None:
        assert resolve_protocol(provider, protocol) == expected

    def test_default_auto(self) -> None:
        """不传 protocol 默认 auto。"""
        assert resolve_protocol("custom") == "openai"
        assert resolve_protocol("anthropic") == "anthropic"


# ══════════════════════════════════════════════════════════════════════════
# resolve_endpoint
# ══════════════════════════════════════════════════════════════════════════


class TestResolveEndpoint:
    """路径拼接:full 直接用 / auto 检测已知后缀 / suffix 强制拼。"""

    @pytest.mark.parametrize(
        "base_url,protocol,path_mode,expected",
        [
            # 用户场景:完整路径,不拼后缀
            # 注意:此用例验证 full 语义(不拼后缀)。澜智端点实测要求 /v1/messages
            # 后缀,实际配置用 auto(见 data/config/llm.yaml),full 会被上游 400 拒绝。
            (
                "http://lanz.hikvision.com/v3/anthropic/model",
                "anthropic",
                "full",
                "http://lanz.hikvision.com/v3/anthropic/model",
            ),
            # 内置 provider 向后兼容:auto 模式补后缀
            ("https://api.anthropic.com", "anthropic", "auto", "https://api.anthropic.com/v1/messages"),
            ("https://api.deepseek.com", "openai", "auto", "https://api.deepseek.com/v1/chat/completions"),
            ("https://api.openai.com", "openai", "auto", "https://api.openai.com/v1/chat/completions"),
            # 已知后缀:auto 模式直接用
            ("http://x.com/v1/messages", "anthropic", "auto", "http://x.com/v1/messages"),
            ("http://x.com/v1/chat/completions", "openai", "auto", "http://x.com/v1/chat/completions"),
            ("http://x.com/messages", "anthropic", "auto", "http://x.com/messages"),
            ("http://x.com/chat/completions", "openai", "auto", "http://x.com/chat/completions"),
            # 尾部斜杠:去除后拼接
            ("http://x.com/", "openai", "auto", "http://x.com/v1/chat/completions"),
            # suffix:强制拼(即使已含后缀也拼 — 极少用但保持语义)
            ("http://x.com", "openai", "suffix", "http://x.com/v1/chat/completions"),
            # full:即使 URL 看起来像只有域名,也直接用(用户明示)
            ("http://x.com:8080/any/path", "openai", "full", "http://x.com:8080/any/path"),
            # 空字符串兜底
            ("", "openai", "auto", "/v1/chat/completions"),
            ("", "anthropic", "full", ""),
        ],
    )
    def test_resolve(
        self, base_url: str, protocol: str, path_mode: str, expected: str
    ) -> None:
        assert resolve_endpoint(base_url, protocol, path_mode) == expected

    def test_default_path_mode_auto(self) -> None:
        """不传 path_mode 默认 auto。"""
        assert resolve_endpoint("https://api.anthropic.com", "anthropic") == (
            "https://api.anthropic.com/v1/messages"
        )


# ══════════════════════════════════════════════════════════════════════════
# build_request
# ══════════════════════════════════════════════════════════════════════════


MESSAGES = [{"role": "user", "content": "ping"}]


class TestBuildRequest:
    """build_request 整合 resolve_protocol + resolve_endpoint + body/headers。"""

    def test_custom_anthropic_full_path(self) -> None:
        """custom + anthropic + full → URL 不拼后缀,x-api-key 头。

        注意:此用例验证 resolve_endpoint 的 full 语义(不拼后缀),
        **不代表澜智端点应配 full**。实测澜智上游要求路径以 /v1/messages
        结尾,full 会被 400 拒绝(见 data/config/llm.yaml custom 用 auto)。
        """
        url, headers, body = build_request(
            provider="custom",
            model="EB-GLM-5.2",
            base_url="http://lanz.hikvision.com/v3/anthropic/model",
            api_key="sk-test",
            system="sys",
            messages=MESSAGES,
            max_tokens=8,
            protocol="anthropic",
            path_mode="full",
        )
        assert url == "http://lanz.hikvision.com/v3/anthropic/model"
        assert headers["x-api-key"] == "sk-test"
        assert "Authorization" not in headers
        assert headers["anthropic-version"] == "2023-06-01"
        assert '"model": "EB-GLM-5.2"' in body

    def test_custom_openai_full_path(self) -> None:
        """custom + openai + full → URL 不拼后缀,Bearer 头。"""
        url, headers, _ = build_request(
            provider="custom",
            model="gpt-custom",
            base_url="http://internal.corp/llm/v1",
            api_key="sk-test",
            system="sys",
            messages=MESSAGES,
            protocol="openai",
            path_mode="full",
        )
        assert url == "http://internal.corp/llm/v1"
        assert headers["Authorization"] == "Bearer sk-test"
        assert "x-api-key" not in headers

    def test_regression_anthropic_auto(self) -> None:
        """回归:内置 anthropic + auto → 老路径 + 老头。"""
        url, headers, _ = build_request(
            provider="anthropic",
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.com",
            api_key="sk-test",
            system="sys",
            messages=MESSAGES,
        )
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["x-api-key"] == "sk-test"

    def test_regression_deepseek_auto(self) -> None:
        """回归:deepseek + auto → openai 路径 + Bearer + thinking 字段。"""
        url, headers, body = build_request(
            provider="deepseek",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key="sk-test",
            system="sys",
            messages=MESSAGES,
            thinking=True,
        )
        assert url == "https://api.deepseek.com/v1/chat/completions"
        assert headers["Authorization"] == "Bearer sk-test"
        assert '"thinking": {"type": "enabled"}' in body

    def test_tools_anthropic(self) -> None:
        """anthropic 协议的 tools 转成 input_schema 格式。"""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        _, _, body = build_request(
            provider="anthropic",
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.com",
            api_key="sk-test",
            system="sys",
            messages=MESSAGES,
            tools=tools,
        )
        assert '"input_schema"' in body
        assert '"tool_choice": {"type": "auto"}' in body

    # ── stream 参数(入库翻译非流式调用方用)──────────────────────────────

    def test_stream_false_anthropic(self) -> None:
        """stream=False → anthropic body 含 "stream": false。"""
        _, _, body = build_request(
            provider="anthropic",
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.com",
            api_key="sk-test",
            system="sys",
            messages=MESSAGES,
            stream=False,
        )
        assert '"stream": false' in body

    def test_stream_false_openai(self) -> None:
        """stream=False → openai body 含 "stream": false。"""
        _, _, body = build_request(
            provider="openai",
            model="gpt-4o",
            base_url="https://api.openai.com",
            api_key="sk-test",
            system="sys",
            messages=MESSAGES,
            stream=False,
        )
        assert '"stream": false' in body

    def test_stream_default_true_anthropic(self) -> None:
        """不传 stream → 默认 True(流式行为回归保护)。"""
        _, _, body = build_request(
            provider="anthropic",
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.com",
            api_key="sk-test",
            system="sys",
            messages=MESSAGES,
        )
        assert '"stream": true' in body


# ══════════════════════════════════════════════════════════════════════════
# 配置 roundtrip + 向后兼容
# ══════════════════════════════════════════════════════════════════════════


class TestConfigRoundtrip:
    """llm.yaml 读写 roundtrip + 老 yaml(无 protocol/path_mode)向后兼容。"""

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        cfg = LlmConfig(
            active_provider="custom",
            providers={
                "custom": LlmProviderConfig(
                    provider="custom",
                    model="EB-GLM-5.2",
                    base_url="http://lanz.hikvision.com/v3/anthropic/model",
                    has_key=True,
                    protocol="anthropic",
                    path_mode="full",
                )
            },
            remember_key=False,
        )
        save_llm_config(cfg, str(tmp_path))
        loaded = load_llm_config(str(tmp_path))
        p = loaded.providers["custom"]
        assert p.protocol == "anthropic"
        assert p.path_mode == "full"
        assert p.base_url == "http://lanz.hikvision.com/v3/anthropic/model"

    def test_legacy_yaml_defaults_to_auto(self, tmp_path: Path) -> None:
        """老 llm.yaml 不含 protocol/path_mode → 读出 auto,行为同旧。"""
        import yaml

        path = tmp_path / "llm.yaml"
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "active_provider": "anthropic",
                    "providers": {
                        "anthropic": {
                            "model": "claude-sonnet-4-6",
                            "base_url": "https://api.anthropic.com",
                            "has_key": True,
                        }
                    },
                },
                f,
            )
        loaded = load_llm_config(str(tmp_path))
        p = loaded.providers["anthropic"]
        assert p.protocol == "auto"
        assert p.path_mode == "auto"


# ══════════════════════════════════════════════════════════════════════════
# HTTP API:settings 读写 + 校验
# ══════════════════════════════════════════════════════════════════════════


def _make_cfg(data_dir: Path) -> AppConfig:
    for sub in ("content", "pageindex", "config", "pdfs"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    return AppConfig(
        content_dir=str(data_dir / "content"),
        pageindex_dir=str(data_dir / "pageindex"),
        config_dir=str(data_dir / "config"),
        pdfs_dir=str(data_dir / "pdfs"),
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = _make_cfg(tmp_path / "data")
    return TestClient(create_app(cfg))


class TestSettingsApi:
    """/api/settings 读写 protocol/path_mode + 取值校验。"""

    def test_get_returns_protocol_path_mode(self, client: TestClient) -> None:
        r = client.get("/api/settings")
        assert r.status_code == 200
        p = r.json()["providers"]["anthropic"]
        assert p["protocol"] == "auto"
        assert p["path_mode"] == "auto"

    def test_put_custom_endpoint(self, client: TestClient) -> None:
        """PUT custom 端点的 protocol/path_mode/base_url/model/active。"""
        for key, value in [
            ("active_provider", "custom"),
            ("base_url", "http://lanz.hikvision.com/v3/anthropic/model"),
            ("model", "EB-GLM-5.2"),
            ("protocol", "anthropic"),
            ("path_mode", "full"),
        ]:
            r = client.put(
                "/api/settings",
                json={
                    "key": key,
                    "value": value,
                    "provider": "custom" if key != "active_provider" else None,
                },
            )
            assert r.status_code == 200, (key, r.status_code, r.text)
        # 重读确认
        r = client.get("/api/settings")
        p = r.json()["providers"]["custom"]
        assert p["base_url"] == "http://lanz.hikvision.com/v3/anthropic/model"
        assert p["protocol"] == "anthropic"
        assert p["path_mode"] == "full"
        assert r.json()["active_provider"] == "custom"

    @pytest.mark.parametrize(
        "key,value",
        [
            ("protocol", "bogus"),
            ("protocol", "Anthropic"),  # 大小写敏感
            ("path_mode", "bogus"),
            ("path_mode", "FULL"),
        ],
    )
    def test_invalid_values_rejected(
        self, client: TestClient, key: str, value: str
    ) -> None:
        r = client.put(
            "/api/settings",
            json={"key": key, "value": value, "provider": "custom"},
        )
        assert r.status_code == 400

    def test_providers_endpoint_has_defaults(self, client: TestClient) -> None:
        """/api/settings/providers 的 defaults 含 protocol/path_mode。"""
        r = client.get("/api/settings/providers")
        assert r.status_code == 200
        anth = r.json()["defaults"]["anthropic"]
        assert anth["protocol"] == "auto"
        assert anth["path_mode"] == "auto"
