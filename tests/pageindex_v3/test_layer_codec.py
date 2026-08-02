from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct

import pytest

from app.index.v2.artifacts import ArtifactRef
from app.index.v2.canonical import canonical_bytes
from app.index.v3.layer_codec import (
    CHUNKS_MAGIC,
    POSTINGS_MAGIC,
    LayerCodecError,
    LayerDocument,
    PostingLayerReader,
    PostingLayerReceipt,
    TokenContribution,
    write_posting_layer,
)
from app.index.v3.models import ChunkRef, LayerPosting, SearchPosting, make_doc_uid
from app.index.v3.segment_projection import ChunkMetric
from app.index.v3.varint import read_uvarint


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _documents() -> tuple[LayerDocument, ...]:
    values = (
        LayerDocument(
            doc_key="note:alpha",
            doc_uid=make_doc_uid("note:alpha"),
            segment_hash=_digest("alpha-segment"),
            chunk_metrics=(
                ChunkMetric(0, 2, 1, 7),
                ChunkMetric(1, 1, 0, 4),
            ),
        ),
        LayerDocument(
            doc_key="note:beta",
            doc_uid=make_doc_uid("note:beta"),
            segment_hash=_digest("beta-segment"),
            chunk_metrics=(ChunkMetric(0, 3, 2, 5),),
        ),
    )
    return tuple(sorted(values, key=lambda item: item.doc_uid.encode("utf-8")))


def _ordinal(documents: tuple[LayerDocument, ...], key: str) -> int:
    return next(index for index, item in enumerate(documents) if item.doc_key == key)


def _base_rows(documents: tuple[LayerDocument, ...]) -> tuple[LayerPosting, ...]:
    alpha = _ordinal(documents, "note:alpha")
    beta = _ordinal(documents, "note:beta")
    rows = (
        LayerPosting("apple", alpha, 0, 2, 1, 4),
        LayerPosting("apple", alpha, 1, 1, 0, 0),
        LayerPosting("apple", beta, 0, 0, 0, 3),
        LayerPosting("body-only", alpha, 1, 0, 0, 2),
        LayerPosting("标题", beta, 0, 1, 2, 1),
    )
    return tuple(sorted(rows, key=lambda row: (row.token.encode("utf-8"), row.doc_ordinal, row.local_id)))


def _write_base(tmp_path: Path) -> tuple[PostingLayerReceipt, tuple[LayerDocument, ...]]:
    documents = _documents()
    receipt = write_posting_layer(
        tmp_path / "layer",
        documents=documents,
        postings=_base_rows(documents),
        layer_kind="base",
    )
    return receipt, documents


def test_writer_emits_five_attested_seekable_artifacts(tmp_path: Path) -> None:
    receipt, documents = _write_base(tmp_path)

    assert receipt.document_count == 2
    assert receipt.chunk_count == 3
    assert receipt.term_count == 3
    assert receipt.nonbody_rows == 3
    assert receipt.body_rows == 4
    assert receipt.documents.records == 2
    assert receipt.chunks.records == 3
    assert receipt.terms.records == 3
    assert (receipt.root / "postings.piv").read_bytes().startswith(POSTINGS_MAGIC)
    assert (receipt.root / "chunks.pcv").read_bytes().startswith(CHUNKS_MAGIC)
    assert set(path.name for path in receipt.root.iterdir()) == {
        "layer-documents.json",
        "postings.piv",
        "chunks.pcv",
        "terms.jsonl",
        "terms.sidx.json",
    }

    round_trip = PostingLayerReceipt.from_dict(receipt.root, receipt.as_dict())
    assert round_trip == receipt
    assert round_trip.search_view_recipe_hash == receipt.search_view_recipe_hash


