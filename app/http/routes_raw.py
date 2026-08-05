"""GET /raw/content/<path> — 替代 GitHub raw fetch。

供 chat.js fetchMdLines(L394)读 md 原文。
路径安全:过 resolve_content_path 校验 .. 越界。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.storage.paths import PathTraversalError
from app.storage.content_io import read_markdown

router = APIRouter(prefix="/raw", tags=["raw"])


@router.get("/content/{path:path}", response_class=PlainTextResponse)
async def get_raw_content(path: str, request: Request) -> str:
    """读 content/<path> 的 md 原文(含 front matter,前端自己剥)。"""
    cfg = request.app.state.app_config
    try:
        return read_markdown(path, cfg)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"not found: {path}")
    except PathTraversalError:
        raise HTTPException(status_code=403, detail="path escapes root")
