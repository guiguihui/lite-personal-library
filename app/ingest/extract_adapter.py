"""提取适配器:调 app.pdf.factory,产出 merged/book.md + images/。

import 调用(同进程),不走 subprocess。理由:
  1. 同进程可返回结构化 ExtractResult/异常
  2. 长任务异步化由 app.ingest.pipeline 在后台线程跑
  3. PyInstaller 打包时少一层外部进程依赖

工作目录布局(对齐 docs/architecture.md L222):
  {pdfs_dir}/{slug}/
    merged/book.md
    images/
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.pdf.factory import make_extractor
from app.ingest.jobs import IngestJob, append_log, update_job


def run_extract(job: IngestJob, pdfs_dir: str) -> dict[str, Any]:
    """执行提取阶段。返回 {merged_path, images_dir, page_count, title}。

    失败时抛 Exception(pipeline 捕获写 log + 标 failed)。
    """
    update_job(job.job_id, current_stage="extract")
    append_log(job.job_id, f"[extract] start: {job.input_pdf}")

    input_path = Path(job.input_pdf)
    if not input_path.is_absolute():
        # 相对路径:相对于 pdfs_dir 解析
        input_path = Path(pdfs_dir) / input_path
    if not input_path.exists():
        raise FileNotFoundError(f"input not found: {input_path}")

    out_dir = Path(pdfs_dir) / job.slug
    out_dir.mkdir(parents=True, exist_ok=True)

    strategy = job.strategy or "local"
    extractor = make_extractor(input_path.name, strategy)
    result = extractor.extract(input_path, out_dir, job.pages)

    for line in result.log:
        append_log(job.job_id, f"  {line}")

    if not result.ok:
        raise RuntimeError(result.error or "extract failed")

    append_log(
        job.job_id,
        f"[extract] done: merged={result.merged_path} pages={result.page_count}",
    )
    return {
        "merged_path": str(result.merged_path),
        "images_dir": str(result.images_dir),
        "page_count": result.page_count,
        "title": result.title,
        "author": result.author,
        "source_format": result.source_format,
        "duration_sec": result.duration_sec,
    }
