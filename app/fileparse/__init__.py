"""本机文件内容检索 — 文件解析模块。

支持 docx/pptx/xlsx/txt/md 等格式的文本提取,保留页码/行号元数据。
扩展接口:实现 BaseParser,在 factory.py 注册即可。
"""

from __future__ import annotations

from app.fileparse.base import ParseChunk, ParseResult, BaseParser
from app.fileparse.factory import parse_file, get_parser, SUPPORTED_EXTENSIONS

__all__ = [
    "ParseChunk",
    "ParseResult",
    "BaseParser",
    "parse_file",
    "get_parser",
    "SUPPORTED_EXTENSIONS",
]