def test_reader_restores_complete_chunk_refs_and_split_fields(tmp_path: Path) -> None:
    receipt, documents = _write_base(tmp_path)
    by_ordinal = {index: document for index, document in enumerate(documents)}

    with PostingLayerReader(receipt) as reader:
        rows = list(reader.iter_token("apple"))
        nonbody_only = list(reader.iter_token("apple", include_body=False))
        missing = list(reader.iter_token("missing"))

    assert rows == [
        # Sort order is the compact physical key, restored to stable ChunkRef values.
        SearchPosting(
            "apple",
            ChunkRef(
                by_ordinal[row.doc_ordinal].doc_uid,
                by_ordinal[row.doc_ordinal].segment_hash,
                row.local_id,
            ),
            row.title_tf,
            row.breadcrumb_tf,
            row.body_tf,
        )
        for row in _base_rows(documents)
        if row.token == "apple"
    ]
    assert [(row.chunk_ref, row.title_tf, row.breadcrumb_tf, row.body_tf) for row in nonbody_only] == [
        (rows[0].chunk_ref, 2, 1, 0),
        (rows[1].chunk_ref, 1, 0, 0),
    ]
    assert missing == []


def test_reader_gets_only_requested_document_metric_blocks(tmp_path: Path) -> None:
    receipt, documents = _write_base(tmp_path)
    target = next(document for document in documents if document.doc_key == "note:alpha")
    refs = (
        ChunkRef(target.doc_uid, target.segment_hash, 1),
        ChunkRef(target.doc_uid, target.segment_hash, 0),
    )

    with PostingLayerReader(receipt) as reader:
        metrics = reader.get_chunk_metrics(refs)

    assert metrics == {
        refs[0]: ChunkMetric(1, 1, 0, 4),
        refs[1]: ChunkMetric(0, 2, 1, 7),
    }


def test_body_pruned_lookup_physically_skips_body_rows(tmp_path: Path) -> None:
    receipt, _documents_value = _write_base(tmp_path)
    reads: list[tuple[str, int, int]] = []

    with PostingLayerReader(receipt) as reader:
        term = reader.lookup_term("body-only")
        assert term is not None
        with (receipt.root / "postings.piv").open("rb", buffering=0) as stream:
            stream.seek(term.block_offset)
            token_size = struct.unpack(">I", stream.read(4))[0]
            stream.seek(token_size, 1)
            assert struct.unpack(">Q", stream.read(8))[0] == 0
            assert struct.unpack(">Q", stream.read(8))[0] == 1
            body_start = stream.tell()

        reader.read_observer = lambda name, offset, size: reads.append((name, offset, size))
        assert list(reader.iter_token("body-only", include_body=False)) == []

    posting_reads = [(offset, size) for name, offset, size in reads if name == "postings.piv"]
    assert posting_reads
    assert all(offset + size <= body_start for offset, size in posting_reads)


def test_interleaved_token_iterators_have_independent_offsets(tmp_path: Path) -> None:
    receipt, _documents_value = _write_base(tmp_path)
    with PostingLayerReader(receipt) as reader:
        apples = iter(reader.iter_token("apple"))
        title = iter(reader.iter_token("标题"))
        first = next(apples)
        only_title = next(title)
        second = next(apples)
        assert first.token == second.token == "apple"
        assert only_title.token == "标题"
        assert list(title) == []
        assert len([first, second, *apples]) == 3


def test_delta_keeps_disappeared_tokens_and_zero_net_posting_tokens(tmp_path: Path) -> None:
    documents = _documents()[:1]
    rows = (LayerPosting("stable", 0, 0, 1, 0, 2),)
    contributions = (
        TokenContribution("gone", -1, -1, 0),
        TokenContribution("stable", 0, 0, 0),
    )
    receipt = write_posting_layer(
        tmp_path / "delta",
        documents=documents,
        postings=rows,
        token_contributions=contributions,
        layer_kind="delta",
    )

    with PostingLayerReader(receipt) as reader:
        gone = reader.lookup_term("gone")
        stable = reader.lookup_term("stable")
        assert gone is not None and gone.block_offset is None and gone.block_bytes == 0
        assert (gone.df_any_delta, gone.df_nonbody_delta, gone.df_body_delta) == (-1, -1, 0)
        assert stable is not None and stable.block_offset is not None
        assert (stable.df_any_delta, stable.df_nonbody_delta, stable.df_body_delta) == (0, 0, 0)


