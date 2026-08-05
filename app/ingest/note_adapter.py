"""笔记适配器:调 vendor.generate_paper_note(paper only,subprocess)。

generate_paper_note.py 尚未从 yuulibrary-main 复制(阶段 5 时 vendor 目录
只有 8 个脚本)。此 adapter 用 subprocess 调用,若脚本不存在则跳过
(返回 skipped=True),不阻断流水线。

paper 专用:ReAct 7 栏目结构化分析 + cross-link + 组装 _index.md。
依赖 llm_config(strong tier, MAX_TOKENS=16384)。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from app.config.schema import AppConfig
from app.ingest.jobs import IngestJob, append_log, update_job


def _inject_llm_config_for_subprocess(app_cfg: AppConfig) -> None:
    """注入 app LlmConfig 到 vendor.llm_config(供 generate_paper_note import 时读)。

    generate_paper_note 在 import 时读 get_tier("strong"),
    必须在 subprocess 启动前注入。但 subprocess 是独立进程,无法注入。

    解决:generate_paper_note.py 若存在,它自己 import llm_config 并读 config.yaml/.env
    (legacy 路径)。app config 注入只对同进程 import 有效。
    此处保留函数供未来同进程调用用,subprocess 路径走 legacy。
    """
    # subprocess 路径:generate_paper_note.py 自己读 config.yaml/.env(legacy)
    # 未来若改同进程 import,需先注入再 import。
    pass


def run_note(
    job: IngestJob,
    prev_result: dict[str, Any],
    app_cfg: AppConfig,
) -> dict[str, Any]:
    """执行笔记生成阶段(paper only)。返回 {note_path, skipped}。

    prev_result: validate 阶段返回的 dict(含 validated_path)。
    脚本不存在时返回 skipped=True,不阻断。
    """
    update_job(job.job_id, current_stage="note")
    append_log(job.job_id, "[note] start")

    script_path = Path(__file__).resolve().parent.parent / "vendor" / "generate_paper_note.py"
    if not script_path.exists():
        append_log(job.job_id, "[note] skipped: generate_paper_note.py not found")
        return {
            "skipped": True,
            "reason": "generate_paper_note.py not in vendor/",
            "validated_path": prev_result.get("validated_path", ""),
        }

    target = None
    if prev_result.get("translated_path"):
        target = Path(prev_result["translated_path"])
    elif prev_result.get("validated_path"):
        target = Path(prev_result["validated_path"])
    elif app_cfg is not None:
        target = Path(app_cfg.pdfs_dir) / job.slug / "merged" / "book.zh.md"
    if target is None or not target.exists():
        raise FileNotFoundError(f"note target not found: {target}")

    # subprocess 调用(隔离,generate_paper_note 有 __main__ 块 + argparse)
    cmd = [sys.executable, str(script_path), str(target)]
    append_log(job.job_id, f"[note] run: {' '.join(cmd)}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 分钟
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("generate_paper_note timeout (600s)")
    except Exception as exc:
        raise RuntimeError(f"generate_paper_note failed: {exc}")

    if proc.returncode != 0:
        append_log(job.job_id, f"[note] exit={proc.returncode}")
        append_log(job.job_id, f"[note] stderr: {proc.stderr[:500]}")
        raise RuntimeError(f"generate_paper_note exit {proc.returncode}")

    # 产出 _index.md 在同目录
    note_path = target.parent / "_index.md"
    append_log(job.job_id, f"[note] done: {note_path}")
    return {
        "note_path": str(note_path),
        "skipped": False,
        "stdout": proc.stdout[-500:] if proc.stdout else "",
    }
