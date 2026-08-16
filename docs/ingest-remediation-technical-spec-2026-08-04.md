# 文件导入、V3 文档库与桌面可用性缺陷修复技术方案

> 状态：Draft，待实现评审
> 日期：2026-08-04
> 适用基线：`dev` / `be33a54601282bfe26aa771f6ad7c1f4a3393774`
> 面向读者：项目维护者、实现工程师、测试人员
> 问题证据：[文件导入端到端测试记录](./ingest-e2e-test-2026-08-04.md)

## 1. 结论

本方案覆盖 QA 记录中的 8 个缺陷，目标是在不引入数据库、常驻远程服务或前端整套索引下载的前提下，修通以下主链路：

```text
原生选择 / 拖放 / 浏览器选择
  → 真实文件传输或受控本地路径
  → 同步预检
  → extract → clean → translate → validate → publish
  → V3 增量构建并发布 Generation/View
  → 搜索、文档库、聊天使用同一 V3 事实来源
```

核心技术决策如下：

| 决策 | 结论 |
|---|---|
| 文件输入 | 浏览器 `File` 使用流式 multipart 上传；桌面绝对路径保留一个版本兼容，但必须在创建任务前校验。禁止再把 `file.name` 当成服务器路径。 |
| EPUB | 复用现有 PyMuPDF 提供轻量兼容提取，Pandoc 作为可选高保真引擎，不成为基础安装的强制依赖。 |
| 文档库 | `/api/content/*` 改为读取当前已发布 V3；不恢复 Legacy Export，也不在异常时静默回退旧 JSON。 |
| 离线语义 | 将“提取引擎”和“是否允许联网/LLM”拆开；`offline` 必须能够由测试证明零外网调用。 |
| 文本质量 | 先关闭 PyMuPDF 连字保留并做保守、幂等的 Unicode 规范化；不把所有异常笼统归为 UTF-8 乱码。 |
| 队列 | 继续使用进程内轻量队列，补齐重试、分类清理和内存上限；本轮不增加数据库或跨重启持久队列。 |
| 聊天布局 | 当前浏览器端未复现原问题，先补 Flex/Grid 高度收缩契约和桌面几何回归；不把未经复现的 CSS 假设写成既定根因。 |

## 2. 目标、非目标与约束

### 2.1 目标

- 拖放、浏览器文件框和 Windows 原生选择器都能完成真实导入。
- 能在创建异步任务前发现源文件、格式、slug、提取引擎和网络策略错误。
- 无 Pandoc 的基础安装仍能导入普通 EPUB；有 Pandoc 时自动使用高保真路径。
- 新增、修改、删除文档发布 V3 后，搜索、文档库和聊天看到相同的文档集合。
- 用户选择“完全离线”后，不发生任何非 loopback 网络请求。
- 修复英文连字对阅读和检索的影响，同时保护中文、公式和数学符号。
- 失败任务可单条或批量重试，完成项和失败项可分别清理。
- 聊天输入框在支持的窗口、缩放和内容状态下始终可见、可聚焦、可发送。

### 2.2 非目标

- 不引入关系数据库、向量数据库、消息队列或独立后端服务。
- 不实现断点续传、跨设备同步或跨重启的持久任务队列。
- 不重新启用 `global-index.json` 或分类型 Legacy JSON 作为运行时数据源。
- 不把 V3 Segment 或倒排索引下载到前端。
- 不在本轮重写阅读器、聊天 Agent 或整个入库流水线。
- DOCX 当前没有真实提取器；在实现专用提取器前应标记为不可用，而不是继续作为“已支持”格式展示。

### 2.3 必须保持的不变量

- 所有用户提供的文件名、slug、路径和节点参数都需要服务端校验。
- 上传采用分块写入和原子完成，不能把大文件一次性读入内存。
- 应用只可清理自己创建的上传暂存文件，绝不能删除用户原文件。
- `offline` 分支从函数入口就禁止 LLM，不能仅靠“没有配置 API Key”间接降级。
- V3 不可用时返回明确错误；禁止自动读取旧索引掩盖故障。
- 章节正文必须与索引中的源文件指纹一致，避免目录属于旧版本、正文属于新版本。
- 所有自动化测试使用 `tmp_path` 或独立测试目录，不能改写用户正式知识库。

## 3. 根因和修复总览

| ID | 已确认根因或状态 | 主要改动 | 优先级 |
|---|---|---|---:|
| BUG-ING-001 | `File` 只保存在前端内存，提交时退化为 `file.name`；后端只收 JSON | 流式 multipart 上传、同步预检、统一旧管理入口 | P1 |
| BUG-ING-002 | pywebview 的首个过滤器只有 PDF | 能力驱动的组合过滤器；DOCX 未实现时不展示 | P2 |
| BUG-ING-003 | Pandoc 只在异步 extract 内检查 | PyMuPDF EPUB 保底、Pandoc 可选增强、能力接口和前置检查 | P1 |
| BUG-ING-004 | 文档库仍读 Legacy JSON，而 V3 禁用了 Legacy Export | V3 文档投影、`LibraryV3Service`、pin 一致性、等待索引发布 | P1 |
| BUG-ING-005 | `local` 仅表示提取器，UI 却把它描述为整条流水线离线；clean 会尝试 LLM | `extract_strategy` 与 `network_policy` 解耦，clean 显式注入策略 | P2 |
| BUG-ING-006 | 已确认 `ﬁ/ﬂ` 来自 PyMuPDF 连字保留，`L¨u` 是字形顺序；`â` 未在产物中复现 | 提取 flags、保守规范化、质量诊断、分层编码验证 | P2 |
| BUG-ING-007 | 没有 retry 状态转换；`clearDone()` 实际同时删 done/failed；轮询日志 updater 失效 | 分类清理、重试状态机、批处理锁、后端终态任务裁剪 | P3 |
| BUG-ING-008 | 当前代码在 1387×762 浏览器测试中未复现；高度链仍有薄弱点 | 桌面复现取证、`min-height: 0` 加固、滚动归属和几何自动化 | P1 验证 |

