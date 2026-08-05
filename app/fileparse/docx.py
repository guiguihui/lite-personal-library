"""DOCX 解析器 — 基于 python-docx。

提取段落文本,按段落序号切片,保留行号(段落序号即行号)。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.fileparse.base import BaseParser, ParseChunk

logger = logging.getLogger("lqd.fileparse.docx")


class DocxParser(BaseParser):
    extensions = [".docx"]

    def parse(self, file_path: Path) -> list[ParseChunk]:
        from docx import Document

        logger.debug("DOCX 解析开始: %s", file_path.name)
        chunks: list[ParseChunk] = []
        try:
            doc = Document(str(file_path))
        except Exception as exc:
            logger.error("DOCX 打开失败 %s: %s", file_path, exc)
            raise

        para_idx = 0
        current_lines: list[str] = []
        current_start = 0
        target_chars = 500

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                para_idx += 1
                continue

            if not current_lines:
                current_start = para_idx + 1  # 1-based
            current_lines.append(text)
            total_chars = sum(len(l) for l in current_lines)

            if total_chars >= target_chars:
                chunk_text = "\n".join(current_lines)
                chunks.append(ParseChunk(
                    text=chunk_text,
                    page=1,  # docx 无页概念,统一用 1
                    page_label="段落",
                    line_start=current_start,
                    line_end=para_idx + 1,
                ))
                current_lines = []
                current_start = para_idx + 2

            para_idx += 1

        # 剩余段落
        if current_lines:
            chunk_text = "\n".join(current_lines)
            chunks.append(ParseChunk(
                text=chunk_text,
                page=1,
                page_label="段落",
                line_start=current_start,
                line_end=para_idx,
            ))

        logger.debug("DOCX 解析完成: %s, %d 切片", file_path.name, len(chunks))
        return chunks
