"""配置读写模块。

职责:应用配置 + LLM 配置持久化(JSON 文件),BYOK key 走 keyring(可选)。
零耦合:只依赖 schema + defaults,不依赖 http/index/ingest。

key 存储策略:
  - keyring 可用 → 存系统凭证管理器(Win Credential Manager / macOS Keychain)
  - keyring 不可用 → 降级到 llm.yaml 明文(本地桌面应用可接受)
  - /api/settings 响应永远不返回 key 本身,只返回 has_key: bool
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from app.config.defaults import default_app_config, default_providers
from app.config.schema import AppConfig, LlmConfig, LlmProviderConfig

# keyring 可选(无则降级明文)
try:
    import keyring  # type: ignore

    _HAS_KEYRING = True
    _KEYRING_SERVICE = "yuulibrary-desktop"
except ImportError:  # pragma: no cover
    _HAS_KEYRING = False
    _KEYRING_SERVICE = ""

_APP_CONFIG_FILE = "app.yaml"
_LLM_CONFIG_FILE = "llm.yaml"


# ── AppConfig ──────────────────────────────────────────────────────────────


def load_app_config(config_dir: str) -> AppConfig:
    """读应用配置;不存在则用默认值并写盘。"""
    path = Path(config_dir) / _APP_CONFIG_FILE
    if not path.exists():
        cfg = default_app_config(
            content_dir=str(Path(config_dir).parent / "content"),
            pageindex_dir=str(Path(config_dir).parent / "pageindex"),
            config_dir=config_dir,
            pdfs_dir=str(Path(config_dir).parent / "pdfs"),
        )
        save_app_config(cfg)
        return cfg
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig(
        content_dir=data.get("content_dir", str(Path(config_dir).parent / "content")),
        pageindex_dir=data.get("pageindex_dir", str(Path(config_dir).parent / "pageindex")),
        config_dir=config_dir,
        pdfs_dir=data.get("pdfs_dir", str(Path(config_dir).parent / "pdfs")),
        pdf_strategy=data.get("pdf_strategy", "local"),
        http_host=data.get("http_host", "127.0.0.1"),
        http_port=int(data.get("http_port", 8765)),
        use_llm_proxy=bool(data.get("use_llm_proxy", False)),
    )


def save_app_config(cfg: AppConfig) -> None:
    """写应用配置。"""
    path = Path(cfg.config_dir) / _APP_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "content_dir": cfg.content_dir,
        "pageindex_dir": cfg.pageindex_dir,
        "pdfs_dir": cfg.pdfs_dir,
        "pdf_strategy": cfg.pdf_strategy,
        "http_host": cfg.http_host,
        "http_port": cfg.http_port,
        "use_llm_proxy": cfg.use_llm_proxy,
    }
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


# ── LlmConfig ──────────────────────────────────────────────────────────────


def load_llm_config(config_dir: str) -> LlmConfig:
    """读 LLM 配置;不存在则用默认值并写盘。"""
    path = Path(config_dir) / _LLM_CONFIG_FILE
    if not path.exists():
        cfg = LlmConfig(active_provider="anthropic", providers=default_providers(), remember_key=False)
        save_llm_config(cfg, config_dir)
        return cfg
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    active = data.get("active_provider", "anthropic")
    remember = bool(data.get("remember_key", False))
    providers: dict[str, LlmProviderConfig] = {}
    for name, p in (data.get("providers") or {}).items():
        providers[name] = LlmProviderConfig(
            provider=name,
            model=p.get("model", ""),
            base_url=p.get("base_url", ""),
            has_key=bool(p.get("has_key", False)),
            protocol=p.get("protocol", "auto"),
            path_mode=p.get("path_mode", "auto"),
        )
    # 补齐缺失的 provider(默认值)
    for name, default in default_providers().items():
        if name not in providers:
            providers[name] = default
    return LlmConfig(active_provider=active, providers=providers, remember_key=remember)


def save_llm_config(cfg: LlmConfig, config_dir: str) -> None:
    """写 LLM 配置(has_key 标记,key 本身走 keyring 或 _plain_keys 明文降级)。

    关键:保留磁盘上已有的 _plain_keys 明文 key 区。keyring 不可用时,key
    存在 _plain_keys 里(见 set_api_key 降级路径)。若此处从零重写 yaml,
    会丢掉 _plain_keys → 已存的 key 被冲掉 → get_api_key 返回空 → 401。
    前端 saveLLM 顺序发多个 PUT(api_key 之后还有 remember_key 等),
    remember_key 走本函数重写,会把刚写的 _plain_keys 冲掉。读-合并-写规避。
    """
    path = Path(config_dir) / _LLM_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # 读已有数据,保留 _plain_keys(若存在)
    existing: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    data: dict[str, Any] = {
        "active_provider": cfg.active_provider,
        "remember_key": cfg.remember_key,
        "providers": {
            name: {
                "model": p.model,
                "base_url": p.base_url,
                "has_key": p.has_key,
                "protocol": p.protocol,
                "path_mode": p.path_mode,
            }
            for name, p in cfg.providers.items()
        },
    }
    # 保留明文 key 降级区(keyring 不可用时 key 在这里)
    if existing.get("_plain_keys"):
        data["_plain_keys"] = existing["_plain_keys"]
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


# ── API key 存取(供 routes_settings 调用)────────────────────────────────


def get_api_key(provider: str, config_dir: str) -> str:
    """读 API key:_plain_keys 优先,keyring 作为备选。

    _plain_keys(llm.yaml)是用户通过配置页保存的明确值,可信度最高。
    keyring(Windows 凭证管理器)可能残留开发期测试值,作为降级备用。
    两者都有时,_plain_keys 胜出。
    """
    # 优先:llm.yaml 的 _plain_keys(用户明确写入的)
    path = Path(config_dir) / _LLM_CONFIG_FILE
    plain_key = ""
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        plain = data.get("_plain_keys") or {}
        plain_key = str(plain.get(provider, ""))
    if plain_key:
        return plain_key
    # 降级:keyring(可能残留旧值)
    if _HAS_KEYRING:
        try:
            k = keyring.get_password(_KEYRING_SERVICE, provider)
            if k:
                return k
        except Exception:  # pragma: no cover — keyring 后端故障降级
            pass
    return ""


def set_api_key(provider: str, key: str, config_dir: str) -> None:
    """写 API key:同时写 keyring + llm.yaml _plain_keys(双写,避免优先级反转)。"""
    # 始终写 _plain_keys(get_api_key 优先读这里)
    _write_plain_key(provider, key, config_dir)
    # 同时尝试写 keyring(增强安全,但仅作备选)
    if _HAS_KEYRING:
        try:
            if key:
                keyring.set_password(_KEYRING_SERVICE, provider, key)
            else:
                try:
                    keyring.delete_password(_KEYRING_SERVICE, provider)
                except keyring.PasswordDeleteError:  # type: ignore
                    pass
        except Exception:  # pragma: no cover — keyring 后端故障降级
            pass
    _update_has_key(provider, bool(key), config_dir)


def _write_plain_key(provider: str, key: str, config_dir: str) -> None:
    """写/删 llm.yaml 的 _plain_keys 条目(原子读-改-写)。"""
    path = Path(config_dir) / _LLM_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    plain = data.setdefault("_plain_keys", {})
    if key:
        plain[provider] = key
    else:
        plain.pop(provider, None)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _update_has_key(provider: str, has_key: bool, config_dir: str) -> None:
    """更新 llm.yaml 里 provider 的 has_key 标记。"""
    path = Path(config_dir) / _LLM_CONFIG_FILE
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    providers = data.setdefault("providers", {})
    if provider not in providers:
        providers[provider] = {}
    providers[provider]["has_key"] = has_key
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def has_keyring() -> bool:
    """暴露 keyring 可用性(供前端展示存储方式)。"""
    return _HAS_KEYRING
