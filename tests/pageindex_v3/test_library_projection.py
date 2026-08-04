from __future__ import annotations

from pathlib import Path

from app.index.v2.object_store import put_segment
from app.index.v3.segment_projection import SegmentProjector
from .test_segment_projection import _valid_segment


def test_projection_returns_document_tree_and_source_fingerprint(tmp_path: Path) -> None:
    segment = _valid_segment("paper:alpha")
    segment["document"].update(
        {
            "title": "Alpha",
            "author": "Ada",
            "description": "A paper",
            "tags": ["qa"],
            "path": "/papers/alpha/",
            "url": "/papers/alpha.html",
            "year": "2026",
        }
    )
    segment["document_tree"] = {
        "doc_name": "alpha",
        "type": "paper",
        "title": "Alpha",
        "author": "Ada",
        "description": "A paper",
        "tags": ["qa"],
        "year": "2026",
        "structure": [{"node_id": "1", "title": "Intro"}],
    }
    pageindex = tmp_path / "pageindex"
    ref = put_segment(pageindex, segment)

    projection = SegmentProjector(pageindex).load_document(ref)

    assert projection.doc_key == "paper:alpha"
    assert projection.document_tree["structure"]
    assert projection.source_files[0]["sha256"] == "a" * 64
