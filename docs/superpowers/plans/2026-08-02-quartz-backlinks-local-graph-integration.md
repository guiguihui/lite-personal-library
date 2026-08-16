# Quartz 双向链接与局部图谱集成实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 轻量个人知识库 桌面知识库中原生实现与 Quartz 语义兼容的 Wikilink、反向链接、悬浮预览和一跳局部知识图谱。

**Architecture:** 不引入 Quartz 的静态站点生成运行时。后端在现有 PageIndex 构建完成后扫描 Markdown，生成独立且确定性的 `link-index.json`；FastAPI 提供解析、反链、预览和一跳邻域接口；Library 阅读器用原生模块渲染 Wikilink、反链和基于 D3 + SVG 的局部图谱。链接主键使用 `doc_type:slug`，避免书籍、论文、笔记同名时发生碰撞。

**Tech Stack:** Python 3.10+、FastAPI、PyYAML、原生 JavaScript、Marked、D3 v7（本地 vendored）、pytest、Node `node:test`。

## Global Constraints

- 保持 PyWebView + FastAPI + 原生 JavaScript 架构，不引入 Quartz/Node 构建运行时。
- 保持 `data/content/{books,papers,notes}` 和现有 PageIndex 产物向后兼容。
- 链接索引必须离线可构建、原子写入、重复构建字节级稳定。
- 所有 URL/文件路径继续通过现有路径安全边界，不允许客户端传任意磁盘路径。
- 局部图谱只展示当前文档的一跳入边和出边，默认最多 40 个邻居。
- 图谱运行时不得依赖 CDN；D3 以固定版本和许可证文件放入 `frontend/vendor/`。
- 不修改用户 Markdown 来“补反链”；反向链接是派生索引。
- 第一版不支持 `![[embed]]`、块引用 `#^id`、全局图谱或自动推荐关系。

---

## 1. 调研结论与建议决策

### 1.1 Quartz v5 的实现链路

Quartz v5 已把核心能力拆成独立插件：

1. Obsidian Flavored Markdown 把 `[[target#heading|alias]]` 转成普通内部链接。
2. CrawlLinks 统一解析链接目标、写入 canonical slug，并把每个页面的 outgoing links 保存到构建数据。
3. Backlinks 不维护单独的写入关系，而是筛选 `file.links` 中包含当前 slug 的来源页面。
4. ContentIndex 把 `title`、`links`、`tags`、`content` 等写入统一内容索引。
5. Graph 从内容索引构建有向边，但以入边和出边都可达的方式做 BFS；局部图默认 `depth=1`。
6. Popover 对内部链接按需获取目标内容，用 Floating UI 定位，并复用已创建的预览节点。

源码依据：