## 4. 公共接口和错误契约

### 4.1 新增能力接口

```http
GET /api/ingest/capabilities
```

建议响应：

```json
{
  "formats": {
    "pdf": {
      "available": true,
      "engine": "pymupdf",
      "degraded": false,
      "extensions": [".pdf"]
    },
    "epub": {
      "available": true,
      "engine": "pymupdf",
      "preferred_engine": "pandoc",
      "preferred_engine_available": false,
      "degraded": true,
      "extensions": [".epub"],
      "message": "Pandoc 未安装，将使用本地兼容提取"
    },
    "docx": {
      "available": false,
      "engine": null,
      "degraded": false,
      "extensions": [".docx"],
      "message": "当前版本尚未提供 DOCX 提取器"
    }
  },
  "max_upload_bytes": 536870912
}
```

该接口是上传页 `accept`、桌面过滤器、格式提示和后端预检的共同能力来源。后端提取器工厂仍是最终裁决者，前端能力状态不能替代服务端验证。

### 4.2 新增真实上传接口

```http
POST /api/ingest/upload
Content-Type: multipart/form-data

file: <binary>
request: <IngestUploadRequest JSON string>
```

`IngestUploadRequest` 与 `IngestFullRequest` 的元数据字段一致，但没有 `input_pdf`：

```json
{
  "doc_type": "book",
  "slug": "the-power",
  "pages": null,
  "extract_strategy": "local",
  "network_policy": "offline",
  "stages": ["extract", "clean", "validate"],
  "title": null,
  "author": null,
  "tags": []
}
```

成功响应保持轻量：

```json
{
  "job_id": "ing_0123456789ab",
  "status": "running"
}
```

文件先分块写入 `data/pdfs/_uploads/<uuid>/<safe-name>.part`，完成大小、扩展名、签名和路径校验后原子改名，再创建 job。失败任务的暂存源文件保留到重试窗口到期；成功任务可移动到文档工作目录作为可复现源文件。清理器只遍历 `_uploads` 下由应用创建且超过 TTL 的终态文件。

现有 JSON `POST /api/ingest/full` 保留一个发布周期，用于桌面原生路径和脚本兼容，但必须同步预检。新前端不得再发送相对文件名占位。后续版本可让 pywebview bridge 直接调用同一上传暂存服务，再移除任意本地路径接口。

### 4.3 统一错误结构

入库和 V3 文档接口采用结构化错误：

```json
{
  "detail": {
    "code": "INGEST_DEPENDENCY_MISSING",
    "message": "没有可用的 EPUB 提取引擎",
    "field": "file",
    "retryable": false,
    "context": {"format": "epub", "dependency": "pandoc"}
  }
}
```

| 状态码 | 代码示例 | 语义 |
|---:|---|---|
| 400 | `INVALID_DOCUMENT_TYPE` | 查询或枚举值非法 |
| 409 | `SLUG_BUSY`、`VIEW_CHANGED`、`CONTENT_OUT_OF_SYNC` | 当前状态与请求冲突 |
| 413 | `UPLOAD_TOO_LARGE` | 超过配置的流式上传上限 |
| 415 | `UNSUPPORTED_FORMAT`、`SIGNATURE_MISMATCH` | 扩展名或文件格式不支持 |
| 422 | `SOURCE_NOT_FOUND`、`INVALID_SLUG`、`OFFLINE_POLICY_CONFLICT`、`INGEST_DEPENDENCY_MISSING` | 请求可以解析，但无法执行 |
| 503 | `V3_VIEW_UNAVAILABLE` | 当前 V3 指针、View 或 Segment 无法验证 |

只有完成同步预检后才能创建 job。同步失败时 `/api/ingest/jobs` 中不得出现新任务，也不得生成 slug 输出目录。

## 5. 分问题修复方案

### 5.1 BUG-ING-001：实现真实文件上传

#### 根因

- `frontend/upload/upload.js` 的拖放和浏览器输入只得到 `File`。
- `frontend/upload/upload-queue.js` 保存了 `File`，但没有上传字节。
- `processItem()` 使用 `item.path || item.name`，因此浏览器入口最终只提交文件名。
- `app/http/routes_ingest.py` 只接收 JSON，`app/ingest/extract_adapter.py` 将相对文件名拼到 `pdfs_dir`，产生 `FileNotFoundError`。
- `frontend/manage/manage.js` 的旧入库入口同样始终发送 `file.name`。

#### 实现

新增 `app/ingest/upload_store.py` 和 `app/ingest/preflight.py`：

