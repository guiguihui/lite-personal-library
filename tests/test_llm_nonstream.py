"""pytest 单元测试:非流式 LLM 调用封装(app.llm.nonstream)。

覆盖 call_llm_once 的协议适配 + 响应解析 + 错误路径:
  - anthropic/openai 两协议的成功解析(content[0].text / choices[0].message.content)
  - 请求构造:stream=false、headers(x-api-key / Authorization Bearer)
  - HTTP 4xx/5xx 抛 LLMError(含 status_code/body)
  - 超时、连接错误、非法 JSON、空 content 不崩

用 httpx.MockTransport mock(无新依赖)。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

import httpx
import pytest

from app.llm.nonstream import LLMError, call_llm_once


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════


def _make_client(handler, timeout: float = 300.0) -> httpx.AsyncClient:
    """构造一个用 MockTransport 的 AsyncClient(供 call_llm_once 替换)。

    call_llm_once 内部自己 new AsyncClient,所以测试用 monkeypatch 替换
    httpx.AsyncClient,把 transport 注入进去。
    """
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout, connect=10.0))


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, handler, timeout: float = 300.0) -> None:
    """让 call_llm_once 内部的 httpx.AsyncClient 用 MockTransport。

    保存原始 AsyncClient,避免 factory 内部调用自身导致递归。
    """
    real_async_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        # 忽略 call_llm_once 传的 timeout,用测试的 transport
        kwargs.pop("timeout", None)
        return real_async_client(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    monkeypatch.setattr("app.llm.nonstream.httpx.AsyncClient", factory)


def _json_response(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload), headers={"content-type": "application/json"})


MESSAGES = [{"role": "user", "content": "ping"}]


# ══════════════════════════════════════════════════════════════════════════
# 成功路径
# ══════════════════════════════════════════════════════════════════════════


class TestCallSuccess:
    def test_anthropic_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            captured["headers"] = dict(req.headers)
            captured["body"] = json.loads(req.content)
            return _json_response(200, {"content": [{"type": "text", "text": "你好"}]})

        _patch_async_client(monkeypatch, handler)

        text = asyncio.run(
            call_llm_once(
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
        )
        assert text == "你好"
        # URL 不拼后缀(full 模式)
        assert captured["url"] == "http://lanz.hikvision.com/v3/anthropic/model"
        # stream=false
        assert captured["body"]["stream"] is False
        # anthropic 头
        assert captured["headers"]["x-api-key"] == "sk-test"
        assert "authorization" not in {k.lower() for k in captured["headers"]}
        # system 在 body 顶层(anthropic 协议)
        assert captured["body"]["system"] == "sys"

    def test_openai_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(req.headers)
            captured["body"] = json.loads(req.content)
            return _json_response(200, {"choices": [{"message": {"content": "hello"}}]})

        _patch_async_client(monkeypatch, handler)

        text = asyncio.run(
            call_llm_once(
                provider="deepseek",
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                api_key="sk-test",
                system="sys",
                messages=MESSAGES,
                protocol="openai",
                path_mode="auto",
            )
        )
        assert text == "hello"
        # Bearer 头
        assert captured["headers"]["authorization"] == "Bearer sk-test"
        assert "x-api-key" not in {k.lower() for k in captured["headers"]}
        # stream=false
        assert captured["body"]["stream"] is False
        # openai 协议 system 拼到 messages[0]
        assert captured["body"]["messages"][0] == {"role": "system", "content": "sys"}

    def test_anthropic_empty_content_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _json_response(200, {"content": []})

        _patch_async_client(monkeypatch, handler)
        text = asyncio.run(
            call_llm_once("anthropic", "m", "https://api.anthropic.com", "sk", "s", MESSAGES)
        )
        assert text == ""

    def test_openai_empty_choices_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _json_response(200, {"choices": []})

        _patch_async_client(monkeypatch, handler)
        text = asyncio.run(
            call_llm_once("openai", "m", "https://api.openai.com", "sk", "s", MESSAGES)
        )
        assert text == ""

    def test_openai_null_content_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # OpenAI 有时 content 为 null(如 tool_use only 响应)
        def handler(req: httpx.Request) -> httpx.Response:
            return _json_response(200, {"choices": [{"message": {"content": None}}]})

        _patch_async_client(monkeypatch, handler)
        text = asyncio.run(
            call_llm_once("openai", "m", "https://api.openai.com", "sk", "s", MESSAGES)
        )
        assert text == ""


# ══════════════════════════════════════════════════════════════════════════
# 错误路径
# ══════════════════════════════════════════════════════════════════════════


class TestCallErrors:
    def test_http_4xx_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b'{"error":"invalid key"}', headers={"content-type": "application/json"})

        _patch_async_client(monkeypatch, handler)
        with pytest.raises(LLMError) as exc:
            asyncio.run(call_llm_once("anthropic", "m", "https://x", "sk", "s", MESSAGES))
        assert exc.value.status_code == 401
        assert "HTTP 401" in str(exc.value)
        assert "invalid key" in exc.value.body

    def test_http_5xx_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"upstream error")

        _patch_async_client(monkeypatch, handler)
        with pytest.raises(LLMError) as exc:
            asyncio.run(call_llm_once("openai", "m", "https://x", "sk", "s", MESSAGES))
        assert exc.value.status_code == 500

    def test_invalid_json_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json at all", headers={"content-type": "text/plain"})

        _patch_async_client(monkeypatch, handler)
        with pytest.raises(LLMError) as exc:
            asyncio.run(call_llm_once("anthropic", "m", "https://x", "sk", "s", MESSAGES))
        assert "invalid JSON" in str(exc.value)
        assert "not json" in exc.value.body

    def test_timeout_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        _patch_async_client(monkeypatch, handler, timeout=0.5)
        with pytest.raises(LLMError, match="timeout"):
            asyncio.run(call_llm_once("anthropic", "m", "https://x", "sk", "s", MESSAGES, timeout=0.5))

    def test_connect_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _patch_async_client(monkeypatch, handler)
        with pytest.raises(LLMError, match="connect error"):
            asyncio.run(call_llm_once("anthropic", "m", "https://x", "sk", "s", MESSAGES))


# ══════════════════════════════════════════════════════════════════════════
# LLMError 属性
# ══════════════════════════════════════════════════════════════════════════


class TestLLMError:
    def test_attributes(self) -> None:
        err = LLMError("boom", status_code=502, body="bad gateway")
        assert err.reason == "boom"
        assert err.status_code == 502
        assert err.body == "bad gateway"
        assert str(err) == "boom"

    def test_defaults(self) -> None:
        err = LLMError("network")
        assert err.status_code is None
        assert err.body == ""
