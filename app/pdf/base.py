"""Extractor Protocol + ExtractResult dataclass。

所有提取后端(local/mineru/epub)实现统一接口,供 factory + adapter 调用。
产出结构统一:out_dir/merged/book.md + out_dir/images/。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ExtractResult:
    """提取结果(不可变)。

    所有后端返回同一结构,adapter 据此推进下游(clean/translate/validate)。
    """

    ok: bool
    source_format: str  # "pdf" | "epub" | "docx" | "txt"
    merged_path: Path  # out_dir/merged/book.md
    images_dir: Path  # out_dir/images/
    title: str = ""
    author: str = ""
    page_count: int = 0
    duration_sec: float = 0.0
    error: str | None = None
    log: tuple[str, ...] = field(default_factory=tuple)


class Extractor(Protocol):
    """提取器协议:按扩展名路由后的具体实现。

    实现类需提供 extract(input_path, out_dir, pages) -> ExtractResult。
    pages 为可选页码范围(如 "1-50"),None 表示全文。
    """

    def extract(
        self,
        input_path: Path,
        out_dir: Path,
        pages: str | None = None,
    ) -> ExtractResult:
        """提取文档到 out_dir/merged/book.md + out_dir/images/。"""
        ...