- [Quartz v5 repository](https://github.com/jackyzha0/quartz/tree/v5)
- [Obsidian Flavored Markdown transformer](https://github.com/quartz-community/obsidian-flavored-markdown/blob/main/src/transformer.ts)
- [CrawlLinks transformer](https://github.com/quartz-community/crawl-links/blob/main/src/transformer.ts)
- [Backlinks component](https://github.com/quartz-community/backlinks/blob/main/src/components/Backlinks.tsx)
- [ContentIndex emitter](https://github.com/quartz-community/content-index/blob/main/src/emitter.ts)
- [Graph component](https://github.com/quartz-community/graph/blob/main/src/components/Graph.tsx)
- [Graph runtime](https://github.com/quartz-community/graph/blob/main/src/components/scripts/graph.inline.ts)
- [Popover runtime](https://github.com/jackyzha0/quartz/blob/v5/quartz/components/scripts/popover.inline.ts)

### 1.2 为什么不直接安装 Quartz 插件

轻量个人知识库 是本地桌面应用，内容按请求从 Markdown 和 PageIndex JSON 读取；Quartz 是 Node/TypeScript 静态站点生成器，其插件依赖 Quartz 的 AST、构建上下文、页面组件和 SPA 生命周期。直接引入会形成第二套内容构建和前端运行时，并破坏当前 PyInstaller 打包路径。

建议采用“语义兼容、架构原生”的移植：借鉴 Quartz 的数据流与交互，不复制其框架代码。若实现阶段直接改编 Quartz 的具体代码，必须保留其 MIT 许可证声明；本计划默认重新实现。

### 1.3 本项目的关键适配点

- 当前索引入口是 `app/index/builder.py:24` 和 `app/index/builder.py:35`，适合作为链接索引构建编排点。
- Markdown 元数据、书籍/论文/笔记处理集中在 `app/vendor/build_pageindex.py:238`、`:610`、`:739`、`:791`；新能力不继续堆入 vendor 文件。
- Library 正文由 `frontend/library/library.js:252` 调用 `frontend/shared/render.js:129` 渲染，适合在渲染后统一 hydrate Wikilink。
- Library 的右侧 Overview 已在 `frontend/overview/overview.js:197` 展示文档元数据和目录，是反链与局部图谱的自然挂载位置。
- 当前 `doc_id` 只有 slug；三种馆藏类型之间可能同名，所以关系索引必须使用 `book:slug`、`paper:slug`、`note:slug`，不能复用裸 `doc_id`。

## 2. 产品范围

### 2.1 第一版包含

- Wikilink：`[[target]]`、`[[target|alias]]`、`[[target#Heading]]`、`[[target#Heading|alias]]`。
- 推荐的无歧义写法：`[[book:slug]]`、`[[paper:slug]]`、`[[note:slug]]`。
- 兼容写法：`[[books/slug]]`、`[[papers/slug]]`、`[[notes/slug]]`、唯一 slug、唯一标题、唯一 frontmatter alias。
- 普通 Markdown 内部链接也进入关系索引；外链和页内 `#anchor` 不进入跨文档索引。
- 反向链接列表显示来源文档、来源标题/章节、上下文摘录和出现次数。
- 局部图谱同时包含当前文档的 outgoing 和 incoming 邻居；互链显示为双向关系。
- 点击正文 Wikilink、反链或图节点都通过 `LqdLibrary.openDoc()` 打开/复用 Library 标签。
- 内部链接悬浮 250ms 后显示标题、类型、治理元数据和正文摘要；移出或切换文档时取消请求。
- 断链与歧义链接保留原文字，分别展示 `broken`、`ambiguous` 样式和原因。

### 2.2 第一版不包含

- `![[embed]]` 内容嵌入。
- Obsidian 块引用 `#^block-id`。
- 跨知识库、远端 URL 或任意本地路径解析。
- 二跳/全局图谱、标签节点、AI 推荐边。
- 自动改写用户 Markdown 或自动创建目标笔记。

## 3. 核心模型与接口

### 3.1 Python 类型

创建 `app/knowledge/models.py`：

```python
DocType = Literal["book", "paper", "note"]
ResolutionStatus = Literal["resolved", "broken", "ambiguous"]

@dataclass(frozen=True)
class DocumentRef:
    doc_key: str          # "paper:attention-is-all-you-need"
    doc_type: DocType
    slug: str
    title: str
    aliases: tuple[str, ...]
    source_files: tuple[str, ...]
    headings: Mapping[str, str]  # normalized heading -> node_id

@dataclass(frozen=True)
class ParsedLink:
    raw: str
    target: str
    alias: str | None
    anchor: str | None
    line: int
    column: int
    syntax: Literal["wikilink", "markdown"]

@dataclass(frozen=True)
class ResolvedEdge:
    source_key: str
    target_key: str | None
    status: ResolutionStatus
    candidates: tuple[str, ...]
    target_anchor: str | None
    target_node_id: str | None
    source_md: str
    source_line: int
    source_heading: str | None
    alias: str | None
    raw: str
    excerpt: str
```

### 3.2 解析优先级

解析 target 时必须按以下顺序，命中多个候选时停止并返回 `ambiguous`：

1. 显式 `book:slug` / `paper:slug` / `note:slug`。
2. 显式 `books/slug` / `papers/slug` / `notes/slug`。
3. 与当前文档同类型的精确 slug。
4. 全库唯一的精确 slug。
5. 全库唯一的规范化 title。
6. 全库唯一的 frontmatter `aliases` 值。
7. 无候选返回 `broken`。

规范化仅做 Unicode NFKC、trim、大小写折叠和连续空白合并；不做模糊匹配，避免错误连边。

### 3.3 `link-index.json` 契约

产物路径：`data/pageindex/link-index.json`。

```json
{
  "schema_version": 1,
  "documents": {
    "note:transformer": {
      "doc_key": "note:transformer",
      "type": "note",
      "slug": "transformer",
      "title": "Transformer",
      "aliases": ["Attention Model"],
      "preview": "正文首个有效段落",
      "governance": {
        "status": "reviewed",
        "reviewed_at": "2026-08-01",
        "source": "manual",
        "confidence": 0.95
      }
    }
  },
  "outgoing": {"note:transformer": []},
  "incoming": {"paper:attention-is-all-you-need": []},
  "diagnostics": {"broken": [], "ambiguous": []}
}
```

不写 `generated_at`，数组统一按 `(source_key, target_key, source_md, source_line, column)` 排序，确保重复构建无 diff。

### 3.4 HTTP API

创建 `app/http/routes_links.py`：

```text
POST /api/links/resolve
  body: {current_type, current_slug, targets:[{target, anchor}]}
  response: {results:[{status, doc_key, type, slug, title, node_id, candidates}]}

GET /api/links/backlinks?type=note&slug=transformer
  response: {doc, backlinks:[ResolvedEdge], total}

GET /api/links/neighborhood?type=note&slug=transformer&limit=40
  response: {center, nodes, edges, total_neighbors, truncated}

GET /api/links/preview?type=paper&slug=attention-is-all-you-need&node_id=0003
  response: {doc_key, title, type, metadata, excerpt_markdown, node_id}
```

所有 type 使用现有 `_normalize_type` 的单复数规则，但响应统一返回单数。

## 4. 文件结构

```text
app/knowledge/
  __init__.py             公开构建与查询接口
  models.py               不可变领域类型
  wikilinks.py            Wikilink/Markdown link 词法解析
  catalog.py              扫描三类馆藏并构造 DocumentRef
  resolver.py             target -> 文档/标题节点解析
  indexer.py              生成并原子写 link-index.json
  queries.py              backlinks/neighborhood/preview 纯查询

app/storage/
  link_index_io.py        读取、schema 校验和轻量 mtime 缓存

app/http/
  routes_links.py         `/api/links/*`
  schemas.py              链接 API 的 Pydantic 请求/响应模型
  server.py               注册 links router

frontend/shared/
  wikilinks.js            Marked extension、批量解析、点击导航
  link-popover.js         延迟、取消、缓存、定位和清理

frontend/library/
  knowledge.js            反链/图谱数据加载与生命周期
  local-graph.js          D3 force + SVG 渲染，导出纯数据函数
  knowledge.css           链接、反链、popover、图谱样式

frontend/vendor/
  d3.v7.min.js            固定版本离线依赖
  d3-LICENSE.txt          ISC 许可证

tests/knowledge/
  test_wikilinks.py
  test_catalog.py
  test_resolver.py
  test_indexer.py
  test_queries.py

tests/frontend/
  wikilinks.test.js
  local-graph.test.js

tests/
  test_links_api.py
  test_frontend_knowledge.py
```

## 5. 实施任务

### Task 1: 固化 Wikilink 语法与解析器

**Files:**
- Create: `app/knowledge/__init__.py`
- Create: `app/knowledge/models.py`
- Create: `app/knowledge/wikilinks.py`
- Create: `tests/knowledge/test_wikilinks.py`

**Interfaces:**
- Produces: `parse_links(markdown: str) -> tuple[ParsedLink, ...]`
- Produces: `normalize_lookup_key(value: str) -> str`
- Consumes: no application globals or filesystem state

- [ ] **Step 1: 写解析语法矩阵测试**

覆盖普通目标、alias、heading、中文标题、空 target、转义、普通 Markdown 内链、外链、图片、代码围栏和行内代码。明确 `![[embed]]` 第一版返回空结果。

- [ ] **Step 2: 运行解析器测试并确认失败**

Run: `pytest tests/knowledge/test_wikilinks.py -q`

Expected: FAIL because `app.knowledge.wikilinks` does not exist.

- [ ] **Step 3: 实现逐行状态机**

先屏蔽 fenced code 和 inline code span，再解析 `[[...]]` 与 Markdown link；记录 1-based line、column 和原始文本。不要用一个跨全文正则处理全部语法。

- [ ] **Step 4: 添加不可变类型与规范化函数**

所有结果用 frozen dataclass；规范化遵循 NFKC + casefold + whitespace collapse。

- [ ] **Step 5: 运行单元测试**

Run: `pytest tests/knowledge/test_wikilinks.py -q`

Expected: PASS.

- [ ] **Step 6: 提交解析器**

```bash
git add app/knowledge tests/knowledge/test_wikilinks.py
git commit -m "feat: parse internal wikilinks"
```

### Task 2: 建立文档目录与确定性链接解析

**Files:**
- Create: `app/knowledge/catalog.py`
- Create: `app/knowledge/resolver.py`
- Create: `tests/knowledge/test_catalog.py`
- Create: `tests/knowledge/test_resolver.py`

**Interfaces:**
- Consumes: `parse_front_matter()` behavior from `app/vendor/build_pageindex.py:238`, but does not import the vendor module
- Produces: `build_catalog(content_dir: Path, pageindex_dir: Path) -> Mapping[str, DocumentRef]`
- Produces: `resolve_link(source: DocumentRef, link: ParsedLink, catalog: Mapping[str, DocumentRef]) -> ResolvedEdge`

- [ ] **Step 1: 写三类馆藏 fixture**

构造 `books/alpha/_index.md` + chapter、`papers/alpha/_index.md`、`notes/transformer.md`，并为两个 `alpha` 制造 slug 冲突。

- [ ] **Step 2: 写解析优先级和歧义测试**

断言 `[[book:alpha]]` 唯一命中，`[[alpha]]` 在跨类型冲突时为 ambiguous，同类型来源按规则优先，title/aliases 仅在唯一时命中。

- [ ] **Step 3: 运行测试并确认失败**

Run: `pytest tests/knowledge/test_catalog.py tests/knowledge/test_resolver.py -q`

- [ ] **Step 4: 实现 catalog 扫描**

复用当前目录约定：book 的 metadata 在 `_index.md`、正文可有多个 chapter；paper 在 `_index.md`；note 是单文件。读取 `title`、`aliases` 和治理字段，并从已有 PageIndex 文档树建立 heading -> node_id 映射。

- [ ] **Step 5: 实现精确解析器**

严格执行 3.2 的七级优先顺序；返回候选键而不是随意选择第一项。

- [ ] **Step 6: 运行测试并提交**

Run: `pytest tests/knowledge/test_catalog.py tests/knowledge/test_resolver.py -q`

```bash
git add app/knowledge/catalog.py app/knowledge/resolver.py tests/knowledge
git commit -m "feat: resolve links across library types"
```

### Task 3: 生成原子、确定性的链接索引

**Files:**
- Create: `app/knowledge/indexer.py`
- Create: `app/storage/link_index_io.py`
- Create: `tests/knowledge/test_indexer.py`
- Modify: `app/index/builder.py:24`
- Modify: `app/index/builder.py:35`

**Interfaces:**
- Consumes: `build_catalog()`, `parse_links()`, `resolve_link()`
- Produces: `build_link_index(content_dir: Path, pageindex_dir: Path) -> LinkBuildStats`
- Produces: `read_link_index(cfg: AppConfig) -> Mapping[str, Any]`

- [ ] **Step 1: 写快照和幂等测试**

断言 documents/outgoing/incoming/diagnostics 结构；连续构建两次后文件 bytes 完全相同；写入中途异常时旧索引仍可读。

- [ ] **Step 2: 运行测试并确认失败**

Run: `pytest tests/knowledge/test_indexer.py -q`

- [ ] **Step 3: 实现两阶段扫描**

第一阶段构造完整 catalog；第二阶段扫描全部 Markdown 并解析边。每条边保存来源行、来源标题、上下文摘录和解析诊断。

- [ ] **Step 4: 实现原子写入**

在 `pageindex_dir` 内写临时文件，flush + `os.fsync()` 后用 `os.replace()` 替换 `link-index.json`；排序后使用固定 JSON separators 和 UTF-8。

- [ ] **Step 5: 接入现有构建编排**

只有 vendor PageIndex 构建成功后才调用关系构建。第一版全量扫描链接，即便 PageIndex 是 incremental；这样避免修改 vendor 的 fingerprint 协议。链接构建失败时返回失败的 `BuildResult`，但保留旧 link index。

- [ ] **Step 6: 实现 mtime 缓存读取**

`link_index_io.py` 只缓存已通过 `schema_version == 1` 校验的对象；文件 mtime 改变时重新读取。

- [ ] **Step 7: 运行构建相关测试并提交**

Run: `pytest tests/knowledge/test_indexer.py tests/test_build_pageindex_docdf.py -q`

```bash
git add app/knowledge/indexer.py app/storage/link_index_io.py app/index/builder.py tests/knowledge/test_indexer.py
git commit -m "feat: build deterministic link index"
```

### Task 4: 提供反链、解析、预览和一跳邻域 API

**Files:**
- Create: `app/knowledge/queries.py`
- Create: `app/http/routes_links.py`
- Create: `tests/knowledge/test_queries.py`
- Create: `tests/test_links_api.py`
- Modify: `app/http/schemas.py`
- Modify: `app/http/server.py:101`

**Interfaces:**
- Consumes: validated `link-index.json`
- Produces: the four endpoints defined in 3.4
- Produces: `get_neighborhood(index, doc_key, limit=40) -> dict[str, Any]`

- [ ] **Step 1: 写查询纯函数测试**

覆盖仅入边、仅出边、互链、自环过滤、邻居截断、稳定排序和未知 doc_key。

- [ ] **Step 2: 写 API 契约测试**

使用 FastAPI TestClient 和临时 `link-index.json`，验证 200/400/404、单复数 type、limit 边界和路径不可注入。

- [ ] **Step 3: 运行测试并确认失败**

Run: `pytest tests/knowledge/test_queries.py tests/test_links_api.py -q`

- [ ] **Step 4: 实现查询和路由**

邻居排序：mutual 优先，其次出现次数降序，再按规范化标题和 doc_key；节点只返回 UI 所需字段。preview 只能根据 doc_key 和已索引 source_md 读取，不接受客户端文件路径。

- [ ] **Step 5: 注册 router 并验证 OpenAPI**

Run: `pytest tests/knowledge/test_queries.py tests/test_links_api.py tests/test_http_api.py -q`

- [ ] **Step 6: 提交 API**

```bash
git add app/knowledge/queries.py app/http/routes_links.py app/http/schemas.py app/http/server.py tests
git commit -m "feat: expose knowledge link APIs"
```

### Task 5: 在 Marked 渲染链中支持 Wikilink

**Files:**
- Create: `frontend/shared/wikilinks.js`
- Create: `tests/frontend/wikilinks.test.js`
- Modify: `frontend/index.html:37`
- Modify: `frontend/index.html:87`
- Modify: `frontend/shared/render.js:129`
- Modify: `frontend/library/library.js:252`
- Modify: `frontend/library/library.js:300`
- Create: `tests/test_frontend_knowledge.py`

**Interfaces:**
- Produces: `LqdWikilinks.tokenize(text)` as a pure function available in browser and CommonJS tests
- Produces: `LqdWikilinks.hydrate(container, {type, slug}) -> Promise<void>`
- Consumes: `POST /api/links/resolve`, `LqdLibrary.openDoc(type, slug, nodeId)`

- [ ] **Step 1: 写前端语法一致性测试**

把 Task 1 的核心语法矩阵复制为 JSON fixture，由 Python 和 Node 测试共同读取，防止前后端对同一文本产生不同 token。

- [ ] **Step 2: 运行 Node 测试并确认失败**

Run: `node --test tests/frontend/wikilinks.test.js`

- [ ] **Step 3: 实现 Marked inline extension**

输出 `<a class="lqd-wikilink pending" data-target="..." data-anchor="...">`；alias 使用文本节点，属性做 HTML escaping；code span/fence 由 Marked tokenizer 边界保护。

- [ ] **Step 4: 实现批量 hydrate**

一次 section 渲染只发一个 resolve 请求；resolved 链接写入 type/slug/node_id 并绑定点击；broken/ambiguous 不导航，提供可读 title 和 `aria-disabled=true`。

- [ ] **Step 5: 接入 Library 生命周期**

`renderSection` 在插入 HTML 与 KaTeX 后调用 hydrate；`selectDoc` 更新当前文档上下文；重复渲染不得重复绑定监听器。

- [ ] **Step 6: 用 pytest 包装 Node 测试**

`tests/test_frontend_knowledge.py` 通过 `shutil.which("node")` 检测 Node；存在时运行两个 Node suite，不存在时显式 skip。

- [ ] **Step 7: 运行测试并提交**

Run: `pytest tests/test_frontend_knowledge.py tests/test_http_api.py -q`

```bash
git add frontend/shared/wikilinks.js frontend/shared/render.js frontend/library/library.js frontend/index.html tests/frontend tests/test_frontend_knowledge.py
git commit -m "feat: render and navigate wikilinks"
```

### Task 6: 在 Overview 展示反向链接

**Files:**
- Create: `frontend/library/knowledge.js`
- Create: `frontend/library/knowledge.css`
- Modify: `frontend/overview/overview.js:197`
- Modify: `frontend/overview/overview.js:441`
- Modify: `frontend/index.html:87`

**Interfaces:**
- Produces: `LqdKnowledge.renderBacklinks(container, doc)`
- Consumes: `GET /api/links/backlinks`, `LqdLibrary.openDoc()`

- [ ] **Step 1: 创建独立挂载点**

在文档信息和目录大纲之间插入 `data-knowledge-backlinks`，Overview 仅负责生命周期，不内嵌业务 fetch 逻辑。

- [ ] **Step 2: 渲染反链列表**

每项显示来源标题、馆藏类型 badge、来源章节、excerpt 和出现次数；同一来源文档的多次出现折叠，展开后显示具体 occurrence。

- [ ] **Step 3: 处理状态**

缺少 `link-index.json` 时显示“尚未构建链接索引”，无反链时显示“暂无反向链接”，请求失败通过 `LqdErrors` 上报但不影响目录和正文。

- [ ] **Step 4: 加入键盘导航与清理**

列表项使用 button/link 语义，支持 Enter/Space；文档切换时 AbortController 取消旧请求。

- [ ] **Step 5: 手工验证并提交**

Run: `pytest tests/test_frontend_knowledge.py tests/test_links_api.py -q`

```bash
git add frontend/library/knowledge.js frontend/library/knowledge.css frontend/overview/overview.js frontend/index.html
git commit -m "feat: show document backlinks"
```

### Task 7: 实现一跳局部图谱与悬浮预览

**Files:**
- Create: `frontend/library/local-graph.js`
- Create: `frontend/shared/link-popover.js`
- Create: `tests/frontend/local-graph.test.js`
- Create: `frontend/vendor/d3.v7.min.js`
- Create: `frontend/vendor/d3-LICENSE.txt`
- Modify: `frontend/library/knowledge.js`
- Modify: `frontend/library/knowledge.css`
- Modify: `frontend/index.html`

**Interfaces:**
- Produces: `LqdLocalGraph.toGraphModel(neighborhood) -> {nodes, edges}` pure function
- Produces: `LqdLocalGraph.mount(container, neighborhood, onOpen) -> cleanup`
- Produces: `LqdLinkPopover.attach(container) -> cleanup`
- Consumes: `GET /api/links/neighborhood`, `GET /api/links/preview`

- [ ] **Step 1: 写图数据转换测试**

断言中心节点唯一、入/出/互链方向正确、自环过滤、40 邻居截断提示和稳定 node order。

- [ ] **Step 2: 固定 D3 版本与许可证**

下载 D3 v7 的固定发布文件，记录来源 URL、版本和 SHA-256；加入 ISC 许可证，不使用运行时 CDN。

- [ ] **Step 3: 实现 D3 force + SVG 图**

中心节点固定；邻居按类型着色；边用 marker 表示方向，互链用双向 marker；支持 hover 高亮、拖动、缩放和点击打开文档。SVG 加 `role="img"` 与摘要 aria-label。

- [ ] **Step 4: 提供等价的可访问列表**

图下方保留 incoming/outgoing 文本列表；Canvas/SVG 不作为唯一导航方式。

- [ ] **Step 5: 实现 popover 生命周期**

只监听 `.lqd-wikilink.resolved`、反链和图谱文本列表；mouseenter/focus 250ms 后请求，LRU 缓存 50 项，mouseleave/blur/文档切换时取消；popover 不复制目标页面完整 DOM，只渲染 API 返回的受控 metadata 与 excerpt Markdown。

- [ ] **Step 6: 清理图实例**

Overview rerender 或 tab unmount 时停止 D3 simulation、移除 ResizeObserver/事件监听器并 abort fetch，避免多标签长期使用造成泄漏。

- [ ] **Step 7: 运行测试并提交**

Run: `node --test tests/frontend/local-graph.test.js`

Run: `pytest tests/test_frontend_knowledge.py tests/test_links_api.py -q`

```bash
git add frontend/library frontend/shared/link-popover.js frontend/vendor frontend/index.html tests/frontend
git commit -m "feat: add local graph and link previews"
```

### Task 8: 端到端回归、文档与发布保护

**Files:**
- Modify: `tests/test_publish.py`
- Modify: `tests/test_ingest.py`
- Modify: `tests/test_http_api.py`
- Modify: `docs/architecture.md`
- Create: `docs/wikilinks.md`
- Modify: `README.md`

**Interfaces:**
- Validates: publish -> PageIndex -> link index -> API -> Library navigation

- [ ] **Step 1: 增加入库 frontmatter 回归**

验证 publish 不删除用户已有 `aliases` 和治理字段；新入库内容无这些字段时仍可构建链接索引。

- [ ] **Step 2: 增加端到端 fixture**

创建 book -> paper、note -> paper、paper -> note 三条边，运行完整构建，断言反链 API 和一跳邻域与预期一致。

- [ ] **Step 3: 验证降级路径**

删除或损坏 `link-index.json` 后，搜索、Library 正文、PageIndex 构建仍可使用；知识面板显示可恢复错误，不白屏。

- [ ] **Step 4: 更新架构文档**

记录 `app/knowledge` 责任边界、`link-index.json` schema、构建数据流、API 和前端清理协议。

- [ ] **Step 5: 写作者指南**

`docs/wikilinks.md` 给出 canonical 写法、alias/heading、同名冲突处理、broken/ambiguous 状态和不支持的 embed/block ref。

- [ ] **Step 6: 运行完整测试**

Run: `pytest -q`

Expected: all existing and new tests pass; optional external-dependency tests remain explicitly skipped.

- [ ] **Step 7: 手工验收桌面 UI**

运行 `python run_app.py`，验证浅色/深色、三个馆藏类型、标签复用、键盘操作、图谱拖拽/缩放、悬浮请求取消和索引缺失降级。

- [ ] **Step 8: 提交文档与回归测试**

```bash
git add tests docs README.md
git commit -m "test: cover knowledge link integration"
```

## 6. 验收标准

- `[[paper:attention-is-all-you-need|Transformer 论文]]` 能在正文中显示 alias 并打开正确论文。
- 同时存在 `book:alpha` 和 `paper:alpha` 时，裸 `[[alpha]]` 不会随机选一个，而显示歧义状态。
- 任一文档能看到 incoming 反链、outgoing 链接和包含两者的一跳局部图谱。
- 图节点点击与正文 Wikilink 都复用现有 Library tab，不新开重复标签。
- 指向 heading 的链接能跳到目标 PageIndex node；不存在的 heading 作为 broken anchor 诊断，不丢失文档级关系。
- 链接索引连续构建两次内容完全一致；构建失败不覆盖上一版可用索引。
- 链接索引缺失、损坏或内容为空时，Library 阅读和搜索功能不受影响。
- 所有新增 Python 测试、Node 纯函数测试和现有 `pytest -q` 通过。

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| slug 跨类型碰撞 | 错链和错误反链 | 使用 `type:slug` 主键；裸 slug 仅唯一时解析 |
| Python 与 JS 语法不一致 | 索引边与正文点击不一致 | 共用语法 fixture；前端最终解析结果以 API 为准 |
| 每次 incremental 都全量扫描链接 | 大库构建变慢 | 第一版优先正确性；记录扫描耗时，超过 2 秒再引入 relation fingerprints |
| D3 生命周期泄漏 | 多标签运行后卡顿 | mount 返回 cleanup；停止 simulation 并清理 observer/listener |
| 未信任 Markdown HTML | XSS | 本次不扩大信任面；popover 只渲染受控字段；后续单独引入 DOMPurify |
| Quartz 上游继续演进 | 语义漂移 | 明确兼容语法版本；不把 Quartz 当运行时依赖 |
| 直接复制 MIT 代码遗漏声明 | 许可证风险 | 默认独立实现；若改编源码，补充 Quartz MIT notice 和来源清单 |

## 8. 发布与回滚

### 发布顺序

1. 先上线后端链接索引和 API；前端在 API/索引不存在时隐藏知识组件。
2. 再上线 Wikilink 渲染和反链列表。
3. 最后启用局部图谱与 popover，并观察大文档和多标签内存表现。

### 回滚

- 前端回滚：从 `frontend/index.html` 移除 knowledge、graph、popover 资源，正文仍由现有 Marked 渲染。
- 后端回滚：停止在 `app/index/builder.py` 调用链接构建并取消 links router；现有 PageIndex 文件不变。
- 数据回滚：`link-index.json` 是纯派生产物，可安全删除并重新生成；不需要迁移用户 Markdown。

## 9. 原待审核决策（已关闭）

以下三项已在 2026-08-02 的逐项评审中确认，最终约束见第 10 节：

1. 是否接受 canonical Wikilink 使用 `[[book:slug]]` / `[[paper:slug]]` / `[[note:slug]]`，同时保留唯一标题和 slug 的简写。
2. 第一版是否包含悬浮预览；若希望更快交付，可在 Task 7 中先只做局部图谱，把 popover 独立为后续 PR。
3. 局部图谱邻居上限是否采用 40；超限时按互链、出现次数、标题排序截断，并保留完整文本列表入口。

---

## 10. 评审决策补充（2026-08-02）

> 状态：已确认。若本节与前文冲突，以本节为准；前文“待审核决策”均已关闭。

### 10.1 身份与治理

- 采用持久化、不可变的类型化 ID：`book:<id>`、`paper:<id>`、`note:<id>`。
- ID 写入 frontmatter，标题、文件名或目录变化时不自动修改；旧名称按需进入 `aliases`。
- 规范链接为 `[[paper:attention-is-all-you-need]]`；简写只有唯一匹配时才能解析。
- 现有内容执行一次可审查、可回滚的迁移：先 dry-run，列出拟写入 ID、冲突、别名和非法字段，经确认后才写入。
- 迁移不批量改写现有 Markdown 正文链接；新文档从创建时必须带 ID。
- `status` 至少支持 `draft`、`reviewed`、`archived`；完整枚举留到实现 PR 固定。
- `reviewed_at` 使用 ISO 8601 日期或 `null`。
- `source` 使用字符串数组，可包含 URL、引用文本或内部规范 ID。
- 文档 `confidence` 使用 `0.0–1.0`；它描述内容可信度，不削弱人工明确链接。

示例：

```yaml
---
id: paper:attention-is-all-you-need
title: Attention Is All You Need
aliases:
  - Transformer paper
status: reviewed
reviewed_at: 2026-08-01
source:
  - https://arxiv.org/abs/1706.03762
confidence: 0.95
---
```

### 10.2 链接与关系语义

| 类型 | 来源 | 默认进入主图谱 | 成为事实关系的方式 |
|---|---|---:|---|
| `explicit` | Wikilink 或站内 Markdown 链接 | 是 | 作者明确写入正文 |
| `suggested` | 后续推荐引擎 | 否 | 用户确认后写回 Markdown |
| `provenance` | `source` 中的内部 ID | 否 | 独立保存，通过图谱开关叠加 |

- 本阶段交付事实关系层和 `suggested` 契约，不实现语义推荐模型。
- 接受推荐时，将规范 Wikilink 写入光标位置或 `## 相关内容`；创建章节前必须展示 diff。
- 推荐边 confidence 与文档 confidence 分开；人工 `explicit` 边不需要 confidence。
- Wikilink 与站内 Markdown 链接均生成有向 `explicit` 边，并保留 `syntax`。
- HTTP 外链与仅页内 `#anchor` 不生成跨文档边。
- 支持 `[[target|alias]]` 和 `[[target#heading|alias]]`。
- 第一版不支持 `![[embed]]` 与 `#^block-id`。

解析遵循“宁可报歧义，也不猜测”：

1. 完整规范 ID 精确匹配。
2. 当前文档同类型内的标题或别名唯一匹配。
3. 全馆藏标题或别名唯一匹配。
4. 无候选为 `broken`，多候选为 `ambiguous`。

只做 Unicode NFKC、trim、大小写折叠和连续空白合并，不做模糊匹配。断链或歧义保留原文并写入诊断，但不生成反链、不进入正式图谱。点击时提供选择目标、显式创建或暂不处理，不自动创建空文档。

### 10.3 索引、一致性与 API

- Markdown/frontmatter 是唯一事实源；`link-index.json` 是可删除、可重建的派生数据，不能覆盖 Markdown。
- 所有仍存在的文档均进入索引；状态只影响展示。`archived` 默认从图谱隐藏，但反链仍可折叠查看。
- 项目 API 保存或导入内容后，增量更新受影响文档及邻接关系。
- 应用启动时全量校验内容指纹；外部编辑器改动在重新聚焦或手动刷新时检测。第一版不引入常驻文件监听器。
- 索引带 `schema_version` 和 `content_fingerprint`，使用临时文件、flush、`fsync`、`os.replace`；失败时保留上一版。
- 相同输入连续构建两次必须字节一致，不写入每次变化的时间戳。
- 完整索引留在后端，前端只按需获取当前文档的解析批次、反链、预览或一跳子图。

建议新增：

```text
POST /api/links/resolve
GET  /api/links/backlinks?id=note:transformer
GET  /api/links/neighborhood?id=note:transformer&limit=40
GET  /api/links/preview?id=paper:attention-is-all-you-need&node_id=0003
GET  /api/links/diagnostics
```

API 只接受规范 ID 和已索引 node ID，不接受文件路径；`limit` 必须有服务端上限。索引缺失、损坏或过期时返回可恢复状态，不得让正文或搜索失败。

### 10.4 UI、图谱与悬浮预览

- 书籍、论文和笔记详情页正文下方显示反链，右侧显示可折叠一跳图谱。
- 所有 Markdown 渲染表面支持 Wikilink 和预览；列表页与全局 Overview 第一版不增加全局图谱。
- 索引保存有向边；局部图同时展开入边和出边，并用箭头或样式区分。
- 默认最多 40 个邻居；超限显示“另有 N 个节点未展开”。
- 截断顺序固定为互链、出链、入链，同级按文档 ID；不使用 confidence 改写人工链接优先级。
- 用户可按馆藏类型、方向、关系类型筛选或主动加载其余节点。
- 图谱只是增强视图；每个节点都有可键盘操作的等价文本入口。图谱失败、关闭动画或屏幕阅读器环境不影响正文导航。
- 预览仅包含标题、馆藏类型、治理字段和正文开头或标题锚点附近的受限片段，不加载目标脚本或远程资源。
- 桌面端悬停 250 ms 或聚焦后加载，移出、失焦、切换文档时取消；触屏首次点击预览、再次点击进入正文。
- 前端使用有界 LRU，不缓存目标页面的完整 DOM。

### 10.5 LLM 导出契约

- 内部编辑继续使用 Wikilink。
- 生成 `llms.txt` 和按 books、papers、notes 拆分的纯 Markdown 上下文时，将 resolved Wikilink 转成标准 Markdown 链接并保留规范 ID。
- broken/ambiguous 原样保留并进入导出诊断。
- 不把 `suggested` 关系伪装成正文事实链接。

## 11. 修订后的系统边界

```text
Markdown + frontmatter
        |
        v
文档目录扫描 ---> ID / alias / heading catalog
        |                    |
        v                    v
链接解析器 ----------> 确定性 resolver
        \                    /
         versioned link-index.json
                    |
            FastAPI query layer
          /          |          \
   backlinks      preview    neighborhood
          \          |          /
             Library detail UI
```

建议新增 `app/knowledge/frontmatter.py` 与 `migration.py`，并让原计划中的 `models.py`、`catalog.py`、`resolver.py`、`indexer.py`、`queries.py` 分别保持领域校验、目录、解析、构建和查询的单一职责。

## 12. 受控迁移

1. 扫描 books、papers、notes 并生成候选 ID。
2. 检测重复 ID、规范化标题/别名冲突和非法治理字段。
3. 输出 JSON 与 Markdown dry-run 报告，不修改文件。
4. 用户确认后生成逐文件备份或补丁清单。
5. 原子写入 frontmatter，只补齐确认字段。
6. 重建 PageIndex 与 link index。
7. 对比迁移前后文档数、正文 hash、诊断与索引健康。
8. 发布观察期结束前保留迁移清单。

回滚时按清单恢复 frontmatter，并删除可重建的 `link-index.json`。由于不批量改写正文，无需反向转换链接语法。

## 13. 对原实施任务的修订

原 Task 1–8 保留，但按以下顺序和约束实施：

1. **语法与 frontmatter 契约**：建立 Python/JavaScript 共享 fixture，覆盖 ID、alias、heading、Unicode、代码区、外链和不支持语法。
2. **Catalog 与 resolver**：实现三段确定性优先级，覆盖跨类型同名、别名冲突、断链与 broken anchor。
3. **迁移工具**：新增 dry-run、显式 apply、原子写入和迁移清单；验证正文不变与重复 apply 幂等。
4. **关系索引**：Wikilink/站内 Markdown 链接生成 `explicit`，内部 source ID 生成 `provenance`；支持增量更新、启动校验与原子替换。
5. **查询 API**：实现 resolve、backlinks、neighborhood、preview、diagnostics；覆盖路径注入、边界参数和损坏索引。
6. **Wikilink 渲染**：使用 Marked inline extension；单次渲染批量 resolve；resolved 复用 `LqdLibrary.openDoc()`；broken/ambiguous 提供候选与显式创建。
7. **反链组件**：挂在详情正文下方，展示来源、章节、摘录、次数和状态；支持键盘、取消、空状态与降级。
8. **局部图谱**：固定 D3 v7 补丁版本、来源、SHA-256、许可证；实现 40 节点上限、筛选、加载更多、文本降级和完整 cleanup。
9. **受限预览**：实现 250 ms 延迟、AbortController、有界 LRU 和触屏两段交互；不复制完整 DOM。
10. **LLM 导出适配**：resolved Wikilink 转标准 Markdown；broken/ambiguous 保留并诊断；三类导出复用同一解析结果。
11. **端到端回归与文档**：覆盖 publish -> PageIndex -> link index -> API -> Library，并更新架构文档和作者指南。

## 14. 修订后的验收标准

### 14.1 正确性

- 类型化 Wikilink 显示 alias 并打开唯一目标。
- 标题、路径或文件名变化后，只要 ID 不变，规范链接仍有效。
- 同名候选返回 `ambiguous`，不随机选择。
- Wikilink 和站内 Markdown 链接均产生反链。
- heading 链接跳到正确 node；heading 不存在时保留文档级关系并报告 broken anchor。
- broken/ambiguous 不进入正式图谱。
- `explicit`、`suggested`、`provenance` 不互相混淆。
- 接受推荐必须产生可审查的 Markdown diff。
- 相同输入连续构建两次，索引字节一致。

### 14.2 体验与可访问性

- 详情页可查看 incoming、outgoing 和两者组成的一跳图谱。
- 图谱截断时显示未展开数量。
- 图节点、正文链接和反链均复用现有 Library 标签。
- 图谱关闭或失败时，等价文本导航完整可用。
- 键盘、焦点、屏幕阅读器和触屏均可访问链接关系。

### 14.3 性能容量

以 1 万文档、10 万条 `explicit` 关系为容量基线：

- 完整构建在后台执行、报告进度，不阻塞 UI。
- 热缓存下 backlinks/neighborhood API P95 小于 100 ms。
- 热缓存下 preview API P95 小于 150 ms。
- 默认 40 节点图谱保持流畅。
- 记录 CPU、内存、索引大小和构建耗时；合成语料不提交仓库。

### 14.4 回归与降级

- `pytest -q` 与新增 Node 纯函数测试通过。
- 索引缺失、损坏、schema 不兼容或内容为空时，正文与搜索仍可用。
- 构建失败不覆盖上一版索引。
- migration dry-run 不写文件，apply 幂等，rollback 可恢复原 frontmatter。

## 15. 分阶段发布与回滚

功能开关至少拆分为：

- `knowledge_index_enabled`
- `wikilinks_enabled`
- `backlinks_enabled`
- `link_preview_enabled`
- `local_graph_enabled`
- `provenance_edges_enabled`

发布顺序：

1. 运行迁移 dry-run，解决重复 ID、别名歧义与非法治理字段。
2. 仅启用索引和 diagnostics，不改变页面。
3. 核对文档数、边数、broken/ambiguous 与索引稳定性。
4. 启用 Wikilink 与反链。
5. 启用受限预览。
6. 最后启用局部图谱，观察大文档、多标签和长期内存。
7. 观察期结束后再进入相关推荐阶段。

回滚：关闭相应 UI 开关；取消 links router；停止 PageIndex 后续的关系构建；删除派生 `link-index.json`；若已迁移 frontmatter，则按迁移清单恢复。图谱回滚不影响文本反链和正文导航。

## 16. 实施级剩余选择

产品与架构边界已确认，没有待决产品问题。实现 PR 仍需固定：

- `status` 的完整枚举及非法值兼容策略。
- link index 的精确字段命名与 schema 演进规则。
- D3 v7 的固定补丁版本与 SHA-256。
- 后台构建进度复用现有 index status 的方式。
- 应用重新聚焦时文件检测的节流窗口。

这些低层选择不得改变本补充确认的身份、关系分层、解析优先级、迁移安全、UI 降级或发布边界。

## 17. 实施状态（2026-08-02）

已完成：领域模型、frontmatter 校验、Wikilink/Markdown 内链解析、确定性 resolver、原子 link index、反链/邻域/预览/诊断 API、迁移 dry-run/apply、LLM Markdown 导出、前端 Wikilink、反链、预览、D3 一跳图谱、文本降级和环境功能开关。

D3 固定为 7.9.0，SHA-256 为 F2094BBF6141B359722C4FE454EB6C4B0F0E42CC10CC7AF921FC158FCEB86539，许可证为 ISC。

自动化验证：完整 pytest 为 260 passed、1 skipped；真实馆藏 full build 成功并生成 link-index.json。待发布前人工执行 PyWebView 浅色/深色、触屏和多标签验收，以及 1 万文档/10 万边容量压测。
