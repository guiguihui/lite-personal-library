"""pytest 单元测试:发布适配器(app/ingest/publish_adapter.py)。

覆盖:
  - book:搬 book.zh.md + 生成 _index.md(front matter 含 title/author/tags)
  - paper:_index.md 含正文(无 note 产出时)
  - paper:note 阶段已产出 _index.md 时直接搬
  - note:单文件 content/notes/<slug>.md,无 _index.md
  - images 搬运
  - 幂等:重跑覆盖,不报错
  - 缺源文件抛 FileNotFoundError
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.schema import AppConfig
from app.ingest.jobs import IngestJob
from app.ingest.publish_adapter import _doc_type_to_subdir, run_publish


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════


def _make_cfg(tmp_path: Path) -> AppConfig:
    """隔离的 AppConfig,content_dir 在 tmp_path 下。"""
    for sub in ("content", "pageindex", "config", "pdfs"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return AppConfig(
        content_dir=str(tmp_path / "content"),
        pageindex_dir=str(tmp_path / "pageindex"),
        config_dir=str(tmp_path / "config"),
        pdfs_dir=str(tmp_path / "pdfs"),
    )


def _make_job(
    job_id: str = "ing_pub",
    doc_type: str = "book",
    slug: str = "test-slug",
    title: str = "",
    author: str = "",
    tags: tuple[str, ...] = (),
) -> IngestJob:
    return IngestJob(
        job_id=job_id,
        status="running",
        started_at=0,
        doc_type=doc_type,
        slug=slug,
        title=title,
        author=author,
        tags=tags,
    )


def _write_source(tmp_path: Path, name: str = "book.zh.md", body: str = "# Ch1\n\n正文\n") -> Path:
    """在 pdfs/<slug>/merged/ 下写源文件。"""
    src = tmp_path / "pdfs" / "test-slug" / "merged" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    return src


# ══════════════════════════════════════════════════════════════════════════
# _doc_type_to_subdir
# ══════════════════════════════════════════════════════════════════════════


class TestDocTypeSubdir:
    def test_mapping(self) -> None:
        assert _doc_type_to_subdir("book") == "books"
        assert _doc_type_to_subdir("paper") == "papers"
        assert _doc_type_to_subdir("note") == "notes"

    def test_unknown_falls_back_to_notes(self) -> None:
        assert _doc_type_to_subdir("unknown") == "notes"


# ══════════════════════════════════════════════════════════════════════════
# book 模式
# ══════════════════════════════════════════════════════════════════════════


class TestPublishBook:
    def test_copies_book_zh_and_creates_index(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        src = _write_source(tmp_path, "book.zh.md", "# Ch1\n\n正文\n")
        job = _make_job(doc_type="book", title="测试书", author="张三", tags=("api",))
        prev = {"translated_path": str(src), "merged_path": str(src)}

        result = run_publish(job, prev, cfg)

        target_dir = Path(result["content_slug_dir"])
        assert (target_dir / "book.zh.md").exists()
        assert (target_dir / "_index.md").exists()
        # front matter 含 title/author/tags
        idx = (target_dir / "_index.md").read_text(encoding="utf-8")
        assert "title: " in idx
        assert "测试书" in idx
        assert "张三" in idx
        assert "api" in idx
        # book 的 _index.md 不含正文(正文在 book.zh.md)
        assert "正文" not in idx
        assert result["index_md_path"] is not None

    def test_title_falls_back_to_slug(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        src = _write_source(tmp_path)
        job = _make_job(doc_type="book", slug="my-slug", title="")
        prev = {"translated_path": str(src)}

        run_publish(job, prev, cfg)
        idx = (Path(cfg.content_dir) / "books" / "my-slug" / "_index.md").read_text(encoding="utf-8")
        assert "my-slug" in idx

    def test_uses_extracted_title_when_no_job_title(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        src = _write_source(tmp_path)
        job = _make_job(doc_type="book", title="")
        prev = {"translated_path": str(src), "title": "PDF 提取的标题"}
        run_publish(job, prev, cfg)
        idx = (Path(cfg.content_dir) / "books" / "test-slug" / "_index.md").read_text(encoding="utf-8")
        assert "PDF 提取的标题" in idx


# ══════════════════════════════════════════════════════════════════════════
# paper 模式
# ══════════════════════════════════════════════════════════════════════════


class TestPublishPaper:
    def test_generates_index_with_body_when_no_note(self, tmp_path: Path) -> None:
        """paper 无 note 产出:_index.md = front matter + 正文。"""
        cfg = _make_cfg(tmp_path)
        src = _write_source(tmp_path, "book.zh.md", "# Paper\n\n论文正文\n")
        job = _make_job(doc_type="paper", title="我的论文")
        prev = {"translated_path": str(src)}

        run_publish(job, prev, cfg)

        target_dir = Path(cfg.content_dir) / "papers" / "test-slug"
        assert (target_dir / "book.zh.md").exists()
        idx = (target_dir / "_index.md").read_text(encoding="utf-8")
        assert "title: " in idx
        assert "我的论文" in idx
        assert "论文正文" in idx  # paper 的 _index.md 含正文

    def test_copies_note_index_when_exists(self, tmp_path: Path) -> None:
        """paper + note 阶段已产出 _index.md:直接搬,不重新生成。"""
        cfg = _make_cfg(tmp_path)
        src = _write_source(tmp_path, "book.zh.md", "# Paper\n\n正文\n")
        # note 阶段产出的 _index.md(在 merged/ 目录)
        note_index = src.parent / "_index.md"
        note_index.write_text("---\ntitle: note 生成的\n---\n\nnote 正文\n", encoding="utf-8")
        job = _make_job(doc_type="paper", title="会被忽略")
        prev = {"translated_path": str(src)}

        run_publish(job, prev, cfg)

        idx = (Path(cfg.content_dir) / "papers" / "test-slug" / "_index.md").read_text(encoding="utf-8")
        assert "note 生成的" in idx
        assert "会被忽略" not in idx  # note 的 _index.md 优先,不重新生成


# ══════════════════════════════════════════════════════════════════════════
# note 模式
# ══════════════════════════════════════════════════════════════════════════


class TestPublishNote:
    def test_copies_single_file_no_index(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        src = _write_source(tmp_path, "book.zh.md", "# Note\n\n笔记正文\n")
        job = _make_job(doc_type="note", slug="my-note")
        prev = {"translated_path": str(src)}

        result = run_publish(job, prev, cfg)

        target = Path(cfg.content_dir) / "notes" / "my-note.md"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "# Note\n\n笔记正文\n"
        assert result["index_md_path"] is None  # note 无 _index.md


# ══════════════════════════════════════════════════════════════════════════
# images + 幂等 + 错误
# ══════════════════════════════════════════════════════════════════════════


class TestPublishImagesAndIdempotency:
    def test_copies_images(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        src = _write_source(tmp_path, "book.zh.md", "# Book\n")
        images = tmp_path / "pdfs" / "test-slug" / "images"
        images.mkdir(parents=True, exist_ok=True)
        (images / "fig1.webp").write_bytes(b"fake-image")
        job = _make_job(doc_type="book")
        prev = {"translated_path": str(src), "images_dir": str(images)}

        run_publish(job, prev, cfg)

        assert (Path(cfg.content_dir) / "books" / "test-slug" / "images" / "fig1.webp").exists()

    def test_skips_empty_images_dir(self, tmp_path: Path) -> None:
        """images_dir 存在但为空:不搬(避免空目录)。"""
        cfg = _make_cfg(tmp_path)
        src = _write_source(tmp_path, "book.zh.md", "# Book\n")
        images = tmp_path / "pdfs" / "test-slug" / "images"
        images.mkdir(parents=True, exist_ok=True)  # 空
        job = _make_job(doc_type="book")
        prev = {"translated_path": str(src), "images_dir": str(images)}

        run_publish(job, prev, cfg)

        assert not (Path(cfg.content_dir) / "books" / "test-slug" / "images").exists()

    def test_idempotent_overwrites(self, tmp_path: Path) -> None:
        """重跑 publish:文件被覆盖,不报错。"""
        cfg = _make_cfg(tmp_path)
        src = _write_source(tmp_path, "book.zh.md", "# V1\n")
        job = _make_job(doc_type="book", title="第一版")
        prev = {"translated_path": str(src)}

        run_publish(job, prev, cfg)
        # 第二次,改 title + 源内容
        src.write_text("# V2\n", encoding="utf-8")
        job2 = _make_job(doc_type="book", title="第二版")
        run_publish(job2, prev, cfg)

        target_dir = Path(cfg.content_dir) / "books" / "test-slug"
        assert (target_dir / "book.zh.md").read_text(encoding="utf-8") == "# V2\n"
        idx = (target_dir / "_index.md").read_text(encoding="utf-8")
        assert "第二版" in idx


class TestPublishErrors:
    def test_missing_source_raises(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        job = _make_job(doc_type="book")
        prev = {"translated_path": str(tmp_path / "nonexistent.md")}
        with pytest.raises(FileNotFoundError, match="no source md"):
            run_publish(job, prev, cfg)

    def test_empty_prev_result_raises(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        job = _make_job(doc_type="book")
        with pytest.raises(FileNotFoundError, match="no source md"):
            run_publish(job, {}, cfg)
