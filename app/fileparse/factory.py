"""解析器工厂 — 按扩展名路由到对应解析器。

扩展接口:新增格式只需实现 BaseParser,在此注册即可。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.fileparse.base import BaseParser, ParseResult
from app.fileparse.docx import DocxParser
from app.fileparse.pptx import PptxParser
from app.fileparse.txt import TxtParser
from app.fileparse.xlsx import XlsxParser

logger = logging.getLogger("lqd.fileparse.factory")

# 解析器注册表:扩展名 → 解析器实例
_PARSERS: dict[str, BaseParser] = {}

# 支持的扩展名列表(供前端展示)
SUPPORTED_EXTENSIONS: list[str] = []


def _register(parser: BaseParser) -> None:
    for ext in parser.extensions:
        _PARSERS[ext.lower()] = parser
    SUPPORTED_EXTENSIONS.extend(parser.extensions)


# 注册内置解析器
_register(DocxParser())
_register(PptxParser())
_register(XlsxParser())
_register(TxtParser())


def get_parser(file_ext: str) -> BaseParser | None:
    """按扩展名获取解析器。file_ext 应为小写含点,如 ".pptx"。"""
    return _PARSERS.get(file_ext.lower())


def parse_file(file_path: str | Path) -> ParseResult:
    """解析单个文件,返回 ParseResult。

    自动检测扩展名,路由到对应解析器。
    不支持的格式返回 error="unsupported"。
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    result = ParseResult(
        file_path=str(path.resolve()),
        file_name=path.name,
        file_ext=ext,
    )

    parser = get_parser(ext)
    if parser is None:
        result.error = f"unsupported file type: {ext}"
        logger.warning("不支持的文件类型: %s", path)
        return result

    try:
        chunks = parser.parse(path)
        result.chunks = chunks
        logger.info("解析成功: %s, %d 切片", path.name, len(chunks))
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.error("解析失败 %s: %s", path, exc, exc_info=True)

    return result
