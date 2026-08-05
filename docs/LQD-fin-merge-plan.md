# LQD-fin — LQ-D-desktop 为基座 + norag-dev 全架构合并 迭代计划

## Context

用户要求以 **LQ-D-desktop（A，main 版本）** 为主体，合并 **norag-dev（B）** 的全部底层技术架构，**不改动原 LQ-D-desktop**，在项目目录外新建综合版本 **`D:\1\LQD-fin`**。

已确认决策：
1. **位置**：`D:\1\LQD-fin`（与 LQ-D-desktop 平级）
2. **检索深度**：完整 V3（不可变 Generation/View + current-v3.json 原子指针 + 子进程 worker + Base/Delta 检索）
3. **Legacy**：保留 legacy 单体 JSON 兼容
4. **测试**：全量移植（A 148 例 + B ~60 个 pytest 文件 + 4 个 Node 测试）

## 勘察结论（决定合并策略的关键事实）

| 维度 | A (LQ-D-desktop) | B (norag-dev) |
|---|---|---|
| 索引构建 | vendor/build_pageindex.py（1380 行，MD5 指纹增量，全量重写单体 JSON） | v2 segment_builder + v3 base/delta 引擎（子进程 worker，no-op <500ms，单文档 <5s） |
| 检索入口 | 双实现：前端 YuuRetrieval（聊天）+ 后端 search_multi_path | 单一 /api/search → search_pinned_view（V3） |
| 知识链接 | ✗ | app/knowledge/ 12 模块 + /api/links/* + 前端 wikilinks/反链/局部图谱(d3) |
| 独有能力 | issues 问题单检索、filesearch 本机文件检索、Codex UI、frameless 窗口、llm /models+/check、model-picker、thinking-orbs | 无 |
| 测试 | 148 例（含 test_filesearch_e2e） | ~60 个 pytest 文件（pageindex_v2 20 + v3 33 + knowledge 4 + http 2 + …）+ Node 4 例 |
| 打包 | yuulibrary-desktop.spec（onedir，datas frontend+data） | 无 spec，run_app.py 带 --pageindex-v3-worker 分发 |

**关键发现**：
- 两项目共享大量字节级相同文件（retrieval.js、config-providers.js、katex/、核心 core 层、vendor 脚本、tests/retrieval/golden.json 等）→ 合并时直接保留 A 的副本即可。
- B 的 V3 是"发布/验证流水线"，不是可调库：supervisor 每任务拉起全新 worker 子进程（request.json → result.json + cancel.request），全内容寻址 + attestation 校验。
- B 的 segment_builder 与 A 的 vendor build_pageindex 是**两套不同的 markdown 解析/chunk 切分实现** → legacy JSON 若由 B 导出，内容将与 A 现有索引不同。**决策：构建任务同时跑两套**——A 的 legacy 构建服务 library/search 阅读（行为完全不变），B 的 V3 构建服务 /api/search + 聊天 + 知识链接。二者共存互不干扰。
- B 的 frontend/chat/agent.js 已收敛到 /api/search（3 工具）；A 的 agent.js 有 5 工具（search_library/search_issues/search_local_files/get_section/rewrite_query）+ 三组引用卡片 + thinking UI → **以 A 的 agent 为基底，把 search_library 切到 /api/search，保留 5 工具与全部 UI 质量件**。

## 目标目录结构（LQD-fin）

```
D:\1\LQD-fin\
├── app/
│   ├── config/         # A（与 B 字节相同，保留）
│   ├── http/           # A 全部 13 router + B routes_links.py；改造 routes_search/status/index
│   ├── storage/        # A + B link_index_io.py
│   ├── index/
│   │   ├── builder.py  # A 保留（legacy 构建入口）
│   │   ├── status.py   # 改造：双轨构建（legacy + V3 supervisor + publish + links）
│   │   ├── v2/         # B 全量 22 模块（segment_builder/object_store/canonical/…）
│   │   └── v3/         # B 全量 27 模块（layer_codec/view_store/reader/runtime/worker/supervisor/…）
│   ├── knowledge/      # B 全量 12 模块（models/wikilinks/frontmatter/catalog/resolver/indexer/queries/build_hook/export/migration/features）
│   ├── retrieval/      # A 8 模块 + B search_view.py（V3 原生检索）
│   ├── fileindex/      # A（本机文件检索，独有保留）
│   ├── fileparse/      # A（docx/pptx/xlsx/txt 解析，独有保留）
│   ├── ingest/  pdf/   # A（与 B 基本同构）
│   ├── llm/            # A（含 /models+/check 增强）
│   ├── vendor/         # A 全量（legacy 构建 + 清洗/翻译/校验）
│   ├── main.py         # A（frameless 窗口 + WebView2 缓存清理 + DesktopApi）
│   ├── pageindex_worker.py / pageindex_v3_worker.py   # B（新增）
├── frontend/           # A 布局 + B 知识链接模块
│   ├── chat/           # A 全量；agent.js 改造：search_library → /api/search（5 工具保留）
│   ├── library/        # A library.js + B knowledge.js/local-graph.js/reader.js/session.js/knowledge.css
│   ├── shared/         # A + B knowledge-flags.js/wikilinks.js/link-popover.js
│   ├── core/           # A + B tab-ids.js
│   ├── vendor/         # B d3.v7.9.0.min.js（新增）
│   ├── issues/ filesearch/  # A（独有保留）
│   └── index.html      # A 为基础 + B 知识链接脚本序
├── tests/              # A 全量 + B 全量（去重字节相同文件）
├── docs/               # A + B（architecture 重写为合并版）
├── data/               # 从 A 复制（content/pageindex/config/pdfs/fileindex）
├── pyproject.toml      # A（含 openpyxl）
├── yuulibrary-desktop.spec  # A + worker hiddenimports
├── run_app.py          # B 版（--pageindex-v3-worker 分发）
└── README.md / CLAUDE.md  # 合并说明
```

## 迭代阶段

### P0 — 脚手架与基线（约 1 天）

1. 复制 A → `D:\1\LQD-fin`（含 data/，git init）。
2. 复制 B 的 `app/index/v2/`、`app/index/v3/`、`app/knowledge/`、`app/retrieval/search_view.py`、`app/storage/link_index_io.py`、`app/http/routes_links.py`、`app/pageindex_worker.py`、`app/pageindex_v3_worker.py`、`run_app.py`（B 版）。
3. `pyproject.toml`：A 的依赖 + B 的 `httpx[socks]`；`yuulibrary-desktop.spec`：hiddenimports 追加 `app.index.v2` / `app.index.v3` 全子模块（worker 子进程冷启动依赖）。
4. **验收**：`python -m pytest tests/ -v` → A 的 148 例全绿（此时新增模块未接线，不应影响现有行为）。

### P1 — V3 引擎后端接线（约 3–4 天）

1. **app/index/status.py 改造为双轨构建**：`start_build` 后台线程依次执行
   - ① legacy：A 的 `build_full/build_incremental`（原样，产出现有 4 个 JSON → library/search 兼容面不变）
   - ② V3：`app.index.v3.supervisor.run_build(content_dir, pageindex_dir, "incremental"|bootstrap)` → `publish_current` → `finish_with_links`
   - 状态合并：V3 失败而 legacy 成功 → `done`（降级标记）；V3 成功 → `done` + 记录 generation/view_id；两者皆败 → `failed`
2. **app/http/routes_status.py**：`index_ready` 改为 `current-v3.json` 存在 && legacy JSON 存在（任一即 true，双轨就绪语义）；新增 `index_version/v3_generation/v3_view_id` 字段。
3. **app/http/routes_search.py**：V3 优先（`open_current_view` + `search_pinned_view`，返回 `generation/view_id/doc_key/doc_uid/segment_hash/source_md/line_num/line_end`），current-v3.json 缺失时回退到 A 现有 legacy Python 检索（兼容降级，不 503）。
4. **run_app.py** 采用 B 版 worker 分发；`app/main.py` 保留 A 版（frameless + 缓存清理 + DesktopApi），不改。
5. **验收**：构建 job 完成后 `data/pageindex/current-v3.json` 存在且含 generation/view；`/api/status` 返回 v3 字段；`/api/search` 返回带稳定引用字段的命中；legacy 4 文件仍在，library/search 页行为不变。

### P2 — 知识链接 API + 前端（约 3 天）

1. **后端**：`routes_links.py` + `storage/link_index_io.py` + `app/knowledge/` 已在 P0 复制，P2 接线：V3 发布成功后 `build_link_index` → `data/pageindex/link-index.json`；`/api/links/features|resolve|backlinks|neighborhood|preview|diagnostics` 可用。
2. **前端脚本序**（index.html，以 A 为基底）：
   - shared 层插入 `knowledge-flags.js → wikilinks.js → link-popover.js`（render.js 之前，LQD_FEATURES 先行）
   - library 层插入 `vendor/d3.v7.9.0.min.js → knowledge.css → local-graph.js → knowledge.js`
   - core 层插入 `tab-ids.js`（tabs.js 之前）
   - **保留**：highlight.js、thinking-orbs、model-picker、retrieval.js（search tab 用）、issues、filesearch
3. **render.js**：`md()` 加 `LQD_FEATURES.wikilinks_enabled` 守卫的 `LqdWikilinks.preprocess`。
4. **library**：A 的 library.js 阅读页集成反链（`LqdKnowledge.renderReader`）+ overview 集成局部图谱（`renderOverview`），A 的 issues 浏览保持。
5. **验收**：`build_link_index` 幂等重建；反链/邻域/preview API 返回正确；阅读页显示反向链接 + 局部图谱；wikilink hover 弹出预览。

### P3 — 聊天收敛 /api/search（约 2 天）

1. **frontend/chat/agent.js**（以 A 版为基底）：
   - `loadIndexes()` 不再下载 inverted/chunks 大 JSON（变轻量 no-op 或仅加载 global-index 供 TOC）
   - `search_library` 工具实现切为 `GET /api/search?q=…&limit=12`（异步），按后端顺序组装上下文（沿用 A 的 `retrieveContextAsText` / `packWithContextBudget` / `truncateAtBoundary` / 三组引用卡片）
   - **保留全部 5 工具**：search_library、search_issues、search_local_files、get_section、rewrite_query
   - `get_section` 改用 API 返回的 `source_md/line_num/line_end` 直接取章（保留 A 的 doc-tree 兜底）
   - **保留**：thinking orbs、tool trail 进度、llmRerank 兜底、强制三方检索、引用卡片分类（doc/本机文件/问题单）
2. **frontend/search/index.js**（全局搜索 tab）不动——仍读 legacy（A 行为）。
3. **验收**：聊天首轮无 26MB 索引下载（网络面板无 *.json 大文件）；5 工具全部可用；golden 对拍（`tests/retrieval/`）回归通过；回答质量与现版相当。

### P4 — 测试全量移植 + 打包 + 文档（约 3 天）

1. **测试**：复制 B 的 `tests/pageindex_v2/`、`tests/pageindex_v3/`、`tests/knowledge/`、`tests/http/`、`tests/frontend/`（Node 4 例）、`tests/retrieval/test_hit_identity.py`、`tests/links_api.py`、`tests/frontend_chat.py`、`tests/frontend_knowledge.py`、`tests/performance/`、`tests/ui/`。与 A 字节相同的文件不重复。适配：
   - `tests/http/test_search_v3.py` / `test_status_v3.py` → 合并后的降级语义（V3 优先 + legacy 回退）
   - `tests/http/test_http_api.py` → 合并 A（issues/filesearch）+ B（links）端点断言
   - Node 测试跑法：`node --test tests/frontend/*.test.js`（新增到文档）
2. **全量回归**：预期 ~1200+ pytest 用例全绿 + Node 4 例全绿。
3. **打包**：`pyinstaller yuulibrary-desktop.spec` → exe 启动后验证：窗口、/api/status、V3 构建、聊天（V3 检索 + 5 工具）、知识链接、issues、filesearch、worker 子进程分发（`--pageindex-v3-worker` 冷启动路径）。
4. **文档**：`docs/architecture.md` 重写为合并版（双轨检索边界：legacy 供阅读/搜索、V3 供聊天/检索/链接）；README/CLAUDE.md 更新目录结构、构建/测试命令、已知限制。

## 关键风险与缓解

| 风险 | 缓解 |
|---|---|
| B 的 segment_builder 与 A 的 build_pageindex 切分不同，V3 命中的 legacy_node_id/source_md 与 A 阅读页可能对不齐 | 聊天 get_section 用 V3 返回的 source_md+行区间（自洽）；library 阅读页仍走 legacy（自洽）。两轨互不交叉 |
| 双轨构建耗时翻倍 | legacy 与 V3 并行线程执行；V3 no-op 快路 <500ms 后增量成本极低 |
| V3 子进程 worker 在 PyInstaller 打包下冷启动失败 | run_app.py 分发 + spec hiddenimports 全量收集 app.index.v2/v3；P4 打包阶段专门验证 |
| 全量测试移植后 B 的 conftest/sample_content 与 A 的 data/ 冲突 | B 的测试全部基于 tmp_path fixture（勘察确认），无真实 data 依赖；仅 http 测试适配合并语义 |
| 前端脚本序合并导致注册时序问题 | 以 A 的 index.html 为基底，B 的 knowledge 脚本按 B 的依赖序（flags→wikilinks→popover；d3→local-graph→knowledge）插入；P2 逐步验收 |

## 验证方式

1. 每阶段结束跑 `python -m pytest tests/ -v`（P4 前至少 A 基线 148 例全绿）。
2. P1/P2 用 `python -m app.main` 实际启动：构建索引（观察双轨 job）、/api/status、/api/search（curl 验证 generation/view_id 字段）、/api/links/*。
3. P3 用聊天实测：首轮无大文件下载、5 工具、三组引用、KaTeX。
4. P4 `pyinstaller yuulibrary-desktop.spec` + exe 冒烟（含 worker 子进程路径）。
5. 全量 pytest + Node 测试收尾。
