"""LLM 后端路由:流式代理 + 模型列表拉取 + 连通性检测。

POST /api/llm/proxy — LLM 流式转发(可选,解决 CORS)
POST /api/llm/models — 拉取上游 provider 暴露的模型列表(对齐 CC-switch「获取模型列表」)
POST /api/llm/check — 批量检测所有已配置 provider 的连通性
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config.store import get_api_key, load_llm_config
from app.llm.models import fetch_models
from app.llm.providers import PROVIDER_DEFAULTS
from app.llm.proxy import proxy_stream

router = APIRouter(prefix="/api/llm", tags=["llm-proxy"])


class LlmProxyRequest(BaseModel):
    provider: str
    model: str
    base_url: str
    system: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    max_tokens: int = 4096
    tools: list[dict[str, Any]] | None = None
    thinking: bool = False
    has_key: bool = False  # 前端从 /api/settings 读的 has_key 标记
    protocol: str = "auto"  # 自定义端点协议:auto|anthropic|openai
    path_mode: str = "auto"  # 自定义端点路径模式:auto|full|suffix


class LlmModelsRequest(BaseModel):
    provider: str
    base_url: str
    has_key: bool = False
    protocol: str = "auto"
    path_mode: str = "auto"
    timeout_sec: float = 30.0


class LlmModelsItem(BaseModel):
    id: str
    owned_by: str = ""
    created: int = 0


class LlmModelsResponse(BaseModel):
    ok: bool
    url: str = ""
    protocol: str = ""
    models: list[LlmModelsItem] = Field(default_factory=list)
    count: int = 0
    error: str | None = None
    status: int | None = None
    elapsed_ms: int = 0


@router.post("/proxy")
async def llm_proxy(body: LlmProxyRequest, request: Request):
    """转发 LLM 请求,返回 SSE 流。"""
    cfg = request.app.state.app_config

    async def stream():
        async for chunk in proxy_stream(
            provider=body.provider,
            model=body.model,
            base_url=body.base_url,
            system=body.system,
            messages=body.messages,
            max_tokens=body.max_tokens,
            tools=body.tools,
            thinking=body.thinking,
            config_dir=cfg.config_dir,
            has_key=body.has_key,
            protocol=body.protocol,
            path_mode=body.path_mode,
        ):
            yield chunk

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/models", response_model=LlmModelsResponse)
async def llm_models(body: LlmModelsRequest, request: Request) -> JSONResponse:
    """从上游拉取模型列表(对齐 CC-switch「获取模型列表」)。

    不返回 key 本身(只读 keyring/_plain_keys 用来构造 Authorization)。
    失败时 status 字段透传上游 HTTP 码,error 字段为可读描述。
    """
    cfg = request.app.state.app_config
    api_key = get_api_key(body.provider, cfg.config_dir) if body.has_key else ""
    result = await fetch_models(
        provider=body.provider,
        base_url=body.base_url,
        api_key=api_key,
        protocol=body.protocol,
        path_mode=body.path_mode,
        timeout_sec=body.timeout_sec,
    )
    # ok=false 时也返回 200(语义错误而非 HTTP 错误),前端可直接展示 error
    return JSONResponse(
        status_code=200,
        content=LlmModelsResponse(**result).model_dump(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# 连通性检测
# ═══════════════════════════════════════════════════════════════════════════

from app.llm.providers import resolve_protocol, resolve_endpoint


async def _check_one_provider(
    name: str,
    model: str,
    base_url: str,
    api_key: str,
    protocol: str,
    path_mode: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """检测单个 provider 的连通性。

    发送一个最小 tokens 的空消息请求,根据响应判断状态:
      - 2xx → "available"
      - 401/403 → "auth_error"
      - 429 → "rate_limited"
      - 4xx/5xx → "unavailable"
      - 超时/连接失败 → "unreachable"
    """
    if not api_key or not base_url:
        return {
            "provider": name,
            "model": model,
            "status": "no_key",
            "error": "未配置 API Key" if not api_key else "未配置 Base URL",
            "latency_ms": 0,
        }

    proto = resolve_protocol(name, protocol)
    url = resolve_endpoint(base_url, proto, path_mode)

    # 构造最简探测请求
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if proto == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        body = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    else:
        headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=8.0)) as client:
            resp = await client.post(url, headers=headers, json=body)
            elapsed = int((time.monotonic() - t0) * 1000)
            if resp.status_code < 300:
                return {
                    "provider": name,
                    "model": model,
                    "status": "available",
                    "error": "",
                    "latency_ms": elapsed,
                }
            if resp.status_code in (401, 403):
                return {
                    "provider": name,
                    "model": model,
                    "status": "auth_error",
                    "error": "API Key 无效或权限不足",
                    "latency_ms": elapsed,
                }
            if resp.status_code == 429:
                return {
                    "provider": name,
                    "model": model,
                    "status": "rate_limited",
                    "error": "请求频率过高，请稍后重试",
                    "latency_ms": elapsed,
                }
            body_text = (await resp.aread()).decode("utf-8", errors="replace")[:200]
            return {
                "provider": name,
                "model": model,
                "status": "unavailable",
                "error": f"HTTP {resp.status_code}: {body_text}",
                "latency_ms": elapsed,
            }
    except httpx.ConnectError:
        return {
            "provider": name,
            "model": model,
            "status": "unreachable",
            "error": "无法连接到服务器，请检查 Base URL 和网络",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }
    except httpx.TimeoutException:
        return {
            "provider": name,
            "model": model,
            "status": "unreachable",
            "error": "连接超时，服务器无响应",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as e:
        return {
            "provider": name,
            "model": model,
            "status": "unreachable",
            "error": f"检测失败: {e}",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }


class CheckResult(BaseModel):
    provider: str
    model: str
    status: str  # available | auth_error | rate_limited | unavailable | unreachable | no_key
    error: str = ""
    latency_ms: int = 0


class CheckResponse(BaseModel):
    ok: bool = True
    active_provider: str = ""
    results: list[CheckResult] = Field(default_factory=list)
    checked_at: float = 0.0


@router.post("/check", response_model=CheckResponse)
async def check_providers(request: Request) -> CheckResponse:
    """批量检测所有已配置 provider 的连通性。

    并行检测所有 has_key=True 的 provider,返回各自的状态。
    """
    cfg = request.app.state.app_config
    llm = load_llm_config(cfg.config_dir)
    providers = llm.providers

    tasks = []
    provider_names = []
    for name, p in providers.items():
        if not p.has_key and name != llm.active_provider:
            continue
        api_key = get_api_key(name, cfg.config_dir) if p.has_key else ""
        tasks.append(
            _check_one_provider(
                name=name,
                model=p.model,
                base_url=p.base_url,
                api_key=api_key,
                protocol=p.protocol,
                path_mode=p.path_mode,
                timeout=10.0,
            )
        )
        provider_names.append(name)

    results_raw = await asyncio.gather(*tasks, return_exceptions=True)
    results: list[CheckResult] = []
    for i, r in enumerate(results_raw):
        if isinstance(r, Exception):
            results.append(CheckResult(
                provider=provider_names[i] if i < len(provider_names) else "unknown",
                model="",
                status="unreachable",
                error=f"检测异常: {r}",
            ))
        else:
            results.append(CheckResult(**r))

    return CheckResponse(
        ok=True,
        active_provider=llm.active_provider,
        results=results,
        checked_at=time.time(),
    )
