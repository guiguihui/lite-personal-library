"""EPUB 提取后端(pandoc 外部命令)。

按 docs/development.md L9:pandoc(可选,EPUB 提取用)。
pandoc 把 EPUB 转 markdown,图片复制到 images/ 子目录。
产出:out_dir/merged/book.md + out_dir/images/。

pandoc 未安装时返回 ok=False + 明确错误。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from app.pdf.base import ExtractResult


class EpubExtractor:
    """pandoc EPUB 提取器。需外部 pandoc 命令。"""

    def extract(
        self,
        input_path: Path,
        out_dir: Path,
        pages: str | None = None,
    ) -> ExtractResult:
        """提取 EPUB 到 out_dir/merged/book.md + out_dir/images/。

        pages 参数对 EPUB 无意义(pandoc 不支持页码),忽略。
        """
        start = time.time()
        log: list[str] = []
        merged_dir = out_dir / "merged"
        images_dir = out_dir / "images"
        merged_path = merged_dir / "book.md"

        out_dir.mkdir(parents=True, exist_ok=True)
        merged_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        pandoc = shutil.which("pandoc")
        if not pandoc:
            log.append("[error] pandoc not found in PATH")
            return ExtractResult(
                ok=False,
                source_format="epub",
                merged_path=merged_path,
                images_dir=images_dir,
                error="pandoc not installed",
                duration_sec=time.time() - start,
                log=tuple(log),
            )

        # pandoc --extract-media 指定图片输出目录
        media_dir = out_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            pandoc,
            "-f", "epub",
            "-t", "markdown",
            f"--extract-media={media_dir}",
            "-o", str(merged_path),
            str(input_path),
        ]
        log.append(f"[run] {' '.join(cmd)}")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            log.append("[error] pandoc timeout (300s)")
            return ExtractResult(
                ok=False,
                source_format="epub",
                merged_path=merged_path,
                images_dir=images_dir,
                error="pandoc timeout",
                duration_sec=time.time() - start,
                log=tuple(log),
            )
        except Exception as exc:
            log.append(f"[error] {type(exc).__name__}: {exc}")
            return ExtractResult(
                ok=False,
                source_format="epub",
                merged_path=merged_path,
                images_dir=images_dir,
                error=f"pandoc failed: {exc}",
                duration_sec=time.time() - start,
                log=tuple(log),
            )

        if proc.returncode != 0:
            log.append(f"[error] pandoc exit={proc.returncode}")
            log.append(f"[stderr] {proc.stderr[:500]}")
            return ExtractResult(
                ok=False,
                source_format="epub",
                merged_path=merged_path,
                images_dir=images_dir,
                error=f"pandoc exit {proc.returncode}",
                duration_sec=time.time() - start,
                log=tuple(log),
            )

        # 把 media/ 下的图片统一移到 images/(pandoc 输出的引用形如 media/xxx)
        img_count = 0
        if media_dir.is_dir():
            for img_path in media_dir.rglob("*"):
                if img_path.is_file() and img_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
                    target = images_dir / img_path.name
                    shutil.move(str(img_path), str(target))
                    img_count += 1
            # 清理空 media 目录
            try:
                shutil.rmtree(media_dir)
            except Exception:
                pass

        # 修正 markdown 里的图片引用:media/xxx → images/xxx
        if merged_path.exists():
            text = merged_path.read_text(encoding="utf-8")
            text = text.replace("media/", "images/")
            merged_path.write_text(text, encoding="utf-8")

        log.append(f"[done] merged={merged_path} images={img_count}")
        return ExtractResult(
            ok=True,
            source_format="epub",
            merged_path=merged_path,
            images_dir=images_dir,
            page_count=0,
            duration_sec=time.time() - start,
            log=tuple(log),
        )
