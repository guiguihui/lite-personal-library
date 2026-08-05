"""pageindex/ 文件 IO。

读索引 JSON 供前端 fetch(global-index/node-index/inverted-index/chunks/books/*.json)。
大文件(chunks.json ~26MB)加 gzip 压缩传输(HTTP 层处理)。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config.schema import AppConfig
from app.storage.paths import resolve_pageindex_path


def read_index(rel_path: str, cfg: AppConfig) -> dict | list:
    """读索引 JSON。"""
    path = resolve_pageindex_path(rel_path, cfg)
    if not path.is_file():
        raise FileNotFoundError(rel_path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_index_bytes(rel_path: str, cfg: AppConfig) -> bytes:
    """读索引 JSON 原始字节(供 HTTP 层 gzip 传输,避免二次编码)。"""
    path = resolve_pageindex_path(rel_path, cfg)
    if not path.is_file():
        raise FileNotFoundError(rel_path)
    return path.read_bytes()


def write_index(rel_path: str, data: dict | list, cfg: AppConfig) -> Path:
    """写索引 JSON。"""
    path = resolve_pageindex_path(rel_path, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return path


def list_index_files(cfg: AppConfig) -> list[str]:
    """列 pageindex 下所有文件(相对路径)。"""
    root = Path(cfg.pageindex_dir)
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())


def index_exists(rel_path: str, cfg: AppConfig) -> bool:
    """索引文件是否存在(供前端探测 chunks/inverted 是否就绪)。"""
    try:
        return resolve_pageindex_path(rel_path, cfg).is_file()
    except Exception:
        return False


def remove_doc_from_global_index(slug: str, cfg: AppConfig) -> bool:
    """从 global-index.json 中移除文档条目。返回 True 表示找到并移除。"""
    global_idx = read_index("global-index.json", cfg)
    if not isinstance(global_idx, dict) or "docs" not in global_idx:
        return False
    original_len = len(global_idx["docs"])
    global_idx["docs"] = [d for d in global_idx["docs"] if d.get("id") != slug]
    if len(global_idx["docs"]) == original_len:
        return False
    write_index("global-index.json", global_idx, cfg)
    return True


def remove_doc_from_node_index(slug: str, cfg: AppConfig) -> int:
    """从 node-index.json 中移除所有匹配 doc_id 的节点。返回移除数量。"""
    ni = read_index("node-index.json", cfg)
    if not isinstance(ni, list):
        return 0
    before = len(ni)
    ni = [n for n in ni if (n.get("doc_id") if isinstance(n, dict) else n.get("doc_id")) != slug]
    after = len(ni)
    if after < before:
        write_index("node-index.json", ni, cfg)
    return before - after


def delete_pageindex_file(rel_path: str, cfg: AppConfig) -> bool:
    """删除 pageindex 下的文件(如 books/foo.json)。"""
    path = resolve_pageindex_path(rel_path, cfg)
    if not path.is_file():
        return False
    path.unlink()
    return True


def delete_pageindex_dir(rel_path: str, cfg: AppConfig) -> bool:
    """删除 pageindex 下的目录(含所有文件)。"""
    import shutil

    path = resolve_pageindex_path(rel_path, cfg)
    if not path.is_dir():
        return False
    shutil.rmtree(path)
    return True
