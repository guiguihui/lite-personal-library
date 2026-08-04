"""Bounded, atomic staging for browser-provided upload bytes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import uuid

from fastapi import UploadFile

from .preflight import MAX_UPLOAD_BYTES, PreflightError, safe_filename


@dataclass(frozen=True, slots=True)
class StagedUpload:
    path: Path
    size_bytes: int
    sha256: str
    original_name: str


class UploadStore:
    def __init__(self, pdfs_dir: str | Path, *, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
        self.root = Path(pdfs_dir) / "_uploads"
        self.max_bytes = max_bytes

    async def stage(self, upload: UploadFile) -> StagedUpload:
        name = safe_filename(upload.filename or "")
        upload_dir = self.root / uuid.uuid4().hex
        upload_dir.mkdir(parents=True, exist_ok=False)
        final_path = upload_dir / name
        part_path = upload_dir / f"{name}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with part_path.open("xb") as stream:
                while block := await upload.read(1024 * 1024):
                    size += len(block)
                    if size > self.max_bytes:
                        raise PreflightError(
                            "UPLOAD_TOO_LARGE",
                            f"文件超过 {self.max_bytes} 字节上传上限",
                            status_code=413,
                            field="file",
                        )
                    digest.update(block)
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if size == 0:
                raise PreflightError("EMPTY_FILE", "文件为空", field="file")
            os.replace(part_path, final_path)
            return StagedUpload(final_path, size, digest.hexdigest(), name)
        except BaseException:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise
        finally:
            await upload.close()
