from __future__ import annotations

from app.retrieval.fuse import rrf_fuse
from app.retrieval.rerank import lexical_rerank
from app.retrieval.search import Hit, _per_doc_truncate


def _hit(
    *,
    doc_key: str,
    doc_uid: str,
    node_key: str,
    score: float = 1.0,
    local_id: int = 0,
) -> Hit:
    slug = doc_key.split(":", 1)[1]
    return Hit(
        node={
            "doc_id": slug,
            "node_id": "0001",
            "title": "alpha",
            "breadcrumb": [],
            "summary": "alpha",
        },
        score=score,
        tokens=["alpha"],
        positions={"alpha": {"title": 0}},
        generation="a" * 64,
        view_id="b" * 64,
        doc_key=doc_key,
        doc_uid=doc_uid,
        segment_hash="c" * 64,
        local_id=local_id,
        node_key=node_key,
    )


def test_rrf_uses_stable_identity_for_same_slug_cross_type() -> None:
    book = _hit(doc_key="book:shared", doc_uid="1" * 64, node_key="root")
    note = _hit(doc_key="note:shared", doc_uid="2" * 64, node_key="root")

    fused = rrf_fuse([[book, note]])

    assert [hit.doc_key for hit in fused] == ["book:shared", "note:shared"]


def test_rrf_merges_same_stable_node_and_preserves_reference_metadata() -> None:
    lower = _hit(
        doc_key="book:alpha",
        doc_uid="1" * 64,
        node_key="chapter",
        score=1.0,
    )
    higher = _hit(
        doc_key="book:alpha",
        doc_uid="1" * 64,
        node_key="chapter",
        score=2.0,
        local_id=1,
    )

    fused = rrf_fuse([[lower], [higher]])

    assert len(fused) == 1
    assert fused[0].local_id == 1
    assert fused[0].generation == "a" * 64
    assert fused[0].rrf_score == 2 / 61


def test_per_doc_limit_does_not_merge_same_slug_cross_type() -> None:
    scored = [
        *(
            _hit(
                doc_key="book:shared",
                doc_uid="1" * 64,
                node_key=f"book-{index}",
                local_id=index,
            )
            for index in range(4)
        ),
        *(
            _hit(
                doc_key="note:shared",
                doc_uid="2" * 64,
                node_key=f"note-{index}",
                local_id=index,
            )
            for index in range(4)
        ),
    ]

    truncated = _per_doc_truncate(scored, 6)

    assert len(truncated) == 6
    assert sum(hit.doc_key == "book:shared" for hit in truncated) == 3
    assert sum(hit.doc_key == "note:shared" for hit in truncated) == 3


def test_lexical_rerank_preserves_stable_reference_metadata() -> None:
    hit = _hit(doc_key="paper:alpha", doc_uid="3" * 64, node_key="root")

    reranked = lexical_rerank(["alpha"], "alpha", [hit])

    assert len(reranked) == 1
    assert reranked[0].doc_key == hit.doc_key
    assert reranked[0].doc_uid == hit.doc_uid
    assert reranked[0].segment_hash == hit.segment_hash
    assert reranked[0].local_id == hit.local_id
    assert reranked[0].node_key == hit.node_key