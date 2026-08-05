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

# 协议后缀(对齐 chat.js buildRequest)
_ANTHROPIC_SUFFIX = "/v1/messages"
_OPENAI_SUFFIX = "/v1/chat/completions"

# 部分 Anthropic 协议代理(如澜智大模型 lanz.hikvision.com)靠 User-Agent 识别
# "Claude 客户端",非 Claude UA 会被 403 "restricted to Claude clients"。
# 对官方 api.anthropic.com 无影响(它不校验 UA),所以默认带上是安全的。
_CLAUDE_UA = "Claude/1.0"


def resolve_protocol(provider: str, protocol: str = "auto") -> str:
    """返回实际使用的协议 "anthropic" | "openai"。

    protocol="auto" 时按 provider 推断(anthropic→anthropic,含 custom 的其余→openai)。
    显式 "anthropic"/"openai" 覆盖推断,用于 custom 端点。
    """
    if protocol == "anthropic":
        return "anthropic"
    if protocol == "openai":
        return "openai"
    return "anthropic" if provider == "anthropic" else "openai"


def resolve_endpoint(base_url: str, protocol: str, path_mode: str = "auto") -> str:
    """返回最终请求 URL。

    - path_mode="full": base_url 是完整路径,直接用(自定义端点场景)
    - path_mode="auto": URL 以已知后缀结尾则直接用,否则按协议补后缀(向后兼容)
    - path_mode="suffix": 强制按协议补后缀
    """
    url = (base_url or "").rstrip("/")
    if path_mode == "full":
        return url
    known = (_ANTHROPIC_SUFFIX, "/messages", _OPENAI_SUFFIX, "/chat/completions")
    if any(url.endswith(s) for s in known):
        return url
    return url + (_ANTHROPIC_SUFFIX if protocol == "anthropic" else _OPENAI_SUFFIX)


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
    protocol: str = "auto",
    path_mode: str = "auto",
    stream: bool = True,
) -> tuple[str, dict[str, str], str]:
    """构造 LLM 请求,返回 (url, headers, body_json)。

    移植 chat.js buildRequest(L125-187)。Anthropic 走 /v1/messages + x-api-key;其余走 /v1/chat/completions + Bearer。
    protocol/path_mode 用于自定义端点(见 resolve_protocol/resolve_endpoint)。
    stream=False 供非流式调用方(如入库翻译)使用;默认 True 保持流式行为。
    """
    actual_proto = resolve_protocol(provider, protocol)
    url = resolve_endpoint(base_url, actual_proto, path_mode)

    if actual_proto == "anthropic":
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 4096,
            "system": system,
            "messages": messages,
            "stream": stream,
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
            "User-Agent": _CLAUDE_UA,
        }
        return url, headers, json.dumps(body, ensure_ascii=False)

    # OpenAI 兼容协议(DeepSeek/OpenAI/SiliconFlow/GLM/DashScope/Gemini/Ollama/custom+openai)
    body = {
        "model": model,
        "max_tokens": max_tokens or 4096,
        "messages": [{"role": "system", "content": system}, *messages],
        "stream": stream,
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
    return url, headers, json.dumps(body, ensure_ascii=False)
