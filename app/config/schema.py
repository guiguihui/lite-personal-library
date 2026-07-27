"""配置 schema 定义。

不可变 dataclass,符合 coding-style 的 immutability 原则。
所有配置对象都是 frozen dataclass,修改时返回新副本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class AppConfig:
    """应用级配置(路径、端口、PDF 策略)。"""

    content_dir: str
    pageindex_dir: str
    config_dir: str
    pdfs_dir: str
    pdf_strategy: str = "local"  # "local" | "mineru"
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    use_llm_proxy: bool = False  # 前端是否走后端 LLM 代理(解决 CORS)


@dataclass(frozen=True)
class LlmProviderConfig:
    """单个 provider 的配置。api_key 不存这里(走 keyring),只存 has_key 标记。"""

    provider: str
    model: str
    base_url: str
    has_key: bool = False


@dataclass(frozen=True)
class LlmConfig:
    """LLM 配置:active provider + 全部 provider 配置。"""

    active_provider: str
    providers: Mapping[str, LlmProviderConfig] = field(default_factory=dict)
    remember_key: bool = False

    def get_active(self) -> LlmProviderConfig:
        """返回 active provider 的配置。"""
        return self.providers.get(self.active_provider)


@dataclass(frozen=True)
class BuildResult:
    """索引构建结果。"""

    ok: bool
    docs_built: int
    duration_sec: float
    error: str | None = None
    log: tuple[str, ...] = field(default_factory=tuple)
