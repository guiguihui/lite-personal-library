# LQ-D Desktop — Project Instructions

> **LQD-clear 精简版**:以 LQD-fin 为基座,砍掉全部问题单相关功能(问题单入口、
> 问题单检索/构建、`issue` 文档类型、聊天 search_issues 工具与问题单引用卡片),
> 保留文档库、聊天、本机文件检索、上传、索引管理、配置等其余全部功能。

## Tech Stack
- **Backend**: Python 3.10+, FastAPI + uvicorn (127.0.0.1:8766), pydantic v2
- **Frontend**: Vanilla JS (no framework) + CSS, loaded via `<script defer>` in `frontend/index.html`
- **Desktop shell**: pywebview (frameless WebView2 window, JS bridge for native dialogs)
- **Packaging**: PyInstaller (`yuulibrary-desktop.spec`)
- **Testing**: pytest (`tests/`) + Node (`tests/frontend/*.test.js`)

## 架构(双轨检索)

```
构建任务(双轨并行,app/index/status.py)
├── legacy 轨道: vendor/build_pageindex → global/node/inverted/chunks.json
│   (library 阅读页 /pageindex 兼容读取面,行为与 LQ-D-desktop 完全一致)
└── V3 轨道: app.index.v3.supervisor → worker 子进程 → current-v3.json 原子发布
    → link-index.json 知识链接; 供 /api/search(聊天)+ /api/links
```

- **/api/search**: V3 优先(current-v3.json 存在时,search_pinned_view,返回
  generation/view_id/doc_key/source_md/line_num 等稳定引用);V3 缺失时回退
  legacy Python 多路检索(兼容面)。
- **/api/status**: `index_version` ∈ legacy|v3|both;`index_ready` 任一轨道就绪。
- **聊天**: frontend/chat/agent.js `search_library` 走 /api/search(不再下载 26MB
  索引),保留 4 工具(search_library/search_local_files/get_section/rewrite_query)
  + 本机文件检索 + 两组引用卡片(文档/本机文件)。
- **知识链接**: app/knowledge/* + /api/links/* + 前端 wikilinks/反链/局部图谱(d3)。

## Code Style
- Python: `from __future__ import annotations`, frozen dataclasses for config/schema, type hints required
- JS: IIFE pattern `(function () { 'use strict'; ... })()`, no ES modules (defer loading order)
- CSS: BEM-ish naming with `lqd-` prefix, CSS custom properties for theming
- Naming: snake_case (Python), camelCase (JS), kebab-case (CSS classes)
- All Chinese comments are the norm — follow existing style

## Build & Run
- **Dev**: `python -m app.main` (starts uvicorn in background thread + opens pywebview window)
- **Tests**: `python -m pytest tests/ -v`; Node: `node --test tests/frontend/*.test.js`
- **Package**: `pyinstaller yuulibrary-desktop.spec` (produces `dist/` exe)
- **V3 worker**: exe 以 `--pageindex-v3-worker <request>` 模式跑子进程构建(见 run_app.py)
- **Lint**: no configured linter — follow existing conventions

## Project Structure
```
app/
  config/      — AppConfig (frozen dataclass), LlmConfig, BYOK keyring
  http/        — FastAPI routes_*.py + schemas.py (Pydantic models)
  storage/     — File IO (content_io, pageindex_io, paths, link_index_io)
  index/       — 双轨: builder/status(legacy) + v2/(segment 引擎) + v3/(不可变 Generation/View, worker/supervisor/runtime)
  knowledge/   — 知识链接(wikilinks/反链/局部图谱/导出/迁移, B 移植)
  fileindex/   — 本机文件索引构建(A 独有)
  fileparse/   — docx/pptx/xlsx/txt 解析(A 独有)
  ingest/      — Pipeline orchestration (extract→clean→translate→validate→note)
  pdf/         — PDF extraction (local PyMuPDF + MinerU API, strategy pattern)
  retrieval/   — Python 检索(legacy 多路 + search_view V3 原生路径)
  llm/         — LLM config + proxy (9 providers, BYOK)
  vendor/      — Scripts copied from yuulibrary-main (build_pageindex, clean_markdown, etc.)
frontend/
  core/        — Shell, sidebar, tabs, events, store, theme, icons, tooltip, modal, toast
  chat/        — Chat module (session, llm, agent, composer, messages, citations, retrieval)
  library/     — Document browser (3-pane: sidebar + reader + overview + 知识链接反链/图谱)
  manage/      — Index management UI
  upload/      — Upload queue UI
  config/      — Settings UI (BYOK providers)
  filesearch/  — 本机文件检索(A 独有)
  shared/      — render.js, settings.js, knowledge-flags/wikilinks/link-popover
  vendor/      — d3.v7.9.0.min.js(局部图谱)
  katex/       — Math rendering (vendored)
data/           — User data (content/, pageindex/, config/, pdfs/, fileindex/) — gitignored
```

## Conventions
- **Config immutability**: all config objects are `@dataclass(frozen=True)`, modify by returning new copy
- **Module registration**: frontend modules register via `LqdTabs.register(type, component)`, component implements `getTitle/getIcon/mount/unmount/renderSidebar`
- **Event bus**: cross-module communication via `LqdEvents.emit/on(event, payload)`, events named `module:action`
- **localStorage**: keys prefixed `lqd_*` (migrated from legacy `yuu_*`)
- **HTTP routes**: each `routes_*.py` has `prefix="/api/<module>"`, router included in `server.py:create_app()`
- **Index cache**: large index files cached on `request.app.state` keyed by (path, mtime) for invalidation
- **PyInstaller**: dev mode uses `__file__` paths; packaged mode uses `sys._MEIPASS` — always use `_project_root()` helper
- **Commit style**: conventional commits in Chinese (`feat:`, `fix:`, `chore:`, `docs:`)

## Key Parameters
| Parameter | Value | Location |
|-----------|-------|----------|
| HTTP port | 8765 | app/config/defaults.py |
| BM25 K/B | 1.5 / 0.75 | retrieval.js:156-157 |
| Chunk size | 500 chars target, 100 overlap | build_pageindex.py:466-467 |
| Sidebar width | 280px | core/shell.css |
| Activity bar width | 48px | core/shell.css |

## Adding New Features
1. **Backend route**: create `app/http/routes_<name>.py` with `router = APIRouter(prefix="/api/<name>")`, include in `server.py`
2. **Frontend module**: create `frontend/<name>/` with `index.js` registering via `LqdTabs.register`, add `<script defer>` + `<link>` to `index.html`
3. **Sidebar nav**: add entry to `SIDE_NAV` array in `frontend/core/shell.js` + `ACTIVITIES` array
4. **Schemas**: add Pydantic models to `app/http/schemas.py`
