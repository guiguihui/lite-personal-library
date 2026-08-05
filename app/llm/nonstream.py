"""非流式 LLM 调用封装(供入库翻译流水线用)。

与 proxy.py 的流式转发互补:translate_chapters 需要一次性拿完整译文写盘 +
质量校验,不能消费 SSE 流。本模块复用 build_request 的协议判定(anthropic/
openai),用 httpx 发非流式 POST 并按协议解析响应。

零耦合:只依赖 app.llm.providers + httpx,不依赖 http/index/ingest。
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from app.llm.providers import build_request, resolve_protocol


class LLMError(Exception):
    """非流式 LLM 调用错误。

    status_code: HTTP 状态码(网络错误时为 None)。
    body: 上游响应体(供调用方诊断 4xx/5xx)。
    """

    def __init__(self, reason: str, status_code: int | None = None, body: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.body = body


async def call_llm_once(
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    system: str,
    messages: list[Mapping[str, Any]],
    max_tokens: int = 4096,
    thinking: bool = False,
    protocol: str = "auto",
    path_mode: str = "auto",
    timeout: float = 300.0,
) -> str:
    """非流式调用 LLM,返回完整响应文本。

    复用 build_request(stream=False) 构造请求,httpx 发 POST,按协议解析:
      - anthropic: data["content"][0]["text"](空 content 数组返回 "")
      - openai: data["choices"][0]["message"]["content"]

    失败抛 LLMError(含 status_code/body)。调用方负责重试与降级。
    """
    url, headers, body_json = build_request(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        tools=None,
        thinking=thinking,
        protocol=protocol,
        path_mode=path_mode,
        stream=False,
    )

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            resp = await client.post(url, headers=headers, content=body_json)
    except httpx.TimeoutException as exc:
        raise LLMError(f"timeout after {timeout}s: {exc}") from exc
    except httpx.ConnectError as exc:
        raise LLMError(f"connect error: {exc}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"http error: {exc}") from exc

    if resp.status_code >= 400:
        raise LLMError(
            f"HTTP {resp.status_code}",
            status_code=resp.status_code,
            body=resp.text,
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMError("invalid JSON response", body=resp.text) from exc

    return _extract_text(data, provider, protocol)


def _extract_text(data: Mapping[str, Any], provider: str, protocol: str) -> str:
    """按协议从响应 JSON 提取 assistant 文本。"""
    if resolve_protocol(provider, protocol) == "anthropic":
        content = data.get("content") or []
        if not content:
            return ""
        first = content[0] if isinstance(content, list) else {}
        return str(first.get("text", "")) if isinstance(first, Mapping) else ""

    # OpenAI 兼容协议
    choices = data.get("choices") or []
    if not choices:
        return ""
    first = choices[0] if isinstance(choices, list) else {}
    message = first.get("message", {}) if isinstance(first, Mapping) else {}
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    return "" if content is None else str(content)
