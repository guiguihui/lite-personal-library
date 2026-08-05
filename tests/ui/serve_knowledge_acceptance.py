"""Serve an isolated knowledge-linking fixture for manual UI acceptance."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import uvicorn

from app.config.schema import AppConfig
from app.http.server import create_app
from app.index.builder import build_full


FIXTURES = {
    "notes/alpha.md": """---
id: note:alpha
title: 双向链接验收笔记
aliases: [Alpha Note]
status: reviewed
reviewed_at: 2026-08-02
source: manual-acceptance
confidence: 0.95
---

# 双向链接验收笔记

这篇笔记引用了 [[paper:graph-paper|Quartz 图谱论文]]，并与
[[note:beta|知识治理笔记]] 形成双向链接。

## Wikilink 状态

- 已解析：[[book:knowledge-book|知识链接实践手册]]
- 带锚点：[[paper:graph-paper#局部图谱|论文的局部图谱章节]]
- 断链：[[note:not-found|尚未收录的笔记]]
- 行内代码不应转换：`[[note:beta]]`

普通 Markdown 站内链接也会进入链接索引：[知识治理笔记](beta.md)。
""",
    "notes/beta.md": """---
id: note:beta
title: 知识治理笔记
status: reviewed
reviewed_at: 2026-08-01
source: research-notes
confidence: 0.82
---

# 知识治理笔记

治理字段让内容生命周期可以被追踪。返回 [[note:alpha|双向链接验收笔记]]，
并参考 [[paper:graph-paper|Quartz 图谱论文]]。
""",
    "notes/gamma.md": """---
id: note:gamma
title: 图谱扩展笔记
status: draft
reviewed_at: 2026-07-30
source: field-note
confidence: 0.68
---

# 图谱扩展笔记

这条单向链接用于扩展一跳邻居：[[note:alpha|双向链接验收笔记]]。
""",
    "papers/graph-paper/_index.md": """---
id: paper:graph-paper
title: Quartz 图谱论文
authors: [Quartz Research Group]
status: reviewed
reviewed_at: 2026-08-02
source: doi:10.0000/quartz.acceptance
confidence: 0.99
---

# Quartz 图谱论文

论文反向引用 [[note:alpha|双向链接验收笔记]]。

## 局部图谱

局部一跳图谱只展示当前文档及其直接邻居，并区分入边和出边。
""",
    "books/knowledge-book/_index.md": """---
id: book:knowledge-book
title: 知识链接实践手册
author: Codex Acceptance Team
status: archived
reviewed_at: 2026-08-02
source: local-fixture
confidence: 0.91
---

# 知识链接实践手册

本书链接到 [[note:alpha|双向链接验收笔记]]，用于验证跨馆藏反链。
""",
}


def _write_fixture(content_dir: Path) -> None:
    for relative_path, markdown in FIXTURES.items():
        target = content_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="lqd-knowledge-acceptance-") as temp:
        root = Path(temp)
        content_dir = root / "content"
        pageindex_dir = root / "pageindex"
        config_dir = root / "config"
        pdfs_dir = root / "pdfs"
        config_dir.mkdir()
        pdfs_dir.mkdir()
        _write_fixture(content_dir)

        result = build_full(str(content_dir), str(pageindex_dir))
        if not result.ok:
            raise RuntimeError(f"fixture index build failed: {result.error}\n{result.log}")

        config = AppConfig(
            content_dir=str(content_dir),
            pageindex_dir=str(pageindex_dir),
            config_dir=str(config_dir),
            pdfs_dir=str(pdfs_dir),
            http_host=args.host,
            http_port=args.port,
        )
        uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
