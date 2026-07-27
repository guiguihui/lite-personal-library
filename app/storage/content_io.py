"""content/ 文件 IO。

read_markdown_body_lines 对齐 chat.js fetchMdLines(L394):
  - 读 md 原文
  - 剥离 front matter(---\n...\n---\n)
  - 按 \n split 返回 list[str]
这样 chat.js fetchMdSection 的 line_num/line_end 切片与后端一致。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config.schema import AppConfig
from app.storage.paths import resolve_content_path

# front matter 正则,对齐 chat.js L404: /^---\n[\s\S]*?\n---\n/
_FM_RE = re.compile(r"^---\n[\s\S]*?\n---\n", re.MULTILINE)


def read_markdown(rel_path: str, cfg: AppConfig) -> str:
    """读 md 原文(含 front matter)。"""
    path = resolve_content_path(rel_path, cfg)
    if not path.is_file():
        raise FileNotFoundError(rel_path)
    return path.read_text(encoding="utf-8")


def read_markdown_body_lines(rel_path: str, cfg: AppConfig) -> list[str]:
    """读 md 原文,剥 front matter,按 \n split。

    对齐 chat.js fetchMdLines(L394-407),line_num/line_end 切片基于此返回。
    """
    text = read_markdown(rel_path, cfg)
    body = _FM_RE.sub("", text, count=1)
    return body.split("\n")


def read_markdown_section(rel_path: str, line_num: int, line_end: int, cfg: AppConfig) -> str:
    """按行号区间取正文(line_num 到 line_end)。

    对齐 chat.js fetchMdSection(L414-419)。
    """
    lines = read_markdown_body_lines(rel_path, cfg)
    start = line_num or 0
    end = line_end or len(lines)
    return "\n".join(lines[start:end]).strip()


def write_markdown(rel_path: str, content: str, cfg: AppConfig) -> Path:
    """写 md 文件(自动建父目录)。"""
    path = resolve_content_path(rel_path, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def list_docs(doc_type: str, cfg: AppConfig) -> list[str]:
    """列 books/papers/notes 下的 slug。

    books/papers: 子目录(slug);notes: 单文件(slug.md,无扩展名)。
    """
    root = Path(cfg.content_dir) / doc_type
    if not root.is_dir():
        return []
    if doc_type == "notes":
        # notes 是扁平 .md
        return sorted(p.stem for p in root.glob("*.md") if p.stem != "_index")
    # books/papers 是子目录
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_") and not p.name.startswith("."))
