# LQ-D 桌面端 UI 重构设计文档

> 本文档描述从「三标签 + 浮动弹窗」向「Trae 风格 AI 知识库 IDE」重构的设计决策。
>
> 关联文档：[architecture.md](architecture.md)、[brand-cleanup.md](brand-cleanup.md)、[development.md](development.md)。

## 1. 背景与问题

### 1.1 当前结构

桌面端最初复用了 Hugo 静态站的 `chat.js`，采用三标签（Chat / Library / Manage）+ 浮动 AI 弹窗的交互模型。后续虽然引入了 Trae 风格的四栏骨架（Activity Bar + Sidebar + Main + Overview），但只是旧 UI 的「壳」，内部逻辑并未适配，导致：

- 左侧 Sidebar 显示「历史对话」；
- 右侧 Overview 又显示「最近对话」；
- `chat.js` 内部还残留旧浮动模式的 FAB、drawer、panel；
- `library.js` / `manage.js` 没有进入 Activity Bar 的视图调度；
- CSS tokens 不一致（`library.css` / `manage.css` 引用未定义的 `--app-*`）。

### 1.2 设计目标

把桌面端改造成一个更像 Trae / Cursor 的 **AI 知识库 IDE**：

- 左侧：Activity Bar + 上下文 Sidebar；
- 中间：多标签工作区（Editor Tabs）；
- 右侧：AI 上下文 / 引用 / 工具面板；
- 底部：状态栏；
- 全局：命令面板（Cmd/Ctrl+K）。

同时彻底拆分 `chat.js`，消除浮动模式，统一设计 tokens。

## 2. 设计原则

| 原则 | 说明 |
|------|------|
| 零构建链 | 继续原生 JS + CSS，不引入 npm/vite/react，降低 PyWebView 集成成本。 |
| 多文件低耦合 | 每个模块一个文件，职责单一；禁止模块间直接调用内部函数。 |
| 事件驱动 | 跨模块通信走统一事件总线 `LqdEvents`。 |
| 生命周期管理 | 所有可标签化视图实现 `mount/unmount/getTitle/getIcon` 接口。 |
| 渐进迁移 | 新框架代码用 `lqd/Lqd` 前缀，旧业务代码在重构中逐步迁移，不一次性全量替换 `yuu/Yuu` 以避免回归。 |

## 3. 总体布局

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Activity Bar │   Sidebar   │        Main Workspace        │    Overview    │
│   (48px)     │   (280px)   │        (flexible)            │    (320px)     │
├──────────────┼─────────────┼──────────────────────────────┼────────────────┤
│              │             │  ┌────────────────────────┐  │                │
│   Chat       │  历史对话    │  │       Tab Bar          │  │   上下文/引用   │
│   Library    │  或文档目录  │  ├────────────────────────┤  │   快捷工具      │
│   Manage     │  或索引任务  │  │                        │  │                │
│   Upload     │             │  │     Tab Content        │  │                │
│   Config     │             │  │                        │  │                │
│              │             │  └────────────────────────┘  │                │
├──────────────┴─────────────┴──────────────────────────────┴────────────────┤
│                              Status Bar (24px)                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Activity Bar

- 图标：Chat、Library、Manage、Upload、Config。
- 底部：折叠 Overview、主题切换按钮。
- 点击只改变左侧 Sidebar 内容，不影响 Main 区已打开的标签。

### 3.2 Sidebar

根据当前 Activity 切换内容：

| Activity | Sidebar 内容 |
|----------|--------------|
| Chat | 历史对话列表 + 新建对话按钮 |
| Library | 文档库分类（books/papers/notes）+ 最近文档 |
| Manage | 索引/入库任务列表 + 触发构建按钮 |
| Upload | 上传队列 |
| Config | 配置分组导航（LLM / 存储 / 应用） |

### 3.3 Main Workspace

- 顶部 Tab Bar，支持多开同类标签。
- 每个标签独立保存状态，切换标签时 `unmount` 旧组件、`mount` 新组件。
- 关闭最后一个标签时，自动打开一个默认 Chat 标签。

### 3.4 Overview

右侧固定宽度面板，根据当前 Activity / 活动标签显示不同内容：

