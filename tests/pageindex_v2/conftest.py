"""Shared fixtures for the PageIndex v2 test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def sample_content(tmp_path: Path) -> Path:
    """Create a small corpus containing every supported document shape."""
    content = tmp_path / "content"

    _write_markdown(content / "books" / "_index.md", "---\ntitle: Books\n---\n")
    _write_markdown(
        content / "books" / "alpha" / "_index.md",
        "---\ntitle: Alpha\n---\n",
    )
    _write_markdown(
        content / "books" / "alpha" / "ch01.md",
        "---\ntitle: First\nweight: 10\n---\n# First\nAlpha common text.\n",
    )
    _write_markdown(
        content / "books" / "alpha" / "ch02.md",
        "---\ntitle: Second\nweight: 20\n---\n# Second\nMore common text.\n",
    )
    _write_markdown(
        content / "books" / "missing-index" / "chapter.md",
        "# This directory is not a document\n",
    )

    _write_markdown(content / "papers" / "_index.md", "---\ntitle: Papers\n---\n")
    _write_markdown(
        content / "papers" / "beta" / "_index.md",
        "---\ntitle: Beta\n---\n# Abstract\nPaper common text.\n",
    )
    _write_markdown(
        content / "papers" / "loose.md",
        "# A loose paper is not a supported v2 document\n",
    )

    _write_markdown(content / "notes" / "_index.md", "---\ntitle: Notes\n---\n")
    _write_markdown(
        content / "notes" / "welcome.md",
        "---\ntitle: Welcome\n---\n# Welcome\nNote common text.\n",
    )
    _write_markdown(
        content / "notes" / "nested" / "ignored.md",
        "# Nested notes are not supported\n",
    )

    return content
