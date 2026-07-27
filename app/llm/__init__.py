"""LLM 配置 + 代理模块。

职责:9 provider 映射、配置解析、(可选)后端 SSE 代理。
零耦合:只依赖 config,不依赖 http/index/ingest。

阶段 1:前端直连 LLM(BYOK),后端只提供 /api/settings 读配置。
阶段 6:可选后端代理(解决 Anthropic CORS),用 build_request + proxy_stream。
"""

from __future__ import annotations

from app.llm.config import resolve_active, resolve_for_tier  # noqa: F401
from app.llm.providers import (  # noqa: F401
    PROVIDER_DEFAULTS,
    PROVIDER_NAMES,
    build_request,
)
