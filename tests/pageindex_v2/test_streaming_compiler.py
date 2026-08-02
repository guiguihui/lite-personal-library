"""Byte-level oracle contract for the bounded-memory schema-3 compiler."""

from __future__ import annotations

import copy
import gc
import hashlib
import json
import weakref
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pytest

import app.index.v2.streaming_compiler as streaming_compiler_module
from app.index.v2.canonical import canonical_bytes, canonical_hash
from app.index.v2.compiler import CompiledGeneration, compile_generation
from app.index.v2.models import CompilerRecipe, SegmentRecipe
from app.index.v2.object_store import put_segment
from app.index.v2.validator import (
    materialize_candidate,
    validate_candidate_normal,
)
from app.retrieval.tokenizer import tokenize

try:
    from app.index.v2.compiler import compile_generation_to_candidate
except ImportError:  # Task 5 supplies the streaming entry point.
    compile_generation_to_candidate = None


@dataclass(frozen=True, slots=True)
class StreamingOracleCase:
    """Rich deterministic Segment inputs shared by all streaming comparisons."""

    segments: tuple[dict[str, object], ...]
    recipe: CompilerRecipe


@dataclass(frozen=True, slots=True)
class GenerationOracle:
    """Legacy compiler output captured as an exact directory byte map."""

    compiled: CompiledGeneration
    directory: Path
    files: Mapping[str, bytes]


def _tf(text: str) -> Counter[str]:
    return Counter(tokenize(text))


def _source_relative(doc_type: str, slug: str) -> str:
    if doc_type == "note":
        return f"notes/{slug}.md"
    return f"{doc_type}s/{slug}/_index.md"


def _segment(
    doc_type: str,
    slug: str,
    *,
    document_title: str,
    chunks: Sequence[tuple[str, Sequence[str], str]],
) -> dict[str, object]:
    """Build one validator-compatible Segment from explicit field text."""

    doc_key = f"{doc_type}:{slug}"
    source_relative = _source_relative(doc_type, slug)
    source_digest = hashlib.sha256(
        f"source:{doc_key}:Unicode:流式编译".encode("utf-8")
    ).hexdigest()
    source_files = [{"path": source_relative, "sha256": source_digest}]
    segment_recipe = SegmentRecipe().as_dict()

    nodes: list[dict[str, object]] = []
    chunk_values: list[dict[str, object]] = []
    postings: dict[str, list[list[int]]] = {}
    tree_nodes: list[dict[str, object]] = []
    for local_id, (title, breadcrumb_value, body) in enumerate(chunks):
        breadcrumb = list(breadcrumb_value)
        node_key = f"n_{canonical_hash([doc_key, local_id])[:24]}"
        legacy_node_id = f"{local_id + 1:04d}"
        line_num = local_id * 10 + 1
        nodes.append(
            {
                "node_key": node_key,
                "legacy_node_id": legacy_node_id,
                "title": title,
                "breadcrumb": breadcrumb,
                "url": "",
                "terms": [],
                "summary": "" if local_id else f"摘要 {document_title}",
                "source_md": source_relative,
                "line_num": line_num,
                "line_end": line_num + 3,
            }
        )
        title_tf = _tf(title)
        breadcrumb_tf = _tf(" ".join(breadcrumb))
        body_tf = _tf(body)
        chunk_values.append(
            {
                "local_id": local_id,
                "node_key": node_key,
                "legacy_node_id": legacy_node_id,
                "node_local_ordinal": 0,
                "title": title,
                "breadcrumb": breadcrumb,
                "body": body,
                "source_md": source_relative,
                "line_num": line_num,
                "line_end": line_num + 3,
                "lengths": {
                    "title": sum(title_tf.values()),
                    "breadcrumb": sum(breadcrumb_tf.values()),
                    "body": sum(body_tf.values()),
                },
            }
        )
        for token in sorted(set(title_tf) | set(breadcrumb_tf) | set(body_tf)):
            postings.setdefault(token, []).append(
                [
                    local_id,
                    int(title_tf.get(token, 0)),
                    int(breadcrumb_tf.get(token, 0)),
                    int(body_tf.get(token, 0)),
                ]
            )
        tree_nodes.append(
            {
                "node_id": legacy_node_id,
                "title": title,
                "summary": "",
                "source_md": source_relative,
                "line_num": line_num,
                "line_end": line_num + 3,
                "nodes": [],
            }
        )

    document: dict[str, object] = {
        "doc_key": doc_key,
        "id": slug,
        "type": doc_type,
        "title": document_title,
        "author": "",
        "description": "",
        "tags": [],
    }
    if doc_type == "paper":
        document["year"] = ""
    elif doc_type == "note":
        document.update({"date": "", "source_type": "", "source_title": ""})

    return {
        "schema_version": 2,
        "segment_recipe": segment_recipe,
        "document": document,
        "fingerprint": {
            "content_hash": canonical_hash(source_files),
            "recipe_hash": canonical_hash(segment_recipe),
            "source_files": source_files,
        },
        "nodes": nodes,
        "chunks": chunk_values,
        "postings": {token: postings[token] for token in sorted(postings)},
        "document_tree": {
            "doc_name": slug,
            "type": doc_type,
            "title": document_title,
            "author": "",
            "description": "",
            "structure": tree_nodes,
        },
    }


