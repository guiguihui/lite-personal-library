from __future__ import annotations

from pathlib import Path

import pytest

from app.index.v3.runtime import (
    CURRENT_POINTER,
    CurrentViewError,
    is_ready,
    load_current,
    open_current_view,
    publish_current,
)
from app.index.v3.supervisor import run_build


def _corpus(root: Path) -> Path:
    content = root / "content"
    note = content / "notes" / "welcome.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: Welcome\n---\n# Welcome\nalpha runtime body\n",
        encoding="utf-8",
    )
    return content


def test_publish_load_and_open_exact_v3_view(tmp_path: Path) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    result = run_build(content, pageindex, "incremental", legacy_export="none")

    published = publish_current(pageindex, result)
    loaded = load_current(pageindex)

    assert (pageindex / CURRENT_POINTER).is_file()
    assert loaded.pin == published.pin
    assert is_ready(pageindex) is True
    with open_current_view(pageindex) as view:
        assert view.documents()
        assert view.pin == published.pin


def test_missing_or_corrupt_publication_is_not_ready(tmp_path: Path) -> None:
    pageindex = tmp_path / "pageindex"
    assert is_ready(pageindex) is False
    with pytest.raises(CurrentViewError, match="has not been published"):
        load_current(pageindex)

    pageindex.mkdir()
    (pageindex / CURRENT_POINTER).write_text("{}", encoding="utf-8")
    assert is_ready(pageindex) is False
    with pytest.raises(CurrentViewError, match="envelope"):
        load_current(pageindex)
