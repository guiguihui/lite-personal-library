"""配置管理模块。

职责:应用配置 + LLM 配置的 schema、默认值、读写。
零耦合:不依赖 http/index/ingest,只被它们调用。
"""
