"""本机文件索引构建器。

流程:
  扫描目录 → fileparse 解析 → 切片 → 构建倒排索引 → 保存

支持全量(build_full)和增量(build_incremental)两种模式:
- 全量:  扫描全部文件,重建索引
- 增量:  基于 .fingerprints.json 检测变更,仅更新受影响文件

日志:核心步骤均有详细日志输出,便于排查解析异常。
"""

from __future__ import annotations

import hashlib
import logging
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from app.fileparse import parse_file, SUPPORTED_EXTENSIONS
from app.fileparse.base import ParseResult
from app.fileindex.store import (
    FileIndexStore, FileIndexData, FileEntry, ChunkEntry,
)
from app.retrieval.tokenizer import tokenize_unique

logger = logging.getLogger("lqd.fileindex.builder")


@dataclass(frozen=True)
class FileBuildResult:
    """索引构建结果。"""

    ok: bool
    files_scanned: int  # 扫描到的文件数
    files_indexed: int  # 成功索引的文件数
    files_skipped: int  # 跳过(未变更/解析失败)的文件数
    chunks_built: int  # 切片总数
    duration_sec: float
    error: str | None = None
    log: tuple[str, ...] = field(default_factory=tuple)


def _make_file_id(path: str) -> str:
    """从文件路径生成 16 字符唯一 ID。"""
    return hashlib.md5(path.encode("utf-8")).hexdigest()[:16]


def _make_chunk_id(file_id: str, seq: int) -> str:
    """从文件 ID 和切片序号生成 chunk_id。"""
    return f"{file_id}_{seq:04d}"


def _scan_files(root: Path, extensions: list[str]) -> list[Path]:
    """递归扫描目录下所有支持的文件。

    日志:扫描开始、找到 N 个文件、逐文件 debug 级日志。
    """
    ext_set = {e.lower() for e in extensions}
    found: list[Path] = []

    logger.info("扫描目录开始: %s (支持扩展名: %s)", root, ", ".join(sorted(ext_set)))

    if not root.is_dir():
        logger.warning("扫描目录不存在: %s", root)
        return found

    for item in root.rglob("*"):
        if item.is_file() and item.suffix.lower() in ext_set:
            found.append(item)
            logger.debug("发现文件: %s", item)

    logger.info("扫描完成: 共找到 %d 个文件", len(found))
    return found


def _build_inverted_index(chunks: list[ChunkEntry]) -> dict[str, list[str]]:
    """从切片列表构建倒排索引(token → [chunk_id, ...])。

    对每个 chunk 的 text 分词(tokenize_unique),token 去重后
    加入倒排表。一个 chunk 对同一 token 只计一次。
    """
    inv: dict[str, list[str]] = {}
    for ch in chunks:
        tokens = tokenize_unique(ch.text)
        for tok in tokens:  # tokenize_unique 已去重
            if tok not in inv:
                inv[tok] = []
            inv[tok].append(ch.chunk_id)
    return inv


