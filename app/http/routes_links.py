"""Knowledge-link resolve, backlink, preview, graph, and diagnostics APIs."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app.knowledge.catalog import build_catalog
from app.knowledge.features import feature_flags
from app.knowledge.models import ParsedLink
from app.knowledge.queries import get_backlinks, get_neighborhood, get_preview
from app.knowledge.resolver import resolve_link
from app.storage.link_index_io import read_link_index

router = APIRouter(prefix="/api/links", tags=["links"])


@router.get('/features')
async def features() -> dict:
    return feature_flags()


def _index(request: Request) -> dict:
    try:
        return read_link_index(request.app.state.app_config)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="link index has not been built") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail=f"link index unavailable: {exc}") from exc


@router.post("/resolve")
async def resolve_targets(payload: dict, request: Request) -> dict:
    current_id = str(payload.get("current_id", ""))
    cfg = request.app.state.app_config
    catalog = build_catalog(Path(cfg.content_dir), Path(cfg.pageindex_dir))
    source = catalog.get(current_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"unknown document: {current_id}")
    results = []
    for item in payload.get("targets", []):
        link = ParsedLink(str(item.get("target", "")), str(item.get("target", "")), None, item.get("anchor"), 0, 0, "wikilink")
        edge = resolve_link(source, link, catalog)
        target = catalog.get(edge.target_id or "")
        results.append({
            "status": edge.status, "id": edge.target_id, "type": target.doc_type if target else None,
            "slug": target.slug if target else None, "title": target.title if target else None,
            "node_id": edge.target_node_id, "candidates": list(edge.candidates),
        })
    return {"results": results}


@router.get("/backlinks")
async def backlinks(id: str, request: Request) -> dict:
    try:
        return get_backlinks(_index(request), id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown document: {id}") from exc


@router.get("/neighborhood")
async def neighborhood(id: str, request: Request, limit: int = Query(40, ge=1, le=200), include_provenance: bool = False) -> dict:
    try:
        return get_neighborhood(_index(request), id, limit, include_provenance)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown document: {id}") from exc


@router.get("/preview")
async def preview(id: str, request: Request, node_id: str | None = None) -> dict:
    try:
        return get_preview(_index(request), id, node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown document: {id}") from exc


@router.get("/diagnostics")
async def diagnostics(request: Request) -> dict:
    index = _index(request)
    diagnostics = index["diagnostics"]
    return {
        "summary": {key: len(value) for key, value in diagnostics.items()},
        **diagnostics,
        "index_health": {"schema_version": index["schema_version"], "content_fingerprint": index.get("content_fingerprint")},
    }
