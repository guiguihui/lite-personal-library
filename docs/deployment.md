# 部署与打包

> 架构见 [architecture.md](architecture.md),开发指南见 [development.md](development.md)。

> **阶段 8 状态**:打包已实现并验证(2026-07-27)。`yuulibrary-desktop.spec` + `run_app.py` 入口,PyInstaller 6.21.0 onedir 模式,产物 `dist/yuulibrary-desktop/`(约 100MB,exe 11.5MB)。已验证:exe 启动后 uvicorn 起 8765,pywebview 窗口加载前端,`/frontend/*` + `/pageindex/*` + `/api/settings` + `/api/content/*` 端点全 200,打包内 data/ 索引可读。当前为策略 A(打包内 data,演示/便携);策略 B(用户目录)见下方。

## 运行模式

### 开发模式

```bash
python -m app.main
```

uvicorn 后台 daemon 线程跑 HTTP 服务(127.0.0.1:8765),pywebview 主线程开窗口。关窗口时 pywebview 退出,daemon 线程自动结束。

### 无 pywebview 模式(开发期)

pywebview 未安装时,main.py 自动降级:启动 HTTP 服务 + 用浏览器打开 `http://127.0.0.1:8765/frontend/index.html`。适合远程开发或无 GUI 环境。

### 纯 HTTP 模式(测试)

```bash
python -c "
from app.config.store import load_app_config
from app.http.server import create_app, run_server
from dataclasses import replace
import os
data = os.path.abspath('data')
cfg = load_app_config(os.path.join(data,'config'))
cfg = replace(cfg, content_dir=os.path.join(data,'content'), pageindex_dir=os.path.join(data,'pageindex'))
run_server(create_app(cfg), '127.0.0.1', 8765)
"
```

只跑后端,用浏览器或 curl 测 API。

## 数据目录布局

```
data/
├── content/           # markdown 文档(用户内容)
│   ├── books/
│   ├── papers/
│   ├── notes/
│   └── _reference/
├── pageindex/         # 索引产物(build_pageindex 写)
│   ├── global-index.json
│   ├── node-index.json
│   ├── inverted-index.json    # 全量构建后才有
│   ├── chunks.json            # 全量构建后才有
│   ├── books/
│   ├── papers/
│   ├── notes/
│   └── .fingerprints.json
├── config/            # 应用配置
│   ├── app.yaml       # 应用配置(路径/端口/PDF 策略)
│   └── llm.yaml       # BYOK 配置(has_key 标记 + 可选明文 key)
└── pdfs/              # PDF 原档 + 提取中间产物
```

首次启动时 `app/main.py:_ensure_data_dirs()` 自动创建这些目录。`app.yaml`/`llm.yaml` 不存在时用默认值写盘。

## 配置文件

### app.yaml

```yaml
content_dir: e:/知识库/yuulibrary-desktop/data/content
pageindex_dir: e:/知识库/yuulibrary-desktop/data/pageindex
pdfs_dir: e:/知识库/yuulibrary-desktop/data/pdfs
pdf_strategy: local        # local | mineru
http_host: 127.0.0.1
http_port: 8765
use_llm_proxy: false       # 前端是否走后端 LLM 代理
```

### llm.yaml

```yaml
active_provider: anthropic
remember_key: false
providers:
  anthropic:
    model: claude-sonnet-4-6
    base_url: https://api.anthropic.com
    has_key: true          # key 本身在 keyring,不在此文件
  deepseek:
    model: deepseek-v4-flash
    base_url: https://api.deepseek.com
    has_key: false
  # ... 其余 provider
```

keyring 不可用时,降级到 `_plain_keys` 明文区(不暴露给前端 `/api/settings`):

```yaml
_plain_keys:
  deepseek: sk-...        # 仅 keyring 不可用时
```

## BYOK key 存储

| 后端 | 存储 | 安全性 |
|------|------|--------|
| keyring 可用 | Win Credential Manager / macOS Keychain / Linux SecretService | 加密,推荐 |
| keyring 不可用 | `data/config/llm.yaml` 明文区 `_plain_keys` | 明文,本地应用可接受 |

`/api/settings` 响应永远只含 `has_key: bool`,不返回 key 本身。`/api/settings/key?provider=X` 端点返回 key(供前端 BYOK 直连 streamText),仅 127.0.0.1 可访问。

## 打包(阶段 8,已实现)

### PyInstaller(spec 文件,推荐)

项目根有 `yuulibrary-desktop.spec`(已验证可用)+ `run_app.py`(打包入口)。

```bash
pip install pyinstaller
pyinstaller yuulibrary-desktop.spec --noconfirm
```

产物:`dist/yuulibrary-desktop/yuulibrary-desktop.exe`(onedir,约 100MB)。

spec 关键配置:
- `--windowed`(`console=False`):无控制台窗口
- `datas`:打包 `frontend/` + `data/`(策略 A,演示用)
- `hiddenimports`:uvicorn 动态子模块(logging/protocols.http.auto/protocols.websockets.auto/lifespan.on)+ pywebview 平台后端(webview.platforms.edgechromium/winforms)+ `collect_submodules("uvicorn")` 保险
- `excludes`:pytest/tests(不打包测试依赖)
- `pathex`:项目根(让 `import app` 可解析)

