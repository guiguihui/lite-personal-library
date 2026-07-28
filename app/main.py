"""应用入口:pywebview + HTTP 服务 + WebView 加载。

启动流程:
  1. 读 AppConfig(data/config/app.yaml,不存在则默认+写盘)
  2. create_app(config) → FastAPI
  3. 后台线程跑 uvicorn(127.0.0.1:8765)
  4. pywebview 主线程打开 WebView 加载 http://127.0.0.1:8765/frontend/index.html

关窗口时 pywebview 退出,daemon 线程自动结束。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from app.config.store import load_app_config
from app.http.server import create_app, run_server_in_thread


def _project_root() -> Path:
    """项目根目录(app/ 的父目录)。

    PyInstaller 打包后:用 sys._MEIPASS(临时解压目录,含打包的 frontend/ + data/)。
    开发模式:用 __file__ 的父目录。
    """
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _ensure_data_dirs() -> tuple[str, str, str, str]:
    """确保 data/{content,pageindex,config,pdfs} 默认目录存在,返回默认路径。

    仅在配置不存在时提供默认值;用户通过配置页自定义的路径不会被覆盖
    (见 main() 中 load_app_config 后不再 replace)。

    PyInstaller 策略 A(打包内 data):data/ 在 _MEIPASS 下,只读,
    不创建(已打包进去)。开发模式:在项目根 data/ 下创建。
    """
    root = _project_root()
    data_dir = root / "data"
    # 打包模式(_MEIPASS)下 data/ 是只读的打包资源,不创建
    if not hasattr(sys, "_MEIPASS"):
        for sub in ("content", "pageindex", "config", "pdfs"):
            (data_dir / sub).mkdir(parents=True, exist_ok=True)
    config_dir = str(data_dir / "config")
    content_dir = str(data_dir / "content")
    pageindex_dir = str(data_dir / "pageindex")
    pdfs_dir = str(data_dir / "pdfs")
    return content_dir, pageindex_dir, config_dir, pdfs_dir


def _ensure_configured_dirs(cfg) -> None:
    """确保配置中指向的目录存在(用户自定义路径时自动创建)。

    不覆盖 cfg 中的路径值,只做存在性保证。打包模式(_MEIPASS)下跳过
    (data/ 只读,自定义路径在打包模式无意义)。
    """
    if hasattr(sys, "_MEIPASS"):
        return
    from pathlib import Path

    for d in (cfg.content_dir, cfg.pageindex_dir, cfg.pdfs_dir):
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)


def main() -> int:
    """应用入口。"""
    content_dir, pageindex_dir, config_dir, pdfs_dir = _ensure_data_dirs()

    # 读配置(不存在则默认+写盘,默认路径指向 data/)
    cfg = load_app_config(config_dir)
    # 确保配置中指向的目录存在(支持配置页自定义路径),不覆盖配置值
    _ensure_configured_dirs(cfg)

    # 启动 HTTP 服务(后台线程)
    app = create_app(cfg)

    # 预检:端口是否已被占用。若已占用,直接报错退出,避免 uvicorn 在线程内
    # 静默退出后,urlopen 连到占用端口的旧进程(可能是旧版前端),导致
    # WebView 加载到陈旧/不一致页面(典型症状:除聊天页外其他页空白)。
    import socket as _socket

    probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
    try:
        probe.bind((cfg.http_host, cfg.http_port))
    except OSError as _bind_exc:
        import sys as _sys

        print(
            f"[LQ-D] 端口 {cfg.http_host}:{cfg.http_port} 已被占用,无法启动 HTTP 服务。\n"
            f"({type(_bind_exc).__name__}: {_bind_exc})\n"
            f"最常见原因:上一次启动的 LQ-D 进程未退出。\n"
            f"请先结束占用该端口的进程再重试,或修改 data/config/app.yaml 的 http_port。",
            file=_sys.stderr,
        )
        return 1
    finally:
        probe.close()

    server_thread, ready = run_server_in_thread(app, cfg.http_host, cfg.http_port)

    # 等 uvicorn 就绪或绑定失败(端口被占等)
    # 关键:必须确认本进程的 server 真的起来了。此前若端口被占,
    # uvicorn 在线程内静默退出(它自己 catch OSError 打日志),下面的 urlopen
    # 会连到占用端口的旧进程(可能是旧版前端),WebView 加载到陈旧页面
    # → 除聊天外其他页空白。用 lifespan startup 信号判断:绑定成功才会
    # 触发 startup;绑定失败 uvicorn 直接 shutdown,信号永不 set。
    import urllib.request

    url = f"http://{cfg.http_host}:{cfg.http_port}/frontend/index.html"
    started = ready.wait(timeout=10)
    bind_error = getattr(ready, "_error", None)
    if bind_error is not None:
        import sys as _sys

        print(
            f"[LQ-D] HTTP 服务启动失败:{bind_error}\n"
            f"通常是端口 {cfg.http_port} 被占用。",
            file=_sys.stderr,
        )
        return 1
    if not started or not server_thread.is_alive():
        import sys as _sys

        print(
            f"[LQ-D] HTTP 服务未能在端口 {cfg.http_host}:{cfg.http_port} 启动。\n"
            f"请检查上方 uvicorn 日志,或修改 data/config/app.yaml 的 http_port。",
            file=_sys.stderr,
        )
        return 1

    # 二次确认:本进程响应(ready 已 set 说明 lifespan 跑了,即本进程在监听)
    try:
        urllib.request.urlopen(url, timeout=2)
    except Exception as _exc:
        import sys as _sys

        print(f"[LQ-D] HTTP 服务就绪但探测失败:{_exc}", file=_sys.stderr)
        return 1

    # 启动 pywebview(主线程阻塞)
    # URL 加启动时间戳查询参数:强制 WebView2 每次启动都重新加载 index.html,
    # 绕过它对旧版前端的缓存(否则升级前端后仍加载旧 JS → 除聊天外其他页空白)。
    # 后端 NoCacheFrontendMiddleware 已对 /frontend/* 发 no-store,但已落入
    # WebView2 磁盘缓存的旧资源仍会被复用,时间戳让 URL 唯一以彻底规避。
    load_url = f"{url}?t={int(time.time())}"
    # debug=True 让 WebView2/EdgeChromium 开 F12 DevTools,便于排查前端 SSE/渲染问题。
    # 临时排查用,正式发布可去掉。
    webview_debug = True
    try:
        import webview

        webview.create_window(
            "LQ-D",
            load_url,
            width=1400,
            height=900,
            min_size=(1000, 600),
            js_api=DesktopApi(),
        )
        webview.start(debug=webview_debug)
    except ImportError:
        # pywebview 未安装 → 退化到浏览器打开(开发期)
        print(f"[pywebview not installed] open {load_url} in browser", file=sys.stderr)
        import webbrowser

        webbrowser.open(load_url)
        # 阻塞等 uvicorn(前台模式)
        try:
            while server_thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            return 0
    return 0


class DesktopApi:
    """pywebview JS Bridge:暴露原生文件对话框 + 应用重启给前端。

    前端通过 window.pywebview.api.choose_files() / restart() 调用。
    浏览器 <input type="file"> 不给真实路径,桌面端必须用此 API。
    """

    @staticmethod
    def choose_files(file_types: list[str] | None = None) -> list[str]:
        """打开原生文件选择对话框,返回真实路径列表。

        file_types: 如 ['PDF Files (*.pdf)', 'EPUB Files (*.epub)']。
        返回空列表表示用户取消。
        """
        try:
            import webview

            win = webview.windows[0] if webview.windows else None
            if win is None:
                return []
            # pywebview 新版用 FileDialog.OPEN(老版 OPEN_DIALOG 已废弃,
            # 每次调用打印 deprecation 警告)。优先新 API,降级老常量。
            dialog_type = getattr(getattr(webview, "FileDialog", None), "OPEN", None)
            if dialog_type is None:
                dialog_type = webview.OPEN_DIALOG  # pragma: no cover — 老版 pywebview
            result = win.create_file_dialog(
                dialog_type,
                file_types=file_types or [
                    "PDF Files (*.pdf)",
                    "EPUB Files (*.epub)",
                    "DOCX Files (*.docx)",
                ],
                allow_multiple=True,
            )
            # create_file_dialog 返回 tuple 或 None
            return list(result) if result else []
        except Exception:
            return []

    @staticmethod
    def choose_directory(self) -> str:
        """打开目录选择对话框,返回路径(供配置页浏览按钮用)。"""
        try:
            import webview

            win = webview.windows[0] if webview.windows else None
            if win is None:
                return ""
            dialog_type = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
            if dialog_type is None:
                dialog_type = webview.FOLDER_DIALOG  # pragma: no cover — 老版
            result = win.create_file_dialog(dialog_type)
            return result if isinstance(result, str) else ""
        except Exception:
            return ""

    @staticmethod
    def restart(self) -> None:
        """重启应用(配置页"重启应用"按钮调用)。"""
        import os
        import sys

        # 优雅退出,由外部启动器(如 run_app.py)负责重启
        os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
