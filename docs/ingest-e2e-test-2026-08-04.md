# 文件导入端到端测试记录（2026-08-04）

## 1. 测试目标

按桌面端推荐流程，从“上传”开始验证以下链路：

`选择文件 → 创建入库任务 → extract → clean → translate → validate → publish → V3 增量索引 → 文档库/搜索/聊天可见性`

本轮只记录问题，不修改业务代码。测试产生的本地资料和索引保留，便于后续复现。

## 2. 测试环境

- 系统：Windows 桌面端
- 项目：`E:\lite-personal-library`
- 分支：`dev`
- 基线提交：`be33a54601282bfe26aa771f6ad7c1f4a3393774`
- Python：3.10.10
- 应用版本：0.1.0
- 索引：PageIndex V3，就绪
- LLM：DeepSeek，已配置，后端代理开启
- EPUB 测试文件：`F:\Downloads\Naomi Alderman - The Power.epub`（1,027,712 bytes）
- PDF 对照文件：`F:\Downloads\2105.12899v1.pdf`

## 3. 测试执行摘要

| ID | 场景 | 结果 | 证据/说明 |
|---|---|---|---|
| TC-ING-001 | 桌面“上传”页选择 EPUB | 部分失败 | 原生对话框默认过滤器为 `PDF Files (*.pdf)`，EPUB 默认不可见；需要手动切换文件类型 |
| TC-ING-002 | 拖放或浏览器文件输入后提交 | 失败 | 前端仅提交文件名，不上传文件内容；后端把文件名拼到 `data\pdfs` 后报 `FileNotFoundError` |
| TC-ING-003 | 使用 EPUB 绝对路径运行完整流水线 | 失败 | 任务 `ing_db7d410d8263` 在 `extract` 阶段失败：`pandoc not installed` |
| TC-ING-004 | PDF 对照：extract + clean + validate | 通过，有告警 | 任务 `ing_f523292a9f95` 完成；12 页、37 张图片、56 次清理修复、1 个校验告警 |
| TC-ING-005 | 发布并自动构建 V3 增量索引 | 通过 | 构建任务 `idx_e40143f6e45c` 完成，`docs_built=1` |
| TC-ING-006 | `/api/search` 搜索新导入文档 | 通过，有质量问题 | 查询 `QA PDF Control` 命中 `paper:qa-pdf-control-20260804`；正文存在乱码/字形问题 |
| TC-ING-007 | 桌面“文档库 → 论文”查看新文档 | 失败 | 页面显示“暂无论文”；`/api/content/docs?type=paper` 返回 1 个旧文档但不包含新导入 slug |
| TC-ING-008 | LLM 后端代理最小调用 | 通过 | `/api/llm/proxy` 返回 HTTP 200 和 `OK` |
| TC-ING-009 | 桌面聊天页提交新文档问题 | 阻塞 | 当前窗口 1387×762 下聊天输入框不在可视区域，无法从界面完成提交 |

## 4. 流水线实际结果

### 4.1 EPUB 任务

任务 ID：`ing_db7d410d8263`

请求使用绝对路径，证明后端可以找到源文件；随后失败：

```text
[pipeline] >>> stage: extract
[extract] start: F:\Downloads\Naomi Alderman - The Power.epub
  [error] pandoc not found in PATH
[pipeline error] RuntimeError: pandoc not installed
```

因此 EPUB 的 `clean/translate/validate/publish/index` 均未执行。

### 4.2 PDF 对照任务

任务 ID：`ing_f523292a9f95`

执行结果：

- extract：成功，12 页，37 张图片。
- clean：成功，56 个修复；`llm_used=true`。
- validate：成功，0 error、1 warning。
- publish：成功，输出到 `data/content/papers/qa-pdf-control-20260804`。
- 自动增量构建：成功，任务 `idx_e40143f6e45c`，构建 1 个文档。
- V3 搜索：成功命中新文档。
- 文档库：失败，论文页不显示新文档。

保留的测试产物：

```text
data/pdfs/qa-pdf-control-20260804/
data/content/papers/qa-pdf-control-20260804/
```

## 5. 缺陷清单

### BUG-ING-001：拖放/浏览器选择没有真正上传文件

- 严重级别：High
- 优先级：P1
- 频率：必现
- 复现：拖放 EPUB，点击“全部开始”。
- 期望：文件内容上传或复制到后端可读位置，再创建任务。
- 实际：仅发送 `file.name`；后端尝试读取 `data\pdfs\<文件名>` 并报文件不存在。
- 影响：拖放入口和浏览器降级入口不可用。
- 临时绕过：桌面原生选择器返回绝对路径，或手动把文件复制到 `data\pdfs`。