`app/main.py:_project_root()` 已适配 `sys._MEIPASS`(PyInstaller 6.x onedir 下指向 `_internal/`,`data/` + `frontend/` 都在 `_internal/` 下,路径正确)。

### 等价 CLI 命令(不用 spec)

```bash
pyinstaller --name yuulibrary-desktop \
  --windowed --onedir \
  --add-data "frontend:frontend" \
  --add-data "data:data" \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import webview.platforms.edgechromium \
  --hidden-import webview.platforms.winforms \
  --exclude-module pytest \
  run_app.py
```

注意:
- `--windowed` 无控制台窗口(Windows)
- `--add-data` 打包 frontend/ + data/(或 data/ 用用户目录,见下)
- `--hidden-import` uvicorn 动态 import 的模块需显式声明
- pywebview 的 WebView2 依赖(Win)需系统自带或打包 runtime

### 验证打包

```bash
# 启动 exe(后台)
./dist/yuulibrary-desktop/yuulibrary-desktop.exe &
sleep 8
# 测端点(应全 200)
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/frontend/index.html
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/pageindex/global-index.json
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/api/settings
```

### 数据目录策略

打包后,数据目录有两种策略:

**策略 A:打包内 data/(演示/便携)**
- 把 `data/content/` + `data/pageindex/` 打进 exe
- 用户改不了内容,适合演示版
- `app/main.py` 的 `_project_root()` 需适配 PyInstaller 的 `_MEIPASS`

**策略 B:用户目录(推荐生产)**
- 数据放 `~/.yuulibrary-desktop/{content,pageindex,config,pdfs}/`
- 首次启动复制示例内容
- 改 `_ensure_data_dirs()` 用 `Path.home()/.yuulibrary-desktop`

### Nuitka(备选)

```bash
pip install nuitka
python -m nuitka --standalone --onefile --enable-plugin=pywebview app/main.py
```

体积更小,但编译慢。

## 安全注意事项

### 路径遍历

`/raw/content/<path>` 和 `/pageindex/<path>` 必须校验 `..` 越界。`app/storage/paths.py:resolve_*_path` 用 `Path.resolve()` 后检查在 root 内,越界抛 403。

### 网络绑定

HTTP 服务绑定 `127.0.0.1`(仅本地),不暴露到网络。CORS 只允许 `http://127.0.0.1:8765`。

### BYOK key

- key 不进 git(`.gitignore` 排除 `data/config/`)
- 优先 keyring 加密
- `/api/settings` 不返回 key 本身
- 前端直连 LLM 时 key 在浏览器内存(与原项目一致)

## 故障排查

### 启动失败:端口被占

```
ERROR: [Errno 10048] address already in use
```

改 `data/config/app.yaml` 的 `http_port`,或杀掉占用 8765 的进程。

### 索引构建失败:content_dir 不存在

检查 `app.yaml` 的 `content_dir` 路径。首次启动 `_ensure_data_dirs()` 会建空目录,需手动放内容或从 yuulibrary-main 复制。

### 前端加载 404

- `/frontend/index.html` 404 → `frontend/` 目录不在项目根
- `/frontend/chat/chat.js` 404 → 没复制 chat 资源
- `/pageindex/global-index.json` 404 → 没构建索引,去 Manage → 全量构建

### LLM 调用失败

- 401 → API key 错误,检查 `/api/settings/key?provider=X`
- CORS → Anthropic 直连受限,启用 LLM 代理(设置 → use_llm_proxy)
- 超时 → 检查网络,或换 provider

### pywebview 窗口黑屏

WebView2 Runtime 缺失(Win10 早期版本)。安装 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。

## 升级原项目同步

当原项目 yuulibrary-main 更新,同步到桌面应用:

```bash
# retrieval.js / chat.css(直接覆盖)
cp e:/知识库/yuulibrary-main/static/chat/retrieval.js frontend/chat/
cp e:/知识库/yuulibrary-main/static/chat/chat.css frontend/chat/

# chat.js(需手动合并 Settings 改造,不能直接覆盖)
# 对比 diff,把桌面应用的 Settings 改造应用到新版本

# build_pageindex.py(需保留 build() 函数)
# 对比 diff,应用到 vendor 版本,保留路径参数化

# 入库脚本(直接覆盖)
cp e:/知识库/yuulibrary-main/.claude/skills/add-book-to-library/scripts/*.py app/vendor/
cp e:/知识库/yuulibrary-main/.claude/skills/add-paper-to-library/scripts/*.py app/vendor/

# golden benchmark(直接覆盖)
cp e:/知识库/yuulibrary-main/tests/retrieval/golden.json tests/retrieval/
```

同步后跑 benchmark 对拍验证:

```bash
node tests/retrieval/harness.js          # JS baseline
python -m app.retrieval.benchmark        # Python 版
# 分数差异应 < 2%
```