| 场景 | Overview 内容 |
|------|---------------|
| Chat | 当前对话的检索引用片段、引用文档列表、快捷操作 |
| Library | 当前文档的元信息、目录大纲 |
| Manage | 索引/入库任务进度、日志摘要 |
| Upload | 上传队列统计、最近完成项 |
| Config | 当前配置项说明 / 快捷键提示 |

### 3.5 Status Bar

底部固定高度面板，显示：

- 左侧：索引状态（就绪 / 未构建 / 构建中）、数据更新时间；
- 中间：当前 LLM provider / model；
- 右侧：版本号、主题切换、网络状态。

### 3.6 Command Palette

`Ctrl/Cmd+K` 呼出，支持：

- 切换 Activity；
- 新建 / 打开 / 关闭标签；
- 打开最近对话；
- 搜索文档标题；
- 全局文本搜索；
- 切换主题；
- 触发索引构建；
- 清空历史。

## 4. 模块接口

### 4.1 事件总线 `LqdEvents`

```javascript
window.LqdEvents = {
  on(event, handler),
  off(event, handler),
  emit(event, payload)
};
```

关键事件：

| 事件 | 触发方 | 监听方 | 用途 |
|------|--------|--------|------|
| `activity:changed` | `LqdShell` | `LqdSidebar` / `LqdOverview` | 切换 Sidebar/Overview 内容 |
| `tab:opened` | `LqdTabs` | `LqdShell` | 更新 Tab Bar |
| `tab:activated` | `LqdTabs` | `LqdSidebar` / `LqdOverview` | 刷新上下文面板 |
| `tab:closed` | `LqdTabs` | `LqdTabs` | 激活相邻标签 |
| `chat:context` | `LqdChatAgent` | `LqdOverview` | 显示检索引用 |
| `chat:message` | `LqdChatComposer` | `LqdChatSession` | 保存会话 |
| `index:status` | `LqdManageBuilder` | `LqdStatusBar` / `LqdOverview` | 更新索引状态 |
| `settings:loaded` | `LqdSettings` | `LqdStatusBar` | 更新模型信息 |
| `theme:changed` | `LqdTheme` | 所有 CSS | 切换主题 |

### 4.2 全局状态 `LqdStore`

只存 UI 层面的最小状态：

```javascript
{
  theme: 'auto',           // light | dark | auto
  activity: 'chat',        // 当前 Activity Bar 选择
  tabs: [],                // { id, type, title, state }
  activeTabId: null,
  status: {
    indexReady: false,
    indexRunning: false,
    ingestRunning: false,
    provider: '',
    model: '',
    version: ''
  }
}
```

业务数据（聊天消息、文档树、索引任务）不进入 `LqdStore`，由各模块自行管理。

### 4.3 标签组件接口

每个可标签化的业务模块注册一个组件对象：

```javascript
window.LqdChat = {
  type: 'chat',
  getTitle(tab) { return tab.title || '新对话'; },
  getIcon() { return 'chat'; },
  mount(container, tab) { /* 渲染并恢复 tab.state */ },
  unmount(container, tab) { /* 保存状态到 tab.state 并释放事件 */ },
  renderSidebar(container) { /* 渲染历史对话 */ },
  renderOverview(container, tab) { /* 渲染引用上下文 */ }
};

LqdTabs.register('chat', window.LqdChat);
```

`LqdTabs.open({ type, title, state })` 会：
1. 生成唯一 `id`；
2. 把标签加入 `LqdStore.tabs`；
3. 激活该标签；
4. 调用组件 `mount`。

### 4.4 Activity 与标签解耦

Activity Bar 只决定 Sidebar 显示什么；标签页是独立的工作区。例如：

- 用户在 Chat Activity 下打开多个 Chat 标签；
- 切换到 Library Activity，Sidebar 变成文档目录，但 Main 区的 Chat 标签仍然保留；
- 从 Library Sidebar 点击文档，打开 Library 标签；
- 切换回 Chat Activity，Main 区标签不变，Sidebar 恢复历史对话。

## 5. 聊天模块拆分

原 `frontend/chat/chat.js`（2027 行）拆分为：

