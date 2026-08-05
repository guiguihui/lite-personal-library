"""LLM 配置解析。

resolve_active() — 给 /api/settings 用,返回 active provider 的完整配置(含 key)。
resolve_for_tier(tier) — 给入库流水线用(translate/validate 需要 LLM)。

tier 映射策略(MVP):
  strong/cheap/fast 全部映射到 active_provider。
  未来可在设置里加 tier→provider 映射。
"""

from __future__ import annotations

from app.config.schema import LlmConfig
from app.config.store import get_api_key


def resolve_active(cfg: LlmConfig, config_dir: str) -> tuple[str, str, str, str]:
    """返回 (provider, model, base_url, api_key)。"""
    p = cfg.get_active()
    if p is None:
        return ("", "", "", "")
    key = get_api_key(p.provider, config_dir) if p.has_key else ""
    return (p.provider, p.model, p.base_url, key)


def resolve_active_full(cfg: LlmConfig, config_dir: str) -> tuple[str, str, str, str, str, str]:
    """返回 (provider, model, base_url, api_key, protocol, path_mode)。

    给入库翻译流水线用:translate_chapters 需要知道协议 + 路径模式,
    以决定走 OpenAI SDK 还是 httpx 直发 Anthropic 协议请求。
    """
    p = cfg.get_active()
    if p is None:
        return ("", "", "", "", "auto", "auto")
    key = get_api_key(p.provider, config_dir) if p.has_key else ""
    return (p.provider, p.model, p.base_url, key, p.protocol, p.path_mode)


def resolve_for_tier(tier: str, cfg: LlmConfig, config_dir: str) -> tuple[str, str, str, str]:
    """入库流水线用:tier → (provider, model, base_url, api_key)。

    MVP 全部 tier 映射到 active_provider。
    """
    # tier 校验(容忍未知 tier,降级到 active)
    _ = tier  # MVP 不区分,统一用 active
    return resolve_active(cfg, config_dir)


def resolve_for_tier_full(
    tier: str, cfg: LlmConfig, config_dir: str
) -> tuple[str, str, str, str, str, str]:
    """入库流水线用:tier → (provider, model, base_url, api_key, protocol, path_mode)。

    translate_chapters 用此函数拿完整配置,据 protocol 决定请求路径。
    """
    _ = tier  # MVP 不区分 tier,统一用 active
    return resolve_active_full(cfg, config_dir)
