"""文件解析基类与数据结构。

ParseChunk 是最小的检索单元,携带页码/行号定位信息:
- page: 页码(PPT 的 slide number、xlsx 的 sheet 名、docx 的段落序号)
- line_start/line_end: 行号(1-based,用于结果精确跳转)
- text: 切片文本
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("lqd.fileparse")


@dataclass
class ParseChunk:
    """单个解析切片,对应检索结果的一条定位单元。

    page_label 用于展示(如 "Slide 3"、"Sheet1"、"段落 5"),
    page 是用于排序的数字(PPT 用 slide 序号,xlsx 用 sheet 序号)。
    """

    text: str
    page: int  # 数字页码(用于排序);txt/docx 用段落序号
    page_label: str  # 展示用页码标签
    line_start: int  # 1-based 行号
    line_end: int  # 1-based 行号
    metadata: dict = field(default_factory=dict)  # 额外元数据(sheet 名等)


@dataclass
class ParseResult:
    """文件解析结果。"""

    file_path: str
    file_name: str
    file_ext: str  # 扩展名(小写,含点),如 ".pptx"
    chunks: list[ParseChunk] = field(default_factory=list)
    error: str | None = None  # 解析失败时的错误信息

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.chunks) > 0


class BaseParser(ABC):
    """文件解析器基类。子类实现 parse() 返回 ParseChunk 列表。"""

    extensions: list[str] = []  # 支持的扩展名(小写,含点),如 [".docx"]

    @abstractmethod
    def parse(self, file_path: Path) -> list[ParseChunk]:
        """解析文件,返回切片列表。失败时抛异常或返回空列表。"""
        ...

    @staticmethod
    def _split_text_to_chunks(
        text: str,
        page: int,
        page_label: str,
        line_offset: int = 0,
        target_chars: int = 500,
        overlap: int = 100,
    ) -> list[ParseChunk]:
        """将一段文本按目标字符数切片,保留行号信息。

        line_offset 是该段文本在文件中的起始行号(1-based)。
        切片策略:按段落/换行切分,累积到 target_chars 后输出一个 chunk,
        下一个 chunk 回退 overlap 字符以保证上下文连续。
        """
        if not text or not text.strip():
            return []

        # 按行切分,保留行号
        lines = text.split("\n")
        chunks: list[ParseChunk] = []

        current_lines: list[str] = []
        current_chars = 0
        chunk_start_line = line_offset

        for i, line in enumerate(lines):
            line_no = line_offset + i
            current_lines.append(line)
            current_chars += len(line) + 1  # +1 for \n

            # 达到目标字符数,输出 chunk
            if current_chars >= target_chars:
                chunk_text = "\n".join(current_lines)
                chunks.append(ParseChunk(
                    text=chunk_text,
                    page=page,
                    page_label=page_label,
                    line_start=chunk_start_line,
                    line_end=line_no,
                ))

                # 重叠:保留末尾几行
                overlap_chars = 0
                overlap_lines: list[str] = []
                for j in range(len(current_lines) - 1, -1, -1):
                    overlap_chars += len(current_lines[j]) + 1
                    if overlap_chars >= overlap:
                        overlap_lines = current_lines[j:]
                        break
                current_lines = overlap_lines[:] if overlap_lines else []
                current_chars = sum(len(l) + 1 for l in current_lines)
                chunk_start_line = line_no - len(current_lines) + 1

        # 剩余文本
        if current_lines and any(l.strip() for l in current_lines):
            chunk_text = "\n".join(current_lines)
            chunks.append(ParseChunk(
                text=chunk_text,
                page=page,
                page_label=page_label,
                line_start=chunk_start_line,
                line_end=line_offset + len(lines) - 1,
            ))

        return chunks