| 文件 | 职责 |
|------|------|
| `chat/session.js` | 当前会话（`sessionStorage`）与归档历史（`localStorage`）CRUD |
| `chat/llm.js` | SSE 读取、`buildRequest`、provider 适配、`streamText`、`callLLMSync` |
| `chat/agent.js` | ReAct 工具循环、`retrieveContext`、工具调用执行 |
| `chat/composer.js` | 输入框、发送按钮、Enter 快捷键、输入状态 |
| `chat/messages.js` | 消息气泡、思考过程、工具调用卡片、空状态 |
| `chat/citations.js` | 引用片段格式化与渲染 |
| `chat/index.js` | 注册 `LqdChat` 组件，组合上述模块 |

删除内容：
- `.yuu-ai-fab` 浮动按钮；
- `.yuu-ai-panel` 浮动面板；
- `data-drawer="history"` / `data-drawer="settings"` drawer；
- 内嵌 Settings 表单（迁移到 `config/`）。

## 6. CSS 设计系统

### 6.1 Tokens

`core/shell.css` 统一定义：

```css
:root {
  --bg-primary: #f8f8f8;
  --bg-secondary: #f3f3f3;
  --bg-tertiary: #e8e8e8;
  --bg-hover: #e0e0e0;
  --bg-active: rgba(9, 105, 218, 0.12);

  --fg-primary: #1f1f1f;
  --fg-secondary: #666666;
  --fg-tertiary: #8a8a8a;

  --border: #d4d4d4;
  --border-strong: #bbbbbb;

  --accent: #0969da;
  --accent-hover: #0550ae;
  --accent-bg: rgba(9, 105, 218, 0.12);

  --success: #1a7f37;
  --success-bg: rgba(26, 127, 55, 0.12);
  --warning: #9a6700;
  --warning-bg: rgba(154, 103, 0, 0.12);
  --danger: #cf222e;
  --danger-bg: rgba(207, 34, 46, 0.12);
}

:root[data-effective-theme="dark"] {
  --bg-primary: #0e0e0e;
  --bg-secondary: #1e1e1e;
  --bg-tertiary: #252526;
  --bg-hover: #2a2d2e;
  --bg-active: rgba(31, 111, 235, 0.15);

  --fg-primary: #e0e0e0;
  --fg-secondary: #a0a0a0;
  --fg-tertiary: #6e6e6e;

  --border: #333333;
  --border-strong: #424242;

  --accent: #1f6feb;
  --accent-hover: #388bfd;
  --accent-bg: rgba(31, 111, 235, 0.15);

  --success: #3fb950;
  --success-bg: rgba(46, 160, 67, 0.15);
  --warning: #d29922;
  --warning-bg: rgba(187, 128, 9, 0.15);
  --danger: #f85149;
  --danger-bg: rgba(248, 81, 73, 0.15);
}
```

### 6.2 布局变量

```css
:root {
  --activity-bar-width: 48px;
  --sidebar-width: 280px;
  --overview-width: 320px;
  --status-bar-height: 24px;
  --tab-bar-height: 36px;
}
```

### 6.3 旧 Token 迁移

| 旧 Token | 新 Token |
|----------|----------|
| `--app-bg` | `--bg-primary` |
| `--app-fg` | `--fg-primary` |
| `--app-border` | `--border` |
| `--app-tab-bg` | `--bg-tertiary` |
| `--app-tab-active` | `--bg-active` |
| `--body-background` | `--bg-primary` |
| `--gray-200` | `--border` |
| `--color-link` | `--accent` |

## 7. 数据流

### 7.1 启动

```
python -m app.main
  → load_app_config
  → create_app (FastAPI + StaticFiles)
  → run_server_in_thread
  → webview.create_window("LQ-D", http://127.0.0.1:8765/frontend/index.html)
  → index.html 加载 core/ → shared/ → chat/ → library/ → manage/ → upload/ → config/
  → LqdShell.init()
      → LqdTheme.init()
      → LqdEvents.init()
      → LqdStore.init()
      → LqdTabs.init()
      → LqdSidebar.init()
      → LqdOverview.init()
      → LqdStatusBar.init()
      → LqdCommandPalette.init()
      → LqdTabs.open({ type: 'chat', title: '新对话' })
```

### 7.2 发送一条消息

```
用户输入 → LqdChatComposer.onSend
  → LqdChatSession.saveUserMessage
  → LqdChatAgent.retrieveContext
      → LqdRetrieval.searchMultiPath
      → fetch /pageindex/*.json
      → fetch /raw/content/...
  → LqdChatLLM.streamText
      → fetch LLM provider SSE
      → LqdChatMessages.appendStream
  → LqdChatSession.saveAssistantMessage
  → LqdEvents.emit('chat:context', contexts)
  → LqdOverview 渲染引用片段
```