### BUG-ING-002：文件选择器默认只显示 PDF

- 严重级别：Medium
- 优先级：P2
- 频率：必现
- 复现：上传页点击“选择文件”。
- 期望：默认同时显示 `.pdf/.epub/.docx`。
- 实际：默认过滤器为 `PDF Files (*.pdf)`；EPUB 文件不可见，必须手动切换文件类型。
- 影响：界面宣称支持 EPUB，但用户会误以为文件不存在或格式不支持。

### BUG-ING-003：EPUB 依赖 Pandoc，但应用没有启动/提交前检查

- 严重级别：High
- 优先级：P1
- 频率：当前环境必现
- 期望：启动状态或提交前明确提示 Pandoc 未安装，并阻止创建必然失败的任务；打包版应携带依赖或提供安装指引。
- 实际：任务先返回 `running`，进入后台后才在 extract 阶段失败。
- 影响：所有 EPUB 无法导入，且用户只能从流水线日志定位原因。

### BUG-ING-004：V3 搜索已收录，但文档库仍读取旧索引

- 严重级别：High
- 优先级：P1
- 频率：必现
- 复现：成功导入 PDF并完成 V3 增量构建，进入“文档库 → 论文”。
- 期望：新论文出现在文档库中并可阅读。
- 实际：V3 `/api/search` 能命中，但论文页显示“暂无论文”。
- 根因证据：`/api/content/docs` 仍读取 `global-index.json`；V3 构建日志明确 `Legacy export: disabled`。
- 影响：入库完成后用户无法从资料库入口找到文档，搜索和浏览状态不一致。

### BUG-ING-005：`local（本地，离线）` + clean 仍调用 LLM

- 严重级别：Medium
- 优先级：P2
- 频率：本次 PDF 对照任务出现
- 复现：选择 `local`，关闭 translate，保留 clean。
- 期望：界面应明确说明 clean 可能调用 LLM，或提供完全离线选项。
- 实际：任务结果 `clean_stats.llm_used=true`。
- 影响：可能产生网络请求、API 费用和隐私预期偏差。

### BUG-ING-006：本地 PDF 提取存在文本乱码和字形损坏

- 严重级别：Medium
- 优先级：P2
- 频率：本次样本可复现
- 例子：搜索结果出现 `â`；正文出现 `L¨u`、`efﬁciency` 等异常字形。
- 影响：阅读质量下降，并可能影响检索分词和模型引用质量。

### BUG-ING-007：失败队列缺少批量清理/重试

- 严重级别：Low
- 优先级：P3
- 频率：持续累积
- 实际：上传页保留 11 条历史失败项；只有“清空已完成”，失败项只能逐条处理。
- 影响：新任务难以定位，长期使用后队列噪声很大。

### BUG-ING-008：聊天输入框在当前桌面窗口尺寸下不可见

- 严重级别：High
- 优先级：P1
- 环境：窗口内容区域约 1387×762。
- 复现：重启应用，打开聊天页。
- 期望：输入框和发送按钮固定在可视区域底部。
- 实际：欢迎页可见，但输入框落在当前可视区域之外；可访问性树中存在输入框，界面中不可操作。
- 影响：用户无法从桌面界面发起聊天。

## 6. 额外观察

- 入库任务对无效源路径和缺失运行时依赖没有同步前置校验；API 会先返回 `running`，随后异步失败。
- PDF 校验发现 8 个非标准列表标记，但只记 warning，未阻断发布，这是合理的容错行为。
- 入库任务完成后自动触发 V3 增量构建，构建、发布和 `/api/search` 链路本身工作正常。
- LLM 代理单独测试正常，之前聊天页出现的 HTTP 500 本轮未在最小代理调用中复现。

## 7. 建议修复顺序

1. P1：实现真实文件上传/复制，并在创建任务前校验路径、扩展名和依赖。
2. P1：让文档库目录和阅读入口改读 V3 catalog/view，不再依赖旧 `global-index.json`。
3. P1：修复聊天 composer 的高度/定位，覆盖常见窗口尺寸回归测试。
4. P2：将文件选择器改为默认组合过滤器；明确 clean 的联网/LLM行为。
5. P2：增加文本编码和 Unicode 规范化处理。
6. P3：增加“清空失败”“重试失败”和队列筛选。
