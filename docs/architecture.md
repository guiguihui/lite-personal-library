# 架构

> 面向想理解 LQ-D Desktop 怎么工作、或要深度定制的开发者。
>
> UI 重构设计见 [ui-refactor.md](ui-refactor.md),品牌清理见 [brand-cleanup.md](brand-cleanup.md),开发指南见 [development.md](development.md),部署/打包见 [deployment.md](deployment.md)。

## 实现状态(2026-07-27 核实)

阶段 1-8 全部已实现并验证可用。当前正在进行 UI 重构(见 [ui-refactor.md](ui-refactor.md))。

| 阶段 | 状态 | 验证方式 |
|------|------|----------|
| 1 最小问答闭环 | ✅ | HTTP 服务端点 200,前端 chat.js 加载 |
| 2 索引构建接入 | ✅ | `build_full`/`build_incremental` 跑通,产物 chunks.json/inverted-index.json 已生成 |
| 3 Library 文档浏览 | ✅ | `frontend/library/library.js` 三栏布局 + `/api/content/*` 端点 200 |
| 4 PDF 提取双后端 | ✅ | `app/pdf/{local,mineru,epub,factory}.py` 全实现 + 26 测试通过 |
| 5 完整入库流水线 | ✅ | `app/ingest/pipeline.py` + 5 adapter(extract/clean/translate/validate/note)+ 27 测试通过 |
| 6 LLM 代理 | ✅ | `app/llm/proxy.py` SSE 流转发,`use_llm_proxy` 开关 |
| 7 Python 检索对拍 | ✅ | `app/retrieval/` 7 模块,54 pytest 用例全通过,golden benchmark 148 题对拍 |
| 8 打包发布 | ✅ | `yuulibrary-desktop.spec` + `run_app.py`,PyInstaller 产物 exe 启动 + 全端点 200 验证 |
| 9 UI 重构(Trae IDE 风格) | 🔄 | 见 [ui-refactor.md](ui-refactor.md) |

**测试汇总**:148 pytest 用例,144 passed + 4 skipped(检索 54 + HTTP API 37 + PDF 26+3skip + 入库 27+1skip)。

## 总体架构

PyWebView 桌面壳 + Web 前端 + Python 后端轻量 HTTP 服务。文档存本地。

```
┌──────────────────────────────────────────────────────────┐
│  pywebview 窗口(WebView2,主线程)                         │
│  ┌────────────────────────────────────────────────────┐  │
│  │  frontend/  (HTML + JS + CSS,WebView 加载)          │  │
│  │  ┌────────────────────────────────────────────────┐│  │
│  │  │ core/  (框架层)                                  ││  │
│  │  │ shell.js / shell.css / theme.js / icons.js     ││  │
│  │  │ events.js / store.js / tabs.js                 ││  │
│  │  │ sidebar.js / overview.js                       ││  │
│  │  │ statusbar.js / command-palette.js              ││  │
│  │  └────────────────────────────────────────────────┘│  │
│  │  ┌────────┬─────────┬─────────┬────────┬──────────┐│  │
│  │  │ chat/  │ library/│ manage/ │ upload/│ config/  ││  │
│  │  │session │ sidebar │ builder │ index  │ index    ││  │
│  │  │ llm    │ reader  │ ingest  │ queue  │          ││  │
│  │  │ agent  │         │         │        │          ││  │
│  │  └────────┴─────────┴─────────┴────────┴──────────┘│  │
│  │  shared/  (render.js / settings.js)                 │  │
│  │  katex/   (数学渲染)                                 │  │
│  └──────────────────────┬─────────────────────────────┘  │
│                         │ HTTP(127.0.0.1:8765)             │
├─────────────────────────┼────────────────────────────────┤
│  Python 后端(uvicorn,后台 daemon 线程)                  │
│  ┌────────────────────────────────────────────────────┐  │
│  │  app/http/      FastAPI 路由层                        │  │
│  │  app/storage/   文件 IO(替代 GitHub raw fetch)       │  │
│  │  app/config/    配置管理(BYOK key 走 keyring)         │  │
│  │  app/llm/       LLM 配置 + 代理                       │  │
│  │  app/index/     索引构建(包装 vendor/build_pageindex)│  │
│  │  app/ingest/    入库流水线编排                        │  │
│  │  app/pdf/       PDF 提取双后端(本地库 / MinerU API)  │  │
│  │  app/retrieval/ Python 检索重写(对拍工具)            │  │
│  │  app/vendor/    从 yuulibrary-main 复制的脚本         │  │
│  └────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────┤
│  data/  (用户数据,本地)                                   │
│  content/   pageindex/   config/   pdfs/                  │
└──────────────────────────────────────────────────────────┘
```

