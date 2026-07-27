"""pytest 测试:PDF 提取双后端(app/pdf/)。

覆盖:
  - factory.make_extractor:按扩展名 + 策略路由
  - LocalExtractor:无 PyMuPDF 时降级返回明确错误
  - MineruExtractor:无 API key 时降级
  - EpubExtractor:无 pandoc 时降级
  - _parse_pages:页码范围解析
  - _to_webp_bytes:图片转 webp(失败回退原格式)
  - 真实提取(需 PyMuPDF,标记 integration,无则 skip)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.pdf.base import ExtractResult
from app.pdf.epub import EpubExtractor
from app.pdf.factory import make_extractor
from app.pdf.local import LocalExtractor, _parse_pages, _to_webp_bytes
from app.pdf.mineru import MineruExtractor


# ══════════════════════════════════════════════════════════════════════════
# Factory 路由
# ══════════════════════════════════════════════════════════════════════════


class TestFactory:
    def test_pdf_local_default(self) -> None:
        # .pdf + 无 strategy → LocalExtractor(strategy 默认 local)
        ext = make_extractor("foo.pdf")
        assert isinstance(ext, LocalExtractor)

    def test_pdf_local_explicit(self) -> None:
        ext = make_extractor("foo.pdf", strategy="local")
        assert isinstance(ext, LocalExtractor)

    def test_pdf_mineru(self) -> None:
        ext = make_extractor("foo.pdf", strategy="mineru")
        assert isinstance(ext, MineruExtractor)

    def test_epub_ignores_strategy(self) -> None:
        # epub 走 pandoc,忽略 strategy 参数
        ext1 = make_extractor("foo.epub")
        ext2 = make_extractor("foo.epub", strategy="mineru")
        assert isinstance(ext1, EpubExtractor)
        assert isinstance(ext2, EpubExtractor)

    def test_docx_routes_to_local(self) -> None:
        # .docx 暂走 LocalExtractor(占位)
        ext = make_extractor("foo.docx")
        assert isinstance(ext, LocalExtractor)

    def test_txt_routes_to_local(self) -> None:
        ext = make_extractor("foo.txt")
        assert isinstance(ext, LocalExtractor)

    def test_unsupported_extension(self) -> None:
        with pytest.raises(ValueError, match="unsupported file type"):
            make_extractor("foo.unknown")

    def test_extension_case_insensitive(self) -> None:
        ext = make_extractor("FOO.PDF")
        assert isinstance(ext, LocalExtractor)

    def test_path_object_accepted(self) -> None:
        ext = make_extractor(Path("subdir/foo.pdf"))
        assert isinstance(ext, LocalExtractor)


# ══════════════════════════════════════════════════════════════════════════
# _parse_pages 单元
# ══════════════════════════════════════════════════════════════════════════


class TestParsePages:
    def test_none_returns_all(self) -> None:
        assert _parse_pages(None, 10) == list(range(10))

    def test_empty_returns_all(self) -> None:
        assert _parse_pages("", 5) == list(range(5))

    def test_single_page(self) -> None:
        # "3" → 第 3 页(0-based index 2)
        assert _parse_pages("3", 10) == [2]

    def test_range(self) -> None:
        # "1-3" → 第 1,2,3 页
        assert _parse_pages("1-3", 10) == [0, 1, 2]

    def test_list(self) -> None:
        # "1,3,5" → 第 1,3,5 页
        assert _parse_pages("1,3,5", 10) == [0, 2, 4]

    def test_mixed_range_and_single(self) -> None:
        # "1-3,5" → 1,2,3,5
        assert _parse_pages("1-3,5", 10) == [0, 1, 2, 4]

    def test_dedup_and_sort(self) -> None:
        # "3,1,2,1" → 去重排序 [0,1,2]
        assert _parse_pages("3,1,2,1", 10) == [0, 1, 2]

    def test_range_clamped_to_total(self) -> None:
        # "1-100" 但只有 5 页 → 裁剪到 1-5
        assert _parse_pages("1-100", 5) == [0, 1, 2, 3, 4]

    def test_out_of_range_dropped(self) -> None:
        # "99" 但只有 5 页 → 丢弃
        assert _parse_pages("99", 5) == []

    def test_range_lo_below_min_clamped(self) -> None:
        # "0-3"(lo<1)→ lo 裁到 1
        assert _parse_pages("0-3", 10) == [0, 1, 2]


# ══════════════════════════════════════════════════════════════════════════
# _to_webp_bytes 单元
# ══════════════════════════════════════════════════════════════════════════


class TestToWebpBytes:
    def test_invalid_bytes_returns_original(self) -> None:
        # 无效图片字节 → PIL 失败 → 原样返回
        raw = b"not an image"
        data, ext = _to_webp_bytes(raw, "png")
        assert data == raw
        assert ext == "png"

    def test_ext_stripped(self) -> None:
        # 扩展名带点 → 去点
        _, ext = _to_webp_bytes(b"x", ".jpg")
        assert ext == "jpg"

    def test_valid_png_converts_to_webp(self) -> None:
        # 造一个最小 PNG,验证能转 webp(需 PIL)
        try:
            from PIL import Image  # type: ignore
            import io
        except ImportError:
            pytest.skip("Pillow not installed")
        buf = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
        raw = buf.getvalue()
        data, ext = _to_webp_bytes(raw, "png")
        # 成功转 webp(ext 变 webp,data 是 webp 字节)
        assert ext == "webp"
        assert data[:4] in (b"RIFF",)  # webp 文件头


# ══════════════════════════════════════════════════════════════════════════
# LocalExtractor 降级
# ══════════════════════════════════════════════════════════════════════════


class TestLocalExtractorDegradation:
    def test_no_pymupdf_returns_error(self, tmp_path: Path) -> None:
        # PyMuPDF 未装时,extract 返回 ok=False + 明确错误(不抛异常)
        # 用一个假 PDF 路径(文件存在但内容无效,触发 fitz import 失败路径)
        fake_pdf = tmp_path / "fake.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")
        ext = LocalExtractor()
        result = ext.extract(fake_pdf, tmp_path / "out")
        assert isinstance(result, ExtractResult)
        assert result.source_format == "pdf"
        # 无 fitz → ok=False,error 含 PyMuPDF;有 fitz → 打开失败也 ok=False
        if not _has_fitz():
            assert result.ok is False
            assert "PyMuPDF" in (result.error or "") or "fitz" in (result.error or "")
        else:
            # 有 fitz 但内容无效 → ok=False(open failed)
            assert result.ok is False

    def test_nonexistent_input_returns_error(self, tmp_path: Path) -> None:
        # 输入文件不存在 → fitz.open 失败 → ok=False
        if not _has_fitz():
            pytest.skip("PyMuPDF not installed")
        ext = LocalExtractor()
        result = ext.extract(tmp_path / "nonexistent.pdf", tmp_path / "out")
        assert result.ok is False
        assert result.error is not None


# ══════════════════════════════════════════════════════════════════════════
# MineruExtractor 降级
# ══════════════════════════════════════════════════════════════════════════


class TestMineruExtractorDegradation:
    def test_no_api_key_returns_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 无 MINERU_API_KEY → ok=False,不发起网络请求
        monkeypatch.delenv("MINERU_API_KEY", raising=False)
        monkeypatch.delenv("MINERU_BASE_URL", raising=False)
        fake_pdf = tmp_path / "fake.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")
        ext = MineruExtractor()
        result = ext.extract(fake_pdf, tmp_path / "out")
        assert result.ok is False
        assert "MINERU_API_KEY" in (result.error or "")
        assert result.source_format == "pdf"

    def test_explicit_empty_key_returns_error(self, tmp_path: Path) -> None:
        fake_pdf = tmp_path / "fake.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")
        ext = MineruExtractor(api_key="")
        result = ext.extract(fake_pdf, tmp_path / "out")
        assert result.ok is False
        assert "MINERU_API_KEY" in (result.error or "")


# ══════════════════════════════════════════════════════════════════════════
# EpubExtractor 降级
# ══════════════════════════════════════════════════════════════════════════


class TestEpubExtractorDegradation:
    def test_no_pandoc_returns_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 模拟 pandoc 不在 PATH
        import app.pdf.epub as epub_mod

        monkeypatch.setattr(epub_mod.shutil, "which", lambda name: None)
        fake_epub = tmp_path / "fake.epub"
        fake_epub.write_bytes(b"fake epub")
        ext = EpubExtractor()
        result = ext.extract(fake_epub, tmp_path / "out")
        assert result.ok is False
        assert "pandoc" in (result.error or "")
        assert result.source_format == "epub"


# ══════════════════════════════════════════════════════════════════════════
# 真实提取(需 PyMuPDF,标记 integration)
# ══════════════════════════════════════════════════════════════════════════


def _has_fitz() -> bool:
    try:
        import fitz  # type: ignore  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.integration
class TestLocalExtractorReal:
    def test_extract_real_pdf(self, tmp_path: Path) -> None:
        if not _has_fitz():
            pytest.skip("PyMuPDF not installed")
        import fitz  # type: ignore

        # 造一个 3 页 PDF,每页含文本
        pdf_path = tmp_path / "sample.pdf"
        doc = fitz.open()
        for i in range(3):
            page = doc.new_page()
            page.insert_text((50, 72), f"Page {i + 1} content", fontsize=12)
        doc.save(str(pdf_path))
        doc.close()

        out_dir = tmp_path / "out"
        ext = LocalExtractor()
        result = ext.extract(pdf_path, out_dir)
        assert result.ok is True
        assert result.source_format == "pdf"
        assert result.page_count == 3
        assert result.merged_path.exists()
        # merged/book.md 含每页的 page 注释
        text = result.merged_path.read_text(encoding="utf-8")
        assert "page 1" in text
        assert "Page 1 content" in text
        assert "page 3" in text

    def test_extract_with_page_range(self, tmp_path: Path) -> None:
        if not _has_fitz():
            pytest.skip("PyMuPDF not installed")
        import fitz  # type: ignore

        pdf_path = tmp_path / "sample.pdf"
        doc = fitz.open()
        for i in range(5):
            page = doc.new_page()
            page.insert_text((50, 72), f"Page {i + 1}", fontsize=12)
        doc.save(str(pdf_path))
        doc.close()

        out_dir = tmp_path / "out"
        ext = LocalExtractor()
        result = ext.extract(pdf_path, out_dir, pages="1-2")
        assert result.ok is True
        assert result.page_count == 2  # 只提取了 2 页
        text = result.merged_path.read_text(encoding="utf-8")
        assert "page 1" in text and "page 2" in text
        assert "page 3" not in text  # 第 3 页未提取


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