def test_sparse_lookup_scans_at_most_stride_lines(tmp_path: Path) -> None:
    document = _documents()[0]
    rows = tuple(
        LayerPosting(f"token-{index:04d}", 0, 0, 1, 0, 0)
        for index in range(300)
    )
    receipt = write_posting_layer(
        tmp_path / "many-terms",
        documents=(document,),
        postings=rows,
        layer_kind="base",
    )

    with PostingLayerReader(receipt) as reader:
        assert reader.lookup_term("token-0255") is not None
        assert 1 <= reader.last_sparse_scan_lines <= 128
        assert reader.lookup_term("token-0255x") is None
        assert 1 <= reader.last_sparse_scan_lines <= 128


def test_batched_sparse_lookup_canonicalizes_dedupes_and_reads_each_window_once(
    tmp_path: Path,
) -> None:
    document = _documents()[0]
    rows = tuple(
        LayerPosting(f"token-{index:04d}", 0, 0, 1, 0, 0)
        for index in range(300)
    )
    receipt = write_posting_layer(
        tmp_path / "batched-terms",
        documents=(document,),
        postings=rows,
        layer_kind="base",
    )
    sparse = json.loads(
        (receipt.root / "terms.sidx.json").read_text("utf-8")
    )
    selected_windows = (sparse["anchors"][0], sparse["anchors"][1])
    reads: list[tuple[str, int, int]] = []

    with PostingLayerReader(
        receipt,
        read_observer=lambda name, offset, size: reads.append(
            (name, offset, size)
        ),
    ) as reader:
        reads.clear()
        found = reader.lookup_terms(
            token
            for token in (
                "token-0200",
                "token-0001",
                "token-0127",
                "token-0200",
                "token-0127x",
                "000-before-first-anchor",
            )
        )

        expected_keys = sorted(
            {
                "token-0200",
                "token-0001",
                "token-0127",
                "token-0127x",
                "000-before-first-anchor",
            },
            key=lambda token: token.encode("utf-8"),
        )
        assert list(found) == expected_keys
        assert found["token-0001"] is not None
        assert found["token-0127"] is not None
        assert found["token-0200"] is not None
        assert found["token-0127x"] is None
        assert found["000-before-first-anchor"] is None
        assert reader.last_sparse_windows_read == 2
        assert reader.last_sparse_scan_lines == 256

        assert {name for name, _offset, _size in reads} == {"terms.jsonl"}
        term_ranges = sorted(
            (offset, offset + size)
            for name, offset, size in reads
            if name == "terms.jsonl"
        )
        expected_ranges = sorted(
            (anchor[1], anchor[1] + anchor[2])
            for anchor in selected_windows
        )
        assert sum(end - start for start, end in term_ranges) == sum(
            end - start for start, end in expected_ranges
        )
        for expected_start, expected_end in expected_ranges:
            covered = [
                (start, end)
                for start, end in term_ranges
                if expected_start <= start < expected_end
            ]
            assert covered[0][0] == expected_start
            assert covered[-1][1] == expected_end
            assert all(
                left[1] == right[0]
                for left, right in zip(covered, covered[1:])
            )


def test_batched_sparse_lookup_validates_inputs_and_resets_work_counters(
    tmp_path: Path,
) -> None:
    receipt, _documents_value = _write_base(tmp_path)

    with PostingLayerReader(receipt) as reader:
        assert reader.lookup_terms(["apple"])["apple"] is not None
        assert reader.last_sparse_windows_read == 1
        assert reader.last_sparse_scan_lines == receipt.term_count

        assert reader.lookup_terms([]) == {}
        assert reader.last_sparse_windows_read == 0
        assert reader.last_sparse_scan_lines == 0

        with pytest.raises(TypeError, match="iterable of tokens"):
            reader.lookup_terms("apple")
        with pytest.raises(TypeError, match="token must be a string"):
            reader.lookup_terms(["apple", 1])
        with pytest.raises(ValueError, match="non-empty"):
            reader.lookup_terms([""])
        with pytest.raises(ValueError, match="valid UTF-8"):
            reader.lookup_terms(["\ud800"])