@pytest.fixture
def streaming_oracle_case() -> StreamingOracleCase:
    """Three documents covering Unicode, field policy, and DF boundaries."""

    # Every chunk contains ``extremebodytoken`` (DF 6/6). The first five also
    # contain ``boundarybodytoken`` (DF 5/6). Only the first two chunks give the
    # extreme token a non-body contribution, proving title/breadcrumb survive
    # body pruning.
    raw_chunks = [
        (
            "titleonlytoken extremebodytoken 流式标题",
            ["Alpha", "章节一"],
            "extremebodytoken boundarybodytoken bodyonlytoken café alphaone",
        ),
        (
            "普通标题",
            ["breadcrumbonlytoken", "extremebodytoken", "路径"],
            "extremebodytoken boundarybodytoken alphatwo",
        ),
        (
            "βeta heading",
            ["Paper", "Unicode"],
            "extremebodytoken boundarybodytoken paperone naïve",
        ),
        (
            "Second paper node",
            ["Paper", "Details"],
            "extremebodytoken boundarybodytoken papertwo",
        ),
        (
            "札记节点一",
            ["札记", "第一节"],
            "extremebodytoken boundarybodytoken noteone 中文正文",
        ),
        (
            "札记节点二",
            ["札记", "第二节"],
            "extremebodytoken notetwo emoji🙂",
        ),
    ]
    segments = (
        _segment(
            "note",
            "札记",
            document_title="Unicode 札记",
            chunks=raw_chunks[4:],
        ),
        _segment(
            "book",
            "alpha",
            document_title="Alpha 流式编译",
            chunks=raw_chunks[:2],
        ),
        _segment(
            "paper",
            "βeta",
            document_title="βeta Paper",
            chunks=raw_chunks[2:4],
        ),
    )
    return StreamingOracleCase(
        segments=segments,
        recipe=CompilerRecipe(body_df_min=6, body_df_ratio=1.0),
    )


def _directory_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda value: value.as_posix())
        if path.is_file()
    }


def _materialize_oracle(
    directory: Path,
    segments: Sequence[Mapping[str, object]],
    recipe: CompilerRecipe,
) -> GenerationOracle:
    compiled = compile_generation(tuple(segments), recipe)
    materialize_candidate(directory, compiled)
    return GenerationOracle(
        compiled=compiled,
        directory=directory,
        files=_directory_bytes(directory),
    )


