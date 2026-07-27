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


def default_providers() -> dict[str, LlmProviderConfig]:
    """构造全部 provider 的默认配置(has_key=False)。"""
    return {
        name: LlmProviderConfig(
            provider=name,
            model=cfg["model"],
            base_url=cfg["base_url"],
            has_key=False,
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
