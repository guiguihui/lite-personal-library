# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Yuunagi Library Desktop.

阶段 8 打包配置(策略 A:打包内 data,演示/便携)。

用法:
  pyinstaller yuulibrary-desktop.spec

产物:dist/yuulibrary-desktop/yuulibrary-desktop.exe(onedir,含 frontend/ + data/)
"""

import os
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# uvicorn 动态 import 的子模块必须显式声明(见 deployment.md)
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # pywebview 平台后端
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    # fastapi/pydantic 运行时
    "email.mime.multipart",
    "email.mime.text",
]

# 收集 uvicorn 全部子模块(保险)
hiddenimports += collect_submodules("uvicorn")

# PageIndex V2/V3 + knowledge 子模块(worker 子进程冷启动依赖,必须全量收集)
hiddenimports += collect_submodules("app.index.v2")
hiddenimports += collect_submodules("app.index.v3")
hiddenimports += collect_submodules("app.knowledge")
hiddenimports += ["app.pageindex_worker", "app.pageindex_v3_worker"]

a = Analysis(
    ["run_app.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=[
        # 前端资源(chat/library/manage/katex)
        ("frontend", "frontend"),
        # 打包内 data(策略 A:演示用;生产改用户目录见 deployment.md 策略 B)
        ("data", "data"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不必要的测试依赖
        "pytest",
        "pytest_cov",
        "tests",
        # 排除备份文件(含 API key)
        "llm.yaml.backup",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yuulibrary-desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # --windowed:无控制台窗口
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # 可选:("assets/app.ico",)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="yuulibrary-desktop",
)
