# 开发指南

> 架构见 [architecture.md](architecture.md),部署/打包见 [deployment.md](deployment.md)。

## 环境要求

- Python 3.10+(与 `build_pageindex.py` 的 PEP 585/604 语法一致,3.13 已验证)
- Windows 10+/macOS/Linux(pywebview 跨平台,Win 用 WebView2,macOS 用 WebKit,Linux 用 GTK WebKit)
- pandoc(可选,EPUB 提取用)
- MinerU CLI(可选,高质量 PDF 提取用)

## 安装

```bash
cd e:\知识库\yuulibrary-desktop
pip install -e .            # 核心依赖
pip install -e ".[dev]"    # 含测试依赖(pytest)
```

核心依赖:fastapi、uvicorn、pydantic、pyyaml、pywebview、PyMuPDF、pdfplumber、httpx、keyring、python-multipart。

## 启动

```bash
python -m app.main
```

窗口打开后默认 Chat 标签。首次问答前需在 Manage → 设置里配 BYOK API key。

开发期可不用 pywebview,直接浏览器访问 `http://127.0.0.1:8765/frontend/index.html`(main.py 检测到 pywebview 未装会自动降级到浏览器模式)。

## 项目结构

```
yuulibrary-desktop/
├── app/                  # Python 后端(多模块,零耦合)
│   ├── main.py           # 入口
│   ├── config/           # 配置管理(schema/store/defaults)
│   ├── http/             # FastAPI 服务层(server/routes_*/schemas)
│   ├── storage/          # 文件 IO(paths/content_io/pageindex_io)
│   ├── llm/              # LLM 配置 + 代理(providers/config/proxy)
│   ├── index/            # 索引构建(builder/status)
│   ├── ingest/           # 入库流水线(pipeline/jobs/*_adapter)
│   ├── pdf/              # PDF 提取(base/local/mineru/epub/factory)
│   ├── retrieval/        # Python 检索重写(tokenizer/bm25/search/fuse/rerank/confidence/benchmark)
│   └── vendor/           # 从 yuulibrary-main 复制的脚本
├── frontend/             # 前端资源(WebView 加载)
│   ├── index.html        # 桌面壳(变量注入 + 三标签)
│   ├── chat/             # 从 yuulibrary-main/static/chat/ 复制
│   ├── library/          # 文档浏览
│   ├── manage/           # 入库+索引+设置
│   ├── shared/           # 共享 markdown 渲染
│   ├── katex/            # 数学渲染
│   └── assets/           # 图标/字体
├── data/                 # 用户数据(gitignore)
│   ├── content/          # markdown 文档
│   ├── pageindex/        # 索引产物
│   ├── config/           # app.yaml + llm.yaml
│   └── pdfs/             # PDF 原档
├── tests/                # 测试
│   └── retrieval/        # golden benchmark + Python 对拍
├── docs/                 # 文档
├── pyproject.toml
└── README.md
```

## 常用命令

```bash
# 启动应用
python -m app.main

# 索引构建(命令行,不走 HTTP)
python -c "
from app.index.builder import build_full
from app.config.store import load_app_config
from dataclasses import replace
import os
data = os.path.abspath('data')
cfg = load_app_config(os.path.join(data,'config'))
cfg = replace(cfg, content_dir=os.path.join(data,'content'), pageindex_dir=os.path.join(data,'pageindex'))
r = build_full(cfg.content_dir, cfg.pageindex_dir)
print(r.ok, r.docs_built, 'docs', r.duration_sec, 's')
"

# 跑检索 benchmark(Python 对拍)
python -m app.retrieval.benchmark

# 跑 JS harness(baseline)
node tests/retrieval/harness.js

# 跑测试
pytest tests/

# 单个测试文件
pytest tests/retrieval/test_py_retrieval.py -v
```

## 添加新文档(手动入库)

### 书籍

1. 在 `data/content/books/<slug>/` 下放 markdown(`_index.md` 封面 + `ch01.md` 起)
2. frontmatter 必填:`title`/`description`/`author`/`date`/`tags`/`category`
3. 标题层级:`#` 章 → `##` 节 → `###` 子节
4. 图片放 `images/` 子目录,用相对路径 `images/xxx.webp`
5. Manage → 索引构建 → 增量构建

### 论文

1. `data/content/papers/<slug>/_index.md`(必须 `_index.md`)
2. frontmatter 全齐:`title`(中文)/`description`/`date`/`author`/`year`/`category`(数组,不含点)/`tags`/`links`/`weight`
3. 图片放 `images/`

### 笔记

