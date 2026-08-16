"""提取器工厂:按扩展名 + 策略路由到具体后端。

make_extractor(filename, strategy) -> Extractor
  filename:  文件名(用于按扩展名路由 .pdf/.epub/.docx/.txt)
  strategy:  "local" | "mineru" | None(None 用 AppConfig.pdf_strategy)

路由规则:
  .pdf  + local  → LocalExtractor
  .pdf  + mineru → MineruExtractor
  .epub          → EpubExtractor(忽略 strategy,pandoc 唯一)
  .docx/.txt     → LocalExtractor(暂走 PyMuPDF 兼容,实际 docx 需另写)
  其他扩展名 → ValueError
"""

from __future__ import annotations

from pathlib import Path

from app.pdf.base import Extractor
from app.pdf.epub import EpubExtractor, resolve_pandoc
from app.pdf.epub_fitz import EpubFitzExtractor
from app.pdf.local import LocalExtractor
from app.pdf.mineru import MineruExtractor

_PDF_EXTS = {".pdf"}
_EPUB_EXTS = {".epub"}


def make_extractor(filename: str | Path, strategy: str | None = None) -> Extractor:
    """按扩展名 + 策略返回提取器实例。

    strategy 为 None 时默认 "local"(对齐 AppConfig.pdf_strategy 默认值)。
    """
    ext = Path(filename).suffix.lower()
    strat = strategy or "local"

    if ext in _EPUB_EXTS:
        pandoc = resolve_pandoc()
        return EpubExtractor(pandoc) if pandoc else EpubFitzExtractor()
    if ext in _PDF_EXTS:
        if strat == "mineru":
            return MineruExtractor()
        return LocalExtractor()
    if ext == ".txt":
        # txt 无需提取,但走 LocalExtractor 的占位(实际 adapter 会直接读)
        return LocalExtractor()
    raise ValueError(f"unsupported file type: {ext}")
