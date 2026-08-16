"""Synchronous validation for ingestion sources and execution policies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import zipfile


MAX_UPLOAD_BYTES = 512 * 1024 * 1024
SUPPORTED_UPLOAD_EXTENSIONS = frozenset({".pdf", ".epub"})
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class PreflightError(ValueError):
    """An ingestion request that must be rejected before creating a job."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        field: str | None = None,
        retryable: bool = False,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field
        self.retryable = retryable
        self.context = context or {}

    def detail(self) -> dict[str, object]:
        value: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.field:
            value["field"] = self.field
        if self.context:
            value["context"] = self.context
        return value


@dataclass(frozen=True, slots=True)
class PreflightResult:
    source_path: Path
    extension: str
    strategy: str
    network_policy: str
    stages: tuple[str, ...]


def safe_filename(raw: str) -> str:
    """Return one safe basename without trusting browser path components."""

    if not isinstance(raw, str):
        raise PreflightError("INVALID_FILENAME", "文件名必须是字符串", field="file")
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or _CONTROL_RE.search(name):
        raise PreflightError("INVALID_FILENAME", "文件名为空或包含非法字符", field="file")
    name = name.rstrip(" .")
    if not name or name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise PreflightError("INVALID_FILENAME", "文件名是 Windows 保留名称", field="file")
    return name


def validate_slug(slug: str, pdfs_dir: str | Path) -> str:
    """Validate a user slug and prove its output path remains under pdfs_dir."""

    if (
        not isinstance(slug, str)
        or not slug
        or len(slug) > 128
        or slug in {".", ".."}
        or "/" in slug
        or "\\" in slug
        or _CONTROL_RE.search(slug)
        or slug.endswith((".", " "))
    ):
        raise PreflightError("INVALID_SLUG", "slug 为空、过长或包含非法路径字符", field="slug")
    root = Path(pdfs_dir).resolve()
    candidate = (root / slug).resolve()
    if candidate.parent != root:
        raise PreflightError("INVALID_SLUG", "slug 会越出文档工作目录", field="slug")
    return slug


def validate_file_signature(path: Path, extension: str) -> None:
    """Fail closed for formats exposed by the upload UI."""

    if path.stat().st_size <= 0:
        raise PreflightError("EMPTY_FILE", "文件为空", field="file")
    if extension == ".pdf":
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise PreflightError(
                    "SIGNATURE_MISMATCH",
                    "文件扩展名是 PDF，但文件头不是 PDF",
                    status_code=415,
                    field="file",
                )
        return
    if extension == ".epub":
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                mimetype = archive.read("mimetype")
                if mimetype.strip() != b"application/epub+zip" or "META-INF/container.xml" not in names:
                    raise KeyError("missing EPUB container metadata")
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            raise PreflightError(
                "SIGNATURE_MISMATCH",
                "文件扩展名是 EPUB，但容器结构无效",
                status_code=415,
                field="file",
            ) from exc


def preflight_source(
    source: str | Path,
    *,
    pdfs_dir: str | Path,
    slug: str,
    strategy: str | None,
    network_policy: str,
    stages: tuple[str, ...] | list[str],
) -> PreflightResult:
    """Resolve and validate a source before a background job exists."""

    validate_slug(slug, pdfs_dir)
    requested_stages = tuple(stages)
    if not requested_stages:
        raise PreflightError("INVALID_STAGES", "至少选择一个处理阶段", field="stages")
    if network_policy not in {"offline", "allow_ai"}:
        raise PreflightError("INVALID_NETWORK_POLICY", "未知网络策略", field="network_policy")
    actual_strategy = strategy or "local"
    if actual_strategy not in {"local", "mineru"}:
        raise PreflightError("INVALID_EXTRACT_STRATEGY", "未知提取策略", field="extract_strategy")
    network_stages = {"translate", "note"}
    if network_policy == "offline" and (
        actual_strategy == "mineru" or network_stages.intersection(requested_stages)
    ):
        raise PreflightError(
            "OFFLINE_POLICY_CONFLICT",
            "完全离线模式不能使用 MinerU、翻译或 AI 笔记阶段",
            field="network_policy",
        )

    path = Path(source)
    if not path.is_absolute():
        path = Path(pdfs_dir) / path
    path = path.resolve()
    if not path.is_file():
        raise PreflightError(
            "SOURCE_NOT_FOUND",
            f"找不到输入文件: {path}",
            field="input_pdf",
        )
    extension = path.suffix.lower()
    if extension not in SUPPORTED_UPLOAD_EXTENSIONS:
        raise PreflightError(
            "UNSUPPORTED_FORMAT",
            f"当前不支持 {extension or '无扩展名'} 文件",
            status_code=415,
            field="file",
        )
    if "extract" in requested_stages:
        validate_file_signature(path, extension)
    return PreflightResult(path, extension, actual_strategy, network_policy, requested_stages)


def get_ingest_capabilities() -> dict[str, object]:
    """Return the single backend-owned format capability description."""

    try:
        import fitz  # type: ignore  # noqa: F401
        fitz_available = True
    except ImportError:
        fitz_available = False
    from app.pdf.epub import resolve_pandoc

    pandoc = resolve_pandoc()
    return {
        "formats": {
            "pdf": {
                "available": fitz_available,
                "engine": "pymupdf" if fitz_available else None,
                "degraded": False,
                "extensions": [".pdf"],
            },
            "epub": {
                "available": bool(fitz_available or pandoc),
                "engine": "pandoc" if pandoc else ("pymupdf" if fitz_available else None),
                "preferred_engine": "pandoc",
                "preferred_engine_available": bool(pandoc),
                "degraded": bool(fitz_available and not pandoc),
                "extensions": [".epub"],
                "message": "Pandoc 未安装，将使用本地兼容提取" if fitz_available and not pandoc else "",
            },
            "docx": {
                "available": False,
                "engine": None,
                "degraded": False,
                "extensions": [".docx"],
                "message": "当前版本尚未提供 DOCX 提取器",
            },
        },
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }
