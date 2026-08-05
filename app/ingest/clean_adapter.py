"""清洗适配器:调 vendor.clean_markdown.clean() import 调用。

clean_markdown.clean(content) -> (cleaned_content, stats_dict)
原地写回 merged/book.md。import 调用(同进程),不走 subprocess。

理由:clean() 是纯函数(无副作用,不读 argv/env),import 调用安全。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from app.config.schema import AppConfig, LlmConfig
from app.config.store import load_llm_config
from app.ingest.jobs import IngestJob, append_log, update_job


def _inject_llm_config(cfg: AppConfig) -> None:
    """注入 app LlmConfig 到 vendor.llm_config(供 clean_markdown 伪标题 LLM 调用)。

    复用 translate_adapter 的注入模式:clean_markdown.fix_pseudo_headings
    通过 llm_config.has_config() 判断是否走 LLM 路径,未注入则降级正则兜底。
    """
    vendor_dir = str(Path(__file__).resolve().parent.parent / "vendor")
    if vendor_dir not in sys.path:
        sys.path.insert(0, vendor_dir)
    import llm_config  # type: ignore  # noqa: E402

    llm_cfg: LlmConfig = load_llm_config(cfg.config_dir)
    llm_config.set_active_config(llm_cfg, cfg.config_dir)


def _import_clean():
    """延迟 import clean_markdown,把 vendor 目录加 sys.path。

    vendor 脚本是独立模块(有 __main__ 块),非 app 包子模块,
    需把 vendor 目录加 sys.path 才能 import clean_markdown。
    """
    vendor_dir = str(Path(__file__).resolve().parent.parent / "vendor")
    if vendor_dir not in sys.path:
        sys.path.insert(0, vendor_dir)
    from clean_markdown import clean  # type: ignore  # noqa: E402
    return clean


def run_clean(job: IngestJob, prev_result: dict[str, Any], app_cfg: AppConfig) -> dict[str, Any]:
    """执行清洗阶段。返回 {stats, merged_path}。

    prev_result: extract 阶段返回的 dict(含 merged_path)。
    app_cfg: AppConfig(注入 LLM 配置供伪标题 LLM 识别用)。
    失败时抛 Exception。
    """
    update_job(job.job_id, current_stage="clean")
    append_log(job.job_id, "[clean] start")

    _inject_llm_config(app_cfg)
    clean = _import_clean()

    merged_path = Path(prev_result["merged_path"])
    if not merged_path.exists():
        raise FileNotFoundError(f"merged not found: {merged_path}")

    content = merged_path.read_text(encoding="utf-8")
    cleaned, stats = clean(content)
    merged_path.write_text(cleaned, encoding="utf-8")

    total = sum(v for v in stats.values() if isinstance(v, int))
    append_log(job.job_id, f"[clean] done: fixes={total} pseudo_promoted={stats.get('pseudo_promoted', 0)}")
    return {
        "merged_path": str(merged_path),
        "clean_stats": stats,
        "clean_fixes": total,
    }


def run_reclean(job: IngestJob, app_cfg: AppConfig) -> dict[str, Any]:
    """对已入库书重跑 clean(修复伪标题等)。

    直接对 content/ 下的正文文件跑 clean(),写回原文件。
    book/paper: content/{books|papers}/<slug>/book.zh.md(不存在则 book.md)
    note: content/notes/<slug>.md
    返回 {cleaned_path, clean_stats}。
    """
    update_job(job.job_id, current_stage="reclean")
    append_log(job.job_id, "[reclean] start")

    _inject_llm_config(app_cfg)
    clean = _import_clean()

    if job.doc_type == "note":
        target = Path(app_cfg.content_dir) / "notes" / f"{job.slug}.md"
    else:
        subdir = "books" if job.doc_type == "book" else "papers"
        target_dir = Path(app_cfg.content_dir) / subdir / job.slug
        target = target_dir / "book.zh.md"
        if not target.exists():
            target = target_dir / "book.md"
    if not target.exists():
        raise FileNotFoundError(f"content file not found: {target}")

    append_log(job.job_id, f"[reclean] target: {target}")
    content = target.read_text(encoding="utf-8")
    cleaned, stats = clean(content)
    target.write_text(cleaned, encoding="utf-8")

    total = sum(v for v in stats.values() if isinstance(v, int))
    append_log(job.job_id, f"[reclean] done: fixes={total} pseudo_promoted={stats.get('pseudo_promoted', 0)}")
    return {
        "cleaned_path": str(target),
        "clean_stats": stats,
        "clean_fixes": total,
    }
