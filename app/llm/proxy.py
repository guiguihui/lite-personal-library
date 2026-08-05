"""LLM 后端代理(可选,解决 CORS / Anthropic 直连限制)。

阶段 6:前端 BYOK 直连受限时,改走后端代理。
  - 前端 fetch /api/llm/proxy,传 provider/model/base_url/messages/tools/thinking
  - 后端用 app.llm.providers.build_request 构造请求,httpx 转发 SSE 流
  - key 从 app.config.store.get_api_key 读(前端不传 key)

不参与阶段 1-5 的主路径(前端直连)。仅当 use_llm_proxy=True 时启用。

错误处理:
  - 401: 认证失败 → "API Key 无效或已过期,请检查配置"
  - 403: 权限不足 → "无权访问该模型,请检查 API Key 权限"
  - 429: 限流 → 自动重试(最多 3 次,指数退避),超限后提示"模型服务繁忙"
  - 400: 请求格式错误 → 透传上游错误信息
  - 网络超时/连接失败 → "上游服务不可达或超时"
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Mapping

import httpx

from app.config.store import get_api_key
from app.llm.providers import build_request

_log = logging.getLogger(__name__)


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
    protocol: str = "auto",
    path_mode: str = "auto",
    retry_on_429: int = 3,
) -> AsyncIterator[bytes]:
    """构造 LLM 请求并转发 SSE 流。

    yield 上游的原始 SSE 字节流(前端 readSSE 直接解析)。
    protocol/path_mode 透传给 build_request,用于自定义端点。
    retry_on_429: 429 限流时最大重试次数(指数退避:1s/2s/4s)。
    """
    api_key = get_api_key(provider, config_dir) if has_key else ""

    # 快速前置检查
    if has_key and not api_key:
        yield _error_event(401, "认证失败:未找到 API Key,请在配置页填写 Key")
        return
    if not base_url:
        yield _error_event(400, "请求格式错误:Base URL 为空")
        return
    if not model:
        yield _error_event(400, "请求格式错误:Model 为空")
        return

    url, headers, body = build_request(
        provider=provider, model=model, base_url=base_url,
        api_key=api_key, system=system, messages=messages,
        max_tokens=max_tokens, tools=tools, thinking=thinking,
        protocol=protocol, path_mode=path_mode,
    )

    # 带重试的流式转发
    last_error = ""
    for attempt in range(1, retry_on_429 + 1):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=15.0)
            ) as client:
                async with client.stream(
                    "POST", url, headers=headers, content=body
                ) as resp:
                    if resp.status_code < 400:
                        # 成功:透传 SSE 字节
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                yield chunk
                        return

                    # 4xx/5xx:读 body 用于错误分类
                    body_text = await resp.aread()
                    raw_body = body_text.decode("utf-8", errors="replace")

                    if resp.status_code == 429 and attempt < retry_on_429:
                        wait = 2 ** (attempt - 1)  # 1s, 2s, 4s
                        _log.warning(
                            "proxy_stream %s 429 限流,第%d次重试,等待%ds",
                            model, attempt, wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    # 不再重试:构造可读错误
                    yield _build_error_event(resp.status_code, raw_body, model)
                    return

        except httpx.ConnectError as e:
            yield _error_event(503, f"网络错误:无法连接上游服务({url}) — {e}")
            return
        except httpx.TimeoutException:
            yield _error_event(504, f"上游超时:请求 {url} 超时,请稍后重试")
            return
        except httpx.HTTPError as e:
            yield _error_event(502, f"网络错误:{type(e).__name__} — {e}")
            return

    # 所有重试耗尽
    last_error = last_error or "模型服务繁忙,请稍后重试"
    yield _error_event(429, last_error)


def _build_error_event(status: int, raw_body: str, model: str) -> bytes:
    """将上游 HTTP 错误码 + body 解析为可读错误提示。"""
    # 尝试从上游 JSON 响应中提取更具体的错误消息
    inner_msg = raw_body[:500]
    try:
        parsed = json.loads(raw_body)
        # OpenAI 兼容格式:{"error": {"message": "...", "type": "..."}}
        if isinstance(parsed, dict):
            err_obj = parsed.get("error")
            if isinstance(err_obj, dict):
                inner_msg = err_obj.get("message", raw_body[:300])
            elif isinstance(err_obj, str):
                inner_msg = err_obj
            elif parsed.get("message"):
                inner_msg = str(parsed["message"])
    except (json.JSONDecodeError, TypeError):
        pass

    # 按 HTTP 状态码生成友好提示
    hints = {
        400: f"请求格式错误({model}):{inner_msg}",
        401: "API Key 无效或已过期,请检查配置中的 Key 是否正确",
        403: "无权访问该模型,请确认 API Key 具备访问权限",
        404: f"模型端点不存在({model}):请检查 Base URL 和模型名称",
        429: f"模型服务繁忙({model}),请稍后重试或切换到其他可用模型",
        500: "上游服务器内部错误,请稍后重试",
        502: "上游网关错误,请稍后重试",
        503: "上游服务暂时不可用,请稍后重试",
    }
    message = hints.get(status, f"上游返回 {status}:{inner_msg}")
    return _error_event(status, message)


def _error_event(status: int, message: str) -> bytes:
    """构造一个 SSE error 事件(前端 readSSE 能解析)。"""
    data = json.dumps(
        {"error": True, "status": status, "message": message},
        ensure_ascii=False,
    )
    return f"data: {data}\n\ndata: [DONE]\n\n".encode("utf-8")
