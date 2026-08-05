"""翻译适配器:先注入 llm_config,再 import 调 vendor.translate_chapters。

关键:translate_chapters.py L37-39 在 import 时调用 get_tier("strong"),
所以必须在 import translate_chapters 之前先 llm_config.set_active_config()。

调用 translate_book/translate_single(asyncio.run),不走 subprocess。
理由:同进程可注入 app config(subprocess 无法注入 keyring)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from app.config.schema import AppConfig, LlmConfig
from app.config.store import load_llm_config
from app.ingest.jobs import IngestJob, append_log, update_job


def _inject_llm_config(cfg: AppConfig) -> None:
    """注入 app LlmConfig 到 vendor.llm_config。

    必须在 import translate_chapters 之前调用(translate_chapters 在
    import 时就读 get_tier)。
    """
    vendor_dir = str(Path(__file__).resolve().parent.parent / "vendor")
    if vendor_dir not in sys.path:
        sys.path.insert(0, vendor_dir)
    import llm_config  # type: ignore  # noqa: E402

    llm_cfg: LlmConfig = load_llm_config(cfg.config_dir)
    llm_config.set_active_config(llm_cfg, cfg.config_dir)


def _import_translate():
    """import translate_chapters(此时 llm_config 已注入)。

    返回 (translate_book, translate_single)。
    """
    vendor_dir = str(Path(__file__).resolve().parent.parent / "vendor")
    if vendor_dir not in sys.path:
        sys.path.insert(0, vendor_dir)
    from translate_chapters import translate_book, translate_single  # type: ignore  # noqa: E402
    return translate_book, translate_single


def run_translate(
    job: IngestJob,
    prev_result: dict[str, Any],
    app_cfg: AppConfig,
) -> dict[str, Any]:
    """执行翻译阶段。返回 {translated_path, stats}。

    prev_result: clean 阶段返回的 dict(含 merged_path)。
    若 prev_result 为空(单阶段运行 translate),从 pdfs_dir/slug/merged/book.md 读。
    book:翻译整个目录(merged/ 下的 book.md)
    paper:翻译单文件(merged/book.md → .zh.md)
    失败时抛 Exception。
    """
    update_job(job.job_id, current_stage="translate")
    append_log(job.job_id, "[translate] start: injecting llm_config")

    _inject_llm_config(app_cfg)
    translate_book, translate_single = _import_translate()

    # 优先用 prev_result 的 merged_path,否则从 pdfs_dir/slug/ 读
    merged_path = Path(prev_result["merged_path"]) if prev_result.get("merged_path") else None
    if merged_path is None or not merged_path.exists():
        merged_path = Path(app_cfg.pdfs_dir) / job.slug / "merged" / "book.md"
    if not merged_path.exists():
        raise FileNotFoundError(f"merged not found: {merged_path}")

    append_log(job.job_id, f"[translate] target: {merged_path}")

    # paper:单文件翻译(产出 .zh.md);book:也走单文件(merged 是单文件)
    # translate_single 产出 {path}.zh.md(in_place=False 默认)
    # on_progress:每个 chunk 完成后追加进度到 job.log,前端 manage 页可见。
    #   断点续跑由 translate_chapter 的 partial marker 机制保证(detect_partial)。
    def on_progress(done: int, total: int) -> None:
        append_log(job.job_id, f"[translate] {done}/{total} chunks")

    rc = asyncio.run(translate_single(str(merged_path), max_retry=2, on_progress=on_progress))
    if rc != 0:
        raise RuntimeError(f"translate_single exit {rc}")

    zh_path = merged_path.with_suffix(".zh.md")
    # 检测中文跳过:book.zh.md 与 book.md 内容相同 = is_chinese_text 命中跳过
    # (translate_chapters 对中文文档直接 copy2,不调 LLM)。给用户明确反馈。
    if zh_path.exists() and _files_identical(merged_path, zh_path):
        append_log(job.job_id, "[translate] skipped: 已是中文，跳过翻译")
    else:
        append_log(job.job_id, f"[translate] done: {zh_path}")
    return {
        "translated_path": str(zh_path),
        "merged_path": str(merged_path),
    }


def _files_identical(a: Path, b: Path) -> bool:
    """比较两文件内容是否相同(用于检测中文跳过翻译)。"""
    import filecmp

    try:
        return filecmp.cmp(str(a), str(b), shallow=False)
    except OSError:
        return False
