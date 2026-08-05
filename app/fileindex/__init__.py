"""本机文件索引模块。

提供文件内容检索的索引存储、构建、状态管理:
- store:   索引文件读写(files.json + chunks.json + inverted-index.json + .fingerprints.json)
- builder: 索引构建器(扫描目录 → fileparse 解析 → 切片 → 倒排索引)
- status:  构建任务状态管理(后台线程,进程内内存态)
"""

from app.fileindex.store import FileIndexStore
from app.fileindex.builder import build_full, build_incremental, FileBuildResult
from app.fileindex.status import start_build, get_status, list_jobs

__all__ = [
    "FileIndexStore",
    "build_full",
    "build_incremental",
    "FileBuildResult",
    "start_build",
    "get_status",
    "list_jobs",
]
