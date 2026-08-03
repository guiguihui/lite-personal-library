# LQ-D 技术架构

> 本文描述 2026-08-03 起的当前实现。开发与测试见 [development.md](development.md)，打包见 [deployment.md](deployment.md)。

## 1. 架构结论

LQ-D 是本地优先、无数据库的桌面知识库。它不是“没有后端”：桌面进程内包含一个只监听 127.0.0.1 的 FastAPI 服务。文档、索引和配置都保存在本地文件系统，WebView 前端通过本地 HTTP API 使用这些能力。

当前检索架构已收口为 PageIndex V3：

- GET /api/search 是唯一检索入口；全局搜索和聊天的 search_library 使用同一接口。
- 构建、发布、检索、状态检测和知识链接以同一个 V3 Generation/View 为准。
- 前端不再下载 global-index.json、node-index.json、inverted-index.json 和 chunks.json 执行检索。
- 前端只负责 Agent 编排、上下文预算与组装、引用和界面；召回、融合与排序只在后端维护。
- V2 不再作为应用运行时。V3 仅复用其中的规范化 JSON、对象存储、Segment 构建、流式读取和校验等底层组件。

## 2. 系统边界

    用户
      ↓
    PyWebView / WebView2 桌面窗口
      ↓
    Vanilla JS 前端
    （Agent 编排、上下文、引用、界面）
      ↓ HTTP
    FastAPI 本地服务 127.0.0.1:8765
      ├─ /api/search → PageIndex V3 Runtime
      ├─ /api/index  → V3 Worker 子进程
      ├─ /api/links  → link-index.json
      ├─ /api/ingest → PDF/EPUB 入库流水线
      └─ /api/llm    → 可选 LLM 代理
             ↓
    data/content、data/pageindex、data/config、data/pdfs

系统没有远程业务服务和数据库。外部网络只在用户主动使用云端 LLM、MinerU 或其他远程 Provider 时发生。

## 3. 运行时进程

app.main:main 完成以下启动流程：

1. 从 data/config 读取配置，并确保本地数据目录存在。
2. 通过 app.http.server:create_app 创建 FastAPI 应用。
3. 在 daemon 线程中启动 Uvicorn，默认监听 127.0.0.1:8765。
4. 主线程创建 PyWebView 窗口并加载 /frontend/index.html。
5. 打包后的 V3 构建使用同一可执行文件的 --pageindex-v3-worker 模式启动隔离子进程。

端口会在启动前预检，前端静态资源带 Cache-Control: no-store，避免 WebView2 复用旧资源。

## 4. 前端职责

前端位于 frontend/，由原生 HTML、CSS 和 JavaScript 组成，不需要 Node 运行时或打包步骤。

| 层 | 目录 | 职责 |
|---|---|---|
| UI 框架 | frontend/core/ | Activity Bar、标签页、侧栏、状态栏、主题、事件总线 |
| 聊天 | frontend/chat/ | 会话、ReAct 工具循环、LLM 流、上下文预算、消息与引用 |
| 资料浏览 | frontend/library/ | 文档列表、阅读器、目录与文档标签 |
| 管理与入库 | frontend/manage/、frontend/upload/ | V3 构建任务、入库队列与进度 |
| 配置 | frontend/config/ | Provider、模型、路径和应用设置 |
| 公共能力 | frontend/shared/ | Markdown 渲染、设置、知识链接组件 |

### 4.1 聊天检索边界

聊天检索流程如下：

    模型调用 search_library
      → GET /api/search?q=...&limit=12
      → 按后端顺序接收 V3 命中
      → 组装 source_id、正文、面包屑和版本信息
      → 按模型窗口打包上下文
      → 交给 ReAct 循环继续回答

前端不执行 BM25、RM3、RRF、词法重排或 MMR，也不加载整套倒排索引。get_section 只在已有检索上下文上读取对应 Markdown 区间。

## 5. 本地 HTTP 服务

