"""GET /pageindex/<path> — 读索引 JSON。

供 chat.js loadIndexes/loadInvertedIndex/loadDocTree fetch。
大文件(chunks.json ~26MB)用 GZipMiddleware 压缩(server.py 挂载)。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse

from app.storage.pageindex_io import read_index, read_index_bytes
from app.storage.paths import PathTraversalError

router = APIRouter(prefix="/pageindex", tags=["pageindex"])


@router.get("/{path:path}")
async def get_pageindex(path: str, request: Request):
    """读 pageindex/<path> 索引 JSON。大文件流式返回(避免二次加载)。"""
    cfg = request.app.state.app_config
    try:
        # 大文件直接 FileResponse(让 uvicorn 流式 + gzip 中间件压缩)
        from app.storage.paths import resolve_pageindex_path

        full = resolve_pageindex_path(path, cfg)
        if not full.is_file():
            raise FileNotFoundError(path)
        # JSON 文件直接流式返回
        return FileResponse(
            path=str(full),
            media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"not found: {path}")
    except PathTraversalError:
        raise HTTPException(status_code=403, detail="path escapes root")


@router.get("/_exists/{path:path}")
async def check_pageindex_exists(path: str, request: Request):
    """探测索引文件是否存在(供前端启动时检查 chunks/inverted 是否就绪)。"""
    cfg = request.app.state.app_config
    from app.storage.pageindex_io import index_exists

    return {"exists": index_exists(path, cfg)}
