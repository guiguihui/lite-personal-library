"""LLM provider 映射 + 请求构造。

build_request 严格移植 chat.js buildRequest(L125-187)。
任何改动都要同步 chat.js,否则前后端协议不一致。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

# provider 名称(对齐 app.config.defaults.PROVIDER_NAMES)
PROVIDER_NAMES: tuple[str, ...] = (
    "anthropic",
    "deepseek",
    "openai",
    "siliconflow",
    "openrouter",
    "zhipu",
    "dashscope",
    "ollama",
    "gemini",
    "custom",
)

# provider → 默认 model + base_url(对齐 chat.js L266-292)
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "anthropic": {"model": "claude-sonnet-4-6", "base_url": "https://api.anthropic.com"},
    "deepseek": {"model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com"},
    "openai": {"model": "gpt-4o", "base_url": "https://api.openai.com"},
    "siliconflow": {"model": "deepseek-ai/DeepSeek-V3", "base_url": "https://api.siliconflow.cn"},
    "openrouter": {"model": "anthropic/claude-sonnet-4", "base_url": "https://openrouter.ai/api"},
    "zhipu": {"model": "glm-4", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "dashscope": {"model": "qwen-plus", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "ollama": {"model": "llama3", "base_url": "http://localhost:11434"},
    "gemini": {"model": "gemini-2.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "custom": {"model": "", "base_url": ""},
}


def build_request(
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    system: str,
    messages: list[Mapping[str, Any]],
    max_tokens: int = 4096,
    tools: list[Mapping[str, Any]] | None = None,
    thinking: bool = False,
) -> tuple[str, dict[str, str], str]:
    """构造 LLM 请求,返回 (url, headers, body_json)。

    移植 chat.js buildRequest(L125-187)。
    Anthropic 走 /v1/messages + x-api-key;其余走 /v1/chat/completions + Bearer。
    """
    if provider == "anthropic":
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 4096,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"]["description"],
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ]
            body["tool_choice"] = {"type": "auto"}
        if thinking:
            body["thinking"] = {"type": "enabled", "budget_tokens": min(max_tokens or 4096, 8000)}
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-dangerous-direct-browser-access": "true",
        }
        return f"{base_url}/v1/messages", headers, json.dumps(body, ensure_ascii=False)

    # OpenAI 兼容协议(DeepSeek/OpenAI/SiliconFlow/GLM/DashScope/Gemini/Ollama)
    body = {
        "model": model,
        "max_tokens": max_tokens or 4096,
        "messages": [{"role": "system", "content": system}, *messages],
        "stream": True,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    # DeepSeek 思考模式(对齐 chat.js L179-181)
    if provider == "deepseek":
        body["thinking"] = {"type": "enabled" if thinking else "disabled"}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    return f"{base_url}/v1/chat/completions", headers, json.dumps(body, ensure_ascii=False)
