"""Bridge PageIndex results to the knowledge-link index builder."""

from __future__ import annotations

from app.config.schema import BuildResult

from .indexer import build_link_index
from .features import feature_flags
from .features import feature_flags


def finish_with_links(raw: dict, content_dir: str, pageindex_dir: str) -> BuildResult:
    result = BuildResult(
        ok=bool(raw.get("ok", False)),
        docs_built=int(raw.get("docs_built", 0)),
        duration_sec=float(raw.get("duration_sec", 0.0)),
        error=raw.get("error"),
        log=tuple(raw.get("log", [])),
    )
    if not result.ok:
        return result
    if not feature_flags()['knowledge_index_enabled']:
        return result
    if not feature_flags()['knowledge_index_enabled']:
        return result
    try:
        index = build_link_index(content_dir, pageindex_dir)
    except Exception as exc:  # noqa: BLE001 - surfaced through build status
        return BuildResult(
            ok=False,
            docs_built=result.docs_built,
            duration_sec=result.duration_sec,
            error=f"knowledge link index failed: {exc}",
            log=result.log,
        )
    return BuildResult(
        ok=True,
        docs_built=result.docs_built,
        duration_sec=result.duration_sec,
        error=None,
        log=(*result.log, f"Knowledge links: {len(index['edges'])} edges"),
    )
