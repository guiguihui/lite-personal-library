"""LLM 模型列表拉取。

供前端 /api/llm/models 调用,返回上游 provider 暴露的模型列表。
对齐 CC-switch「获取模型列表」行为:从当前 Base URL 推导 models 端点,
GET 拉上游,解析 data 数组为统一 {id, owned_by, created} 结构返回。

URL 推导规则(优先按已知后缀剥离,再按协议补后缀,与 providers.resolve_endpoint
保持一致风格):
  1. 已知 chat 后缀(/v1/chat/completions、/v1/messages、/messages、/chat/completions)
     → 去掉该后缀,再补 /v1/models(OpenAI 协议)或 /v1/models(Anthropic 协议尽量也试)
  2. URL 末尾是 /model(单数,如 lanz.hikvision.com/v3/openai/model)
     → 保留 /model,直接 append /v1/models(lanz 的实际端点就是 /v3/openai/model/v1/models)
  3. URL 末尾是 /models → 直接用
  4. 都没有 → 按协议补 /v1/models

Anthropic 协议 /v1/models 官方未定义,但澜智大模型 / OpenRouter 等代理通常
实现了 OpenAI 兼容的 /v1/models,优先按 OpenAI 协议补 /v1/models。

兜底:推导出的第一个 URL 若 404/无数据,自动尝试备选 URL 列表(共 N 个候选),
任一返回 200 + JSON + 有模型数据即采纳。这样能覆盖非标准平台如
lanz.hikvision.com(实际端点 /v3/openai/model/v1/models)和那些不带 v1 的旧服务。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from app.llm.providers import resolve_protocol


# 已知 chat/消息 后缀,命中后剥掉再补 /v1/models
_CHAT_SUFFIXES = (
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/messages",
    "/messages",
)
# 已知 models 后缀,命中后直接用
_MODELS_SUFFIXES = (
    "/v1/models",
    "/models",
)


def resolve_models_url(base_url: str, protocol: str = "openai") -> str:
    """从 Base URL 推导 models 端点 URL(返回第一候选,供简单场景)。

    见模块顶部规则。protocol 仅用于补后缀时选 /v1/models(目前两协议都用同一后缀,
    留参以兼容未来差异)。
    """
    candidates = generate_models_url_candidates(base_url, protocol)
    return candidates[0] if candidates else ""


def generate_models_url_candidates(base_url: str, protocol: str = "openai") -> list[str]:
    """从 Base URL 推导 models 端点候选 URL 列表(按优先级排序)。

    用于 fetch_models 兜底探测:第一候选 404 或无数据时,自动尝试下一个。
    候选生成规则见模块顶部。
    """
    if not base_url:
        return []
    url = base_url.rstrip("/")
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return []

    # 规则 3:已经以 models 后缀结尾,直接用
    for s in _MODELS_SUFFIXES:
        if url.endswith(s):
            return [url]

    # 规则 1:chat/消息 后缀 → 剥掉再补 /v1/models
    for s in _CHAT_SUFFIXES:
        if url.endswith(s):
            return [url[: -len(s)] + "/v1/models"]

    # 规则 2/4:末尾是 /model(单数)→ 优先保留并 append /v1/models(lanz 风格)
    # 再退回 /model → /models(单变复,老服务风格)
    if url.endswith("/model"):
        return [
            url + "/v1/models",                # lanz.hikvision.com: /v3/openai/model/v1/models
            url[: -len("/model")] + "/models", # 通用: /v3/openai/models
        ]
    # 默认:补 /v1/models
    return [url + "/v1/models"]


def _build_headers(provider: str, api_key: str, protocol: str) -> dict[str, str]:
    """按协议构造 GET /models 请求头。

    - OpenAI 协议:Authorization: Bearer <key>
    - Anthropic 协议:x-api-key + anthropic-version + User-Agent
      (lanz.hikvision.com 需 Claude UA 才能识别,见 providers.py 注释)
    """
    if protocol == "anthropic":
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": "Claude/1.0",
        }
    return {"Authorization": f"Bearer {api_key}"}


def _parse_models_response(payload: Any) -> list[dict[str, Any]]:
    """统一上游响应为 [{id, owned_by, created}] 列表。

    兼容三种上游返回:
      1. 标准 OpenAI 风格:{data: [{id, owned_by, created, ...}, ...]}
      2. 简单数组:["model-a", "model-b", ...]
      3. 嵌套 model 字段:无(目前未见,留扩展)
    """
    if isinstance(payload, list):
        # 简单数组
        return [
            {"id": str(x), "owned_by": "", "created": 0}
            for x in payload
            if x is not None and str(x).strip()
        ]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        # 退化:把整个 payload 的 id 字段当作单条
        mid = payload.get("id") or payload.get("model")
        if mid:
            return [{"id": str(mid), "owned_by": "", "created": 0}]
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            # 字符串条目
            if item is not None and str(item).strip():
                out.append({"id": str(item), "owned_by": "", "created": 0})
            continue
        mid = item.get("id") or item.get("model") or item.get("name")
        if not mid:
            continue
        out.append(
            {
                "id": str(mid),
                "owned_by": str(item.get("owned_by") or item.get("owner") or ""),
                "created": int(item.get("created") or 0),
            }
        )
    return out


async def fetch_models(
    provider: str,
    base_url: str,
    api_key: str,
    protocol: str = "auto",
    path_mode: str = "auto",
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    """从上游拉取模型列表。

    返回:
      {
        "ok": bool,
        "url": str,                # 实际请求的 URL(候选中第一个成功的)
        "protocol": "openai" | "anthropic",
        "models": [{"id", "owned_by", "created"}, ...],
        "count": int,
        "error": Optional[str],     # 失败时
        "status": Optional[int],    # 失败时上游 HTTP 状态码
        "elapsed_ms": int,
      }

    兜底策略:推导的第一候选 URL 失败时,自动尝试备选 URL,直到命中 200 + JSON + 有数据。
    """
    if not base_url:
        return {"ok": False, "url": "", "protocol": "", "models": [], "count": 0,
                "error": "Base URL 为空", "status": None, "elapsed_ms": 0}

    proto = resolve_protocol(provider, protocol)
    candidates = generate_models_url_candidates(base_url, proto)
    if not candidates:
        return {"ok": False, "url": "", "protocol": proto, "models": [], "count": 0,
                "error": f"无法从 Base URL 推导 models 端点: {base_url}",
                "status": None, "elapsed_ms": 0}

    headers = _build_headers(provider, api_key, proto)
    import time

    started = time.monotonic()
    last_error = "未知错误"
    last_status = None
    tried_urls: list[str] = []

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec, connect=10.0)
        ) as client:
            for url in candidates:
                tried_urls.append(url)
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.TimeoutException as e:
                    last_error = f"上游超时({timeout_sec}s) @ {url}: {e}"
                    last_status = None
                    continue
                except httpx.HTTPError as e:
                    last_error = f"网络错误 @ {url}: {type(e).__name__}: {e}"
                    last_status = None
                    continue

                if resp.status_code >= 400:
                    snippet = (resp.text or "")[:200]
                    last_error = f"上游返回 {resp.status_code} @ {url}: {snippet}"
                    last_status = resp.status_code
                    continue

                # 200 OK — 解析 JSON
                try:
                    payload = resp.json()
                except Exception as e:
                    last_error = f"{url} 返回 200 但非 JSON: {e}"
                    last_status = resp.status_code
                    continue

                models = _parse_models_response(payload)
                if not models:
                    last_error = f"{url} 返回 200 但无模型数据(响应格式未识别)"
                    last_status = resp.status_code
                    continue

                # 命中!
                elapsed = int((time.monotonic() - started) * 1000)
                return {
                    "ok": True,
                    "url": url,
                    "protocol": proto,
                    "models": models,
                    "count": len(models),
                    "error": None,
                    "status": resp.status_code,
                    "elapsed_ms": elapsed,
                }

            # 所有候选都失败
            elapsed = int((time.monotonic() - started) * 1000)
            return {
                "ok": False,
                "url": tried_urls[-1] if tried_urls else "",
                "protocol": proto,
                "models": [],
                "count": 0,
                "error": f"所有候选都失败(共 {len(candidates)} 个):{last_error}",
                "status": last_status,
                "elapsed_ms": elapsed,
            }
    except Exception as e:
        return {"ok": False, "url": tried_urls[-1] if tried_urls else "", "protocol": proto,
                "models": [], "count": 0,
                "error": f"拉取异常: {type(e).__name__}: {e}", "status": None,
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