def test_batched_sparse_lookup_rejects_bad_window_digest_and_sorting(
    tmp_path: Path,
) -> None:
    document = _documents()[0]
    rows = tuple(
        LayerPosting(f"token-{index:04d}", 0, 0, 1, 0, 0)
        for index in range(129)
    )
    receipt = write_posting_layer(
        tmp_path / "corrupt-batch",
        documents=(document,),
        postings=rows,
        layer_kind="base",
    )
    terms_path = receipt.root / "terms.jsonl"
    lines = terms_path.read_bytes().splitlines(keepends=True)
    record = json.loads(lines[1])
    record["token"] = "token-0000"
    lines[1] = canonical_bytes(record) + b"\n"
    terms_path.write_bytes(b"".join(lines))
    receipt = _rebind_artifact(receipt, "terms")

    sparse_path = receipt.root / "terms.sidx.json"
    sparse = json.loads(sparse_path.read_text("utf-8"))
    sparse["terms_sha256"] = receipt.terms.sha256
    sparse_path.write_bytes(canonical_bytes(sparse))
    receipt = _rebind_artifact(receipt, "sparse_index")

    with PostingLayerReader(receipt) as reader:
        with pytest.raises(LayerCodecError, match="strictly sorted"):
            reader.lookup_terms(["token-0000", "token-0001"])
        assert reader.last_sparse_windows_read == 1
        assert reader.last_sparse_scan_lines == 2

    # Restore sorting but mutate an attested field while retaining the old window digest.
    record["token"] = "token-0001"
    prefix_sha256 = record["prefix_sha256"]
    assert isinstance(prefix_sha256, str)
    record["prefix_sha256"] = (
        ("1" if prefix_sha256[0] == "0" else "0") + prefix_sha256[1:]
    )
    lines[1] = canonical_bytes(record) + b"\n"
    terms_path.write_bytes(b"".join(lines))
    receipt = _rebind_artifact(receipt, "terms")
    sparse["terms_sha256"] = receipt.terms.sha256
    sparse_path.write_bytes(canonical_bytes(sparse))
    receipt = _rebind_artifact(receipt, "sparse_index")

    with PostingLayerReader(receipt) as reader:
        with pytest.raises(LayerCodecError, match="SHA-256"):
            reader.lookup_terms(["token-0001", "token-0127"])
        assert reader.last_sparse_windows_read == 1
        assert reader.last_sparse_scan_lines == 128


def test_batched_sparse_lookup_rejects_bad_sparse_stride(tmp_path: Path) -> None:
    receipt, _documents_value = _write_base(tmp_path)
    sparse_path = receipt.root / "terms.sidx.json"
    sparse = json.loads(sparse_path.read_text("utf-8"))
    sparse["stride"] = 64
    sparse_path.write_bytes(canonical_bytes(sparse))
    receipt = _rebind_artifact(receipt, "sparse_index")

    with pytest.raises(LayerCodecError, match="unsupported sparse term index"):
        PostingLayerReader(receipt)


def test_reader_rejects_artifact_mutation_before_yield(tmp_path: Path) -> None:
    receipt, _documents_value = _write_base(tmp_path)
    path = receipt.root / receipt.postings.relative_path
    payload = bytearray(path.read_bytes())
    payload[-1] ^= 1
    path.write_bytes(payload)

    with PostingLayerReader(receipt) as reader:
        rows = iter(reader.iter_token("标题"))
        with pytest.raises(LayerCodecError, match="SHA-256"):
            next(rows)


