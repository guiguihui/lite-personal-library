"""本机文件索引存储 — JSON 文件读写。

索引文件布局(data/fileindex/):
  files.json            — 文件元信息列表(path/name/ext/mtime/size/chunk_count)
  chunks.json           — 全部切片列表(file_id/chunk_id/page/page_label/line_start/line_end/text)
  inverted-index.json   — 倒排索引(token → [chunk_id, ...])
  .fingerprints.json    — 文件指纹(path → "mtime:size",用于增量更新检测)

所有文件均为 JSON(桌面端单进程,无需数据库)。
加载时全量读入内存;保存时全量覆写(文件量 <10k 时足够)。
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("lqd.fileindex.store")

# 索引文件名
FILES_JSON = "files.json"
CHUNKS_JSON = "chunks.json"
INVERTED_INDEX_JSON = "inverted-index.json"
FINGERPRINTS_JSON = ".fingerprints.json"


@dataclass
class FileEntry:
    """文件元信息。"""

    file_id: str  # 文件唯一 ID(path 的 hash,16 字符)
    path: str  # 绝对路径
    name: str  # 文件名
    ext: str  # 扩展名(小写含点)
    mtime: float  # 修改时间戳
    size: int  # 文件大小(字节)
    chunk_count: int  # 该文件的切片数


@dataclass
class ChunkEntry:
    """切片条目(检索结果定位单元)。"""

    chunk_id: str  # 切片唯一 ID
    file_id: str  # 所属文件 ID
    file_name: str  # 所属文件名(冗余,展示用,避免反查)
    file_path: str  # 所属文件绝对路径(冗余,展示用)
    page: int  # 页码(用于排序)
    page_label: str  # 展示用页码标签
    line_start: int  # 1-based 行号
    line_end: int  # 1-based 行号
    text: str  # 切片文本


@dataclass
class FileIndexData:
    """内存中的完整索引数据。"""

    files: list[FileEntry] = field(default_factory=list)
    chunks: list[ChunkEntry] = field(default_factory=list)
    inverted_index: dict[str, list[str]] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)


class FileIndexStore:
    """索引文件读写器。

    用法:
        store = FileIndexStore(index_dir)
        data = store.load()           # 加载全部索引到内存
        store.save(data)              # 全量保存
        store.get_fingerprint(path)   # 读单个文件指纹
    """

    def __init__(self, index_dir: str | Path):
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)

    # ── 加载 ─────────────────────────────────────────────────────────

    def load(self) -> FileIndexData:
        """加载全部索引文件到内存。文件不存在时返回空索引。"""
        data = FileIndexData()

        # files.json
        files_path = self.index_dir / FILES_JSON
        if files_path.is_file():
            try:
                raw = json.loads(files_path.read_text(encoding="utf-8"))
                data.files = [
                    FileEntry(
                        file_id=f["file_id"],
                        path=f["path"],
                        name=f["name"],
                        ext=f["ext"],
                        mtime=f["mtime"],
                        size=f["size"],
                        chunk_count=f["chunk_count"],
                    )
                    for f in raw
                ]
            except Exception as exc:
                logger.error(
                    "加载 %s 失败: %s — %s: %s\n%s",
                    files_path, type(exc).__name__, exc, "",
                    traceback.format_exc(),
                )

        # chunks.json
        chunks_path = self.index_dir / CHUNKS_JSON
        if chunks_path.is_file():
            try:
                raw = json.loads(chunks_path.read_text(encoding="utf-8"))
                data.chunks = [
                    ChunkEntry(
                        chunk_id=c["chunk_id"],
                        file_id=c["file_id"],
                        file_name=c["file_name"],
                        file_path=c["file_path"],
                        page=c["page"],
                        page_label=c["page_label"],
                        line_start=c["line_start"],
                        line_end=c["line_end"],
                        text=c["text"],
                    )
                    for c in raw
                ]
            except Exception as exc:
                logger.error(
                    "加载 %s 失败: %s — %s: %s\n%s",
                    chunks_path, type(exc).__name__, exc, "",
                    traceback.format_exc(),
                )

        # inverted-index.json
        inv_path = self.index_dir / INVERTED_INDEX_JSON
        if inv_path.is_file():
            try:
                data.inverted_index = json.loads(inv_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error(
                    "加载 %s 失败: %s — %s: %s\n%s",
                    inv_path, type(exc).__name__, exc, "",
                    traceback.format_exc(),
                )

        # .fingerprints.json
        fp_path = self.index_dir / FINGERPRINTS_JSON
        if fp_path.is_file():
            try:
                data.fingerprints = json.loads(fp_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error(
                    "加载 %s 失败: %s — %s: %s\n%s",
                    fp_path, type(exc).__name__, exc, "",
                    traceback.format_exc(),
                )

        logger.info(
            "索引加载完成: %d 文件, %d 切片, %d 倒排项",
            len(data.files), len(data.chunks), len(data.inverted_index),
        )
        return data

    # ── 保存 ─────────────────────────────────────────────────────────

    def save(self, data: FileIndexData) -> None:
        """全量保存索引文件(覆写)。

        每个文件独立写入,单个文件写入失败不影响其他文件,
        但会在日志中记录完整路径和 traceback。
        """
        # files.json
        files_raw = [
            {
                "file_id": f.file_id,
                "path": f.path,
                "name": f.name,
                "ext": f.ext,
                "mtime": f.mtime,
                "size": f.size,
                "chunk_count": f.chunk_count,
            }
            for f in data.files
        ]
        files_path = self.index_dir / FILES_JSON
        try:
            files_path.write_text(
                json.dumps(files_raw, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            logger.error(
                "保存 %s 失败: %s — %s: %s\n%s",
                files_path, type(exc).__name__, exc, "",
                traceback.format_exc(),
            )

        # chunks.json
        chunks_raw = [
            {
                "chunk_id": c.chunk_id,
                "file_id": c.file_id,
                "file_name": c.file_name,
                "file_path": c.file_path,
                "page": c.page,
                "page_label": c.page_label,
                "line_start": c.line_start,
                "line_end": c.line_end,
                "text": c.text,
            }
            for c in data.chunks
        ]
        chunks_path = self.index_dir / CHUNKS_JSON
        try:
            chunks_path.write_text(
                json.dumps(chunks_raw, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            logger.error(
                "保存 %s 失败: %s — %s: %s\n%s",
                chunks_path, type(exc).__name__, exc, "",
                traceback.format_exc(),
            )

        # inverted-index.json
        inv_path = self.index_dir / INVERTED_INDEX_JSON
        try:
            inv_path.write_text(
                json.dumps(data.inverted_index, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            logger.error(
                "保存 %s 失败: %s — %s: %s\n%s",
                inv_path, type(exc).__name__, exc, "",
                traceback.format_exc(),
            )

        # .fingerprints.json
        fp_path = self.index_dir / FINGERPRINTS_JSON
        try:
            fp_path.write_text(
                json.dumps(data.fingerprints, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            logger.error(
                "保存 %s 失败: %s — %s: %s\n%s",
                fp_path, type(exc).__name__, exc, "",
                traceback.format_exc(),
            )

        logger.info(
            "索引保存完成: %d 文件, %d 切片, %d 倒排项",
            len(data.files), len(data.chunks), len(data.inverted_index),
        )

    # ── 指纹 ─────────────────────────────────────────────────────────

    @staticmethod
    def make_fingerprint(mtime: float, size: int) -> str:
        """生成文件指纹:"mtime:size"。"""
        return f"{mtime}:{size}"

    def get_fingerprint(self, path: str, data: FileIndexData | None = None) -> str | None:
        """读单个文件的已存指纹。data 为 None 时从磁盘加载。"""
        if data is None:
            data = self.load()
        return data.fingerprints.get(path)