1. `data/content/notes/<slug>.md`(扁平,无子目录)
2. frontmatter:`title`/`description`/`date`/`author`/`source_type`/`source_title`/`tags`/`weight`

## PDF 入库流水线(阶段 4-5)

Manage 标签 → 选 PDF + doc_type(book/paper) + slug + 策略(local/mineru):

1. **extract**:PDF → `merged/book.md` + `images/`(本地库或 MinerU API)
2. **clean**:`clean_markdown.py` 清洗(LaTeX 修复 + 标题层级)
3. **translate**:`translate_chapters.py` 翻译(英文书必翻,种子章建术语表 + 并发 + validate 重试)
4. **validate**:`validate_book.py` 38 项机械验证
5. **note**(论文):`generate_paper_note.py` ReAct 7 栏目结构化分析
6. **index**:自动触发增量构建

## 从原项目(yuulibrary-main)同步更新

当原项目更新了检索算法或索引构建逻辑,需要同步到桌面应用:

### retrieval.js / chat.css

直接覆盖(完全不动):

```bash
cp e:/知识库/yuulibrary-main/static/chat/retrieval.js frontend/chat/retrieval.js
cp e:/知识库/yuulibrary-main/static/chat/chat.css frontend/chat/chat.css
```

### chat.js

**不能直接覆盖**(桌面应用改了 Settings)。同步流程:

1. 复制原项目 chat.js 到临时位置
2. 把桌面应用的 Settings 改造(见 `frontend/chat/chat.js` L233-295)应用到新版本
3. 同步 handleSend/loadSettingsForm/saveSettings/clearApiKey 的异步改造
4. 跑检索 benchmark 验证行为一致

### build_pageindex.py

`app/vendor/build_pageindex.py` 是复制 + 路径参数化版本。同步时:

1. 对比原项目 `scripts/build_pageindex.py` 的改动
2. 应用到 vendor 版本,**保留** `build(content_dir, pageindex_dir, llm_model, mode)` 函数
3. 跑 `build_full` + `build_incremental` 验证

### 入库脚本

`app/vendor/` 下的 clean_markdown/translate_chapters/validate_book 等可直接覆盖(零改造或仅 llm_config.py 改造)。

## 编码规范

### Python

- PEP 8 + type annotations
- frozen dataclass(不可变,见 `app/config/schema.py`)
- 函数 < 50 行,文件 < 800 行
- 不用 `print`(用 logging 或返回值)
- 路径用 `pathlib`
- 错误显式处理,不静默吞掉

### 前端

- 零依赖优先(可用 CDN,不引入 npm)
- chat.js/retrieval.js/chat.css **不动**(原项目复用)
- 新增 JS(library/manage)用 ES2020+,无 build step
- 样式用 `--app-*` CSS 变量(见 index.html),与桌面壳一致

### 模块耦合

- 依赖单向向下,无循环(见架构图)
- `vendor/` 是叶子,只被 adapter 调用
- `http/` 调用各业务模块,业务模块不反向依赖 `http/`
- `retrieval/` 独立于 `http/`(对拍工具,不参与运行时)

## 测试

```bash
pytest tests/ -v                # 全部
pytest tests/retrieval/ -v      # 检索对拍
pytest --cov=app tests/         # 覆盖率
```

当前测试状态(2026-07-27):

- **148 个 pytest 用例,144 passed + 4 skipped**(4.15s)
  - `tests/retrieval/test_py_retrieval.py` — 54 用例(检索对拍,全通过)
  - `tests/test_http_api.py` — 37 用例(HTTP 端点集成,全通过)
  - `tests/test_pdf.py` — 26 通过 + 3 skip(无 PyMuPDF/Pillow 时正确跳过)
  - `tests/test_ingest.py` — 27 通过 + 1 skip(端到端 done 需 PyMuPDF,skip)
- **golden benchmark 跑通**:`python -m app.retrieval.benchmark` 跑 `tests/retrieval/golden.json` 148 题,overall recall 30%,与 JS harness 基线对齐(失败案例与 JS 版完全一致,差异 <2%)

