"""清洗适配器:调 vendor.clean_markdown.clean() import 调用。

clean_markdown.clean(content) -> (cleaned_content, stats_dict)
原地写回 merged/book.md。import 调用(同进程),不走 subprocess。

理由:clean() 是纯函数(无副作用,不读 argv/env),import 调用安全。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from app.ingest.jobs import IngestJob, append_log, update_job


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


def run_clean(job: IngestJob, prev_result: dict[str, Any]) -> dict[str, Any]:
    """执行清洗阶段。返回 {stats, merged_path}。

    prev_result: extract 阶段返回的 dict(含 merged_path)。
    失败时抛 Exception。
    """
    update_job(job.job_id, current_stage="clean")
    append_log(job.job_id, "[clean] start")

    merged_path = Path(prev_result["merged_path"])
    if not merged_path.exists():
        raise FileNotFoundError(f"merged not found: {merged_path}")

    clean = _import_clean()
    content = merged_path.read_text(encoding="utf-8")
    cleaned, stats = clean(content)
    merged_path.write_text(cleaned, encoding="utf-8")

    total = sum(v for v in stats.values() if isinstance(v, int))
    append_log(job.job_id, f"[clean] done: fixes={total}")
    return {
        "merged_path": str(merged_path),
        "clean_stats": stats,
        "clean_fixes": total,
    }
