"""POST /api/llm/proxy — LLM 后端代理(可选,解决 CORS)。

前端 BYOK 直连受限时启用(设置 → use_llm_proxy)。
后端用 app.llm.proxy.proxy_stream 转发 SSE 流。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