def assert_candidate_matches_oracle(
    candidate_dir: Path,
    oracle: GenerationOracle,
) -> None:
    """Compare complete file sets and raw bytes, with useful path diagnostics."""

    actual = _directory_bytes(candidate_dir)
    assert set(actual) == set(oracle.files)
    for relative in sorted(oracle.files):
        assert actual[relative] == oracle.files[relative], relative


def test_legacy_compiler_records_rich_schema3_oracle_bytes(
    tmp_path: Path,
    streaming_oracle_case: StreamingOracleCase,
) -> None:
    oracle = _materialize_oracle(
        tmp_path / "oracle",
        streaming_oracle_case.segments,
        streaming_oracle_case.recipe,
    )

    assert set(oracle.files) == {
        "books/alpha.json",
        "chunks.json",
        "global-index.json",
        "input-proof.json",
        "inverted-index.json",
        "manifest.json",
        "node-index.json",
        "notes/札记.json",
        "papers/βeta.json",
    }
    for relative, raw in oracle.files.items():
        assert raw == canonical_bytes(json.loads(raw.decode("utf-8"))), relative

    joined = b"".join(oracle.files.values())
    assert "流式编译".encode("utf-8") in joined
    assert "札记".encode("utf-8") in joined

    inverted = oracle.compiled.payloads["inverted-index.json"]
    postings = inverted["postings"]
    assert postings["titleonlytoken"] == [[1, 1]]
    assert postings["breadcrumbonlytoken"] == [[2, 1]]
    assert postings["bodyonlytoken"] == [[1, 1]]
    assert postings["extremebodytoken"] == [[1, 1], [2, 1]]
    assert "boundarybodytoken" in postings
    assert oracle.compiled.manifest["pruning"]["body_tokens_pruned"] == 1
    assert oracle.compiled.manifest["pruning"]["body_postings_pruned"] == 6


@pytest.mark.parametrize(
    ("recipe", "extreme_kept", "boundary_kept"),
    [
        (CompilerRecipe(body_df_min=6, body_df_ratio=1.0), False, True),
        (CompilerRecipe(body_df_min=7, body_df_ratio=1.0), True, True),
        (CompilerRecipe(body_df_min=5, body_df_ratio=5 / 6), False, False),
        (CompilerRecipe(body_df_min=5, body_df_ratio=0.84), False, True),
    ],
)
def test_oracle_locks_body_df_threshold_and_field_survival(
    streaming_oracle_case: StreamingOracleCase,
    recipe: CompilerRecipe,
    extreme_kept: bool,
    boundary_kept: bool,
) -> None:
    compiled = compile_generation(streaming_oracle_case.segments, recipe)
    postings = compiled.payloads["inverted-index.json"]["postings"]

    # The extreme token is always retained by title/breadcrumb even when its
    # body contribution is pruned. Its row count reveals which policy fired.
    assert (len(postings["extremebodytoken"]) == 6) is extreme_kept
    assert ("boundarybodytoken" in postings) is boundary_kept
    if recipe.body_df_min == 6:
        assert postings["extremebodytoken"] == [[1, 1], [2, 1]]


@pytest.mark.skipif(
    compile_generation_to_candidate is None,
    reason="compile_generation_to_candidate is introduced by P2 Task 5",
)
@pytest.mark.parametrize(
    "recipe",
    [
        CompilerRecipe(body_df_min=6, body_df_ratio=1.0),
        CompilerRecipe(body_df_min=7, body_df_ratio=1.0),
        CompilerRecipe(body_df_min=5, body_df_ratio=5 / 6),
        CompilerRecipe(body_df_min=5, body_df_ratio=0.84),
    ],
)
@pytest.mark.parametrize("reverse_refs", [False, True])
def test_streaming_candidate_is_byte_for_byte_oracle(
    tmp_path: Path,
    streaming_oracle_case: StreamingOracleCase,
    recipe: CompilerRecipe,
    reverse_refs: bool,
) -> None:
    pageindex = tmp_path / "pageindex"
    refs = [
        put_segment(pageindex, segment)
        for segment in streaming_oracle_case.segments
    ]
    if reverse_refs:
        refs.reverse()
    oracle = _materialize_oracle(
        tmp_path / "oracle",
        streaming_oracle_case.segments,
        recipe,
    )
    candidate = tmp_path / "candidate"

    assert compile_generation_to_candidate is not None
    receipt = compile_generation_to_candidate(
        refs,
        pageindex,
        candidate,
        recipe,
        max_run_bytes=256,
        merge_fan_in=2,
    )

    assert receipt.generation_id == oracle.compiled.generation_id
    assert_candidate_matches_oracle(candidate, oracle)

