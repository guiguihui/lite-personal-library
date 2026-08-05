"""GET/PUT /api/settings — BYOK 配置读写。

供前端 chat.js Settings.load()/set() 调用。
响应永远不返回 key 本身,只返回 has_key: bool。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config.schema import LlmConfig, LlmProviderConfig
from app.config.defaults import PROVIDER_DEFAULTS, PROVIDER_NAMES
from app.config.store import (
    get_api_key,
    has_keyring,
    load_app_config,
    load_llm_config,
    save_app_config,
    save_llm_config,
    set_api_key,
)
from app.http.schemas import SettingsResponse, SettingsUpdate, SettingsUpdateResponse

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _to_response(request: Request) -> SettingsResponse:
    cfg = request.app.state.app_config
    llm = load_llm_config(cfg.config_dir)
    providers = {
        name: {
            "model": p.model,
            "base_url": p.base_url,
            "has_key": p.has_key,
            "protocol": p.protocol,
            "path_mode": p.path_mode,
        }
        for name, p in llm.providers.items()
    }
    return SettingsResponse(
        active_provider=llm.active_provider,
        providers=providers,
        remember_key=llm.remember_key,
        use_llm_proxy=cfg.use_llm_proxy,
        has_keyring=has_keyring(),
    )


@router.get("", response_model=SettingsResponse)
async def get_settings(request: Request) -> SettingsResponse:
    """读 BYOK 配置(不返回 key 本身)。"""
    return _to_response(request)


@router.get("/key", response_model=dict)
async def get_api_key_endpoint(provider: str, request: Request) -> dict:
    """读 API key 本身(供前端 BYOK 直连 streamText 用)。

    仅本地 127.0.0.1 可访问(CORS 只允许本地)。
    """
    cfg = request.app.state.app_config
    key = get_api_key(provider, cfg.config_dir)
    return {"api_key": key}


def _mask_key(key: str) -> str:
    """将 key 掩码为"前6位...后4位"格式,短 key(≤10)全掩。"""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "****" + key[-2:] if len(key) >= 4 else "****"
    return key[:6] + "..." + key[-4:]


@router.get("/key/masked", response_model=dict)
async def get_masked_key(provider: str, request: Request) -> dict:
    """读 API key 的掩码版本 + 存储位置(供配置页展示,不暴露完整 key)。

    返回 {masked_key, storage}。
    storage: "plaintext"(llm.yaml 优先) | "keyring"(仅 keyring,无明文时) | "none"

    注意:get_api_key 优先读 llm.yaml _plain_keys,因此 storage="plaintext" 是最常见情况。
    """
    cfg = request.app.state.app_config
    from app.config.store import _HAS_KEYRING as _has_keyring_flag

    # 先检查 _plain_keys(主存储)
    from pathlib import Path
    import yaml
    path = Path(cfg.config_dir) / "llm.yaml"
    in_plain = False
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        in_plain = bool((data.get("_plain_keys") or {}).get(provider))

    key = get_api_key(provider, cfg.config_dir)
    if not key:
        return {"masked_key": "", "storage": "none"}

    if in_plain:
        return {"masked_key": _mask_key(key), "storage": "plaintext"}
    if _has_keyring_flag:
        return {"masked_key": _mask_key(key), "storage": "keyring"}
    return {"masked_key": _mask_key(key), "storage": "plaintext"}


@router.get("/providers", response_model=dict)
async def get_providers() -> dict:
    """返回 provider 列表 + 默认 model/base_url(单一来源)。

    前端 config-providers.js 从此端点读取,消除 chat.js/defaults.py/providers.py
    三处手动同步。后端 defaults.py 是唯一真实来源。
    """
    return {
        "names": list(PROVIDER_NAMES),
        "defaults": PROVIDER_DEFAULTS,
    }


@router.put("", response_model=SettingsUpdateResponse)
async def update_settings(body: SettingsUpdate, request: Request) -> SettingsUpdateResponse:
    """更新 BYOK 配置。

    key="api_key" + provider=X → set_api_key(X, value)
    key="active_provider" → 切换 active
    key="remember_key" → 切换 remember
    key="use_llm_proxy" → 切换代理开关(写 app.yaml)
    key="model"/"base_url" + provider=X → 更新该 provider 的 model/base_url
    """
    cfg = request.app.state.app_config
    llm = load_llm_config(cfg.config_dir)

    key = body.key
    value = body.value
    provider = body.provider

    if key == "api_key":
        if not provider:
            raise HTTPException(status_code=400, detail="provider required for api_key")
        # set_api_key 自己管理 has_key 标记 + 明文/keyring 存储,
        # 不能走末尾 save_llm_config(会丢 _plain_keys)
        set_api_key(provider, str(value) if value else "", cfg.config_dir)
        return SettingsUpdateResponse(ok=True)
    elif key == "active_provider":
        if value not in llm.providers:
            raise HTTPException(status_code=400, detail=f"unknown provider: {value}")
        llm = LlmConfig(
            active_provider=str(value),
            providers=llm.providers,
            remember_key=llm.remember_key,
        )
    elif key == "remember_key":
        llm = LlmConfig(
            active_provider=llm.active_provider,
            providers=llm.providers,
            remember_key=bool(value),
        )
    elif key == "use_llm_proxy":
        # 写 app.yaml
        from dataclasses import replace

        new_cfg = replace(cfg, use_llm_proxy=bool(value))
        save_app_config(new_cfg)
        request.app.state.app_config = new_cfg
        cfg = new_cfg
    elif key in ("model", "base_url", "protocol", "path_mode"):
        if not provider:
            raise HTTPException(status_code=400, detail=f"provider required for {key}")
        old = llm.providers.get(provider)
        if old is None:
            raise HTTPException(status_code=400, detail=f"unknown provider: {provider}")
        # 取值校验
        if key == "protocol" and str(value) not in ("auto", "anthropic", "openai"):
            raise HTTPException(
                status_code=400, detail="protocol must be one of: auto|anthropic|openai"
            )
        if key == "path_mode" and str(value) not in ("auto", "full", "suffix"):
            raise HTTPException(
                status_code=400, detail="path_mode must be one of: auto|full|suffix"
            )
        new_p = LlmProviderConfig(
            provider=old.provider,
            model=str(value) if key == "model" else old.model,
            base_url=str(value) if key == "base_url" else old.base_url,
            has_key=old.has_key,
            protocol=str(value) if key == "protocol" else old.protocol,
            path_mode=str(value) if key == "path_mode" else old.path_mode,
        )
        new_providers = {**llm.providers, provider: new_p}
        llm = LlmConfig(
            active_provider=llm.active_provider,
            providers=new_providers,
            remember_key=llm.remember_key,
        )
    else:
        raise HTTPException(status_code=400, detail=f"unknown setting key: {key}")

    save_llm_config(llm, cfg.config_dir)
    return SettingsUpdateResponse(ok=True)
