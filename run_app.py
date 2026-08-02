"""PyInstaller 打包入口脚本。

用法(deployment.md 阶段 8):
  pyinstaller --name yuulibrary-desktop \\
    --windowed --onedir \\
    --add-data "frontend:frontend" \\
    --add-data "data:data" \\
    --hidden-import uvicorn.logging \\
    --hidden-import uvicorn.protocols.http.auto \\
    --hidden-import uvicorn.protocols.websockets.auto \\
    --hidden-import uvicorn.lifespan.on \\
    run_app.py

或直接用 spec 文件:
  pyinstaller yuulibrary-desktop.spec

打包后双击 dist/yuulibrary-desktop/yuulibrary-desktop.exe 启动。
"""

from __future__ import annotations

import sys


def _dispatch() -> int:
    # PyInstaller workers reuse this executable. Route the special mode before
    # importing app.main so a build subprocess does not initialize the desktop
    # window, HTTP server, or long-lived application state.
    if len(sys.argv) > 1 and sys.argv[1] == "--pageindex-v3-worker":
        from app.pageindex_v3_worker import main as worker_main

        return worker_main(sys.argv[2:])

    if len(sys.argv) > 1 and sys.argv[1] == "--pageindex-worker":
        from app.pageindex_worker import main as worker_main

        return worker_main(sys.argv[2:])

    from app.main import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(_dispatch())
