"""路径解析 + 安全校验。

所有从 HTTP 入参来的相对路径都必须过 resolve_*_path,禁止 .. 越界。
"""

from __future__ import annotations

from pathlib import Path

from app.config.schema import AppConfig


class PathTraversalError(Exception):
    """路径越界(../)异常,HTTP 层应返回 403。"""


def _resolve(rel: str, root: str) -> Path:
    """解析相对路径到绝对路径,校验在 root 内。"""
    root_path = Path(root).resolve()
    # 拼接后 resolve,再检查是否在 root 内
    full = (root_path / rel).resolve()
    try:
        full.relative_to(root_path)
    except ValueError as exc:
        raise PathTraversalError(f"path escapes root: {rel}") from exc
    return full


def resolve_content_path(rel: str, cfg: AppConfig) -> Path:
    """解析 content/ 下的相对路径(如 'books/foo/ch01.md')。"""
    return _resolve(rel, cfg.content_dir)


def resolve_pageindex_path(rel: str, cfg: AppConfig) -> Path:
    """解析 pageindex/ 下的相对路径(如 'global-index.json')。"""
    return _resolve(rel, cfg.pageindex_dir)


def resolve_config_path(rel: str, cfg: AppConfig) -> Path:
    """解析 config/ 下的相对路径。"""
    return _resolve(rel, cfg.config_dir)


def content_root(cfg: AppConfig) -> Path:
    return Path(cfg.content_dir).resolve()


def pageindex_root(cfg: AppConfig) -> Path:
    return Path(cfg.pageindex_dir).resolve()
