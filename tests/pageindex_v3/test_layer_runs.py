from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.index.v3.layer_codec import LayerDocument, PostingLayerReader
from app.index.v3.layer_runs import (
    LayerRunBuilder,
    LayerRunError,
    LayerRunReader,
    build_sorted_layer,
    iter_layer_run,
    merge_layer_runs,
)
from app.index.v3.models import ChunkRef, LayerPosting, SearchPosting, make_doc_uid
from app.index.v3.segment_projection import ChunkMetric


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _document(key: str = "note:one") -> LayerDocument:
    return LayerDocument(
        doc_key=key,
        doc_uid=make_doc_uid(key),
        segment_hash=_digest(f"segment:{key}"),
        chunk_metrics=(ChunkMetric(0, 1, 0, 3), ChunkMetric(1, 2, 1, 4)),
    )


def _rows() -> tuple[LayerPosting, ...]:
    return (
        LayerPosting("zulu", 0, 1, 0, 0, 2),
        LayerPosting("alpha", 0, 1, 1, 0, 0),
        LayerPosting("alpha", 0, 0, 0, 1, 2),
        LayerPosting("中", 0, 0, 1, 0, 1),
    )


def _key(row: LayerPosting) -> tuple[bytes, int, int]:
    return row.token.encode("utf-8"), row.doc_ordinal, row.local_id


def test_forced_one_row_runs_and_two_way_merge_are_bounded(tmp_path: Path) -> None:
    builder = LayerRunBuilder(tmp_path / "runs", max_run_bytes=1)
    for row in _rows():
        builder.add(row)
    built = builder.finish()

    assert len(built.paths) == len(_rows())
    assert built.records == len(_rows())
    assert built.run_buffer_peak_bytes == built.largest_record_bytes

    merged = merge_layer_runs(
        built.paths,
        tmp_path / "merged.p3r",
        fan_in=2,
    )
    assert merged.records == len(_rows())
    assert merged.peak_open_inputs <= 2
    assert merged.passes >= 2
    assert tuple(iter_layer_run(merged.path)) == tuple(sorted(_rows(), key=_key))


def test_duplicate_key_across_runs_fails_deterministically(tmp_path: Path) -> None:
    row = LayerPosting("same", 0, 0, 1, 0, 0)
    first = LayerRunBuilder(tmp_path / "first", max_run_bytes=1)
    first.add(row)
    first_path = first.finish().paths[0]
    second = LayerRunBuilder(tmp_path / "second", max_run_bytes=1)
    second.add(row)
    second_path = second.finish().paths[0]

    with pytest.raises(LayerRunError, match="duplicate"):
        merge_layer_runs((first_path, second_path), tmp_path / "duplicate.p3r", fan_in=2)


def test_run_reader_rejects_truncation_and_closes_windows_handle(tmp_path: Path) -> None:
    builder = LayerRunBuilder(tmp_path / "runs", max_run_bytes=1024)
    builder.add(_rows()[0])
    path = builder.finish().paths[0]
    path.write_bytes(path.read_bytes()[:-1])

    with pytest.raises(LayerRunError, match="truncated"):
        with LayerRunReader(path) as reader:
            tuple(reader)

    renamed = path.with_suffix(".renamed")
    path.rename(renamed)
    assert renamed.exists()


def test_build_sorted_layer_maps_logical_refs_and_is_input_order_independent(tmp_path: Path) -> None:
    document = _document()
    logical = tuple(
        SearchPosting(
            row.token,
            ChunkRef(document.doc_uid, document.segment_hash, row.local_id),
            row.title_tf,
            row.breadcrumb_tf,
            row.body_tf,
        )
        for row in _rows()
    )

    first = build_sorted_layer(
        tmp_path / "first-layer",
        documents=(document,),
        postings=logical,
        layer_kind="base",
        max_run_bytes=1,
        merge_fan_in=2,
    )
    second = build_sorted_layer(
        tmp_path / "second-layer",
        documents=(document,),
        postings=tuple(reversed(logical)),
        layer_kind="base",
        max_run_bytes=10_000,
        merge_fan_in=8,
    )

    for name in (
        "layer-documents.json",
        "postings.piv",
        "chunks.pcv",
        "terms.jsonl",
        "terms.sidx.json",
    ):
        assert (first.root / name).read_bytes() == (second.root / name).read_bytes()
    with PostingLayerReader(first) as reader:
        assert [row.chunk_ref.local_id for row in reader.iter_token("alpha")] == [0, 1]


def test_build_sorted_layer_rejects_foreign_or_unknown_chunk_refs(tmp_path: Path) -> None:
    document = _document()
    foreign = SearchPosting(
        "token",
        ChunkRef(document.doc_uid, _digest("other-segment"), 0),
        1,
        0,
        0,
    )
    with pytest.raises(LayerRunError, match="segment_hash"):
        build_sorted_layer(
            tmp_path / "foreign",
            documents=(document,),
            postings=(foreign,),
            layer_kind="base",
            max_run_bytes=1024,
            merge_fan_in=2,
        )


def test_failed_build_cleans_owned_scratch_directory(tmp_path: Path) -> None:
    document = _document()
    duplicate = SearchPosting(
        "duplicate",
        ChunkRef(document.doc_uid, document.segment_hash, 0),
        1,
        0,
        0,
    )
    with pytest.raises(LayerRunError, match="duplicate"):
        build_sorted_layer(
            tmp_path / "failed",
            documents=(document,),
            postings=(duplicate, duplicate),
            layer_kind="base",
            max_run_bytes=1,
            merge_fan_in=2,
        )

    assert not (tmp_path / "failed").exists()
    assert not tuple(tmp_path.glob(".failed.layer-build.*"))

def test_run_reader_rejects_exact_record_boundary_truncation(
    tmp_path: Path,
) -> None:
    rows = (
        LayerPosting("a", 0, 0, 1, 0, 0),
        LayerPosting("b", 0, 0, 1, 0, 0),
    )
    builder = LayerRunBuilder(tmp_path / "boundary-runs", max_run_bytes=10_000)
    for row in rows:
        builder.add(row)
    path = builder.finish().paths[0]
    payload = path.read_bytes()
    header_bytes = 8 + 8
    first_record_bytes = 4 + 1 + 5
    path.write_bytes(payload[: header_bytes + first_record_bytes])

    with pytest.raises(LayerRunError, match="attested record count"):
        tuple(iter_layer_run(path))


def test_run_reader_rejects_same_size_record_mutation(tmp_path: Path) -> None:
    builder = LayerRunBuilder(tmp_path / "mutated-runs", max_run_bytes=10_000)
    builder.add(LayerPosting("a", 0, 0, 1, 0, 0))
    path = builder.finish().paths[0]
    payload = bytearray(path.read_bytes())
    token_offset = 8 + 8 + 4
    payload[token_offset] = ord("b")
    path.write_bytes(payload)

    with pytest.raises(LayerRunError, match="digest footer"):
        tuple(iter_layer_run(path))


def test_non_bmp_token_is_charged_by_resident_representation(
    tmp_path: Path,
) -> None:
    prefix = "a" * 50_000 + "😀"
    rows = (
        LayerPosting(prefix + "a", 0, 0, 1, 0, 0),
        LayerPosting(prefix + "b", 0, 0, 1, 0, 0),
    )
    builder = LayerRunBuilder(tmp_path / "unicode-runs", max_run_bytes=300_000)
    for row in rows:
        builder.add(row)
    built = builder.finish()

    assert len(built.paths) == 2
    assert built.run_buffer_peak_bytes < 60_000
    assert 200_000 < built.run_resident_peak_bytes <= 300_000
