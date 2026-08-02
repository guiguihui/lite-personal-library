"""Environment-backed rollout flags for knowledge-link features."""

from __future__ import annotations

import os

DEFAULTS = {
    "knowledge_index_enabled": True,
    "wikilinks_enabled": True,
    "backlinks_enabled": True,
    "link_preview_enabled": True,
    "local_graph_enabled": True,
    "provenance_edges_enabled": False,
}


def feature_flags() -> dict[str, bool]:
    flags = {}
    for name, default in DEFAULTS.items():
        raw = os.getenv(f"LQD_{name.upper()}")
        flags[name] = default if raw is None else raw.strip().casefold() not in {"0", "false", "no", "off"}
    return flags