def test_receipt_rejects_wrong_role_paths_and_out_of_range_counts(tmp_path: Path) -> None:
    receipt, _documents_value = _write_base(tmp_path)
    bad_ref = ArtifactRef("other.piv", receipt.postings.sha256, receipt.postings.byte_size, receipt.postings.records)
    with pytest.raises(ValueError, match="postings.piv"):
        replace(receipt, postings=bad_ref)
    with pytest.raises(ValueError, match="u64"):
        replace(receipt, body_rows=2**64)


@pytest.mark.parametrize(
    "documents",
    [
        lambda values: tuple(reversed(values)),
        lambda values: values + (values[0],),
    ],
)
def test_writer_rejects_noncanonical_document_tables(tmp_path: Path, documents) -> None:
    values = _documents()
    with pytest.raises((TypeError, ValueError), match="document"):
        write_posting_layer(
            tmp_path / "bad-documents",
            documents=documents(values),
            postings=(),
            layer_kind="base",
        )


def test_writer_rejects_duplicate_or_nonmonotonic_postings(tmp_path: Path) -> None:
    documents = _documents()
    row = LayerPosting("token", 0, 0, 1, 0, 0)
    with pytest.raises(LayerCodecError, match="duplicate"):
        write_posting_layer(
            tmp_path / "duplicate",
            documents=documents,
            postings=(row, row),
            layer_kind="base",
        )


def test_clean_empty_layer_contains_only_binary_magics(tmp_path: Path) -> None:
    receipt = write_posting_layer(
        tmp_path / "empty",
        documents=(),
        postings=(),
        layer_kind="base",
    )
    assert (receipt.root / "postings.piv").read_bytes() == POSTINGS_MAGIC
    assert (receipt.root / "chunks.pcv").read_bytes() == CHUNKS_MAGIC
    assert (receipt.root / "terms.jsonl").read_bytes() == b""
    with PostingLayerReader(receipt) as reader:
        assert list(reader.iter_token("anything")) == []
        assert reader.get_chunk_metrics(()) == {}

def _rebind_artifact(
    receipt: PostingLayerReceipt,
    field: str,
) -> PostingLayerReceipt:
    reference = getattr(receipt, field)
    path = receipt.root / reference.relative_path
    payload = path.read_bytes()
    rebound = ArtifactRef(
        reference.relative_path,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        reference.records,
    )
    return replace(receipt, **{field: rebound})


def test_hot_open_and_lookup_do_not_scan_large_base_artifacts(
    tmp_path: Path,
) -> None:
    document = LayerDocument(
        doc_key="note:many-terms",
        doc_uid=make_doc_uid("note:many-terms"),
        segment_hash=_digest("many-terms-segment"),
        chunk_metrics=(ChunkMetric(0, 1, 0, 1),),
    )
    rows = tuple(
        LayerPosting(f"term-{index:04d}", 0, 0, 1, 0, 0)
        for index in range(300)
    )
    receipt = write_posting_layer(
        tmp_path / "many-terms",
        documents=(document,),
        postings=rows,
        layer_kind="base",
    )
    sparse = json.loads(
        (receipt.root / "terms.sidx.json").read_text("utf-8")
    )
    target_anchor = sparse["anchors"][1]
    assert target_anchor[4] == 128
    reads: list[tuple[str, int, int]] = []

    with PostingLayerReader(
        receipt,
        read_observer=lambda name, offset, size: reads.append(
            (name, offset, size)
        ),
    ) as reader:
        assert reader.startup_bytes_read["postings.piv"] == 0
        assert reader.startup_bytes_read["chunks.pcv"] == 0
        assert reader.startup_bytes_read["terms.jsonl"] == 0
        assert {name for name, _offset, _size in reads} == {
            "layer-documents.json",
            "terms.sidx.json",
        }

        reads.clear()
        assert reader.lookup_term("term-0200") is not None
        term_reads = [
            (offset, size)
            for name, offset, size in reads
            if name == "terms.jsonl"
        ]
        assert {name for name, _offset, _size in reads} == {"terms.jsonl"}
        assert sum(size for _offset, size in term_reads) == target_anchor[2]
        assert target_anchor[2] < receipt.terms.byte_size // 2
        assert reader.last_sparse_scan_lines == 128


