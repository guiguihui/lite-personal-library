"""LLM 后端代理(可选,解决 CORS / Anthropic 直连限制)。

阶段 6:前端 BYOK 直连受限时,改走后端代理。
  - 前端 fetch /api/llm/proxy,传 provider/model/base_url/messages/tools/thinking
  - 后端用 app.llm.providers.build_request 构造请求,httpx 转发 SSE 流
  - key 从 app.config.store.get_api_key 读(前端不传 key)

不参与阶段 1-5 的主路径(前端直连)。仅当 use_llm_proxy=True 时启用。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Mapping

import httpx

from app.config.store import get_api_key
from app.llm.providers import build_request


async def proxy_stream(
    provider: str,
    model: str,
    base_url: str,
    system: str,
    messages: list[Mapping[str, Any]],
    max_tokens: int,
    tools: list[Mapping[str, Any]] | None,
    thinking: bool,
    config_dir: str,
    has_key: bool,
) -> AsyncIterator[bytes]:
    """构造 LLM 请求并转发 SSE 流。

    yield 上游的原始 SSE 字节流(前端 readSSE 直接解析)。
    """
    api_key = get_api_key(provider, config_dir) if has_key else ""
    url, headers, body = build_request(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        tools=tools,
        thinking=thinking,
    )
    # 流式转发(httpx stream)
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        async with client.stream("POST", url, headers=headers, content=body) as resp:
            # 检查上游响应
            if resp.status_code >= 400:
                body_text = await resp.aread()
                yield _error_event(resp.status_code, body_text.decode("utf-8", errors="replace"))
                return
            # 透传 SSE 字节
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk


def _error_event(status: int, message: str) -> bytes:
    """构造一个 SSE error 事件(前端 readSSE 能解析)。"""
    import json

    data = json.dumps({"error": True, "status": status, "message": message}, ensure_ascii=False)
    return f"data: {data}\n\ndata: [DONE]\n\n".encode("utf-8")
