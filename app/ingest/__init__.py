"""入库流水线(阶段 5)。

模块组织(零耦合,依赖单向向下):
  jobs              — IngestJob dataclass + 进程内任务表(线程安全)
  extract_adapter   — 调 app.pdf.factory,产出 merged/book.md + images/
  clean_adapter     — 调 vendor.clean_markdown(subprocess)
  translate_adapter — 调 vendor.translate_chapters(先注入 llm_config)
  validate_adapter  — 调 vendor.validate_book(import validate_file)
  note_adapter      — 调 vendor.generate_paper_note(paper only,subprocess)
  pipeline          — run_pipeline(job): 按 stages 顺序执行,更新 job 状态

流水线阶段(按 doc_type 选择):
  book:  extract → clean → translate → validate
  paper: extract → clean → translate → validate → note
  note:  extract → clean → validate
"""

from __future__ import annotations

from app.ingest.jobs import (
    IngestJob,
    create_job,
    get_job,
    list_jobs,
    update_job,
)
from app.ingest.pipeline import run_pipeline

__all__ = [
    "IngestJob",
    "create_job",
    "get_job",
    "list_jobs",
    "update_job",
    "run_pipeline",
]