## 核心设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| GUI | PyWebView + Web 前端复用 | 复用现有 retrieval.js/chat.css/KaTeX,避免重写 UI |
| 运行时检索 + ReAct | 全在前端 | 复用 retrieval.js 纯函数 + chat ReAct agent,零重写 |
| Python 检索重写 | 对拍工具 | 移植 retrieval.js 到 Python,跑 golden benchmark 防回归 + 未来迁移基础 |
| LLM | 9 provider BYOK 前端直连 | 复用 chat buildRequest,key 存本地 keyring |
| PDF 提取 | 本地库 + MinerU 双后端 | 本地库零依赖离线,MinerU 高质量(公式/表格) |
| 文档存储 | 本地 data/content/ | markdown + frontmatter,直接读 |
| UI 风格 | Trae/Cursor IDE 风格 | 多标签 + 命令面板 + 状态栏,提升知识库操作效率 |

## 模块依赖图(零耦合)

依赖单向向下,无循环。`vendor/` 是叶子,只被 adapter 调用。

```
main ──┬── config ─── (叶子,无依赖)
       ├── http ──┬── storage ── config
       │          ├── index ── vendor.build_pageindex ─ config
       │          ├── ingest ──┬── pdf ── config
       │          │            ├── vendor.* ─ llm.config ─ config
       │          │            └── llm.config ─ config
       │          ├── llm ── config
       │          ├── search ── retrieval ── storage ── config
       │          └── status ── index/ingest/llm ── config
       └── (pywebview)
retrieval ── storage ── config   (独立工具,不进 http,不参与运行时)
```

## 前端架构

### 资源加载(frontend/index.html)

桌面壳注入变量,按顺序加载框架层与业务模块:

```html
<script>
  window.LQD_CHAT_BASE = "/";            // 后端根(FastAPI 挂载点)
  window.LQD_CHAT_RAW_BASE = "/raw/";   // 替代 GitHub raw,后端 /raw/content/<path>
</script>

<!-- core 框架层 -->
<link rel="stylesheet" href="/frontend/core/shell.css">
<script defer src="/frontend/core/theme.js"></script>
<script defer src="/frontend/core/icons.js"></script>
<script defer src="/frontend/core/events.js"></script>
<script defer src="/frontend/core/store.js"></script>
<script defer src="/frontend/core/tabs.js"></script>
<script defer src="/frontend/core/sidebar.js"></script>
<script defer src="/frontend/core/overview.js"></script>
<script defer src="/frontend/core/statusbar.js"></script>
<script defer src="/frontend/core/command-palette.js"></script>
<script defer src="/frontend/core/shell.js"></script>

<!-- 业务模块 -->
<link rel="stylesheet" href="/frontend/chat/chat.css">
<script defer src="/frontend/chat/retrieval.js"></script>
<script defer src="/frontend/chat/session.js"></script>
<script defer src="/frontend/chat/llm.js"></script>
<script defer src="/frontend/chat/agent.js"></script>
<script defer src="/frontend/chat/composer.js"></script>
<script defer src="/frontend/chat/messages.js"></script>
<script defer src="/frontend/chat/citations.js"></script>
<script defer src="/frontend/chat/index.js"></script>
<!-- library / manage / upload / config 类似 -->
```

### 五视图 + 多标签布局

| Activity | Sidebar | Main Tab | Overview |
|----------|---------|----------|----------|
| Chat | 历史对话列表 | 对话标签 | 检索引用 / 快捷操作 |
| Library | 文档分类列表 | 文档阅读标签 | 文档元信息 / 目录大纲 |
| Manage | 索引/任务列表 | 索引管理标签 | 任务进度 / 日志摘要 |
| Upload | 上传队列 | 上传队列标签 | 上传统计 |
| Config | 配置分组导航 | 配置页标签 | 配置说明 / 快捷键 |

### 模块接口约定

所有业务模块通过 `LqdTabs.register(type, component)` 注册,组件对象实现:

```javascript
{
  type: 'chat',
  getTitle(tab) { return tab.title || '新对话'; },
  getIcon() { return 'chat'; },
  mount(container, tab) { /* 渲染并恢复 tab.state */ },
  unmount(container, tab) { /* 保存 tab.state 并释放事件 */ },
  renderSidebar(container) { /* 渲染左侧 Sidebar */ },
  renderOverview(container, tab) { /* 渲染右侧 Overview */ }
}
```

跨模块通信统一走事件总线:

```javascript
LqdEvents.emit('chat:context', { query, contexts });
LqdEvents.on('chat:context', ({ contexts }) => renderCitations(contexts));
```

### chat 模块拆分

原 `frontend/chat/chat.js` 拆分为:

