"""入库流水线编排:按 job.stages 顺序执行各 adapter,更新 job 状态。

run_pipeline(job_id, app_config) -> None
  在后台线程跑,异常捕获写 log + 标 failed。
  每阶段产物 dict 传给下一阶段(prev_result 链式传递)。

阶段路由:
  extract   → extract_adapter.run_extract(job, pdfs_dir)
  clean     → clean_adapter.run_clean(job, prev_result)
  translate → translate_adapter.run_translate(job, prev_result, app_cfg)
  validate  → validate_adapter.run_validate(job, prev_result)
  note      → note_adapter.run_note(job, prev_result, app_cfg)
"""

from __future__ import annotations

import time
import traceback
from typing import Any

from app.config.schema import AppConfig
from app.ingest.clean_adapter import run_clean
from app.ingest.extract_adapter import run_extract
from app.ingest.jobs import IngestJob, append_log, get_job, update_job
from app.ingest.note_adapter import run_note
from app.ingest.translate_adapter import run_translate
from app.ingest.validate_adapter import run_validate


def run_pipeline(job_id: str, app_cfg: AppConfig) -> None:
    """后台线程:执行入库流水线,逐步更新 job 状态。

    job_id:     IngestJob.job_id
    app_cfg:    AppConfig(含 pdfs_dir, config_dir 等)
    """
    job = get_job(job_id)
    if job is None:
        return

    start = time.time()
    update_job(job_id, status="running", current_stage="starting")
    append_log(job_id, f"[pipeline] start: stages={list(job.stages)}")

    prev_result: dict[str, Any] = {}
    final_result: dict[str, Any] = {"stages": {}}

    try:
        for stage in job.stages:
            update_job(job_id, current_stage=stage)
            append_log(job_id, f"[pipeline] >>> stage: {stage}")

            if stage == "extract":
                prev_result = run_extract(job, app_cfg.pdfs_dir)
            elif stage == "clean":
                prev_result = run_clean(job, prev_result)
            elif stage == "translate":
                prev_result = run_translate(job, prev_result, app_cfg)
            elif stage == "validate":
                prev_result = run_validate(job, prev_result, app_cfg)
            elif stage == "note":
                prev_result = run_note(job, prev_result, app_cfg)
            else:
                raise ValueError(f"unknown stage: {stage}")

            final_result["stages"][stage] = prev_result

        update_job(
            job_id,
            status="done",
            current_stage="done",
            result=final_result,
        )
        append_log(
            job_id,
            f"[pipeline] done: {time.time() - start:.1f}s",
        )
    except Exception as exc:
        tb = traceback.format_exc()
        append_log(job_id, f"[pipeline error] {type(exc).__name__}: {exc}")
        append_log(job_id, f"[pipeline traceback]\n{tb}")
        update_job(
            job_id,
            status="failed",
            current_stage="failed",
            result={"stages": final_result.get("stages", {}), "error": str(exc)},
        )
