"""发布适配器:把入库产物搬到 content/{books|papers|notes}/<slug>/

让 library 可见。build_pageindex 的 process_book/process_paper 强制要求
_index.md(否则文档被跳过),所以 book/paper 模式必须生成 _index.md。

搬运规则:
  book:  content/books/<slug>/_index.md(生成) + book.zh.md(搬) + images/(搬)
  paper: content/papers/<slug>/_index.md(note 阶段产出则搬,否则生成含正文)
  note:  content/notes/<slug>.md(单文件,搬 book.zh.md 重命名)

幂等:目标目录已存在时覆盖文件,不删目录(避免误删)。_index.md 每次重写
(front matter 可能随 job.title 变)。
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path
from typing import Any

from app.config.schema import AppConfig
from app.ingest.jobs import IngestJob, append_log, update_job

_DOC_TYPE_SUBDIR = {"book": "books", "paper": "papers", "note": "notes"}


def _doc_type_to_subdir(doc_type: str) -> str:
    """book→books, paper→papers, note→notes。未知降级 notes。"""
    return _DOC_TYPE_SUBDIR.get(doc_type, "notes")


def _build_front_matter(job: IngestJob, prev_result: dict[str, Any]) -> str:
    """从 job + prev_result 构造 YAML front matter。

    title 优先级:job.title(用户自定义) > prev_result["title"](extract metadata) > slug。
    """
    title = job.title or prev_result.get("title") or job.slug
    author = job.author or prev_result.get("author") or ""
    tags = list(job.tags) if job.tags else []
    date = datetime.date.today().isoformat()

    # 简单 YAML 转义:title/author 可能含特殊字符,用双引号包裹 + 转义内部引号
    def yaml_str(s: str) -> str:
        return '"' + str(s).replace('"', '\\"') + '"'

    lines = ["---", f"title: {yaml_str(title)}"]
    if author:
        lines.append(f"author: {yaml_str(author)}")
    lines.append(f"date: {date}")
    if tags:
        tag_str = ", ".join(yaml_str(t) for t in tags)
        lines.append(f"tags: [{tag_str}]")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _resolve_source_md(prev_result: dict[str, Any]) -> Path | None:
    """从 prev_result 拿要发布的正文文件(translated_path 优先,无则 merged_path)。"""
    for key in ("translated_path", "merged_path", "validated_path"):
        p = prev_result.get(key)
        if p and Path(p).exists():
            return Path(p)
    return None


def run_publish(job: IngestJob, prev_result: dict[str, Any], app_cfg: AppConfig) -> dict[str, Any]:
    """执行发布阶段。返回 {published_path, content_slug_dir, index_md_path}。

    失败时抛 Exception(pipeline 捕获标 failed)。
    """
    update_job(job.job_id, current_stage="publish")
    append_log(job.job_id, "[publish] start")

    source_md = _resolve_source_md(prev_result)
    if source_md is None:
        raise FileNotFoundError(
            f"publish: no source md in prev_result (keys: {list(prev_result)})"
        )

    subdir = _doc_type_to_subdir(job.doc_type)
    content_root = Path(app_cfg.content_dir)

    if job.doc_type == "note":
        # note:单文件 content/notes/<slug>.md,无 _index.md
        target_dir = content_root / "notes"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{job.slug}.md"
        shutil.copy2(source_md, target)
        append_log(job.job_id, f"[publish] done: {target}")
        return {
            "published_path": str(target),
            "content_slug_dir": str(target_dir),
            "index_md_path": None,
        }

    # book / paper:目标目录 content/<subdir>/<slug>/
    target_dir = content_root / subdir / job.slug
    target_dir.mkdir(parents=True, exist_ok=True)

    # 搬正文(book.zh.md 或 book.md)
    target_md = target_dir / source_md.name
    shutil.copy2(source_md, target_md)

    # 搬 images/(若 extract 产出且非空)
    images_dir = prev_result.get("images_dir")
    if images_dir and Path(images_dir).is_dir() and any(Path(images_dir).iterdir()):
        target_images = target_dir / "images"
        shutil.copytree(images_dir, target_images, dirs_exist_ok=True)
        append_log(job.job_id, f"[publish] images: {target_images}")

    # 生成/搬运 _index.md
    index_path = target_dir / "_index.md"
    _write_index_md(job, prev_result, source_md, index_path)

    append_log(job.job_id, f"[publish] done: {target_dir}")
    return {
        "published_path": str(target_md),
        "content_slug_dir": str(target_dir),
        "index_md_path": str(index_path),
    }


def _write_index_md(
    job: IngestJob,
    prev_result: dict[str, Any],
    source_md: Path,
    index_path: Path,
) -> None:
    """写 _index.md(book/paper)。

    paper:若 note 阶段已产出 _index.md(在 source_md 同目录),直接搬;否则
           生成 front matter + book.zh.md 正文(paper 的 _index.md 本身是正文)。
    book:生成 front matter + 空(book.zh.md 作为章节提供正文)。
    """
    # note 阶段产出的 _index.md(paper,在 merged/ 目录)
    note_index = source_md.parent / "_index.md"
    if note_index.exists():
        shutil.copy2(note_index, index_path)
        return

    front_matter = _build_front_matter(job, prev_result)
    if job.doc_type == "paper":
        # paper:_index.md 本身是正文,拼 front matter + 全文
        body = source_md.read_text(encoding="utf-8")
        index_path.write_text(front_matter + "\n" + body, encoding="utf-8")
    else:
        # book:_index.md 只放 front matter,正文在 book.zh.md
        index_path.write_text(front_matter, encoding="utf-8")
