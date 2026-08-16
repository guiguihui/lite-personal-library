# 知识链接 UI 验收报告（2026-08-02）

## 结论

当前结论：**通过**。

Wikilink、反向链接、悬浮/触控预览、局部一跳图谱、深浅色主题、首次跨文档打开和已有标签复用均通过。重复打开已有文档标签后，标签、活动栏和主内容区保持一致；刷新后状态仍能正确恢复。

## 验收环境

- 分支：`feat/knowledge-linking-governance-context`
- 日期：2026-08-02
- 服务：隔离的临时馆藏，未改动真实馆藏
- 自动回归：`260 passed, 1 skipped`
- 样本：1 本书、1 篇论文、3 篇笔记，覆盖双向、单向、断链、锚点和跨馆藏链接

## 人工验收清单

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| Markdown 正文原样响应与正常换行 | 通过 | `01-dark-reader-backlinks.png` |
| Wikilink 已解析、带锚点、断链、代码保护 | 通过 | `04-dark-wikilink-states.png` |
| 反向链接来源、次数与摘录 | 通过 | `01-dark-reader-backlinks.png`、`05-light-reader-backlinks-graph.png` |
| 深色模式预览卡 | 通过 | `03-dark-wikilink-popover.png` |
| 浅色模式预览卡 | 通过 | `06-light-wikilink-popover.png` |
| 触控首次点击只显示预览 | 通过 | `07-touch-first-tap-preview.png` |
| 局部一跳图谱、节点类型颜色、文本后备列表 | 通过 | `02-dark-local-graph.png`、`05-light-reader-backlinks-graph.png` |
| 图谱边缘标签不裁切 | 通过 | `02-dark-local-graph.png` |
| 首次跨文档打开新标签 | 通过 | `08-multitab-paper-opened.png` |
| 已有标签复用 | 通过 | `10-existing-tab-reuse-fixed.png`（失败前证据：`09-failed-existing-tab-reuse.png`） |

## 本轮发现并修复

1. `/api/content/section` 把 Markdown 当 JSON 字符串返回，页面出现外层引号和字面量换行；已改为 `PlainTextResponse`。
2. 知识组件使用了项目中不存在的主题变量，深色预览卡显示为白底；已对齐现有主题变量。
3. 图谱边缘节点及标签被裁切；已限制节点坐标并按画布半区调整标签锚点。
4. API 单数文档类型与文档库复数类型不一致；已统一归一化。
5. 初始化默认书架与指定文档加载发生异步覆盖；已合并初始化入口并增加挂载版本隔离。
6. 触控/焦点重复触发会创建多个 tooltip；已在显示前清理旧预览实例。
7. 恢复显式标签 ID 时未推进新标签计数器，导致聊天标签和新建论文标签同为 `tab-1`；已增加独立标签 ID 分配器，恢复时预留 ID，新建时跳过所有已占用 ID。

## 阻断项复验

已重放以下浏览器序列：

`笔记 → Wikilink 打开论文 → 图谱打开笔记 → 再次点击同一论文 Wikilink → 复用已有论文标签`

结果：仅存在一个 `graph-paper` 标签，文档库活动态保持选中，聊天输入区不存在，论文标题与局部图谱正确显示；刷新后仍保持一致。另行打开新聊天标签时，其 ID 为 `tab-3`，与恢复的 `tab-1`、`tab-2` 不冲突。