1. `UploadStore` 以固定大小块写 `.part`，计算 SHA-256，并在成功后原子改名。
2. 文件名只取 basename，过滤控制字符、Windows 保留名、尾随点和尾随空格。
3. PDF 检查 `%PDF-`；EPUB 检查 ZIP 结构、`mimetype` 和基础容器文件；不能只信 MIME 或扩展名。
4. slug 只允许安全字符集合，拒绝 `/`、`\`、`.`、`..`、控制字符和越界后的 resolved path。
5. 同 slug 的并发导入加进程内锁并返回 409；禁止静默覆盖已发布文档。
6. `/api/ingest/upload` 通过 `UploadFile` 流式写入，项目已有 `python-multipart`，无需新增运行时包。
7. `frontend/upload/upload.js` 有 `item.file` 时使用 `FormData`；有受信任桌面路径时临时使用 JSON `/full`；两者都没有时直接失败。
8. `frontend/manage/manage.js` 删除重复入库表单或调用与上传页相同的提交模块，不能保留第二条错误链。

上传中断、超限或签名失败时删除当前 `.part`；不能删除用户原文件。源文件、工作产物和发布内容的清理分别建模，不能由一个“清空队列”按钮隐式联动。

#### 验收标准

- 拖放和浏览器选择不再把 `file.name` 发送为 `input_pdf`。
- F 盘、中文目录、空格目录和两个不同目录中的同名文件都可导入。
- 非法文件在创建 job 前失败；错误代码可由前端直接展示。
- 500 MB 上限内的大文件上传内存保持有界，不随文件大小线性增长。
- 全链路不再出现 `data\pdfs\<file.name>` 的 `FileNotFoundError`。

### 5.2 BUG-ING-002：修复原生文件过滤器

#### 根因

`app/main.py::DesktopApi.choose_files()` 把 PDF、EPUB、DOCX 配成三个独立过滤器，Windows 默认选择第一项，所以初始只显示 PDF。

#### 实现

由能力接口生成过滤器，并把组合项放第一位：

```python
[
    "Supported Documents (*.pdf;*.epub)",
    "PDF Files (*.pdf)",
    "EPUB Files (*.epub)",
]
```

后面保留单格式过滤器。只有 `available=true` 的格式进入组合项和 HTML `accept`。DOCX 当前只是 `factory.py` 中路由到 PyMuPDF 的占位实现，专用提取器完成前应从默认能力中移除并在 UI 显示“尚未支持”。文件过滤器只是便捷提示，服务端仍必须验证格式。

#### 验收标准

- 文件对话框打开后，所有当前真实可用格式默认同时可见。
- 单格式过滤、多选、大写扩展名、中文名、取消和异常降级工作正常。
- `choose_files(file_types=...)` 的自定义参数行为保持不变。
- UI 不再宣称支持没有可用提取器的 DOCX。

### 5.3 BUG-ING-003：EPUB 无 Pandoc 时仍可用，并增加预检

#### 根因

`app/pdf/epub.py` 只在后台 extract 已开始后执行 `shutil.which("pandoc")`；路由创建 job 前不检查能力，打包配置也没有携带 Pandoc。

#### 实现

采用“PyMuPDF 轻量保底 + Pandoc 可选增强”策略：

```text
Pandoc 可执行且版本探测成功 → PandocExtractor，高保真 Markdown/媒体转换
Pandoc 不可用              → EpubFitzExtractor，本机兼容提取
两者都不可用               → 创建 job 前返回 422
```

新增 `app/pdf/epub_fitz.py`，不能直接复用会返回 `source_format="pdf"` 的 `LocalExtractor`。它至少需要：

- 返回 `source_format="epub"`，读取 EPUB 标题和作者。
- 以重排页顺序输出正文，保留稳定的页面/章节边界标记。
- 导出能够安全取得的图片并改写相对引用。
- 对加密/DRM、损坏 ZIP、缺 OPF、空正文返回明确格式错误。
- 输出章节数、字符数、图片数和降级警告，便于与 Pandoc 质量比较。

抽出 `resolve_pandoc()`：检查显式配置路径、打包工具目录和 `PATH`，执行 `pandoc --version`，超时 3 秒。命令继续使用参数数组、`shell=False`。即使预检通过，extract 内仍保留运行时防御，处理依赖在任务开始后消失的竞态。

现有用户 EPUB 已由只读实验确认可被当前 PyMuPDF 打开并提取正文，因此该保底路径不需要增加强制运行时依赖。Pandoc 不打入基础包，以保持体积优势；发行版可以提供独立的可选高保真组件。

#### 验收标准

- 无系统 Pandoc 的干净机器上，普通 EPUB 能完成 extract、clean、validate、publish 和 V3 构建。
- 有 Pandoc 时使用高保真引擎，UI 明确显示当前引擎。
- 完全没有可用引擎时同步失败，不创建“先 running 后 failed”的任务。
- EPUB 格式错误和依赖错误能够区分。
- PDF 路径不受 EPUB 引擎选择改动影响。

### 5.4 BUG-ING-004：文档库完全收口到 V3

#### 根因

- `app/http/routes_content.py` 的 `/docs` 读取 `global-index.json`，`/read` 读取分类型 Legacy JSON。
- V3 构建固定关闭 Legacy Export，因此新导入文档只存在 V3。
- V3 Segment 已保存 `document`、`document_tree`、节点行号和源文件指纹，但 `PinnedSearchView` 尚未公开安全的文档投影读取方法。
- `app/knowledge/catalog.py` 会扫描实时 `data/content`，仅标题锚点来自 V3，不能作为与已发布 Generation 一致的 Library 数据源。
- 入库任务触发 V3 build 后立即结束，上传页没有继续等待 `build_job_id`，即使 API 修复也会出现短暂的“完成但不可浏览”。

#### 实现

1. 在 `app/index/v3/segment_projection.py` 增加不可变 `DocumentProjection` 和 `SegmentProjector.load_document(ref)`。
2. 在 `PinnedSearchView` 增加公开的 `get_document_projections(doc_uids, include_tree=...)`，HTTP 层禁止访问 `_refs_by_uid` 私有字段。
3. 投影严格校验 `owner/ref/document.doc_key`、类型、slug、节点唯一性、行号、`source_md` 和源文件指纹。
4. 新建 `app/library/v3_service.py`，每次请求固定当前 Generation/View；列表从活跃 owner 枚举，阅读只解码目标 Segment。
5. 保持一个以 `(generation, view_id)` 为键的不可变内存目录快照，并用 `segment_hash` 键控、默认 32 MB 的 LRU 缓存文档投影。P1 不新增持久目录索引。
6. `/api/content/docs`、`/read` 保留 URL 和旧主要字段，增加 `index_version`、`generation`、`view_id`、`doc_key`、`doc_uid`、`segment_hash`。
7. 列表按稳定 slug 排序。前端保存列表 pin，读取文档和章节时携带同一 pin。
8. pin 与当前已发布 View 不同则返回 409 `VIEW_CHANGED`；前端刷新列表并最多自动重试一次。
9. `/api/content/section` 改为接收 `type + slug + node_id + generation + view_id`，由服务端从 V3 树解析路径和行号，不再信任客户端任意文件路径。
10. 读正文前比较当前 Markdown SHA-256 与 Segment `fingerprint.source_files`。不一致时返回 409 `CONTENT_OUT_OF_SYNC`，提示增量构建。
11. 没有 V3 指针返回 409 `INDEX_NOT_READY`；指针、View 或 Segment 损坏返回 503，禁止读取旧 JSON。
12. 上传页在入库 job 完成后继续轮询 `result.build_job_id`；只有 V3 发布完成才显示“已入库，可浏览”，并发出 `index:published` 事件刷新文档库。

建议列表响应：

```json
{
  "index_version": "v3",
  "generation": "<sha256>",
  "view_id": "<sha256>",
  "type": "paper",
  "docs": [
    {
      "id": "qa-pdf-control-20260804",
      "type": "paper",
      "doc_key": "paper:qa-pdf-control-20260804",
      "doc_uid": "<sha256>",
      "segment_hash": "<sha256>",
      "title": "QA PDF Control",
      "author": "",
      "description": "",
      "tags": ["qa", "pdf"]
    }
  ]
}
```

旧 `source_md/line_num/line_end` 章节参数保留一个版本并记录弃用日志。旧 JSON 可保留一个发布周期用于代码回滚，但不参与新版本运行；不得加入自动 fallback 或新的 `library_backend` 双源开关。

#### 验收标准

- 新导入的 `paper:qa-pdf-control-20260804` 同时出现在 `/api/search` 和论文书架。
- 新增、修改、删除经过增量发布后，搜索和 Library 文档集合一致。
- 列表、阅读、章节响应使用相同 Generation/View。
- 删除或故意篡改 `global-index.json` 不影响 Library。
- V3 损坏返回 503，而不是显示陈旧内容。
- 前端不会下载 Segment、倒排表或完整 catalog。

### 5.5 BUG-ING-005：把“本地提取”和“完全离线”拆开

#### 根因

`strategy=local` 只在 extract 阶段选择 PyMuPDF，UI 却标注为“本地，离线”。`clean_adapter.py` 无条件注入 LLM 配置，`fix_pseudo_headings()` 有候选时会尝试 LLM；上传页默认阶段还包含 translate。因此当前产品把提取位置和整条流水线网络策略混成了一个概念。

#### 实现

请求模型改为两个正交字段：

```json
{
  "extract_strategy": "local",
  "network_policy": "offline"
}
```

- `extract_strategy`: `local | mineru`，UI 标签为“PDF 提取引擎：PyMuPDF 本机 / MinerU 云端”。
- `network_policy`: `offline | allow_ai`，UI 标签为“处理模式：完全离线 / AI 增强”。
- `offline + mineru`、`offline + translate` 以及其他明确需要联网的阶段在同步预检返回 422 `OFFLINE_POLICY_CONFLICT`。
- 新 UI 默认 `offline`，只选择离线阶段；AI 增强必须由用户显式开启并提示可能的费用和数据发送。

清洗 API 改为显式依赖注入：

```python
clean(content, *, heading_mode="regex", classifier=None)
fix_pseudo_headings(text, *, allow_llm=False, classifier=None)
```

offline 分支不加载 LLM 配置，也不调用分类器。不能只停止 `_inject_llm_config()`，因为 `vendor.llm_config` 有进程级 active config，可能残留上一个任务状态。`run_clean` 和 `run_reclean` 都要接收相同策略；并发 offline/AI 任务不能相互污染。

统计和日志改为明确字段：

```json
{
  "classifier": "regex",
  "llm_attempted": false,
  "llm_succeeded": false,
  "llm_failed": false,
  "network_policy": "offline"
}
```

旧 `strategy` 暂时作为 `extract_strategy` alias。旧请求未传 `network_policy` 时，为保持行为兼容映射为 `allow_ai` 并写弃用警告；新桌面 UI 始终显式发送该字段。一个发布周期后移除隐式行为。

#### 验收标准

- `offline` 完整导入在网络哨兵下通过，除 `127.0.0.1/localhost` 外请求数为 0。
- offline clean 的 `llm_attempted=false`，即使进程中已经运行过 AI 任务。
- AI 模式能调用显式注入的 classifier，并记录成功、失败和 fallback。
- UI 不再把 `local` 描述为整条流程离线。
- 非法策略组合在创建 job 前返回 422。

### 5.6 BUG-ING-006：修复连字和字形质量，先定位再修编码

#### 缺陷纠偏

保留产物 `data/pdfs/qa-pdf-control-20260804/merged/book.md` 是有效 UTF-8；其中确认有 94 个 `ﬁ`、20 个 `ﬂ` 和 1 个 `L¨u`，但没有字面量 `â` 或 U+FFFD。当前 PyMuPDF 默认保留 PDF 字体连字；关闭 `TEXT_PRESERVE_LIGATURES` 后可直接提取 `efficiency`。因此：

- `ﬁ/ﬂ` 是提取和检索规范化问题，不是 UTF-8 写盘失败。
- `L¨u` 是 PDF 字形/重音顺序问题。
- `â` 更可能是 QA 终端或客户端显示解码问题，目前不能据此改写正文。

#### 实现

1. `app/pdf/local.py` 调用 `page.get_text("text", flags=fitz.TEXTFLAGS_TEXT & ~fitz.TEXT_PRESERVE_LIGATURES)`。
2. 新增 `app/text/normalization.py::normalize_extracted_text()`，只做保守、幂等规则：
   - 显式展开 U+FB00–U+FB06 连字。
   - 统一 NBSP，移除软连字符。
   - 对“拉丁字母 + spacing accent + 拉丁元音”做高置信组合，例如 `L¨u → Lü`。
   - 使用 NFC；禁止全局 NFKC，避免改变数学符号、兼容字符、全角字符和 CJK。
3. 增加 `text_stats` 和 warning：统计 replacement、控制字符、private-use、疑似 mojibake 和修复数。超过阈值只提示改用 MinerU/OCR，不做不可逆猜测。
4. `app/retrieval/tokenizer.py` 在索引和查询两侧共同调用 `normalize_for_search()`，使旧 `efﬁciency` 与 `efficiency` 等价。
5. 展示修复后的正文前保留源 PDF；调试构建可额外保留 raw extract，不覆盖用户源文件。
6. 对 `â` 做分层 code point 验证：Markdown → V3 chunk → `/api/search` 原始 UTF-8 bytes → `response.json()` → WebView `textContent`。若前三层正确，只修测试客户端或响应 charset，不添加盲目替换规则。

已存在文档需要重新提取才能改善展示正文；仅改 tokenizer 后全量重建 V3 可以先改善检索。没有原始 PDF 的旧文档不进行猜测性批量改写。

#### 验收标准

- 新导入样本正文使用 `efficiency`、`Lü`，UTF-8 重读后 code point 正确。
- 正确的 en dash、中文、希腊字母、LaTeX、公式和已规范 Unicode 不被修改。
- normalizer 连续运行两次结果相同。
- `efﬁciency` 和 `efficiency` 生成相同检索 token。
- 如果 `â` 只在 PowerShell 显示，应用正文和索引不做无依据替换。

### 5.7 BUG-ING-007：补齐失败队列的重试和分类清理

#### 根因纠偏

现有 `clearDone()` 并非“不能批量清失败”，而是名称和按钮写“清空已完成”，实现却同时删除 `done` 和 `failed`。真正缺陷还包括：没有 retry 状态转换、`startBatch()` 只处理 pending、轮询错误传入 updater function 但 `update()` 只支持对象、后端 `cleanup_done()` 从未调用。前端队列仅在内存中，刷新即丢失。

#### 实现

队列增加：

```javascript
clearByStatus(["done"])
clearByStatus(["failed"])
retry(id)
retryAllFailed()
counts()
```

失败项显示“重试 / 查看日志 / 删除”，批量区显示“重试失败（N）/ 清空失败（N）/ 清空已完成（N）”。重试状态转换为：

```text
failed → pending
清空 jobId 和当前 stage
attempt += 1
保留 File/path/meta
重新预检并创建新 job
```

`attemptHistory` 只保存 job ID、时间和错误摘要，设置固定上限，不复制完整日志。增加 `batchRunning` 锁，避免连续点击启动多组循环；`update()` 明确支持 updater function，或把轮询错误改成对象更新。

后端在创建任务前或终态后调用 `cleanup_done(max_keep=N)`，只裁剪最旧的 done/failed，绝不删除 running。清前端队列不取消后端任务、不删除源文件、不删除已发布内容。

本轮保持进程内队列，不引入 localStorage 或数据库。以后若要跨重启恢复，应先把上传源建模为可持久、可过期的 `source_id`，不能尝试序列化浏览器 `File`。

#### 验收标准

- 完成和失败可以分别清理，running 不会被误删。
- 单条和批量重试只作用于 failed，每次创建新 job ID。
- 临时轮询错误能追加日志并继续轮询，不会因 updater 失效丢失。
- 连续点击“全部开始”只有一个 batch loop。
- 累计 100+ 终态任务后，前后端内存受配置上限约束。

### 5.8 BUG-ING-008：桌面聊天 composer 复现与防御性加固

#### 缺陷纠偏

当前代码已使用纵向 flex、`height: 100%`、`overflow: hidden` 和 composer `flex-shrink: 0`。使用真实 Chromium 在 1387×762、1000×600、1110×610 和 1280×720 下只读测量时 composer 均在可视区。因此原报告不能直接证明当前版本 CSS 已损坏；可能因素包括旧进程/旧缓存、WebView2 特定状态、窗口缩放、标签切换遗留 scrollTop 或桌面 device scale。

#### 实现

先补齐可证明安全的 Flex/Grid 收缩契约：

```css
.lqd-main,
.lqd-main-body,
.lqd-chat,
.lqd-chat-empty,
.lqd-chat-messages {
  min-height: 0;
}

