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

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