### 7.3 打开一篇文档

```
Library Sidebar 点击文档 → LqdLibrary.openDoc(type, slug, nodeId)
  → LqdTabs.open({ type: 'library', title: doc.title, state: { type, slug, nodeId } })
  → LqdLibrary.mount(container, tab)
      → fetch /api/content/docs/<type>/<slug>
      → render 目录树 + markdown 内容
  → LqdOverview 渲染文档元信息 + 目录大纲
```

## 8. 命令面板

`core/command-palette.js` 实现：

```javascript
window.LqdCommands = {
  register(command),          // 注册静态命令
  registerProvider(id, fn),   // 注册动态命令提供者
  open(),                     // 显示面板
  close(),
  filter(query),              // 模糊匹配
  execute(id, arg)
};
```

快捷键：
- `Cmd/Ctrl+K`：打开/关闭；
- `Esc`：关闭；
- `↑/↓`：选择；
- `Enter`：执行；
- `Ctrl/Cmd+W`：关闭当前标签（由 `LqdTabs` 处理）。

命令分类：

| 命令 | 说明 |
|------|------|
| `activity:*` | 切换 Activity |
| `tab:new:*` | 新建某类型标签 |
| `tab:close` | 关闭当前标签 |
| `chat:new` | 新建对话 |
| `chat:recent:<id>` | 打开历史对话（动态） |
| `history:clear` | 清空历史 |
| `library:open:<type>:<slug>` | 打开文档（动态） |
| `search:global` | 全局文本搜索 |
| `theme:light/dark/auto` | 切换主题 |
| `index:build` | 触发索引构建 |

## 9. 后端新端点

### 9.1 `/api/status`

```json
{
  "app_name": "LQ-D",
  "version": "0.1.0",
  "index_ready": true,
  "index_running": false,
  "ingest_running": false,
  "active_provider": "deepseek",
  "model": "deepseek-chat",
  "has_key": true,
  "keyring": true
}
```

### 9.2 `/api/search`

```
GET /api/search?q=<query>&limit=20
```

```json
{
  "query": "...",
  "results": [
    {
      "type": "chunk",
      "doc_type": "books",
      "slug": "...",
      "node_id": "...",
      "title": "...",
      "breadcrumb": "...",
      "text": "...",
      "score": 0.95
    }
  ]
}
```

实现优先复用 `app/retrieval/` 中的 Python 检索逻辑。

## 10. 迁移步骤

1. 创建 `core/` 框架层（events、store、tabs、sidebar、overview、statusbar、command-palette、theme、icons、shell）。
2. 重写 `index.html`，引入 `core/` 与新布局（Tab Bar + Status Bar）。
3. 拆分 `chat.js` 为 `chat/{index,session,llm,agent,composer,messages,citations}.js`。
4. 改造 `library.js` / `manage.js` / `upload.js` / `config.js` 为标签组件。
5. 删除 `home/home.js` / `home/home.css`，创建 `overview/`。
6. 统一 CSS tokens，修复 `--app-*` 未定义问题。
7. 后端新增 `routes_status.py` / `routes_search.py`。
8. 替换品牌文本为 LQ-D，清理 `uynajgi` / `KKKKhazix`。
9. 运行测试、构建验证、手动 UI 检查清单。

## 11. 兼容性

- URL 路径 `/api/*`、`/pageindex/*`、`/raw/*` 保持不变；
- `localStorage` key 从 `yuu_*` 迁移到 `lqd_*`，启动时做一次性迁移；
- 不修改 Python 检索核心，仅新增端点；
- `retrieval.js` 行为不变，仅命名空间与注释调整。

## 12. 待决策项

| 项 | 当前决策 | 备注 |
|----|----------|------|
| 内部 `yuu/Yuu` 前缀 | 本次不全量替换 | 风险低，后续可作为独立迭代。 |
| 标签状态持久化 | v1 不持久化 | 刷新页面后标签丢失，仅保留当前会话。 |
| 全局搜索实现 | 后端 `/api/search` | 可降级为前端加载 inverted-index 搜索。 |
| Library 多开标签 | 支持 | 每篇文档一个标签。 |
| Config 多开标签 | 支持 | 但通常只开一个。 |
