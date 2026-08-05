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
from app.ingest.publish_adapter import run_publish
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
                prev_result = run_clean(job, prev_result, app_cfg)
            elif stage == "translate":
                prev_result = run_translate(job, prev_result, app_cfg)
            elif stage == "validate":
                prev_result = run_validate(job, prev_result, app_cfg)
            elif stage == "note":
                prev_result = run_note(job, prev_result, app_cfg)
            else:
                raise ValueError(f"unknown stage: {stage}")

            final_result["stages"][stage] = prev_result

        # ── 收尾:publish + 触发增量索引构建(不在 stages 里,必做) ────────
        # publish 把产物搬到 content/,build 让 library 可见。
        # build 用 start_build 后台触发(不阻塞 pipeline;build 耗时 30s-2min,
        # 不该让 ingest job 卡在 running)。build 触发失败只记 log(辅助步骤,
        # 用户可手动在 manage 页重试)。
        update_job(job_id, current_stage="publish")
        append_log(job_id, "[pipeline] >>> stage: publish")
        publish_result = run_publish(job, prev_result, app_cfg)
        final_result["stages"]["publish"] = publish_result
        final_result["published_path"] = publish_result.get("published_path")

        try:
            from app.index.status import start_build

            build_job_id = start_build(
                mode="incremental",
                content_dir=app_cfg.content_dir,
                pageindex_dir=app_cfg.pageindex_dir,
                llm_model="",
            )
            final_result["build_job_id"] = build_job_id
            append_log(job_id, f"[pipeline] build triggered: {build_job_id}")
        except Exception as build_exc:
            # build 触发失败不阻断入库(publish 已成功,产物在 content/)
            append_log(job_id, f"[pipeline] build trigger failed: {build_exc}")
            final_result["build_error"] = str(build_exc)

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