def test_sparse_only_reader_defers_document_table_and_rejects_non_bool_flag(
    tmp_path: Path,
) -> None:
    receipt, _documents_value = _write_base(tmp_path)
    reads: list[tuple[str, int, int]] = []

    with PostingLayerReader(
        receipt,
        load_documents=False,
        read_observer=lambda name, offset, size: reads.append(
            (name, offset, size)
        ),
    ) as reader:
        assert reader.startup_bytes_read["layer-documents.json"] == 0
        assert {name for name, _offset, _size in reads} == {
            "terms.sidx.json"
        }
        reads.clear()
        terms = reader.lookup_terms(["apple", "missing"])
        assert terms["apple"] is not None
        assert terms["missing"] is None
        assert {name for name, _offset, _size in reads} == {"terms.jsonl"}
        assert reader.startup_bytes_read["layer-documents.json"] == 0

    for invalid in (0, 1, None, "false"):
        with pytest.raises(TypeError, match="load_documents must be a bool"):
            PostingLayerReader(receipt, load_documents=invalid)  # type: ignore[arg-type]


def test_lazy_ownership_paths_load_documents_once_and_audit_does_not_rehash(
    tmp_path: Path,
) -> None:
    receipt, documents = _write_base(tmp_path)
    reads: list[tuple[str, int, int]] = []
    first = documents[0]
    ref = ChunkRef(first.doc_uid, first.segment_hash, 0)

    with PostingLayerReader(
        receipt,
        load_documents=False,
        read_observer=lambda name, offset, size: reads.append(
            (name, offset, size)
        ),
    ) as reader:
        assert reader.get_chunk_metrics((ref,))[ref] == first.chunk_metrics[0]
        assert reader.startup_bytes_read["layer-documents.json"] == (
            receipt.documents.byte_size
        )
        assert list(reader.iter_token("apple"))
        reader.audit()

    document_reads = [
        size for name, _offset, size in reads if name == "layer-documents.json"
    ]
    assert sum(document_reads) == receipt.documents.byte_size