| 文件 | 职责 |
|------|------|
| `chat/session.js` | 当前会话与归档历史 CRUD |
| `chat/llm.js` | SSE 读取、provider 请求构造 |
| `chat/agent.js` | ReAct 工具循环、retrieveContext |
| `chat/composer.js` | 输入区与发送交互 |
| `chat/messages.js` | 消息气泡、思考过程、工具调用 |
| `chat/citations.js` | 引用片段格式化 |
| `chat/index.js` | 注册 `LqdChat` 组件 |

### 状态管理

- `LqdEvents`:跨模块事件总线。
- `LqdStore`:最小全局 UI 状态(theme / activity / tabs / status)。
- 业务数据由各模块自行管理(localStorage / sessionStorage / 内存)。
- localStorage key 统一使用 `lqd_*` 前缀,启动时从旧 `yuu_*` key 一次性迁移。

## 后端架构

### HTTP 服务层(app/http/)

FastAPI app 工厂(`server.py:create_app`),挂载所有 router + GZipMiddleware(压缩 chunks.json ~26MB)+ CORS(仅 127.0.0.1)+ StaticFiles(`/frontend`)。

| 路由文件 | 端点 | 用途 |
|----------|------|------|
| `routes_raw.py` | `GET /raw/content/<path>` | 替代 GitHub raw fetch |
| `routes_pageindex.py` | `GET /pageindex/<path>` | 索引 JSON |
| `routes_content.py` | `GET /api/content/*` | Library 浏览 |
| `routes_settings.py` | `GET/PUT /api/settings` | BYOK 配置 CRUD |
| `routes_index.py` | `POST /api/index/build` | 触发索引构建 |
| `routes_ingest.py` | `POST /api/ingest/*` | 入库各阶段 |
| `routes_llm_proxy.py` | `POST /api/llm/proxy` | LLM 代理(可选,解决 CORS) |
| `routes_status.py` | `GET /api/status` | 应用/索引/模型状态(新增) |
| `routes_search.py` | `GET /api/search` | 全局文本搜索(新增) |

### 文件 IO(app/storage/)

替代 GitHub raw fetch。所有路径过 `resolve_*_path` 校验 `..` 越界。

`content_io.read_markdown_body_lines` 对齐 chat.js `fetchMdLines`:读 md → 剥 front matter → 按 `\n` split。

### 配置管理(app/config/)

- `AppConfig`(frozen dataclass):content_dir/pageindex_dir/config_dir/pdfs_dir/pdf_strategy/http_host/http_port/use_llm_proxy
- `LlmConfig`:active_provider + 9 个 `LlmProviderConfig`(provider/model/base_url/has_key)
- BYOK key 存储:优先 `keyring`,无则降级 `llm.yaml` 明文
- `/api/settings` 响应只含 `has_key: bool`,不返回 key 本身;`/api/settings/key` 端点返回 key(供前端直连 LLM)

### 索引构建(app/index/)

包装 `vendor/build_pageindex.py`(从 yuulibrary-main 复制 + 路径参数化)。

- `builder.build_full(content_dir, pageindex_dir, llm_model="")` → `BuildResult`
- `builder.build_incremental(...)` → `BuildResult`
- `status.start_build(mode, ...)` → job_id(后台 threading.Thread 跑)

产物:`data/pageindex/{global-index,node-index,inverted-index,chunks,books/*,papers/*,notes/*}.json` + `.fingerprints.json`。

### 入库流水线(app/ingest/)

编排 extract→clean→translate→validate→note,长任务异步化。各 adapter 包装 `vendor/` 里对应脚本,统一签名。

### PDF 提取(app/pdf/)

双后端策略模式:

- `local.py`:PyMuPDF/pdfplumber(离线,零外部依赖)
- `mineru.py`:MinerU API(httpx,高质量,需 API key + 网络)
- `factory.py`:按扩展名 + 策略路由

### Python 检索重写(app/retrieval/)

移植 `retrieval.js` 30+ 函数到 Python,作为对拍工具。**不参与运行时检索**(运行时仍走前端 retrieval.js)。新增 `/api/search` 可调用这些函数返回搜索结果。

| 文件 | 移植自 retrieval.js |
|------|---------------------|
| `tokenizer.py` | tokenizeRaw/tokenizeUnique |
| `bm25.py` | buildBM25Stats/bm25Score/buildChunkStats/bm25ScoreChunk |
| `search.py` | search/searchInverted/searchTitlePhrase/searchDocRoute/searchMultiPath |
| `fuse.py` | rrfFuse/rm3Expand |
| `rerank.py` | lexicalRerank/shingle/jaccard/mmrSelect |
| `confidence.py` | classifyConfidenceMulti/computeConfidenceSignals |
| `benchmark.py` | 跑 golden.json 148 题,对拍 JS harness |

## 数据流

### 启动

