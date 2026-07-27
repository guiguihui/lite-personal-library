"""PDF 提取双后端(阶段 4)。

模块组织(零耦合,依赖单向向下):
  base    — Extractor Protocol + ExtractResult dataclass(叶子)
  local   — PyMuPDF/pdfplumber 实现(离线,零外部依赖)
  mineru  — MinerU HTTP API 实现(httpx,高质量,需 API key + 网络)
  epub    — pandoc EPUB 实现(外部命令)
  factory — make_extractor(filename, strategy) 按扩展名 + 策略路由

产出结构(所有后端统一):
  out_dir/
    merged/
      book.md        # 合并后的单文件 markdown
    images/          # 图片子目录(引用形如 images/xxx.webp)
"""

from __future__ import annotations

from app.pdf.base import ExtractResult, Extractor
from app.pdf.factory import make_extractor

__all__ = ["ExtractResult", "Extractor", "make_extractor"]