def test_sparse_only_reader_defers_but_never_trusts_corrupt_documents(
    tmp_path: Path,
) -> None:
    receipt, documents = _write_base(tmp_path)
    path = receipt.root / receipt.documents.relative_path
    payload = bytearray(path.read_bytes())
    payload[len(payload) // 2] ^= 1
    path.write_bytes(payload)
    first = documents[0]
    ref = ChunkRef(first.doc_uid, first.segment_hash, 0)

    with PostingLayerReader(receipt, load_documents=False) as reader:
        assert reader.lookup_term("apple") is not None
        with pytest.raises(LayerCodecError, match="SHA-256"):
            reader.get_chunk_metrics((ref,))

def test_pcv_block_hash_rejects_same_size_local_id_mutation(tmp_path: Path) -> None:
    receipt, documents = _write_base(tmp_path)
    table = json.loads((receipt.root / "layer-documents.json").read_text("utf-8"))
    first = table["documents"][0]
    chunks_path = receipt.root / "chunks.pcv"
    payload = bytearray(chunks_path.read_bytes())
    # Small fixture: ordinal and chunk_count are one-byte varints, followed by local_id=0.
    local_id_offset = first["chunk_block_offset"] + 2
    assert payload[local_id_offset] == 0
    payload[local_id_offset] = 1
    chunks_path.write_bytes(payload)
    receipt = _rebind_artifact(receipt, "chunks")
    document = documents[0]
    ref = ChunkRef(document.doc_uid, document.segment_hash, 0)

    with PostingLayerReader(receipt) as reader:
        with pytest.raises(LayerCodecError, match="local block SHA-256"):
            reader.get_chunk_metrics((ref,))


def test_deep_audit_rejects_rebound_wrong_sparse_window_hash(tmp_path: Path) -> None:
    receipt, _documents_value = _write_base(tmp_path)
    sparse_path = receipt.root / "terms.sidx.json"
    sparse = json.loads(sparse_path.read_text("utf-8"))
    sparse["anchors"][0][3] = "0" * 64
    sparse_path.write_bytes(canonical_bytes(sparse))
    receipt = _rebind_artifact(receipt, "sparse_index")

    with PostingLayerReader(receipt) as reader:
        with pytest.raises(LayerCodecError, match="window SHA-256"):
            reader.audit()


def test_reader_rejects_recipe_hash_rebinding(tmp_path: Path) -> None:
    receipt, _documents_value = _write_base(tmp_path)
    rebound = replace(receipt, search_view_recipe_hash="0" * 64)
    with pytest.raises(ValueError, match="SearchViewRecipe"):
        PostingLayerReader(rebound)


def test_control_character_token_round_trips_canonical_jsonl(tmp_path: Path) -> None:
    document = _documents()[0]
    token = "\x00" * 1024
    receipt = write_posting_layer(
        tmp_path / "control-token",
        documents=(document,),
        postings=(LayerPosting(token, 0, 0, 1, 0, 0),),
        layer_kind="base",
    )
    with PostingLayerReader(receipt) as reader:
        assert [row.token for row in reader.iter_token(token)] == [token]


def test_large_partition_uses_bounded_physical_reads_not_per_varint_syscalls(
    tmp_path: Path,
) -> None:
    key = "note:large"
    chunk_count = 2000
    document = LayerDocument(
        key,
        make_doc_uid(key),
        _digest("large-segment"),
        tuple(ChunkMetric(index, 1, 0, 0) for index in range(chunk_count)),
    )
    rows = tuple(
        LayerPosting("shared", 0, index, 1, 0, 0)
        for index in range(chunk_count)
    )
    receipt = write_posting_layer(
        tmp_path / "large-partition",
        documents=(document,),
        postings=rows,
        layer_kind="base",
    )
    reads: list[tuple[str, int, int]] = []
    with PostingLayerReader(receipt) as reader:
        reader.read_observer = lambda name, offset, size: reads.append(
            (name, offset, size)
        )
        assert len(list(reader.iter_token("shared", include_body=False))) == chunk_count
    posting_reads = [item for item in reads if item[0] == "postings.piv"]
    assert len(posting_reads) < 20
    assert any(size > 1 for _name, _offset, size in posting_reads)


def test_deep_audit_accepts_writer_output(tmp_path: Path) -> None:
    receipt, _documents_value = _write_base(tmp_path)
    with PostingLayerReader(receipt) as reader:
        reader.audit()

def test_high_df_group_checks_cancellation_before_consuming_every_row(
    tmp_path: Path,
) -> None:
    class Cancelled(RuntimeError):
        pass

    row_count = 9000
    document = LayerDocument(
        doc_key="note:high-df",
        doc_uid=make_doc_uid("note:high-df"),
        segment_hash=_digest("high-df-segment"),
        chunk_metrics=tuple(
            ChunkMetric(local_id, 1, 0, 0)
            for local_id in range(row_count)
        ),
    )
    yielded = 0

    def postings():
        nonlocal yielded
        for local_id in range(row_count):
            yielded += 1
            yield LayerPosting("shared", 0, local_id, 1, 0, 0)

    def check_cancelled() -> None:
        if yielded >= 8193:
            raise Cancelled("cancelled inside one high-DF token")

    target = tmp_path / "cancelled-layer"
    with pytest.raises(Cancelled, match="high-DF token"):
        write_posting_layer(
            target,
            documents=(document,),
            postings=postings(),
            layer_kind="base",
            check_cancelled=check_cancelled,
        )

    assert 8193 <= yielded < row_count
    assert not target.exists()