class _WeakDict(dict):
    """Weak-referenceable mapping used to observe decoded Segment lifetime."""


class _WeakList(list):
    """Weak-referenceable list used to observe decoded Segment lifetime."""


def _weak_container_tree(value: object) -> object:
    if isinstance(value, dict):
        return _WeakDict(
            (key, _weak_container_tree(child))
            for key, child in value.items()
        )
    if isinstance(value, list):
        return _WeakList(_weak_container_tree(child) for child in value)
    return value


def _container_refs(value: object) -> list[weakref.ReferenceType[object]]:
    references: list[weakref.ReferenceType[object]] = []
    if isinstance(value, (_WeakDict, _WeakList)):
        references.append(weakref.ref(value))
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            references.extend(_container_refs(child))
    return references


@pytest.mark.skipif(
    compile_generation_to_candidate is None,
    reason="compile_generation_to_candidate is introduced by P2 Task 5",
)
def test_streaming_compiler_releases_each_decoded_segment_before_next_load(
    tmp_path: Path,
    streaming_oracle_case: StreamingOracleCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    refs = [
        put_segment(pageindex, segment)
        for segment in streaming_oracle_case.segments[:2]
    ]
    real_load = streaming_compiler_module.load_segment
    previous_refs: list[weakref.ReferenceType[object]] = []
    load_calls = 0

    def tracking_load(
        pageindex_dir: Path,
        ref: object,
    ) -> Mapping[str, object]:
        nonlocal load_calls, previous_refs
        gc.collect()
        assert not [
            reference
            for reference in previous_refs
            if reference() is not None
        ], "a decoded Segment child survived until the next Segment load"

        segment = _weak_container_tree(real_load(pageindex_dir, ref))
        assert isinstance(segment, Mapping)
        previous_refs = _container_refs(segment)
        load_calls += 1
        return segment

    monkeypatch.setattr(
        streaming_compiler_module,
        "load_segment",
        tracking_load,
    )

    assert compile_generation_to_candidate is not None
    receipt = compile_generation_to_candidate(
        refs,
        pageindex,
        tmp_path / "candidate",
        streaming_oracle_case.recipe,
        max_run_bytes=256,
        merge_fan_in=2,
    )

    assert load_calls == len(refs)
    assert receipt.invariants["segments_loaded_peak"] == 1


@pytest.mark.skipif(
    compile_generation_to_candidate is None,
    reason="compile_generation_to_candidate is introduced by P2 Task 5",
)
def test_streaming_compiler_preserves_preexisting_nonempty_candidate_on_error(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    sentinel = candidate / "owned-by-user.txt"
    sentinel.write_text("keep", encoding="utf-8")

    assert compile_generation_to_candidate is not None
    with pytest.raises(ValueError, match="not empty"):
        compile_generation_to_candidate(
            [],
            tmp_path / "pageindex",
            candidate,
            CompilerRecipe(),
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(
    compile_generation_to_candidate is None,
    reason="compile_generation_to_candidate is introduced by P2 Task 5",
)
def test_streaming_compiler_preserves_preexisting_empty_candidate_on_error(
    tmp_path: Path,
    streaming_oracle_case: StreamingOracleCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = put_segment(pageindex, streaming_oracle_case.segments[0])
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    def fail_load(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected Segment load failure")

    monkeypatch.setattr(streaming_compiler_module, "load_segment", fail_load)

    assert compile_generation_to_candidate is not None
    with pytest.raises(RuntimeError, match="injected"):
        compile_generation_to_candidate(
            [ref],
            pageindex,
            candidate,
            streaming_oracle_case.recipe,
        )

    assert candidate.is_dir()


@pytest.mark.skipif(
    compile_generation_to_candidate is None,
    reason="compile_generation_to_candidate is introduced by P2 Task 5",
)
def test_streaming_compiler_removes_only_owned_candidate_on_error(
    tmp_path: Path,
    streaming_oracle_case: StreamingOracleCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    ref = put_segment(pageindex, streaming_oracle_case.segments[0])
    candidate = tmp_path / "candidate"

    def fail_load(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected Segment load failure")

    monkeypatch.setattr(streaming_compiler_module, "load_segment", fail_load)

    assert compile_generation_to_candidate is not None
    with pytest.raises(RuntimeError, match="injected"):
        compile_generation_to_candidate(
            [ref],
            pageindex,
            candidate,
            streaming_oracle_case.recipe,
        )

    assert not candidate.exists()


@pytest.mark.skipif(
    compile_generation_to_candidate is None,
    reason="compile_generation_to_candidate is introduced by P2 Task 5",
)
def test_streaming_receipt_passes_normal_validation(
    tmp_path: Path,
    streaming_oracle_case: StreamingOracleCase,
) -> None:
    pageindex = tmp_path / "pageindex"
    refs = [
        put_segment(pageindex, segment)
        for segment in streaming_oracle_case.segments
    ]

    assert compile_generation_to_candidate is not None
    receipt = compile_generation_to_candidate(
        refs,
        pageindex,
        tmp_path / "candidate",
        streaming_oracle_case.recipe,
        max_run_bytes=256,
        merge_fan_in=2,
    )

    report = validate_candidate_normal(receipt, pageindex)

    assert report.ok, report.errors


def test_small_runtime_value_is_written_as_one_canonical_chunk() -> None:
    writes: list[bytes] = []

    class RecordingSink:
        def write(self, payload: bytes) -> int:
            encoded = bytes(payload)
            writes.append(encoded)
            return len(encoded)

    value = {
        "body": "Unicode 中文",
        "breadcrumb": ["A", "B"],
        "chunk_id": "c000001",
    }
    streaming_compiler_module._write_value(RecordingSink(), value)

    assert writes == [canonical_bytes(value)]


@pytest.mark.skipif(
    compile_generation_to_candidate is None,
    reason="compile_generation_to_candidate is introduced by P2 Task 5",
)
def test_inverted_compiler_reads_each_posting_at_most_twice(
    tmp_path: Path,
    streaming_oracle_case: StreamingOracleCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex = tmp_path / "pageindex"
    refs = [
        put_segment(pageindex, segment)
        for segment in streaming_oracle_case.segments
    ]
    records_read = 0
    reader_type = streaming_compiler_module.PostingRunReader

    class CountingReader(reader_type):
        def __next__(self):
            nonlocal records_read
            record = super().__next__()
            records_read += 1
            return record

    monkeypatch.setattr(
        streaming_compiler_module,
        "PostingRunReader",
        CountingReader,
    )

    assert compile_generation_to_candidate is not None
    receipt = compile_generation_to_candidate(
        refs,
        pageindex,
        tmp_path / "candidate",
        streaming_oracle_case.recipe,
        max_run_bytes=256,
        merge_fan_in=2,
    )

    postings = receipt.invariants["postings_visited"]
    assert isinstance(postings, int)
    assert records_read == postings * 2


@pytest.mark.skipif(
    compile_generation_to_candidate is None,
    reason="compile_generation_to_candidate is introduced by P2 Task 5",
)
def test_inverted_output_buffer_is_bounded_and_byte_exact(
    tmp_path: Path,
    streaming_oracle_case: StreamingOracleCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer_limit = 64
    monkeypatch.setattr(
        streaming_compiler_module,
        "_INVERTED_WRITE_BUFFER_BYTES",
        buffer_limit,
        raising=False,
    )
    sink_write = streaming_compiler_module.AtomicHashingSink.write
    inverted_write_sizes: list[int] = []

    def recording_write(self, payload):
        if self.target.name == "inverted-index.json":
            inverted_write_sizes.append(len(payload))
        return sink_write(self, payload)

    monkeypatch.setattr(
        streaming_compiler_module.AtomicHashingSink,
        "write",
        recording_write,
    )
    pageindex = tmp_path / "pageindex"
    refs = [
        put_segment(pageindex, segment)
        for segment in streaming_oracle_case.segments
    ]
    oracle = _materialize_oracle(
        tmp_path / "oracle",
        streaming_oracle_case.segments,
        streaming_oracle_case.recipe,
    )

    assert compile_generation_to_candidate is not None
    receipt = compile_generation_to_candidate(
        refs,
        pageindex,
        tmp_path / "candidate",
        streaming_oracle_case.recipe,
        max_run_bytes=256,
        merge_fan_in=2,
    )

    assert_candidate_matches_oracle(receipt.candidate_dir, oracle)
    peak = receipt.invariants["inverted_write_buffer_peak_bytes"]
    assert isinstance(peak, int)
    assert 0 < peak <= buffer_limit
    assert len(inverted_write_sizes) > 1
    assert max(inverted_write_sizes) <= buffer_limit


def _with_fully_pruned_body_token(
    segments: Sequence[Mapping[str, object]],
    token: str,
) -> tuple[dict[str, object], ...]:
    copied = copy.deepcopy(tuple(segments))
    result: list[dict[str, object]] = []
    for raw_segment in copied:
        assert isinstance(raw_segment, dict)
        chunks = raw_segment["chunks"]
        postings = raw_segment["postings"]
        assert isinstance(chunks, list)
        assert isinstance(postings, dict)
        rows: list[list[int]] = []
        for chunk in chunks:
            assert isinstance(chunk, dict)
            local_id = chunk["local_id"]
            lengths = chunk["lengths"]
            assert isinstance(local_id, int)
            assert isinstance(lengths, dict)
            chunk["body"] = f"{chunk['body']} {token}"
            lengths["body"] = int(lengths["body"]) + 1
            rows.append([local_id, 0, 0, 1])
        postings[token] = rows
        result.append(raw_segment)
    return tuple(result)


@pytest.mark.skipif(
    compile_generation_to_candidate is None,
    reason="compile_generation_to_candidate is introduced by P2 Task 5",
)
def test_fully_body_pruned_token_emits_no_empty_posting_list(
    tmp_path: Path,
    streaming_oracle_case: StreamingOracleCase,
) -> None:
    token = "fullyprunedbodytoken"
    segments = _with_fully_pruned_body_token(
        streaming_oracle_case.segments,
        token,
    )
    recipe = CompilerRecipe(body_df_min=6, body_df_ratio=1.0)
    oracle = _materialize_oracle(tmp_path / "oracle", segments, recipe)
    pageindex = tmp_path / "pageindex"
    refs = [put_segment(pageindex, segment) for segment in segments]

    assert compile_generation_to_candidate is not None
    receipt = compile_generation_to_candidate(
        refs,
        pageindex,
        tmp_path / "candidate",
        recipe,
        max_run_bytes=256,
        merge_fan_in=2,
    )

    assert_candidate_matches_oracle(receipt.candidate_dir, oracle)
    inverted = json.loads(
        (receipt.candidate_dir / "inverted-index.json").read_text(
            encoding="utf-8"
        )
    )
    assert token not in inverted["postings"]
    assert receipt.invariants["pruning"]["body_tokens_pruned"] == 2
    assert receipt.invariants["pruning"]["body_postings_pruned"] == 12
    assert receipt.invariants["pruning"]["body_tf_pruned"] == 12
