"""GET/PUT /api/app/config — 应用级配置读写(路径、端口、PDF 策略)。

供前端 config.js 调用。与 routes_settings.py 的 BYOK LLM 配置分离:
  - /api/settings    → LLM provider/key(走 llm.yaml + keyring)
  - /api/app/config  → 应用路径/端口/策略(走 app.yaml)

路径安全:PUT 时校验新路径不在系统敏感目录(如 C:\\Windows、/etc),
但不做路径遍历校验(用户配置的是绝对路径,非 HTTP 入参相对路径)。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.config.schema import AppConfig
from app.config.store import save_app_config
from app.http.schemas import (
    AppConfigResponse,
    AppConfigUpdate,
    AppConfigUpdateResponse,
)

router = APIRouter(prefix="/api/app", tags=["app-config"])

# 系统敏感目录(禁止作为文档存储位置)— Windows + POSIX
_FORBIDDEN_PATH_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/](Windows|System32|Program Files|Program Files \(x86\))([\\/]|$)", re.IGNORECASE),
    re.compile(r"^/(etc|usr|bin|sbin|boot|proc|sys|dev)(/|$)", re.IGNORECASE),
    re.compile(r"^/(System|Library)(/|$)", re.IGNORECASE),  # macOS
)


def _is_safe_path(p: str) -> bool:
    """校验路径不在系统敏感目录。空路径视为不安全。"""
    if not p or not p.strip():
        return False
    normalized = p.strip().replace("\\", "/")
    for pat in _FORBIDDEN_PATH_PATTERNS:
        if pat.match(normalized) or pat.match(p.strip()):
            return False
    return True


def _to_response(cfg: AppConfig) -> AppConfigResponse:
    return AppConfigResponse(
        content_dir=cfg.content_dir,
        pageindex_dir=cfg.pageindex_dir,
        pdfs_dir=cfg.pdfs_dir,
        pdf_strategy=cfg.pdf_strategy,
        http_host=cfg.http_host,
        http_port=cfg.http_port,
        use_llm_proxy=cfg.use_llm_proxy,
    )


@router.get("/config", response_model=AppConfigResponse)
async def get_app_config(request: Request) -> AppConfigResponse:
    """读应用配置(不返回敏感信息)。"""
    cfg = request.app.state.app_config
    return _to_response(cfg)


@router.put("/config", response_model=AppConfigUpdateResponse)
async def update_app_config(
    body: AppConfigUpdate, request: Request
) -> AppConfigUpdateResponse:
    """更新应用配置(部分更新语义,只更新传入字段)。

    路径字段变更:校验安全性 + 创建新目录。
    http_host/http_port 变更:返回 requires_restart=True(需重启生效)。
    """
    cfg = request.app.state.app_config

    # 收集要更新的字段
    updates: dict[str, object] = {}
    for field_name in (
        "content_dir",
        "pageindex_dir",
        "pdfs_dir",
        "pdf_strategy",
        "http_host",
        "http_port",
        "use_llm_proxy",
    ):
        val = getattr(body, field_name, None)
        if val is not None:
            updates[field_name] = val

    # 路径字段安全校验
    for path_key in ("content_dir", "pageindex_dir", "pdfs_dir"):
        if path_key in updates and not _is_safe_path(str(updates[path_key])):
            raise HTTPException(
                status_code=400,
                detail=f"unsafe path for {path_key}: system directories are forbidden",
            )

    # 检测是否需要重启(host/port 变更)
    requires_restart = (
        "http_host" in updates and updates["http_host"] != cfg.http_host
    ) or ("http_port" in updates and updates["http_port"] != cfg.http_port)

    new_cfg = replace(cfg, **updates)

    # 创建新路径目录(开发模式;打包模式由调用方处理)
    import sys

    if not hasattr(sys, "_MEIPASS"):
        for path_key in ("content_dir", "pageindex_dir", "pdfs_dir"):
            p = getattr(new_cfg, path_key)
            if p:
                try:
                    Path(p).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"cannot create directory {p}: {exc}",
                    ) from exc

    save_app_config(new_cfg)
    request.app.state.app_config = new_cfg
    return AppConfigUpdateResponse(ok=True, requires_restart=requires_restart)
