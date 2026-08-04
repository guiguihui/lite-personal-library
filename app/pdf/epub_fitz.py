"""Lightweight EPUB compatibility extractor backed by bundled PyMuPDF."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.pdf.base import ExtractResult
from app.pdf.local import LocalExtractor


class EpubFitzExtractor:
    """Extract reflowed EPUB pages without requiring the Pandoc binary."""

    def extract(
        self,
        input_path: Path,
        out_dir: Path,
        pages: str | None = None,
    ) -> ExtractResult:
        result = LocalExtractor().extract(input_path, out_dir, pages)
        return replace(
            result,
            source_format="epub",
            log=("[info] EPUB compatibility engine: PyMuPDF", *result.log),
        )
