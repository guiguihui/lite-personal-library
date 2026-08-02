"""Validated reads for the derived knowledge-link index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config.schema import AppConfig

_cache: dict[str, tuple[int, dict[str, Any]]] = {}


def read_link_index(cfg: AppConfig) -> dict[str, Any]:
    path = (Path(cfg.pageindex_dir) / "link-index.json").resolve()
    stamp = path.stat().st_mtime_ns
    key = str(path)
    cached = _cache.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("unsupported link index schema")
    for required in ("documents", "edges", "outgoing", "incoming", "diagnostics"):
        if required not in data:
            raise ValueError(f"invalid link index: missing {required}")
    _cache[key] = (stamp, data)
    return data


def clear_link_index_cache() -> None:
    _cache.clear()
