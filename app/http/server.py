"""FastAPI app 工厂 + uvicorn 启动。

职责:创建 FastAPI app,挂载所有 router、静态资源、CORS、gzip 中间件。
uvicorn 在后台线程跑(daemon),pywebview 在主线程。
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app.config.schema import AppConfig
from app.http.routes_app_config import router as app_config_router
from app.http.routes_content import router as content_router
from app.http.routes_index import router as index_router
from app.http.routes_ingest import router as ingest_router
from app.http.routes_llm_proxy import router as llm_proxy_router
from app.http.routes_links import router as links_router
from app.http.routes_pageindex import router as pageindex_router
from app.http.routes_raw import router as raw_router
from app.http.routes_settings import router as settings_router
from app.http.routes_search import router as search_router
from app.http.routes_status import router as status_router


class NoCacheFrontendMiddleware:
    """给 /frontend/* 响应加 Cache-Control: no-store,防止 WebView2 缓存旧前端。

    见 create_app 中的注释:升级前端后,WebView2 启发式缓存会导致加载到旧版
    JS,表现为"除聊天页外其他页空白"。no-store 强制每次拉最新资源。

    用纯 ASGI 中间件(而非 BaseHTTPMiddleware)以确保对 StaticFiles 的
    FileResponse 也生效——BaseHTTPMiddleware 在某些 Starlette 版本下
    不改写 mount 子应用的响应头。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/frontend/"):
            await self.app(scope, receive, send)
            return

        # 拦截响应头,注入 no-cache
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # 移除已有的缓存相关头
                filtered = [
                    (k, v) for (k, v) in headers
                    if k.lower() not in (b"cache-control", b"pragma", b"expires")
                ]
                filtered.extend([
                    (b"cache-control", b"no-store, must-revalidate"),
                    (b"pragma", b"no-cache"),
                    (b"expires", b"0"),
                ])
                message = dict(message)
                message["headers"] = filtered
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(cfg: AppConfig) -> FastAPI:
    """FastAPI 工厂:挂载所有 router + 静态资源 + 中间件。"""
    app = FastAPI(
        title="轻量个人知识库",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # 状态注入(路由通过 request.app.state 读)
    app.state.app_config = cfg

    # 中间件:gzip 压缩大文件(chunks.json ~26MB → ~16MB)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # 中间件:前端静态资源禁缓存(no-store)。
    # 关键:WebView2 会启发式缓存 /frontend/* 的 JS/HTML。若用户跑过旧版前端,
    # 升级后 WebView2 仍从缓存加载旧 JS → 旧前端(三标签,manage/upload/config
    # 面板未初始化)→ 典型症状"除聊天页外其他页空白"。no-store 强制每次拉最新。
    app.add_middleware(NoCacheFrontendMiddleware)
    # CORS:仅本地(WebView2 从 http://127.0.0.1:8765 加载,同源,但保险起见)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://{cfg.http_host}:{cfg.http_port}"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(raw_router)
    app.include_router(pageindex_router)
    app.include_router(settings_router)
    app.include_router(app_config_router)
    app.include_router(content_router)
    app.include_router(status_router)
    app.include_router(search_router)
    app.include_router(index_router)
    app.include_router(llm_proxy_router)
    app.include_router(links_router)
    app.include_router(ingest_router)

    # 静态资源:前端 frontend/ 目录(WebView 加载 index.html/chat/katex)
    frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
    if frontend_dir.is_dir():
        app.mount("/frontend", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    # 根路径重定向到 /frontend/index.html
    from fastapi.responses import RedirectResponse

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(url="/frontend/index.html")

    return app


def run_server(app: FastAPI, host: str, port: int) -> None:
    """uvicorn 启动(在主线程阻塞调用,供 main.py 包到后台线程)。"""
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


def run_server_in_thread(
    app: FastAPI, host: str, port: int
) -> tuple[threading.Thread, threading.Event]:
    """后台线程跑 uvicorn(daemon,主线程退出自动结束)。

    返回 (thread, ready_event)。ready_event 在 uvicorn 真正开始监听后被 set;
    若绑定失败(端口被占),线程内捕获 OSError 并把异常塞回 event 的 `_error`,
    ready 保持 unset——调用方据此判断是否连到了"别的进程"。

    为什么要这个:此前端口被占时 uvicorn.run 在线程内静默抛 OSError 死亡,
    main.py 的 urlopen 探测会连到占用端口的旧进程(可能是旧版前端),
    导致 WebView 加载到陈旧/不一致的页面(典型症状:除聊天页外其他页空白)。
    """
    ready = threading.Event()
    thread = threading.Thread(
        target=_serve_with_signal,
        args=(app, host, port, ready),
        daemon=True,
        name="uvicorn-server",
    )
    thread.start()
    return thread, ready


def _serve_with_signal(
    app: FastAPI, host: str, port: int, ready: threading.Event
) -> None:
    """跑 uvicorn,绑定成功后 set ready;绑定失败记录异常到 ready._error。"""
    import uvicorn
    from uvicorn.config import Config
    from uvicorn.server import Server

    config = Config(app=app, host=host, port=port, log_level="info")
    server = Server(config)

    # lifespan startup 触发即代表套接字已绑定并开始监听
    @app.on_event("startup")
    async def _mark_ready() -> None:  # type: ignore[unused-ignore]
        ready.set()

    try:
        server.run()
    except OSError as exc:
        ready._error = exc  # type: ignore[attr-defined]
        ready.set()  # 解除主线程等待,让它读到错误
    except Exception as exc:  # noqa: BLE001 - 任何启动异常都要回传
        ready._error = exc  # type: ignore[attr-defined]
        ready.set()
