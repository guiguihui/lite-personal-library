from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.knowledge.catalog import _v3_headings


def test_heading_map_is_read_from_current_v3_view(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Ref:
        doc_uid = "uid"

    ref = Ref()
    owner = SimpleNamespace(doc_key="note:alpha")

    class FakeView:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def documents(self):
            return {"uid": owner}

        def document_chunk_refs(self, _uids):
            return (ref,)

        def get_chunks(self, _refs):
            return {ref: {"title": "Alpha Heading", "legacy_node_id": "0001"}}

    (tmp_path / "current-v3.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "app.index.v3.runtime.open_current_view",
        lambda _path: FakeView(),
    )

    assert _v3_headings(tmp_path) == {
        "note:alpha": (("alpha heading", "0001"),)
    }
