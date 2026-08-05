"""索引构建薄包装:调 vendor.build_pageindex.build(),转成 BuildResult。

不直接调 vendor 的 main()/__main__ 块,只走 build() 函数(monkey-patch 全局,
进程级隔离,不污染后续调用)。BuildResult 为 frozen dataclass,符合 immutability。
"""

from __future__ import annotations

from app.config.schema import BuildResult
from app.knowledge.build_hook import finish_with_links as _finish_with_links
from app.vendor.build_pageindex import build as _vendor_build


def _to_build_result(raw: dict) -> BuildResult:
    """把 vendor.build() 返回的 dict 转成 frozen BuildResult。"""
    return BuildResult(
        ok=bool(raw.get("ok", False)),
        docs_built=int(raw.get("docs_built", 0)),
        duration_sec=float(raw.get("duration_sec", 0.0)),
        error=raw.get("error"),
        log=tuple(raw.get("log", [])),
    )


def build_full(content_dir: str, pageindex_dir: str, llm_model: str = "") -> BuildResult:
    """全量构建索引。遍历 content/{books,papers,notes},重写所有索引文件。

    content_dir:    内容根(data/content)
    pageindex_dir:  输出根(data/pageindex)
    llm_model:       litellm 模型串;"" = 本地退化(不调 LLM,summary 用截断)
    """
    raw = _vendor_build(content_dir, pageindex_dir, llm_model=llm_model, mode="full")
    return _finish_with_links(raw, content_dir, pageindex_dir)


def build_incremental(content_dir: str, pageindex_dir: str, llm_model: str = "") -> BuildResult:
    """增量构建索引。基于 .fingerprints.json 检测变更,仅 patch 受影响文档。

    content_dir:    内容根(data/content)
    pageindex_dir:  输出根(data/pageindex)
    llm_model:       litellm 模型串;"" = 本地退化
    """
    raw = _vendor_build(content_dir, pageindex_dir, llm_model=llm_model, mode="incremental")
    return _finish_with_links(raw, content_dir, pageindex_dir)