测试覆盖:
- **检索对拍**:tokenizer(中文 2-gram/英文/数字/边界)、BM25(IDF 非负/字段加权/权重)、chunk stats(per-chunk DF 去重)、search(召回/per-doc 截断/positions)、search_inverted(cidMap 缓存)、RRF(key=doc:node/3 位小数)、RM3(原始优先/top15)、rerank+MMR(4-gram/空集/贪心)、confidence(短路顺序/阈值)、benchmark 集成
- **HTTP API**:根重定向/前端静态/openapi、`/raw/content`(读+404+路径遍历 403)、`/pageindex`(读+404+403)、`/api/settings`(GET 形状/不泄露 key/PUT active_provider/use_llm_proxy/model/api_key/校验)、`/api/content/*`(docs 归一化/read/section+路径遍历)、`/api/index/build`(job_id/校验/轮询/404/list)、`/api/ingest/*`(extract job_id/校验/轮询/404/list/失败)
- **PDF 提取**:factory 路由(扩展名+策略+大小写+Path 对象)、`_parse_pages`(单页/范围/列表/混合/去重排序/裁剪/越界丢弃)、`_to_webp_bytes`(无效字节回退/扩展名去点/PNG→webp)、三后端降级(无 fitz/无 key/无 pandoc)、真实提取(integration,有 fitz 时跑)
- **入库流水线**:`_default_stages`(book/paper/note/unknown)、create/get/list jobs、update/append_log、cleanup_done(保留最近 N)、extract/clean/validate/note adapter(缺失文件抛错/skipped 不阻断)、pipeline(未知 stage/missing job/失败路径)

测试类型:
- `test_py_retrieval.py` — Python 检索重写对拍 ✅ 已实现(54 用例)
- `test_http_api.py` — HTTP 端点集成测试 ✅ 已实现(37 用例)
- `test_ingest.py` — 入库流水线 ✅ 已实现(27 用例 + 1 skip)
- `test_pdf.py` — PDF 提取双后端 ✅ 已实现(26 用例 + 3 skip)

## 调试

### HTTP 服务日志

`python -m app.main` 启动后,uvicorn 日志打印到 stderr,显示每个请求:

```
INFO: 127.0.0.1:59652 - "GET /pageindex/global-index.json HTTP/1.1" 200 OK
INFO: 127.0.0.1:59656 - "GET /api/settings HTTP/1.1" 200 OK
```

### 前端调试

pywebview 窗口右键 → Inspect(WebView2 开发者工具),可看 console.log + network。

### 索引构建调试

```bash
python -c "
from app.index.builder import build_full
r = build_full('data/content', 'data/pageindex')
print('ok:', r.ok)
print('docs:', r.docs_built)
print('log:', r.log[-10:])  # 最后 10 行
if r.error: print('error:', r.error)
"
```

### BYOK key 调试

```bash
python -c "
from app.config.store import get_api_key, has_keyring
print('keyring:', has_keyring())
print('deepseek key:', get_api_key('deepseek', 'data/config')[:10] + '...')
"
```

## 常见问题

### Q: chunks.json/inverted-index.json 不存在?

A: 首次启动时这两个文件没构建(原项目 CI 生成不进 git)。前端会降级到线性 BM25(node-index)。去 Manage → 索引构建 → 全量构建 生成。

### Q: Anthropic 直连报 CORS 错?

A: Anthropic 浏览器直连需 `anthropic-dangerous-direct-browser-access` header(chat.js 已加),但 WebView2 可能仍受限。启用阶段 6 LLM 代理(设置 → 使用代理)。

### Q: 中文终端打印报 GBK 错?

A: Windows 终端默认 GBK,打印中文会 `UnicodeEncodeError`。在脚本开头加:

```python
import sys
sys.stdout.reconfigure(encoding='utf-8')
```

或用 ASCII 输出。

### Q: pywebview 窗口打不开?

A: 检查 `pip install pywebview` 是否成功。Windows 需要 WebView2 Runtime(Win10 自带)。无 pywebview 时 main.py 自动降级到浏览器模式。

## Ingest remediation verification (2026-08-04)

Run the focused gates before the full suite:

```powershell
python -m pytest tests/test_ingest_upload.py tests/test_ingest_policy.py tests/test_text_normalization.py tests/test_pdf.py tests/test_ingest.py tests/test_clean_pseudo_headings.py tests/pageindex_v3/test_library_projection.py -q
node --test tests/frontend/upload-queue.test.js
node --check frontend/upload/upload.js
node --check frontend/library/reader.js
node --check frontend/chat/index.js
```

Then run the repository gates:

```powershell
python -m pytest -q
node --test tests/frontend/*.test.js
```

On restricted Windows sandboxes, Node's test runner may fail with `spawn EPERM` before executing assertions. Re-run the Node command outside that restricted token; `node --check` alone is only a syntax gate.

For manual desktop acceptance, verify both a browser-selected file (multipart) and a native-picker absolute path. Confirm PDF and EPUB capabilities, offline policy rejection, failed-item retry, V3 publish completion, Library pin propagation, and chat composer visibility at 1000x600, 1387x762, and 1400x900.