```
python -m app.main
  → load_app_config(data/config/app.yaml)
  → create_app(cfg) → FastAPI(挂载 router + StaticFiles)
  → run_server_in_thread(uvicorn, 127.0.0.1, 8765)  daemon
  → webview.create_window("LQ-D", http://127.0.0.1:8765/frontend/index.html)
  → WebView 加载 index.html → 注入变量 → 加载 core/ + 业务模块
  → LqdShell.init() → 打开默认 Chat 标签
```

### 问答(运行时检索 + ReAct 全在前端)

```
用户输入 → LqdChatComposer.onSend
  → LqdChatSession.saveUserMessage
  → LqdSettings.load() + fetchApiKey()  [从 /api/settings]
  → LqdChatAgent.retrieveContext
      → LqdRetrieval.searchMultiPath
      → fetch /pageindex/global-index.json / node-index.json / inverted-index.json
      → fetch /pageindex/books/${id}.json
      → fetchMdSection → fetch /raw/content/...
  → LqdChatLLM.streamText
      → fetch LLM provider SSE
      → LqdChatMessages.appendStream
  → LqdChatSession.saveAssistantMessage
  → LqdEvents.emit('chat:context', contexts)
  → LqdOverview 渲染引用片段
```

### 入库

```
Manage 视图 → 选 PDF + book + slug → POST /api/ingest/extract
  → app.ingest.pipeline.run_pipeline [后台 asyncio]
      → [extract] app.pdf.factory → merged/book.md + images/
      → [clean] vendor/clean_markdown.py
      → [translate] vendor/translate_chapters.py(调 app.llm.config.resolve_for_tier)
      → [validate] vendor/validate_book.py → 38 项报告
      → [note] (paper) vendor/generate_paper_note.py → _index.md
  → md 落到 data/content/{books|papers}/<slug>/
  → POST /api/index/build {mode:"incremental"}
  → vendor/build_pageindex.py --incremental → patch_indexes → 写 data/pageindex/*.json
  → 前端下次 loadIndexes() 加载新索引
```

## 关键参数速查

| 参数 | 值 | 位置 |
|------|-----|------|
| HTTP 端口 | 8765 | app/config/defaults.py |
| BM25 K | 1.5 | retrieval.js:156 |
| BM25 B | 0.75 | retrieval.js:157 |
| FIELD_BOOST (node) | title:6, breadcrumb:3, terms:2, summary:2 | retrieval.js:155 |
| CHUNK_FIELD_BOOST | title:6, breadcrumb:3, body:1 | retrieval.js:329 |
| RRF k | 60 | retrieval.js:540 |
| RM3 M | 10 | retrieval.js:596 |
| MMR lambda | 0.6 | retrieval.js:715 |
| CHUNK_TARGET_CHARS | 500 | build_pageindex.py:466 |
| CHUNK_OVERLAP_CHARS | 100 | build_pageindex.py:467 |
| STOPWORD_DF_RATIO | 0.35 | build_pageindex.py:469 |
| Activity Bar 宽度 | 48px | core/shell.css |
| Sidebar 宽度 | 280px | core/shell.css |
| Overview 宽度 | 320px | core/shell.css |
| Tab Bar 高度 | 36px | core/shell.css |
| Status Bar 高度 | 24px | core/shell.css |

## 与原项目(yuulibrary-main)的关系

| 原项目 | 桌面应用 | 处理 |
|--------|----------|------|
| `scripts/build_pageindex.py` | `app/vendor/build_pageindex.py` | 复制 + 路径参数化 |
| `static/chat/retrieval.js` | `frontend/chat/retrieval.js` | 复制,仅命名空间与注释调整 |
| `static/chat/chat.js` | `frontend/chat/{index,session,llm,agent,composer,messages,citations}.js` | 拆分 + 移除浮动模式 |
| `static/chat/chat.css` | `frontend/chat/chat.css` | 删除浮动样式,统一 tokens |
| `static/katex/` | `frontend/katex/` | 原样复制 |
| `content/` | `data/content/` | 原样复制,清理旧品牌与第三方链接 |
| `.claude/skills/*/scripts/` | `app/vendor/` | 复制(extract/clean/translate/validate 等) |
| `tests/retrieval/golden.json` | `tests/retrieval/golden.json` | 原样复制 |
| `hugo.toml`/`layouts/`/`themes/` | — | 丢弃(Hugo 特有) |
| `.staticrypt.json`/`.github/workflows/` | — | 丢弃(Web 部署特有) |

详见 [development.md](development.md) 的"从原项目同步更新"章节。

## 品牌说明

桌面应用与原 Hugo 站已从「Yuunagi Library」统一迁移至 **LQ-D**。清理细节见 [brand-cleanup.md](brand-cleanup.md)。
