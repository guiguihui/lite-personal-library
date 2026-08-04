from __future__ import annotations

from pathlib import Path
import sys

import pytest

from app.ingest.preflight import PreflightError, preflight_source


def _load_clean():
    vendor = Path(__file__).resolve().parents[1] / "app" / "vendor"
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    from clean_markdown import clean

    return clean


def test_offline_clean_never_calls_classifier() -> None:
    called = 0

    def forbidden(_items):
        nonlocal called
        called += 1
        raise AssertionError("network classifier called")

    cleaned, stats = _load_clean()(
        "1.1 Introduction\nBody",
        heading_mode="regex",
        classifier=forbidden,
    )
    assert called == 0
    assert cleaned.startswith("### 1.1 Introduction")
    assert stats["classifier"] == "regex"
    assert stats["llm_attempted"] is False


def test_offline_rejects_network_stages_before_job(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(PreflightError) as caught:
        preflight_source(
            source,
            pdfs_dir=tmp_path,
            slug="book",
            strategy="local",
            network_policy="offline",
            stages=("extract", "translate"),
        )
    assert caught.value.code == "OFFLINE_POLICY_CONFLICT"
