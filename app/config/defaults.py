"""默认配置值。

provider 默认 model + base_url 严格对齐 chat.js Settings.resolve()(L262-292)。
任何改动都要同步 chat.js,否则前端后端默认值不一致。
"""

from __future__ import annotations

from app.config.schema import AppConfig, LlmProviderConfig

# 9 provider 名称(顺序对齐 chat.js,custom 在最后)
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
# protocol/path_mode 全默认 "auto"(向后兼容;custom 端点由用户显式覆盖)
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "anthropic": {"model": "claude-sonnet-4-6", "base_url": "https://api.anthropic.com", "protocol": "auto", "path_mode": "auto"},
    "deepseek": {"model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com", "protocol": "auto", "path_mode": "auto"},
    "openai": {"model": "gpt-4o", "base_url": "https://api.openai.com", "protocol": "auto", "path_mode": "auto"},
    "siliconflow": {"model": "deepseek-ai/DeepSeek-V3", "base_url": "https://api.siliconflow.cn", "protocol": "auto", "path_mode": "auto"},
    "openrouter": {"model": "anthropic/claude-sonnet-4", "base_url": "https://openrouter.ai/api", "protocol": "auto", "path_mode": "auto"},
    "zhipu": {"model": "glm-4", "base_url": "https://open.bigmodel.cn/api/paas/v4", "protocol": "auto", "path_mode": "auto"},
    "dashscope": {"model": "qwen-plus", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "protocol": "auto", "path_mode": "auto"},
    "ollama": {"model": "llama3", "base_url": "http://localhost:11434", "protocol": "auto", "path_mode": "auto"},
    "gemini": {"model": "gemini-2.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "protocol": "auto", "path_mode": "auto"},
    "custom": {"model": "", "base_url": "", "protocol": "auto", "path_mode": "auto"},
}


def default_providers() -> dict[str, LlmProviderConfig]:
    """构造全部 provider 的默认配置(has_key=False)。"""
    return {
        name: LlmProviderConfig(
            provider=name,
            model=cfg["model"],
            base_url=cfg["base_url"],
            has_key=False,
            protocol=cfg.get("protocol", "auto"),
            path_mode=cfg.get("path_mode", "auto"),
        )
        for name, cfg in PROVIDER_DEFAULTS.items()
    }


def default_app_config(content_dir: str, pageindex_dir: str, config_dir: str, pdfs_dir: str) -> AppConfig:
    """构造默认应用配置。"""
    return AppConfig(
        content_dir=content_dir,
        pageindex_dir=pageindex_dir,
        config_dir=config_dir,
        pdfs_dir=pdfs_dir,
    )