| 端点 | 职责 |
|---|---|
| GET /api/search | 唯一文本检索入口，查询当前 V3 Search View |
| POST /api/index/build | 启动 V3 bootstrap 或增量构建 |
| GET /api/index/build/{job_id} | 查询构建、发布和知识链接阶段 |
| GET /api/status | 返回 V3 就绪状态、Generation、View ID 和其他状态 |
| GET /api/content/section、GET /raw/content/* | 安全读取 Markdown 或区间 |
| GET/POST /api/links/* | 知识链接、反向链接、邻域、预览和诊断 |
| POST /api/ingest/* | PDF/EPUB 入库与处理 |
| GET/PUT /api/settings | 应用与 LLM 配置 |
| POST /api/llm/proxy | 可选本地 LLM 流代理 |

/pageindex/* 和部分 /api/content/* 仍是资料阅读界面的兼容读取面，但不参与构建、检索、聊天、状态或知识链接的权威判定，也不会向聊天下载全量索引。

## 6. PageIndex V3

### 6.1 不可变数据模型

- Generation：文档到 Segment 的逻辑映射，身份由内容和构建配方决定。
- Search View：某个 Generation 的可查询物理视图，由 Base 和零个或多个 Delta 组成。
- Segment：单篇文档的节点、切片和原始检索事实。
- Base/Delta：持久化词项层、文档所有权和统计信息。

检索请求必须同时绑定 Generation 和 View，不能在查询中猜测“最新版本”。

### 6.2 应用发布指针

V3 核心刻意不提供可变 latest。应用层 app.index.v3.runtime 通过 data/pageindex/current-v3.json 维护唯一发布指针，其中保存 Generation attestation、View attestation 和紧凑 Generation receipt。

发布使用同目录临时文件、fsync 和 os.replace 原子替换。旧请求继续使用已经打开的不可变视图，新请求才会读取新指针，因此不需要数据库事务或常驻索引服务。

### 6.3 构建与发布顺序

    管理界面
      → POST /api/index/build
      → app.index.status 创建后台任务
      → incremental 时读取 current-v3.json 作为 parent
      → V3 Supervisor 启动全新 Worker 子进程
      → Worker 生成 Generation/View attestation
      → Supervisor 校验结果、摘要和 lineage
      → runtime 原子发布 current-v3.json
      → 基于已发布 V3 重建 link-index.json
      → 任务返回 done 或 failed

外部 API 继续接受 full 和 incremental：

- full 表示不带 parent 的 V3 bootstrap。
- incremental 使用 current-v3.json 中经过验证的 parent；首次运行时自动 bootstrap。
- 两种模式都强制 legacy_export=none。

## 7. 唯一检索链路

app.http.routes_search 不读取旧的 global-index.json、inverted-index.json 或 chunks.json，也不做 shadow 双跑。

每次请求执行：

1. 检查 current-v3.json 是否存在。
2. 严格解析 Generation/View attestation 和 receipt。
3. 打开精确的 PinnedSearchView。
4. 通过 app.retrieval.search_view:search_pinned_view 执行稀疏候选检索。
5. 返回排序命中、正文区间和稳定引用字段。
6. 请求结束后关闭不可变 reader。

返回字段分三类：

- UI：doc_type、slug、node_id、title、breadcrumb、text。
- 上下文：source_md、line_num、line_end。
- 可重复性：generation、view_id、doc_key、doc_uid、segment_hash、local_id、node_key。

没有发布索引时返回空结果；指针存在但校验或打开失败时返回 HTTP 503，禁止静默回退到另一套索引。

## 8. 状态与知识链接

GET /api/status 的 index_ready 只由 V3 发布指针决定，同时返回 index_version=v3、generation 和 view_id。旧 JSON 是否存在不再影响就绪状态。

知识链接在 V3 发布成功后构建：

1. 从 data/content 建立文档目录并解析 wikilink/frontmatter。
2. 从当前 V3 View 读取标题到稳定节点 ID 的映射。
3. 原子生成 data/pageindex/link-index.json。
4. 构建失败显示在索引任务中，不会伪装成成功。

反向链接、邻域和预览只读取这份链接索引。

## 9. V2 的保留边界

V2 不再拥有应用当前指针或生产检索路径。V3 直接复用的 V2 底层能力包括：

- canonical.py、artifacts.py：规范化 JSON、哈希和原子 artifact 写入。
- ids.py、models.py：稳定文档标识和 Segment 配方。
- object_store.py、segment_builder.py：内容寻址 Segment。
- source_snapshot.py、input_proof.py：输入快照与证明。
- streaming_json.py、streaming_compiler.py、validator.py：有界读取、兼容验证与离线工具。
- process_metrics.py、benchmark.py：V3 基准测试复用。

这些模块是 V3 的实现库，不是第二套运行时。旧 V2 supervisor/worker、shadow 检索和前端 JS 检索均不在生产请求链路中。

## 10. 本地数据布局

    data/
    ├─ content/                  Markdown 真源
    │  ├─ books/
    │  ├─ papers/
    │  └─ notes/
    ├─ pageindex/
    │  ├─ current-v3.json        应用唯一发布指针
    │  ├─ generations/<id>/      不可变逻辑 Generation
    │  ├─ views/<id>/            不可变 Search View
    │  ├─ objects/               Segment、Base、Delta 等对象
    │  ├─ build/<job-id>/        Worker 请求与结果
    │  └─ link-index.json        知识链接索引
    ├─ config/                   应用与 Provider 配置
    └─ pdfs/                     导入源文件

Markdown 是事实源，V3 和链接索引都是可重建派生数据。API Key 优先保存在系统 Keyring。

## 11. 入库与外部服务

app/ingest 编排 extract → clean → translate → validate → note。PDF 可使用本地 PyMuPDF/pdfplumber，也可选择 MinerU。内容落盘后触发 V3 增量构建，发布成功后自动刷新知识链接。

LLM 采用 BYOK。前端可以按 Provider 直连，也可以通过 /api/llm/proxy 解决 CORS。LLM 不参与 V3 索引身份、发布和本地检索的正确性判断。

## 12. 关键约束

- 所有文本检索和聊天工具都调用 /api/search，排序策略只能维护一份。
- current-v3.json 是唯一“当前索引”定义，不得通过目录时间或文件名猜测 latest。
- 发布前必须由 Supervisor 验证 Worker 结果和 lineage。
- 生产构建不得开启 legacy export。
- 前端不得恢复全量倒排索引或 chunks 下载。
- 文档路径必须通过 storage 层校验，禁止 .. 越界。
- 本地服务默认只监听 loopback；外部网络能力必须由用户配置触发。