.lqd-chat-empty,
.lqd-chat-messages {
  overflow-y: auto;
}
```

- composer 继续 `flex-shrink: 0`，聊天消息/空态拥有滚动，输入框不随内容滚走。
- chat mount 时重置共享 `.lqd-main-body.scrollTop = 0`。
- 不全局把 `.lqd-main-body` 改为 `overflow: hidden`，除非资料库、管理页等每个 tab 都已接管自己的滚动。
- 如果 Windows pywebview 仍能复现，再只对 `.lqd-chat` 使用 `position: absolute; inset: 0; height: auto`，切断不确定的百分比高度链。
- 桌面启动必须验证加载的是当前进程和当前资源，记录 URL 时间戳、WebView2 版本、缩放、窗口 inner size 和关键元素 bounding rect。

#### 验收标准

- 所有支持矩阵下满足 `composer.top >= mainBody.top` 且 `composer.bottom <= mainBody.bottom`。
- textarea 和发送按钮可见、可 Tab 聚焦、可 Enter 发送。
- 空会话、长历史、流式回答和 150 px textarea 只滚动内容区，不移动 composer。
- 从长文档标签切回聊天后，旧 `main-body.scrollTop` 不影响布局。
- Windows pywebview/WebView2 实机测试通过；不能只用普通浏览器代替桌面验收。

## 6. 完整测试过程

### 6.1 测试入口条件

1. 从待测提交建立独立工作树或副本，记录：

   ```powershell
   git rev-parse HEAD
   git status --short
   python --version
   pandoc --version
   ```

   Pandoc 不存在时记录为预期环境状态，不因此中止测试。

2. 自动化全部使用 `tmp_path` 创建 `content/pageindex/config/pdfs`，不得复制或修改用户正式 V3 指针。
3. 仓库只提交程序生成的最小 PDF/EPUB fixture，不提交受版权保护书籍。
4. 桌面人工测试使用单独配置目录和唯一 slug，例如 `qa-e2e-<timestamp>`。
5. 分别准备：最小 PDF、最小 EPUB、损坏 EPUB、伪装扩展名、超限文件、中文/空格文件名、包含中英公式和连字的 PDF。

### 6.2 新增测试文件

| 文件 | 主要覆盖 |
|---|---|
| `tests/test_ingest_upload.py` | 流式上传、签名、大小、slug、原子文件、同步预检 |
| `tests/test_ingest_policy.py` | offline/AI、非法组合、并发隔离 |
| `tests/pageindex_v3/test_library_projection.py` | V3 文档投影、指纹、缓存和身份校验 |
| `tests/test_text_normalization.py` | 连字、重音、NFC、幂等和保护样本 |
| `tests/frontend/upload-queue.test.js` | 重试、分类清理、批处理锁、日志 updater |
| `tests/frontend/upload-flow.test.js` | FormData/JSON 分流、能力和错误展示 |
| `tests/frontend/library-v3.test.js` | pin 传播、409 单次重试、发布事件 |
| `tests/frontend/chat-layout.test.js` | composer 行为和几何断言 |

同时扩展现有 `tests/test_pdf.py`、`tests/test_ingest.py`、`tests/test_http_api.py`、`tests/frontend/library-session.test.js`。

### 6.3 单元测试

#### 上传与预检

- `safe_filename()` 覆盖中文、空格、大小写扩展名、`../../x.pdf`、空名、Windows 保留名。
- slug 覆盖 `/`、`\`、`.`、`..`、控制字符、尾随点/空格和 resolved-path 越界。
- 分块写入结果 SHA-256 与输入一致；同名并发上传不会覆盖。
- 空文件 422、超限 413、不支持格式和签名不符 415。
- 上传中断和校验失败后 `.part` 被删除。
- 同 slug 并发导入返回 409；同步失败不创建 job。

#### EPUB 能力

- `resolve_pandoc()` 覆盖不存在、有效、损坏、超时、中文/空格路径。
- Pandoc 可用时优先；不可用时选择 `EpubFitzExtractor`。
- 最小 EPUB 提取标题、作者、关键段落和图片；返回 `source_format="epub"`。
- 损坏 ZIP、缺 OPF、空正文和加密 EPUB 返回不同结构化错误。
- capabilities 与提取器工厂对可用格式的结论一致。

#### V3 Library

- 合法 Segment 投影完整元数据和递归目录树。
- owner、ref、`document.doc_key` 任一不一致时拒绝。
- 路径越界、重复节点、负行号、逆序行号、未知 doc UID 被拒绝。
- 返回对象与底层 Segment 解耦，调用方修改不会污染缓存。
- 内存快照按 pin 更新，LRU 按 `segment_hash` 命中和淘汰。
- monkeypatch Legacy `read_index()` 为直接失败，所有 V3 Library 测试仍通过。

#### 离线和文本质量

- clean 的候选/无候选 × offline/AI 成功/AI 失败完整组合。
- offline 时 classifier 被替换为“一旦调用就抛错”，断言调用次数为 0。
- adapter offline 不读取 LLM 配置；一条 offline 与一条 AI 并发无串扰。
- fake fitz page 断言 `get_text` 收到关闭 ligature 的 flags。
- normalizer golden：`ﬁ/ﬂ/L¨u/NBSP/soft hyphen`；断言 en dash、中文、希腊文、LaTeX 和公式不变。
- normalizer 幂等；`efﬁciency` 与 `efficiency` 的 token 相同。

#### 队列和布局

- `clearDone` 只删 done，`clearFailed` 只删 failed。
- `retry` 和 `retryAllFailed` 不影响 pending/running/done，保留源和元数据并增加 attempt。
- listeners 每次操作只通知一次；attempt history 有上限。
- updater function 能追加轮询错误；连续点击只启动一个 batch。
- composer 创建、自动增高、Enter 发送、Shift+Enter 换行和 disabled 状态正常。

### 6.4 HTTP 集成测试

使用 FastAPI `TestClient` 和真实临时目录：

1. multipart 上传最小 PDF/EPUB，确认后台读取的是落盘内容而不是文件名。
2. 恶意文件名不能在 `pdfs_dir` 外生成文件。
3. 旧 `/full` 绝对路径一个版本内可用；不存在路径同步返回错误。
4. offline 非法组合返回 422，且没有 job。
5. 在临时 content 中建立 book/paper/note，运行真实 V3 build 并 `publish_current()`。
6. 无 V3 指针返回 `INDEX_NOT_READY`。
7. 即使存在故意陈旧的 `global-index.json`，`/api/content/docs` 也只返回 V3 文档。
8. 新增、改名、删除文档并增量发布，`/api/search` 与 `/api/content/docs` 的集合和 pin 一致。
9. `/read` 返回 V3 元数据和目录；`/section` 按 node ID 返回正文。
10. 旧 pin 返回 `VIEW_CHANGED`；前端最多重试一次。
11. Markdown 修改但未重建时返回 `CONTENT_OUT_OF_SYNC`。
12. V3 指针、View 或 Segment 损坏返回 503，存在 Legacy 文件时也不得回退。
13. `/api/search` 原始响应 bytes 中 en dash 为 UTF-8 `e2 80 93`，JSON 解码后仍为 U+2013。
14. 第一次轮询失败、第二次成功时，重试创建新 job ID并最终完成。

### 6.5 前端自动化与真实浏览器布局测试

Node 单元测试覆盖 API payload 和状态机。布局测试使用真实 Chromium/WebView2 环境读取 `getBoundingClientRect()`，而不是只验证 CSS 文本。

矩阵：

| 维度 | 取值 |
|---|---|
| 窗口 | 1000×600、1280×720、1387×762、1400×900 |
| 缩放 | 100%、125%、150% |
| 面板 | 左右栏均开、均关、分别开关 |
| 会话 | 空会话、长历史、流式回答 |
| 输入框 | 单行、自动增长到 150 px |
| 切换 | 从长文档/管理页切回聊天，预置 `mainBody.scrollTop > 0` |

每个组合至少断言：

```text
composer.top >= mainBody.top
composer.bottom <= mainBody.bottom
textarea 与 send button 的 visibility != hidden
Tab 可聚焦 textarea/send
内容滚动后 composer 的 top/bottom 不变
document.body 不产生额外纵向滚动
```

Windows pywebview/WebView2 是发布阻断项；普通浏览器通过不能替代桌面测试。失败时保存页面截图、元素 rect、`innerWidth/innerHeight`、device scale、缩放、当前 URL 和 WebView2 版本。

### 6.6 入库端到端自动化

在临时目录中执行以下流程，不调用真实外部 LLM：

1. 通过 multipart 提交程序生成的小型 PDF，选择 `network_policy=offline`。
2. 轮询入库 job 到终态，断言 `llm_attempted=false`。
3. 从结果取得 `build_job_id`，继续轮询到 V3 发布完成。
4. 调 `/api/status` 记录 Generation/View。
5. 断言 `/api/search` 命中新文档。
6. 断言 `/api/content/docs?type=paper` 出现同一 slug 和相同 pin。
7. `/read` 打开文档，选择首节点；`/section` 返回关键字。
8. 放置故意陈旧的 Legacy JSON，重复步骤 5–7，结果必须不变。
9. 无 Pandoc 环境对最小 EPUB 重复 1–8，断言使用 PyMuPDF 兼容引擎。
10. 有 Pandoc 的可选 CI job 对同一 EPUB 重复 1–8，并比较关键段落顺序、章节数和图片数。
11. 模拟 V3 build 失败，队列状态必须是“文件已发布，索引失败”，不能显示“可浏览”。
12. 测试结束仅由 fixture 删除临时目录。

### 6.7 完整自动化执行命令

先跑高相关测试：

```powershell
python -m pytest tests/test_ingest_upload.py tests/test_ingest_policy.py tests/test_ingest.py tests/test_pdf.py tests/test_text_normalization.py -q
python -m pytest tests/pageindex_v3/test_library_projection.py tests/test_http_api.py -q
node --test tests/frontend/upload-queue.test.js tests/frontend/upload-flow.test.js tests/frontend/library-v3.test.js tests/frontend/library-session.test.js tests/frontend/chat-layout.test.js
```

再跑全部回归：

```powershell
python -m pytest -q
node --test tests/frontend/chat-search-api.test.js tests/frontend/library-session.test.js tests/frontend/tab-ids.test.js tests/frontend/wikilinks.test.js tests/frontend/upload-queue.test.js tests/frontend/upload-flow.test.js tests/frontend/library-v3.test.js tests/frontend/chat-layout.test.js
```

如果新测试文件尚未创建，命令应失败而不是静默跳过；实现 PR 必须同时提交代码和对应测试。

### 6.8 桌面端人工验收

使用真正的 pywebview 应用，不使用浏览器调试窗口：

1. 在未安装 Pandoc 的干净 Windows 环境启动应用，记录 capabilities。
2. 打开上传页，确认默认显示 PDF 和 EPUB，不显示未实现的 DOCX。
3. 依次用原生选择、拖放、浏览器降级导入 PDF/EPUB。
4. 文件分别放在 F 盘、中文目录和含空格目录；测试同名文件。
5. 选择完全离线，关闭网络，完成 PDF 和 EPUB 导入。
6. 观察状态依次为“处理中 → 索引中 → 已入库，可浏览”。
7. 不重启应用，新文档应自动出现在书架。
8. 打开目录和至少三个章节，再从搜索、知识链接和聊天引用打开同一文档。
9. 新增、改名、删除测试文档并增量构建，核对书架与搜索同步变化。
10. 构造 3 个成功、3 个失败、1 个运行中任务，验证单条/批量重试和分类清理。
11. 在布局矩阵下验证聊天输入框、长回答滚动和标签切换。
12. 重启应用，确认已发布内容和 V3 可读；进程内上传队列清空属于本轮设计预期。
13. 有 Pandoc 的机器再导入同一最小 EPUB，比较引擎和输出质量。
14. 最后用用户样本 EPUB/PDF 做非仓库验收，记录 job ID、日志、字符/章节/图片数、耗时、内存峰值和截图。

### 6.9 性能和轻量性门槛

- 上传为固定块流式处理；500 MB 文件的额外峰值 RSS 不超过 32 MB。
- 基础发行包不捆绑 Pandoc；除项目现有依赖外不新增重量级运行时或数据库。
- Library 热态列表不得重新解码所有 Segment；一次文档阅读最多加载目标文档 Segment。
- V3 Library 缓存受 32 MB 上限约束，发布新 pin 后旧快照不再服务新请求。
- 前端 Network 中不存在 `global-index.json`、分类型 Legacy JSON或整套 V3 索引下载。
- 离线 E2E 除 loopback 外网络请求为 0。
- 100 个队列项的批量状态操作和重绘无明显卡顿；100+ 后端终态 job 被上限裁剪。
- 性能测试记录冷/热耗时、RSS 和缓存命中；以后以首次合格数据作为回归基线，允许波动不超过 20%。

### 6.10 发布门禁和测试证据

只有同时满足以下条件才能合入：

- 新增和全量 Python/Node 测试全部通过，无针对 P1/P2 缺陷的 `xfail`。
- offline 网络哨兵、Legacy 禁读哨兵、V3 损坏 fail-closed 测试通过。
- Windows pywebview/WebView2 布局和导入人工验收通过。
- 无 Pandoc 和有 Pandoc 两条 EPUB 路径至少各有一份自动化或可复查证据。
- 测试报告记录 commit、环境、命令、退出码、job/build ID、Generation/View、耗时和截图路径。
- 正式用户 `data` 未被测试修改。

建议测试记录模板：

```text
Commit:
Windows / WebView2 / Python / PyMuPDF / Pandoc:
Config root:
Command and exit code:
Fixture and SHA-256:
Ingest job ID / build job ID:
Generation / view_id:
Expected / actual:
Duration / peak RSS:
Logs / screenshots:
Cleanup result:
```

## 7. 实施顺序

### 阶段 A：输入可靠性和同步预检

- 实现 multipart 上传、文件/slug 校验和结构化错误。
- 增加 capabilities、组合过滤器和 DOCX 能力纠正。
- 增加 EPUB PyMuPDF 保底、Pandoc resolver。
- 统一或下线 `manage.js` 旧入库入口。

完成门槛：BUG-ING-001/002/003 的单元、HTTP 和桌面入口测试通过。

### 阶段 B：V3 文档库闭环

- 实现 DocumentProjection、LibraryV3Service 和安全章节读取。
- 切换 `/api/content/*`，前端传递 pin。
- 上传状态继续等待 V3 build，并触发 Library 刷新。

完成门槛：搜索、书架、阅读在新增/更新/删除和 V3 故障场景下保持一致。

### 阶段 C：离线、文本质量和队列效率

- 拆分网络策略，移除 clean 的隐式全局 LLM 依赖。
- 修改 PyMuPDF flags、规范化和 tokenizer。
- 增加队列 retry、分类清理、batch lock 和 job 裁剪。

完成门槛：offline 零外网、文本 golden、并发隔离和队列 100+ 任务测试通过。

### 阶段 D：桌面布局验证和完整回归

- 先在桌面复现或证伪 BUG-ING-008，再应用最小高度链加固。
- 跑窗口/缩放/内容矩阵及完整 E2E。
- 更新 `docs/architecture.md`、`docs/development.md` 和最终验收记录。

## 8. 兼容、迁移与回滚

### 8.1 兼容策略

- `/api/ingest/full.input_pdf` 和旧 `strategy` 保留一个发布周期，服务端添加弃用日志。
- `/api/content/*` URL 和主要响应字段保持；新增 pin 和身份字段。
- 旧章节参数保留一个版本，但必须通过旧路径安全校验并记录弃用。
- Legacy JSON 文件可暂留在磁盘，但新代码不读取。
- 当前进程内 job 和上传队列语义不改变为持久化，不引入数据迁移。

### 8.2 内容迁移

- 新导入 PDF 自动使用新文本质量路径。
- 旧 V3 内容先通过 tokenizer 规范化和全量重建改善检索。
- 要改善旧正文展示，必须从保留源文件重新提取；没有源文件时不做猜测性重写。
- V3 Library 使用 Segment 内已有 `document/document_tree`，通常不需要为接口切换改变 Segment schema；若实现时新增投影字段，则必须升级 recipe 并执行一次全量 V3 构建。

### 8.3 回滚

- 本方案不加入运行时自动 Legacy fallback 或长期双后端开关。
- 发布前保留旧 JSON 和用户内容不动；需要紧急回滚时部署上一已知版本，而不是在新版本内部静默切源。
- 上传暂存、内存缓存和新响应字段均为可丢弃派生状态；回滚不需要数据库迁移。
- 文本规范化前保留用户源文件；回滚时可重新提取，不执行不可逆的批量正文替换。
- 若 V3 Library 上线后出现阻断性 503，停止继续发布新版本、保留完整错误证据并回滚应用代码；不得删除 V3 Generation/View。

## 9. 风险与缓解

| 风险 | 缓解措施 |
|---|---|
| PyMuPDF EPUB 目录/图片质量低于 Pandoc | 明确标记兼容模式；用同一 fixture 对比正文顺序、章节和图片；Pandoc 可用时优先。 |
| 上传接口扩大本地攻击面 | loopback 绑定之外仍做大小、签名、路径、slug、ZIP 和并发校验；原子写入。 |
| offline 与 AI 并发串扰 | 删除 clean 对进程级 active LLM config 的隐式依赖；classifier 按 job 注入；并发测试。 |
| 文档列表冷启动解码较多 Segment | 以 pin 为键建立单份内存快照；按 segment_hash 做有界 LRU；用基准决定是否需要可重建磁盘缓存。 |
| 章节正文已改但 V3 未重建 | 每次按 source fingerprint 校验，返回 `CONTENT_OUT_OF_SYNC`。 |
| Unicode 规则破坏公式或 CJK | 只做明确映射和 NFC，不全局 NFKC；建立多语言/公式 golden。 |
| composer 问题来自缓存而非 CSS | 桌面记录当前进程 URL、资源时间戳、WebView2 和 rect；先取证，再做最小加固。 |
| 兼容字段长期不删除 | 每个弃用路径写日志、测试和移除版本；架构文档同步维护。 |

## 10. 完成定义

全部满足以下条件，才视为本轮修复完成：

1. 八个问题都有对应实现、自动化测试和可复查验收证据。
2. PDF 和 EPUB 能从桌面原生选择、拖放或浏览器降级入口完成导入。
3. 入库 job 和 V3 build job 的状态对用户无歧义，只有索引发布后才显示“可浏览”。
4. 搜索、Library 和聊天引用都基于同一 V3 Generation/View。
5. `offline` 由网络哨兵证明零外网，clean 不尝试 LLM。
6. 连字、重音和编码链路测试通过，且没有破坏中文、公式和正确 Unicode。
7. 队列批量重试/清理与后端任务上限工作正常。
8. Windows 桌面端聊天 composer 几何和交互矩阵通过。
9. 全量 Python、Node 回归通过，正式用户数据未被测试污染。
10. `docs/architecture.md` 与 `docs/development.md` 在实现合入时同步更新。
