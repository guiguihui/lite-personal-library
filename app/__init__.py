"""LQ-D — Python 后端包。

模块组织(零耦合,依赖单向向下):
  config   — 配置管理(叶子,无依赖)
  storage  — 文件 IO(依赖 config)
  llm      — LLM 配置 + 代理(依赖 config)
  pdf      — PDF 提取双后端(依赖 config)
  vendor   — 从 yuulibrary-main 复制的脚本(叶子,被 adapter 调用)
  index    — 索引构建(依赖 vendor + config)
  ingest   — 入库流水线(依赖 pdf + vendor + llm + config)
  http     — FastAPI 服务层(依赖以上所有,被 main 组合)
  retrieval — Python 检索重写(对拍工具,依赖 storage + config,不进 http)
"""

__version__ = "0.1.0"