def _index_single_file(
    file_path: Path,
    file_id: str,
    chunk_seq: int,
) -> tuple[FileEntry | None, list[ChunkEntry], int, str | None]:
    """解析单个文件,返回 (FileEntry, ChunkEntry 列表, 下一个 chunk_seq, error)。

    error 为 None 表示成功,非 None 表示失败(含文件路径+原因)。
    日志:解析开始、切片数、页码分布、解析异常详情(含 traceback)。
    """
    name = file_path.name
    ext = file_path.suffix.lower()
    abs_path = str(file_path.resolve())

    logger.info("解析文件开始: %s (路径: %s)", name, abs_path)

    try:
        stat = file_path.stat()
    except Exception as exc:
        err_detail = f"获取文件状态失败: {name} (路径: {abs_path}) — {type(exc).__name__}: {exc}"
        logger.error(err_detail, exc_info=True)
        return None, [], chunk_seq, err_detail

    # 调用解析器(内部已捕获异常,但再兜一层以防 import 失败等)
    try:
        result: ParseResult = parse_file(file_path)
    except Exception as exc:
        tb_str = traceback.format_exc()
        err_detail = (
            f"解析器调用异常: {name} (路径: {abs_path}) — "
            f"{type(exc).__name__}: {exc}\n{tb_str}"
        )
        logger.error(err_detail)
        return None, [], chunk_seq, err_detail

    if not result.ok:
        err_detail = result.error or f"无有效内容 (路径: {abs_path})"
        logger.warning(
            "文件解析无有效内容: %s (error=%s, chunks=%d, 路径: %s)",
            name, result.error, len(result.chunks), abs_path,
        )
        return None, [], chunk_seq, f"解析无内容: {name} — {err_detail}"

    # 构建 FileEntry
    file_entry = FileEntry(
        file_id=file_id,
        path=str(file_path.resolve()),
        name=name,
        ext=ext,
        mtime=stat.st_mtime,
        size=stat.st_size,
        chunk_count=len(result.chunks),
    )

    # 构建 ChunkEntry 列表
    chunk_entries: list[ChunkEntry] = []
    for i, ch in enumerate(result.chunks):
        cid = _make_chunk_id(file_id, chunk_seq + i)
        chunk_entries.append(ChunkEntry(
            chunk_id=cid,
            file_id=file_id,
            file_name=name,
            file_path=str(file_path.resolve()),
            page=ch.page,
            page_label=ch.page_label,
            line_start=ch.line_start,
            line_end=ch.line_end,
            text=ch.text,
        ))

    # 页码分布日志(便于排查 PPT 页码提取异常)
    page_set = sorted({c.page for c in result.chunks})
    if len(page_set) <= 10:
        logger.info(
            "文件 %s: %d 切片, 页码分布 %s",
            name, len(result.chunks), page_set,
        )
    else:
        logger.info(
            "文件 %s: %d 切片, 页码范围 %d-%d (共 %d 页)",
            name, len(result.chunks),
            min(page_set), max(page_set), len(page_set),
        )

    # 逐切片 debug 日志(含 page_label,便于定位)
    for ce in chunk_entries:
        logger.debug(
            "  切片 %s: %s L%d-%d (%s)",
            ce.chunk_id, ce.page_label, ce.line_start, ce.line_end,
            ce.text[:60].replace("\n", " ") + ("..." if len(ce.text) > 60 else ""),
        )

    next_seq = chunk_seq + len(result.chunks)
    logger.info("解析文件完成: %s, %d 切片", name, len(result.chunks))
    return file_entry, chunk_entries, next_seq, None


