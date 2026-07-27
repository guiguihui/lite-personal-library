"""验证适配器:import 调 vendor.validate_book.validate_file。

validate_file(path, all_files) -> [(level, msg)] level=ERR/WARN/REVIEW
import 调用(同进程),不走 subprocess。

理由:validate_file 是纯函数(无副作用,不读 argv/env),import 调用安全。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.ingest.jobs import IngestJob, append_log, update_job

if TYPE_CHECKING:
    from app.config.schema import AppConfig

# validate_book 的 level 常量(0=ERR, 1=WARN, 2=REVIEW)
_LEVEL_NAMES = {0: "ERR", 1: "WARN", 2: "REVIEW"}


def _import_validate():
    """延迟 import validate_book,把 vendor 目录加 sys.path。"""
    vendor_dir = str(Path(__file__).resolve().parent.parent / "vendor")
    if vendor_dir not in sys.path:
        sys.path.insert(0, vendor_dir)
    from validate_book import validate_file, ERR, WARN, REVIEW  # type: ignore  # noqa: E402
    return validate_file, (ERR, WARN, REVIEW)


def run_validate(
    job: IngestJob,
    prev_result: dict[str, Any],
    app_cfg: "AppConfig" = None,
) -> dict[str, Any]:
    """执行验证阶段。返回 {issues, error_count, warn_count, review_count}。

    prev_result: translate 阶段返回的 dict(含 translated_path)或
                 clean 阶段(无翻译时,含 merged_path)。
    若 prev_result 为空(单阶段运行 validate),从 pdfs_dir/slug/ 读。
    失败时抛 Exception。
    """
    update_job(job.job_id, current_stage="validate")
    append_log(job.job_id, "[validate] start")

    validate_file, (ERR, WARN, REVIEW) = _import_validate()

    # 优先验证翻译后的文件,无翻译则验证清洗后的 merged
    target = None
    if prev_result.get("translated_path"):
        target = Path(prev_result["translated_path"])
    elif prev_result.get("merged_path"):
        target = Path(prev_result["merged_path"])
    elif app_cfg is not None:
        # 单阶段运行:从 pdfs_dir/slug/merged/book.md 读
        target = Path(app_cfg.pdfs_dir) / job.slug / "merged" / "book.md"
    if target is None or not target.exists():
        raise FileNotFoundError(f"validate target not found: {target}")

    issues = validate_file(str(target))
    err_count = sum(1 for lvl, _ in issues if lvl == ERR)
    warn_count = sum(1 for lvl, _ in issues if lvl == WARN)
    review_count = sum(1 for lvl, _ in issues if lvl == REVIEW)

    for lvl, msg in issues:
        name = _LEVEL_NAMES.get(lvl, str(lvl))
        append_log(job.job_id, f"  [{name}] {msg}")

    append_log(
        job.job_id,
        f"[validate] done: errors={err_count} warns={warn_count} reviews={review_count}",
    )
    return {
        "validated_path": str(target),
        "issues": [{"level": _LEVEL_NAMES.get(lvl, str(lvl)), "msg": msg} for lvl, msg in issues],
        "error_count": err_count,
        "warn_count": warn_count,
        "review_count": review_count,
    }
