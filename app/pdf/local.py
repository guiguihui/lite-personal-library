"""本地 PDF 提取后端(PyMuPDF,离线零外部依赖)。

文本提取为主,图片提取可选(默认开,可关)。pdfplumber 备选(表格,暂未用)。
产出:out_dir/merged/book.md + out_dir/images/。

PyMuPDF(fitz)提取文本按页拼接,图片按页导出到 images/ 子目录,
引用形如 images/p{page}_img{idx}.webp(转 webp 减体积)。
"""

from __future__ import annotations

import io
import time
from app.text.normalization import normalize_extracted_text
from pathlib import Path

from app.pdf.base import ExtractResult


def _parse_pages(pages: str | None, total: int) -> list[int]:
    """解析页码范围(如 "1-50" "1,3,5")为 0-based 索引列表。

    None 或空 → 全部页。越界自动裁剪。
    """
    if not pages:
        return list(range(total))
    result: list[int] = []
    for part in pages.split(","):
        part = part.strip()
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo = max(1, int(lo_s))
            hi = min(total, int(hi_s))
            result.extend(range(lo - 1, hi))
        else:
            idx = int(part) - 1
            if 0 <= idx < total:
                result.append(idx)
    return sorted(set(result))


def _to_webp_bytes(pix_bytes: bytes, ext: str) -> tuple[bytes, str]:
    """尝试转 webp;失败则原样返回(保留原扩展名)。

    返回 (data, final_ext)。final_ext 不含点。
    """
    try:
        from PIL import Image  # type: ignore

        img = Image.open(io.BytesIO(pix_bytes))
        if img.mode in ("CMYK", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=85)
        return buf.getvalue(), "webp"
    except Exception:
        return pix_bytes, ext.lstrip(".")


class LocalExtractor:
    """PyMuPDF 本地提取器。离线,零外部依赖。"""

    def extract(
        self,
        input_path: Path,
        out_dir: Path,
        pages: str | None = None,
    ) -> ExtractResult:
        """提取 PDF 到 out_dir/merged/book.md + out_dir/images/。"""
        start = time.time()
        log: list[str] = []
        try:
            import fitz  # PyMuPDF  # type: ignore
        except ImportError as exc:
            return ExtractResult(
                ok=False,
                source_format="pdf",
                merged_path=out_dir / "merged" / "book.md",
                images_dir=out_dir / "images",
                error=f"PyMuPDF not installed: {exc}",
                duration_sec=time.time() - start,
                log=tuple(log),
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        merged_dir = out_dir / "merged"
        images_dir = out_dir / "images"
        merged_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        try:
            doc = fitz.open(str(input_path))
        except Exception as exc:
            log.append(f"[open error] {type(exc).__name__}: {exc}")
            return ExtractResult(
                ok=False,
                source_format="pdf",
                merged_path=merged_dir / "book.md",
                images_dir=images_dir,
                error=f"open failed: {exc}",
                duration_sec=time.time() - start,
                log=tuple(log),
            )

        total = doc.page_count
        page_indices = _parse_pages(pages, total)
        log.append(f"[info] pages: {len(page_indices)}/{total}")

        title = doc.metadata.get("title", "") or ""
        author = doc.metadata.get("author", "") or ""
        log.append(f"[info] title={title!r} author={author!r}")

        chunks: list[str] = []
        img_count = 0
        for idx in page_indices:
            page = doc.load_page(idx)
            flags = fitz.TEXTFLAGS_TEXT & ~fitz.TEXT_PRESERVE_LIGATURES
            text = page.get_text("text", flags=flags) or ""
            text, _ = normalize_extracted_text(text)
            chunks.append(f"\n\n<!-- page {idx + 1} -->\n\n{text}")
            # 图片提取(可选,失败不阻断)
            try:
                for img_info in page.get_images(full=True):
                    xref = img_info[0]
                    try:
                        pix = doc.extract_image(xref)
                        raw = pix.get("image", b"")
                        ext = pix.get("ext", "png")
                        data, final_ext = _to_webp_bytes(raw, ext)
                        fname = f"p{idx + 1}_img{img_count}.{final_ext}"
                        (images_dir / fname).write_bytes(data)
                        chunks.append(f"\n\n![](images/{fname})\n")
                        img_count += 1
                    except Exception:
                        continue
            except Exception:
                pass

        doc.close()

        merged_path = merged_dir / "book.md"
        merged_path.write_text("".join(chunks), encoding="utf-8")
        log.append(f"[done] merged={merged_path} images={img_count}")

        return ExtractResult(
            ok=True,
            source_format="pdf",
            merged_path=merged_path,
            images_dir=images_dir,
            title=title,
            author=author,
            page_count=len(page_indices),
            duration_sec=time.time() - start,
            log=tuple(log),
        )