def build_full(
    scan_dir: str,
    index_dir: str,
    progress_callback=None,
) -> FileBuildResult:
    """全量构建索引。

    扫描 scan_dir 下所有支持的文件,解析 → 切片 → 倒排索引 → 保存。
    完全重建,不依赖已有索引。

    scan_dir:           要扫描的目录(如 E:\\文档\\iSC-PPT文件)
    index_dir:          索引输出目录(如 data/fileindex)
    progress_callback:  实时进度回调 fn(stage:str, msg:str, progress:float) -> None
                        progress 为 0.0~1.0 的进度比例
    """
    start_time = time.time()
    log_lines: list[str] = []

    def _log(msg: str, level: str = "info", progress: float = -1.0) -> None:
        log_lines.append(msg)
        getattr(logger, level)(msg)
        if progress_callback:
            try:
                progress_callback("building", msg, progress)
            except Exception:
                pass

    _log(f"全量构建开始: scan_dir={scan_dir}, index_dir={index_dir}", progress=0.0)

    root = Path(scan_dir)
    if not root.is_dir():
        err = f"扫描目录不存在: {scan_dir}"
        _log(err, "error", 0.0)
        return FileBuildResult(
            ok=False, files_scanned=0, files_indexed=0, files_skipped=0,
            chunks_built=0, duration_sec=time.time() - start_time,
            error=err, log=tuple(log_lines),
        )

    # 扫描文件
    files = _scan_files(root, SUPPORTED_EXTENSIONS)
    total = len(files)
    _log(f"扫描到 {total} 个文件", progress=0.0)

    store = FileIndexStore(index_dir)
    data = FileIndexData()

    files_indexed = 0
    files_skipped = 0
    chunk_seq = 0  # 全局切片序号

    for idx, fpath in enumerate(files):
        progress = (idx + 1) / total if total > 0 else 1.0
        _log(f"[{idx + 1}/{total}] 正在解析: {fpath.name}", progress=progress)

        file_id = _make_file_id(str(fpath.resolve()))
        file_entry, chunk_entries, chunk_seq, file_error = _index_single_file(
            fpath, file_id, chunk_seq,
        )

        if file_entry is None:
            files_skipped += 1
            if file_error:
                _log(f"  ✗ 跳过(解析失败): {file_error}", "warning", progress)
            else:
                _log(f"  ✗ 跳过(未知原因): {fpath.name}", "warning", progress)
            continue

        data.files.append(file_entry)
        data.chunks.extend(chunk_entries)
        data.fingerprints[file_entry.path] = FileIndexStore.make_fingerprint(
            file_entry.mtime, file_entry.size,
        )
        files_indexed += 1

    _log(f"解析完成: {files_indexed} 文件成功, {files_skipped} 跳过, {len(data.chunks)} 切片", progress=0.9)

    # 构建倒排索引
    _log("构建倒排索引开始...", progress=0.92)
    data.inverted_index = _build_inverted_index(data.chunks)
    _log(f"倒排索引完成: {len(data.inverted_index)} 个 token", progress=0.96)

    # 保存
    _log("保存索引文件...", progress=0.97)
    store.save(data)
    _log("保存完成", progress=0.99)

    duration = time.time() - start_time
    _log(f"全量构建完成: 耗时 {duration:.2f}s", progress=1.0)

    return FileBuildResult(
        ok=True,
        files_scanned=len(files),
        files_indexed=files_indexed,
        files_skipped=files_skipped,
        chunks_built=len(data.chunks),
        duration_sec=duration,
        log=tuple(log_lines),
    )


