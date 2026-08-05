"""纯文本解析器 — 支持 .txt/.md/.csv 等。

按行读取,按目标字符数切片,行号 = 文件行号(1-based)。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.fileparse.base import BaseParser, ParseChunk

logger = logging.getLogger("lqd.fileparse.txt")


class TxtParser(BaseParser):
    extensions = [".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml"]

    def parse(self, file_path: Path) -> list[ParseChunk]:
        logger.debug("TXT 解析开始: %s", file_path.name)
        chunks: list[ParseChunk] = []

        # 尝试常见编码
        content = None
        for encoding in ("utf-8", "gbk", "gb2312", "latin-1"):
            try:
                content = file_path.read_text(encoding=encoding)
                logger.debug("TXT %s: 编码 %s 解码成功", file_path.name, encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue

        if content is None:
            logger.error("TXT %s: 无法解码(尝试了 utf-8/gbk/gb2312/latin-1)", file_path.name)
            return []

        chunks = self._split_text_to_chunks(
            text=content,
            page=1,
            page_label="文本",
            line_offset=1,
        )

        logger.debug("TXT 解析完成: %s, %d 切片", file_path.name, len(chunks))
        return chunks
