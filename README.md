# LQ-D — Python 桌面知识库应用

基于 [yuulibrary-main](../yuulibrary-main/) 复用的 Python 桌面知识库应用。

## 架构

- **GUI**: PyWebView + Web 前端复用(chat.js / retrieval.js / chat.css / KaTeX)
- **运行时检索 + ReAct**: 全在前端(WebView)跑,复用 retrieval.js + chat.js
- **Python 后端**: FastAPI 轻量 HTTP 服务 + 索引构建 + 入库流水线 + 配置管理
- **LLM**: 9 provider BYOK,前端直连,key 存本地(keyring 加密)
- **文档**: 本地 `data/content/`,索引 `data/pageindex/`,配置 `data/config/`

## 快速开始

```bash
# 安装依赖
pip install -e .

# 启动
python -m app.main
```

窗口打开后,默认 Chat 标签。首次问答前需在 Manage → 设置里配置 BYOK API key。

## 目录结构

```
yuulibrary-desktop/
├── app/          # Python 后端(多模块,零耦合)
│   ├── config/   # 配置管理
│   ├── http/     # FastAPI 服务层
│   ├── storage/  # 文件 IO
│   ├── llm/      # LLM 配置 + 代理
│   ├── index/    # 索引构建(阶段 2)
│   ├── ingest/   # 入库流水线(阶段 4-5)
│   ├── pdf/      # PDF 提取(阶段 4)
│   ├── retrieval/# Python 检索重写对拍(阶段 7)
│   └── vendor/   # 从 yuulibrary-main 复制的脚本
├── frontend/     # 前端资源(WebView 加载)
│   ├── chat/     # 从 yuulibrary-main/static/chat/ 复制
│   ├── library/  # 文档浏览(阶段 3)
│   ├── manage/   # 入库+索引+设置(阶段 2-5)
│   └── katex/    # 数学渲染
├── data/         # 用户数据(gitignore)
│   ├── content/      # markdown 文档
│   ├── pageindex/    # 索引产物
│   ├── config/       # app.yaml + llm.yaml
│   └── pdfs/         # PDF 原档
└── tests/        # 测试
```

## 实施阶段

- [x] 阶段 1:最小问答闭环
- [x] 阶段 2:索引构建接入
- [x] 阶段 3:Library 文档浏览
- [x] 阶段 4:PDF 提取(双后端)
- [x] 阶段 5:完整入库流水线
- [x] 阶段 6:LLM 代理(可选)
- [x] 阶段 7:Python 检索重写对拍
- [x] 阶段 8:打包发布

> 全部阶段已实现并验证:
> - **测试**:148 pytest 用例,144 passed + 4 skipped(检索对拍 54 + HTTP API 37 + PDF 26+3skip + 入库 27+1skip)
> - **benchmark**:golden.json 148 题对拍,overall recall 30%,与 JS harness 基线对齐
> - **打包**:PyInstaller spec 产物 `dist/lq-d/lq-d.exe`(约 100MB),exe 启动后 uvicorn + pywebview + 全端点 200 验证通过

详见 [计划文件](../python-vectorized-coral.md) 与 [docs/](docs/)。