def build_incremental(
    scan_dir: str,
    index_dir: str,
    progress_callback=None,
) -> FileBuildResult:
    """增量构建索引。

    基于 .fingerprints.json 检测文件变更(mtime+size):
    - 新文件:  解析并加入索引
    - 已变更:  删除旧切片,重新解析
    - 已删除:  从索引移除
    - 未变更:  跳过

    scan_dir:           要扫描的目录
    index_dir:          索引输出目录
    progress_callback:  实时进度回调 fn(stage:str, msg:str, progress:float) -> None
                        progress 为 0.0~1.0 的进度比例
    """
    start_time = time.time()
    log_lines: list[str] = []

    def _log(msg: str, level: str = "info", progress: float = -1.0) -> None:
        log_lines.append(msg)
        getattr(logger, level)(msg)
        if progress_callback:
            try:
                progress_callback("building", msg, progress)
            except Exception:
                pass

    _log(f"增量构建开始: scan_dir={scan_dir}, index_dir={index_dir}", progress=0.0)

    root = Path(scan_dir)
    if not root.is_dir():
        err = f"扫描目录不存在: {scan_dir}"
        _log(err, "error", 0.0)
        return FileBuildResult(
            ok=False, files_scanned=0, files_indexed=0, files_skipped=0,
            chunks_built=0, duration_sec=time.time() - start_time,
            error=err, log=tuple(log_lines),
        )

    # 加载已有索引
    store = FileIndexStore(index_dir)
    data = store.load()
    _log(f"已有索引: {len(data.files)} 文件, {len(data.chunks)} 切片", progress=0.02)

    # 扫描当前文件
    files = _scan_files(root, SUPPORTED_EXTENSIONS)
    total = len(files)
    _log(f"扫描到 {total} 个文件", progress=0.05)

    # 构建当前文件路径集合
    current_paths: set[str] = set()
    files_indexed = 0
    files_skipped = 0
    chunk_seq = 0

    # 已有文件 ID → FileEntry 映射
    old_files_by_id: dict[str, FileEntry] = {f.file_id: f for f in data.files}
    old_chunks_by_file: dict[str, list[ChunkEntry]] = {}
    for ch in data.chunks:
        old_chunks_by_file.setdefault(ch.file_id, []).append(ch)

    # 新的索引数据(从旧数据中保留未变更的,新增/变更的重新解析)
    new_files: list[FileEntry] = []
    new_chunks: list[ChunkEntry] = []
    new_fingerprints: dict[str, str] = {}

    for idx, fpath in enumerate(files):
        abs_path = str(fpath.resolve())
        file_id = _make_file_id(abs_path)
        current_paths.add(abs_path)
        progress = (idx + 1) / total if total > 0 else 1.0

        try:
            stat = fpath.stat()
        except Exception as exc:
            err_detail = f"获取文件状态失败: {fpath.name} (路径: {abs_path}) — {type(exc).__name__}: {exc}"
            _log(f"  ✗ {err_detail}", "error", progress)
            files_skipped += 1
            continue

        new_fp = FileIndexStore.make_fingerprint(stat.st_mtime, stat.st_size)
        old_fp = data.fingerprints.get(abs_path)

        if old_fp == new_fp and file_id in old_files_by_id:
            # 未变更:保留旧数据
            old_fe = old_files_by_id[file_id]
            new_files.append(old_fe)
            new_chunks.extend(old_chunks_by_file.get(file_id, []))
            new_fingerprints[abs_path] = new_fp
            files_skipped += 1
            logger.debug("文件未变更,跳过: %s", fpath.name)
        else:
            # 新增或变更:重新解析
            if old_fp is not None:
                _log(f"[{idx + 1}/{total}] 文件已变更,重新索引: {fpath.name}", progress=progress)
            else:
                _log(f"[{idx + 1}/{total}] 新文件: {fpath.name}", progress=progress)

            file_entry, chunk_entries, chunk_seq, file_error = _index_single_file(
                fpath, file_id, chunk_seq,
            )
            if file_entry is None:
                files_skipped += 1
                if file_error:
                    _log(f"  ✗ 跳过(解析失败): {file_error}", "warning", progress)
                else:
                    _log(f"  ✗ 跳过(未知原因): {fpath.name}", "warning", progress)
                continue

            new_files.append(file_entry)
            new_chunks.extend(chunk_entries)
            new_fingerprints[abs_path] = new_fp
            files_indexed += 1

    # 检测已删除的文件(在旧索引中但不在当前扫描中)
    old_paths = set(data.fingerprints.keys())
    deleted = old_paths - current_paths
    if deleted:
        _log(f"检测到 {len(deleted)} 个已删除文件", progress=0.9)
        for dp in deleted:
            logger.info("文件已删除: %s", dp)

    # 更新索引数据
    data.files = new_files
    data.chunks = new_chunks
    data.fingerprints = new_fingerprints

    _log(f"解析完成: {files_indexed} 文件更新, {files_skipped} 跳过, {len(data.chunks)} 切片", progress=0.92)

    # 重建倒排索引
    _log("构建倒排索引开始...", progress=0.95)
    data.inverted_index = _build_inverted_index(data.chunks)
    _log(f"倒排索引完成: {len(data.inverted_index)} 个 token", progress=0.97)

    # 保存
    _log("保存索引文件...", progress=0.98)
    store.save(data)
    _log("保存完成", progress=0.99)

    duration = time.time() - start_time
    _log(f"增量构建完成: 耗时 {duration:.2f}s", progress=1.0)

    return FileBuildResult(
        ok=True,
        files_scanned=len(files),
        files_indexed=files_indexed,
        files_skipped=files_skipped,
        chunks_built=len(data.chunks),
        duration_sec=duration,
        log=tuple(log_lines),
    )
